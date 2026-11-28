"""/api/thumb: 202 'try later' while the camera's video slot is busy, real failures stay 4xx/5xx, cache header.
No camera, no ffmpeg: the download is replaced by a fake and CACHE is a temp dir. Run: python -m unittest tests.test_thumb
"""
import asyncio
import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import server  # noqa: E402
from kinora.camera import slots as slots_mod  # noqa: E402
from kinora.camera.slots import slots  # noqa: E402

HOST = "127.0.0.1:8080"
TS = 1_000_000_000   # far in the past: never 'still recording'


def cam_entry():
    cid = next(c for c in server.CAMS if server.CAMS[c].get("thumbs") is not False)
    return cid, dict(server.CAMS[cid], thumbs=True, enabled=True, verified=True)


async def get_thumb(cid, ts=TS):
    client = TestClient(TestServer(server.make_app(background=False)))
    await client.start_server()
    try:
        resp = await client.get(f"/api/thumb?cam={cid}&ts={ts}", headers={"Host": HOST})
        body = await resp.read()
        return resp.status, dict(resp.headers), body
    finally:
        await client.close()


class ThumbBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        self.cid, cam = cam_entry()
        for p in (mock.patch.object(server, "CACHE", self.cache),
                  mock.patch.dict(server.CAMS, {self.cid: cam}),
                  mock.patch.object(server.session, "check_usable", lambda cam: None),
                  mock.patch.object(slots, "_live_running", lambda cid: False),
                  mock.patch.object(slots, "_play_active", lambda cid: False),
                  mock.patch.dict(server._fail, clear=True),           # per-window memo
                  mock.patch.dict(server._media_fails, clear=True),    # pause counter
                  mock.patch.dict(server._media_block, clear=True),    # the 30 min pause
                  mock.patch.dict(server._thumb_p2p_end, clear=True)): # P2P thumbnail gap
            p.start()
            self.addCleanup(p.stop)
        self.cam_name = server.CAMS[self.cid]["name"]


class BusyIs202(ThumbBase):
    def test_live_open_and_not_idle_is_202_without_download_when_beside_live_is_off(self):
        downloads = []
        server.CAMS[self.cid]["thumbs_beside_live"] = False

        async def no_download(*a, **kw):
            downloads.append(a)
            raise AssertionError("must not download while live view is open")

        with mock.patch.object(slots, "_live_running", lambda cid: True), \
             mock.patch.object(slots, "_live_idle_s", lambda cid: 0), \
             mock.patch.object(server, "_download", no_download):
            status, headers, body = asyncio.run(get_thumb(self.cid))
        self.assertEqual(status, 202)
        self.assertEqual(headers.get("Retry-After"), "5")
        data = json.loads(body)
        self.assertEqual(data["state"], "busy")
        self.assertIn("live view is open", data["reason"])
        self.assertEqual(downloads, [])

    def test_live_open_still_downloads_by_default(self):
        downloads = []

        async def fake_download(cam, start, end, name, **kw):
            downloads.append(name)
            (self.cache / name).write_bytes(b"x")

        async def fake_exec(*a, **kw):
            Path(a[-1]).write_bytes(b"jpg")
            return mock.Mock(wait=mock.AsyncMock(return_value=0))

        with mock.patch.object(slots, "_live_running", lambda cid: True), \
             mock.patch.object(slots, "_live_idle_s", lambda cid: 0), \
             mock.patch.object(server, "_download", fake_download), \
             mock.patch.object(server.asyncio, "create_subprocess_exec", fake_exec):
            status, headers, body = asyncio.run(get_thumb(self.cid))
        self.assertEqual(status, 200)
        self.assertEqual(len(downloads), 1)

    def test_playback_open_is_202(self):
        with mock.patch.object(slots, "_play_active", lambda cid: True):
            status, headers, body = asyncio.run(get_thumb(self.cid))
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body)["state"], "busy")
        self.assertIn("playback is open", json.loads(body)["reason"])

    def test_lock_held_by_a_clip_is_202_within_one_second(self):
        async def run():
            lock = slots.lock(self.cid)
            await lock.acquire()
            try:
                t0 = time.monotonic()
                got = await get_thumb(self.cid)
                return got, time.monotonic() - t0
            finally:
                lock.release()
        (status, headers, body), took = asyncio.run(run())
        self.assertEqual(status, 202)
        self.assertEqual(headers.get("Retry-After"), "5")
        self.assertLess(took, 2.5)   # the wait is capped at 1 s (plus client overhead)

    def test_lock_freed_during_the_wait_goes_on_to_download(self):
        calls = []

        async def fake_download(cam, start, end, name, **kw):
            calls.append((start, end))
            raise web.HTTPBadGateway(text="stop here: the download was reached")

        async def run():
            lock = slots.lock(self.cid)
            await lock.acquire()
            asyncio.get_running_loop().call_later(0.2, lock.release)
            return await get_thumb(self.cid)
        with mock.patch.object(server, "_download", fake_download):
            status, _, _ = asyncio.run(run())
        self.assertEqual(status, 502)           # a real failure, reached after the lock was freed
        self.assertEqual(calls, [(TS, TS + server.THUMB_SEG_S)])


