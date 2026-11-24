"""Local web timeline for Tapo SD-card recordings.

Run: `kinora` (installed), or `.venv/bin/python server.py` from a checkout; then open http://localhost:8080 (or the KINORA_PORT you set)

Design rules (these exist because cameras lock us out after repeated logins):
  * ONE long-lived pytapo client per camera, created under a per-camera lock (no login races).
  * Every login, including pytapo's own internal re-logins (the login primitives are patched
    below), goes through gate(): persisted back-off + login budget (survives restarts).
  * A failed login is remembered; "Temporary Suspension: N s" is honoured, bad credentials
    disable the camera until you fix the credentials and run
    `python -m kinora.camera.gate credentials-changed <cam id>`.
  * No retry-with-fresh-login. A client is dropped only on real session/auth errors.
  * Never try several auth methods automatically: cameras.json names the working one.
  * Media downloads are tracked per clip window, refuse recordings that are still being written
    (the camera returns nothing for the last ~minute), and a camera that returns no video twice
    in a row is left alone for 30 minutes.
  * Listings are cached on disk (past days forever); clips/thumbnails are cached on disk.
"""
import asyncio
import datetime
import errno
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import sys
import time
import uuid
from pathlib import Path

from aiohttp import web
from pytapo.media_stream.downloader import Downloader

from kinora import config, creds, devices, health, instance
from kinora.binaries import BinaryMissing, ffmpeg_path, prepend_ffmpeg_dir_to_path, require_pytapo_tools
from kinora import p2p_media
from kinora.errors import CameraError
from kinora.services import play as p2p_play
from kinora.services import live
from kinora.camera import slots as slots_mod
from kinora.camera.slots import slots
from kinora.services import export, hls, listings, ptz as ptz_service
from kinora.services import discover as discover_api
from kinora.services import devices_api
from kinora.services import setup as setup_api
from kinora.camera import gate, patch, session
from kinora.camera.gate import MARK, st, _reload
from kinora.camera.session import api, client, as_http, _clients

WEB, CACHE, META, EXPORTS = config.WEB, config.CACHE, config.META, config.EXPORTS
LIVE_ROOT, PLAY_ROOT, CAMS = config.LIVE_ROOT, config.PLAY_ROOT, config.CAMS
config.ensure_dirs()

# pytapo's login primitives are wrapped by the login gate; must run before any client is built.
patch.install()


def get_cam(request):
    cam = CAMS.get(request.query.get("cam", ""))
    if not cam:
        raise web.HTTPNotFound(text="unknown camera")
    return cam


# ---- listings (cached on disk; see kinora/services/listings.py) -----------------------
async def cached_listing(request, kind, fetch):
    cam, date = get_cam(request), request.query.get("date", "")  # YYYYMMDD
    session.check_usable(cam)   # a switched-off camera answers 400 from the cache too: no login, no listing
    if not re.fullmatch(r"\d{8}", date):
        raise web.HTTPBadRequest(text="date must be YYYYMMDD")
    data, how = await listings.listing_data(cam, date, kind, fetch, bool(request.query.get("refresh")))
    return web.json_response(data, headers={"X-Cache": how})


async def _fetch_recordings(cam, date):
    return await listings.fetch_recordings(api, cam, date)


async def _fetch_events(cam, date):
    return await listings.fetch_events(api, cam, date)


async def cameras(request):
    # disabled cameras are listed too (the page shows them); enabled:false means the session layer refuses to log in
    return web.json_response([{"id": c["id"], "name": c["name"], "enabled": c["enabled"], "verified": bool(c.get("verified")),
                               "thumbs": c.get("thumbs", True), "native_thumbs": bool(c.get("native_thumbs")), "win": c.get("win"), "stream": bool(c.get("stream_playback")), "ff": bool(c.get("fast_forward")),
                               "ptz": bool(c.get("ptz"))} for c in CAMS.values()])


async def status(request):
    """Local-only diagnostics (never contacts a camera)."""
    now = time.time()
    out = {}
    try:
        with gate._gate_lock():   # the file lock too: _reload() may save the state another process also writes
            _reload()
    except gate.GateRefused:      # lock file unusable: report the in-memory state rather than fail the diagnostics
        pass
    for cid, cam in CAMS.items():
        s = st(cam)
        out[cid] = {
            "name": cam["name"], "connected": cid in _clients, "disabled": s["disabled"],
            "blocked_for_s": max(0, int(s["blocked_until"] - now)),
            "logins_last_hour": len([t for t in s["logins"] if now - t < 3600]),
            "transport": s.get("transport"),
            "media_blocked_for_s": max(0, int(_media_block.get(cid, 0) - now)),
            "health": health.get(cid),
        }
    return web.json_response(out)


