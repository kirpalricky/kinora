"""Control-session serialisation (kinora/camera/session.py): offline, fake Tapo objects, no login.

Run: .venv/bin/python -m unittest tests.test_session_lock   (from the repo root)
Waits use Events; the only time bounds are negative checks ("did not happen within 0.2 s").
"""
import asyncio
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kinora.camera import session  # noqa: E402  (installs the gate on import; no camera I/O)

CAM = {"id": "tS", "name": "Test", "ip": "10.9.9.8"}


class Busy:
    """Fake Tapo object: getAudioConfig detects overlapping entry; the others are plain."""

    def __init__(self):
        self.playerID = "p-1"
        self.logger = object()
        self._guard = threading.Lock()
        self.active = 0
        self.max_active = 0

    def getAudioConfig(self):
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.01)
        with self._guard:
            self.active -= 1
        return "ok"

    def getUserID(self):
        return 7

    def getMediaSession(self, kind):
        return ("session", kind)

    def quick(self):
        return "q"


class FreshAsyncLocks(dict):
    """Every api() call gets its own asyncio.Lock, so only the threading ctl lock can serialise calls."""

    def __getitem__(self, key):
        return asyncio.Lock()


class SessionLockBase(unittest.TestCase):
    def setUp(self):
        self.t = Busy()
        patches = [
            mock.patch.object(session, "client", lambda cam: self.t),
            mock.patch.dict(session._ctl_locks, {CAM["id"]: threading.RLock()}),
            mock.patch.object(session, "_api_locks", FreshAsyncLocks()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class Serialisation(SessionLockBase):
    def test_api_and_proxy_audio_calls_never_overlap_across_threads(self):
        proxy = session.serialised(CAM, self.t)

        def thread_direct():
            with ThreadPoolExecutor(1) as ex:
                for f in [ex.submit(proxy.getAudioConfig) for _ in range(5)]:
                    f.result()

        def thread_api():
            for _ in range(5):
                asyncio.run(session.api(CAM, lambda t: t.getAudioConfig()))

        ths = [threading.Thread(target=thread_direct), threading.Thread(target=thread_api)]
        for th in ths:
            th.start()
        for th in ths:
            th.join()
        self.assertEqual(self.t.max_active, 1)

    def test_api_waits_for_a_held_control_lock(self):
        # Without the ctl lock inside api(), fn B would enter while fn A is still inside.
        order = []
        inside_a, release_a = threading.Event(), threading.Event()
        entered_b = threading.Event()

        def fn_a(t):
            order.append("A in")
            inside_a.set()
            release_a.wait(5)
            order.append("A out")
            return "a"

        def fn_b(t):
            entered_b.set()
            order.append("B in")
            return "b"

        results = {}
        ta = threading.Thread(target=lambda: results.update(a=asyncio.run(session.api(CAM, fn_a))))
        ta.start()
        self.assertTrue(inside_a.wait(5))
        tb = threading.Thread(target=lambda: results.update(b=asyncio.run(session.api(CAM, fn_b))))
        tb.start()
        self.assertFalse(entered_b.wait(0.2), "B entered while A held the camera")
        release_a.set()
        ta.join(5)
        tb.join(5)
        self.assertEqual(results, {"a": "a", "b": "b"})
        self.assertEqual(order, ["A in", "A out", "B in"])


class EventLoopNotBlocked(SessionLockBase):
    def setUp(self):
        super().setUp()
        self.lock = session._ctl_locks[CAM["id"]]
        self.held = threading.Event()
        self.release = threading.Event()

        def holder():
            with self.lock:
                self.held.set()
                self.release.wait(10)
        self.holder = threading.Thread(target=holder)
        self.holder.start()
        self.addCleanup(self._let_go)
        self.assertTrue(self.held.wait(5))

    def _let_go(self):
        self.release.set()
        self.holder.join(5)

    def test_loop_thread_calls_do_not_wait_for_the_lock(self):
        proxy = session.serialised(CAM, self.t)
        # Safety net: if the call did block, release the holder after 2 s so the test fails instead of hanging.
        watchdog = threading.Timer(2.0, self.release.set)
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        for name, args in (("getUserID", ()), ("getMediaSession", ("download",))):
            start = time.monotonic()
            getattr(proxy, name)(*args)
            self.assertLess(time.monotonic() - start, 0.2, f"{name} waited for the control lock")
            self.assertTrue(self.holder.is_alive())
            self.assertTrue(self._lock_still_held_by_holder())

    def _lock_still_held_by_holder(self):
        """Probe from another thread: a non-blocking acquire must fail while the holder keeps the lock."""
        def probe():
            if self.lock.acquire(blocking=False):
                self.lock.release()   # only reached if the lock was free (the test then fails)
                return False
            return True
        with ThreadPoolExecutor(1) as ex:
            return ex.submit(probe).result(5)

    def test_audio_config_from_a_worker_waits_for_the_lock(self):
        proxy = session.serialised(CAM, self.t)
        started, entered = threading.Event(), threading.Event()
        real = self.t.getAudioConfig

        def traced():
            entered.set()
            return real()
        self.t.getAudioConfig = traced   # the proxy looks the attribute up at call time

        def worker():
            started.set()
            return proxy.getAudioConfig()
        with ThreadPoolExecutor(1) as ex:
            fut = ex.submit(worker)
            self.assertTrue(started.wait(5))
            self.assertFalse(entered.wait(0.2), "getAudioConfig ran while the lock was held")
            self.release.set()
            self.holder.join(5)
            self.assertTrue(entered.wait(5))
            self.assertEqual(fut.result(5), "ok")


class DownloadDoesNotBlockListings(SessionLockBase):
    def test_api_completes_while_a_download_holds_only_short_calls(self):
        proxy = session.serialised(CAM, self.t)
        state = {"done": False}

        async def fake_download():
            for _ in range(10):          # long-running, like pytapo's Downloader between calls
                await asyncio.sleep(0.05)
                proxy.quick()
                proxy.getUserID()
            state["done"] = True

        async def main():
            dl = asyncio.create_task(fake_download())
            await asyncio.sleep(0.01)
            result = await asyncio.wait_for(session.api(CAM, lambda t: t.quick()), timeout=0.3)
            finished_during = not state["done"]
            await dl
            return result, finished_during

        result, finished_during = asyncio.run(main())
        self.assertEqual(result, "q")
        self.assertTrue(finished_during, "api() waited for the whole download")


class Passthrough(SessionLockBase):
    def test_non_callable_attributes_are_returned_unchanged(self):
        proxy = session.serialised(CAM, self.t)
        self.assertEqual(proxy.playerID, "p-1")
        self.assertIs(proxy.logger, self.t.logger)

    def test_only_locked_methods_are_wrapped(self):
        proxy = session.serialised(CAM, self.t)
        self.assertIsNot(proxy.getAudioConfig, self.t.getAudioConfig)
        self.assertEqual(proxy.getUserID, self.t.getUserID)
        self.assertEqual(proxy.getMediaSession, self.t.getMediaSession)
        self.assertEqual(proxy.quick, self.t.quick)

    def test_locked_call_returns_the_result(self):
        proxy = session.serialised(CAM, self.t)
        self.assertEqual(proxy.getAudioConfig(), "ok")


if __name__ == "__main__":
    unittest.main()
