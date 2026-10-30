"""Survey the board and 10 cm around it: find every piece in arm coordinates, name it, put it on a square.

The wrist camera visits six poses 35 cm above the board (twice the detail of the single look pose), together covering
the board plus a 10 cm margin on every side. In each view the depth image, pinned to the board plane, gives every piece
as a 3D blob: its position on the table in arm coordinates and the height of its top. Views of the same piece merge.
Claude names all pieces in one call from a zoomed crop of each (taken from the view where it is nearest the image
centre) together with its measured height. Each piece then lands on a square, or stays an off-board spot "xy:<x>,<y>"
that the planner can pick from. Low blobs (a piece lying on its side) and anything between squares are reported as
obstacles, never touched.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any

import numpy as np

from amperes.integrations import chess_robot as CR

SURVEY_HEIGHT_MM = 350.0   # camera above the board: fingertips ~170 mm up, 85 mm over the tallest king
MARGIN_MM = 100.0          # look this far beyond the board's edges
UPRIGHT_MIN_MM = 36.0      # a top lower than this is a piece lying down (an upright pawn is ~45 mm)
MERGE_MM = 22.0
# measured 30 Oct on 36 sightings of pieces the arm itself had placed: the camera model reports positions this far off
CAMERA_BIAS_MM = (8.6, 12.2)            # two blobs closer than this are one piece
FOV_HALF_WIDE, FOV_HALF_NARROW = 220.0, 120.0  # usable half-footprint at 350 mm (D435 colour ~480 x 270 mm), with overlap


def _axes(cal: CR.BoardCal):
    fx, fy = cal.file_axis
    rx, ry = cal.rank_axis
    s = cal.square_mm
    return np.array(cal.square_xy("a1")), np.array([fx, fy]) / s, np.array([rx, ry]) / s


def to_world(cal: CR.BoardCal, u: float, v: float) -> tuple[float, float]:
    """Board-local mm (u along the files a->h, v along the ranks 1->8, origin at a1's centre) to arm coordinates."""
    a1, fh, rh = _axes(cal)
    p = a1 + fh * u + rh * v
    return float(p[0]), float(p[1])


def to_board(cal: CR.BoardCal, x: float, y: float) -> tuple[float, float]:
    a1, fh, rh = _axes(cal)
    d = np.array([x, y]) - a1
    return float(d @ fh), float(d @ rh)


def survey_poses(cal: CR.BoardCal, height: float = SURVEY_HEIGHT_MM, margin: float = MARGIN_MM) -> list[dict[str, float]]:
    """Camera centres covering the board plus `margin`; the image's wide axis runs along the ranks at the look theta."""
    s = cal.square_mm
    lo, hi = -0.5 * s - margin, 7.5 * s + margin
    us = [lo + FOV_HALF_NARROW, (lo + hi) / 2, hi - FOV_HALF_NARROW]   # along the files: narrow image axis
    vs = [lo + FOV_HALF_WIDE, hi - FOV_HALF_WIDE]                        # along the ranks: wide image axis
    theta = cal.theta_deg - 45.0
    a = math.radians(-theta)
    ox = CR.CAM_OFFSET[0] * math.cos(a) - CR.CAM_OFFSET[1] * math.sin(a)
    oy = CR.CAM_OFFSET[0] * math.sin(a) + CR.CAM_OFFSET[1] * math.cos(a)
    poses = []
    for i, u in enumerate(us):
        for v in (vs if i % 2 == 0 else vs[::-1]):  # serpentine: short moves
            x, y = to_world(cal, u, v)
            poses.append({"x": x - ox, "y": y - oy, "z": height - 4.0, **CR.DOWN, "theta": theta, "_u": u, "_v": v})
    return poses


def square_of(cal: CR.BoardCal, x: float, y: float, tol: float = 0.36) -> str | None:
    """The square whose centre this point is near (within `tol` of a square), or None (off the board or on a line)."""
    u, v = to_board(cal, x, y)
    f, r = u / cal.square_mm, v / cal.square_mm
    fi, ri = round(f), round(r)
    if 0 <= fi <= 7 and 0 <= ri <= 7 and abs(f - fi) <= tol and abs(r - ri) <= tol:
        return "abcdefgh"[fi] + str(ri + 1)
    return None


def on_board_area(cal: CR.BoardCal, x: float, y: float) -> bool:
    u, v = to_board(cal, x, y)
    s = cal.square_mm
    return -0.5 * s <= u <= 7.5 * s and -0.5 * s <= v <= 7.5 * s


def merge(views: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """One record per physical piece: the observation nearest its image centre wins (sharpest, least oblique)."""
    pieces: list[dict[str, Any]] = []
    for vi, blobs in enumerate(views):
        for b in blobs:
            b = {**b, "view": vi, "centre_dist": math.hypot(b["u"] - 320, b["v"] - 240)}
            for p in pieces:
                if math.hypot(p["x"] - b["x"], p["y"] - b["y"]) < MERGE_MM:
                    p["seen"] += 1
                    p["heights"].append(b["height"])
                    if b["centre_dist"] < p["centre_dist"]:
                        p.update({k: b[k] for k in ("x", "y", "u", "v", "view", "centre_dist", "area", "brightness", "lying") if k in b})
                    break
            else:
                pieces.append({**b, "seen": 1, "heights": [b["height"]]})
    for p in pieces:
        p["height"] = float(np.median(p["heights"]))
    return pieces


def crop(colour: np.ndarray, u: float, v: float, half: int = 64, scale: int = 2) -> np.ndarray:
    import cv2

    h, w = colour.shape[:2]
    x0, y0 = int(max(0, u - half)), int(max(0, v - half))
    x1, y1 = int(min(w, u + half)), int(min(h, v + half))
    c = colour[y0:y1, x0:x1]
    return cv2.resize(c, (c.shape[1] * scale, c.shape[0] * scale), interpolation=cv2.INTER_CUBIC)


TRUE_HEIGHT = {"k": 85.0, "q": 76.0, "b": 63.0, "n": 54.0, "r": 49.0, "p": 45.0}
MIN_CONFIDENCE = 0.6   # pieces named less surely than this are left where they are (looked at again next round)
HEIGHT_TOL_MM = 6.0    # a name whose expected height is further than this from the measured one is not trusted


def check_heights(pieces: list[dict[str, Any]], log=print) -> float | None:
    """Cross-check Claude's names against the measured heights. The depth camera reads tops a few mm low and the bias
    varies per setup, so the scale comes from the pawns Claude is sure of (the commonest piece). A name whose expected
    height disagrees with the measurement loses its confidence; a height that only one kind fits (king, queen, bishop)
    gets that kind. Returns the scale used (None when there were too few sure pawns)."""
    sure_pawns = [p["height"] for p in pieces if p.get("upright") and p.get("symbol", "?").lower() == "p" and p.get("confidence", 0) >= 0.8]
    if len(sure_pawns) < 3:
        return None
    scale = float(np.median(sure_pawns)) / TRUE_HEIGHT["p"]
    for p in pieces:
        sym = p.get("symbol", "?")
        if not p.get("upright") or sym in ("?", "."):
            continue
        h = p["height"]
        best = min(TRUE_HEIGHT, key=lambda k: abs(h - TRUE_HEIGHT[k] * scale))
        exp = TRUE_HEIGHT[sym.lower()] * scale
        p["height_class"] = best
        if abs(h - exp) > HEIGHT_TOL_MM:
            # only kings and queens stand clear of the rest; knight and bishop heights vary between sets
            if best in ("k", "q") and abs(h - TRUE_HEIGHT[best] * scale) <= HEIGHT_TOL_MM / 2:
                new = best.upper() if sym.isupper() else best
                log(f"  {p['id']}: Claude said {sym} but it stands {h:.0f} mm tall: a {CR.PIECE_NAME[best]}")
                p["symbol"], p["confidence"] = new, 0.7
            else:
                log(f"  {p['id']}: Claude said {sym} ({p.get('confidence', 0):.2f}) but it stands {h:.0f} mm tall (expected {exp:.0f}); unsure")
                p["confidence"] = min(p.get("confidence", 0), 0.3)
    return scale


HEIGHT_GUIDE = ("Heights of this set's pieces when standing: king 85 mm, queen 76, bishop 63, knight 54, rook 49, pawn 45 "
                "(measured heights read a few mm low). Seen from straight above: king = a cross on a round crown; queen = a "
                "ring of small points around a ball; bishop = a pointed mitre with a slit; knight = a horse head, clearly "
                "longer one way; rook = a flat round top with notches around its rim; pawn = a small plain ball.")


async def survey(sess: CR.ChessSession, cal: CR.BoardCal, out_dir: Path, log=print, identify: bool = True) -> dict[str, Any]:
    """Visit the survey poses, detect and name every piece. Returns {"pieces": [...], "layout": {location: symbol},
    "obstacles": [...], "views": n}."""
    import cv2

    from amperes.integrations import chess_claude_vision as CCV
    from amperes.integrations import chess_vision as CV

    img_dir = out_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    views, colours = [], []
    poses = survey_poses(cal)
    try:  # start at the end of the serpentine nearest the arm: no long sweeps (a 45 cm swing tripped the collision check)
        cur = await sess.ctl.pose()
        if math.dist((cur["x"], cur["y"]), (poses[-1]["x"], poses[-1]["y"])) < math.dist((cur["x"], cur["y"]), (poses[0]["x"], poses[0]["y"])):
            poses = poses[::-1]
    except Exception:
        pass
    for i, pose in enumerate(poses):
        target = {k: v for k, v in pose.items() if not k.startswith("_")}
        sess.session["moving"] = True
        try:
            await sess.ctl._move(target, f"survey_{i}", 40)
        finally:
            sess.session["moving"] = False
        await asyncio.sleep(0.1)
        cap = None
        for attempt in range(3):  # the venue link drops for seconds at a time: retry, then skip the view
            try:
                cap = await CV.capture(sess.m, sess.spec.camera_names[0])
                break
            except Exception as exc:
                log(f"view {i + 1}: camera did not answer ({type(exc).__name__}); retry {attempt + 1}/3")
                await asyncio.sleep(2.0)
        if cap is None:
            log(f"view {i + 1}/6 skipped: no picture")
            continue
        capp = CV.pin_camera(cap)
        W = CV.world_points(capp["depth"], capp["K"], capp["R"], capp["t"])
        blobs = CV.detect_pieces(W, CV.VISION_BOARD_Z, min_height=22.0, min_area=120, colour=cap["colour"])
        from amperes.integrations import chess_righting as RT

        for b in blobs:
            if b["height"] < UPRIGHT_MIN_MM:
                b["lying"] = RT.lying_shape(W, CV.VISION_BOARD_Z, b["x"], b["y"])
        cv2.imwrite(str(img_dir / f"survey_{i}.jpg"), cap["colour"])
        views.append(blobs)
        colours.append(cap["colour"])
        log(f"view {i + 1}/6: {len(blobs)} blobs")
    sess.session["moving"] = True
    try:
        await sess.ctl._move(cal.look_pose(), "survey_done", 40)  # back to the hub above the board centre
    finally:
        sess.session["moving"] = False
    pieces = merge(views)
    for p in pieces:  # correct the camera model's offset so the fingers land on the piece
        p["x"] -= CAMERA_BIAS_MM[0]
        p["y"] -= CAMERA_BIAS_MM[1]
    try:  # then the fitted correction (shift, rotation, scale) from pieces the arm placed: amperes_data/robot/survey_correction.json
        import json as _json

        from amperes import store as _store

        M = np.array(_json.loads((_store.home() / "robot" / "survey_correction.json").read_text())["affine"])
        for p in pieces:
            p["x"], p["y"] = (float(v) for v in np.array([p["x"], p["y"], 1.0]) @ M)
    except (FileNotFoundError, KeyError, ValueError):
        pass
    reach_ok = lambda p: math.hypot(p["x"], p["y"]) <= 640.0  # noqa: E731
    for k, p in enumerate(pieces):
        p["id"] = f"p{k + 1}"
        p["upright"] = p["height"] >= UPRIGHT_MIN_MM
        p["square"] = square_of(cal, p["x"], p["y"])
        p["on_board"] = on_board_area(cal, p["x"], p["y"])
        p["reachable"] = reach_ok(p)
        p["colour"] = "white" if p.get("brightness", 0) > 110 else "black"
        cv2.imwrite(str(img_dir / f"piece_{p['id']}.jpg"), crop(colours[p["view"]], p["u"], p["v"]))
    named: dict[str, dict[str, Any]] = {}
    upright = [p for p in pieces if p["upright"]]
    if identify and upright and CCV.available():
        ctx = HEIGHT_GUIDE + " Measured top heights: " + ", ".join(f"{p['id']} {p['height']:.0f} mm" for p in upright) + "."
        crops = {p["id"]: crop(colours[p["view"]], p["u"], p["v"]) for p in upright}
        try:
            named = await asyncio.to_thread(CCV.identify_pieces, crops, ctx + " Each image is a top-down close-up from 35 cm.", "medium")
            log(f"Claude named {len([k for k in named if not k.startswith('_')])} pieces ({(named.get('_call') or {}).get('latency_s', 0):.0f} s)")
        except Exception as exc:
            log(f"Claude failed: {type(exc).__name__}: {str(exc)[:100]}")
    for p in pieces:
        n = named.get(p["id"]) or {}
        p["symbol"], p["confidence"], p["reason"] = n.get("symbol", "?"), float(n.get("confidence", 0.0)), n.get("reason", "")
    check_heights(pieces, log)
    layout: dict[str, str] = {}
    obstacles = []
    for p in pieces:
        if p["symbol"] in CCV.SYMBOLS and p["confidence"] < MIN_CONFIDENCE:
            p["reason"] = f"unsure ({p['confidence']:.2f}): {p['reason']}"
            p["symbol"] = "?"
        if p["symbol"] in CCV.SYMBOLS and p["colour"] != ("white" if p["symbol"].isupper() else "black"):
            p["colour_note"] = f"brightness says {p['colour']}, Claude says {p['symbol']}"
        if not p["upright"] or p["symbol"] not in CCV.SYMBOLS:
            obstacles.append(p)
            if p["square"]:
                layout[p["square"]] = "?"
            continue
        if p["square"]:
            layout[p["square"]] = p["symbol"]
        elif p["on_board"]:  # standing across a line: picked from where it stands, like a piece beside the board
            layout[f"xy:{p['x']:.0f},{p['y']:.0f}"] = p["symbol"]
        elif p["reachable"]:
            layout[f"xy:{p['x']:.0f},{p['y']:.0f}"] = p["symbol"]
        else:
            obstacles.append(p)
    return {"pieces": pieces, "layout": layout, "obstacles": obstacles, "views": len(views), "lying": [p["lying"] for p in pieces if not p["upright"] and p.get("lying") and p["reachable"]]}
