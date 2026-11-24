"""First-run setup and camera settings API (wizard design, sections 5 and 8 of the design notes).

Handlers are thin: parse the JSON object, call one function here, return JSON. Domain errors are CameraError
subclasses and become HTTP responses in server.camera_errors. Request bodies are never echoed in an error.
Secrets go to kinora.creds and only their source labels ("wizard", "dev_override", "missing") ever
leave this module. No function here contacts a camera except verify(), which performs exactly one gated login
through the normal session path.
"""
import asyncio
import ipaddress
import re
import secrets
import shutil
import time

from aiohttp import web

from kinora import cameras, config, creds, health
from kinora.binaries import BinaryMissing, ffmpeg_path
from kinora.camera import gate, session
from kinora.errors import BadInput, CameraError, Conflict

DEFAULT_USER = "rtspadmin"
PORTS = (443, 554, 8800)                  # login-free TCP connects only (health._tcp)
RFC1918 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
MODELS = {   # model presets: flags only (the sign-in method is automatic). `auth` is what a manual PATCH model names
    "C100": {"auth": "admin_cloud", "p2p": True, "stream_playback": True, "thumbs": False, "win": 30,
             "fast_forward": True,
             # EXPERIMENT (docs/OPERATIONS.md, 'Native thumbnails'): ask the camera for its own thumbnail over P2P. Remove if the camera says unsupported.
             "native_thumbs": True},
    "C200": {"auth": "cam_cloud", "ptz": True, "fast_forward": True},
    "C210": {"auth": "admin_cloud", "ptz": True, "fast_forward": True},
    "other": {},
}
EDITABLE_FLAGS = ("ptz", "win", "thumbs", "ptz_invert", "ptz_step_deg", "fast_forward")
READONLY_FLAGS = ("p2p", "stream_playback")
PRESET_FLAGS = ("ptz", "win", "thumbs", "fast_forward", "p2p", "stream_playback", "ptz_invert", "ptz_step_deg", "native_thumbs")
PUBLIC_KEYS = ("id", "name", "ip", "auth", "model", "model_note", "device_id", "enabled", "verified", "model_seen",
               "fw_seen")
UNKNOWN_NOTE = "Unknown model: using standard settings"
MODEL_TOKENS = ("C100", "C200", "C210")
ADD_FIELDS = ("name", "ip", "auth", "model", "camera_user", "camera_password")
PATCH_FIELDS = ("name", "ip", "auth", "flags", "enabled", "model", "apply_preset")
CRED_FIELDS = ("camera_user", "camera_password", "cloud_password", "reset_auth")
EMAIL_RE = re.compile(r"[^@\s]{1,64}@[^@\s]{1,253}\.[^@\s]{2,}")

_verify_lock = asyncio.Lock()   # one camera at a time: a second Verify while one runs gets 409
REACH_GAP_S = 1.0               # minimum seconds between two port checks of one camera (429 inside it)
_reach_last = {}                # camera id -> monotonic time of its last port check


# ---- small validators (fixed messages: nothing from the request is echoed) ---------------
async def _body(request, allowed):
    try:
        body = await request.json()
    except Exception:
        raise BadInput("expected a JSON object") from None
    if not isinstance(body, dict):
        raise BadInput("expected a JSON object")
    if set(body) - set(allowed):
        raise BadInput("unknown field in the request")
    return body


def _text(value, field, limit=config.NAME_MAX):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise BadInput(f"{field} must be 1 to {limit} characters")
    return value.strip()


def _secret(value, field):
    if not isinstance(value, str) or not value or len(value) > 256:
        raise BadInput(f"{field} must be a non-empty text (at most 256 characters)")
    return value


def _bool(value, field):
    if not isinstance(value, bool):
        raise BadInput(f"{field} must be true or false")
    return value


def _ip(value):
    try:
        if isinstance(value, str):
            return str(ipaddress.IPv4Address(value))
    except ValueError:
        pass
    raise BadInput("ip must be an IPv4 address")


def _private(ip):
    return any(ipaddress.IPv4Address(ip) in net for net in RFC1918)


