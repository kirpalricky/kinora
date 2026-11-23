"""Single server per machine (kinora/instance.py): OS lock in the user cache dir (no camera, no network).

The lock file is pointed at a temp dir; the real user cache dir is never touched.
Run: .venv/bin/python -m unittest tests.test_instance   (from the repo root)
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from kinora import config, instance  # noqa: E402

REAL_LOCK_PATH = instance.lock_path   # captured before setUp replaces it

# Same sequence as server.py's __main__ block: acquire, or exit non-zero with the message.
CHILD = r"""
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from kinora import instance
instance.lock_path = lambda: Path(sys.argv[2])
try:
    instance.acquire()
except instance.AlreadyRunning as e:
    sys.exit(str(e))
"""


class SingleInstance(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.lock = Path(tmp.name) / "server.lock"
        for p in (mock.patch.object(instance, "lock_path", lambda: self.lock),
                  mock.patch.object(instance, "_held", []),
                  mock.patch.dict(os.environ, {}, clear=False)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("KINORA_NO_INSTANCE_LOCK", None)

    def child(self):
        env = {k: v for k, v in os.environ.items() if k != "KINORA_NO_INSTANCE_LOCK"}
        return subprocess.run([sys.executable, "-c", CHILD, str(ROOT), str(self.lock)],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)

    def test_second_acquire_is_refused_with_clear_message(self):
        handle = instance.acquire()
        self.addCleanup(handle.close)
        with self.assertRaisesRegex(instance.AlreadyRunning,
                                    "another Kinora server is already running on this machine"):
            instance.acquire()

    def test_second_process_exits_non_zero_with_the_message(self):
        handle = instance.acquire()
        self.addCleanup(handle.close)
        r = self.child()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("another Kinora server is already running on this machine", r.stderr)

    def test_lock_is_released_when_the_holder_exits(self):
        r = self.child()          # the child takes the lock and exits, as a server that stopped would
        self.assertEqual(r.returncode, 0, r.stderr)
        handle = instance.acquire()
        self.addCleanup(handle.close)
        self.assertIsNotNone(handle)

    def test_escape_hatch_for_tests(self):
        handle = instance.acquire()
        self.addCleanup(handle.close)
        with mock.patch.dict(os.environ, {"KINORA_NO_INSTANCE_LOCK": "1"}):
            self.assertIsNone(instance.acquire())

    def test_lock_lives_in_user_cache_dir_not_the_data_dir(self):
        with mock.patch.object(instance, "user_cache_dir", lambda app: str(self.lock.parent / "cache-root")), \
             mock.patch.object(config, "HOME", self.lock.parent / "some-data-home"):
            p = REAL_LOCK_PATH()
        self.assertEqual(p, self.lock.parent / "cache-root" / "server.lock")
        self.assertTrue(p.parent.is_dir())


if __name__ == "__main__":
    unittest.main()
