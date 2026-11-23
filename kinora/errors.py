"""Domain errors for the camera layer and services. No HTTP here: server.py maps them to status codes
in one place (the `camera_errors` middleware)."""


class CameraError(Exception):          # 502
    status = 502

    def __init__(self, text):
        super().__init__(text)
        self.text = text


class Unavailable(CameraError):        # gate marker refusal, busy, media paused
    status = 503


class BadInput(CameraError):           # disabled, no auth method, bad ptz direction
    status = 400


class NotSupported(CameraError):       # cannot pan/tilt
    status = 501


class TooFast(CameraError):            # ptz gap
    status = 429


class Conflict(CameraError):           # setup API: another verify running, camera already verified and on, export queued
    status = 409
