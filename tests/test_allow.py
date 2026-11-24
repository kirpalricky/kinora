"""Device allowlist (--allow / KINORA_ALLOW): who may reach the server from another computer. Offline, no sockets.
Run: .venv/bin/python -m unittest tests.test_allow   (from the repo root)"""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["KINORA_NO_INSTANCE_LOCK"] = "1"

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402

import server  # noqa: E402
from kinora import config  # noqa: E402


async def ok(request):
    return web.Response(text="ok")


def call(remote, path="/api/cameras", method="GET", host=None, headers=None, allow="192.168.1.0/24"):
    h = {"Host": host or f"localhost:{config.PORT}", **(headers or {})}
    transport = mock.Mock(get_extra_info=lambda k, d=None: (remote, 1234) if k == "peername" else d)
    req = make_mocked_request(method, path, headers=h, transport=transport)
    with mock.patch.object(config, "ALLOW", config.parse_allow(allow)):
        try:
            return asyncio.run(server.local_only(req, ok)).status
        except web.HTTPException as e:
            return e.status


class Allow(unittest.TestCase):
    P = config.PORT

    def test_parse(self):
        self.assertEqual([str(n) for n in config.parse_allow("10.0.0.5, 192.168.1.0/24")], ["10.0.0.5/32", "192.168.1.0/24"])
        with self.assertRaises(ValueError):
            config.parse_allow("not-an-ip")

    def test_loopback_always_ok(self):
        self.assertEqual(call("127.0.0.1", allow=""), 200)

    def test_unlisted_device_refused(self):
        self.assertEqual(call("192.168.2.9", host=f"192.168.1.2:{self.P}"), 403)
        self.assertEqual(call("192.168.1.9", host=f"192.168.1.2:{self.P}", allow=""), 403)

    def test_listed_device_ok(self):
        self.assertEqual(call("192.168.1.9", host=f"192.168.1.2:{self.P}"), 200)

    def test_listed_device_cannot_change_settings(self):
        h = f"192.168.1.2:{self.P}"
        self.assertEqual(call("192.168.1.9", "/api/setup/state", host=h), 403)
        self.assertEqual(call("192.168.1.9", "/api/cameras/x", "PATCH", host=h), 403)
        self.assertEqual(call("127.0.0.1", "/api/setup/state"), 200)

    def test_rebinding_name_refused(self):
        self.assertEqual(call("192.168.1.9", host=f"evil.example.com:{self.P}"), 403)
        self.assertEqual(call("192.168.1.9", host="192.168.1.2:1"), 403)

    def test_origin_must_match_host(self):
        h = f"192.168.1.2:{self.P}"
        ct = {"Content-Type": "application/json"}
        self.assertEqual(call("192.168.1.9", "/api/ptz", "POST", host=h, headers={**ct, "Origin": f"http://{h}"}), 200)
        self.assertEqual(call("192.168.1.9", "/api/ptz", "POST", host=h, headers={**ct, "Origin": "http://evil.com"}), 403)


if __name__ == "__main__":
    unittest.main()
