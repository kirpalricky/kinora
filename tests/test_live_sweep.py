"""live: the secret ffconcat list is deleted with retries (Windows can hold it open), and stale lists are swept at
startup. Offline: a temp dir stands in for the temp directory; no ffmpeg runs.
Run: .venv/bin/python -m unittest tests.test_live_sweep
"""
import asyncio
import errno
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kinora.services import live  # noqa: E402


class Sweep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        p = mock.patch("kinora.services.live.tempfile.gettempdir", return_value=str(self.dir))
        p.start()
        self.addCleanup(p.stop)

    def make(self, name, age_s):
        f = self.dir / name
        f.write_text("secret")
        t = time.time() - age_s
        os.utime(f, (t, t))
        return f

    def test_removes_only_stale_secret_lists(self):
        stale = self.make("tapo-live-old.ffconcat", 600)
        fresh = self.make("tapo-live-new.ffconcat", 5)
        other = self.make("unrelated.ffconcat", 600)
        live.sweep_secret_lists()
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists(), "a list younger than 60 s may belong to a live relay")
        self.assertTrue(other.exists())

    def test_empty_dir_is_fine(self):
        live.sweep_secret_lists()


class UnlinkRetry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(live, "UNLINK_GAP_S", 0.01)
        p.start()
        self.addCleanup(p.stop)

    def test_retries_a_locked_file_until_it_goes(self):
        path = os.path.join(self.tmp.name, "tapo-live-x.ffconcat")
        Path(path).write_text("secret")
        real_unlink = os.unlink
        calls = {"n": 0}

        def flaky(p):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise PermissionError(errno.EACCES, "locked")
            return real_unlink(p)

        async def run():
            with mock.patch.object(live.os, "unlink", flaky):
                live._unlink(path)
                for _ in range(100):
                    if not os.path.exists(path):
                        break
                    await asyncio.sleep(0.01)
        asyncio.run(run())
        self.assertFalse(os.path.exists(path))
        self.assertEqual(calls["n"], 4)

    def test_gives_up_after_the_retry_budget(self):
        path = os.path.join(self.tmp.name, "tapo-live-y.ffconcat")
        Path(path).write_text("secret")

        calls = {"n": 0}

        def always_locked(p):
            calls["n"] += 1
            raise PermissionError(errno.EACCES, "locked")

        async def run():
            with mock.patch.object(live.os, "unlink", always_locked):
                live._unlink(path)
                await asyncio.sleep(0.3)
        asyncio.run(run())
        self.assertEqual(calls["n"], 1 + live.UNLINK_TRIES)


if __name__ == "__main__":
    unittest.main()
