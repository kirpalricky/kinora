"""Credential order (env > .env > keyring > macOS security CLI), the no-backend error, the cloud password and the
`set` / `status` helper. Offline: a fake keyring backend, a temp data dir, and the macOS `security` lookup patched out.
Run: .venv/bin/python -m unittest tests.test_creds
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import keyring  # noqa: E402
from keyring.backend import KeyringBackend  # noqa: E402
from keyring.errors import NoKeyringError  # noqa: E402

from kinora import config, creds  # noqa: E402

CAM_PW = "cam-secret-AAA"
CLOUD_PW = "cloud-secret-BBB"
ENV_KEYS = ("TAPO_PASS", "TAPO_USER", "TAPO_CLOUD_PASS", "TAPO_CLOUD_ACCOUNT", "KINORA_DEV_SECRETS")


class FakeKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


class NoBackend(KeyringBackend):
    priority = 1

    def get_password(self, service, username):
        raise NoKeyringError("no backend in this test")

    def set_password(self, service, username, password):
        raise NoKeyringError("no backend in this test")


class CredsBase(unittest.TestCase):
    def setUp(self):
        self._saved_env = {k: os.environ.pop(k) for k in ENV_KEYS if k in os.environ}
        self.addCleanup(self._restore_env)
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        for p in (mock.patch.object(config, "HOME", Path(self.home.name)),
                  mock.patch.object(creds, "_macos_security", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        self.security = creds._macos_security
        self._reset()
        self.addCleanup(self._reset)
        self.saved_backend = keyring.get_keyring()
        self.addCleanup(keyring.set_keyring, self.saved_backend)

    def _restore_env(self):
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(self._saved_env)

    def _reset(self):
        creds.get_credentials.cache_clear()
        creds._cloud = None

    def use_backend(self, backend):
        keyring.set_keyring(backend)
        return backend


class DevBase(CredsBase):
    """Developer fallbacks switched on (KINORA_DEV_SECRETS=1): the legacy env, .env, keyring and Keychain chain."""
    def setUp(self):
        super().setUp()
        os.environ["KINORA_DEV_SECRETS"] = "1"


class ProductPath(CredsBase):
    """Developer switch off (the default): no legacy item, env var or .env value is read, and no lookup is attempted."""
    def test_legacy_items_are_not_used(self):
        kr = self.use_backend(FakeKeyring())
        kr.set_password(creds.SERVICE, "rtspadmin", CAM_PW)
        kr.set_password(creds.CLOUD_SERVICE, "a@example.test", CLOUD_PW)
        self.security.return_value = CAM_PW
        (Path(self.home.name) / ".env").write_text("TAPO_PASS=dotenv-secret\n")
        os.environ.update(TAPO_PASS="env-secret", TAPO_CLOUD_PASS=CLOUD_PW, TAPO_CLOUD_ACCOUNT="a@example.test")
        with self.assertRaises(RuntimeError):
            creds.get_credentials()
        with self.assertRaises(RuntimeError):
            creds.get_cloud_password()
        self.security.assert_not_called()

    def test_global_secrets_are_refused_with_a_plain_message(self):
        with self.assertRaises(RuntimeError) as cm:
            creds.get_credentials()
        self.assertIn("camera account", str(cm.exception))
        self.assertNotIn("TAPO_PASS", str(cm.exception))

    def test_developer_cloud_lookup_needs_the_switch(self):
        os.environ["TAPO_CLOUD_PASS"] = CLOUD_PW
        self.assertIsNone(creds._dev_cloud_password("a@example.test"))
        self.assertFalse(creds.dev_secrets())

    def test_dev_switch_must_be_exactly_1(self):
        for value in ("0", "true", "yes", ""):
            os.environ["KINORA_DEV_SECRETS"] = value
            self.assertFalse(creds.dev_secrets(), value)
        os.environ["KINORA_DEV_SECRETS"] = "1"
        self.assertTrue(creds.dev_secrets())

    def test_announce_logs_one_line_only_when_on(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertFalse(creds.announce_dev_secrets())
        self.assertEqual(out.getvalue(), "")
        os.environ["KINORA_DEV_SECRETS"] = "1"
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertTrue(creds.announce_dev_secrets())
        self.assertEqual(out.getvalue(), "developer credential fallbacks are ON\n")


class CameraCredentials(DevBase):
    def test_env_beats_dotenv_and_keyring(self):
        (Path(self.home.name) / ".env").write_text("TAPO_PASS=from-dotenv\n")
        kr = self.use_backend(FakeKeyring())
        kr.set_password(creds.SERVICE, "rtspadmin", "from-keyring")
        os.environ["TAPO_PASS"] = "from-env"
        self.assertEqual(creds.get_credentials(), ("rtspadmin", "from-env"))

    def test_dotenv_beats_keyring(self):
        (Path(self.home.name) / ".env").write_text("TAPO_PASS=from-dotenv\n")
        kr = self.use_backend(FakeKeyring())
        kr.set_password(creds.SERVICE, "rtspadmin", "from-keyring")
        self.assertEqual(creds.get_credentials(), ("rtspadmin", "from-dotenv"))
        self.security.assert_not_called()

    def test_keyring_used_when_no_env_or_dotenv(self):
        kr = self.use_backend(FakeKeyring())
        kr.set_password(creds.SERVICE, "rtspadmin", CAM_PW)
        self.assertEqual(creds.get_credentials(), ("rtspadmin", CAM_PW))
        self.security.assert_any_call(creds.SERVICE, "rtspadmin")   # tried first, returned nothing

    def test_keyring_account_follows_tapo_user(self):
        kr = self.use_backend(FakeKeyring())
        kr.set_password(creds.SERVICE, "someone", CAM_PW)
        os.environ["TAPO_USER"] = "someone"
        self.assertEqual(creds.get_credentials(), ("someone", CAM_PW))

    def test_security_cli_is_tried_before_the_keyring(self):
        kr = self.use_backend(FakeKeyring())
        kr.set_password(creds.SERVICE, "rtspadmin", "from-keyring")
        self.security.return_value = "from-keychain-cli"
        self.assertEqual(creds.get_credentials(), ("rtspadmin", "from-keychain-cli"))
        self.security.assert_called_with(creds.SERVICE, "rtspadmin")

    def test_security_cli_result_used_when_keyring_is_empty(self):
        self.use_backend(FakeKeyring())          # empty keyring
        self.security.return_value = "from-keychain-cli"
        self.assertEqual(creds.get_credentials(), ("rtspadmin", "from-keychain-cli"))
        self.security.assert_called_with(creds.SERVICE, "rtspadmin")

    def test_no_keyring_backend_falls_through_to_clear_error(self):
        self.use_backend(NoBackend())
        with self.assertRaises(RuntimeError) as cm:
            creds.get_credentials()
        msg = str(cm.exception)
        self.assertIn("TAPO_PASS", msg)
        self.assertIn(".env", msg)
        self.assertIn("keyring", msg)
        self.assertNotIn(CAM_PW, msg)

    def test_no_backend_still_uses_env(self):
        self.use_backend(NoBackend())
        os.environ["TAPO_PASS"] = "env-wins"
        self.assertEqual(creds.get_credentials(), ("rtspadmin", "env-wins"))


class CloudCredentials(DevBase):
    def test_env_cloud_password_first(self):
        kr = self.use_backend(FakeKeyring())
        os.environ["TAPO_CLOUD_ACCOUNT"] = "a@example.test"
        kr.set_password(creds.CLOUD_SERVICE, "a@example.test", "from-keyring")
        os.environ["TAPO_CLOUD_PASS"] = CLOUD_PW
        self.assertEqual(creds.get_cloud_password(), CLOUD_PW)

    def test_keyring_with_account_from_env(self):
        kr = self.use_backend(FakeKeyring())
        kr.set_password(creds.CLOUD_SERVICE, "a@example.test", CLOUD_PW)
        os.environ["TAPO_CLOUD_ACCOUNT"] = "a@example.test"
        self.assertEqual(creds.get_cloud_password(), CLOUD_PW)
        self.security.assert_not_called()

    def test_no_bare_cloud_lookup_without_an_account(self):
        self.use_backend(FakeKeyring())
        self.security.return_value = CLOUD_PW
        with self.assertRaises(RuntimeError):
            creds.get_cloud_password()
        self.security.assert_not_called()   # never the bare -s kinora-cloud lookup

    def test_macos_security_with_the_account(self):
        self.use_backend(FakeKeyring())
        os.environ["TAPO_CLOUD_ACCOUNT"] = "a@example.test"
        self.security.return_value = CLOUD_PW
        self.assertEqual(creds.get_cloud_password(), CLOUD_PW)
        self.security.assert_called_with(creds.CLOUD_SERVICE, "a@example.test")

    def test_cached_once_found(self):
        os.environ["TAPO_CLOUD_PASS"] = CLOUD_PW
        creds.get_cloud_password()
        del os.environ["TAPO_CLOUD_PASS"]
        self.assertEqual(creds.get_cloud_password(), CLOUD_PW)

    def test_missing_cloud_password_is_a_clear_error(self):
        self.use_backend(NoBackend())
        with self.assertRaises(RuntimeError) as cm:
            creds.get_cloud_password()
        self.assertIn("TAPO_CLOUD_PASS", str(cm.exception))


class Helper(CredsBase):
    def test_set_uses_getpass_and_stores_in_keyring(self):
        kr = self.use_backend(FakeKeyring())
        out = io.StringIO()
        with mock.patch("getpass.getpass", return_value=CAM_PW) as gp, contextlib.redirect_stdout(out):
            self.assertEqual(creds.main(["set"]), 0)
        gp.assert_called_once()
        self.assertEqual(kr.get_password(creds.SERVICE, "rtspadmin"), CAM_PW)
        self.assertNotIn(CAM_PW, out.getvalue())

    def test_set_cloud_stores_under_cloud_service(self):
        kr = self.use_backend(FakeKeyring())
        os.environ["TAPO_CLOUD_ACCOUNT"] = "a@example.test"
        with mock.patch("getpass.getpass", return_value=CLOUD_PW), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(creds.main(["set", "--cloud"]), 0)
        self.assertEqual(kr.get_password(creds.CLOUD_SERVICE, "a@example.test"), CLOUD_PW)

    def test_set_without_backend_is_a_clean_failure(self):
        self.use_backend(NoBackend())
        err = io.StringIO()
        with mock.patch("getpass.getpass", return_value=CAM_PW), contextlib.redirect_stderr(err):
            self.assertEqual(creds.main(["set"]), 1)
        self.assertIn("TAPO_PASS", err.getvalue())
        self.assertNotIn(CAM_PW, err.getvalue())

    def test_status_in_product_mode_reads_no_legacy_item(self):
        kr = self.use_backend(FakeKeyring())
        kr.set_password(creds.SERVICE, "rtspadmin", CAM_PW)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(creds.main(["status"]), 0)
        self.assertIn("developer fallbacks: off", out.getvalue())
        self.assertNotIn("OS keyring", out.getvalue())
        self.assertNotIn(CAM_PW, out.getvalue())

    def test_status_prints_sources_never_values(self):
        kr = self.use_backend(FakeKeyring())
        kr.set_password(creds.SERVICE, "rtspadmin", CAM_PW)
        os.environ["KINORA_DEV_SECRETS"] = "1"
        os.environ["TAPO_CLOUD_PASS"] = CLOUD_PW
        (Path(self.home.name) / ".env").write_text("TAPO_PASS=dotenv-secret-CCC\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(creds.main(["status"]), 0)
        text = out.getvalue()
        for secret in (CAM_PW, CLOUD_PW, "dotenv-secret-CCC"):
            self.assertNotIn(secret, text)
        self.assertIn("OS keyring (service kinora or tapo-cameras): set", text)
        self.assertIn("TAPO_CLOUD_PASS in environment: set", text)
        self.assertIn("TAPO_PASS in environment: not set", text)
        self.assertIn("TAPO_PASS in .env: set", text)

    def test_unknown_subcommand_is_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(creds.main(["show"]), 2)


if __name__ == "__main__":
    unittest.main()
