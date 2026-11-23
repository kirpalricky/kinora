"""The live relay must keep the RTSP password out of ffmpeg's argv (no camera, no ffmpeg run).
Run: .venv/bin/python -m unittest tests.test_live_argv
"""
import asyncio
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kinora.services import live  # noqa: E402

SECRET = "p@ss'w0rd/with%odd chars"
CAM = {"id": "c1", "name": "Cam", "ip": "192.0.2.10"}


class FakeProc:
    pid = 4242
    returncode = None


class LiveArgv(unittest.TestCase):
    def test_password_not_in_argv_and_list_file_is_private(self):
        seen = {}

        async def fake_exec(*argv, **kw):
            seen["argv"] = list(argv)
            lst = argv[argv.index("-i") + 1]
            seen["list"] = lst
            seen["mode"] = stat.S_IMODE(os.stat(lst).st_mode)
            seen["text"] = Path(lst).read_text()
            return FakeProc()

        async def run():
            with tempfile.TemporaryDirectory() as d, \
                    mock.patch.object(live, "get_camera_credentials", return_value=("rtspadmin", SECRET)), \
                    mock.patch.object(live.asyncio, "create_subprocess_exec", fake_exec), \
                    mock.patch.object(live, "LIST_KEEP_S", 0.05):
                await live._start(Path(d), CAM)
                await asyncio.sleep(0.2)
                live._procs.pop("c1", None)
        asyncio.run(run())

        joined = " ".join(seen["argv"])
        self.assertNotIn(SECRET, joined)
        self.assertNotIn("p%40ss", joined)
        self.assertNotIn("rtsp://", joined)
        self.assertEqual(seen["mode"], 0o600)
        self.assertIn("rtsp://rtspadmin:", seen["text"])
        self.assertIn("option rtsp_transport tcp", seen["text"])
        self.assertFalse(os.path.exists(seen["list"]), "list file must be deleted shortly after start")


if __name__ == "__main__":
    unittest.main()
