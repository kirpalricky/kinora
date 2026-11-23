import asyncio
import re
import threading
import time

from pytapo import Tapo

from kinora import config, health
from kinora.errors import BadInput, CameraError, Unavailable
from kinora.camera import gate, patch
from kinora.creds import get_camera_credentials, get_cloud_password_for

patch.install()   # importing this module must never allow a login that bypasses the gate (idempotent)

SESSION_ERRS = ("-40401", "-40421", "-40209", "-60102", "Invalid authentication")
KNOWN_TRANSPORTS = ("pytapo", "tpap")   # the login paths this app uses (KLAP is refused)
_login_locks = {cid: threading.Lock() for cid in config.CAMS}   # serialise client creation
_clients = {}
_born = {}   # camera id -> when its session was created


def login_lock(cid):
    """The camera's login lock (held while a client is created). cameras.py takes it, in a worker thread, to quiesce.
    Created on first use, so a camera that reached the table without register_camera() still has one."""
    return _login_locks.setdefault(cid, threading.Lock())


def drop_client(cid):
    """Forget the camera's cached session (the next request logs in again, if the gate allows)."""
    _clients.pop(cid, None)
    _born.pop(cid, None)


def register_camera(cam):
    """Create the per-camera locks of a camera added at runtime (idempotent)."""
    cid = cam["id"]
    _login_locks.setdefault(cid, threading.Lock())
    _api_locks.setdefault(cid, asyncio.Lock())
    _ctl_locks.setdefault(cid, threading.RLock())


def unregister_camera(cid):
    """Remove a removed camera's locks and cached session."""
    drop_client(cid)
    _login_locks.pop(cid, None)
    _api_locks.pop(cid, None)
    _ctl_locks.pop(cid, None)


VERIFYING = set()   # camera ids inside a Verify run (services/setup.py): the one login a switched-off camera gets


def _usable(cam):
    """Only a camera that is switched on AND verified may log in; its own Verify run is the one exception."""
    return cam["id"] in VERIFYING or bool(cam.get("enabled") and cam.get("verified"))


def check_usable(cam):
    """Refusal for the routes (no Verify exception): a switched-off or unverified camera is not contacted at all."""
    if not (cam.get("enabled") and cam.get("verified")):
        raise BadInput(f"{cam['name']} is switched off or not verified: verify it in Settings first")


def _current(cam):
    """The table's entry, checked against the dict the caller captured. An address, login or device change made while
    this request waited for the login lock is a retry, never a login with the stale settings."""
    cur = config.CAMS.get(cam["id"])
    if cur is None:
        raise BadInput(f"{cam['name']} was removed")
    if any(cur.get(k) != cam.get(k) for k in ("ip", "auth", "device_id")):
        raise Unavailable(f"{cam['name']}: its address or login changed while this request waited: try again")
    return cur


def resolve(cam):
    """(user, camera password, cloud password) for one login, read before any gate step. A credential store that
    cannot be read is a configuration problem (BadInput): it is never counted as a failed login. The camera account
    is read only by cam_cloud: the local API login never uses it (auto and admin_cloud sign in with the cloud password)."""
    try:
        user, pw = get_camera_credentials(cam) if cam["auth"] == "cam_cloud" else (None, None)
        cloud = get_cloud_password_for(cam)
    except RuntimeError as e:
        raise BadInput(str(e)) from None
    return user, pw, cloud


def build(cam, resolved=None):
    user, pw, cloud = resolved if resolved is not None else resolve(cam)
    kw = {"isKLAP": False}   # never pytapo's KLAP path: its login is refused by patch.install()
    tr = gate.st(cam).get("transport")  # known transport: skip some of pytapo's discovery probes
    if tr in KNOWN_TRANSPORTS:   # any other stored value is unknown: ignored, not passed to pytapo
        kw["transportMethod"] = tr
    if cam["auth"] in ("auto", "admin_cloud"):   # auto is this same login (pytapo's own: admin + TP-Link password)
        return Tapo(cam["ip"], "admin", cloud, cloudPassword=cloud, retryStok=False, **kw)
    if cam["auth"] == "cam_cloud":
        return Tapo(cam["ip"], user, pw, cloudPassword=cloud, retryStok=False, **kw)
    raise BadInput(f"{cam['id']} has no auth method configured")


def _refuse_unless_usable(cam):
    if not _usable(cam):
        raise BadInput(f"{cam['name']} is switched off or not verified: verify it in Settings first")


