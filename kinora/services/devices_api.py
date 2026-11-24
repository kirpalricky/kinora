"""Other devices API (Settings > Other devices): the allowed list, the waiting list and the sharing switch.

Routes are under /api/setup/, so server.local_only answers them only to this computer. Sharing means a second
listening socket on this computer's network address (never 0.0.0.0): the loopback socket is always there.
When the server was started with --host / KINORA_HOST / --allow, the one socket is on that address and the
switch is read-only (from_command_line). Handlers are thin: validate, call kinora.devices, return the view.
"""
import asyncio
import ipaddress
import socket

from aiohttp import web

from kinora import config, devices
from kinora.errors import Conflict
from kinora.services.setup import _body

ANY_ADDRESSES = ("0.0.0.0", "::")
# Listening state, set by server.py before the runner starts (aiohttp rejects new app keys once started):
# {"runner": AppRunner, "lan_site": TCPSite or None (the network socket; None when not sharing)}
LISTEN = web.AppKey("listen", dict)
_listen_lock = asyncio.Lock()   # one start or stop of the network socket at a time


def lan_address():
    """This computer's address on its network: found by a UDP connect (no packet is sent). (ip, None) or (None, text)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ip = s.getsockname()[0]
    except OSError:
        return None, "No network connection found"
    if not ip or ipaddress.ip_address(ip).is_loopback or ip in ANY_ADDRESSES:
        return None, "No network connection found"
    return ip, None


def from_command_line():
    return bool(config.ALLOW) or config.HOST != "127.0.0.1"


def view(app):
    data = devices.load()
    cmd = from_command_line()
    listening = app[LISTEN]["lan_site"] is not None
    sharing = data["sharing"] or cmd
    address = address_error = None
    if sharing:
        if cmd and config.HOST not in ANY_ADDRESSES:
            ip, address_error = config.HOST, None
        else:
            ip, address_error = lan_address()
        if ip and listening:
            address = f"http://{ip}:{config.PORT}"
        elif ip and address_error is None:
            address_error = "Kinora is not listening on your network. Turn sharing off and on again."
    return {
        "sharing": sharing,
        "listening": listening,
        "address": address,
        "address_error": address_error,
        "from_command_line": cmd,
        "devices": data["devices"],
        "pending": devices.pending(),
        "command_line": [str(n) for n in config.ALLOW],
    }


async def listen(app):
    """Start the sockets when the server starts (called by server.main). Loopback always; the network socket when
    sharing is on. A bind failure on loopback (port in use) is raised for main() to explain."""
    state = app[LISTEN]
    runner = state["runner"]
    if from_command_line():
        site = web.TCPSite(runner, config.HOST, config.PORT)
        await site.start()
        state["lan_site"] = site
        return
    await web.TCPSite(runner, "127.0.0.1", config.PORT).start()
    if devices.load()["sharing"]:
        ip, _ = lan_address()
        if ip:
            try:
                site = web.TCPSite(runner, ip, config.PORT)
                await site.start()
                state["lan_site"] = site
            except OSError:
                pass   # the view says Kinora is not listening on the network; loopback keeps working


async def _body_bool(request):
    body = await _body(request, ("enabled",))
    if not isinstance(body.get("enabled"), bool):
        raise web.HTTPBadRequest(text="enabled must be true or false")
    return body["enabled"]


async def get_devices(request):
    return web.json_response(view(request.app))


async def add_device(request):
    body = await _body(request, ("ip", "name"))
    if "ip" not in body:
        raise web.HTTPBadRequest(text="ip is required")
    devices.add(body["ip"], body.get("name", ""))
    return web.json_response(view(request.app))


async def remove_device(request):
    if not devices.remove(request.match_info["ip"]):
        raise web.HTTPNotFound(text="That device is not in the list")
    return web.json_response(view(request.app))


async def dismiss_device(request):
    body = await _body(request, ("ip",))
    devices.dismiss(body.get("ip"))
    return web.json_response(view(request.app))


async def set_sharing(request):
    enabled = await _body_bool(request)
    app = request.app
    if from_command_line():
        raise Conflict("Sharing is set when Kinora is started, so it cannot change here")
    async with _listen_lock:
        state = app[LISTEN]
        runner = state["runner"]
        if enabled and runner is not None and state["lan_site"] is None:
            ip, err = lan_address()
            if not ip:
                raise Conflict(err)
            try:
                site = web.TCPSite(runner, ip, config.PORT)
                await site.start()
            except OSError:
                raise Conflict(f"Kinora could not start listening on your network. Check that no other program uses port {config.PORT}.") from None
            state["lan_site"] = site
        if not enabled and state["lan_site"] is not None:
            await state["lan_site"].stop()
            state["lan_site"] = None
        devices.set_sharing(enabled)
    return web.json_response(view(app))
