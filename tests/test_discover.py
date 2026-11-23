"""Subnet discovery for the setup wizard (kinora/services/discover.py). Offline: the TCP probe is replaced, so no
socket is opened. Run: .venv/bin/python -m unittest tests.test_discover   (from the repo root)
"""
import asyncio
import ipaddress
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aiohttp import web  # noqa: E402
from kinora.errors import BadInput  # noqa: E402
from kinora.services import discover  # noqa: E402


def fake_open(table):
    calls = []

    async def _open(ip, port):
        calls.append((ip, port))
        return port in table.get(ip, ())
    return _open, calls


class ParseSubnet(unittest.TestCase):
    def test_accepts_private(self):
        self.assertEqual(str(discover.parse_subnet("192.168.1.7/24")), "192.168.1.0/24")
        self.assertEqual(str(discover.parse_subnet("10.1.2.0/22")), "10.1.0.0/22")

    def test_rejects(self):
        for bad in ("8.8.8.0/24", "192.168.0.0/16", "10.0.0.0/8", "172.15.0.0/24", "nonsense", "", None, 5,
                    "192.168.1.0/24; rm", "::1/64"):
            with self.assertRaises(BadInput, msg=repr(bad)):
                discover.parse_subnet(bad)


class Scan(unittest.IsolatedAsyncioTestCase):
    async def test_candidates_need_443_and_another_port(self):
        op, calls = fake_open({"192.168.1.10": (443, 554), "192.168.1.11": (443,), "192.168.1.12": (554,),
                               "192.168.1.9": (443, 8800)})
        net = ipaddress.ip_network("192.168.1.0/24")
        with mock.patch.object(discover, "_open", op):
            found, scanned = await discover.scan(net, known={"192.168.1.9"})
        self.assertEqual([f["ip"] for f in found], ["192.168.1.10"])
        self.assertEqual(found[0]["ports"], {"443": True, "554": True, "8800": False, "2020": False})
        self.assertEqual(scanned, 253)
        self.assertFalse([c for c in calls if c[0] == "192.168.1.9"])   # known cameras are never probed
        self.assertFalse([c for c in calls if c[1] not in (443, 554, 8800, 2020)])

    async def test_extra_ports_are_probed_one_at_a_time_and_stop_at_the_first_answer(self):
        op, calls = fake_open({"192.168.1.10": (443, 554, 8800)})
        with mock.patch.object(discover, "_open", op):
            found, _ = await discover.scan(ipaddress.ip_network("192.168.1.8/30"))
        self.assertEqual(found[0]["ports"], {"443": True, "554": True, "8800": False, "2020": False})
        self.assertEqual([c[1] for c in calls if c[0] == "192.168.1.10"], [443, 554])   # 8800 and 2020 never probed

    async def test_concurrency_is_modest(self):
        self.assertLessEqual(discover.CONCURRENCY, 24)

    async def test_out_of_file_handles_is_an_error_not_an_empty_result(self):
        import errno

        async def boom(ip, port):
            raise discover.OutOfFileHandles()
        with mock.patch.object(discover, "_open", boom):
            with self.assertRaises(discover.OutOfFileHandles):
                await discover.scan(ipaddress.ip_network("192.168.1.8/30"))
        with mock.patch.object(discover.asyncio, "open_connection", side_effect=OSError(errno.EMFILE, "too many")):
            with self.assertRaises(discover.OutOfFileHandles):
                await discover._open("192.168.1.1", 443)
        with mock.patch.object(discover.asyncio, "open_connection", side_effect=OSError(errno.ECONNREFUSED, "no")):
            self.assertFalse(await discover._open("192.168.1.1", 443))

    async def test_sorted_numerically(self):
        op, _ = fake_open({"192.168.1.100": (443, 554), "192.168.1.20": (443, 554)})
        with mock.patch.object(discover, "_open", op):
            found, _ = await discover.scan(ipaddress.ip_network("192.168.1.0/24"))
        self.assertEqual([f["ip"] for f in found], ["192.168.1.20", "192.168.1.100"])


class Handler(unittest.IsolatedAsyncioTestCase):
    def req(self, body):
        r = mock.Mock()
        r.json = mock.AsyncMock(return_value=body)
        return r

    async def test_scan_and_throttle(self):
        op, _ = fake_open({"192.168.50.5": (443, 2020)})
        with mock.patch.object(discover, "_open", op), mock.patch.object(discover, "_last", [-99.0]):
            resp = await discover.discover(self.req({"subnet": "192.168.50.0/24"}))
            self.assertEqual(resp.status, 200)
            self.assertIn(b"192.168.50.5", resp.body)
            with self.assertRaises(web.HTTPTooManyRequests):
                await discover.discover(self.req({"subnet": "192.168.50.0/24"}))

    async def test_handler_reports_out_of_handles_as_503(self):
        async def boom(ip, port):
            raise discover.OutOfFileHandles()
        with mock.patch.object(discover, "_open", boom), mock.patch.object(discover, "_last", [-99.0]):
            with self.assertRaises(web.HTTPServiceUnavailable):
                await discover.discover(self.req({"subnet": "192.168.50.0/24"}))

    async def test_default_subnet_and_unknown_field(self):
        with mock.patch.object(discover, "_last", [-99.0]), mock.patch.object(discover, "default_subnet", return_value=""):
            with self.assertRaises(BadInput):
                await discover.discover(self.req({}))
        with self.assertRaises(BadInput):
            await discover.discover(self.req({"subnet": "192.168.1.0/24", "x": 1}))

    def test_default_subnet_private_only(self):
        with mock.patch.object(discover, "_local_ip", return_value="192.168.1.23"):
            self.assertEqual(discover.default_subnet(), "192.168.1.0/24")
        with mock.patch.object(discover, "_local_ip", return_value="8.8.4.4"):
            self.assertEqual(discover.default_subnet(), "")
        with mock.patch.object(discover, "_local_ip", return_value=None):
            self.assertEqual(discover.default_subnet(), "")


if __name__ == "__main__":
    unittest.main()