def client(cam):
    """The camera's single long-lived session (runs in a worker thread). A switched-off or unverified camera is refused
    first, before the cached session is looked at: it is never logged in to and never connected."""
    cid = cam["id"]
    _refuse_unless_usable(config.CAMS.get(cid, cam))
    h = health.get(cid)
    if cid in _clients and h and h["state"] == "ready" and h["since"] > _born.get(cid, 0):
        _clients.pop(cid, None)   # the camera went down and came back after this session was made: it is dead
    if cid in _clients:
        return _clients[cid]
    lock = _login_locks.get(cid)
    if lock is None:
        raise BadInput(f"{cam['name']} was removed")
    with lock:
        cur = _current(cam)   # the settings this login will use, read under the lock
        _refuse_unless_usable(cur)
        if cid in _clients:  # someone else logged in while we waited
            return _clients[cid]
        resolved = resolve(cur)   # a credential error stops here: no gate step, nothing counted
        try:
            gate.precheck(cur)
            patch._depth.noted = False
            t = build(cur, resolved)  # the login itself is gated inside pytapo (see _guard_login)
        except CameraError:   # configuration errors (BadInput) are not login failures: never counted below
            raise
        except Exception as e:
            if gate.MARK in str(e):
                raise as_http(e)
            if not getattr(patch._depth, "noted", False):  # failed before reaching a login (e.g. discovery):
                gate.note_login_failure(cur, e)           # still back off, or every request re-probes
            raise CameraError(f"{cur['name']}: login failed: {explain(e, cur.get('auth'))}")
        try:
            t.getUserID()          # prime blocking calls here, not inside the async Downloader
            t.getTimeCorrection()
        except Exception:
            pass                   # keep the good session; the request that needs it will report
        gate.remember_transport(cur, t.transport.method)
        _clients[cid] = t
        _born[cid] = time.time()
        return t
_api_locks = {cid: asyncio.Lock() for cid in config.CAMS}
# One control request at a time per camera, across threads (api() calls and the pytapo Downloader's
# calls via serialised()). Held for one call only, never across a whole download.
_ctl_locks = {cid: threading.RLock() for cid in config.CAMS}


_LOCKED_METHODS = ("getAudioConfig",)   # the only Tapo calls pytapo's Downloader runs in a worker thread


class _Serialised:
    """Proxy over a Tapo object: only _LOCKED_METHODS take the camera's control lock.

    Everything else (other methods, attributes, logger, playerID) is returned unchanged, so calls that
    pytapo's Downloader makes on the event-loop thread (getUserID, getMediaSession) never wait for the lock.
    """

    def __init__(self, cid, t):
        object.__setattr__(self, "_cid", cid)
        object.__setattr__(self, "_t", t)

    def __getattr__(self, name):
        attr = getattr(self._t, name)
        if name not in _LOCKED_METHODS or not callable(attr):
            return attr
        lock = _ctl_locks[self._cid]

        def locked(*a, **kw):
            with lock:
                return attr(*a, **kw)
        return locked


def serialised(cam, t):
    """A view of the Tapo object whose _LOCKED_METHODS take the camera's control lock (not a subclass)."""
    return _Serialised(cam["id"], t)


async def api(cam, fn):
    """Run one blocking pytapo call on the camera's session. No retries, no re-login here."""
    async with _api_locks[cam["id"]]:  # pytapo clients are not thread-safe: one call at a time
        t = await asyncio.to_thread(client, cam)

        def run():
            with _ctl_locks[cam["id"]]:
                return fn(t)
        try:
            return await asyncio.to_thread(run)
        except CameraError:
            raise
        except Exception as e:
            if gate.MARK in str(e):
                raise as_http(e)
            if any(code in str(e) for code in SESSION_ERRS):
                _clients.pop(cam["id"], None)  # next request logs in again, if the gate allows
            raise CameraError(f"{cam['name']}: {explain(e, cam.get('auth'))}")


def explain(e, auth=None):
    """Plain-English reason for a camera/library error (falls back to the raw message). auth: the camera's login method
    (a refused login is the camera account password for cam_cloud, the TP-Link password otherwise)."""
    m = str(e)
    n = re.search(r"Try again in (\d+)", m)
    if n:
        return f"the camera locked itself after too many login attempts (about {int(n.group(1)) // 60 + 1} min left)"
    if "Invalid authentication" in m:
        return gate.CAM_INVALID_TEXT if auth == "cam_cloud" else gate.INVALID_TEXT
    if any(x in m for x in ("Max retries", "Connection refused", "timed out", "Timeout", "No route", "NewConnectionError")):
        return "the camera is not reachable on the network"
    if any(x in m for x in ("-71102", "-71101", "USER_ID_EMPLOYED", "USER_ID_FULL")):
        return "the camera's session slots are in use (close the Tapo app / other viewers, then wait a few minutes)"
    if "-40211" in m:
        return "this camera needs a newer login method than the library is using"
    if "MOTOR_LOCKED_ROTOR" in m or "-64304" in m:
        return "reached the end of its pan/tilt range"
    if "-60102" in m:
        return "the camera reported an internal timeout (-60102); try again shortly"
    return gate.short(e)


def as_http(e):
    """Turn an exception that carries our gate marker into a 503 response."""
    msg = str(e)
    i = msg.find(gate.MARK)
    return Unavailable(msg[i + len(gate.MARK):] if i >= 0 else msg)
