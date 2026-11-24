"""Other devices API (Settings > Other devices): the allowed list, the waiting list and the sharing switch.

Routes are under /api/setup/, so server.local_only answers them only to this computer. Sharing means a second
listening socket on this computer's network address (never 0.0.0.0): the loopback socket is always there.
When the server was started with --host / KINORA_HOST / --allow, the one socket is on that address and the
switch is read-only (from_command_line). Handlers are thin: validate, call kinora.devices, return the view.
"""
import asyncio
import io
import ipaddress
import socket

import segno
from aiohttp import web

from kinora import config, devices
from kinora.errors import Conflict
from kinora.services.setup import _body

ANY_ADDRESSES = ("0.0.0.0", "::")
CHANGED_TEXT = "This computer's network address changed. Turn sharing off and on again."
# Listening state, set by server.py before the runner starts (aiohttp rejects new app keys once started):
# {"runner": AppRunner, "lan_site": TCPSite or None (the network socket), "lan_ip": the address it is bound to}
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


def _network_now():
    """The address the network socket should use now: the command-line address, else this computer's address."""
    if devices.from_command_line() and config.HOST not in ANY_ADDRESSES:
        return config.HOST, None
    return lan_address()


def view(app):
    state = app[LISTEN]
    data = devices.load()
    cmd = devices.from_command_line()
    listening = state["lan_site"] is not None
    sharing = data["sharing"] or cmd
    address = address_error = None
    if sharing and listening:
        bound = state["lan_ip"]
        now, _ = _network_now()
        if now == bound:
            address = f"http://{bound}:{config.PORT}"
        else:
            address_error = CHANGED_TEXT
    elif sharing:
        now, err = _network_now()
        address_error = err if now is None else "Kinora is not listening on your network. Turn sharing off and on again."
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


async def _start_lan(app, ip):
    """Start the network socket on this address. Raises OSError when it cannot listen there."""
    state = app[LISTEN]
    site = web.TCPSite(state["runner"], ip, config.PORT)
    await site.start()
    state["lan_site"] = site
    state["lan_ip"] = ip


async def _stop_lan(app):
    """Stop the network socket and close the connections that arrived on it (keep-alive ones included)."""
    state = app[LISTEN]
    site, ip, runner = state["lan_site"], state["lan_ip"], state["runner"]
    if site is None:
        return
    await site.stop()
    state["lan_site"] = None
    state["lan_ip"] = None
    server = getattr(runner, "server", None)
    for handler in list(getattr(server, "connections", [])):
        transport = getattr(handler, "transport", None)
        sock = transport.get_extra_info("sockname") if transport is not None else None
        if sock and sock[0] == ip:
            transport.close()


async def listen(app):
    """Start the sockets when the server starts (called by server.main). Loopback always; the network socket when
    sharing is on. A bind failure is raised for main() to explain."""
    state = app[LISTEN]
    runner = state["runner"]
    devices.load()   # the stored settings are read once here; requests use the copy in memory
    if devices.from_command_line():
        host = config.HOST
        if host not in ("127.0.0.1", "0.0.0.0"):   # a specific or IPv6 address: loopback is kept as well
            await web.TCPSite(runner, "127.0.0.1", config.PORT).start()
        site = web.TCPSite(runner, host, config.PORT)
        await site.start()
        state["lan_site"] = site
        state["lan_ip"] = host if host not in ANY_ADDRESSES else lan_address()[0]
        return
    await web.TCPSite(runner, "127.0.0.1", config.PORT).start()
    if devices.load()["sharing"]:
        ip, _ = lan_address()
        if ip:
            try:
                await _start_lan(app, ip)
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


def qr_svg(address):
    """The address as an SVG QR code: error level M, a 4-module border, dark modules on white (it scans in dark mode too)."""
    buf = io.BytesIO()
    segno.make(address, error="m").save(buf, kind="svg", scale=1, border=4, dark="#000", light="#fff", xmldecl=False, nl=False)
    return buf.getvalue()


async def address_qr(request):
    """GET /api/setup/devices/qr: the QR code of the address the network socket is listening on (no input). 404 when not."""
    address = view(request.app)["address"]
    if not address:
        return web.Response(status=404, text="Kinora is not listening on your network, so there is no address to show.",
                            content_type="text/plain")
    return web.Response(body=qr_svg(address), content_type="image/svg+xml", headers={"Cache-Control": "no-store"})


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
    if devices.from_command_line():
        raise Conflict("Sharing is set when Kinora is started, so it cannot change here")
    async with _listen_lock:
        state = app[LISTEN]
        if enabled:
            if state["runner"] is not None and state["lan_site"] is None:
                ip, err = lan_address()
                if not ip:
                    raise Conflict(err)
                try:
                    await _start_lan(app, ip)
                except OSError:
                    raise Conflict(f"Kinora could not start listening on your network. Check that no other program uses port {config.PORT}.") from None
            devices.set_sharing(True)
        else:
            devices.set_sharing(False)   # first the switch: a request arriving now is refused
            await _stop_lan(app)
    return web.json_response(view(app))
