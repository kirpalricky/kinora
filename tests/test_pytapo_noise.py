"""server.quiet_pytapo: drops exactly pytapo's 'Received response with no or invalid session information' warning (logged on
pytapo.media_stream.session once per message the camera sends into a finished playback session) and nothing else.
No camera, no network. Run: python -m unittest tests.test_pytapo_noise
"""
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402

LOGGER = "pytapo.media_stream.session"
NOISE = server.PYTAPO_NOISE + " (sequence 12, session 3456), can't be delivered"


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class QuietPytapo(unittest.TestCase):
    def setUp(self):
        self.log = logging.getLogger(LOGGER)
        self.saved_level = self.log.level
        self.log.setLevel(logging.DEBUG)
        self.cap = Capture()
        self.log.addHandler(self.cap)
        self.addCleanup(self.restore)

    def restore(self):
        self.log.removeHandler(self.cap)
        self.log.setLevel(self.saved_level)
        for f in [f for f in self.log.filters if isinstance(f, server._PytapoNoise)]:
            self.log.removeFilter(f)

    def test_the_noise_message_is_dropped(self):
        server.quiet_pytapo()
        self.log.warning(NOISE)
        self.assertEqual(self.cap.messages, [])

    def test_other_pytapo_warnings_and_errors_still_show(self):
        server.quiet_pytapo()
        self.log.warning("Unable to parse JSON sent from device")
        self.log.error("Received response with no or invalid session information from another path")
        self.log.warning("Received a response for an unknown session")
        self.assertEqual(self.cap.messages, [
            "Unable to parse JSON sent from device",
            "Received response with no or invalid session information from another path",
            "Received a response for an unknown session"])

    def test_installing_twice_adds_one_filter(self):
        server.quiet_pytapo()
        server.quiet_pytapo()
        self.assertEqual(sum(isinstance(f, server._PytapoNoise) for f in self.log.filters), 1)

    def test_other_loggers_are_not_touched(self):
        server.quiet_pytapo()
        other = logging.getLogger("pytapo.media_stream.other_for_test")
        cap = Capture()
        other.addHandler(cap)
        self.addCleanup(other.removeHandler, cap)
        other.setLevel(logging.DEBUG)
        other.warning(NOISE)
        self.assertEqual(cap.messages, [NOISE])


if __name__ == "__main__":
    unittest.main()
