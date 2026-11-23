"""Shared helpers for the two HLS producers (live relay and playback streams)."""
import re
from pathlib import Path

import psutil
from aiohttp import web

FILE_RE = re.compile(r"^(index\.m3u8|seg_\d{5}\.ts)$")


def content_type(name: str) -> str:
    return "video/mp2t" if name.endswith(".ts") else "application/vnd.apple.mpegurl"


def file_response(path: Path) -> web.FileResponse:
    return web.FileResponse(path, headers={"Cache-Control": "no-store", "Content-Type": content_type(path.name)})


def pidfile(root: Path, cid: str) -> Path:
    return root / f"{cid}.pid"


def kill_orphans(root: Path):
    """Kill ffmpeg processes recorded in <root>/*.pid by a server that was killed without cleanup.
    Only a process whose name contains "ffmpeg" is terminated (a pid can be reused); the pidfile is always removed."""
    root.mkdir(parents=True, exist_ok=True)
    for f in root.glob("*.pid"):
        try:
            proc = psutil.Process(int(f.read_text()))
            if "ffmpeg" in proc.name().lower():
                proc.terminate()
        except (psutil.Error, ValueError, OSError):   # NoSuchProcess, AccessDenied, bad pidfile, ...
            pass
        finally:
            f.unlink(missing_ok=True)
