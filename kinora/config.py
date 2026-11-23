"""Paths, port, camera table and tunables shared by the services.

Importing this module has no side effects on disk. The one-time move of the pre-rename data folder happens only in
`init()`, which the entry points call (server after the instance lock, `python -m kinora.creds`, `python -m
kinora.camera.gate`). `ensure_dirs()` creates the working folders."""
import ipaddress
import json
import os
import re
import sys
from pathlib import Path

from platformdirs import user_cache_dir, user_data_dir

from kinora.oslock import os_lock

ROOT = Path(__file__).resolve().parent.parent   # checkout directory (dev layout: cameras.json, .env); site-packages when installed
WEB = Path(__file__).resolve().parent / "web"   # index.html and static/: package data, shipped in the wheel


def env(name, default=None):
    """Environment setting KINORA_<name>; falls back to the old TAPOCAM_<name> name (still accepted)."""
    for prefix in ("KINORA_", "TAPOCAM_"):
        value = os.environ.get(prefix + name)
        if value is not None:
            return value
    return default


def _has_data(folder):
    """True when the folder holds cameras or login-gate state (an empty folder is not worth keeping)."""
    folder = Path(folder)
    return (folder / "cameras.json").exists() or (folder / "cache" / "meta" / "breaker.json").exists()


def plan_legacy_home(new, old):
    """Decide, without touching the disk, which per-user folder to use. -> (folder, move, note).

    move=True: only `old` exists, it should be renamed to `new` (done by init(), never at import).
    note: a sentence for stderr, or None. Both folders present: the one that holds data wins, never a merge.
    """
    new, old = Path(new), Path(old)
    if new.exists():
        if old.is_dir() and old != new:
            if _has_data(new):
                return new, False, f"both {new} and the old data folder {old} exist; using {new}, {old} was left alone"
            if _has_data(old):
                return old, False, (f"{new} holds no cameras or login state but the old data folder {old} does: "
                                    f"using {old} for now. Move its contents into {new} by hand, or remove the empty {new}")
        return new, False, None
    if old.is_dir() and old != new:
        return old, True, None
    return new, False, None


def migrate_legacy_home(new, old, legacy_lock_held=False):
    """Move the pre-rename data folder `old` to `new`, once. -> the folder to use. Never merges, never creates `new`
    when the move did not happen. Refuses to move while an old-code server holds the legacy instance lock."""
    new, old = Path(new), Path(old)
    folder, move, note = plan_legacy_home(new, old)
    if note:
        print(f"warning: {note}", file=sys.stderr)
    if not move:
        return folder
    lock = None
    if not legacy_lock_held:
        lock = _try_legacy_lock()
        if lock is False:
            print(f"warning: an older Kinora (tapo-cameras) server is running, so the data folder {old} was not moved. "
                  "Stop it, then start again. Using the old folder this time.", file=sys.stderr)
            return old
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)   # atomic on the same volume
    except OSError as e:
        print(f"warning: could not move the old data folder {old} to {new} ({e.strerror or e}); "
              "using the old folder this time", file=sys.stderr)
        return old
    finally:
        if lock:
            lock.close()
    print(f"moved the data folder {old} to {new}", file=sys.stderr)
    return new


def legacy_lock_path():
    """Single-instance lock file of the pre-rename code (user cache dir 'tapo-cameras')."""
    return Path(user_cache_dir("tapo-cameras")) / "server.lock"


def _try_legacy_lock():
    """Take the legacy lock without waiting. -> an open handle (caller closes it), None when no old server can exist
    (no lock file), False when an old server holds it. Creates nothing."""
    path = legacy_lock_path()
    if not path.exists():
        return None
    try:
        f = open(path, "a+b")
    except OSError:
        return None
    try:
        os_lock(f, blocking=False)
    except OSError:
        f.close()
        return False
    return f


