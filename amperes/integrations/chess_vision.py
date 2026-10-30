"""Calibrate the chessboard from the wrist camera instead of hand-teaching: one RGB-D frame from above, the pieces in
their start position give the lattice; the king and queen heights give the files; the right-hand rule gives a8.

Read-only: it photographs, computes, and writes `chessboard.json`; it moves nothing. The overlay image lets the operator
check the detection before the first (dry) move.
"""

from __future__ import annotations

import asyncio
import json
import math
import struct
import time
from pathlib import Path
from typing import Any

import numpy as np

from amperes.integrations.chess_robot import DOWN, PIECE_HEIGHT, TOOL_LENGTH_MM, BoardCal, calibration_path
from amperes.integrations.viam_control import POSE_KEYS, client_options, dial_robot, timed

FILES = "abcdefgh"


def decode_depth(raw: bytes) -> np.ndarray:
    magic, (w, h) = raw[:8], struct.unpack(">QQ", raw[8:24])
    if magic not in (b"DEPTHMAP", b"VERSIONX"):
        raise ValueError("not a Viam raw depth frame")
    return np.frombuffer(raw[24:24 + w * h * 2], dtype=">u2").reshape(h, w).astype(np.float32)


async def capture(m, cam_name: str = "cam") -> dict[str, Any]:
    """Colour, depth (mm), intrinsics and the camera's world pose as a 3x3 rotation + translation (mm)."""
    import cv2
    from viam.components.camera import Camera
    from viam.proto.common import Pose, PoseInFrame

    try:
        await timed(m.refresh(), 10)  # the resource list can lag a camera restart
    except Exception:
        pass
    cam = Camera.from_robot(m, cam_name)
    try:
        r = await timed(cam.get_images(), 20)
    except asyncio.TimeoutError:  # one slow frame should not abort a read
        r = await timed(cam.get_images(), 20)
    imgs = r[0] if isinstance(r, tuple) else r
    colour = depth = None
    for im in imgs:
        if "jpeg" in str(im.mime_type) or "png" in str(im.mime_type):
            colour = cv2.imdecode(np.frombuffer(im.data, np.uint8), cv2.IMREAD_COLOR)
        elif "dep" in str(im.mime_type):
            depth = decode_depth(im.data)
    if colour is None or depth is None:
        raise RuntimeError("camera did not return both colour and depth")
    props = await timed(cam.get_properties(), 8)
    ip = props.intrinsic_parameters
    K = {"fx": float(ip.focal_x_px), "fy": float(ip.focal_y_px), "cx": float(ip.center_x_px), "cy": float(ip.center_y_px)}

    async def world(x, y, z):
        pif = await timed(m.transform_pose(PoseInFrame(reference_frame=cam_name, pose=Pose(x=x, y=y, z=z, o_z=1)), "world"), 8)
        return np.array([pif.pose.x, pif.pose.y, pif.pose.z], dtype=np.float64)

    o = await world(0, 0, 0)
    R = np.stack([(await world(1000, 0, 0) - o) / 1000, (await world(0, 1000, 0) - o) / 1000, (await world(0, 0, 1000) - o) / 1000], axis=1)
    return {"colour": colour, "depth": depth, "K": K, "R": R, "t": o, "captured_at": time.time()}


def world_points(depth: np.ndarray, K: dict, R: np.ndarray, t: np.ndarray) -> np.ndarray:
    """(h, w, 3) world coordinates in mm; NaN where depth is missing."""
    h, w = depth.shape
    u, v = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    z = depth.copy()
    z[z <= 0] = np.nan
    x = (u - K["cx"]) * z / K["fx"]
    y = (v - K["cy"]) * z / K["fy"]
    cam = np.stack([x, y, z], axis=-1)
    return cam @ R.T + t


def pixel_of(p: np.ndarray, K: dict, R: np.ndarray, t: np.ndarray) -> tuple[float, float]:
    c = R.T @ (np.asarray(p, dtype=np.float64) - t)
    return (K["fx"] * c[0] / c[2] + K["cx"], K["fy"] * c[1] / c[2] + K["cy"])


def detect_pieces(W: np.ndarray, board_z: float, min_height: float = 22.0, min_area: int = 120, colour=None) -> list[dict[str, Any]]:
    import cv2

    height = W[..., 2] - board_z
    mask = np.nan_to_num(height, nan=0.0) > min_height
    mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] < min_area:
            continue
        sel = lab == i
        hs = height[sel]
        top = np.nanpercentile(hs, 90)
        if not 25 <= top <= 140:
            continue
        cap = sel & (height >= top - 8)  # the piece's crown: directly above its base for an upright piece
        xy = np.nanmean(W[cap][:, :2], axis=0)
        vv, uu = np.nonzero(cap)
        rec = {"x": float(xy[0]), "y": float(xy[1]), "height": float(top), "u": float(uu.mean()), "v": float(vv.mean()),
               "area": int(stats[i, cv2.CC_STAT_AREA])}
        if colour is not None:
            rec["brightness"] = float(cv2.cvtColor(colour, cv2.COLOR_BGR2GRAY)[sel].mean())
        out.append(rec)
    return out


