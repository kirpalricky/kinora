"""Saved PTZ presets, recall only (kinora/services/ptz.py presets and goto_preset). Offline: a fake api, no camera.
Run: .venv/bin/python -m unittest tests.test_ptz_presets   (from the repo root)
"""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kinora import errors  # noqa: E402
from kinora.services import ptz  # noqa: E402


class FakeTapo:
    """Records the pytapo calls the service makes; getPresets answers from `presets`."""
    def __init__(self, presets=None):
        self.presets = presets if presets is not None else {}
        self.calls = []

    def getPresets(self):
        self.calls.append(("getPresets",))
        return dict(self.presets)

    def setPreset(self, preset_id):
        self.calls.append(("setPreset", preset_id))
        if preset_id not in self.presets:
            raise Exception("unknown preset")


def run_api(tapo):
    """An api(cam, fn) that runs fn on the fake camera handle, as session.api does."""
    async def api(cam, fn):
        return fn(tapo)
    return api


CAM = {"id": "cP", "name": "Pan Cam", "ip": "10.9.9.9", "ptz": True}
FIXED = {"id": "cF", "name": "Fixed Cam", "ip": "10.9.9.8"}


class PresetList(unittest.TestCase):
    def test_sorted_by_name_with_ids(self):
        tapo = FakeTapo({"3": "Kitchen door", "1": "crib", "2": "Bed"})
        out = asyncio.run(ptz.presets(run_api(tapo), CAM))
        self.assertEqual(out, [{"id": "2", "name": "Bed"}, {"id": "1", "name": "crib"},
                               {"id": "3", "name": "Kitchen door"}])
        self.assertEqual(tapo.calls, [("getPresets",)])

    def test_no_presets_is_an_empty_list(self):
        self.assertEqual(asyncio.run(ptz.presets(run_api(FakeTapo()), CAM)), [])

    def test_non_ptz_camera_is_not_supported(self):
        tapo = FakeTapo({"1": "x"})
        with self.assertRaises(errors.NotSupported) as cm:
            asyncio.run(ptz.presets(run_api(tapo), FIXED))
        self.assertEqual(cm.exception.status, 501)
        self.assertEqual(tapo.calls, [])


class GotoPreset(unittest.TestCase):
    def goto(self, cam, preset_id, tapo=None):
        tapo = tapo or FakeTapo({"1": "crib", "a-2_b": "door"})
        with mock.patch.dict(ptz._last, clear=True):
            asyncio.run(ptz.goto_preset(run_api(tapo), cam, preset_id))
        return tapo

    def test_setpreset_called_with_the_id(self):
        tapo = self.goto(CAM, "a-2_b")
        self.assertEqual(tapo.calls, [("setPreset", "a-2_b")])

    def test_non_ptz_camera_is_not_supported(self):
        with self.assertRaises(errors.NotSupported):
            self.goto(FIXED, "1")

    def test_bad_ids_are_bad_input(self):
        for bad in ("", "x" * 33, "a b", "1;rm", "../2", None, 1, ["1"]):
            with self.subTest(bad=bad):
                tapo = FakeTapo({"1": "crib"})
                with self.assertRaises(errors.BadInput) as cm:
                    self.goto(CAM, bad, tapo)
                self.assertEqual(cm.exception.status, 400)
                self.assertEqual(tapo.calls, [])

    def test_32_characters_is_allowed(self):
        tapo = self.goto(CAM, "x" * 32, FakeTapo({"x" * 32: "long"}))
        self.assertEqual(tapo.calls, [("setPreset", "x" * 32)])

    def test_rate_limit_is_too_fast_and_shared_with_pan(self):
        tapo = FakeTapo({"1": "crib"})
        with mock.patch.dict(ptz._last, {"cP": time.time()}):
            with self.assertRaises(errors.TooFast) as cm:
                asyncio.run(ptz.goto_preset(run_api(tapo), CAM, "1"))
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(tapo.calls, [])

    def test_a_preset_counts_against_the_next_pan_step(self):
        tapo = FakeTapo({"1": "crib"})
        calls = []

        async def api(cam, fn):
            calls.append(fn)
        with mock.patch.dict(ptz._last, clear=True):
            asyncio.run(ptz.goto_preset(run_api(tapo), CAM, "1"))
            with self.assertRaises(errors.TooFast):
                asyncio.run(ptz.step(api, CAM, "left"))


if __name__ == "__main__":
    unittest.main()
