"""p2p_media.fetch_windows (batch thumbnails): one playback session for several short windows. Item 0 fails like fetch_window; a
refused second request on the session stops the loop (reuse_failed); should_stop ends it early; the channel list of the request
(thumbnails: channel 1) falls back once to the main stream for item 0. No camera: open_playback and _grab are replaced.
Run: python -m unittest tests.test_p2p_batch
"""
import asyncio
import contextlib
import io
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kinora import p2p_media  # noqa: E402

T0 = 1_000_000_000
BUSY = -52405


class BatchBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.events, self.codes, self.grabs, self.chans, self.outcome = [], [], [], [], {}
        self.add_code = {}     # start -> a camera error code that the request adds to the session's codes

        @contextlib.asynccontextmanager
        async def fake_open(call, cam, days):
            self.events.append("open")
            try:
                yield types.SimpleNamespace(codes=self.codes)
            finally:
                self.events.append("close")

        async def fake_grab(pc, start, end, out_path, stall_s, hard_limit_s, marks, channels=None, info=None,
                            still=False, still_min_bytes=None, still_max_s=None):
            chan = tuple(channels) if channels else (0,)
            self.grabs.append((start, end, stall_s, hard_limit_s))
            self.chans.append(chan)
            if start in self.add_code:
                self.codes.append(self.add_code[start])
            if info is not None:            # a window with no video ends silently after stall_s
                info.update(end="silent", other=0)
            # (start, channel) outcome first, then the per-start outcome (True unless set)
            return self.outcome.get((start, chan), self.outcome.get(start, True))

        for p in (mock.patch.object(p2p_media, "open_playback", fake_open),
                  mock.patch.object(p2p_media, "_grab", fake_grab),
                  mock.patch.object(p2p_media, "require_pytapo_tools", lambda: None)):
            p.start()
            self.addCleanup(p.stop)

    def items(self, n):
        return [(T0 + 10 * i, self.dir / f"w{i}.mp4") for i in range(n)]

    def batch(self, items, **kw):
        self.err = io.StringIO()
        with contextlib.redirect_stderr(self.err):
            return asyncio.run(p2p_media.fetch_windows(None, None, {}, items, **kw))


class FetchWindows(BatchBase):
    def test_every_item_saved_in_order_on_one_session(self):
        seen = []

        async def on_item(i, start, out):
            seen.append((i, start, out))
        done, reuse_failed, stopped, codes = self.batch(self.items(3), on_item=on_item)
        self.assertEqual((done, reuse_failed, stopped, codes), ([0, 1, 2], False, False, []))
        self.assertEqual([g[0] for g in self.grabs], [T0, T0 + 10, T0 + 20])
        self.assertEqual([s[0] for s in seen], [0, 1, 2])
        self.assertEqual(self.events, ["open", "close"])   # one session, torn down once

    def test_windows_are_three_seconds_with_the_thumbnail_limits(self):
        self.batch(self.items(1))
        self.assertEqual(self.grabs, [(T0, T0 + 3, 6, 12)])

    def test_item0_without_video_raises_like_fetch_window(self):
        self.outcome[T0] = False
        with self.assertRaises(p2p_media.NoVideo):
            self.batch(self.items(3))
        self.assertEqual(len(self.grabs), 1)            # no later item after a failed first one
        self.assertEqual(self.events, ["open", "close"])

    def test_item0_busy_code_raises_busy(self):
        self.outcome[T0] = False
        self.codes.append(BUSY)
        with self.assertRaises(p2p_media.Busy):
            self.batch(self.items(2))

    def test_item2_without_video_is_reuse_failed_after_the_two_saved(self):
        self.outcome[T0 + 20] = False
        seen = []

        async def on_item(i, start, out):
            seen.append(i)
        done, reuse_failed, stopped, codes = self.batch(self.items(4), on_item=on_item)
        self.assertEqual((done, reuse_failed, stopped), ([0, 1], True, False))
        self.assertEqual(seen, [0, 1])
        self.assertEqual(len(self.grabs), 3)            # the loop stops at the failed item
        self.assertEqual(self.events, ["open", "close"])

    def test_should_stop_before_item1_ends_early(self):
        done, reuse_failed, stopped, _ = self.batch(self.items(3), should_stop=lambda: True)
        self.assertEqual((done, reuse_failed, stopped), ([0], False, True))
        self.assertEqual(len(self.grabs), 1)

    def test_should_stop_is_not_asked_before_item0(self):
        asked = []

        def stop():
            asked.append(True)
            return True
        done, _, stopped, _ = self.batch(self.items(2), should_stop=stop)
        self.assertEqual((done, stopped), ([0], True))
        self.assertEqual(len(asked), 1)                 # asked once, before item 1 only

    def test_log_line_has_counts_codes_and_no_secrets(self):
        self.outcome[T0 + 20] = False
        self.batch(self.items(3))
        lines = self.err.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^p2p batch n=3 ok=2 reuse_failed=True session \+[\d.]+s per-item (\+[\d.]+s ){2}\+[\d.]+s "
                                   r"items 0\[.*\] 1\[.*\] 2\[.*\] codes=\[\]$")
        self.assertNotRegex(lines[0], re.compile(r"pass|user|rtsp", re.I))

    def test_a_busy_code_on_a_later_item_is_a_stale_stream_not_a_reuse_refusal(self):
        self.add_code[T0 + 10] = BUSY
        self.outcome[T0 + 10] = False
        done, reuse_failed, stopped, codes = self.batch(self.items(3))
        self.assertEqual((done, reuse_failed, stopped, codes), ([0], False, False, [BUSY]))
        self.assertIn("busy code on a later request, stale stream holds slot?", self.err.getvalue())

    def test_the_first_json_reply_is_logged_once_per_item_and_cut_to_300_characters(self):
        self.batch(self.items(1))
        self.assertNotIn("first reply", self.err.getvalue())   # the fake sets no reply
        fake = types.SimpleNamespace(plaintext=b"x" * 500)
        self.assertEqual(len(p2p_media._reply_text(fake)), 300)
        self.assertEqual(p2p_media._reply_text(types.SimpleNamespace()), "")





