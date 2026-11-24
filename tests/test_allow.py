"""Other computers and the device allowlist (server.local_only, kinora/devices.py): which peers may connect, which routes a
remote peer may call (REMOTE_ROUTES), the Host and Origin rules, and the owner opening the address on this computer.
Offline: a temp data folder (never the real one), no real socket. Routes are resolved through the app's router.
Run: .venv/bin/python -m unittest tests.test_allow   (from the repo root)
"""
import asyncio
import atexit
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_home = tempfile.mkdtemp(prefix="kinora-allow-test-")   # set before kinora is imported: config reads it once
atexit.register(shutil.rmtree, _home, True)
os.environ["KINORA_HOME"] = _home
os.environ["KINORA_NO_INSTANCE_LOCK"] = "1"

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402

import server  # noqa: E402
from kinora import config, devices  # noqa: E402
from kinora.services import devices_api  # noqa: E402

PORT = config.PORT
LAN_PEER = "192.168.1.9"
LAN_OWN = "192.168.1.2"                 # this computer's address in these tests
LAN_HOST = f"{LAN_OWN}:{PORT}"


def transport(peer, sock=None):
    """A transport that reports the peer address and (optionally) the address the connection arrived on."""
    def info(key, default=None):
        if key == "peername":
            return (peer, 1234)
        if key == "sockname" and sock:
            return (sock, PORT)
        return default
    return mock.Mock(get_extra_info=info)


async def ok(request):
    return web.Response(text="ok")


def concrete(canonical):
    """A request path for a route pattern: every {name} becomes x."""
    return re.sub(r"\{[^}]+\}", "x", canonical)


def run(method, path, peer=LAN_PEER, host=LAN_HOST, sock=None, headers=None, handler=ok):
    """Pass one request through server.local_only (after routing, as aiohttp does). Returns (status, text, response)."""
    app = server.make_app(background=False)
    h = {"Host": host or "", **(headers or {})}
    if method not in ("GET", "HEAD"):
        h.setdefault("Origin", f"http://{host}")
        h.setdefault("Content-Type", "application/json")
    req = make_mocked_request(method, path, headers=h, transport=transport(peer, sock), app=app)
    req._match_info = asyncio.run(app.router.resolve(req))

    async def go():
        try:
            return await server.local_only(req, handler)
        except web.HTTPException as e:
            return e
    resp = asyncio.run(go())
    return resp.status, resp.text, resp


def allowed(*ips):
    return mock.patch.object(config, "ALLOW", config.parse_allow(",".join(ips)))


class Allow(unittest.TestCase):
    def setUp(self):
        devices._pending.clear()
        devices._cache["data"] = None
        p = mock.patch.object(config, "ALLOW", [])
        p.start()
        self.addCleanup(p.stop)
        devices.set_sharing(True)   # sharing on: the peer checks below are the ones that count

    def test_parse(self):
        self.assertEqual([str(n) for n in config.parse_allow("10.0.0.5, 192.168.1.0/24")], ["10.0.0.5/32", "192.168.1.0/24"])
        with self.assertRaises(ValueError):
            config.parse_allow("not-an-ip")

    def test_loopback_always_ok(self):
        self.assertEqual(run("GET", "/api/cameras", peer="127.0.0.1", host=f"127.0.0.1:{PORT}")[0], 200)
        self.assertEqual(run("GET", "/api/setup/state", peer="127.0.0.1", host=f"127.0.0.1:{PORT}")[0], 200)

    def test_unlisted_device_refused(self):
        self.assertEqual(run("GET", "/api/cameras", peer="192.168.2.9")[0], 403)
        with allowed(LAN_PEER):
            self.assertEqual(run("GET", "/api/cameras", peer="192.168.2.9")[0], 403)

    def test_listed_device_ok(self):
        with allowed(LAN_PEER):
            self.assertEqual(run("GET", "/api/cameras")[0], 200)

    def test_listed_device_cannot_change_settings(self):
        with allowed(LAN_PEER):
            status, text, _ = run("GET", "/api/setup/state")
            self.assertEqual(status, 403)
            self.assertIn("settings can only be changed", text)
            self.assertEqual(run("PATCH", "/api/cameras/x")[0], 403)
            self.assertEqual(run("PUT", "/api/cameras/order")[0], 403)

    def test_rebinding_name_refused(self):
        with allowed(LAN_PEER):
            self.assertEqual(run("GET", "/api/cameras", host=f"evil.example.com:{PORT}")[0], 403)
            self.assertEqual(run("GET", "/api/cameras", host=f"{LAN_OWN}:1")[0], 403)

    def test_origin_must_match_host(self):
        with allowed(LAN_PEER):
            self.assertEqual(run("POST", "/api/ptz", headers={"Origin": f"http://{LAN_HOST}"})[0], 200)
            self.assertEqual(run("POST", "/api/ptz", headers={"Origin": "http://evil.com"})[0], 403)

    def test_remote_origin_is_its_own_host_only(self):
        with allowed(LAN_PEER):
            self.assertEqual(run("POST", "/api/ptz", headers={"Origin": f"http://localhost:{PORT}"})[0], 403)
            self.assertEqual(run("POST", "/api/ptz", headers={"Origin": f"http://127.0.0.1:{PORT}"})[0], 403)


