"""Native C100 thumbnails (experiment): route outcomes and fetch_thumbnail parsing, all with fakes (no camera, no network).
Run: python -m unittest tests.test_native_thumb"""
import asyncio
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import server  # noqa: E402
from kinora import p2p_media  # noqa: E402
from kinora.camera.slots import slots  # noqa: E402

HOST = "127.0.0.1:8080"
TS = 1_000_000_000
JPG = b"\xff\xd8" + b"x" * 100 + b"\xff\xd9"


async def get_thumb(cid, ts=TS):
    client = TestClient(TestServer(server.make_app(background=False)))
    await client.start_server()
    try:
        r = await client.get(f"/api/thumb?cam={cid}&ts={ts}", headers={"Host": HOST})
        return r.status, dict(r.headers), await r.read()
    finally:
        await client.close()


class Route(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        self.cid = next(iter(server.CAMS))
        cam = dict(server.CAMS[self.cid], p2p=True, thumbs=False, native_thumbs=True, enabled=True, verified=True)
        self.calls = []
        for p in (mock.patch.object(server, "CACHE", self.cache),
                  mock.patch.dict(server.CAMS, {self.cid: cam}),
                  mock.patch.object(server.session, "check_usable", lambda cam: None),
                  mock.patch.object(server, "client", lambda cam: object()),
                  mock.patch.object(server, "_native_unsupported", set()),
                  mock.patch.object(server, "THUMB_P2P_GAP_S", 0),   # the P2P gap has its own tests (test_thumb)
                  mock.patch.object(slots, "_live_running", lambda cid: False),
                  mock.patch.object(slots, "_play_active", lambda cid: False)):
            p.start()
            self.addCleanup(p.stop)

    def fake(self, behaviour, name="fetch_thumbnail"):
        async def f(call, t, cam, start, out, log=None, **kw):
            self.calls.append((name, start))
            return behaviour(out, log)
        return mock.patch.object(p2p_media, name, f)

    def test_success_is_200_cached_with_header(self):
        def ok(out, log):
            Path(out).write_bytes(JPG)
        with self.fake(ok):
            status, headers, body = asyncio.run(get_thumb(self.cid))
        self.assertEqual((status, body), (200, JPG))
        self.assertEqual(headers["Cache-Control"], "public, max-age=31536000, immutable")
        self.assertTrue((self.cache / f"{self.cid}_{TS}.jpg").exists())

    def test_unsupported_is_404_logged_once_then_no_more_fetches(self):
        def no(out, log):
            raise p2p_media.Unsupported("error_code -40106")
        err = io.StringIO()
        with self.fake(no), self.fake(no, "fetch_snapshot"), contextlib.redirect_stderr(err):
            s1 = asyncio.run(get_thumb(self.cid))[0]
            s2 = asyncio.run(get_thumb(self.cid, TS + 1))[0]   # snapshot fallback, refused too
            s3 = asyncio.run(get_thumb(self.cid, TS + 2))[0]
            s4 = asyncio.run(get_thumb(self.cid, TS + 3))[0]
        self.assertEqual((s1, s2, s3, s4), (404, 404, 404, 404))
        self.assertEqual([c[0] for c in self.calls], ["fetch_thumbnail", "fetch_snapshot"])
        self.assertEqual(server._native_unsupported, {(self.cid, "image"), (self.cid, "snapshot")})
        self.assertEqual(err.getvalue().count("unsupported: error_code -40106"), 2)   # once per mode
        self.assertIn(f"[thumb {self.cid} native] unsupported", err.getvalue())

    def test_camera_busy_is_202(self):
        def busy(out, log):
            raise p2p_media.Busy("camera is busy")
        with self.fake(busy):
            status, headers, _ = asyncio.run(get_thumb(self.cid))
        self.assertEqual((status, headers.get("Retry-After")), (202, "5"))
        self.assertEqual(server._native_unsupported, set())

    def test_playback_open_is_202_without_fetch(self):
        with self.fake(lambda o, l: None), mock.patch.object(slots, "_play_active", lambda cid: True):
            status, _, _ = asyncio.run(get_thumb(self.cid))
        self.assertEqual(status, 202)
        self.assertEqual(self.calls, [])

    def test_other_failure_is_502_and_not_remembered(self):
        def bad(out, log):
            raise p2p_media.NoVideo("truncated")
        with self.fake(bad), contextlib.redirect_stderr(io.StringIO()):
            status, _, _ = asyncio.run(get_thumb(self.cid))
        self.assertEqual(status, 502)
        self.assertEqual(server._native_unsupported, set())

    def test_flag_off_is_unchanged_404(self):
        server.CAMS[self.cid]["native_thumbs"] = False
        with self.fake(lambda o, l: None):
            status, _, _ = asyncio.run(get_thumb(self.cid))
        self.assertEqual(status, 404)
        self.assertEqual(self.calls, [])

    def test_snapshot_mode_never_calls_image_strategy(self):
        server.CAMS[self.cid]["native_thumbs_mode"] = "snapshot"
        def ok(out, log):
            Path(out).write_bytes(JPG)
        with self.fake(ok, "fetch_snapshot"), self.fake(lambda o, l: 1 / 0, "fetch_thumbnail"):
            status, headers, body = asyncio.run(get_thumb(self.cid))
        self.assertEqual((status, body), (200, JPG))
        self.assertEqual([c[0] for c in self.calls], ["fetch_snapshot"])

    def test_snapshot_unsupported_is_404_remembered_per_mode(self):
        server.CAMS[self.cid]["native_thumbs_mode"] = "snapshot"
        def no(out, log):
            raise p2p_media.Unsupported("no reply (silence)")
        err = io.StringIO()
        with self.fake(no, "fetch_snapshot"), self.fake(lambda o, l: 1 / 0, "fetch_thumbnail"), contextlib.redirect_stderr(err):
            codes = [asyncio.run(get_thumb(self.cid, TS + i))[0] for i in range(3)]
        self.assertEqual(codes, [404, 404, 404])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(server._native_unsupported, {(self.cid, "snapshot")})
        self.assertEqual(err.getvalue().count(f"[thumb {self.cid} snapshot] unsupported: no reply (silence)"), 1)

    def test_image_unsupported_then_snapshot_takes_over(self):
        def no(out, log):
            raise p2p_media.Unsupported("no reply (silence)")
        def ok(out, log):
            Path(out).write_bytes(JPG)
        with self.fake(no), self.fake(ok, "fetch_snapshot"), contextlib.redirect_stderr(io.StringIO()):
            r = [asyncio.run(get_thumb(self.cid, TS + i))[0] for i in range(3)]
        self.assertEqual(r, [404, 200, 200])
        self.assertEqual([c[0] for c in self.calls], ["fetch_thumbnail", "fetch_snapshot", "fetch_snapshot"])

    def test_snapshot_busy_is_202_and_no_image_is_404_unremembered(self):
        server.CAMS[self.cid]["native_thumbs_mode"] = "snapshot"
        def busy(out, log):
            raise p2p_media.Busy("camera is busy")
        with self.fake(busy, "fetch_snapshot"):
            self.assertEqual(asyncio.run(get_thumb(self.cid))[0], 202)
        def none(out, log):
            raise p2p_media.NoSnapshot("no snapshot for this recording")
        with self.fake(none, "fetch_snapshot"), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(asyncio.run(get_thumb(self.cid))[0], 404)
        self.assertEqual(server._native_unsupported, set())

    def test_snapshot_first_reply_logged_once(self):
        server.CAMS[self.cid]["native_thumbs_mode"] = "snapshot"
        def ok(out, log):
            log("first reply: {}")
            Path(out).write_bytes(JPG)
        err = io.StringIO()
        with mock.patch.object(server, "_native_logged", set()), self.fake(ok, "fetch_snapshot"), contextlib.redirect_stderr(err):
            asyncio.run(get_thumb(self.cid))
            asyncio.run(get_thumb(self.cid, TS + 1))
        self.assertEqual(err.getvalue().count(f"[thumb {self.cid} snapshot] first reply"), 1)

    def test_cameras_list_exposes_flag(self):
        async def run():
            client = TestClient(TestServer(server.make_app(background=False)))
            await client.start_server()
            try:
                return await (await client.get("/api/cameras", headers={"Host": HOST})).json()
            finally:
                await client.close()
        row = next(c for c in asyncio.run(run()) if c["id"] == self.cid)
        self.assertIs(row["native_thumbs"], True)


class Resp:
    def __init__(self, mimetype, body):
        self.mimetype = mimetype
        self.plaintext = body if isinstance(body, bytes) else json.dumps(body).encode()


class FakeMS:
    def __init__(self, parts):
        self.parts, self.sent = parts, []

    def transceive(self, data, **kw):
        self.sent.append(data)
        parts = self.parts

        async def gen():
            for p in parts:
                yield p
            if parts == "hang":
                await asyncio.sleep(60)
        return gen() if parts != "hang" else gen_hang()


async def gen_hang():
    await asyncio.sleep(60)
    yield None


class Fetch(unittest.TestCase):
    def run_fetch(self, parts, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "t.jpg"
        ms = FakeMS(parts)
        lines = []

        @contextlib.asynccontextmanager
        async def fake_open(call, cam, days):
            yield mock.Mock(ms=ms, uid="u", codes=[])

        async def go():
            with mock.patch.object(p2p_media, "open_playback", fake_open):
                return await p2p_media.fetch_thumbnail(None, None, {"mac": "AA-BB"}, TS, out, log=lines.append, **kw)
        try:
            return asyncio.run(go()), out, ms, lines
        except Exception as e:
            self.err = e
            return None, out, ms, lines

    def test_good_jpeg_in_two_chunks(self):
        ok, out, ms, lines = self.run_fetch([
            Resp("application/json", {"type": "response", "params": {"error_code": 0, "seq_image_lens": [{"seq_image_id": "1", "length": str(len(JPG))}]}}),
            Resp("image/jpeg", JPG[:50]), Resp("image/jpeg", JPG[50:])])
        self.assertTrue(ok)
        self.assertEqual(out.read_bytes(), JPG)
        self.assertFalse(Path(str(out) + ".part").exists())
        req = json.loads(ms.sent[0])["params"]["image"]
        self.assertEqual((req["type"], req["start_time"], req["seq_image_type"], req["mac"]), ("playback_display", str(TS), "thumbnail", "AA-BB"))
        self.assertTrue(lines[0].startswith("first reply: "))

    def test_truncated_jpeg_fails_and_writes_nothing(self):
        ok, out, _, _ = self.run_fetch([Resp("image/jpeg", JPG[:60]), Resp("application/json", {"params": {"event_type": "stream_status", "status": "finished"}})])
        self.assertIsNone(ok)
        self.assertIsInstance(self.err, p2p_media.NoVideo)
        self.assertFalse(out.exists())

    def test_error_json_is_unsupported_with_code(self):
        ok, out, _, lines = self.run_fetch([Resp("application/json", {"type": "response", "params": {"error_code": -40106}})])
        self.assertIsInstance(self.err, p2p_media.Unsupported)
        self.assertEqual(self.err.reason, "error_code -40106")
        self.assertFalse(out.exists())
        self.assertEqual(len(lines), 1)

    def test_busy_code_is_busy(self):
        self.run_fetch([Resp("application/json", {"type": "response", "params": {"error_code": -52405}})])
        self.assertIsInstance(self.err, p2p_media.Busy)

    def test_reply_without_image_is_unsupported(self):
        self.run_fetch([Resp("application/json", {"type": "response", "params": {"error_code": 0}}),
                        Resp("application/json", {"params": {"event_type": "stream_status", "status": "finished"}})])
        self.assertIsInstance(self.err, p2p_media.Unsupported)

    def test_oversized_image_rejected(self):
        self.run_fetch([Resp("image/jpeg", b"\xff\xd8" + b"x" * (p2p_media.THUMB_MAX_BYTES + 10))])
        self.assertIsInstance(self.err, p2p_media.NoVideo)

    def test_hard_timeout(self):
        self.run_fetch("hang", hard_s=0.3)
        self.assertIsInstance(self.err, p2p_media.NoVideo)
        self.assertIn("timed out", str(self.err))


class FetchSnapshot(unittest.TestCase):
    def run_fetch(self, parts, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "s.jpg"
        ms = FakeMS(parts)
        lines = []

        @contextlib.asynccontextmanager
        async def fake_open(call, cam, days):
            yield mock.Mock(ms=ms, uid="u-1", codes=[])

        async def go():
            with mock.patch.object(p2p_media, "open_playback", fake_open):
                return await p2p_media.fetch_snapshot(None, mock.Mock(playerID="pl-1"), {}, TS, out, log=lines.append, **kw)
        try:
            return asyncio.run(go()), out, ms, lines
        except Exception as e:
            self.err = e
            return None, out, ms, lines

    def test_good_jpeg_and_request_shape(self):
        ok, out, ms, lines = self.run_fetch([Resp("application/json", {"type": "response", "params": {"error_code": 0}}), Resp("image/jpeg", JPG)])
        self.assertTrue(ok)
        self.assertEqual(out.read_bytes(), JPG)
        self.assertFalse(Path(str(out) + ".part").exists())
        self.assertEqual(json.loads(ms.sent[0])["params"], {"download": {
            "client_id": "u-1", "channels": [0], "media_type": 2, "start_time": str(TS), "player_id": "pl-1"}, "method": "get"})
        self.assertEqual(len(lines), 1)

    def test_truncated(self):
        ok, out, _, _ = self.run_fetch([Resp("image/jpeg", JPG[:60]), Resp("application/json", {"params": {"event_type": "stream_status", "status": "finished"}})])
        self.assertIsInstance(self.err, p2p_media.NoVideo)
        self.assertFalse(out.exists())

    def test_error_json_unsupported(self):
        self.run_fetch([Resp("application/json", {"type": "response", "params": {"error_code": -40106}})])
        self.assertIsInstance(self.err, p2p_media.Unsupported)
        self.assertEqual(self.err.reason, "error_code -40106")

    def test_busy(self):
        self.run_fetch([Resp("application/json", {"type": "response", "params": {"error_code": -52405}})])
        self.assertIsInstance(self.err, p2p_media.Busy)

    def test_silence_is_unsupported(self):
        self.run_fetch([])
        self.assertIsInstance(self.err, p2p_media.Unsupported)
        self.assertEqual(self.err.reason, "no reply (silence)")

    def test_ack_then_finished_without_image_is_no_snapshot(self):
        self.run_fetch([Resp("application/json", {"type": "response", "params": {"error_code": 0}}),
                        Resp("application/json", {"params": {"event_type": "stream_status", "status": "finished"}})])
        self.assertIsInstance(self.err, p2p_media.NoSnapshot)
        self.assertNotIsInstance(self.err, p2p_media.Unsupported)

    def test_oversize(self):
        self.run_fetch([Resp("image/jpeg", b"\xff\xd8" + b"x" * (p2p_media.THUMB_MAX_BYTES + 10))])
        self.assertIsInstance(self.err, p2p_media.NoVideo)

    def test_hard_timeout(self):
        self.run_fetch("hang", hard_s=0.3)
        self.assertIn("timed out", str(self.err))


if __name__ == "__main__":
    unittest.main()