def solve_board(pieces: list[dict[str, Any]], board_z: float) -> dict[str, Any]:
    """From one colour's back rank + pawn rank in their start squares: file axis, rank axis, a1, h1.
    Robust to ±10 mm centroid noise and to a file or a pawn being out of view."""
    if len(pieces) < 8:
        raise ValueError(f"only {len(pieces)} pieces detected; need the two starting rows of one colour in view")
    P = np.array([[p["x"], p["y"]] for p in pieces])
    c = P.mean(axis=0)
    _, _, vt = np.linalg.svd(P - c)
    major, minor = vt[0], vt[1]                      # rows run along the major axis (8 squares vs 1 square apart)
    m = (P - c) @ minor
    split = (m.min() + m.max()) / 2
    rows = [np.nonzero(m <= split)[0], np.nonzero(m > split)[0]]
    if min(len(r) for r in rows) < 3:
        raise ValueError(f"pieces do not form two rows ({[len(r) for r in rows]}); set the pieces up in the start position")
    row_h = [float(np.mean([pieces[i]["height"] for i in r])) for r in rows]
    back, pawn = (0, 1) if row_h[0] > row_h[1] else (1, 0)
    rank_dir = minor * (1 if np.mean(m[rows[pawn]]) > np.mean(m[rows[back]]) else -1)  # rank 1 -> rank 2
    file_dir = np.array([rank_dir[1], -rank_dir[0]])  # right-hand rule: a1 -> h1 -> a8 is counter-clockwise seen from above
    proj = (P - c) @ file_dir
    gaps = []
    for r in rows:
        pr = np.sort(proj[r])
        d = np.diff(pr)
        gaps += [g for g in d if g < 1.5 * np.median(d)]
    s = float(np.median(gaps))
    back_idx = sorted(rows[back], key=lambda i: proj[i])
    heights = np.array([pieces[i]["height"] for i in back_idx])
    order = np.argsort(heights)[::-1]
    k_pos, q_pos = int(order[0]), int(order[1])
    king, queen = back_idx[k_pos], back_idx[q_pos]
    kq = (proj[king] - proj[queen]) / s
    if abs(abs(kq) - 1) > 0.35:
        raise ValueError(f"the two tallest back-rank pieces are {kq:.2f} squares apart, not adjacent; is the position the standard start?")
    kp = P[king]
    a1 = kp - file_dir * s * 4
    side = "white"
    bright = [p.get("brightness") for p in pieces if p.get("brightness") is not None]
    if bright and np.median(bright) < 110:  # dark pieces: these rows are ranks 8 and 7, so the board is the other way round
        side = "black"
        a1 = a1 + file_dir * s * 7 + rank_dir * s * 7
        file_dir, rank_dir = -file_dir, -rank_dir
    h1 = a1 + file_dir * s * 7
    files_resid = float(np.max(np.abs(((proj[back_idx] - proj[king]) / s) - np.round((proj[back_idx] - proj[king]) / s))))
    row_sep = float(abs(np.mean(m[rows[pawn]]) - np.mean(m[rows[back]])))
    return {"spacing_mm": s, "row_separation_mm": row_sep, "file_residual_squares": files_resid, "file_dir": file_dir.tolist(),
            "rank_dir": rank_dir.tolist(), "a1": a1.tolist(), "h1": h1.tolist(), "king_xy": kp.tolist(),
            "king_height": float(heights[k_pos]), "queen_height": float(heights[q_pos]), "king_queen_order_ok": bool(kq > 0),
            "back_rank_count": len(back_idx), "pawn_count": len(rows[pawn]), "pieces": len(pieces), "board_z": board_z, "side_in_view": side,
            "piece_brightness": float(np.median(bright)) if bright else None,
            "row_heights": {"back": row_h[back], "pawns": row_h[pawn]},
            "squares": {f"{FILES[i]}{j + 1}": (a1 + file_dir * s * i + rank_dir * s * j).tolist() for i in range(8) for j in range(8)}}


def refine_with_grid(cap: dict[str, Any], sol: dict[str, Any]) -> dict[str, Any]:
    """Snap the lattice to the printed squares: the piece crowns give axes and the king's file, the colour edges give
    spacing and offset. Samples the image gradient along each candidate grid line through the empty ranks 3-6."""
    import cv2

    K, R, t = cap["K"], cap["R"], cap["t"]
    g = cv2.GaussianBlur(cv2.cvtColor(cap["colour"], cv2.COLOR_BGR2GRAY).astype(np.float32), (5, 5), 0)
    gu, gv = np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)), np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))
    h, w = g.shape
    f, r = np.array(sol["file_dir"]), np.array(sol["rank_dir"])
    a1_0, s0, z = np.array(sol["a1"]), sol["spacing_mm"], sol["board_z"]

    def sample(G, pts):
        px = np.array([pixel_of([p[0], p[1], z], K, R, t) for p in pts])
        u, v = np.round(px[:, 0]).astype(int), np.round(px[:, 1]).astype(int)
        ok = (u >= 2) & (u < w - 2) & (v >= 2) & (v < h - 2)
        return float(G[v[ok], u[ok]].mean()) if ok.sum() >= 10 else 0.0

    ranks_empty = np.linspace(2.0, 5.9, 24)        # 0-based ranks 3-6: no pieces at the start position
    files_all = np.linspace(0.0, 7.0, 36)

    def score_files(a1, s):
        return np.mean([sample(gu, [a1 + f * s * (i - 0.5) + r * s * j for j in ranks_empty]) for i in range(9)])

    def score_ranks(a1, s):
        return np.mean([sample(gv, [a1 + f * s * i + r * s * (j - 0.5) for i in files_all]) for j in (3, 4, 5, 6)])

    best = (-1.0, a1_0, s0)
    for s in np.arange(s0 - 6, s0 + 9, 0.5):
        for df in np.arange(-0.5 * s, 0.5 * s, 1.5):
            a1 = a1_0 + f * df
            sc = score_files(a1, s)
            if sc > best[0]:
                best = (sc, a1, s)
    sc_f, a1, s = best
    best_r = (-1.0, a1)
    for dr in np.arange(-0.5 * s, 0.5 * s, 1.5):
        sc = score_ranks(a1 + r * dr, s)
        if sc > best_r[0]:
            best_r = (sc, a1 + r * dr)
    sc_r, a1 = best_r
    base_f, base_r = score_files(a1_0, s0), score_ranks(a1_0, s0)
    return {**sol, "spacing_mm": float(s), "a1": a1.tolist(), "h1": (a1 + f * s * 7).tolist(), "refined": True,
            "grid_scores": {"files": sc_f, "ranks": sc_r, "files_before": base_f, "ranks_before": base_r},
            "shift_from_pieces_mm": [float((a1 - a1_0) @ f), float((a1 - a1_0) @ r)], "spacing_from_pieces_mm": s0,
            "squares": {f"{FILES[i]}{j + 1}": (a1 + f * s * i + r * s * j).tolist() for i in range(8) for j in range(8)}}


