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


async def fetch_recordings(api, cam, date):
    raw = await api(cam, lambda t: t.getRecordings(date))
    clips = [{"start": v["startTime"], "end": v["endTime"], "type": v.get("vedio_type")}
             for day in raw for v in day.values()]
    return sorted(clips, key=lambda c: c["start"])


async def fetch_events(api, cam, date):
    d = datetime.datetime.strptime(date, "%Y%m%d")
    raw = await api(cam, lambda t: t.getEvents(int(d.timestamp()),
                                               int((d + datetime.timedelta(days=1)).timestamp())))
    out = [{"start": e["start_time"], "end": e["end_time"], "type": e["alarm_type"]} for e in raw]
    return sorted(out, key=lambda e: e["start"])