class RealFailuresStay(ThumbBase):
    def test_gate_refusal_stays_503_not_202(self):
        async def refuse(cam, start, end, name, **kw):
            raise web.HTTPServiceUnavailable(text="this clip failed recently")
        with mock.patch.object(server, "_download", refuse):
            status, headers, _ = asyncio.run(get_thumb(self.cid))
        self.assertEqual(status, 503)
        self.assertNotIn("Retry-After", headers)

    def test_no_video_stays_502(self):
        async def no_video(cam, start, end, name, **kw):
            raise web.HTTPBadGateway(text=f"{self.cam_name}: camera returned no video")
        with mock.patch.object(server, "_download", no_video):
            status, _, body = asyncio.run(get_thumb(self.cid))
        self.assertEqual(status, 502)
        self.assertIn(b"no video", body)

    def test_thumbnails_off_is_404(self):
        with mock.patch.dict(server.CAMS, {self.cid: dict(server.CAMS[self.cid], thumbs=False)}):
            status, _, _ = asyncio.run(get_thumb(self.cid))
        self.assertEqual(status, 404)


class CachedJpg(ThumbBase):
    def test_cached_jpg_is_200_with_immutable_cache_headers_and_no_lock(self):
        (self.cache / f"{self.cid}_{TS}.jpg").write_bytes(b"\xff\xd8not-a-real-image-for-tests")
        # a busy camera must not matter for a cached thumbnail
        with mock.patch.object(slots, "_live_running", lambda cid: True), \
             mock.patch.object(slots, "_live_idle_s", lambda cid: 0):
            status, headers, _ = asyncio.run(get_thumb(self.cid))
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "public, max-age=31536000, immutable")

    def test_fresh_thumbnail_also_carries_the_cache_header(self):
        async def fake_download(cam, start, end, name, **kw):
            (self.cache / name).write_bytes(b"video")

        async def fake_exec(*args, **kw):
            (self.cache / f"{self.cid}_{TS}.jpg").write_bytes(b"\xff\xd8x")
            proc = mock.Mock()
            proc.wait = mock.AsyncMock(return_value=0)
            return proc

        with mock.patch.object(server, "_download", fake_download), \
             mock.patch.object(server.asyncio, "create_subprocess_exec", fake_exec), \
             mock.patch.object(server, "ffmpeg_path", lambda: "ffmpeg"):
            status, headers, _ = asyncio.run(get_thumb(self.cid))
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "public, max-age=31536000, immutable")
        self.assertFalse((self.cache / f"thumb_{self.cid}_{TS}.mp4").exists())   # the clip is removed


class P2PGenericThumb(ThumbBase):
    """A P2P camera with thumbs on and no native_thumbs (the C100 preset) takes the generic clip + ffmpeg path."""
    def test_p2p_camera_without_native_thumbs_uses_the_clip_and_ffmpeg_not_the_native_request(self):
        # thumb_batch off: this test pins the single-thumbnail path (the batch path is tested in test_thumb_batch)
        server.CAMS[self.cid].update(p2p=True, thumbs=True, thumb_batch=False)
        server.CAMS[self.cid].pop("native_thumbs", None)
        windows, native_calls = [], []

        async def fake_fetch_media(cam, start, end, name, **kw):
            windows.append(end - start)
            (self.cache / name).write_bytes(b"video")

        async def fake_exec(*args, **kw):
            (self.cache / f"{self.cid}_{TS}.jpg").write_bytes(b"\xff\xd8x")
            proc = mock.Mock()
            proc.wait = mock.AsyncMock(return_value=0)
            return proc

        async def native(*a, **kw):
            native_calls.append(a)
            raise AssertionError("native thumbnail request must not be sent")

        with mock.patch.object(server, "fetch_media", fake_fetch_media), \
             mock.patch.object(server.p2p_media, "fetch_thumbnail", native), \
             mock.patch.object(server.p2p_media, "fetch_snapshot", native), \
             mock.patch.object(server.asyncio, "create_subprocess_exec", fake_exec), \
             mock.patch.object(server, "ffmpeg_path", lambda: "ffmpeg"):
            status, headers, _ = asyncio.run(get_thumb(self.cid))
        self.assertEqual(status, 200)
        self.assertEqual(windows, [server.THUMB_SEG_S])
        self.assertEqual(native_calls, [])
        self.assertEqual(headers.get("Content-Type"), "image/jpeg")


