#!/usr/bin/env python3
"""Ingest an Apple Health export.zip (or export.xml) into the local DuckDB store.

Handles:
- export.xml (any localized filename, root <HealthData>) via streaming iterparse
- workout-routes/*.gpx route files -> route_points + unified series
- HR / RHR / HRV / VO2max / respiratory rate / sleep / active energy records
- multi-source deduplication (Apple Watch vs iPhone vs COROS vs third-party)
- idempotent re-ingest

Usage:
  python ingest_apple_health.py ~/Downloads/export.zip [--db data/health.duckdb] [--from 2025-01-01]
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import sys
import tempfile
import zipfile
from datetime import timedelta
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eha_lib as E  # noqa: E402

BATCH = 5000

METRIC_TYPES = {
    "HKQuantityTypeIdentifierRestingHeartRate": ("resting_hr", "bpm"),
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": ("hrv_sdnn", "ms"),
    "HKQuantityTypeIdentifierVO2Max": ("vo2max", "ml/kg/min"),
    "HKQuantityTypeIdentifierRespiratoryRate": ("respiratory_rate", "count/min"),
    "HKQuantityTypeIdentifierActiveEnergyBurned": ("active_energy", "kcal"),
    "HKQuantityTypeIdentifierAppleExerciseTime": ("exercise_time", "min"),
    "HKQuantityTypeIdentifierRunningSpeed": ("running_speed", "km/h"),
    "HKQuantityTypeIdentifierRunningCadence": ("running_cadence", "count/min"),
    "HKQuantityTypeIdentifierWalkingSpeed": ("walking_speed", "km/h"),
    "HKCategoryTypeIdentifierSleepAnalysis": ("sleep_phase", "min"),
}
SLEEP_ASLEEP_PREFIXES = ("HKCategoryValueSleepAnalysisAsleep",)
HR_TYPE = "HKQuantityTypeIdentifierHeartRate"

STAT_KEYS = {
    "HKQuantityTypeIdentifierHeartRate": "hr",
    "HKQuantityTypeIdentifierRunningSpeed": "run_speed",
    "HKQuantityTypeIdentifierWalkingSpeed": "walk_speed",
    "HKQuantityTypeIdentifierElevationAscended": "elev",
    "HKQuantityTypeIdentifierActiveEnergyBurned": "energy",
    "HKQuantityTypeIdentifierRunningCadence": "cadence",
    "HKQuantityTypeIdentifierRunningPower": "power",
    "HKQuantityTypeIdentifierCyclingPower": "power",
    # HealthKit ≥14 exports carry distance/energy in WorkoutStatistics sums,
    # not in the legacy totalDistance/totalEnergyBurned attributes.
    "HKQuantityTypeIdentifierDistanceWalkingRunning": "distance",
    "HKQuantityTypeIdentifierDistanceCycling": "distance",
    "HKQuantityTypeIdentifierSwimmingStrokeCount": "strokes",
}


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# ---------------------------------------------------------------------------
# Locate + open the export
# ---------------------------------------------------------------------------

def find_main_xml(extract_dir: str) -> str:
    """The main export file's name can be localized (导出.xml) or mojibake."""
    cands = []
    for root_dir, _dirs, files in os.walk(extract_dir):
        depth = root_dir[len(extract_dir):].count(os.sep)
        if depth > 1:
            continue
        for fn in files:
            if fn.lower().endswith((".xml", ".xmlexport")):
                cands.append(os.path.join(root_dir, fn))
    for p in sorted(cands, key=lambda x: (x.count(os.sep), len(os.path.basename(x)))):
        try:
            # HealthKit Export v14+ carries a long DOCTYPE prolog, so scan
            # well past the first 2KB instead of trusting a fixed head window.
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                buf, scanned = "", 0
                while scanned < 1 << 20:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    scanned += len(chunk)
                    if "<HealthData" in buf or "<ClinicalDocument" in buf:
                        break
            if "<HealthData" in buf:
                return p
        except OSError:
            continue
    raise SystemExit(f"[ingest] no HealthData xml found under {extract_dir}")


def selective_unzip(zip_path: str, dest: str) -> str:
    """Extract the main xml + workout-routes/*.gpx only (exports can be GBs)."""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        wanted = [
            n for n in names
            if n.lower().endswith(".xml")
            or ("/workout-routes/" in n or n.startswith("workout-routes/"))
            or n.lower().endswith(".gpx")
        ]
        for n in wanted:
            zf.extract(n, dest)
    return dest


# ---------------------------------------------------------------------------
# GPX route parsing
# ---------------------------------------------------------------------------

