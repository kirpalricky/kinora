"""Live view: relay a camera's RTSP stream to the browser as HLS (browsers can't play RTSP).

RTSP uses the separate camera account (rtspadmin), not the Tapo API login, so it does not
touch the login budget in server.py. One ffmpeg per camera is shared by all viewers, started on
demand and stopped after IDLE_S seconds without a playlist/segment request.
Video is copied (no re-encode); audio (G.711) is transcoded to AAC for browsers.
The RTSP URL (incl. password) is NOT put on ffmpeg's command line (it would show in `ps`): it goes into a
private (0600) ffconcat list file that ffmpeg reads at start-up and that is deleted a few seconds later.
"""
import asyncio
import os
import re
import sys
import tempfile
import shutil
import time
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from kinora import config
from kinora.camera import gate
from kinora.errors import BadInput
from kinora.binaries import ffmpeg_path
from kinora.camera.slots import slots
from kinora.creds import get_camera_credentials, secret_sources
from kinora.services import hls

IDLE_S = 45            # stop ffmpeg this long after the last request
START_TIMEOUT_S = 15   # wait this long for the first playlist
LIST_KEEP_S = 6        # ffmpeg reads the whole list file when it starts; delete it after this long
UNLINK_TRIES = 10      # Windows can keep the list open a moment after ffmpeg exits: retry ~10 s in total
UNLINK_GAP_S = 1.0
SECRET_LIST_MAX_AGE_S = 60   # startup sweep: a tapo-live-*.ffconcat older than this is stale
BLOCK_S = 300          # pause live for a camera whose relay died right after starting

_procs, _last, _blocked, _started = {}, {}, {}, {}
RESTART_GUARD_S = 30   # a relay that dies sooner than this after starting is not restarted in a loop
_locks = {}
RTSP_AUTH_RE = re.compile(rb"\b401 Unauthorized", re.IGNORECASE)   # the only ffmpeg stderr text that is acted on
STDERR_TAIL = 64      # bytes of stderr kept between reads, so a match split across two reads is seen
_watchers = set()


def _dir(live_root: Path, cam) -> Path:
    return live_root / cam["id"]


def running(cid) -> bool:
    p = _procs.get(cid)
    return p is not None and p.returncode is None


def idle_s(cid) -> float:
    """Seconds since the last playlist/segment request for this camera's relay."""
    return time.time() - _last.get(cid, 0)


async def stop(live_root, cam):
    await _stop(live_root, cam)


async def _stop(live_root, cam):
    p = _procs.pop(cam["id"], None)
    if p and p.returncode is None:
        p.terminate()
        try:
            await asyncio.wait_for(p.wait(), 5)
        except asyncio.TimeoutError:
            p.kill()
    shutil.rmtree(_dir(live_root, cam), ignore_errors=True)
    hls.pidfile(live_root, cam["id"]).unlink(missing_ok=True)


def _write_secret_list(url: str) -> str:
    """An ffconcat file holding the RTSP URL, readable by this user only (mkstemp creates it 0600)."""
    fd, path = tempfile.mkstemp(prefix="tapo-live-", suffix=".ffconcat")
    with os.fdopen(fd, "w") as f:
        f.write("ffconcat version 1.0\n")
        f.write("file '" + url.replace("'", "'\\''") + "'\n")
        f.write("option rtsp_transport tcp\noption timeout 10000000\n")
    return path


