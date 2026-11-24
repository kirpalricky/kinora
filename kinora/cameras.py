"""Runtime camera registry: the only code that mutates the camera table (config.CAMS, config.CAM_BY_IP) after startup.

The server starts with zero cameras; cameras are added, changed and removed while it runs, and every write
is validated (config.check_camera / check_cameras) and persisted to cameras.json before it is published.

Lock order (never reversed):
  1. _cfg_lock (asyncio): one registry write at a time.
  2. the per-camera login locks (session.login_lock, a threading.Lock taken in a worker thread), in id order
     when several are needed. Held while a client is created, so an update waits for a login in progress.
  3. the gate lock (gate._gate_lock): only inside gate calls. It is not re-entrant and is never held across
     an operation. This module makes no gate call.

Quiesce (update that changes ip or auth, update that switches a camera off, and remove): stop play and live for the
camera, take the login lock, drop the cached client (ip or auth change: refuse if an export is queued or running, and
clear health state); then swap the table entry. An update that changes a playback key (PLAYBACK_KEYS) stops play and
live and refuses while an export of the camera runs, but keeps the session (no login). Remove refuses while a clip or
export holds the camera's slot lock.
Nothing is published before cameras.json is written, and cameras.json is written in a worker thread under _cfg_lock.

Tombstones: a removed camera keeps {id, device_id, ip, removed_at} in cameras.json "tombstones". Its id is never
reused; a camera added at a tombstoned IP reuses that device_id, so its gate budget carries over.

Writes: a temp file in the data directory, fsync, os.replace; the previous file is kept as cameras.json.bak.
"""
import asyncio
import builtins
import contextlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

from kinora import config, health
from kinora.camera import session
from kinora.camera.slots import slots
from kinora.services import export, live
from kinora.services import play as p2p_play

_cfg_lock = asyncio.Lock()   # 1. one registry write at a time
DROP = object()              # a change value that removes the field from the camera (its default then applies)
PLAYBACK_KEYS = ("p2p", "stream_playback", "fast_forward")   # changing these changes the playback path


class Refused(Exception):
    """The change is refused because the camera is busy (an export is queued or running)."""


def list():
    """Copies of the cameras in the live table."""
    return [dict(c) for c in config.CAMS.values()]


# ---- persistence -------------------------------------------------------------------------
def _write_atomic(path: Path, data: bytes):
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)   # the old file is still the one on disk
        raise


def _persist(cams, tombs):
    """cameras.json <- {"version": 1, "cameras", "tombstones"}, previous version kept as cameras.json.bak."""
    path = config.HOME / "cameras.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"version": 1, "cameras": cams, "tombstones": tombs}, indent=2).encode() + b"\n"
    if path.exists():
        _write_atomic(path.with_name(path.name + ".bak"), path.read_bytes())
    _write_atomic(path, data)


# ---- locks -------------------------------------------------------------------------------
def _release_if_taken(lock):
    def done(f):
        if not f.cancelled() and f.exception() is None:
            lock.release()
    return done


async def _acquire(lock: threading.Lock):
    """Take a threading lock in a worker thread. If this task is cancelled while waiting, the lock is released
    as soon as the worker acquires it (a cancelled to_thread would otherwise leave it held forever)."""
    fut = asyncio.ensure_future(asyncio.to_thread(lock.acquire))
    try:
        await asyncio.shield(fut)
    except BaseException:
        fut.add_done_callback(_release_if_taken(lock))
        raise


@contextlib.asynccontextmanager
async def _login_locked(cids):
    """The login locks of these cameras, taken in id order (2.)."""
    taken = []
    try:
        for cid in sorted(cids):
            lock = session.login_lock(cid)
            await _acquire(lock)
            taken.append(lock)
        yield
    finally:
        for lock in reversed(taken):
            lock.release()


# ---- quiesce -----------------------------------------------------------------------------
def _refuse_if_exporting(cams):
    ids = {c["id"] for c in cams}
    for job in export.jobs.values():
        if job.get("cam") in ids and job.get("state") in ("queued", "running"):
            raise Refused(f"camera {job['cam']}: an export is queued or running; cancel it first")


async def _stop_media(cam):
    """Play and live of the camera stop (both are no-ops when nothing runs)."""
    await p2p_play.stop(cam["id"], "stopped: the camera was changed or removed")
    await live.stop(config.LIVE_ROOT, cam)


# ---- writes ------------------------------------------------------------------------------
def _tombstone_for_ip(ip):
    return next((t for t in reversed(config.TOMBSTONES) if t["ip"] == ip), None)


async def add(cam: dict):
    """Add a camera. New cameras get a random device_id (or the device_id of a removed camera at the same IP) and
    enabled:false unless the caller says otherwise. Returns a copy of the stored camera."""
    async with _cfg_lock:
        new = dict(cam)
        cid = new.get("id")
        if cid in config.CAMS:
            raise ValueError(f"camera id {cid!r} already exists")
        if any(t["id"] == cid for t in config.TOMBSTONES):
            raise ValueError(f"camera id {cid!r} belonged to a removed camera and cannot be reused")
        tomb = _tombstone_for_ip(new.get("ip"))
        if "device_id" not in new:
            new["device_id"] = tomb["device_id"] if tomb else str(uuid.uuid4())
        elif tomb and new["device_id"] != tomb["device_id"]:
            raise ValueError(f"ip {new['ip']} was used by a removed camera: its device_id must be reused")
        new.setdefault("enabled", False)
        config.check_camera(new)
        config.check_cameras([*config.CAMS.values(), new])
        # create the per-camera entries first, then publish
        session.register_camera(new)
        slots.register_camera(cid)
        try:
            await asyncio.to_thread(_persist, [*config.CAMS.values(), new], config.TOMBSTONES)
        except BaseException:
            session.unregister_camera(cid)
            slots.unregister_camera(cid)
            raise
        config.CAMS[cid] = new
        config.CAM_BY_IP[new["ip"]] = new
        return dict(new)


