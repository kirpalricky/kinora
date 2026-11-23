"""Gate hardening: cross-process lock, device identity, credentials-changed, RTSP flag (no camera, no network).

The real cache/meta/breaker.json is never touched: BREAKER_F and the config home point at temp dirs.
Run: .venv/bin/python -m unittest tests.test_gate_hardening   (from the repo root)
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from kinora import config  # noqa: E402
from kinora.camera import gate  # noqa: E402

CAM = {"id": "cT", "name": "Test", "ip": "10.9.9.9"}
T0 = 1_000_000.0
RACE_PROCS, RACE_LOGINS = 2, 20

# A child process that performs RACE_LOGINS gated logins against a shared breaker file. No camera is contacted:
# the gate only counts; the limits are relaxed so every attempt is allowed and the count is the thing under test.
CHILD = r"""
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from kinora.camera import gate
gate.BREAKER_F = Path(sys.argv[2])
gate.MIN_LOGIN_GAP = 0
gate.MAX_LOGINS_PER_HOUR = 10 ** 6
gate.health.unreachable = lambda cid: False
cam = {"id": "cR", "name": "Race", "ip": "10.9.9.7"}
for _ in range(int(sys.argv[3])):
    gate.gate(cam)
"""


class GateEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.now = T0
        for p in (
            mock.patch.object(gate, "BREAKER_F", self.dir / "breaker.json"),
            mock.patch.object(gate.health, "unreachable", lambda cid: False),
            mock.patch.object(gate.time, "time", lambda: self.now),
        ):
            p.start()
            self.addCleanup(p.stop)
        saved = dict(gate._state)
        gate._state.clear()
        self.addCleanup(lambda: (gate._state.clear(), gate._state.update(saved)))

    def seed(self, data):
        gate.BREAKER_F.write_text(json.dumps(data))

    def disk(self):
        return json.loads(gate.BREAKER_F.read_text())

    def write_cameras(self, cams):
        (self.dir / "cameras.json").write_text(json.dumps({"version": 1, "cameras": cams}))

    def lock_is_free(self):
        """True when nobody holds breaker.lock (a non-blocking OS lock succeeds)."""
        f = open(gate.BREAKER_F.parent / "breaker.lock", "a+b")
        try:
            gate._os_lock(f, blocking=False)
            gate._os_unlock(f)
            return True
        except OSError:
            return False
        finally:
            f.close()


class CrossProcessLock(GateEnv):
    def test_two_processes_never_lose_a_login(self):
        procs = [subprocess.Popen([sys.executable, "-c", CHILD, str(ROOT), str(gate.BREAKER_F), str(RACE_LOGINS)],
                                  cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(RACE_PROCS)]
        for p in procs:
            out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err.decode(errors="replace")[-500:])
        self.assertEqual(len(self.disk()["cR"]["logins"]), RACE_PROCS * RACE_LOGINS)

    def test_lock_released_when_save_raises(self):
        with mock.patch.object(gate, "_save", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                gate.gate(CAM)
        self.assertFalse(gate._state_lock.locked())
        self.assertTrue(self.lock_is_free())

    def test_lock_released_when_login_is_refused(self):
        gate.gate(CAM)
        with self.assertRaises(gate.GateRefused):
            gate.gate(CAM)   # inside the 180 s gap
        self.assertFalse(gate._state_lock.locked())
        self.assertTrue(self.lock_is_free())

    def test_meta_dir_is_created_for_the_lock(self):
        nested = self.dir / "missing" / "meta"
        with mock.patch.object(gate, "BREAKER_F", nested / "breaker.json"):
            gate.precheck(CAM)
        self.assertTrue((nested / "breaker.lock").exists())


class DeviceIdentity(GateEnv):
    def test_gate_key_prefers_device_id(self):
        self.assertEqual(gate.gate_key({"id": "c1", "device_id": "dev-1"}), "dev-1")
        self.assertEqual(gate.gate_key({"id": "c1"}), "c1")
        self.assertEqual(gate.gate_key({"id": "c1", "device_id": ""}), "c1")

    def test_state_is_stored_under_gate_key(self):
        gate.gate({"id": "c1", "name": "Cam", "ip": "10.0.0.1", "device_id": "dev-1"})
        self.assertIn("dev-1", self.disk())
        self.assertNotIn("c1", self.disk())

    def test_ip_change_keeps_the_budget(self):
        cam = {"id": "c1", "name": "Cam", "ip": "10.0.0.1", "device_id": "dev-1", "auth": "cam_cloud", "enabled": True}
        self.write_cameras([cam])
        with mock.patch.object(config, "HOME", self.dir), \
             mock.patch.dict(config.CAMS, {}, clear=True), mock.patch.dict(config.CAM_BY_IP, {}, clear=True):
            config.reload_cameras()
            gate.gate(config.CAM_BY_IP["10.0.0.1"])
            self.write_cameras([{**cam, "ip": "10.0.0.2"}])   # DHCP moved the camera
            config.reload_cameras()
            with self.assertRaisesRegex(gate.GateRefused, "last login"):
                gate.gate(config.CAM_BY_IP["10.0.0.2"])
            self.now += gate.MIN_LOGIN_GAP + 1
            gate.gate(config.CAM_BY_IP["10.0.0.2"])
        self.assertEqual(len(self.disk()["dev-1"]["logins"]), 2)

    def test_swapped_ips_do_not_swap_state(self):
        a = {"id": "a1", "name": "A", "ip": "10.0.0.1", "device_id": "dev-a", "auth": "cam_cloud", "enabled": True}
        b = {"id": "b1", "name": "B", "ip": "10.0.0.2", "device_id": "dev-b", "auth": "cam_cloud", "enabled": True}
        self.write_cameras([a, b])
        with mock.patch.object(config, "HOME", self.dir), \
             mock.patch.dict(config.CAMS, {}, clear=True), mock.patch.dict(config.CAM_BY_IP, {}, clear=True):
            config.reload_cameras()
            gate.gate(config.CAM_BY_IP["10.0.0.1"])                  # A logs in
            self.write_cameras([{**a, "ip": "10.0.0.2"}, {**b, "ip": "10.0.0.1"}])
            config.reload_cameras()
            gate.gate(config.CAM_BY_IP["10.0.0.1"])                  # now B: no gap owed by B's state
            with self.assertRaisesRegex(gate.GateRefused, "last login"):
                gate.gate(config.CAM_BY_IP["10.0.0.2"])              # now A: its gap still applies
        data = self.disk()
        self.assertEqual(len(data["dev-a"]["logins"]), 1)
        self.assertEqual(len(data["dev-b"]["logins"]), 1)

    def test_reload_failure_leaves_tables_untouched(self):
        self.write_cameras([{"id": "c1", "name": "Cam", "ip": "10.0.0.1", "auth": "cam_cloud"}])
        with mock.patch.object(config, "HOME", self.dir), \
             mock.patch.dict(config.CAMS, {"keep": {"id": "keep", "ip": "10.0.0.9"}}, clear=True), \
             mock.patch.dict(config.CAM_BY_IP, {}, clear=True):
            (self.dir / "cameras.json").write_text("{not json")
            with self.assertRaises(ValueError):
                config.reload_cameras()
            self.assertEqual(list(config.CAMS), ["keep"])

    def test_reload_keeps_the_same_dict_objects(self):
        self.write_cameras([{"id": "c1", "name": "Cam", "ip": "10.0.0.1", "auth": "cam_cloud"}])
        with mock.patch.object(config, "HOME", self.dir), \
             mock.patch.dict(config.CAMS, {}, clear=True), mock.patch.dict(config.CAM_BY_IP, {}, clear=True):
            cams, by_ip = config.CAMS, config.CAM_BY_IP
            config.reload_cameras()
            self.assertIs(config.CAMS, cams)
            self.assertIs(config.CAM_BY_IP, by_ip)
            self.assertEqual(by_ip["10.0.0.1"]["id"], "c1")


class DeviceIdValidation(unittest.TestCase):
    def load(self, device_id):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "cameras.json"
        path.write_text(json.dumps({"version": 1, "cameras": [
            {"id": "c1", "name": "Cam", "ip": "10.0.0.1", "auth": "cam_cloud", "device_id": device_id}]}))
        return config.load_cameras(path)

    def test_int_device_id_is_refused_at_load(self):
        with self.assertRaisesRegex(ValueError, "device_id of camera 'c1' must be a non-empty string"):
            self.load(123)

    def test_empty_blank_or_null_device_id_is_refused_at_load(self):
        for bad in ("", "   ", None, ["dev"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.load(bad)

    def test_string_device_id_is_accepted(self):
        self.assertEqual(self.load("dev-1")[0]["device_id"], "dev-1")

    def test_camera_without_device_id_is_accepted(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "cameras.json"
        path.write_text(json.dumps([{"id": "c1", "name": "Cam", "ip": "10.0.0.1", "auth": "cam_cloud"}]))
        self.assertNotIn("device_id", config.load_cameras(path)[0])


class DeviceIdNormalisation(GateEnv):
    def test_gate_key_normalises_case_and_whitespace(self):
        self.assertEqual(gate.gate_key({"id": "c1", "device_id": "  DEV-1 "}), "dev-1")

    def test_budget_is_enforced_for_a_configured_device_id(self):
        cam = {"id": "c1", "name": "Cam", "ip": "10.0.0.1", "device_id": "  DEV-1 "}
        with mock.patch.dict(config.CAMS, {}, clear=True):
            for _ in range(gate.MAX_LOGINS_PER_HOUR):
                gate.gate(cam)
                self.now += gate.MIN_LOGIN_GAP + 1
            # the same device written differently shares the same budget
            with self.assertRaisesRegex(gate.GateRefused, "budget"):
                gate.gate({**cam, "device_id": "dev-1"})
        self.assertEqual(len(self.disk()["dev-1"]["logins"]), gate.MAX_LOGINS_PER_HOUR)


class DeviceIdMigration(GateEnv):
    """A device_id added to a camera that already had state under its camera id (cT)."""
    CAM_DEV = {"id": "cT", "name": "Test", "ip": "10.9.9.9", "device_id": "dev-t"}

    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(config.CAMS, {}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def blank(self, **fields):
        return {**gate._blank(), **fields}

    def test_disabled_state_stays_disabled_after_device_id_added(self):
        self.seed({"cT": self.blank(disabled="bad credentials", fails=3, transport="tpap")})
        with self.assertRaisesRegex(gate.GateRefused, "disabled: bad credentials"):
            gate.precheck(self.CAM_DEV)
        data = self.disk()
        self.assertNotIn("cT", data)
        self.assertEqual(data["dev-t"]["disabled"], "bad credentials")

    def test_block_of_1920_s_stays_after_device_id_added(self):
        self.seed({"cT": self.blank(blocked_until=T0 + 1920, logins=[T0 - 200])})
        with self.assertRaisesRegex(gate.GateRefused, "waiting 1920 s"):
            gate.gate(self.CAM_DEV)
        self.assertEqual(self.disk()["dev-t"]["blocked_until"], T0 + 1920)

    def test_both_keys_present_merge_to_the_strictest(self):
        self.seed({
            "cT": self.blank(blocked_until=T0 + 100, logins=[T0 - 10, T0 - 300], fails=1, transport="pytapo"),
            "dev-t": self.blank(blocked_until=T0 + 500, logins=[T0 - 50, T0 - 10], disabled="x", fails=2,
                                live_disabled="rtsp off"),
        })
        with self.assertRaises(gate.GateRefused):
            gate.precheck(self.CAM_DEV)
        data = self.disk()
        self.assertNotIn("cT", data)
        s = data["dev-t"]
        self.assertEqual(s["blocked_until"], T0 + 500)
        self.assertEqual(s["logins"], [T0 - 300, T0 - 50, T0 - 10])
        self.assertEqual(s["disabled"], "x")
        self.assertEqual(s["fails"], 2)
        self.assertEqual(s["transport"], "pytapo")
        self.assertEqual(s["live_disabled"], "rtsp off")

    def test_status_reload_migrates_configured_cameras(self):
        self.seed({"cT": self.blank(disabled="bad")})
        with mock.patch.dict(config.CAMS, {"cT": self.CAM_DEV}, clear=True):
            gate._reload()
        data = self.disk()
        self.assertNotIn("cT", data)
        self.assertEqual(data["dev-t"]["disabled"], "bad")

    def test_nothing_to_migrate_leaves_the_file_alone(self):
        self.seed({"dev-t": self.blank(fails=1)})
        mtime = gate.BREAKER_F.stat().st_mtime_ns
        gate.precheck(self.CAM_DEV)
        self.assertEqual(gate.BREAKER_F.stat().st_mtime_ns, mtime)


class CredentialsChanged(GateEnv):
    def test_clears_failure_state_keeps_logins_block_and_transport(self):
        self.seed({"cT": {"blocked_until": T0 + 500, "logins": [T0 - 10], "disabled": "bad creds",
                          "fails": 3, "transport": "tpap", "live_disabled": "rtsp"}})
        gate.credentials_changed(CAM)
        s = self.disk()["cT"]
        self.assertIsNone(s["disabled"])
        self.assertEqual(s["fails"], 0)
        self.assertIsNone(s["live_disabled"])
        self.assertEqual(s["transport"], "tpap")
        self.assertEqual(s["blocked_until"], T0 + 500)
        self.assertEqual(s["logins"], [T0 - 10])

    def test_reset_auth_also_forgets_the_transport(self):
        self.seed({"cT": {"blocked_until": 0, "logins": [], "disabled": "x", "fails": 1, "transport": "tpap"}})
        gate.credentials_changed(CAM, reset_auth=True)
        self.assertNotIn("transport", self.disk()["cT"])

    def test_unopenable_lock_is_a_clear_refusal(self):
        (self.dir / "breaker.lock").mkdir()   # a directory cannot be opened as the lock file
        with self.assertRaisesRegex(gate.GateRefused, "cannot be opened") as cm:
            gate.gate(CAM)
        self.assertIn(gate.MARK, str(cm.exception))
        self.assertFalse(gate._state_lock.locked())

    def test_block_is_not_shortened(self):
        self.seed({"cT": {"blocked_until": T0 + 500, "logins": [], "disabled": "x", "fails": 3}})
        gate.credentials_changed(CAM)
        with self.assertRaisesRegex(gate.GateRefused, "waiting 500 s"):
            gate.gate(CAM)

    def test_messages_point_at_the_action(self):
        gate.note_login_failure(CAM, Exception("Invalid authentication data"))
        self.assertIn("Check it (it must be the account that owns this camera)", self.disk()["cT"]["disabled"])
        self.assertNotIn("python -m", self.disk()["cT"]["disabled"])   # user-facing text: no CLI
        self.assertNotIn("breaker.json", self.disk()["cT"]["disabled"])

    def test_cli_keeps_transport_unless_reset_auth(self):
        home = self.dir / "home2"
        (home / "cache" / "meta").mkdir(parents=True)
        (home / "cameras.json").write_text(json.dumps([{"id": "c1", "name": "Cam", "ip": "10.0.0.1", "auth": "cam_cloud"}]))
        breaker = home / "cache" / "meta" / "breaker.json"
        env = {**os.environ, "KINORA_HOME": str(home)}
        env.pop("KINORA_NO_INSTANCE_LOCK", None)
        run = [sys.executable, "-m", "kinora.camera.gate"]
        breaker.write_text(json.dumps({"c1": {"blocked_until": 0, "logins": [], "disabled": "bad", "fails": 3,
                                              "transport": "tpap"}}))
        ok = subprocess.run(run + ["credentials-changed", "c1"], cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(breaker.read_text())["c1"]["transport"], "tpap")
        reset = subprocess.run(run + ["credentials-changed", "c1", "--reset-auth"], cwd=ROOT, env=env,
                               capture_output=True, text=True)
        self.assertEqual(reset.returncode, 0, reset.stderr)
        self.assertNotIn("transport", json.loads(breaker.read_text())["c1"])
        bogus = subprocess.run(run + ["credentials-changed", "c1", "--nope"], cwd=ROOT, env=env,
                               capture_output=True, text=True)
        self.assertEqual(bogus.returncode, 2)

    def test_cli_clears_state_in_a_temp_home(self):
        home = self.dir / "home"
        (home / "cache" / "meta").mkdir(parents=True)
        (home / "cameras.json").write_text(json.dumps([{"id": "c1", "name": "Cam", "ip": "10.0.0.1", "auth": "cam_cloud"}]))
        (home / "cache" / "meta" / "breaker.json").write_text(json.dumps(
            {"c1": {"blocked_until": 0, "logins": [], "disabled": "bad", "fails": 3}}))
        env = {**os.environ, "KINORA_HOME": str(home)}
        env.pop("KINORA_NO_INSTANCE_LOCK", None)
        run = [sys.executable, "-m", "kinora.camera.gate"]
        ok = subprocess.run(run + ["credentials-changed", "c1"], cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        state = json.loads((home / "cache" / "meta" / "breaker.json").read_text())["c1"]
        self.assertIsNone(state["disabled"])
        self.assertEqual(state["fails"], 0)
        unknown = subprocess.run(run + ["credentials-changed", "nope"], cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(unknown.returncode, 1)
        usage = subprocess.run(run + ["reset"], cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(usage.returncode, 2)


class SessionTransport(GateEnv):
    """session.build: pytapo is always told isKLAP=False; only the known transports are passed on."""
    CAM_CLOUD = {**CAM, "auth": "cam_cloud"}

    def built_kwargs(self, stored):
        from kinora.camera import session
        gate._state.update({"cT": {**gate._blank(), **({"transport": stored} if stored else {})}})
        with mock.patch.object(session, "get_camera_credentials", return_value=("rtspadmin", "pw")), \
             mock.patch.object(session, "get_cloud_password_for", return_value="cloud"), \
             mock.patch.object(session, "Tapo") as tapo:
            session.build(self.CAM_CLOUD)
        return tapo.call_args.kwargs

    def test_no_stored_transport_passes_isklap_false_only(self):
        kw = self.built_kwargs(None)
        self.assertIs(kw["isKLAP"], False)
        self.assertNotIn("transportMethod", kw)

    def test_known_transport_is_passed(self):
        kw = self.built_kwargs("tpap")
        self.assertEqual(kw["transportMethod"], "tpap")
        self.assertIs(kw["isKLAP"], False)

    def test_klap_or_unknown_stored_transport_is_ignored(self):
        for stored in ("klap", "kasa", "bogus"):
            with self.subTest(stored=stored):
                kw = self.built_kwargs(stored)
                self.assertNotIn("transportMethod", kw)
                self.assertIs(kw["isKLAP"], False)


class RtspFlag(GateEnv):
    def test_flag_is_persisted_and_read_back(self):
        self.assertIsNone(gate.live_disabled(CAM))
        gate.note_rtsp_auth_failure(CAM)
        reason = gate.live_disabled(CAM)
        self.assertIn("Live view disabled: the camera rejected the camera-account password", reason)
        self.assertIn("Save it again in Settings", reason)
        self.assertNotIn("python -m", reason)
        self.assertIsNotNone(self.disk()["cT"]["live_disabled"])

    def test_flag_does_not_touch_login_gate(self):
        self.seed({"cT": {"blocked_until": T0 + 100, "logins": [T0 - 5], "disabled": None, "fails": 0}})
        gate.note_rtsp_auth_failure(CAM)
        s = self.disk()["cT"]
        self.assertEqual(s["blocked_until"], T0 + 100)
        self.assertEqual(s["logins"], [T0 - 5])
        self.assertIsNone(s["disabled"])

    def test_remember_transport_persists(self):
        gate.remember_transport(CAM, "klap")
        self.assertEqual(self.disk()["cT"]["transport"], "klap")


if __name__ == "__main__":
    unittest.main()
