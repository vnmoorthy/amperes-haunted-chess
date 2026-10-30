"""Stand a fallen piece up the way a person does: hold it loosely just under its head and lift.

The fingers straddle the lying piece's neck, close to a gap smaller than the head but wider than the neck (a cage, not a
grip), and rise slowly: the head catches on the fingers and the piece pivots on its base until it stands straight, base
on the board. Opening the fingers leaves it standing where it was; a normal pick-and-place then takes it home.

The survey's depth image gives the lying piece's long axis and which end is the head (the narrower end). If the gap
misses either way (head slips through, or the neck is gripped), the piece just stays down: nothing is thrown.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

import numpy as np

from amperes.integrations import chess_robot as CR

CAGE_GAP_MM = 14.0          # narrower than every head in the set, wider than every neck
NECK_FROM_TIP_MM = 12.0     # where to hold, measured from the head end
FINGERTIP_LOW_MM = 5.0      # fingertips this far above the board while caging (the envelope floor is 5 mm)


def lying_shape(W: np.ndarray, board_z: float, x: float, y: float, radius: float = 60.0) -> dict[str, Any] | None:
    """The fallen piece's footprint near (x, y) from the depth image's world points: long axis, length, and which end is
    the head (narrower). None when the footprint is not clearly elongated."""
    pts = W.reshape(-1, 3)
    ok = np.isfinite(pts).all(axis=1)
    pts = pts[ok]
    h = pts[:, 2] - board_z
    near = (np.hypot(pts[:, 0] - x, pts[:, 1] - y) < radius) & (h > 6.0) & (h < 45.0)
    sel = pts[near]
    if len(sel) < 80:
        return None
    xy = sel[:, :2]
    c = xy.mean(axis=0)
    cov = np.cov((xy - c).T)
    w, v = np.linalg.eigh(cov)
    axis = v[:, 1] / np.linalg.norm(v[:, 1])
    t = (xy - c) @ axis
    s = (xy - c) @ np.array([-axis[1], axis[0]])
    t0, t1 = np.percentile(t, 2), np.percentile(t, 98)
    length = float(t1 - t0)
    if length < 30.0 or w[1] < 2.0 * w[0]:
        return None  # not a lying piece (or a cluster of things)
    q = 0.25 * length
    end0 = s[t < t0 + q]
    end1 = s[t > t1 - q]
    if len(end0) < 10 or len(end1) < 10:
        return None
    w0 = float(np.percentile(end0, 95) - np.percentile(end0, 5))
    w1 = float(np.percentile(end1, 95) - np.percentile(end1, 5))
    p0, p1 = c + axis * t0, c + axis * t1
    head, base = (p0, p1) if w0 < w1 else (p1, p0)
    return {"centre": [float(c[0]), float(c[1])], "head": [float(head[0]), float(head[1])], "base": [float(base[0]), float(base[1])],
            "length": length, "head_width": min(w0, w1), "base_width": max(w0, w1), "points": int(len(sel))}


def righting_poses(cal: CR.BoardCal, shape: dict[str, Any], current_theta: float | None = None) -> dict[str, dict[str, float]]:
    """approach (high), cage (fingertips 5 mm up, across the neck), stand (fingertips where the upright piece's grip point
    is: the piece then stands with its base on the board)."""
    head, base = np.array(shape["head"]), np.array(shape["base"])
    u = (base - head) / max(1e-6, float(np.linalg.norm(base - head)))
    g = head + u * NECK_FROM_TIP_MM
    phi = math.degrees(math.atan2(u[1], u[0]))
    ref = current_theta if current_theta is not None else cal.theta_deg
    # fingers close across the piece: closing axis at phi + 90; of the two equivalent wrist angles take the nearer one
    cands = [phi + 90.0 + k * 180.0 for k in range(-3, 4)]
    theta = min(cands, key=lambda a: abs(((a - ref) + 180.0) % 360.0 - 180.0) + (0 if -270 <= a <= 270 else 1000))
    floor = cal.board_top_z + CR.TOOL_LENGTH_MM  # model flange z with the fingertips on the board
    stand_h = max(FINGERTIP_LOW_MM + 10.0, shape["length"] - NECK_FROM_TIP_MM + 2.0)
    common = {"x": float(g[0]), "y": float(g[1]), **CR.DOWN, "theta": float(theta)}
    return {"approach": {**common, "z": cal.transport_z}, "cage": {**common, "z": floor + FINGERTIP_LOW_MM},
            "stand": {**common, "z": floor + stand_h}, "grip_xy": [float(g[0]), float(g[1])]}


async def right_piece(sess: CR.ChessSession, cal: CR.BoardCal, shape: dict[str, Any], log=print) -> dict[str, Any]:
    """Cage the neck, lift until the piece stands, let go. Returns what happened (the next survey says if it worked)."""
    ctl = sess.ctl
    try:
        cur = await ctl.pose()
        ref = cur.get("theta")
    except Exception:
        ref = None
    poses = righting_poses(cal, shape, ref)
    log(f"  standing up the piece at ({shape['centre'][0]:.0f}, {shape['centre'][1]:.0f}): {shape['length']:.0f} mm long, "
        f"head at ({shape['head'][0]:.0f}, {shape['head'][1]:.0f})")
    sess.session["moving"] = True
    try:
        await ctl._gripper("open")
        await ctl._move(poses["approach"], "right_approach", 40)
        await ctl._descend(poses["cage"], "right_cage", 40)
        if ctl.execute:
            from amperes.integrations.viam_control import timed

            await timed(ctl.gripper.do_command({"set": float(CAGE_GAP_MM * 10.0)}), 15)  # G2: ~10 units per mm of gap
            await asyncio.sleep(0.4)
        await ctl._move(poses["stand"], "right_lift", 40)  # slow: inside the speed profile's slow zone
        await asyncio.sleep(0.3)
        await ctl._gripper("open")
        await ctl._move(poses["approach"], "right_retreat", 40)
        return {"ok": True, "grip_xy": poses["grip_xy"], "theta": poses["approach"]["theta"]}
    except Exception as exc:
        try:
            await ctl._gripper("open")
        except Exception:
            pass
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    finally:
        sess.session["moving"] = False


def open_square(cal: CR.BoardCal, physical: dict[str, str]) -> str | None:
    """A middle square (ranks 3-6) with nothing on it or within 1.6 squares: room to stand a piece up."""
    import chess

    from amperes.integrations import chess_lifecycle as L

    best = None
    cx, cy = cal.square_xy("d4")
    for sq in chess.SQUARES:
        name = chess.square_name(sq)
        if not 2 <= chess.square_rank(sq) <= 5:
            continue
        x, y = cal.square_xy(name)
        if any(math.dist((x, y), L.location_xy(cal, k)) < 1.6 * cal.square_mm for k in physical):
            continue
        d = math.dist((x, y), (cx, cy))
        if best is None or d < best[0]:
            best = (d, name)
    return best[1] if best else None


async def isolate_and_right(sess: CR.ChessSession, cal: CR.BoardCal, shape: dict[str, Any], physical: dict[str, str], log=print) -> dict[str, Any]:
    """Carry the fallen piece, still lying, to an open middle square, then stand it up there where nothing can be hit."""
    target = open_square(cal, physical)
    if target is None:
        return {"ok": False, "error": "no open square to stand it up on"}
    head, base = np.array(shape["head"]), np.array(shape["base"])
    mid = (head + base) / 2.0
    u = (base - head) / max(1e-6, float(np.linalg.norm(base - head)))
    phi = math.degrees(math.atan2(u[1], u[0]))
    try:
        ref = (await sess.ctl.pose()).get("theta")
    except Exception:
        ref = cal.theta_deg
    theta = min((phi + 90.0 + k * 180.0 for k in range(-3, 4)), key=lambda a: abs(((a - ref) + 180.0) % 360.0 - 180.0))
    floor = cal.board_top_z + CR.TOOL_LENGTH_MM
    tx, ty = cal.square_xy(target)
    grip_z = floor + FINGERTIP_LOW_MM + 3.0
    pick = {"x": float(mid[0]), "y": float(mid[1]), **CR.DOWN, "theta": float(theta)}
    drop = {"x": tx, "y": ty, **CR.DOWN, "theta": float(theta)}
    log(f"  carrying the fallen piece at ({mid[0]:.0f}, {mid[1]:.0f}) to open square {target} to stand it up")
    ctl = sess.ctl
    sess.session["moving"] = True
    try:
        await ctl._gripper("open")
        await ctl._move({**pick, "z": cal.transport_z}, "iso_approach", 40)
        await ctl._descend({**pick, "z": grip_z}, "iso_grip", 40)
        await ctl._gripper("grab")
        pos = await ctl.grip_position()
        if pos is not None and pos < ctl.empty_grip_below:
            await ctl._gripper("open")
            await ctl._move({**pick, "z": cal.transport_z}, "iso_retreat", 40)
            return {"ok": False, "error": "the fingers met nothing"}
        await ctl._move({**pick, "z": cal.transport_z}, "iso_lift", 40)
        await ctl._move({**drop, "z": cal.transport_z}, "iso_carry", 40)
        await ctl._descend({**drop, "z": grip_z + 2.0}, "iso_lower", 40)
        await ctl._gripper("open")
        await ctl._move({**drop, "z": cal.transport_z}, "iso_up", 40)
    except Exception as exc:
        try:
            await ctl._gripper("open")
        except Exception:
            pass
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:100]}"}
    finally:
        sess.session["moving"] = False
    off = np.array([tx, ty]) - mid
    moved = {**shape, "head": list(head + off), "base": list(base + off), "centre": [tx, ty]}
    r = await right_piece(sess, cal, moved, log)
    return {**r, "square": target}