def _unlink(path: str, tries: int = 0) -> None:
    """Delete a secret list file. A failed delete is retried on the loop (never blocks it) for ~UNLINK_TRIES seconds."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError:
        if tries < UNLINK_TRIES:
            asyncio.get_running_loop().call_later(UNLINK_GAP_S, _unlink, path, tries + 1)


def sweep_secret_lists() -> None:
    """Startup: delete stale tapo-live-*.ffconcat files left by a server that was killed before it could unlink them."""
    cutoff = time.time() - SECRET_LIST_MAX_AGE_S
    for f in Path(tempfile.gettempdir()).glob("tapo-live-*.ffconcat"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


async def _watch_stderr(p, cam):
    """Drain ffmpeg's stderr so it can never block on a full pipe. The text is never logged or stored
    (with -v error it can contain the RTSP URL and password): only a camera auth rejection is acted on,
    and it is persisted (gate.note_rtsp_auth_failure) and stops the relay."""
    stream = getattr(p, "stderr", None)
    if stream is None:
        return
    tail, hit = b"", False
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        if not hit:
            window = tail + chunk
            if RTSP_AUTH_RE.search(window):
                hit = True
                try:
                    gate.note_rtsp_auth_failure(cam)
                finally:
                    if p.returncode is None:
                        p.terminate()
            tail = window[-STDERR_TAIL:]


NO_ACCOUNT_TEXT = "Add this camera's camera account in Settings to use Live view"


def _no_account_text(cam, e):
    """The plain reason Live is refused: a camera with no camera account (no leak of setup commands). Any other
    store error (an unreadable keyring) is passed on as it is."""
    try:
        missing = secret_sources(cam)["camera"] == "missing"
    except RuntimeError:
        missing = False
    return NO_ACCOUNT_TEXT if missing else str(e)


async def _start(live_root, cam, stream="stream1"):
    try:
        user, pw = get_camera_credentials(cam)
    except RuntimeError as e:   # an unreadable store or no camera account: refused, no RTSP attempt, not a camera failure
        raise BadInput(_no_account_text(cam, e)) from None
    d = _dir(live_root, cam)
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    url = f"rtsp://{quote(user, safe='')}:{quote(pw, safe='')}@{cam['ip']}/{stream}"
    listfile = _write_secret_list(url)
    p = await asyncio.create_subprocess_exec(
        ffmpeg_path(), "-v", "error", "-f", "concat", "-safe", "0",
        "-protocol_whitelist", "file,rtsp,tcp,rtp,udp", "-i", listfile,
        "-c:v", "copy", "-c:a", "aac", "-b:a", "64k",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
        "-hls_flags", "delete_segments+omit_endlist+independent_segments",
        "-hls_segment_filename", str(d / "seg_%05d.ts"), str(d / "index.m3u8"),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE)
    asyncio.get_running_loop().call_later(LIST_KEEP_S, _unlink, listfile)
    w = asyncio.ensure_future(_watch_stderr(p, cam))
    _watchers.add(w)
    w.add_done_callback(_watchers.discard)
    _procs[cam["id"]] = p
    _started[cam["id"]] = time.time()
    hls.pidfile(live_root, cam["id"]).write_text(str(p.pid))


async def live_file(request, cam, live_root: Path):
    name = request.match_info["file"]
    if not hls.FILE_RE.fullmatch(name):
        raise web.HTTPNotFound()
    cid = cam["id"]
    _last[cid] = time.time()
    if time.time() < _blocked.get(cid, 0):
        raise web.HTTPServiceUnavailable(text=f"{cam['name']}: live view paused after a failed start")
    reason = gate.live_disabled(cam)   # persisted: the camera rejected the RTSP password (survives restarts)
    if reason:
        raise web.HTTPServiceUnavailable(text=reason)
    async with _locks.setdefault(cid, asyncio.Lock()):
        p = _procs.get(cid)
        if p is None or p.returncode is not None:
            if p is not None:  # relay died on its own
                _procs.pop(cid, None)
                if time.time() - _started.get(cid, 0) < RESTART_GUARD_S:   # died right after start: do not loop
                    _blocked[cid] = time.time() + BLOCK_S
                    raise web.HTTPBadGateway(text=f"{cam['name']}: live relay keeps failing, paused for a while")
            if name != "index.m3u8":
                raise web.HTTPNotFound()
            await _start(live_root, cam)
        playlist = _dir(live_root, cam) / "index.m3u8"
        t0 = time.time()
        while name == "index.m3u8" and not playlist.exists():
            p = _procs.get(cid)
            if p is None or p.returncode is not None or time.time() - t0 > START_TIMEOUT_S:
                await _stop(live_root, cam)
                _blocked[cid] = time.time() + BLOCK_S
                # the RTSP auth refusal is persisted before the relay is stopped: say so rather than "busy"
                reason = gate.live_disabled(cam)
                raise web.HTTPBadGateway(text=reason or f"{cam['name']}: no RTSP video (camera busy or unreachable)")
            await asyncio.sleep(0.25)
    f = _dir(live_root, cam) / name
    if not f.exists():
        raise web.HTTPNotFound()
    return hls.file_response(f)


async def _stop_known(live_root: Path, cams, cid):
    """Stop a relay by camera id. A camera removed from the table still gets its relay stopped (no KeyError)."""
    try:
        await _stop(live_root, cams.get(cid) or {"id": cid})
    except Exception as e:   # one failing stop must not end the reaper
        print(f"live: stopping the relay of {cid} failed: {e}", file=sys.stderr)


async def reaper(live_root: Path, cams):
    """Stop idle relays; call as a background task. Never dies on a camera that disappeared."""
    while True:
        await asyncio.sleep(10)
        for cid in list(_procs):
            if time.time() - _last.get(cid, 0) > IDLE_S:
                await _stop_known(live_root, cams, cid)


async def stop_all(live_root: Path, cams):
    for cid in list(_procs):
        await _stop_known(live_root, cams, cid)


# Register with the shared video-slot owner (camera/slots.py): a clip may stop an idle relay, play stops it.
slots.register_live(running, idle_s, lambda cam: stop(config.LIVE_ROOT, cam))
