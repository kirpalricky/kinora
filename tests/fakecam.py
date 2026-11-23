"""Fake Tapo camera for offline tests. A test double only: it is never pointed at a real device.

What it does:
  * installs a counting fake as the ORIGINAL pyTapo._refreshStok (and Tpap._login) and then calls
    kinora.camera.patch.install(), so the real gate wrapper wraps the fake: every login the gate allows is counted
    here and every login the gate counts is counted there;
  * answers pytapo's transport send (the only request path a login needs) for getDeviceInfo, getUserID, and refuses
    everything else with a plain error code, so pytapo never retries and never re-logs in behind the gate;
  * neutralises pytapo's discovery probes (they open their own connections);
  * refuses every socket connect to a non-loopback address (a guard, recorded in `connects_refused`).

Login behaviour per device (`add_device`): the expected password, or a `login_error` message raised instead
("Error: Invalid authentication", "Error: Temporary Suspension: Try again in N seconds", ...), and a `hold` event a
test can use to keep one login in progress.
"""
import socket
import time
from collections import Counter
from types import SimpleNamespace
from unittest import mock

import pytapo
from pytapo.transport.pytapo.pytapo import pyTapo
from pytapo.transport.tpap.tpap import Tpap

from kinora.camera import patch

LOOPBACK = ("127.0.0.1", "::1", "localhost")
_ACTIVE = None   # the FakeCamera that the installed fakes answer for


class Device:
    def __init__(self, ip, password, model="C200", firmware="1.2.3 Build 000000", mac="AA-BB-CC-11-22-33",
                 hw_ver="1.0"):
        self.ip, self.password, self.model, self.firmware, self.mac = ip, password, model, firmware, mac
        self.hw_ver = hw_ver
        self.login_error = None   # when set: every login raises this (a message, or an exception object) instead
        self.hold = None          # threading.Event: a login waits on it (signalled through `entered`)
        self.entered = None       # threading.Event set when a held login starts


def _login(self_t):
    """Shared login behaviour for both primitives. self_t is the pytapo transport (host, password, stok)."""
    h = _ACTIVE
    ip = self_t.host
    h.login_attempts[ip] += 1
    dev = h.devices.get(ip)
    if dev is None:
        raise Exception("Error: no fake device at this address")
    if dev.hold is not None:
        if dev.entered is not None:
            dev.entered.set()
        dev.hold.wait(30)
    if dev.login_error:
        raise dev.login_error if isinstance(dev.login_error, BaseException) else Exception(dev.login_error)
    if self_t.password != dev.password:
        raise Exception("Error: Invalid authentication")
    self_t.stok = "fake-stok"


def _refresh_stok(self_t, loginRetryCount=0):
    _login(self_t)


def _tpap_login(self_t, *a, **kw):
    _login(self_t)
    self_t.expiresAt = time.time() + 3600   # as the real TPAP login sets it: later requests do not log in again


def _answer(self_t, request):
    """The camera's answer to one request (pytapo's multipleRequest wrapper). Refuses a request sent without a login."""
    if not getattr(self_t, "stok", False):
        raise Exception("fakecam: a request was sent without a login (the gate would not see it)")
    dev = _ACTIVE.devices.get(self_t.host)
    params = request.get("params") if isinstance(request, dict) else None
    reqs = params.get("requests", []) if isinstance(params, dict) else []
    method = reqs[0].get("method") if reqs else request.get("method")
    if method == "getDeviceInfo" and dev is not None:
        result = {"device_info": {"basic_info": {"device_model": dev.model, "device_type": "SMART.IPCAMERA",
                                                 "fw_ver": dev.firmware, "mac": dev.mac, "hw_ver": dev.hw_ver}}}
        inner = {"method": method, "error_code": 0, "result": result}
    elif method == "getUserID":
        inner = {"method": method, "error_code": 0, "result": {"user_id": "fake-user-id"}}
    else:
        inner = {"method": method, "error_code": -40000}   # an error, not -1: pytapo must not re-login on it
    return {"error_code": 0, "result": {"responses": [inner]}}


async def _send(self_t, request, retry=0):
    """pytapo's legacy transport send: the multipleRequest wrapper."""
    return _answer(self_t, request)


def _send_tpap(self_t, request):
    """Tpap._sendSync (the TPAP transport's blocking send, run by Tpap.send): a single call returns its own answer.
    Replaced here so the fake never runs the real encrypted channel or its own re-login."""
    wrapper = _answer(self_t, request)
    if request.get("method") == "multipleRequest":
        return wrapper
    return wrapper["result"]["responses"][0]


class FakeCamera:
    """Install with install(); close() (or the context manager) restores pytapo and the socket layer."""

    def __init__(self):
        self.devices = {}
        self.login_attempts = Counter()      # host -> logins that reached the fake (gate-allowed)
        self.connects_refused = []           # hosts a socket tried to reach outside loopback
        self._restore = []
        self._patches = []

    def add_device(self, ip, password, **kw):
        dev = Device(ip, password, **kw)
        self.devices[ip] = dev
        return dev

    def install(self):
        global _ACTIVE
        _ACTIVE = self
        for owner, name, fake in ((pyTapo, "_refreshStok", _refresh_stok), (Tpap, "_login", _tpap_login),
                                  (pyTapo, "send", _send), (Tpap, "_sendSync", _send_tpap)):
            self._restore.append((owner, name, owner.__dict__.get(name)))
            setattr(owner, name, fake)
        patch.install()   # wraps the fakes with the gate (idempotent for anything already wrapped)
        self._real_connect_ex = socket.socket.connect_ex
        self._patches = [
            mock.patch.object(pytapo, "discover_login", lambda *a, **k: None),   # no discovery probe
            mock.patch.object(pytapo, "rejects_legacy_login", lambda *a, **k: False),
            mock.patch.object(socket.socket, "connect", self._guard_connect(socket.socket.connect)),
            mock.patch.object(socket.socket, "connect_ex", self._guard_connect_ex),
        ]
        for p in self._patches:
            p.start()
        return self

    def close(self):
        global _ACTIVE
        for p in reversed(self._patches):
            p.stop()
        for owner, name, prev in reversed(self._restore):
            if prev is None:
                delattr(owner, name)
            else:
                setattr(owner, name, prev)
        self._restore = []
        self._patches = []
        if _ACTIVE is self:
            _ACTIVE = None

    def __enter__(self):
        return self.install()

    def __exit__(self, *exc):
        self.close()

    def _guard_connect(self, real):
        def guarded(sock, address):
            host = address[0] if isinstance(address, tuple) else address
            if isinstance(host, str) and host in LOOPBACK:
                return real(sock, address)
            self.connects_refused.append(str(host))
            raise OSError("fakecam: connections outside loopback are refused in tests")
        return guarded

    def _guard_connect_ex(self, sock, address):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host in LOOPBACK:
            return self._real_connect_ex(sock, address)
        self.connects_refused.append(str(host))
        return 111   # ECONNREFUSED


class Clock:
    """Replaces the time source of kinora.camera.gate (the login spacing and the 30 min window)."""

    def __init__(self, start=None):
        self.now = time.time() if start is None else start

    def module(self):
        return SimpleNamespace(time=lambda: self.now)

    def advance(self, seconds):
        self.now += seconds
