"""P2P thumbnail batches (server.py _batch_thumb / _batch_run): one playback session makes the requested thumbnail plus up to
THUMB_BATCH_N-1 older neighbours from the cached events listing; the requested one answers early, the rest finish in a background task
holding the camera's media lock; a refused second request falls back per process; every batch logs its outcome (cause, stage, codes);
thumbnails use the main stream (channel 0) unless cameras.json says thumb_channel 1 (experiment).
No camera, no ffmpeg: p2p_media.fetch_windows, the ffmpeg still and the single-thumbnail download are replaced; any unexpected call to the
real fetch_windows fails the test. Run: python -m unittest tests.test_thumb_batch
"""
import asyncio
import datetime
import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import server  # noqa: E402
from kinora import p2p_media  # noqa: E402
from kinora.camera import slots as slots_mod  # noqa: E402
from kinora.camera.slots import slots  # noqa: E402

HOST = "127.0.0.1:8080"
TS = int(datetime.datetime(2001, 9, 9, 12, 0).timestamp())   # local noon: older events stay on the same local day
JPG = b"\xff\xd8not-a-real-image-for-tests\xff\xd9"
BUSY = -52405


async def request(cid, ts):
    client = TestClient(TestServer(server.make_app(background=False)))
    await client.start_server()
    try:
        resp = await client.get(f"/api/thumb?cam={cid}&ts={ts}", headers={"Host": HOST})
        return resp.status, dict(resp.headers), await resp.read()
    finally:
        await client.close()


async def settle(cid):
    """Waits until the camera's background batch has ended (fails the test if it never does)."""
    for _ in range(1000):
        if cid not in server._batch_tasks:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the background batch did not end")


def fake_batch(test, *, fail=None, reuse_fail=False, gate=None):
    """A stand-in for p2p_media.fetch_windows: writes each item's clip and calls on_item as the real loop does, returns
    (done, reuse_failed, stopped_early, codes) like the real one. fail: {start: exception} raised for that item. reuse_fail: item 1
    is refused (reuse_failed). gate: an asyncio.Event the loop waits for before item 1."""
    async def run(call, t, cam, items, *, seg=server.THUMB_SEG_S, stall_s=6, hard_limit_s=12, should_stop=None, on_item=None, channels=None,
                  still=False, still_min_bytes=None, still_max_s=None):
        test.fetches.append({"items": [s for s, _ in items], "seg": seg, "stall_s": stall_s, "hard_limit_s": hard_limit_s,
                             "should_stop": should_stop, "channels": channels, "still": still})
        done = []
        for i, (start, out) in enumerate(items):
            if i and should_stop and should_stop():
                return done, False, True, []
            if i == 1 and gate is not None:
                await gate.wait()
            if fail and start in fail:
                raise fail[start]
            if i == 1 and reuse_fail:
                return done, True, False, []
            Path(out).write_bytes(b"video")
            done.append(i)
            await on_item(i, start, str(out))
        return done, False, False, []
    return run


class BatchBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name) / "cache"
        self.cache.mkdir()
        self.meta = Path(tmp.name) / "meta"
        self.meta.mkdir()
        self.cid = next(iter(server.CAMS))
        cam = dict(server.CAMS[self.cid], p2p=True, thumbs=True, enabled=True, verified=True)
        cam.pop("native_thumbs", None)
        self.fetches, self.single = [], []
        self.batch = fake_batch(self)   # replaced per test; the default fails loudly if a test forgot to set it

        async def no_batch(*a, **kw):
            raise AssertionError("the batch reached fetch_windows without a test replacing it")

        async def dispatch(*a, **kw):
            return await self.batch(*a, **kw)

        async def single(cam, start, end, name, **kw):
            self.single.append((start, end))
            (self.cache / name).write_bytes(b"video")

        async def fake_exec(*args, **kw):
            Path(args[-1]).write_bytes(JPG)
            proc = mock.Mock()
            proc.wait = mock.AsyncMock(return_value=0)
            return proc

        self.cam = cam
        for p in (mock.patch.object(server, "CACHE", self.cache),
                  mock.patch.object(server, "META", self.meta),
                  mock.patch.dict(server.CAMS, {self.cid: cam}),
                  mock.patch.object(server.session, "check_usable", lambda cam: None),
                  mock.patch.object(slots, "_live_running", lambda cid: False),
                  mock.patch.object(slots, "_play_active", lambda cid: False),
                  mock.patch.dict(server._fail, clear=True),
                  mock.patch.dict(server._media_fails, clear=True),
                  mock.patch.dict(server._media_block, clear=True),
                  mock.patch.dict(server._thumb_p2p_end, clear=True),
                  mock.patch.dict(server._batch_tasks, clear=True),
                  mock.patch.object(server, "_batch_unsupported", set()),
                  mock.patch.object(server, "require_pytapo_tools", lambda: None),
                  mock.patch.object(server, "client", lambda cam: object()),
                  mock.patch.object(server, "ffmpeg_path", lambda: "ffmpeg"),
                  mock.patch.object(server, "fetch_media", single),
                  mock.patch.object(server.asyncio, "create_subprocess_exec", fake_exec),
                  mock.patch.object(p2p_media, "fetch_windows", dispatch),
                  mock.patch.object(p2p_media, "fetch_window", no_batch)):
            p.start()
            self.addCleanup(p.stop)

    def write_events(self, starts):
        day = datetime.datetime.fromtimestamp(TS).strftime("%Y%m%d")
        (self.meta / f"{self.cid}_{day}_ev.json").write_text(
            json.dumps([{"start": s, "end": s + 10, "type": 1} for s in starts]))

    def run_async(self, coro):
        with redirect_stderr(io.StringIO()) as err:
            result = asyncio.run(coro)
        return result, err.getvalue()


class BatchContents(BatchBase):
    def test_batch_is_the_request_then_the_newest_older_uncached_events_up_to_batch_n(self):
        # newest first: TS-200 is cached and TS-300 failed recently, so they are skipped; the batch has THUMB_BATCH_N = 4 items
        self.write_events([TS - 200, TS - 300, TS - 600, TS - 700, TS + 100, TS - 900, TS - 1000])
        (self.cache / f"{self.cid}_{TS - 200}.jpg").write_bytes(JPG)           # cached: skipped
        server._fail[(self.cid, TS - 300, TS - 300 + server.THUMB_SEG_S)] = time.time()   # failed recently: skipped

        async def run():
            got = await request(self.cid, TS)
            await settle(self.cid)
            return got
        (status, headers, body), _ = self.run_async(run())
        self.assertEqual(status, 200)
        self.assertEqual(body, JPG)
        self.assertEqual(headers["Cache-Control"], "public, max-age=31536000, immutable")
        self.assertEqual(server.THUMB_BATCH_N, 4)
        self.assertEqual(self.fetches[0]["items"], [TS, TS - 600, TS - 700, TS - 900])
        self.assertEqual((self.fetches[0]["seg"], self.fetches[0]["stall_s"], self.fetches[0]["hard_limit_s"]),
                         (server.THUMB_SEG_S, 6, 12))
        for s in (TS, TS - 600, TS - 700, TS - 900):
            self.assertTrue((self.cache / f"{self.cid}_{s}.jpg").exists())
        self.assertEqual(list(self.cache.glob("thumb_*")), [])                # clips removed (.ts in fast still mode, .mp4 otherwise)
        self.assertEqual(self.single, [])

    def test_the_requested_thumbnail_returns_first_and_the_rest_keeps_the_lock(self):
        self.write_events([TS - 200, TS - 300])

        async def run():
            gate = asyncio.Event()
            self.batch = fake_batch(self, gate=gate)
            first = await request(self.cid, TS)
            busy_now = slots.busy(self.cid)
            other = await request(self.cid, TS - 1000)     # the lock is held by the background part: 202
            gate.set()
            await settle(self.cid)
            return first, busy_now, other
        (first, busy_now, other), _ = self.run_async(run())
        self.assertEqual(first[0], 200)
        self.assertTrue(busy_now)
        self.assertEqual(other[0], 202)
        for s in (TS, TS - 200):
            self.assertTrue((self.cache / f"{self.cid}_{s}.jpg").exists())
        self.assertFalse(slots.busy(self.cid))

    def test_a_missing_or_unreadable_listing_gives_a_batch_of_one(self):
        async def one():
            resp = await request(self.cid, TS)
            await settle(self.cid)
            return resp
        (status, _, _), _ = self.run_async(one())            # no listing file
        self.assertEqual(status, 200)
        self.assertEqual(self.fetches[-1]["items"], [TS])
        (self.meta / f"{self.cid}_{datetime.datetime.fromtimestamp(TS):%Y%m%d}_ev.json").write_text("not json")
        server._thumb_p2p_end.clear()
        (status, _, _), _ = self.run_async(one())            # unreadable listing
        self.assertEqual(status, 200)
        self.assertEqual(self.fetches[-1]["items"], [TS])

    def test_the_10_second_gap_applies_once_per_batch(self):
        self.write_events([TS - 200])

        async def run():
            first = await request(self.cid, TS)
            await settle(self.cid)
            second = await request(self.cid, TS - 1000)     # right after the batch: the gap answers 202
            return first, second
        (first, second), _ = self.run_async(run())
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 202)
        self.assertEqual(len(self.fetches), 1)

    def test_thumb_batch_false_uses_the_single_path(self):
        self.write_events([TS - 200])
        server.CAMS[self.cid]["thumb_batch"] = False
        (status, _, _), _ = self.run_async(request(self.cid, TS))
        self.assertEqual(status, 200)
        self.assertEqual(self.fetches, [])
        self.assertEqual(self.single, [(TS, TS + server.THUMB_SEG_S)])


