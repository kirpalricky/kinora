"""hls.kill_orphans with psutil: only a process named *ffmpeg* is terminated; a harmless name mismatch, a vanished
process, access denied and a bad pidfile are all handled, and the pidfile is always removed. Offline: nothing is killed.
Run: .venv/bin/python -m unittest tests.test_hls_orphans
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import psutil  # noqa: E402
from kinora.services import hls  # noqa: E402


class KillOrphans(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def pidfile(self, cid, text):
        f = hls.pidfile(self.root, cid)
        f.write_text(text)
        return f

    def test_name_mismatch_is_not_terminated_and_pidfile_removed(self):
        # our own pid: a real process whose name is not ffmpeg, so it must never be touched
        f = self.pidfile("cam1", str(os.getpid()))
        with mock.patch.object(psutil.Process, "terminate") as term:
            hls.kill_orphans(self.root)
        term.assert_not_called()
        self.assertFalse(f.exists())

    def test_ffmpeg_named_process_is_terminated(self):
        f = self.pidfile("cam1", "4242")
        fake = mock.Mock()
        fake.name.return_value = "ffmpeg"
        with mock.patch.object(hls.psutil, "Process", return_value=fake) as P:
            hls.kill_orphans(self.root)
        P.assert_called_once_with(4242)
        fake.terminate.assert_called_once_with()
        self.assertFalse(f.exists())

    def test_vanished_process_is_ignored_and_pidfile_removed(self):
        f = self.pidfile("cam1", "4242")
        with mock.patch.object(hls.psutil, "Process", side_effect=psutil.NoSuchProcess(4242)):
            hls.kill_orphans(self.root)
        self.assertFalse(f.exists())

    def test_access_denied_is_ignored_and_pidfile_removed(self):
        f = self.pidfile("cam1", "4242")
        fake = mock.Mock()
        fake.name.side_effect = psutil.AccessDenied(4242)
        with mock.patch.object(hls.psutil, "Process", return_value=fake):
            hls.kill_orphans(self.root)
        fake.terminate.assert_not_called()
        self.assertFalse(f.exists())

    def test_garbage_pidfile_is_removed(self):
        f = self.pidfile("cam1", "not-a-pid")
        hls.kill_orphans(self.root)
        self.assertFalse(f.exists())


if __name__ == "__main__":
    unittest.main()