async def recordings(request):
    return await cached_listing(request, "rec", _fetch_recordings)


async def events(request):
    """Detection events (person, baby cry, ...) for a day, like the Tapo app's 'Detected Events'."""
    return await cached_listing(request, "ev", _fetch_events)


# ---- media (clips + thumbnails) ---------------------------------------------------------
STILL_RECORDING_S = 90     # the camera returns nothing for the most recent recording seconds
MEDIA_STALL_S = 20         # pytapo default is 120, which would hold the camera lock for minutes
WINDOW_FAIL_TTL = 300
MEDIA_BLOCK_S = 1800       # camera that returned no video twice in a row (for old recordings)
COUNT_FAILS_OLDER_THAN = 300   # an empty result for a window this old is a real failure
_fail, _media_fails, _media_block = {}, {}, {}


def media_limit(cam):
    """Latest timestamp the camera can serve, allowing for its clock being behind ours."""
    t = _clients.get(cam["id"])
    corr = getattr(t, "timeCorrection", 0) or 0
    return int(time.time()) - STILL_RECORDING_S - abs(int(corr))


def media_gate(cam, start, end):
    now = time.time()
    if now < _media_block.get(cam["id"], 0):
        raise web.HTTPServiceUnavailable(
            text=f"{cam['name']}: video downloads paused for {int(_media_block[cam['id']] - now)} s "
                 "(camera returned no video twice in a row)")
    if now - _fail.get((cam["id"], start, end), 0) < WINDOW_FAIL_TTL:
        raise web.HTTPServiceUnavailable(text="this clip failed recently")


def media_failed(cam, start, end):
    """Remember an empty download. Only old windows count towards pausing the camera: for recent
    ones 'no video' usually just means the recording is still being written or the camera is busy."""
    cid = cam["id"]
    _fail[(cid, start, end)] = time.time()
    if end < time.time() - COUNT_FAILS_OLDER_THAN:
        _media_fails[cid] = _media_fails.get(cid, 0) + 1
        if _media_fails[cid] >= 2:
            _media_block[cid] = time.time() + MEDIA_BLOCK_S


async def fetch_media(cam, start, end, name, *, raise_busy=False, beside_live=False):
    """Download [start, end] from the camera into CACHE/name. raise_busy: let slots.Busy through (thumb answers 202)."""
    if (CACHE / name).exists():
        return
    try:
        async with slots.clip(cam, beside_live=beside_live):
            await _download(cam, start, end, name)
    except slots_mod.Busy as e:
        if raise_busy:
            raise
        raise web.HTTPServiceUnavailable(text=str(e))


async def _download(cam, start, end, name):
    """Runs with the camera's media lock held (slots.clip)."""
    if (CACHE / name).exists():
        return
    require_pytapo_tools()   # before any login or media request: a missing ffprobe/ffmpeg is not a camera failure
    media_gate(cam, start, end)
    t = await asyncio.to_thread(client, cam)
    part = name[:-4] + ".part.mp4"          # never serve or cache a half-written file
    (CACHE / part).unlink(missing_ok=True)
    if cam.get("p2p"):
        # SD playback only through the app's P2P (UDT) channel, e.g. C100 v2 fw 1.3.17: see p2p_media.py
        try:
            await p2p_media.fetch_window(lambda fn: api(cam, fn), t, cam, start, end, str(CACHE / part))
        except p2p_media.Busy:
            raise web.HTTPServiceUnavailable(
                text=f"{cam['name']}: camera is busy (another app or viewer holds a video stream)")
        except (CameraError, web.HTTPException):   # a gate refusal must not count as a failed download
            raise
        except Exception as e:
            if MARK in str(e):
                raise as_http(e)
            media_failed(cam, start, end)
            raise web.HTTPBadGateway(text=f"{cam['name']}: {str(e)[:160] or 'camera returned no video'}")
        if not (CACHE / part).exists():
            media_failed(cam, start, end)
            raise web.HTTPBadGateway(text=f"{cam['name']}: camera returned no video")
        os.replace(CACHE / part, CACHE / name)
        _media_fails[cam["id"]] = 0
        return
    # primes pytapo's cached calls; returns the client that is actually used (the session may be replaced)
    t, corr, _ = await api(cam, lambda t: (t, t.getTimeCorrection(), t.getUserID()))
    try:
        dl = Downloader(session.serialised(cam, t), start, end, corr, str(CACHE), padding=0, window_size=50,
                        stall_timeout=MEDIA_STALL_S, overwriteFiles=True, fileName=part)
        status = await dl.downloadFile()
    except Exception as e:
        if MARK in str(e):
            raise as_http(e)
        media_failed(cam, start, end)
        raise web.HTTPBadGateway(text=f"{cam['name']}: camera returned no video")
    if isinstance(status, dict) and status.get("errorCode") in Downloader.BUSY_ERROR_CODES:
        raise web.HTTPServiceUnavailable(           # busy is not a failure: no memo, no pause
            text=f"{cam['name']}: camera is busy (another app or viewer holds a video stream)")
    if not (CACHE / part).exists():
        media_failed(cam, start, end)
        raise web.HTTPBadGateway(text=f"{cam['name']}: camera returned no video")
    os.replace(CACHE / part, CACHE / name)
    _media_fails[cam["id"]] = 0


