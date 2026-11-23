"""Slot ownership rules (no camera). Run: .venv/bin/python -m unittest tests.test_slots"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kinora.camera import slots as S  # noqa: E402

CAM = {"id": "c1", "name": "Cam"}


class World:
    """Fake play/live state the Slots object consults."""
    def __init__(self):
        self.playing, self.live, self.idle, self.stopped = False, False, 0, 0

    def make(self):
        async def stop_live(cam):
            self.stopped += 1
            self.live = False
        return S.Slots(["c1"], play_active=lambda cid: self.playing, live_running=lambda cid: self.live,
                       live_idle_s=lambda cid: self.idle, stop_live=stop_live)


def run(c):
    return asyncio.run(c)


class Clip(unittest.TestCase):
    def test_clip_runs_and_releases(self):
        async def go():
            s = World().make()
            async with s.clip(CAM):
                self.assertTrue(s.lock("c1").locked())
            self.assertFalse(s.lock("c1").locked())
        run(go())

    def test_clip_refused_while_playing(self):
        async def go():
            w = World(); w.playing = True; s = w.make()
            with self.assertRaisesRegex(S.Busy, "playback is open"):
                async with s.clip(CAM):
                    pass
            self.assertFalse(s.lock("c1").locked())
        run(go())

    def test_exception_in_body_releases_lock(self):
        async def go():
            s = World().make()
            with self.assertRaises(RuntimeError):
                async with s.clip(CAM):
                    raise RuntimeError("x")
            self.assertFalse(s.lock("c1").locked())
        run(go())

    def test_idle_live_is_stopped_for_a_clip(self):
        async def go():
            w = World(); w.live, w.idle = True, S.LIVE_IDLE_S + 1; s = w.make()
            async with s.clip(CAM):
                pass
            self.assertEqual(w.stopped, 1)
        run(go())

    def test_live_in_use_blocks_a_clip(self):
        async def go():
            w = World(); w.live, w.idle = True, 1; s = w.make()
            with self.assertRaisesRegex(S.Busy, "live view is open"):
                async with s.clip(CAM):
                    pass
            self.assertEqual(w.stopped, 0)
            self.assertFalse(s.lock("c1").locked())
        run(go())

    def test_thumbnail_runs_beside_live_without_stopping_it(self):
        async def go():
            w = World(); w.live, w.idle = True, 1; s = w.make()
            async with s.clip(CAM, beside_live=True):
                self.assertTrue(s.lock("c1").locked())
            self.assertEqual(w.stopped, 0)
            w.live, w.idle = True, S.LIVE_IDLE_S + 1
            async with s.clip(CAM, beside_live=True):
                pass
            self.assertEqual(w.stopped, 0)
        run(go())

    def test_thumbnail_beside_live_still_yields_to_playback(self):
        async def go():
            w = World(); w.live = True; w.playing = True; s = w.make()
            with self.assertRaisesRegex(S.Busy, "playback is open"):
                async with s.clip(CAM, beside_live=True):
                    pass
        run(go())

    def test_clip_gives_way_to_a_waiting_play_start(self):
        async def go():
            s = World().make()
            gate = asyncio.Event()

            async def first():
                async with s.clip(CAM):
                    await gate.wait()
            t = asyncio.create_task(first())
            await asyncio.sleep(0.01)
            starter = asyncio.create_task(s.acquire_play(CAM, 2))
            await asyncio.sleep(0.01)
            self.assertTrue(s.play_wanted("c1"))
            with self.assertRaises(S.Busy):          # refused before it even queues
                async with s.clip(CAM):
                    pass
            gate.set()
            await t
            await starter
            self.assertFalse(s.play_wanted("c1"))
            s.release_play("c1")
        run(go())

    def test_refuse_if_playing_message(self):
        w = World(); w.playing = True
        with self.assertRaisesRegex(S.Busy, "stop it to export"):
            w.make().refuse_if_playing(CAM, "export")


class Play(unittest.TestCase):
    def test_play_waits_for_clip_then_gets_the_lock(self):
        async def go():
            s = World().make()
            async def clip():
                async with s.clip(CAM):
                    await asyncio.sleep(0.1)
            t = asyncio.create_task(clip())
            await asyncio.sleep(0.01)
            await s.acquire_play(CAM, 2)
            self.assertTrue(s.lock("c1").locked())
            s.release_play("c1")
            await t
        run(go())

    def test_play_times_out_with_busy_and_leaves_no_marker(self):
        async def go():
            s = World().make()
            await s.lock("c1").acquire()
            with self.assertRaises(S.Busy):
                await s.acquire_play(CAM, 0.05)
            self.assertFalse(s.play_wanted("c1"))
            s.lock("c1").release()
        run(go())

    def test_play_stops_live(self):
        async def go():
            w = World(); w.live = True; s = w.make()
            await s.acquire_play(CAM, 1)
            self.assertEqual(w.stopped, 1)
            s.release_play("c1")
        run(go())

    def test_failed_live_stop_releases_the_lock(self):
        async def go():
            w = World(); w.live = True
            async def boom(cam): raise RuntimeError("x")
            s = S.Slots(["c1"], play_active=lambda c: False, live_running=lambda c: True,
                        live_idle_s=lambda c: 0, stop_live=boom)
            with self.assertRaises(RuntimeError):
                await s.acquire_play(CAM, 1)
            self.assertFalse(s.lock("c1").locked())
        run(go())


if __name__ == "__main__":
    unittest.main()
