"""Streaming SD-card playback for P2P-only cameras (Baby Room, C100 v2): one P2P session -> ffmpeg -> HLS.   Design: docs/p2p-hls-design.md

One Play per camera at most. It owns the camera's media lock (via kinora.camera.slots) for its whole life, so clips / exports / thumbnails / the RTSP live relay for
that camera are refused (not queued) while it runs. A single feeder task owns the whole P2P session; stop() cancels it and its `finally` tears everything
down in a fixed order (media session -> loopback bridge -> UDT shutdown -> ffmpeg stdin closed so the playlist gets ENDLIST).
Files of an ended session are kept for a few minutes (a viewer may still be playing its buffer) and removed later.
Video only for now (the camera's G.711 audio is a separate step).
"""
import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid

from kinora import config, p2p_media
from kinora.binaries import ffmpeg_path
from kinora.errors import CameraError
from kinora.camera import session
from kinora.camera.slots import Busy, slots
from kinora.services import hls

IDLE_S = 45               # no playlist/segment request for this long -> stop feeding
LAG_S = 120               # the player is this many seconds of media behind what we produced (paused tab) -> stop feeding
LAG_QUIET_S = 15          # ...but only once no SEGMENT was requested for this long (a paused hls.js page still polls the playlist)
KEEP_ENDED_S = 300        # keep files of an ended session this long
MAX_RUN_S = 4 * 3600
STALL_S = 20              # camera sends nothing for this long -> ended
SETUP_TIMEOUT_S = 15
WRITE_TIMEOUT_S = 10
MIN_SETUP_GAP_S = 2.0     # rapid scrubbing: spaced out, not refused
MAX_STARTS_PER_MIN = 10
LOCK_WAIT_S = 25          # a start waits this long for clip downloads that are already running (they finish within seconds)
SEG_S = 2                 # nominal; copy-mode segments are cut on key frames
FILE_RE = hls.FILE_RE
SID_RE = re.compile(r"^[0-9a-f]{12}$")

_sessions = {}            # cid -> Play (the current / last one)
_old = []                 # (finished_at, dir) of sessions whose files are waiting for removal
_tokens, _start_locks, _last_setup, _starts = {}, {}, {}, {}


def _log(p, msg):
    """One line to stderr and to <session dir>/play.log (kept with the session so a failure can be diagnosed afterwards)."""
    line = "[play %s %s +%.1fs] %s" % (p.cid, p.sid, time.time() - p.started, msg)
    print(line, flush=True)
    try:
        with open(p.dir / "play.log", "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


class PlayError(Exception):
    def __init__(self, kind, text):
        super().__init__(text)
        self.kind, self.text = kind, text      # kind: busy | superseded | toomany | nocamera | failed


def startup():
    """Server startup (called once from server.py): kill ffmpeg left by a killed server, wipe old session directories."""
    kill_orphans()


def _pidfile(cid):
    return hls.pidfile(config.PLAY_ROOT, cid)


def kill_orphans():
    root = config.PLAY_ROOT
    hls.kill_orphans(root)
    for d in root.iterdir():
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)


class Play:
    def __init__(self, cam, ts, scale="1/1"):
        self.cam, self.cid, self.ts = cam, cam["id"], int(ts)
        self.scale = scale                      # "1/1" = normal playback; "16/1" = key frames only (fast-forward), no audio
        self.direct = not cam.get("stream_playback")   # P2P (UDT) cameras vs cameras that serve playback on port 8800
        self.sid = uuid.uuid4().hex[:12]
        self.dir = config.PLAY_ROOT / self.cid / self.sid
        self.state, self.message = "starting", ""
        self.started = time.time()
        self.ended_at = None
        self.proc = None
        self.task = None
        self.last_get = time.time()
        self.last_seg = time.time()             # last SEGMENT request (the playlist alone does not count: a paused page keeps polling it)
        self.max_seg = -1
        self.codes = []
        self.ready = None
        self.lock_held = False
        self.tearing = False
        self.bytes = 0
        self.chunks = 0
        self.other = 0

    def segments(self):
        try:
            return sum(1 for n in os.listdir(self.dir) if n.startswith("seg_") and n.endswith(".ts"))
        except OSError:
            return 0

    def status(self):
        return {"sid": self.sid, "state": self.state, "message": self.message, "start": self.ts,
                "playlist": f"/play/{self.cid}/{self.sid}/index.m3u8" if (self.dir / "index.m3u8").exists() else None,
                "segments": self.segments(), "age": int(time.time() - self.started), "codes": self.codes[-3:], "scale": self.scale}


