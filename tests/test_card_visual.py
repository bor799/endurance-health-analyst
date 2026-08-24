from __future__ import annotations

import base64
import io
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import card_visual as V  # noqa: E402
import render_card as R  # noqa: E402


class PrivacyTrimTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"privacy": {"start_trim_m": 300, "end_trim_m": 300}}
        self.t0 = datetime(2026, 1, 1, 6, 0)

    def points(self, n=20, step=0.0005):
        return [(self.t0 + timedelta(seconds=i), 22.5, 113.9 + i * step, None)
                for i in range(n)]

    def test_no_route(self):
        self.assertEqual(R.privacy_trim([], self.cfg, True), ([], "no_route"))

    def test_privacy_off_is_explicit(self):
        points = self.points()
        safe, status = R.privacy_trim(points, self.cfg, False)
        self.assertEqual(status, "off")
        self.assertEqual(safe, points)

    def test_short_route_fails_closed(self):
        points = self.points(9)
        safe, status = R.privacy_trim(points, self.cfg, True)
        self.assertEqual(status, "fully_hidden")
        self.assertEqual(safe, [])

    def test_fully_trimmed_route_never_restores_original(self):
        points = self.points(20, step=0.00001)
        safe, status = R.privacy_trim(points, self.cfg, True)
        self.assertEqual(status, "fully_hidden")
        self.assertEqual(safe, [])

    def test_normal_trim_projects_to_local_xy(self):
        safe, status = R.privacy_trim(self.points(), self.cfg, True)
        self.assertEqual(status, "trimmed")
        self.assertLess(len(safe), 20)
        paths = R.project_route_xy(safe)
        self.assertTrue(paths)
        for path in paths:
            for x, y in path:
                self.assertGreaterEqual(x, 0)
                self.assertLessEqual(x, 1)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(y, 1)


class AtomicOutputTests(unittest.TestCase):
    def setUp(self):
        import eha_lib as E

        self.tmp = tempfile.TemporaryDirectory(prefix="eha_atomic_")
        self.db = os.path.join(self.tmp.name, "health.duckdb")
        self.out = os.path.join(self.tmp.name, "output")
        os.makedirs(self.out)
        conn = E.connect(self.db, E.DEFAULT_CONFIG)
        conn.execute(
            """INSERT INTO workouts
               (workout_id, sport_type, source, start_time, duration_s, distance_m,
                avg_pace_s_per_km, avg_hr)
               VALUES ('atomic-run', 'run', 'test', '2026-08-24 10:00:00',
                       1800, 5000, 360, 150)"""
        )
        conn.executemany(
            """INSERT INTO route_points VALUES
               ('atomic-run', ?, ?, ?, ?, NULL)""",
            [[i, datetime(2026, 8, 24, 10, 0) + timedelta(seconds=i),
              35.1, 110.1 + i * 0.0005] for i in range(20)],
        )
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def seed_stale_outputs(self):
        for name in ("workout-card.html", "report.html", "workout-card.png"):
            with open(os.path.join(self.out, name), "wb") as f:
                f.write(b"OLD PRIVACY OFF ARTIFACT")

    def test_explicit_privacy_on_overrides_persistent_off_default(self):
        import copy
        import eha_lib as E

        cfg = copy.deepcopy(E.DEFAULT_CONFIG)
        cfg["privacy"]["default_on"] = False
        argv = ["--db", self.db, "--workout", "atomic-run", "--privacy", "on",
                "--renderer", "pillow"]
        with mock.patch.object(R.E, "OUTPUT_DIR", self.out), \
                mock.patch.object(R.E, "load_config", return_value=cfg):
            self.assertEqual(R.main(argv), 0)
        with open(os.path.join(self.out, "workout-card.html"), encoding="utf-8") as f:
            page = f.read()
        self.assertIn("起终点已隐藏", page)
        self.assertNotIn("隐私模式已关闭", page)

    def test_failed_photo_invalidates_canonical_outputs(self):
        self.seed_stale_outputs()
        argv = ["--db", self.db, "--workout", "atomic-run",
                "--photo", "/private/home-route.jpg", "--renderer", "pillow"]
        with mock.patch.object(R.E, "OUTPUT_DIR", self.out):
            self.assertEqual(R.main(argv), 2)
        for name in ("workout-card.html", "report.html", "workout-card.png"):
            self.assertFalse(os.path.exists(os.path.join(self.out, name)))

    def test_failed_photo_invalidates_custom_output(self):
        custom = os.path.join(self.tmp.name, "custom-share.png")
        with open(custom, "wb") as f:
            f.write(b"OLD PRIVACY OFF ARTIFACT")
        argv = ["--db", self.db, "--workout", "atomic-run", "--out", custom,
                "--photo", "/private/home-route.jpg", "--renderer", "pillow"]
        with mock.patch.object(R.E, "OUTPUT_DIR", self.out):
            self.assertEqual(R.main(argv), 2)
        self.assertFalse(os.path.exists(custom))

    def test_failed_renderer_does_not_publish_partial_outputs(self):
        self.seed_stale_outputs()
        argv = ["--db", self.db, "--workout", "atomic-run", "--renderer", "chrome"]
        with mock.patch.object(R.E, "OUTPUT_DIR", self.out), \
                mock.patch.object(R, "chrome_screenshot", return_value=False):
            self.assertEqual(R.main(argv), 2)
        for name in ("workout-card.html", "report.html", "workout-card.png"):
            self.assertFalse(os.path.exists(os.path.join(self.out, name)))


