"""Login gate: persisted back-off and login budget per camera (moved verbatim from server.py).

Every reload -> check -> save sequence runs under two locks: the thread lock `_state_lock` and an
OS file lock on `<META>/breaker.lock`, so two processes (server, probe) cannot double the budget.

CLI (no camera contact):  python -m kinora.camera.gate credentials-changed <cam id>
"""
import json
import math
import os
import re
import sys
import threading
import time
from contextlib import contextmanager

from kinora import config, health

# ---- login gate (persisted) -------------------------------------------------------------
MIN_LOGIN_GAP = 180        # seconds between logins to one camera
MAX_LOGINS_PER_HOUR = 4
SAFETY_MARGIN = 120        # extra seconds added to a camera-reported suspension
GENERIC_BACKOFF = 300      # after a failed login that is not a suspension / bad credentials
AUTH_RETRY_BLOCK = 3600    # "Invalid authentication" from a camera that has logged in before
FAIL_WINDOW = 1800         # seconds: failures inside this window count in recent_fails
FAIL_WINDOW_BLOCK = 1920   # block after 2 failures inside FAIL_WINDOW, whatever the user did since
BREAKER_F = config.META / "breaker.json"
MARK = "[gate] "
# CLI for logs and docs only: python -m kinora.camera.gate credentials-changed <cam id> [--reset-auth]
# user-facing text (the gate's reason and the session's explanation): never a CLI command
INVALID_TEXT = ("The camera refused the TP-Link password. Check it (it must be the account that owns this camera), "
                "then Verify once after the wait shown.")
RELOGIN_TEXT = ("The camera changed how it signs in. Save the TP-Link password again in Settings, then Verify.")
RELOGIN_CAM_TEXT = ("The camera changed how it signs in. Save the camera account again in Settings, then Verify.")
CAM_INVALID_TEXT = ("The camera refused the camera account password. Check it in Settings, then Verify once after the wait "
                    "shown. To sign in with the TP-Link account instead, choose Use automatic sign-in in Settings.")


class GateRefused(Exception):
    """A login was refused locally (never reached the camera)."""


_state_lock = threading.Lock()


def gate_key(cam):
    """Key of a camera's gate state: a stable device_id when cameras.json has one, else the camera id.
    Keyed this way, changing a camera's IP keeps its budget and two cameras swapping IPs do not swap state.
    The device_id is normalised (trimmed, lower case) so the same device always maps to the same state."""
    dev = cam.get("device_id")
    if dev:
        return str(dev).strip().lower()
    return cam["id"]


def _blank():
    return {"blocked_until": 0, "logins": [], "disabled": None, "fails": 0}


def _load_state(extra=()):
    """`extra`: cameras added at runtime (kinora/cameras.py) that a corrupt file must also block."""
    try:
        return json.loads(BREAKER_F.read_text())
    except FileNotFoundError:
        return {}
    except Exception:  # corrupt file: fail closed, do not silently reset the budget
        return {gate_key(cam): {**_blank(), "blocked_until": time.time() + 3600}
                for cam in [*config.CAMS.values(), *extra]}
_state = _load_state()


from kinora.oslock import os_lock as _os_lock, os_unlock as _os_unlock  # noqa: E402,F401  (kept under the old names)


@contextmanager
def _gate_lock():
    """Thread lock, then the cross-process file lock (<META>/breaker.lock), for one gate state change.
    Both are released on exceptions."""
    with _state_lock:
        path = BREAKER_F.parent / "breaker.lock"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            f = open(path, "a+b")
        except OSError as e:   # fail closed, with a readable reason instead of a raw OSError
            raise GateRefused(f"{MARK}the login gate lock {path} cannot be opened ({e.strerror or e}). "
                              "No login attempted: check the data directory's permissions.") from None
        try:
            _os_lock(f)
            try:
                yield
            finally:
                _os_unlock(f)
        finally:
            f.close()


def _merge(a, b):
    """Two states of one device (id-keyed and device_id-keyed) as one, the strictest of both."""
    merged = {**b, **a}
    merged["blocked_until"] = max(a.get("blocked_until", 0), b.get("blocked_until", 0))
    merged["logins"] = sorted(set(a.get("logins", [])) | set(b.get("logins", [])))
    merged["disabled"] = a.get("disabled") or b.get("disabled")
    merged["fails"] = max(a.get("fails", 0), b.get("fails", 0))
    merged["live_disabled"] = a.get("live_disabled") or b.get("live_disabled")
    merged["recent_fails"] = sorted(set(a.get("recent_fails", [])) | set(b.get("recent_fails", [])))
    if a.get("transport") or b.get("transport"):
        merged["transport"] = a.get("transport") or b.get("transport")
    return merged