class ReuseFallback(BatchBase):
    def test_refused_second_request_switches_to_single_thumbnails_for_the_process(self):
        self.write_events([TS - 200])
        self.batch = fake_batch(self, reuse_fail=True)

        async def run():
            first = await request(self.cid, TS)
            await settle(self.cid)
            server._thumb_p2p_end.clear()
            second = await request(self.cid, TS - 1000)
            return first, second
        ((first, second), err) = self.run_async(run())
        self.assertEqual((first[0], second[0]), (200, 200))
        self.assertIn(self.cid, server._batch_unsupported)
        self.assertEqual(len(self.fetches), 1)                  # the second request never reached the batch
        self.assertEqual(self.single, [(TS - 1000, TS - 1000 + server.THUMB_SEG_S)])
        self.assertEqual(err.count("session reuse unsupported"), 1)
        self.assertNotIn((self.cid, TS - 200, TS - 200 + server.THUMB_SEG_S), server._fail)   # a session fault is not remembered per window

    def test_a_busy_code_on_a_later_item_is_a_stale_stream_and_not_a_refusal(self):
        self.write_events([TS - 200])

        async def stale(call, t, cam, items, *, on_item=None, **kw):
            Path(items[0][1]).write_bytes(b"video")
            await on_item(0, items[0][0], str(items[0][1]))
            return [0], False, False, [BUSY]       # the second request met a busy code: not remembered as unsupported
        self.batch = stale

        async def run():
            resp = await request(self.cid, TS)
            await settle(self.cid)
            return resp
        ((status, _, _), err) = self.run_async(run())
        self.assertEqual(status, 200)
        self.assertNotIn(self.cid, server._batch_unsupported)
        self.assertIn(f"[thumb {self.cid} batch] n=2 ok=1 cause=StaleStream: busy code on a later request stage=video codes=[{BUSY}]", err)
        self.assertNotIn("session reuse unsupported", err)