class Routes(unittest.TestCase):
    def setUp(self):
        devices._pending.clear()
        devices._cache["data"] = None
        p = mock.patch.object(config, "ALLOW", [])
        p.start()
        self.addCleanup(p.stop)
        devices.set_sharing(True)

    def test_every_route_is_classified(self):
        app = server.make_app(background=False)
        pairs = {(r.method, r.resource.canonical) for r in app.router.routes() if r.resource is not None and r.method != "HEAD"}
        remote, local = server.REMOTE_ROUTES, server.LOCAL_ONLY_ROUTES
        self.assertEqual(remote & local, set(), "a route is in both sets")
        unclassified = pairs - remote - local
        self.assertEqual(unclassified, set(), "add each new route to REMOTE_ROUTES or LOCAL_ONLY_ROUTES in server.py")
        self.assertEqual(remote - pairs, set(), "REMOTE_ROUTES names a route that does not exist")
        self.assertEqual(local - pairs, set(), "LOCAL_ONLY_ROUTES names a route that does not exist")

    def test_remote_allowlist_reaches_the_handler(self):
        with allowed(LAN_PEER):
            for method, canonical in sorted(server.REMOTE_ROUTES):
                status, text, _ = run(method, concrete(canonical))
                self.assertEqual(status, 200, f"{method} {canonical}: {text}")

    def test_other_routes_refused_to_remote(self):
        with allowed(LAN_PEER):
            for method, canonical in sorted(server.LOCAL_ONLY_ROUTES):
                self.assertEqual(run(method, concrete(canonical))[0], 403, f"{method} {canonical}")

    def test_unmatched_head_and_encoded_paths(self):
        with allowed(LAN_PEER):
            self.assertEqual(run("GET", "/api/nothing-here")[0], 403)
            self.assertEqual(run("HEAD", "/api/cameras")[0], 403)
            self.assertEqual(run("GET", "/api/%73etup/state")[0], 403)          # %73 = s, so /api/setup/state
            self.assertEqual(run("GET", "/api/setup/%64evices")[0], 403)        # %64 = d
            self.assertEqual(run("GET", "/api/cam%65ras")[0], 200)              # %65 = e: really /api/cameras

    def test_sharing_off_refuses_and_closes(self):
        # a stored device, sharing off: the command line (which would turn sharing on) is not involved
        devices.add(LAN_PEER)
        self.addCleanup(devices.remove, LAN_PEER)
        devices.set_sharing(False)
        status, text, resp = run("GET", "/api/cameras")
        self.assertEqual(status, 403)
        self.assertIn("sharing is off", text)
        self.assertFalse(resp.keep_alive, "a refused connection must not be kept alive")

    def test_refused_device_is_recorded_only_for_an_address_host(self):
        self.assertEqual(run("GET", "/api/cameras", host=f"evil.example.com:{PORT}")[0], 403)
        self.assertNotIn(LAN_PEER, [i["ip"] for i in devices.pending()])
        self.assertEqual(run("GET", "/api/cameras", host=LAN_HOST)[0], 403)
        self.assertIn(LAN_PEER, [i["ip"] for i in devices.pending()])

    def test_request_without_a_peer_address_is_refused(self):
        with allowed(LAN_PEER):
            status, _, _ = run("GET", "/api/cameras", peer=None)
        self.assertEqual(status, 403)
        self.assertEqual(devices.pending(), [])


class OwnerAddress(unittest.TestCase):
    def setUp(self):
        devices._cache["data"] = None
        p = mock.patch.object(config, "ALLOW", [])
        p.start()
        self.addCleanup(p.stop)
        devices.set_sharing(True)

    def test_owner_opening_the_shown_address_gets_the_full_app(self):
        # the peer is this computer's own address (the connection arrived on it): local, so settings work
        self.assertEqual(run("GET", "/api/setup/state", peer=LAN_OWN, sock=LAN_OWN)[0], 200)
        self.assertEqual(run("POST", "/api/setup/sharing", peer=LAN_OWN, sock=LAN_OWN)[0], 200)

    def test_another_computer_is_still_remote(self):
        status, _, _ = run("GET", "/api/setup/state", peer=LAN_PEER, sock=LAN_OWN)
        self.assertEqual(status, 403)


class HostRules(unittest.TestCase):
    def test_host_ok(self):
        self.assertTrue(server._host_ok(f"[fe80::1]:{PORT}", True))          # IPv6 literal with our port
        self.assertFalse(server._host_ok("[fe80::1]", True))                  # IPv6 literal, no port
        self.assertFalse(server._host_ok(LAN_OWN, True))                      # missing port
        self.assertFalse(server._host_ok(f"unknown.example:{PORT}", True))    # not a machine name
        self.assertFalse(server._host_ok(None, True))
        self.assertFalse(server._host_ok(LAN_HOST, False))                    # loopback clients: localhost / 127.0.0.1 only
        self.assertTrue(server._host_ok(f"127.0.0.1:{PORT}", False))
        self.assertTrue(server._host_ok(f"localhost:{PORT}", False))

    def test_machine_names(self):
        self.assertTrue(server.MACHINE_NAMES)
        for name in server.MACHINE_NAMES:
            self.assertTrue(server._host_ok(f"{name}:{PORT}", True), name)
        self.assertFalse(server._lan_host(f"{next(iter(server.MACHINE_NAMES))}:1"))


if __name__ == "__main__":
    unittest.main()
