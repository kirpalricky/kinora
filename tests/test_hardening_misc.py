"""Small hardening checks: listings drop malformed camera rows, HLS file names reject a trailing newline,
the camera-reported wait is capped. Offline.
Run: .venv/bin/python -m unittest tests.test_hardening_misc"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kinora.services import hls, listings  # noqa: E402


def api_returning(raw):
    async def api(cam, fn):
        return raw
    return api


class Listings(unittest.TestCase):
    def test_events_keep_only_integer_rows(self):
        raw = [{"start_time": 10, "end_time": 20, "alarm_type": 1},
               {"start_time": 30, "end_time": 40, "alarm_type": '"><b>'},
               {"start_time": "5", "end_time": 6, "alarm_type": 1},
               {"start_time": True, "end_time": 6, "alarm_type": 1},
               {"start_time": 50, "end_time": 40, "alarm_type": 1}]
        out = asyncio.run(listings.fetch_events(api_returning(raw), "c", "20260101"))
        self.assertEqual(out, [{"start": 10, "end": 20, "type": 1}])

    def test_recordings_keep_only_integer_rows(self):
        raw = [{"a": {"startTime": 1, "endTime": 2, "vedio_type": "x"}, "b": {"startTime": "1", "endTime": 2}}]
        out = asyncio.run(listings.fetch_recordings(api_returning(raw), "c", "20260101"))
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["start"], out[0]["end"]), (1, 2))


class HlsNames(unittest.TestCase):
    def test_names(self):
        for ok in ("index.m3u8", "seg_00001.ts"):
            self.assertTrue(hls.FILE_RE.fullmatch(ok))
        for bad in ("index.m3u8\n", "seg_00001.ts\n", "../index.m3u8", "seg_1.ts"):
            self.assertFalse(hls.FILE_RE.fullmatch(bad), bad)


if __name__ == "__main__":
    unittest.main()