def parse_route_gpx(path: str) -> list[dict]:
    """-> [{ts, lat, lon, alt, speed_mps, dist_m}] with per-point GPS speed."""
    pts = []
    try:
        for _ev, el in ET.iterparse(path, events=("end",)):
            if local(el.tag) != "trkpt":
                continue
            lat, lon = float(el.get("lat")), float(el.get("lon"))
            alt, tstr = None, None
            for ch in el:
                lt = local(ch.tag)
                if lt == "ele":
                    alt = E.safe_float(ch.text)
                elif lt == "time":
                    tstr = ch.text
            if tstr is None:
                el.clear()
                continue
            try:
                ts = E.parse_gpx_time(tstr)
            except ValueError:
                el.clear()
                continue
            pts.append({"ts_utc": E.to_utc(ts), "lat": lat, "lon": lon, "alt": alt})
            el.clear()
    except ET.ParseError as e:
        print(f"[ingest] WARN gpx parse error {path}: {e}")
        return []
    pts.sort(key=lambda p: p["ts_utc"])
    cum = 0.0
    for i, p in enumerate(pts):
        if i == 0:
            p["speed_mps"], p["dist_m"] = 0.0, 0.0
            continue
        q = pts[i - 1]
        dt = (p["ts_utc"] - q["ts_utc"]).total_seconds()
        d = E.haversine_m(q["lat"], q["lon"], p["lat"], p["lon"])
        # GPS noise guard: cap instantaneous speed at 8 m/s; treat >30 m gaps as stops
        if dt <= 0:
            p["speed_mps"], p["dist_m"] = q["speed_mps"], cum
            continue
        v = d / dt
        if v > 8.0:
            v = 0.0 if d > 100 else v * 0.3  # jump: mostly noise, shrink contribution
            d = v * dt
        cum += d
        p["speed_mps"], p["dist_m"] = v, cum
    return pts


# ---------------------------------------------------------------------------
# Main XML pass
# ---------------------------------------------------------------------------

def iter_export(xml_path: str, from_utc=None):
    """Yield ('workout', dict) and ('record', dict) elements in one streaming pass."""
    ctx = ET.iterparse(xml_path, events=("start", "end"))
    _, root = next(ctx)  # HealthData
    for ev, el in ctx:
        tag = local(el.tag)
        if ev != "end" or tag not in ("Workout", "Record"):
            continue
        try:
            if tag == "Workout":
                yield ("workout", parse_workout(el))
            else:
                rtype = el.get("type", "")
                if rtype == HR_TYPE:
                    yield ("hr", el)
                elif rtype in METRIC_TYPES:
                    yield ("record", (rtype, el))
        finally:
            el.clear()
            if len(root) > 0:
                root.clear()  # keep memory flat on multi-GB exports


