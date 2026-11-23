"""Characterization tests: listing cache, interval merge, media_limit, export window planning (no camera).

Uses temp dirs for cache/exports and a fake fetch_media that writes tiny synthetic clips (real ffmpeg).
Run: .venv/bin/python -m unittest tests.test_listings_export
"""
import asyncio
import datetime
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402
from kinora import config  # noqa: E402
from kinora.errors import CameraError  # noqa: E402
from kinora.services import export, listings  # noqa: E402

CAM = {"id": "cT", "name": "Test Cam", "ip": "10.9.9.9"}


def run(coro):
    return asyncio.run(coro)


class Merge(unittest.TestCase):
    def test_merge_overlaps_and_adjacent(self):
        self.assertEqual(export.merge([[5, 8], [1, 3], [2, 4], [8, 9]]), [[1, 4], [5, 9]])

    def test_merge_empty(self):
        self.assertEqual(export.merge([]), [])


class ListingCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(config, "META", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)
        self.calls = 0

    def fetch_ok(self, cam, date):
        async def go():
            self.calls += 1
            return [{"start": 1, "end": 2}]
        return go()

    def test_miss_then_hit_within_ttl(self):
        today = datetime.date.today().strftime("%Y%m%d")
        d, how = run(listings.listing_data(CAM, today, "rec", self.fetch_ok))
        self.assertEqual(how, "miss")
        d, how = run(listings.listing_data(CAM, today, "rec", self.fetch_ok))
        self.assertEqual(how, "hit")
        self.assertEqual(self.calls, 1)

    def test_refresh_forces_fetch(self):
        today = datetime.date.today().strftime("%Y%m%d")
        run(listings.listing_data(CAM, today, "rec", self.fetch_ok))
        _, how = run(listings.listing_data(CAM, today, "rec", self.fetch_ok, refresh=True))
        self.assertEqual(how, "miss")

    def test_today_expires_after_ttl(self):
        today = datetime.date.today().strftime("%Y%m%d")
        run(listings.listing_data(CAM, today, "rec", self.fetch_ok))
        f = Path(self.tmp.name) / f"cT_{today}_rec.json"
        old = time.time() - config.TODAY_TTL - 5
        import os
        os.utime(f, (old, old))
        _, how = run(listings.listing_data(CAM, today, "rec", self.fetch_ok))
        self.assertEqual(how, "miss")

    def test_past_day_saved_after_it_ended_is_cached_forever(self):
        day = "20200102"
        run(listings.listing_data(CAM, day, "rec", self.fetch_ok))   # saved now = long after that day
        f = Path(self.tmp.name) / f"cT_{day}_rec.json"
        import os
        old = time.time() - 10 * 86400
        os.utime(f, (old, old))  # mtime is still > day end + 1 h
        _, how = run(listings.listing_data(CAM, day, "rec", self.fetch_ok))
        self.assertEqual(how, "hit")

    def test_day_saved_before_it_ended_is_not_final(self):
        day = "20200102"
        run(listings.listing_data(CAM, day, "rec", self.fetch_ok))
        f = Path(self.tmp.name) / f"cT_{day}_rec.json"
        import os
        mid = datetime.datetime(2020, 1, 2, 12).timestamp()
        os.utime(f, (mid, mid))
        _, how = run(listings.listing_data(CAM, day, "rec", self.fetch_ok))
        self.assertEqual(how, "miss")

    def test_stale_copy_when_camera_fails(self):
        today = datetime.date.today().strftime("%Y%m%d")
        run(listings.listing_data(CAM, today, "rec", self.fetch_ok))

        def bad(cam, date):
            async def go():
                raise CameraError("down")
            return go()
        data, how = run(listings.listing_data(CAM, today, "rec", bad, refresh=True))
        self.assertEqual(how, "stale")
        self.assertEqual(data, [{"start": 1, "end": 2}])

    def test_error_without_copy_propagates(self):
        def bad(cam, date):
            async def go():
                raise CameraError("down")
            return go()
        with self.assertRaises(CameraError):
            run(listings.listing_data(CAM, "20200102", "rec", bad))


