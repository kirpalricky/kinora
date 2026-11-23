"""Find cameras on a home subnet for the setup wizard.

Login-free by construction: only TCP connects that are closed at once (no bytes sent), so a camera sees no login
attempt and the lockout gate is never involved. A host is a candidate when it answers on 443 (the Tapo API) and on at
least one of the camera's other ports. Only RFC 1918 subnets of at most 1022 hosts are scanned. One scan at a time.
"""
import asyncio
import errno
import ipaddress
import socket
import time

from aiohttp import web

from kinora import config
from kinora.errors import BadInput
from kinora.services.setup import RFC1918, _body

API_PORT = 443
OTHER_PORTS = (554, 8800, 2020)     # RTSP, media/P2P, ONVIF: a router answers on 443 but rarely on these
TIMEOUT_S = 0.7
CONCURRENCY = 24                    # stays far below the default 256 open-file limit of macOS
MIN_PREFIX = 22                     # at most 1022 hosts
SCAN_GAP_S = 2.0                    # minimum seconds between two scans
_lock = asyncio.Lock()
_last = [-SCAN_GAP_S]


class OutOfFileHandles(Exception):
    """The process ran out of file descriptors (EMFILE/ENFILE): the scan result would be wrong, not empty."""


async def _open(ip, port):
    """Connect and close at once. True if something accepts."""
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), TIMEOUT_S)
    except OSError as e:
        if e.errno in (errno.EMFILE, errno.ENFILE):
            raise OutOfFileHandles() from None
        return False
    except Exception:
        return False
    w.close()
    try:
        await w.wait_closed()
    except Exception:
        pass
    return True


def _local_ip():
    """This computer's LAN address. A UDP connect sends no packet; it only picks the outgoing interface."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def default_subnet():
    """The /24 around this computer's LAN address, or '' if it has none (or it is not a home address)."""
    ip = _local_ip()
    if not ip:
        return ""
    try:
        if not any(ipaddress.IPv4Address(ip) in n for n in RFC1918):
            return ""
        return str(ipaddress.ip_network(f"{ip}/24", strict=False))
    except ValueError:
        return ""


def parse_subnet(value):
    """A private IPv4 network like 192.168.1.0/24 (host bits allowed), at most 1022 hosts."""
    if not isinstance(value, str) or not value.strip() or len(value) > 43:
        raise BadInput("subnet must look like 192.168.1.0/24")
    try:
        net = ipaddress.IPv4Network(value.strip(), strict=False)
    except ValueError:
        raise BadInput("subnet must look like 192.168.1.0/24") from None
    if not any(net.subnet_of(r) for r in RFC1918):
        raise BadInput("subnet must be a private network (10/8, 172.16/12 or 192.168/16)")
    if net.prefixlen < MIN_PREFIX:
        raise BadInput(f"subnet is too large: use /{MIN_PREFIX} or smaller")
    return net


async def scan(net, known=()):
    """Candidates in `net`, skipping `known` addresses. [{'ip', 'ports': {'443': True, ...}}], sorted by address."""
    hosts = [str(h) for h in net.hosts() if str(h) not in known]
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(ip):
        async with sem:
            if not await _open(ip, API_PORT):
                return None
            others = []
            for p in OTHER_PORTS:        # one at a time, stop at the first that answers
                ok = await _open(ip, p)
                others.append(ok)
                if ok:
                    break
        if not any(others):
            return None
        ports = {str(API_PORT): True, **{str(p): False for p in OTHER_PORTS}}
        ports.update({str(p): True for p, ok in zip(OTHER_PORTS, others) if ok})
        return {"ip": ip, "ports": ports}

    found = [r for r in await asyncio.gather(*(one(ip) for ip in hosts)) if r]
    return sorted(found, key=lambda r: ipaddress.IPv4Address(r["ip"])), len(hosts)


async def suggest(request):
    """GET: the subnet this computer is on (empty if unknown). Contacts nothing."""
    return web.json_response({"subnet": default_subnet()})


async def discover(request):
    """POST {subnet?}: scan it (default: this computer's /24). Addresses already saved as cameras are left out."""
    body = await _body(request, ("subnet",))
    raw = body.get("subnet")
    if raw in (None, ""):
        raw = default_subnet()
        if not raw:
            raise BadInput("Could not tell which network this computer is on: enter the subnet")
    net = parse_subnet(raw)
    if _lock.locked() or time.monotonic() - _last[0] < SCAN_GAP_S:
        raise web.HTTPTooManyRequests(text="a scan is running or just finished: wait a moment")
    async with _lock:
        _last[0] = time.monotonic()
        try:
            found, scanned = await scan(net, set(config.CAM_BY_IP))
        except OutOfFileHandles:
            _last[0] = time.monotonic()
            raise web.HTTPServiceUnavailable(text="The scan ran out of open file handles on this computer, so it "
                                                  "could not finish. Close other programs and try again.")
        _last[0] = time.monotonic()
    return web.json_response({"subnet": str(net), "scanned": scanned, "found": found})