def parse_workout(el) -> dict:
    dur = E.safe_float(el.get("duration"))
    if dur is not None and (el.get("durationUnit") or "").lower() in ("min", "minutes"):
        dur *= 60.0  # HealthKit ≥14 exports duration in minutes
    w = {
        "activity": el.get("workoutActivityType", ""),
        "sport": E.map_apple_sport(el.get("workoutActivityType", "")),
        "source": el.get("sourceName", "unknown"),
        "device": el.get("device", "") or "",
        "duration_s": dur,
        "distance_km": E.safe_float(el.get("totalDistance")),
        "energy_kcal": E.safe_float(el.get("totalEnergyBurned")),
        "stats": {},
        "route_file": None,
        "elev_gain_m": None,
    }
    try:
        w["start_utc"] = E.to_utc(E.parse_apple_date(el.get("startDate")))
    except ValueError:
        w["start_utc"] = None
    end = el.get("endDate")
    try:
        w["end_utc"] = E.to_utc(E.parse_apple_date(end)) if end else None
    except ValueError:
        w["end_utc"] = None
    for ch in el:
        ct = local(ch.tag)
        if ct == "WorkoutStatistics":
            t = ch.get("type", "")
            if t in STAT_KEYS:
                w["stats"][STAT_KEYS[t]] = {
                    "average": E.safe_float(ch.get("average")),
                    "maximum": E.safe_float(ch.get("maximum")),
                    "sum": E.safe_float(ch.get("sum")),
                    "unit": ch.get("unit", ""),
                }
        elif ct == "WorkoutRoute":
            # HealthKit ≥14: <WorkoutRoute><FileReference path="/workout-routes/….gpx"/>
            for sub in ch:
                if local(sub.tag) == "FileReference":
                    w["route_file"] = os.path.basename(sub.get("path", ""))
        elif ct == "MetadataEntry":
            key = ch.get("key", "")
            if key == "com.apple.health.workout-route":
                w["route_file"] = os.path.basename(ch.get("value", ""))
            elif key == "HKElevationAscended":
                val, unit_val = ch.get("value", ""), ch.get("unit", "") or ""
                if not unit_val and " cm" in val:
                    val, unit_val = val[:-3], "cm"
                v = E.safe_float(val)
                if v is not None:
                    w["elev_gain_m"] = v / 100.0 if "cm" in unit_val else v
    return w


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def dedup_workouts(workouts: list[dict], cfg: dict) -> tuple[list[dict], list[dict]]:
    """Same session = same sport + start within 10 min. Winner = source priority, then duration."""
    order = cfg.get("source_priority") or E.DEFAULT_CONFIG["source_priority"]

    def rank(w):
        src = w["source"]
        for i, name in enumerate(order):
            if name.lower() in src.lower():
                return i
        return len(order)

    by_sport: dict[str, list[dict]] = {}
    for w in workouts:
        if w["start_utc"] is not None:
            by_sport.setdefault(w["sport"], []).append(w)
    kept, dropped = [], []
    for _sport, ws in by_sport.items():
        ws.sort(key=lambda w: w["start_utc"])
        groups: list[list[dict]] = []
        for w in ws:
            placed = False
            for g in groups[-3:]:  # only compare with recent groups (sorted)
                if abs((w["start_utc"] - g[0]["start_utc"]).total_seconds()) <= 600:
                    g.append(w)
                    placed = True
                    break
            if not placed:
                groups.append([w])
        for g in groups:
            g.sort(key=lambda w: (rank(w), -(w["duration_s"] or 0)))
            kept.append(g[0])
            dropped.extend(g[1:])
    return kept, dropped


# ---------------------------------------------------------------------------
# Ingest driver
# ---------------------------------------------------------------------------

def workout_derived_columns(w: dict) -> dict:
    st = w["stats"]
    dist_m = w["distance_km"] * 1000 if w.get("distance_km") else None
    if dist_m is None and st.get("distance", {}).get("sum"):
        dist_m = st["distance"]["sum"] * 1000.0  # km → m
    dur = w["duration_s"]
    avg_pace = best_pace = None
    spd = st.get("run_speed") or st.get("walk_speed")
    if spd:
        if spd.get("average"):
            avg_pace = 3600.0 / spd["average"]
        if spd.get("maximum"):
            best_pace = 3600.0 / spd["maximum"]
    if avg_pace is None and dist_m and dur:
        avg_pace = dur / (dist_m / 1000.0)
    elev = st.get("elev", {}).get("sum") if st.get("elev") else None
    if elev is None:
        elev = w.get("elev_gain_m")
    cad = st.get("cadence", {}).get("average") if st.get("cadence") else None
    pwr = st.get("power", {}).get("average") if st.get("power") else None
    hr = st.get("hr", {})
    return {
        "distance_m": dist_m, "avg_pace_s_per_km": avg_pace, "best_pace_s_per_km": best_pace,
        "elevation_gain_m": elev, "avg_cadence": cad, "avg_power": pwr,
        "avg_hr": hr.get("average"), "max_hr": hr.get("maximum"),
        "active_energy_kcal": w.get("energy_kcal") or (st.get("energy", {}) or {}).get("sum"),
    }


