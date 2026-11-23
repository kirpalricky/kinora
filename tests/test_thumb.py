"""/api/thumb: 202 'try later' while the camera's video slot is busy, real failures stay 4xx/5xx, cache header.
No camera, no ffmpeg: the download is replaced by a fake and CACHE is a temp dir. Run: python -m unittest tests.test_thumb
"""
import asyncio
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
                  mock.patch.object(slots, "_play_active", lambda cid: False)):
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

        async def fake_download(cam, start, end, name):
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

        async def fake_download(cam, start, end, name):
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
        self.assertEqual(calls, [(TS, TS + 3)])


class RealFailuresStay(ThumbBase):
    def test_gate_refusal_stays_503_not_202(self):
        async def refuse(cam, start, end, name):
            raise web.HTTPServiceUnavailable(text="this clip failed recently")
        with mock.patch.object(server, "_download", refuse):
            status, headers, _ = asyncio.run(get_thumb(self.cid))
        self.assertEqual(status, 503)
        self.assertNotIn("Retry-After", headers)

    def test_no_video_stays_502(self):
        async def no_video(cam, start, end, name):
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
        async def fake_download(cam, start, end, name):
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


if __name__ == "__main__":
    unittest.main()