def choose_graveyard(sol: dict[str, Any], s: float) -> list[float]:
    """An empty strip beside the board: 1.6 squares beyond whichever of the a/h sides is nearer the arm base."""
    a1, h1, f = np.array(sol["a1"]), np.array(sol["h1"]), np.array(sol["file_dir"])
    cands = [a1 - f * s * 1.6, h1 + f * s * 1.6]
    return min(cands, key=lambda c: float(np.linalg.norm(c))).tolist()


def overlay(cap: dict[str, Any], sol: dict[str, Any], pieces: list[dict[str, Any]], path: Path) -> Path:
    import cv2

    img = cap["colour"].copy()
    K, R, t = cap["K"], cap["R"], cap["t"]
    f, r, s = np.array(sol["file_dir"]), np.array(sol["rank_dir"]), sol["spacing_mm"]
    a1 = np.array(sol["a1"])
    z = sol["board_z"]
    for i in range(9):
        for j in range(9):
            if i < 8 and j < 8:
                c = a1 + f * s * i + r * s * j
                u, v = pixel_of([c[0], c[1], z], K, R, t)
                if 0 <= u < img.shape[1] and 0 <= v < img.shape[0]:
                    cv2.circle(img, (int(u), int(v)), 4, (0, 0, 255), -1)
                    cv2.putText(img, f"{FILES[i]}{j + 1}", (int(u) + 5, int(v) - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1)
        corner0 = a1 + f * s * (i - 0.5) + r * s * (-0.5)
        corner1 = a1 + f * s * (i - 0.5) + r * s * 7.5
        p0, p1 = pixel_of([*corner0, z], K, R, t), pixel_of([*corner1, z], K, R, t)
        cv2.line(img, (int(p0[0]), int(p0[1])), (int(p1[0]), int(p1[1])), (255, 200, 0), 1)
        corner0 = a1 + f * s * (-0.5) + r * s * (i - 0.5)
        corner1 = a1 + f * s * 7.5 + r * s * (i - 0.5)
        p0, p1 = pixel_of([*corner0, z], K, R, t), pixel_of([*corner1, z], K, R, t)
        cv2.line(img, (int(p0[0]), int(p0[1])), (int(p1[0]), int(p1[1])), (255, 200, 0), 1)
    for p in pieces:
        cv2.circle(img, (int(p["u"]), int(p["v"])), 6, (0, 255, 0), 2)
        cv2.putText(img, f"{p['height']:.0f}", (int(p["u"]) + 6, int(p["v"]) + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 255, 0), 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)
    return path


async def calibrate_from_camera(cam_name: str = "cam", king_height_mm: float | None = None, save: bool = True) -> dict[str, Any]:
    from viam.robot.client import RobotClient

    from amperes.integrations.viam_live import _env
    import os

    address = _env()
    opts = client_options()
    m = await dial_robot(address, opts, "calibrate")
    try:
        cap = await capture(m, cam_name)
    finally:
        await m.close()
    return calibrate_from_capture(cap, king_height_mm, save)


TABLE_Z = -23.0        # table top in the arm's world frame (frame system); the arm, not the camera, is the authority on z
BOARD_THICKNESS = 1.0  # roll-up vinyl board


def calibrate_from_capture(cap: dict[str, Any], king_height_mm: float | None = None, save: bool = True) -> dict[str, Any]:
    W = world_points(cap["depth"], cap["K"], cap["R"], cap["t"])
    measured = float(np.nanmedian(W[..., 2]))  # most of the frame is the board surface
    if not -120 < measured < 80:
        raise ValueError(f"board plane came out at z={measured:.0f} mm, far from the table ({TABLE_Z}): camera frame transform is wrong")
    # The hand-eye preset puts the camera a few cm off in height; pin the board plane to the known table and shift the camera to match.
    board_z = TABLE_Z + BOARD_THICKNESS
    z_shift = board_z - measured
    t = cap["t"] + np.array([0.0, 0.0, z_shift])
    cap = {**cap, "t": t}
    W = world_points(cap["depth"], cap["K"], cap["R"], t)
    sanity = {"camera_world": t.tolist(), "board_z_measured": measured, "camera_z_shift_mm": z_shift,
              "valid_depth_share": float(np.isfinite(W[..., 2]).mean())}
    pieces = detect_pieces(W, board_z, colour=cap["colour"])
    sol = refine_with_grid(cap, solve_board(pieces, board_z))
    s = sol["spacing_mm"]
    grave = choose_graveyard(sol, s)
    out_png = calibration_path().parent / "chessboard_detect.png"
    overlay(cap, sol, pieces, out_png)
    result = {**sol, "graveyard": grave, "overlay": str(out_png), "sanity": sanity, "method": "camera",
              "king_height_mm": king_height_mm or round(sol["king_height"] + 2, 1)}  # crown percentile sits a little under the tip
    if save:
        zf = board_z + TOOL_LENGTH_MM
        for name, xy in (("a1", sol["a1"]), ("h1", sol["h1"]), ("graveyard", grave)):
            BoardCal.save_point(name, {"x": xy[0], "y": xy[1], "z": zf, **DOWN, "theta": 0.0}, {"_frame": "world", "_method": "camera"})
        BoardCal.save_settings(king_height_mm=result["king_height_mm"], detection={k: v for k, v in result.items() if k not in ("overlay",)})
        result["calibration"] = BoardCal.load().describe()
    return result


# --- board state from the look pose (human move detection) ----------------------------------------------------------
VISION_BOARD_Z = TABLE_Z + BOARD_THICKNESS  # board plane in the pinned camera model (grasp heights use their own correction)


def pin_camera(cap: dict[str, Any]) -> dict[str, Any]:
    """Shift the camera's modelled height so the depth-measured board plane sits at VISION_BOARD_Z (see calibrate)."""
    W = world_points(cap["depth"], cap["K"], cap["R"], cap["t"])
    measured = float(np.nanmedian(W[..., 2]))
    return {**cap, "t": cap["t"] + np.array([0.0, 0.0, VISION_BOARD_Z - measured]), "plane_measured": measured}


def board_state(cap: dict[str, Any], cal, patch_px: int = 11, min_height: float = 14.0) -> dict[str, dict[str, Any]]:
    """Per square: occupied (depth above the board at the square centre), colour (white/black from brightness), visible."""
    import cv2

    cap = pin_camera(cap)
    W = world_points(cap["depth"], cap["K"], cap["R"], cap["t"])
    height = W[..., 2] - VISION_BOARD_Z
    grey = cv2.cvtColor(cap["colour"], cv2.COLOR_BGR2GRAY)
    h, w = grey.shape
    out: dict[str, dict[str, Any]] = {}
    for f in range(8):
        for r in range(8):
            sq = f"{FILES[f]}{r + 1}"
            x, y = cal.square_xy(sq)
            u, v = pixel_of([x, y, VISION_BOARD_Z], cap["K"], cap["R"], cap["t"])
            u, v = int(round(u)), int(round(v))
            if not (patch_px <= u < w - patch_px and patch_px <= v < h - patch_px):
                out[sq] = {"visible": False, "occupied": None, "colour": None, "height": None, "brightness": None}
                continue
            hp = height[v - patch_px:v + patch_px + 1, u - patch_px:u + patch_px + 1]
            gp = grey[v - patch_px:v + patch_px + 1, u - patch_px:u + patch_px + 1]
            valid = np.isfinite(hp)
            if valid.mean() < 0.3:
                out[sq] = {"visible": False, "occupied": None, "colour": None, "height": None, "brightness": None}
                continue
            hv = hp[valid]
            top = float(np.percentile(hv, 75))
            occ = top > min_height
            bright = float(gp[valid & (hp > min_height)].mean()) if occ and (valid & (hp > min_height)).any() else float(gp.mean())
            out[sq] = {"visible": True, "occupied": bool(occ), "colour": ("white" if bright > 110 else "black") if occ else None,
                       "height": round(top, 1), "brightness": round(bright, 1)}
    return out


def expected_state(board) -> dict[str, tuple[bool, str | None]]:
    import chess

    exp = {}
    for sq in chess.SQUARES:
        p = board.piece_at(sq)
        exp[chess.square_name(sq)] = (p is not None, (("white" if p.color else "black") if p else None))
    return exp


def mismatches(observed: dict[str, dict[str, Any]], expected: dict[str, tuple[bool, str | None]]) -> list[str]:
    bad = []
    for sq, (occ, col) in expected.items():
        o = observed.get(sq)
        if not o or not o["visible"]:
            continue
        if o["occupied"] != occ or (occ and o["colour"] != col):
            bad.append(sq)
    return bad


def match_human_move(board, observed: dict[str, dict[str, Any]], ignore: set[str] | None = None) -> dict[str, Any]:
    """Which legal move turns `board` into what the camera sees? Returns the best move, its mismatch count, and the runner-up.
    `ignore`: squares known to be misread at rest; they only count for a move that touches them."""
    import chess

    ignore = ignore or set()
    raw_base = mismatches(observed, expected_state(board))
    base = [sq for sq in raw_base if sq not in ignore]
    scored = []
    for mv in board.legal_moves:
        touched = {chess.square_name(mv.from_square), chess.square_name(mv.to_square)}
        board.push(mv)
        try:
            raw = mismatches(observed, expected_state(board))
            bad = [sq for sq in raw if sq not in ignore or sq in touched]
            scored.append((len(bad), len(raw), mv.uci()))
        finally:
            board.pop()
    scored.sort()
    best = scored[0] if scored else (None, None, None)
    second = scored[1] if len(scored) > 1 else (None, None, None)
    return {"unchanged_mismatches": len(base), "unchanged_squares": base, "unchanged_raw": len(raw_base), "raw_squares": raw_base,
            "best": best[2], "best_mismatches": best[0], "best_raw": best[1], "explained": (len(raw_base) - best[1]) if best[1] is not None else None,
            "second": second[2], "second_mismatches": second[0],
            "visible_squares": sum(1 for o in observed.values() if o["visible"])}


# --- image-space grid at the look pose: independent of hand-eye errors ------------------------------------------------
def _periodic_fit(profile: np.ndarray, approx: list[float]) -> tuple[list[float], float]:
    """9 evenly spaced grid lines: search offset and spacing around the camera-model guess for the strongest edge response."""
    prof = np.convolve(profile, np.ones(3) / 3, mode="same")
    n = len(prof)
    step0 = abs(approx[8] - approx[0]) / 8
    sign = 1 if approx[8] > approx[0] else -1
    best = (-1.0, approx, 0.0)
    for s in np.arange(0.85 * step0, 1.15 * step0, 0.5):
        for off in np.arange(approx[0] - 0.6 * step0, approx[0] + 0.6 * step0, 1.0):
            pos = [off + sign * s * k for k in range(9)]
            idx = [int(round(p)) for p in pos if 0 <= round(p) < n]
            if len(idx) < 5:
                continue
            score = float(prof[idx].sum()) / len(idx)
            if score > best[0]:
                best = (score, pos, float(s))
    return best[1], best[2]


def grid_from_image(colour: np.ndarray, approx_centres: dict[str, tuple[float, float]]) -> dict[str, Any]:
    """Square centres in pixels from the printed grid: OpenCV finds exact corner intersections in the empty middle ranks,
    an affine lattice is fitted through them, and `approx_centres` (camera model, tens of px off) names which boundary
    each detected row/column is. Falls back to a periodic edge fit if the corner detector finds nothing."""
    import cv2

    g = cv2.cvtColor(colour, cv2.COLOR_BGR2GRAY)
    h, w = g.shape
    a1, h1, a8 = (np.array(approx_centres[k], dtype=float) for k in ("a1", "h1", "a8"))
    fvec, rvec = (h1 - a1) / 7, (a8 - a1) / 7
    # boundary k (k=0..8) along files: a1 + fvec*(k-0.5); along ranks: a1 + rvec*(k-0.5)
    files_along_u = abs(fvec[0]) > abs(fvec[1])
    r_axis = 0 if not files_along_u else 1          # image axis along which ranks advance
    approx_r_pos = [float((a1 + rvec * (k - 0.5))[r_axis]) for k in range(9)]
    approx_f_pos = [float((a1 + fvec * (k - 0.5))[1 - r_axis]) for k in range(9)]
    lo, hi = sorted([approx_r_pos[2], approx_r_pos[6]])
    lo, hi = max(0, int(lo) - 35), min((w if r_axis == 0 else h), int(hi) + 35)
    sub = g[:, lo:hi] if r_axis == 0 else g[lo:hi, :]
    found = None
    for pat in ((3, 7), (7, 3), (3, 6), (6, 3), (3, 5), (5, 3)):
        try:
            ok, corners = cv2.findChessboardCornersSB(sub, pat, flags=cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY)
        except cv2.error:
            continue
        if ok and corners is not None and len(corners) >= 12:
            c = corners.reshape(-1, 2).astype(float)
            c[:, 0 if r_axis == 0 else 1] += lo
            found = c
            break
    if found is None:
        return _grid_periodic(colour, approx_centres)
    # Index the detected corners relatively along the lattice's own axes (the board is slightly rotated in the image),
    # then fix the absolute labels later from where the pieces stand (`anchor_grid`).
    ncols = pat[0]
    pts = found.reshape(-1, ncols, 2)                      # rows of the detected pattern
    row_dir = np.mean(pts[:, -1, :] - pts[:, 0, :], axis=0)
    col_dir = np.mean(pts[-1, :, :] - pts[0, :, :], axis=0)
    row_dir /= np.linalg.norm(row_dir) or 1.0
    col_dir /= np.linalg.norm(col_dir) or 1.0
    c0 = found[0]
    pr = (found - c0) @ row_dir
    pc = (found - c0) @ col_dir

    def index_along(proj):
        order = np.argsort(proj)
        clusters, cur = [], [order[0]]
        for a, b in zip(order, order[1:]):
            if proj[b] - proj[a] > 20:
                clusters.append(cur)
                cur = []
            cur.append(b)
        clusters.append(cur)
        means = np.array([proj[c].mean() for c in clusters])
        step = float(np.diff(means).min()) if len(means) > 1 else 60.0
        idx = np.zeros(len(proj), dtype=int)
        for c, m in zip(clusters, means):
            idx[c] = int(round((m - means[0]) / step))
        return step, idx

    s_row, i_row = index_along(pr)
    s_col, i_col = index_along(pc)
    # the detector sometimes returns corners two squares apart along one axis; squares are square, so the true step is
    # the smaller of the two and indices are recomputed with it
    step_px = min(s_row, s_col)
    if s_row > 1.5 * step_px:
        i_row = np.round(pr / step_px).astype(int) - int(round(pr.min() / step_px))
    if s_col > 1.5 * step_px:
        i_col = np.round(pc / step_px).astype(int) - int(round(pc.min() / step_px))
    # which lattice axis is the rank axis? the one closer to the camera-model rank direction
    rank_px = np.array([approx_r_pos[8] - approx_r_pos[0], 0.0]) if r_axis == 0 else np.array([0.0, approx_r_pos[8] - approx_r_pos[0]])
    rank_is_row = abs(row_dir @ rank_px) > abs(col_dir @ rank_px)
    r_sign = 1 if (row_dir if rank_is_row else col_dir) @ rank_px > 0 else -1
    file_px = np.array([approx_f_pos[8] - approx_f_pos[0], 0.0]) if r_axis == 1 else np.array([0.0, approx_f_pos[8] - approx_f_pos[0]])
    f_sign = 1 if (col_dir if rank_is_row else row_dir) @ file_px > 0 else -1
    rows = []
    for k, (u, v) in enumerate(found):
        rr, ff = (i_row[k], i_col[k]) if rank_is_row else (i_col[k], i_row[k])
        rows.append((int(rr * r_sign), int(ff * f_sign), float(u), float(v)))
    rmin, fmin = min(r for r, _, _, _ in rows), min(f for _, f, _, _ in rows)
    rows = [(r - rmin, f - fmin, u, v) for r, f, u, v in rows]
    A = np.array([[ri, fi, 1.0] for ri, fi, _, _ in rows])
    U = np.array([u for _, _, u, _ in rows])
    V = np.array([v for _, _, _, v in rows])
    cu = np.linalg.lstsq(A, U, rcond=None)[0]
    cv_ = np.linalg.lstsq(A, V, rcond=None)[0]
    resid = float(np.sqrt(np.mean((A @ cu - U) ** 2 + (A @ cv_ - V) ** 2)))
    centres = {}
    for fi in range(8):
        for ri in range(8):
            x = np.array([ri + 0.5, fi + 0.5, 1.0])
            centres[f"{FILES[fi]}{ri + 1}"] = (float(x @ cu), float(x @ cv_))
    square_px = float((np.hypot(cu[0], cv_[0]) + np.hypot(cu[1], cv_[1])) / 2)
    return {"centres": centres, "method": "corners", "corners": len(found), "fit_rms_px": resid, "square_px": square_px,
            "files_along_u": files_along_u, "affine_u": cu.tolist(), "affine_v": cv_.tolist(), "anchored": False,
            "n_rows_f": len(set(fi for _, fi, _, _ in rows)), "n_cols_r": len(set(ri for ri, _, _, _ in rows)),
            "debug": {"rank_is_row": bool(rank_is_row), "r_sign": int(r_sign), "f_sign": int(f_sign), "s_row": s_row, "s_col": s_col,
                      "i_row": i_row.tolist(), "i_col": i_col.tolist(), "rows": rows[:6]}}


def shift_grid(grid: dict[str, Any], d_rank: int, d_file: int) -> dict[str, Any]:
    """Re-label the lattice: square (r, f) becomes (r + d_rank, f + d_file)."""
    cu, cv_ = np.array(grid["affine_u"]), np.array(grid["affine_v"])
    centres = {}
    for fi in range(8):
        for ri in range(8):
            x = np.array([ri - d_rank + 0.5, fi - d_file + 0.5, 1.0])
            centres[f"{FILES[fi]}{ri + 1}"] = (float(x @ cu), float(x @ cv_))
    return {**grid, "centres": centres, "anchored": True, "shift": [d_rank, d_file]}


def anchor_grid(grid: dict[str, Any], colour: np.ndarray, depth: np.ndarray, expected_board=None) -> dict[str, Any]:
    """Fix the lattice's absolute labels from the pieces: try every shift of up to ±4 squares and keep the one whose
    occupancy/colour map best matches the expected position (the start position unless given)."""
    import chess

    board = expected_board or chess.Board()
    exp = expected_state(board)
    best = None
    for dr in range(-7, 8):
        for df in range(-7, 8):
            g2 = shift_grid(grid, dr, df)
            st = state_from_image(colour, depth, g2["centres"], g2["square_px"])
            vis = [sq for sq, o in st.items() if o["visible"]]
            if len(vis) < 52:
                continue
            bad = len(mismatches(st, exp))
            score = (bad, -len(vis))
            if best is None or score < best[0]:
                best = (score, dr, df, len(vis), bad)
    if best is None:
        return {**grid, "anchored": False}
    _, dr, df, nvis, bad = best
    return {**shift_grid(grid, dr, df), "anchor_mismatches": bad, "anchor_visible": nvis}


def _grid_periodic(colour: np.ndarray, approx_centres: dict[str, tuple[float, float]]) -> dict[str, Any]:
    import cv2

    g = cv2.GaussianBlur(cv2.cvtColor(colour, cv2.COLOR_BGR2GRAY).astype(np.float32), (5, 5), 0)
    gu = np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3))
    gv = np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))
    a1, h1, a8 = approx_centres["a1"], approx_centres["h1"], approx_centres["a8"]
    file_vec = ((h1[0] - a1[0]) / 7, (h1[1] - a1[1]) / 7)
    rank_vec = ((a8[0] - a1[0]) / 7, (a8[1] - a1[1]) / 7)
    files_along_u = abs(file_vec[0]) > abs(file_vec[1])
    f_axis, r_axis = (0, 1) if files_along_u else (1, 0)
    approx_f = [a1[f_axis] + file_vec[f_axis] * (k - 0.5) for k in range(9)]
    approx_r = [a1[r_axis] + rank_vec[r_axis] * (k - 0.5) for k in range(9)]
    prof_f = (gu if f_axis == 0 else gv).sum(axis=1 - f_axis)
    prof_r = (gu if r_axis == 0 else gv).sum(axis=1 - r_axis)
    lines_f, step_f = _periodic_fit(prof_f, approx_f)
    lines_r, step_r = _periodic_fit(prof_r, approx_r)
    centres = {}
    for fi in range(8):
        for ri in range(8):
            cf = (lines_f[fi] + lines_f[fi + 1]) / 2
            cr = (lines_r[ri] + lines_r[ri + 1]) / 2
            u, v = (cf, cr) if f_axis == 0 else (cr, cf)
            centres[f"{FILES[fi]}{ri + 1}"] = (float(u), float(v))
    return {"centres": centres, "method": "periodic", "square_px": float((step_f + step_r) / 2), "files_along_u": files_along_u}


