#!/usr/bin/env python3
"""Privacy-safe square workout-card visuals.

This module only accepts already-projected local XY route paths. It never
accepts geographic coordinates or the original photo path. Photos are decoded,
EXIF-transposed, center-cropped, and re-encoded as metadata-free PNG bytes
before entering a SafeCardSpec.
"""
from __future__ import annotations

import base64
import html
import io
import math
import os
from typing import Iterable

CARD_W = 1080
CARD_H = 1080
EXPORT_SCALE = 2
ACCENT = "#F26B4B"
OFF_BLACK = "#11151B"
OFF_WHITE = "#F4F1EB"
MUTED = "#C9CDD4"


def _register_heif() -> bool:
    try:
        from pillow_heif import register_heif_opener
    except ImportError:
        return False
    register_heif_opener()
    return True


def normalize_photo(path: str, size=(CARD_W * EXPORT_SCALE, CARD_H * EXPORT_SCALE)) -> bytes:
    """Return center-cover, orientation-corrected, metadata-free PNG bytes."""
    from PIL import Image, ImageOps

    if not path or not os.path.isfile(path):
        name = os.path.basename(path) if path else "(missing path)"
        raise ValueError(f"photo not found: {name}")
    if os.path.splitext(path)[1].lower() in (".heic", ".heif") and not _register_heif():
        raise ValueError("HEIC support requires pillow-heif; run ./setup.sh")
    try:
        with Image.open(path) as src:
            img = ImageOps.exif_transpose(src).convert("RGB")
    except (OSError, ValueError) as exc:
        raise ValueError(f"unable to read photo: {os.path.basename(path)}") from exc

    target_w, target_h = size
    scale = max(target_w / img.width, target_h / img.height)
    resized = img.resize(
        (max(target_w, round(img.width * scale)), max(target_h, round(img.height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = (resized.width - target_w) // 2
    top = (resized.height - target_h) // 2
    clean = resized.crop((left, top, left + target_w, top + target_h))
    out = io.BytesIO()
    clean.save(out, "PNG", optimize=True)
    return out.getvalue()


def photo_data_url(photo_png: bytes | None) -> str:
    if not photo_png:
        return ""
    return "data:image/png;base64," + base64.b64encode(photo_png).decode("ascii")


def warp_route_paths(paths: Iterable[Iterable[tuple[float, float]]], w=CARD_W, h=CARD_H):
    """Map normalized XY paths onto a deterministic trapezoid photo plane.

    The transform is visual perspective only. It preserves point order and
    topology; it does not claim to reconstruct terrain or road geometry.
    """
    cx = w * 0.50
    top_y, bottom_y = h * 0.23, h * 0.70
    top_half, bottom_half = w * 0.16, w * 0.43
    warped = []
    for path in paths:
        out = []
        for u, v in path:
            u = min(1.0, max(0.0, float(u)))
            v = min(1.0, max(0.0, float(v)))
            depth = v ** 1.38
            half = top_half + (bottom_half - top_half) * depth
            x = cx + (u - 0.5) * 2 * half
            y = top_y + (bottom_y - top_y) * depth
            out.append((x, y))
        if len(out) >= 2:
            warped.append(out)
    return warped


def _svg_path(points):
    return f"M{points[0][0]:.1f},{points[0][1]:.1f}" + "".join(
        f"L{x:.1f},{y:.1f}" for x, y in points[1:]
    )


def route_svg(route_paths, w=CARD_W, h=CARD_H):
    warped = warp_route_paths(route_paths, w, h)
    if not warped:
        return ""
    under = "".join(
        f'<path d="{_svg_path(p)}" fill="none" stroke="#11151B" stroke-opacity="0.78" '
        f'stroke-width="12" stroke-linecap="round" stroke-linejoin="round"/>'
        for p in warped
    )
    line = "".join(
        f'<path d="{_svg_path(p)}" fill="none" stroke="{ACCENT}" stroke-width="5" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
        for p in warped
    )
    first, last = warped[0][0], warped[-1][-1]
    return (
        f'<svg class="route" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">'
        f"{under}{line}"
        f'<circle cx="{first[0]:.1f}" cy="{first[1]:.1f}" r="9" fill="{OFF_WHITE}" '
        f'stroke="{OFF_BLACK}" stroke-width="4"/>'
        f'<circle cx="{last[0]:.1f}" cy="{last[1]:.1f}" r="9" fill="{ACCENT}" '
        f'stroke="{OFF_BLACK}" stroke-width="4"/></svg>'
    )


def build_html(spec: dict, template_path: str) -> str:
    with open(template_path, encoding="utf-8") as f:
        page = f.read()
    photo = photo_data_url(spec.get("photo_png"))
    subs = {
        "PHOTO_CLASS": "has-photo" if photo else "route-only",
        "PHOTO_DATA_URL": photo,
        "ROUTE_SVG": route_svg(spec.get("route_paths") or []),
        "SPORT_LABEL": spec["sport_label"],
        "DATE_STR": spec["date_str"],
        "PRIVACY_TAG": spec["privacy_tag"],
        "DIST": spec["distance"],
        "DUR": spec["duration"],
        "PACE": spec["pace"],
        "AVGHR": spec["avg_hr"],
        "ONE_LINER": spec["one_liner"],
        "FOOT_SUB": spec["foot_sub"],
    }
    for key, value in subs.items():
        safe = value if key in ("PHOTO_DATA_URL", "ROUTE_SVG", "PHOTO_CLASS") else html.escape(str(value))
        page = page.replace("{{" + key + "}}", safe)
    return page


def _font(draw, size, cjk=False):
    from PIL import ImageFont

    paths = [
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ]
    if cjk:
        paths = [paths[-1]] + paths[:-1]
    for path in paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    return ImageFont.load_default()


def _cover_image(photo_png: bytes | None, size):
    from PIL import Image

    if not photo_png:
        return Image.new("RGB", size, "#1C232B")
    with Image.open(io.BytesIO(photo_png)) as src:
        img = src.convert("RGB")
    return img.resize(size, Image.Resampling.LANCZOS)


def _wrap(draw, text, font, max_width, max_lines=2):
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) > max_width and cur:
            lines.append(cur)
            cur = ch
            if len(lines) == max_lines:
                break
        else:
            cur += ch
    if len(lines) < max_lines and cur:
        lines.append(cur)
    if len(lines) == max_lines and "".join(lines) != text:
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > max_width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


def render_pillow(spec: dict, png_path: str):
    """Render the square card at 2x without carrying source photo metadata."""
    from PIL import Image, ImageDraw

    scale = EXPORT_SCALE
    w, h = CARD_W * scale, CARD_H * scale
    img = _cover_image(spec.get("photo_png"), (w, h))
    draw = ImageDraw.Draw(img, "RGBA")

    # A restrained tonal layer keeps text legible while preserving the photo.
    draw.rectangle((0, 0, w, 190 * scale), fill=(10, 14, 19, 72))
    for i in range(430 * scale):
        alpha = round(220 * (i / (430 * scale)) ** 1.6)
        y = h - 430 * scale + i
        draw.line((0, y, w, y), fill=(10, 14, 19, alpha), width=1)

    # Perspective route from safe local XY only.
    warped = warp_route_paths(spec.get("route_paths") or [], w, h)
    for path in warped:
        xy = [(round(x), round(y)) for x, y in path]
        draw.line(xy, fill=(17, 21, 27, 205), width=12 * scale, joint="curve")
        draw.line(xy, fill=(242, 107, 75, 255), width=5 * scale, joint="curve")
    if warped:
        for point, fill in ((warped[0][0], OFF_WHITE), (warped[-1][-1], ACCENT)):
            x, y = point
            r = 9 * scale
            draw.ellipse((x - r, y - r, x + r, y + r), fill=fill, outline=OFF_BLACK, width=4 * scale)

    pad = 54 * scale
    f_meta = _font(draw, 21 * scale, cjk=True)
    f_priv = _font(draw, 18 * scale, cjk=True)
    draw.text((pad, 46 * scale), spec["sport_label"], font=f_meta, fill=(244, 241, 235, 220))
    draw.text((pad, 82 * scale), spec["date_str"], font=f_priv, fill=(213, 216, 221, 210))
    priv_w = draw.textlength(spec["privacy_tag"], font=f_priv)
    draw.text((w - pad - priv_w, 50 * scale), spec["privacy_tag"], font=f_priv,
              fill=(213, 216, 221, 205))

    # Distance is the visual anchor; HR is secondary, with two quiet support metrics.
    base_y = 735 * scale
    f_distance = _font(draw, 104 * scale)
    f_unit = _font(draw, 26 * scale)
    f_label = _font(draw, 19 * scale, cjk=True)
    draw.text((pad, base_y), spec["distance"], font=f_distance, fill=OFF_WHITE)
    dist_w = draw.textlength(spec["distance"], font=f_distance)
    draw.text((pad + dist_w + 13 * scale, base_y + 61 * scale), "km", font=f_unit, fill=MUTED)
    draw.text((pad, base_y + 118 * scale), "距离", font=f_label, fill=(201, 205, 212, 210))

    hr_x = 720 * scale
    f_hr = _font(draw, 54 * scale)
    draw.text((hr_x, base_y + 25 * scale), spec["avg_hr"], font=f_hr, fill=OFF_WHITE)
    hr_w = draw.textlength(spec["avg_hr"], font=f_hr)
    draw.text((hr_x + hr_w + 10 * scale, base_y + 58 * scale), "bpm", font=f_label, fill=MUTED)
    draw.text((hr_x, base_y + 103 * scale), "平均心率", font=f_label, fill=(201, 205, 212, 210))

    support_y = 904 * scale
    f_support = _font(draw, 24 * scale, cjk=True)
    draw.text((pad, support_y), f"{spec['duration']}  时长", font=f_support, fill=(228, 230, 233, 235))
    draw.text((360 * scale, support_y), f"{spec['pace']}  平均配速", font=f_support,
              fill=(228, 230, 233, 235))
    draw.text((820 * scale, support_y), "GPS TRACE", font=f_label, fill=(201, 205, 212, 185))

    line_y = 966 * scale
    draw.rounded_rectangle((pad, line_y, pad + 8 * scale, h - 46 * scale),
                           radius=4 * scale, fill=ACCENT)
    f_line = _font(draw, 27 * scale, cjk=True)
    lines = _wrap(draw, spec["one_liner"], f_line, w - pad * 2 - 34 * scale, 2)
    for i, line in enumerate(lines):
        draw.text((pad + 28 * scale, line_y - 3 * scale + i * 42 * scale), line,
                  font=f_line, fill=OFF_WHITE)

    # Save without info/exif arguments so source metadata cannot propagate.
    img.save(png_path, "PNG", optimize=True)
