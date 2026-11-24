"""pytapo's discovery probes (sent from the Tapo() constructor) are counted by the login gate (no camera, no network).
Run: .venv/bin/python -m unittest tests.test_gate_probes   (from the repo root)"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_gate import CAM, GateBase  # noqa: E402
from kinora.camera import gate, patch  # noqa: E402


class Probes(GateBase):
    def test_probe_is_counted_and_has_its_own_hourly_budget(self):
        for _ in range(gate.MAX_PROBES_PER_HOUR):
            gate.note_probe(CAM)
        self.assertEqual(len(self.state()["probes"]), gate.MAX_PROBES_PER_HOUR)
        with self.assertRaises(gate.GateRefused):
            gate.note_probe(CAM)
        gate.gate(CAM)                                   # logins are a separate budget, and the gap ignores probes

    def test_probes_expire_after_an_hour(self):
        gate.note_probe(CAM)
        self.now += 3601
        gate.note_probe(CAM)
        self.assertEqual(len(self.state()["probes"]), 1)

    def test_cold_build_needs_room_for_its_probes(self):
        self.seed(probes=[self.now - 10] * (gate.MAX_PROBES_PER_HOUR - gate.PROBES_PER_COLD_BUILD))
        gate.precheck(CAM)                               # exactly room for one more cold build
        self.seed(probes=[self.now - 10] * (gate.MAX_PROBES_PER_HOUR - gate.PROBES_PER_COLD_BUILD + 1))
        with self.assertRaises(gate.GateRefused):
            gate.precheck(CAM)
        self.seed(probes=[self.now - 10] * gate.MAX_PROBES_PER_HOUR, transport="tpap")   # known transport: no probes
        gate.precheck(CAM)

    def test_probe_refused_when_blocked_or_disabled(self):
        self.seed(blocked_until=self.now + 100)
        with self.assertRaises(gate.GateRefused):
            gate.note_probe(CAM)
        self.seed(disabled="x")
        with self.assertRaises(gate.GateRefused):
            gate.note_probe(CAM)
        self.assertEqual(self.state()["probes"], [])

    def test_wrapped_probe_counts_then_calls_through(self):
        calls = []
        wrapped = patch._guard_probe(lambda host, *a, **k: calls.append(host) or "ok")
        self.assertEqual(wrapped(CAM["ip"], 443), "ok")
        self.assertEqual(calls, [CAM["ip"]])
        self.assertEqual(len(self.state()["probes"]), 1)

    def test_wrapped_probe_refuses_unknown_host_before_any_call(self):
        called = mock.Mock()
        with self.assertRaises(gate.GateRefused):
            patch._guard_probe(called)("10.1.2.3")
        called.assert_not_called()

    def test_install_wraps_pytapo_probes_once(self):
        import pytapo
        patch.install()
        patch.install()
        for name in ("discover_login", "rejects_legacy_login"):
            fn = getattr(pytapo, name)
            self.assertTrue(getattr(fn, "_kinora_guarded", False))
            self.assertFalse(getattr(getattr(fn, "__wrapped__", None), "_kinora_guarded", False))


if __name__ == "__main__":
    unittest.main()
