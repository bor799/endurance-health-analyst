"""Shared library for endurance-health-analyst.

Local-first endurance analytics: unified workout schema over DuckDB.
All timestamps are stored as UTC TIMESTAMPs in the database; formatting
helpers convert back to local time for human output.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(SKILL_ROOT, "data")
OUTPUT_DIR = os.path.join(SKILL_ROOT, "output")
DEFAULT_DB = os.path.join(DATA_DIR, "health.duckdb")

try:
    import duckdb  # noqa: F401
except ImportError:  # pragma: no cover
    print("[eha] missing dependency 'duckdb'. Run: ./setup.sh", file=sys.stderr)
    raise

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "athlete": {"name": None, "age": None, "max_hr": None, "lthr": None, "lab_threshold_hr": None},
    "source_priority": ["COROS", "Apple Watch", "Garmin", "Strava", "iPhone", "Other App"],
    "privacy": {"default_on": True, "trim_radius_m": 300, "start_trim_m": 300, "end_trim_m": 300},
    "similar_filters": {
        "pace_tolerance_pct": 12, "distance_tolerance_pct": 25,
        "temp_tolerance_c": 4.0, "humidity_tolerance_pct": 15,
        "min_similar": 3, "max_lookback_days": 365,
    },
    "steady_state": {
        "min_speed_mps": 1.8, "grade_max_pct": 3.0, "pace_band_pct": 15,
        "warmup_min": 5, "cooldown_min": 3, "min_steady_minutes": 10,
    },
    "weather": {"archive_delay_days": 5},
    "recovery": {"macos_reminders_list": "Reminders"},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(db_dir: str | None = None) -> dict:
    """Load config.json (created from config.example.json on first run)."""
    d = db_dir or DATA_DIR
    cfg_path = os.path.join(d, "config.json")
    if not os.path.exists(cfg_path):
        example = os.path.join(d, "config.example.json")
        if os.path.exists(example):
            with open(example, encoding="utf-8") as f:
                return json.load(f)
        return DEFAULT_CONFIG
    with open(cfg_path, encoding="utf-8") as f:
        user_cfg = json.load(f)
    return _deep_merge(DEFAULT_CONFIG, user_cfg)


def save_config(cfg: dict, db_dir: str | None = None) -> None:
    d = db_dir or DATA_DIR
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Database schema
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS workouts (
    workout_id        TEXT PRIMARY KEY,
    sport_type        TEXT,
    source            TEXT,
    device            TEXT,
    start_time        TIMESTAMP,
    end_time          TIMESTAMP,
    duration_s        DOUBLE,
    moving_time_s     DOUBLE,
    distance_m        DOUBLE,
    avg_pace_s_per_km DOUBLE,
    best_pace_s_per_km DOUBLE,
    elevation_gain_m  DOUBLE,
    avg_cadence       DOUBLE,
    avg_power         DOUBLE,
    active_energy_kcal DOUBLE,
    avg_hr            DOUBLE,
    max_hr            DOUBLE,
    hr_drift_pct      DOUBLE,
    hr_recovery_60s   DOUBLE,
    hr_recovery_120s  DOUBLE,
    has_route         BOOLEAN,
    raw               JSON
);
CREATE TABLE IF NOT EXISTS route_points (
    workout_id TEXT, seq INTEGER, ts TIMESTAMP,
    lat DOUBLE, lon DOUBLE, alt DOUBLE
);
CREATE TABLE IF NOT EXISTS series (
    workout_id TEXT, ts TIMESTAMP,
    lat DOUBLE, lon DOUBLE, alt DOUBLE,
    speed_mps DOUBLE, pace_s_per_km DOUBLE, hr DOUBLE,
    cadence DOUBLE, dist_m DOUBLE, source TEXT
);
CREATE TABLE IF NOT EXISTS hr_samples (
    ts TIMESTAMP, hr DOUBLE, source TEXT
);
CREATE TABLE IF NOT EXISTS metric_samples (
    ts TIMESTAMP, metric TEXT, value DOUBLE, unit TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS weather (
    workout_id TEXT PRIMARY KEY,
    temperature_c DOUBLE, humidity_pct DOUBLE, apparent_c DOUBLE,
    dew_point_c DOUBLE, wind_kmh DOUBLE, precip_mm DOUBLE,
    raw JSON, fetched_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS subjective (
    workout_id TEXT PRIMARY KEY,
    rpe INTEGER, breathing TEXT, legs TEXT, pain TEXT,
    feeling TEXT, created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS analyses (
    workout_id TEXT PRIMARY KEY,
    generated_at TIMESTAMP, analysis JSON
);
CREATE TABLE IF NOT EXISTS muscle_recovery (
    plan_id              TEXT PRIMARY KEY,
    workout_id           TEXT,
    muscle_groups        JSON,
    routine              JSON,
    duration_min         INTEGER,
    remind_at            TIMESTAMP,
    timezone             TEXT,
    reminder_backend     TEXT,
    reminder_external_id TEXT,
    reminder_state       TEXT,
    plan_status          TEXT,
    soreness_before      INTEGER,
    soreness_after       INTEGER,
    note                 TEXT,
    checkin_note         TEXT,
    created_at           TIMESTAMP,
    updated_at           TIMESTAMP,
    completed_at         TIMESTAMP
);
CREATE TABLE IF NOT EXISTS ingest_log (
    ts TIMESTAMP, kind TEXT, detail JSON
);
CREATE INDEX IF NOT EXISTS idx_series_w_ts ON series(workout_id, ts);
CREATE INDEX IF NOT EXISTS idx_route_w ON route_points(workout_id);
CREATE INDEX IF NOT EXISTS idx_hr_ts ON hr_samples(ts);
CREATE INDEX IF NOT EXISTS idx_metric_ts ON metric_samples(metric, ts);
"""

