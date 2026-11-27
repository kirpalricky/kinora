"""Fast still mode for P2P thumbnails (p2p_media._grab still=True, cameras.json thumb_fast): the raw MPEG-TS video of one playback
request is written as it arrives and the request ends after THUMB_STILL_MIN_BYTES in >= 2 chunks, or THUMB_STILL_MAX_S after the first
video chunk. Also: the end reasons reported in info, the per-item log text, and the thumb_fast flag (server.thumb_fast, config check).
No camera: the playback session is a fake pc whose stream is a scripted async generator; time is a fake clock for the 2.5 s limit.
Run: python -m unittest tests.test_thumb_still
"""
import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kinora import config, p2p_media  # noqa: E402
import server  # noqa: E402

VIDEO, JSON = "video/mp2t", "application/json"


class Clock:
    """Stands in for the time module inside p2p_media: time() is whatever the scripted stream says it is."""
    def __init__(self):
        self.now = 1_000.0

    def time(self):
        return self.now


def resp(mimetype, data=b""):
    return types.SimpleNamespace(mimetype=mimetype, plaintext=data, session=None, audioPayload=None, audioPayloadType=None)


class FakePC:
    """A playback session: ms.transceive(...) yields the scripted responses; send_play is never needed (no session in the replies)."""
    def __init__(self, stream):
        self.codes = []
        self.ms = types.SimpleNamespace(transceive=lambda request: stream())

    def first_request(self, start, channels=None):
        return "{}"


def scripted(items, clock=None, times=None, then_silent=False):
    """An async generator factory: yields items; when clock and times are given, the clock is set to times[i] before item i is
    yielded. then_silent: after the items the camera says nothing (the generator waits forever, like a stalled stream)."""
    async def stream():
        for i, item in enumerate(items):
            if clock is not None and times is not None:
                clock.now = times[i]
            yield item
        if then_silent:
            await asyncio.Event().wait()
    return stream


class GrabBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.out = self.dir / "thumb.ts"
        self.clock = Clock()
        p = mock.patch.object(p2p_media, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)

    def grab(self, stream, *, stall_s=6, hard_limit_s=12, still=True, marks=None, info=None, seg=1, **kw):
        marks = {} if marks is None else marks
        info = {} if info is None else info
        ok = asyncio.run(p2p_media._grab(FakePC(stream), 1000, 1000 + seg, str(self.out), stall_s, hard_limit_s, marks,
                                         info=info, still=still, still_min_bytes=server.THUMB_STILL_MIN_BYTES,
                                         still_max_s=server.THUMB_STILL_MAX_S, **kw))
        return ok, info, marks


class StillEnds(GrabBase):
    def test_stops_at_150_kb_in_two_or_more_chunks_and_writes_the_concatenated_stream(self):
        chunks = [bytes([n]) * 60_000 for n in (1, 2, 3, 4)]
        ok, info, marks = self.grab(scripted([resp(VIDEO, c) for c in chunks]))
        self.assertTrue(ok)
        self.assertEqual(info["end"], "still")
        self.assertEqual((info["chunks"], info["bytes"]), (3, 180_000))   # 120 KB after two chunks: not yet; the 4th is never read
        self.assertEqual(self.out.read_bytes(), chunks[0] + chunks[1] + chunks[2])
        self.assertIn("saved", marks)

    def test_two_chunks_are_needed_even_when_the_bytes_are_there(self):
        ok, info, _ = self.grab(scripted([resp(VIDEO, b"a" * 200_000), resp(VIDEO, b"b" * 10_000), resp(VIDEO, b"c" * 10)]))
        self.assertTrue(ok)
        self.assertEqual((info["end"], info["chunks"], info["bytes"]), ("still", 2, 210_000))
        self.assertEqual(self.out.read_bytes(), b"a" * 200_000 + b"b" * 10_000)

    def test_stops_2_5_s_after_the_first_video_chunk_with_fewer_bytes(self):
        # 30 KB a second from t=1001 (the first video), then the camera goes quiet: the deadline is t=1003.5, so the request
        # ends there with 90 KB (under the 150 KB minimum)
        items = [resp(VIDEO, b"x" * 30_000) for _ in range(3)]
        ok, info, marks = self.grab(scripted(items, self.clock, [1001.0, 1002.0, 1003.0], then_silent=True))
        self.assertTrue(ok)
        self.assertEqual((info["end"], info["chunks"], info["bytes"]), ("still-max", 3, 90_000))
        self.assertEqual(info["since_first"], 2.0)   # fake clock: the request ended at t=1003 (the deadline is 1003.5)
        self.assertEqual(self.out.read_bytes(), b"x" * 90_000)
        self.assertEqual(marks["first_video"], 1001.0)

    def test_no_video_writes_no_file_and_is_not_saved(self):
        ok, info, marks = self.grab(scripted([resp(JSON, b"{}")]))
        self.assertFalse(ok)
        self.assertEqual((info["end"], info["chunks"], info["bytes"]), ("stream-end", 0, 0))
        self.assertFalse(self.out.exists())
        self.assertNotIn("saved", marks)

    def test_silence_before_any_video_is_silent_and_writes_nothing(self):
        ok, info, _ = self.grab(scripted([], then_silent=True), stall_s=0.05)
        self.assertFalse(ok)
        self.assertEqual(info["end"], "silent")
        self.assertFalse(self.out.exists())

    def test_hard_limit_is_reported_as_hard(self):
        ok, info, _ = self.grab(scripted([resp(VIDEO, b"x" * 10)]), hard_limit_s=0.0)
        self.assertFalse(ok)
        self.assertEqual(info["end"], "hard")

    def test_silence_after_some_video_before_the_2_5_s_limit_is_silent_and_keeps_the_bytes(self):
        # no more chunks for stall_s (the stall limit is shorter than the still limit here): the video so far is kept
        ok, info, _ = self.grab(scripted([resp(VIDEO, b"x" * 30_000)], self.clock, [1001.0], then_silent=True), stall_s=0.05)
        self.assertTrue(ok)
        self.assertEqual((info["end"], info["bytes"]), ("silent", 30_000))
        self.assertEqual(self.out.read_bytes(), b"x" * 30_000)