def state_from_image(colour: np.ndarray, depth: np.ndarray, centres: dict[str, tuple[float, float]], square_px: float,
                     min_height: float = 10.0) -> dict[str, dict[str, Any]]:
    """Occupancy = depth rises above the board plane at the square centre; colour = brightness of those pixels."""
    import cv2

    d = depth.astype(np.float32)
    d[d <= 0] = np.nan
    # board plane: least-squares plane through pixels close to the median depth (the board dominates the frame)
    h, w = d.shape
    vv, uu = np.mgrid[0:h, 0:w]
    med = np.nanmedian(d)
    sel = np.isfinite(d) & (np.abs(d - med) < 25)
    A = np.stack([uu[sel], vv[sel], np.ones(sel.sum())], axis=1).astype(np.float64)
    coef = np.linalg.lstsq(A[::7], d[sel][::7].astype(np.float64), rcond=None)[0]
    plane = coef[0] * uu + coef[1] * vv + coef[2]
    height = plane - d  # mm above the board
    grey = cv2.cvtColor(colour, cv2.COLOR_BGR2GRAY)
    r = max(8, int(0.36 * square_px))  # most of the square: pieces near the frame edge lean outward in the image
    out: dict[str, dict[str, Any]] = {}
    for sq, (u, v) in centres.items():
        u, v = int(round(u)), int(round(v))
        if not (r <= u < w - r and r <= v < h - r):
            out[sq] = {"visible": False, "occupied": None, "colour": None, "height": None, "brightness": None}
            continue
        hp = height[v - r:v + r + 1, u - r:u + r + 1]
        gp = grey[v - r:v + r + 1, u - r:u + r + 1]
        valid = np.isfinite(hp)
        if valid.mean() < 0.3:
            out[sq] = {"visible": False, "occupied": None, "colour": None, "height": None, "brightness": None}
            continue
        top = float(np.percentile(hp[valid], 92))
        peak = float(np.percentile(hp[valid], 99))  # the narrow crown of a king or knight occupies few pixels
        occ = top > min_height
        piece = valid & (hp > min_height * 0.7)
        bright = float(gp[piece].mean()) if occ and piece.any() else float(gp[valid].mean())
        out[sq] = {"visible": True, "occupied": bool(occ), "colour": ("white" if bright > 110 else "black") if occ else None,
                   "height": round(top, 1), "height_peak": round(peak, 1), "brightness": round(bright, 1)}
    return out


