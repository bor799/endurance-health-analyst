#!/usr/bin/env python3
"""Compare a workout against the athlete's own history — never against population averages.

- Similar-workout retrieval: same sport, similar distance/pace, optionally matched
  on temperature & humidity (relaxed when too few matches).
- Rolling baselines: 7 / 28 / 90 / 365 days / all.
- Aerobic trend: EF (speed per heartbeat) 28d vs 90d, HR-at-typical-pace trend.

Usage:
  python compare_history.py --workout latest:run [--db ...]
  python compare_history.py --trend [--sport run]
  python compare_history.py --windows
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eha_lib as E  # noqa: E402

RUN_COLS = ("workout_id, sport_type, start_time, duration_s, distance_m, avg_pace_s_per_km, "
            "avg_hr, max_hr, hr_drift_pct, source")
RUN_KEYS = ["workout_id", "sport_type", "start_time", "duration_s", "distance_m",
            "avg_pace_s_per_km", "avg_hr", "max_hr", "hr_drift_pct", "source"]


def _row(r):
    d = dict(zip(RUN_KEYS, r))
    return d


def _ef(pace_s_per_km, avg_hr):
    if pace_s_per_km and avg_hr and pace_s_per_km > 0:
        return (1000.0 / pace_s_per_km) / avg_hr
    return None


def _pct(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return round(s[0], 1)
    k = (len(s) - 1) * q
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    v = s[f] + (s[c] - s[f]) * (k - f)
    return round(v, 1)


# ---------------------------------------------------------------------------
# Similar workouts
# ---------------------------------------------------------------------------

def similar_workouts(conn, wid: str, cfg: dict):
    f = cfg["similar_filters"]
    row = conn.execute(
        f"SELECT {RUN_COLS} FROM workouts WHERE workout_id = ?", [wid]).fetchone()
    if not row:
        return None, []
    cur = _row(row)
    pace_tol = f["pace_tolerance_pct"] / 100.0
    dist_tol = f["distance_tolerance_pct"] / 100.0
    pace, dist = cur["avg_pace_s_per_km"], cur["distance_m"]
    if not pace or not dist:
        return cur, []

    cands = conn.execute(
        f"""SELECT {RUN_COLS} FROM workouts
            WHERE sport_type = ? AND workout_id != ?
              AND start_time < ? AND start_time >= ? - INTERVAL {int(f['max_lookback_days'])} DAY
              AND avg_pace_s_per_km IS NOT NULL AND distance_m IS NOT NULL""",
        [cur["sport_type"], wid, cur["start_time"], cur["start_time"]]).fetchall()
    out = []
    for r in cands:
        c = _row(r)
        if abs(c["avg_pace_s_per_km"] - pace) / pace > pace_tol:
            continue
        if abs(c["distance_m"] - dist) / dist > dist_tol:
            continue
        wx = E.get_weather(conn, c["workout_id"])
        if wx:
            c["weather"] = wx
        out.append(c)
    cur_wx = E.get_weather(conn, wid)

    def env_ok(c):
        wx = c.get("weather")
        if not wx or not cur_wx:
            return True  # unknown env: don't exclude, but strict tier needs known+matching
        return (abs((wx["temperature_c"] or 99) - (cur_wx["temperature_c"] or 99)) <= f["temp_tolerance_c"]
                and abs((wx["humidity_pct"] or 999) - (cur_wx["humidity_pct"] or 999)) <= f["humidity_tolerance_pct"])

    strict = [c for c in out if env_ok(c)]
    relaxed = False
    if len(strict) >= f["min_similar"]:
        chosen = strict
    elif len(out) >= f["min_similar"]:
        chosen, relaxed = out, True
    else:
        chosen, relaxed = out, True
    return cur, chosen


def _drifts_on_the_fly(conn, sims, cfg) -> list[float]:
    """Compute HR drift for similar workouts that were never analyzed."""
    # lazy import: analyze_workout imports this module at load time
    import analyze_workout as AW
    out = []
    for c in sims[:12]:
        pts = AW.load_series(conn, c["workout_id"])
        if len(pts) < 60:
            continue
        d = AW.compute_drift(pts, cfg)
        if d.get("available"):
            out.append(d["hr_drift_pct"])
            conn.execute("UPDATE workouts SET hr_drift_pct = ? WHERE workout_id = ?",
                         [d["hr_drift_pct"], c["workout_id"]])
    conn.commit()
    return out


def build_comparison(conn, wid: str, cfg: dict) -> dict | None:
    cur, sims = similar_workouts(conn, wid, cfg)
    if not cur:
        return None
    cur_wx = E.get_weather(conn, wid)
    hrs = [c["avg_hr"] for c in sims if c["avg_hr"]]
    cur_ef = _ef(cur["avg_pace_s_per_km"], cur["avg_hr"])
    efs = [_ef(c["avg_pace_s_per_km"], c["avg_hr"]) for c in sims]
    efs = [e for e in efs if e]
    drifts = [c["hr_drift_pct"] for c in sims if c["hr_drift_pct"] is not None]
    if not drifts and sims:
        drifts = _drifts_on_the_fly(conn, sims, cfg)
    enough = len(hrs) >= cfg["similar_filters"]["min_similar"]
    delta = round(cur["avg_hr"] - statistics.median(hrs), 1) if (hrs and cur["avg_hr"]) else None
    percentile = None
    if hrs and cur["avg_hr"]:
        below = sum(1 for h in hrs if h < cur["avg_hr"])
        percentile = round(below / len(hrs) * 100)
    return {
        "similar_workout_count": len(sims),
        "enough_for_baseline": enough,
        "filters": {
            "pace_within_pct": cfg["similar_filters"]["pace_tolerance_pct"],
            "distance_within_pct": cfg["similar_filters"]["distance_tolerance_pct"],
            "environment_matched": cur_wx is not None and not any(
                c.get("weather") is None for c in sims[:3]),
            "environment_relaxed": len(sims) > 0 and not enough,
        },
        "baseline_avg_hr": {
            "median": round(statistics.median(hrs), 1) if hrs else None,
            "p25": _pct(hrs, 0.25), "p75": _pct(hrs, 0.75), "n": len(hrs),
        },
        "hr_vs_baseline_bpm": delta,
        "hr_percentile_in_history": percentile,
        "baseline_drift_pct_median": round(statistics.median(drifts), 1) if drifts else None,
        "ef_today": round(cur_ef, 4) if cur_ef else None,
        "ef_baseline_median": round(statistics.median(efs), 4) if efs else None,
        "ef_delta_pct": (round((cur_ef / statistics.median(efs) - 1) * 100, 1)
                         if cur_ef and efs else None),
        "today_avg_hr": cur["avg_hr"],
        "similar_recent": [
            {"date": c["start_time"].date().isoformat(),
             "km": round(c["distance_m"] / 1000, 1),
             "pace": E.fmt_pace(c["avg_pace_s_per_km"]),
             "avg_hr": c["avg_hr"],
             "temp_c": (c.get("weather") or {}).get("temperature_c")}
            for c in sorted(sims, key=lambda c: c["start_time"], reverse=True)[:8]
        ],
    }


# ---------------------------------------------------------------------------
# Rolling baseline windows
# ---------------------------------------------------------------------------

def rolling_windows(conn, as_of=None, sport="run"):
    as_of = as_of or datetime.now(timezone.utc).replace(tzinfo=None)
    out = []
    for label, days in [("7d", 7), ("28d", 28), ("90d", 90), ("365d", 365), ("all", None)]:
        cond = "" if days is None else f"AND start_time >= ? - INTERVAL {days} DAY"
        rows = conn.execute(
            f"""SELECT count(*), sum(distance_m)/1000.0, avg(avg_pace_s_per_km), avg(avg_hr)
                FROM workouts WHERE sport_type = ? {cond}""",
            [sport, as_of] if days else [sport]).fetchone()
        n, km, pace, hr = rows
        rhr = conn.execute(
            f"""SELECT avg(value) FROM metric_samples WHERE metric='resting_hr' {cond.replace('start_time', 'ts')}""",
            ["resting_hr", as_of] if days else ["resting_hr"]).fetchone()[0] if n else None
        hrv = conn.execute(
            f"""SELECT avg(value) FROM metric_samples WHERE metric='hrv_sdnn' {cond.replace('start_time', 'ts')}""",
            ["hrv_sdnn", as_of] if days else ["hrv_sdnn"]).fetchone()[0] if n else None
        out.append({
            "window": label, "runs": n,
            "km": round(km, 1) if km else 0.0,
            "avg_pace": E.fmt_pace(pace) if pace else None,
            "avg_hr": round(hr, 1) if hr else None,
            "resting_hr": round(rhr, 1) if rhr else None,
            "hrv_sdnn": round(hrv, 1) if hrv else None,
        })
    return out


# ---------------------------------------------------------------------------
# Aerobic trend: better or worse?
# ---------------------------------------------------------------------------

def aerobic_trend(conn, sport="run", as_of=None):
    as_of = as_of or datetime.now(timezone.utc).replace(tzinfo=None)
    rows = conn.execute(
        f"""SELECT {RUN_COLS} FROM workouts
            WHERE sport_type = ? AND start_time <= ? AND start_time >= ? - INTERVAL 90 DAY
              AND avg_pace_s_per_km IS NOT NULL AND avg_hr IS NOT NULL""",
        [sport, as_of, as_of]).fetchall()
    runs = [_row(r) for r in rows]
    if len(runs) < 6:
        return {"available": False,
                "reason": f"insufficient history ({len(runs)}/6 runs in 90d)",
                "runs_90d": len(runs)}

    def bucket(days):
        cut = as_of - timedelta(days=days)
        sel = [r for r in runs if r["start_time"] >= cut]
        efs = [_ef(r["avg_pace_s_per_km"], r["avg_hr"]) for r in sel]
        efs = [e for e in efs if e]
        return {"n": len(sel),
                "ef_avg": round(statistics.mean(efs), 4) if efs else None,
                "hr_avg": round(statistics.mean([r["avg_hr"] for r in sel]), 1) if sel else None,
                "pace_median": statistics.median([r["avg_pace_s_per_km"] for r in sel]) if sel else None}

    recent, baseline = bucket(28), bucket(90)
    # HR-at-similar-pace comparison (fairer than raw avg across different paces)
    pace_ref = recent["pace_median"] or baseline["pace_median"]
    near = [r for r in runs if abs(r["avg_pace_s_per_km"] - pace_ref) / pace_ref <= 0.08]
    hr_pace_28 = [r["avg_hr"] for r in near if r["start_time"] >= as_of - timedelta(days=28)]
    hr_pace_29_90 = [r["avg_hr"] for r in near if r["start_time"] < as_of - timedelta(days=28)]
    hr_at_pace_delta = None
    if hr_pace_28 and hr_pace_29_90:
        hr_at_pace_delta = round(statistics.mean(hr_pace_28) - statistics.mean(hr_pace_29_90), 1)

    rhr_28 = conn.execute(
        """SELECT avg(value) FROM metric_samples WHERE metric='resting_hr'
           AND ts >= ? - INTERVAL 28 DAY AND ts <= ?""", [as_of, as_of]).fetchone()[0]
    rhr_29_90 = conn.execute(
        """SELECT avg(value) FROM metric_samples WHERE metric='resting_hr'
           AND ts >= ? - INTERVAL 90 DAY AND ts < ? - INTERVAL 28 DAY""", [as_of, as_of]).fetchone()[0]
    rhr_delta = round(rhr_28 - rhr_29_90, 1) if (rhr_28 and rhr_29_90) else None

    verdict, reasons = None, []
    ef_delta_pct = None
    if recent["ef_avg"] and baseline["ef_avg"]:
        ef_delta_pct = round((recent["ef_avg"] / baseline["ef_avg"] - 1) * 100, 1)
    if ef_delta_pct is not None or hr_at_pace_delta is not None:
        votes = []
        if ef_delta_pct is not None:
            votes.append(1 if ef_delta_pct >= 1 else (-1 if ef_delta_pct <= -1 else 0))
            reasons.append(f"有氧效率 EF 28d vs 90d {ef_delta_pct:+.1f}%")
        if hr_at_pace_delta is not None:
            votes.append(-1 if hr_at_pace_delta >= 2 else (1 if hr_at_pace_delta <= -2 else 0))
            reasons.append(f"同配速 HR 28d vs 90d {hr_at_pace_delta:+.1f} bpm")
        if rhr_delta is not None:
            votes.append(-1 if rhr_delta >= 2 else (1 if rhr_delta <= -2 else 0))
            reasons.append(f"静息心率 28d vs 90d {rhr_delta:+.1f} bpm")
        s = sum(votes)
        verdict = "improving" if s >= 1 else ("declining" if s <= -1 else "stable")
    verdict_zh = {"improving": "正在变好", "declining": "有走弱迹象", "stable": "保持稳定",
                  None: "数据不足"}[verdict]
    return {
        "available": True, "runs_90d": len(runs),
        "recent_28d": recent, "baseline_90d": baseline,
        "ef_delta_pct": ef_delta_pct,
        "hr_at_similar_pace_delta_bpm": hr_at_pace_delta,
        "resting_hr_delta_bpm": rhr_delta,
        "verdict": verdict, "verdict_zh": verdict_zh, "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workout", default=None, help="workout id / latest:run (similar-workout comparison)")
    ap.add_argument("--trend", action="store_true", help="28d vs 90d aerobic trend")
    ap.add_argument("--windows", action="store_true", help="rolling baseline table")
    ap.add_argument("--sport", default="run")
    ap.add_argument("--db", default=E.DEFAULT_DB)
    args = ap.parse_args()

    cfg = E.load_config()
    conn = E.connect(args.db)
    rc = 0
    if args.trend:
        t = aerobic_trend(conn, args.sport)
        print(json.dumps(t, ensure_ascii=False, indent=2, default=str))
    elif args.windows:
        for w in rolling_windows(conn, sport=args.sport):
            print(w)
    else:
        wid = E.resolve_workout(conn, args.workout or "latest:run", args.sport)
        if not wid:
            print("[compare] workout not found", file=sys.stderr)
            rc = 1
        else:
            cmp_ = build_comparison(conn, wid, cfg)
            print(json.dumps(cmp_, ensure_ascii=False, indent=2, default=str))
    conn.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