async def get_clip(cid, start, end):
    client = TestClient(TestServer(server.make_app(background=False)))
    await client.start_server()
    try:
        resp = await client.get(f"/api/clip?cam={cid}&start={start}&end={end}", headers={"Host": HOST})
        return resp.status, await resp.read()
    finally:
        await client.close()


class P2PFetchBase(ThumbBase):
    """Runs the real _download p2p branch with the camera-side pieces faked: no login, no P2P, no ffmpeg."""
    def setUp(self):
        super().setUp()
        # thumb_batch off: these tests pin the single-thumbnail path (the batch path is tested in test_thumb_batch)
        server.CAMS[self.cid].update(p2p=True, thumbs=True, thumb_batch=False)
        server.CAMS[self.cid].pop("native_thumbs", None)
        for p in (mock.patch.object(server, "require_pytapo_tools", lambda: None),
                  mock.patch.object(server, "client", lambda cam: object())):
            p.start()
            self.addCleanup(p.stop)

    def fake_exec(self):
        async def run(*args, **kw):
            Path(args[-1]).write_bytes(b"\xff\xd8x")
            proc = mock.Mock()
            proc.wait = mock.AsyncMock(return_value=0)
            return proc
        return mock.patch.object(server.asyncio, "create_subprocess_exec", run)


class CamerasTbl(unittest.TestCase):
    """GET /api/cameras carries tbl: thumbnails may run beside Live (true unless thumbs_beside_live is false)."""
    def tbl_of(self, **change):
        cid = next(iter(server.CAMS))
        cam = dict(server.CAMS[cid])
        cam.pop("thumbs_beside_live", None)
        cam.update(change)
        with mock.patch.dict(server.CAMS, {cid: cam}, clear=True):
            resp = asyncio.run(server.cameras(None))
        return {c["id"]: c for c in json.loads(resp.text)}[cid]["tbl"]

    def test_tbl_is_true_by_default(self):
        self.assertIs(self.tbl_of(), True)

    def test_tbl_is_false_when_thumbs_beside_live_is_false(self):
        self.assertIs(self.tbl_of(thumbs_beside_live=False), False)

    def test_tbl_is_true_when_thumbs_beside_live_is_true(self):
        self.assertIs(self.tbl_of(thumbs_beside_live=True), True)


class ThumbFailuresNeverPause(P2PFetchBase):
    """A failed thumbnail window is memoised, but it never counts towards the camera's 30 min download pause."""
    def test_failed_thumbs_leave_the_pause_counter_and_block_alone_and_the_window_is_memoised(self):
        calls = []

        async def fail(call, t, cam, start, end, out_path, **kw):
            calls.append((start, end))
            raise RuntimeError("no data")

        with mock.patch.object(server.p2p_media, "fetch_window", fail):
            first, _, _ = asyncio.run(get_thumb(self.cid, TS))
            server._thumb_p2p_end.clear()     # the P2P gap is tested elsewhere
            second, _, _ = asyncio.run(get_thumb(self.cid, TS + 30))
            server._thumb_p2p_end.clear()
            repeat, _, _ = asyncio.run(get_thumb(self.cid, TS))   # same window: the memo answers, no camera request
        self.assertEqual((first, second), (502, 502))
        self.assertEqual(repeat, 503)
        self.assertEqual(calls, [(TS, TS + server.THUMB_SEG_S), (TS + 30, TS + 30 + server.THUMB_SEG_S)])
        self.assertEqual(server._media_fails.get(self.cid, 0), 0)
        self.assertNotIn(self.cid, server._media_block)

    def test_a_clip_is_not_refused_after_failed_thumbs(self):
        async def fail(call, t, cam, start, end, out_path, **kw):
            raise RuntimeError("no data")

        async def ok(call, t, cam, start, end, out_path, **kw):
            Path(out_path).write_bytes(b"video")

        with mock.patch.object(server.p2p_media, "fetch_window", fail):
            asyncio.run(get_thumb(self.cid, TS))
            server._thumb_p2p_end.clear()
            asyncio.run(get_thumb(self.cid, TS + 30))
        with mock.patch.object(server.p2p_media, "fetch_window", ok):
            status, _ = asyncio.run(get_clip(self.cid, TS + 60, TS + 65))
        self.assertEqual(status, 200)

    def test_a_timed_out_thumb_is_a_failure_that_is_not_counted(self):
        async def stall(call, t, cam, start, end, out_path, **kw):
            await asyncio.sleep(1)

        with mock.patch.object(server.p2p_media, "fetch_window", stall), \
             mock.patch.object(server, "THUMB_TOTAL_S", 0.05):
            status, _, body = asyncio.run(get_thumb(self.cid, TS))
        self.assertEqual(status, 502)
        self.assertIn(b"no video", body)
        self.assertEqual(server._media_fails.get(self.cid, 0), 0)
        self.assertNotIn(self.cid, server._media_block)


