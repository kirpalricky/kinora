"""Minimal UDT4 stream socket in rendezvous mode (what the Tapo app uses to talk to a camera over P2P).

Wire format verified against a phone capture of the Tapo app talking to a C100:
  control header: word0 = 0x80000000 | type<<16 | ext, word1 = additional info, word2 = timestamp, word3 = dest socket id
  handshake (type 0), 48-byte body: version=4, sockType=1 (stream), initSeq, MSS=1420, flowWin, reqType, socketId, cookie=0, peerIP (16 B)
  rendezvous: both sides send reqType 0 (dest id 0), then -1 (conclusion, dest = peer id), then -2 (agreement)
  data: word0 = seq (31 bit), word1 = message flags (the app sends 0), word2 = timestamp, word3 = dest id; payload <= MSS-44
  ACK (type 2, word1 = journal no) 24-byte body: next expected seq, rtt, rttvar, avail buf, recv rate, link capacity; ACK2 (type 6) echoes the journal no
  NAK (type 3): loss list (single seq, or start|0x80000000 followed by end); keepalive type 1; shutdown type 5

Not a full UDT: no congestion control or pacing (traffic is a few request packets out, a video stream in over a LAN).
Usage: s = UdtStream(bound_udp_socket, (peer_ip, peer_port)); await s.connect(); s.write(b"..."); data = await s.reader.read(n)
"""
import asyncio
import os
import socket
import struct
import time

SEQ_MASK = 0x7FFFFFFF
HALF = 0x40000000
MSS = 1420
PAYLOAD = MSS - 44
FLOW_WIN = 1024
T_HS, T_KEEPALIVE, T_ACK, T_NAK, T_SHUTDOWN, T_ACK2 = 0, 1, 2, 3, 5, 6


def seq_add(a, n=1):
    return (a + n) & SEQ_MASK


def seq_ahead(a, b):
    """True if a is after b (circular 31-bit)."""
    return a != b and ((a - b) & SEQ_MASK) < HALF


class UdtError(Exception):
    pass


