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
import sys
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

    def first_request(self, start, scale="1/1", channels=None):
        """channels: the camera channel list of the request (default [0], the main stream). Thumbnails ask for [1], the lower-quality
        stream (untested on the C100). The direct form ignores it (it always lists [0, 1])."""
        if self.direct:                        # pytapo's own 'playback' request form, as used by Downloader(method="playback")
            return json.dumps({"type": "request", "seq": 1, "params": {"playback": {
                "client_id": self.uid, "channels": [0, 1], "scale": scale, "start_time": str(start),
                "end_time": str(start + 6 * 3600), "event_type": [1, 2]}, "method": "get"}})
        chans = [0] if channels is None else list(channels)
        return json.dumps({"type": "request", "seq": 1, "params": {"playback": {
            "audio_config": {"encode_type": "G711alaw", "sample_rate": "8"}, "auto_switch_date": "0", "channels": chans,
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


async def _grab(pc, start, end, out_path, stall_s, hard_limit_s, marks, channels=None, info=None,
                still=False, still_min_bytes=None, still_max_s=None):
    """One playback request on the open session pc: streams [start, end] into out_path. True when saved, False when the camera sent
    no video. marks gets first_video / stream_done / saved times (the caller prints them). info (optional dict) gets how the window
    ended: "length" (the exact-length confirm reached the window), "stream-end" (the camera ended the stream), "silent" (stall_s
    without a message), "hard" (hard_limit_s), "still" / "still-max" (still mode), and diagnostics: "other" (non-video messages),
    "chunks" and "bytes" (video), "len" (last cheap length), "exact" (last exact length), "since_first" (seconds from the first
    video chunk to the end). Diagnostics only.
    still=True (thumbnails, cameras.json thumb_fast): no pytapo Convert and no length probing. The MPEG-TS bytes of each video chunk
    are written to out_path as they arrive; the file is created at the first video chunk, so no video means no file. The request
    ends once at least still_min_bytes of video came in over at least 2 chunks ("still"), or still_max_s after the first video
    chunk ("still-max"), or on the silent / hard limits. The caller runs ffmpeg on the file (ffmpeg reads the container itself)."""
    seg = end - start
    convert, got_video, saved = (None if still else Convert()), False, False
    gen = pc.ms.transceive(pc.first_request(start, channels=channels))
    t0, last_len, sent_play, last_calc, last_exact = time.time(), 0.0, False, 0.0, 0.0
    how, other, sink, chunks, nbytes, exact_len = "hard", 0, None, 0, 0, None
    try:
        while time.time() - t0 < hard_limit_s:
            timeout, capped = stall_s, False
            if still and "first_video" in marks:
                left = marks["first_video"] + still_max_s - time.time()
                if left <= 0:
                    how = "still-max"
                    break
                if left < stall_s:
                    timeout, capped = left, True
            try:
                resp = await asyncio.wait_for(gen.__anext__(), timeout)
            except asyncio.TimeoutError:
                how = "still-max" if capped else "silent"
                break
            except StopAsyncIteration:
                how = "stream-end"
                break
            if resp.session is not None and not sent_play:
                sent_play = True
                await pc.send_play(resp.session, start)
            if resp.mimetype != "video/mp2t":
                other += 1
            if info is not None and resp.mimetype == "application/json" and "reply" not in info:
                info["reply"] = _reply_text(resp)
                info["reply_at"] = time.time()
            if resp.mimetype == "video/mp2t":
                marks.setdefault("first_video", time.time())
                got_video = True
                chunks += 1
                nbytes += len(resp.plaintext)
                if still:
                    if sink is None:
                        sink = open(out_path, "wb")
                    sink.write(resp.plaintext)
                    if nbytes >= still_min_bytes and chunks >= 2:
                        how = "still"
                        break
                    continue
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
                        exact_len = exact
                        if exact and exact >= seg:
                            how = "length"
                            break
                        last_len = exact or 0.0
                    else:
                        last_len = 0.0
    finally:
        try:
            await gen.aclose()
        except Exception:
            pass
        if sink is not None:
            sink.close()
    if info is not None:
        info.update(end=how, other=other, chunks=chunks, bytes=nbytes, len=None if still else last_len, exact=exact_len,
                    since_first=time.time() - marks["first_video"] if "first_video" in marks else None)
    marks["stream_done"] = time.time()
    if got_video and not still:
        exact = await convert.getLengthAsync(exact=True)
        await convert.save(out_path, min(seg, exact) if exact else seg, "ffmpeg")
    if got_video:
        saved = True
        marks["saved"] = time.time()
    return saved


async def fetch_window(call, t, cam, start, end, out_path, stall_s=20, hard_limit_s=None, channels=None,
                       still=False, still_min_bytes=None, still_max_s=None):
    """Saves [start, end] as MP4 at out_path. Playback is ~1x realtime and does not stop at end_time: the clip is cut by measured length.
    still=True: the raw MPEG-TS video bytes go to out_path instead (thumbnails, see _grab)."""
    require_pytapo_tools()   # pytapo's Convert needs ffprobe and ffmpeg on PATH: report that, not a camera failure
    seg = end - start
    hard_limit_s = hard_limit_s or seg * 1.6 + 40
    marks = {"begin": time.time()}
    saved, codes = False, []
    try:
        async with open_playback(call, cam, day_list(start, end)) as pc:
            codes = pc.codes
            marks["session"] = time.time()
            saved = await _grab(pc, start, end, out_path, stall_s, hard_limit_s, marks, channels=channels,
                                still=still, still_min_bytes=still_min_bytes, still_max_s=still_max_s)
    finally:
        b = marks["begin"]
        print("p2p window %s..%s (%ds): " % (start, end, seg) + ", ".join("%s +%.1fs" % (k, v - b) for k, v in marks.items() if k != "begin"), flush=True)
    if not saved:
        if any(c in BUSY_CODES for c in codes):
            raise Busy("camera is busy (stream slots in use)")
        raise NoVideo("camera returned no video" + (" (error %s)" % codes[-1] if codes else ""))
    return True


def _reply_text(resp):
    """The first JSON reply of a playback request for the log: 300 characters at most (the same kind of line as the native thumbnail's)."""
    try:
        return resp.plaintext.decode("utf-8", "replace")[:300]
    except (AttributeError, TypeError):
        return ""


def _item_text(i, mk, info):
    """One item's timeline for the batch log line: seconds after the item's start (reply, first_video, stream_done, saved)."""
    b = mk["begin"]
    parts = []
    if "reply_at" in info:
        parts.append("reply+%.1f" % (info["reply_at"] - b))
    parts += ["%s+%.1f" % (k, mk[k] - b) for k in ("first_video", "stream_done", "saved") if k in mk]
    kb = info.get("bytes")
    return "%d[%s end=%s other=%s chunks=%s bytes=%s len=%s exact=%s since_first=%s]" % (
        i, " ".join(parts), info.get("end", "-"), info.get("other", "-"), info.get("chunks", "-"),
        "-" if kb is None else "%dk" % (kb // 1024), _num(info.get("len")), _num(info.get("exact")),
        _num(info.get("since_first")))


def _num(v):
    """A diagnostic number for the log line: one decimal, or '-' when the window did not measure it."""
    return "-" if v is None else "%.1f" % v


def _no_video(channels, info, codes, stall_s, hard_limit_s):
    """The NoVideo text of a window with no video: the channels asked, how the window ended (see _grab), the non-video messages
    that arrived and the camera's error codes. Numbers and codes only, no secrets."""
    how = info.get("end", "silent")
    if how == "silent":
        when = "silent for %ss" % stall_s
    elif how == "hard":
        when = "hard limit %ss" % hard_limit_s
    else:
        when = "stream ended"
    return "no video on channels %s, %s, %d other messages, codes %s" % (
        list(channels or [0]), when, info.get("other", 0), codes)


async def fetch_windows(call, t, cam, items, *, seg=3, stall_s=6, hard_limit_s=12, should_stop=None, on_item=None,
                        channels=None, still=False, still_min_bytes=None, still_max_s=None):
    """EXPERIMENT (batch thumbnails): several short windows over ONE playback session. items = [(start, out_path), ...], all on the
    same local day (the caller guarantees it). Item 0 is fetched exactly like fetch_window (no video -> Busy or NoVideo). Items 1..
    send a second playback request on the same session; the camera may refuse that. If an item >= 1 gets no video, the loop stops:
    reuse_failed is True when the camera gave no busy code (a refused second request), and only logged ("stale stream holds slot?")
    when a busy code came (the earlier stream may still hold a camera slot; not a refusal). should_stop() is checked before each
    item >= 1; on_item(index, start, out_path) is awaited after each saved item. channels: the camera channels asked for (None = the
    main stream). Returns (done indexes, reuse_failed, stopped_early, codes): codes are the camera's error codes seen on the session.
    The session is always torn down (open_playback). still=True: each item is raw MPEG-TS (see _grab), the caller runs ffmpeg on it.
    Log lines go to stderr: the first JSON reply of each item and one batch line."""
    require_pytapo_tools()
    items = list(items)
    if not items:
        return [], False, False, []
    marks = {"begin": time.time()}
    done, reuse_failed, stopped_early, per_item, texts, codes = [], False, False, [], [], []
    try:
        async with open_playback(call, cam, day_list(items[0][0])) as pc:
            codes = pc.codes
            marks["session"] = time.time()
            for i, (start, out_path) in enumerate(items):
                if i and should_stop and should_stop():
                    stopped_early = True
                    break
                t_item = time.time()
                mk, info, before = {"begin": t_item}, {}, len(codes)
                ok = await _grab(pc, start, start + seg, str(out_path), stall_s, hard_limit_s, mk, channels=channels, info=info,
                                 still=still, still_min_bytes=still_min_bytes, still_max_s=still_max_s)
                per_item.append(time.time() - t_item)
                texts.append(_item_text(i, mk, info))
                if info.get("reply"):
                    print("p2p item %d first reply: %s" % (i, info["reply"]), file=sys.stderr, flush=True)
                if not ok:
                    busy = any(c in BUSY_CODES for c in (codes if i == 0 else codes[before:]))   # item 0: the session's codes
                    if i == 0:
                        if busy:
                            raise Busy("camera is busy (stream slots in use)")
                        raise NoVideo(_no_video(channels, info, list(codes), stall_s, hard_limit_s))
                    if busy:
                        print("p2p item %d: busy code on a later request, stale stream holds slot? codes %s"
                              % (i, list(codes)), file=sys.stderr, flush=True)
                    else:
                        reuse_failed = True   # the camera did not play a second request on this session
                    break
                done.append(i)
                if on_item:
                    await on_item(i, start, str(out_path))
    finally:
        b = marks["begin"]
        sess = "+%.1fs" % (marks["session"] - b) if "session" in marks else "failed"
        print("p2p batch n=%d ok=%d reuse_failed=%s session %s per-item %s items %s codes=%s"
              % (len(items), len(done), reuse_failed, sess, " ".join("+%.1fs" % d for d in per_item), " ".join(texts),
                 list(codes)), file=sys.stderr, flush=True)
    return done, reuse_failed, stopped_early, list(codes)


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
