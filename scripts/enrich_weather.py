#!/usr/bin/env python3
"""Enrich workouts with weather from Open-Meteo (no API key, local-first).

For each workout: take the median route coordinate (robust to GPS noise),
query hourly temperature / humidity / apparent temperature / dew point /
wind / precipitation around the workout window, and store it in DuckDB.

Archive API covers history up to ~5 days ago; newer workouts fall back to
the forecast API with past_days. All requests are read-only GETs.

Usage:
  python enrich_weather.py --workout latest:run [--db ...]
  python enrich_weather.py --all-missing [--db ...]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eha_lib as E  # noqa: E402

HOURLY = ["temperature_2m", "relative_humidity_2m", "apparent_temperature",
          "dew_point_2m", "wind_speed_10m", "precipitation"]
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
UA = {"User-Agent": "endurance-health-analyst/0.1 (local personal analytics)"}


def http_get_json(url: str, params: dict, timeout: float = 20.0) -> dict:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{qs}", headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def workout_position(conn, workout_id: str):
    row = conn.execute(
        """SELECT median(lat), median(lon) FROM route_points
           WHERE workout_id = ? AND lat IS NOT NULL AND lon IS NOT NULL""", [workout_id]
    ).fetchone()
    if row and row[0] is not None:
        return float(row[0]), float(row[1])
    # Fallback for summary-only workouts (COROS/Keep sync without GPX): the
    # athlete trains in the same city — borrow the nearest geo-located workout.
    start = conn.execute("SELECT start_time FROM workouts WHERE workout_id = ?",
                         [workout_id]).fetchone()
    if not start:
        return None
    row = conn.execute(
        """SELECT median(rp.lat), median(rp.lon)
           FROM route_points rp JOIN workouts w ON w.workout_id = rp.workout_id
           WHERE w.start_time BETWEEN ? - INTERVAL 60 DAY AND ? + INTERVAL 60 DAY
           GROUP BY w.workout_id, w.start_time
           ORDER BY abs(date_diff('minute', w.start_time, ?)) LIMIT 1""",
        [start[0], start[0], start[0]]).fetchone()
    if row and row[0] is not None:
        return float(row[0]), float(row[1])
    return None


def fetch_hourly(lat: float, lon: float, start_utc: datetime, end_utc: datetime,
                 archive_delay_days: int) -> tuple[dict | None, str]:
    """Return (hourly dict with 'time' + per-var lists, source_api)."""
    # pad the request window so the workout start/end hours are covered
    d0 = (start_utc - timedelta(days=1)).date().isoformat()
    d1 = (end_utc + timedelta(days=1)).date().isoformat()
    params = {"latitude": round(lat, 4), "longitude": round(lon, 4),
              "start_date": d0, "end_date": d1, "hourly": ",".join(HOURLY),
              "timezone": "auto", "timeformat": "iso8601"}
    now = datetime.now(timezone.utc)
    stale = (now - start_utc).days <= archive_delay_days + 2
    errors = []
    if not stale:
        try:
            data = http_get_json(ARCHIVE_URL, params)
            return (data or {}).get("hourly"), "archive"
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as e:
            errors.append(f"archive: {e}")
    try:
        params.pop("start_date"), params.pop("end_date")
        params["past_days"] = min(92, max(2, (now - start_utc).days + 2))
        params["forecast_days"] = 1
        data = http_get_json(FORECAST_URL, params)
        return (data or {}).get("hourly"), "forecast"
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as e:
        errors.append(f"forecast: {e}")
    print("[weather] ERROR unable to fetch: " + "; ".join(errors), file=sys.stderr)
    return None, "none"


def summarize_window(hourly: dict, api: str, start_utc: datetime, end_utc: datetime) -> dict | None:
    times = hourly.get("time") or []
    if not times:
        return None
    # Open-Meteo with timezone=auto returns local ISO times without offset;
    # utc_offset_seconds lets us map back to UTC.
    off = timedelta(seconds=float(hourly.get("utc_offset_seconds") or 0))
    utc_stamps = [datetime.fromisoformat(t).replace(tzinfo=timezone(off)).astimezone(timezone.utc)
                  for t in times]
    lo, hi = start_utc - timedelta(minutes=45), end_utc + timedelta(minutes=45)
    idx = [i for i, t in enumerate(utc_stamps) if lo <= t <= hi]
    if not idx:  # nearest hour fallback
        nearest = min(range(len(utc_stamps)),
                      key=lambda i: abs((utc_stamps[i] - start_utc).total_seconds()))
        idx = [nearest]

    def agg(var, reducer):
        vals = [hourly.get(var, [None] * len(times))[i] for i in idx]
        vals = [v for v in vals if v is not None]
        return round(reducer(vals), 1) if vals else None

    out = {
        "temperature_c": agg("temperature_2m", statistics.mean),
        "humidity_pct": agg("relative_humidity_2m", statistics.mean),
        "apparent_c": agg("apparent_temperature", statistics.mean),
        "dew_point_c": agg("dew_point_2m", statistics.mean),
        "wind_kmh": agg("wind_speed_10m", statistics.mean),
        "precip_mm": agg("precipitation", lambda v: sum(v) / max(1, len(v))),
    }
    out["api"] = api
    out["hours_averaged"] = len(idx)
    return out


def enrich_one(conn, workout_id: str, archive_delay_days: int, force=False) -> dict | None:
    row = conn.execute(
        "SELECT start_time, end_time FROM workouts WHERE workout_id = ?", [workout_id]
    ).fetchone()
    if not row:
        print(f"[weather] no workout {workout_id}", file=sys.stderr)
        return None
    start_utc = row[0].replace(tzinfo=timezone.utc)
    end_utc = (row[1] or row[0]).replace(tzinfo=timezone.utc)
    if not force and conn.execute(
            "SELECT 1 FROM weather WHERE workout_id = ?", [workout_id]).fetchone():
        return {"cached": True}

    pos = workout_position(conn, workout_id)
    if not pos:
        print(f"[weather] no GPS route for {workout_id} — cannot locate; skipping", file=sys.stderr)
        return None
    lat, lon = pos
    hourly, api = fetch_hourly(lat, lon, start_utc, end_utc, archive_delay_days)
    if not hourly or api == "none":
        return None
    s = summarize_window(hourly, api, start_utc, end_utc)
    if not s:
        return None
    conn.execute(
        "INSERT OR REPLACE INTO weather VALUES (?,?,?,?,?,?,?,?,now())",
        [workout_id, s["temperature_c"], s["humidity_pct"], s["apparent_c"],
         s["dew_point_c"], s["wind_kmh"], s["precip_mm"],
         json.dumps({"lat": lat, "lon": lon, "api": api, "hours": s["hours_averaged"]})])
    conn.commit()
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workout", default="latest:run", help="workout_id / latest / latest:run")
    ap.add_argument("--all-missing", action="store_true", help="enrich every workout lacking weather")
    ap.add_argument("--force", action="store_true", help="re-fetch even if cached")
    ap.add_argument("--db", default=E.DEFAULT_DB)
    args = ap.parse_args()

    cfg = E.load_config()
    conn = E.connect(args.db)

    if args.all_missing:
        wids = [r[0] for r in conn.execute(
            """SELECT w.workout_id FROM workouts w
               LEFT JOIN weather x ON x.workout_id = w.workout_id
               WHERE x.workout_id IS NULL
                 AND EXISTS (SELECT 1 FROM route_points r WHERE r.workout_id = w.workout_id)
               ORDER BY w.start_time""").fetchall()]
        ok = miss = 0
        for wid in wids:
            r = enrich_one(conn, wid, cfg["weather"]["archive_delay_days"], force=args.force)
            if r and not r.get("cached"):
                ok += 1
            elif r:
                ok += 1
            else:
                miss += 1
        print(f"[weather] enriched {ok}/{len(wids)} (failed {miss})")
        return 0 if miss == 0 else 0

    wid = E.resolve_workout(conn, args.workout)
    if not wid:
        print("[weather] workout not found", file=sys.stderr)
        return 1
    s = enrich_one(conn, wid, cfg["weather"]["archive_delay_days"], force=args.force)
    if s and s.get("cached"):
        print(f"[weather] {wid}: cached")
        s = E.get_weather(conn, wid)
    if s:
        print(f"[weather] {wid}: {s['temperature_c']}°C  RH {s['humidity_pct']}%  "
              f"feels {s['apparent_c']}°C  dew {s['dew_point_c']}°C  wind {s['wind_kmh']} km/h")
    return 0 if s else 2


if __name__ == "__main__":
    sys.exit(main())