async def clip(request):
    cam = get_cam(request)
    session.check_usable(cam)
    try:
        start, end = int(request.query["start"]), int(request.query["end"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text="start and end must be integers")
    if start <= 0 or end - start > 300:
        raise web.HTTPBadRequest(text="invalid clip range (max 300 s)")
    limit = media_limit(cam)
    if end > limit:  # never ask for the part still being written; stable key so it can be cached
        end = limit - limit % 30
    if end - start < 2:
        raise web.HTTPConflict(text="still recording, try again in a minute")
    out = CACHE / f"{cam['id']}_{start}_{end}.mp4"
    if not out.exists():
        await fetch_media(cam, start, end, out.name)
    else:
        os.utime(out)   # LRU: a clip being watched is the last to go
    return web.FileResponse(out)  # supports Range requests


THUMB_WAIT_S = 1.0          # the most a thumbnail request waits for the camera's media lock (playback shares it)
THUMB_RETRY_S = 5           # Retry-After for a busy answer
THUMB_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}   # keyed by event time: never changes


async def _media_lock_free(cam, wait_s=THUMB_WAIT_S):
    """True when the camera's media lock is free within wait_s seconds (a short poll, never a queue)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while slots.busy(cam["id"]):
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


def thumb_busy(reason):
    """202 'try later': the camera's video slot is in use (live view, playback, a clip). Not a failure."""
    return web.json_response({"state": "busy", "reason": reason}, status=202,
                             headers={"Retry-After": str(THUMB_RETRY_S)})


_native_unsupported = set()   # (camera id, mode) pairs that refused the native thumbnail request: no further P2P sessions for them (per process)
_native_logged = set()        # (camera id, mode) pairs whose raw first reply was logged already (once per process)
NATIVE_LABEL = {"image": "native", "snapshot": "snapshot"}   # log tag per strategy ("native" kept for the original one)


def native_mode(cam):
    """Strategy for this request or None when every strategy was refused. "image" (default) = app-style image request; once it is
    remembered unsupported, "snapshot" (pytapo's recording snapshot) takes over; "snapshot" selects it directly and never uses "image"."""
    cid = cam["id"]
    if cam.get("native_thumbs_mode") == "snapshot":
        order = ("snapshot",)
    else:
        order = ("image", "snapshot")
    return next((m for m in order if (cid, m) not in _native_unsupported), None)


async def _native_thumb(cam, ts, jpg, mode):
    """One P2P session asking the camera for its own thumbnail (p2p_media.fetch_thumbnail / fetch_snapshot). Never retried here."""
    cid = cam["id"]
    tag = NATIVE_LABEL[mode]
    if not await _media_lock_free(cam):
        return thumb_busy(f"{cam['name']}: a clip download or export is using the camera")

    def log(line):
        if line.startswith("first reply"):
            if (cid, mode) in _native_logged:
                return
            _native_logged.add((cid, mode))
        print(f"[thumb {cid} {tag}] {line}", file=sys.stderr, flush=True)
    fetch = p2p_media.fetch_snapshot if mode == "snapshot" else p2p_media.fetch_thumbnail
    try:
        async with slots.clip(cam, beside_live=cam.get("thumbs_beside_live", True) is not False):
            if not jpg.exists():
                t = await asyncio.to_thread(client, cam)
                await fetch(lambda fn: api(cam, fn), t, cam, ts, str(jpg), log=log)
    except slots_mod.Busy as e:
        return thumb_busy(str(e))
    except p2p_media.Busy as e:
        return thumb_busy(str(e))
    except p2p_media.Unsupported as e:
        _native_unsupported.add((cid, mode))
        print(f"[thumb {cid} {tag}] unsupported: {e.reason}", file=sys.stderr, flush=True)
        raise web.HTTPNotFound(text="the camera does not serve native thumbnails")
    except p2p_media.NoSnapshot as e:
        print(f"[thumb {cid} {tag} {ts}] no image: {e}", file=sys.stderr, flush=True)
        raise web.HTTPNotFound(text="no snapshot for this recording")
    except (CameraError, web.HTTPException):
        raise
    except Exception as e:
        print(f"[thumb {cid} {tag} {ts}] failed: {type(e).__name__}: {str(e)[:160]}", file=sys.stderr, flush=True)
        raise web.HTTPBadGateway(text=f"{cam['name']}: {str(e)[:160] or 'no thumbnail'}")
    if not jpg.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(jpg, headers=THUMB_HEADERS)


