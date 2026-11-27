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
import logging
import math
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


def media_failed(cam, start, end, count=True):
    """Remember an empty download. Only old windows count towards pausing the camera: for recent
    ones 'no video' usually just means the recording is still being written or the camera is busy.
    count=False (thumbnails): the window is memoised but never counts towards the pause."""
    cid = cam["id"]
    _fail[(cid, start, end)] = time.time()
    if count and end < time.time() - COUNT_FAILS_OLDER_THAN:
        _media_fails[cid] = _media_fails.get(cid, 0) + 1
        if _media_fails[cid] >= 2:
            _media_block[cid] = time.time() + MEDIA_BLOCK_S


async def fetch_media(cam, start, end, name, *, raise_busy=False, beside_live=False, thumb=False):
    """Download [start, end] from the camera into CACHE/name. raise_busy: let slots.Busy through (thumb answers 202).
    thumb: a thumbnail's download (short limits, never counts towards the camera's download pause)."""
    if (CACHE / name).exists():
        return
    try:
        async with slots.clip(cam, beside_live=beside_live):
            await _download(cam, start, end, name, thumb=thumb)
    except slots_mod.Busy as e:
        if raise_busy:
            raise
        raise web.HTTPServiceUnavailable(text=str(e))


async def _download(cam, start, end, name, thumb=False):
    """Runs with the camera's media lock held (slots.clip). thumb: see fetch_media."""
    if (CACHE / name).exists():
        return
    require_pytapo_tools()   # before any login or media request: a missing ffprobe/ffmpeg is not a camera failure
    media_gate(cam, start, end)
    t = await asyncio.to_thread(client, cam)
    part = _part_name(name)                  # never serve or cache a half-written file
    (CACHE / part).unlink(missing_ok=True)
    count = not thumb                        # a thumbnail's failure is memoised per window, never pauses the camera
    if cam.get("p2p"):
        # SD playback only through the app's P2P (UDT) channel, e.g. C100 v2 fw 1.3.17: see p2p_media.py
        try:
            if thumb:
                await asyncio.wait_for(p2p_media.fetch_window(
                    lambda fn: api(cam, fn), t, cam, start, end, str(CACHE / part),
                    stall_s=THUMB_STALL_S, hard_limit_s=THUMB_HARD_S, channels=thumb_channels(cam),
                    still=still_mode(cam), still_min_bytes=THUMB_STILL_MIN_BYTES, still_max_s=THUMB_STILL_MAX_S), THUMB_TOTAL_S)
            else:
                await p2p_media.fetch_window(lambda fn: api(cam, fn), t, cam, start, end, str(CACHE / part))
        except p2p_media.Busy:
            raise web.HTTPServiceUnavailable(
                text=f"{cam['name']}: camera is busy (another app or viewer holds a video stream)")
        except (CameraError, web.HTTPException):   # a gate refusal must not count as a failed download
            raise
        except Exception as e:                      # includes asyncio.TimeoutError from the thumbnail limit
            if MARK in str(e):
                raise as_http(e)
            media_failed(cam, start, end, count=count)
            raise web.HTTPBadGateway(text=f"{cam['name']}: {str(e)[:160] or 'camera returned no video'}")
        if not (CACHE / part).exists():
            media_failed(cam, start, end, count=count)
            raise web.HTTPBadGateway(text=f"{cam['name']}: camera returned no video")
        os.replace(CACHE / part, CACHE / name)
        if count:
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
        media_failed(cam, start, end, count=count)
        raise web.HTTPBadGateway(text=f"{cam['name']}: camera returned no video")
    if isinstance(status, dict) and status.get("errorCode") in Downloader.BUSY_ERROR_CODES:
        raise web.HTTPServiceUnavailable(           # busy is not a failure: no memo, no pause
            text=f"{cam['name']}: camera is busy (another app or viewer holds a video stream)")
    if not (CACHE / part).exists():
        media_failed(cam, start, end, count=count)
        raise web.HTTPBadGateway(text=f"{cam['name']}: camera returned no video")
    os.replace(CACHE / part, CACHE / name)
    if count:
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
THUMB_P2P_GAP_S = 10        # least time between two P2P thumbnail sessions of one camera (the baby monitor has ~2 stream slots)
THUMB_STALL_S = 6           # P2P thumbnail download: stall limit and hard limit, short so the media lock is never held long
THUMB_HARD_S = 12
THUMB_TOTAL_S = 22          # overall cap of one thumbnail download: under services/play.py LOCK_WAIT_S (25 s)
THUMB_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}   # keyed by event time: never changes
_thumb_p2p_end = {}         # camera id -> time.monotonic() when its last thumbnail fetch attempt ended (P2P gap)
THUMB_SEG_S = 1             # seconds of video in one thumbnail clip: one frame is all a thumbnail needs (cameras.json thumb_seg, 1..3, per camera)
THUMB_STILL_MIN_BYTES = 150_000   # fast still mode (cameras.json thumb_fast, default on): a thumbnail's stream ends after this much video ...
THUMB_STILL_MAX_S = 2.5           # ... in at least 2 chunks, or this long after its first video chunk, whichever comes first
THUMB_BATCH_N = 4           # P2P batch: one session makes the requested thumbnail plus up to THUMB_BATCH_N-1 older neighbours (cameras.json thumb_batch)
THUMB_BATCH_BG_S = 25       # the rest of a batch may run this long beyond the batch start (it runs after the requested thumbnail is ready; Play waits 25 s)
_batch_unsupported = set()  # camera ids that refused a second playback request on one session (per process): single thumbnails from then on
_batch_tasks = {}           # camera id -> the background batch task; it holds the camera's media lock until it ends
THUMB_CHANNELS = [1]        # EXPERIMENT, off by default: channel 1 selects a second lens (the C100 has one): expected to fail, see OPERATIONS
MAIN_CHANNELS = [0]         # thumbnails' default: the main stream (what the app's playback uses)