def run_ingest(xml_path: str, route_dir: str, conn, from_utc=None) -> dict:
    cfg = E.load_config()
    workouts = []
    hr_buf, metric_buf = [], []
    stats = {"hr": 0, "metrics": 0, "sleep_min": 0}

    def flush():
        if hr_buf:
            conn.executemany("INSERT INTO hr_samples VALUES (?, ?, ?)", hr_buf)
            stats["hr"] += len(hr_buf)
            hr_buf.clear()
        if metric_buf:
            conn.executemany("INSERT INTO metric_samples VALUES (?, ?, ?, ?, ?)", metric_buf)
            stats["metrics"] += len(metric_buf)
            metric_buf.clear()

    for kind, payload in iter_export(xml_path):
        if kind == "workout":
            w = payload
            if w["start_utc"] is None:
                continue
            if from_utc and w["start_utc"] < from_utc:
                continue
            workouts.append(w)
        elif kind == "hr":
            el = payload
            try:
                ts = E.to_utc(E.parse_apple_date(el.get("startDate")))
            except ValueError:
                continue
            v = E.safe_float(el.get("value"))
            if v and (from_utc is None or ts >= from_utc):
                hr_buf.append((ts, v, el.get("sourceName", "unknown")))
        else:
            rtype, el = payload
            metric, unit = METRIC_TYPES[rtype]
            if rtype == "HKCategoryTypeIdentifierSleepAnalysis":
                val = el.get("value", "")
                if not any(val.startswith(p) for p in SLEEP_ASLEEP_PREFIXES):
                    continue
                try:
                    s = E.to_utc(E.parse_apple_date(el.get("startDate")))
                    e = E.to_utc(E.parse_apple_date(el.get("endDate")))
                except ValueError:
                    continue
                mins = (e - s).total_seconds() / 60.0
                if 0 < mins < 24 * 60 and (from_utc is None or s >= from_utc):
                    metric_buf.append((s, "sleep_asleep", mins, "min", el.get("sourceName", "unknown")))
                    stats["sleep_min"] += mins
                continue
            try:
                ts = E.to_utc(E.parse_apple_date(el.get("startDate")))
            except ValueError:
                continue
            v = E.safe_float(el.get("value"))
            if v and (from_utc is None or ts >= from_utc):
                metric_buf.append((ts, metric, v, unit, el.get("sourceName", "unknown")))
        if len(hr_buf) + len(metric_buf) >= BATCH:
            flush()
    flush()

    kept, dropped = dedup_workouts(workouts, cfg)

    # map of gpx basenames
    gpx_files = {}
    if route_dir and os.path.isdir(route_dir):
        for root_dir, _d, files in os.walk(route_dir):
            for fn in files:
                if fn.lower().endswith(".gpx"):
                    gpx_files[fn] = os.path.join(root_dir, fn)

    # Fallback: some workouts carry no FileReference/metadata route link even
    # though the export has their GPX. Match such files to workouts by the
    # first trackpoint time (both UTC, no timezone arithmetic needed).
    def _gpx_first_utc(path, _cache={}):
        if path not in _cache:
            t = None
            try:
                for _ev, el in ET.iterparse(path, events=("end",)):
                    if local(el.tag) == "trkpt":
                        for ch in el:
                            if local(ch.tag) == "time" and ch.text:
                                t = E.parse_gpx_time(ch.text.strip())
                        el.clear()
                        if t is not None:
                            break
            except (ET.ParseError, OSError, ValueError):
                pass
            _cache[path] = t.replace(tzinfo=None) if t is not None else None  # naive UTC, matches start_utc
        return _cache[path]

    if gpx_files:
        pending = [(p, _gpx_first_utc(p)) for p in gpx_files.values()]
        used = set()
        for w in kept:
            if w["route_file"] or w["start_utc"] is None:
                continue
            for path, t0 in pending:
                if path in used or t0 is None:
                    continue
                if -300 <= (t0 - w["start_utc"]).total_seconds() <= 900:
                    w["route_file"] = os.path.basename(path)
                    used.add(path)
                    break

    rows = []
    seen_perf = 0
    for w in kept:
        d = workout_derived_columns(w)
        start = w["start_utc"]
        dur = w["duration_s"] or 0.0
        end = w["end_utc"] or (start + timedelta(seconds=dur) if dur else start)
        wid = E.make_workout_id(w["source"], start, dur, d["distance_m"])
        route_path = gpx_files.get(w["route_file"]) if w["route_file"] else None
        if w["route_file"] and not route_path and w["route_file"].endswith(".perf"):
            seen_perf += 1
        pts = parse_route_gpx(route_path) if route_path else []
        if pts:
            conn.execute("DELETE FROM route_points WHERE workout_id = ?", [wid])
            conn.execute("DELETE FROM series WHERE workout_id = ?", [wid])
            rp = [(wid, i, p["ts_utc"], p["lat"], p["lon"], p["alt"]) for i, p in enumerate(pts)]
            conn.executemany("INSERT INTO route_points VALUES (?,?,?,?,?,?)", rp)
            conn.executemany(
                "INSERT INTO series (workout_id, ts, lat, lon, alt, speed_mps, pace_s_per_km, hr, cadence, dist_m, source) "
                "VALUES (?,?,?,?,?,?,?,NULL,NULL,?,?)",
                [(wid, p["ts_utc"], p["lat"], p["lon"], p["alt"], p["speed_mps"],
                  (1000.0 / p["speed_mps"]) if p["speed_mps"] > 0.4 else None, p["dist_m"], w["source"])
                 for p in pts])
        rows.append((
            wid, w["sport"], w["source"], E.safe_extract_device(w["device"]) or w["source"],
            start, end, w["duration_s"], None, d["distance_m"], d["avg_pace_s_per_km"],
            d["best_pace_s_per_km"], d["elevation_gain_m"], d["avg_cadence"], d["avg_power"],
            d["active_energy_kcal"], d["avg_hr"], d["max_hr"], None, None, None,
            bool(pts), json.dumps({"activity": w["activity"], "route_file": w["route_file"],
                                   "stats": w["stats"]}, ensure_ascii=False, default=str),
        ))
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO workouts VALUES (" + ",".join(["?"] * 22) + ")", rows)
    for w in dropped:
        E.log_ingest(conn, "duplicate_suppressed", {
            "source": w["source"], "sport": w["sport"],
            "start": str(w["start_utc"]), "duration_s": w["duration_s"]})

    # fill missing HR stats from samples
    conn.execute("""
        UPDATE workouts w SET avg_hr = s.a, max_hr = s.m
        FROM (SELECT workout_id, avg(hr) a, max(hr) m FROM series WHERE workout_id IS NOT NULL GROUP BY workout_id) s
        WHERE w.workout_id = s.workout_id AND (w.avg_hr IS NULL OR w.max_hr IS NULL)
    """)
    # merge HR into GPS-based series (nearest earlier sample per point)
    backfill_series_hr(conn)
    E.log_ingest(conn, "apple_health_ingest", {
        "workouts_kept": len(rows), "duplicates": len(dropped), "hr_samples": stats["hr"],
        "metric_rows": stats["metrics"], "perf_routes_unsupported": seen_perf})
    return {"kept": len(rows), "dropped": len(dropped), **stats,
            "perf_unsupported": seen_perf}


