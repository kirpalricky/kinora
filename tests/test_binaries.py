"""ffmpeg resolution (KINORA_FFMPEG > PATH, clear BinaryMissing) and the ffprobe-free duration probe.
Offline: no ffmpeg is run; the subprocess is faked.
Run: .venv/bin/python -m unittest tests.test_binaries
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
import server  # noqa: E402,F401  (installs the login gate; no camera is contacted)
from kinora import binaries  # noqa: E402
from kinora.errors import CameraError  # noqa: E402
from kinora.services import export  # noqa: E402

# what ffmpeg prints on stderr for `ffmpeg -hide_banner -i file.mp4` (no output file: exit 1)
STDERR = (b"Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'x.mp4':\n"
          b"  Duration: 00:01:02.50, start: 0.000000, bitrate: 512 kb/s\n"
          b"At least one output file must be specified\n")


class FfmpegPath(unittest.TestCase):
    def test_env_override_wins_over_path(self):
        with tempfile.NamedTemporaryFile() as f:
            os.chmod(f.name, stat.S_IRWXU)
            with mock.patch.dict(os.environ, {"KINORA_FFMPEG": f.name}), \
                    mock.patch.object(binaries.shutil, "which", return_value="/usr/bin/ffmpeg"):
                self.assertEqual(binaries.ffmpeg_path(), f.name)

    def test_env_pointing_at_missing_file_is_binary_missing(self):
        with mock.patch.dict(os.environ, {"KINORA_FFMPEG": "/no/such/ffmpeg-binary"}):
            with self.assertRaises(binaries.BinaryMissing) as cm:
                binaries.ffmpeg_path()
        self.assertIn("KINORA_FFMPEG", cm.exception.text)

    def test_path_lookup_when_env_unset(self):
        env = {k: v for k, v in os.environ.items() if k != "KINORA_FFMPEG"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(binaries.shutil, "which", return_value="/opt/bin/ffmpeg") as which:
            self.assertEqual(binaries.ffmpeg_path(), "/opt/bin/ffmpeg")
        which.assert_called_once_with("ffmpeg")

    def test_absent_everywhere_raises_clear_error(self):
        env = {k: v for k, v in os.environ.items() if k != "KINORA_FFMPEG"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(binaries.shutil, "which", return_value=None):
            with self.assertRaises(binaries.BinaryMissing) as cm:
                binaries.ffmpeg_path()
        self.assertIsInstance(cm.exception, CameraError)   # the error middleware maps it like other domain errors
        self.assertIn("KINORA_FFMPEG", cm.exception.text)
        self.assertIn("ffmpeg not found", cm.exception.text)


class PytapoTools(unittest.TestCase):
    """pytapo's converter needs ffprobe and a literal ffmpeg on PATH: a missing one is named, as BinaryMissing."""

    def which(self, present):
        return lambda name: f"/opt/bin/{name}" if name in present else None

    def test_both_present_is_fine(self):
        with mock.patch.object(binaries.shutil, "which", side_effect=self.which({"ffprobe", "ffmpeg"})):
            binaries.require_pytapo_tools()   # no raise

    def test_missing_ffprobe_is_named(self):
        with mock.patch.object(binaries.shutil, "which", side_effect=self.which({"ffmpeg"})):
            with self.assertRaises(binaries.BinaryMissing) as cm:
                binaries.require_pytapo_tools()
        self.assertTrue(cm.exception.text.startswith("ffprobe not found on PATH"), cm.exception.text)
        self.assertIsInstance(cm.exception, CameraError)

    def test_missing_ffmpeg_is_named(self):
        with mock.patch.object(binaries.shutil, "which", side_effect=self.which({"ffprobe"})):
            with self.assertRaises(binaries.BinaryMissing) as cm:
                binaries.require_pytapo_tools()
        self.assertTrue(cm.exception.text.startswith("ffmpeg not found on PATH"), cm.exception.text)

    def test_both_missing_names_both(self):
        with mock.patch.object(binaries.shutil, "which", return_value=None):
            with self.assertRaises(binaries.BinaryMissing) as cm:
                binaries.require_pytapo_tools()
        self.assertTrue(cm.exception.text.startswith("ffprobe and ffmpeg not found on PATH"), cm.exception.text)

    def test_fetch_window_reports_missing_tool_before_any_camera_call(self):
        from kinora import p2p_media
        calls = []

        def call(fn):   # must never be reached
            calls.append(fn)
        with mock.patch.object(binaries.shutil, "which", return_value=None):
            with self.assertRaises(binaries.BinaryMissing):
                asyncio.run(p2p_media.fetch_window(call, None, {"id": "c1"}, 0, 60, "/tmp/never.mp4"))
        self.assertEqual(calls, [])

    def test_download_reports_missing_tool_before_login(self):
        import server
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(server, "CACHE", Path(home)), \
                mock.patch.object(server, "client") as client, \
                mock.patch.object(binaries.shutil, "which", return_value=None):
            with self.assertRaises(binaries.BinaryMissing):
                asyncio.run(server._download({"id": "c1", "name": "Cam", "ip": "10.0.0.1"}, 0, 60, "clip.mp4"))
        client.assert_not_called()

    def test_prepend_puts_the_ffmpeg_folder_first_on_path(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "bin", "ffmpeg")
            with mock.patch.dict(os.environ, {"KINORA_FFMPEG": target, "PATH": "/usr/bin"}):
                binaries.prepend_ffmpeg_dir_to_path()
                self.assertEqual(os.environ["PATH"], os.path.dirname(os.path.abspath(target)) + os.pathsep + "/usr/bin")

    def test_prepend_is_a_no_op_without_kinora_ffmpeg(self):
        env = {k: v for k, v in os.environ.items() if k != "KINORA_FFMPEG"}
        env["PATH"] = "/usr/bin"
        with mock.patch.dict(os.environ, env, clear=True):
            binaries.prepend_ffmpeg_dir_to_path()
            self.assertEqual(os.environ["PATH"], "/usr/bin")


