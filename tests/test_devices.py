"""Other devices (kinora/devices.py, kinora/services/devices_api.py, the device check in server.local_only).
Offline: a temp data folder, no camera, no real network socket (the network site is a fake; loopback
requests go through TestClient as in test_setup_api).
Run: .venv/bin/python -m unittest tests.test_devices   (from the repo root)
"""
import asyncio
import atexit
import ipaddress
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_ENV_BEFORE = {k: os.environ.get(k) for k in ("KINORA_HOME", "KINORA_NO_INSTANCE_LOCK")}
_home = tempfile.mkdtemp(prefix="kinora-devices-test-")   # set before kinora is imported: config reads it once
atexit.register(shutil.rmtree, _home, True)
os.environ["KINORA_HOME"] = _home
os.environ["KINORA_NO_INSTANCE_LOCK"] = "1"

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request  # noqa: E402

import server  # noqa: E402
from kinora import config, devices  # noqa: E402
from kinora.errors import BadInput  # noqa: E402
from kinora.services import devices_api  # noqa: E402

for _k, _v in _ENV_BEFORE.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v

HOST = f"127.0.0.1:{config.PORT}"
ORIGIN = f"http://{HOST}"
LAN = "192.168.1.5"


class FakeSite:
    """Stands in for web.TCPSite: records the address it was asked to listen on. No socket is opened."""
    started = []
    stopped = []
    fail_on = set()

    def __init__(self, runner, host, port):
        self.host, self.port = host, port

    async def start(self):
        if self.host in FakeSite.fail_on:
            raise OSError(98, "Address already in use")
        FakeSite.started.append(self.host)

    async def stop(self):
        FakeSite.stopped.append(self.host)


def remote(ip, method="GET", path="/api/cameras", host=None, headers=None):
    """A request from another computer, as local_only sees it."""
    h = {"Host": host or f"{LAN}:{config.PORT}", **(headers or {})}
    transport = mock.Mock(get_extra_info=lambda k, d=None: (ip, 1234) if k == "peername" else d)
    return make_mocked_request(method, path, headers=h, transport=transport)


