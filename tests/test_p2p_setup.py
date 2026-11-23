"""Offline test of the playback setup order: the day to play must be the LAST one listed, every time (no stale per-login cache)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kinora import p2p, p2p_media  # noqa: E402

calls = []
class FakeTapo:
    def getRecordings(self, day): calls.append(("list", day))
    def getUserID(self): calls.append(("uid",)); return 7
p2p.request_session = lambda tt, ip: (calls.append(("request_p2p",)) or ("SOCK", "SID", ("1.2.3.4", 5)))
cam = {"ip": "1.2.3.4"}
ok = True
def check(name, cond):
    global ok; print(("PASS " if cond else "FAIL ") + name); ok &= bool(cond)

# 11:00 on 2026-11-22 .. 11:01 (same day)
import time, datetime
t0 = int(datetime.datetime(2026, 11, 22, 11, 0).timestamp())
check("one day -> [that day]", p2p_media.day_list(t0, t0 + 60) == ["20261122"])
# a window crossing midnight: the START day must be listed last
t1 = int(datetime.datetime(2026, 11, 22, 23, 59).timestamp())
check("crossing midnight lists the end day first, the start day last", p2p_media.day_list(t1, t1 + 120) == ["20261123", "20261122"])
check("start only -> no extra next day", p2p_media.day_list(t1) == ["20261122"])
for run in (1, 2):                      # second run with the same day must list again
    calls.clear()
    p2p_media.prepare_session(FakeTapo(), cam, p2p_media.day_list(t0))
    check(f"run {run}: listing happens before request_p2p, once, for the right day", calls == [("list", "20261122"), ("uid",), ("request_p2p",)])
print("ALL PASS" if ok else "SOME FAILED"); sys.exit(0 if ok else 1)