class DurationFromStderr(unittest.TestCase):
    def test_parses_hms_with_fraction(self):
        self.assertAlmostEqual(export.duration_from_ffmpeg_stderr(STDERR), 62.5)

    def test_hours(self):
        self.assertAlmostEqual(export.duration_from_ffmpeg_stderr(b"  Duration: 02:00:00.04, start: 0"), 7200.04)

    def test_no_duration_line_is_zero(self):
        self.assertEqual(export.duration_from_ffmpeg_stderr(b"Stream #0:0: Video"), 0.0)

    def test_na_duration_is_zero(self):
        self.assertEqual(export.duration_from_ffmpeg_stderr(b"  Duration: N/A, start: 0"), 0.0)


class ProbeDuration(unittest.TestCase):
    def test_runs_ffmpeg_not_ffprobe_and_parses_stderr(self):
        seen = {}

        class FakeProc:
            returncode = 1

            async def communicate(self):
                return b"", STDERR

        async def fake_exec(*argv, **kw):
            seen["argv"] = list(argv)
            return FakeProc()

        with mock.patch.object(export.asyncio, "create_subprocess_exec", fake_exec), \
                mock.patch.object(export, "ffmpeg_path", return_value="/fake/ffmpeg"):
            dur = asyncio.run(export.probe_duration(Path("/tmp/clip.mp4")))
        self.assertAlmostEqual(dur, 62.5)
        self.assertEqual(seen["argv"][0], "/fake/ffmpeg")
        self.assertNotIn("ffprobe", seen["argv"])
        self.assertEqual(seen["argv"][1:4], ["-hide_banner", "-i", str(Path("/tmp/clip.mp4"))])


if __name__ == "__main__":
    unittest.main()
