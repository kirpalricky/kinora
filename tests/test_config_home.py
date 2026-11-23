"""Settings resolution: data home, cameras.json layouts, port, allowed hosts, .env (no camera).
Run: .venv/bin/python -m unittest tests.test_config_home
"""
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import platformdirs  # noqa: E402
from kinora import config  # noqa: E402
from kinora.oslock import os_lock  # noqa: E402

REAL_DIRS = [Path(platformdirs.user_data_dir(n)) for n in ("kinora", "tapo-cameras")] + \
            [Path(platformdirs.user_cache_dir(n)) for n in ("kinora", "tapo-cameras")]   # captured before any patch


def _snapshot():
    return {str(p): (p.exists(), p.stat().st_mtime_ns if p.exists() else None) for p in REAL_DIRS}

# TEST-NET address, not a real camera
CAM = {"id": "c1", "name": "Cam", "ip": "192.0.2.10", "auth": "cam_cloud"}
ENV_KEYS = ("KINORA_HOME", "KINORA_PORT", "TAPOCAM_HOME", "TAPOCAM_PORT")


class ConfigHome(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ENV_KEYS}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        # every reload of config re-imports platformdirs' functions: patch them at the source so no test can reach
        # the developer's real data or cache folders
        before = _snapshot()
        self.addCleanup(lambda: self.assertEqual(_snapshot(), before, "a test touched the real user folders"))
        for name, sub in (("user_data_dir", "appdata"), ("user_cache_dir", "appcache")):
            p = mock.patch.object(platformdirs, name, lambda app, *a, _s=sub, **k: str(self.tmp / _s / app))
            p.start()
            self.addCleanup(p.stop)
        # LIFO: restore the environment and reload config before the temp dir is removed
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(config)

    def reload_with(self, **env):
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(env)
        return importlib.reload(config)

    def write_cameras(self, data, folder=None):
        folder = folder or self.tmp
        (folder / "cameras.json").write_text(json.dumps(data))

    def test_env_home_override(self):
        self.write_cameras([CAM])
        cfg = self.reload_with(KINORA_HOME=str(self.tmp))
        self.assertEqual(cfg.HOME, self.tmp)
        self.assertEqual(cfg.CACHE, self.tmp / "cache")
        self.assertEqual(cfg.META, self.tmp / "cache" / "meta")
        self.assertEqual(cfg.EXPORTS, self.tmp / "exports")
        self.assertEqual(cfg.LIVE_ROOT, self.tmp / "cache" / "live")
        self.assertEqual(cfg.PLAY_ROOT, self.tmp / "cache" / "play")
        # ROOT stays the code directory
        self.assertTrue((cfg.ROOT / "kinora" / "config.py").exists())
        self.assertEqual(cfg.CAM_BY_IP, {"192.0.2.10": CAM})

    def test_fresh_home_without_cameras_json(self):
        cfg = self.reload_with(KINORA_HOME=str(self.tmp))
        self.assertEqual(cfg.CAMS, {})
        self.assertEqual(cfg.CAM_BY_IP, {})

    def test_legacy_layout_detection(self):
        self.reload_with()
        legacy = self.tmp / "legacy"
        empty = self.tmp / "empty"
        legacy.mkdir()
        empty.mkdir()
        self.write_cameras([CAM], folder=legacy)
        with mock.patch.object(config, "ROOT", legacy):
            self.assertEqual(config.resolve_home(), legacy)
        fake = lambda name: str(self.tmp / "appdata" / name)
        with mock.patch.object(config, "ROOT", empty), mock.patch.object(config, "user_data_dir", fake):
            self.assertEqual(config.resolve_home(), self.tmp / "appdata" / "kinora")
        # the env override beats the legacy layout
        with mock.patch.object(config, "ROOT", legacy), \
                mock.patch.dict(os.environ, {"KINORA_HOME": str(self.tmp / "override")}):
            self.assertEqual(config.resolve_home(), self.tmp / "override")

    def test_versioned_and_legacy_cameras_json(self):
        for label, data in (("legacy list", [CAM]), ("versioned", {"version": 1, "cameras": [CAM]})):
            with self.subTest(label):
                self.write_cameras(data)
                cfg = self.reload_with(KINORA_HOME=str(self.tmp))
                self.assertEqual(list(cfg.CAMS), ["c1"])
                self.assertEqual(cfg.CAMS["c1"]["ip"], "192.0.2.10")

    def test_unknown_cameras_json_version_is_refused(self):
        self.write_cameras({"version": 2, "cameras": [CAM]})
        with self.assertRaises(ValueError):
            self.reload_with(KINORA_HOME=str(self.tmp))

    def test_port_default_and_override(self):
        self.assertEqual(self.reload_with().PORT, 8080)
        self.assertEqual(self.reload_with(KINORA_PORT="9123").PORT, 9123)

    def test_allowed_hosts(self):
        cfg = self.reload_with()
        self.assertEqual(cfg.allowed_hosts(8080), {"127.0.0.1:8080", "localhost:8080"})
        self.assertEqual(cfg.allowed_hosts(9123), {"127.0.0.1:9123", "localhost:9123"})

    def test_dotenv_is_read_from_home(self):
        (self.tmp / ".env").write_text("TAPO_USER=testuser\n")
        self.reload_with(KINORA_HOME=str(self.tmp))
        from kinora import creds
        self.assertEqual(creds._from_dotenv("TAPO_USER"), "testuser")
        self.assertIsNone(creds._from_dotenv("TAPO_MISSING_KEY"))

    def test_env_helper_prefers_kinora_then_old_name_then_default(self):
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        self.assertEqual(config.env("PORT", "8080"), "8080")
        os.environ["TAPOCAM_PORT"] = "1111"
        self.assertEqual(config.env("PORT", "8080"), "1111")
        os.environ["KINORA_PORT"] = "2222"
        self.assertEqual(config.env("PORT", "8080"), "2222")
        self.assertIsNone(config.env("NO_SUCH_SETTING"))

    def test_old_home_and_port_names_still_work(self):
        self.write_cameras([CAM])
        cfg = self.reload_with(TAPOCAM_HOME=str(self.tmp), TAPOCAM_PORT="9124")
        self.assertEqual(cfg.HOME, self.tmp)
        self.assertEqual(cfg.PORT, 9124)

    def test_kinora_home_wins_over_old_name(self):
        other = self.tmp / "other"
        other.mkdir()
        cfg = self.reload_with(KINORA_HOME=str(self.tmp), TAPOCAM_HOME=str(other))
        self.assertEqual(cfg.HOME, self.tmp)

    def _appdata(self):
        return lambda name: str(self.tmp / "appdata" / name)

    def _patched(self, fake_data=None):
        self.reload_with()
        fake_data = fake_data or self._appdata()
        cache = lambda name: str(self.tmp / "appcache" / name)
        stack = [mock.patch.object(config, "ROOT", self.tmp / "empty"),
                 mock.patch.object(config, "user_data_dir", fake_data),
                 mock.patch.object(config, "user_cache_dir", cache)]
        for p in stack:
            p.start()
            self.addCleanup(p.stop)

    def _old(self, data=True):
        old = Path(self._appdata()("tapo-cameras"))
        (old / "cache" / "meta").mkdir(parents=True)
        if data:
            (old / "cameras.json").write_text("[]")
            (old / "cache" / "meta" / "breaker.json").write_text("{}")
        return old

    def _hold_legacy_lock(self):
        path = config.legacy_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a+b")   # a second descriptor: an old server
        os_lock(f, blocking=False)
        self.addCleanup(f.close)

    def test_import_never_moves_the_old_folder(self):
        old = self._old()
        self.reload_with()          # fake appdata from setUp, no env override
        self.assertTrue(old.exists())
        self.assertFalse(Path(self._appdata()("kinora")).exists())
        self.assertEqual(config.HOME, old)   # until init(): the old folder is used as is

    def test_init_moves_the_old_folder_once_and_repoints_paths(self):
        old = self._old()
        self.reload_with()
        new = Path(self._appdata()("kinora"))
        self.assertEqual(config.init(), new)
        self.assertEqual(config.HOME, new)
        self.assertEqual(config.META, new / "cache" / "meta")
        self.assertTrue((new / "cameras.json").exists())
        self.assertFalse(old.exists())
        self.assertEqual(config.init(), new)      # second call: nothing to move, nothing lost
        self.assertTrue((new / "cache" / "meta" / "breaker.json").exists())

    def test_init_with_a_running_old_server_does_not_move(self):
        old = self._old()
        self.reload_with()
        self._hold_legacy_lock()
        with mock.patch("sys.stderr"):
            home = config.init()
        self.assertEqual(home, old)
        self.assertTrue((old / "cameras.json").exists())
        self.assertFalse(Path(self._appdata()("kinora")).exists())   # not even created
        self.assertEqual(config.HOME, old)

    def test_init_moves_when_the_caller_holds_the_legacy_lock_itself(self):
        self._old()
        self.reload_with()
        self._hold_legacy_lock()
        new = Path(self._appdata()("kinora"))
        self.assertEqual(config.init(legacy_lock_held=True), new)
        self.assertTrue((new / "cameras.json").exists())

    def test_failed_move_uses_the_old_folder_and_creates_no_new_one(self):
        old = self._old()
        self.reload_with()
        new = Path(self._appdata()("kinora"))
        with mock.patch.object(Path, "rename", side_effect=PermissionError(13, "Permission denied")), \
                mock.patch("sys.stderr") as err:
            home = config.init()
        self.assertEqual(home, old)
        self.assertFalse(new.exists())
        self.assertTrue((old / "cameras.json").exists())
        self.assertIn("old folder", "".join(c.args[0] for c in err.write.call_args_list))

    def test_both_exist_new_has_data_leaves_old_alone(self):
        old, new = self._old(), Path(self._appdata()("kinora"))
        new.mkdir(parents=True)
        (new / "cameras.json").write_text("[ ]")
        self.reload_with()
        with mock.patch("sys.stderr"):
            self.assertEqual(config.init(), new)
        self.assertEqual((old / "cameras.json").read_text(), "[]")
        self.assertEqual((new / "cameras.json").read_text(), "[ ]")

    def test_both_exist_but_only_old_has_data_uses_old(self):
        old, new = self._old(), Path(self._appdata()("kinora"))
        (new / "cache").mkdir(parents=True)     # an empty folder, e.g. made by ensure_dirs
        with mock.patch("sys.stderr") as err:
            self.assertEqual(config.migrate_legacy_home(new, old), old)
        self.assertIn("by hand", "".join(c.args[0] for c in err.write.call_args_list))
        self.assertTrue((old / "cameras.json").exists())

    def test_both_exist_and_neither_has_data_uses_new(self):
        old, new = self._old(data=False), Path(self._appdata()("kinora"))
        new.mkdir(parents=True)
        with mock.patch("sys.stderr"):
            self.assertEqual(config.migrate_legacy_home(new, old), new)

    def test_no_old_folder_changes_nothing(self):
        self._patched()
        new = Path(self._appdata()("kinora"))
        self.assertEqual(config.resolve_home(), new)
        self.assertEqual(config.init(), new)
        self.assertFalse(new.exists())
        self.assertFalse(Path(self._appdata()("tapo-cameras")).exists())

    def test_no_lock_file_means_no_old_server(self):
        self._old()
        self.reload_with()
        self.assertFalse(config.legacy_lock_path().exists())
        config.init()
        self.assertFalse(config.legacy_lock_path().exists())   # probing creates nothing


if __name__ == "__main__":
    unittest.main()
