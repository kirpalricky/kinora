"""Recording and event listings, cached on disk (a past day is cached forever, today for TODAY_TTL s)."""
import datetime
import json
import time

from kinora import config
from kinora.errors import CameraError


async def listing_data(cam, date, kind, fetch, refresh=False):
    """Recording/event listing for one day -> (data, 'hit'|'miss'|'stale').
    A day saved after it ended is cached forever; otherwise TODAY_TTL s; stale copy if the camera
    is unreachable."""
    f = config.META / f"{cam['id']}_{date}_{kind}.json"
    day_end = (datetime.datetime.strptime(date, "%Y%m%d") + datetime.timedelta(days=1)).timestamp()
    have = f.exists()
    final = have and f.stat().st_mtime > day_end + 3600   # saved after the day ended: complete forever
    fresh = have and (final or time.time() - f.stat().st_mtime < config.TODAY_TTL)
    if fresh and not refresh:
        return json.loads(f.read_text()), "hit"
    try:
        data = await fetch(cam, date)
    except CameraError:
        if have:
            return json.loads(f.read_text()), "stale"
        raise
    f.write_text(json.dumps(data))
    return data, "miss"


def _int(v):
    """v as an int, or None for anything else (bool, str, float, None): the page puts these in HTML and the cache."""
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _span(start, end):
    s, e = _int(start), _int(end)
    return (s, e) if s is not None and e is not None and 0 <= s <= e < 10**11 else None


async def fetch_recordings(api, cam, date):
    raw = await api(cam, lambda t: t.getRecordings(date))
    clips = []
    for day in raw:
        for v in day.values():
            span = _span(v.get("startTime"), v.get("endTime"))
            if span:   # malformed rows (a camera is not trusted to send numbers) are dropped
                clips.append({"start": span[0], "end": span[1], "type": v.get("vedio_type")})
    return sorted(clips, key=lambda c: c["start"])


async def fetch_events(api, cam, date):
    d = datetime.datetime.strptime(date, "%Y%m%d")
    raw = await api(cam, lambda t: t.getEvents(int(d.timestamp()),
                                               int((d + datetime.timedelta(days=1)).timestamp())))
    out = []
    for e in raw:
        span, kind = _span(e.get("start_time"), e.get("end_time")), _int(e.get("alarm_type"))
        if span and kind is not None:
            out.append({"start": span[0], "end": span[1], "type": kind})
    return sorted(out, key=lambda e: e["start"])
