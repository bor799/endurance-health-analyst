#!/usr/bin/env python3
"""Render a square workout share card (PNG) and the pace × HR report (HTML).

An optional local photo is the card hero; the privacy-trimmed route is projected
onto it as a visual perspective trace. Privacy is ON by default and fails closed:
short or fully trimmed routes are hidden rather than restored.

Usage:
  python render_card.py --workout latest:run [--photo IMG.HEIC] [--privacy off]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eha_lib as E  # noqa: E402
import card_visual as V  # noqa: E402

CARD_W, CARD_H = V.CARD_W, V.CARD_H
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]

ZONE_COLORS = ["#334155", "#4ADE80", "#FBBF24", "#FB923C", "#F87171"]
ZONE_TEXT = ["#94A3B8", "#0B0E13", "#0B0E13", "#0B0E13", "#0B0E13"]


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------

def gather(conn, workout_id: str):
    wrow = E.get_workout_row(conn, workout_id)
    if not wrow:
        return None
    w = dict(zip(E.WORKOUT_COLS, wrow))
    analysis = conn.execute("SELECT analysis FROM analyses WHERE workout_id = ?",
                            [workout_id]).fetchone()
    analysis = json.loads(analysis[0]) if analysis else None
    route = conn.execute(
        "SELECT ts, lat, lon, alt FROM route_points WHERE workout_id = ? ORDER BY ts",
        [workout_id]).fetchall()
    series = conn.execute(
        "SELECT ts, speed_mps, pace_s_per_km, hr FROM series WHERE workout_id = ? ORDER BY ts",
        [workout_id]).fetchall()
    weather = E.get_weather(conn, workout_id)
    return {"w": w, "analysis": analysis, "route": route, "series": series,
            "weather": weather}


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------

def privacy_trim(points, cfg, enabled=True):
    """Return (safe points, status), failing closed whenever privacy is on.

    Status is one of: off, trimmed, fully_hidden, no_route. In privacy mode a
    short route or a route fully consumed by the trim radius is hidden; the
    original coordinates are never restored as a fallback.
    """
    if not points:
        return [], "no_route"
    if not enabled:
        return list(points), "off"
    if len(points) < 10:
        return [], "fully_hidden"
    p = cfg["privacy"]
    r0 = (points[0][1], points[0][2])
    r1 = (points[-1][1], points[-1][2])
    rs = max(p.get("start_trim_m", p.get("trim_radius_m", 300)), 1)
    re_ = max(p.get("end_trim_m", p.get("trim_radius_m", 300)), 1)
    kept = []
    for ts, lat, lon, alt in points:
        if E.haversine_m(lat, lon, *r0) < rs or E.haversine_m(lat, lon, *r1) < re_:
            continue
        kept.append((ts, lat, lon, alt))
    return (kept, "trimmed") if kept else ([], "fully_hidden")


# ---------------------------------------------------------------------------
# Privacy-safe local route projection
# ---------------------------------------------------------------------------

def project_route_xy(points, pad=0.06):
    """Project safe geo points into normalized local XY chunks.

    Call only after privacy_trim(). Raw coordinates stop at this boundary;
    card_visual receives only the returned 0..1 XY paths.
    """
    if not points:
        return []
    lat0 = sum(p[1] for p in points) / len(points)
    k = math.cos(math.radians(lat0))
    xs = [p[2] * k for p in points]
    ys = [p[1] for p in points]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    sx = (1 - 2 * pad) / max(x1 - x0, 1e-9)
    sy = (1 - 2 * pad) / max(y1 - y0, 1e-9)
    scale = min(sx, sy)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    xy = [(0.5 + (x - cx) * scale, 0.5 - (y - cy) * scale)
          for x, y in zip(xs, ys)]
    chunks, cur = [], [xy[0]]
    for i in range(1, len(points)):
        dt = (points[i][0] - points[i - 1][0]).total_seconds()
        dist = E.haversine_m(points[i - 1][1], points[i - 1][2],
                             points[i][1], points[i][2])
        if dt > 8 or dist > 120:
            if len(cur) >= 2:
                chunks.append(cur)
            cur = []
        cur.append(xy[i])
    if len(cur) >= 2:
        chunks.append(cur)
    return chunks


def privacy_label(status):
    return {
        "trimmed": "起终点已隐藏",
        "fully_hidden": "路线因隐私保护未展示",
        "off": "隐私模式已关闭",
        "no_route": "本次无 GPS 轨迹",
    }[status]


def build_card_spec(ctx, route_paths, privacy_status, photo_png=None):
    """Build the renderer contract with no geo coordinates or source paths."""
    w = ctx["w"]
    a = ctx["analysis"] or {}
    st = w["start_time"].replace(tzinfo=timezone.utc).astimezone()
    wx = ctx["weather"]
    return {
        "photo_png": photo_png,
        "route_paths": route_paths,
        "privacy_status": privacy_status,
        "privacy_tag": privacy_label(privacy_status),
        "sport_label": {"run": "R U N", "ride": "R I D E", "swim": "S W I M"}.get(
            w["sport_type"], w["sport_type"].upper()),
        "date_str": st.strftime("%Y.%m.%d  %H:%M"),
        "distance": E.fmt_km(w["distance_m"]),
        "duration": E.fmt_duration(w["duration_s"]),
        "pace": E.fmt_pace(w["avg_pace_s_per_km"]),
        "avg_hr": f"{w['avg_hr']:.0f}" if w["avg_hr"] else "–",
        "one_liner": a.get("one_liner", "今天完成了一次训练。"),
        "foot_sub": (f"{w['sport_type']} · {st.strftime('%H:%M')} 出发 · "
                     + (f"{wx['temperature_c']:.0f}°C RH {wx['humidity_pct']:.0f}%"
                        if wx and wx.get("temperature_c") is not None else "天气未记录")),
    }


# ---------------------------------------------------------------------------
# Pace × HR timeline SVG (report)
# ---------------------------------------------------------------------------

def timeline_svg(series, analysis, w=952, h=340):
    """Two stacked panes sharing a time axis: pace (top, inverted) and HR (bottom)."""
    if not series or len(series) < 10:
        return "<div style='color:#7C8AA0;font-size:20px'>时间序列数据不足</div>"
    t0 = series[0][0]
    stride = max(1, len(series) // 650)
    pts = series[::stride]
    t_end = max((p[0] - t0).total_seconds() for p in pts) or 1.0
    paces = [p[2] for p in pts if p[2]]
    hrs = [p[3] for p in pts if p[3]]
    if not paces and not hrs:
        return "<div style='color:#7C8AA0;font-size:20px'>时间序列数据不足</div>"
    pad_l, pad_r, top_h, gap, bot_h = 46, 16, 130, 44, 130

    def X(t):
        return pad_l + t / t_end * (w - pad_l - pad_r)

    def pace_y(pace):
        lo, hi = 240, 900  # 4'00"-15'00" window
        v = min(max(pace, lo), hi)
        return top_h - (v - lo) / (hi - lo) * (top_h - 18)

    def hr_y(hr):
        lo, hi = 90, 195
        v = min(max(hr, lo), hi)
        return top_h + gap + bot_h - (v - lo) / (hi - lo) * (bot_h - 18)

    pace_d = "".join(f"{X((p[0] - t0).total_seconds()):.1f},{pace_y(p[2]):.1f} "
                     for p in pts if p[2])
    hr_d = "".join(f"{X((p[0] - t0).total_seconds()):.1f},{hr_y(p[3]):.1f} "
                   for p in pts if p[3])
    steady_fill = ""
    drift = (analysis or {}).get("metrics", {}).get("hr_drift", {})
    if drift.get("available"):
        steady_fill = (
            f'<rect x="{X(drift.get("steady_t0_s", 0)):.0f}" y="10" '
            f'width="{max(0, X(drift.get("steady_t1_s", t_end)) - X(drift.get("steady_t0_s", 0))):.0f}" '
            f'height="{top_h + gap + bot_h - 10}" fill="#182238" opacity="0.55"/>')
    mid = drift.get("half_split_s")
    split_line = (f'<line x1="{X(mid):.0f}" y1="8" x2="{X(mid):.0f}" '
                  f'y2="{top_h + gap + bot_h}" stroke="#2E3B52" stroke-dasharray="6 6"/>'
                  if mid else "")
    minutes = int(t_end // 60)
    xticks = "".join(
        f'<line x1="{X(m * 60):.0f}" y1="{top_h + gap + bot_h}" x2="{X(m * 60):.0f}" '
        f'y2="{top_h + gap + bot_h + 6}" stroke="#3A445A"/>'
        f'<text x="{X(m * 60):.0f}" y="{top_h + gap + bot_h + 26}" fill="#7C8AA0" font-size="14" '
        f'text-anchor="middle">{m}m</text>'
        for m in range(5, minutes, max(5, minutes // 6)))
    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">'
        f"{steady_fill}{split_line}"
        f'<text x="6" y="26" fill="#60A5FA" font-size="15" font-weight="700">PACE s/km</text>'
        f'<polyline points="{pace_d}" fill="none" stroke="#60A5FA" stroke-width="2.5"/>'
        f'<text x="6" y="{top_h + gap - 18}" fill="#F87171" font-size="15" font-weight="700">HR bpm</text>'
        f'<polyline points="{hr_d}" fill="none" stroke="#F87171" stroke-width="2.5"/>'
        f"{xticks}</svg>"
    )


# ---------------------------------------------------------------------------
# HTML assembly
# ---------------------------------------------------------------------------

def build_report_html(ctx):
    w = ctx["w"]
    a = ctx["analysis"] or {}
    st = w["start_time"].replace(tzinfo=timezone.utc).astimezone()
    m = a.get("metrics", {})
    zones = m.get("hr_zones", {})
    cmp_ = a.get("baseline_comparison") or {}

    # timeline shading relies on steady-window offsets stored by analyze_workout
    series = [(r[0], r[1], r[2], r[3]) for r in ctx["series"]]
    tmp_analysis = a

    zones_bar, legend = "<div style='color:#7C8AA0'>区间数据不足</div>", ""
    if zones.get("available"):
        zsec = zones["zone_seconds"]
        zpct = zones["zone_pct"] or {k: 0 for k in zsec}
        total = sum(zsec.values()) or 1
        cells = []
        for i, name in enumerate(["Z1", "Z2", "Z3", "Z4", "Z5"]):
            width = zsec.get(name, 0) / total * 100
            if width <= 0:
                continue
            cells.append(f'<div style="width:{width:.1f}%;background:{ZONE_COLORS[i]};'
                         f'color:{ZONE_TEXT[i]}">{name}</div>')
            mins = zsec.get(name, 0) / 60
            legend += f"<span><b style='color:{ZONE_COLORS[i]}'>■</b> {name} {mins:.0f}min ({zpct.get(name, 0):.0f}%)</span>"
        zones_bar = f'<div class="zones">{"".join(cells)}</div>'
    basis = zones.get("basis") or {}
    basis_kv = (f"<span>区间依据 <b>{basis.get('type', '–')}</b></span>"
                f"<span>{'估计值 estimated' if basis.get('estimated') else '实测值'}</span>"
                + (f"<span>max HR <b>{basis['max_hr']:.0f}</b></span>" if basis.get("max_hr") else "")
                + (f"<span>LTHR <b>{basis['lthr']:.0f}</b></span>" if basis.get("lthr") else ""))

    rows = ""
    base = cmp_.get("baseline_avg_hr") or {}
    delta = cmp_.get("hr_vs_baseline_bpm")
    for s in cmp_.get("similar_recent", []):
        rows += (f"<tr><td>{s['date']}</td><td>{s['km']} km</td><td>{s['pace']}</td>"
                 f"<td>{s['avg_hr']:.0f} bpm</td>"
                 f"<td>{s['temp_c'] if s['temp_c'] is not None else '–'}°C</td></tr>")
    delta_html = ""
    if delta is not None:
        cls = ' class="hl"' if delta >= 5 else ""
        delta_html = (f"<div class='kv' style='margin-top:20px'>"
                      f"<span>今天平均 HR <b>{(cmp_.get('today_avg_hr') or 0):.0f}</b></span>"
                      f"<span>相似跑步基线 <b>{base.get('median', '–')}</b> "
                      f"(P25 {base.get('p25', '–')}–P75 {base.get('p75', '–')}, n={base.get('n', 0)})</span>"
                      f"<span>差值 <b{cls}>{delta:+.1f} bpm</b></span></div>")
    baseline_block = (
        f"<table><tr><th>日期</th><th>距离</th><th>配速</th><th>平均心率</th><th>气温</th></tr>{rows}</table>"
        + delta_html if rows else "<div style='color:#7C8AA0;font-size:20px'>历史相似跑步不足</div>")

    steps = "".join(f"<li>{s}</li>" for s in a.get("next_steps", []))
    drift = m.get("hr_drift") or {}
    note = ("速度基本持平而心率持续爬升 = 生理成本上升的信号。阴影为稳定有氧段，虚线为前后半程分界。"
            if drift.get("available") else "稳定段不足，未计算漂移。")
    with open(os.path.join(E.SKILL_ROOT, "templates", "report.html"), encoding="utf-8") as f:
        html = f.read()
    avghr_txt = f"{w['avg_hr']:.0f} bpm" if w["avg_hr"] else "–"
    subs = {
        "TITLE": f"{st.strftime('%Y-%m-%d')} 跑步报告",
        "SUBTITLE": f"{E.fmt_km(w['distance_m'])} km · {E.fmt_duration(w['duration_s'])} · "
                    f"{E.fmt_pace(w['avg_pace_s_per_km'])} · {avghr_txt} · source {w['source']}",
        "ONE_LINER": a.get("one_liner", ""),
        "TIMELINE_SVG": timeline_svg(series, tmp_analysis),
        "TIMELINE_NOTE": note,
        "ZONES_BAR": zones_bar, "ZONES_LEGEND": legend, "ZONE_BASIS": basis_kv,
        "BASELINE_BLOCK": baseline_block,
        "STEPS": steps,
    }
    for k, v in subs.items():
        html = html.replace("{{" + k + "}}", str(v))
    return html


# ---------------------------------------------------------------------------
# PNG via Chrome, fallback Pillow
# ---------------------------------------------------------------------------

def find_chrome():
    if os.environ.get("CHROME_PATH") and os.path.exists(os.environ["CHROME_PATH"]):
        return os.environ["CHROME_PATH"]
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    return shutil.which("google-chrome") or shutil.which("chromium")


def chrome_screenshot(html_path: str, png_path: str, w=CARD_W, h=CARD_H, scale=2,
                      timeout_s=25.0) -> bool:
    """Best-effort Chrome headless render. Some machines hang in headless mode —
    after a failure a marker file makes later calls skip straight to Pillow."""
    marker = os.path.join(E.DATA_DIR, ".chrome_headless_broken")
    if os.path.exists(marker):
        return False
    chrome = find_chrome()
    if not chrome:
        return False
    import tempfile
    profile = tempfile.mkdtemp(prefix="eha_chrome_")
    cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--no-first-run", "--no-default-browser-check",
           f"--user-data-dir={profile}",
           "--force-device-scale-factor", str(scale),
           "--window-size", f"{w},{h}",
           "--default-background-color=00000000",
           "--virtual-time-budget=2500",
           f"--screenshot={png_path}", f"file://{html_path}"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
        ok = r.returncode == 0 and os.path.exists(png_path) and os.path.getsize(png_path) > 5000
        if not ok:
            os.makedirs(E.DATA_DIR, exist_ok=True)
            open(marker, "w").write("chrome headless screenshot failed; delete to retry\n")
            if r.stderr:
                sys.stderr.write(r.stderr.decode("utf-8", "ignore")[:400] + "\n")
        return ok
    except (subprocess.TimeoutExpired, OSError):
        os.makedirs(E.DATA_DIR, exist_ok=True)
        open(marker, "w").write("chrome headless screenshot timed out; delete to retry\n")
        return False
    finally:
        shutil.rmtree(profile, ignore_errors=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workout", default="latest:run")
    parser.add_argument("--photo", default=None, help="local JPEG/PNG/HEIC used as the card hero")
    parser.add_argument("--privacy", choices=["on", "off"], default=None,
                        help="default: config.privacy.default_on")
    parser.add_argument("--renderer", choices=["auto", "chrome", "pillow"], default="auto")
    parser.add_argument("--out", default=None, help="default output/workout-card.png")
    parser.add_argument("--no-png", action="store_true", help="only write card/report HTML")
    parser.add_argument("--retry-chrome", action="store_true",
                        help="retry Chrome headless even if previously marked broken")
    parser.add_argument("--db", default=E.DEFAULT_DB)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    cfg = E.load_config()
    out_dir = E.ensure_output_dir()
    card_html_path = os.path.join(out_dir, "workout-card.html")
    report_path = os.path.join(out_dir, "report.html")
    default_png_path = os.path.join(out_dir, "workout-card.png")
    png_path = args.out or default_png_path

    # Canonical paths always describe the latest attempt. Remove prior generated
    # artifacts up front so a failed privacy-on render cannot leave an older
    # privacy-off card looking current.
    for stale in {card_html_path, report_path, default_png_path, os.path.abspath(png_path)}:
        try:
            os.remove(stale)
        except FileNotFoundError:
            pass

    conn = E.connect(args.db)
    wid = E.resolve_workout(conn, args.workout)
    if not wid:
        conn.close()
        print("[render] workout not found", file=sys.stderr)
        return 1
    ctx = gather(conn, wid)
    conn.close()

    privacy_on = (args.privacy == "on") if args.privacy else cfg["privacy"].get("default_on", True)
    safe_route, privacy_status = privacy_trim(ctx["route"], cfg, privacy_on)
    route_paths = project_route_xy(safe_route)
    # Raw geo data stops here. The renderer contract contains local XY only.
    ctx.pop("route", None)
    try:
        photo_png = V.normalize_photo(args.photo) if args.photo else None
    except ValueError as exc:
        print(f"[render] {exc}; no output was published", file=sys.stderr)
        return 2
    spec = build_card_spec(ctx, route_paths, privacy_status, photo_png)

    template = os.path.join(E.SKILL_ROOT, "templates", "workout-card.html")
    temp_dir = tempfile.mkdtemp(prefix=".eha-render-", dir=out_dir)
    tmp_card = os.path.join(temp_dir, "workout-card.html")
    tmp_report = os.path.join(temp_dir, "report.html")
    tmp_png = os.path.join(temp_dir, "workout-card.png")
    try:
        with open(tmp_card, "w", encoding="utf-8") as f:
            f.write(V.build_html(spec, template))
        with open(tmp_report, "w", encoding="utf-8") as f:
            f.write(build_report_html(ctx))

        if args.no_png:
            os.replace(tmp_card, card_html_path)
            os.replace(tmp_report, report_path)
            print(f"[render] html → {card_html_path}, {report_path}")
            return 0

        if args.retry_chrome:
            marker = os.path.join(E.DATA_DIR, ".chrome_headless_broken")
            if os.path.exists(marker):
                os.remove(marker)

        ok, how = False, args.renderer
        if args.renderer in ("auto", "chrome"):
            ok = chrome_screenshot(tmp_card, tmp_png)
            how = "chrome headless"
        if not ok and args.renderer in ("auto", "pillow"):
            try:
                V.render_pillow(spec, tmp_png)
                ok, how = True, "pillow"
            except (ImportError, OSError, ValueError) as exc:
                print(f"[render] pillow failed: {exc}", file=sys.stderr)
        if not ok:
            print("[render] PNG failed; no canonical output was published", file=sys.stderr)
            return 2

        os.makedirs(os.path.dirname(os.path.abspath(png_path)), exist_ok=True)
        os.replace(tmp_card, card_html_path)
        os.replace(tmp_report, report_path)
        os.replace(tmp_png, png_path)
        print(f"[render] html → {card_html_path}, {report_path}")
        kb = os.path.getsize(png_path) / 1024
        print(f"[render] png → {png_path} ({kb:.0f} KB, {how}, privacy={privacy_status})")
        return 0
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