async def _media_lock_free(cam, wait_s=THUMB_WAIT_S):
    """True when the camera's media lock is free within wait_s seconds (a short poll, never a queue)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while slots.busy(cam["id"]):
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


def thumb_busy(reason, retry_s=None):
    """202 'try later': the camera's video slot is in use (live view, playback, a clip). Not a failure."""
    return web.json_response({"state": "busy", "reason": reason}, status=202,
                             headers={"Retry-After": str(THUMB_RETRY_S if retry_s is None else retry_s)})


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
                try:
                    await fetch(lambda fn: api(cam, fn), t, cam, ts, str(jpg), log=log)
                finally:
                    _thumb_p2p_end[cid] = time.monotonic()
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


def thumb_channels(cam):
    """The camera channels a P2P thumbnail asks for: the main stream (channel 0) unless cameras.json says thumb_channel 1 (experiment)."""
    if cam.get("thumb_channel") == 1:
        return list(THUMB_CHANNELS)
    return list(MAIN_CHANNELS)


def _item0_budget(cam):
    """Seconds the requested thumbnail (batch item 0) may wait: THUMB_TOTAL_S with the main stream. With channel 1 opted in, one silent
    stall (THUMB_STALL_S) and 6 s more must fit as well."""
    return THUMB_TOTAL_S if thumb_channels(cam) == MAIN_CHANNELS else THUMB_TOTAL_S + THUMB_STALL_S + 6


def _why(err):
    """The cause of a failed batch for the log: type and message (160 chars at most), no secrets beyond the camera's own text."""
    if err is None:
        return "NothingSaved: no item was saved"
    text = str(err)[:160] or ("cancelled" if isinstance(err, asyncio.CancelledError) else "no message")
    return "%s: %s" % (type(err).__name__, text)


def _batch_on(cam):
    """P2P cameras batch their generic thumbnails unless cameras.json says thumb_batch false or the camera refused a second request."""
    return bool(cam.get("p2p")) and cam.get("thumb_batch", True) is not False and cam["id"] not in _batch_unsupported


def thumb_seg(cam):
    """Seconds of video one thumbnail clip asks for: THUMB_SEG_S, or cameras.json thumb_seg (an int 1..3, checked at load) for a
    camera whose stills come out wrong."""
    return cam.get("thumb_seg", THUMB_SEG_S)


def thumb_fast(cam):
    """cameras.json thumb_fast (bool, default true, checked at load): the P2P thumbnail stream is kept as raw MPEG-TS and given to
    ffmpeg without pytapo's Convert (fast still mode). false: the Convert-based clip of thumb_seg, as before."""
    return cam.get("thumb_fast", True) is not False


def still_mode(cam):
    """The fast still mode applies to P2P thumbnails only (the Downloader path of other cameras has no raw stream)."""
    return bool(cam.get("p2p")) and thumb_fast(cam)


def _part_name(name):
    """The half-written name of a download: thumb_x.ts -> thumb_x.part.ts, clip.mp4 -> clip.part.mp4."""
    root, ext = os.path.splitext(name)
    return root + ".part" + ext