def _lan_ip(value):
    """An IPv4 address on the home network (RFC 1918): cameras are never addressed outside it."""
    ip = _ip(value)
    if not _private(ip):
        raise BadInput("ip must be a private address (10/8, 172.16/12 or 192.168/16)")
    return ip


def _ensure_free_ip(ip, cid=None):
    other = config.CAM_BY_IP.get(ip)
    if other is not None and other["id"] != cid:
        raise BadInput("another camera already uses this address")


def _camera(cid):
    cam = config.CAMS.get(cid) if isinstance(cid, str) else None
    if cam is None:
        raise web.HTTPNotFound(text="unknown camera")
    return cam


def _flag_value(key, value):
    if key not in EDITABLE_FLAGS:
        raise BadInput("unknown flag")
    if key == "win":
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3600:
            raise BadInput("win must be a whole number of seconds from 1 to 3600")
    elif key == "ptz_step_deg":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 180:
            raise BadInput("ptz_step_deg must be a number from 1 to 180")
    else:
        _bool(value, key)
    return value


async def _registry(coro):
    """A cameras.py write with its domain errors mapped to HTTP errors."""
    try:
        return await coro
    except cameras.Refused as e:
        raise Conflict(str(e)) from None
    except KeyError:
        raise web.HTTPNotFound(text="unknown camera") from None
    except ValueError as e:
        raise BadInput(str(e)) from None


def public(cam):
    """The camera as the browser may see it: no secrets, no internal keys (see PUBLIC_KEYS and the flags)."""
    out = {k: cam[k] for k in PUBLIC_KEYS if k in cam}
    out["enabled"] = bool(cam.get("enabled"))
    out["verified"] = bool(cam.get("verified"))
    out["flags"] = {k: cam[k] for k in EDITABLE_FLAGS + READONLY_FLAGS if k in cam}
    return out


# ---- state -------------------------------------------------------------------------------
def _gate_view(cam):
    try:
        return gate.snapshot(cam)
    except gate.GateRefused:   # lock file unusable: show no gate detail (a login would be refused anyway)
        return {"disabled": None, "live_disabled": None, "wait_s": 0, "recent_fails": 0}


def _status(cam, snap):
    if cam["id"] in session.VERIFYING:   # a Verify run in progress (shown as "verifying")
        return "verifying"
    if snap["disabled"]:
        return "disabled_by_gate"
    if cam.get("verified"):
        return "live_disabled" if snap["live_disabled"] else "ok"
    if snap["wait_s"]:
        return "waiting"
    if snap["recent_fails"]:
        return "failed"
    return "not_verified"


def _read_camera(cam, errors):
    """The camera's secret sources, camera account user and whether it has one (one store read). A store that cannot be
    read shows as "missing" here; its reason is reported once in prerequisites (`errors` collects it)."""
    try:
        return creds.camera_state(cam)
    except RuntimeError as e:
        errors.append(str(e))
        return {"secret_sources": {"camera": "missing", "cloud": "missing"}, "camera_user": None,
                "has_camera_account": False}


def _prerequisites(store_error=None):
    try:
        ffmpeg = bool(ffmpeg_path())
    except BinaryMissing:
        ffmpeg = False
    try:
        store = creds.credential_store()
    except RuntimeError:
        store = None
    cred = {"name": store, **creds.backend_status()}
    if store_error:
        cred = {"name": store, "ok": False, "backend": cred.get("backend"),
                "reason": "a saved password could not be read: unlock the keyring or allow this app to use it, "
                          "then try again"}
    return {"ffmpeg": ffmpeg, "ffprobe": shutil.which("ffprobe") is not None, "credential_store": cred}


def _account_email():
    """The saved TP-Link account email (not a secret), or None."""
    try:
        return creds.account_email()
    except RuntimeError:
        return None


def _step(cams, email_set):
    if not cams:
        return "prepare"
    if not email_set or any(c["secret_sources"]["cloud"] == "missing" for c in cams):
        return "account"
    if any(c["status"] != "ok" for c in cams):
        return "verify"
    return "done"


