#!/usr/bin/env python3
"""Analyze one workout: HR drift, HR recovery, zones, aerobic efficiency.

Produces the fixed daily structure (one-liner + 3 signals + next steps),
attaches history comparison (compare_history) and writes:
  output/workout.json   - unified workout schema
  output/analysis.json  - daily analysis structure

Medical boundary: this is a sports-analysis tool. It never diagnoses.
Usage:
  python analyze_workout.py --workout latest:run [--db ...]
  python analyze_workout.py --workout latest --rpe 7 --breathing hard --legs fresh \
      --pain none --feeling "最后两公里有点顶"
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
import compare_history as CH  # noqa: E402


# ---------------------------------------------------------------------------
# Series helpers
# ---------------------------------------------------------------------------

def load_series(conn, workout_id: str):
    rows = conn.execute(
        """SELECT ts, lat, lon, alt, speed_mps, pace_s_per_km, hr, cadence, dist_m
           FROM series WHERE workout_id = ? ORDER BY ts""", [workout_id]).fetchall()
    keys = ["ts", "lat", "lon", "alt", "speed_mps", "pace_s_per_km", "hr", "cadence", "dist_m"]
    return [dict(zip(keys, r)) for r in rows]


def smoothed_grades(pts, window_m=150.0):
    """Two-pointer rolling altitude grade (%) over ~window_m of distance."""
    out = [None] * len(pts)
    dists = [p["dist_m"] if p["dist_m"] is not None else None for p in pts]
    if any(d is None for d in dists):
        return out
    j = 0
    for i in range(len(pts)):
        while dists[i] - dists[j] > window_m:
            j += 1
        k = i
        while k + 1 < len(pts) and dists[k + 1] - dists[i] < window_m:
            k += 1
        dd = dists[k] - dists[j]
        if dd > 20 and pts[i]["alt"] is not None and pts[j]["alt"] is not None:
            out[i] = (pts[i]["alt"] - pts[j]["alt"]) / dd * 100.0
    return out


def steady_state_segment(pts, cfg):
    """Pick the steady aerobic slice: drop warm-up/cool-down/stops/hills/intervals."""
    ss = cfg["steady_state"]
    n = len(pts)
    if n < 30:
        return {"ok": False, "reason": "series_too_short", "points": []}
    t0 = pts[0]["ts"]
    speeds = [p["speed_mps"] or 0.0 for p in pts]
    moving = [v for v in speeds if v >= ss["min_speed_mps"]]
    if len(moving) < n * 0.3:
        return {"ok": False, "reason": "not_enough_moving_time", "points": []}
    med_v = statistics.median(moving)
    band = ss["pace_band_pct"] / 100.0
    grades = smoothed_grades(pts)
    lo_t = t0 + timedelta(minutes=ss["warmup_min"])
    hi_t = pts[-1]["ts"] - timedelta(minutes=ss["cooldown_min"])
    mask = []
    for i, p in enumerate(pts):
        ok = (lo_t <= p["ts"] <= hi_t
              and p["speed_mps"] is not None
              and med_v * (1 - band) <= p["speed_mps"] <= med_v * (1 + band)
              and p["hr"] is not None)
        mask.append(ok)
    # hill filter pass (relaxed if it kills too much data)
    def with_grade(m):
        return [ok and (grades[i] is not None and abs(grades[i]) <= ss["grade_max_pct"])
                for i, ok in enumerate(m)]
    strict = with_grade(mask)
    steady_s = sum((pts[i + 1]["ts"] - pts[i]["ts"]).total_seconds()
                   for i in range(n - 1) if strict[i] and strict[i + 1])
    grade_relaxed = False
    if steady_s < ss["min_steady_minutes"] * 60:
        strict = mask
        grade_relaxed = True
        steady_s = sum((pts[i + 1]["ts"] - pts[i]["ts"]).total_seconds()
                       for i in range(n - 1) if strict[i] and strict[i + 1])
    if steady_s < ss["min_steady_minutes"] * 60:
        return {"ok": False, "reason": "no_steady_segment", "points": [],
                "steady_seconds": steady_s}
    sel = [p for i, p in enumerate(pts) if strict[i]]
    sel_v = [p["speed_mps"] for p in sel if p["speed_mps"]]
    pace_cv = statistics.pstdev(sel_v) / statistics.mean(sel_v) * 100 if len(sel_v) > 2 else None
    return {"ok": True, "points": sel, "steady_seconds": steady_s,
            "median_speed_mps": med_v, "pace_cv_pct": round(pace_cv, 1) if pace_cv else None,
            "grade_relaxed": grade_relaxed,
            "interval_like": bool(pace_cv and pace_cv > 12)}


def _half_stats(points):
    hr = [p["hr"] for p in points if p["hr"] is not None]
    v = [p["speed_mps"] for p in points if p["speed_mps"]]
    out = {}
    out["avg_hr"] = statistics.mean(hr) if hr else None
    out["avg_speed_mps"] = statistics.mean(v) if v else None
    out["avg_pace_s_per_km"] = 1000.0 / out["avg_speed_mps"] if out["avg_speed_mps"] else None
    out["ef"] = (out["avg_speed_mps"] / out["avg_hr"]) if out["avg_speed_mps"] and out["avg_hr"] else None
    return out


def compute_drift(pts, cfg):
    """HR drift over the steady segment: second half vs first half, pace-held-constant check."""
    seg = steady_state_segment(pts, cfg)
    if not seg["ok"]:
        return {"available": False, "reason": seg["reason"]}
    sel = seg["points"]
    mid = sel[len(sel) // 2]["ts"]
    first, second = [p for p in sel if p["ts"] < mid], [p for p in sel if p["ts"] >= mid]
    a, b = _half_stats(first), _half_stats(second)
    if not a["avg_hr"] or not b["avg_hr"]:
        return {"available": False, "reason": "no_hr_in_steady_segment"}
    drift_pct = (b["avg_hr"] - a["avg_hr"]) / a["avg_hr"] * 100.0
    pace_change_pct = ((b["avg_pace_s_per_km"] - a["avg_pace_s_per_km"]) / a["avg_pace_s_per_km"] * 100.0
                       if a["avg_pace_s_per_km"] and b["avg_pace_s_per_km"] else None)
    # linear HR slope (bpm per 10 min) over the steady window
    t0 = sel[0]["ts"]
    xs = [(p["ts"] - t0).total_seconds() / 60.0 for p in sel]
    ys = [p["hr"] for p in sel]
    slope = None
    if len(xs) > 5 and statistics.pstdev(xs) > 0:
        mx, my = statistics.mean(xs), statistics.mean(ys)
        denom = sum((x - mx) ** 2 for x in xs)
        slope = (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom * 10) if denom else None
    interpretable = pace_change_pct is not None and abs(pace_change_pct) <= 5.0 and not seg["interval_like"]
    t_origin = pts[0]["ts"]
    return {
        "available": True,
        "steady_t0_s": round((sel[0]["ts"] - t_origin).total_seconds()),
        "steady_t1_s": round((sel[-1]["ts"] - t_origin).total_seconds()),
        "half_split_s": round(((sel[0]["ts"] - t_origin) + (sel[-1]["ts"] - sel[0]["ts"]) / 2).total_seconds()),
        "hr_drift_pct": round(drift_pct, 1),
        "first_half": {"avg_hr": round(a["avg_hr"], 1), "avg_pace_s_per_km": round(a["avg_pace_s_per_km"], 1)},
        "second_half": {"avg_hr": round(b["avg_hr"], 1), "avg_pace_s_per_km": round(b["avg_pace_s_per_km"], 1)},
        "pace_change_pct": round(pace_change_pct, 1) if pace_change_pct is not None else None,
        "hr_slope_bpm_per_10min": round(slope, 1) if slope is not None else None,
        "steady_seconds": round(seg["steady_seconds"]),
        "pace_cv_pct": seg["pace_cv_pct"],
        "interval_like": seg["interval_like"],
        "grade_relaxed": seg["grade_relaxed"],
        "interpretable": interpretable,
        "note": None if interpretable else
                ("配速变化较大或间歇课，漂移仅供参考" if not interpretable else ""),
    }


# ---------------------------------------------------------------------------
# HR recovery
# ---------------------------------------------------------------------------

def compute_hrr(conn, workout_id: str):
    row = conn.execute("SELECT end_time FROM workouts WHERE workout_id = ?", [workout_id]).fetchone()
    if not row or not row[0]:
        return {"available": False, "reason": "no_end_time"}
    end = row[0]  # naive UTC — must match the naive TIMESTAMP column for correct comparison

    # HRR's anchor should be the cessation of *effort*, not the end of the file:
    # interval sessions that finish with a cool-down walk have already recovered
    # at "end", so classic end-anchored HRR reads ~0. Anchor on the last HR peak
    # in the final 10 min when the ending is easy.
    win = conn.execute(
        """SELECT ts, hr FROM hr_samples
           WHERE ts BETWEEN ? AND ? ORDER BY ts""",
        [end - timedelta(minutes=10), end]).fetchall()
    if not win:
        return {"available": False, "reason": "no_hr_at_end"}
    anchor_ts, anchor_hr = max(win, key=lambda r: r[1])
    anchor_off = (end - anchor_ts).total_seconds()  # 0 = peaked right at the stop
    tail = [h for t, h in win if t >= end - timedelta(seconds=30)]
    hr_end = statistics.mean(tail) if tail else anchor_hr

    # Only re-anchor when the peak is followed by a sharp drop (interval walk
    # recovery). A gradual cooldown ramp right after the peak means "effort
    # tapered", where peak-anchoring would read near-zero; fall back to the
    # workout end there.
    steep_drop = False
    if anchor_off > 90:
        after = [h for t, h in win if anchor_ts + timedelta(seconds=20) <= t
                 <= anchor_ts + timedelta(seconds=70)]
        steep_drop = bool(after) and (anchor_hr - min(after)) >= 12
    if not steep_drop:
        anchor_ts, anchor_hr = end, hr_end
        anchor_off = 0.0

    def hr_at(offset_s, tol=25):
        # offsets are measured from the anchor (peak) time, so HRR semantics
        # stay "recovery from effort cessation" even when the file keeps rolling
        near = conn.execute(
            """SELECT hr FROM hr_samples
               WHERE ts BETWEEN ? AND ? ORDER BY ABS(EPOCH(ts - ?)) LIMIT 1""",
            [anchor_ts + timedelta(seconds=offset_s - tol),
             anchor_ts + timedelta(seconds=offset_s + tol),
             anchor_ts + timedelta(seconds=offset_s)]).fetchall()
        return near[0][0] if near else None

    h60, h120 = hr_at(60), hr_at(120)
    if h60 is None:
        return {"available": False, "reason": "no_post_workout_hr"}
    anchored_on_peak = anchor_off > 90
    note = ("训练以放松收尾，HRR 锚定在最后一段强度峰值（结束前 "
            f"{anchor_off/60:.0f} 分钟）" if anchored_on_peak else None)
    return {
        "available": True,
        "hr_end": round(hr_end, 1),
        "hr_anchor": round(anchor_hr, 1),
        "anchor_offset_s": round(anchor_off),
        "anchored_on": "last_effort_peak" if anchored_on_peak else "workout_end",
        "hrr_60s": round(anchor_hr - h60, 1),
        "hrr_120s": round(anchor_hr - h120, 1) if h120 is not None else None,
        "hr_60s": h60, "hr_120s": h120,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------

def resolve_hr_basis(conn, cfg, current_max_hr):
    """Priority: lab threshold > LTHR > configured max > observed max > 220-age."""
    ath = cfg.get("athlete") or {}
    if ath.get("lab_threshold_hr"):
        return {"type": "lab_threshold", "lthr": float(ath["lab_threshold_hr"]), "estimated": False}
    if ath.get("lthr"):
        return {"type": "configured_lthr", "lthr": float(ath["lthr"]), "estimated": False}
    if ath.get("max_hr") is not None:
        configured = float(ath["max_hr"])
        if not 100 <= configured <= 240:
            raise ValueError("athlete.max_hr must be between 100 and 240 bpm")
        return {"type": "configured_max_hr", "max_hr": configured, "estimated": False,
                "note": "user-configured maximum heart rate"}
    hist_max = conn.execute(
        """SELECT max(max_hr) FROM workouts
           WHERE max_hr IS NOT NULL AND start_time >= now() - INTERVAL 365 DAY""").fetchone()
    hist_max = hist_max[0] if hist_max else None
    if current_max_hr and (hist_max is None or current_max_hr >= hist_max):
        return {"type": "observed_max_history", "max_hr": float(current_max_hr),
                "estimated": True, "note": "personal historical max (lower bound of true max)"}
    if hist_max:
        return {"type": "observed_max_history", "max_hr": float(hist_max), "estimated": True,
                "note": "personal historical max (lower bound of true max)"}
    if ath.get("age"):
        est = 220 - int(ath["age"])
        return {"type": "age_formula", "max_hr": float(est), "estimated": True,
                "note": "220-age estimate — set athlete.max_hr or lthr in config for accuracy"}
    return None


def zone_boundaries(basis):
    """Return [(name, lo_frac, hi_frac)] in HR terms."""
    if basis["type"] in ("lab_threshold", "configured_lthr"):
        l = basis["lthr"]
        return [("Z1", 0, l * 0.81), ("Z2", l * 0.81, l * 0.89),
                ("Z3", l * 0.89, l * 0.96), ("Z4", l * 0.96, l), ("Z5", l, 999)]
    m = basis["max_hr"]
    return [("Z1", 0, m * 0.60), ("Z2", m * 0.60, m * 0.70),
            ("Z3", m * 0.70, m * 0.80), ("Z4", m * 0.80, m * 0.90), ("Z5", m * 0.90, 999)]


def compute_zones(pts, basis):
    if not basis:
        return {"available": False, "reason": "no_hr_basis"}
    zs = zone_boundaries(basis)
    secs = [0.0] * 5
    for i in range(len(pts) - 1):
        p, q = pts[i], pts[i + 1]
        if p["hr"] is None or p["speed_mps"] is None or p["speed_mps"] < 1.0:
            continue
        dt = (q["ts"] - p["ts"]).total_seconds()
        for zi, (_name, lo, hi) in enumerate(zs):
            if lo <= p["hr"] < hi:
                secs[zi] += dt
                break
    total = sum(secs)
    return {
        "available": total > 0,
        "basis": basis,
        "zone_seconds": {name: round(s) for (name, _lo, _hi), s in zip(zs, secs)},
        "zone_pct": ({name: round(s / total * 100, 1) for (name, _lo, _hi), s in zip(zs, secs)}
                     if total else None),
    }


# ---------------------------------------------------------------------------
# Recovery context
# ---------------------------------------------------------------------------

def recovery_context(conn, workout_id: str):
    row = conn.execute("SELECT start_time FROM workouts WHERE workout_id = ?", [workout_id]).fetchone()
    start = row[0].replace(tzinfo=timezone.utc)

    def recent(metric, days, agg="avg"):
        q = f"""SELECT {agg}(value) FROM metric_samples WHERE metric = ?
                AND ts BETWEEN ? - INTERVAL {days} DAY AND ?"""
        r = conn.execute(q, [metric, start, start]).fetchone()
        return round(r[0], 1) if r and r[0] is not None else None

    rhr_day = conn.execute(
        """SELECT avg(value) FROM metric_samples WHERE metric='resting_hr'
           AND ts BETWEEN ? - INTERVAL 1 DAY AND ?""", [start, start]).fetchone()
    hrv_day = conn.execute(
        """SELECT avg(value) FROM metric_samples WHERE metric='hrv_sdnn'
           AND ts BETWEEN ? - INTERVAL 1 DAY AND ?""", [start, start]).fetchone()
    sleep_prev = conn.execute(
        """SELECT sum(value) FROM metric_samples WHERE metric='sleep_asleep'
           AND ts BETWEEN ? - INTERVAL 36 HOUR AND ? - INTERVAL 6 HOUR""", [start, start]).fetchone()
    load7 = conn.execute(
        """SELECT sum(distance_m)/1000.0 FROM workouts
           WHERE sport_type='run' AND start_time < ? AND start_time >= ? - INTERVAL 7 DAY""",
        [start, start]).fetchone()
    return {
        "resting_hr_day": round(rhr_day[0], 1) if rhr_day and rhr_day[0] else None,
        "hrv_sdnn_day": round(hrv_day[0], 1) if hrv_day and hrv_day[0] else None,
        "sleep_prev_night_min": round(sleep_prev[0]) if sleep_prev and sleep_prev[0] else None,
        "km_last_7d": round(load7[0], 1) if load7 and load7[0] else None,
        "resting_hr_28d": recent("resting_hr", 28),
        "hrv_sdnn_28d": recent("hrv_sdnn", 28),
    }


# ---------------------------------------------------------------------------
# Narrative (deterministic templates; the agent may refine wording)
# ---------------------------------------------------------------------------

def _fmt_env(weather):
    if not weather:
        return None
    t, h = weather.get("temperature_c"), weather.get("humidity_pct")
    if t is None:
        return None
    s = f"{t:.0f}°C"
    if h is not None:
        s += f" · RH {h:.0f}%"
    return s


def heat_load(weather):
    if not weather:
        return "unknown"
    t = weather.get("temperature_c") or 0
    dp = weather.get("dew_point_c") or 0
    ap = weather.get("apparent_c") or t
    if t >= 27 or dp >= 21 or ap >= 31:
        return "high"
    if t >= 22 or dp >= 18:
        return "moderate"
    if t <= 4:
        return "cold"
    return "low"


def classify_session(pts) -> dict:
    """Coarse session shape from per-minute speed: steady run vs run-walk
    intervals vs walk-dominated. Used for narrative honesty — drift and
    EF only read well when we know what the session was."""
    if not pts:
        return {"type": "unknown", "reason": "no_series"}
    # minute buckets of median speed
    t0 = pts[0]["ts"]
    buckets = {}
    for p in pts:
        if p.get("speed_mps") is None:
            continue
        key = int((p["ts"] - t0).total_seconds() // 60)
        buckets.setdefault(key, []).append(p["speed_mps"])
    if len(buckets) < 6:
        return {"type": "unknown", "reason": "too_short"}
    mins = [statistics.median(v) for _k, v in sorted(buckets.items())]
    flags = ["run" if m >= 2.2 else ("walk" if m <= 1.7 else "jog") for m in mins]
    transitions = sum(1 for a, b in zip(flags, flags[1:]) if {a, b} == {"run", "walk"})
    run_min = sum(1 for f in flags if f == "run")
    walk_min = sum(1 for f in flags if f == "walk")
    if transitions >= 5 and run_min >= 8:
        return {"type": "run_walk_intervals", "cycles": transitions,
                "run_min": run_min, "walk_min": walk_min}
    if walk_min > len(flags) * 0.6:
        return {"type": "walk_dominant", "run_min": run_min, "walk_min": walk_min}
    if transitions >= 3 and walk_min >= 3:
        return {"type": "mixed", "transitions": transitions,
                "run_min": run_min, "walk_min": walk_min}
    return {"type": "continuous_run", "run_min": run_min, "walk_min": walk_min}


def build_narrative(w, drift, hrr, zones, weather, comparison, rctx, subjective,
                    session=None):
    sig = {}
    base_delta = (comparison or {}).get("hr_vs_baseline_bpm")
    session = session or {}

    # ❤️ cardiovascular
    if base_delta is not None and base_delta >= 5:
        sig["cardiovascular"] = f"HR Cost · 同配速 HR 比个人基线 +{base_delta:.0f} bpm"
    elif base_delta is not None and base_delta <= -5:
        sig["cardiovascular"] = f"HR Cost · 同配速 HR 比个人基线 {base_delta:.0f} bpm（成本更低）"
    else:
        sig["cardiovascular"] = "HR Cost · 心率在个人基线范围内" if base_delta is not None else \
            "HR Cost · 历史基线数据不足，无法对比"

    # 🌡 environment
    env = _fmt_env(weather)
    hl = heat_load(weather)
    if env and hl == "high":
        sig["environment"] = f"Heat Load · {env}，湿热环境推高生理成本"
    elif env and hl == "moderate":
        sig["environment"] = f"Heat Load · {env}，轻度热负荷"
    elif env and hl == "cold":
        sig["environment"] = f"Heat Load · {env}，低温注意热身"
    elif env:
        sig["environment"] = f"Heat Load · {env}，接近理想区间"
    else:
        sig["environment"] = "Heat Load · 天气数据未补全"

    # ⚡ performance
    ses_tag = {"run_walk_intervals": "跑走间歇", "walk_dominant": "以走为主",
               "mixed": "走跑混合", "continuous_run": "持续跑"}.get(session.get("type"))
    if drift.get("available") and drift.get("interpretable"):
        d = drift["hr_drift_pct"]
        base = f"Drift · 后半程 HR 漂移 +{d:.1f}%" if d >= 0 else f"Drift · 后半程 HR {d:.1f}%"
        sig["performance"] = f"{base}（稳定段）" if session.get("type") == "run_walk_intervals" else base
    elif drift.get("available"):
        sig["performance"] = f"Drift · {drift['hr_drift_pct']:+.1f}%（配速波动大，仅供参考）"
    else:
        sig["performance"] = "Drift · 稳定段不足，未计算"
    if ses_tag and session.get("type") in ("run_walk_intervals", "mixed"):
        sig["performance"] += f" · 本次识别为{ses_tag}"

    # one-liner
    parts = []
    cv_high = base_delta is not None and base_delta >= 5
    if cv_high:
        parts.append("今天心血管成本明显偏高")
    elif base_delta is not None and base_delta <= -5:
        parts.append("今天同配速下心率更低，有氧状态良好")
    else:
        parts.append("今天心血管负荷接近个人正常水平")
    if session.get("type") == "run_walk_intervals":
        parts[0] += f"（{session['cycles']} 组跑走间歇）"
    causes = []
    if hl in ("high", "moderate"):
        causes.append("环境湿热" if hl == "high" else "环境温度偏高")
    if drift.get("available") and drift.get("hr_drift_pct", 0) >= 4 and drift.get("interpretable"):
        causes.append(f"后半程心率漂移 {drift['hr_drift_pct']:+.1f}%")
    if rctx.get("sleep_prev_night_min") and rctx["sleep_prev_night_min"] < 360:
        causes.append(f"前一晚睡眠偏短（{rctx['sleep_prev_night_min'] / 60:.1f}h）")
    one = parts[0]
    if cv_high and causes:
        one += f"；{'、'.join(causes[:2])} 更可能是环境与恢复状态推高了生理成本，而不是跑力下降"
    elif causes and not cv_high:
        one += "；" + "、".join(causes[:2])
    if subjective and subjective.get("breathing") in ("hard", "very hard") \
            and subjective.get("legs") in ("fresh", "normal") and cv_high:
        one += "。主观感受（呼吸吃力、腿不累）与心肺限制为主的模式一致"

    # next steps (1-2)
    steps = []
    if cv_high and hl in ("high", "moderate"):
        steps.append("下一次轻松跑改以心率/RPE 控制强度，不强行守配速")
        steps.append("在凉爽时段复测相同配速，观察心率是否回落")
    elif cv_high:
        steps.append("近期注意恢复（睡眠/补水），复测同配速心率")
    elif base_delta is not None and base_delta <= -5:
        steps.append("有氧效率上行，可保持当前负荷结构，逐步加入质量课")
    if not steps:
        steps.append("保持当前训练结构，注意训练与恢复平衡")
    steps = steps[:2]

    return {"one_liner": one + "。", "signals": sig, "next_steps": steps}


# ---------------------------------------------------------------------------
# Unified schema + main
# ---------------------------------------------------------------------------

def unified_workout(conn, workout_id: str, w, weather, rctx):
    st = w["start_time"].replace(tzinfo=timezone.utc)
    local = st.astimezone()
    subjective = conn.execute(
        "SELECT rpe, breathing, legs, pain, feeling FROM subjective WHERE workout_id = ?",
        [workout_id]).fetchone()
    subj = None
    if subjective:
        subj = dict(zip(["rpe", "breathing", "legs", "pain", "feeling"], subjective))
    n_route = conn.execute(
        "SELECT count(*) FROM route_points WHERE workout_id = ?", [workout_id]).fetchone()[0]
    return {
        "identity": {"workout_id": workout_id, "sport_type": w["sport_type"],
                     "source": w["source"], "device": w["device"]},
        "time": {"start_time": local.isoformat(), "duration_s": w["duration_s"],
                 "moving_time_s": w["moving_time_s"]},
        "performance": {
            "distance_km": round(w["distance_m"] / 1000, 2) if w["distance_m"] else None,
            "avg_pace": E.fmt_pace(w["avg_pace_s_per_km"]),
            "avg_pace_s_per_km": round(w["avg_pace_s_per_km"], 1) if w["avg_pace_s_per_km"] else None,
            "best_pace": E.fmt_pace(w["best_pace_s_per_km"]),
            "elevation_gain_m": w["elevation_gain_m"], "cadence": w["avg_cadence"],
            "power": w["avg_power"]},
        "cardiovascular": {"avg_hr": w["avg_hr"], "max_hr": w["max_hr"]},
        "environment": weather,
        "recovery_context": {**rctx, "subjective": subj},
        "route": {"points": n_route},
    }


def analyze(conn, workout_id: str, cfg):
    wrow = E.get_workout_row(conn, workout_id)
    if not wrow:
        return None
    w = dict(zip(E.WORKOUT_COLS, wrow))
    pts = load_series(conn, workout_id)
    drift = compute_drift(pts, cfg)
    hrr = compute_hrr(conn, workout_id)
    basis = resolve_hr_basis(conn, cfg, w["max_hr"])
    zones = compute_zones(pts, basis)
    weather = E.get_weather(conn, workout_id)
    rctx = recovery_context(conn, workout_id)
    comparison = CH.build_comparison(conn, workout_id, cfg)
    subjective = conn.execute(
        "SELECT rpe, breathing, legs, pain, feeling FROM subjective WHERE workout_id = ?",
        [workout_id]).fetchone()
    subj = dict(zip(["rpe", "breathing", "legs", "pain", "feeling"], subjective)) if subjective else None

    # aerobic efficiency for this workout (steady segment)
    ef = None
    if drift.get("available"):
        seg = steady_state_segment(pts, cfg)
        if seg["ok"]:
            st = _half_stats(seg["points"])
            ef = round(st["ef"], 4) if st["ef"] else None
    if w["avg_hr"] and w["avg_pace_s_per_km"]:
        v = 1000.0 / w["avg_pace_s_per_km"]
        ef_whole = round(v / w["avg_hr"], 4)
    else:
        ef_whole = None

    narrative = build_narrative(w, drift, hrr, zones, weather, comparison, rctx, subj,
                                session=classify_session(pts))

    analysis = {
        "workout_id": workout_id,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "one_liner": narrative["one_liner"],
        "signals": narrative["signals"],
        "next_steps": narrative["next_steps"],
        "metrics": {
            "hr_drift": drift,
            "heart_rate_recovery": hrr,
            "hr_zones": zones,
            "aerobic_efficiency": {"steady_ef": ef, "whole_workout_ef": ef_whole,
                                   "unit": "m/s per bpm — personal longitudinal index only"},
        },
        "baseline_comparison": comparison,
        "medical_boundary": {
            "is_medical_diagnosis": False,
            "note": "运动健康分析，不构成医学诊断；异常表现+症状请寻求专业医学评估。",
        },
    }
    # persist computed columns + analysis
    conn.execute(
        "UPDATE workouts SET hr_drift_pct = ?, hr_recovery_60s = ?, hr_recovery_120s = ? "
        "WHERE workout_id = ?",
        [drift.get("hr_drift_pct") if drift.get("available") else None,
         hrr.get("hrr_60s") if hrr.get("available") else None,
         hrr.get("hrr_120s") if hrr.get("available") else None,
         workout_id])
    conn.execute("INSERT OR REPLACE INTO analyses VALUES (?, now(), ?)",
                 [workout_id, json.dumps(analysis, ensure_ascii=False, default=str)])
    conn.commit()

    unified = unified_workout(conn, workout_id, w, weather, rctx)
    return {"workout": unified, "analysis": analysis}


def save_subjective(conn, workout_id, args):
    conn.execute(
        "INSERT OR REPLACE INTO subjective VALUES (?,?,?,?,?,?,now())",
        [workout_id, args.rpe, args.breathing, args.legs, args.pain, args.feeling])
    conn.commit()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workout", default="latest:run")
    ap.add_argument("--db", default=E.DEFAULT_DB)
    ap.add_argument("--rpe", type=int, choices=range(1, 11), default=None)
    ap.add_argument("--breathing", choices=["easy", "moderate", "hard", "very_hard"], default=None)
    ap.add_argument("--legs", choices=["fresh", "normal", "tired"], default=None)
    ap.add_argument("--pain", default=None)
    ap.add_argument("--feeling", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    cfg = E.load_config()
    conn = E.connect(args.db)
    wid = E.resolve_workout(conn, args.workout)
    if not wid:
        print("[analyze] workout not found", file=sys.stderr)
        return 1
    has_subjective = any([args.rpe, args.breathing, args.legs, args.pain, args.feeling])
    if has_subjective:
        save_subjective(conn, wid, args)

    result = analyze(conn, wid, cfg)
    conn.close()
    if not result:
        return 1
    out_dir = E.ensure_output_dir()
    with open(os.path.join(out_dir, "workout.json"), "w", encoding="utf-8") as f:
        json.dump(result["workout"], f, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "analysis.json"), "w", encoding="utf-8") as f:
        json.dump(result["analysis"], f, ensure_ascii=False, indent=2)

    a = result["analysis"]
    print(f"[analyze] {wid}")
    print(f"  一句话  {a['one_liner']}")
    for k, v in a["signals"].items():
        print(f"  {k:<16} {v}")
    for s in a["next_steps"]:
        print(f"  → {s}")
    m = a["metrics"]
    if m["hr_drift"].get("available"):
        d = m["hr_drift"]
        print(f"  drift {d['hr_drift_pct']:+.1f}%  (pace {d.get('pace_change_pct', '–')}%, "
              f"steady {d['steady_seconds'] / 60:.0f}min, interpretable={d['interpretable']})")
    if m["heart_rate_recovery"].get("available"):
        h = m["heart_rate_recovery"]
        print(f"  HRR60 {h['hrr_60s']:.0f} bpm  HRR120 {h.get('hrr_120s', '–')}")
    print(f"  outputs → {out_dir}/workout.json, analysis.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
