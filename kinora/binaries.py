"""Locate the external ffmpeg binary: KINORA_FFMPEG (explicit path) first, then PATH."""
import os
import shutil
import sys

from kinora.config import env as _env
from kinora.errors import CameraError


class BinaryMissing(CameraError):
    """ffmpeg cannot be found. A CameraError so the existing error middleware reports it."""


_HINT = ("ffmpeg not found. Install FFmpeg (macOS: brew install ffmpeg; Windows: winget install ffmpeg) "
         "or set KINORA_FFMPEG to the full path of the ffmpeg binary.")


def install_hint():
    """The one-line ffmpeg install command for this OS."""
    if sys.platform == "darwin":
        return "brew install ffmpeg"
    if sys.platform == "win32":
        return "winget install ffmpeg"
    return "sudo apt install ffmpeg   (Debian/Ubuntu; other distributions: use their package manager)"


def require_pytapo_tools():
    """pytapo's own converter (P2P downloads and clips) runs `ffprobe` and a literal `ffmpeg` found on PATH.
    Raise BinaryMissing naming what is missing, so it is not mistaken for a camera failure."""
    missing = [name for name in ("ffprobe", "ffmpeg") if shutil.which(name) is None]
    if missing:
        raise BinaryMissing(f"{' and '.join(missing)} not found on PATH, needed for playback downloads and clips. "
                            "Install FFmpeg (it ships both), or set KINORA_FFMPEG to the ffmpeg binary: its folder "
                            "is put on PATH at startup, beside ffprobe.")


def prepend_ffmpeg_dir_to_path():
    """Startup: with KINORA_FFMPEG set, put its folder first on PATH so pytapo finds ffmpeg and ffprobe beside it."""
    path = _env("FFMPEG")
    if path:
        folder = os.path.dirname(os.path.abspath(os.path.expanduser(path)))
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")


def ffmpeg_path():
    path = _env("FFMPEG")
    if path:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
        raise BinaryMissing(f"KINORA_FFMPEG is set but is not an executable file: {path}")
    found = shutil.which("ffmpeg")
    if found is None:
        raise BinaryMissing(_HINT)
    return found
