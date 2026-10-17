"""Notebook cover: a generated SVG from the title (deterministic, no network, no model). Same title -> same cover."""
from __future__ import annotations

import hashlib
import math

from .render import STYLE, esc, wrap

PALETTES = [("#3b4f8f", "#7aa2f7", "#f0b86e"), ("#2e6b5a", "#6fcf97", "#f2c94c"), ("#7a3b69", "#d98cc0", "#8ecae6"),
            ("#8a4b2b", "#f2a65a", "#a8dadc"), ("#2f5d7c", "#7fc8f8", "#ffd166"), ("#5b4b8a", "#b79ced", "#9be8c0")]


def cover_svg(title: str, subtitle: str = "") -> str:
    h = hashlib.sha256((title or "notebook").lower().encode()).digest()
    a, b, c = PALETTES[h[0] % len(PALETTES)]
    lines = wrap(title or "Notebook", 18, 3)
    shapes = []
    for i in range(7):
        cx, cy = 40 + h[1 + i] / 255 * 520, 30 + h[9 + i] / 255 * 240
        r = 20 + h[17 + i] / 255 * 60
        kind = h[25 + i] % 3
        col = (b, c, a)[kind]
        op = 0.16 + (h[3 + i] % 20) / 100
        if kind == 0:
            shapes.append(f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{r:.0f}" fill="{col}" opacity="{op:.2f}"/>')
        elif kind == 1:
            shapes.append(f'<rect x="{cx - r:.0f}" y="{cy - r / 2:.0f}" width="{r * 2:.0f}" height="{r:.0f}" rx="12" fill="{col}" opacity="{op:.2f}" '
                          f'transform="rotate({(h[10 + i] % 60) - 30} {cx:.0f} {cy:.0f})"/>')
        else:
            pts = " ".join(f"{cx + r * math.cos(k * 2.0944 + h[12 + i] / 40):.0f},{cy + r * math.sin(k * 2.0944 + h[12 + i] / 40):.0f}" for k in range(3))
            shapes.append(f'<polygon points="{pts}" fill="{col}" opacity="{op:.2f}"/>')
    text = "".join(f'<text x="32" y="{196 + i * 34 - (len(lines) - 1) * 17:.0f}" fill="#ffffff" font-family="Georgia,\'Times New Roman\',serif" '
                   f'font-size="30" font-weight="600">{esc(l)}</text>' for i, l in enumerate(lines))
    sub = f'<text x="32" y="266" fill="#ffffff" opacity=".78" font-family="system-ui,sans-serif" font-size="13">{esc(subtitle[:60])}</text>' if subtitle else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 300" width="600" height="300" role="img" aria-label="Cover for {esc(title)}">'
            f'<defs><linearGradient id="cg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{a}"/><stop offset="1" stop-color="#14161a"/></linearGradient></defs>'
            f'<rect width="600" height="300" fill="url(#cg)"/>{"".join(shapes)}<rect x="0" y="0" width="600" height="300" fill="#000" opacity=".18"/>{text}{sub}</svg>')
