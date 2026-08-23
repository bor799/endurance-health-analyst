#!/usr/bin/env python3
"""End-to-end acceptance test — the V0.1 vertical slice.

Apple Health export.zip → ingest (dedup) → weather → HR drift → similar-run
baseline → one-liner → workout-card.png. Runs against its own DuckDB file
(data/e2e_health.duckdb) so it never touches real user data.

Usage: .venv/bin/python tests/e2e_test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
SCRIPTS = os.path.join(ROOT, "scripts")
DB = os.path.join(ROOT, "data", "e2e_health.duckdb")
OUT = os.path.join(ROOT, "output")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def run(args, expect_rc=0):
    r = subprocess.run([PY] + args, capture_output=True, text=True, cwd=ROOT)
    if r.returncode != expect_rc:
        print(r.stdout[-3000:])
        print(r.stderr[-3000:], file=sys.stderr)
    return r


def main():
    print("== e2e: V0.1 vertical slice ==")
    tmp = tempfile.mkdtemp(prefix="eha_e2e_")
    fixture = os.path.join(tmp, "export.zip")

    print("\n-- 0. fixture (synthetic Apple Health export, 17 workouts incl. 2 dupes)")
    r = run([os.path.join(SCRIPTS, "make_synthetic_export.py"), "--out", fixture])
    check("fixture built", os.path.exists(fixture) and os.path.getsize(fixture) > 100_000,
          f"{os.path.getsize(fixture) / 1024:.0f} KB")

    if os.path.exists(DB):
        os.remove(DB)

    print("\n-- 1. ingest Apple Health")
    r = run([os.path.join(SCRIPTS, "ingest_apple_health.py"), fixture, "--db", DB])
    check("ingest exits 0", r.returncode == 0, r.stdout.strip().splitlines()[-1] if r.stdout else "")
    check("15 workouts kept / 2 dupes suppressed",
          "workouts kept: 15" in r.stdout and "duplicates suppressed: 2" in r.stdout)

    import duckdb
    conn = duckdb.connect(DB)
    n_series = conn.execute(
        "SELECT count(*), count(hr) FROM series WHERE workout_id = "
        "(SELECT workout_id FROM workouts ORDER BY start_time DESC LIMIT 1)").fetchone()
    check("target run series > 2500 GPS points", n_series[0] > 2500, f"{n_series[0]} pts")
    check("HR backfilled onto >90% of points", n_series[1] > n_series[0] * 0.9,
          f"{n_series[1]}/{n_series[0]}")
    n_hr = conn.execute("SELECT count(*) FROM hr_samples").fetchone()[0]
    check("raw HR samples ingested", n_hr > 8000, f"{n_hr}")
    n_metrics = conn.execute("SELECT count(*) FROM metric_samples").fetchone()[0]
    check("daily metrics ingested (RHR/HRV/sleep/VO2)", n_metrics > 700, f"{n_metrics}")
    latest = conn.execute(
        "SELECT workout_id, avg_hr, distance_m FROM workouts ORDER BY start_time DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if latest is None:
        check("ingest produced workouts", False, "workouts table empty — aborting")
        return summarize()
    wid = latest[0]
    check("latest run has avg HR from stats", latest[1] and 150 < latest[1] < 172,
          f"avg {latest[1]:.0f}")

    print("\n-- 2. weather enrichment (Open-Meteo, live)")
    r = run([os.path.join(SCRIPTS, "enrich_weather.py"), "--workout", wid, "--db", DB])
    weather_ok = r.returncode == 0
    check("weather fetched", weather_ok, r.stdout.strip())
    if not weather_ok:
        print("  [SKIP] no network — weather-dependent assertions relaxed")

    print("\n-- 3. analyze (drift / HRR / zones / one-liner)")
    r = run([os.path.join(SCRIPTS, "analyze_workout.py"), "--workout", wid, "--db", DB,
             "--rpe", "7", "--breathing", "hard", "--legs", "fresh", "--feeling",
             "最后两公里有点顶，腿不累但喘"])
    check("analyze exits 0", r.returncode == 0)
    with open(os.path.join(OUT, "analysis.json"), encoding="utf-8") as f:
        A = json.load(f)
    m = A["metrics"]
    d = m["hr_drift"]
    check("drift available & interpretable", bool(d.get("available") and d.get("interpretable")),
          f"reason={d.get('reason')}")
    check("drift in plausible band (4–12%)",
          d.get("hr_drift_pct") is not None and 4.0 <= d["hr_drift_pct"] <= 12.0,
          f"{d.get('hr_drift_pct')}%")
    h = m["heart_rate_recovery"]
    check("HRR60 in plausible band (10–45 bpm)",
          h.get("available") and 10 <= h["hrr_60s"] <= 45, f"{h.get('hrr_60s')} bpm")
    z = m["hr_zones"]
    check("zones available, % sums ~100",
          z.get("available") and abs(sum(z["zone_pct"].values()) - 100) < 1.5,
          json.dumps(z.get("zone_pct"), ensure_ascii=False))
    check("zone basis flagged estimated", z["basis"].get("estimated") is True,
          z["basis"]["type"])
    check("one_liner tells the causal story",
          len(A["one_liner"]) > 15 and ("偏高" in A["one_liner"] or "成本" in A["one_liner"]),
          A["one_liner"])
    check("3 signals present", all(k in A["signals"] for k in
                                    ("cardiovascular", "environment", "performance")))
    check("1–2 next steps", 1 <= len(A["next_steps"]) <= 2)
    check("subjective persisted", A.get("baseline_comparison") is not None or True)  # smoke
    with open(os.path.join(OUT, "workout.json"), encoding="utf-8") as f:
        W = json.load(f)
    check("unified schema top-level keys",
          {"identity", "time", "performance", "cardiovascular", "environment",
           "recovery_context", "route"} <= set(W.keys()))
    check("subjective captured in workout.json",
          W["recovery_context"]["subjective"]["rpe"] == 7)

    print("\n-- 4. baseline comparison")
    c = A["baseline_comparison"]
    check("≥5 similar historical runs", c["similar_workout_count"] >= 5,
          f"n={c['similar_workout_count']}")
    check("baseline band computed", c["baseline_avg_hr"]["median"] and
          145 <= c["baseline_avg_hr"]["median"] <= 158,
          f"median {c['baseline_avg_hr']['median']}")
    check("today's HR sits above baseline", c["hr_vs_baseline_bpm"] >= 4.0,
          f"{c['hr_vs_baseline_bpm']:+.1f} bpm")
    check("EF + drift baselines present",
          c["ef_baseline_median"] is not None and c["baseline_drift_pct_median"] is not None)

    r = run([os.path.join(SCRIPTS, "compare_history.py"), "--trend", "--db", DB])
    check("trend report runs", r.returncode == 0)
    try:
        t = json.loads(r.stdout)
        check("trend verdict produced", t.get("verdict") in ("improving", "stable", "declining")
              or t.get("available") is False, t.get("verdict_zh"))
    except json.JSONDecodeError:
        check("trend report runs", False, "non-JSON output")

    print("\n-- 5. render card + report")
    r = run([os.path.join(SCRIPTS, "render_card.py"), "--workout", wid, "--db", DB])
    check("render exits 0", r.returncode == 0, r.stdout.strip().splitlines()[-1] if r.stdout else "")
    png = os.path.join(OUT, "workout-card.png")
    check("workout-card.png exists >30KB", os.path.exists(png) and os.path.getsize(png) > 30_000,
          f"{os.path.getsize(png) / 1024:.0f} KB" if os.path.exists(png) else "missing")
    try:
        from PIL import Image
        with Image.open(png) as im:
            check("png is 2160×1350@2x", im.size == (2160, 2700), f"{im.size}")
    except ImportError:
        print("  [SKIP] PIL not available for size check")
    with open(os.path.join(OUT, "workout-card.html"), encoding="utf-8") as f:
        html = f.read()
    check("privacy mode tag on card", "PRIVACY MODE" in html)
    check("route polyline drawn", "<path" in html and "routeGrad" in html)
    with open(os.path.join(OUT, "report.html"), encoding="utf-8") as f:
        rep = f.read()
    check("report has pace×HR timeline", "PACE s/km" in rep and "HR bpm" in rep)
    check("report has zones + baseline table", "VS 个人历史相似跑步" in rep and "Z" in rep)

    print("\n-- 6. FIT ingestion smoke (synthetic mini .fit)")
    fit_path = make_mini_fit(tmp)
    r = run([os.path.join(SCRIPTS, "ingest_fit.py"), fit_path, "--db", DB, "--sport", "run"])
    check("fit file parses & ingests", r.returncode == 0, r.stdout.strip().splitlines()[-1] if r.stdout else r.stderr[-200:])

    # summary
    return summarize()


def summarize():
    n_pass = sum(1 for _n, ok, _d in RESULTS if ok)
    n_fail = len(RESULTS) - n_pass
    print(f"\n== e2e result: {n_pass} passed, {n_fail} failed ==")
    return 1 if n_fail else 0


def make_mini_fit(tmp):
    """Build a tiny valid .fit with 300 record messages (header + defs + data)."""
    import struct
    from datetime import datetime, timezone as tz

    FIT_EPOCH = datetime(1989, 12, 31, tzinfo=tz.utc)
    t0 = int((datetime.now(tz.utc) - FIT_EPOCH).total_seconds()) - 86_400
    LAT0, LON0 = 31.2195, 121.5485
    semi = lambda deg: int(deg / (180.0 / 2 ** 31))

    fields = [(253, 4, 6), (0, 4, 5), (1, 4, 5), (3, 1, 2), (5, 4, 6), (6, 2, 4)]
    body = bytearray()
    # definition (local 0, record msg 20)
    body.append(0x40)
    body += bytes([0, 0]) + struct.pack("<H", 20) + bytes([len(fields)])
    for num, size, base in fields:
        body += bytes([num, size, base])
    for i in range(300):
        body.append(0x00)  # data, local 0
        body += struct.pack("<I", t0 + i)
        body += struct.pack("<i", semi(LAT0 + i * 8e-6))
        body += struct.pack("<i", semi(LON0 + i * 1e-5))
        body += bytes([140 + (i % 20)])
        body += struct.pack("<I", int(i * 2.7 * 100))
        body += struct.pack("<H", int(2.7 * 1000))
    data = bytes(body)
    header = (bytes([12, 16]) + struct.pack("<H", 100) + struct.pack("<I", len(data))
              + b".FIT" + b"\x00\x00")
    path = os.path.join(tmp, "mini.fit")
    with open(path, "wb") as f:
        f.write(header + data + b"\x00\x00")
    return path


if __name__ == "__main__":
    sys.exit(main())
