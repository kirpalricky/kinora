"""Runtime camera registry (kinora/cameras.py): zero-camera start, validation, atomic cameras.json, quiesce,
tombstones, lock ordering. Offline: config.HOME, the gate breaker, and the session/slots/health/live/play tables are
temp or patched, and play/live/export are mocked where a camera would be involved. No camera is contacted.
Run: .venv/bin/python -m unittest tests.test_registry   (from the repo root)
"""
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import make_mocked_request  # noqa: E402
from kinora import cameras, config, health  # noqa: E402
from kinora.camera import gate, session  # noqa: E402
from kinora.camera import slots as slots_mod  # noqa: E402
from kinora.services import export, live, play  # noqa: E402

IP1, IP2 = "10.0.0.1", "10.0.0.2"


def make(cid, ip, **kw):
    return {"id": cid, "name": cid.title(), "ip": ip, "auth": "cam_cloud", **kw}


class FakeProc:
    """Stands in for the ffmpeg process of a live relay (no process is started)."""

    def __init__(self):
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


class Env(unittest.IsolatedAsyncioTestCase):
    """Every module-level table the registry touches is a temp or patched copy."""

    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / "cache" / "meta").mkdir(parents=True)
        self.slots = slots_mod.Slots([])
        patches = [
            mock.patch.object(config, "HOME", self.home),
            mock.patch.object(config, "LIVE_ROOT", self.home / "live"),
            mock.patch.object(config, "CAMS", {}),
            mock.patch.object(config, "CAM_BY_IP", {}),
            mock.patch.object(config, "TOMBSTONES", []),
            mock.patch.object(gate, "BREAKER_F", self.home / "cache" / "meta" / "breaker.json"),
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
            mock.patch.object(cameras, "slots", self.slots),
            mock.patch.object(cameras, "_cfg_lock", asyncio.Lock()),   # bound to this test's loop
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def disk(self):
        return json.loads((self.home / "cameras.json").read_text())

    def ids(self):
        return sorted(config.CAMS)


class ZeroCameras(unittest.TestCase):
    def test_server_builds_and_answers_with_no_cameras(self):
        """A fresh data folder: the server imports, the app builds, the camera list is empty (own process, own home)."""
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "KINORA_HOME": home}
            env.pop("KINORA_NO_INSTANCE_LOCK", None)
            code = r"""
import asyncio, json
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
import server
from kinora import config
async def main():
    cams = await server.cameras(make_mocked_request("GET", "/api/cameras"))
    st = await server.status(make_mocked_request("GET", "/api/status"))
    print(json.dumps({"cams": len(config.CAMS), "by_ip": len(config.CAM_BY_IP), "tombs": len(config.TOMBSTONES),
                      "app": isinstance(server.app, web.Application), "list": json.loads(cams.text),
                      "status": json.loads(st.text)}))
asyncio.run(main())
"""
            r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                               timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout.strip().splitlines()[-1])
            self.assertFalse((Path(home) / "cameras.json").exists())
        self.assertEqual(out, {"cams": 0, "by_ip": 0, "tombs": 0, "app": True, "list": [], "status": {}})


class Load(unittest.TestCase):
    def write(self, data):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "cameras.json"
        path.write_text(json.dumps(data))
        return path

    def test_legacy_list_and_v1_without_tombstones_load(self):
        self.assertEqual(len(config.load_cameras(self.write([make("kit", IP1)]))), 1)
        self.assertEqual(len(config.load_cameras(self.write({"version": 1, "cameras": [make("kit", IP1)]}))), 1)

    def test_tombstones_are_returned_by_load_document_and_ignored_by_load_cameras(self):
        tomb = {"id": "old", "device_id": "dev-old", "ip": IP2, "removed_at": 5}
        path = self.write({"version": 1, "cameras": [make("kit", IP1)], "tombstones": [tomb]})
        self.assertEqual(config.load_cameras(path), [make("kit", IP1)])
        self.assertEqual(config.load_document(path)[1], [tomb])

    def test_duplicate_ip_is_refused_at_load(self):
        with self.assertRaisesRegex(ValueError, "duplicate ip 10.0.0.1"):
            config.load_cameras(self.write([make("kit", IP1), make("cam_b", IP1)]))

    def test_duplicate_id_and_device_id_are_refused_at_load(self):
        with self.assertRaisesRegex(ValueError, "duplicate camera id 'kit'"):
            config.load_cameras(self.write([make("kit", IP1), make("kit", IP2)]))
        with self.assertRaisesRegex(ValueError, "duplicate device_id"):
            config.load_cameras(self.write([make("kit", IP1, device_id="d"), make("cam_b", IP2, device_id="D ")]))

    def test_bad_entries_are_refused_at_load(self):
        bad = [make("kit", IP1, auth="klap"), make("kit", "example.com"), make("kit", "300.1.1.1"),
               {k: v for k, v in make("kit", IP1).items() if k != "name"}, make("kit", IP1, name="")]
        for entry in bad:
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                config.load_cameras(self.write([entry]))

    def test_malformed_tombstone_is_refused(self):
        with self.assertRaises(ValueError):
            config.load_document(self.write({"version": 1, "cameras": [], "tombstones": [{"id": "x"}]}))