def draw_state(colour: np.ndarray, centres: dict[str, tuple[float, float]], state: dict[str, dict[str, Any]], path: Path) -> Path:
    import cv2

    img = colour.copy()
    for sq, (u, v) in centres.items():
        o = state.get(sq, {})
        col = (0, 0, 255) if not o.get("visible") else (255, 255, 255) if o.get("colour") == "white" else (0, 0, 0) if o.get("colour") == "black" else (0, 200, 0)
        cv2.circle(img, (int(u), int(v)), 7, col, 2)
        cv2.putText(img, sq, (int(u) + 8, int(v) + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 0, 255), 1)
    cv2.imwrite(str(path), img)
    return path


# --- grasp refinement from the wrist camera at the approach pose -------------------------------------------------------
def refine_grasp_xy(cap: dict[str, Any], target_xy: tuple[float, float], z_top: float, colour: str,
                    search_mm: float = 60.0, max_shift_mm: float = 25.0) -> dict[str, Any]:
    """From above the target square: find the piece of the given colour nearest the expected spot and return the world xy
    shift that puts the gripper over its centroid. Colour image only (the depth sensor cannot see this close)."""
    import cv2

    K, R, t = cap["K"], cap["R"], cap["t"]
    img = cap["colour"]
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if colour == "white":
        mask = ((grey > 150) & (hsv[..., 1] < 70)).astype(np.uint8)
    else:
        mask = (grey < 40).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    u0, v0 = pixel_of([target_xy[0], target_xy[1], z_top], K, R, t)
    # mm per pixel at the piece-top plane
    cam = R.T @ (np.array([target_xy[0], target_xy[1], z_top]) - t)
    depth = float(cam[2])
    if not 60 < depth < 700:
        return {"ok": False, "reason": f"implausible camera distance {depth:.0f} mm"}
    mm_px = depth / K["fx"]
    r_px = int(search_mm / mm_px)
    n, lab, stats, cent = cv2.connectedComponentsWithStats(mask, connectivity=8)
    best = None
    for i in range(1, n):
        area = stats[i, cv2.CC_STAT_AREA]
        if area < 150:
            continue
        cu, cv_ = cent[i]
        d = float(np.hypot(cu - u0, cv_ - v0))
        if d <= r_px and (best is None or d < best[0]):
            best = (d, cu, cv_, int(area))
    if best is None:
        return {"ok": False, "reason": "no piece blob near the target", "expected_px": [u0, v0], "search_px": r_px}
    _, cu, cv_, area = best
    # pixel offsets -> world: move along the camera's image axes expressed in world coordinates
    p_exp = R @ np.array([(u0 - K["cx"]) * depth / K["fx"], (v0 - K["cy"]) * depth / K["fy"], depth]) + t
    p_obs = R @ np.array([(cu - K["cx"]) * depth / K["fx"], (cv_ - K["cy"]) * depth / K["fy"], depth]) + t
    dx, dy = float(p_obs[0] - p_exp[0]), float(p_obs[1] - p_exp[1])
    mag = float(np.hypot(dx, dy))
    if mag > max_shift_mm:
        dx, dy = dx * max_shift_mm / mag, dy * max_shift_mm / mag
    return {"ok": True, "dx": dx, "dy": dy, "shift_mm": min(mag, max_shift_mm), "raw_shift_mm": mag, "blob_px": [float(cu), float(cv_)],
            "expected_px": [float(u0), float(v0)], "area": area, "depth_mm": depth}


