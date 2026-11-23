"""health.ping argv per OS (sys.platform patched) and a missing ping binary returning False. Offline: no ping is sent.
Run: .venv/bin/python -m unittest tests.test_health_ping
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kinora import health  # noqa: E402


class PingArgv(unittest.TestCase):
    def argv(self, platform):
        with mock.patch.object(sys, "platform", platform):
            return health.ping_argv("192.0.2.10")

    def test_darwin_uses_milliseconds(self):
        self.assertEqual(self.argv("darwin"), ["ping", "-c", "1", "-W", "1000", "192.0.2.10"])

    def test_linux_uses_seconds(self):
        self.assertEqual(self.argv("linux"), ["ping", "-c", "1", "-W", "1", "192.0.2.10"])

    def test_windows_uses_n_and_w_ms(self):
        self.assertEqual(self.argv("win32"), ["ping", "-n", "1", "-w", "1000", "192.0.2.10"])


class MissingPing(unittest.TestCase):
    def test_missing_binary_returns_false(self):
        async def no_ping(*a, **kw):
            raise FileNotFoundError("ping")
        with mock.patch.object(health.asyncio, "create_subprocess_exec", no_ping):
            self.assertIs(asyncio.run(health._ping("192.0.2.10")), False)


if __name__ == "__main__":
    unittest.main()
