"""Who may use a camera's video slot.

Tapo cameras serve only ~2 video streams at once, shared with the phone app. Four kinds of user compete here:
  clip   a download (clips, thumbnails, export windows): takes the camera's media lock for the duration
  play   a streaming playback session: owns the media lock for its whole life
  live   the RTSP relay: a separate ffmpeg, not under the lock; it gives way to a clip once nobody watches it
  (export is a series of clips)
Rules (one place instead of four ad hoc checks):
  * a clip is refused while a playback is open or while a playback start is waiting for the camera;
  * a thumbnail (beside_live) ignores the live relay; every other clip frees an idle live relay (last request longer than LIVE_IDLE_S ago) and is refused while live is in use;
  * a playback start WAITS for running clip downloads (they finish within seconds), then stops live;
  * everything else is refused, never queued.
Callers get `Busy` (a domain error, no HTTP); the web layer maps it to 503.
"""
import asyncio
import contextlib

from kinora import config

LIVE_IDLE_S = 8   # nobody is watching live any more


class Busy(Exception):
    """The camera's video slot is taken (text is meant for the user)."""


async def _no_stop(cam):
    return None


class Slots:
    def __init__(self, cam_ids, *, play_active=lambda cid: False, live_running=lambda cid: False,
                 live_idle_s=lambda cid: 0, stop_live=_no_stop):
        """play_active(cid)->bool, live_running(cid)->bool, live_idle_s(cid)->seconds since the last live request,
        stop_live(cam)->coroutine. The defaults mean: nothing playing, no live relay."""
        self._locks = {cid: asyncio.Lock() for cid in cam_ids}
        self._wanted = {cid: 0 for cid in cam_ids}
        self._play_active, self._live_running = play_active, live_running
        self._live_idle_s, self._stop_live = live_idle_s, stop_live

    def register_play(self, active):
        """Called by kinora.services.play at import: active(cid)->bool, true while a playback session is open."""
        self._play_active = active

    def register_live(self, running, idle_s, stop):
        """Called by kinora.services.live at import: running(cid), idle_s(cid), stop(cam)->coroutine."""
        self._live_running, self._live_idle_s, self._stop_live = running, idle_s, stop

    def register_camera(self, cid):
        """A camera added at runtime gets its media lock and wait counter (idempotent)."""
        self._locks.setdefault(cid, asyncio.Lock())
        self._wanted.setdefault(cid, 0)

    def unregister_camera(self, cid):
        """A removed camera's entries go away (its play and live were stopped first by cameras.py)."""
        self._locks.pop(cid, None)
        self._wanted.pop(cid, None)

    def lock(self, cid):
        return self._locks[cid]

    def busy(self, cid):
        """True while a clip, export or playback holds the camera's media lock (False for an unknown camera)."""
        lock = self._locks.get(cid)
        return lock is not None and lock.locked()

    def play_wanted(self, cid):
        """True while a playback start is waiting for the camera."""
        return self._wanted[cid] > 0

    def refuse_if_playing(self, cam, action):
        """Early refusal for callers that must not even queue (clip, export, live) while a playback is open."""
        if self._play_active(cam["id"]) or self.play_wanted(cam["id"]):
            raise Busy(f"{cam['name']}: playback is open; stop it to {action}")

    @contextlib.asynccontextmanager
    async def clip(self, cam, beside_live=False):
        """Hold the media lock for one download, applying the rules above. beside_live (thumbnails): an open Live
        relay neither refuses nor stops the download; playback still wins."""
        cid = cam["id"]
        self.refuse_if_playing(cam, "download clips or export")
        async with self._locks[cid]:
            if self.play_wanted(cid):    # a playback start is waiting: give way instead of starting another download
                raise Busy(f"{cam['name']}: playback is starting")
            if self._live_running(cid) and not beside_live:
                if self._live_idle_s(cid) > LIVE_IDLE_S:
                    await self._stop_live(cam)
                else:
                    raise Busy(f"{cam['name']}: live view is open; close it to load recordings")
            yield

    async def acquire_play(self, cam, wait_s):
        """Take the media lock for a playback session (the caller releases it with release_play) and stop live."""
        cid = cam["id"]
        self._wanted[cid] += 1
        try:
            await asyncio.wait_for(self._locks[cid].acquire(), wait_s)
        except asyncio.TimeoutError:
            raise Busy(f"{cam['name']}: a clip download or export is using the camera")
        finally:
            self._wanted[cid] -= 1
        try:
            if self._live_running(cid):
                await self._stop_live(cam)
        except BaseException:
            self._locks[cid].release()
            raise

    def release_play(self, cid):
        self._locks[cid].release()


# The shared instance: the services register into it; server.py and the services use this one.
slots = Slots(config.CAMS)
