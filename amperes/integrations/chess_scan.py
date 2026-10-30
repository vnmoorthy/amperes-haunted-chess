"""Autonomous close scan of the chessboard: read occupancy from the look pose, then fly the wrist camera over every
occupied square (camera centred, ~35 cm up) and classify the piece from its close-up depth and silhouette.
Writes a labelled close-up per piece (images/<square>_<guess>.jpg) and a recognised board (recognized.json).

Classes: by measured height (pawn < 50, rook < 58, knight < 66, bishop < 78, else king/queen); at 35 cm a king's or
queen's top is inside the D435's minimum range, which shows up as missing depth at the centre: that too means 'tall'.
King vs queen is settled by colour counts (one of each per side; the taller/broader one is the king)."""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from pathlib import Path
from typing import Any

import chess
import numpy as np

from amperes.integrations import chess_robot as CR
from amperes.integrations import chess_vision as CV

CLOSE_HEIGHT_MM = 350.0  # camera above the real board surface


def close_pose(cal: CR.BoardCal, sq: str) -> dict[str, float]:
    x, y = cal.square_xy(sq)
    theta = cal.theta_deg - 45.0
    a = math.radians(-theta)
    ox, oy = CR.CAM_OFFSET[0] * math.cos(a) - CR.CAM_OFFSET[1] * math.sin(a), CR.CAM_OFFSET[0] * math.sin(a) + CR.CAM_OFFSET[1] * math.cos(a)
    # camera real height = flange z + 4 (camera 18 mm below the flange, board 22 mm below the arm origin)
    return {"x": x - ox, "y": y - oy, "z": CLOSE_HEIGHT_MM - 4.0, **CR.DOWN, "theta": theta}


