"""Exclusive OS file locks (flock on POSIX, msvcrt.locking on Windows). No kinora imports: usable before config."""
import os
import time


def os_lock(f, blocking=True):
    """Exclusive OS lock on an open file: flock on POSIX, msvcrt.locking on Windows.
    Non-blocking raises OSError when another process holds it. Released by os_unlock or on close."""
    if os.name == "nt":
        import msvcrt
        f.seek(0)
        if not blocking:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)   # raises at once when held
            return
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.05)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)


def os_unlock(f):
    if os.name == "nt":
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
