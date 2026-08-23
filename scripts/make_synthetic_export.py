#!/usr/bin/env python3
"""Generate a synthetic Apple Health export.zip for testing & demo purposes.

Builds a believable Shanghai runner: a "target" evening run 2 days ago
(8.2 km, hot & humid, +HR drift) plus ~14 historical runs over 130 days
that form the personal baseline, daily resting-HR / HRV / VO2max / sleep
records, and two cross-source duplicates to exercise dedup.

Usage:
  python make_synthetic_export.py --out /tmp/eha_e2e/export.zip [--hist 14]
"""
from __future__ import annotations

import argparse
import io
import math
import random
import zipfile
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai
LAT0, LON0 = 31.2195, 121.5485     # 世纪公园-ish loop
M_PER_DEG = 111320.0
WATCH_DEV = "<<HKDevice: name:Apple Watch, manufacturer:Apple, model:Watch10,3>>"


def local_now() -> datetime:
    return datetime.now(TZ)


def apl(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S +0800")


def gpx_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gen_route(rng, start_local: datetime, dist_m: float, speed_fn, total_s: int):
    """-> list of (t_local, lat, lon, alt, speed_mps, dist_cum, hr_fn_applied_later)."""
    r = dist_m / (2 * math.pi)
    pts = []
    theta = 0.0
    x, y = r, 0.0
    d_carry = 0.0
    for i in range(int(total_s)):
        v = speed_fn(i / total_s, i)
        r_t = r * (1 + 0.03 * math.sin(2 * theta))
        theta += v / r_t
        x = r_t * math.cos(theta)
        y = r_t * math.sin(theta)
        lat = LAT0 + y / M_PER_DEG
        lon = LON0 + x / (M_PER_DEG * math.cos(math.radians(LAT0)))
        alt = 4 + 3 * math.sin(2 * theta)
        pts.append((start_local + timedelta(seconds=i), lat, lon, alt, v, i * v))
    return pts


def make_hr_target(total_s: int):
    """Target run HR: warm-up ramp → convex drift 152→172 → cooldown → post-end decay."""
    warm_s, cool_start = int(total_s * 0.09), int(total_s * 0.965)

    def hr(secs: float, rng) -> float:
        if secs <= warm_s:
            return 128 + 24 * secs / warm_s + rng.gauss(0, 1.5)
        if secs > total_s:  # watch keeps recording 150 s past the end (HRR window)
            return max(95, 150 - 90 * (secs - total_s) / 150.0) + rng.gauss(0, 1.2)
        if secs >= cool_start:
            return 168 - 18 * (secs - cool_start) / max(1, total_s - cool_start) + rng.gauss(0, 1.5)
        p = (secs - warm_s) / (cool_start - warm_s)
        return 152 + 20 * p ** 1.7 + rng.gauss(0, 1.2)
    return hr


def make_hr_hist(total_s: int, base: float):
    warm_s, cool_start = int(total_s * 0.1), int(total_s * 0.96)

    def hr(secs: float, rng) -> float:
        if secs <= warm_s:
            return 124 + (base - 10 - 124) * secs / warm_s + rng.gauss(0, 1.5)
        if secs > total_s:
            return max(92, base - 55 * (secs - total_s) / 150.0) + rng.gauss(0, 1.2)
        if secs >= cool_start:
            return base + 2 - 14 * (secs - cool_start) / max(1, total_s - cool_start) + rng.gauss(0, 1.5)
        p = (secs - warm_s) / (cool_start - warm_s)
        return base + 4 * p + rng.gauss(0, 1.5)   # mild +2-3% drift
    return hr


def speed_target(frac: float, i: int, rng=None):
    if frac < 0.09:
        return 2.15
    if frac > 0.965:
        return 2.1
    return 2.75 * (1 + 0.012 * math.sin(i * 0.7) + 0.008 * math.sin(i * 2.3))


def build(args):
    rng = random.Random(2026)
    now = local_now()
    target_start = (now - timedelta(days=2)).replace(hour=18, minute=30, second=0, microsecond=0)

    recs = []       # (type, sourceName, device, startDate, endDate, value, unit)
    workouts = []   # dicts
    routes = {}     # filename -> gpx str

    def add_hr_samples(start_local, total_s, hr_fn, source="Apple Watch"):
        t = start_local
        while t <= start_local + timedelta(seconds=total_s + 150):
            v = hr_fn((t - start_local).total_seconds(), rng)
            recs.append(("HKQuantityTypeIdentifierHeartRate", source, WATCH_DEV,
                         t, t, f"{v:.0f}", "count/min"))
            t += timedelta(seconds=5)

    def add_gpx_route(wid, pts):
        fn = f"route_{wid}.gpx"
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<gpx version="1.1" creator="Apple Health Export" '
                 'xmlns="http://www.topografix.com/GPX/1/1">',
                 "<trk><name>Run</name><trkseg>"]
        for (t, lat, lon, alt, _v, _d) in pts:
            lines.append(f'<trkpt lat="{lat:.6f}" lon="{lon:.6f}">'
                         f"<ele>{alt:.1f}</ele><time>{gpx_time(t)}</time></trkpt>")
        lines.append("</trkseg></trk></gpx>")
        routes[fn] = "\n".join(lines)
        return fn

    # --- target run -------------------------------------------------------
    tgt_total = 3138  # ~52:18
    tgt_dist = 8200.0
    tgt_pts = gen_route(rng, target_start, tgt_dist, lambda f, i: speed_target(f, i), tgt_total)
    tgt_hr = make_hr_target(tgt_total)
    add_hr_samples(target_start, tgt_total, tgt_hr)
    hrs = [tgt_hr(i, rng) for i in range(0, tgt_total, 5)]
    workouts.append(dict(
        start=target_start, total=tgt_total, dist=tgt_dist, avg_hr=sum(hrs) / len(hrs),
        max_hr=max(hrs), source="Apple Watch", device=WATCH_DEV, route=True,
        pts=tgt_pts, avg_speed=2.62))
    # Strava duplicate of the same session (no route)
    workouts.append(dict(
        start=target_start + timedelta(minutes=2), total=tgt_total + 300, dist=tgt_dist,
        avg_hr=None, max_hr=None, source="Strava", device="", route=False, pts=None,
        avg_speed=2.5, dupe_of=0))

    # --- historical baseline runs ------------------------------------------
    hist_temps_note = []
    for k in range(args.hist):
        day = now - timedelta(days=3 + int(130 * k / max(1, args.hist - 1)))
        start_local = day.replace(hour=18 + (k % 3), minute=25 + (k * 7) % 30, second=0, microsecond=0)
        dist = rng.uniform(7.5, 9.5) * 1000
        steady_v = rng.uniform(2.50, 2.85)
        total = int(dist / (steady_v * 0.94))
        base_hr = rng.uniform(146, 152)

        def sp(f, i, sv=steady_v):
            if f < 0.1:
                return sv * 0.8
            if f > 0.96:
                return sv * 0.78
            return sv * (1 + 0.012 * math.sin(i * 0.6))
        pts = gen_route(rng, start_local, dist, sp, total) if k % 7 != 6 else None
        hist_hr = make_hr_hist(total, base_hr)
        add_hr_samples(start_local, total, hist_hr)
        hrs = [hist_hr(i, rng) for i in range(0, total, 5)]
        workouts.append(dict(
            start=start_local, total=total, dist=dist, avg_hr=sum(hrs) / len(hrs),
            max_hr=max(hrs), source="Apple Watch", device=WATCH_DEV, route=pts is not None,
            pts=pts, avg_speed=dist / total))
        hist_temps_note.append((start_local.date().isoformat(), round(dist / 1000, 1),
                                round(3600 / steady_v / 60, 1)))
        # iPhone duplicate for one mid-history run
        if k == 3:
            workouts.append(dict(
                start=start_local + timedelta(minutes=3), total=total + 240, dist=dist,
                avg_hr=None, max_hr=None, source="iPhone", device="<<HKDevice: name:iPhone>>",
                route=False, pts=None, avg_speed=dist / (total + 240), dupe_of=k))

    # --- daily metrics ------------------------------------------------------
    d = now - timedelta(days=130)
    while d <= now:
        recs.append(("HKQuantityTypeIdentifierRestingHeartRate", "Apple Watch", WATCH_DEV,
                     d.replace(hour=7, minute=30), d.replace(hour=7, minute=30),
                     f"{rng.uniform(51, 56):.0f}", "count/min"))
        recs.append(("HKQuantityTypeIdentifierHeartRateVariabilitySDNN", "Apple Watch", WATCH_DEV,
                     d.replace(hour=7, minute=35), d.replace(hour=7, minute=35),
                     f"{rng.uniform(42, 50):.0f}", "ms"))
        # sleep: 4 x ~100 min asleep intervals ending ~07:10
        cur = d.replace(hour=1, minute=0)
        for seg in range(4):
            dur = rng.uniform(85, 115)
            e = cur + timedelta(minutes=dur)
            recs.append(("HKCategoryTypeIdentifierSleepAnalysis", "Apple Watch", WATCH_DEV,
                         cur, e, "HKCategoryValueSleepAnalysisAsleepCore", ""))
            cur = e + timedelta(minutes=rng.uniform(8, 20))
        if d.day == 1:
            recs.append(("HKQuantityTypeIdentifierVO2Max", "Apple Watch", WATCH_DEV,
                         d.replace(hour=8), d.replace(hour=8), f"{rng.uniform(46.5, 48.9):.1f}",
                         "ml/kg·min"))
        d += timedelta(days=1)

    # --- assemble XML --------------------------------------------------------
    out = io.StringIO()
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    out.write(f'<HealthData locale="zh_CN" exportDate="{apl(now)}">\n')
    def xattr(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    for (typ, src, dev, s, e, val, unit) in sorted(recs, key=lambda r: r[3]):
        end_attr = f' endDate="{apl(e)}"'
        unit_attr = f' unit="{unit}"' if unit else ""
        dev_attr = f' device="{xattr(dev)}"' if dev else ""
        out.write(f'<Record type="{typ}" sourceName="{src}"{dev_attr}{unit_attr} '
                  f'startDate="{apl(s)}"{end_attr} value="{val}"/>\n')
    for idx, w in enumerate(workouts):
        route_meta = ""
        if w.get("route") and w.get("pts"):
            stamp = w["start"].strftime("%Y%m%d_%H%M")
            fn = add_gpx_route(f"{stamp}_{idx}", w["pts"])
            route_meta = (f'<MetadataEntry key="com.apple.health.workout-route" '
                          f'value="../workout-routes/{fn}"/>')
        avg_attr = f' average="{w["avg_hr"]:.1f}"' if w.get("avg_hr") else ""
        max_attr = f' maximum="{w.get("max_hr") or 0:.0f}"' if w.get("max_hr") else ""
        dev_attr = f' device="{xattr(w["device"])}"' if w["device"] else ""
        km_pace = 3600 / (w["avg_speed"] * 3.6) / 60 if w["avg_speed"] else None
        out.write(
            f'<Workout workoutActivityType="HKWorkoutActivityTypeRunning" '
            f'duration="{w["total"]}" startDate="{apl(w["start"])}" '
            f'endDate="{apl(w["start"] + timedelta(seconds=w["total"]))}" '
            f'totalDistance="{w["dist"] / 1000:.2f}" sourceName="{w["source"]}"{dev_attr}>\n'
            f'<WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate"{avg_attr}{max_attr} '
            f'unit="count/min"/>\n'
            f'<WorkoutStatistics type="HKQuantityTypeIdentifierRunningSpeed" '
            f'average="{w["avg_speed"] * 3.6:.2f}" maximum="{w["avg_speed"] * 3.6 * 1.25:.2f}" '
            f'unit="km/h"/>\n'
            f'<WorkoutStatistics type="HKQuantityTypeIdentifierElevationAscended" '
            f'sum="{rng.uniform(20, 90):.0f}" unit="m"/>\n'
            f"{route_meta}\n</Workout>\n")
    out.write("</HealthData>\n")
    return out.getvalue(), routes, len(workouts)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, help="path of export.zip to write")
    ap.add_argument("--hist", type=int, default=14)
    args = ap.parse_args()

    xml, routes, n_wk = build(args)
    import os
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("导出.xml", xml)
        for fn, content in routes.items():
            zf.writestr(f"workout-routes/{fn}", content)
    print(f"[fixture] {args.out}: {n_wk} workouts ({len(routes)} GPX routes), "
          f"xml {len(xml) / 1024:.0f} KB")


if __name__ == "__main__":
    main()