class Failures(BatchBase):
    def test_item0_plain_failure_is_502_remembered_for_its_window_only(self):
        self.write_events([TS - 200])
        self.batch = fake_batch(self, fail={TS: RuntimeError("no data")})

        async def run():
            first = await request(self.cid, TS)
            await settle(self.cid)
            server._thumb_p2p_end.clear()
            repeat = await request(self.cid, TS)
            return first, repeat
        ((first, repeat), _) = self.run_async(run())
        self.assertEqual(first[0], 502)
        self.assertIn(b"no data", first[2])
        self.assertEqual(repeat[0], 503)                          # the memo answers, no camera request
        self.assertEqual(server._media_fails, {})
        self.assertNotIn(self.cid, server._media_block)

    def test_item0_p2p_busy_is_503_and_not_remembered(self):
        self.batch = fake_batch(self, fail={TS: p2p_media.Busy("camera is busy")})
        (status, _, _), _ = self.run_async(request(self.cid, TS))
        self.assertEqual(status, 503)
        self.assertEqual(server._media_fails, {})
        self.assertNotIn((self.cid, TS, TS + server.THUMB_SEG_S), server._fail)

    def test_extra_failure_is_memo_only_and_the_requested_thumbnail_is_fine(self):
        self.write_events([TS - 200, TS - 300])
        self.batch = fake_batch(self, fail={TS - 200: RuntimeError("boom")})

        async def run():
            first = await request(self.cid, TS)
            await settle(self.cid)
            return first
        (first, _) = self.run_async(run())
        self.assertEqual(first[0], 200)
        self.assertIn((self.cid, TS - 200, TS - 200 + server.THUMB_SEG_S), server._fail)
        self.assertNotIn((self.cid, TS, TS + server.THUMB_SEG_S), server._fail)
        self.assertEqual(server._media_fails, {})
        self.assertNotIn(self.cid, server._media_block)
        self.assertFalse(slots.busy(self.cid))

    def test_a_playback_waiting_gives_202_and_no_batch(self):
        self.write_events([TS - 200])
        slots._wanted[self.cid] += 1
        try:
            (status, _, body), _ = self.run_async(request(self.cid, TS))
        finally:
            slots._wanted[self.cid] -= 1
        self.assertEqual(status, 202)
        self.assertEqual(self.fetches, [])


class Lock(BatchBase):
    def test_the_lock_is_released_when_the_background_part_finishes(self):
        self.write_events([TS - 200, TS - 300])

        async def run():
            await request(self.cid, TS)
            await settle(self.cid)
            return slots.busy(self.cid)
        busy_after, _ = self.run_async(run())
        self.assertFalse(busy_after)

    def test_the_lock_is_released_when_the_background_part_raises(self):
        self.write_events([TS - 200, TS - 300])
        self.batch = fake_batch(self, fail={TS - 200: RuntimeError("boom")})

        async def run():
            first = await request(self.cid, TS)
            await settle(self.cid)
            return first[0], slots.busy(self.cid)
        (status, busy_after), _ = self.run_async(run())
        self.assertEqual(status, 200)
        self.assertFalse(busy_after)


class Channels(BatchBase):
    def test_thumbnails_ask_for_the_main_stream_by_default(self):
        self.write_events([TS - 200])

        async def run():
            resp = await request(self.cid, TS)
            await settle(self.cid)
            return resp
        self.run_async(run())
        self.assertEqual(self.fetches[0]["channels"], [0])
        self.assertEqual(self.fetches[0]["items"], [TS, TS - 200])

    def test_opt_in_channel_1_asks_channel_1_with_no_fallback(self):
        self.write_events([TS - 200])
        server.CAMS[self.cid]["thumb_channel"] = 1

        async def run():
            resp = await request(self.cid, TS)
            await settle(self.cid)
            return resp
        self.run_async(run())
        self.assertEqual(self.fetches[0]["channels"], [1])

    def test_thumb_channel_0_asks_the_main_stream(self):
        self.write_events([TS - 200])
        server.CAMS[self.cid]["thumb_channel"] = 0

        async def run():
            resp = await request(self.cid, TS)
            await settle(self.cid)
            return resp
        self.run_async(run())
        self.assertEqual(self.fetches[0]["channels"], [0])

    def test_item0_budget_is_larger_only_when_channel_1_is_opted_in(self):
        self.assertEqual(server._item0_budget(self.cam), server.THUMB_TOTAL_S)
        server.CAMS[self.cid]["thumb_channel"] = 1
        self.assertEqual(server._item0_budget(self.cam), server.THUMB_TOTAL_S + server.THUMB_STALL_S + 6)
        with mock.patch.object(server, "THUMB_TOTAL_S", 22):
            self.assertEqual(server._item0_budget(self.cam), 34)