def _build_state():
    cams = []
    errors = []
    for cam in list(config.CAMS.values()):
        snap = _gate_view(cam)
        read = _read_camera(cam, errors)
        cams.append({**public(cam), "status": _status(cam, snap), "wait_s": snap["wait_s"],
                     "gate_reason": snap["disabled"] or snap["live_disabled"],
                     "mac_mismatch": bool(cam.get("mac_mismatch")),
                     "model_mismatch": _model_mismatch(cam.get("model"), cam.get("model_seen")),
                     "secret_sources": read["secret_sources"], "camera_user": read["camera_user"],
                     "has_camera_account": read["has_camera_account"],
                     "live_refused": bool(snap["live_disabled"])})
    email = _account_email()
    return {"prerequisites": _prerequisites(errors[0] if errors else None),
            "account": {"email": email, "email_set": bool(email)},
            "cameras": cams, "step": _step(cams, bool(email)), "version": config.VERSION}


async def state(request):
    return web.json_response(await asyncio.to_thread(_build_state), headers={"Cache-Control": "no-store"})


# ---- account -----------------------------------------------------------------------------
def _store_account(email, password):
    try:
        creds.set_cloud_secret(email, password)
        creds.set_account_email(email)
    except (RuntimeError, ValueError) as e:
        raise BadInput(str(e)) from None


async def account(request):
    """The TP-Link account only (email and cloud password, one pair, one account for every camera that signs in with
    the Tapo account). A camera account belongs to one camera and is entered when the camera is added."""
    body = await _body(request, ("email", "password", "camera_user", "camera_password"))
    if "camera_user" in body or "camera_password" in body:
        raise BadInput("A camera account belongs to one camera: enter it when you add the camera")
    if "email" not in body or "password" not in body:
        raise BadInput("send email and password")
    email = body["email"]
    if not isinstance(email, str) or len(email) > 254 or not EMAIL_RE.fullmatch(email.strip()):
        raise BadInput("email must look like an email address")
    await asyncio.to_thread(_store_account, email.strip(), _secret(body["password"], "password"))
    await asyncio.to_thread(_tplink_saved)
    return web.json_response({"ok": True, "saved": ["tplink"]})


def _tplink_saved():
    """A new TP-Link password: the cameras that sign in with it (auto, admin_cloud) lose a `disabled` or `fails` set by a
    refused TP-Link password. Their blocks, Live refusals and failure window are kept (gate.tplink_changed)."""
    for cam in list(config.CAMS.values()):
        if cam["auth"] in ("auto", "admin_cloud"):
            gate.tplink_changed(cam)


# ---- add camera (model presets) ------------------------------------------------------------
def _new_id(name):
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:24] or "cam"
    taken = set(config.CAMS) | {t["id"] for t in config.TOMBSTONES}
    for _ in range(50):
        cid = f"{slug}_{secrets.token_hex(2)}"
        if config.ID_RE.fullmatch(cid) and cid not in taken:
            return cid
    raise BadInput("could not make a unique id: choose another name")


def _new_record(body):
    """A camera to verify. No model and no flags yet: the first successful Verify reads the model from the device.
    auth defaults to auto; a `model` sent by an older client is accepted and ignored."""
    name = _text(body.get("name"), "name")
    ip = _lan_ip(body.get("ip"))
    _ensure_free_ip(ip)
    auth = body.get("auth") or "auto"
    if auth not in config.AUTH_METHODS:
        raise BadInput("auth must be auto, admin_cloud or cam_cloud")
    return {"id": _new_id(name), "name": name, "ip": ip, "auth": auth, "verified": False}


def _camera_account(body):
    """(user, password) of the optional camera account (Live view only), or None. Both or neither."""
    user, password = body.get("camera_user"), body.get("camera_password")
    if user in (None, "") and password in (None, ""):
        return None
    if user in (None, "") or password in (None, ""):
        raise BadInput("Enter both the camera account user and its password, or leave both empty")
    return _text(user, "camera_user", 64), _secret(password, "camera_password")


async def add_camera(request):
    """A camera with an optional camera account (used by Live view only; Verify never needs it)."""
    body = await _body(request, ADD_FIELDS)
    rec = _new_record(body)
    account = _camera_account(body)
    made = await _registry(cameras.add(rec))          # enabled:false until Verify
    if account is None:
        return web.json_response({"id": made["id"], "device_id": made["device_id"]})
    try:
        await asyncio.to_thread(creds.set_camera_secret, made, *account)
    except (RuntimeError, ValueError) as e:
        await _registry(cameras.remove(made["id"]))   # no half-made camera without its secret
        raise BadInput(str(e)) from None
    return web.json_response({"id": made["id"], "device_id": made["device_id"]})