# --- anchoring without a known position: the board's own extent ----------------------------------------------------------
def anchor_by_extent(grid: dict[str, Any], colour: np.ndarray, approx_a1: tuple[float, float] | None = None) -> dict[str, Any]:
    """Label the lattice so the 8x8 block covers the checkerboard: cells inside the board alternate strongly between light
    and dark, the vinyl margin outside is uniform. Orientation (which corner is a1) is kept from the camera-model guess."""
    import cv2

    g = cv2.cvtColor(colour, cv2.COLOR_BGR2GRAY).astype(np.float32)
    h, w = g.shape
    cu, cv_ = np.array(grid["affine_u"]), np.array(grid["affine_v"])
    sq = grid.get("square_px", 57.0)
    r = max(4, int(0.25 * sq))

    def cell_mean(ri, fi):
        x = np.array([ri + 0.5, fi + 0.5, 1.0])
        u, v = int(round(float(x @ cu))), int(round(float(x @ cv_)))
        if not (r <= u < w - r and r <= v < h - r):
            return None
        return float(g[v - r:v + r + 1, u - r:u + r + 1].mean())

    best = None
    for dr in range(-8, 9):
        for df in range(-8, 9):
            means = [[cell_mean(ri - dr, fi - df) for fi in range(8)] for ri in range(8)]
            vals = [m for row in means for m in row if m is not None]
            if len(vals) < 64:  # the look pose sees the whole board: a block with cells off-frame is not the board
                continue
            contrast = sum(abs(means[ri][fi] - means[rj][fj]) for ri in range(8) for fi in range(8)
                           for rj, fj in ((ri + 1, fi), (ri, fi + 1)) if rj < 8 and fj < 8)
            # the margin is uniform: cells just outside the block should NOT alternate
            outside = [cell_mean(ri - dr, fi - df) for ri in (-1, 8) for fi in range(8)] + [cell_mean(ri - dr, fi - df) for fi in (-1, 8) for ri in range(8)]
            outside = [m for m in outside if m is not None]
            penalty = float(np.std(outside)) * 8 if len(outside) >= 8 else 0.0
            score = contrast - penalty
            if approx_a1 is not None:  # tie-break towards the camera model's guess
                x = np.array([0.5 - dr, 0.5 - df, 1.0])
                score -= 0.02 * float(np.hypot(float(x @ cu) - approx_a1[0], float(x @ cv_) - approx_a1[1]))
            if best is None or score > best[0]:
                best = (score, dr, df, len(vals))
    if best is None:
        return {**grid, "anchored": False}
    _, dr, df, nvis = best
    return {**shift_grid(grid, dr, df), "anchor_method": "extent", "anchor_contrast": best[0], "anchor_visible": nvis, "anchor_mismatches": None}