async def thumb(request):
    """First frame of an event: ~3 s download, one ffmpeg still, cached as jpg.
    200 jpg | 202 busy (Retry-After) | 404 no thumbnail | 409 still recording | 4xx/5xx real failure."""
    cam = get_cam(request)
    session.check_usable(cam)
    try:
        ts = int(request.query["ts"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text="ts must be an integer")
    native = bool(cam.get("p2p") and cam.get("native_thumbs"))   # experiment: the camera's own thumbnail over P2P
    mode = native_mode(cam) if native else None
    if native and mode is None:
        raise web.HTTPNotFound(text="the camera does not serve native thumbnails")
    if cam.get("thumbs") is False and not native:   # e.g. P2P cameras: a thumbnail would cost a whole P2P session each
        raise web.HTTPNotFound(text="no thumbnails for this camera")
    ahead = ts + 3 - media_limit(cam)
    if ahead > 0:   # the camera returns nothing for the newest seconds: tell the page when to ask again
        raise web.HTTPConflict(text="still recording", headers={"Retry-After": str(min(120, max(5, ahead + 2)))})
    jpg = CACHE / f"{cam['id']}_{ts}.jpg"
    if native and not jpg.exists():
        return await _native_thumb(cam, ts, jpg, mode)
    if not jpg.exists():
        tmp = f"thumb_{cam['id']}_{ts}.mp4"
        try:
            if not await _media_lock_free(cam):
                return thumb_busy(f"{cam['name']}: a clip download or export is using the camera")
            await fetch_media(cam, ts, ts + 3, tmp, raise_busy=True,
                              beside_live=cam.get("thumbs_beside_live", True) is not False)
            proc = await asyncio.create_subprocess_exec(
                ffmpeg_path(), "-v", "error", "-y", "-i", str(CACHE / tmp), "-frames:v", "1",
                "-vf", "scale=320:-2", str(jpg))
            await proc.wait()
        except slots_mod.Busy as e:
            return thumb_busy(str(e))
        except Exception as e:
            print(f"[thumb {cam['id']} {ts}] failed: {type(e).__name__}: {str(e)[:160]}", file=sys.stderr, flush=True)
            raise
        finally:
            (CACHE / tmp).unlink(missing_ok=True)
    if not jpg.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(jpg, headers=THUMB_HEADERS)


# ---- export: a time range as video+audio or audio only ----------------------------------
# ---- export: a time range as video+audio or audio only (see kinora/services/export.py) --
async def export_start(request):
    if request.content_type != "application/json":   # forces a CORS preflight for cross-site callers
        raise web.HTTPUnsupportedMediaType()
    try:
        body = await request.json()
        cam = CAMS[body["cam"]]
        start, end, mode = int(body["start"]), int(body["end"]), body["mode"]
    except Exception:
        raise web.HTTPBadRequest(text="expected JSON {cam, start, end, mode}")
    if mode not in config.MODES:
        raise web.HTTPBadRequest(text=f"mode must be one of {sorted(config.MODES)}")
    session.check_usable(cam)
    try:
        slots.refuse_if_playing(cam, "export")
    except slots_mod.Busy as e:
        raise web.HTTPServiceUnavailable(text=str(e))
    end = min(end, media_limit(cam))
    if start <= 0 or end - start < 2:
        raise web.HTTPBadRequest(text="empty time range (or it ends within the last ~90 s of recording)")
    if end - start > config.MAX_EXPORT_S:
        raise web.HTTPBadRequest(text=f"range too long: max {config.MAX_EXPORT_S // 3600} hours per export")
    job = {"id": uuid.uuid4().hex[:8], "cam": cam["id"], "start": start, "end": end, "mode": mode,
           "state": "queued", "message": "Queued", "done": 0, "total": 0, "cancel": False, "file": None}
    export.jobs[job["id"]] = job

    async def list_recordings(date):
        return (await listings.listing_data(cam, date, "rec", _fetch_recordings))[0]
    asyncio.create_task(export.run(job, cam, list_recordings, fetch_media))
    return web.json_response({"id": job["id"]})


async def export_status(request):
    job = export.jobs.get(request.match_info["id"])
    if not job:
        raise web.HTTPNotFound()
    return web.json_response({k: v for k, v in job.items() if k != "cancel"})


async def export_cancel(request):
    job = export.jobs.get(request.match_info["id"])
    if not job:
        raise web.HTTPNotFound()
    job["cancel"] = True
    return web.json_response({"ok": True})


async def export_file(request):
    name = request.match_info["name"]
    f = EXPORTS / name
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not f.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(f, headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---- pan / tilt (see kinora/services/ptz.py) --------------------------------------------
async def ptz(request):
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    try:
        body = await request.json()
        cam, direction = CAMS[body["cam"]], body["dir"]
    except Exception:
        raise web.HTTPBadRequest(text="expected JSON {cam, dir}")
    session.check_usable(cam)
    await ptz_service.step(api, cam, direction)
    return web.json_response({"ok": True})


# ---- live view (RTSP -> HLS), independent of the API login budget -----------------------


async def live_stop(request):
    """Close a camera's live relay now, freeing its video slot (the page calls this when you leave Live)."""
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    try:
        cam = CAMS[(await request.json())["cam"]]
    except Exception:
        raise web.HTTPBadRequest(text="expected JSON {cam}")
    await live.stop(LIVE_ROOT, cam)
    return web.json_response({"ok": True})


async def live_route(request):
    cam = CAMS.get(request.match_info["cam"])
    if not cam:
        raise web.HTTPNotFound()
    session.check_usable(cam)   # RTSP carries the camera password: never opened for a switched-off camera
    try:
        slots.refuse_if_playing(cam, "use live view")
    except slots_mod.Busy as e:
        raise web.HTTPServiceUnavailable(text=str(e))
    return await live.live_file(request, cam, LIVE_ROOT)


# ---- streaming playback for P2P-only cameras (HLS) --------------------------------------


def require_stream(cam, scale="1/1"):
    """P2P-only cameras stream normal playback ("1/1"); with fast_forward they stream every camera scale (2/1 full frames, 4/1, 8/1, 16/1 key frames).
    Cameras that serve playback on port 8800 and have fast_forward stream only the key-frame scales 8/1 and 16/1 (slower speeds use full pieces)."""
    if cam.get("fast_forward"):
        ok = scale in (("1/1", "2/1", "4/1", "8/1", "16/1") if cam.get("stream_playback") else ("8/1", "16/1"))
    else:
        ok = bool(cam.get("stream_playback")) and scale == "1/1"
    if not ok:
        raise web.HTTPNotImplemented(text=f"{cam['name']}: no streaming playback ({scale}) for this camera")


async def play_start(request):
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    try:
        body = await request.json()
        cam, ts, scale = CAMS[body["cam"]], int(body["ts"]), str(body.get("scale", "1/1"))
    except Exception:
        raise web.HTTPBadRequest(text="expected JSON {cam, ts[, scale]}")
    session.check_usable(cam)
    require_stream(cam, scale)
    ts = min(ts, media_limit(cam))     # the camera has nothing for the last ~90 s of recording
    if ts <= 0:
        raise web.HTTPBadRequest(text="invalid time")
    try:
        p = await p2p_play.start(cam, ts, scale)
    except p2p_play.PlayError as e:
        code = {"busy": web.HTTPServiceUnavailable, "superseded": web.HTTPConflict,
                "toomany": web.HTTPTooManyRequests}.get(e.kind, web.HTTPBadGateway)
        raise code(text=f"{cam['name']}: {e.text}")
    return web.json_response(p.status())


async def play_stop(request):
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    try:
        cam = CAMS[(await request.json())["cam"]]
    except Exception:
        raise web.HTTPBadRequest(text="expected JSON {cam}")
    await p2p_play.stop(cam["id"], "stopped")
    return web.json_response({"ok": True})


async def play_status(request):
    cam = get_cam(request)
    p = p2p_play.get(cam["id"])
    return web.json_response(p.status() if p else {"state": "none"}, headers={"Cache-Control": "no-store"})


async def play_file(request):
    cam = CAMS.get(request.match_info["cam"])
    if not cam:
        raise web.HTTPNotFound()
    f = p2p_play.file_for(cam["id"], request.match_info["sid"], request.match_info["file"])
    if f is None:
        raise web.HTTPNotFound()
    return hls.file_response(f)


CLIP_CACHE_MAX = 300 * 1024**2    # clips (*.mp4), least recently used go first
THUMB_CACHE_MAX = 120 * 1024**2   # thumbnails are ~10 KB each: ~12000 of them, evicted oldest first
# clips + thumbnails + meta/ + live/play session leftovers stay well below 512 MB


def _evict(files, limit):
    files = sorted(files, key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    while files and total > limit:
        f = files.pop(0)
        try:
            total -= f.stat().st_size
            f.unlink()
        except OSError:
            pass


def prune_cache():
    """Keep the cache bounded: videos are evicted aggressively, thumbnails liberally."""
    _evict([f for f in CACHE.glob("*.mp4") if not f.name.endswith(".part.mp4")], CLIP_CACHE_MAX)
    # today's thumbnails (named <cam>_<event epoch>.jpg) are never evicted on size
    midnight = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def is_today(f):
        try:
            return int(f.stem.rsplit("_", 1)[1]) >= midnight
        except (IndexError, ValueError):
            return False
    _evict([f for f in CACHE.glob("*.jpg") if not is_today(f)], THUMB_CACHE_MAX)
    # abandoned HLS session dirs (a running session rewrites its files constantly)
    cutoff = time.time() - 3600
    for d in PLAY_ROOT.glob("*/*"):
        try:
            if d.is_dir() and max((f.stat().st_mtime for f in d.rglob("*") if f.is_file()), default=0) < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


async def prune(app):
    for f in CACHE.glob("*.part.mp4"):   # leftovers from an interrupted download
        f.unlink(missing_ok=True)
    hls.kill_orphans(LIVE_ROOT)         # ffmpeg left running by a killed server holds an RTSP slot
    live.sweep_secret_lists()           # RTSP-carrying ffconcat temp files left by a killed server
    creds.announce_dev_secrets()        # one line when KINORA_DEV_SECRETS=1 (developer fallbacks on)
    try:
        ffmpeg_path()
    except BinaryMissing as e:
        print("WARNING:", e.text, file=sys.stderr)   # not fatal: only the features that need ffmpeg fail

    async def loop():
        while True:
            try:
                await asyncio.to_thread(prune_cache)
            except Exception as e:
                print("cache prune failed:", e, file=sys.stderr)
            await asyncio.sleep(60)
    app["prune"] = asyncio.create_task(loop())
    app["reaper"] = asyncio.create_task(live.reaper(LIVE_ROOT, CAMS))
    app["health"] = asyncio.create_task(health.loop(CAMS))
    p2p_play.startup()
    app["play_reaper"] = asyncio.create_task(p2p_play.reaper())
    yield
    app["prune"].cancel()
    app["reaper"].cancel()
    app["health"].cancel()
    app["play_reaper"].cancel()
    await p2p_play.stop_all()
    await live.stop_all(LIVE_ROOT, CAMS)


ALLOWED_HOSTS = config.allowed_hosts(config.PORT)
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}
STATE_CHANGING = ("POST", "PUT", "PATCH", "DELETE")
# Routes another computer may call while sharing is on (method, route canonical). Everything else answers 403 to a
# remote peer, including unmatched paths and HEAD. LOCAL_ONLY_ROUTES lists the rest; tests/test_allow.py checks that every
# route of make_app is in one of the two sets.
REMOTE_ROUTES = {
    ("GET", "/"), ("GET", "/static/{name}"), ("GET", "/api/cameras"), ("GET", "/api/status"),
    ("GET", "/api/recordings"), ("GET", "/api/events"), ("GET", "/api/thumb"), ("GET", "/api/clip"),
    ("GET", "/live/{cam}/{file}"), ("POST", "/api/play/start"), ("POST", "/api/play/stop"),
    ("GET", "/api/play/status"), ("GET", "/play/{cam}/{sid}/{file}"), ("POST", "/api/live/stop"),
    ("POST", "/api/export"), ("GET", "/api/export/{id}"), ("POST", "/api/export/{id}/cancel"),
    ("GET", "/exports/{name}"), ("POST", "/api/ptz"),
}
LOCAL_ONLY_ROUTES = {
    ("GET", "/api/setup/state"), ("POST", "/api/setup/account"), ("POST", "/api/setup/cameras"),
    ("POST", "/api/setup/reach"), ("GET", "/api/setup/discover"), ("POST", "/api/setup/discover"),
    ("POST", "/api/setup/verify"), ("PUT", "/api/cameras/order"), ("PATCH", "/api/cameras/{cam_id}"),
    ("POST", "/api/cameras/{cam_id}/credentials"), ("DELETE", "/api/cameras/{cam_id}"),
    ("GET", "/api/setup/devices"), ("POST", "/api/setup/devices"), ("DELETE", "/api/setup/devices/{ip}"),
    ("POST", "/api/setup/devices/dismiss"), ("PUT", "/api/setup/sharing"),
}
NO_BODY_ROUTES = {"/api/export/{id}/cancel"}   # body-less POST from the page: the Origin check still applies
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; worker-src 'self' blob:; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def _peer_ip(request):
    try:
        ip = ipaddress.ip_address(request.remote or "")
    except ValueError:
        return None
    return getattr(ip, "ipv4_mapped", None) or ip


def _socket_ip(request):
    """The address this connection arrived on: this computer's own address when its owner opens the shown address."""
    transport = request.transport
    sock = transport.get_extra_info("sockname") if transport is not None else None
    try:
        ip = ipaddress.ip_address(sock[0]) if sock else None
    except (ValueError, TypeError, IndexError):
        return None
    return getattr(ip, "ipv4_mapped", None) or ip


def _is_admin_route(request):
    """Setup and camera settings: only the computer the server runs on may change them (or read the setup state)."""
    p = request.path
    return p.startswith("/api/setup/") or (p.startswith("/api/cameras/") and request.method != "GET")


MACHINE_NAMES = {n.lower() for n in (socket.gethostname(), socket.gethostname().split(".")[0] + ".local", socket.getfqdn())}


def _lan_host(host):
    """A Host header that names this computer by an IP address or machine name on our port: a person opening the address."""
    name, _, port = (host or "").rpartition(":")
    if port != str(config.PORT):
        return False
    try:
        ipaddress.ip_address(name.strip("[]"))
        return True
    except ValueError:
        return name.lower() in MACHINE_NAMES


def _host_ok(host, remote):
    """Loopback clients: localhost / 127.0.0.1 on our port. Other computers: this machine's IP address or name on our
    port (_lan_host); any other name is refused, so a rebound DNS name cannot reach the server."""
    if host in ALLOWED_HOSTS:
        return True
    return bool(remote and _lan_host(host))


def _route_key(request):
    resource = getattr(request.match_info.route, "resource", None)
    return (request.method, getattr(resource, "canonical", None))


def _forbid(text):
    resp = web.Response(status=403, text=text)
    resp.force_close()   # a refused connection is not kept alive
    return resp


@web.middleware
async def local_only(request, handler):
    """Block DNS-rebinding / cross-site reads of the (private) camera video, and devices that are not allowed.
    Another computer (a peer that is not this computer) may only call REMOTE_ROUTES, and only while sharing is on."""
    ip = _peer_ip(request)
    loopback = bool(ip and ip.is_loopback)
    local = loopback or bool(ip and ip == _socket_ip(request))   # this computer, opened by its own address
    if not local:
        if not devices.sharing_on():
            return _forbid("Kinora: sharing is off, so other computers cannot connect. Turn it on in Settings > Other devices.")
        if not devices.is_allowed(ip):
            if ip and _lan_host(request.headers.get("Host")):
                devices.note_denied(str(ip))   # waits in Settings > Other devices until the owner allows it
            return _forbid(f"Kinora: this device ({request.remote or 'unknown'}) is not allowed yet. Ask the owner to allow it in Settings > Other devices.")
        if _is_admin_route(request):
            return _forbid("settings can only be changed on the computer running Kinora")
        if _route_key(request) not in REMOTE_ROUTES:
            return _forbid("only the computer running Kinora can use this")
    host = request.headers.get("Host")
    if not _host_ok(host, remote=not loopback):
        raise web.HTTPForbidden(text="bad Host header")
    if request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
        raise web.HTTPForbidden(text="cross-site request refused")
    if request.method in STATE_CHANGING:
        # browsers always send Origin on fetch POST; a missing or foreign one is refused (no fallback to Sec-Fetch-Site).
        # Another computer: only its own address as Origin.
        allowed = (ALLOWED_ORIGINS | {f"http://{host}"}) if loopback else {f"http://{host}"}
        if request.headers.get("Origin") not in allowed:
            raise web.HTTPForbidden(text="missing or foreign Origin refused")
        route = getattr(request.match_info.route, "resource", None)
        if request.content_type != "application/json" and getattr(route, "canonical", None) not in NO_BODY_ROUTES:
            raise web.HTTPUnsupportedMediaType(text="expected Content-Type: application/json")
    resp = await handler(request)
    # The page may only talk to this server: even a compromised script (hls.js, a future dependency)
    # cannot send camera data anywhere else.
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    return resp


STATIC_FILES = {"hls.min.js", "app.js", "setup.js"}


async def static_file(request):
    name = request.match_info["name"]
    if name not in STATIC_FILES:
        raise web.HTTPNotFound()
    return web.FileResponse(WEB / "static" / name, headers={"Cache-Control": "no-cache"})   # always re-validate: a new page version must load on a normal reload


@web.middleware
async def camera_errors(request, handler):
    """The one place where camera/service domain errors (kinora/errors.py) become HTTP responses."""
    try:
        return await handler(request)
    except CameraError as e:
        return web.Response(status=e.status, text=e.text)


def make_app(background=True):
    """The aiohttp application. background=False leaves out the cleanup context (cache prune, health loop, reapers):
    tests use it so that no task starts and nothing is probed."""
    app = web.Application(middlewares=[local_only, camera_errors])
    app[devices_api.LISTEN] = {"runner": None, "lan_site": None}   # filled by _serve (kinora/services/devices_api.py)
    if background:
        app.cleanup_ctx.append(prune)
    app.add_routes([
        web.get("/", lambda r: web.FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})),
        web.get("/api/cameras", cameras),
        web.get("/api/status", status),
        web.get("/api/recordings", recordings),
        web.get("/api/events", events),
        web.get("/api/thumb", thumb),
        web.get("/api/clip", clip),
        web.get("/live/{cam}/{file}", live_route),
        web.post("/api/play/start", play_start),
        web.post("/api/play/stop", play_stop),
        web.get("/api/play/status", play_status),
        web.get("/play/{cam}/{sid}/{file}", play_file),
        web.post("/api/live/stop", live_stop),
        web.get("/static/{name}", static_file),
        web.post("/api/export", export_start),
        web.post("/api/ptz", ptz),
        web.get("/api/export/{id}", export_status),
        web.post("/api/export/{id}/cancel", export_cancel),
        web.get("/exports/{name}", export_file),
        # first-run setup and camera settings (kinora/services/setup.py)
        web.get("/api/setup/state", setup_api.state),
        web.post("/api/setup/account", setup_api.account),
        web.post("/api/setup/cameras", setup_api.add_camera),
        web.post("/api/setup/reach", setup_api.reach),
        web.get("/api/setup/discover", discover_api.suggest),
        web.post("/api/setup/discover", discover_api.discover),
        web.post("/api/setup/verify", setup_api.verify),
        web.put("/api/cameras/order", setup_api.reorder_cameras),
        web.patch("/api/cameras/{cam_id}", setup_api.patch_camera),
        web.post("/api/cameras/{cam_id}/credentials", setup_api.camera_credentials),
        web.delete("/api/cameras/{cam_id}", setup_api.delete_camera),
        # other devices (kinora/services/devices_api.py): the allowed list, waiting devices, the sharing switch
        web.get("/api/setup/devices", devices_api.get_devices),
        web.post("/api/setup/devices", devices_api.add_device),
        web.delete("/api/setup/devices/{ip}", devices_api.remove_device),
        web.post("/api/setup/devices/dismiss", devices_api.dismiss_device),
        web.put("/api/setup/sharing", devices_api.set_sharing),
    ])
    return app