# ---- reachability (login-free) ------------------------------------------------------------
async def reach(request):
    """Port check of a saved camera: the camera's id, and the address it is saved with (save it first)."""
    body = await _body(request, ("ip", "id"))
    cid = body.get("id")
    if not isinstance(cid, str) or not cid:
        raise BadInput("id must name the camera to check")
    cam = _camera(cid)
    ip = _lan_ip(body.get("ip"))
    if ip != cam["ip"]:
        raise BadInput("ip must be the address the camera is saved with")
    now = time.monotonic()
    if now - _reach_last.get(cid, -REACH_GAP_S) < REACH_GAP_S:
        raise web.HTTPTooManyRequests(text="wait a moment before checking the ports again")
    _reach_last[cid] = now
    open_ports = await asyncio.gather(*(health._tcp(ip, p) for p in PORTS))
    return web.json_response({"ip": ip, "ports": {str(p): bool(ok) for p, ok in zip(PORTS, open_ports)}})


# ---- verify (exactly one gated login) -----------------------------------------------------
def _missing_secret(cam):
    """Verify needs the TP-Link account only (refused before any login). A cam_cloud camera also needs its camera account."""
    try:
        sources = creds.secret_sources(cam)
    except RuntimeError:
        return None   # an unreadable store: the login refuses it before any attempt, uncounted (session.resolve)
    if sources["cloud"] == "missing":
        return "Save your TP-Link account first"
    if cam["auth"] == "cam_cloud" and sources["camera"] == "missing":
        return "this camera has no camera account password stored: add it in Settings"
    return None


def _model_mismatch(chosen, seen):
    if not chosen or chosen == "other" or not seen:
        return False
    return chosen.upper() not in seen.upper()


async def _disable(cid):
    """A failed Verify: the camera stays (or goes) switched off and its cached session is dropped. The gate keeps
    its own state."""
    session.drop_client(cid)
    try:
        await _registry(cameras.update(cid, {"enabled": False}))
    except web.HTTPNotFound:
        pass


async def _attempt(cid):
    """One gated login through session.client, then one getBasicInfo through session.api. The camera is switched on
    only after both succeed (in _attempt's last step); session.client allows this login through VERIFYING."""
    cam = config.CAMS[cid]
    snap = await asyncio.to_thread(_gate_view, cam)
    if snap["wait_s"] and not snap["disabled"]:   # the gate's own spacing: refuse without touching the camera
        return {"state": "waiting", "wait_s": snap["wait_s"]}
    try:
        await asyncio.to_thread(session.client, cam)
        info = await session.api(cam, lambda t: t.getBasicInfo())
    except CameraError as e:
        await _disable(cid)   # not enabled after a failed login; the gate keeps its own state
        return {"state": "failed", "reason": e.text}
    basic = info.get("device_info", {}).get("basic_info", {}) if isinstance(info, dict) else {}
    model = str(basic.get("device_model") or "")[:40]
    firmware = str(basic.get("fw_ver") or "")[:80]
    mac = str(basic.get("mac") or "").strip().upper()[:32]
    changes = {"enabled": True, "verified": True, "model_seen": model, "fw_seen": firmware}
    if mac:
        if not cam.get("mac"):
            changes.update(mac=mac, mac_mismatch=False)
        else:   # a different device at this address is flagged, never re-keyed (the gate keeps its device_id)
            changes["mac_mismatch"] = cam["mac"].strip().upper() != mac
    note = None
    if model and not cam.get("model"):   # first successful Verify, no model recorded: presets from the device
        note = _apply_detected_model(cam, basic, model, changes)
    await _registry(cameras.update(cid, changes))   # the flags, enabled and verified land in one write
    out = {"state": "ok", "model": model, "firmware": firmware, "mismatch": _model_mismatch(cam.get("model"), model)}
    if note:
        out["note"] = note
    return out


