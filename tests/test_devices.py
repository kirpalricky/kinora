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
import socket
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

import aiohttp  # noqa: E402
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


_APP = server.make_app(background=False)


async def remote(ip, method="GET", path="/api/cameras", host=None, headers=None):
    """A request from another computer, as local_only sees it: routed by the app, like a real request."""
    h = {"Host": host or f"{LAN}:{config.PORT}", **(headers or {})}
    transport = mock.Mock(get_extra_info=lambda k, d=None: (ip, 1234) if k == "peername" else d)
    req = make_mocked_request(method, path, headers=h, transport=transport, app=_APP)
    req._match_info = await _APP.router.resolve(req)
    return req


async def ok(request):
    return web.Response(text="ok")


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.path = config.HOME / "devices.json"
        if self.path.exists():
            self.path.unlink()
        devices._pending.clear()
        devices._cache["data"] = None
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
    def setUp(self):
        super().setUp()
        devices.set_sharing(True)   # sharing on: the refusals below are about the device, not the switch
    def call(self, ip, path="/api/cameras", method="GET", host=None, headers=None):
        async def go():
            req = await remote(ip, method, path, host, headers)
            try:
                resp = await server.local_only(req, ok)
            except web.HTTPException as e:
                resp = e
            return resp.status, resp.text
        return asyncio.run(go())

    def test_unlisted_device_refused_and_waiting(self):
        status, text = self.call("192.168.1.40")
        self.assertEqual(status, 403)
        self.assertIn("this device (192.168.1.40) is not allowed yet", text)
        self.assertIn("Settings > Other devices", text)
        self.assertIn("192.168.1.40", [i["ip"] for i in devices.pending()])

    def test_allow_then_remove(self):
        self.assertEqual(self.call("192.168.1.41")[0], 403)
        devices.add("192.168.1.41", "Tablet")
        self.assertEqual(self.call("192.168.1.41")[0], 200)
        devices.remove("192.168.1.41")
        self.assertEqual(self.call("192.168.1.41")[0], 403)

    def test_command_line_allow_still_works(self):
        with mock.patch.object(config, "ALLOW", config.parse_allow("192.168.1.0/24")):
            self.assertEqual(self.call("192.168.1.42")[0], 200)

    def test_remote_cannot_reach_device_routes(self):
        devices.add("192.168.1.43")
        for method, path in (("GET", "/api/setup/devices"), ("POST", "/api/setup/devices"), ("DELETE", "/api/setup/devices/192.168.1.43"),
                             ("PUT", "/api/setup/sharing"), ("POST", "/api/setup/devices/dismiss")):
            self.assertEqual(self.call("192.168.1.43", path, method, headers={"Origin": f"http://{LAN}:{config.PORT}", "Content-Type": "application/json"})[0], 403, path)

    def test_remote_cannot_get_the_qr_code(self):
        devices.add("192.168.1.45")
        self.assertEqual(self.call("192.168.1.45", "/api/setup/devices/qr")[0], 403)

    def test_bad_host_from_allowed_device_refused(self):
        devices.add("192.168.1.44")
        self.assertEqual(self.call("192.168.1.44", host="evil.example.com:%d" % config.PORT)[0], 403)


class Api(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = config.HOME / "devices.json"
        if self.path.exists():
            self.path.unlink()
        devices._pending.clear()
        devices._cache["data"] = None
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

    async def _listen_with_host(self, host, allow=""):
        FakeSite.started.clear()
        runner = web.AppRunner(self.app)
        self.app[devices_api.LISTEN]["runner"] = runner
        with mock.patch.object(config, "HOST", host), mock.patch.object(config, "ALLOW", config.parse_allow(allow)):
            await devices_api.listen(self.app)
        return list(FakeSite.started)

    async def test_command_line_host_bind_rules(self):
        # a wildcard covers loopback: one socket
        self.assertEqual(await self._listen_with_host("0.0.0.0", "192.168.1.0/24"), ["0.0.0.0"])
        # a specific address or IPv6: loopback is bound as well
        self.assertEqual(await self._listen_with_host("192.168.1.5", "192.168.1.0/24"), ["127.0.0.1", "192.168.1.5"])
        self.assertEqual(await self._listen_with_host("::", "192.168.1.0/24"), ["127.0.0.1", "::"])
        # loopback itself: one socket
        self.assertEqual(await self._listen_with_host("127.0.0.1", "192.168.1.0/24"), ["127.0.0.1"])
        with mock.patch.object(config, "ALLOW", config.parse_allow("192.168.1.0/24")):
            view = devices_api.view(self.app)
        self.assertTrue(view["from_command_line"])
        self.assertTrue(view["listening"])

    async def test_qr_code_of_the_listening_address(self):
        devices.set_sharing(True)
        self.app[devices_api.LISTEN]["runner"] = web.AppRunner(self.app)
        await devices_api.listen(self.app)
        resp = await self.http.request("GET", "/api/setup/devices/qr", headers={"Host": HOST})
        body = await resp.read()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["Content-Type"], "image/svg+xml")
        self.assertEqual(resp.headers["Cache-Control"], "no-store")
        self.assertTrue(body.startswith(b"<svg"))
        self.assertGreater(len(body), 500)
        self.assertNotIn(b"<script", body.lower())

    async def test_qr_code_is_404_when_not_listening(self):
        status, text = await self.call("GET", "/api/setup/devices/qr")
        self.assertEqual(status, 404)
        self.assertIn("not listening", text)

    def test_qr_svg_is_dark_on_white_with_a_border(self):
        svg = devices_api.qr_svg("http://192.168.1.20:80")
        self.assertTrue(svg.startswith(b"<svg"))
        self.assertIn(b'fill="#fff"', svg)
        self.assertIn(b'stroke="#000"', svg)
        self.assertGreater(len(svg), 500)

    async def test_bound_address_is_shown_and_a_changed_one_is_reported(self):
        devices.set_sharing(True)
        runner = web.AppRunner(self.app)
        self.app[devices_api.LISTEN]["runner"] = runner
        await devices_api.listen(self.app)
        self.assertEqual(devices_api.view(self.app)["address"], f"http://{LAN}:{config.PORT}")
        with mock.patch.object(devices_api, "lan_address", return_value=("192.168.1.77", None)):
            v = devices_api.view(self.app)
        self.assertIsNone(v["address"])
        self.assertEqual(v["address_error"], "This computer's network address changed. Turn sharing off and on again.")

    async def test_bad_host_and_ctrl_c_in_main(self):
        import errno
        def fail(coro, *a, **k):
            coro.close()
            raise OSError(errno.EADDRNOTAVAIL, "Can't assign requested address")
        with mock.patch.object(server.asyncio, "run", side_effect=fail), mock.patch.object(config, "HOST", "10.9.9.9"):
            with self.assertRaises(SystemExit) as cm:
                server.main()
        self.assertIn("Kinora could not listen on 10.9.9.9:", str(cm.exception))
        self.assertNotIn("Traceback", str(cm.exception))
        def interrupted(coro, *a, **k):
            coro.close()
            raise KeyboardInterrupt
        with mock.patch.object(server.asyncio, "run", side_effect=interrupted):
            server.main()   # returns: no traceback

    async def test_allowed_remote_peer_cannot_call_admin_api(self):
        devices.set_sharing(True)   # sharing on and the device allowed: only the admin guard can refuse
        devices.add("192.168.1.60")
        self.addCleanup(devices.remove, "192.168.1.60")
        resp = await server.local_only(await remote("192.168.1.60", "GET", "/api/setup/devices"), devices_api.get_devices)
        self.assertEqual(resp.status, 403)
        self.assertIn("settings can only be changed on the computer running Kinora", resp.text)


