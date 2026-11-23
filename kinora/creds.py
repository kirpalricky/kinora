"""Credentials. The product path uses only the secrets the setup wizard stored: one camera account per camera, and one
TP-Link (cloud) account whose email is in settings.json (`account_email`).

Per-camera secrets live in the store chosen by `credential_store` in <HOME>/settings.json:
    keyring (default)  OS keyring, service "kinora" (items under the old service "tapo-cameras" are copied over on first read), username "camera:<device_id or id>" / "cloud:<email>"
    file               <HOME>/secrets.json, mode 0600 (refused when group/other can read it)
    env                read-only: no per-camera store; writes are refused
A store that cannot be read raises RuntimeError: it is never read as "no secret" (no login, no failure counted).
Every store key starts with "camera:" or "cloud:": other items under the same service are never read or written.

Developer fallbacks are OFF unless the environment has KINORA_DEV_SECRETS=1 (the server logs one line when they are on):
TAPO_PASS / TAPO_CLOUD_PASS / TAPO_USER environment variables, the .env file in the data folder, the macOS `security`
CLI, and the older global keyring items (service "kinora" or the old "tapo-cameras", account = camera user; "kinora-cloud" or the old "tapo-cameras-cloud",
account = the saved TP-Link email). The cloud item is looked up only for the saved email, never without an account.
A secret that comes from a developer fallback is reported as "dev_override".

Store and check (the password is read with getpass: never echoed, never on the command line):
    python -m kinora.creds set            global camera password (developers; user from TAPO_USER, default rtspadmin)
    python -m kinora.creds set --cloud    global TP-Link password (developers; account TAPO_CLOUD_ACCOUNT or asked)
    python -m kinora.creds status         which sources exist; never prints a value
"""
import getpass
import json
import os
import stat
import subprocess
import sys
from functools import lru_cache

import keyring
from keyring import errors as keyring_errors
from keyring.backends import fail
from keyring.errors import KeyringError

from kinora import config

SERVICE = "kinora"
CLOUD_SERVICE = "kinora-cloud"
# Names used before the rename to Kinora. Read-through migration copies items from them to the new services;
# the old items are never deleted automatically.
OLD_SERVICE = "tapo-cameras"
OLD_CLOUD_SERVICE = "tapo-cameras-cloud"
STORES = ("keyring", "file", "env")
DEV_ENV = "KINORA_DEV_SECRETS"
KEY_PREFIXES = ("camera:", "cloud:")
NO_ACCOUNT = "this camera has no camera account password stored: add it in Settings"
NO_CLOUD = "Save your TP-Link account first"


def dev_secrets():
    """True when the developer fallbacks are switched on (KINORA_DEV_SECRETS=1 in the environment)."""
    return config.env("DEV_SECRETS") == "1"


def announce_dev_secrets():
    """Logs one line (no values) at startup when the developer fallbacks are on. Returns the flag."""
    on = dev_secrets()
    if on:
        print("developer credential fallbacks are ON", file=sys.stderr)
    return on


def _from_dotenv(key):
    # config.HOME is the repo in the legacy layout, so this is ROOT/.env there
    f = config.HOME / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip("'\"")


def _keyring_get(service, account):
    """Password from the OS keyring, or None (no backend, locked, or not stored). Developer lookups only."""
    try:
        return keyring.get_password(service, account)
    except KeyringError:   # includes NoKeyringError: no usable backend on this system
        return None


def _macos_security(service, account=None):
    """macOS Keychain through the `security` CLI. None on other systems or when not found. Developer lookups only."""
    if sys.platform != "darwin":
        return None
    argv = ["security", "find-generic-password", "-s", service]
    if account:
        argv += ["-a", account]
    try:
        r = subprocess.run(argv + ["-w"], capture_output=True, text=True)
    except OSError:
        return None
    pw = r.stdout.strip() if r.returncode == 0 else ""
    return pw or None


def _camera_user():
    return os.environ.get("TAPO_USER") or _from_dotenv("TAPO_USER") or "rtspadmin"