class FailureLog(BatchBase):
    """Every batch says why: a failed batch logs one `failed:` line with its stage, and every batch ends with a summary line with
    the cause, the stage and the camera codes."""

    def test_a_cancelled_first_item_logs_the_cause_at_stage_request(self):
        self.write_events([TS - 200])
        self.batch = fake_batch(self, fail={TS: asyncio.CancelledError()})
        (status, _, _), err = self.run_async(request(self.cid, TS))
        self.assertEqual(status, 502)
        self.assertIn(f"[thumb {self.cid} batch {TS}] failed: CancelledError: cancelled (stage: request)", err)
        self.assertLess(err.index("failed: CancelledError"), err.index(f"[thumb {self.cid} batch] n=2 ok=0 cause=CancelledError"))
        self.assertIn(f"[thumb {self.cid} batch] n=2 ok=0 cause=CancelledError: cancelled stage=request codes=[]", err)

    def test_a_cancel_after_item0_is_logged_at_stage_video_and_the_request_still_answers(self):
        self.write_events([TS - 200])
        self.batch = fake_batch(self, fail={TS - 200: asyncio.CancelledError()})

        async def run():
            resp = await request(self.cid, TS)
            await settle(self.cid)
            return resp
        ((status, _, _), err) = self.run_async(run())
        self.assertEqual(status, 200)
        self.assertIn(f"[thumb {self.cid} batch {TS}] failed: CancelledError: cancelled (stage: video)", err)
        self.assertIn(f"[thumb {self.cid} batch] n=2 ok=1 cause=CancelledError: cancelled stage=video codes=[]", err)
        self.assertFalse(slots.busy(self.cid))

    def test_a_timeout_of_the_requested_thumbnail_logs_the_limit_and_stage_and_frees_the_lock(self):
        self.write_events([TS - 200])

        async def hang(*a, **kw):
            await asyncio.Event().wait()          # never saves anything: the requested thumbnail times out

        self.batch = hang
        with mock.patch.object(server, "THUMB_TOTAL_S", 0.2), mock.patch.object(server, "THUMB_BATCH_BG_S", 0.2):
            async def run():
                resp = await request(self.cid, TS)
                await settle(self.cid)
                return resp
            (status, _, _), err = self.run_async(run())
        self.assertEqual(status, 502)
        self.assertIn(f"[thumb {self.cid} batch {TS}] requested thumbnail timed out after 0.2s at stage request", err)
        self.assertIn(f"[thumb {self.cid} batch {TS}] failed: CancelledError: cancelled (stage: request)", err)
        self.assertFalse(slots.busy(self.cid))
        self.assertNotIn(self.cid, server._batch_tasks)

    def test_a_busy_first_item_logs_the_busy_cause(self):
        self.write_events([TS - 200])
        self.batch = fake_batch(self, fail={TS: p2p_media.Busy("camera is busy (stream slots in use)")})
        (status, _, _), err = self.run_async(request(self.cid, TS))
        self.assertEqual(status, 503)
        self.assertIn(f"[thumb {self.cid} batch {TS}] failed: Busy: camera is busy (stream slots in use) (stage: request)", err)
        self.assertIn("cause=Busy: camera is busy (stream slots in use) stage=request", err)

    def test_a_batch_that_saved_nothing_without_an_error_logs_it(self):
        self.write_events([TS - 200])

        async def nothing(*a, **kw):
            return [], False, False, []
        self.batch = nothing
        (status, _, _), err = self.run_async(request(self.cid, TS))
        self.assertEqual(status, 502)
        self.assertIn(f"[thumb {self.cid} batch {TS}] failed: NothingSaved: no item was saved (stage: request)", err)
        self.assertIn(f"[thumb {self.cid} batch] n=2 ok=0 cause=NothingSaved: no item was saved stage=request codes=[]", err)

    def test_a_refused_gate_is_logged_at_stage_setup_and_leaves_the_p2p_gap_alone(self):
        self.write_events([TS - 200])

        def refuse(cam, start, end):
            raise web.HTTPConflict(text="still recording")
        with mock.patch.object(server, "media_gate", refuse):
            (status, _, _), err = self.run_async(request(self.cid, TS))
        self.assertEqual(status, 409)
        self.assertIn("(stage: setup)", err)
        self.assertIn("stage=setup", err)
        self.assertNotIn(self.cid, server._thumb_p2p_end)
        self.assertEqual(self.fetches, [])

    def test_a_good_batch_logs_its_summary_with_cause_none_and_no_failure(self):
        self.write_events([TS - 200])
        (status, _, _), err = self.run_async(request(self.cid, TS))
        self.assertEqual(status, 200)
        self.assertIn(f"[thumb {self.cid} batch] n=2 ok=2 cause=none stage=video codes=[]", err)
        self.assertNotIn("] failed:", err)


