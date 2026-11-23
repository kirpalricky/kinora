"""Domain errors -> HTTP status mapping (kinora/errors.py, server.camera_errors). Offline: loopback only."""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import server  # noqa: E402
from kinora import config, errors  # noqa: E402
from kinora.camera import gate, session  # noqa: E402
from kinora.services import ptz  # noqa: E402


def make_app(exc):
    async def boom(request):
        raise exc
    app = web.Application(middlewares=[server.local_only, server.camera_errors])
    app.add_routes([web.get("/x", boom)])
    return app


async def fetch(exc, host="127.0.0.1:8080"):
    client = TestClient(TestServer(make_app(exc)))
    await client.start_server()
    try:
        resp = await client.get("/x", headers={"Host": host})
        return resp.status, await resp.text()
    finally:
        await client.close()


class StatusMapping(unittest.TestCase):
    def check(self, cls, status):
        got = asyncio.run(fetch(cls("exact text for the body")))
        self.assertEqual(got, (status, "exact text for the body"))

    def test_camera_error_is_502(self):
        self.check(errors.CameraError, 502)

    def test_unavailable_is_503(self):
        self.check(errors.Unavailable, 503)

    def test_bad_input_is_400(self):
        self.check(errors.BadInput, 400)

    def test_not_supported_is_501(self):
        self.check(errors.NotSupported, 501)

    def test_too_fast_is_429(self):
        self.check(errors.TooFast, 429)

    def test_subclasses_are_camera_errors(self):
        for cls in (errors.Unavailable, errors.BadInput, errors.NotSupported, errors.TooFast):
            self.assertTrue(issubclass(cls, errors.CameraError))
        self.assertEqual(errors.CameraError("x").text, "x")

    def test_bad_host_is_refused_before_the_handler(self):
        # local_only is outermost: a bad Host header gets 403 even though the handler would raise CameraError
        status, body = asyncio.run(fetch(errors.BadInput("should not be seen"), host="evil.example:8080"))
        self.assertEqual(status, 403)
        self.assertEqual(body, "bad Host header")


class ConfigErrorsAreNotLoginFailures(unittest.TestCase):
    def setUp(self):
        self.cid = next(iter(session._login_locks))   # a real camera id (the locks are per configured camera)

    def test_no_auth_method_is_bad_input_and_not_counted(self):
        cam = {"id": self.cid, "name": "X", "ip": "10.9.9.9", "enabled": True, "verified": True, "auth": "nope"}
        with mock.patch.object(session, "get_camera_credentials", return_value=("u", "p")), \
                mock.patch.object(session, "get_cloud_password_for", return_value="c"), \
                mock.patch.object(session.gate, "precheck"), \
                mock.patch.object(session.gate, "note_login_failure") as nlf, \
                mock.patch.dict(config.CAMS, {self.cid: cam}), \
                mock.patch.dict(session._clients, clear=True):
            with self.assertRaises(errors.BadInput) as cm:
                session.client(cam)
        self.assertEqual(cm.exception.text, f"{self.cid} has no auth method configured")
        nlf.assert_not_called()

    def test_disabled_camera_is_bad_input_and_not_counted(self):
        cam = {"id": self.cid, "name": "X", "ip": "10.9.9.9", "enabled": False, "verified": True, "auth": "admin_cloud"}
        with mock.patch.object(session.gate, "note_login_failure") as nlf, \
                mock.patch.dict(config.CAMS, {self.cid: cam}), \
                mock.patch.dict(session._clients, clear=True):
            with self.assertRaises(errors.BadInput) as cm:
                session.client(cam)
        self.assertEqual(cm.exception.text, "X is switched off or not verified: verify it in Settings first")
        nlf.assert_not_called()

    def test_real_login_failure_is_counted_and_is_a_camera_error(self):
        cam = {"id": self.cid, "name": "X", "ip": "10.9.9.9", "enabled": True, "verified": True, "auth": "admin_cloud"}

        def boom(c, resolved=None):
            raise RuntimeError("boom")
        with mock.patch.object(session, "build", boom), \
                mock.patch.object(session, "resolve", return_value=("u", "p", "c")), \
                mock.patch.object(session.gate, "precheck"), \
                mock.patch.object(session.gate, "note_login_failure") as nlf, \
                mock.patch.dict(config.CAMS, {self.cid: cam}), \
                mock.patch.dict(session._clients, clear=True):
            with self.assertRaises(errors.CameraError) as cm:
                session.client(cam)
        self.assertNotIsInstance(cm.exception, errors.BadInput)
        self.assertEqual(cm.exception.text, "X: login failed: boom")
        nlf.assert_called_once()   # a genuine failure still backs off


