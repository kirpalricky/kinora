"""Export a time range as video+audio or audio only.

Recordings are pulled in EXPORT_WIN_S windows through the caller's `fetch_media` (so the camera layer's
locks, back-off and failure memory apply), concatenated by ffmpeg, and a JSON sidecar maps output offsets
back to wall-clock time (gaps between recordings are skipped).
"""
import asyncio
import datetime
import json
import re

from aiohttp import web

from kinora import config
from kinora.binaries import ffmpeg_path
from kinora.errors import BadInput, CameraError

jobs = {}
lock = asyncio.Lock()   # one export at a time


def _short(e):
    return re.sub(r"\s+", " ", str(e))[:200]


def merge(intervals):
    out = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def plan_windows(intervals, start, end):
    """Recorded intervals -> list of (a, b) request windows inside [start, end]: gaps skipped,
    pieces under 2 s dropped, each piece split into windows of at most EXPORT_WIN_S."""
    segs = [(max(a, start), min(b, end)) for a, b in merge(intervals)]
    segs = [(a, b) for a, b in segs if b - a >= 2]
    wins = []
    for a, b in segs:
        while a < b:
            wins.append((a, min(a + config.EXPORT_WIN_S, b)))
            a += config.EXPORT_WIN_S
    return wins


_DURATION_RE = re.compile(rb"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def duration_from_ffmpeg_stderr(text: bytes) -> float:
    """Seconds from the `Duration: HH:MM:SS.xx` line of `ffmpeg -hide_banner -i <file>` (0.0 if absent or N/A)."""
    m = _DURATION_RE.search(text)
    if not m:
        return 0.0
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


async def probe_duration(path):
    # ffmpeg with no output file exits 1 after printing the input's Duration on stderr; that is expected
    p = await asyncio.create_subprocess_exec(
        ffmpeg_path(), "-hide_banner", "-i", str(path),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await p.communicate()
    return duration_from_ffmpeg_stderr(err)


async def run(job, cam, list_recordings, fetch_media):
    """list_recordings(date 'YYYYMMDD') -> [{'start','end'}...]; fetch_media(cam, a, b, name) writes config.CACHE/name."""
    try:
        async with lock:
            job["state"] = "running"
            job["message"] = "Looking up recordings"
            d0 = datetime.datetime.fromtimestamp(job["start"]).date()
            d1 = datetime.datetime.fromtimestamp(job["end"]).date()
            ivs, d = [], d0
            while d <= d1:
                data = await list_recordings(d.strftime("%Y%m%d"))
                ivs += [[c["start"], c["end"]] for c in data]
                d += datetime.timedelta(days=1)
            wins = plan_windows(ivs, job["start"], job["end"])
            if not wins:
                raise BadInput("no recording in that time range")
            job["total"] = len(wins)
            files = []
            for i, (a, b) in enumerate(wins):
                if job["cancel"]:
                    job["state"], job["message"] = "cancelled", "Cancelled"
                    return
                job["message"] = f"Downloading part {i + 1} of {len(wins)} from the camera"
                name = f"{cam['id']}_{a}_{b}.mp4"
                await fetch_media(cam, a, b, name)
                files.append((a, b, config.CACHE / name))
                job["done"] = i + 1
            job["message"] = "Building the output file"
            stamp = datetime.datetime.fromtimestamp(job["start"]).strftime("%Y%m%d-%H%M%S")
            safe = re.sub(r"[^A-Za-z0-9]+", "_", cam["name"]).strip("_")
            out = config.EXPORTS / f"{safe}_{stamp}_{int(job['end'] - job['start'])}s.{config.MODES[job['mode']]}"
            lst = config.EXPORTS / f".{job['id']}.txt"
            lst.write_text("".join(f"file '{f}'\n" for _, _, f in files))
            if job["mode"] == "video":
                codec = ["-c:v", "copy", "-c:a", "aac", "-b:a", "96k"]
            elif job["mode"] == "audio_wav":
                codec = ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"]   # ready for ASR
            else:
                codec = ["-vn", "-c:a", "aac", "-b:a", "64k"]
            proc = await asyncio.create_subprocess_exec(
                ffmpeg_path(), "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), *codec, str(out),
                stderr=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL)
            _, err = await proc.communicate()
            lst.unlink(missing_ok=True)
            if proc.returncode != 0 or not out.exists():
                out.unlink(missing_ok=True)
                raise CameraError("ffmpeg failed (does this camera record audio?): "
                                  + err.decode(errors="replace")[-200:])
            offset, segments = 0.0, []
            for a, b, f in files:   # map output positions back to wall-clock time
                dur = await probe_duration(f)
                segments.append({"start": a, "end": b, "offset_s": round(offset, 3), "duration_s": round(dur, 3)})
                offset += dur
            out.with_suffix(out.suffix + ".json").write_text(json.dumps({
                "camera": cam["name"], "mode": job["mode"], "requested_start": job["start"],
                "requested_end": job["end"], "segments": segments}, indent=2))
            job.update(state="done", message="Done", file=out.name, size=out.stat().st_size)
    except (CameraError, web.HTTPException) as e:   # HTTPException: transitional, server.fetch_media still raises web.HTTP*
        job.update(state="error", message=e.text or getattr(e, "reason", ""))
    except Exception as e:
        job.update(state="error", message=_short(e))
