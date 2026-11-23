"""The `kinora` command: start the server, `--version`, `doctor`. Offline: doctor never contacts a camera.

--port and --home are applied through KINORA_PORT / KINORA_HOME before kinora.config is imported (config reads
them at import time), so every import of the heavy modules is lazy.
"""
import argparse
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


def _version():
    from kinora.config import VERSION
    return VERSION


def build_parser():
    p = argparse.ArgumentParser(prog="kinora", description="Kinora: scrubbable playback of Tapo camera SD cards, in your browser.")
    p.add_argument("command", nargs="?", choices=["doctor"], help="doctor: check this computer is ready (no camera is contacted)")
    p.add_argument("--port", type=int, help="port to listen on (default 8080, or KINORA_PORT)")
    p.add_argument("--no-browser", action="store_true", help="do not open a browser tab on start")
    p.add_argument("--home", metavar="PATH", help="data folder (default: the per-user data folder, or KINORA_HOME)")
    p.add_argument("--version", action="store_true", help="print the version and exit")
    return p


def apply_env(args, environ=os.environ):
    """Must run before kinora.config is imported."""
    if args.port is not None:
        if not 0 < args.port < 65536:
            sys.exit("--port must be between 1 and 65535")
        environ["KINORA_PORT"] = str(args.port)
    if args.home:
        environ["KINORA_HOME"] = str(Path(args.home).expanduser())


def should_open_browser(no_browser=False, environ=None, platform=None):
    """False with --no-browser, KINORA_NO_BROWSER=1, or on Linux without a display."""
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    if no_browser or environ.get("KINORA_NO_BROWSER") == "1":
        return False
    if platform.startswith("linux") and not (environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY")):
        return False
    return True


def _wait_then_open(port, url, opener=webbrowser.open, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        except OSError:
            time.sleep(0.2)
            continue
        try:
            opener(url)
        except Exception:
            pass
        return True
    return False


def start(args):
    from kinora import config, instance
    instance.startup()   # one server per machine, then the one-time data folder move, before kinora.server is imported
    from kinora import server
    url = f"http://localhost:{config.PORT}"
    on_started = None
    if should_open_browser(args.no_browser):
        def on_started():
            threading.Thread(target=_wait_then_open, args=(config.PORT, url), daemon=True).start()
    print(f"Kinora {config.VERSION}: {url}  (Ctrl+C to stop)", flush=True)
    server.main(on_started=on_started)


# ---- doctor ---------------------------------------------------------------------------------
OK, WARN, FAIL = "ok", "warn", "FAIL"


def _port_check(port):
    """(status, text): free, or already answered by a Kinora server, or taken by something else."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return OK, f"port {port} is free"
    except OSError:
        pass
    finally:
        s.close()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as r:
            if r.status == 200:
                return OK, f"port {port} is in use by a running Kinora (http://localhost:{port})"
    except Exception:
        pass
    return FAIL, f"port {port} is in use by another program: stop it, or start Kinora with --port <number>"


def _data_folder_check(home):
    try:
        home.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=home):
            pass
    except OSError as e:
        return FAIL, f"data folder {home} is not writable ({e.strerror}): choose another with --home"
    return OK, f"data folder {home} (writable)"


def run_doctor(home=None, port=None, which=shutil.which, keyring_status=None, port_check=_port_check,
               hint=None, out=print):
    """Print the checklist, return the exit code (1 when anything FAILs). All probes are injectable for tests."""
    from kinora import binaries, config
    home = Path(config.HOME) if home is None else Path(home)
    port = config.PORT if port is None else port
    hint = binaries.install_hint() if hint is None else hint
    rows = []
    v = sys.version_info
    rows.append((OK if v >= (3, 10) else FAIL, f"Python {v.major}.{v.minor}.{v.micro}" + ("" if v >= (3, 10) else " (Kinora needs 3.10 or newer)")))
    rows.append(_data_folder_check(home))
    for tool in ("ffmpeg", "ffprobe"):
        found = which(tool)
        rows.append((OK, f"{tool}: {found}") if found else (FAIL, f"{tool} not found on PATH. Install FFmpeg: {hint}"))
    if keyring_status is None:
        from kinora import creds
        keyring_status = creds.backend_status
    try:
        ks = keyring_status()
    except Exception as e:
        ks = {"ok": False, "backend": None, "reason": str(e) or type(e).__name__}
    rows.append((OK, f"keyring: {ks.get('backend')}") if ks.get("ok") else
                (WARN, f"keyring not usable ({ks.get('reason')}): Kinora can store passwords in a private file instead (Settings)"))
    rows.append(port_check(port))
    rows.append((OK, f"Kinora {_version()}"))
    for status, text in rows:
        out(f"[{status:>4}] {text}")
    failed = sum(1 for s, _ in rows if s == FAIL)
    out("Everything looks good." if not failed else f"{failed} problem(s) to fix before Kinora will work fully.")
    return 1 if failed else 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"kinora {_version()}")
        return 0
    apply_env(args)
    if args.command == "doctor":
        return run_doctor()
    start(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
