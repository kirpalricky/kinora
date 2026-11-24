"""Other devices that may use this server: the owner's list (Settings > Other devices) in HOME/devices.json, the
sharing switch, and the devices that were refused and are waiting for the owner to allow them (memory only).

File: {"version": 1, "sharing": false, "devices": [{"ip": "192.168.1.20", "name": "Anna's laptop"}]}.
A missing or corrupt file means the defaults (sharing off, no devices); it never stops the server.
Addresses from --allow / KINORA_ALLOW (config.ALLOW) are allowed too, but they are not stored here."""
import ipaddress
import json
import os
import threading
import time

from kinora import config
from kinora.errors import BadInput

MAX_DEVICES = 20
NAME_MAX = 60
PENDING_MAX = 20            # distinct refused addresses kept (the oldest is dropped)
PENDING_TTL_S = 24 * 3600   # a refused device is forgotten after a day
BAD_IP = "Enter the device's IP address, like 192.168.1.20"

_lock = threading.Lock()      # one read-modify-write of devices.json at a time
_plock = threading.Lock()     # the waiting list
_pending = {}                 # normalised ip -> last_seen (epoch seconds)


def _path():
    return config.HOME / "devices.json"


def _defaults():
    return {"version": 1, "sharing": False, "devices": []}


def _write_atomic(path, data: bytes):
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)   # the old file is still the one on disk
        raise


def _check_ip(value):
    """A device address: an IP literal that can be a device on the network (not loopback, unspecified or multicast)."""
    if not isinstance(value, str):
        raise BadInput(BAD_IP)
    try:
        ip = ipaddress.ip_address(value.strip())
    except ValueError:
        raise BadInput(BAD_IP) from None
    ip = getattr(ip, "ipv4_mapped", None) or ip
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        raise BadInput("That address cannot be another device on your network")
    return str(ip)


def _check_name(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BadInput(f"The name can be at most {NAME_MAX} characters")
    name = value.strip()
    if len(name) > NAME_MAX:
        raise BadInput(f"The name can be at most {NAME_MAX} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise BadInput("The name cannot contain control characters")
    return name


def _normal(ip):
    """The IP of a peer or a request path as a string (IPv4-mapped IPv6 becomes IPv4), or None when it is not an address."""
    if ip is None:
        return None
    try:
        addr = ipaddress.ip_address(ip) if isinstance(ip, str) else ip
    except ValueError:
        return None
    if not isinstance(addr, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return None
    return str(getattr(addr, "ipv4_mapped", None) or addr)


def load():
    """The stored settings. Never raises: a missing or unreadable file gives the defaults; bad entries are skipped."""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _defaults()
    if not isinstance(raw, dict):
        return _defaults()
    out = _defaults()
    out["sharing"] = raw.get("sharing") is True
    seen = set()
    for item in raw.get("devices") if isinstance(raw.get("devices"), list) else []:
        if not isinstance(item, dict):
            continue
        try:
            ip = _check_ip(item.get("ip"))
            name = _check_name(item.get("name"))
        except BadInput:
            continue
        if ip not in seen and len(out["devices"]) < MAX_DEVICES:
            seen.add(ip)
            out["devices"].append({"ip": ip, "name": name})
    return out


def save(data):
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"version": 1, "sharing": bool(data.get("sharing")), "devices": list(data.get("devices", []))}
    _write_atomic(path, json.dumps(body, indent=2, ensure_ascii=False).encode() + b"\n")


def is_allowed(ip):
    """True for a loopback-free peer address that is in config.ALLOW or is a stored device."""
    addr = _normal(ip)
    if addr is None:
        return False
    parsed = ipaddress.ip_address(addr)
    if any(parsed in net for net in config.ALLOW):
        return True
    return addr in {d["ip"] for d in load()["devices"]}


def add(ip, name=""):
    addr = _check_ip(ip)
    label = _check_name(name)
    with _lock:
        data = load()
        if any(d["ip"] == addr for d in data["devices"]):
            raise BadInput("That device is already allowed")
        if len(data["devices"]) >= MAX_DEVICES:
            raise BadInput(f"Up to {MAX_DEVICES} devices can be allowed")
        data["devices"].append({"ip": addr, "name": label})
        save(data)
    dismiss(addr)   # allowing a waiting device removes it from the waiting list
    return addr


def remove(ip):
    """True when the device was in the list and is now removed."""
    addr = _normal(ip)
    if addr is None:
        return False
    with _lock:
        data = load()
        kept = [d for d in data["devices"] if d["ip"] != addr]
        if len(kept) == len(data["devices"]):
            return False
        data["devices"] = kept
        save(data)
    return True


def set_sharing(enabled):
    with _lock:
        data = load()
        data["sharing"] = bool(enabled)
        save(data)


def _prune(now):
    for ip, seen in list(_pending.items()):
        if now - seen > PENDING_TTL_S:
            del _pending[ip]


def note_denied(ip):
    """A device was refused: remember its address (memory only, no request content) for the owner to allow."""
    addr = _normal(ip)
    if addr is None:
        return
    now = time.time()
    with _plock:
        _pending[addr] = now
        _prune(now)
        while len(_pending) > PENDING_MAX:
            del _pending[min(_pending, key=_pending.get)]


def pending():
    """The waiting devices, most recent first: [{"ip", "last_seen"}] with last_seen in epoch seconds."""
    now = time.time()
    with _plock:
        _prune(now)
        items = sorted(_pending.items(), key=lambda kv: kv[1], reverse=True)
    return [{"ip": ip, "last_seen": int(seen)} for ip, seen in items]


def dismiss(ip):
    addr = _check_ip(ip) if isinstance(ip, str) else None
    if addr is None:
        raise BadInput(BAD_IP)
    with _plock:
        _pending.pop(addr, None)