class GateRefusalInP2pClip(unittest.TestCase):
    """A gate refusal inside a P2P clip download is a 503 with the gate's text, not a failed download."""
    CAM = {"id": "cT", "name": "Test Cam", "ip": "10.9.9.9", "p2p": True, "enabled": True}
    MSG = "last login was 5 s ago, waiting 175 s"

    def test_is_503_with_gate_text_and_no_failure_memo(self):
        async def fake_api(cam, fn):
            raise session.as_http(Exception(gate.MARK + "Test Cam: " + self.MSG))

        async def fake_window(call, t, cam, start, end, out):
            await call(lambda t: None)   # the refusal comes from api(), as in production

        async def clip(request):
            await server._download(self.CAM, 1000, 1030, "cT_test_1000_1030.mp4")
            return web.Response(text="not reached")

        async def go():
            app = web.Application(middlewares=[server.local_only, server.camera_errors])
            app.add_routes([web.get("/clip", clip)])
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.get("/clip", headers={"Host": "127.0.0.1:8080"})
                return resp.status, await resp.text()
            finally:
                await client.close()

        # require_pytapo_tools is mocked: CI has no ffmpeg/ffprobe on PATH
        with mock.patch.object(server, "api", fake_api), \
                mock.patch.object(server, "client", lambda cam: None), \
                mock.patch.object(server, "require_pytapo_tools", lambda: None), \
                mock.patch.object(server.p2p_media, "fetch_window", fake_window), \
                mock.patch.object(server, "media_failed") as failed, \
                mock.patch.dict(server._fail, clear=True), \
                mock.patch.dict(server._media_block, clear=True), \
                mock.patch.dict(server._media_fails, clear=True):
            status, body = asyncio.run(go())
            self.assertEqual(server._fail, {})
            self.assertEqual(server._media_block, {})
            self.assertEqual(server._media_fails, {})
        failed.assert_not_called()
        self.assertEqual(status, 503)
        self.assertEqual(body, "Test Cam: " + self.MSG)
        self.assertRegex(body, r"last login was (\d+) s ago, waiting (\d+) s")   # static/app.js retries on this


class RealApiPath(unittest.TestCase):
    """The real session.api(): gate refusals stay 503 and keep the client; session errors drop it."""

    def setUp(self):
        self.cid = next(iter(config.CAMS))
        self.cam = {"id": self.cid, "name": "Test Cam"}
        self.fake = object()

    def call(self, exc):
        def fn(t):
            raise exc
        async def go():
            return await session.api(self.cam, fn)
        with mock.patch.object(session, "client", lambda cam: self.fake), \
                mock.patch.dict(session._clients, {self.cid: self.fake}):
            with self.assertRaises(errors.CameraError) as cm:
                asyncio.run(go())
            kept = session._clients.get(self.cid) is self.fake
        return cm.exception, kept

    def test_gate_refusal_is_503_without_marker_and_keeps_client(self):
        e, kept = self.call(gate.GateRefused(gate.MARK + "Test Cam: last login was 5 s ago, waiting 175 s"))
        self.assertIsInstance(e, errors.Unavailable)
        self.assertEqual(e.status, 503)
        self.assertNotIn(gate.MARK, e.text)
        self.assertEqual(e.text, "Test Cam: last login was 5 s ago, waiting 175 s")
        self.assertRegex(e.text, r"last login was \d+ s ago, waiting (\d+) s")
        self.assertTrue(kept, "a gate refusal is not a session error: the client must stay")

    def test_session_error_is_502_and_drops_client(self):
        e, kept = self.call(Exception("Error: -40401 Invalid stok"))
        self.assertIsInstance(e, errors.CameraError)
        self.assertNotIsInstance(e, errors.Unavailable)
        self.assertEqual(e.status, 502)
        self.assertFalse(kept, "-40401 is a session error: the client must be dropped")


class AsHttp(unittest.TestCase):
    def test_returns_unavailable_without_the_marker(self):
        r = session.as_http(Exception(gate.MARK + "Test Cam: hello there"))
        self.assertIsInstance(r, errors.Unavailable)
        self.assertEqual(r.status, 503)
        self.assertEqual(r.text, "Test Cam: hello there")
        self.assertNotIn(gate.MARK, r.text)

    def test_text_without_marker_is_kept(self):
        r = session.as_http(Exception("plain"))
        self.assertIsInstance(r, errors.Unavailable)
        self.assertEqual(r.text, "plain")


class PtzErrors(unittest.TestCase):
    CAM = {"id": "cP", "name": "Pan Cam", "ip": "10.9.9.9", "ptz": True}

    def step(self, cam, direction, api=None):
        async def fake_api(c, fn):
            return None
        return asyncio.run(ptz.step(api or fake_api, cam, direction))

    def test_cannot_pan_is_not_supported(self):
        with self.assertRaises(errors.NotSupported) as cm:
            self.step({"id": "cP", "name": "Fixed Cam"}, "left")
        self.assertEqual((cm.exception.status, cm.exception.text), (501, "Fixed Cam cannot pan/tilt"))

    def test_bad_direction_is_bad_input(self):
        with self.assertRaises(errors.BadInput) as cm:
            self.step(self.CAM, "diagonal")
        self.assertEqual((cm.exception.status, cm.exception.text), (400, "dir must be left, right, up or down"))

    def test_too_fast_is_too_many_requests(self):
        with mock.patch.dict(ptz._last, {"cP": time.time()}):
            with self.assertRaises(errors.TooFast) as cm:
                self.step(self.CAM, "up")
        self.assertEqual((cm.exception.status, cm.exception.text), (429, "too fast"))

    def test_valid_step_calls_the_camera_once(self):
        calls = []

        async def fake_api(c, fn):
            calls.append(fn)
        with mock.patch.dict(ptz._last, clear=True):
            asyncio.run(ptz.step(fake_api, self.CAM, "right"))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