def _stored_camera_password(user):
    # Developer only. macOS: the `security` CLI first. The keyring backend may show a GUI prompt, and this runs while
    # the login gate's file lock is held, so a prompt there would stall every other process on the lock.
    return (_macos_security(SERVICE, user) or _macos_security(OLD_SERVICE, user)
            or _keyring_get(SERVICE, user) or _keyring_get(OLD_SERVICE, user))


@lru_cache(maxsize=1)
def get_credentials():
    """Developer fallback (KINORA_DEV_SECRETS=1): the global camera login from TAPO_PASS, .env or the legacy keyring or
    Keychain item. Refused otherwise: the product path never reads these."""
    if not dev_secrets():
        raise RuntimeError(NO_ACCOUNT)
    user = _camera_user()
    pw = os.environ.get("TAPO_PASS") or _from_dotenv("TAPO_PASS") or _stored_camera_password(user)
    if not pw:
        raise RuntimeError(
            "No camera password found. Use one of:\n"
            "  - environment: export TAPO_PASS=...\n"
            "  - .env file in the data directory: TAPO_PASS=...\n"
            "  - OS keyring: python -m kinora.creds set\n"
            "Check what is set (values are never printed): python -m kinora.creds status"
        )
    return user, pw


_cloud = None


def get_cloud_password():
    """Developer fallback (KINORA_DEV_SECRETS=1): the global TP-Link password from TAPO_CLOUD_PASS, or the keyring or
    Keychain item of account TAPO_CLOUD_ACCOUNT (an account is always required: no bare lookup). Cached once found."""
    global _cloud
    if not dev_secrets():
        raise RuntimeError(NO_CLOUD)
    if _cloud:
        return _cloud
    pw = os.environ.get("TAPO_CLOUD_PASS")
    account = os.environ.get("TAPO_CLOUD_ACCOUNT")
    if not pw and account:
        pw = _dev_cloud_item(account)
    if not pw:
        raise RuntimeError(
            "No Tapo cloud password found. Set TAPO_CLOUD_PASS, or store it with: "
            "python -m kinora.creds set --cloud (with TAPO_CLOUD_ACCOUNT set to the account email)"
        )
    _cloud = pw
    return pw


def _dev_cloud_item(account):
    """Global TP-Link password item for `account`: the new service first, then the pre-rename one."""
    return (_keyring_get(CLOUD_SERVICE, account) or _macos_security(CLOUD_SERVICE, account)
            or _keyring_get(OLD_CLOUD_SERVICE, account) or _macos_security(OLD_CLOUD_SERVICE, account))


def _dev_cloud_password(email):
    """Developer fallback for the saved TP-Link account only: TAPO_CLOUD_PASS, or the keyring / Keychain item whose
    account is exactly `email`. Never a bare lookup, and nothing is cached."""
    if not dev_secrets() or not email:
        return None
    pw = os.environ.get("TAPO_CLOUD_PASS")
    return pw or _dev_cloud_item(email)


# ---- settings and per-camera secrets (wizard) --------------------------------------------

