"""First-run setup and settings API (kinora/services/setup.py), the Origin rule in server.py, and the fake camera
harness (tests/fakecam.py). Offline: a temp data folder, a fake keyring, a fake gate clock, and pytapo's login
primitives replaced by fakes. No camera is contacted and no non-loopback socket is opened (checked at teardown).
Run: .venv/bin/python -m unittest tests.test_setup_api   (from the repo root)
"""
import asyncio
import atexit
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_ENV_BEFORE = {k: os.environ.get(k) for k in ("KINORA_HOME", "KINORA_NO_INSTANCE_LOCK")}
_home = tempfile.mkdtemp(prefix="kinora-setup-test-")   # set before kinora is imported: config reads it once
atexit.register(shutil.rmtree, _home, True)
os.environ["KINORA_HOME"] = _home
os.environ["KINORA_NO_INSTANCE_LOCK"] = "1"

import keyring  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from keyring.backend import KeyringBackend  # noqa: E402
from keyring.errors import KeyringError, NoKeyringError  # noqa: E402

import server  # noqa: E402
from kinora import cameras, config, creds, health  # noqa: E402
from kinora.camera import gate, session  # noqa: E402
from kinora.errors import BadInput, Unavailable  # noqa: E402
from kinora.camera import slots as slots_mod  # noqa: E402
from kinora.services import export, live, play  # noqa: E402
from kinora.services import setup as setup_api  # noqa: E402
from tests import fakecam  # noqa: E402
import pytapo  # noqa: E402
from pytapo.transport.tpap.tpap import TpapError  # noqa: E402

for _k, _v in _ENV_BEFORE.items():   # config is read once at import: the variables are not needed any more
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v

HOST = f"127.0.0.1:{config.PORT}"
ORIGIN = f"http://{HOST}"
SENTINEL = "PW-SENTINEL-FAKE-5530"          # distinctive fake secrets: scanned for in every response and file
CAM_PW = SENTINEL + "-cam"
CLOUD_PW = SENTINEL + "-cloud"
EMAIL = "owner@example.test"
ENV_KEYS = ("TAPO_PASS", "TAPO_USER", "TAPO_CLOUD_PASS", "TAPO_CLOUD_ACCOUNT")


class FakeKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        from keyring.errors import PasswordDeleteError
        if (service, username) not in self.store:
            raise PasswordDeleteError("not found")
        del self.store[(service, username)]