class Cache(StoreCase):
    def test_corrupt_file_is_kept_as_bak_before_it_is_overwritten(self):
        self.path.write_text("{broken")
        bak = self.path.with_name("devices.json.bak")
        self.addCleanup(lambda: bak.unlink() if bak.exists() else None)
        devices.add("192.168.1.80")
        self.assertEqual(bak.read_text(), "{broken")
        self.assertEqual([d["ip"] for d in json.loads(self.path.read_text())["devices"]], ["192.168.1.80"])

    def test_is_allowed_does_not_read_the_file_per_request(self):
        devices.add("192.168.1.81")
        devices.is_allowed("192.168.1.81")   # warm
        with mock.patch.object(devices, "_parse", wraps=devices._parse) as parse:
            for _ in range(20):
                self.assertTrue(devices.is_allowed("192.168.1.81"))
        self.assertEqual(parse.call_count, 0)

    def test_an_edit_of_the_file_is_seen(self):
        devices.add("192.168.1.82")
        self.assertTrue(devices.is_allowed("192.168.1.82"))
        self.path.write_text(json.dumps({"sharing": True, "devices": []}))
        self.assertFalse(devices.is_allowed("192.168.1.82"))


class RealSocketSharing(unittest.IsolatedAsyncioTestCase):
    """Sharing off closes the connections that arrived on the network socket, keep-alive ones included. Real sockets:
    the network socket is bound to this computer's own address, and a request on it counts as the owner's (local),
    so the test needs no second computer. Skipped when this computer has no network address."""

    async def asyncSetUp(self):
        lan, err = devices_api.lan_address()
        if not lan:
            self.skipTest(f"no network address on this computer ({err}): the network socket test needs one")
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self.lan, self.port = lan, port
        path = config.HOME / "devices.json"
        if path.exists():
            path.unlink()
        devices._cache["data"] = None
        for p in (mock.patch.object(config, "PORT", port), mock.patch.object(config, "ALLOW", []),
                  mock.patch.object(config, "HOST", "127.0.0.1"),
                  mock.patch.object(server, "ALLOWED_HOSTS", config.allowed_hosts(port)),
                  mock.patch.object(server, "ALLOWED_ORIGINS", {f"http://{h}" for h in config.allowed_hosts(port)})):
            p.start()
            self.addCleanup(p.stop)
        self.app = server.make_app(background=False)
        runner = web.AppRunner(self.app)
        self.app[devices_api.LISTEN]["runner"] = runner
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        await web.TCPSite(runner, "127.0.0.1", port).start()
        devices.set_sharing(True)
        await devices_api._start_lan(self.app, lan)
        self.assertIsNotNone(self.app[devices_api.LISTEN]["lan_site"])

    async def test_sharing_off_closes_an_open_keep_alive_connection(self):
        url = f"http://{self.lan}:{self.port}/api/cameras"
        connector = aiohttp.TCPConnector(limit=1)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url) as r:
                self.assertEqual(r.status, 200)
            async with session.put(f"http://127.0.0.1:{self.port}/api/setup/sharing", json={"enabled": False},
                                   headers={"Origin": f"http://127.0.0.1:{self.port}"}) as r:
                self.assertEqual(r.status, 200, await r.text())
            try:
                async with session.get(url) as r:
                    status = r.status
            except aiohttp.ClientError:
                status = None   # the connection was closed and the socket is gone: refused, as it should be
            self.assertNotEqual(status, 200, "a connection kept open survived sharing being turned off")
        self.assertIsNone(self.app[devices_api.LISTEN]["lan_site"])


if __name__ == "__main__":
    unittest.main()
