"""Playback from a camera that only serves SD-card playback over the app's P2P channel (see p2p.py).

open_playback(call, cam, start, days): async context manager yielding a PlaybackCtx = an authenticated pytapo media session
  over the P2P channel. Setup (day listing + user id + request_p2p) runs in ONE call() on ONE pytapo client, so a re-login in
  between cannot mix sessions; teardown is always: media session closed -> loopback bridge closed -> UDT shutdown.
fetch_window(...): saves [start, end] as MP4 (used by /api/clip and export).
  call(fn)  runs a blocking pytapo function on the camera's session under the server's per-camera lock (server.api semantics)
"""
import asyncio
import contextlib
import datetime
import json
import os
import time

from pytapo.media_stream.convert import Convert

from kinora import p2p
from kinora.binaries import require_pytapo_tools

BUSY_CODES = (-52405, -52407, -52417, -52435)


class NoVideo(Exception):
    pass


class Busy(Exception):
    pass


class Unsupported(Exception):
    """The camera refused the native-thumbnail request or answered with something that is not an image (experiment, see fetch_thumbnail)."""
    def __init__(self, reason, raw=None):
        super().__init__(reason)
        self.reason, self.raw = reason, raw


class NoSnapshot(NoVideo):
    """The camera acknowledged a snapshot request but has no image for that time (continuous recordings, pytapo notes). Not a camera-wide refusal."""


THUMB_MAX_BYTES = 200 * 1024
THUMB_HARD_S = 15
THUMB_SILENCE_S = 8


def day_list(start, end=None):
    """Days to list before playback. The camera plays from the day listed LAST in the login, so the day of `start` comes last."""
    d0 = datetime.datetime.fromtimestamp(start).date()
    d1 = datetime.datetime.fromtimestamp(end if end else start).date()
    return ([d1.strftime("%Y%m%d")] if d1 != d0 else []) + [d0.strftime("%Y%m%d")]


class PlaybackCtx:
    def __init__(self, ms, uid, stream, codes, direct=False):
        self.ms, self.uid, self.stream, self.codes, self.direct = ms, uid, stream, codes, direct
        self.needs_play = not direct          # the P2P (app) flow has a second 'do play' step; pytapo's direct flow starts after the first request

    def first_request(self, start, scale="1/1"):
        if self.direct:                        # pytapo's own 'playback' request form, as used by Downloader(method="playback")
            return json.dumps({"type": "request", "seq": 1, "params": {"playback": {
                "client_id": self.uid, "channels": [0, 1], "scale": scale, "start_time": str(start),
                "end_time": str(start + 6 * 3600), "event_type": [1, 2]}, "method": "get"}})
        return json.dumps({"type": "request", "seq": 1, "params": {"playback": {
            "audio_config": {"encode_type": "G711alaw", "sample_rate": "8"}, "auto_switch_date": "0", "channels": [0],
            "client_id": self.uid, "event_type": [1], "filter": True, "scale": scale, "start_time": str(start)}, "method": "get"}})

    async def send_play(self, session, start, scale="1/1"):
        """The app's second step ('do play'), bound to the session id the camera returned. It repeats the scale of the first request."""
        ms = self.ms
        body = json.dumps({"type": "request", "seq": 3, "params": {"play": {
            "auto_switch_date": "0", "scale": scale, "auto_seek": "0", "start_time": str(start)}, "method": "do"}},
            separators=(",", ":")).encode()
        hdr = {b"X-Session-Id": str(session).encode(), b"Content-Type": b"application/json",
               b"Content-Length": str(len(body)).encode(), b"X-Data-Window-Size": b"50"}
        await ms._send_http_request(b"--" + ms.client_boundary, hdr)
        ms._writer.write(body + b"\r\n")
        await ms._writer.drain()


def prepare_session(tt, cam, days):
    """Blocking, runs under the camera's api lock on ONE client. The camera rejects playback (-52402) or sends nothing unless the
    day to play was the LAST one listed in this login: every other listing call (page loads!) changes that, so list again every time."""
    for day in days:
        tt.getRecordings(day)
    uid = tt.getUserID()
    sock, sid, addr = p2p.request_session(tt, cam["ip"])
    return tt, uid, sock, sid, addr


