# -*- coding: utf-8 -*-
"""Generate PWA icons (no external assets). Run once: python gen_icons.py
Draws an upward stock line on dark navy — content kept in the central
safe zone so 'maskable' cropping on iOS/Android stays clean."""
import os
from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "icons")
BG = (13, 27, 42)
GREEN = (46, 204, 113)
SIZE = 512


def render(size=SIZE):
    img = Image.new("RGB", (size, size), BG)
    d = ImageDraw.Draw(img)
    s = size / 512.0
    # upward polyline in central safe zone
    pts = [(120, 360), (200, 300), (270, 330), (340, 235), (410, 165)]
    pts = [(x * s, y * s) for x, y in pts]
    d.line(pts, fill=GREEN, width=int(26 * s), joint="curve")
    r = int(15 * s)
    for x, y in pts:
        d.ellipse([x - r, y - r, x + r, y + r], fill=GREEN)
    # arrow head at the top-right node
    hx, hy = pts[-1]
    a = int(34 * s)
    d.polygon([(hx + a, hy - a), (hx + a, hy + int(6 * s)), (hx - int(6 * s), hy - a)], fill=GREEN)
    # baseline
    d.line([(110 * s, 400 * s), (410 * s, 400 * s)], fill=(38, 64, 92), width=int(8 * s))
    return img


def main():
    os.makedirs(OUT, exist_ok=True)
    base = render(SIZE)
    base.save(os.path.join(OUT, "icon-512.png"))
    base.resize((192, 192), Image.LANCZOS).save(os.path.join(OUT, "icon-192.png"))
    base.resize((180, 180), Image.LANCZOS).save(os.path.join(OUT, "apple-touch-icon-180.png"))
    print("icons written to", OUT)


if __name__ == "__main__":
    main()