def _adopt(cam):
    """A device_id added to a camera that already had state under its camera id: move that state to the
    device_id key (or merge it into an existing device_id entry). In memory only: _reload() saves it.
    Returns True when _state changed."""
    key, legacy = gate_key(cam), cam["id"]
    if key == legacy or legacy not in _state:
        return False
    old = _state.pop(legacy)
    _state[key] = _merge(_state[key], old) if key in _state else old
    return True


def _reload(cams=()):
    """Re-read the state file (another process, e.g. probe.py, may have logged in). Call under _gate_lock().
    Adopts id-keyed state of the configured cameras and of `cams` (a device_id added later), and saves if it changed."""
    fresh = _load_state(cams)
    _state.clear()
    _state.update(fresh)
    changed = False
    for cam in [*config.CAMS.values(), *cams]:
        changed = _adopt(cam) or changed
    if changed:
        _save()


def st(cam):
    return _state.setdefault(gate_key(cam), _blank())


def _save():
    tmp = BREAKER_F.with_suffix(".tmp")
    tmp.write_text(json.dumps(_state))
    os.replace(tmp, BREAKER_F)  # atomic


def short(e):
    return re.sub(r"\s+", " ", str(e))[:200]


def _check(cam, now):
    if health.unreachable(cam["id"]):
        h = health.get(cam["id"])
        raise GateRefused(f"{MARK}{cam['name']} is not answering ({h['state']}): its API port is closed, "
                          "probably rebooting. No login attempted.")
    s = st(cam)
    if s["disabled"]:
        raise GateRefused(f"{MARK}{cam['name']} is disabled: {s['disabled']}")
    if now < s["blocked_until"]:
        raise GateRefused(f"{MARK}{cam['name']}: waiting {int(s['blocked_until'] - now)} s before the next login")


def _check_rate(cam, now, s):
    if s["logins"] and now - s["logins"][-1] < MIN_LOGIN_GAP:
        raise GateRefused(f"{MARK}{cam['name']}: last login was {int(now - s['logins'][-1])} s ago, "
                          f"waiting {int(MIN_LOGIN_GAP - (now - s['logins'][-1]))} s")
    if len(s["logins"]) >= MAX_LOGINS_PER_HOUR:
        raise GateRefused(f"{MARK}{cam['name']}: login budget for this hour used up")


def precheck(cam):
    """Before creating a client (pytapo sends discovery probes at construction, ahead of any
    login): refuse if blocked, disabled, too soon after the last login, or over budget."""
    with _gate_lock():
        now = time.time()   # read under the lock: another process may have logged in after we waited
        _reload([cam])
        s = st(cam)
        s["logins"] = [t for t in s["logins"] if now - t < 3600]
        _check(cam, now)
        _check_rate(cam, now, s)


def gate(cam, count_only=False):
    """Allow (and count) one login attempt, or raise GateRefused."""
    with _gate_lock():
        now = time.time()   # under the lock, so timestamps are monotonic across processes
        _reload([cam])
        s = st(cam)
        s["logins"] = [t for t in s["logins"] if now - t < 3600]
        _check(cam, now)
        if not count_only:
            _check_rate(cam, now, s)
        s["logins"].append(now)  # count the attempt before making it
        _save()


def snapshot(cam):
    """Read-only view of one camera's gate state for the setup API (reloads the state file under the lock).
    wait_s: seconds until the gate would allow the next login (0: allowed now, or refused for another reason).
    disabled / live_disabled: the persisted reasons, or None. Never contacts a camera."""
    with _gate_lock():
        now = time.time()
        _reload([cam])
        s = st(cam)
        logins = [t for t in s["logins"] if now - t < 3600]
        waits = [s["blocked_until"] - now]
        if logins:
            waits.append(MIN_LOGIN_GAP - (now - logins[-1]))
        recent = [t for t in s.get("recent_fails", []) if now - t < FAIL_WINDOW]
        return {"disabled": s["disabled"], "live_disabled": s.get("live_disabled"),
                "wait_s": max(0, math.ceil(max(waits))), "recent_fails": len(recent)}


def _tpap_conflict(s, e, msg):
    """A stored TPAP login refused at pake_register with a code other than the passcode refusal (-40401)."""
    return (s.get("transport") == "tpap" and type(e).__name__ == "TpapError" and "pake_register" in msg
            and "-40401" not in msg)