def _batch_starts(cam, ts):
    """[ts] + up to THUMB_BATCH_N-1 older event starts of the same local day, newest first. The events come from the listing cache on
    disk (the camera is never asked). Skipped: a window that already has a thumbnail, failed recently or is still being recorded.
    No listing, or one that cannot be read, gives [ts] alone."""
    cid = cam["id"]
    seg = thumb_seg(cam)
    batch = [ts]
    if time.time() < _media_block.get(cid, 0):   # downloads paused: only the requested thumbnail (it answers 503)
        return batch
    day = datetime.datetime.fromtimestamp(ts)
    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    try:
        events = json.loads((META / f"{cid}_{day.strftime('%Y%m%d')}_ev.json").read_text())
        starts = sorted({e.get("start") for e in events if type(e.get("start")) is int}, reverse=True)
    except (OSError, ValueError, TypeError, AttributeError):
        return batch
    limit = media_limit(cam)
    for s in starts:
        if len(batch) >= THUMB_BATCH_N:
            break
        if not day_start <= s < ts or s + seg > limit:
            continue
        if (CACHE / f"{cid}_{s}.jpg").exists() or time.time() - _fail.get((cid, s, s + seg), 0) < WINDOW_FAIL_TTL:
            continue
        batch.append(s)
    return batch


async def _still(mp4, jpg):
    """One ffmpeg still (320 px wide) of a thumbnail clip: the command of the single thumbnail path."""
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_path(), "-v", "error", "-y", "-i", str(mp4), "-frames:v", "1", "-vf", "scale=320:-2", str(jpg))
    await proc.wait()


def _batch_error(cam, e):
    """The answer for a batch's failed requested thumbnail, mapped as _download and fetch_media do: a slots Busy is answered 202 by
    thumb(); a P2P Busy is 503, as before."""
    if isinstance(e, (slots_mod.Busy, CameraError, web.HTTPException)):
        return e
    if isinstance(e, p2p_media.Busy):
        return web.HTTPServiceUnavailable(text=f"{cam['name']}: camera is busy (another app or viewer holds a video stream)")
    if MARK in str(e):
        return as_http(e)
    return web.HTTPBadGateway(text=f"{cam['name']}: {str(e)[:160] or 'camera returned no video'}")


async def _batch_run(cam, items, first, state):
    """The batch as a background task (see _batch_thumb). It holds the camera's media lock from start to end. `first` (the requested
    thumbnail) is set as soon as its jpg exists; the other items only fill the cache. state["stage"] tracks where the batch is
    (setup | request | still | video | stopped) for the log. Every outcome is logged: a failed batch first, one line with its cause
    and stage, then the summary line. A plain failure is remembered for its window only (never counts towards the download pause)."""
    cid = cam["id"]
    seg = thumb_seg(cam)
    fast = still_mode(cam)
    saved, nofr, err, reuse_failed, stopped_early, codes = [], [], None, False, False, []

    async def on_item(i, start, out):
        state["stage"] = "still"
        jpg = CACHE / f"{cid}_{start}.jpg"
        await _still(out, jpg)
        if fast and not jpg.exists():   # the stream held no decodable frame: a failed window, memoised only (never counted)
            nofr.append(i)
            print(f"[thumb {cid} batch {items[0][0]}] failed: NoFrame (stage: still)", file=sys.stderr, flush=True)
            media_failed(cam, start, start + seg, count=False)
            if i == 0 and not first.done():
                first.set_exception(web.HTTPBadGateway(text=f"{cam['name']}: no frame in the recording"))
            state["stage"] = "video"
            return
        saved.append(i)
        state["stage"] = "video"
        if i == 0 and not first.done():
            first.set_result(True)

    try:
        async with slots.clip(cam, beside_live=cam.get("thumbs_beside_live", True) is not False):
            require_pytapo_tools()
            media_gate(cam, items[0][0], items[0][0] + seg)   # a refusal here never reaches the camera
            t = await asyncio.to_thread(client, cam)
            chans = thumb_channels(cam)
            state["stage"] = "request"
            _, reuse_failed, stopped_early, codes = await asyncio.wait_for(
                p2p_media.fetch_windows(lambda fn: api(cam, fn), t, cam, items, seg=seg, stall_s=THUMB_STALL_S,
                                        hard_limit_s=THUMB_HARD_S, should_stop=lambda: slots.play_wanted(cid), on_item=on_item,
                                        channels=chans, still=fast, still_min_bytes=THUMB_STILL_MIN_BYTES,
                                        still_max_s=THUMB_STILL_MAX_S),
                THUMB_TOTAL_S + THUMB_BATCH_BG_S)
            if stopped_early:
                state["stage"] = "stopped"
            if reuse_failed:   # not remembered per window: the single path may still work for that event
                _batch_unsupported.add(cid)
                print(f"[thumb {cid} batch] session reuse unsupported", file=sys.stderr, flush=True)
    except asyncio.CancelledError as e:   # the requested thumbnail's timeout or a shutdown: logged below, then re-raised
        err = e
        raise
    except Exception as e:                # Busy, CameraError, gate refusals, HTTPException: all logged, none silent
        err = e
    finally:
        stage = state["stage"]
        if stage != "setup":   # a refusal before the camera was reached leaves the P2P gap alone
            _thumb_p2p_end[cid] = time.monotonic()
        for _, out in items:
            Path(out).unlink(missing_ok=True)
        mapped = _batch_error(cam, err) if err is not None else web.HTTPBadGateway(text=f"{cam['name']}: camera returned no video")
        only_noframe = err is None and not saved and bool(nofr)   # the NoFrame lines already said why
        if (err is not None or not saved) and not only_noframe:
            print(f"[thumb {cid} batch {items[0][0]}] failed: {_why(err)} (stage: {stage})", file=sys.stderr, flush=True)
        if only_noframe:
            cause = "NoFrame: %d item(s) without a decodable frame" % len(nofr)
        elif err is not None or not saved:
            cause = _why(err)
        elif reuse_failed:
            cause = "ReuseRefused: the camera refused a second playback request"
        elif not stopped_early and len(saved) < len(items) and any(c in p2p_media.BUSY_CODES for c in codes):
            cause = "StaleStream: busy code on a later request"
        else:
            cause = "none"
        print(f"[thumb {cid} batch] n={len(items)} ok={len(saved)} cause={cause} stage={stage} codes={list(codes)}",
              file=sys.stderr, flush=True)
        if err is not None and isinstance(mapped, web.HTTPBadGateway) and len(saved) < len(items):
            s = items[len(saved)][0]
            media_failed(cam, s, s + seg, count=False)
        if not first.done():
            first.set_exception(mapped)