class Validation(Env):
    async def test_bad_input_is_refused_and_nothing_changes(self):
        bad = [
            {"name": "x", "ip": IP1, "auth": "cam_cloud"},                       # no id
            make("A", IP1), make("x", IP1), make("a" * 33, IP1), make("bad-id", IP1), make("a b", IP1),
            make("kit", "300.1.1.1"), make("kit", "10.0.0"), make("kit", "example.com"), make("kit", ""),
            make("kit", IP1, auth="klap"), make("kit", IP1, name=""), make("kit", IP1, name="n" * 61),
            make("kit", IP1, device_id=""), make("kit", IP1, device_id=7),
        ]
        for entry in bad:
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                await cameras.add(entry)
        self.assertEqual(config.CAMS, {})
        self.assertFalse((self.home / "cameras.json").exists())

    async def test_name_of_60_characters_is_accepted(self):
        await cameras.add(make("kit", IP1, name="n" * 60))
        self.assertIn("kit", config.CAMS)

    async def test_duplicate_ip_and_id_are_refused(self):
        await cameras.add(make("kit", IP1))
        with self.assertRaisesRegex(ValueError, "duplicate ip"):
            await cameras.add(make("cam_b", IP1))
        with self.assertRaisesRegex(ValueError, "already exists"):
            await cameras.add(make("kit", IP2))
        self.assertEqual(self.ids(), ["kit"])

    async def test_id_and_device_id_are_immutable(self):
        await cameras.add(make("kit", IP1))
        with self.assertRaises(ValueError):
            await cameras.update("kit", {"id": "other"})
        with self.assertRaises(ValueError):
            await cameras.update("kit", {"device_id": "someone-else"})
        self.assertEqual(self.ids(), ["kit"])

    async def test_unknown_camera_is_a_key_error(self):
        with self.assertRaises(KeyError):
            await cameras.update("nope", {"name": "x"})
        with self.assertRaises(KeyError):
            await cameras.remove("nope")


class Add(Env):
    async def test_add_appears_in_every_table(self):
        stored = await cameras.add(make("kit", IP1))
        self.assertEqual(config.CAM_BY_IP[IP1]["id"], "kit")
        self.assertIn("kit", self.slots._locks)
        self.assertIn("kit", self.slots._wanted)
        self.assertIn("kit", session._login_locks)
        self.assertIn("kit", session._api_locks)
        self.assertIn("kit", session._ctl_locks)
        self.assertFalse(config.CAMS["kit"]["enabled"])
        self.assertEqual(str(uuid.UUID(stored["device_id"])), stored["device_id"])
        self.assertEqual(self.disk()["cameras"][0]["id"], "kit")
        self.assertEqual(self.disk()["tombstones"], [])

    async def test_caller_may_enable_and_the_stored_copy_is_detached(self):
        src = make("kit", IP1, enabled=True)
        await cameras.add(src)
        src["name"] = "changed by caller"
        self.assertTrue(config.CAMS["kit"]["enabled"])
        self.assertEqual(config.CAMS["kit"]["name"], "Kit")

    async def test_list_returns_copies(self):
        await cameras.add(make("kit", IP1))
        listed = cameras.list()
        listed[0]["name"] = "changed"
        self.assertEqual(config.CAMS["kit"]["name"], "Kit")