@contextlib.asynccontextmanager
async def open_playback(call, cam, days):
    t, uid, sock, sid, addr = await call(lambda tt: prepare_session(tt, cam, days))
    stream = await p2p.connect(sock, sid, addr)      # closes the socket itself on failure
    br = p2p.Bridge(stream)
    try:
        codes = []
        orig = stream.reader.feed_data

        def feed(d):                                  # remember the camera's JSON error codes (pytapo drops them)
            if len(d) < 600 and d[:2] == b"--" and b"application/json" in d:
                try:
                    body = json.loads(d.split(b"\r\n\r\n", 1)[-1].decode("latin1").strip())
                    ec = (body.get("params") or {}).get("error_code")
                    if ec not in (None, 0):
                        codes.append(ec)
                except Exception:
                    pass
            return orig(d)

        stream.reader.feed_data = feed
        port = await br.start()
        ms = t.getMediaSession(None)
        ms.ip, ms.port, ms.window_size = "127.0.0.1", port, 50
        async with ms:
            yield PlaybackCtx(ms, uid, stream, codes)
    finally:
        await br.close()


@contextlib.asynccontextmanager
async def open_direct(call, cam, days):
    """Playback over the camera's normal media port (8800), for cameras that serve playback there (Kitchen, Living Room).
    Same teardown guarantees as open_playback; no P2P layer."""
    def setup(tt):
        for day in days:                       # the camera plays from the last day listed in this login
            tt.getRecordings(day)
        return tt, tt.getUserID()

    t, uid = await call(setup)
    ms = t.getMediaSession(None)
    ms.window_size = 50
    async with ms:
        codes = []
        orig = ms._reader.feed_data

        def feed(d):                           # remember the camera's JSON error codes (pytapo drops them)
            if len(d) < 600 and d[:2] == b"--" and b"application/json" in d:
                try:
                    body = json.loads(d.split(b"\r\n\r\n", 1)[-1].decode("latin1").strip())
                    ec = (body.get("params") or {}).get("error_code")
                    if ec not in (None, 0):
                        codes.append(ec)
                except Exception:
                    pass
            return orig(d)

        ms._reader.feed_data = feed
        yield PlaybackCtx(ms, uid, None, codes, direct=True)


async def fetch_window(call, t, cam, start, end, out_path, stall_s=20, hard_limit_s=None):
    """Saves [start, end] as MP4 at out_path. Playback is ~1x realtime and does not stop at end_time: the clip is cut by measured length."""
    require_pytapo_tools()   # pytapo's Convert needs ffprobe and ffmpeg on PATH: report that, not a camera failure
    seg = end - start
    hard_limit_s = hard_limit_s or seg * 1.6 + 40
    marks = {"begin": time.time()}
    convert, got_video, saved, codes = Convert(), False, False, []
    try:
        async with open_playback(call, cam, day_list(start, end)) as pc:
            codes = pc.codes
            marks["session"] = time.time()
            gen = pc.ms.transceive(pc.first_request(start))
            t0, last_len, sent_play, last_calc, last_exact = time.time(), 0.0, False, 0.0, 0.0
            try:
                while time.time() - t0 < hard_limit_s:
                    try:
                        resp = await asyncio.wait_for(gen.__anext__(), stall_s)
                    except (asyncio.TimeoutError, StopAsyncIteration):
                        break
                    if resp.session is not None and not sent_play:
                        sent_play = True
                        await pc.send_play(resp.session, start)
                    if resp.mimetype == "video/mp2t":
                        marks.setdefault("first_video", time.time())
                        got_video = True
                        convert.write(resp.plaintext, resp.audioPayload, resp.audioPayloadType, 8000)
                        if time.time() - last_calc >= 1.0:   # probing the length is costly: at most once a second
                            last_calc = time.time()
                            ln = await convert.getLengthAsync()
                            if ln:
                                last_len = ln
                        if last_len >= seg:
                            # the cheap length is an estimate that overshoots during the initial burst: confirm it
                            if time.time() - last_exact >= 2.5:
                                last_exact = time.time()
                                exact = await convert.getLengthAsync(exact=True)
                                if exact and exact >= seg:
                                    break
                                last_len = exact or 0.0
                            else:
                                last_len = 0.0
            finally:
                try:
                    await gen.aclose()
                except Exception:
                    pass
            marks["stream_done"] = time.time()
            if got_video:
                exact = await convert.getLengthAsync(exact=True)
                await convert.save(out_path, min(seg, exact) if exact else seg, "ffmpeg")
                saved = True
                marks["saved"] = time.time()
    finally:
        b = marks["begin"]
        print("p2p window %s..%s (%ds): " % (start, end, seg) + ", ".join("%s +%.1fs" % (k, v - b) for k, v in marks.items() if k != "begin"), flush=True)
    if not saved:
        if any(c in BUSY_CODES for c in codes):
            raise Busy("camera is busy (stream slots in use)")
        raise NoVideo("camera returned no video" + (" (error %s)" % codes[-1] if codes else ""))
    return True


