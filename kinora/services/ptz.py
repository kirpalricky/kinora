"""Pan / tilt in small relative steps.

pytapo moveMotor(x, y) moves by a relative amount in degrees (as Home Assistant's Tapo integration does:
right = +x, left = -x, up = +y, down = -y). moveMotorStep(direction) moves continuously to the end stop: never use it.
cameras.json: "ptz": true enables it; "ptz_invert": {"x": true, "y": true} flips a mirrored or upside-down camera.
"""
import time

from kinora import config
from kinora.errors import BadInput, NotSupported, TooFast

VECTORS = {"right": (1, 0), "left": (-1, 0), "up": (0, 1), "down": (0, -1)}
OPPOSITE = {"left": "right", "right": "left", "up": "down", "down": "up"}
_last = {}


async def step(api, cam, direction):
    if not cam.get("ptz"):
        raise NotSupported(f"{cam['name']} cannot pan/tilt")
    if direction not in VECTORS:
        raise BadInput("dir must be left, right, up or down")
    inv = cam.get("ptz_invert", {})
    if inv.get("x" if direction in ("left", "right") else "y"):
        direction = OPPOSITE[direction]
    now = time.time()
    if now - _last.get(cam["id"], 0) < config.PTZ_MIN_GAP_S:
        raise TooFast("too fast")
    _last[cam["id"]] = now
    deg = cam.get("ptz_step_deg", config.PTZ_STEP_DEG)
    x, y = (v * deg for v in VECTORS[direction])
    await api(cam, lambda t: t.moveMotor(x, y))