def get(cid):
    return _sessions.get(cid)


def active(cid):
    p = _sessions.get(cid)
    return bool(p) and p.state in ("starting", "playing")


def file_for(cid, sid, name):
    """Path of a served file or None. Marks the session as being watched."""
    p = _sessions.get(cid)
    if not p or p.sid != sid or not SID_RE.match(sid) or not FILE_RE.match(name):
        return None
    p.last_get = time.time()
    m = re.match(r"seg_(\d{5})\.ts$", name)
    if m:
        p.max_seg = max(p.max_seg, int(m.group(1)))
        p.last_seg = time.time()
    f = p.dir / name
    return f if f.exists() else None


async def _next(gen, timeout, early_stop=None):
    """Next item of the media generator with a timeout that does NOT cancel the generator (a cancelled pytapo generator is finalised)."""
    fut = asyncio.ensure_future(gen.__anext__())
    t0 = time.time()
    try:
        while True:
            done, _ = await asyncio.wait({fut}, timeout=0.5)
            if done:
                return fut.result()
            if early_stop and early_stop():
                raise asyncio.TimeoutError
            if time.time() - t0 > timeout:
                raise asyncio.TimeoutError
    except BaseException:
        if not fut.done():
            fut.cancel()
        raise


async def _spawn_ffmpeg(p: Play):
    p.dir.mkdir(parents=True, exist_ok=True)
    log = open(p.dir / "ffmpeg.log", "wb")
    p.proc = await asyncio.create_subprocess_exec(
        ffmpeg_path(), "-v", "error", "-probesize", "600k", "-analyzeduration", "1M",
        "-f", "mpegts", "-i", "pipe:0", "-map", "0:v", "-c:v", "copy",
        "-f", "hls", "-hls_time", str(SEG_S), "-hls_list_size", "0", "-hls_playlist_type", "event",
        "-hls_flags", "independent_segments+temp_file", "-hls_segment_filename", str(p.dir / "seg_%05d.ts"), str(p.dir / "index.m3u8"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=log)
    log.close()
    _pidfile(p.cid).write_text(str(p.proc.pid))


def _ffmpeg_tail(p):
    try:
        return (p.dir / "ffmpeg.log").read_text(errors="replace").strip()[-300:]
    except OSError:
        return ""


async def _feed(p: Play):
    """The one task that owns the P2P session. Sets p.ready (result or exception) once the camera accepted the playback request."""
    cam = p.cam
    call = lambda fn: session.api(cam, fn)  # noqa: E731
    try:
        await _spawn_ffmpeg(p)
        _log(p, "started: ts=%s scale=%s %s ffmpeg pid %s" % (p.ts, p.scale, "direct" if p.direct else "p2p", p.proc.pid))
        days = p2p_media.day_list(p.ts)
        opener = p2p_media.open_direct if p.direct else p2p_media.open_playback
        async with opener(call, cam, days) as pc:
            _log(p, "session up, playback request sent")
            gen = pc.ms.transceive(pc.first_request(p.ts, p.scale))
            sent_play = False
            t0 = time.time()
            last_report = time.time()
            try:
                while True:
                    limit = STALL_S if sent_play else SETUP_TIMEOUT_S
                    try:
                        resp = await _next(gen, limit, early_stop=(lambda: any(c in p2p_media.BUSY_CODES or c == -52402 for c in pc.codes)) if not sent_play else None)
                    except asyncio.TimeoutError:
                        if not sent_play:
                            busy = any(c in p2p_media.BUSY_CODES for c in pc.codes)
                            raise PlayError("busy" if busy else "failed",
                                            "camera is busy (stream slots in use)" if busy else
                                            "camera refused the playback request" + (" (error %s)" % pc.codes[-1] if pc.codes else ""))
                        p.message = "the camera stopped sending video"
                        _log(p, "no data from the camera for %ds (chunks=%d bytes=%d)" % (limit, p.chunks, p.bytes))
                        break
                    except StopAsyncIteration:
                        if not sent_play:
                            raise PlayError("failed", "camera closed the playback session")
                        p.message = "the camera ended the stream"
                        _log(p, "camera/pytapo ended the stream (chunks=%d bytes=%d)" % (p.chunks, p.bytes))
                        break
                    p.codes = pc.codes
                    if resp.session is not None and not sent_play:
                        sent_play = True
                        if getattr(pc, "needs_play", True):
                            await pc.send_play(resp.session, p.ts, p.scale)
                        _log(p, "camera accepted playback (session %s), 'play' sent" % resp.session)
                        p.state = "playing"
                        if p.ready and not p.ready.done():
                            p.ready.set_result(True)
                    if resp.mimetype == "video/mp2t":
                        if p.chunks == 0:
                            _log(p, "first video chunk: %d bytes, audio payload %d bytes" % (len(resp.plaintext), len(resp.audioPayload or b"")))
                            try:
                                _log(p, "chunk headers: %s" % {k: v for k, v in dict(getattr(resp, "headers", None) or {}).items() if str(k).lower().startswith(("x-", "content-"))})
                            except Exception:
                                pass
                        p.chunks += 1
                        p.bytes += len(resp.plaintext)
                    else:
                        p.other += 1
                        try:
                            _log(p, "message: %s %s" % (resp.mimetype, (resp.plaintext.decode("latin1") if isinstance(resp.plaintext, bytes) else str(resp.plaintext))[:200].replace("\n", " ")))
                        except Exception:
                            pass
                    if time.time() - last_report >= 5:
                        last_report = time.time()
                        _log(p, "fed %d chunks / %d bytes, segments=%d, ffmpeg %s" % (p.chunks, p.bytes, p.segments(), "alive" if p.proc and p.proc.returncode is None else "EXITED %s" % (p.proc.returncode if p.proc else None)))
                    if resp.mimetype == "video/mp2t" and p.proc and p.proc.returncode is None:
                        p.proc.stdin.write(resp.plaintext)
                        await asyncio.wait_for(p.proc.stdin.drain(), WRITE_TIMEOUT_S)
                    elif p.proc and p.proc.returncode is not None:
                        p.message = "ffmpeg stopped: " + _ffmpeg_tail(p)
                        _log(p, p.message)
                        break
                    if time.time() - t0 > MAX_RUN_S:
                        p.message = "maximum playback time reached"
                        break
            finally:
                try:
                    await gen.aclose()
                except Exception:
                    pass
    except asyncio.CancelledError:
        p.message = p.message or "stopped"
        raise
    except PlayError as e:
        _log(p, "error: %s (codes %s)" % (e.text, p.codes))
        p.state, p.message = "error", e.text
        if p.ready and not p.ready.done():
            p.ready.set_exception(e)
    except BaseException as e:
        if isinstance(e, CameraError) or hasattr(e, "status_code") or hasattr(e, "text"):   # aiohttp HTTP errors from the login gate etc.: pass them on as they are
            err = e
        else:
            err = PlayError("failed", ("%s: %s" % (type(e).__name__, e))[:200])
        p.state, p.message = "error", getattr(err, "text", None) or str(err)
        if p.ready and not p.ready.done():
            p.ready.set_exception(err)
    finally:
        _log(p, "ending: state=%s message=%r chunks=%d bytes=%d segments=%d codes=%s" % (p.state, p.message, p.chunks, p.bytes, p.segments(), p.codes[-3:]))
        p.tearing = True          # stop() must not cancel us now: the teardown releases the media lock
        await _teardown(p)


async def _teardown(p: Play):
    """Always runs. The P2P context manager has already closed the media session, bridge and UDT by the time we get here."""
    proc = p.proc
    if proc is not None:
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()               # EOF -> ffmpeg finalises the playlist (ENDLIST)
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), 4)
        except Exception:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), 2)
            except Exception:
                pass
    _pidfile(p.cid).unlink(missing_ok=True)
    if p.state in ("starting", "playing"):
        p.state = "ended"
    p.ended_at = time.time()
    if p.ready and not p.ready.done():
        p.ready.set_exception(PlayError("failed", p.message or "playback ended before it started"))
    if p.lock_held:
        p.lock_held = False
        slots.release_play(p.cid)
    _old.append((p.ended_at, p.dir))