class Reorder(Env):
    async def test_reorder_persists_and_keeps_the_same_dict(self):
        for cid, ip in (("aa", IP1), ("bb", IP2), ("cc", "10.0.0.3")):
            await cameras.add(make(cid, ip))
        table = config.CAMS
        await cameras.reorder(["cc", "aa", "bb"])
        self.assertIs(config.CAMS, table)
        self.assertEqual(list(config.CAMS), ["cc", "aa", "bb"])
        self.assertEqual([c["id"] for c in self.disk()["cameras"]], ["cc", "aa", "bb"])
        self.assertEqual([c["id"] for c in cameras.list()], ["cc", "aa", "bb"])

    async def test_reorder_refuses_a_list_that_is_not_every_camera_once(self):
        for cid, ip in (("aa", IP1), ("bb", IP2)):
            await cameras.add(make(cid, ip))
        for bad in (["aa"], ["aa", "aa"], ["aa", "bb", "cc"], ["aa", "xx"], "aabb", None):
            with self.assertRaises(ValueError):
                await cameras.reorder(bad)
        self.assertEqual(list(config.CAMS), ["aa", "bb"])

    async def test_same_order_writes_nothing(self):
        await cameras.add(make("aa", IP1))
        await cameras.add(make("bb", IP2))
        before = (self.home / "cameras.json").stat().st_mtime_ns
        await cameras.reorder(["aa", "bb"])
        self.assertEqual((self.home / "cameras.json").stat().st_mtime_ns, before)


class AtomicWrite(Env):
    async def test_write_keeps_a_backup_of_the_previous_version(self):
        await cameras.add(make("kit", IP1))
        first = (self.home / "cameras.json").read_bytes()
        await cameras.add(make("cam_b", IP2))
        bak = self.home / "cameras.json.bak"
        self.assertEqual(bak.read_bytes(), first)
        doc = self.disk()
        self.assertEqual(doc["version"], 1)
        self.assertEqual([c["id"] for c in doc["cameras"]], ["kit", "cam_b"])
        self.assertEqual(doc["tombstones"], [])
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["cache", "cameras.json", "cameras.json.bak"])

    async def test_crash_between_temp_write_and_replace_keeps_the_old_file(self):
        await cameras.add(make("kit", IP1))
        old = (self.home / "cameras.json").read_bytes()
        real_replace = os.replace

        def crash_on_main_file(src, dst):
            if Path(dst).name == "cameras.json":
                raise RuntimeError("simulated crash before the replace")
            return real_replace(src, dst)
        with mock.patch.object(cameras.os, "replace", crash_on_main_file):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                await cameras.add(make("cam_b", IP2))
        self.assertEqual((self.home / "cameras.json").read_bytes(), old)
        self.assertEqual(self.disk()["cameras"][0]["id"], "kit")
        self.assertFalse((self.home / "cameras.json.tmp").exists())
        self.assertNotIn("cam_b", config.CAMS)            # nothing published
        self.assertNotIn("cam_b", session._login_locks)   # and the new camera's entries were rolled back
        self.assertNotIn("cam_b", self.slots._locks)

    async def test_a_legacy_bare_list_becomes_v1_on_the_next_write(self):
        (self.home / "cameras.json").write_text(json.dumps([make("kit", IP1)]))
        with mock.patch.object(config, "CAMS", {"kit": make("kit", IP1)}), \
                mock.patch.object(config, "CAM_BY_IP", {IP1: make("kit", IP1)}):
            await cameras.add(make("cam_b", IP2))
        self.assertEqual(self.disk()["version"], 1)
        self.assertEqual(len(self.disk()["cameras"]), 2)


