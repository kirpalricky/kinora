"""Slots defaults, register_play / register_live, and the module-level singleton (no camera).
Run: .venv/bin/python -m unittest tests.test_slots_register
"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kinora import config  # noqa: E402
from kinora.camera import slots as S  # noqa: E402

CAM = {"id": "c1", "name": "Cam"}


class Defaults(unittest.TestCase):
    def test_no_arguments_means_nothing_playing_and_no_live(self):
        s = S.Slots(["c1"])
        self.assertFalse(s._play_active("c1"))
        self.assertFalse(s._live_running("c1"))
        self.assertEqual(s._live_idle_s("c1"), 0)
        s.refuse_if_playing(CAM, "export")          # must not raise

    def test_default_stop_live_is_a_coroutine_that_does_nothing(self):
        s = S.Slots(["c1"])
        self.assertIsNone(asyncio.run(s._stop_live(CAM)))

    def test_clip_works_with_defaults(self):
        async def go():
            s = S.Slots(["c1"])
            async with s.clip(CAM):
                self.assertTrue(s.lock("c1").locked())
            self.assertFalse(s.lock("c1").locked())
        asyncio.run(go())

    def test_partial_arguments_keep_the_other_defaults(self):
        s = S.Slots(["c1"], play_active=lambda cid: True)
        self.assertTrue(s._play_active("c1"))
        self.assertFalse(s._live_running("c1"))


class Registration(unittest.TestCase):
    def test_register_play_replaces_the_check(self):
        s = S.Slots(["c1"])
        s.register_play(lambda cid: True)
        with self.assertRaisesRegex(S.Busy, "playback is open"):
            s.refuse_if_playing(CAM, "export")

    def test_register_live_sets_all_three_callbacks(self):
        async def go():
            calls = []

            async def stop(cam):
                calls.append(cam["id"])
            s = S.Slots(["c1"])
            s.register_live(lambda cid: True, lambda cid: S.LIVE_IDLE_S + 1, stop)
            self.assertTrue(s._live_running("c1"))
            self.assertEqual(s._live_idle_s("c1"), S.LIVE_IDLE_S + 1)
            async with s.clip(CAM):             # idle live is stopped for a clip
                pass
            self.assertEqual(calls, ["c1"])
        asyncio.run(go())

    def test_register_live_while_live_busy_refuses_clip(self):
        async def go():
            s = S.Slots(["c1"])
            s.register_live(lambda cid: True, lambda cid: 1, lambda cam: asyncio.sleep(0))
            with self.assertRaisesRegex(S.Busy, "live view is open"):
                async with s.clip(CAM):
                    pass
        asyncio.run(go())

    def test_second_registration_wins(self):
        s = S.Slots(["c1"])
        s.register_play(lambda cid: True)
        s.register_play(lambda cid: False)
        s.refuse_if_playing(CAM, "export")      # must not raise


class Singleton(unittest.TestCase):
    def test_singleton_covers_every_configured_camera(self):
        self.assertTrue(config.CAMS)
        for cid in config.CAMS:
            self.assertFalse(S.slots.lock(cid).locked())

    def test_singleton_is_a_slots_and_the_same_object_on_reimport(self):
        self.assertIsInstance(S.slots, S.Slots)
        import importlib
        self.assertIs(importlib.import_module("kinora.camera.slots").slots, S.slots)


class ServerWiring(unittest.TestCase):
    def test_importing_server_registers_play_and_live_with_the_singleton(self):
        # The clip/play/live rules only work if play and live register at import; a missing registration
        # would silently fall back to "nothing playing, no live" (two streams on one camera).
        import server
        self.assertIs(S.slots._play_active, server.p2p_play.active)
        self.assertIs(S.slots._live_running, server.live.running)
        self.assertIs(S.slots._live_idle_s, server.live.idle_s)
        self.assertIs(server.slots, S.slots)


if __name__ == "__main__":
    unittest.main()
