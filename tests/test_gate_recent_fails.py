"""Persisted failure record (recent_fails): two failures in 30 min block for 1920 s whatever the user did in between.
A typo cycle (wrong -> credentials_changed -> wrong -> credentials_changed -> attempt) must be refused. Offline:
BREAKER_F points at a temp dir, the clock and the health check are patched, no camera is contacted.
Run: .venv/bin/python -m unittest tests.test_gate_recent_fails   (from the repo root)
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from kinora import config  # noqa: E402
from kinora.camera import gate  # noqa: E402

CAM = {"id": "cW", "name": "Wrong", "ip": "10.8.8.8", "auth": "cam_cloud", "enabled": True}
T0 = 1_000_000.0
INVALID = Exception("Invalid authentication")   # no saved transport: the gate disables the camera
GENERIC = Exception("connection reset by peer")  # generic failure: 300 s back-off


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

    def disk(self):
        return json.loads(gate.BREAKER_F.read_text())

    def attempt(self, error=None):
        """One gated login that fails with `error` (None: it is refused, the test expects GateRefused)."""
        gate.gate(CAM)
        gate.note_login_failure(CAM, error)


class TypoCycle(GateEnv):
    def test_wrong_cc_wrong_cc_then_refused_for_1920_s(self):
        self.attempt(INVALID)                       # 1st wrong password: disabled
        gate.credentials_changed(CAM)               # user "fixes" it (still wrong)
        self.now += 200                             # past the 180 s gap
        self.attempt(INVALID)                       # 2nd wrong password: two failures in 30 min
        gate.credentials_changed(CAM)               # clears disabled and fails only
        self.assertEqual(len(self.disk()["cW"]["recent_fails"]), 2)
        self.now += 1                               # the next attempt is refused: the block is not cleared
        with self.assertRaises(gate.GateRefused) as cm:
            gate.gate(CAM)
        self.assertIn("waiting", str(cm.exception))
        self.now = T0 + 200 + 1920 - 1
        with self.assertRaises(gate.GateRefused):
            gate.gate(CAM)
        self.assertEqual(self.disk()["cW"]["logins"][-1], T0 + 200)   # the refused attempt was not counted

    def test_block_is_1920_s_from_the_second_failure(self):
        self.attempt(INVALID)
        gate.credentials_changed(CAM)
        self.now += 200
        self.attempt(INVALID)
        self.assertEqual(self.disk()["cW"]["blocked_until"], T0 + 200 + 1920)

    def test_never_more_than_two_failed_attempts_in_any_30_min_window(self):
        failed = []
        for _ in range(6):
            try:
                gate.gate(CAM)
            except gate.GateRefused:
                self.now += 200
                continue
            failed.append(self.now)
            gate.note_login_failure(CAM, INVALID)
            gate.credentials_changed(CAM)
            self.now += 200
        for t in failed:
            self.assertLessEqual(len([u for u in failed if t <= u < t + 1800]), 2)
        self.assertEqual(len(failed), 2)

    def test_generic_failures_also_count(self):
        self.attempt(GENERIC)                       # blocked 300 s
        self.now += 300
        self.attempt(GENERIC)                       # second in the window: 1920 s
        self.assertEqual(self.disk()["cW"]["blocked_until"], T0 + 300 + 1920)


class Window(GateEnv):
    def test_failures_outside_30_min_do_not_count_together(self):
        self.attempt(GENERIC)                       # t = T0
        gate.credentials_changed(CAM)
        self.now += 1900
        self.attempt(GENERIC)
        s = self.disk()["cW"]
        self.assertEqual(s["recent_fails"], [T0 + 1900])            # the first was pruned on write
        self.assertEqual(s["blocked_until"], T0 + 1900 + 300)       # back-off only, no 1920 s block

    def test_list_is_pruned_on_write(self):
        gate.BREAKER_F.write_text(json.dumps({"cW": {"blocked_until": 0, "logins": [], "disabled": None, "fails": 0,
                                                     "recent_fails": [T0 - 4000, T0 - 100]}}))
        self.attempt(GENERIC)
        self.assertEqual(self.disk()["cW"]["recent_fails"], [T0 - 100, T0])


class CredentialsChanged(GateEnv):
    def test_keeps_recent_fails_logins_and_block(self):
        self.attempt(GENERIC)                       # back-off 300 s
        self.now += 301
        self.attempt(GENERIC)                       # second failure in the window: block 1920 s
        before = self.disk()["cW"]
        gate.credentials_changed(CAM, reset_auth=True)
        after = self.disk()["cW"]
        for key in ("recent_fails", "logins", "blocked_until"):
            self.assertEqual(after[key], before[key], key)
        self.assertIsNone(after["disabled"])
        self.assertEqual(after["fails"], 0)


class OldFiles(GateEnv):
    def test_breaker_without_recent_fails_loads_and_is_upgraded(self):
        gate.BREAKER_F.write_text(json.dumps({"cW": {"blocked_until": 0, "logins": [T0 - 900], "disabled": None,
                                                     "fails": 1}}))
        gate.gate(CAM)
        gate.note_login_failure(CAM, GENERIC)
        s = self.disk()["cW"]
        self.assertEqual(s["recent_fails"], [T0])
        self.assertEqual(s["logins"], [T0 - 900, T0])

    def test_failure_on_a_state_without_the_key(self):
        gate.BREAKER_F.write_text(json.dumps({"cW": {"blocked_until": 0, "logins": [], "disabled": None, "fails": 0}}))
        gate.note_login_failure(CAM, GENERIC)
        self.assertEqual(self.disk()["cW"]["blocked_until"], T0 + 300)


class Merge(GateEnv):
    def test_merge_unions_recent_fails(self):
        a = {"blocked_until": 0, "recent_fails": [T0 - 500, T0 - 100]}
        b = {"blocked_until": 0, "recent_fails": [T0 - 100, T0 - 50]}
        self.assertEqual(gate._merge(a, b)["recent_fails"], [T0 - 500, T0 - 100, T0 - 50])

    def test_device_id_migration_keeps_the_failures(self):
        cam = {**CAM, "device_id": "dev-w"}
        gate._state.update({"cW": {**gate._blank(), "recent_fails": [T0 - 60]},
                            "dev-w": {**gate._blank(), "recent_fails": [T0 - 30]}})
        with mock.patch.dict(config.CAMS, {"cW": cam}, clear=True):
            self.assertTrue(gate._adopt(cam))
        self.assertNotIn("cW", gate._state)
        self.assertEqual(gate._state["dev-w"]["recent_fails"], [T0 - 60, T0 - 30])


if __name__ == "__main__":
    unittest.main()