class Update(Env):
    async def test_ip_change_keeps_device_id_and_gate_state_and_drops_the_client(self):
        await cameras.add(make("kit", IP1, device_id="dev-kit"))
        cam = config.CAMS["kit"]
        gate.gate(cam)                                       # one login counted under the device key
        session._clients["kit"] = object()
        session._born["kit"] = time.time()
        health._state["kit"] = {"state": "ready", "since": 0, "checked": time.time()}
        await cameras.update("kit", {"ip": IP2})
        self.assertEqual(config.CAM_BY_IP, {IP2: config.CAMS["kit"]})
        self.assertEqual(config.CAMS["kit"]["device_id"], "dev-kit")
        self.assertNotIn("kit", session._clients)
        self.assertNotIn("kit", session._born)
        self.assertIsNone(health.get("kit"))
        self.assertEqual(len(gate.st(config.CAMS["kit"])["logins"]), 1)
        self.assertEqual(self.disk()["cameras"][0]["device_id"], "dev-kit")

    async def test_name_change_keeps_the_cached_client(self):
        await cameras.add(make("kit", IP1))
        client = object()
        session._clients["kit"] = client
        await cameras.update("kit", {"name": "Lounge"})
        self.assertIs(session._clients["kit"], client)
        self.assertEqual(config.CAMS["kit"]["name"], "Lounge")

    async def test_two_cameras_can_swap_ips(self):
        await cameras.add(make("cam_a", IP1, device_id="dev-a"))
        await cameras.add(make("cam_b", IP2, device_id="dev-b"))
        gate.gate(config.CAMS["cam_a"])
        gate.gate(config.CAMS["cam_b"])
        await cameras.update_many({"cam_a": {"ip": IP2}, "cam_b": {"ip": IP1}})
        self.assertEqual(config.CAM_BY_IP[IP2]["id"], "cam_a")
        self.assertEqual(config.CAM_BY_IP[IP1]["id"], "cam_b")
        self.assertEqual(config.CAMS["cam_a"]["ip"], IP2)
        doc = {c["id"]: c for c in self.disk()["cameras"]}
        self.assertEqual((doc["cam_a"]["ip"], doc["cam_b"]["ip"]), (IP2, IP1))
        data = json.loads(gate.BREAKER_F.read_text())
        self.assertEqual(len(data["dev-a"]["logins"]), 1)
        self.assertEqual(len(data["dev-b"]["logins"]), 1)

    async def test_ip_in_use_by_another_camera_is_refused_for_a_single_update(self):
        await cameras.add(make("cam_a", IP1))
        await cameras.add(make("cam_b", IP2))
        with self.assertRaisesRegex(ValueError, "duplicate ip"):
            await cameras.update("cam_a", {"ip": IP2})
        self.assertEqual(config.CAM_BY_IP[IP1]["id"], "cam_a")

    async def test_update_during_an_export_is_refused_but_a_name_change_is_not(self):
        await cameras.add(make("kit", IP1))
        export.jobs["j1"] = {"cam": "kit", "state": "running"}
        with self.assertRaises(cameras.Refused):
            await cameras.update("kit", {"ip": IP2})
        await cameras.update("kit", {"name": "Lounge"})
        self.assertEqual(config.CAMS["kit"]["ip"], IP1)


class Remove(Env):
    async def test_remove_with_live_running_stops_the_relay_and_the_reaper_survives(self):
        await cameras.add(make("kit", IP1))
        proc = FakeProc()
        live._procs["kit"] = proc
        live._last["kit"] = time.time()
        with mock.patch.object(play, "stop", mock.AsyncMock()) as play_stop:
            await cameras.remove("kit")
        play_stop.assert_awaited_once()
        self.assertEqual(play_stop.await_args.args[0], "kit")
        self.assertTrue(proc.terminated)
        self.assertNotIn("kit", live._procs)
        self.assertNotIn("kit", config.CAMS)
        self.assertNotIn(IP1, config.CAM_BY_IP)

        # a relay of a camera that is no longer in the table must not kill the reaper
        ghost = FakeProc()
        live._procs["ghost"] = ghost
        live._last["ghost"] = 0
        real_sleep = asyncio.sleep

        async def quick(seconds, *a, **kw):
            await real_sleep(0)
        with mock.patch.object(live.asyncio, "sleep", quick):
            task = asyncio.ensure_future(live.reaper(self.home / "live", config.CAMS))
            for _ in range(500):
                if ghost.terminated:
                    break
                await real_sleep(0.001)
            self.assertTrue(ghost.terminated)
            self.assertFalse(task.done(), "the reaper must keep running")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def test_remove_keeps_a_tombstone_and_clears_the_entries(self):
        await cameras.add(make("kit", IP1, device_id="dev-kit"))
        await cameras.remove("kit")
        self.assertNotIn("kit", session._login_locks)
        self.assertNotIn("kit", session._api_locks)
        self.assertNotIn("kit", session._ctl_locks)
        self.assertNotIn("kit", self.slots._locks)
        doc = self.disk()
        self.assertEqual(doc["cameras"], [])
        self.assertEqual([t["id"] for t in doc["tombstones"]], ["kit"])
        t = doc["tombstones"][0]
        self.assertEqual((t["device_id"], t["ip"]), ("dev-kit", IP1))
        self.assertIsInstance(t["removed_at"], int)

    async def test_removed_id_is_never_reused(self):
        await cameras.add(make("kit", IP1))
        await cameras.remove("kit")
        with self.assertRaisesRegex(ValueError, "cannot be reused"):
            await cameras.add(make("kit", IP2))

    async def test_re_adding_at_a_tombstoned_ip_reuses_the_device_id(self):
        await cameras.add(make("kit", IP1, device_id="dev-kit"))
        await cameras.remove("kit")
        again = await cameras.add(make("kit2", IP1))
        self.assertEqual(again["device_id"], "dev-kit")
        with self.assertRaisesRegex(ValueError, "must be reused"):
            await cameras.add(make("kit3", IP1, device_id="someone-else"))
        self.assertEqual(len(self.disk()["tombstones"]), 1)

    async def test_legacy_camera_tombstone_keeps_its_gate_key(self):
        (self.home / "cameras.json").write_text(json.dumps({"version": 1, "cameras": [make("kit", IP1)]}))
        config.reload_cameras()
        await cameras.remove("kit")
        self.assertEqual(config.TOMBSTONES[0]["device_id"], "kit")   # the gate key of a camera without a device_id

    async def test_export_running_refuses_remove(self):
        await cameras.add(make("kit", IP1))
        export.jobs["j1"] = {"cam": "kit", "state": "queued"}
        with self.assertRaises(cameras.Refused):
            await cameras.remove("kit")
        self.assertIn("kit", config.CAMS)
        self.assertEqual(self.disk()["tombstones"], [])


