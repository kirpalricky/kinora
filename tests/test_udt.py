"""Loopback self-test of udt.py: rendezvous, 2 MB each way, with simulated packet loss."""
import asyncio, os, socket, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kinora import udt  # noqa: E402


async def main(loss):
    a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); a.bind(("127.0.0.1", 0))
    b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); b.bind(("127.0.0.1", 0))
    A = udt.UdtStream(a, b.getsockname(), loss_in=loss, loss_out=loss)
    B = udt.UdtStream(b, a.getsockname(), loss_in=loss, loss_out=loss)
    t0 = time.time()
    await asyncio.gather(A.connect(5), B.connect(5))
    print(f"loss={loss:.0%} connected in {time.time()-t0:.2f}s, mss {A.mss}")
    da, db = os.urandom(2_000_000), os.urandom(2_000_000)
    A.write(da); B.write(db)
    t0 = time.time()
    ra = await asyncio.wait_for(B.reader.readexactly(len(da)), 60)
    rb = await asyncio.wait_for(A.reader.readexactly(len(db)), 60)
    ok = ra == da and rb == db
    print(f"  transfer ok={ok} in {time.time()-t0:.2f}s  A={A.stats}  B={B.stats}")
    A.close(); B.close()
    await asyncio.sleep(0.1)
    return ok

ok = all([asyncio.run(main(l)) for l in (0.0, 0.05)])
print("PASS" if ok else "FAIL"); sys.exit(0 if ok else 1)
