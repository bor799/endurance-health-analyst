from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import eha_lib as E  # noqa: E402
import enrich_weather as W  # noqa: E402


class WeatherPositionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eha_weather_")
        self.conn = E.connect(os.path.join(self.tmp.name, "health.duckdb"), E.DEFAULT_CONFIG)
        self.conn.execute(
            """INSERT INTO workouts (workout_id, sport_type, start_time)
               VALUES ('with-route', 'run', '2026-08-01 10:00:00'),
                      ('without-route', 'run', '2026-08-10 10:00:00')"""
        )
        self.conn.executemany(
            "INSERT INTO route_points VALUES ('with-route', ?, '2026-08-01 10:00:00', ?, ?, NULL)",
            [[0, 35.123456, 110.654321], [1, 35.123556, 110.654421]],
        )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_own_route_is_labeled(self):
        lat, lon, source = W.workout_position(self.conn, "with-route")
        self.assertAlmostEqual(lat, 35.123506)
        self.assertAlmostEqual(lon, 110.654371)
        self.assertEqual(source, "workout_route")

    def test_route_less_workout_discloses_borrowed_position(self):
        lat, lon, source = W.workout_position(self.conn, "without-route")
        self.assertAlmostEqual(lat, 35.123506)
        self.assertAlmostEqual(lon, 110.654371)
        self.assertEqual(source, "nearest_geolocated_workout_60d")

    def test_position_source_is_exposed_in_weather_output(self):
        self.conn.execute(
            """INSERT INTO weather VALUES
               ('without-route', 28, 75, 33, 23, 8, 0,
                '{"position_source":"nearest_geolocated_workout_60d"}', now())"""
        )
        weather = E.get_weather(self.conn, "without-route")
        self.assertEqual(weather["position_source"], "nearest_geolocated_workout_60d")


if __name__ == "__main__":
    unittest.main()