class FastStill(BatchBase):
    """Fast still mode (cameras.json thumb_fast, default on): the batch asks for raw streams named .ts; an item whose stream held no
    decodable frame is a NoFrame window (logged, memoised, never counted); thumb_fast false keeps the Convert clip (.mp4)."""
    def capture_names(self):
        names, real = [], self.batch

        async def run(call, t, cam, items, **kw):
            names.append([Path(out).name for _, out in items])
            return await real(call, t, cam, items, **kw)
        self.batch = run
        return names

    def test_fast_batch_asks_for_still_streams_named_ts_and_removes_them(self):
        self.write_events([TS - 200])
        names = self.capture_names()
        (status, _, _), _ = self.run_async(request(self.cid, TS))
        self.assertEqual(status, 200)
        self.assertTrue(self.fetches[0]["still"])
        self.assertEqual(names, [[f"thumb_{self.cid}_{TS}.ts", f"thumb_{self.cid}_{TS - 200}.ts"]])
        self.assertEqual(list(self.cache.glob("thumb_*")), [])

    def test_thumb_fast_false_keeps_the_convert_clip_names_and_flag(self):
        self.write_events([TS - 200])
        server.CAMS[self.cid]["thumb_fast"] = False
        names = self.capture_names()
        (status, _, _), _ = self.run_async(request(self.cid, TS))
        self.assertEqual(status, 200)
        self.assertFalse(self.fetches[0]["still"])
        self.assertEqual(names, [[f"thumb_{self.cid}_{TS}.mp4", f"thumb_{self.cid}_{TS - 200}.mp4"]])

    def test_a_requested_stream_with_no_frame_is_502_and_a_memo_only(self):
        async def run():
            got = await request(self.cid, TS)
            await settle(self.cid)
            return got

        async def no_frame(*args, **kw):
            proc = mock.Mock()
            proc.wait = mock.AsyncMock(return_value=0)
            return proc
        with mock.patch.object(server.asyncio, "create_subprocess_exec", no_frame):
            ((status, _, body), err) = self.run_async(run())
        self.assertEqual(status, 502)
        self.assertIn(b"no frame", body)
        self.assertIn(f"[thumb {self.cid} batch {TS}] failed: NoFrame (stage: still)", err)
        self.assertIn("cause=NoFrame: 1 item(s) without a decodable frame", err)
        self.assertNotIn("NothingSaved", err)
        self.assertIn((self.cid, TS, TS + server.THUMB_SEG_S), server._fail)
        self.assertEqual(server._media_fails, {})
        self.assertNotIn(self.cid, server._media_block)
        self.assertEqual(list(self.cache.glob("thumb_*")), [])

    def test_a_later_stream_with_no_frame_is_memo_only_and_the_requested_thumbnail_is_fine(self):
        self.write_events([TS - 200])

        async def ffmpeg(*args, **kw):
            if f"_{TS - 200}.jpg" not in args[-1]:
                Path(args[-1]).write_bytes(JPG)
            proc = mock.Mock()
            proc.wait = mock.AsyncMock(return_value=0)
            return proc

        async def run():
            got = await request(self.cid, TS)
            await settle(self.cid)
            return got
        with mock.patch.object(server.asyncio, "create_subprocess_exec", ffmpeg):
            ((status, _, _), err) = self.run_async(run())
        self.assertEqual(status, 200)
        self.assertIn(f"[thumb {self.cid} batch {TS}] failed: NoFrame (stage: still)", err)
        self.assertIn(f"[thumb {self.cid} batch] n=2 ok=1 cause=none", err)
        self.assertIn((self.cid, TS - 200, TS - 200 + server.THUMB_SEG_S), server._fail)
        self.assertEqual(server._media_fails, {})
        self.assertNotIn(self.cid, server._media_block)


if __name__ == "__main__":
    unittest.main()
