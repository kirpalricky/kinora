"""Single server per machine: an exclusive OS lock on a file in the user cache dir.

The lock lives outside the data dir on purpose: two installs with different data dirs (KINORA_HOME)
still collide, and so do two servers started from the same checkout. Held for the life of the process
(the OS releases it on exit, even after a crash).
KINORA_NO_INSTANCE_LOCK=1 disables the check. For tests only, never for a real server.
"""
import os
from pathlib import Path

from platformdirs import user_cache_dir

from kinora import config
from kinora.config import env as _env
from kinora.oslock import os_lock as _os_lock

_held = []   # keeps the handles open (and the locks held) for the process lifetime


class AlreadyRunning(RuntimeError):
    pass


def lock_path() -> Path:
    d = Path(user_cache_dir("kinora"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "server.lock"


def legacy_lock_path() -> Path:
    """The pre-rename code's lock (user cache dir 'tapo-cameras'). Held too, so old and new servers exclude each other."""
    d = Path(user_cache_dir("tapo-cameras"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "server.lock"


def _take(path, message):
    f = open(path, "a+b")
    try:
        _os_lock(f, blocking=False)
    except OSError:
        f.close()
        raise AlreadyRunning(message)
    return f


def record_port(port):
    """Write the listening port into the lock file so a second `kinora` can print the running instance's URL."""
    if not _held:
        return
    try:
        f = _held[0]
        f.seek(0)
        f.truncate()
        f.write(str(int(port)).encode())
        f.flush()
    except (OSError, ValueError):
        pass


def running_url(default_port=None):
    """URL of the server holding the lock: the port it recorded, else `default_port`, else the configured port."""
    from kinora import config
    port = default_port or config.PORT
    try:
        text = lock_path().read_text().strip()
        if text.isdigit() and 0 < int(text) < 65536:
            port = int(text)
    except (OSError, ValueError):
        pass
    return f"http://localhost:{port}"


def acquire():
    """Take the machine-wide locks (new and legacy) without waiting, or raise AlreadyRunning. Returns the new lock's
    handle, or None when disabled by KINORA_NO_INSTANCE_LOCK=1."""
    global _held
    if _env("NO_INSTANCE_LOCK") == "1":
        return None
    path = lock_path()
    f = _take(path, f"another Kinora server is already running on this machine (lock: {path}). "
                    "Stop it first: two servers would double the camera login budget.")
    try:
        old = legacy_lock_path()
        g = _take(old, "an older tapo-cameras server is still running on this machine. Stop it first, then start Kinora: "
                       "two servers would double the camera login budget.")
    except BaseException:
        f.close()
        raise
    _held = [f, g]
    return f


def startup():
    """Server entry point: take the locks, then run the one-time data folder move (config.init). Exits with a plain
    sentence when another or an older server is running."""
    import sys
    try:
        handle = acquire()
    except AlreadyRunning as e:
        sys.exit(f"{e} Open {running_url()}" if "another Kinora" in str(e) else str(e))
    config.init(legacy_lock_held=handle is not None)
    return handle