class ConvertPathUnchanged(GrabBase):
    """still=False keeps pytapo's Convert: the length confirm ends the request as "length" and the clip is saved by Convert."""
    def test_length_confirm_is_reported_as_length_and_convert_saves(self):
        saves = []

        class FakeConvert:
            def write(self, *a):
                pass

            async def getLengthAsync(self, exact=False):
                return 5.0

            async def save(self, path, length, tool):
                saves.append((path, length, tool))
                Path(path).write_bytes(b"clip")

        with mock.patch.object(p2p_media, "Convert", FakeConvert):
            ok, info, marks = self.grab(scripted([resp(VIDEO, b"x" * 1000), resp(VIDEO, b"y" * 1000)]), still=False)
        self.assertTrue(ok)
        self.assertEqual((info["end"], info["chunks"], info["bytes"]), ("length", 1, 1000))
        self.assertEqual(info["len"], 5.0)
        self.assertEqual(info["exact"], 5.0)
        self.assertEqual(saves, [(str(self.out), 1, "ffmpeg")])   # min(seg, exact)
        self.assertEqual(self.out.read_bytes(), b"clip")

    def test_stream_end_is_reported_as_stream_end_not_done(self):
        class FakeConvert:
            def write(self, *a):
                pass

            async def getLengthAsync(self, exact=False):
                return 0.0

            async def save(self, path, length, tool):
                Path(path).write_bytes(b"clip")

        with mock.patch.object(p2p_media, "Convert", FakeConvert):
            ok, info, _ = self.grab(scripted([resp(VIDEO, b"x" * 10)]), still=False)
        self.assertTrue(ok)
        self.assertEqual(info["end"], "stream-end")


class LogText(unittest.TestCase):
    def test_item_text_has_the_end_reason_and_the_diagnostics_in_key_value_form(self):
        mk = {"begin": 100.0, "first_video": 100.6, "stream_done": 102.4, "saved": 102.5}
        info = {"reply_at": 100.2, "end": "still", "other": 2, "chunks": 3, "bytes": 267 * 1024 + 500, "len": None,
                "exact": None, "since_first": 1.8}
        self.assertEqual(p2p_media._item_text(0, mk, info),
                         "0[reply+0.2 first_video+0.6 stream_done+2.4 saved+2.5 end=still other=2 chunks=3 bytes=267k len=- "
                         "exact=- since_first=1.8]")

    def test_item_text_for_a_window_without_video(self):
        self.assertEqual(p2p_media._item_text(1, {"begin": 5.0}, {"end": "silent", "other": 0}),
                         "1[ end=silent other=0 chunks=- bytes=- len=- exact=- since_first=-]")


class Flag(unittest.TestCase):
    def cam(self, **extra):
        c = {"id": "cam_fast", "name": "Fast test", "auth": config.AUTH_METHODS[0], "ip": "10.20.30.40"}
        c.update(extra)
        return c

    def test_default_is_fast_and_false_is_the_convert_path(self):
        self.assertTrue(server.thumb_fast(self.cam()))
        self.assertTrue(server.thumb_fast(self.cam(thumb_fast=True)))
        self.assertFalse(server.thumb_fast(self.cam(thumb_fast=False)))

    def test_still_mode_is_p2p_only(self):
        self.assertTrue(server.still_mode(self.cam(p2p=True)))
        self.assertFalse(server.still_mode(self.cam(p2p=True, thumb_fast=False)))
        self.assertFalse(server.still_mode(self.cam(p2p=False)))

    def test_part_name_keeps_the_extension(self):
        self.assertEqual(server._part_name("thumb_c_100.ts"), "thumb_c_100.part.ts")
        self.assertEqual(server._part_name("c_1_2.mp4"), "c_1_2.part.mp4")

    def test_load_accepts_bool_only(self):
        for v in (True, False):
            config.check_camera(self.cam(thumb_fast=v))
        for bad in (0, 1, "true", None, 1.0):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config.check_camera(self.cam(thumb_fast=bad))

    def test_camera_without_the_key_loads(self):
        config.check_camera(self.cam())


if __name__ == "__main__":
    unittest.main()