class P2PThumbLimits(P2PFetchBase):
    """A thumbnail's P2P download has short limits; a clip keeps the defaults."""
    def test_thumb_gets_short_limits_and_a_clip_keeps_the_defaults(self):
        seen = []

        async def record(call, t, cam, start, end, out_path, **kw):
            seen.append(kw)
            Path(out_path).write_bytes(b"video")

        with mock.patch.object(server.p2p_media, "fetch_window", record), self.fake_exec():
            status, _, _ = asyncio.run(get_thumb(self.cid, TS))
            server._thumb_p2p_end.clear()
            clip_status, _ = asyncio.run(get_clip(self.cid, TS + 60, TS + 65))
        self.assertEqual(status, 200)
        self.assertEqual(clip_status, 200)
        # TESTS-CHANGED: the thumbnail also carries the fast still mode (thumb_fast default on), the clip keeps its defaults
        self.assertEqual(seen, [{"stall_s": 6, "hard_limit_s": 12, "channels": [0], "still": True,
                                 "still_min_bytes": server.THUMB_STILL_MIN_BYTES, "still_max_s": server.THUMB_STILL_MAX_S}, {}])

    def test_thumb_channel_1_is_opt_in_for_the_single_path(self):
        seen = []

        async def record(call, t, cam, start, end, out_path, **kw):
            seen.append(kw["channels"])
            Path(out_path).write_bytes(b"video")

        server.CAMS[self.cid]["thumb_channel"] = 1
        with mock.patch.object(server.p2p_media, "fetch_window", record), self.fake_exec():
            status, _, _ = asyncio.run(get_thumb(self.cid, TS))
        self.assertEqual(status, 200)
        self.assertEqual(seen, [[1]])


class P2PThumbGap(P2PFetchBase):
    """One P2P thumbnail session per camera, THUMB_P2P_GAP_S apart; a busy answer says when to ask again."""
    def test_second_p2p_thumb_within_the_gap_is_202_and_fetches_nothing(self):
        fetches = []

        async def fake_fetch_media(cam, start, end, name, **kw):
            fetches.append(start)
            (self.cache / name).write_bytes(b"video")

        with mock.patch.object(server, "fetch_media", fake_fetch_media), self.fake_exec():
            first, _, _ = asyncio.run(get_thumb(self.cid, TS))
            second, headers, body = asyncio.run(get_thumb(self.cid, TS + 60))
        self.assertEqual(first, 200)
        self.assertEqual(second, 202)
        self.assertEqual(json.loads(body)["state"], "busy")
        self.assertTrue(1 <= int(headers["Retry-After"]) <= server.THUMB_P2P_GAP_S)
        self.assertEqual(fetches, [TS])

    def test_after_the_gap_the_next_thumb_proceeds(self):
        fetches = []

        async def fake_fetch_media(cam, start, end, name, **kw):
            fetches.append(start)
            (self.cache / name).write_bytes(b"video")

        with mock.patch.object(server, "fetch_media", fake_fetch_media), self.fake_exec():
            asyncio.run(get_thumb(self.cid, TS))
            server._thumb_p2p_end[self.cid] = time.monotonic() - server.THUMB_P2P_GAP_S - 1   # the gap has passed
            status, _, _ = asyncio.run(get_thumb(self.cid, TS + 60))
        self.assertEqual(status, 200)
        self.assertEqual(fetches, [TS, TS + 60])

    def test_a_non_p2p_camera_is_not_gapped(self):
        server.CAMS[self.cid].update(p2p=False)
        fetches = []

        async def fake_fetch_media(cam, start, end, name, **kw):
            fetches.append(start)
            (self.cache / name).write_bytes(b"video")

        with mock.patch.object(server, "fetch_media", fake_fetch_media), self.fake_exec():
            first, _, _ = asyncio.run(get_thumb(self.cid, TS))
            second, _, _ = asyncio.run(get_thumb(self.cid, TS + 60))
        self.assertEqual((first, second), (200, 200))
        self.assertEqual(fetches, [TS, TS + 60])