class Channels(BatchBase):
    def test_first_request_default_is_the_main_stream_and_channels_are_passed_through(self):
        ctx = p2p_media.PlaybackCtx(None, "uid", None, [])
        self.assertEqual(json.loads(ctx.first_request(T0))["params"]["playback"]["channels"], [0])
        self.assertEqual(json.loads(ctx.first_request(T0, channels=[1]))["params"]["playback"]["channels"], [1])

    def test_thumbnail_batch_asks_only_the_channels_it_is_given(self):
        self.batch(self.items(2), channels=[1])
        self.assertEqual(self.chans, [(1,), (1,)])

    def test_item0_no_video_on_channel1_is_no_video_with_no_retry(self):
        self.outcome[(T0, (1,))] = False
        with self.assertRaises(p2p_media.NoVideo):
            self.batch(self.items(3), channels=[1])
        self.assertEqual(self.chans, [(1,)])      # no fallback request on the main stream

    def test_channel1_busy_is_busy_and_not_retried(self):
        self.outcome[(T0, (1,))] = False
        self.codes.append(BUSY)
        with self.assertRaises(p2p_media.Busy):
            self.batch(self.items(2), channels=[1])
        self.assertEqual(self.chans, [(1,)])

    def test_no_video_message_names_the_channels_the_codes_and_how_the_window_ended(self):
        self.outcome[(T0, (1,))] = False
        self.codes.extend([7])
        with self.assertRaises(p2p_media.NoVideo) as cm:
            self.batch(self.items(2), channels=[1])
        self.assertEqual(str(cm.exception), "no video on channels [1], silent for 6s, 0 other messages, codes [7]")

    def test_no_video_text_for_each_way_a_window_can_end(self):
        self.assertEqual(p2p_media._no_video([1], {"end": "silent", "other": 2}, [], 6, 12),
                         "no video on channels [1], silent for 6s, 2 other messages, codes []")
        self.assertEqual(p2p_media._no_video([0], {"end": "hard", "other": 0}, [5, 9], 6, 12),
                         "no video on channels [0], hard limit 12s, 0 other messages, codes [5, 9]")
        self.assertEqual(p2p_media._no_video(None, {"end": "done", "other": 1}, [], 6, 12),
                         "no video on channels [0], stream ended, 1 other messages, codes []")


class FetchWindowUnchanged(BatchBase):
    def test_fetch_window_still_saves_and_raises_as_before(self):
        out = self.dir / "one.mp4"
        with contextlib.redirect_stdout(io.StringIO()) as log:
            self.assertTrue(asyncio.run(p2p_media.fetch_window(None, None, {}, T0, T0 + 3, str(out))))
        self.assertRegex(log.getvalue(), r"^p2p window \d+\.\.\d+ \(3s\): session \+[\d.]+s\n$")   # the fake _grab sets no stream marks
        self.assertEqual(self.chans, [(0,)])            # fetch_window keeps the main stream by default
        self.outcome[T0] = False
        with self.assertRaises(p2p_media.NoVideo), contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(p2p_media.fetch_window(None, None, {}, T0, T0 + 3, str(out)))
        self.codes.append(BUSY)
        with self.assertRaises(p2p_media.Busy), contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(p2p_media.fetch_window(None, None, {}, T0, T0 + 3, str(out)))


if __name__ == "__main__":
    unittest.main()
