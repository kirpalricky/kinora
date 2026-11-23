"""Cheap camera health checks: ICMP ping plus plain TCP connects. No login, no request, no video slot.

Why both: a rebooting camera answers ping within seconds while its services (API 443, RTSP 554, media 8800)
stay closed for another ~30 s. Ping alone would say "up" while every login would fail.

States (per camera):
  down     no ping reply
  booting  ping works but the API port (443) is closed
  ready    API port open
Also reports whether RTSP (554) and the media port (8800) accept connections.
"""
import asyncio
import sys
import time

PORTS = {"api": 443, "rtsp": 554, "media": 8800}
INTERVAL_S = 10
CONNECT_TIMEOUT_S = 1.5

_state = {}   # camera id -> dict
_misses = {}
_gen = {}     # camera id -> generation, bumped by forget()


def ping_argv(ip):
    """One ICMP echo with a 1 s wait. The -W unit differs: ms on macOS and Windows (-w), s on Linux."""
    if sys.platform == "win32":
        return ["ping", "-n", "1", "-w", "1000", ip]
    if sys.platform == "darwin":
        return ["ping", "-c", "1", "-W", "1000", ip]
    return ["ping", "-c", "1", "-W", "1", ip]


async def _ping(ip):
    try:
        p = await asyncio.create_subprocess_exec(
            *ping_argv(ip), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    except OSError:   # no ping binary on this system: report "no reply" rather than crash the health loop
        return False
    try:
        return (await asyncio.wait_for(p.wait(), 3)) == 0
    except asyncio.TimeoutError:
        p.kill()
        return False


async def _tcp(ip, port):
    """Connect and close at once: no bytes are sent, so the camera sees no login attempt."""
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), CONNECT_TIMEOUT_S)
    except Exception:
        return False
    w.close()
    try:
        await w.wait_closed()
    except Exception:
        pass
    return True


async def check(cam, gen=None):
    """One check of `cam`. `gen` is the camera's generation when the check was scheduled (see forget)."""
    cid = cam["id"]
    gen = _gen.get(cid, 0) if gen is None else gen
    ip = cam["ip"]
    ping = await _ping(ip)
    ports = dict(zip(PORTS, await asyncio.gather(*(_tcp(ip, p) for p in PORTS.values())))) if ping \
        else {k: False for k in PORTS}
    if _gen.get(cid, 0) != gen:   # forgotten (removed, or its IP changed) while we were probing: drop the result
        return
    raw = "ready" if ports["api"] else ("booting" if ping else "down")
    prev = _state.get(cam["id"], {})
    # A single lost ping or refused connect must not look like a reboot: leave "ready" only after 2 misses in a row.
    misses = 0 if raw == "ready" else _misses.get(cam["id"], 0) + 1
    _misses[cam["id"]] = misses
    state = raw if (raw == "ready" or misses >= 2 or prev.get("state") not in (None, "ready")) else "ready"
    since = prev.get("since", time.time()) if prev.get("state") == state else time.time()
    _state[cam["id"]] = {"state": state, "ping": ping, **ports, "since": since, "checked": time.time()}


async def loop(cams):
    """Checks every camera of the live table each round (cameras may be added or removed between rounds)."""
    while True:
        await asyncio.gather(*(check(c, _gen.get(c["id"], 0)) for c in cams.values()), return_exceptions=True)
        await asyncio.sleep(INTERVAL_S)


def forget(cid):
    """Clear a camera's health state (it was removed, or its address changed). Checks already running for it are dropped."""
    _state.pop(cid, None)
    _misses.pop(cid, None)
    _gen[cid] = _gen.get(cid, 0) + 1


def get(cid):
    return _state.get(cid)


def unreachable(cid):
    """True only when a fresh, debounced check says the camera is not ready (never blocks on missing data)."""
    s = _state.get(cid)
    return bool(s) and time.time() - s["checked"] < 3 * INTERVAL_S and s["state"] != "ready"
