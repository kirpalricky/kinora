"""Tapo P2P (UDT) transport for cameras that serve SD-card playback only over the app's P2P channel (C100 v2 fw 1.3.17).

Flow, reverse engineered from the Tapo app 3.22.105 and verified on Baby Room (see docs/pytapo-notes.md):
  1. over the normal LOCAL API: {"method":"do","p2p":{"request_p2p":{...}}} -> sid + camera LAN address/port (stun_url must be non-empty)
  2. UDP: send the sid string from the advertised socket to the camera; it answers '2'+sid[1:]+'\\0'; echo that back
  3. UDT rendezvous on the same socket (udt.py), then the usual `POST /stream` multipart protocol runs inside it
bridge(): a one-connection loopback TCP server that relays bytes to/from the UDT stream, so pytapo's HttpMediaSession can be pointed at
127.0.0.1:<port> unchanged.
"""
import asyncio
import os
import random
import socket
import time

from kinora import udt

STUN_URL = "stun.tplinkcloud.com"


def _nonce():
    r = random.SystemRandom()
    return "".join(chr(97 + r.randrange(26)) if i % 2 == 0 else str(r.randrange(10)) for i in range(16))


def _local_ip(peer_ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer_ip, 9))
        return s.getsockname()[0]
    finally:
        s.close()


def request_session(tapo, ip):
    """Blocking. Returns (udp_socket, sid, (cam_ip, cam_port)). One local API request (no login here)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    sock.bind(("0.0.0.0", 0))
    me, port = _local_ip(ip), sock.getsockname()[1]
    body = {"version": "1.1", "app_imp_version": 4, "pub_ip": me, "pub_port": port, "pri_ip": me, "pri_port": port,
            "nat_type": 0, "app_nonce": _nonce(), "punch_version": 0, "punch_timeout": 30, "punch_maxfds": 128,
            "cone2symm_interval": 20, "symm2cone_interval": 80, "stun_url": STUN_URL}
    r = tapo.performRequest({"method": "do", "p2p": {"request_p2p": body}})
    res = (r or {}).get("result") or {}
    if not res.get("sid") or res.get("is_ready") != 1:
        sock.close()
        raise RuntimeError("request_p2p gave no ready session: %s" % {k: v for k, v in res.items() if k != "dev_nonce"})
    return sock, res["sid"], (res["pri_ip"], int(res["pri_port"]))


def punch(sock, sid, cam, timeout=8.0):
    """Blocking. Send the sid until the camera answers with its '2' form; echo it. Returns the camera address."""
    if hasattr(socket, "SIO_UDP_CONNRESET"):   # Windows: an ICMP port-unreachable must not surface as ConnectionResetError
        sock.ioctl(socket.SIO_UDP_CONNRESET, False)
    sock.settimeout(0.3)
    t0 = time.time()
    while time.time() - t0 < timeout:
        sock.sendto(sid.encode(), cam)
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except ConnectionResetError:   # ICMP unreachable from an earlier send (Windows): keep punching
            time.sleep(0.05)
            continue
        if addr[0] != cam[0]:   # only the camera answers the probe: a spoofed reply from another host is ignored
            continue
        if data.rstrip(b"\0") == b"2" + sid.encode()[1:]:
            sock.sendto(data.rstrip(b"\0"), addr)
            sock.setblocking(False)
            return addr
    raise RuntimeError("camera did not answer the P2P probe")


async def connect(sock, sid, cam):
    """Punch + UDT rendezvous on an already bound socket. The socket is closed again if anything fails. Returns a UdtStream."""
    try:
        addr = await asyncio.to_thread(punch, sock, sid, cam)
        s = udt.UdtStream(sock, addr)
        await s.connect(10)
        return s
    except BaseException:
        sock.close()
        raise


async def open_stream(tapo, ip):
    """Scripts/tests: full setup from a pytapo client (no server locks). Returns a connected udt.UdtStream (the caller closes it)."""
    sock, sid, cam = await asyncio.to_thread(request_session, tapo, ip)
    return await connect(sock, sid, cam)


class Bridge:
    """Loopback TCP <-> UDT relay for exactly one connection (a second connection is refused)."""

    def __init__(self, stream):
        self.stream, self.server, self.port, self.tasks, self.writer = stream, None, None, [], None

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def _client(self, reader, writer):
        if self.writer is not None:
            writer.close()
            return
        self.writer = writer

        async def up():
            try:
                while True:
                    d = await reader.read(16384)
                    if not d:
                        break
                    while len(self.stream.snd_buf) > 2000:
                        if self.stream.state != "connected":
                            return
                        await asyncio.sleep(0.01)
                    self.stream.write(d)
            except udt.UdtError:
                return

        async def down():
            try:
                while True:
                    d = await self.stream.reader.read(65536)
                    if not d:
                        break
                    writer.write(d)
                    await writer.drain()
            except (ConnectionError, OSError):
                pass

        self.tasks = [asyncio.ensure_future(up()), asyncio.ensure_future(down())]
        try:
            await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in self.tasks:
                t.cancel()
            writer.close()

    async def close(self):
        """Idempotent. Order: stop the relay tasks, close the loopback connection and listener, then shut the UDT down."""
        for t in self.tasks:
            t.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.writer is not None:
            self.writer.close()
        if self.server is not None:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), 2)
            except Exception:
                pass
            self.server = None
        self.stream.close()