class Concurrency(Env):
    async def test_add_during_active_play_on_another_camera_leaves_it_alone(self):
        await cameras.add(make("cam_a", IP1))
        self.slots.register_play(lambda cid: cid == "cam_a")
        await self.slots.acquire_play(config.CAMS["cam_a"], 5)   # what a playback start holds
        await cameras.add(make("cam_b", IP2))
        self.assertTrue(self.slots.lock("cam_a").locked())
        self.assertEqual(self.slots._wanted["cam_a"], 0)
        with self.assertRaisesRegex(slots_mod.Busy, "playback is open"):
            self.slots.refuse_if_playing(config.CAMS["cam_a"], "export")
        self.assertFalse(self.slots.lock("cam_b").locked())
        self.slots.release_play("cam_a")

    async def test_concurrent_adds_and_a_gate_call_on_another_camera_do_not_deadlock(self):
        await cameras.add(make("cam_a", IP1, device_id="dev-a"))
        gate_cam = config.CAMS["cam_a"]

        async def adds():
            for i in range(5):
                await cameras.add(make(f"bulk{i}", f"10.0.1.{i + 1}"))

        async def gated():
            await asyncio.to_thread(gate.gate, gate_cam)
        await asyncio.wait_for(asyncio.gather(adds(), gated()), 10)
        self.assertEqual(len(config.CAMS), 6)

    async def test_update_waits_for_a_login_in_progress_and_does_not_deadlock(self):
        await cameras.add(make("cam_a", IP1))
        lock = session.login_lock("cam_a")
        lock.acquire()                                  # a client is being created
        task = asyncio.ensure_future(cameras.update("cam_a", {"ip": IP2}))
        await asyncio.sleep(0.2)
        self.assertFalse(task.done(), "the update must wait for the login")
        self.assertEqual(config.CAMS["cam_a"]["ip"], IP1)
        lock.release()
        await asyncio.wait_for(task, 5)
        self.assertEqual(config.CAM_BY_IP, {IP2: config.CAMS["cam_a"]})

    async def test_cancelled_update_does_not_leak_the_login_lock(self):
        await cameras.add(make("cam_a", IP1))
        lock = session.login_lock("cam_a")
        lock.acquire()
        task = asyncio.ensure_future(cameras.update("cam_a", {"ip": IP2}))
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        lock.release()
        await asyncio.sleep(0.3)                        # the worker's acquire completes; its callback releases it
        self.assertTrue(lock.acquire(timeout=2), "the login lock must be free after a cancelled update")
        lock.release()
        self.assertEqual(config.CAMS["cam_a"]["ip"], IP1)


class Health(unittest.IsolatedAsyncioTestCase):
    async def test_forgotten_camera_result_is_dropped_and_a_new_check_is_kept(self):
        cam = make("hh1", IP1)
        health.forget("hh1")
        gen_before = health._gen["hh1"]
        with mock.patch.object(health, "_ping", mock.AsyncMock(return_value=False)):
            health.forget("hh1")                        # the camera changed while the check was scheduled
            await health.check(cam, gen_before)
            self.assertIsNone(health.get("hh1"))
            await health.check(cam)                     # a check started after the change is kept
            await health.check(cam)                     # "ready" is left only after 2 misses in a row
        self.assertEqual(health.get("hh1")["state"], "down")
        health._state.pop("hh1", None)
        health._misses.pop("hh1", None)


class StatusLock(Env):
    async def test_status_takes_the_gate_file_lock(self):
        import server
        calls = []

        @contextlib.contextmanager
        def spy():
            calls.append("file")
            yield
        with mock.patch.object(gate, "_gate_lock", spy):
            await server.status(make_mocked_request("GET", "/api/status"))
        self.assertEqual(calls, ["file"])


if __name__ == "__main__":
    unittest.main()