PIECE_BY_HEIGHT = [(50.0, "p"), (58.0, "r"), (66.0, "n"), (78.0, "b"), (999.0, "k")]  # upper bounds (mm, scaled set)


def classify_pieces(state: dict[str, dict[str, Any]], king_height_mm: float = 85.0) -> dict[str, dict[str, Any]]:
    """Piece type per occupied square from the measured height (king and queen share a class 'k'; resolved by counts)."""
    scale = king_height_mm / 95.0
    peaks = [o.get("height_peak") or o.get("height") or 0.0 for o in state.values() if o.get("occupied")]
    # depth at ~55 cm under-reads piece tops (narrow crowns, averaging): the tallest piece seen is a king or queen
    depth_gain = (king_height_mm / max(peaks)) if peaks and max(peaks) > 30 else 1.0
    depth_gain = min(max(depth_gain, 1.0), 1.6)
    out = {}
    for sq, o in state.items():
        if not o.get("occupied"):
            continue
        hmm = (o.get("height_peak") or o.get("height") or 0.0) * depth_gain / scale
        kind = next(k for ub, k in PIECE_BY_HEIGHT if hmm <= ub)
        sym = kind.upper() if o.get("colour") == "white" else kind
        out[sq] = {"symbol": sym, "height": o.get("height"), "colour": o.get("colour")}
    return out
