"""p2p.punch: a ConnectionResetError (Windows ICMP port-unreachable) is not fatal and punching continues until the
deadline; SIO_UDP_CONNRESET is switched off where the platform has it. Mock socket, offline.
Run: .venv/bin/python -m unittest tests.test_p2p_punch
"""
import socket
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kinora import p2p  # noqa: E402

SID = "Xabcdef1234567890"
CAM = ("192.0.2.10", 9999)


class Punch(unittest.TestCase):
    def test_reset_once_then_answer_succeeds(self):
        sock = mock.Mock()
        answer = b"2" + SID.encode()[1:]
        sock.recvfrom.side_effect = [ConnectionResetError(10054, "reset"), (answer, CAM)]
        self.assertEqual(p2p.punch(sock, SID, CAM, timeout=5), CAM)
        self.assertEqual(sock.recvfrom.call_count, 2)
        sock.sendto.assert_any_call(answer, CAM)

    def test_keeps_punching_until_the_deadline(self):
        sock = mock.Mock()
        sock.recvfrom.side_effect = ConnectionResetError(10054, "reset")
        t0 = time.time()
        with self.assertRaises(RuntimeError):
            p2p.punch(sock, SID, CAM, timeout=0.4)
        self.assertGreaterEqual(time.time() - t0, 0.35)
        self.assertGreater(sock.recvfrom.call_count, 1)

    def test_udp_connreset_disabled_where_available(self):
        sock = mock.Mock()
        sock.recvfrom.side_effect = [(b"2" + SID.encode()[1:], CAM)]
        with mock.patch.object(socket, "SIO_UDP_CONNRESET", 0x9800000C, create=True):
            p2p.punch(sock, SID, CAM, timeout=5)
        sock.ioctl.assert_called_once_with(0x9800000C, False)

    def test_no_ioctl_without_the_constant(self):
        sock = mock.Mock()
        sock.recvfrom.side_effect = [(b"2" + SID.encode()[1:], CAM)]
        bare = types.SimpleNamespace(timeout=socket.timeout)   # a socket module without SIO_UDP_CONNRESET
        with mock.patch.object(p2p, "socket", bare):
            self.assertEqual(p2p.punch(sock, SID, CAM, timeout=5), CAM)
        sock.ioctl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
