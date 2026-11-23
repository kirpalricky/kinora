"""Per-camera secrets (wizard): own store per camera, global fallback, credential_store modes, cache invalidation,
and no secret value in errors or in secret_sources. Offline: fake keyring, temp data dir, macOS `security` patched out.
Run: .venv/bin/python -m unittest tests.test_creds_percam   (from the repo root)
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import keyring  # noqa: E402
from keyring.backend import KeyringBackend  # noqa: E402
from keyring.errors import NoKeyringError  # noqa: E402

from kinora import config, creds  # noqa: E402

SENTINEL = "PW-SENTINEL-7731"        # distinctive fake secrets: scanned for in every message and output
CAM_PW = SENTINEL + "-cam"
GLOBAL_PW = SENTINEL + "-global"
CLOUD_PW = SENTINEL + "-cloud"
EMAIL = "Owner@Example.test"
ENV_KEYS = ("TAPO_PASS", "TAPO_USER", "TAPO_CLOUD_PASS", "TAPO_CLOUD_ACCOUNT", "KINORA_DEV_SECRETS")
NO_ACCOUNT = "this camera has no camera account password stored: add it in Settings"
KITCHEN = {"id": "cK", "name": "Kitchen", "ip": "10.5.5.5", "auth": "cam_cloud"}
LIVING = {"id": "cL", "name": "Living", "ip": "10.5.5.6", "auth": "cam_cloud", "device_id": " DEV-L "}


class FakeKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.store = {}
        self.calls = 0

    def get_password(self, service, username):
        self.calls += 1
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        from keyring.errors import PasswordDeleteError
        if (service, username) not in self.store:
            raise PasswordDeleteError("not found")
        del self.store[(service, username)]


class NoBackend(KeyringBackend):
    priority = 1

    def get_password(self, service, username):
        raise NoKeyringError("no backend in this test")

    def set_password(self, service, username, password):
        raise NoKeyringError("no backend in this test")

    def delete_password(self, service, username):
        raise NoKeyringError("no backend in this test")


class Base(unittest.TestCase):
    def setUp(self):
        self._saved_env = {k: os.environ.pop(k) for k in ENV_KEYS if k in os.environ}
        self.addCleanup(self._restore_env)
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.dir = Path(self.home.name)
        for p in (mock.patch.object(config, "HOME", self.dir),
                  mock.patch.object(creds, "_macos_security", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        self.saved_backend = keyring.get_keyring()
        self.addCleanup(keyring.set_keyring, self.saved_backend)
        self.kr = FakeKeyring()
        keyring.set_keyring(self.kr)
        creds._cloud = None
        creds.get_credentials.cache_clear()
        self.addCleanup(creds._invalidate)

    def _restore_env(self):
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(self._saved_env)

    def settings(self, **kw):
        (self.dir / "settings.json").write_text(json.dumps(kw))

    def file_mode(self, store):
        self.settings(credential_store=store)


class PerCameraLookup(Base):
    def test_camera_secret_wins_over_global_env(self):
        os.environ["TAPO_PASS"] = GLOBAL_PW
        creds.set_camera_secret(KITCHEN, "kitchen-user", CAM_PW)
        self.assertEqual(creds.get_camera_credentials(KITCHEN), ("kitchen-user", CAM_PW))

    def test_camera_without_secret_has_no_fallback_in_the_product_path(self):
        os.environ["TAPO_PASS"] = GLOBAL_PW
        creds.set_camera_secret(KITCHEN, "kitchen-user", CAM_PW)
        with self.assertRaises(RuntimeError) as cm:
            creds.get_camera_credentials(LIVING)
        self.assertEqual(str(cm.exception), NO_ACCOUNT)
        self.assertEqual(creds.secret_sources(LIVING)["camera"], "missing")

    def test_legacy_keyring_item_is_not_used_without_the_dev_switch(self):
        self.kr.set_password(creds.SERVICE, "rtspadmin", GLOBAL_PW)
        with self.assertRaises(RuntimeError):
            creds.get_camera_credentials(KITCHEN)
        self.assertEqual(creds.secret_sources(KITCHEN)["camera"], "missing")

    def test_store_keys_always_carry_the_wizard_prefix(self):
        for cam in (KITCHEN, LIVING, {"id": "rtspadmin", "name": "X"}):
            self.assertTrue(creds.camera_key(cam).startswith("camera:"))
        self.assertTrue(creds.cloud_key(EMAIL).startswith("cloud:"))
        # another item under the same service (the legacy camera account name) is never read as a wizard secret
        self.kr.set_password(creds.SERVICE, "rtspadmin", '{"user": "x", "password": "%s"}' % CAM_PW)
        with self.assertRaises(ValueError):
            creds._store_get("rtspadmin")
        with self.assertRaises(ValueError):
            creds._store_set("rtspadmin", "v")
        with self.assertRaises(ValueError):
            creds._store_delete("rtspadmin")
        self.assertEqual(creds.secret_sources({"id": "rtspadmin", "name": "X"})["camera"], "missing")
        self.assertEqual(self.kr.store[(creds.SERVICE, "rtspadmin")], '{"user": "x", "password": "%s"}' % CAM_PW)

    def test_camera_state_returns_user_and_presence_from_one_read_and_no_password(self):
        creds.set_camera_secret(KITCHEN, "kitchen-user", CAM_PW)
        self.kr.calls = 0
        state = creds.camera_state(KITCHEN)
        self.assertEqual(self.kr.calls, 1)   # the camera's own item is read once (the cloud item is a separate read)
        self.assertEqual(state["camera_user"], "kitchen-user")
        self.assertTrue(state["has_camera_account"])
        self.assertEqual(state["secret_sources"]["camera"], "wizard")
        self.assertNotIn(CAM_PW, repr(state))
        missing = creds.camera_state(LIVING)
        self.assertEqual((missing["camera_user"], missing["has_camera_account"]), (None, False))

    def test_camera_state_user_is_none_for_an_unreadable_record(self):
        self.kr.set_password(creds.SERVICE, "camera:cK", "not json " + CAM_PW)
        state = creds.camera_state(KITCHEN)
        self.assertIsNone(state["camera_user"])
        self.assertTrue(state["has_camera_account"])
        self.assertNotIn(CAM_PW, repr(state))

    def test_dev_legacy_keyring_used_without_wizard_secret(self):
        os.environ["KINORA_DEV_SECRETS"] = "1"
        self.kr.set_password(creds.SERVICE, "rtspadmin", GLOBAL_PW)
        self.assertEqual(creds.get_camera_credentials(KITCHEN), ("rtspadmin", GLOBAL_PW))
        self.assertEqual(creds.secret_sources(KITCHEN)["camera"], "dev_override")
        self.assertIsNone(creds.camera_state(KITCHEN)["camera_user"])   # not the wizard's account

    def test_store_key_uses_normalised_device_id_else_camera_id(self):
        creds.set_camera_secret(LIVING, "u", CAM_PW)
        creds.set_camera_secret(KITCHEN, "u", CAM_PW)
        self.assertIn((creds.SERVICE, "camera:dev-l"), self.kr.store)
        self.assertIn((creds.SERVICE, "camera:cK"), self.kr.store)

    def test_cloud_secret_wins_over_global_cloud_password(self):
        creds.set_account_email(EMAIL)
        os.environ["TAPO_CLOUD_PASS"] = GLOBAL_PW
        creds.set_cloud_secret("owner@example.test", CLOUD_PW)   # stored under the lower-cased email
        self.assertIn((creds.SERVICE, "cloud:owner@example.test"), self.kr.store)
        self.assertEqual(creds.get_cloud_password_for(KITCHEN), CLOUD_PW)

    def test_cloud_without_wizard_secret_is_refused_in_the_product_path(self):
        os.environ["TAPO_CLOUD_PASS"] = GLOBAL_PW
        with self.assertRaises(RuntimeError) as cm:          # no saved account email
            creds.get_cloud_password_for(KITCHEN)
        self.assertEqual(str(cm.exception), "Save your TP-Link account first")
        creds.set_account_email(EMAIL)
        with self.assertRaises(RuntimeError) as cm:          # saved email, no wizard secret for it
            creds.get_cloud_password_for(KITCHEN)
        self.assertEqual(str(cm.exception), "Save your TP-Link account first")
        self.assertEqual(creds.secret_sources(KITCHEN)["cloud"], "missing")
        self.assertNotIn(GLOBAL_PW, str(cm.exception))

    def test_cloud_item_for_another_account_is_never_used(self):
        creds.set_account_email(EMAIL)
        self.kr.set_password(creds.CLOUD_SERVICE, "owner@example.test", CLOUD_PW)   # legacy item: product ignores it
        self.kr.set_password(creds.SERVICE, "cloud:other@example.test", GLOBAL_PW)  # wizard item of another account
        with self.assertRaises(RuntimeError):
            creds.get_cloud_password_for(KITCHEN)

    def test_dev_cloud_lookup_is_for_the_saved_account_only(self):
        os.environ["KINORA_DEV_SECRETS"] = "1"
        os.environ["TAPO_CLOUD_PASS"] = GLOBAL_PW
        with self.assertRaises(RuntimeError):                 # no saved email: nothing is looked up
            creds.get_cloud_password_for(KITCHEN)
        creds.set_account_email(EMAIL)
        self.assertEqual(creds.get_cloud_password_for(KITCHEN), GLOBAL_PW)
        self.assertEqual(creds.secret_sources(KITCHEN)["cloud"], "dev_override")

    def test_corrupt_stored_login_is_a_clear_error_without_the_value(self):
        self.kr.set_password(creds.SERVICE, "camera:cK", "not json " + CAM_PW)
        with self.assertRaises(RuntimeError) as cm:
            creds.get_camera_credentials(KITCHEN)
        self.assertIn("unreadable", str(cm.exception))
        self.assertNotIn(CAM_PW, str(cm.exception))


class WritesAndCache(Base):
    def test_delete_has_no_fallback_in_the_product_path(self):
        os.environ["TAPO_PASS"] = GLOBAL_PW
        creds.set_camera_secret(KITCHEN, "kitchen-user", CAM_PW)
        creds.delete_camera_secret(KITCHEN)
        with self.assertRaises(RuntimeError):
            creds.get_camera_credentials(KITCHEN)
        self.assertEqual(creds.secret_sources(KITCHEN)["camera"], "missing")

    def test_changed_password_is_used_on_the_next_login(self):
        creds.set_camera_secret(KITCHEN, "kitchen-user", CAM_PW)
        self.assertEqual(creds.get_camera_credentials(KITCHEN)[1], CAM_PW)
        creds.set_camera_secret(KITCHEN, "kitchen-user", CAM_PW + "2")
        self.assertEqual(creds.get_camera_credentials(KITCHEN), ("kitchen-user", CAM_PW + "2"))

    def test_delete_falls_back_to_global_only_in_dev_mode(self):
        os.environ["KINORA_DEV_SECRETS"] = "1"
        os.environ["TAPO_PASS"] = GLOBAL_PW
        creds.set_camera_secret(KITCHEN, "kitchen-user", CAM_PW)
        creds.delete_camera_secret(KITCHEN)
        self.assertEqual(creds.get_camera_credentials(KITCHEN), ("rtspadmin", GLOBAL_PW))

    def test_write_clears_the_global_get_credentials_cache(self):
        os.environ["KINORA_DEV_SECRETS"] = "1"
        self.kr.set_password(creds.SERVICE, "rtspadmin", "old-" + GLOBAL_PW)
        self.assertEqual(creds.get_credentials(), ("rtspadmin", "old-" + GLOBAL_PW))   # cached
        self.kr.set_password(creds.SERVICE, "rtspadmin", GLOBAL_PW)                    # changed behind its back
        creds.set_camera_secret(KITCHEN, "kitchen-user", CAM_PW)                       # any write invalidates
        self.assertEqual(creds.get_credentials(), ("rtspadmin", GLOBAL_PW))

    def test_write_clears_the_cached_cloud_password(self):
        os.environ["KINORA_DEV_SECRETS"] = "1"
        os.environ["TAPO_CLOUD_ACCOUNT"] = EMAIL
        os.environ["TAPO_CLOUD_PASS"] = "old-" + CLOUD_PW
        self.assertEqual(creds.get_cloud_password(), "old-" + CLOUD_PW)                # cached in _cloud
        os.environ["TAPO_CLOUD_PASS"] = CLOUD_PW                                       # corrected in the environment
        creds.set_camera_secret(KITCHEN, "u", CAM_PW)                                  # any write invalidates
        self.assertEqual(creds.get_cloud_password(), CLOUD_PW)
        creds.set_account_email(EMAIL)
        self.assertEqual(creds.get_cloud_password_for(KITCHEN), CLOUD_PW)              # per saved account, not cached

    def test_delete_of_missing_secret_is_not_an_error(self):
        creds.delete_camera_secret(KITCHEN)
        self.assertEqual(creds.secret_sources(KITCHEN)["camera"], "missing")

    def test_empty_user_or_password_refused(self):
        with self.assertRaises(ValueError) as cm:
            creds.set_camera_secret(KITCHEN, "  ", CAM_PW)
        self.assertNotIn(CAM_PW, str(cm.exception))
        with self.assertRaises(ValueError):
            creds.set_camera_secret(KITCHEN, "u", "")
        with self.assertRaises(ValueError):
            creds.set_cloud_secret("", CLOUD_PW)


class FileStore(Base):
    def setUp(self):
        super().setUp()
        self.file_mode("file")

    def test_file_is_created_0600_and_is_used(self):
        creds.set_camera_secret(KITCHEN, "kitchen-user", CAM_PW)
        f = self.dir / "secrets.json"
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        self.assertFalse((self.dir / "secrets.json.tmp").exists())
        self.assertEqual(creds.get_camera_credentials(KITCHEN), ("kitchen-user", CAM_PW))
        self.assertEqual(self.kr.calls, 0)   # the keyring is not used in file mode

    def test_replace_keeps_0600(self):
        creds.set_camera_secret(KITCHEN, "u", CAM_PW)
        creds.set_cloud_secret(EMAIL, CLOUD_PW)
        self.assertEqual(stat.S_IMODE((self.dir / "secrets.json").stat().st_mode), 0o600)
        self.assertEqual(json.loads((self.dir / "secrets.json").read_text())["cloud:owner@example.test"], CLOUD_PW)

    def test_stale_temp_file_from_a_crash_is_replaced(self):
        (self.dir / "secrets.json.tmp").write_text("junk")
        creds.set_camera_secret(KITCHEN, "u", CAM_PW)
        self.assertEqual(creds.get_camera_credentials(KITCHEN), ("u", CAM_PW))

    @unittest.skipIf(os.name == "nt", "POSIX mode bits only")
    def test_group_or_other_readable_file_is_refused(self):
        creds.set_camera_secret(KITCHEN, "u", CAM_PW)
        os.chmod(self.dir / "secrets.json", 0o644)
        with self.assertRaises(RuntimeError) as cm:
            creds.get_camera_credentials(KITCHEN)
        msg = str(cm.exception)
        self.assertIn("chmod 600", msg)
        self.assertNotIn(CAM_PW, msg)
        with self.assertRaises(RuntimeError):
            creds.set_camera_secret(LIVING, "u", CAM_PW)   # a write does not bypass the check either

    def test_missing_file_means_no_secret(self):
        self.assertFalse((self.dir / "secrets.json").exists())
        self.assertEqual(creds.secret_sources(KITCHEN)["camera"], "missing")


class EnvStore(Base):
    def setUp(self):
        super().setUp()
        self.file_mode("env")

    def test_writes_and_deletes_are_refused(self):
        for call in (lambda: creds.set_camera_secret(KITCHEN, "u", CAM_PW),
                     lambda: creds.set_cloud_secret(EMAIL, CLOUD_PW),
                     lambda: creds.delete_camera_secret(KITCHEN)):
            with self.assertRaises(RuntimeError) as cm:
                call()
            self.assertIn("read-only", str(cm.exception))
            self.assertNotIn(CAM_PW, str(cm.exception))
        self.assertEqual(self.kr.store, {})
        self.assertFalse((self.dir / "secrets.json").exists())

    def test_reads_come_from_env_only(self):
        os.environ["KINORA_DEV_SECRETS"] = "1"
        os.environ["TAPO_PASS"] = GLOBAL_PW
        self.kr.set_password(creds.SERVICE, "camera:cK", '{"user": "u", "password": "%s"}' % CAM_PW)
        self.assertEqual(creds.get_camera_credentials(KITCHEN), ("rtspadmin", GLOBAL_PW))
        self.assertEqual(creds.secret_sources(KITCHEN)["camera"], "dev_override")
        self.assertEqual(self.kr.calls, 0)   # no per-camera store in env mode

    def test_unknown_store_is_refused(self):
        self.settings(credential_store="cloud")
        with self.assertRaises(RuntimeError) as cm:
            creds.credential_store()
        self.assertIn("keyring, file, env", str(cm.exception))


class KeyringStore(Base):
    """Reads and deletes in the "kinora" keyring service."""
    KEY = "camera:ck"
    VALUE = '{"user": "u", "password": "%s"}' % CAM_PW

    def test_stored_item_is_read_back(self):
        self.kr.set_password(creds.SERVICE, self.KEY, self.VALUE)
        self.assertEqual(creds._store_get(self.KEY), self.VALUE)

    def test_nothing_stored_is_none(self):
        self.assertIsNone(creds._store_get(self.KEY))
        self.assertEqual(self.kr.store, {})

    def test_unreadable_store_raises_and_leaks_nothing(self):
        from keyring.errors import KeyringLocked
        self.kr.set_password(creds.SERVICE, self.KEY, self.VALUE)
        with mock.patch.object(self.kr, "get_password", side_effect=KeyringLocked("locked")):
            with self.assertRaises(RuntimeError) as cm:
                creds._store_get(self.KEY)
        self.assertNotIn(SENTINEL, str(cm.exception))

    def test_delete_removes_the_item(self):
        self.kr.set_password(creds.SERVICE, self.KEY, "a")
        creds._store_delete(self.KEY)
        self.assertIsNone(creds._store_get(self.KEY))

    def test_failed_delete_keeps_the_item_and_leaks_nothing(self):
        from keyring.errors import KeyringError
        self.kr.set_password(creds.SERVICE, self.KEY, self.VALUE)
        with mock.patch.object(self.kr, "delete_password", side_effect=KeyringError("locked")):
            with self.assertRaises(RuntimeError) as cm:
                creds._store_delete(self.KEY)
        self.assertNotIn(CAM_PW, str(cm.exception))
        self.assertEqual(creds._store_get(self.KEY), self.VALUE)


class NoKeyring(Base):
    def setUp(self):
        super().setUp()
        keyring.set_keyring(NoBackend())

    def test_developer_env_still_works_without_backend_when_switched_on(self):
        os.environ["KINORA_DEV_SECRETS"] = "1"
        os.environ["TAPO_PASS"] = GLOBAL_PW
        self.assertEqual(creds.get_camera_credentials(KITCHEN), ("rtspadmin", GLOBAL_PW))

    def test_product_path_without_backend_and_without_wizard_secret_is_refused(self):
        os.environ["TAPO_PASS"] = GLOBAL_PW
        with self.assertRaises(RuntimeError) as cm:
            creds.get_camera_credentials(KITCHEN)
        self.assertEqual(str(cm.exception), NO_ACCOUNT)
        self.assertEqual(creds.secret_sources(KITCHEN)["camera"], "missing")

    def test_write_without_backend_is_a_clear_error(self):
        with self.assertRaises(RuntimeError) as cm:
            creds.set_camera_secret(KITCHEN, "u", CAM_PW)
        self.assertIn("file", str(cm.exception))
        self.assertNotIn(CAM_PW, str(cm.exception))

    def test_backend_status_reports_no_backend(self):
        status = creds.backend_status()
        self.assertFalse(status["ok"])
        self.assertTrue(status["reason"])

    def test_backend_status_reports_a_working_backend(self):
        keyring.set_keyring(FakeKeyring())
        self.assertTrue(creds.backend_status()["ok"])


class Sources(Base):
    def test_each_secret_names_its_source(self):
        self.assertEqual(creds.secret_sources(KITCHEN), {"camera": "missing", "cloud": "missing"})
        self.kr.set_password(creds.SERVICE, "rtspadmin", GLOBAL_PW)           # legacy item: not a source here
        os.environ["TAPO_PASS"] = GLOBAL_PW
        os.environ["TAPO_CLOUD_PASS"] = CLOUD_PW
        self.assertEqual(creds.secret_sources(KITCHEN), {"camera": "missing", "cloud": "missing"})
        os.environ["KINORA_DEV_SECRETS"] = "1"
        self.assertEqual(creds.secret_sources(KITCHEN)["camera"], "dev_override")
        creds.set_camera_secret(KITCHEN, "u", CAM_PW)
        self.assertEqual(creds.secret_sources(KITCHEN)["camera"], "wizard")
        self.assertEqual(creds.secret_sources(KITCHEN)["cloud"], "missing")     # no saved account email yet
        creds.set_account_email(EMAIL)
        self.assertEqual(creds.secret_sources(KITCHEN)["cloud"], "dev_override")
        creds.set_cloud_secret(EMAIL, CLOUD_PW)
        self.assertEqual(creds.secret_sources(KITCHEN)["cloud"], "wizard")
        self.assertEqual(set(creds.secret_sources(KITCHEN).values()), {"wizard"})

    def test_sources_and_errors_never_contain_a_value(self):
        os.environ["TAPO_PASS"] = GLOBAL_PW
        os.environ["TAPO_CLOUD_PASS"] = CLOUD_PW
        creds.set_account_email(EMAIL)
        creds.set_camera_secret(KITCHEN, "u", CAM_PW)
        creds.set_cloud_secret(EMAIL, CLOUD_PW)
        texts = [repr(creds.secret_sources(KITCHEN)), repr(creds.secret_sources(LIVING)),
                 repr(creds.backend_status()), repr(creds.load_settings())]
        for t in texts:
            for secret in (CAM_PW, GLOBAL_PW, CLOUD_PW):
                self.assertNotIn(secret, t)

    def test_settings_file_is_atomic_and_holds_no_secret(self):
        creds.set_account_email("  " + EMAIL + " ")
        self.assertEqual(creds.account_email(), EMAIL)
        self.assertFalse((self.dir / "settings.json.tmp").exists())
        self.assertNotIn(CAM_PW, (self.dir / "settings.json").read_text())
        self.assertEqual(creds.credential_store(), "keyring")   # default when the key is absent
        with self.assertRaises(ValueError):
            creds.set_account_email("   ")


class ErrorsDoNotLeak(Base):
    def test_no_global_secret_error_names_a_value(self):
        self.kr.set_password(creds.SERVICE, "rtspadmin", "")  # empty: treated as not stored
        with self.assertRaises(RuntimeError) as cm:
            creds.get_camera_credentials(KITCHEN)
        self.assertNotIn(SENTINEL, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