def thumbnail_request(cam, uid, start):
    """The app's 'get playback display cover' request (decompile: GetImageRequest + GetCoverParams). GUESSES: wrapped like every other
    stream-control request ({type, seq, params:{<kind>:{...}, method}}), mac taken from the camera record, channel 0."""
    img = {"type": "playback_display", "start_time": str(start), "channel": 0, "seq_image_type": "thumbnail"}
    if cam.get("mac"):
        img["mac"] = str(cam["mac"])
    return json.dumps({"type": "request", "seq": 1, "params": {"image": img, "method": "get"}})


def _jpeg_ok(b):
    return len(b) > 4 and b[:2] == b"\xff\xd8" and b.rstrip(b"\0")[-2:] == b"\xff\xd9"


async def fetch_thumbnail(call, t, cam, start, out_path, log=None, hard_s=THUMB_HARD_S, silence_s=THUMB_SILENCE_S):
    """EXPERIMENT: ask the camera for its own thumbnail of the recording starting at `start` (what the Tapo app does) and save the JPEG
    at out_path (written as .part, renamed). One P2P session, always released. Caller holds slots.clip.
    Raises Busy (camera stream slots in use), Unsupported (error code, a reply with no image, or silence: the caller remembers it),
    NoVideo (truncated or oversized image, session trouble). log(line) gets the first JSON reply (400 chars, no secrets)."""
    out_path = str(out_path)
    part = out_path + ".part"

    async def run():
        async with open_playback(call, cam, day_list(start)) as pc:
            got_reply = False
            buf, expect = bytearray(), None
            gen = pc.ms.transceive(thumbnail_request(cam, pc.uid, start), no_data_timeout=silence_s)
            try:
                async for resp in gen:
                    if resp.mimetype == "application/json":
                        try:
                            data = json.loads(resp.plaintext.decode())
                        except (ValueError, AttributeError):
                            continue
                        params = data.get("params") or {}
                        if not got_reply:
                            got_reply = True
                            if log:
                                log("first reply: " + json.dumps(data, separators=(",", ":"))[:400])
                        ec = params.get("error_code", data.get("error_code"))
                        if data.get("type") == "response" and ec not in (0, None):
                            if ec in BUSY_CODES:
                                raise Busy("camera is busy (stream slots in use)")
                            raise Unsupported("error_code %s" % ec, data)
                        for item in params.get("seq_image_lens") or []:
                            try:
                                expect = int(item.get("length"))
                            except (TypeError, ValueError, AttributeError):
                                pass
                        if params.get("event_type") == "stream_status" and params.get("status") == "finished":
                            break
                    elif resp.mimetype.startswith("image/") or resp.mimetype == "application/octet-stream":
                        buf += resp.plaintext
                        if len(buf) > THUMB_MAX_BYTES:
                            raise NoVideo("thumbnail larger than %d bytes" % THUMB_MAX_BYTES)
                        if _jpeg_ok(bytes(buf)) and (expect is None or len(buf) >= expect):
                            break
            finally:
                try:
                    await gen.aclose()
                except Exception:
                    pass
            if buf:
                if not _jpeg_ok(bytes(buf)):
                    raise NoVideo("truncated thumbnail (%d bytes)" % len(buf))
                with open(part, "wb") as f:
                    f.write(bytes(buf))
                os.replace(part, out_path)
                return True
            if any(c in BUSY_CODES for c in pc.codes):
                raise Busy("camera is busy (stream slots in use)")
            raise Unsupported("reply without an image" if got_reply else "no reply (silence)")

    try:
        return await asyncio.wait_for(run(), hard_s)
    except asyncio.TimeoutError:
        raise NoVideo("thumbnail session timed out after %d s" % hard_s)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(part)