def note_login_failure(cam, e):
    msg = str(e)
    if MARK in msg:  # refused locally, nothing happened on the camera
        return
    with _gate_lock():
        now = time.time()
        _reload([cam])
        s = st(cam)
        s["fails"] = s.get("fails", 0) + 1
        # persisted failure times: credentials_changed never clears them, so a typo cycle cannot restart the count
        s["recent_fails"] = [t for t in s.get("recent_fails", []) if now - t < FAIL_WINDOW] + [now]
        m = re.search(r"Try again in (\d+) seconds", msg)
        if m:
            s["blocked_until"] = now + int(m.group(1)) + SAFETY_MARGIN
        elif "Invalid authentication" in msg:
            if s.get("transport"):  # has logged in before: pytapo also uses this text as a catch-all
                s["blocked_until"] = now + AUTH_RETRY_BLOCK
            else:   # a cam_cloud camera's refusal is its camera account password, not the TP-Link password
                s["disabled"] = CAM_INVALID_TEXT if cam.get("auth") == "cam_cloud" else INVALID_TEXT
        else:
            s["blocked_until"] = now + GENERIC_BACKOFF
        # a cam_cloud camera signs in with its camera account, so its advice names that password, not the TP-Link one
        relogin = RELOGIN_CAM_TEXT if cam.get("auth") == "cam_cloud" else RELOGIN_TEXT
        if "-40211" in msg or "-40211" in str(getattr(e, "__cause__", "")):
            s.pop("transport", None)
            s["disabled"] = relogin
        elif _tpap_conflict(s, e, msg):   # the stored TPAP login no longer works: forget it, the next Verify re-discovers
            s.pop("transport", None)
            s["disabled"] = relogin
        elif s["fails"] >= 3:
            s["disabled"] = (f"{s['fails']} failed logins in a row ({short(e)}). Check the password in Settings, "
                             "then Verify once after the wait shown.")
        elif s["fails"] == 2:
            s["blocked_until"] = max(s["blocked_until"], now + 1920)  # outlast a 1800 s camera lockout
        if len(s["recent_fails"]) >= 2:   # two failures in the window: block, whatever fails says
            s["blocked_until"] = max(s["blocked_until"], now + FAIL_WINDOW_BLOCK)
        _save()


def note_login_success(cam):
    with _gate_lock():
        _reload([cam])
        s = st(cam)
        if s.get("fails"):
            s["fails"] = 0
            _save()


def remember_transport(cam, method):
    """Persist the transport pytapo settled on, so later logins skip some discovery probes."""
    with _gate_lock():
        _reload([cam])
        st(cam)["transport"] = method
        _save()


def forget_transport(cam):
    """The auth method changed: forget the stored transport only. disabled, fails, recent_fails, logins and every block
    are kept (a change of login method is not a way round the gate)."""
    with _gate_lock():
        _reload([cam])
        if st(cam).pop("transport", None) is not None:
            _save()


def note_rtsp_auth_failure(cam):
    """The camera rejected the RTSP (camera-account) password: disable Live until credentials_changed()."""
    with _gate_lock():
        _reload([cam])
        st(cam)["live_disabled"] = ("Live view disabled: the camera rejected the camera-account password. "
                                    "Save it again in Settings.")
        _save()


def live_disabled(cam):
    """The persisted reason Live is disabled for this camera, or None (reads the file, never contacts a camera)."""
    with _gate_lock():
        _reload([cam])
        return st(cam).get("live_disabled")


def tplink_changed(cam):
    """The TP-Link password was saved again (the Accounts step or Settings > TP-Link account). Called by
    services/setup.py `_tplink_saved` for auto and admin_cloud cameras only; a cam_cloud camera is not touched here
    (its camera account save clears it through credentials_changed). Clears `disabled` and `fails` ONLY: live_disabled,
    logins, blocked_until, recent_fails and the transport are kept, so a block is never shortened, the Live refusal
    stays and the 30 min failure window still counts (a second failure after the fix blocks 1920 s again)."""
    with _gate_lock():
        _reload([cam])
        s = st(cam)
        if s["disabled"] or s.get("fails"):
            s["disabled"] = None
            s["fails"] = 0
            _save()


def credentials_changed(cam, reset_auth=False):
    """Recovery after the user fixed the password. Clears disabled, fails and live_disabled. The transport is kept
    unless reset_auth (the auth method was changed too). Keeps logins, blocked_until and recent_fails: a block is
    never shortened and the 30 min failure record is not wiped by a credentials change."""
    with _gate_lock():
        _reload([cam])
        s = st(cam)
        s["disabled"] = None
        s["fails"] = 0
        s["live_disabled"] = None
        if reset_auth:
            s.pop("transport", None)
        _save()


def _cli(argv):
    global BREAKER_F
    BREAKER_F = config.META / "breaker.json"
    flags = {a for a in argv if a.startswith("--")}
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 2 or args[0] != "credentials-changed" or flags - {"--reset-auth"}:
        print("usage: python -m kinora.camera.gate credentials-changed <cam id> [--reset-auth]", file=sys.stderr)
        return 2
    cam = config.CAMS.get(args[1])
    if cam is None:
        print(f"unknown camera id {args[1]!r} (see cameras.json)", file=sys.stderr)
        return 1
    reset = "--reset-auth" in flags
    credentials_changed(cam, reset_auth=reset)
    what = "disabled, fails, live_disabled and transport" if reset else "disabled, fails and live_disabled (transport kept)"
    print(f"gate state for {cam['id']} cleared: {what}. Logins and any block are kept.")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