async def _batch_thumb(cam, ts, jpg):
    """The generic P2P thumbnail as a batch (p2p_media.fetch_windows, see _batch_starts). Answers as soon as ts's own jpg exists; the
    older neighbours finish in a background task that holds the camera's media lock until it ends, so other requests get 202 meanwhile."""
    cid = cam["id"]
    if not await _media_lock_free(cam):
        return thumb_busy(f"{cam['name']}: a clip download or export is using the camera")
    ext = ".ts" if still_mode(cam) else ".mp4"   # fast still mode: the raw stream, see thumb_fast
    items = [(s, CACHE / f"thumb_{cid}_{s}{ext}") for s in _batch_starts(cam, ts)]
    first = asyncio.get_running_loop().create_future()
    state = {"stage": "setup"}
    task = asyncio.create_task(_batch_run(cam, items, first, state))
    _batch_tasks[cid] = task

    def forget(done):
        if _batch_tasks.get(cid) is done:
            del _batch_tasks[cid]
    task.add_done_callback(forget)
    budget = _item0_budget(cam)
    try:
        await asyncio.wait_for(first, budget)
    except asyncio.TimeoutError:
        print(f"[thumb {cid} batch {ts}] requested thumbnail timed out after {budget}s at stage {state['stage']}",
              file=sys.stderr, flush=True)
        task.cancel()   # the batch ends now and releases the camera's media lock (it is never left running)
        media_failed(cam, ts, ts + thumb_seg(cam), count=False)
        raise web.HTTPBadGateway(text=f"{cam['name']}: camera returned no video")
    except slots_mod.Busy as e:
        return thumb_busy(str(e))
    if not jpg.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(jpg, headers=THUMB_HEADERS)