def snapshot_request(uid, player_id, start):
    """What pytapo's getRecordingSnapshot puts on the wire (pytapo/media_stream/snapshot.py), byte for byte in content:
    {"type":"request","seq":N,"params":{"download":{"client_id","channels":[0],"media_type":2,"start_time":"<ts>","player_id"},"method":"get"}}.
    pytapo's transceive overwrites seq with a random one, so ours does too (seq 1 here is a placeholder)."""
    return json.dumps({"type": "request", "seq": 1, "params": {"download": {
        "client_id": uid, "channels": [0], "media_type": 2, "start_time": str(start), "player_id": player_id}, "method": "get"}})


async def fetch_snapshot(call, t, cam, start, out_path, log=None, hard_s=THUMB_HARD_S, silence_s=THUMB_SILENCE_S):
    """EXPERIMENT 2: pytapo's getRecordingSnapshot request (media_type 2, download-style) over OUR P2P media session. Saves the JPEG at out_path
    (.part, renamed). One P2P session, always released; caller holds slots.clip. Outcomes as fetch_thumbnail, except: silence after an
    error-free acknowledgement is NoSnapshot (no image for this recording, not remembered); silence with no reply at all is Unsupported."""
    out_path = str(out_path)
    part = out_path + ".part"

    async def run():
        async with open_playback(call, cam, day_list(start)) as pc:
            got_reply, buf = False, bytearray()
            player_id = getattr(t, "playerID", None) or ""
            gen = pc.ms.transceive(snapshot_request(pc.uid, player_id, start), no_data_timeout=silence_s)
            try:
                async for resp in gen:
                    if resp.mimetype == "application/json":
                        try:
                            data = json.loads(resp.plaintext.decode())
                        except (ValueError, AttributeError):
                            continue
                        params = data.get("params") or {}
                        if not got_reply:
                            got_reply = True
                            if log:
                                log("first reply: " + json.dumps(data, separators=(",", ":"))[:400])
                        ec = params.get("error_code", data.get("error_code"))
                        if data.get("type") == "response" and ec not in (0, None):
                            if ec in BUSY_CODES:
                                raise Busy("camera is busy (stream slots in use)")
                            raise Unsupported("error_code %s" % ec, data)
                        if params.get("event_type") == "stream_status" and params.get("status") == "finished":
                            break
                    elif resp.mimetype == "image/jpeg":
                        buf += resp.plaintext
                        if len(buf) > THUMB_MAX_BYTES:
                            raise NoVideo("snapshot larger than %d bytes" % THUMB_MAX_BYTES)
                        if _jpeg_ok(bytes(buf)):
                            break
            finally:
                try:
                    await gen.aclose()
                except Exception:
                    pass
            if buf:
                if not _jpeg_ok(bytes(buf)):
                    raise NoVideo("truncated snapshot (%d bytes)" % len(buf))
                with open(part, "wb") as f:
                    f.write(bytes(buf))
                os.replace(part, out_path)
                return True
            if any(c in BUSY_CODES for c in pc.codes):
                raise Busy("camera is busy (stream slots in use)")
            if got_reply:
                raise NoSnapshot("no snapshot for this recording")
            raise Unsupported("no reply (silence)")

    try:
        return await asyncio.wait_for(run(), hard_s)
    except asyncio.TimeoutError:
        raise NoVideo("snapshot session timed out after %d s" % hard_s)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(part)