class MediaLimit(unittest.TestCase):
    def test_limit_accounts_for_clock_correction(self):
        cam = {"id": "cL"}
        class T: timeCorrection = -30
        with mock.patch.dict(server._clients, {"cL": T()}), mock.patch.object(server.time, "time", lambda: 10_000):
            self.assertEqual(server.media_limit(cam), 10_000 - server.STILL_RECORDING_S - 30)

    def test_no_client_means_no_correction(self):
        with mock.patch.object(server.time, "time", lambda: 10_000):
            self.assertEqual(server.media_limit({"id": "nobody"}), 10_000 - server.STILL_RECORDING_S)


class PlanWindows(unittest.TestCase):
    def test_plan(self):
        w = config.EXPORT_WIN_S
        self.assertEqual(export.plan_windows([[0, 10], [10, 20], [100, 100 + w + 5]], 5, 100 + w + 50),
                         [(5, 20), (100, 100 + w), (100 + w, 100 + w + 5)])

    def test_drops_pieces_under_two_seconds(self):
        self.assertEqual(export.plan_windows([[0, 600]], 599, 700), [])


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class ExportPlan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "cache").mkdir()
        (root / "exports").mkdir()
        self.fetched = []

        async def fake_fetch(cam, a, b, name):
            self.fetched.append((a, b))
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=1:r=16000",
                            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-shortest", "-c:v", "libx264",
                            "-c:a", "aac", str(root / "cache" / name)], check=True)

        async def fake_list(date):
            return self.recs
        self.fake_list, self.fake_fetch = fake_list, fake_fetch
        for p in (mock.patch.object(config, "CACHE", root / "cache"),
                  mock.patch.object(config, "EXPORTS", root / "exports")):
            p.start()
            self.addCleanup(p.stop)
        self.root = root

    def job(self, start, end, mode="audio_wav"):
        j = {"id": "j1", "cam": "cT", "start": start, "end": end, "mode": mode,
             "state": "queued", "message": "", "cancel": False, "done": 0, "total": 0}
        run(export.run(j, CAM, self.fake_list, self.fake_fetch))
        return j

    def test_gaps_skipped_and_windows_split(self):
        base = int(datetime.datetime(2026, 1, 5, 10).timestamp())
        w = config.EXPORT_WIN_S
        self.recs = [{"start": base, "end": base + 10}, {"start": base + 10, "end": base + 20},   # merge
                     {"start": base + 100, "end": base + 100 + w + 5}]                            # needs 2 windows
        j = self.job(base - 50, base + 100 + w + 50)
        self.assertEqual(j["state"], "done", j["message"])
        self.assertEqual(self.fetched, [(base, base + 20), (base + 100, base + 100 + w), (base + 100 + w, base + 100 + w + 5)])
        side = json.loads(next(self.root.glob("exports/*.json")).read_text())
        self.assertEqual([s["start"] for s in side["segments"]], [a for a, _ in self.fetched])
        offs = [s["offset_s"] for s in side["segments"]]
        self.assertEqual(offs[0], 0)
        self.assertTrue(all(b > a for a, b in zip(offs, offs[1:])))   # offsets increase by each clip's length

    def test_range_is_clipped_to_the_request(self):
        base = int(datetime.datetime(2026, 1, 5, 10).timestamp())
        self.recs = [{"start": base, "end": base + 600}]
        self.job(base + 100, base + 160)
        self.assertEqual(self.fetched, [(base + 100, base + 160)])

    def test_too_short_overlap_is_dropped(self):
        base = int(datetime.datetime(2026, 1, 5, 10).timestamp())
        self.recs = [{"start": base, "end": base + 600}]
        j = self.job(base + 599, base + 700)
        self.assertEqual(j["state"], "error")
        self.assertIn("no recording", j["message"])
        self.assertEqual(self.fetched, [])


if __name__ == "__main__":
    unittest.main()
