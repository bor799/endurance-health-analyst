#!/usr/bin/env python3
"""Focused unit tests for muscle recovery tracking and reminders."""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import eha_lib as E  # noqa: E402
import manage_recovery as R  # noqa: E402


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eha_recovery_")
        self.db = os.path.join(self.tmp.name, "health.duckdb")
        self.conn = E.connect(self.db, E.DEFAULT_CONFIG)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_schema_uses_one_recovery_table_with_approved_fields(self):
        tables = {row[0] for row in self.conn.execute("SHOW TABLES").fetchall()}
        self.assertIn("muscle_recovery", tables)
        self.assertNotIn("muscle_recovery_plans", tables)
        self.assertNotIn("muscle_recovery_checkins", tables)
        columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info('muscle_recovery')").fetchall()
        }
        self.assertTrue({
            "plan_id", "workout_id", "muscle_groups", "routine", "duration_min",
            "remind_at", "timezone", "reminder_backend", "reminder_external_id",
            "reminder_state", "plan_status", "soreness_before", "soreness_after",
            "note", "created_at", "updated_at", "completed_at",
        }.issubset(columns))

    def test_create_complete_list_and_score_validation(self):
        plan = R.create_plan(
            self.conn,
            muscles="Calves, quads, calves",
            routine_name="post_run_basic",
            remind_at="2026-08-24T21:30:00+08:00",
            note="Keep it easy",
            soreness_before=5,
        )
        self.assertEqual(plan["plan_status"], "planned")
        self.assertEqual(plan["reminder_state"], "pending")
        self.assertEqual(plan["muscle_groups"], ["calves", "quads"])
        self.assertEqual(plan["timezone"], "UTC+08:00")
        self.assertTrue(plan["remind_at_local"].startswith("2026-08-24T21:30:00+08:00"))
        self.assertIn("不追求疼痛", " ".join(plan["routine"]["steps"]))
        self.assertEqual(plan["soreness_before"], 5)

        with self.assertRaisesRegex(R.RecoveryError, "between 0 and 10"):
            R.finish_plan(self.conn, plan["plan_id"], "completed", soreness_before=11)

        completed = R.finish_plan(
            self.conn, plan["plan_id"], "completed",
            soreness_before=5, soreness_after=2, note="Felt comfortable",
        )
        self.assertEqual(completed["plan_status"], "completed")
        self.assertEqual(completed["reminder_state"], "canceled")
        self.assertEqual(completed["soreness_before"], 5)
        self.assertEqual(completed["soreness_after"], 2)
        self.assertIsNotNone(completed["completed_at"])
        self.assertEqual(
            [item["plan_id"] for item in R.list_plans(self.conn, status="completed")],
            [plan["plan_id"]],
        )

    def test_score_boundaries_and_timezone_validation(self):
        plan = R.create_plan(self.conn, "calves", "post_run_basic")
        skipped = R.finish_plan(
            self.conn, plan["plan_id"], "skipped",
            soreness_before=0, soreness_after=10,
        )
        self.assertEqual(skipped["soreness_before"], 0)
        self.assertEqual(skipped["soreness_after"], 10)
        self.assertIsNone(skipped["completed_at"])
        with self.assertRaisesRegex(R.RecoveryError, "unknown timezone"):
            R.create_plan(
                self.conn, "quads", "post_run_basic",
                remind_at="2026-08-24T21:30:00+08:00", timezone_name="Mars/Olympus",
            )

    def test_dst_gaps_and_ambiguous_local_times_are_rejected(self):
        with self.assertRaisesRegex(R.RecoveryError, "does not exist"):
            R.create_plan(
                self.conn, "calves", "post_run_basic",
                remind_at="2026-03-08T02:30:00", timezone_name="America/New_York",
            )
        with self.assertRaisesRegex(R.RecoveryError, "ambiguous"):
            R.create_plan(
                self.conn, "calves", "post_run_basic",
                remind_at="2026-11-01T01:30:00", timezone_name="America/New_York",
            )
        explicit = R.create_plan(
            self.conn, "calves", "post_run_basic",
            remind_at="2026-11-01T01:30:00-05:00",
        )
        self.assertEqual(explicit["remind_at"], "2026-11-01T06:30:00Z")

    def test_audit_timestamps_remain_utc_under_non_utc_duckdb_session(self):
        self.conn.execute("SET TimeZone='Asia/Shanghai'")
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        plan = R.create_plan(self.conn, "calves", "post_run_basic")
        after = datetime.now(timezone.utc).replace(tzinfo=None)
        created_at, updated_at = self.conn.execute(
            "SELECT created_at, updated_at FROM muscle_recovery WHERE plan_id=?",
            [plan["plan_id"]],
        ).fetchone()
        self.assertLessEqual(before, created_at)
        self.assertLessEqual(created_at, after)
        self.assertEqual(updated_at, created_at)
        self.assertEqual(plan["created_at"], created_at.isoformat() + "Z")

    def test_list_limit_must_be_positive(self):
        with self.assertRaisesRegex(R.RecoveryError, "positive integer"):
            R.list_plans(self.conn, limit=0)
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            code = R.main(["list", "--db", self.db, "--limit", "-1"])
        self.assertEqual(code, 2)
        self.assertIn("limit must be a positive integer", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_due_lists_and_marks_only_due_plans(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        due = R.create_plan(self.conn, "calves", "lower_leg_gentle", remind_at=past)
        R.create_plan(self.conn, "glutes", "hips_glutes_gentle", remind_at=future)

        listed = R.due_plans(self.conn)
        self.assertEqual([item["plan_id"] for item in listed], [due["plan_id"]])
        fired = R.due_plans(self.conn, mark_fired=True)
        self.assertEqual(fired[0]["reminder_state"], "fired")
        self.assertEqual(R.due_plans(self.conn), [])

    @mock.patch("manage_recovery.subprocess.run")
    def test_macos_create_uses_safe_argv_and_schedules_after_success(self, run):
        run.return_value = SimpleNamespace(returncode=0, stdout="x-apple-reminder-id\n", stderr="")
        plan = R.create_plan(
            self.conn,
            muscles='calves"; display dialog "unsafe',
            routine_name="post_run_basic",
            remind_at="2026-08-24T21:30:00+08:00",
            reminder_backend="macos",
            macos_list="Training Reminders",
        )

        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["osascript", R.SCRIPT_PATH, "create"])
        self.assertEqual(command[3], "Training Reminders")
        self.assertIn('calves"; display dialog "unsafe', command[4])
        self.assertNotIn("-e", command)
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(plan["reminder_state"], "scheduled")
        self.assertEqual(plan["reminder_external_id"], "x-apple-reminder-id")

    @mock.patch("manage_recovery.subprocess.run")
    def test_macos_failure_never_marks_plan_scheduled(self, run):
        run.return_value = SimpleNamespace(
            returncode=1, stdout="", stderr="Not authorized to send Apple events"
        )
        with self.assertRaisesRegex(R.RecoveryError, "Automation and Reminders permissions"):
            R.create_plan(
                self.conn,
                muscles="calves",
                routine_name="post_run_basic",
                remind_at="2026-08-24T21:30:00+08:00",
                reminder_backend="macos",
            )
        row = self.conn.execute(
            "SELECT reminder_state, reminder_external_id FROM muscle_recovery"
        ).fetchone()
        self.assertEqual(row, ("failed", None))

    @mock.patch("manage_recovery.subprocess.run")
    def test_complete_and_cancel_sync_external_reminders_first(self, run):
        run.return_value = SimpleNamespace(returncode=0, stdout="external-id\n", stderr="")
        completed_plan = R.create_plan(
            self.conn, "calves", "post_run_basic",
            remind_at="2026-08-24T21:30:00+08:00", reminder_backend="macos",
        )
        canceled_plan = R.create_plan(
            self.conn, "quads", "post_run_basic",
            remind_at="2026-08-24T22:00:00+08:00", reminder_backend="macos",
        )

        run.reset_mock()
        completed = R.finish_plan(self.conn, completed_plan["plan_id"], "completed")
        canceled = R.cancel_plan(self.conn, canceled_plan["plan_id"])
        actions = [call.args[0][2] for call in run.call_args_list]
        self.assertEqual(actions, ["complete", "cancel"])
        self.assertEqual(completed["plan_status"], "completed")
        self.assertEqual(canceled["plan_status"], "canceled")

    @mock.patch("manage_recovery.run_macos_reminder")
    def test_interrupted_terminal_transition_is_retryable(self, reminder):
        reminder.return_value = "external-id"
        plan = R.create_plan(
            self.conn, "calves", "post_run_basic",
            remind_at="2026-08-24T21:30:00+08:00", reminder_backend="macos",
        )
        self.conn.execute(
            "UPDATE muscle_recovery SET reminder_state='canceling' WHERE plan_id=?",
            [plan["plan_id"]],
        )
        self.conn.commit()

        observed_states = []

        def already_absent(action, external_id):
            observed_states.append(
                self.conn.execute(
                    "SELECT reminder_state FROM muscle_recovery WHERE plan_id=?",
                    [plan["plan_id"]],
                ).fetchone()[0]
            )
            self.assertEqual((action, external_id), ("cancel", "external-id"))
            return "not-found"

        reminder.side_effect = already_absent
        canceled = R.cancel_plan(self.conn, plan["plan_id"])
        self.assertEqual(observed_states, ["canceling"])
        self.assertEqual(canceled["plan_status"], "canceled")
        self.assertEqual(canceled["reminder_state"], "canceled")

    @mock.patch("manage_recovery.subprocess.run")
    def test_failed_external_completion_leaves_database_planned(self, run):
        run.return_value = SimpleNamespace(returncode=0, stdout="external-id\n", stderr="")
        plan = R.create_plan(
            self.conn, "calves", "post_run_basic",
            remind_at="2026-08-24T21:30:00+08:00", reminder_backend="macos",
        )
        run.return_value = SimpleNamespace(returncode=1, stdout="", stderr="permission denied")
        with self.assertRaises(R.RecoveryError):
            R.finish_plan(self.conn, plan["plan_id"], "completed")
        persisted = R.get_plan(self.conn, plan["plan_id"])
        self.assertEqual(persisted["plan_status"], "planned")
        self.assertEqual(persisted["reminder_state"], "failed")

        run.return_value = SimpleNamespace(returncode=0, stdout="not-found\n", stderr="")
        completed = R.finish_plan(self.conn, plan["plan_id"], "completed")
        self.assertEqual(completed["plan_status"], "completed")
        self.assertEqual(completed["reminder_state"], "canceled")


if __name__ == "__main__":
    unittest.main()
