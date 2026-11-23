"""Live view and the login gate: persisted RTSP-auth refusal and stderr scanning (no camera, no ffmpeg run).
Run: .venv/bin/python -m unittest tests.test_live_gate   (from the repo root)
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aiohttp import web  # noqa: E402
from kinora.camera import gate  # noqa: E402
from kinora.services import live  # noqa: E402

SECRET = "rtsp-secret-pw"
CAM = {"id": "c1", "name": "Cam", "ip": "192.0.2.10"}
REASON = "Live view disabled: the camera rejected the camera-account password. Fix it, then run python -m kinora.camera.gate credentials-changed c1"


class FakeRequest:
    match_info = {"file": "index.m3u8"}


class FakeProc:
    """Fake ffmpeg. Its stderr reader is built inside the running loop (run_relay) from `chunks`."""
    pid = 4343

    def __init__(self, chunks=()):
        self.returncode = None
        self.chunks = list(chunks)
        self.stderr = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15


class LiveRefusal(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for p in (mock.patch.dict(live._blocked, {}, clear=True),
                  mock.patch.dict(live._procs, {}, clear=True),
                  mock.patch.object(live, "get_camera_credentials", return_value=("rtspadmin", SECRET))):
            p.start()
            self.addCleanup(p.stop)

    def test_refused_before_spawn_when_persisted_flag_set(self):
        spawned = []

        async def fake_exec(*a, **kw):
            spawned.append(a)
            raise AssertionError("ffmpeg must not be spawned while Live is disabled")

        async def run():
            with mock.patch.object(gate, "live_disabled", return_value=REASON), \
                 mock.patch.object(live.asyncio, "create_subprocess_exec", fake_exec):
                await live.live_file(FakeRequest(), CAM, self.root)
        with self.assertRaises(web.HTTPServiceUnavailable) as cm:
            asyncio.run(run())
        self.assertEqual(cm.exception.text, REASON)
        self.assertEqual(spawned, [])

    def test_not_refused_when_flag_clear(self):
        async def fake_exec(*a, **kw):
            raise RuntimeError("spawned")

        async def run():
            with mock.patch.object(gate, "live_disabled", return_value=None), \
                 mock.patch.object(live.asyncio, "create_subprocess_exec", fake_exec):
                await live.live_file(FakeRequest(), CAM, self.root)
        with self.assertRaisesRegex(RuntimeError, "spawned"):
            asyncio.run(run())


class LiveFileReason(unittest.TestCase):
    """The relay died on a 401: the response carries the persisted reason, not "camera busy or unreachable"."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for p in (mock.patch.dict(live._blocked, {}, clear=True),
                  mock.patch.dict(live._procs, {}, clear=True),
                  mock.patch.object(live, "LIST_KEEP_S", 0.01)):
            p.start()
            self.addCleanup(p.stop)

    def test_dead_relay_after_401_reports_the_persisted_reason(self):
        async def fake_start(live_root, cam, stream="stream1"):
            proc = FakeProc()
            proc.returncode = 1          # ffmpeg exited straight away: the 401 watcher stopped it
            live._procs[cam["id"]] = proc

        async def run():
            # first check (before start): not disabled; second check (after the relay died): disabled
            with mock.patch.object(gate, "live_disabled", side_effect=[None, REASON]), \
                 mock.patch.object(live, "_start", fake_start):
                await live.live_file(FakeRequest(), CAM, self.root)
        with self.assertRaises(web.HTTPBadGateway) as cm:
            asyncio.run(run())
        self.assertEqual(cm.exception.text, REASON)

    def test_dead_relay_without_flag_keeps_the_generic_text(self):
        async def fake_start(live_root, cam, stream="stream1"):
            proc = FakeProc()
            proc.returncode = 1
            live._procs[cam["id"]] = proc

        async def run():
            with mock.patch.object(gate, "live_disabled", return_value=None), \
                 mock.patch.object(live, "_start", fake_start):
                await live.live_file(FakeRequest(), CAM, self.root)
        with self.assertRaises(web.HTTPBadGateway) as cm:
            asyncio.run(run())
        self.assertIn("camera busy or unreachable", cm.exception.text)


