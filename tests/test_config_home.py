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

REAL_DIRS = [Path(platformdirs.user_data_dir("kinora")), Path(platformdirs.user_cache_dir("kinora"))]   # captured before any patch


def _snapshot():
    return {str(p): (p.exists(), p.stat().st_mtime_ns if p.exists() else None) for p in REAL_DIRS}

# TEST-NET address, not a real camera
CAM = {"id": "c1", "name": "Cam", "ip": "192.0.2.10", "auth": "cam_cloud"}
ENV_KEYS = ("KINORA_HOME", "KINORA_PORT")


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

    def test_dev_layout_detection(self):
        self.reload_with()
        checkout = self.tmp / "checkout"
        empty = self.tmp / "empty"
        checkout.mkdir()
        empty.mkdir()
        self.write_cameras([CAM], folder=checkout)
        with mock.patch.object(config, "ROOT", checkout):
            self.assertEqual(config.resolve_home(), checkout)
        fake = lambda name: str(self.tmp / "appdata" / name)
        with mock.patch.object(config, "ROOT", empty), mock.patch.object(config, "user_data_dir", fake):
            self.assertEqual(config.resolve_home(), self.tmp / "appdata" / "kinora")
        # the env override beats the dev layout
        with mock.patch.object(config, "ROOT", checkout), \
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

    def test_env_helper_reads_kinora_names_or_the_default(self):
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        self.assertEqual(config.env("PORT", "8080"), "8080")
        os.environ["KINORA_PORT"] = "2222"
        self.assertEqual(config.env("PORT", "8080"), "2222")
        self.assertIsNone(config.env("NO_SUCH_SETTING"))


if __name__ == "__main__":
    unittest.main()