def _plan_home():
    """(home, move) without side effects. KINORA_HOME (or the old TAPOCAM_HOME) wins; the legacy/dev layout
    (cameras.json in the repo) keeps using the repo; otherwise the per-user data dir from platformdirs."""
    home = env("HOME")
    if home:
        return Path(home).expanduser(), False
    if (ROOT / "cameras.json").exists():
        return ROOT, False
    folder, move, _ = plan_legacy_home(Path(user_data_dir("kinora")), Path(user_data_dir("tapo-cameras")))
    return folder, move


def resolve_home():
    """Data directory: cameras.json, cache/, exports/, .env. Pure: it never moves or creates anything. While the
    pre-rename folder still has to be moved this is the OLD folder; init() moves it and re-points the paths."""
    return _plan_home()[0]


HOME = resolve_home()
PORT = int(env("PORT", "8080"))
VERSION = "0.1.0"   # shown in Settings > About; bump with each release
CACHE = HOME / "cache"
META = CACHE / "meta"
EXPORTS = HOME / "exports"
LIVE_ROOT = CACHE / "live"
PLAY_ROOT = CACHE / "play"


def _bind(home):
    global HOME, CACHE, META, EXPORTS, LIVE_ROOT, PLAY_ROOT
    HOME = home
    CACHE = HOME / "cache"
    META = CACHE / "meta"
    EXPORTS = HOME / "exports"
    LIVE_ROOT = CACHE / "live"
    PLAY_ROOT = CACHE / "play"


def init(legacy_lock_held=False):
    """Entry-point startup: move the pre-rename data folder once (see migrate_legacy_home) and re-point HOME, CACHE,
    META, EXPORTS, LIVE_ROOT, PLAY_ROOT and the camera tables. Call it before importing modules that copy those paths.
    `legacy_lock_held`: the caller already holds the legacy instance lock (instance.acquire does). With an older server
    running nothing is moved and the old folder is used. -> HOME."""
    if env("HOME") or (ROOT / "cameras.json").exists():
        return HOME
    home = migrate_legacy_home(Path(user_data_dir("kinora")), Path(user_data_dir("tapo-cameras")), legacy_lock_held)
    if home != HOME:
        _bind(home)
        reload_cameras()
    return HOME


def allowed_hosts(port):
    """Host header values accepted by the local-only middleware."""
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


ID_RE = re.compile(r"[a-z0-9_]{2,32}")   # used with fullmatch: the id is immutable and never reused
AUTH_METHODS = ("auto", "admin_cloud", "cam_cloud")   # auto: signs in as pytapo does (admin + TP-Link password)
CAM_REQUIRED = ("id", "name", "ip", "auth")
NAME_MAX = 60


def check_ipv4(ip, what):
    """ip must be an IPv4 literal (no hostnames)."""
    try:
        if isinstance(ip, str):
            ipaddress.IPv4Address(ip)
            return
    except ValueError:
        pass
    raise ValueError(f"{what}: ip must be an IPv4 address (got {ip!r})")


def check_camera(c, where="cameras.json"):
    """Validate one camera entry (used at load and on every runtime write, see kinora/cameras.py)."""
    if not isinstance(c, dict):
        raise ValueError(f"{where}: each camera must be an object")
    for k in CAM_REQUIRED:
        if k not in c:
            raise ValueError(f"{where}: camera {c.get('id')!r} has no {k!r}")
    cid = c["id"]
    if not isinstance(cid, str) or not ID_RE.fullmatch(cid):
        raise ValueError(f"{where}: camera id must match ^[a-z0-9_]{{2,32}}$ (got {cid!r})")
    name = c["name"]
    if not isinstance(name, str) or not name.strip() or len(name) > NAME_MAX:
        raise ValueError(f"{where}: name of camera {cid!r} must be 1 to {NAME_MAX} characters")
    if c["auth"] not in AUTH_METHODS:
        raise ValueError(f"{where}: auth of camera {cid!r} must be one of {', '.join(AUTH_METHODS)}")
    check_ipv4(c["ip"], f"{where}: camera {cid!r}")
    if "device_id" in c and (not isinstance(c["device_id"], str) or not c["device_id"].strip()):
        raise ValueError(f"{where}: device_id of camera {cid!r} must be a non-empty string "
                         f"(got {c['device_id']!r})")


