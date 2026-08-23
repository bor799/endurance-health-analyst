#!/usr/bin/env python3
"""Render the Show Up workout card (PNG) and the pace × HR report (HTML).

Route is the visual hero: minimal dark map, gradient polyline, start/finish dots.
Privacy mode is ON by default — GPS points within config.privacy.trim_radius_m of
the start AND finish are removed before anything is drawn or exported.

PNG pipeline: HTML template -> Chrome headless screenshot; falls back to a
pure-Pillow renderer if Chrome is unavailable.

Usage:
  python render_card.py --workout latest:run [--privacy off] [--out output/workout-card.png]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eha_lib as E  # noqa: E402

CARD_W, CARD_H = 1080, 1350
MAP_W, MAP_H = 1080, 620
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
    """Drop GPS points within trim radius of start and finish (default 300 m)."""
    if not enabled or not points or len(points) < 10:
        return points, False
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
    return (kept or points), bool(kept and len(kept) < len(points))


# ---------------------------------------------------------------------------
# Route -> SVG
# ---------------------------------------------------------------------------

def project_paths(points, w, h, pad=70.0):
    """Equirectangular projection fitted to the box. Returns list of SVG path 'd' strings."""
    if not points:
        return []
    lat0 = sum(p[1] for p in points) / len(points)
    k = math.cos(math.radians(lat0))
    xs = [(p[2] * k) for p in points]
    ys = [p[1] for p in points]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    sx = (w - 2 * pad) / max(x1 - x0, 1e-9)
    sy = (h - 2 * pad) / max(y1 - y0, 1e-9)
    s = min(sx, sy)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    px = [w / 2 + (x - cx) * s for x in xs]
    py = [h / 2 - (y - cy) * s for y in ys]
    # split into contiguous chunks on >8s GPS gaps or >120m jumps
    chunks, cur = [], [(px[0], py[0])]
    for i in range(1, len(points)):
        dt = (points[i][0] - points[i - 1][0]).total_seconds()
        d = E.haversine_m(points[i - 1][1], points[i - 1][2], points[i][1], points[i][2])
        if dt > 8 or d > 120:
            chunks.append(cur)
            cur = []
        cur.append((px[i], py[i]))
    chunks.append(cur)
    paths = []
    for ch in chunks:
        if len(ch) < 2:
            continue
        d = f"M{ch[0][0]:.1f},{ch[0][1]:.1f}" + "".join(f"L{x:.1f},{y:.1f}" for x, y in ch[1:])
        paths.append(d)
    first, last = (px[0], py[0]), (px[-1], py[-1])
    return paths, first, last


def map_svg(points, w=MAP_W, h=MAP_H):
    if not points:
        return fallback_hero_svg(w, h)
    paths, first, last = project_paths(points, w, h)
    grid = []
    for gx in range(0, w, 90):
        grid.append(f'<line x1="{gx}" y1="0" x2="{gx}" y2="{h}" stroke="#161D2A" stroke-width="1"/>')
    for gy in range(0, h, 90):
        grid.append(f'<line x1="0" y1="{gy}" x2="{w}" y2="{gy}" stroke="#161D2A" stroke-width="1"/>')
    route_svg = "".join(
        f'<path d="{d}" fill="none" stroke="#FF5A3C" stroke-opacity="0.22" stroke-width="14" '
        f'stroke-linecap="round" stroke-linejoin="round"/>' for d in paths)
    route_svg += "".join(
        f'<path d="{d}" fill="none" stroke="url(#routeGrad)" stroke-width="5.5" '
        f'stroke-linecap="round" stroke-linejoin="round"/>' for d in paths)
    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">'
        f'<defs><linearGradient id="routeGrad" x1="0%" y1="0%" x2="100%" y2="100%">'
        f'<stop offset="0%" stop-color="#FF5A3C"/><stop offset="100%" stop-color="#FFB020"/>'
        f'</linearGradient></defs>'
        f'<rect width="{w}" height="{h}" fill="#10151D"/>'
        f'{"".join(grid)}'
        f'<circle cx="{w * 0.5}" cy="{h * 0.5}" r="{min(w, h) * 0.52}" fill="#0D1219"/>'
        f'{route_svg}'
        f'<circle cx="{first[0]:.1f}" cy="{first[1]:.1f}" r="9" fill="#4ADE80" stroke="#0B0E13" stroke-width="3"/>'
        f'<circle cx="{last[0]:.1f}" cy="{last[1]:.1f}" r="9" fill="#F8FAFC" stroke="#0B0E13" stroke-width="3"/>'
        f"</svg>"
    )


def fallback_hero_svg(w, h):
    return (
        f'<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg">'
        f'<rect width="{w}" height="{h}" fill="#10151D"/>'
        f'<text x="{w / 2}" y="{h / 2}" text-anchor="middle" fill="#4A5568" font-size="26">'
        f"无 GPS 轨迹（路线为分享卡主角，建议带表记录）</text></svg>"
    )


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

def _sig(analysis, key, default="–"):
    return ((analysis or {}).get("signals", {}).get(key)) or default


def build_card_html(ctx, cfg, privacy_on):
    w = ctx["w"]
    a = ctx["analysis"]
    st = w["start_time"].replace(tzinfo=timezone.utc).astimezone()
    dist = E.fmt_km(w["distance_m"])
    dur = E.fmt_duration(w["duration_s"])
    pace = E.fmt_pace(w["avg_pace_s_per_km"])
    avghr = f"{w['avg_hr']:.0f}" if w["avg_hr"] else "–"
    wx = ctx["weather"]
    env = _sig(a, "environment", "– 天气")
    drift_txt = _sig(a, "performance", "–")
    drift_txt = drift_txt.replace("Drift · ", "")
    with open(os.path.join(E.SKILL_ROOT, "templates", "workout-card.html"), encoding="utf-8") as f:
        html = f.read()
    subs = {
        "MAP_SVG": map_svg(ctx["route_trimmed"]),
        "SPORT_LABEL": {"run": "R U N", "ride": "R I D E", "swim": "S W I M"}.get(w["sport_type"], w["sport_type"].upper()),
        "PRIVACY_TAG": "PRIVACY MODE · 起终点已隐藏" if privacy_on else "",
        "DATE_STR": st.strftime("%Y.%m.%d  %H:%M"),
        "DIST": dist, "DUR": dur, "PACE": pace, "AVGHR": avghr,
        "SIG_CARDIO": _sig(a, "cardiovascular").replace("HR Cost · ", ""),
        "SIG_ENV": env.replace("Heat Load · ", ""),
        "SIG_PERF": drift_txt,
        "ONE_LINER": (a or {}).get("one_liner", "今天完成了一次跑步。"),
        "FOOT_SUB": (f"{w['sport_type']} · {st.strftime('%H:%M')} 出发 · "
                     + (f"{wx['temperature_c']:.0f}°C RH {wx['humidity_pct']:.0f}%"
                        if wx and wx.get("temperature_c") is not None else "天气未记录")),
    }
    for k, v in subs.items():
        html = html.replace("{{" + k + "}}", str(v))
    return html


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


def pillow_card(ctx, cfg, privacy_on, png_path):
    """Primary renderer without a browser (Chrome headless is best-effort only)."""
    from PIL import Image, ImageDraw, ImageFont

    S = 2  # supersample
    W, H = CARD_W * S, CARD_H * S
    img = Image.new("RGB", (W, H), "#0B0E13")
    d = ImageDraw.Draw(img)
    CJK = "/System/Library/Fonts/PingFang.ttc"
    LAT = "/System/Library/Fonts/Helvetica.ttc"

    def font(size, cjk=False):
        for path in ([CJK, LAT] if cjk else [LAT, CJK]):
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, int(size * S))
                except OSError:
                    continue
        return ImageFont.load_default()

    def text_w(s, f):
        return d.textlength(s, font=f)

    def wrap(s, f, max_px):
        lines, cur = [], ""
        for ch in s:
            if text_w(cur + ch, f) > max_px and cur:
                lines.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            lines.append(cur)
        return lines

    # ---- map hero ----------------------------------------------------------
    d.rectangle([0, 0, W, MAP_H * S], fill="#10151D")
    for gx in range(0, CARD_W, 90):
        d.line([(gx * S, 0), (gx * S, MAP_H * S)], fill="#161D2A", width=S)
    for gy in range(0, MAP_H, 90):
        d.line([(0, gy * S), (W, gy * S)], fill="#161D2A", width=S)
    pts = ctx["route_trimmed"]
    if pts:
        paths, first, last = project_paths([(p[0], p[1], p[2], p[3]) for p in pts], MAP_W, MAP_H)
        for path_d in paths:
            xy = [(float(seg[1:].split(",")[0]), float(seg[1:].split(",")[1]))
                  for seg in path_d.split("L")]
            scaled = [(x * S, y * S) for x, y in xy]
            d.line(scaled, fill="#FF5A3C", width=14 * S, joint="curve")  # glow underlay
            d.line(scaled, fill="#FF7A3C", width=5 * S, joint="curve")
        for (px, py), fill, ring in [(first, "#4ADE80", "#0B0E13"), (last, "#F8FAFC", "#0B0E13")]:
            x, y = px * S, py * S
            d.ellipse([x - 18 * S, y - 18 * S, x + 18 * S, y + 18 * S], fill=ring)
            d.ellipse([x - 13 * S, y - 13 * S, x + 13 * S, y + 13 * S], fill=fill)
    f_sport = font(22, cjk=True)
    d.text((36 * S, 28 * S), "R U N", fill="#5B6778", font=f_sport)
    if privacy_on:
        tag = "PRIVACY MODE · 起终点已隐藏"
        f_tag = font(19, cjk=True)
        d.text(((CARD_W - 36) * S - text_w(tag, f_tag), 28 * S), tag,
               fill="#4A5568", font=f_tag)

    # ---- header + stats ----------------------------------------------------
    w_, a = ctx["w"], ctx["analysis"] or {}
    st = w_["start_time"].replace(tzinfo=timezone.utc).astimezone()
    y = (MAP_H + 36) * S
    d.text((52 * S, y), st.strftime("%Y.%m.%d  %H:%M"), fill="#7C8AA0", font=font(26, cjk=True))
    y += 74 * S
    stats = [
        (E.fmt_km(w_["distance_m"]), "km", "距离", "#F1F5FB"),
        (E.fmt_duration(w_["duration_s"]), "", "时长", "#F1F5FB"),
        (E.fmt_pace(w_["avg_pace_s_per_km"]), "", "配速", "#F1F5FB"),
        (f"{w_['avg_hr']:.0f}" if w_["avg_hr"] else "–", "bpm", "平均心率", "#F87171"),
    ]
    col_w = (CARD_W - 104) / 4
    for i, (v, unit, label, color) in enumerate(stats):
        x = (52 + i * col_w) * S
        f_v = font(46)
        if text_w(v, f_v) > col_w * S * 0.9:
            f_v = font(int(46 * col_w * S * 0.9 / text_w(v, f_v)))
        d.text((x, y), v, fill=color, font=f_v)
        if unit:
            d.text((x + text_w(v, f_v) + 8 * S, y + 26 * S), unit, fill="#7C8AA0", font=font(22))
        d.text((x, y + 72 * S), label, fill="#7C8AA0", font=font(20, cjk=True))
    y += 132 * S

    # ---- signals panel -----------------------------------------------------
    panel_h = 232
    d.rounded_rectangle([52 * S, y, (CARD_W - 52) * S, (y + panel_h * S)],
                        radius=20 * S, fill="#121826")
    sy = y + 30 * S
    rows = [
        ("❤", "有氧负荷", _sig(a, "cardiovascular").replace("HR Cost · ", ""), "#F87171"),
        ("🌡", "环境热负荷", _sig(a, "environment").replace("Heat Load · ", ""), "#FBBF24"),
        ("⚡", "心率漂移", _sig(a, "performance").replace("Drift · ", ""), "#60A5FA"),
    ]
    for ico, label, val, color in rows:
        d.text((82 * S, sy - 4 * S), ico, fill=color, font=font(26))
        d.text((128 * S, sy), label, fill="#7C8AA0", font=font(21, cjk=True))
        f_val = font(25, cjk=True)
        vx = 340 * S
        if text_w(val, f_val) > (CARD_W - 52 - 340 - 30) * S:
            f_val = font(int(25 * (CARD_W - 422) * S / text_w(val, f_val)), cjk=True)
        d.text((vx, sy), val, fill=color, font=f_val)
        sy += 68 * S
    y += (panel_h + 40) * S

    # ---- footer one-liner --------------------------------------------------
    wx = ctx["weather"]
    sub = (f"{w_['sport_type']} · {st.strftime('%H:%M')} 出发 · "
           + (f"{wx['temperature_c']:.0f}°C RH {wx['humidity_pct']:.0f}%" if wx and wx.get("temperature_c") is not None else "天气未记录"))
    f_line = font(31, cjk=True)
    lines = wrap(a.get("one_liner", ""), f_line, (CARD_W - 104 - 34) * S)
    while len(lines) > 3:  # clamp
        f_line = font(int(31 * 0.88), cjk=True)
        lines = wrap(a.get("one_liner", ""), f_line, (CARD_W - 104 - 34) * S)
    line_h = 52 * S
    block_h = len(lines) * line_h + 40 * S
    fy = H - 64 * S - block_h - 34 * S
    d.rectangle([52 * S, fy, 58 * S, fy + block_h], fill="#FF5A3C")
    ly = fy
    for ln in lines:
        d.text((90 * S, ly), ln, fill="#F1F5FB", font=f_line)
        ly += line_h
    d.text((90 * S, ly + 6 * S), sub, fill="#55627A", font=font(19, cjk=True))
    img.save(png_path, "PNG")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workout", default="latest:run")
    ap.add_argument("--privacy", choices=["on", "off"], default=None,
                    help="default: config.privacy.default_on")
    ap.add_argument("--out", default=None, help="default output/workout-card.png")
    ap.add_argument("--no-png", action="store_true", help="only write report.html")
    ap.add_argument("--retry-chrome", action="store_true",
                    help="retry Chrome headless even if previously marked broken")
    ap.add_argument("--db", default=E.DEFAULT_DB)
    args = ap.parse_args()

    cfg = E.load_config()
    conn = E.connect(args.db)
    wid = E.resolve_workout(conn, args.workout)
    if not wid:
        print("[render] workout not found", file=sys.stderr)
        return 1
    ctx = gather(conn, wid)
    conn.close()
    privacy_on = (args.privacy == "on") if args.privacy else cfg["privacy"].get("default_on", True)
    ctx["route_trimmed"], trimmed = privacy_trim(ctx["route"], cfg, privacy_on)

    out_dir = E.ensure_output_dir()
    card_html_path = os.path.join(out_dir, "workout-card.html")
    report_path = os.path.join(out_dir, "report.html")
    with open(card_html_path, "w", encoding="utf-8") as f:
        f.write(build_card_html(ctx, cfg, privacy_on))
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(build_report_html(ctx))
    print(f"[render] html → {card_html_path}, {report_path}")

    if args.no_png:
        return 0
    png_path = args.out or os.path.join(out_dir, "workout-card.png")
    if args.retry_chrome:
        marker = os.path.join(E.DATA_DIR, ".chrome_headless_broken")
        if os.path.exists(marker):
            os.remove(marker)
    ok = chrome_screenshot(card_html_path, png_path)
    how = "chrome headless"
    if not ok:
        try:
            pillow_card(ctx, cfg, privacy_on, png_path)
            ok = True
            how = "pillow"
        except ImportError:
            ok = False
    if ok:
        kb = os.path.getsize(png_path) / 1024
        print(f"[render] png → {png_path} ({kb:.0f} KB, {how}"
              + (", privacy trimmed)" if trimmed else ")"))
        return 0
    print("[render] PNG failed: no Chrome and no Pillow. Open the HTML and screenshot manually.",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
