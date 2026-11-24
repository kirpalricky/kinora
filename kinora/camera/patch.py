"""pytapo login guard: every login (including pytapo's own internal re-logins) goes through gate.gate()."""
import threading

from kinora import config
from kinora.camera import gate

_depth = threading.local()


def _guard_login(orig):
    """Wrap a pytapo login primitive so its own (re-)logins are gated and counted too."""
    def wrapper(self, *a, **kw):
        host = getattr(self, "host", None)
        cam = config.CAM_BY_IP.get(host)
        if cam is None:   # fail closed: an unknown host (or a renamed attribute) never reaches the camera
            raise gate.GateRefused(f"{gate.MARK}login to unknown host {host!r} refused: it is not in cameras.json")
        nested = getattr(_depth, "n", 0) > 0   # pytapo's internal retry of this same login
        gate.gate(cam, count_only=nested)
        _depth.n = getattr(_depth, "n", 0) + 1
        try:
            result = orig(self, *a, **kw)
            if not nested:
                gate.note_login_success(cam)
            return result
        except Exception as e:
            if not nested:
                _depth.noted = True
                gate.note_login_failure(cam, e)
            raise
        finally:
            _depth.n -= 1
    return wrapper


def _guard_probe(orig):
    """Wrap a pytapo discovery probe (sent from the Tapo() constructor, outside the gated login): it counts towards
    the camera's lockout, so it is counted by the gate and refused for an unknown, blocked or disabled camera."""
    def wrapper(host, *a, **kw):
        cam = config.CAM_BY_IP.get(host)
        if cam is None:   # fail closed, like the login guard
            raise gate.GateRefused(f"{gate.MARK}probe of unknown host {host!r} refused: it is not in cameras.json")
        gate.note_probe(cam)
        return orig(host, *a, **kw)
    return wrapper


async def _refuse_klap_login(self, *a, **kw):
    """Replaces pytapo's KLAP login: this app never logs in over KLAP, so the login is refused before any request."""
    raise gate.GateRefused(f"{gate.MARK}klap login is not supported by this app")


def install():
    """Wrap pytapo's login primitives with the gate, and replace the KLAP login with a refusal.
    Idempotent: a wrapper or the refusal is never wrapped or replaced twice."""
    from pytapo.transport.klap.klap import Klap
    from pytapo.transport.pytapo.pytapo import pyTapo
    from pytapo.transport.tpap.tpap import Tpap

    for owner, name in ((Klap, "authenticate"),):
        if getattr(getattr(owner, name), "_kinora_refused", False):
            continue
        _refuse_klap_login._kinora_refused = True
        setattr(owner, name, _refuse_klap_login)

    for owner, name in ((Tpap, "_login"), (pyTapo, "_refreshStok")):
        if not callable(getattr(owner, name, None)):
            raise RuntimeError(f"pytapo's internals changed ({owner.__name__}.{name} is missing): "
                               "the login gate cannot be installed, so the server refuses to run ungated")
    import pytapo   # the constructor calls these two by their package-level names
    for name in ("discover_login", "rejects_legacy_login"):
        if not callable(getattr(pytapo, name, None)):
            raise RuntimeError(f"pytapo's internals changed (pytapo.{name} is missing): "
                               "the login gate cannot be installed, so the server refuses to run ungated")
        probe = getattr(pytapo, name)
        if not getattr(probe, "_kinora_guarded", False):
            wrapped = _guard_probe(probe)
            wrapped._kinora_guarded = True
            setattr(pytapo, name, wrapped)

    for owner, name in ((Tpap, "_login"), (pyTapo, "_refreshStok")):
        current = getattr(owner, name)
        if getattr(current, "_kinora_guarded", False):
            continue
        wrapped = _guard_login(current)
        wrapped._kinora_guarded = True
        setattr(owner, name, wrapped)