class PhotoAndPerspectiveTests(unittest.TestCase):
    def test_photo_is_transposed_cropped_and_metadata_free(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "oriented.jpg")
            src = Image.new("RGB", (120, 80), "#ef2f24")
            for x in range(60, 120):
                for y in range(80):
                    src.putpixel((x, y), (25, 70, 220))
            exif = src.getexif()
            exif[274] = 6  # rotate 90° clockwise: red becomes top, blue bottom
            exif[34853] = {1: "N"}
            src.save(path, quality=100, subsampling=0, exif=exif)
            clean = V.normalize_photo(path, size=(200, 200))
            with Image.open(io.BytesIO(clean)) as out:
                self.assertEqual(out.size, (200, 200))
                self.assertEqual(out.mode, "RGB")
                top, bottom = out.getpixel((100, 25)), out.getpixel((100, 175))
                self.assertGreater(top[0], top[2], "EXIF orientation did not move red to top")
                self.assertGreater(bottom[2], bottom[0], "EXIF orientation did not move blue to bottom")
                self.assertFalse(out.getexif())
                self.assertNotIn("gps", {str(k).lower() for k in out.info})

    def test_heic_photo_is_supported(self):
        from PIL import Image
        from pillow_heif import register_heif_opener

        register_heif_opener()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "iphone-photo.heic")
            Image.new("RGB", (90, 140), "#61798a").save(path, format="HEIF")
            clean = V.normalize_photo(path, size=(120, 120))
            with Image.open(io.BytesIO(clean)) as out:
                self.assertEqual(out.size, (120, 120))
                self.assertFalse(out.getexif())

    def test_missing_photo_error_hides_parent_path(self):
        path = "/Users/private-person/Pictures/home-location.jpg"
        with self.assertRaises(ValueError) as caught:
            V.normalize_photo(path)
        self.assertIn("home-location.jpg", str(caught.exception))
        self.assertNotIn("/Users/private-person", str(caught.exception))

    def test_broken_photo_is_an_error(self):
        with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
            f.write(b"not an image")
            f.flush()
            with self.assertRaises(ValueError):
                V.normalize_photo(f.name)

    def test_perspective_preserves_path_order(self):
        paths = [[(0.1, 0.1), (0.5, 0.5), (0.9, 0.9)]]
        warped = V.warp_route_paths(paths)
        self.assertEqual(len(warped), 1)
        self.assertEqual(len(warped[0]), 3)
        self.assertLess(warped[0][0][1], warped[0][1][1])
        self.assertLess(warped[0][1][1], warped[0][2][1])

    def test_html_escapes_text_and_embeds_normalized_png(self):
        from PIL import Image

        photo = io.BytesIO()
        Image.new("RGB", (20, 20), "#5a7180").save(photo, "PNG")
        spec = {
            "photo_png": photo.getvalue(),
            "route_paths": [],
            "privacy_tag": '起终点已隐藏 <script>alert("x")</script>',
            "sport_label": "R U N",
            "date_str": "2026.08.24  18:00",
            "distance": "5.23",
            "duration": "36:48",
            "pace": "7'02\"/km",
            "avg_hr": "151",
            "one_liner": '<img src=x onerror="alert(1)"> & 安全',
            "foot_sub": "run",
        }
        template = os.path.join(ROOT, "templates", "workout-card.html")
        page = V.build_html(spec, template)
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&lt;img src=x", page)
        match = re.search(r'src="data:image/png;base64,([A-Za-z0-9+/=]+)"', page)
        self.assertIsNotNone(match)
        with Image.open(io.BytesIO(base64.b64decode(match.group(1)))) as embedded:
            self.assertEqual(embedded.format, "PNG")

    def test_pillow_card_is_square_and_has_no_metadata(self):
        from PIL import Image

        spec = {
            "photo_png": None,
            "route_paths": [[(0.1, 0.1), (0.5, 0.5), (0.9, 0.9)]],
            "privacy_tag": "起终点已隐藏",
            "sport_label": "R U N",
            "date_str": "2026.08.24  18:00",
            "distance": "5.23",
            "duration": "36:48",
            "pace": "7'02\"/km",
            "avg_hr": "151",
            "one_liner": "今天与个人基线接近，湿热环境增加了生理成本。",
            "foot_sub": "run",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "card.png")
            V.render_pillow(spec, path)
            with Image.open(path) as out:
                self.assertEqual(out.size, (2160, 2160))
                self.assertFalse(out.getexif())
                self.assertNotIn("gps", {str(k).lower() for k in out.info})


if __name__ == "__main__":
    unittest.main()
