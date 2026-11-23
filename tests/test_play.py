"""Offline test of kinora.services.play with SYNTHETIC video (ffmpeg testsrc2) behind a fake camera session. No camera, no network."""
import asyncio, contextlib, os, subprocess, sys, tempfile, time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kinora import config, p2p_media  # noqa: E402
from kinora.camera import session  # noqa: E402
from kinora.camera.slots import slots  # noqa: E402
from kinora.services import play as p2p_play  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="playtest-"))
SYN = TMP / "synth.ts"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15", "-t", "30", "-c:v", "libx264",
                "-g", "30", "-pix_fmt", "yuv420p", "-f", "mpegts", str(SYN)], check=True)
DATA = SYN.read_bytes()
CH = 188 * 40


class FakeMS:
    def __init__(self, die_after=None, pace=0.05, refuse=False):
        self.die_after, self.pace, self.refuse = die_after, pace, refuse

    async def transceive(self, req):
        if self.refuse:
            await asyncio.sleep(30)
            return
        yield SimpleNamespace(session=5, mimetype="application/json", plaintext=b"{}", audioPayload=b"", audioPayloadType=None)
        n = 0
        for off in range(0, len(DATA), CH):
            yield SimpleNamespace(session=5, mimetype="video/mp2t", plaintext=DATA[off:off + CH], audioPayload=b"", audioPayloadType=None)
            n += 1
            if self.die_after and n >= self.die_after:
                return
            await asyncio.sleep(self.pace)
        await asyncio.sleep(60)   # camera goes quiet: stall handling


OPENED = {"n": 0, "closed": 0, "scales": [], "play_steps": 0}


def make_open(direct=False, **kw):
    @contextlib.asynccontextmanager
    async def fake_open(call, cam, days):
        OPENED["n"] += 1
        ctx = SimpleNamespace(ms=FakeMS(**kw), uid=1, codes=[], needs_play=not direct,
                              first_request=lambda s, scale="1/1": OPENED["scales"].append(scale) or "{}")
        async def send_play(sess, start, scale="1/1"): OPENED["play_steps"] += 1
        ctx.send_play = send_play
        try:
            yield ctx
        finally:
            OPENED["closed"] += 1
    return fake_open


def ffprocs():
    out = subprocess.run(["pgrep", "-f", str(TMP)], capture_output=True, text=True).stdout.split()
    return [p for p in out if p != str(os.getpid())]


