"""Make assets/deck-grid.png: a 2 x 5 contact sheet of deck/previews/slide-*.png on black.

Usage (after `pdftoppm -png -r 80 Amperes_Failure_Lab.pdf previews/slide`): python3 contact_sheet.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "assets" / "deck-grid.png"
COLS, TW, TH, GAP, MARGIN = 2, 800, 450, 24, 40

slides = sorted((HERE / "previews").glob("slide-*.png"))
rows = (len(slides) + COLS - 1) // COLS
sheet = Image.new("RGB", (2 * MARGIN + COLS * TW + (COLS - 1) * GAP, 2 * MARGIN + rows * TH + (rows - 1) * GAP), (0, 0, 0))
draw = ImageDraw.Draw(sheet)
for i, p in enumerate(slides):
    im = Image.open(p).convert("RGB")
    im = im.crop((0, 0, im.width - 1, im.height))  # drop the half-pixel page edge pdftoppm leaves at 80 dpi
    im = im.resize((TW, TH), Image.LANCZOS)
    x = MARGIN + (i % COLS) * (TW + GAP)
    y = MARGIN + (i // COLS) * (TH + GAP)
    sheet.paste(im, (x, y))
    draw.rectangle([x - 1, y - 1, x + TW, y + TH], outline=(42, 43, 47))
sheet.save(OUT, optimize=True)
print(OUT, sheet.size, len(slides), "slides")