class StderrScan(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for p in (mock.patch.dict(live._procs, {}, clear=True),
                  mock.patch.object(live, "get_camera_credentials", return_value=("rtspadmin", SECRET)),
                  mock.patch.object(live, "LIST_KEEP_S", 0.01)):
            p.start()
            self.addCleanup(p.stop)

    def run_relay(self, proc):
        """Start one relay whose fake ffmpeg writes proc.chunks to stderr (a pause between chunks);
        wait for the watcher to see EOF. Returns the mocked gate.note_rtsp_auth_failure."""
        async def fake_exec(*a, **kw):
            return proc

        async def run():
            proc.stderr = asyncio.StreamReader()
            with mock.patch.object(live.asyncio, "create_subprocess_exec", fake_exec), \
                 mock.patch.object(gate, "note_rtsp_auth_failure") as note:
                await live._start(self.root, CAM)
                for i, chunk in enumerate(proc.chunks):
                    proc.stderr.feed_data(chunk)
                    if i < len(proc.chunks) - 1:
                        await asyncio.sleep(0.05)
                proc.stderr.feed_eof()
                await asyncio.gather(*list(live._watchers))
                await asyncio.sleep(0.1)   # let the temp list-file unlink fire
                live._procs.pop(CAM["id"], None)
                return note
        return asyncio.run(run())

    def test_rtsp_401_is_persisted_and_relay_stopped(self):
        msg = (f"[rtsp @ 0x1] Server returned 401 Unauthorized (authorization failed)\n"
               f"rtsp://rtspadmin:{SECRET}@192.0.2.10/stream1: Server returned 401\n").encode()
        proc = FakeProc([msg])
        note = self.run_relay(proc)
        note.assert_called_once_with(CAM)
        self.assertTrue(proc.terminated)

    def test_rtsp_status_line_triggers(self):
        note = self.run_relay(FakeProc([b"RTSP/1.0 401 Unauthorized\r\n"]))
        note.assert_called_once_with(CAM)

    def test_match_is_case_insensitive(self):
        note = self.run_relay(FakeProc([b"rtsp/1.0 401 unauthorized\n"]))
        note.assert_called_once_with(CAM)

    def test_bare_unauthorized_word_does_not_trigger(self):
        proc = FakeProc([b"Unauthorized\n"])
        note = self.run_relay(proc)
        note.assert_not_called()
        self.assertFalse(proc.terminated)

    def test_muxer_comparison_with_401_does_not_trigger(self):
        proc = FakeProc([b"Assertion status 401 >= 400 failed in the muxer\n"])
        note = self.run_relay(proc)
        note.assert_not_called()
        self.assertFalse(proc.terminated)

    def test_match_split_across_reads_is_seen(self):
        proc = FakeProc([b"...RTSP/1.0 401 Unau", b"thorized\n"])
        note = self.run_relay(proc)
        note.assert_called_once_with(CAM)

    def test_other_errors_are_ignored(self):
        proc = FakeProc([b"Connection refused\nServer returned 4010 timeout\nNo route to host\n"])
        note = self.run_relay(proc)
        note.assert_not_called()
        self.assertFalse(proc.terminated)

    def test_no_stderr_attribute_is_harmless(self):
        class Bare:
            pid = 1
            returncode = None

        async def fake_exec(*a, **kw):
            return Bare()

        async def run():
            with mock.patch.object(live.asyncio, "create_subprocess_exec", fake_exec), \
                 mock.patch.object(gate, "note_rtsp_auth_failure") as note:
                await live._start(self.root, CAM)
                await asyncio.gather(*list(live._watchers))
                live._procs.pop(CAM["id"], None)
                return note
        note = asyncio.run(run())
        note.assert_not_called()


if __name__ == "__main__":
    unittest.main()