def _atomic_write(path, text, mode):
    """Write a temp file in the same folder (created with `mode`, O_EXCL), fsync, then os.replace."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)   # a stale temp file from a crash
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_settings():
    """<HOME>/settings.json as a dict ({} when missing). Non-secret settings only."""
    f = config.HOME / "settings.json"
    try:
        text = f.read_text()
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        raise RuntimeError(f"{f} is not valid JSON: fix it or remove it") from None
    if not isinstance(data, dict):
        raise RuntimeError(f"{f} must contain a JSON object")
    return data


def save_settings(data):
    _atomic_write(config.HOME / "settings.json", json.dumps(data, indent=2, sort_keys=True), 0o644)


def set_account_email(email):
    """The TP-Link account email (not a secret). Used to find the cloud secret."""
    email = (email or "").strip()
    if not email:
        raise ValueError("the account email must not be empty")
    data = load_settings()
    data["account_email"] = email
    save_settings(data)


def account_email():
    return (load_settings().get("account_email") or "").strip() or None


def credential_store():
    mode = load_settings().get("credential_store", "keyring")
    if mode not in STORES:
        raise RuntimeError(f"credential_store in settings.json must be one of {', '.join(STORES)} (got {mode!r})")
    return mode


def backend_status():
    """Whether the OS keyring can store secrets here. Never returns a value."""
    try:
        kr = keyring.get_keyring()
    except KeyringError:
        return {"ok": False, "backend": None, "reason": "no keyring backend could be loaded"}
    name = f"{type(kr).__module__}.{type(kr).__name__}"
    if isinstance(kr, fail.Keyring):
        return {"ok": False, "backend": name, "reason": "no usable OS keyring on this system"}
    try:
        keyring.get_password(SERVICE, "__probe__")
    except KeyringError:
        return {"ok": False, "backend": name, "reason": "the OS keyring refused access"}
    return {"ok": True, "backend": name, "reason": ""}


def camera_key(cam):
    """Store key of a camera's secret: device_id (normalised as in gate.gate_key) when set, else the camera id."""
    dev = cam.get("device_id")
    return "camera:" + (str(dev).strip().lower() if dev else cam["id"])


def cloud_key(email):
    return "cloud:" + email.strip().lower()


def _secrets_path():
    return config.HOME / "secrets.json"


def _file_read():
    """<HOME>/secrets.json as a dict ({} when missing). Refused when group or other users can read it."""
    f = _secrets_path()
    try:
        st = f.stat()
    except FileNotFoundError:
        return {}
    if os.name != "nt" and stat.S_IMODE(st.st_mode) & 0o077:
        raise RuntimeError(f"{f} is readable by other users (mode {stat.S_IMODE(st.st_mode):o}), so it was not read. "
                           f"Restrict it with: chmod 600 {f}")
    try:
        data = json.loads(f.read_text())
    except ValueError:
        raise RuntimeError(f"{f} is not valid JSON") from None
    if not isinstance(data, dict):
        raise RuntimeError(f"{f} must contain a JSON object")
    return data


def _file_write(data):
    _atomic_write(_secrets_path(), json.dumps(data, indent=2, sort_keys=True), 0o600)


_READ_ONLY = ("credential_store is env: secrets are read-only here. Set the passwords in the environment, or set "
              "credential_store in settings.json to keyring or file.")


def _check_key(key):
    """Only wizard keys ("camera:..." / "cloud:...") are read or written: other items under SERVICE are not ours."""
    if not str(key).startswith(KEY_PREFIXES):
        raise ValueError("not a wizard secret key")


def _store_get(key):
    """The stored secret for `key`, or None when no item exists. A keyring with no usable backend holds nothing, so
    existing setups without a backend are unaffected. Any other keyring error (a denied prompt, a locked keychain)
    raises: it must never read as "no secret"."""
    _check_key(key)
    mode = credential_store()
    if mode == "keyring":
        try:
            found = keyring.get_password(SERVICE, key)
            if found:
                return found
            old = keyring.get_password(OLD_SERVICE, key)   # pre-rename item: copy it over, keep the old one
            if old:
                try:
                    keyring.set_password(SERVICE, key, old)
                except KeyringError:
                    pass   # still usable from the old item; the copy is retried on the next read
            return old or None
        except keyring_errors.NoKeyringError:   # no backend on this system: nothing is stored here
            return None
        except KeyringError as e:
            raise RuntimeError(f"the OS keyring could not be read ({type(e).__name__}): unlock it or allow this app "
                               "to use it, then try again. No login was attempted.") from None
    if mode == "file":
        return _file_read().get(key) or None
    return None   # env: no per-camera store


def _store_set(key, value):
    _check_key(key)
    mode = credential_store()
    if mode == "env":
        raise RuntimeError(_READ_ONLY)
    if mode == "keyring":
        try:
            keyring.set_password(SERVICE, key, value)
        except KeyringError:
            raise RuntimeError("No usable OS keyring on this system. Set credential_store to file in "
                               "settings.json, or set the passwords in the environment.") from None
        return
    data = _file_read()
    data[key] = value
    _file_write(data)