async def update(cid, changes: dict):
    """Change one camera (see update_many)."""
    await update_many({cid: changes})


async def update_many(changes: dict):
    """Apply {camera id: {field: value}} as one registry write. Two cameras may swap IPs in one call.
    The id is immutable and device_id cannot be changed (it keys the gate budget). Changing ip or auth
    quiesces the camera first."""
    async with _cfg_lock:
        old, new = {}, {}
        for cid, ch in changes.items():
            if cid not in config.CAMS:
                raise KeyError(cid)
            old[cid] = config.CAMS[cid]
            if "id" in ch and ch["id"] != cid:
                raise ValueError(f"camera id is immutable ({cid!r})")
            if "device_id" in ch and "device_id" in old[cid] and ch["device_id"] != old[cid]["device_id"]:
                raise ValueError(f"device_id of camera {cid!r} cannot be changed")
            new[cid] = {k: v for k, v in {**old[cid], **ch}.items() if v is not DROP}
            config.check_camera(new[cid])
        final = [new.get(c["id"], c) for c in config.CAMS.values()]
        config.check_cameras(final)   # uniqueness is judged on the final state, so a swap of IPs is allowed
        moved = [cid for cid in changes
                 if any(new[cid].get(k) != old[cid].get(k) for k in ("ip", "auth"))]
        off = [cid for cid in changes if old[cid].get("enabled") and not new[cid].get("enabled")]
        playback = [cid for cid in changes if any(new[cid].get(k) != old[cid].get(k) for k in PLAYBACK_KEYS)]
        if moved or playback:
            _refuse_if_exporting([old[cid] for cid in [*moved, *playback]])
        quiesce = [*dict.fromkeys([*moved, *off])]   # switched off or moved: play and live stop, the session goes
        for cid in [*dict.fromkeys([*quiesce, *playback])]:   # a playback change stops play and live, keeps the session
            await _stop_media(old[cid])
        async with _login_locked(quiesce):
            for cid in quiesce:
                session.drop_client(cid)
            for cid in moved:
                health.forget(cid)
            await asyncio.to_thread(_persist, final, config.TOMBSTONES)
            for cid in changes:   # first out, then in: a swap of two IPs must not clobber either entry
                if config.CAM_BY_IP.get(old[cid]["ip"]) is old[cid]:
                    del config.CAM_BY_IP[old[cid]["ip"]]
            for cid in changes:
                config.CAMS[cid] = new[cid]
                config.CAM_BY_IP[new[cid]["ip"]] = new[cid]


async def reorder(order):
    """Put the cameras in this order (a list of every camera id, once each). The order is the one of cameras.json and
    is what the switcher, Settings and /api/cameras show. Nothing else changes: no camera is quiesced and no login happens.
    A list that does not name exactly the cameras present (one was added or removed meanwhile) is refused."""
    async with _cfg_lock:
        if (not isinstance(order, builtins.list) or len(set(order)) != len(order) or set(order) != set(config.CAMS)):
            raise ValueError("the order must name every camera once")
        final = [config.CAMS[cid] for cid in order]
        if [c["id"] for c in final] == builtins.list(config.CAMS):
            return
        await asyncio.to_thread(_persist, final, config.TOMBSTONES)
        config.CAMS.clear()   # the same dict object: importers of config.CAMS see the new order
        config.CAMS.update({c["id"]: c for c in final})


async def credentials_changed(cid):
    """A camera's secret changed: stop its play and live and drop its cached session, so the next use logs in with
    the new secret (through the gate). The gate itself is told separately (gate.credentials_changed)."""
    async with _cfg_lock:
        if cid not in config.CAMS:
            raise KeyError(cid)
        await _stop_media(config.CAMS[cid])
        async with _login_locked([cid]):
            session.drop_client(cid)


async def remove(cid):
    """Remove a camera: quiesce it, delete its locks and slots entries, keep a tombstone in cameras.json."""
    async with _cfg_lock:
        if cid not in config.CAMS:
            raise KeyError(cid)
        cam = config.CAMS[cid]
        _refuse_if_exporting([cam])
        await _stop_media(cam)
        if slots.busy(cid):   # a clip or export holds the camera: its lock must not be deleted under it
            raise Refused(f"{cam['name']}: a clip or export is using the camera; try again when it has finished")
        async with _login_locked([cid]):
            session.drop_client(cid)
            health.forget(cid)
            tomb = {"id": cid, "device_id": cam.get("device_id") or cid,   # legacy camera: its gate key was its id
                    "ip": cam["ip"], "removed_at": int(time.time())}
            tombs = [*config.TOMBSTONES, tomb]
            await asyncio.to_thread(_persist, [c for c in config.CAMS.values() if c["id"] != cid], tombs)
            config.TOMBSTONES[:] = tombs
            del config.CAMS[cid]
            if config.CAM_BY_IP.get(cam["ip"]) is cam:
                del config.CAM_BY_IP[cam["ip"]]
        session.unregister_camera(cid)
        slots.unregister_camera(cid)