class SetupCase(unittest.IsolatedAsyncioTestCase):
    """Every module-level table the API touches is a temp or patched copy; config tables are mutated in place
    (server.py holds references to them), so they are saved and restored rather than replaced."""

    async def asyncSetUp(self):
        self._env = {k: os.environ.pop(k) for k in ENV_KEYS if k in os.environ}
        self.addCleanup(os.environ.update, self._env)
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = Path(home.name)
        (self.home / "cache" / "meta").mkdir(parents=True)
        saved_backend = keyring.get_keyring()
        self.addCleanup(keyring.set_keyring, saved_backend)
        self.kr = FakeKeyring()
        keyring.set_keyring(self.kr)
        self.clock = fakecam.Clock()
        self._tables = {"CAMS": dict(config.CAMS), "CAM_BY_IP": dict(config.CAM_BY_IP),
                        "TOMBSTONES": list(config.TOMBSTONES)}
        config.CAMS.clear()
        config.CAM_BY_IP.clear()
        config.TOMBSTONES[:] = []
        self.addCleanup(self._restore_tables)
        patches = [
            mock.patch.object(config, "HOME", self.home),
            mock.patch.object(gate, "BREAKER_F", self.home / "cache" / "meta" / "breaker.json"),
            mock.patch.object(gate, "time", self.clock.module()),
            mock.patch.object(creds, "_macos_security", return_value=None),
            mock.patch.dict(gate._state, {}, clear=True),
            mock.patch.dict(session._login_locks, {}, clear=True),
            mock.patch.dict(session._api_locks, {}, clear=True),
            mock.patch.dict(session._ctl_locks, {}, clear=True),
            mock.patch.dict(session._clients, {}, clear=True),
            mock.patch.dict(session._born, {}, clear=True),
            mock.patch.dict(health._state, {}, clear=True),
            mock.patch.dict(health._misses, {}, clear=True),
            mock.patch.dict(health._gen, {}, clear=True),
            mock.patch.dict(live._procs, {}, clear=True),
            mock.patch.dict(live._last, {}, clear=True),
            mock.patch.dict(play._sessions, {}, clear=True),
            mock.patch.dict(export.jobs, {}, clear=True),
            mock.patch.object(cameras, "slots", slots_mod.Slots([])),
            mock.patch.object(cameras, "_cfg_lock", asyncio.Lock()),
            mock.patch.object(setup_api, "_verify_lock", asyncio.Lock()),
            mock.patch.object(session, "VERIFYING", set()),
            mock.patch.dict(setup_api._reach_last, {}, clear=True),
            # server.py binds CAMS at import; test_config_home reloads config, so point it at the live table
            mock.patch.object(server, "CAMS", config.CAMS),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.fake = fakecam.FakeCamera().install()
        self.addCleanup(self.fake.close)
        self.out = io.StringIO()             # stdout and stderr of the code under test, for the leak check
        for p in (mock.patch.object(sys, "stdout", self.out), mock.patch.object(sys, "stderr", self.out)):
            p.start()
            self.addCleanup(p.stop)
        self.seen = []                       # every response body, for the leak check at teardown
        self.http = TestClient(TestServer(server.make_app(background=False)))
        await self.http.start_server()
        self.addAsyncCleanup(self.http.close)

    def _restore_tables(self):
        config.CAMS.clear()
        config.CAMS.update(self._tables["CAMS"])
        config.CAM_BY_IP.clear()
        config.CAM_BY_IP.update(self._tables["CAM_BY_IP"])
        config.TOMBSTONES[:] = self._tables["TOMBSTONES"]

    async def asyncTearDown(self):
        for text in self.seen + [self.out.getvalue()]:
            self.assertNotIn(SENTINEL, text)
        for name in ("cameras.json", "settings.json"):
            f = self.home / name
            if f.exists():
                self.assertNotIn(SENTINEL, f.read_text())
        breaker = self.home / "cache" / "meta" / "breaker.json"
        if breaker.exists():
            self.assertNotIn(SENTINEL, breaker.read_text())
        self.assertEqual(self.fake.connects_refused, [], "a socket tried to leave loopback")

    # ---- helpers ---------------------------------------------------------------------------
    async def call(self, method, path, body=None, *, origin=ORIGIN, ctype="application/json", raw=None):
        headers = {"Host": HOST}
        if origin is not None:
            headers["Origin"] = origin
        if ctype is not None:
            headers["Content-Type"] = ctype
        data = raw if raw is not None else (json.dumps(body) if body is not None else None)
        resp = await self.http.request(method, path, headers=headers, data=data)
        text = await resp.text()
        self.seen.append(text)
        return resp.status, text

    async def ok(self, method, path, body=None):
        status, text = await self.call(method, path, body)
        self.assertEqual(status, 200, text)
        return json.loads(text)

    async def set_account(self):
        return await self.ok("POST", "/api/setup/account", {"email": EMAIL, "password": CLOUD_PW})

    async def add_cam(self, name="Kitchen", ip="10.0.0.11", password=CAM_PW, **extra):
        """Add a camera. The camera account is optional; a password is sent with the default user. `model` (an old
        client's field) is accepted and ignored."""
        body = {"name": name, "ip": ip}
        if password is not None:
            body.update(camera_user="rtspadmin", camera_password=password)
        body.update(extra)
        return await self.ok("POST", "/api/setup/cameras", body)

    def attempts(self, ip):
        return self.fake.login_attempts.get(ip, 0)

    def gate_state(self, cid):
        return copy.deepcopy(gate.st(config.CAMS[cid]))

    async def verify(self, cid):
        return await self.ok("POST", "/api/setup/verify", {"id": cid})


class OriginAndShape(SetupCase):
    async def test_get_needs_no_origin(self):
        status, _ = await self.call("GET", "/api/cameras", origin=None, ctype=None)
        self.assertEqual(status, 200)

    async def test_post_without_origin_is_refused(self):
        status, _ = await self.call("POST", "/api/setup/account", {"email": EMAIL, "password": CLOUD_PW}, origin=None)
        self.assertEqual(status, 403)
        self.assertEqual(self.kr.store, {})

    async def test_foreign_or_wrong_origin_is_refused(self):
        for origin in ("http://evil.example", "https://127.0.0.1:8080", f"http://127.0.0.1:{config.PORT + 1}", "null"):
            status, _ = await self.call("POST", "/api/setup/account", {"email": EMAIL, "password": CLOUD_PW},
                                        origin=origin)
            self.assertEqual(status, 403, origin)
        status, _ = await self.call("DELETE", "/api/cameras/nope", {"confirm": "nope"}, origin="http://evil.example")
        self.assertEqual(status, 403)

    async def test_state_changing_routes_need_json(self):
        status, _ = await self.call("POST", "/api/setup/account", raw="email=x", ctype="text/plain")
        self.assertEqual(status, 415)

    async def test_cancel_route_has_no_body_so_only_origin_is_required(self):
        status, _ = await self.call("POST", "/api/export/nojob/cancel", None, ctype=None)
        self.assertEqual(status, 404)   # reached the handler: no 415 for a body-less route
        status, _ = await self.call("POST", "/api/export/nojob/cancel", None, origin=None, ctype=None)
        self.assertEqual(status, 403)

    async def test_bodies_are_never_echoed(self):
        status, text = await self.call("POST", "/api/setup/account", {"email": "bad" + SENTINEL, "password": CLOUD_PW,
                                                                     "extra": SENTINEL})
        self.assertEqual(status, 400)
        self.assertNotIn(SENTINEL, text)
        status, text = await self.call("POST", "/api/setup/account", raw="{not json " + SENTINEL)
        self.assertEqual(status, 400)
        self.assertNotIn(SENTINEL, text)
        status, text = await self.call("POST", "/api/setup/cameras", {"name": "x", "ip": "not-" + SENTINEL,
                                                                      "model": "C200", "camera_password": CAM_PW})
        self.assertEqual(status, 400)
        self.assertNotIn(SENTINEL, text)


class State(SetupCase):
    async def test_zero_cameras_is_prepare_and_no_login(self):
        state = await self.ok("GET", "/api/setup/state")
        self.assertEqual(state["step"], "prepare")
        self.assertEqual(state["cameras"], [])
        self.assertEqual(set(state["prerequisites"]), {"ffmpeg", "ffprobe", "credential_store"})
        self.assertFalse(state["account"]["email_set"])
        self.assertEqual(dict(self.fake.login_attempts), {})

    async def test_state_carries_the_app_version_for_about(self):
        state = await self.ok("GET", "/api/setup/state")
        self.assertEqual(state["version"], config.VERSION)
        self.assertRegex(state["version"], r"^\d+\.\d+\.\d+$")

    async def test_account_stores_secrets_and_never_returns_them(self):
        body = await self.set_account()
        self.assertEqual(body, {"ok": True, "saved": ["tplink"]})
        self.assertIn(("kinora", "cloud:" + EMAIL.lower()), self.kr.store)
        self.assertEqual(json.loads((self.home / "settings.json").read_text())["account_email"], EMAIL)
        state = await self.ok("GET", "/api/setup/state")
        self.assertTrue(state["account"]["email_set"])
        self.assertEqual(state["account"]["email"], EMAIL)       # the saved TP-Link email: not a secret

    async def test_state_account_email_is_null_when_none_is_saved(self):
        state = await self.ok("GET", "/api/setup/state")
        self.assertEqual(state["account"], {"email": None, "email_set": False})

    async def test_camera_fields_come_from_its_own_account_and_never_hold_the_password(self):
        await self.set_account()
        made = await self.add_cam(name="Hall", ip="10.0.0.120", camera_user=OWN_USER, password=OWN_PW)
        bare = await self.add_cam(name="Porch", ip="10.0.0.121", password=None)
        status, text = await self.call("GET", "/api/setup/state")
        self.assertEqual(status, 200)
        self.assertNotIn(OWN_PW, text)
        self.assertNotIn(CAM_PW, text)
        by_id = {c["id"]: c for c in json.loads(text)["cameras"]}
        self.assertEqual(by_id[made["id"]]["camera_user"], OWN_USER)
        self.assertIs(by_id[made["id"]]["has_camera_account"], True)
        self.assertEqual(by_id[made["id"]]["secret_sources"]["camera"], "wizard")
        self.assertIsNone(by_id[bare["id"]]["camera_user"])
        self.assertIs(by_id[bare["id"]]["has_camera_account"], False)
        self.assertEqual(by_id[bare["id"]]["secret_sources"]["camera"], "missing")
        for c in by_id.values():
            self.assertIs(c["live_refused"], False)
            self.assertEqual(c["secret_sources"]["cloud"], "wizard")

    async def test_live_refused_follows_the_gate_live_state(self):
        made = await self.add_cam(ip="10.0.0.122", camera_user=OWN_USER, password=OWN_PW)
        gate.note_rtsp_auth_failure(config.CAMS[made["id"]])
        state = await self.ok("GET", "/api/setup/state")
        self.assertIs(state["cameras"][0]["live_refused"], True)

    async def test_legacy_items_and_env_do_not_make_a_source_in_the_state(self):
        await self.set_account()
        made = await self.add_cam(ip="10.0.0.123", password=None)   # no own camera account
        self.kr.set_password(creds.SERVICE, "rtspadmin", LEGACY_PW)
        self.kr.set_password(creds.CLOUD_SERVICE, EMAIL, LEGACY_PW)
        with mock.patch.dict(os.environ, {"TAPO_PASS": LEGACY_PW, "TAPO_CLOUD_PASS": LEGACY_PW}):
            state = await self.ok("GET", "/api/setup/state")
        cam = state["cameras"][0]
        self.assertEqual(cam["secret_sources"], {"camera": "missing", "cloud": "wizard"})
        self.assertIsNone(cam["camera_user"])
        self.assertEqual(state["step"], "verify")
        self.assertNotIn(LEGACY_PW, json.dumps(state))

    async def test_unreadable_store_is_reported_once_and_cameras_show_missing(self):
        await self.set_account()
        await self.add_cam(name="Hall", ip="10.0.0.124", camera_user=OWN_USER, password=OWN_PW)
        await self.add_cam(name="Porch", ip="10.0.0.125", camera_user=OWN_USER, password=OWN_PW)
        with mock.patch.object(self.kr, "get_password", side_effect=KeyringError("denied")):
            status, text = await self.call("GET", "/api/setup/state")
        self.assertEqual(status, 200)
        state = json.loads(text)
        store = state["prerequisites"]["credential_store"]
        self.assertIs(store["ok"], False)
        self.assertIn("could not be read", store["reason"])
        for c in state["cameras"]:
            self.assertEqual(c["secret_sources"]["camera"], "missing")
            self.assertIsNone(c["camera_user"])
            self.assertIs(c["has_camera_account"], False)
        self.assertNotIn(OWN_PW, text)

    async def test_account_rejects_bad_email(self):
        status, _ = await self.call("POST", "/api/setup/account", {"email": "nobody", "password": CLOUD_PW})
        self.assertEqual(status, 400)
        status, _ = await self.call("POST", "/api/setup/account", {"email": EMAIL, "password": ""})
        self.assertEqual(status, 400)


class AddAndListing(SetupCase):
    async def test_add_is_automatic_with_an_optional_camera_account(self):
        made = await self.add_cam(name="Kitchen", ip="10.0.0.11")
        self.assertTrue(made["id"].startswith("kitchen_"))
        self.assertTrue(made["device_id"])
        self.assertIn("camera:" + made["device_id"], [k[1] for k in self.kr.store])
        cam = config.CAMS[made["id"]]
        self.assertEqual((cam["auth"], cam["enabled"], cam["verified"]), ("auto", False, False))
        self.assertNotIn("model", cam)        # the model and its flags come from the first Verify
        self.assertNotIn("ptz", cam)

    async def test_a_model_sent_by_an_old_client_is_ignored(self):
        made = await self.add_cam(name="Baby", ip="10.0.0.12", model="C100")
        cam = config.CAMS[made["id"]]
        self.assertEqual(cam["auth"], "auto")
        self.assertNotIn("model", cam)
        self.assertNotIn("p2p", cam)

    async def test_camera_account_is_optional_and_all_or_nothing(self):
        made = await self.ok("POST", "/api/setup/cameras", {"name": "Hall", "ip": "10.0.0.13"})
        self.assertEqual(creds.secret_sources(config.CAMS[made["id"]])["camera"], "missing")
        for body in ({"name": "Y", "ip": "10.0.0.14", "camera_user": "rtspadmin"},
                     {"name": "Y", "ip": "10.0.0.14", "camera_password": CAM_PW}):
            status, text = await self.call("POST", "/api/setup/cameras", body)
            self.assertEqual((status, text), (400, "Enter both the camera account user and its password, or leave both empty"))
        self.assertNotIn("10.0.0.14", [c["ip"] for c in config.CAMS.values()])

    async def test_explicit_auth_is_accepted_and_unknown_is_refused(self):
        made = await self.add_cam(name="Other", ip="10.0.0.15", auth="admin_cloud")
        self.assertEqual(config.CAMS[made["id"]]["auth"], "admin_cloud")
        status, _ = await self.call("POST", "/api/setup/cameras", {"name": "Z", "ip": "10.0.0.16", "auth": "nope"})
        self.assertEqual(status, 400)

    async def test_bad_name_and_duplicate_address_are_refused(self):
        await self.add_cam(ip="10.0.0.17")
        status, _ = await self.call("POST", "/api/setup/cameras", {"name": "Y", "ip": "10.0.0.17"})
        self.assertEqual(status, 400)
        status, _ = await self.call("POST", "/api/setup/cameras", {"name": " ", "ip": "10.0.0.18"})
        self.assertEqual(status, 400)

    async def test_added_camera_and_normal_listings_do_not_login(self):
        made = await self.add_cam(ip="10.0.0.17")
        cams = await self.ok("GET", "/api/cameras")
        self.assertEqual([(c["id"], c["enabled"], c["verified"]) for c in cams], [(made["id"], False, False)])
        await self.ok("GET", "/api/status")
        await self.ok("GET", "/api/setup/state")
        status, _ = await self.call("GET", f"/api/recordings?cam={made['id']}&date=20260101", origin=None, ctype=None)
        self.assertNotEqual(status, 200)   # a disabled camera is refused by the session layer, not logged in to
        self.assertEqual(dict(self.fake.login_attempts), {})


class Reach(SetupCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.set_account()

    async def test_only_private_addresses_and_only_three_ports(self):
        made = await self.add_cam(ip="192.168.1.5")
        calls = []

        async def fake_tcp(ip, port):
            calls.append((ip, port))
            return port == 443
        with mock.patch.object(health, "_tcp", fake_tcp):
            for bad in ("8.8.8.8", "127.0.0.1", "169.254.1.1", "203.0.113.5", "not-an-ip"):
                status, _ = await self.call("POST", "/api/setup/reach", {"ip": bad, "id": made["id"]})
                self.assertEqual(status, 400, bad)
            self.assertEqual(calls, [])
            body = await self.ok("POST", "/api/setup/reach", {"ip": "192.168.1.5", "id": made["id"]})
        self.assertEqual(body["ports"], {"443": True, "554": False, "8800": False})
        self.assertEqual(sorted(calls), [("192.168.1.5", 443), ("192.168.1.5", 554), ("192.168.1.5", 8800)])

    async def test_id_is_required_and_the_saved_address_must_match(self):
        made = await self.add_cam(ip="192.168.1.6")
        calls = []

        async def fake_tcp(ip, port):
            calls.append((ip, port))
            return False
        with mock.patch.object(health, "_tcp", fake_tcp):
            status, _ = await self.call("POST", "/api/setup/reach", {"ip": "192.168.1.6"})
            self.assertEqual(status, 400)                       # no id
            status, _ = await self.call("POST", "/api/setup/reach", {"ip": "192.168.1.7", "id": made["id"]})
            self.assertEqual(status, 400)                       # not the saved address
            self.assertEqual(calls, [])

    async def test_a_second_check_of_one_camera_within_a_second_is_throttled(self):
        made = await self.add_cam(ip="192.168.1.8")

        async def fake_tcp(ip, port):
            return False
        with mock.patch.object(health, "_tcp", fake_tcp):
            await self.ok("POST", "/api/setup/reach", {"ip": "192.168.1.8", "id": made["id"]})
            status, text = await self.call("POST", "/api/setup/reach", {"ip": "192.168.1.8", "id": made["id"]})
            self.assertEqual(status, 429)
            self.assertNotIn("{", text)                         # plain text
            setup_api._reach_last[made["id"]] -= 2              # two seconds later: allowed again
            await self.ok("POST", "/api/setup/reach", {"ip": "192.168.1.8", "id": made["id"]})

    async def test_unknown_camera_id_is_404(self):
        status, _ = await self.call("POST", "/api/setup/reach", {"ip": "10.0.0.9", "id": "nobody_1"})
        self.assertEqual(status, 404)


class Verify(SetupCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.set_account()

    async def test_one_login_then_ok_with_device_info(self):
        made = await self.add_cam(ip="10.0.0.21")
        self.fake.add_device("10.0.0.21", CLOUD_PW, model="C200", firmware="1.4.6 Build 1", mac="AA-BB-CC-00-00-01")
        body = await self.verify(made["id"])
        self.assertEqual(body, {"state": "ok", "model": "C200", "firmware": "1.4.6 Build 1", "mismatch": False})
        self.assertEqual(self.attempts("10.0.0.21"), 1)
        cam = config.CAMS[made["id"]]
        self.assertEqual((cam["enabled"], cam["verified"], cam["model_seen"], cam["fw_seen"]),
                         (True, True, "C200", "1.4.6 Build 1"))
        self.assertEqual(cam["model"], "C200")
        self.assertEqual(cam["mac"], "AA-BB-CC-00-00-01")
        self.assertEqual(len(self.gate_state(made["id"])["logins"]), 1)
        state = await self.ok("GET", "/api/setup/state")
        self.assertEqual(state["cameras"][0]["status"], "ok")
        self.assertEqual(state["step"], "done")

    async def test_different_mac_later_is_flagged_and_never_rekeys_the_gate(self):
        made = await self.add_cam(ip="10.0.0.20")
        cid = made["id"]
        dev = self.fake.add_device("10.0.0.20", CLOUD_PW, mac="AA-AA-AA-00-00-01")
        await self.verify(cid)
        device_id = config.CAMS[cid]["device_id"]
        await self.ok("PATCH", f"/api/cameras/{cid}", {"enabled": False})
        dev.mac = "BB-BB-BB-00-00-02"           # a different device now answers at this address
        self.clock.advance(181)
        self.assertEqual((await self.verify(cid))["state"], "ok")
        cam = config.CAMS[cid]
        self.assertEqual((cam["mac"], cam["mac_mismatch"], cam["device_id"]), ("AA-AA-AA-00-00-01", True, device_id))
        state = await self.ok("GET", "/api/setup/state")
        self.assertTrue(state["cameras"][0]["mac_mismatch"])
        self.assertNotIn("mac", state["cameras"][0])
        self.assertEqual(self.attempts("10.0.0.20"), 2)          # switching off dropped the session: a new login
        self.assertEqual(len(self.gate_state(cid)["logins"]), 2)

    async def test_model_mismatch_is_a_warning(self):
        made = await self.add_cam(ip="10.0.0.22")
        await self.ok("PATCH", f"/api/cameras/{made['id']}", {"model": "C200"})   # a manual model that is wrong
        self.fake.add_device("10.0.0.22", CLOUD_PW, model="C210")
        body = await self.verify(made["id"])
        self.assertEqual(body["state"], "ok")
        self.assertTrue(body["mismatch"])

    async def test_verified_camera_cannot_be_verified_as_pending(self):
        made = await self.add_cam(ip="10.0.0.23")
        self.fake.add_device("10.0.0.23", CLOUD_PW)
        await self.verify(made["id"])
        status, _ = await self.call("POST", "/api/setup/verify", {"id": made["id"]})
        self.assertEqual(status, 409)
        self.assertEqual(self.attempts("10.0.0.23"), 1)

    async def test_second_verify_within_180s_is_waiting_with_no_attempt(self):
        made = await self.add_cam(ip="10.0.0.24")
        self.fake.add_device("10.0.0.24", CLOUD_PW)
        await self.verify(made["id"])
        await self.ok("PATCH", f"/api/cameras/{made['id']}", {"enabled": False})
        self.clock.advance(60)
        body = await self.verify(made["id"])
        self.assertEqual(body["state"], "waiting")
        self.assertTrue(0 < body["wait_s"] <= 180)
        self.assertEqual(self.attempts("10.0.0.24"), 1)
        self.clock.advance(200)
        body = await self.verify(made["id"])   # switching off dropped the session: this verify logs in again
        self.assertEqual(body["state"], "ok")
        self.assertEqual(self.attempts("10.0.0.24"), 2)

    async def test_one_camera_at_a_time_second_request_gets_409_and_no_login(self):
        made = await self.add_cam(ip="10.0.0.25")
        dev = self.fake.add_device("10.0.0.25", CLOUD_PW)
        dev.hold, dev.entered = threading.Event(), threading.Event()
        first = asyncio.create_task(self.verify(made["id"]))
        self.assertTrue(await asyncio.to_thread(dev.entered.wait, 10), "the first login never started")
        status, text = await self.call("POST", "/api/setup/verify", {"id": made["id"]})
        self.assertEqual(status, 409, text)
        dev.hold.set()
        body = await first
        self.assertEqual(body["state"], "ok")
        self.assertEqual(self.attempts("10.0.0.25"), 1)

    async def test_failed_login_leaves_camera_disabled_and_is_not_retried(self):
        made = await self.add_cam(ip="10.0.0.26")
        self.fake.add_device("10.0.0.26", CLOUD_PW)
        await self.ok("POST", "/api/setup/account", {"email": EMAIL, "password": "WRONG-" + SENTINEL})
        body = await self.verify(made["id"])
        self.assertEqual(body["state"], "failed")
        self.assertIn("The camera refused the TP-Link password", body["reason"])
        self.assertEqual(self.attempts("10.0.0.26"), 1)
        self.assertFalse(config.CAMS[made["id"]]["enabled"])
        state = await self.ok("GET", "/api/setup/state")
        self.assertEqual(state["cameras"][0]["status"], "disabled_by_gate")

    async def test_typo_cycle_cannot_exceed_two_failed_logins_in_30_minutes(self):
        made = await self.add_cam(ip="10.0.0.27")
        cid = made["id"]
        self.fake.add_device("10.0.0.27", CLOUD_PW)
        await self.ok("POST", "/api/setup/account", {"email": EMAIL, "password": "typo-one-" + SENTINEL})
        self.assertEqual((await self.verify(cid))["state"], "failed")                    # attempt 1
        await self.ok("POST", f"/api/cameras/{cid}/credentials", {"cloud_password": "typo-two-" + SENTINEL})
        self.assertEqual((await self.verify(cid))["state"], "waiting")                   # no attempt: 180 s gap
        self.assertEqual(self.attempts("10.0.0.27"), 1)
        self.clock.advance(181)
        self.assertEqual((await self.verify(cid))["state"], "failed")                    # attempt 2
        self.assertEqual(self.attempts("10.0.0.27"), 2)
        await self.ok("POST", f"/api/cameras/{cid}/credentials", {"cloud_password": CLOUD_PW})
        body = await self.verify(cid)                                                    # the fix: still blocked
        self.assertEqual(body["state"], "waiting")
        self.assertGreater(body["wait_s"], 1000)
        self.assertEqual(self.attempts("10.0.0.27"), 2)
        self.assertEqual(len(self.gate_state(cid)["recent_fails"]), 2)

    async def test_camera_suspension_is_honoured(self):
        made = await self.add_cam(ip="10.0.0.28")
        dev = self.fake.add_device("10.0.0.28", CLOUD_PW)
        dev.login_error = "Error: Temporary Suspension: Try again in 300 seconds"
        body = await self.verify(made["id"])
        self.assertEqual(body["state"], "failed")
        self.assertIn("locked itself", body["reason"])
        body = await self.verify(made["id"])
        self.assertEqual(body["state"], "waiting")
        self.assertEqual(self.attempts("10.0.0.28"), 1)

    async def test_missing_tplink_account_is_refused_before_any_login(self):
        made = await self.add_cam(ip="10.0.0.29")
        self.kr.store.clear()                            # the TP-Link password is gone from the store
        self.fake.add_device("10.0.0.29", CLOUD_PW)
        body = await self.verify(made["id"])
        self.assertEqual(body, {"state": "failed", "reason": "Save your TP-Link account first"})
        self.assertEqual(self.attempts("10.0.0.29"), 0)
        self.assertEqual(self.gate_state(made["id"]).get("logins", []), [])

    async def test_verify_needs_only_the_tplink_account_not_the_camera_account(self):
        made = await self.ok("POST", "/api/setup/cameras", {"name": "Hall", "ip": "10.0.0.31"})   # no camera account
        self.fake.add_device("10.0.0.31", CLOUD_PW)
        self.assertEqual((await self.verify(made["id"]))["state"], "ok")
        self.assertEqual(self.attempts("10.0.0.31"), 1)

    async def test_unknown_camera_is_404(self):
        status, _ = await self.call("POST", "/api/setup/verify", {"id": "nobody_1"})
        self.assertEqual(status, 404)


class Settings(SetupCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.set_account()

    async def test_ip_edit_keeps_gate_state_and_needs_verify(self):
        made = await self.add_cam(ip="10.0.0.31")
        cid = made["id"]
        self.fake.add_device("10.0.0.31", CLOUD_PW)
        await self.verify(cid)
        before = self.gate_state(cid)
        device_id = config.CAMS[cid]["device_id"]
        body = await self.ok("PATCH", f"/api/cameras/{cid}", {"ip": "10.0.0.32"})
        self.assertEqual((body["ip"], body["verified"], body["enabled"]), ("10.0.0.32", False, False))
        self.assertNotIn("mac", body)
        self.assertEqual(config.CAMS[cid]["device_id"], device_id)
        after = self.gate_state(cid)
        self.assertEqual(after["logins"], before["logins"])
        self.assertEqual(after.get("transport"), before.get("transport"))
        state = await self.ok("GET", "/api/setup/state")
        self.assertEqual(state["cameras"][0]["status"], "waiting")   # the 180 s spacing followed the device
        self.clock.advance(181)
        self.fake.add_device("10.0.0.32", CLOUD_PW)
        self.assertEqual((await self.verify(cid))["state"], "ok")
        self.assertEqual(self.attempts("10.0.0.32"), 1)
        self.assertEqual(len(self.gate_state(cid)["logins"]), 2)

    async def test_flags_are_validated_and_enable_needs_verify(self):
        made = await self.add_cam(ip="10.0.0.33")
        cid = made["id"]
        status, _ = await self.call("PATCH", f"/api/cameras/{cid}", {"enabled": True})
        self.assertEqual(status, 409)
        status, _ = await self.call("PATCH", f"/api/cameras/{cid}", {"flags": {"win": 0}})
        self.assertEqual(status, 400)
        status, _ = await self.call("PATCH", f"/api/cameras/{cid}", {"flags": {"p2p": True}})
        self.assertEqual(status, 400)
        body = await self.ok("PATCH", f"/api/cameras/{cid}", {"flags": {"ptz_invert": True, "win": 45}})
        self.assertEqual(body["flags"]["ptz_invert"], True)
        self.assertEqual(body["flags"]["win"], 45)
        self.assertNotIn("mac", body)
        self.assertNotIn("camera:", json.dumps(body))

    async def test_order_is_saved_and_listed_by_the_state_and_the_camera_list(self):
        ids = [(await self.add_cam(name=n, ip=f"10.0.0.{40 + i}"))["id"] for i, n in enumerate(("One", "Two", "Three"))]
        new = [ids[2], ids[0], ids[1]]
        body = await self.ok("PUT", "/api/cameras/order", {"order": new})
        self.assertEqual(body["order"], new)
        self.assertEqual([c["id"] for c in (await self.ok("GET", "/api/setup/state"))["cameras"]], new)
        self.assertEqual([c["id"] for c in await self.ok("GET", "/api/cameras")], new)
        self.assertEqual([c["id"] for c in json.loads((config.HOME / "cameras.json").read_text())["cameras"]], new)

    async def test_order_must_name_every_camera_once(self):
        ids = [(await self.add_cam(name=n, ip=f"10.0.0.{50 + i}"))["id"] for i, n in enumerate(("One", "Two"))]
        for bad in ({"order": ids[:1]}, {"order": [ids[0], ids[0]]}, {"order": ids + ["nope"]}, {"order": "x"}, {"order": [1, 2]}, {}):
            status, _ = await self.call("PUT", "/api/cameras/order", bad)
            self.assertEqual(status, 400, bad)
        status, _ = await self.call("PUT", "/api/cameras/order", {"order": ids, "x": 1})
        self.assertEqual(status, 400)
        status, _ = await self.call("PUT", "/api/cameras/order", {"order": ids}, origin=None)
        self.assertEqual(status, 403)
        self.assertEqual(list(config.CAMS), ids)

    async def test_remove_then_readd_keeps_device_id_and_budget(self):
        made = await self.add_cam(name="Kitchen", ip="10.0.0.34")
        cid = made["id"]
        self.fake.add_device("10.0.0.34", CLOUD_PW)
        await self.verify(cid)
        device_id = config.CAMS[cid]["device_id"]
        status, _ = await self.call("DELETE", f"/api/cameras/{cid}", {"confirm": "wrong"})
        self.assertEqual(status, 400)
        self.assertIn(cid, config.CAMS)
        body = await self.ok("DELETE", f"/api/cameras/{cid}", {"confirm": cid})
        self.assertTrue(body["secret_removed"])
        self.assertNotIn(cid, config.CAMS)
        self.assertNotIn("camera:" + device_id, [k[1] for k in self.kr.store])
        again = await self.add_cam(name="Kitchen", ip="10.0.0.34")
        self.assertNotEqual(again["id"], cid)                      # ids are never reused
        self.assertEqual(again["device_id"], device_id)            # the same device at the same address
        body = await self.verify(again["id"])
        self.assertEqual(body["state"], "waiting")                 # the old login is still inside the 180 s gap
        self.assertEqual(self.attempts("10.0.0.34"), 1)

    async def test_credentials_are_stored_and_never_log_in(self):
        made = await self.add_cam(ip="10.0.0.35", password="typo-" + SENTINEL)
        cid = made["id"]
        self.fake.add_device("10.0.0.35", CLOUD_PW)
        await self.verify(cid)
        before = self.attempts("10.0.0.35")
        body = await self.ok("POST", f"/api/cameras/{cid}/credentials", {"camera_password": CAM_PW, "reset_auth": False})
        self.assertEqual(body, {"ok": True, "reset_auth": False})
        self.assertEqual(self.attempts("10.0.0.35"), before)
        status, _ = await self.call("POST", f"/api/cameras/{cid}/credentials", {})
        self.assertEqual(status, 400)
        status, _ = await self.call("POST", f"/api/cameras/{cid}/credentials", {"cloud_password": CLOUD_PW,
                                                                              "reset_auth": "yes"})
        self.assertEqual(status, 400)

    async def test_cloud_password_change_needs_the_account_email(self):
        made = await self.add_cam(ip="10.0.0.36")
        (self.home / "settings.json").unlink()   # no account email yet
        status, text = await self.call("POST", f"/api/cameras/{made['id']}/credentials", {"cloud_password": CLOUD_PW})
        self.assertEqual(status, 400)
        self.assertIn("account email", text)

class VerifyPersistsNothingEarly(SetupCase):
    """Verify switches a camera on only after its login succeeds (no enabled:true before the login)."""
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.set_account()

    async def test_verify_persists_nothing_before_the_login_succeeds(self):
        made = await self.add_cam(ip="10.0.0.61")
        cid = made["id"]
        dev = self.fake.add_device("10.0.0.61", CLOUD_PW)
        dev.hold, dev.entered = threading.Event(), threading.Event()
        task = asyncio.create_task(self.verify(cid))
        self.assertTrue(await asyncio.to_thread(dev.entered.wait, 10), "the login never started")
        on_disk = json.loads((self.home / "cameras.json").read_text())
        disk = next(c for c in on_disk["cameras"] if c["id"] == cid)
        self.assertFalse(disk["enabled"])
        self.assertFalse(disk.get("verified"))
        self.assertFalse(config.CAMS[cid]["enabled"])
        dev.hold.set()
        self.assertEqual((await task)["state"], "ok")
        self.assertTrue(config.CAMS[cid]["enabled"])

    async def test_a_crash_during_verify_leaves_the_camera_switched_off_and_verifiable(self):
        made = await self.add_cam(ip="10.0.0.62")
        cid = made["id"]
        self.fake.add_device("10.0.0.62", CLOUD_PW)
        with mock.patch.object(session, "client", side_effect=RuntimeError("crash during verify")):
            status, _ = await self.call("POST", "/api/setup/verify", {"id": cid})
        self.assertEqual(status, 500)
        cam = config.CAMS[cid]
        self.assertFalse(cam["enabled"])
        self.assertFalse(cam.get("verified"))
        self.assertEqual(session.VERIFYING, set())
        self.assertEqual((await self.verify(cid))["state"], "ok")
        self.assertEqual(self.attempts("10.0.0.62"), 1)

    async def test_legacy_enabled_camera_without_verified_gets_one_gated_verify(self):
        await cameras.add({"id": "legacy_c", "name": "Legacy", "ip": "10.0.0.63", "auth": "cam_cloud",
                           "model": "C200", "enabled": True})
        creds.set_camera_secret(config.CAMS["legacy_c"], "rtspadmin", CAM_PW)
        self.fake.add_device("10.0.0.63", CAM_PW)
        status, _ = await self.call("GET", "/api/recordings?cam=legacy_c&date=20260101", origin=None, ctype=None)
        self.assertEqual(status, 400)                       # not verified: refused, no login
        self.assertEqual(self.attempts("10.0.0.63"), 0)
        self.assertEqual((await self.verify("legacy_c"))["state"], "ok")
        self.assertEqual(self.attempts("10.0.0.63"), 1)
        cam = config.CAMS["legacy_c"]
        self.assertEqual((cam["enabled"], cam["verified"]), (True, True))


class SwitchedOff(SetupCase):
    """A camera that is switched off or not verified is never logged in to and never connected (no RTSP either)."""
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.set_account()

    async def verified_camera(self, ip):
        made = await self.add_cam(ip=ip)
        self.fake.add_device(ip, CLOUD_PW)
        self.assertEqual((await self.verify(made["id"]))["state"], "ok")
        return made["id"]

    async def test_switched_off_camera_is_refused_by_every_route_without_a_login_or_ffmpeg(self):
        cid = await self.verified_camera("10.0.0.51")
        await self.ok("PATCH", f"/api/cameras/{cid}", {"enabled": False})
        logins = self.attempts("10.0.0.51")
        spies = {"clip": mock.patch.object(server, "fetch_media", new=mock.AsyncMock()),
                 "live": mock.patch.object(live, "_start", new=mock.AsyncMock()),
                 "play": mock.patch.object(play, "start", new=mock.AsyncMock()),
                 "ptz": mock.patch.object(server.ptz_service, "step", new=mock.AsyncMock())}
        spy = {}
        for name, p in spies.items():
            spy[name] = p.start()
            self.addCleanup(p.stop)
        cases = [
            ("GET", f"/api/recordings?cam={cid}&date=20260101", None),
            ("GET", f"/api/events?cam={cid}&date=20260101", None),
            ("GET", f"/api/clip?cam={cid}&start=1000&end=1010", None),
            ("GET", f"/api/thumb?cam={cid}&ts=1000", None),
            ("POST", "/api/play/start", {"cam": cid, "ts": 1000}),
            ("POST", "/api/ptz", {"cam": cid, "dir": "left"}),
            ("GET", f"/live/{cid}/index.m3u8", None),
        ]
        for method, path, body in cases:
            get = body is None
            status, text = await self.call(method, path, body, origin=None if get else ORIGIN,
                                           ctype=None if get else "application/json")
            self.assertEqual(status, 400, f"{path}: {text}")
        self.assertEqual(self.attempts("10.0.0.51"), logins)
        for name, m in spy.items():
            m.assert_not_awaited()

    async def test_switching_off_stops_media_and_drops_the_session(self):
        cid = await self.verified_camera("10.0.0.52")
        self.assertIn(cid, session._clients)
        with mock.patch.object(play, "stop", new=mock.AsyncMock()) as play_stop, \
                mock.patch.object(live, "stop", new=mock.AsyncMock()) as live_stop:
            await self.ok("PATCH", f"/api/cameras/{cid}", {"enabled": False})
        play_stop.assert_awaited()
        live_stop.assert_awaited()
        self.assertNotIn(cid, session._clients)

    async def test_a_cached_session_is_not_handed_out_once_switched_off(self):
        cid = await self.verified_camera("10.0.0.53")
        session._clients[cid] = object()          # a stale cache entry (a request that captured the camera earlier)
        stale = dict(config.CAMS[cid], enabled=True)
        config.CAMS[cid] = {**config.CAMS[cid], "enabled": False}
        with self.assertRaises(BadInput):
            session.client(stale)
        self.assertEqual(self.attempts("10.0.0.53"), 1)

    async def test_failed_verify_drops_the_cached_session(self):
        made = await self.add_cam(ip="10.0.0.54")
        self.fake.add_device("10.0.0.54", CLOUD_PW)
        dev = self.fake.devices["10.0.0.54"]
        dev.login_error = "Error: Temporary Suspension: Try again in 300 seconds"
        self.assertEqual((await self.verify(made["id"]))["state"], "failed")
        self.assertNotIn(made["id"], session._clients)
        self.assertFalse(config.CAMS[made["id"]]["enabled"])


class StaleSettings(SetupCase):
    async def test_a_login_with_settings_changed_while_it_waited_is_retried_not_run(self):
        await self.set_account()
        made = await self.add_cam(ip="10.0.0.55")
        cid = made["id"]
        self.fake.add_device("10.0.0.55", CLOUD_PW)
        self.assertEqual((await self.verify(cid))["state"], "ok")
        captured = config.CAMS[cid]
        await cameras.update(cid, {"ip": "10.0.0.56", "enabled": True, "verified": True})
        with self.assertRaises(Unavailable):
            session.client(captured)              # the request captured the old address
        self.assertEqual(self.attempts("10.0.0.55"), 1)
        self.assertEqual(self.attempts("10.0.0.56"), 0)


class KeyringDenied(SetupCase):
    """A keyring that refuses a read is an error to show, never "no secret": no login, no counted failure, no legacy item."""
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.set_account()

    async def test_denied_keyring_read_refuses_without_login_or_failure_count(self):
        made = await self.add_cam(ip="10.0.0.57")
        cid = made["id"]
        self.fake.add_device("10.0.0.57", CLOUD_PW)
        with mock.patch.object(self.kr, "get_password", side_effect=KeyringError("denied")), \
                mock.patch.dict(os.environ, {"TAPO_PASS": CAM_PW}):
            creds.get_credentials.cache_clear()   # the legacy item must not be used for this camera
            try:
                body = await self.verify(cid)
            finally:
                creds.get_credentials.cache_clear()
        self.assertEqual(body["state"], "failed")
        self.assertIn("could not be read", body["reason"])
        self.assertEqual(self.attempts("10.0.0.57"), 0)
        st = self.gate_state(cid)
        self.assertEqual(st.get("recent_fails", []), [])
        self.assertEqual(st.get("logins", []), [])
        self.assertEqual(st.get("fails", 0), 0)
        self.assertFalse(config.CAMS[cid]["enabled"])

    async def test_no_keyring_backend_holds_nothing_and_the_legacy_env_is_not_used(self):
        made = await self.add_cam(ip="10.0.0.58", password=None)
        cam = config.CAMS[made["id"]]
        with mock.patch.object(self.kr, "get_password", side_effect=NoKeyringError("none")), \
                mock.patch.dict(os.environ, {"TAPO_PASS": CAM_PW, "TAPO_CLOUD_PASS": CLOUD_PW}):
            with self.assertRaises(RuntimeError):
                creds.get_camera_credentials(cam)
            self.assertEqual(creds.secret_sources(cam)["camera"], "missing")


class AuthSwitch(SetupCase):
    """A login-method change: one call with the secrets (or PATCH alone) forgets the stored transport only."""
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.set_account()

    def set_gate(self, cid, **fields):
        cam = config.CAMS[cid]
        with gate._gate_lock():
            gate._reload([cam])
            gate.st(cam).update(fields)
            gate._save()

    async def verified(self, ip):
        made = await self.add_cam(ip=ip, model="C200")
        self.fake.add_device(ip, CLOUD_PW)
        self.assertEqual((await self.verify(made["id"]))["state"], "ok")
        return made["id"]

    async def test_one_call_switches_the_login_method_and_secret(self):
        cid = await self.verified("10.0.0.71")
        self.set_gate(cid, transport="tpap", logins=[])
        body = await self.ok("POST", f"/api/cameras/{cid}/credentials",
                             {"camera_password": CAM_PW, "auth": "admin_cloud"})
        self.assertEqual(body, {"ok": True, "reset_auth": True})
        cam = config.CAMS[cid]
        self.assertEqual((cam["auth"], cam["enabled"], cam["verified"]), ("admin_cloud", False, False))
        self.assertNotIn("transport", self.gate_state(cid))
        status, text = await self.call("POST", f"/api/cameras/{cid}/credentials", {"auth": "nope"})
        self.assertEqual(status, 400)

    async def test_patch_auth_alone_forgets_only_the_transport(self):
        cid = await self.verified("10.0.0.72")
        self.set_gate(cid, transport="tpap", disabled="kept reason", fails=1, blocked_until=0)
        await self.ok("PATCH", f"/api/cameras/{cid}", {"auth": "admin_cloud"})   # a different login method
        st = self.gate_state(cid)
        self.assertNotIn("transport", st)
        self.assertEqual((st["disabled"], st["fails"]), ("kept reason", 1))


class LanAddress(SetupCase):
    async def test_add_and_patch_refuse_addresses_outside_the_home_network(self):
        status, text = await self.call("POST", "/api/setup/cameras",
                                       {"name": "Out", "ip": "8.8.8.8", "model": "C200", "camera_password": CAM_PW})
        self.assertEqual(status, 400)
        self.assertIn("private address", text)
        made = await self.add_cam(ip="10.0.0.73")
        status, text = await self.call("PATCH", f"/api/cameras/{made['id']}", {"ip": "203.0.113.9"})
        self.assertEqual(status, 400)
        self.assertIn("private address", text)
        self.assertEqual(config.CAMS[made["id"]]["ip"], "10.0.0.73")


class RemoveWhileBusy(SetupCase):
    async def test_remove_refuses_while_a_clip_or_export_holds_the_camera(self):
        await self.set_account()
        made = await self.add_cam(ip="10.0.0.74")
        cid = made["id"]
        await cameras.slots.lock(cid).acquire()
        try:
            status, text = await self.call("DELETE", f"/api/cameras/{cid}", {"confirm": cid})
            self.assertEqual(status, 409)
            self.assertIn("clip or export", text)
            self.assertIn(cid, config.CAMS)
        finally:
            cameras.slots.lock(cid).release()
        body = await self.ok("DELETE", f"/api/cameras/{cid}", {"confirm": cid})
        self.assertNotIn(cid, config.CAMS)
        self.assertTrue(body["ok"])


class RegistryWrites(SetupCase):
    async def test_cameras_json_is_written_in_a_worker_thread(self):
        threads = []
        real = cameras._persist

        def spy(cams, tombs):
            threads.append(threading.current_thread() is threading.main_thread())
            return real(cams, tombs)
        with mock.patch.object(cameras, "_persist", spy):
            await self.add_cam(ip="10.0.0.75")
        self.assertEqual(threads, [False])



OWN_USER = "cam-own-user"
OWN_PW = SENTINEL + "-own"
LEGACY_PW = SENTINEL + "-legacy"
ACCOUNT_REFUSED = "A camera account belongs to one camera: enter it when you add the camera"


class CameraAccounts(SetupCase):
    """Each camera has its own camera account (its own user and password from the Tapo app). The TP-Link account is the
    only shared account. A camera with a secret of its own never uses the global or legacy secrets."""
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.set_account()

    async def test_the_shared_camera_pair_is_refused_and_stores_nothing(self):
        before = dict(self.kr.store)
        for body in ({"camera_user": OWN_USER, "camera_password": OWN_PW},
                     {"email": EMAIL, "password": CLOUD_PW, "camera_user": OWN_USER, "camera_password": OWN_PW},
                     {"camera_user": OWN_USER}, {"camera_password": OWN_PW}):
            status, text = await self.call("POST", "/api/setup/account", body)
            self.assertEqual(status, 400, body)
            self.assertNotIn(SENTINEL, text)
            self.assertNotIn("{", text)                    # plain text
        status, text = await self.call("POST", "/api/setup/account", {"camera_user": OWN_USER, "camera_password": OWN_PW})
        self.assertEqual((status, text), (400, ACCOUNT_REFUSED))
        self.assertEqual(self.kr.store, before)
        self.assertNotIn("camera_default_user", json.loads((self.home / "settings.json").read_text()))

    async def test_tplink_pair_alone_is_still_accepted_and_partial_tplink_is_400(self):
        body = await self.ok("POST", "/api/setup/account", {"email": EMAIL, "password": CLOUD_PW})
        self.assertEqual(body, {"ok": True, "saved": ["tplink"]})
        for body in ({"email": EMAIL}, {"password": CLOUD_PW}, {}):
            status, text = await self.call("POST", "/api/setup/account", body)
            self.assertEqual(status, 400, body)
            self.assertNotIn(SENTINEL, text)

    async def test_state_has_no_shared_camera_account(self):
        state = await self.ok("GET", "/api/setup/state")
        self.assertNotIn("shared_camera_account", state)
        self.assertEqual(state["step"], "prepare")

    async def test_half_a_camera_account_is_400_and_adds_nothing(self):
        for body in ({"name": "Kitchen", "ip": "10.0.0.82", "camera_user": OWN_USER},
                     {"name": "Kitchen", "ip": "10.0.0.82", "camera_password": OWN_PW}):
            status, text = await self.call("POST", "/api/setup/cameras", body)
            self.assertEqual(status, 400)
            self.assertNotIn(OWN_PW, text)
        self.assertEqual(config.CAMS, {})
        self.assertFalse([k for k in self.kr.store if k[1].startswith("camera:")])

    async def test_camera_account_is_stored_per_camera_with_the_user_given(self):
        made = await self.add_cam(ip="10.0.0.81")
        self.assertEqual(creds.get_camera_credentials(config.CAMS[made["id"]]), ("rtspadmin", CAM_PW))
        made = await self.add_cam(name="Hall", ip="10.0.0.80", camera_user=OWN_USER, password=OWN_PW)
        self.assertEqual(creds.get_camera_credentials(config.CAMS[made["id"]]), (OWN_USER, OWN_PW))
        state = await self.ok("GET", "/api/setup/state")
        self.assertEqual([c["secret_sources"]["camera"] for c in state["cameras"]], ["wizard", "wizard"])

    async def test_automatic_cameras_sign_in_with_the_tplink_password_and_cam_cloud_with_its_account(self):
        """An automatic camera never uses its camera account for the login; a cam_cloud camera does."""
        first = await self.add_cam(name="Kitchen", ip="10.0.0.83", password=CAM_PW)
        second = await self.add_cam(name="Hall", ip="10.0.0.84", camera_user=OWN_USER, password=OWN_PW, auth="cam_cloud")
        self.fake.add_device("10.0.0.83", CLOUD_PW)
        self.fake.add_device("10.0.0.84", OWN_PW)
        self.assertEqual((await self.verify(first["id"]))["state"], "ok")
        self.assertEqual((await self.verify(second["id"]))["state"], "ok")
        self.assertEqual((self.attempts("10.0.0.83"), self.attempts("10.0.0.84")), (1, 1))

    async def test_a_camera_own_secret_is_never_replaced_by_the_legacy_global_secret(self):
        made = await self.add_cam(name="Living", ip="10.0.0.85", camera_user=OWN_USER, password=OWN_PW)
        cam = config.CAMS[made["id"]]
        with mock.patch.dict(os.environ, {"TAPO_PASS": LEGACY_PW}):
            creds.get_credentials.cache_clear()
            try:
                self.assertEqual(creds.get_camera_credentials(cam), (OWN_USER, OWN_PW))
                self.assertEqual(creds.secret_sources(cam)["camera"], "wizard")
            finally:
                creds.get_credentials.cache_clear()

    async def test_legacy_env_is_refused_without_the_dev_switch(self):
        cam = {"id": "legacy_c", "name": "Legacy"}          # an older camera: no wizard secret
        with mock.patch.dict(os.environ, {"TAPO_PASS": LEGACY_PW}):
            creds.get_credentials.cache_clear()
            try:
                with self.assertRaises(RuntimeError) as cm:
                    creds.get_camera_credentials(cam)
                self.assertNotIn(LEGACY_PW, str(cm.exception))
                self.assertEqual(creds.secret_sources(cam)["camera"], "missing")
            finally:
                creds.get_credentials.cache_clear()

    async def test_dev_switch_uses_the_legacy_env_only_when_the_camera_has_no_secret_of_its_own(self):
        cam = {"id": "legacy_c", "name": "Legacy"}          # an older camera: no wizard secret
        with mock.patch.dict(os.environ, {"TAPO_PASS": LEGACY_PW, "KINORA_DEV_SECRETS": "1"}):
            creds.get_credentials.cache_clear()
            try:
                self.assertEqual(creds.get_camera_credentials(cam), ("rtspadmin", LEGACY_PW))
                self.assertEqual(creds.secret_sources(cam)["camera"], "dev_override")
                creds.set_camera_secret(cam, OWN_USER, OWN_PW)
                self.assertEqual(creds.get_camera_credentials(cam), (OWN_USER, OWN_PW))
                self.assertEqual(creds.secret_sources(cam)["camera"], "wizard")
            finally:
                creds.get_credentials.cache_clear()

    async def test_a_refused_keyring_read_refuses_without_falling_back(self):
        made = await self.add_cam(ip="10.0.0.86")
        with mock.patch.object(self.kr, "get_password", side_effect=KeyringError("denied")), \
                mock.patch.dict(os.environ, {"TAPO_PASS": LEGACY_PW}):
            creds.get_credentials.cache_clear()
            try:
                with self.assertRaises(RuntimeError):
                    creds.get_camera_credentials(config.CAMS[made["id"]])
            finally:
                creds.get_credentials.cache_clear()

    async def test_no_secret_value_is_in_any_state_response(self):
        await self.add_cam(ip="10.0.0.87", camera_user=OWN_USER, password=OWN_PW)
        status, text = await self.call("GET", "/api/setup/state")
        self.assertEqual(status, 200)
        self.assertNotIn(OWN_PW, text)
        self.assertNotIn(CLOUD_PW, text)
        self.assertNotIn(SENTINEL, text)


class ModelPreset(SetupCase):
    """PATCH model / apply_preset: flags follow the model preset; auth, enabled and verified do not change; playback
    stops; a running export refuses; a plain model change only records the model."""
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.set_account()

    async def c100_reporting_c200(self):
        """A camera whose manual model (C100) disagrees with what the device reports (C200)."""
        made = await self.add_cam(name="Kitchen", ip="10.0.0.91")
        cid = made["id"]
        self.fake.add_device("10.0.0.91", CLOUD_PW, model="C200")   # automatic sign-in uses the TP-Link password
        body = await self.verify(cid)                                # first Verify: the C200 presets from the device
        self.assertEqual(body["state"], "ok")
        self.assertFalse(body["mismatch"])
        await self.ok("PATCH", f"/api/cameras/{cid}", {"model": "C100", "apply_preset": True})   # a wrong manual model
        await self.ok("PATCH", f"/api/cameras/{cid}", {"flags": {"ptz_step_deg": 20, "win": 45}})
        return cid

    async def test_apply_preset_resets_the_flags_and_keeps_auth_enabled_and_verified(self):
        cid = await self.c100_reporting_c200()
        cam = config.CAMS[cid]
        self.assertEqual((cam["auth"], cam["enabled"], cam["verified"]), ("auto", True, True))
        self.assertTrue((await self.ok("GET", "/api/setup/state"))["cameras"][0]["model_mismatch"])
        logins = self.attempts("10.0.0.91")
        with mock.patch.object(play, "stop", new=mock.AsyncMock()) as play_stop, \
                mock.patch.object(live, "stop", new=mock.AsyncMock()) as live_stop:
            body = await self.ok("PATCH", f"/api/cameras/{cid}", {"model": "C200", "apply_preset": True})
        play_stop.assert_awaited()
        live_stop.assert_awaited()
        cam = config.CAMS[cid]
        self.assertEqual(cam["model"], "C200")
        self.assertEqual((cam["auth"], cam["enabled"], cam["verified"]), ("auto", True, True))
        self.assertTrue(cam["ptz"])
        self.assertTrue(cam["fast_forward"])
        for key in ("p2p", "stream_playback", "thumbs", "win", "ptz_step_deg", "ptz_invert"):
            self.assertNotIn(key, cam)                    # the C200 preset does not set it: the default applies
        self.assertFalse(body["auth_differs_from_preset"])  # automatic sign-in: the preset names no sign-in method
        self.assertEqual(body["model"], "C200")
        self.assertEqual(body["flags"], {"ptz": True, "fast_forward": True})
        self.assertEqual(self.attempts("10.0.0.91"), logins)  # no login
        self.assertFalse((await self.ok("GET", "/api/setup/state"))["cameras"][0]["model_mismatch"])

    async def test_apply_preset_other_uses_the_c200_flags_and_never_the_auth(self):
        cid = await self.c100_reporting_c200()
        body = await self.ok("PATCH", f"/api/cameras/{cid}", {"model": "other", "apply_preset": True})
        cam = config.CAMS[cid]
        self.assertEqual((cam["model"], cam["auth"]), ("other", "auto"))
        self.assertTrue(cam["ptz"])
        self.assertNotIn("p2p", cam)
        self.assertFalse(body["auth_differs_from_preset"])     # other has no preset auth

    async def test_model_without_apply_only_records_the_model(self):
        cid = await self.c100_reporting_c200()
        before = copy.deepcopy(config.CAMS[cid])
        await self.ok("PATCH", f"/api/cameras/{cid}", {"model": "C210"})
        after = copy.deepcopy(config.CAMS[cid])
        self.assertEqual(after.pop("model"), "C210")
        before.pop("model")
        self.assertEqual(after, before)
        self.assertTrue((await self.ok("GET", "/api/setup/state"))["cameras"][0]["model_mismatch"])

    async def test_a_running_export_refuses_a_playback_change_with_409(self):
        cid = await self.c100_reporting_c200()
        export.jobs["job1"] = {"cam": cid, "state": "running"}
        before = copy.deepcopy(config.CAMS[cid])
        with mock.patch.object(play, "stop", new=mock.AsyncMock()) as play_stop:
            status, text = await self.call("PATCH", f"/api/cameras/{cid}", {"model": "C200", "apply_preset": True})
        self.assertEqual(status, 409)
        self.assertNotIn("{", text)
        play_stop.assert_not_awaited()
        self.assertEqual(config.CAMS[cid], before)
        body = await self.ok("PATCH", f"/api/cameras/{cid}", {"name": "Kitchen two"})   # not a playback change
        self.assertEqual(body["name"], "Kitchen two")

    async def test_bad_model_and_bad_apply_are_400(self):
        cid = await self.c100_reporting_c200()
        for body in ({"model": "C999"}, {"model": ["C200"]}, {"apply_preset": "yes"}):
            status, text = await self.call("PATCH", f"/api/cameras/{cid}", body)
            self.assertEqual(status, 400, body)
            self.assertNotIn("{", text)
        await cameras.add({"id": "nomodel_1", "name": "No model", "ip": "10.0.0.92", "auth": "cam_cloud"})
        status, text = await self.call("PATCH", "/api/cameras/nomodel_1", {"apply_preset": True})
        self.assertEqual(status, 400)
        self.assertIn("model", text)
        self.assertEqual(self.attempts("10.0.0.91"), 1)


class AutoSignIn(SetupCase):
    """`auto`: the login pytapo makes on its own (user admin, the TP-Link password). No camera account, no model and no
    sign-in question. Gate limits are the same as for admin_cloud."""
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.set_account()

    async def test_auto_builds_the_tplink_login_on_both_transports(self):
        made = await self.add_cam(ip="10.0.0.41")
        cam = config.CAMS[made["id"]]
        for transport in ("tpap", "pytapo"):
            gate.remember_transport(cam, transport)
            with mock.patch.object(session, "Tapo") as tapo:
                session.build(cam)
            args, kwargs = tapo.call_args
            self.assertEqual(args, ("10.0.0.41", "admin", CLOUD_PW))
            self.assertEqual((kwargs["cloudPassword"], kwargs["transportMethod"], kwargs["isKLAP"]),
                             (CLOUD_PW, transport, False))

    async def test_auto_logs_in_to_a_tpap_and_a_legacy_camera_with_the_tplink_password(self):
        for ip, transport in (("10.0.0.42", "tpap"), ("10.0.0.43", "pytapo")):
            made = await self.add_cam(name=f"Cam {transport}", ip=ip)
            self.fake.add_device(ip, CLOUD_PW)
            gate.remember_transport(config.CAMS[made["id"]], transport)
            body = await self.verify(made["id"])
            self.assertEqual(body["state"], "ok", body)
            self.assertEqual(self.attempts(ip), 1)
        self.assertEqual(self.fake.connects_refused, [])

    async def test_first_verify_sends_the_same_login_phase_requests_as_admin_cloud(self):
        counts = {}
        for ip, auth in (("10.0.0.44", "admin_cloud"), ("10.0.0.45", "auto")):
            made = await self.add_cam(name=f"Cam {auth}", ip=ip, auth=auth)
            self.fake.add_device(ip, CLOUD_PW)
            with mock.patch.object(pytapo, "discover_login", return_value=None) as discover:
                body = await self.verify(made["id"])
            self.assertEqual(body["state"], "ok")
            counts[auth] = (self.attempts(ip), sum(1 for c in discover.call_args_list if c.args[0] == ip))
        self.assertEqual(counts["auto"], counts["admin_cloud"])
        self.assertEqual(counts["auto"], (1, 1))

    async def test_a_stored_transport_means_no_discovery_probe(self):
        made = await self.add_cam(ip="10.0.0.46")
        self.fake.add_device("10.0.0.46", CLOUD_PW)
        gate.remember_transport(config.CAMS[made["id"]], "pytapo")
        with mock.patch.object(pytapo, "discover_login", return_value=None) as discover:
            body = await self.verify(made["id"])
        self.assertEqual(body["state"], "ok")
        self.assertEqual(discover.call_count, 0)
        self.assertEqual(self.attempts("10.0.0.46"), 1)

    async def test_invalid_authentication_on_auto_disables_with_the_plain_wording(self):
        made = await self.add_cam(ip="10.0.0.47")
        cid = made["id"]
        self.fake.add_device("10.0.0.47", CLOUD_PW)
        await self.ok("POST", "/api/setup/account", {"email": EMAIL, "password": "WRONG-" + SENTINEL})
        body = await self.verify(cid)
        wording = "The camera refused the TP-Link password"
        self.assertEqual(body["state"], "failed")
        self.assertIn(wording, body["reason"])
        self.assertNotIn("python -m", body["reason"])
        self.assertNotIn("cameras.json", body["reason"])
        self.assertFalse(config.CAMS[cid]["enabled"])
        self.assertEqual(len(self.gate_state(cid)["recent_fails"]), 1)
        self.assertIn(wording, self.gate_state(cid)["disabled"])
        state = await self.ok("GET", "/api/setup/state")
        self.assertIn(wording, state["cameras"][0]["gate_reason"])
        body = await self.verify(cid)            # inside 30 min: refused locally, nothing reaches the camera
        self.assertEqual(body["state"], "failed")
        self.assertEqual(self.attempts("10.0.0.47"), 1)
        self.assertEqual(self.fake.connects_refused, [])

    async def test_40211_and_a_stored_tpap_conflict_both_forget_the_transport(self):
        for ip, transport, error in (("10.0.0.48", "pytapo", Exception("Error -40211")),
                                     ("10.0.0.49", "tpap", TpapError(-40212, "pake_register failed"))):
            made = await self.add_cam(name=f"Cam {ip}", ip=ip)
            cid = made["id"]
            dev = self.fake.add_device(ip, CLOUD_PW)
            gate.remember_transport(config.CAMS[cid], transport)
            dev.login_error = error
            body = await self.verify(cid)
            self.assertEqual(body["state"], "failed")
            state = self.gate_state(cid)
            self.assertNotIn("transport", state)
            self.assertIn("Save the TP-Link password again in Settings", state["disabled"])
            self.assertFalse(config.CAMS[cid]["enabled"])
            self.assertNotIn("error_code", state["disabled"])

    async def test_use_automatic_sign_in_switches_back_without_a_login(self):
        made = await self.add_cam(ip="10.0.0.107", auth="cam_cloud")
        cid = made["id"]
        body = await self.ok("POST", f"/api/cameras/{cid}/credentials", {"auth": "auto", "reset_auth": True})
        self.assertEqual(body, {"ok": True, "reset_auth": True})
        cam = config.CAMS[cid]
        self.assertEqual((cam["auth"], cam["enabled"], cam["verified"]), ("auto", False, False))
        self.assertEqual(self.attempts("10.0.0.107"), 0)

    async def test_live_without_a_camera_account_says_how_to_add_one(self):
        made = await self.ok("POST", "/api/setup/cameras", {"name": "Hall", "ip": "10.0.0.108"})
        with self.assertRaises(BadInput) as ctx:
            await live._start(config.LIVE_ROOT, config.CAMS[made["id"]])
        self.assertEqual(str(ctx.exception), "Add this camera's camera account in Settings to use Live view")


class DetectedModel(SetupCase):
    """The model is read from the device at the first successful Verify. Its preset flags go into the same registry write
    as enabled and verified. A re-Verify never applies them again, and a flag the camera already has is never overwritten."""
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.set_account()

    async def verify_device(self, ip, model):
        made = await self.add_cam(ip=ip)
        self.fake.add_device(ip, CLOUD_PW, model=model)
        return made["id"]

    async def test_exact_c100_sets_the_baby_monitor_flags_and_says_so(self):
        cid = await self.verify_device("10.0.0.101", "C100")
        body = await self.verify(cid)
        self.assertEqual(body["note"], "Baby-monitor mode applied (hardware 1.0)")
        cam = config.CAMS[cid]
        self.assertEqual(cam["model"], "C100")
        self.assertEqual({k: cam[k] for k in ("p2p", "stream_playback", "thumbs", "win", "fast_forward")},
                         {"p2p": True, "stream_playback": True, "thumbs": False, "win": 30, "fast_forward": True})
        self.assertNotIn("ptz", cam)
        self.assertNotIn("model_note", cam)
        self.assertEqual((cam["enabled"], cam["verified"]), (True, True))

    async def test_c1000_is_not_c100_and_gets_the_standard_settings_with_a_note(self):
        cid = await self.verify_device("10.0.0.102", "C1000")
        body = await self.verify(cid)
        cam = config.CAMS[cid]
        self.assertEqual(body["note"], "Unknown model: using standard settings")
        self.assertEqual((cam["model"], cam["model_note"]), ("C1000", "Unknown model: using standard settings"))
        self.assertEqual((cam["p2p"], cam["stream_playback"], cam["ptz"], cam["fast_forward"]), (False, False, True, True))
        self.assertNotIn("thumbs", cam)
        state = await self.ok("GET", "/api/setup/state")
        self.assertEqual(state["cameras"][0]["model_note"], "Unknown model: using standard settings")

    async def test_c210_and_c200_get_the_pan_tilt_flags_and_no_baby_monitor_flags(self):
        for ip, device, model in (("10.0.0.103", "Tapo C210", "C210"), ("10.0.0.104", "C200", "C200")):
            made = await self.add_cam(ip=ip)
            self.fake.add_device(ip, CLOUD_PW, model=device)
            body = await self.verify(made["id"])
            cam = config.CAMS[made["id"]]
            self.assertEqual((cam["model"], cam["ptz"], cam["fast_forward"]), (model, True, True))
            self.assertNotIn("p2p", cam)
            self.assertNotIn("note", body)

    async def test_a_re_verify_never_reapplies_or_overwrites_flags(self):
        cid = await self.verify_device("10.0.0.105", "C200")
        await self.verify(cid)
        await self.ok("PATCH", f"/api/cameras/{cid}", {"flags": {"ptz": False}})
        await self.ok("PATCH", f"/api/cameras/{cid}", {"enabled": False})
        self.clock.advance(181)
        self.fake.devices["10.0.0.105"].model = "C100"        # the device now reports another model
        body = await self.verify(cid)
        self.assertEqual(body["state"], "ok")
        cam = config.CAMS[cid]
        self.assertEqual(cam["model"], "C200")
        self.assertFalse(cam["ptz"])
        self.assertNotIn("p2p", cam)
        self.assertTrue(body["mismatch"])

    async def test_the_presets_land_in_the_same_write_as_verified(self):
        cid = await self.verify_device("10.0.0.106", "C100")
        real = cameras.update
        writes = []

        async def spy(cid_, changes):
            writes.append(dict(changes))
            return await real(cid_, changes)
        with mock.patch.object(cameras, "update", spy):
            body = await self.verify(cid)
        self.assertEqual(body["state"], "ok")
        self.assertEqual(len(writes), 1)
        self.assertTrue(writes[0]["enabled"] and writes[0]["verified"])
        self.assertEqual((writes[0]["model"], writes[0]["p2p"]), ("C100", True))


class TplinkSaved(SetupCase):
    """Saving the TP-Link password again clears only `disabled` and `fails` of the cameras that sign in with it. Blocks,
    the Live refusal, the failure window and cam_cloud cameras are not touched."""
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.set_account()

    async def save_tplink(self, password):
        return await self.ok("POST", "/api/setup/account", {"email": EMAIL, "password": password})

    async def test_corrected_tplink_password_makes_the_camera_verifiable_again_after_the_block(self):
        made = await self.add_cam(ip="10.0.0.110")
        cid = made["id"]
        self.fake.add_device("10.0.0.110", CLOUD_PW)
        await self.save_tplink("WRONG-" + SENTINEL)
        self.assertEqual((await self.verify(cid))["state"], "failed")
        self.assertIn("refused the TP-Link password", self.gate_state(cid)["disabled"])
        await self.save_tplink(CLOUD_PW)
        s = self.gate_state(cid)
        self.assertIsNone(s["disabled"])
        self.assertEqual(s["fails"], 0)
        self.assertEqual(len(s["recent_fails"]), 1)          # the 30 min record is kept
        body = await self.verify(cid)                        # the 180 s spacing is still enforced: no request
        self.assertEqual(body["state"], "waiting")
        self.assertEqual(self.attempts("10.0.0.110"), 1)
        self.clock.advance(181)
        self.assertEqual((await self.verify(cid))["state"], "ok")
        self.assertEqual(self.attempts("10.0.0.110"), 2)

    async def test_a_second_failure_after_the_fix_still_blocks_32_minutes(self):
        made = await self.add_cam(ip="10.0.0.111")
        cid = made["id"]
        self.fake.add_device("10.0.0.111", CLOUD_PW)
        await self.save_tplink("WRONG-" + SENTINEL)
        self.assertEqual((await self.verify(cid))["state"], "failed")          # failure 1
        await self.save_tplink("STILL-WRONG-" + SENTINEL)
        self.clock.advance(181)
        self.assertEqual((await self.verify(cid))["state"], "failed")          # failure 2, inside 30 min
        self.assertEqual(self.attempts("10.0.0.111"), 2)
        self.assertEqual(len(self.gate_state(cid)["recent_fails"]), 2)
        await self.save_tplink(CLOUD_PW)                                       # the fix clears `disabled` only
        body = await self.verify(cid)
        self.assertEqual(body["state"], "waiting")
        self.assertGreater(body["wait_s"], 1900)
        self.assertEqual(self.attempts("10.0.0.111"), 2)                       # zero requests while blocked
        self.clock.advance(1921)
        self.assertEqual((await self.verify(cid))["state"], "ok")
        self.assertEqual(self.attempts("10.0.0.111"), 3)

    async def test_live_refusal_and_cam_cloud_cameras_are_not_touched(self):
        made = await self.add_cam(ip="10.0.0.112")
        cid = made["id"]
        self.fake.add_device("10.0.0.112", CLOUD_PW)
        await self.save_tplink("WRONG-" + SENTINEL)
        await self.verify(cid)
        gate.note_rtsp_auth_failure(config.CAMS[cid])
        await self.save_tplink(CLOUD_PW)
        s = self.gate_state(cid)
        self.assertIsNone(s["disabled"])
        self.assertIn("Live view disabled", s["live_disabled"])

        cloud = await self.add_cam(name="Legacy", ip="10.0.0.113", auth="cam_cloud", password=CAM_PW)
        self.fake.add_device("10.0.0.113", "WRONG-" + CAM_PW)              # the camera refuses its account password
        body = await self.verify(cloud["id"])
        self.assertEqual(body["state"], "failed")
        self.assertIn("refused the camera account password", body["reason"])
        self.assertIn("Use automatic sign-in", body["reason"])
        self.assertNotIn("refused the TP-Link password", body["reason"])
        await self.save_tplink(CLOUD_PW)
        self.assertIn("camera account password", self.gate_state(cloud["id"])["disabled"])
        self.assertNotIn("refused the TP-Link password", self.gate_state(cloud["id"])["disabled"])
        status = await self.ok("GET", "/api/setup/state")
        self.assertIn("camera account password", [c for c in status["cameras"] if c["id"] == cloud["id"]][0]["gate_reason"])


if __name__ == "__main__":
    unittest.main()