async def thumb(request):
    """First frame of an event: a THUMB_SEG_S (1 s) download, one ffmpeg still, cached as jpg.
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
    seg = thumb_seg(cam)
    ahead = ts + seg - media_limit(cam)
    if ahead > 0:   # the camera returns nothing for the newest seconds: tell the page when to ask again
        raise web.HTTPConflict(text="still recording", headers={"Retry-After": str(min(120, max(5, ahead + 2)))})
    jpg = CACHE / f"{cam['id']}_{ts}.jpg"
    last_end = _thumb_p2p_end.get(cam["id"])
    if cam.get("p2p") and not jpg.exists() and last_end is not None:   # one P2P thumbnail session, then a pause
        wait = THUMB_P2P_GAP_S - (time.monotonic() - last_end)
        if wait > 0:
            return thumb_busy(f"{cam['name']}: the last thumbnail is still being fetched from the camera",
                              retry_s=max(1, math.ceil(wait)))
    if native and not jpg.exists():
        return await _native_thumb(cam, ts, jpg, mode)
    if not jpg.exists() and _batch_on(cam):
        return await _batch_thumb(cam, ts, jpg)
    if not jpg.exists():
        still = still_mode(cam)
        tmp = f"thumb_{cam['id']}_{ts}{'.ts' if still else '.mp4'}"
        try:
            if not await _media_lock_free(cam):
                return thumb_busy(f"{cam['name']}: a clip download or export is using the camera")
            try:
                await fetch_media(cam, ts, ts + seg, tmp, raise_busy=True,
                                  beside_live=cam.get("thumbs_beside_live", True) is not False, thumb=True)
            finally:
                _thumb_p2p_end[cam["id"]] = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                ffmpeg_path(), "-v", "error", "-y", "-i", str(CACHE / tmp), "-frames:v", "1",
                "-vf", "scale=320:-2", str(jpg))
            await proc.wait()
            if still and not jpg.exists():   # the stream held no decodable frame: memoised only (never counted)
                print(f"[thumb {cam['id']} {ts}] failed: NoFrame (stage: still)", file=sys.stderr, flush=True)
                media_failed(cam, ts, ts + seg, count=False)
        except slots_mod.Busy as e:
            return thumb_busy(str(e))
        except Exception as e:
            print(f"[thumb {cam['id']} {ts}] failed: {type(e).__name__}: {str(e)[:160]}", file=sys.stderr, flush=True)
            raise
        finally:
            (CACHE / tmp).unlink(missing_ok=True)
            (CACHE / _part_name(tmp)).unlink(missing_ok=True)   # a still stream is written in place: a failed one leaves its part
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


async def ptz_presets(request):
    """The camera's saved presets for the Live pad's menu (fetched on demand: one camera login)."""
    cam = get_cam(request)
    session.check_usable(cam)
    return web.json_response(await ptz_service.presets(api, cam))


async def ptz_preset(request):
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    try:
        body = await request.json()
        cam, preset_id = CAMS[body["cam"]], body["id"]
    except Exception:
        raise web.HTTPBadRequest(text="expected JSON {cam, id}")
    session.check_usable(cam)
    await ptz_service.goto_preset(api, cam, preset_id)
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
    for task in list(_batch_tasks.values()):   # a background thumbnail batch: its P2P session ends with the server
        task.cancel()


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
    ("GET", "/api/ptz/presets"), ("POST", "/api/ptz/preset"),
}
LOCAL_ONLY_ROUTES = {
    ("GET", "/api/setup/state"), ("POST", "/api/setup/account"), ("POST", "/api/setup/cameras"),
    ("POST", "/api/setup/reach"), ("GET", "/api/setup/discover"), ("POST", "/api/setup/discover"),
    ("POST", "/api/setup/verify"), ("PUT", "/api/cameras/order"), ("PATCH", "/api/cameras/{cam_id}"),
    ("POST", "/api/cameras/{cam_id}/credentials"), ("DELETE", "/api/cameras/{cam_id}"),
    ("GET", "/api/setup/devices"), ("POST", "/api/setup/devices"), ("DELETE", "/api/setup/devices/{ip}"),
    ("GET", "/api/setup/devices/qr"),
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


# no socket.getfqdn(): it can block on a DNS lookup at import time (seen on macOS CI: the server took over 30 s to start)
_HOST = socket.gethostname()
MACHINE_NAMES = {n.lower() for n in (_HOST, _HOST.split(".")[0] + ".local")}


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
        web.get("/api/ptz/presets", ptz_presets),
        web.post("/api/ptz/preset", ptz_preset),
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
        web.get("/api/setup/devices/qr", devices_api.address_qr),
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


PYTAPO_NOISE = "Received response with no or invalid session information"


class _PytapoNoise(logging.Filter):
    """Drops exactly pytapo's PYTAPO_NOISE warning. The camera keeps sending into a playback session that has ended, and pytapo logs
    one warning per message. Every other record of that logger passes."""

    def filter(self, record):
        return not record.getMessage().startswith(PYTAPO_NOISE + " (sequence ")


def quiet_pytapo():
    """Install _PytapoNoise on pytapo's media session logger (pytapo/media_stream/session.py, logger.warning). Idempotent."""
    log = logging.getLogger("pytapo.media_stream.session")
    if not any(isinstance(f, _PytapoNoise) for f in log.filters):
        log.addFilter(_PytapoNoise())


def main(on_started=None):
    """Start the server (blocking). Used by `python server.py` and by kinora.cli.
    on_started() runs once the single-instance lock is held, just before the server starts listening."""
    quiet_pytapo()
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
