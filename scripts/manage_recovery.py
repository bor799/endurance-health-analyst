#!/usr/bin/env python3
"""Track gentle muscle-recovery routines and optional macOS reminders.

This is a self-management log, not a diagnostic or treatment tool. Reminder
creation is opt-in and uses argv-based ``osascript`` calls; user text is never
interpolated into AppleScript source.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eha_lib as E  # noqa: E402

PLAN_STATUSES = {"planned", "completed", "skipped", "canceled"}
REMINDER_STATES = {
    "pending", "scheduled", "fired", "completing", "skipping", "canceling",
    "canceled", "failed",
}
TERMINAL_SYNC_STATES = {"completing", "skipping", "canceling"}
SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminder_macos.scpt")

ROUTINES = {
    "post_run_basic": {
        "name": "跑后温和放松",
        "default_duration_min": 10,
        "steps": [
            "轻松走 3-5 分钟，让呼吸自然平稳。",
            "在舒适范围内放松小腿和股四头肌，每侧 20-30 秒。",
            "用轻柔的髋部和踝部活动收尾，全程不追求疼痛或极限幅度。",
        ],
        "caution": "如果动作引起尖锐疼痛或不适持续加重，立即停止，不要强行拉伸。",
    },
    "lower_leg_gentle": {
        "name": "小腿温和放松",
        "default_duration_min": 8,
        "steps": [
            "慢走 2-3 分钟。",
            "在舒适范围内活动踝关节，再做轻柔的小腿放松。",
            "如使用泡沫轴，保持轻压力并避开疼痛区域。",
        ],
        "caution": "该清单只用于舒适的日常自我放松，不用于损伤治疗。",
    },
    "hips_glutes_gentle": {
        "name": "髋部与臀肌温和活动",
        "default_duration_min": 10,
        "steps": [
            "先轻松走动，或站立做小幅髋部环绕。",
            "在舒适范围内活动臀肌和髋屈肌，每侧 20-30 秒。",
            "用缓慢可控的髋部活动收尾，不强求动作深度。",
        ],
        "caution": "如果症状加重或动作不舒服，立即停止。",
    },
}

MUSCLE_LABELS = {
    "calves": "小腿",
    "quads": "股四头肌",
    "glutes": "臀肌",
    "hamstrings": "腘绳肌",
    "hips": "髋部",
    "ankles": "踝部",
    "lower_back": "下背部",
}

ROW_COLS = [
    "plan_id", "workout_id", "muscle_groups", "routine", "duration_min",
    "remind_at", "timezone", "reminder_backend", "reminder_external_id",
    "reminder_state", "plan_status", "soreness_before", "soreness_after",
    "note", "checkin_note", "created_at", "updated_at", "completed_at",
]


class RecoveryError(ValueError):
    """Expected CLI validation or integration error."""


def validate_score(value: int | None, label: str) -> int | None:
    if value is None:
        return None
    if not 0 <= value <= 10:
        raise RecoveryError(f"{label} must be between 0 and 10")
    return value


def parse_muscles(value: str) -> list[str]:
    muscles = []
    for item in value.split(","):
        normalized = item.strip().lower().replace(" ", "_")
        if normalized and normalized not in muscles:
            muscles.append(normalized)
    if not muscles:
        raise RecoveryError("at least one muscle group is required")
    return muscles


def _localize_strict(local_time: datetime, zone: ZoneInfo) -> datetime:
    """Attach an IANA timezone, rejecting DST gaps and ambiguous wall times."""
    candidates = []
    for fold in (0, 1):
        aware = local_time.replace(tzinfo=zone, fold=fold)
        round_trip = aware.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) == local_time:
            candidates.append(aware)
    if not candidates:
        raise RecoveryError(
            "remind-at does not exist in the requested timezone because of a DST transition"
        )
    if len(candidates) == 2 and candidates[0].utcoffset() != candidates[1].utcoffset():
        raise RecoveryError(
            "remind-at is ambiguous in the requested timezone; include an explicit UTC offset"
        )
    return candidates[0]


def parse_remind_at(value: str, timezone_name: str | None = None):
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise RecoveryError("remind-at must be an ISO-8601 date and time") from exc
    named_timezone = None
    if timezone_name:
        try:
            named_timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise RecoveryError(f"unknown timezone: {timezone_name}") from exc
    if parsed.tzinfo is None:
        if named_timezone is None:
            raise RecoveryError("a timezone is required when remind-at has no UTC offset")
        parsed = _localize_strict(parsed, named_timezone)
    stored_timezone = timezone_name or str(parsed.tzinfo)
    return E.to_utc(parsed), stored_timezone, parsed


def _json_value(value):
    if value is None or isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _timezone_for_name(name: str | None):
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        match = re.fullmatch(r"(?:UTC)?([+-])(\d{2}):(\d{2})", name)
        if not match:
            return timezone.utc
        sign = 1 if match.group(1) == "+" else -1
        offset = timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))
        return timezone(sign * offset)


def row_to_dict(row) -> dict:
    item = dict(zip(ROW_COLS, row))
    item["muscle_groups"] = _json_value(item["muscle_groups"])
    item["routine"] = _json_value(item["routine"])
    for key in ("remind_at", "created_at", "updated_at", "completed_at"):
        if item[key] is not None:
            item[key] = item[key].replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    if row[5] is not None:
        local = row[5].replace(tzinfo=timezone.utc).astimezone(_timezone_for_name(row[6]))
        item["remind_at_local"] = local.isoformat()
    return item


def get_plan(conn, ref: str):
    rows = conn.execute(
        f"SELECT {', '.join(ROW_COLS)} FROM muscle_recovery WHERE plan_id LIKE ?",
        [ref + "%"],
    ).fetchall()
    if not rows:
        raise RecoveryError(f"recovery plan not found: {ref}")
    if len(rows) > 1:
        raise RecoveryError(f"recovery plan prefix is ambiguous: {ref}")
    return row_to_dict(rows[0])


def run_macos_reminder(action: str, *args: str) -> str:
    if sys.platform != "darwin":
        raise RecoveryError("macOS Reminders integration is only available on macOS")
    if action not in {"create", "complete", "cancel"}:
        raise RecoveryError(f"unsupported reminder action: {action}")
    command = ["osascript", SCRIPT_PATH, action, *(str(arg) for arg in args)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise RecoveryError(f"could not run osascript: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown osascript error").strip()
        raise RecoveryError(
            "macOS Reminders request failed. Check Automation and Reminders permissions: "
            + detail
        )
    return result.stdout.strip()


def _schedule_macos(conn, plan: dict, due: datetime, list_name: str) -> dict:
    local_due = due.astimezone()
    routine_name = plan["routine"]["name"]
    muscles = "、".join(MUSCLE_LABELS.get(m, m.replace("_", " "))
                       for m in plan["muscle_groups"])
    title = f"跑后放松：{muscles}"
    body = f"{routine_name}，约 {plan['duration_min']} 分钟"
    try:
        external_id = run_macos_reminder(
            "create", list_name, title, body,
            local_due.year, local_due.month, local_due.day,
            local_due.hour, local_due.minute, local_due.second,
        )
        if not external_id:
            raise RecoveryError("macOS Reminders did not return a reminder id")
    except RecoveryError:
        conn.execute(
            "UPDATE muscle_recovery SET reminder_backend='macos', reminder_state='failed', "
            "updated_at=? WHERE plan_id=?",
            [E.utc_now(), plan["plan_id"]],
        )
        conn.commit()
        raise
    conn.execute(
        "UPDATE muscle_recovery SET reminder_backend='macos', reminder_external_id=?, "
        "reminder_state='scheduled', updated_at=? WHERE plan_id=?",
        [external_id, E.utc_now(), plan["plan_id"]],
    )
    conn.commit()
    return get_plan(conn, plan["plan_id"])


def create_plan(
    conn,
    muscles: str,
    routine_name: str,
    duration_min: int | None = None,
    workout_ref: str | None = None,
    remind_at: str | None = None,
    timezone_name: str | None = None,
    note: str | None = None,
    soreness_before: int | None = None,
    reminder_backend: str = "none",
    macos_list: str = "Reminders",
) -> dict:
    if routine_name not in ROUTINES:
        raise RecoveryError(f"unknown routine: {routine_name}")
    muscle_groups = parse_muscles(muscles)
    routine = ROUTINES[routine_name]
    duration = duration_min if duration_min is not None else routine["default_duration_min"]
    if not 1 <= duration <= 60:
        raise RecoveryError("duration-min must be between 1 and 60")
    soreness = validate_score(soreness_before, "soreness-before")

    workout_id = None
    if workout_ref:
        workout_id = E.resolve_workout(conn, workout_ref)
        if not workout_id:
            raise RecoveryError(f"workout not found: {workout_ref}")

    remind_utc = stored_timezone = due = None
    if remind_at:
        remind_utc, stored_timezone, due = parse_remind_at(remind_at, timezone_name)
    if reminder_backend == "macos" and due is None:
        raise RecoveryError("--remind-at is required for a macOS reminder")

    plan_id = "recovery_" + uuid.uuid4().hex[:12]
    routine_payload = {"id": routine_name, **routine}
    created_at = E.utc_now()
    conn.execute(
        """INSERT INTO muscle_recovery (
               plan_id, workout_id, muscle_groups, routine, duration_min,
               remind_at, timezone, reminder_backend, reminder_external_id,
               reminder_state, plan_status, soreness_before, soreness_after,
               note, checkin_note, created_at, updated_at, completed_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'pending', 'planned',
                     ?, NULL, ?, NULL, ?, ?, NULL)""",
        [
            plan_id, workout_id, json.dumps(muscle_groups),
            json.dumps(routine_payload), duration, remind_utc, stored_timezone,
            soreness, note, created_at, created_at,
        ],
    )
    conn.commit()
    plan = get_plan(conn, plan_id)
    if reminder_backend == "macos":
        return _schedule_macos(conn, plan, due, macos_list)
    return plan


def _sync_terminal_reminder(conn, plan: dict, action: str, sync_state: str) -> None:
    if plan["reminder_backend"] != "macos" or not plan["reminder_external_id"]:
        return
    other_states = TERMINAL_SYNC_STATES - {sync_state}
    if plan["reminder_state"] in other_states:
        raise RecoveryError(
            f"reminder transition is already {plan['reminder_state']}; retry the matching command"
        )
    conn.execute(
        "UPDATE muscle_recovery SET reminder_state=?, updated_at=? WHERE plan_id=?",
        [sync_state, E.utc_now(), plan["plan_id"]],
    )
    conn.commit()
    try:
        # The AppleScript treats a missing reminder as success so rerunning this
        # transition reconciles a crash after the external side effect.
        run_macos_reminder(action, plan["reminder_external_id"])
    except RecoveryError:
        conn.execute(
            "UPDATE muscle_recovery SET reminder_state='failed', updated_at=? WHERE plan_id=?",
            [E.utc_now(), plan["plan_id"]],
        )
        conn.commit()
        raise


def finish_plan(
    conn,
    ref: str,
    status: str,
    soreness_before: int | None = None,
    soreness_after: int | None = None,
    note: str | None = None,
) -> dict:
    if status not in {"completed", "skipped"}:
        raise RecoveryError("finish status must be completed or skipped")
    before = validate_score(soreness_before, "soreness-before")
    after = validate_score(soreness_after, "soreness-after")
    plan = get_plan(conn, ref)
    if plan["plan_status"] != "planned":
        raise RecoveryError(f"plan is already {plan['plan_status']}")
    _sync_terminal_reminder(
        conn, plan,
        "complete" if status == "completed" else "cancel",
        "completing" if status == "completed" else "skipping",
    )
    updated_at = E.utc_now()
    conn.execute(
        """UPDATE muscle_recovery
           SET plan_status=?, soreness_before=COALESCE(?, soreness_before),
               soreness_after=?, checkin_note=?,
               completed_at=CASE WHEN ?='completed' THEN ? ELSE NULL END,
               updated_at=?, reminder_state=CASE
                   WHEN reminder_state IN ('pending', 'scheduled', 'completing', 'skipping', 'canceling')
                   THEN 'canceled' ELSE reminder_state END
           WHERE plan_id=?""",
        [status, before, after, note, status, updated_at, updated_at, plan["plan_id"]],
    )
    conn.commit()
    return get_plan(conn, plan["plan_id"])


def cancel_plan(conn, ref: str) -> dict:
    plan = get_plan(conn, ref)
    if plan["plan_status"] != "planned":
        raise RecoveryError(f"plan is already {plan['plan_status']}")
    _sync_terminal_reminder(conn, plan, "cancel", "canceling")
    conn.execute(
        """UPDATE muscle_recovery
           SET plan_status='canceled', reminder_state='canceled', updated_at=?
           WHERE plan_id=?""",
        [E.utc_now(), plan["plan_id"]],
    )
    conn.commit()
    return get_plan(conn, plan["plan_id"])


def list_plans(conn, since: str = "28d", status: str | None = None, limit: int = 100):
    if status is not None and status not in PLAN_STATUSES:
        raise RecoveryError(f"invalid plan status: {status}")
    if limit < 1:
        raise RecoveryError("limit must be a positive integer")
    match = re.fullmatch(r"(\d+)d", since)
    if not match:
        raise RecoveryError("since must use a day window such as 28d")
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=int(match.group(1)))
    where = ["created_at >= ?"]
    args = [cutoff]
    if status:
        where.append("plan_status = ?")
        args.append(status)
    args.append(limit)
    rows = conn.execute(
        f"""SELECT {', '.join(ROW_COLS)} FROM muscle_recovery
            WHERE {' AND '.join(where)}
            ORDER BY remind_at ASC NULLS LAST, created_at DESC LIMIT ?""",
        args,
    ).fetchall()
    return [row_to_dict(row) for row in rows]


def due_plans(conn, at: str | None = None, mark_fired: bool = False):
    if at:
        cutoff, _timezone_name, _aware = parse_remind_at(at, "UTC")
    else:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = conn.execute(
        f"""SELECT {', '.join(ROW_COLS)} FROM muscle_recovery
            WHERE plan_status='planned' AND remind_at IS NOT NULL AND remind_at <= ?
              AND reminder_state IN ('pending', 'scheduled')
            ORDER BY remind_at ASC""",
        [cutoff],
    ).fetchall()
    plan_ids = [row[0] for row in rows]
    if mark_fired and plan_ids:
        updated_at = E.utc_now()
        conn.executemany(
            "UPDATE muscle_recovery SET reminder_state='fired', updated_at=? WHERE plan_id=?",
            [[updated_at, plan_id] for plan_id in plan_ids],
        )
        conn.commit()
        return [get_plan(conn, plan_id) for plan_id in plan_ids]
    return [row_to_dict(row) for row in rows]


def _add_common(subparser):
    subparser.add_argument("--db", default=E.DEFAULT_DB)
    subparser.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)

    create = subs.add_parser("create", help="create a gentle recovery plan")
    _add_common(create)
    create.add_argument("--workout")
    create.add_argument("--muscles", required=True, help="comma-separated muscle groups")
    create.add_argument("--routine", choices=sorted(ROUTINES), default="post_run_basic")
    create.add_argument("--duration-min", type=int)
    create.add_argument("--remind-at")
    create.add_argument("--timezone")
    create.add_argument("--note")
    create.add_argument("--soreness-before", type=int)
    create.add_argument("--reminder-backend", choices=("none", "macos"), default="none")
    create.add_argument("--macos-list")

    for command in ("complete", "skip"):
        sub = subs.add_parser(command, help=f"mark a plan {command}d")
        _add_common(sub)
        sub.add_argument("--plan", required=True)
        sub.add_argument("--soreness-before", type=int)
        sub.add_argument("--soreness-after", type=int)
        sub.add_argument("--note")

    cancel = subs.add_parser("cancel", help="cancel a planned recovery routine")
    _add_common(cancel)
    cancel.add_argument("--plan", required=True)

    listing = subs.add_parser("list", help="list recovery plans")
    _add_common(listing)
    listing.add_argument("--since", default="28d")
    listing.add_argument("--status", choices=sorted(PLAN_STATUSES))
    listing.add_argument("--limit", type=int, default=100)

    due = subs.add_parser("due", help="list recovery reminders due by a time")
    _add_common(due)
    due.add_argument("--at", help="ISO-8601 cutoff; defaults to now")
    due.add_argument("--mark-fired", action="store_true")
    return parser


def _print_result(result, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if isinstance(result, list):
        if not result:
            print("No recovery plans found.")
            return
        for plan in result:
            print(
                f"{plan['plan_id']}  {plan['plan_status']}  "
                f"{','.join(plan['muscle_groups'])}  {plan.get('remind_at_local') or 'no reminder'}"
            )
        return
    print(f"{result['plan_id']}  {result['plan_status']}  reminder={result['reminder_state']}")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = E.load_config(os.path.dirname(args.db))
        conn = E.connect(args.db, cfg)
        try:
            if args.command == "create":
                list_name = args.macos_list or cfg.get("recovery", {}).get(
                    "macos_reminders_list", "Reminders"
                )
                result = create_plan(
                    conn, args.muscles, args.routine, args.duration_min,
                    args.workout, args.remind_at, args.timezone, args.note,
                    args.soreness_before, args.reminder_backend, list_name,
                )
            elif args.command in {"complete", "skip"}:
                result = finish_plan(
                    conn, args.plan,
                    "completed" if args.command == "complete" else "skipped",
                    args.soreness_before, args.soreness_after, args.note,
                )
            elif args.command == "cancel":
                result = cancel_plan(conn, args.plan)
            elif args.command == "list":
                result = list_plans(conn, args.since, args.status, args.limit)
            else:
                result = due_plans(conn, args.at, args.mark_fired)
        finally:
            conn.close()
    except RecoveryError as exc:
        print(f"[eha] recovery: {exc}", file=sys.stderr)
        return 2
    _print_result(result, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
