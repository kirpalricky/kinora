"""One budgeted login to ONE camera (shares the server's login gate and budget).

Usage: .venv/bin/python tools/probe.py c201
Never loops over auth methods: the method comes from cameras.json.
"""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402

cam = server.CAMS[sys.argv[1]]
t = server.client(cam)  # goes through gate(): back-off and login budget apply
info = t.getBasicInfo()["device_info"]["basic_info"]
print(cam["name"], info.get("device_model"), info.get("sw_version"), "transport:", t.transport.method)
rec = t.getRecordings(datetime.date.today().strftime("%Y%m%d"))
print("recordings today:", len(rec))