app = make_app()

async def _serve(app):
    """Listen until SIGINT / SIGTERM, then stop the sockets and run the cleanup contexts (as web.run_app does)."""
    runner = web.AppRunner(app)
    app[devices_api.LISTEN]["runner"] = runner
    await runner.setup()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await devices_api.listen(app)
        address = devices_api.view(app)["address"]
        if address:
            print(f"Other devices on your network can open {address}", flush=True)
        await stop.wait()
    finally:
        await runner.cleanup()


def main(on_started=None):
    """Start the server (blocking). Used by `python server.py` and by kinora.cli.
    on_started() runs once the single-instance lock is held, just before the server starts listening."""
    prepend_ffmpeg_dir_to_path()          # KINORA_FFMPEG's folder first on PATH: pytapo finds ffmpeg/ffprobe beside it
    instance.record_port(config.PORT)   # the lock is already held: instance.startup() runs before this module is imported
    if on_started:
        on_started()
    try:
        asyncio.run(_serve(app))
    except KeyboardInterrupt:
        pass   # Ctrl+C where signal handlers are not available (Windows): the cleanup has run
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            sys.exit(f"Port {config.PORT} is already in use. Stop the other program, or set KINORA_PORT to another port.")
        sys.exit(f"Kinora could not listen on {config.HOST}:{config.PORT} ({e.strerror or e}). Check KINORA_HOST or --host.")


if __name__ == "__main__":
    main()
