"""Render the Amperes brand pieces the deck needs as PNGs (run before `node build.js`).

Writes deck/brand/:
  bg.png        black slide background with the console's subtle radial glow
  wordmark.png  "AMPERES" in a light face, wide tracking, silver gradient with a frozen sheen
  logo.png      the console's favicon mark (rounded square, line-drawn A)

Tokens come from amperes/ui/index.html (dark theme). Needs Pillow.
"""
from pathlib import Path
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent / "brand"
FONT = "/System/Library/Fonts/HelveticaNeue.ttc"  # index 7 = Helvetica Neue Light (weight 300)


def hexrgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def background(w=1920, h=1080, glow="#141416"):
    # radial-gradient(ellipse 70% 55% at 50% 0%, glow 0%, #000 72%), dithered to avoid banding
    g = hexrgb(glow)
    img = Image.new("RGB", (w, h))
    px = img.load()
    rx, ry = 0.70 * w, 0.55 * h
    rnd = random.Random(7)
    for y in range(h):
        for x in range(w):
            d = (((x - w / 2) / rx) ** 2 + (y / ry) ** 2) ** 0.5
            t = max(0.0, 1.0 - d / 0.72)
            t = t * t * (3 - 2 * t)
            n = rnd.random() - 0.5
            px[x, y] = tuple(max(0, min(255, int(c * t + n))) for c in g)
    return img


def silver(w, h):
    # --silver: linear-gradient(180deg, #f5f5f6 0%, #cdced1 48%, #84868b 100%)
    stops = [(0.0, hexrgb("#f5f5f6")), (0.48, hexrgb("#cdced1")), (1.0, hexrgb("#84868b"))]
    col = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
            if t0 <= t <= t1:
                k = (t - t0) / (t1 - t0)
                col.putpixel((0, y), tuple(int(a + (b - a) * k) for a, b in zip(c0, c1)))
                break
    return col.resize((w, h))


def wordmark(text="AMPERES", size=360, tracking=0.36):
    font = ImageFont.truetype(FONT, size, index=7)
    adv = [font.getlength(ch) for ch in text]
    gap = tracking * size
    width = int(sum(adv) + gap * (len(text) - 1)) + 40
    height = int(size * 1.25)
    mask = Image.new("L", (width, height), 0)
    d = ImageDraw.Draw(mask)
    x = 20
    for ch, a in zip(text, adv):
        d.text((x, 10), ch, font=font, fill=255)
        x += a + gap
    bbox = mask.getbbox()
    mask = mask.crop(bbox)
    w, h = mask.size
    fill = silver(w, h).convert("RGBA")
    # frozen sheen: a soft diagonal highlight about a third of the way across
    sheen = Image.new("L", (w, h), 0)
    sd = ImageDraw.Draw(sheen)
    cx = int(w * 0.34)
    band = int(w * 0.05)
    sd.polygon([(cx - band, h), (cx + band, h), (cx + band + int(h * 0.36), 0), (cx - band + int(h * 0.36), 0)], fill=150)
    sheen = sheen.filter(ImageFilter.GaussianBlur(band * 0.6))
    white = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    fill = Image.composite(white, fill, sheen)
    fill.putalpha(mask)
    return fill


def logo(px=512, ss=4):
    s = px * ss / 64.0
    img = Image.new("RGBA", (px * ss, px * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lw = max(1, int(1.2 * s))
    d.rounded_rectangle([lw // 2, lw // 2, px * ss - lw // 2 - 1, px * ss - lw // 2 - 1], radius=int(14 * s),
                        fill=(0, 0, 0, 255), outline=hexrgb("#3a3b40") + (255,), width=lw)
    ink = hexrgb("#d8d9dc") + (255,)
    w = int(3.5 * s)
    pts = [(17, 47), (32, 15), (47, 47)]
    seg = [(pts[0], pts[1]), (pts[1], pts[2]), ((23.5, 36), (40.5, 36))]
    for (x0, y0), (x1, y1) in seg:
        d.line([(x0 * s, y0 * s), (x1 * s, y1 * s)], fill=ink, width=w)
    for x, y in [(17, 47), (32, 15), (47, 47), (23.5, 36), (40.5, 36)]:
        r = w / 2
        d.ellipse([x * s - r, y * s - r, x * s + r, y * s + r], fill=ink)
    return img.resize((px, px), Image.LANCZOS)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    background().save(OUT / "bg.png", optimize=True)
    wordmark().save(OUT / "wordmark.png", optimize=True)
    logo().save(OUT / "logo.png", optimize=True)
    for p in sorted(OUT.glob("*.png")):
        print(p.name, Image.open(p).size)
