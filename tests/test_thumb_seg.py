"""Per-camera thumbnail clip length: server.thumb_seg (THUMB_SEG_S = 1 s unless cameras.json thumb_seg says 1..3) and its check at load
(config.check_camera). No camera, no network. Run: python -m unittest tests.test_thumb_seg
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kinora import config  # noqa: E402
import server  # noqa: E402


def cam(**extra):
    c = {"id": "cam_seg", "name": "Seg test", "auth": config.AUTH_METHODS[0], "ip": "10.20.30.40"}
    c.update(extra)
    return c


class ThumbSeg(unittest.TestCase):
    def test_default_is_one_second(self):
        self.assertEqual(server.THUMB_SEG_S, 1)
        self.assertEqual(server.thumb_seg(cam()), 1)

    def test_camera_can_raise_it(self):
        for v in (1, 2, 3):
            self.assertEqual(server.thumb_seg(cam(thumb_seg=v)), v)

    def test_load_accepts_1_to_3_and_rejects_the_rest(self):
        for v in (1, 2, 3):
            config.check_camera(cam(thumb_seg=v))
        for bad in (0, 4, -1, 1.0, "2", None, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config.check_camera(cam(thumb_seg=bad))

    def test_camera_without_the_key_loads(self):
        config.check_camera(cam())


if __name__ == "__main__":
    unittest.main()