# Source-priority dedup view: one HR sample per minute bucket, highest-priority source first.
DEDUP_HR_VIEW = """
CREATE OR REPLACE VIEW preferred_hr AS
WITH ranked AS (
    SELECT ts, hr, source,
           date_trunc('minute', ts) AS bucket,
           row_number() OVER (
               PARTITION BY date_trunc('minute', ts)
               ORDER BY source_rank(source) ASC, ts DESC
           ) AS rn
    FROM hr_samples
)
SELECT ts, hr, source FROM ranked WHERE rn = 1;
"""


def source_rank_expr(cfg: dict) -> str:
    """SQL CASE expression ranking sources by configured priority (lower = better)."""
    order = cfg.get("source_priority") or DEFAULT_CONFIG["source_priority"]
    parts = []
    for i, name in enumerate(order):
        safe = name.replace("'", "''")
        parts.append(f"WHEN source LIKE '%{safe}%' THEN {i}")
    return f"CASE {' '.join(parts)} ELSE {len(order)} END"


def connect(db_path: str | None = None, cfg: dict | None = None):
    cfg = cfg or load_config(os.path.dirname(db_path) if db_path else None)
    path = db_path or DEFAULT_DB
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = duckdb.connect(path)
    conn.execute(DDL)
    conn.execute(
        "CREATE OR REPLACE MACRO source_rank(src) AS ("
        + source_rank_expr(cfg).replace("source", "src")
        + ")"
    )
    conn.execute(DEDUP_HR_VIEW)
    return conn


# ---------------------------------------------------------------------------
# Identity / sport mapping
# ---------------------------------------------------------------------------

def make_workout_id(source: str, start: datetime, duration_s: float, distance_m: float | None) -> str:
    key = f"{source}|{start.astimezone(timezone.utc).isoformat()}|{round(duration_s or 0)}|{round(distance_m or 0)}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


SPORT_MAP = {
    "running": "run", "walking": "walk", "hiking": "hike",
    "cycling": "ride", "swimming": "swim", "elliptical": "elliptical",
    "rowing": "row", "stationarybike": "ride", "functionalsrengthtraining": "strength",
}


_DEVICE_NAME_RE = re.compile(r"name:([^,>]+)")


def safe_extract_device(device_attr: str | None) -> str | None:
    """'<<HKDevice: name:Apple Watch, manufacturer:Apple, model:Watch9,4>>' -> 'Apple Watch'"""
    if not device_attr:
        return None
    m = _DEVICE_NAME_RE.search(device_attr)
    return m.group(1).strip() if m else device_attr[:40]


def map_apple_sport(activity_type: str) -> str:
    """HKWorkoutActivityTypeRunning -> run"""
    raw = (activity_type or "").replace("HKWorkoutActivityType", "").lower()
    for k, v in SPORT_MAP.items():
        if raw.startswith(k):
            return v
    return raw or "other"


def map_fit_sport(gsport: str, sport: int) -> str:
    s = (gsport or "").lower()
    if "run" in s:
        return "run"
    if "bike" in s or "cycl" in s:
        return "ride"
    if "swim" in s:
        return "swim"
    return {"1": "run", "2": "ride", "5": "swim"}.get(str(sport), "other")


# ---------------------------------------------------------------------------
# Time parsing (Apple export style: "2026-08-21 18:30:00 +0800")
# ---------------------------------------------------------------------------

_APPLE_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?\s*([+-]\d{4})?"
)


