from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import analyze_workout as A  # noqa: E402
import eha_lib as E  # noqa: E402


class HeartRateBasisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eha_analysis_")
        self.conn = E.connect(os.path.join(self.tmp.name, "health.duckdb"), E.DEFAULT_CONFIG)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_configured_max_hr_precedes_observed_history(self):
        self.conn.execute(
            """INSERT INTO workouts (workout_id, start_time, max_hr)
               VALUES ('observed', now(), 151)"""
        )
        cfg = {"athlete": {"max_hr": 190}}
        basis = A.resolve_hr_basis(self.conn, cfg, current_max_hr=150)
        self.assertEqual(basis["type"], "configured_max_hr")
        self.assertEqual(basis["max_hr"], 190)
        self.assertFalse(basis["estimated"])
        boundaries = A.zone_boundaries(basis)
        self.assertEqual(boundaries[1][1], 114)
        self.assertEqual(boundaries[4][1], 171)

    def test_invalid_configured_max_hr_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "between 100 and 240"):
            A.resolve_hr_basis(self.conn, {"athlete": {"max_hr": 80}}, current_max_hr=150)


if __name__ == "__main__":
    unittest.main()