async def ok(request):
    return web.Response(text="ok")


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.path = config.HOME / "devices.json"
        if self.path.exists():
            self.path.unlink()
        devices._pending.clear()
        patches = [mock.patch.object(config, "ALLOW", [])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()


class Store(StoreCase):
    def test_round_trip(self):
        self.assertEqual(devices.load(), {"version": 1, "sharing": False, "devices": []})
        devices.add("192.168.1.20", "  Anna's laptop  ")
        devices.set_sharing(True)
        data = json.loads(self.path.read_text())
        self.assertEqual(data, {"version": 1, "sharing": True, "devices": [{"ip": "192.168.1.20", "name": "Anna's laptop"}]})
        self.assertTrue(devices.remove("192.168.1.20"))
        self.assertFalse(devices.remove("192.168.1.20"))
        self.assertEqual(devices.load()["devices"], [])

    def test_corrupt_or_odd_file_gives_defaults(self):
        for text in ("{not json", "[1, 2]", '"text"'):
            self.path.write_text(text)
            self.assertEqual(devices.load(), {"version": 1, "sharing": False, "devices": []})
            self.assertFalse(devices.is_allowed("192.168.1.20"))
        self.path.write_text(json.dumps({"sharing": "yes", "devices": [{"ip": "127.0.0.1"}, "x", {"ip": "192.168.1.9", "name": 4}, {"ip": "192.168.1.9"}]}))
        data = devices.load()
        self.assertFalse(data["sharing"])   # only the literal true turns sharing on
        self.assertEqual(data["devices"], [{"ip": "192.168.1.9", "name": ""}])

    def test_validation(self):
        for bad in ("127.0.0.1", "0.0.0.0", "224.0.0.1", "::1", "not-an-ip", "", 5, None, "<script>"):
            with self.assertRaises(BadInput) as cm:
                devices.add(bad)
            self.assertNotIn("<script>", str(cm.exception))
        with self.assertRaises(BadInput):
            devices.add("192.168.1.20", "x" * 61)
        with self.assertRaises(BadInput):
            devices.add("192.168.1.20", "bad\x07name")
        with self.assertRaises(BadInput):
            devices.add("192.168.1.20", "tab\tinside")
        self.assertEqual(devices.add("::ffff:192.168.1.21"), "192.168.1.21")
        with self.assertRaises(BadInput):
            devices.add("192.168.1.21")   # duplicate, after normalising
        self.assertEqual(devices.add("192.168.1.22", None), "192.168.1.22")
        self.assertEqual(devices.load()["devices"][1]["name"], "")

    def test_at_most_twenty(self):
        for n in range(devices.MAX_DEVICES):
            devices.add(f"192.168.1.{100 + n}")
        with self.assertRaises(BadInput):
            devices.add("192.168.1.200")

    def test_is_allowed(self):
        self.assertFalse(devices.is_allowed(None))
        self.assertFalse(devices.is_allowed("no-address"))
        devices.add("192.168.1.20")
        self.assertTrue(devices.is_allowed("192.168.1.20"))
        self.assertTrue(devices.is_allowed(ipaddress.ip_address("::ffff:192.168.1.20")))
        self.assertFalse(devices.is_allowed("192.168.1.21"))
        devices.remove("192.168.1.20")
        self.assertFalse(devices.is_allowed("192.168.1.20"))
        with mock.patch.object(config, "ALLOW", config.parse_allow("10.0.0.0/8")):
            self.assertTrue(devices.is_allowed("10.1.2.3"))
            self.assertFalse(devices.is_allowed("192.168.1.20"))


class Pending(StoreCase):
    def test_cap_expiry_and_dismiss(self):
        t = 1_000_000.0
        with mock.patch.object(devices.time, "time", return_value=t):
            devices.note_denied("192.168.1.2")
        with mock.patch.object(devices.time, "time", return_value=t + 10):
            for n in range(devices.PENDING_MAX + 4):
                devices.note_denied(f"192.168.1.{10 + n}")
        with mock.patch.object(devices.time, "time", return_value=t + 10):
            items = devices.pending()
        self.assertEqual(len(items), devices.PENDING_MAX)
        self.assertNotIn("192.168.1.2", [i["ip"] for i in items])   # the oldest one went
        self.assertEqual(set(items[0]), {"ip", "last_seen"})
        self.assertIsInstance(items[0]["last_seen"], int)
        devices.dismiss("192.168.1.13")
        self.assertNotIn("192.168.1.13", [i["ip"] for i in devices.pending()])
        with mock.patch.object(devices.time, "time", return_value=t + 10 + devices.PENDING_TTL_S + 1):
            self.assertEqual(devices.pending(), [])
        with self.assertRaises(BadInput):
            devices.dismiss("0.0.0.0")

    def test_allowing_removes_from_pending(self):
        devices.note_denied("192.168.1.30")
        self.assertIn("192.168.1.30", [i["ip"] for i in devices.pending()])
        devices.add("192.168.1.30")
        self.assertNotIn("192.168.1.30", [i["ip"] for i in devices.pending()])


class Middleware(StoreCase):
    def call(self, ip, path="/api/cameras", method="GET", host=None, headers=None):
        try:
            return asyncio.run(server.local_only(remote(ip, method, path, host, headers), ok)).status
        except web.HTTPException as e:
            return e.status, e.text

    def test_unlisted_device_refused_and_waiting(self):
        status, text = self.call("192.168.1.40")
        self.assertEqual(status, 403)
        self.assertIn("this device (192.168.1.40) is not allowed yet", text)
        self.assertIn("Settings > Other devices", text)
        self.assertIn("192.168.1.40", [i["ip"] for i in devices.pending()])

    def test_allow_then_remove(self):
        self.assertEqual(self.call("192.168.1.41")[0], 403)
        devices.add("192.168.1.41", "Tablet")
        self.assertEqual(self.call("192.168.1.41"), 200)
        devices.remove("192.168.1.41")
        self.assertEqual(self.call("192.168.1.41")[0], 403)

    def test_command_line_allow_still_works(self):
        with mock.patch.object(config, "ALLOW", config.parse_allow("192.168.1.0/24")):
            self.assertEqual(self.call("192.168.1.42"), 200)

    def test_remote_cannot_reach_device_routes(self):
        devices.add("192.168.1.43")
        for method, path in (("GET", "/api/setup/devices"), ("POST", "/api/setup/devices"), ("DELETE", "/api/setup/devices/192.168.1.43"),
                             ("PUT", "/api/setup/sharing"), ("POST", "/api/setup/devices/dismiss")):
            self.assertEqual(self.call("192.168.1.43", path, method, headers={"Origin": f"http://{LAN}:{config.PORT}", "Content-Type": "application/json"})[0], 403, path)

    def test_bad_host_from_allowed_device_refused(self):
        devices.add("192.168.1.44")
        self.assertEqual(self.call("192.168.1.44", host="evil.example.com:%d" % config.PORT)[0], 403)


class Api(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = config.HOME / "devices.json"
        if self.path.exists():
            self.path.unlink()
        devices._pending.clear()
        FakeSite.started.clear()
        FakeSite.stopped.clear()
        FakeSite.fail_on.clear()
        for p in (mock.patch.object(config, "ALLOW", []), mock.patch.object(config, "HOST", "127.0.0.1"),
                  mock.patch.object(devices_api.web, "TCPSite", FakeSite), mock.patch.object(devices_api, "lan_address", return_value=(LAN, None))):
            p.start()
            self.addCleanup(p.stop)
        self.app = server.make_app(background=False)
        self.app[devices_api.LISTEN]["runner"] = object()   # a runner stands in: the server is not started
        self.http = TestClient(TestServer(self.app))
        await self.http.start_server()
        self.addAsyncCleanup(self.http.close)

    async def call(self, method, path, body=None, origin=ORIGIN):
        headers = {"Host": HOST, "Origin": origin, "Content-Type": "application/json"}
        resp = await self.http.request(method, path, headers=headers, data=json.dumps(body) if body is not None else None)
        return resp.status, await resp.text()

    async def view(self, method, path, body=None):
        status, text = await self.call(method, path, body)
        self.assertEqual(status, 200, text)
        return json.loads(text)

    async def test_view_shape(self):
        v = await self.view("GET", "/api/setup/devices")
        self.assertEqual(set(v), {"sharing", "listening", "address", "address_error", "from_command_line", "devices", "pending", "command_line"})
        self.assertFalse(v["sharing"])
        self.assertIsNone(v["address"])
        self.assertFalse(v["from_command_line"])

    async def test_add_remove_dismiss(self):
        devices.note_denied("192.168.1.50")
        v = await self.view("POST", "/api/setup/devices", {"ip": "192.168.1.50", "name": "Anna's laptop"})
        self.assertEqual(v["devices"], [{"ip": "192.168.1.50", "name": "Anna's laptop"}])
        self.assertEqual(v["pending"], [])
        status, text = await self.call("POST", "/api/setup/devices", {"ip": "192.168.1.50"})
        self.assertEqual(status, 400, text)
        status, text = await self.call("POST", "/api/setup/devices", {"ip": "192.168.1.51", "extra": 1})
        self.assertEqual(status, 400)
        v = await self.view("DELETE", "/api/setup/devices/192.168.1.50")
        self.assertEqual(v["devices"], [])
        status, _ = await self.call("DELETE", "/api/setup/devices/192.168.1.50")
        self.assertEqual(status, 404)
        devices.note_denied("192.168.1.52")
        v = await self.view("POST", "/api/setup/devices/dismiss", {"ip": "192.168.1.52"})
        self.assertEqual(v["pending"], [])
        status, _ = await self.call("POST", "/api/setup/devices/dismiss", {"ip": "127.0.0.1"})
        self.assertEqual(status, 400)

    async def test_sharing_starts_and_stops_the_network_site(self):
        v = await self.view("PUT", "/api/setup/sharing", {"enabled": True})
        self.assertTrue(v["sharing"])
        self.assertEqual(FakeSite.started, [LAN])   # the computer's address, never 0.0.0.0
        self.assertEqual(v["address"], f"http://{LAN}:{config.PORT}")
        self.assertTrue(v["listening"])
        self.assertTrue(devices.load()["sharing"])
        v = await self.view("PUT", "/api/setup/sharing", {"enabled": False})
        self.assertFalse(v["sharing"])
        self.assertEqual(FakeSite.stopped, [LAN])
        self.assertIsNone(v["address"])
        self.assertFalse(devices.load()["sharing"])

    async def test_sharing_failure_is_409_and_not_saved(self):
        FakeSite.fail_on.add(LAN)
        status, text = await self.call("PUT", "/api/setup/sharing", {"enabled": True})
        self.assertEqual(status, 409)
        self.assertIn("could not start listening", text)
        self.assertFalse(devices.load()["sharing"])

    async def test_no_network_connection(self):
        with mock.patch.object(devices_api, "lan_address", return_value=(None, "No network connection found")):
            status, text = await self.call("PUT", "/api/setup/sharing", {"enabled": True})
        self.assertEqual(status, 409)
        self.assertEqual(text, "No network connection found")

    async def test_sharing_body_checked(self):
        status, _ = await self.call("PUT", "/api/setup/sharing", {"enabled": "yes"})
        self.assertEqual(status, 400)
        status, _ = await self.call("PUT", "/api/setup/sharing", {})
        self.assertEqual(status, 400)

    async def test_command_line_switch_is_read_only(self):
        with mock.patch.object(config, "ALLOW", config.parse_allow("192.168.1.0/24")):
            v = await self.view("GET", "/api/setup/devices")
            self.assertTrue(v["from_command_line"])
            self.assertTrue(v["sharing"])
            self.assertEqual(v["command_line"], ["192.168.1.0/24"])
            status, _ = await self.call("PUT", "/api/setup/sharing", {"enabled": False})
            self.assertEqual(status, 409)

    async def test_startup_listens_on_loopback_and_network(self):
        devices.set_sharing(True)
        runner = web.AppRunner(self.app)
        self.app[devices_api.LISTEN]["runner"] = runner
        await devices_api.listen(self.app)
        self.assertEqual(FakeSite.started, ["127.0.0.1", LAN])
        self.assertIsNotNone(self.app[devices_api.LISTEN]["lan_site"])

    async def test_startup_without_sharing_is_loopback_only(self):
        runner = web.AppRunner(self.app)
        self.app[devices_api.LISTEN]["runner"] = runner
        await devices_api.listen(self.app)
        self.assertEqual(FakeSite.started, ["127.0.0.1"])
        self.assertIsNone(self.app[devices_api.LISTEN]["lan_site"])

    async def test_command_line_host_is_the_one_site(self):
        runner = web.AppRunner(self.app)
        self.app[devices_api.LISTEN]["runner"] = runner
        with mock.patch.object(config, "HOST", "0.0.0.0"), mock.patch.object(config, "ALLOW", config.parse_allow("192.168.1.0/24")):
            await devices_api.listen(self.app)
        self.assertEqual(FakeSite.started, ["0.0.0.0"])

    async def test_remote_peer_cannot_call_api(self):
        with self.assertRaises(web.HTTPForbidden):
            await server.local_only(remote("192.168.1.60", "GET", "/api/setup/devices"), devices_api.get_devices)


if __name__ == "__main__":
    unittest.main()
