"""kinora.cli: doctor output and exit code with fakes, --version, flags, browser-open decision (offline, no camera).
Run: .venv/bin/python -m unittest tests.test_cli   (from the repo root)
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kinora import cli, config, instance  # noqa: E402

GOOD_KR = lambda: {"ok": True, "backend": "fake.Keyring", "reason": ""}  # noqa: E731
FREE = lambda port: (cli.OK, f"port {port} is free")  # noqa: E731


def doctor(**kw):
    lines = []
    with tempfile.TemporaryDirectory() as tmp:
        kw.setdefault("home", tmp)
        kw.setdefault("keyring_status", GOOD_KR)
        kw.setdefault("port_check", FREE)
        kw.setdefault("which", lambda n: f"/usr/bin/{n}")
        code = cli.run_doctor(port=8123, out=lines.append, **kw)
    return code, "\n".join(lines)


class Doctor(unittest.TestCase):
    def test_all_good_exits_0(self):
        code, text = doctor()
        self.assertEqual(code, 0, text)
        self.assertIn("ffmpeg: /usr/bin/ffmpeg", text)
        self.assertIn("Everything looks good", text)
        self.assertIn(f"Kinora {config.VERSION}", text)

    def test_missing_ffmpeg_fails_with_hint(self):
        code, text = doctor(which=lambda n: None, hint="brew install ffmpeg")
        self.assertEqual(code, 1)
        self.assertIn("ffmpeg not found on PATH. Install FFmpeg: brew install ffmpeg", text)
        self.assertIn("ffprobe not found", text)

    def test_only_ffprobe_missing(self):
        code, text = doctor(which=lambda n: "/x/ffmpeg" if n == "ffmpeg" else None)
        self.assertEqual(code, 1)
        self.assertIn("ffprobe not found", text)

    def test_keyring_unusable_is_a_warning_not_failure(self):
        code, text = doctor(keyring_status=lambda: {"ok": False, "backend": None, "reason": "no usable OS keyring"})
        self.assertEqual(code, 0)
        self.assertIn("warn", text)

    def test_keyring_probe_raising_is_a_warning(self):
        def boom():
            raise RuntimeError("locked")
        code, text = doctor(keyring_status=boom)
        self.assertEqual(code, 0)
        self.assertIn("locked", text)

    def test_port_busy_fails(self):
        code, _ = doctor(port_check=lambda p: (cli.FAIL, "port in use"))
        self.assertEqual(code, 1)

    def test_unwritable_data_folder_fails(self):
        with tempfile.NamedTemporaryFile() as f:   # a file where a folder is expected
            code, text = doctor(home=Path(f.name) / "sub")
        self.assertEqual(code, 1)
        self.assertIn("not writable", text)

    def test_install_hint_is_os_specific(self):
        from kinora import binaries
        for plat, word in (("darwin", "brew"), ("linux", "apt"), ("win32", "winget")):
            with mock.patch.object(sys, "platform", plat):
                self.assertIn(word, binaries.install_hint())

    def test_port_check_free_and_busy(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        try:
            status, _ = cli._port_check(port)   # busy, not Kinora (nothing answers HTTP 200)
        finally:
            s.close()
        self.assertEqual(status, cli.FAIL)
        self.assertEqual(cli._port_check(port)[0], cli.OK)


class Flags(unittest.TestCase):
    def test_version(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["--version"]), 0)
        self.assertEqual(buf.getvalue().strip(), f"kinora {config.VERSION}")

    def test_parse(self):
        a = cli.build_parser().parse_args(["--port", "9000", "--no-browser", "--home", "/tmp/x", "doctor"])
        self.assertEqual((a.port, a.no_browser, a.home, a.command), (9000, True, "/tmp/x", "doctor"))
        self.assertIsNone(cli.build_parser().parse_args([]).command)

    def test_apply_env(self):
        env = {}
        cli.apply_env(cli.build_parser().parse_args(["--port", "9001", "--home", "/tmp/h"]), env)
        self.assertEqual(env, {"KINORA_PORT": "9001", "KINORA_HOME": "/tmp/h"})
        with self.assertRaises(SystemExit):
            cli.apply_env(cli.build_parser().parse_args(["--port", "70000"]), {})

    def test_version_pyproject_is_dynamic(self):
        text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
        self.assertIn('dynamic = ["version"]', text)
        self.assertIn("kinora.config.VERSION", text)
        self.assertIn("pytapo==3.4.34", text)


class BrowserDecision(unittest.TestCase):
    def test_cases(self):
        f = cli.should_open_browser
        self.assertTrue(f(False, {}, "darwin"))
        self.assertFalse(f(True, {"DISPLAY": ":0"}, "linux"))
        self.assertFalse(f(False, {"KINORA_NO_BROWSER": "1"}, "darwin"))
        self.assertFalse(f(False, {}, "linux"))
        self.assertTrue(f(False, {"DISPLAY": ":0"}, "linux"))
        self.assertTrue(f(False, {"WAYLAND_DISPLAY": "wayland-0"}, "linux"))
        self.assertTrue(f(False, {"KINORA_NO_BROWSER": "0"}, "darwin"))

    def test_opens_once_listening(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        opened = []
        try:
            ok = cli._wait_then_open(s.getsockname()[1], "http://localhost:1", opener=opened.append, timeout=3)
        finally:
            s.close()
        self.assertTrue(ok)
        self.assertEqual(opened, ["http://localhost:1"])


class RunningUrl(unittest.TestCase):
    def test_recorded_port_is_read_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "server.lock"
            legacy = Path(tmp) / "legacy.lock"   # never the real legacy lock in the user cache dir
            with mock.patch.object(instance, "lock_path", lambda: lock), mock.patch.object(instance, "legacy_lock_path", lambda: legacy), \
                    mock.patch.object(instance, "_held", []), mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("KINORA_NO_INSTANCE_LOCK", None)
                instance.acquire()
                instance.record_port(8765)
                self.assertEqual(instance.running_url(), "http://localhost:8765")
                for h in instance._held:
                    h.close()

    def test_missing_file_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(instance, "lock_path", lambda: Path(tmp) / "none"):
            self.assertEqual(instance.running_url(8080), "http://localhost:8080")


if __name__ == "__main__":
    unittest.main()