async def stop(cid, reason=None):
    p = _sessions.get(cid)
    if not p or not p.task:
        return
    if reason and not p.message:
        p.message = reason
    if not p.task.done() and not p.tearing:
        p.task.cancel()
    await asyncio.gather(p.task, return_exceptions=True)


async def stop_all():
    for cid in list(_sessions):
        await stop(cid)


async def start(cam, ts, scale="1/1"):
    """Start (or restart) playback at ts. Returns the Play once the camera accepted the request. Raises PlayError / aiohttp HTTP errors."""
    cid = cam["id"]
    tok = _tokens[cid] = _tokens.get(cid, 0) + 1
    lock = _start_locks.setdefault(cid, asyncio.Lock())
    async with lock:
        if _tokens[cid] != tok:
            raise PlayError("superseded", "a newer playback request replaced this one")
        now = time.time()
        _starts[cid] = [x for x in _starts.get(cid, []) if now - x < 60]
        if len(_starts[cid]) >= MAX_STARTS_PER_MIN:
            raise PlayError("toomany", "too many playback starts in a minute: wait a moment")
        _starts[cid].append(now)
        await stop(cid)
        wait = MIN_SETUP_GAP_S - (time.time() - _last_setup.get(cid, 0))
        if wait > 0:
            await asyncio.sleep(wait)
        if _tokens[cid] != tok:
            raise PlayError("superseded", "a newer playback request replaced this one")
        try:
            await slots.acquire_play(cam, LOCK_WAIT_S)   # waits for running clip downloads, then stops live
        except Busy as e:
            raise PlayError("busy", str(e))
        if _tokens[cid] != tok:           # a newer request arrived while we waited for the lock
            slots.release_play(cid)
            raise PlayError("superseded", "a newer playback request replaced this one")
        p = Play(cam, ts, scale)
        p.lock_held = True
        try:
            p.ready = asyncio.get_running_loop().create_future()
            _sessions[cid] = p
            _last_setup[cid] = time.time()
            p.task = asyncio.ensure_future(_feed(p))
        except BaseException:
            p.lock_held = False
            slots.release_play(cid)
            raise
        try:
            await asyncio.wait_for(asyncio.shield(p.ready), SETUP_TIMEOUT_S + 10)
            if _tokens[cid] != tok:      # a newer request arrived while we were setting up: it wins
                raise PlayError("superseded", "a newer playback request replaced this one")
        except asyncio.TimeoutError:
            await stop(cid)
            raise PlayError("failed", "the camera did not answer in time")
        except BaseException:
            await stop(cid)
            raise
        return p


async def reaper():
    """Background task: stop sessions nobody watches, remove old files."""
    while True:
        await asyncio.sleep(5)
        now = time.time()
        for cid, p in list(_sessions.items()):
            if p.state != "playing":
                continue
            if now - p.last_get > IDLE_S:
                await stop(cid, "Playback stopped because this page stopped watching. Pick a time to continue.")
            elif (p.segments() - (p.max_seg + 1)) * SEG_S > LAG_S and p.max_seg >= 0 and now - p.last_seg > LAG_QUIET_S:
                # a page that keeps fetching segments is never stopped for lag (a fast-forward session runs ahead of the player by design;
                # hls.js's buffer cap and IDLE_S bound it). A paused page that only polls the playlist is caught here.
                await stop(cid, "Playback paused to free the camera. Press play or pick a time to continue.")
        for item in list(_old):
            fin, d = item
            cur = _sessions.get(d.parent.name)
            if now - fin > KEEP_ENDED_S and not (cur and cur.dir == d and cur.state in ("starting", "playing")):
                shutil.rmtree(d, ignore_errors=True)
                _old.remove(item)


# Register with the shared video-slot owner (camera/slots.py): clips, exports and live are refused while a playback is open.
slots.register_play(active)