def _model_token(raw):
    """The exact model token (C100, C200 or C210) of a device-reported name; C1000 is not C100 (no substring match)."""
    for token in re.split(r"[^A-Z0-9]+", str(raw or "").upper()):
        if token in MODEL_TOKENS:
            return token
    return None


def _flags(preset):
    return {k: v for k, v in preset.items() if k != "auth"}


def _apply_detected_model(cam, basic, model, changes):
    """Record the model the device reports and add its preset flags to `changes` (the same write as verified). A flag the
    camera already has is never overwritten. Returns the note shown in the UI (None when there is none)."""
    token = _model_token(model)
    if token is None:   # unknown model: C200-like flags, the two camera-account features off
        changes.update(model=model, model_note=UNKNOWN_NOTE)
        preset = {**_flags(MODELS["C200"]), "p2p": False, "stream_playback": False}
        note = UNKNOWN_NOTE
    else:
        changes["model"] = token
        preset = _flags(MODELS[token])
        note = None
        if token == "C100":
            hw = str(basic.get("hw_ver") or "").strip()[:20]
            note = "Baby-monitor mode applied" + (f" (hardware {hw})" if hw else "")
    changes.update({k: v for k, v in preset.items() if k not in cam})
    return note


async def _verify(cid):
    cam = config.CAMS[cid]
    if cam.get("enabled") and cam.get("verified"):
        raise Conflict(f"{cam['name']} is already verified and switched on: nothing to verify")
    missing = await asyncio.to_thread(_missing_secret, cam)
    if missing:
        return {"state": "failed", "reason": missing}
    session.VERIFYING.add(cid)   # the one login this unverified camera gets (session.client)
    try:
        if cam.get("enabled"):   # a camera enabled before verification (older files): off until this run succeeds
            await _registry(cameras.update(cid, {"enabled": False}))
        return await _attempt(cid)
    finally:
        session.VERIFYING.discard(cid)


async def verify(request):
    body = await _body(request, ("id",))
    cam = _camera(body.get("id"))
    if _verify_lock.locked():
        raise Conflict("another camera is being verified: wait for it to finish")
    async with _verify_lock:
        return web.json_response(await _verify(cam["id"]))


# ---- camera settings ---------------------------------------------------------------------
async def reorder_cameras(request):
    """PUT /api/cameras/order {"order": [every camera id]}: the order the switcher and Settings list the cameras in."""
    body = await _body(request, ("order",))
    order = body.get("order")
    if not isinstance(order, list) or not all(isinstance(i, str) for i in order):
        raise BadInput("order must be a list of camera ids")
    await _registry(cameras.reorder(order))
    return web.json_response({"order": list(config.CAMS)})


async def patch_camera(request):
    cam = _camera(request.match_info["cam_id"])
    body = await _body(request, PATCH_FIELDS)
    changes = {}
    if "name" in body:
        changes["name"] = _text(body["name"], "name")
    if "ip" in body:
        ip = _lan_ip(body["ip"])
        if ip != cam["ip"]:
            _ensure_free_ip(ip, cam["id"])
            changes["ip"] = ip
    if "auth" in body and body["auth"] != cam["auth"]:
        if body["auth"] not in config.AUTH_METHODS:
            raise BadInput("auth must be auto, admin_cloud or cam_cloud")
        changes["auth"] = body["auth"]
    if "model" in body:
        if not _known_model(body["model"]):
            raise BadInput("model must be one of C100, C200, C210, other")
        changes["model"] = body["model"]
    if "apply_preset" in body and _bool(body["apply_preset"], "apply_preset"):
        model = changes.get("model", cam.get("model"))
        if not _known_model(model):
            raise BadInput("apply_preset needs a model: send model, or set one first")
        changes.update(_preset_changes(model))   # the flags only: auth, enabled and verified are never touched
    flags = body.get("flags", {})
    if not isinstance(flags, dict):
        raise BadInput("flags must be an object")
    for key, value in flags.items():   # explicit flags are applied after a preset
        changes[key] = _flag_value(key, value)
    enabled = _bool(body["enabled"], "enabled") if "enabled" in body else None
    if "ip" in changes or "auth" in changes:   # a new address or account must be verified again
        if enabled:
            raise BadInput("an address or account change must be verified before the camera is enabled")
        changes.update(verified=False, enabled=False)
    elif enabled is not None:
        if enabled and not cam.get("verified"):
            raise Conflict("verify the camera before enabling it")
        changes["enabled"] = enabled
    if changes:
        await _registry(cameras.update(cam["id"], changes))
    if "auth" in changes:   # a changed login method: the stored transport is wrong now (the gate state is kept)
        await asyncio.to_thread(gate.forget_transport, config.CAMS[cam["id"]])
    out = public(config.CAMS[cam["id"]])
    out["auth_differs_from_preset"] = _auth_differs(config.CAMS[cam["id"]])
    return web.json_response(out)