class UdtStream(asyncio.DatagramProtocol):
    def __init__(self, sock, peer, loss_in=0.0, loss_out=0.0):
        self.sock, self.peer = sock, peer
        self.loss_in, self.loss_out = loss_in, loss_out     # test hooks only
        self.reader = asyncio.StreamReader(limit=1 << 22)
        self.my_id = int.from_bytes(os.urandom(4), "big") & 0x3FFFFFFF | 1
        self.my_seq = int.from_bytes(os.urandom(4), "big") & 0x3FFFFFFF
        self.peer_id = 0
        self.peer_seq = None
        self.mss = MSS
        self.state = "init"                 # init -> connected -> closed
        self.t0 = time.monotonic()
        self.snd_next = self.my_seq
        self.snd_buf = {}                   # seq -> (packet, last send time)
        self.rcv_next = None
        self.rcv_buf = {}
        self.last_acked = None
        self.journal = 0
        self.last_rx = self.last_tx = time.monotonic()
        self.connected = asyncio.Event()
        self.transport = None
        self.tasks = []
        self.stats = {"data_rx": 0, "data_tx": 0, "retx": 0, "naks": 0, "dup": 0}

    # ---- plumbing -------------------------------------------------------------------------------------------
    def _ts(self):
        return int((time.monotonic() - self.t0) * 1e6) & 0xFFFFFFFF

    def _tx(self, pkt):
        self.last_tx = time.monotonic()
        if self.loss_out and os.urandom(1)[0] / 255 < self.loss_out and pkt[0] & 0x80 == 0:
            return
        self.transport.sendto(pkt, self.peer)

    def _ctrl(self, typ, info=0, body=b"", ext=0):
        self._tx(struct.pack(">IIII", 0x80000000 | typ << 16 | ext, info, self._ts(), self.peer_id) + body)

    def _hs(self, req):
        ip = socket.inet_aton(self.peer[0]) + b"\0" * 12
        body = struct.pack(">IIIIIIII", 4, 1, self.my_seq, MSS, FLOW_WIN, req & 0xFFFFFFFF, self.my_id, 0) + ip
        self._tx(struct.pack(">IIII", 0x80000000, 0, 0, self.peer_id if req != 0 else 0) + body)

    def connection_made(self, transport):
        self.transport = transport

    def error_received(self, exc):
        pass

    def datagram_received(self, data, addr):
        if addr[0] != self.peer[0] or len(data) < 16:
            return
        if self.loss_in and os.urandom(1)[0] / 255 < self.loss_in and data[0] & 0x80 == 0:
            return
        self.last_rx = time.monotonic()
        w0, w1, _, dst = struct.unpack(">IIII", data[:16])
        if w0 & 0x80000000:
            self._on_ctrl((w0 >> 16) & 0x7FFF, w1, data[16:], dst)
        elif self.state == "connected" and dst == self.my_id:
            self._on_data(w0, data[16:])

    # ---- handshake ------------------------------------------------------------------------------------------
    async def connect(self, timeout=10.0):
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: self, sock=self.sock)
        t_end = time.monotonic() + timeout
        while not self.connected.is_set():
            if time.monotonic() > t_end:
                self.close()
                raise UdtError("UDT rendezvous timed out")
            self._hs(0 if self.peer_seq is None else -1)
            try:
                await asyncio.wait_for(self.connected.wait(), 0.25)
            except asyncio.TimeoutError:
                pass
        self.tasks = [loop.create_task(self._timers())]

    def _on_ctrl(self, typ, info, body, dst):
        if typ == T_HS and len(body) >= 32:
            ver, stype, iseq, mss, fw, req, sid, cookie = struct.unpack(">IIIIIIII", body[:32])
            req = req - (1 << 32) if req >= 1 << 31 else req
            if self.state == "closed":
                return
            if self.peer_seq is None or self.state == "init":
                self.peer_id, self.peer_seq, self.rcv_next = sid, iseq & SEQ_MASK, iseq & SEQ_MASK
                self.last_acked = self.rcv_next
                self.mss = min(MSS, mss) if mss else MSS
            if req == 0:
                self._hs(-1)
            elif req == -1:
                self._hs(-2)
                self._set_connected()
            elif req == -2:
                self._set_connected()
        elif self.state != "connected":
            return
        elif typ == T_ACK:
            ackseq = struct.unpack(">I", body[:4])[0] & SEQ_MASK if len(body) >= 4 else None
            self._ctrl(T_ACK2, info)
            if ackseq is not None:
                for s in [s for s in self.snd_buf if not seq_ahead(s, seq_add(ackseq, -1)) ]:
                    self.snd_buf.pop(s, None)
        elif typ == T_NAK:
            vals = struct.unpack(">%dI" % (len(body) // 4), body[: len(body) // 4 * 4])
            i = 0
            while i < len(vals):
                if vals[i] & 0x80000000 and i + 1 < len(vals):
                    a, b = vals[i] & SEQ_MASK, vals[i + 1] & SEQ_MASK
                    i += 2
                    n = (b - a) & SEQ_MASK
                    for k in range(min(n + 1, 4096)):
                        self._resend(seq_add(a, k))
                else:
                    self._resend(vals[i] & SEQ_MASK)
                    i += 1
        elif typ == T_SHUTDOWN:
            self._finish()
        # keepalive / ACK2: nothing to do

    def _set_connected(self):
        if self.state == "init":
            self.state = "connected"
            self.connected.set()

    # ---- data -----------------------------------------------------------------------------------------------
    def write(self, data: bytes):
        if self.state != "connected":
            raise UdtError("not connected")
        for off in range(0, len(data), PAYLOAD):
            seq = self.snd_next
            self.snd_next = seq_add(seq)
            pkt = struct.pack(">IIII", seq, 0, self._ts(), self.peer_id) + data[off:off + PAYLOAD]
            self.snd_buf[seq] = [pkt, time.monotonic()]
            self.stats["data_tx"] += 1
            self._tx(pkt)

    def _resend(self, seq):
        e = self.snd_buf.get(seq)
        if e:
            e[1] = time.monotonic()
            self.stats["retx"] += 1
            self._tx(e[0])

    def _on_data(self, seq, payload):
        self.stats["data_rx"] += 1
        if seq == self.rcv_next:
            self.reader.feed_data(payload)
            self.rcv_next = seq_add(seq)
            while self.rcv_next in self.rcv_buf:
                self.reader.feed_data(self.rcv_buf.pop(self.rcv_next))
                self.rcv_next = seq_add(self.rcv_next)
        elif seq_ahead(seq, self.rcv_next):
            if seq not in self.rcv_buf:
                if len(self.rcv_buf) > 20000:
                    return
                first_gap = not self.rcv_buf
                self.rcv_buf[seq] = payload
                if first_gap or seq_ahead(seq, max(self.rcv_buf, key=lambda s: (s - self.rcv_next) & SEQ_MASK)):
                    self._nak()
        else:
            self.stats["dup"] += 1

    def _missing(self, limit=256):
        out, s = [], self.rcv_next
        top = max(self.rcv_buf, key=lambda x: (x - self.rcv_next) & SEQ_MASK) if self.rcv_buf else None
        while top is not None and s != top and len(out) < limit:
            if s not in self.rcv_buf:
                out.append(s)
            s = seq_add(s)
        return out

    def _nak(self):
        miss = self._missing()
        if not miss:
            return
        self.stats["naks"] += 1
        words, i = [], 0
        while i < len(miss):
            j = i
            while j + 1 < len(miss) and miss[j + 1] == seq_add(miss[j]):
                j += 1
            if j > i:
                words += [miss[i] | 0x80000000, miss[j]]
            else:
                words.append(miss[i])
            i = j + 1
        self._ctrl(T_NAK, 0, struct.pack(">%dI" % len(words), *words[:300]))

    def _ack(self):
        self.journal = (self.journal + 1) & 0x7FFFFFFF
        self.last_acked = self.rcv_next
        self._ctrl(T_ACK, self.journal, struct.pack(">IIIIII", self.rcv_next, 10000, 5000, FLOW_WIN * 2048, 0, 0))

    async def _timers(self):
        n = 0
        while self.state == "connected":
            await asyncio.sleep(0.01)
            n += 1
            now = time.monotonic()
            if self.rcv_next is not None and self.rcv_next != self.last_acked:
                self._ack()
            if n % 10 == 0 and self.rcv_buf:
                self._nak()
            if n % 50 == 0:
                for s, (pkt, t) in list(self.snd_buf.items())[:16]:
                    if now - t > 1.0:
                        self._resend(s)
            if now - self.last_tx > 1.0:
                self._ctrl(T_KEEPALIVE)
            if now - self.last_rx > 15.0:
                self._finish()

    # ---- close ----------------------------------------------------------------------------------------------
    def _finish(self):
        if self.state != "closed":
            self.state = "closed"
            self.connected.set()
            self.reader.feed_eof()
            for t in self.tasks:
                t.cancel()
            if self.transport:
                self.transport.close()

    def close(self):
        if self.state == "connected":
            for _ in range(2):
                self._ctrl(T_SHUTDOWN)
        self._finish()
