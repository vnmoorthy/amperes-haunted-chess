"""Contact sheet of a close scan: one tile per visited square (centre crop of the close-up, square name and the
height-based guess drawn on it), so a person or a vision model can name every piece in one look."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path


def make_sheet(scan_dir: Path, out: Path, tile: int = 220, cols: int = 5) -> Path:
    import cv2
    import numpy as np

    rec = json.loads((scan_dir / "recognized.json").read_text())
    items = sorted(rec["pieces"].items())
    rows = math.ceil(len(items) / cols) or 1
    sheet = np.full((rows * tile, cols * tile, 3), 40, np.uint8)
    for k, (sq, c) in enumerate(items):
        img = cv2.imread(str(scan_dir / "images" / c["image"]))
        if img is None:
            continue
        h, w = img.shape[:2]
        r = 120  # the target piece sits at the image centre at the close pose; neighbours appear at the edges
        crop = img[max(0, h // 2 - r):h // 2 + r, max(0, w // 2 - r):w // 2 + r]
        crop = cv2.resize(crop, (tile, tile))
        cv2.circle(crop, (tile // 2, tile // 2), tile // 3, (0, 0, 255), 2)  # the piece to name is inside the red circle
        cv2.rectangle(crop, (0, 0), (tile - 1, 28), (0, 0, 0), -1)
        cv2.putText(crop, f"{sq}  {c['colour']}  guess {c['symbol']}  {c['height_peak']:.0f}mm", (4, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        y, x = (k // cols) * tile, (k % cols) * tile
        sheet[y:y + tile, x:x + tile] = crop
    cv2.imwrite(str(out), sheet)
    return out


def make_zoom_sheet(scan_dir: Path, out: Path, tile: int = 260, cols: int = 5, r: int = 75) -> Path:
    """Tight crops (the piece top only), upscaled, for naming pieces by eye."""
    import cv2
    import numpy as np

    rec = json.loads((scan_dir / "recognized.json").read_text())
    items = sorted(rec["pieces"].items())
    rows = math.ceil(len(items) / cols) or 1
    sheet = np.full((rows * tile, cols * tile, 3), 40, np.uint8)
    for k, (sq, c) in enumerate(items):
        img = cv2.imread(str(scan_dir / "images" / c["image"]))
        if img is None:
            continue
        h, w = img.shape[:2]
        crop = cv2.resize(img[h // 2 - r:h // 2 + r, w // 2 - r:w // 2 + r], (tile, tile), interpolation=cv2.INTER_CUBIC)
        if c.get("colour") == "black":  # dark pieces on dark squares: lift the shadows so rims and crowns are readable
            lab = cv2.cvtColor(crop, cv2.COLOR_BGR2LAB)
            lab[..., 0] = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4)).apply(lab[..., 0])
            crop = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
            crop = cv2.convertScaleAbs(crop, alpha=1.3, beta=25)
        cv2.rectangle(crop, (0, 0), (tile - 1, 26), (0, 0, 0), -1)
        cv2.putText(crop, f"{sq} {c['colour']}", (4, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        y, x = (k // cols) * tile, (k % cols) * tile
        sheet[y:y + tile, x:x + tile] = crop
    cv2.imwrite(str(out), sheet)
    return out


if __name__ == "__main__":
    d = Path(sys.argv[1])
    print(make_sheet(d, d / "contact_sheet.jpg"))
    print(make_zoom_sheet(d, d / "contact_zoom.jpg"))