def _store_delete(key):
    _check_key(key)
    mode = credential_store()
    if mode == "env":
        raise RuntimeError(_READ_ONLY)
    if mode == "keyring":
        # the pre-rename item first, then the current one: if the old delete fails the new item still exists, so the
        # secret stays visible and the user can retry (the other order would let the read-through copy bring it back)
        for service, what in ((OLD_SERVICE, "the old (tapo-cameras) keychain item"), (SERVICE, "the saved password")):
            try:
                keyring.delete_password(service, key)
            except keyring_errors.PasswordDeleteError:
                pass   # not stored
            except KeyringError:
                raise RuntimeError(f"Could not delete {what} from the OS keyring. "
                                   + ("Nothing was deleted: try again." if service == OLD_SERVICE
                                      else "The old keychain item was removed; delete the saved password again.")) from None
        return
    data = _file_read()
    if data.pop(key, None) is not None:
        _file_write(data)


def _invalidate():
    """Forget the cached global camera secret and cloud password, so the next login uses what was just written.
    Per-camera secrets are read from the store on every call and are not cached."""
    global _cloud
    get_credentials.cache_clear()
    _cloud = None


def _parse_camera_record(raw, label):
    try:
        rec = json.loads(raw)
    except ValueError:
        rec = None
    if not isinstance(rec, dict) or not rec.get("user") or not rec.get("password"):
        raise RuntimeError(f"the stored login for {label} is unreadable: set it again in the setup wizard")
    return rec["user"], rec["password"]


def _camera_secret(cam):
    raw = _store_get(camera_key(cam))
    if not raw:
        return None
    return _parse_camera_record(raw, cam.get("name", cam["id"]))


def get_camera_credentials(cam):
    """(user, password) for one camera from its own stored camera account. Raises RuntimeError when there is none
    (developer fallback with KINORA_DEV_SECRETS=1: the global camera login, when the camera has no account of its own)."""
    own = _camera_secret(cam)
    if own:
        return own
    if dev_secrets():
        return get_credentials()
    raise RuntimeError(NO_ACCOUNT)


def get_cloud_password_for(cam):
    """The TP-Link password of the saved account email (the wizard's secret). Raises RuntimeError "Save your TP-Link
    account first" when there is none. The account is shared by all cameras, so `cam` does not change the choice."""
    email = account_email()
    if not email:
        raise RuntimeError(NO_CLOUD)
    pw = _store_get(cloud_key(email)) or _dev_cloud_password(email)
    if not pw:
        raise RuntimeError(NO_CLOUD)
    return pw


def set_camera_secret(cam, user, password):
    user = (user or "").strip()
    if not user or not password:
        raise ValueError("the camera user and password must not be empty")
    _store_set(camera_key(cam), json.dumps({"user": user, "password": password}))
    _invalidate()


def set_cloud_secret(email, password):
    email = (email or "").strip()
    if not email or not password:
        raise ValueError("the cloud account email and password must not be empty")
    _store_set(cloud_key(email), password)
    _invalidate()


def delete_camera_secret(cam):
    _store_delete(camera_key(cam))
    _invalidate()


def _record_user(raw):
    """The user name in a stored camera record, or None when it is not readable. Never the password."""
    try:
        rec = json.loads(raw)
    except ValueError:
        return None
    user = rec.get("user") if isinstance(rec, dict) else None
    return user if isinstance(user, str) and user else None


def _dev_camera_present():
    return bool(os.environ.get("TAPO_PASS") or _from_dotenv("TAPO_PASS") or _stored_camera_password(_camera_user()))


def _cloud_source():
    email = account_email()
    if not email:
        return "missing"
    if _store_get(cloud_key(email)):
        return "wizard"
    if _dev_cloud_password(email):
        return "dev_override"
    return "missing"