class SingleStill(P2PFetchBase):
    """The single-thumbnail path in fast still mode: a .ts download with still=True, a NoFrame memo that never counts, no part file
    left behind; thumb_fast false is the .mp4 clip with Convert (still=False)."""
    def test_fast_single_path_downloads_a_ts_part_with_still_on(self):
        seen = []

        async def record(call, t, cam, start, end, out_path, **kw):
            seen.append((Path(out_path).name, kw["still"]))
            Path(out_path).write_bytes(b"video")
        with mock.patch.object(server.p2p_media, "fetch_window", record), self.fake_exec():
            status, _, _ = asyncio.run(get_thumb(self.cid, TS))
        self.assertEqual(status, 200)
        self.assertEqual(seen, [(f"thumb_{self.cid}_{TS}.part.ts", True)])
        self.assertEqual(list(self.cache.glob("thumb_*")), [])

    def test_fast_single_path_ffmpeg_command_has_an_before_the_output_file(self):
        async def record(call, t, cam, start, end, out_path, **kw):
            Path(out_path).write_bytes(b"video")
        calls = []

        async def ffmpeg(*args, **kw):
            calls.append(args)
            Path(args[-1]).write_bytes(b"\xff\xd8x")
            proc = mock.Mock()
            proc.wait = mock.AsyncMock(return_value=0)
            return proc
        with mock.patch.object(server.p2p_media, "fetch_window", record), \
             mock.patch.object(server.asyncio, "create_subprocess_exec", ffmpeg):
            status, _, _ = asyncio.run(get_thumb(self.cid, TS))
        self.assertEqual(status, 200)
        self.assertEqual(len(calls), 1)
        args = calls[0]
        self.assertIn("-an", args)
        self.assertLess(args.index("-an"), len(args) - 1)
        self.assertTrue(args[-1].endswith(f"{self.cid}_{TS}.jpg"))
        self.assertEqual(args[args.index("-frames:v") + 1], "1")
        self.assertEqual(args[args.index("-v") + 1], "fatal")

    def test_thumb_fast_false_downloads_the_mp4_clip_with_still_off(self):
        seen = []

        async def record(call, t, cam, start, end, out_path, **kw):
            seen.append((Path(out_path).name, kw["still"]))
            Path(out_path).write_bytes(b"video")
        server.CAMS[self.cid]["thumb_fast"] = False
        with mock.patch.object(server.p2p_media, "fetch_window", record), self.fake_exec():
            status, _, _ = asyncio.run(get_thumb(self.cid, TS))
        self.assertEqual(status, 200)
        self.assertEqual(seen, [(f"thumb_{self.cid}_{TS}.part.mp4", False)])

    def test_a_failed_still_download_leaves_no_part_file(self):
        async def partial_then_fail(call, t, cam, start, end, out_path, **kw):
            Path(out_path).write_bytes(b"half")
            raise RuntimeError("no data")
        with mock.patch.object(server.p2p_media, "fetch_window", partial_then_fail):
            status, _, _ = asyncio.run(get_thumb(self.cid, TS))
        self.assertEqual(status, 502)
        self.assertEqual(list(self.cache.glob("thumb_*")), [])
        self.assertEqual(server._media_fails.get(self.cid, 0), 0)

    def test_no_frame_is_logged_memo_only_and_the_repeat_is_answered_from_the_memo(self):
        async def ok_stream(call, t, cam, start, end, out_path, **kw):
            Path(out_path).write_bytes(b"video")

        async def no_frame(*args, **kw):
            proc = mock.Mock()
            proc.wait = mock.AsyncMock(return_value=0)
            return proc
        err = io.StringIO()
        with mock.patch.object(server.p2p_media, "fetch_window", ok_stream), \
             mock.patch.object(server.asyncio, "create_subprocess_exec", no_frame), contextlib.redirect_stderr(err):
            first, _, _ = asyncio.run(get_thumb(self.cid, TS))
            server._thumb_p2p_end.clear()
            repeat, _, _ = asyncio.run(get_thumb(self.cid, TS))
        self.assertEqual(first, 404)
        self.assertIn(f"[thumb {self.cid} {TS}] failed: NoFrame (stage: still)", err.getvalue())
        self.assertEqual(repeat, 503)
        self.assertEqual(server._media_fails, {})
        self.assertNotIn(self.cid, server._media_block)


if __name__ == "__main__":
    unittest.main()
