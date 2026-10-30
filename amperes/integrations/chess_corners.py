"""Check and correct the board calibration at the four corners, so pieces land in the middle of their squares.

The wrist camera goes straight over a1, h1, a8 and h8 (35 cm up, camera axis through the square centre the calibration
predicts). If the calibration is right, the square's centre appears at the image's principal point. The printed square
is found in the image as the region of its colour around the image centre (corners of the region, so a piece standing
in the middle does not matter), and the pixel offset of its centre, at 35 cm, is the calibration error in mm. The four
offsets give a corrected a1 and h1 (least-squares similarity fit), saved only with --save.

    python -m amperes.integrations.chess_corners --ack "<operator phrase>"          # measure only
    python -m amperes.integrations.chess_corners --ack "<operator phrase>" --save   # measure and correct
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from amperes.integrations import chess_robot as CR

CORNERS = ("a1", "h1", "a8", "h8")


def square_centre_px(colour: np.ndarray, near: tuple[float, float], square_px: float) -> dict[str, Any] | None:
    """Centre of the printed square containing `near` (pixels), from the region of its colour (dark green or white)."""
    import cv2

    hsv = cv2.cvtColor(colour, cv2.COLOR_BGR2HSV)
    grey = cv2.cvtColor(colour, cv2.COLOR_BGR2GRAY)
    u0, v0 = int(near[0]), int(near[1])
    r = int(square_px * 0.12)
    patch = grey[max(0, v0 - r):v0 + r, max(0, u0 - r):u0 + r]
    # sample the square's colour around (not at) the centre, where a piece may stand: the 4 inner corners of the square
    samples = []
    for du, dv in ((-0.32, -0.32), (0.32, -0.32), (-0.32, 0.32), (0.32, 0.32)):
        uu, vv = int(u0 + du * square_px), int(v0 + dv * square_px)
        if 0 <= uu < grey.shape[1] and 0 <= vv < grey.shape[0]:
            samples.append(int(grey[vv, uu]))
    if not samples or patch.size == 0:
        return None
    light = float(np.median(samples)) > 110  # white square, else dark green
    mask = (grey > 110) if light else ((grey < 100) & (hsv[..., 1] > 40))
    mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, lab, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=4)
    best = None
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if not (0.6 * square_px <= w <= 1.35 * square_px and 0.6 * square_px <= h <= 1.35 * square_px):
            continue  # not a single square (merged with the border, or a fragment)
        cx, cy = x + w / 2.0, y + h / 2.0  # bounding-box centre: robust to a piece hiding the middle
        d = math.hypot(cx - near[0], cy - near[1])
        if d < 0.6 * square_px and (best is None or d < best["dist_px"]):
            best = {"u": cx, "v": cy, "w": float(w), "h": float(h), "dist_px": d, "light": light}
    return best


async def measure(sess: CR.ChessSession, cal: CR.BoardCal, out_dir: Path, log=print) -> dict[str, Any]:
    import cv2

    from amperes.integrations import chess_scan as CS
    from amperes.integrations import chess_vision as CV

    out = {}
    for sq in CORNERS:
        pose = CS.close_pose(cal, sq)
        sess.session["moving"] = True
        try:
            await sess.ctl._move(pose, f"corner_{sq}", 40)
        finally:
            sess.session["moving"] = False
        await asyncio.sleep(0.3)
        cap = await CV.capture(sess.m, sess.spec.camera_names[0])
        K = cap["K"]
        mm_per_px = CS.CLOSE_HEIGHT_MM / K["fx"]
        square_px = cal.square_mm / mm_per_px
        found = square_centre_px(cap["colour"], (K["cx"], K["cy"]), square_px)
        img = cap["colour"].copy()
        cv2.drawMarker(img, (int(K["cx"]), int(K["cy"])), (0, 0, 255), cv2.MARKER_CROSS, 30, 2)
        if found:
            cv2.drawMarker(img, (int(found["u"]), int(found["v"])), (0, 255, 0), cv2.MARKER_TILTED_CROSS, 30, 2)
            # image -> board directions: rotate the pixel offset into the world with the camera's rotation
            d_cam = np.array([(found["u"] - K["cx"]) * mm_per_px, (found["v"] - K["cy"]) * mm_per_px, 0.0])
            d_world = cap["R"] @ d_cam
            out[sq] = {"dx": float(d_world[0]), "dy": float(d_world[1]), "px": [found["u"] - K["cx"], found["v"] - K["cy"]],
                       "square_px_seen": [found["w"], found["h"]], "square_px_expected": square_px}
            log(f"{sq}: square centre seen {math.hypot(*d_world[:2]):.1f} mm from where the calibration puts it "
                f"(dx {d_world[0]:+.1f}, dy {d_world[1]:+.1f} mm)")
        else:
            out[sq] = None
            log(f"{sq}: could not find the printed square")
        cv2.imwrite(str(out_dir / f"corner_{sq}.jpg"), img)
    return out


def corrected(cal: CR.BoardCal, offsets: dict[str, dict[str, float] | None]) -> dict[str, Any] | None:
    """Least-squares similarity (rotation, scale, shift) from the predicted to the seen corner centres -> new a1, h1."""
    pairs = [(np.array(cal.square_xy(sq)), np.array(cal.square_xy(sq)) + np.array([o["dx"], o["dy"]])) for sq, o in offsets.items() if o]
    if len(pairs) < 2:
        return None
    P = np.array([p for p, _ in pairs])
    Q = np.array([q for _, q in pairs])
    pc, qc = P.mean(0), Q.mean(0)
    A, B = P - pc, Q - qc
    # complex-number fit of q = s*e^{i t} * p + c
    a = (A[:, 0] + 1j * A[:, 1])
    b = (B[:, 0] + 1j * B[:, 1])
    z = (np.conj(a) @ b) / (np.conj(a) @ a)

    def tf(p):
        w = z * complex(*(np.array(p) - pc)) + complex(*qc)
        return float(w.real), float(w.imag)

    new_a1, new_h1 = tf(cal.square_xy("a1")), tf(cal.square_xy("h1"))
    resid = [math.dist(tf(p), q) for p, q in pairs]
    return {"a1": new_a1, "h1": new_h1, "scale": float(abs(z)), "rotation_deg": float(math.degrees(np.angle(z))),
            "residual_mm": [round(r, 1) for r in resid]}


async def run(ack: str, save: bool, out_dir: Path, log=print) -> dict[str, Any]:
    cal = CR.BoardCal.load()
    spec = CR.ChessSpec(out_dir=out_dir, execute=True, operator_ack=ack, auto=False)
    sess = CR.ChessSession(spec, cal, CR.Game(None, robot_color=None), threading.Event(), log=log)
    sess.position_unknown = True
    sess.env = cal.envelope(margin_mm=130.0)
    await sess.connect()
    sess.ctl.env = sess.env
    try:
        await sess.go_home()
        offsets = await measure(sess, cal, out_dir, log)
        await sess.ctl._move(cal.look_pose(), "look", 40)
    finally:
        await sess.close()
    fit = corrected(cal, offsets)
    res = {"offsets": offsets, "fit": fit, "saved": False}
    if fit and save:
        old = json.loads(CR.calibration_path().read_text())
        for name, (x, y) in (("a1", fit["a1"]), ("h1", fit["h1"])):
            old[name] = {**old[name], "x": round(x, 2), "y": round(y, 2), "_corrected_at": time.time(), "_corrected_from": {k: old[name][k] for k in ("x", "y")}}
        CR.calibration_path().write_text(json.dumps(old, indent=2))
        res["saved"] = True
        log(f"calibration corrected: a1 -> ({fit['a1'][0]:.1f}, {fit['a1'][1]:.1f}), h1 -> ({fit['h1'][0]:.1f}, {fit['h1'][1]:.1f}); "
            f"fit residuals {fit['residual_mm']} mm")
    (out_dir / "corners.json").write_text(json.dumps(res, indent=1, default=float))
    return res


if __name__ == "__main__":
    import argparse

    from amperes import store

    ap = argparse.ArgumentParser()
    ap.add_argument("--ack", default="")
    ap.add_argument("--save", action="store_true")
    a = ap.parse_args()
    out = store.artifacts_dir() / "robot_sessions" / ("corners_" + time.strftime("%Y%m%dT%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    print(json.dumps(asyncio.run(run(a.ack, a.save, out)), indent=1, default=float))
