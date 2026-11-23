"""Guard against pytapo gaining a new login path that bypasses the login gate.

Every method of a pytapo transport class whose name matches login|stok|handshake|auth (case-insensitive) must be
on one of the lists below, with the reason it is safe: WRAPPED (kinora/camera/patch.py guards it), REVIEWED (not a
network login of its own, or reached only through a wrapped one, or unreachable from kinora), or OPEN (a login path
the gate does NOT guard, listed so it is visible; needs a decision). A new or renamed candidate fails the test with
the instruction to review it. Candidates were read from the source of pytapo 3.4.34 (the pinned version).

Offline: nothing is imported from kinora.camera (importing gate reads breaker.json); pytapo's transport modules are
only inspected, never called.
Run: .venv/bin/python -m unittest tests.test_pytapo_guard
"""
import importlib
import inspect
import pkgutil
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pytapo.transport as transport_pkg  # noqa: E402

NAME = re.compile(r"login|stok|handshake|auth", re.I)

# Guarded by kinora/camera/patch.py: every call goes through gate.gate() and is counted.
WRAPPED = {
    "pytapo.transport.tpap.tpap:Tpap._login": "the SPAKE2+ login; patch.py wraps it",
    "pytapo.transport.pytapo.pytapo:pyTapo._refreshStok": "the password login that yields a stok; patch.py wraps it",
    "pytapo.transport.klap.klap:Klap.authenticate": "the KLAP login is replaced by a stub in patch.py that raises "
                                                     "GateRefused before any request (session.py never asks for KLAP)",
}

REVIEWED = {
    "pytapo.transport.transport:Transport.authenticate": "dispatcher: forwards to the backend's authenticate, no I/O",
    "pytapo.transport.tpap.tpap:Tpap.authenticate": "calls only self._login (wrapped) when no session is held",
    "pytapo.transport.tpap.tpap:Tpap._loginWith": "pake login exchange, called only from the wrapped Tpap._login",
    "pytapo.transport.tpap.tpap:Tpap._postLogin": "HTTP POST helper, called only from Tpap._loginWith",
    "pytapo.transport.pytapo.pytapo:pyTapo.authenticate": "calls only the wrapped _refreshStok when no stok is held",
    "pytapo.transport.kasa.kasa:Kasa.authenticate": "network login, but Transport(method='kasa') is never built: "
                                                     "kinora/camera/session.py passes only klap/tpap/pytapo",
    "pytapo.transport.kasa.kasa:Kasa._is_kasa_ssl_handshake_failure": "pure check of an exception's text, no I/O",
}

# A login path the gate does not guard. Not an approval: decide whether to wrap it in patch.py. (Empty: none known.)
OPEN = {}


def candidates():
    """Login-like functions defined on the classes of every module in pytapo.transport, keyed 'module:Class.method'."""
    found = {}
    for m in pkgutil.walk_packages(transport_pkg.__path__, transport_pkg.__name__ + "."):
        mod = importlib.import_module(m.name)
        for cname, cls in inspect.getmembers(mod, inspect.isclass):
            if cls.__module__ != mod.__name__:
                continue
            for mname, member in vars(cls).items():
                if NAME.search(mname) and inspect.isfunction(member):
                    found[f"{mod.__name__}:{cname}.{mname}"] = member
    return found


class PytapoLoginGuard(unittest.TestCase):
    def setUp(self):
        self.found = candidates()

    def test_every_candidate_is_on_a_reviewed_list(self):
        unreviewed = sorted(k for k in self.found if k not in WRAPPED and k not in REVIEWED and k not in OPEN)
        self.assertEqual(
            unreviewed, [],
            "New login-like pytapo method(s): read their source, then either wrap them in kinora/camera/patch.py "
            "or add them to REVIEWED in tests/test_pytapo_guard.py with a reason. Not reviewed: " + ", ".join(unreviewed))

    def test_lists_name_only_methods_that_exist(self):
        listed = set(WRAPPED) | set(REVIEWED) | set(OPEN)
        stale = sorted(listed - set(self.found))
        self.assertEqual(stale, [], "listed pytapo method(s) no longer exist (renamed or removed): " + ", ".join(stale))

    def test_every_entry_has_a_reason(self):
        for table in (WRAPPED, REVIEWED, OPEN):
            for key, why in table.items():
                self.assertTrue(why.strip(), key)

    def test_wrapped_methods_are_really_wrapped_by_patch(self):
        # Static check (importing patch.py pulls in gate.py, which reads breaker.json): patch.py must wrap these names.
        src = (ROOT / "kinora" / "camera" / "patch.py").read_text()
        for key in WRAPPED:
            owner, name = key.split(":")[1].split(".")
            self.assertRegex(src, r"\(\s*%s\s*,\s*\"%s\"\s*\)" % (owner, re.escape(name)),
                             f"{key} is listed as WRAPPED but kinora/camera/patch.py does not wrap it")

    def test_open_gaps_are_reported(self):
        for key in OPEN:
            print(f"\nNOTE (known open gap, not guarded by the login gate): {key}", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