def camera_state(cam):
    """One read of the camera's own store item. Returns {"secret_sources": {"camera", "cloud"}, "camera_user": the user
    name or None, "has_camera_account": bool}. Sources are "wizard", "dev_override" (developer switch on and a fallback
    would be used) or "missing". Never a password. Raises RuntimeError when a store cannot be read."""
    raw = _store_get(camera_key(cam))
    if raw:
        camera = "wizard"
    elif dev_secrets() and _dev_camera_present():
        camera = "dev_override"
    else:
        camera = "missing"
    return {"secret_sources": {"camera": camera, "cloud": _cloud_source()},
            "camera_user": _record_user(raw) if raw else None, "has_camera_account": bool(raw)}


def secret_sources(cam):
    """Where each secret comes from: "wizard", "dev_override" or "missing". Never a value."""
    return camera_state(cam)["secret_sources"]


def _cmd_set(argv):
    if "--cloud" in argv:
        account = os.environ.get("TAPO_CLOUD_ACCOUNT") or input("Tapo cloud account (email): ").strip()
        if not account:
            print("No account given: nothing stored.", file=sys.stderr)
            return 1
        service, account_name, label = CLOUD_SERVICE, account, "cloud password"
    else:
        service, account_name, label = SERVICE, _camera_user(), "camera password"
    pw = getpass.getpass(f"{label} for {account_name} (input hidden): ")
    if not pw:
        print("Empty password: nothing stored.", file=sys.stderr)
        return 1
    try:
        keyring.set_password(service, account_name, pw)
    except KeyringError:
        print("No usable OS keyring on this system. Set TAPO_PASS / TAPO_CLOUD_PASS in the environment instead.",
              file=sys.stderr)
        return 1
    print(f"stored {label} in the OS keyring (service {service})")
    return 0


def _cmd_status():
    def yn(ok):
        return "set" if ok else "not set"
    if not dev_secrets():
        print(f"developer fallbacks: off ({DEV_ENV} is not 1): the environment, .env and global items are not used")
        print("per-camera secrets are saved by the setup wizard: their presence is shown in the app, never here")
        return 0
    user = _camera_user()
    env_pass = bool(os.environ.get("TAPO_PASS"))
    dotenv_pass = bool(_from_dotenv("TAPO_PASS"))
    kr_pass = bool(_keyring_get(SERVICE, user) or _keyring_get(OLD_SERVICE, user))
    mac_pass = (bool(_macos_security(SERVICE, user) or _macos_security(OLD_SERVICE, user))
                if sys.platform == "darwin" else None)
    print(f"developer fallbacks: on ({DEV_ENV}=1)")
    print(f"camera user: {user}")
    print(f"  TAPO_PASS in environment: {yn(env_pass)}")
    print(f"  TAPO_PASS in .env: {yn(dotenv_pass)}")
    print(f"  OS keyring (service {SERVICE} or {OLD_SERVICE}): {yn(kr_pass)}")
    if mac_pass is not None:
        print(f"  macOS Keychain via security CLI: {yn(mac_pass)}")
    cloud_env = bool(os.environ.get("TAPO_CLOUD_PASS"))
    cloud_account = os.environ.get("TAPO_CLOUD_ACCOUNT")
    cloud_kr = (bool(_keyring_get(CLOUD_SERVICE, cloud_account) or _keyring_get(OLD_CLOUD_SERVICE, cloud_account))
                if cloud_account else None)
    print("cloud password:")
    print(f"  TAPO_CLOUD_PASS in environment: {yn(cloud_env)}")
    print(f"  OS keyring (service {CLOUD_SERVICE}, account TAPO_CLOUD_ACCOUNT "
          f"{'set' if cloud_account else 'not set'}): " + (yn(cloud_kr) if cloud_kr is not None else "not checked"))
    return 0


def main(argv):
    if argv == ["set"] or argv == ["set", "--cloud"]:
        return _cmd_set(argv)
    if argv == ["status"]:
        return _cmd_status()
    print("usage: python -m kinora.creds set [--cloud] | status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    config.init()   # same startup as the server: the one-time data folder move (never while an old server runs)
    sys.exit(main(sys.argv[1:]))
