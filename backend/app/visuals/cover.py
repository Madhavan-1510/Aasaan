"""Notebook cover: a generated SVG from the title (deterministic, no network, no model). Same title -> same cover.

Colours come from the page's theme tokens (--cv-*, built from the per-cover hue --h), so the cover restyles when the theme changes.
The fallbacks give a dark cover when the SVG is opened on its own.
"""
from __future__ import annotations

import hashlib
import math

from .render import esc, wrap


def cover_svg(title: str, subtitle: str = "") -> str:
    h = hashlib.sha256((title or "notebook").lower().encode()).digest()
    hue = int(h[0] / 255 * 360)
    lines = wrap(title or "Notebook", 18, 3)
    shapes = []
    for i in range(7):
        cx, cy = 40 + h[1 + i] / 255 * 520, 30 + h[9 + i] / 255 * 240
        r = 20 + h[17 + i] / 255 * 60
        kind = h[25 + i] % 3
        op = 0.14 + (h[3 + i] % 20) / 100
        if kind == 0:
            shapes.append(f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{r:.0f}" class="cv-shape" opacity="{op:.2f}"/>')
        elif kind == 1:
            shapes.append(f'<rect x="{cx - r:.0f}" y="{cy - r / 2:.0f}" width="{r * 2:.0f}" height="{r:.0f}" rx="12" class="cv-shape" opacity="{op:.2f}" '
                          f'transform="rotate({(h[10 + i] % 60) - 30} {cx:.0f} {cy:.0f})"/>')
        else:
            pts = " ".join(f"{cx + r * math.cos(k * 2.0944 + h[12 + i] / 40):.0f},{cy + r * math.sin(k * 2.0944 + h[12 + i] / 40):.0f}" for k in range(3))
            shapes.append(f'<polygon points="{pts}" class="cv-shape" opacity="{op:.2f}"/>')
    text = "".join(f'<text x="32" y="{196 + i * 34 - (len(lines) - 1) * 17:.0f}" class="cv-ink" font-family="Georgia,serif" '
                   f'font-size="30" font-weight="600">{esc(l)}</text>' for i, l in enumerate(lines))
    sub = f'<text x="32" y="266" class="cv-ink" opacity=".75" font-family="system-ui,sans-serif" font-size="13">{esc(subtitle[:60])}</text>' if subtitle else ""
    style = ("<style>.cv-s1{stop-color:var(--cv-bg1,hsl(var(--h) 35% 24%))}.cv-s2{stop-color:var(--cv-bg2,hsl(var(--h) 30% 12%))}"
             ".cv-shape{fill:var(--cv-shape,hsl(var(--h) 55% 62%))}.cv-ink{fill:var(--cv-ink,#f6f3ec)}</style>")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 300" width="600" height="300" role="img" class="cover-svg" style="--h:{hue}" '
            f'aria-label="Cover for {esc(title)}">{style}<defs><linearGradient id="cg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" class="cv-s1"/>'
            f'<stop offset="1" class="cv-s2"/></linearGradient></defs><rect width="600" height="300" fill="url(#cg)"/>{"".join(shapes)}{text}{sub}</svg>')