def classify_close(cap: dict[str, Any], king_height_mm: float) -> dict[str, Any]:
    import cv2

    d = cap["depth"].astype(np.float32)
    h, w = d.shape
    cx, cy = int(cap["K"]["cx"]), int(cap["K"]["cy"])
    r = int(0.6 * 54.0 * cap["K"]["fx"] / CLOSE_HEIGHT_MM)  # 0.6 square at the board plane, in pixels
    patch = d[cy - r:cy + r, cx - r:cx + r]
    valid = patch > 0
    board_depth = float(np.nanmedian(np.where(valid, patch, np.nan))) if valid.any() else CLOSE_HEIGHT_MM
    ring = d[max(0, cy - 3 * r):cy + 3 * r, max(0, cx - 3 * r):cx + 3 * r]
    ring_valid = ring[ring > 0]
    plane = float(np.percentile(ring_valid, 80)) if ring_valid.size else CLOSE_HEIGHT_MM  # the board around the piece
    heights = plane - patch[valid]
    peak = float(np.percentile(heights, 99)) if heights.size else 0.0
    core = patch[r // 2: r + r // 2, r // 2: r + r // 2]
    missing = float((core <= 0).mean()) if core.size else 0.0  # too close for the sensor -> tall piece
    grey = cv2.cvtColor(cap["colour"], cv2.COLOR_BGR2GRAY)
    core_g = grey[cy - r // 3:cy + r // 3, cx - r // 3:cx + r // 3]  # the piece's top fills the centre at 35 cm
    bright = float(np.median(core_g)) if core_g.size else 0.0
    colour = "white" if bright > 110 else "black"
    scale = king_height_mm / 95.0
    hmm = peak / scale
    if missing > 0.35:
        kind = "k"
    else:
        kind = next(k for ub, k in CV.PIECE_BY_HEIGHT if hmm <= ub)
    return {"kind": kind, "colour": colour, "height_peak": round(peak, 1), "missing_centre": round(missing, 2), "brightness": round(bright, 1),
            "board_depth": round(board_depth, 1), "plane": round(plane, 1)}


async def run(out_dir: Path, log=print, stop: threading.Event | None = None, ack: str = "") -> dict[str, Any]:
    import cv2

    cal = CR.BoardCal.load()
    spec = CR.ChessSpec(out_dir=out_dir, execute=True, operator_ack=ack, auto=False)
    sess = CR.ChessSession(spec, cal, CR.Game(None, robot_color=None), stop or threading.Event(), log=log)
    sess.position_unknown = True
    sess.env = cal.envelope(margin_mm=130.0)  # camera-centred poses put the flange ~85 mm beyond the edge squares
    await sess.connect()
    sess.ctl.env = sess.env
    (out_dir / "images").mkdir(parents=True, exist_ok=True)
    found: dict[str, Any] = {}
    try:
        await sess.look("overview", 0)
        obs = await sess.observe(save_debug=True)
        occupied = [sq for sq, o in obs.items() if o.get("occupied")]
        log(f"overview: {len(occupied)} occupied squares: {occupied}")
        # visit in a serpentine order over files to keep moves short
        occupied.sort(key=lambda s: (s[0], s[1] if (ord(s[0]) - 97) % 2 == 0 else chr(ord("9") - int(s[1]))))
        for i, sq in enumerate(occupied):
            if sess.stop_event.is_set():
                break
            pose = close_pose(cal, sq)
            sess.session["moving"] = True
            try:
                await sess.ctl._move(pose, f"close_{sq}", 40)
            except Exception as exc:
                log(f"{sq}: could not reach ({type(exc).__name__}: {str(exc)[:60]})")
                sess.session["moving"] = False
                continue
            sess.session["moving"] = False
            await asyncio.sleep(0.4)
            cap = await CV.capture(sess.m, "cam")
            c = classify_close(cap, cal.king_height_mm)
            c["overview_colour"] = obs[sq].get("colour")
            if c["overview_colour"] in ("white", "black"):
                c["colour"] = c["overview_colour"]  # the depth-masked overview read is the more reliable colour
            sym = c["kind"].upper() if c["colour"] == "white" else c["kind"]
            fn = f"{sq}_{sym}.jpg"
            cv2.imwrite(str(out_dir / "images" / fn), cap["colour"])
            found[sq] = {**c, "symbol": sym, "image": fn}
            log(f"[{i + 1}/{len(occupied)}] {sq}: {c['colour']} {CR.PIECE_NAME.get(c['kind'], c['kind'])} (peak {c['height_peak']} mm, missing centre {c['missing_centre']})")
        await sess.look("overview_end", 1)
    finally:
        await sess.close()
    # king vs queen: per colour the two 'k' candidates; the one with more missing depth / taller is the king
    for colour in ("white", "black"):
        ks = [sq for sq, c in found.items() if c["colour"] == colour and c["kind"] == "k"]
        ks.sort(key=lambda s: (found[s]["missing_centre"], found[s]["height_peak"]), reverse=True)
        for j, sq in enumerate(ks):
            kind = "k" if j == 0 else "q"
            found[sq]["kind"] = kind
            found[sq]["symbol"] = kind.upper() if colour == "white" else kind
    board = chess.Board(None)
    for sq, c in found.items():
        board.set_piece_at(chess.parse_square(sq), chess.Piece.from_symbol(c["symbol"]))
    result = {"board_fen": board.board_fen(), "pieces": found, "counts": {s: sum(1 for c in found.values() if c["symbol"] == s) for s in "PRNBQKprnbqk"},
              "images": str(out_dir / "images"), "scanned_at": time.time()}
    (out_dir / "recognized.json").write_text(json.dumps(result, indent=1))
    log(str(board))
    return result


if __name__ == "__main__":
    import signal
    import sys

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    from amperes import store

    out = store.artifacts_dir() / "robot_sessions" / ("closescan_" + time.strftime("%Y%m%dT%H%M%S"))
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--ack", default="", help="operator acknowledgement, typed by the person at the e-stop (required to move)")
    r = asyncio.run(run(out, stop=stop, ack=ap.parse_args().ack))
    print(json.dumps({k: v for k, v in r.items() if k != "pieces"}, indent=1))
    sys.exit(0)