def check_cameras(cams, where="cameras.json"):
    """Every camera valid, and no two cameras share an id, an IP or a device_id (one camera per address and per device)."""
    ids, ips, devs = set(), set(), set()
    for c in cams:
        check_camera(c, where)
        if c["id"] in ids:
            raise ValueError(f"{where}: duplicate camera id {c['id']!r}")
        if c["ip"] in ips:
            raise ValueError(f"{where}: duplicate ip {c['ip']} (camera {c['id']!r}): one camera per address")
        ids.add(c["id"])
        ips.add(c["ip"])
        if "device_id" in c:
            key = c["device_id"].strip().lower()
            if key in devs:
                raise ValueError(f"{where}: duplicate device_id of camera {c['id']!r}")
            devs.add(key)


def check_tombstones(tombs, where="cameras.json"):
    """Removed cameras: {id, device_id, ip, removed_at}. Their ids are never reused."""
    if not isinstance(tombs, list):
        raise ValueError(f"{where}: tombstones must be a list")
    for t in tombs:
        if (not isinstance(t, dict) or not isinstance(t.get("id"), str)
                or not isinstance(t.get("device_id"), str) or not t["device_id"].strip() or "removed_at" not in t):
            raise ValueError(f"{where}: malformed tombstone (needs id, device_id, ip, removed_at)")
        check_ipv4(t.get("ip"), f"{where}: tombstone {t['id']!r}")


def load_document(path):
    """(cameras, tombstones) from cameras.json: the legacy bare list or {"version": 1, "cameras": [...],
    "tombstones": [...]} (tombstones optional). A missing file is an empty registry."""
    if not path.exists():
        return [], []
    data = json.loads(path.read_text())
    tombs = []
    if isinstance(data, dict):
        if data.get("version") != 1:
            raise ValueError(f"{path}: unsupported cameras.json version {data.get('version')!r}")
        tombs = data.get("tombstones", [])
        data = data.get("cameras", [])
    if not isinstance(data, list):
        raise ValueError(f"{path}: cameras must be a list")
    check_cameras(data, str(path))
    check_tombstones(tombs, str(path))
    return data, tombs


def load_cameras(path):
    """Camera list from cameras.json (tombstones are ignored here; see load_document)."""
    return load_document(path)[0]


_cams0, _tombs0 = load_document(HOME / "cameras.json")
CAMS = {c["id"]: c for c in _cams0}
CAM_BY_IP = {c["ip"]: c for c in CAMS.values()}
TOMBSTONES = _tombs0          # removed cameras, in cameras.json "tombstones"


def reload_cameras():
    """Re-read cameras.json into the existing CAMS, CAM_BY_IP and TOMBSTONES dicts (same objects, so importers see the change).
    A file that fails to load leaves all tables untouched."""
    fresh, tombs = load_document(HOME / "cameras.json")
    by_id = {c["id"]: c for c in fresh}
    CAMS.clear()
    CAMS.update(by_id)
    CAM_BY_IP.clear()
    CAM_BY_IP.update({c["ip"]: c for c in fresh})
    TOMBSTONES[:] = tombs
    return CAMS

# listings
TODAY_TTL = 60             # seconds a listing of the current day stays fresh

# export
MAX_EXPORT_S = 2 * 3600    # protect the camera: one long pull is many media sessions
EXPORT_WIN_S = 300         # seconds fetched per camera request
MODES = {"video": "mp4", "audio_wav": "wav", "audio_m4a": "m4a"}

# pan / tilt (per camera overrides in cameras.json: "ptz_step_deg", "ptz_invert")
PTZ_STEP_DEG = 10
PTZ_MIN_GAP_S = 0.15


def ensure_dirs():
    for d in (META, EXPORTS):
        d.mkdir(parents=True, exist_ok=True)