def parse_apple_date(s: str) -> datetime:
    m = _APPLE_RE.match(s.strip())
    if not m:
        raise ValueError(f"unparseable date: {s!r}")
    y, mo, d, h, mi, sec = (int(m.group(i)) for i in range(1, 7))
    tz = m.group(8)
    naive = datetime(y, mo, d, h, mi, sec)
    if tz:
        sign = 1 if tz[0] == "+" else -1
        off = timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])) * sign
        return naive.replace(tzinfo=timezone(off))
    return naive.replace(tzinfo=timezone.utc)  # GPX 'Z' handled separately


def parse_gpx_time(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        return datetime.fromisoformat(s[:-1]).replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def utc_now() -> datetime:
    """Return a UTC instant for storage in the existing naive TIMESTAMP schema."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Physics / formatting helpers
# ---------------------------------------------------------------------------

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fmt_pace(pace_s_per_km: float | None) -> str:
    if not pace_s_per_km or not math.isfinite(pace_s_per_km) or pace_s_per_km <= 0:
        return "–"
    total = int(round(pace_s_per_km))
    return f"{total // 60}'{total % 60:02d}\"/km"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    s = int(round(seconds))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def fmt_km(meters: float | None, digits: int = 2) -> str:
    return "–" if meters is None else f"{meters / 1000:.{digits}f}"


def safe_float(v, default=None):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Workout lookup
# ---------------------------------------------------------------------------

def resolve_workout(conn, ref: str, sport: str | None = None):
    """ref: workout_id (or prefix) | 'latest' | 'latest:run' | 'YYYY-MM-DD'."""
    ref = ref or "latest"
    sport_filter = sport
    date_filter = None
    if ref.startswith("latest:"):
        sport_filter = ref.split(":", 1)[1]
    elif re.match(r"^\d{4}-\d{2}-\d{2}", ref):
        date_filter = ref[:10]
    elif ref != "latest":
        row = conn.execute(
            "SELECT workout_id FROM workouts WHERE workout_id LIKE ?", [ref + "%"]
        ).fetchone()
        if row:
            return row[0]

    where, args = "1=1", []
    if sport_filter:
        where += " AND sport_type = ?"
        args.append(sport_filter)
    if date_filter:
        where += " AND CAST(start_time AS VARCHAR) LIKE ?"
        args.append(date_filter + "%")
    row = conn.execute(
        f"SELECT workout_id FROM workouts WHERE {where} ORDER BY start_time DESC LIMIT 1", args
    ).fetchone()
    return row[0] if row else None


def get_workout_row(conn, workout_id: str):
    return conn.execute(
        "SELECT workout_id, sport_type, source, device, start_time, end_time, duration_s,"
        " moving_time_s, distance_m, avg_pace_s_per_km, best_pace_s_per_km, elevation_gain_m,"
        " avg_cadence, avg_power, avg_hr, max_hr, hr_drift_pct, hr_recovery_60s, hr_recovery_120s,"
        " has_route FROM workouts WHERE workout_id = ?", [workout_id]
    ).fetchone()


WORKOUT_COLS = ["workout_id", "sport_type", "source", "device", "start_time", "end_time",
                "duration_s", "moving_time_s", "distance_m", "avg_pace_s_per_km",
                "best_pace_s_per_km", "elevation_gain_m", "avg_cadence", "avg_power",
                "avg_hr", "max_hr", "hr_drift_pct", "hr_recovery_60s", "hr_recovery_120s",
                "has_route"]


def workout_row_to_dict(row) -> dict:
    d = dict(zip(WORKOUT_COLS, row))
    for k in ("start_time", "end_time"):
        if d.get(k) is not None:
            d[k] = d[k].isoformat()
    for k in ("has_route",):
        if d.get(k) is not None:
            d[k] = bool(d[k])
    return d


def get_weather(conn, workout_id: str):
    row = conn.execute(
        "SELECT temperature_c, humidity_pct, apparent_c, dew_point_c, wind_kmh, precip_mm, raw"
        " FROM weather WHERE workout_id = ?", [workout_id]
    ).fetchone()
    if not row:
        return None
    keys = ["temperature_c", "humidity_pct", "apparent_c", "dew_point_c", "wind_kmh", "precip_mm"]
    out = {k: (float(v) if v is not None else None) for k, v in zip(keys, row[:6])}
    raw = row[6] if isinstance(row[6], dict) else (json.loads(row[6]) if row[6] else {})
    out["position_source"] = raw.get("position_source", "unknown")
    return out


def log_ingest(conn, kind: str, detail: dict):
    conn.execute("INSERT INTO ingest_log VALUES (now(), ?, ?)",
                 [kind, json.dumps(detail, ensure_ascii=False, default=str)])


def ensure_output_dir(subdir: str | None = None) -> str:
    d = os.path.join(OUTPUT_DIR, subdir) if subdir else OUTPUT_DIR
    os.makedirs(d, exist_ok=True)
    return d