def backfill_series_hr(conn):
    """Attach HR samples onto GPS series points (ASOF semantics, per workout)."""
    wids = [r[0] for r in conn.execute(
        "SELECT DISTINCT workout_id FROM series WHERE hr IS NULL").fetchall()]
    for wid in wids:
        hr = conn.execute(
            """SELECT ts, hr FROM preferred_hr
               WHERE ts BETWEEN (SELECT start_time - INTERVAL 2 MINUTE FROM workouts WHERE workout_id = ?)
                             AND (SELECT end_time + INTERVAL 5 MINUTE FROM workouts WHERE workout_id = ?)
               ORDER BY ts""", [wid, wid]).fetchall()
        if not hr:
            continue
        pts = conn.execute(
            "SELECT ts FROM series WHERE workout_id = ? AND hr IS NULL ORDER BY ts", [wid]).fetchall()
        times = [p[0].timestamp() for p in hr]
        vals = [p[1] for p in hr]
        upd = []
        for (ts,) in pts:
            i = bisect.bisect_right(times, ts.timestamp()) - 1
            if i >= 0 and ts.timestamp() - times[i] <= 90:
                upd.append((vals[i], wid, ts))
        if upd:
            conn.executemany("UPDATE series SET hr = ? WHERE workout_id = ? AND ts = ?", upd)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("export", help="export.zip / export.xml path")
    ap.add_argument("--db", default=E.DEFAULT_DB)
    ap.add_argument("--from", dest="from_date", default=None, help="only ingest data from this date (YYYY-MM-DD)")
    args = ap.parse_args()

    from_utc = None
    if args.from_date:
        from_utc = E.to_utc(E.parse_apple_date(args.from_date + " 00:00:00 +0000"))

    src = os.path.abspath(os.path.expanduser(args.export))
    conn = E.connect(args.db)

    if zipfile.is_zipfile(src):
        tmp = tempfile.mkdtemp(prefix="eha_export_")
        selective_unzip(src, tmp)
        xml_path = find_main_xml(tmp)
        route_dir = tmp
    elif os.path.isfile(src) and src.lower().endswith(".xml"):
        xml_path = src
        route_dir = os.path.dirname(src)
    else:
        raise SystemExit(f"[ingest] unsupported input: {src}")

    print(f"[ingest] parsing {xml_path} ...")
    summary = run_ingest(xml_path, route_dir, conn, from_utc)
    conn.close()
    print(f"[ingest] workouts kept: {summary['kept']}  duplicates suppressed: {summary['dropped']}")
    print(f"[ingest] hr samples: {summary['hr']}  metric rows: {summary['metrics']}  sleep minutes: {summary['sleep_min']:.0f}")
    if summary.get("perf_unsupported"):
        print(f"[ingest] NOTE: {summary['perf_unsupported']} legacy .perf routes skipped "
              f"(pre-iOS16 export); re-export from a newer device to get GPX routes.")


if __name__ == "__main__":
    main()