def _known_model(model):
    return isinstance(model, str) and model in MODELS


def _preset_changes(model):
    """The model's flags as changes. A flag the preset does not set is removed, as add leaves it. `other` gets the
    C200 flags (it has no preset of its own). Auth is not part of it."""
    preset = MODELS["C200" if model == "other" else model]
    return {key: preset.get(key, cameras.DROP) for key in PRESET_FLAGS}


def _auth_differs(cam):
    """True when the camera's login method is not the one its model's preset names (`other` has none: False).
    Automatic sign-in is never a difference."""
    if cam.get("auth") == "auto":
        return False
    preset_auth = MODELS[cam["model"]].get("auth") if _known_model(cam.get("model")) else None
    return preset_auth is not None and cam.get("auth") != preset_auth


def _current_user(cam):
    try:
        return creds.get_camera_credentials(cam)[0]
    except RuntimeError:
        return DEFAULT_USER


def _store_credentials(cam, user, camera_password, cloud_password):
    try:
        if cloud_password is not None:
            email = creds.account_email()
            if not email:
                raise BadInput("set the TP-Link account email first (account step)")
            creds.set_cloud_secret(email, cloud_password)
        if camera_password is not None:
            creds.set_camera_secret(cam, user or _current_user(cam), camera_password)
    except (RuntimeError, ValueError) as e:
        raise BadInput(str(e)) from None


async def camera_credentials(request):
    """New secrets and, optionally, a new login method in one call. Everything is validated first; then the login
    method is changed (the camera must be verified again), the secrets are stored, and the gate is told. A changed
    login method always forgets the stored transport (reset_auth)."""
    cam = _camera(request.match_info["cam_id"])
    body = await _body(request, CRED_FIELDS + ("auth",))
    reset = _bool(body["reset_auth"], "reset_auth") if "reset_auth" in body else False
    camera_password = _secret(body["camera_password"], "camera_password") if "camera_password" in body else None
    cloud_password = _secret(body["cloud_password"], "cloud_password") if "cloud_password" in body else None
    user = _text(body["camera_user"], "camera_user", 64) if "camera_user" in body else None
    auth = None
    if "auth" in body and body["auth"] != cam["auth"]:
        if body["auth"] not in config.AUTH_METHODS:
            raise BadInput("auth must be auto, admin_cloud or cam_cloud")
        auth = body["auth"]
        reset = True   # a changed login method: the stored transport is forgotten below (no login here)
    if camera_password is None and cloud_password is None and auth is None:
        raise BadInput("send camera_password and/or cloud_password, or a new auth")
    if auth is not None:
        await _registry(cameras.update(cam["id"], {"auth": auth, "verified": False, "enabled": False}))
    await asyncio.to_thread(_store_credentials, cam, user, camera_password, cloud_password)
    await _registry(cameras.credentials_changed(cam["id"]))
    await asyncio.to_thread(gate.credentials_changed, config.CAMS[cam["id"]], reset)   # never logs in
    return web.json_response({"ok": True, "reset_auth": reset})


async def delete_camera(request):
    cam = _camera(request.match_info["cam_id"])
    body = await _body(request, ("confirm",))
    if body.get("confirm") != cam["id"]:
        raise BadInput("confirm must repeat the camera id")
    await _registry(cameras.remove(cam["id"]))   # tombstone: the id and the gate budget are kept
    try:
        await asyncio.to_thread(creds.delete_camera_secret, cam)
        removed_secret = True
    except RuntimeError:
        removed_secret = False
    return web.json_response({"ok": True, "id": cam["id"], "secret_removed": removed_secret})