async def main():
    locks = {cid: slots.lock(cid) for cid in ("c199", "c201")}
    async def api(cam, fn): return fn(None)
    # the real shared slots (play registered itself at import); a fake live relay that is never running
    saved = (session.api, config.PLAY_ROOT, slots._live_running, slots._live_idle_s, slots._stop_live)
    session.api, config.PLAY_ROOT = api, TMP / "play"
    slots.register_live(lambda cid: False, lambda cid: 0, lambda cam: asyncio.sleep(0))
    p2p_play.startup()
    cam = {"id": "c199", "name": "Baby Room", "stream_playback": True}
    kitchen = {"id": "c201", "name": "Kitchen", "fast_forward": True}
    ok = True
    def check(name, cond, extra=""):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name, extra)
        ok &= bool(cond)

    # 1. start: playlist appears quickly, segments grow
    p2p_media.open_playback = make_open()
    t0 = time.time(); p = await p2p_play.start(cam, 1000)
    check("start returns state playing", p.state == "playing", f"({time.time()-t0:.1f}s)")
    for _ in range(40):
        if p.status()["playlist"]: break
        await asyncio.sleep(0.25)
    check("playlist appears within 4 s of start", p.status()["playlist"] and time.time() - t0 < 4, f"({time.time()-t0:.1f}s)")
    await asyncio.sleep(4)
    check("segments are being produced", p.segments() >= 1, f"(segments={p.segments()})")
    f = p2p_play.file_for("c199", p.sid, "index.m3u8")
    check("playlist is served and is an EVENT playlist", f is not None and "EVENT" in f.read_text())
    check("bad names/sids are refused", p2p_play.file_for("c199", p.sid, "../x") is None and p2p_play.file_for("c199", "zzzz", "index.m3u8") is None)
    check("media lock held while playing", locks["c199"].locked())
    # 2. a clip/export job cannot start; a start while the lock is held by a clip is refused fast
    check("active() true", p2p_play.active("c199"))
    # 3. stop: ENDLIST written, lock released, files kept, no ffmpeg left
    await p2p_play.stop("c199")
    txt = (p.dir / "index.m3u8").read_text()
    check("stop writes ENDLIST and keeps files", "#EXT-X-ENDLIST" in txt and p.segments() >= 1)
    check("lock released after stop", not locks["c199"].locked())
    check("state ended, session context closed", p.state == "ended" and OPENED["closed"] == OPENED["n"], f"(state={p.state})")
    await asyncio.sleep(0.5)
    check("no ffmpeg orphan", not ffprocs(), str(ffprocs()))
    # 4. busy when a clip holds the lock for longer than the start is willing to wait; a short clip job is waited for
    p2p_play.LOCK_WAIT_S = 0.5
    await locks["c199"].acquire()
    try:
        await p2p_play.start(cam, 1000); check("start refused when a clip holds the lock too long", False)
    except p2p_play.PlayError as e:
        check("start refused when a clip holds the lock too long", e.kind == "busy", e.text)
    check("a waiting start left no 'wanted' marker behind", not slots.play_wanted("c199"))
    locks["c199"].release()
    p2p_play._last_setup["c199"] = 0
    await locks["c199"].acquire()
    async def release_soon():
        await asyncio.sleep(0.2); locks["c199"].release()
    rel = asyncio.ensure_future(release_soon())
    marker = []
    async def watch():
        await asyncio.sleep(0.05); marker.append(slots.play_wanted("c199"))
    w = asyncio.ensure_future(watch())
    ps = await p2p_play.start(cam, 1000)
    await asyncio.gather(rel, w)
    check("start waits for a short clip job and then plays", ps.state == "playing", ps.state)
    check("clip requests see 'wanted' while a start is waiting", marker == [True], str(marker))
    await p2p_play.stop("c199")
    p2p_play.LOCK_WAIT_S = 25
    # 5. latest wins: three quick starts, only the last survives
    p2p_play._last_setup["c199"] = 0
    res = await asyncio.gather(p2p_play.start(cam, 1), p2p_play.start(cam, 2), p2p_play.start(cam, 3), return_exceptions=True)
    kinds = [getattr(r, "kind", "ok") for r in res]
    check("rapid starts: older ones superseded, newest ok", kinds == ["superseded", "superseded", "ok"], str(kinds))
    check("exactly one live session and one lock", p2p_play.get("c199").ts == 3 and locks["c199"].locked())
    # 6. natural end of camera stream (generator returns)
    await p2p_play.stop("c199")
    p2p_media.open_playback = make_open(die_after=5, pace=0.01)
    p2p_play._last_setup["c199"] = 0
    p = await p2p_play.start(cam, 5)
    for _ in range(40):
        if p.state != "playing": break
        await asyncio.sleep(0.25)
    check("camera ending the stream ends the session cleanly", p.state == "ended" and not locks["c199"].locked(), f"({p.state}: {p.message})")
    # 7. setup failure -> error + lock released + no ffmpeg
    @contextlib.asynccontextmanager
    async def failing(call, cam, days):
        raise p2p_media.Busy("camera is busy")
        yield
    p2p_media.open_playback = failing
    p2p_play._last_setup["c199"] = 0
    try:
        await p2p_play.start(cam, 7); check("setup failure is reported", False)
    except Exception as e:
        check("setup failure is reported and releases the lock", not locks["c199"].locked(), type(e).__name__ + ": " + str(e)[:60])
    await asyncio.sleep(0.5)
    check("no ffmpeg orphan after failure", not ffprocs())
    # 8. reaper: idle session is stopped
    p2p_media.open_playback = make_open()
    p2p_play._last_setup["c199"] = 0
    p = await p2p_play.start(cam, 9)
    p.last_get = time.time() - 100
    r = asyncio.ensure_future(p2p_play.reaper()); await asyncio.sleep(6); r.cancel()
    check("idle session stopped by the reaper", p.state == "ended" and not locks["c199"].locked(), f"({p.state}: {p.message})")
    check("idle stop message is the new wording", p.message == "Playback stopped because this page stopped watching. Pick a time to continue.", p.message)
    # 8b. lag: production far ahead of requests. A page that keeps fetching is NOT stopped; a quiet page (> LAG_QUIET_S) IS stopped
    p2p_media.open_playback = make_open()
    p2p_play._last_setup["c199"] = 0
    p = await p2p_play.start(cam, 9)
    await asyncio.sleep(0.5)
    p.segments = lambda: 100            # 200 s of media produced; the page has asked for nothing beyond segment 0
    # 8b-4: which requests count as watching. A playlist request refreshes last_get only; a segment request also refreshes last_seg
    p.last_get = p.last_seg = time.time() - 30
    p2p_play.file_for("c199", p.sid, "index.m3u8")
    check("playlist request refreshes last_get but NOT last_seg", time.time() - p.last_get < 1 and time.time() - p.last_seg > 25, f"(last_get {time.time()-p.last_get:.1f}s, last_seg {time.time()-p.last_seg:.1f}s ago)")
    p2p_play.file_for("c199", p.sid, "seg_00000.ts")
    check("segment request refreshes last_seg", time.time() - p.last_seg < 1)
    async def poll(name, secs):
        end = time.time() + secs
        while time.time() < end:
            p2p_play.file_for("c199", p.sid, name); await asyncio.sleep(1)
    # 8b-1: lagging, segments requested every second (a 16x page outrunning real time): NOT stopped
    r = asyncio.ensure_future(p2p_play.reaper())
    await poll("seg_00000.ts", 6)
    check("lagging but fetching segments is NOT stopped by lag", p.state == "playing", f"({p.state}: {p.message})")
    # 8b-2: lagging, playlist polled every second but no segment for 20 s (paused page): stopped with the lag message
    p.last_seg = time.time() - 20
    await poll("index.m3u8", 6)
    r.cancel()
    check("lagging, playlist-only page (no segment for 20 s) IS stopped with the new lag message", p.state == "ended" and p.message == "Playback paused to free the camera. Press play or pick a time to continue.", f"({p.state}: {p.message})")
    check("lag stop released the lock", not locks["c199"].locked())
    # 8c. IDLE_S unchanged: 50 s without a request stops the session even when it is not lagging
    p2p_media.open_playback = make_open()
    p2p_play._last_setup["c199"] = 0
    p = await p2p_play.start(cam, 9)
    p.last_get = time.time() - (p2p_play.IDLE_S + 5)
    r = asyncio.ensure_future(p2p_play.reaper()); await asyncio.sleep(6); r.cancel()
    check("IDLE_S still stops a non-lagging session", p.state == "ended" and not locks["c199"].locked(), f"({p.state}: {p.message})")
    # 9. 30 start/stop cycles: no task / fd growth
    p2p_play.MIN_SETUP_GAP_S = 0.0; p2p_play.MAX_STARTS_PER_MIN = 1000
    def fds(): return len(os.listdir("/dev/fd"))
    base_fds, base_tasks = fds(), len(asyncio.all_tasks())
    for i in range(30):
        await p2p_play.start(cam, 100 + i); await asyncio.sleep(0.1); await p2p_play.stop("c199")
    await asyncio.sleep(1)
    check("30 start/stop cycles leave fds and tasks flat", fds() <= base_fds + 2 and len(asyncio.all_tasks()) <= base_tasks + 1, f"(fds {base_fds}->{fds()}, tasks {base_tasks}->{len(asyncio.all_tasks())})")
    check("no ffmpeg orphans at the end", not ffprocs(), str(ffprocs()))
    # 10. direct (port 8800) camera, key-frame fast-forward: scale passed on, no 'do play' step, status carries the scale
    p2p_media.open_playback = make_open()
    p2p_media.open_direct = make_open(direct=True)
    OPENED["scales"].clear(); OPENED["play_steps"] = 0
    p2p_play._last_setup["c201"] = 0
    pk = await p2p_play.start(kitchen, 500, "16/1")
    check("direct camera: playing with scale 16/1 and kind direct", pk.state == "playing" and pk.direct and pk.status()["scale"] == "16/1", str(pk.status()["scale"]))
    check("direct camera: first request carried scale 16/1", OPENED["scales"] == ["16/1"], str(OPENED["scales"]))
    for _ in range(40):
        if pk.status()["playlist"]: break
        await asyncio.sleep(0.25)
    check("direct camera: HLS playlist appears", bool(pk.status()["playlist"]))
    check("direct camera: no 'do play' step was sent", OPENED["play_steps"] == 0)
    await p2p_play.stop("c201")
    check("direct camera: lock released after stop", not locks["c201"].locked())
    # 11. P2P camera still sends the play step and uses scale 1/1
    OPENED["scales"].clear(); OPENED["play_steps"] = 0
    p2p_play._last_setup["c199"] = 0
    pp = await p2p_play.start(cam, 600)
    check("P2P camera: scale 1/1, kind p2p, play step sent", pp.status()["scale"] == "1/1" and not pp.direct and OPENED["scales"] == ["1/1"] and OPENED["play_steps"] == 1)
    await p2p_play.stop("c199")
    check("no ffmpeg orphans after the direct/p2p tests", not ffprocs(), str(ffprocs()))
    session.api, config.PLAY_ROOT, slots._live_running, slots._live_idle_s, slots._stop_live = saved
    print("ALL PASS" if ok else "SOME FAILED")
    return ok

sys.exit(0 if asyncio.run(main()) else 1)
