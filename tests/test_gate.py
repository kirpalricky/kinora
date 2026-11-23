"""Characterization tests for the login gate (kinora/camera/gate.py) (no camera, no network).

The real cache/meta/breaker.json is never touched: BREAKER_F is pointed at a temp dir.
Run: .venv/bin/python -m unittest tests.test_gate   (from the repo root)
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402  (importing installs the gate on pytapo's login primitives, no I/O to cameras)
from kinora import config  # noqa: E402
from kinora.camera import gate, patch  # noqa: E402

CAM = {"id": "cT", "name": "Test", "ip": "10.9.9.9"}
T0 = 1_000_000.0


class GateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = T0
        patches = [
            mock.patch.object(gate, "BREAKER_F", Path(self.tmp.name) / "breaker.json"),
            mock.patch.object(gate.health, "unreachable", lambda cid: False),
            mock.patch.object(gate.time, "time", lambda: self.now),
            mock.patch.dict(config.CAM_BY_IP, {CAM["ip"]: CAM}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        saved = dict(gate._state)
        gate._state.clear()
        self.addCleanup(lambda: (gate._state.clear(), gate._state.update(saved)))

    def seed(self, **fields):
        """Put a camera entry in the state file (the gate re-reads the file before every decision)."""
        gate.BREAKER_F.write_text(json.dumps({"cT": {**gate._blank(), **fields}}))

    def state(self):
        return json.loads(gate.BREAKER_F.read_text())["cT"]


class RateAndBudget(GateBase):
    def test_gap_between_logins(self):
        gate.gate(CAM)
        self.now += gate.MIN_LOGIN_GAP - 1
        with self.assertRaises(gate.GateRefused):
            gate.gate(CAM)
        self.now += 2
        gate.gate(CAM)

    def test_hourly_budget_and_expiry(self):
        for _ in range(gate.MAX_LOGINS_PER_HOUR):
            gate.gate(CAM)
            self.now += gate.MIN_LOGIN_GAP + 1
        with self.assertRaisesRegex(gate.GateRefused, "budget"):
            gate.gate(CAM)
        self.now = T0 + 3601 + gate.MIN_LOGIN_GAP * gate.MAX_LOGINS_PER_HOUR
        gate.gate(CAM)  # old entries aged out

    def test_count_only_skips_rate_check_but_counts(self):
        gate.gate(CAM)
        gate.gate(CAM, count_only=True)
        self.assertEqual(len(self.state()["logins"]), 2)

    def test_precheck_does_not_count(self):
        gate.precheck(CAM)
        self.assertFalse(gate.BREAKER_F.exists() and self.state()["logins"])

    def test_attempt_is_counted_before_it_is_made(self):
        gate.gate(CAM)
        self.assertEqual(self.state()["logins"], [T0])

    def test_unreachable_camera_refused(self):
        with mock.patch.object(gate.health, "unreachable", lambda cid: True), \
             mock.patch.object(gate.health, "get", lambda cid: {"state": "starting"}):
            with self.assertRaisesRegex(gate.GateRefused, "not answering"):
                gate.gate(CAM)
            self.assertFalse(gate.BREAKER_F.exists())  # nothing counted


class Failures(GateBase):
    def test_suspension_is_honoured_with_margin(self):
        gate.note_login_failure(CAM, Exception("Temporary Suspension: Try again in 100 seconds"))
        self.assertEqual(self.state()["blocked_until"], T0 + 100 + gate.SAFETY_MARGIN)
        with self.assertRaisesRegex(gate.GateRefused, "waiting"):
            gate.gate(CAM)

    def test_generic_failure_backs_off(self):
        gate.note_login_failure(CAM, Exception("boom"))
        self.assertEqual(self.state()["blocked_until"], T0 + gate.GENERIC_BACKOFF)

    def test_second_failure_outlasts_camera_lockout(self):
        gate.note_login_failure(CAM, Exception("boom"))
        gate.note_login_failure(CAM, Exception("boom"))
        self.assertGreaterEqual(self.state()["blocked_until"], T0 + 1920)

    def test_third_failure_disables(self):
        for _ in range(3):
            gate.note_login_failure(CAM, Exception("boom"))
        self.assertIn("3 failed logins", self.state()["disabled"])
        with self.assertRaisesRegex(gate.GateRefused, "disabled"):
            gate.gate(CAM)

    def test_invalid_auth_before_any_success_disables(self):
        gate.note_login_failure(CAM, Exception("Invalid authentication data"))
        self.assertIn("refused the TP-Link password", self.state()["disabled"])

    def test_invalid_auth_after_success_only_blocks(self):
        self.seed(transport="tpap")
        gate.note_login_failure(CAM, Exception("Invalid authentication data"))
        s = self.state()
        self.assertIsNone(s["disabled"])
        self.assertEqual(s["blocked_until"], T0 + gate.AUTH_RETRY_BLOCK)

    def test_40211_disables_and_forgets_transport(self):
        self.seed(transport="pytapo")
        gate.note_login_failure(CAM, Exception("Error -40211"))
        s = self.state()
        self.assertNotIn("transport", s)
        self.assertIn("Save the TP-Link password again in Settings", s["disabled"])
        self.assertNotIn("python -m", s["disabled"])

    def test_locally_refused_attempt_is_not_a_failure(self):
        gate.note_login_failure(CAM, Exception(gate.MARK + "waiting 30 s"))
        self.assertFalse(gate.BREAKER_F.exists())

    def test_success_resets_fail_count(self):
        gate.note_login_failure(CAM, Exception("boom"))
        gate.note_login_success(CAM)
        self.assertEqual(self.state()["fails"], 0)


class Persistence(GateBase):
    def test_corrupt_file_fails_closed(self):
        gate.BREAKER_F.write_text("{not json")
        st = gate._load_state()
        self.assertGreater(st[next(iter(config.CAMS))]["blocked_until"], T0)

    def test_state_is_reloaded_before_each_decision(self):
        gate.gate(CAM)  # another process (probe.py) logs in meanwhile
        data = json.loads(gate.BREAKER_F.read_text())
        data["cT"]["logins"].append(self.now)
        gate.BREAKER_F.write_text(json.dumps(data))
        self.now += gate.MIN_LOGIN_GAP - 1
        with self.assertRaises(gate.GateRefused):
            gate.gate(CAM)


class Wrapper(GateBase):
    def make(self, host, fn):
        class Fake:
            pass
        Fake.host = host
        Fake.login = patch._guard_login(fn)
        return Fake()

    def test_login_is_gated_and_counted(self):
        calls = []
        f = self.make(CAM["ip"], lambda self: calls.append(1) or "ok")
        self.assertEqual(f.login(), "ok")
        self.assertEqual(len(self.state()["logins"]), 1)
        with self.assertRaises(gate.GateRefused):
            f.login()
        self.assertEqual(calls, [1])  # second attempt never reached the camera

    def test_nested_retry_is_counted_not_rate_checked(self):
        f = None

        def login(self):
            if not getattr(self, "inner", False):
                self.inner = True
                return f.login()  # pytapo's own retry of the same login
            return "inner"
        f = self.make(CAM["ip"], login)
        f.login()
        self.assertEqual(len(self.state()["logins"]), 2)

    def test_failure_is_recorded_once_for_nested_call(self):
        def login(self):
            raise Exception("boom")
        f = self.make(CAM["ip"], login)
        with self.assertRaises(Exception):
            f.login()
        self.assertEqual(self.state()["fails"], 1)

    def test_unknown_host_is_refused(self):
        calls = []
        f = self.make("203.0.113.5", lambda self: calls.append(1) or "ok")
        with self.assertRaises(gate.GateRefused) as cm:
            f.login()
        self.assertIn(gate.MARK, str(cm.exception))
        self.assertEqual(calls, [])          # the wrapped login was never called
        self.assertFalse(gate.BREAKER_F.exists())

    def test_missing_host_attribute_is_refused(self):
        calls = []

        class NoHost:
            def login(self):
                return patch._guard_login(lambda self: calls.append(1) or "ok")(self)
        with self.assertRaises(gate.GateRefused):
            NoHost().login()
        self.assertEqual(calls, [])
        self.assertFalse(gate.BREAKER_F.exists())


class Install(unittest.TestCase):
    def test_importing_session_alone_installs_the_guard(self):
        import subprocess
        code = ("import sys; sys.path.insert(0, '.');"
                "from kinora.camera import session;"
                "from pytapo.transport.tpap.tpap import Tpap;"
                "from pytapo.transport.pytapo.pytapo import pyTapo;"
                "sys.exit(0 if Tpap._login._kinora_guarded and pyTapo._refreshStok._kinora_guarded else 1)")
        root = Path(__file__).resolve().parent.parent
        self.assertEqual(subprocess.run([sys.executable, "-c", code], cwd=root).returncode, 0)

    def test_install_refuses_when_a_target_is_missing(self):
        import pytapo.transport.tpap.tpap as tpap_mod
        import pytapo.transport.pytapo.pytapo as pytapo_mod

        class NoLogin:
            pass
        with mock.patch.object(tpap_mod, "Tpap", NoLogin):
            with self.assertRaisesRegex(RuntimeError, "pytapo's internals changed"):
                patch.install()
        class NoRefresh:
            pass
        with mock.patch.object(pytapo_mod, "pyTapo", NoRefresh):
            with self.assertRaisesRegex(RuntimeError, "pytapo's internals changed"):
                patch.install()

    def test_requirements_pin_matches_installed_pytapo(self):
        import importlib.metadata
        import re as _re
        req = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
        pinned = _re.search(r"^pytapo==(\S+)$", req, _re.M)
        self.assertIsNotNone(pinned)
        self.assertEqual(pinned.group(1), importlib.metadata.version("pytapo"))

    def test_install_twice_wraps_login_once(self):
        from pytapo.transport.tpap.tpap import Tpap
        from pytapo.transport.pytapo.pytapo import pyTapo
        before, before2 = Tpap._login, pyTapo._refreshStok
        patch.install()
        patch.install()
        self.assertIs(Tpap._login, before)
        self.assertIs(pyTapo._refreshStok, before2)
        self.assertTrue(Tpap._login._kinora_guarded)

    def test_klap_login_is_refused_before_any_request(self):
        import asyncio
        from pytapo.transport.klap.klap import Klap
        patch.install()
        with self.assertRaises(gate.GateRefused) as cm:
            asyncio.run(Klap.authenticate(object()))   # the stub never touches self: no request is made
        self.assertIn(gate.MARK, str(cm.exception))
        self.assertIn("klap login is not supported by this app", str(cm.exception))

    def test_install_twice_does_not_replace_klap_stub_twice(self):
        from pytapo.transport.klap.klap import Klap
        patch.install()
        first = Klap.authenticate
        patch.install()
        self.assertIs(Klap.authenticate, first)
        self.assertTrue(first._kinora_refused)

    def test_import_server_guards_pytapo_login_primitives(self):
        from pytapo.transport.pytapo.pytapo import pyTapo
        from pytapo.transport.tpap.tpap import Tpap
        self.assertTrue(Tpap._login._kinora_guarded)
        self.assertTrue(pyTapo._refreshStok._kinora_guarded)


if __name__ == "__main__":
    unittest.main()
