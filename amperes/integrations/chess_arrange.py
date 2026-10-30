"""Set the board up in the start position by itself: look, recognise, plan, move, look again, until it is right.

Each round:
1. The wrist camera locks the printed grid from the look pose and reads, per square, whether a piece stands there and
   its colour (depth plus brightness: reliable).
2. Claude reads what each piece is from the overhead photo with every square labelled, plus the table camera's side view
   (chess_claude_vision.read_board). Depth decides occupancy; Claude decides the type; the set's make-up (one king, at
   most two queens, two rooks, two bishops, two knights and eight pawns per colour) settles impossible reads, the
   lowest-confidence piece giving way.
3. The planner (chess_lifecycle.plan_reset) orders the pick-and-places so the gripper never crowds a neighbour.
4. The arm executes them one by one and stops at the first failure; the next round looks again, so a misread or a
   slipped piece corrects itself.
Rounds end when the board shows the start position, or a round makes no progress.

Spots beside the board are used only with --slots-clear (a person has checked that area is empty): unseen spots are
never placed into. Motion needs --ack, typed for the person standing at the e-stop.

    python -m amperes.integrations.chess_arrange --plan-only
    python -m amperes.integrations.chess_arrange --ack "I am at sfhack1-main with the e-stop in reach"
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import chess

from amperes.integrations import chess_lifecycle as L
from amperes.integrations import chess_robot as CR

SET_COUNTS = {"k": 1, "q": 2, "r": 2, "b": 2, "n": 2, "p": 8}  # per colour, spare queen included
CONFUSABLE = {"k": ["q", "b"], "q": ["k", "b"], "r": ["p", "n"], "p": ["r", "b", "n"], "b": ["p", "q", "n"], "n": ["p", "b", "r"]}


def fuse(obs: dict[str, dict[str, Any]], read: dict[str, dict[str, Any]], obstructions: dict[str, str] | None = None
         ) -> tuple[dict[str, str], dict[str, float], list[str]]:
    """A square counts as occupied when either the depth camera or Claude sees something there (a missed piece is
    dangerous, an extra obstacle is not). Claude names the piece and its colour (the depth camera's brightness test
    misjudged a cream pawn as black on 30 Oct). Anything nobody can name, and every square Claude reports as obstructed
    (a fallen piece, a piece across a border, a hand), becomes '?': never moved, never placed on, kept clear of."""
    layout: dict[str, str] = {}
    conf: dict[str, float] = {}
    notes: list[str] = []
    obstructions = obstructions or {}
    for sq in (chess.square_name(s) for s in chess.SQUARES):
        o = obs.get(sq, {})
        c = read.get(sq)
        named = c is not None and c.get("symbol") not in (None, "?")
        depth_occ = bool(o.get("occupied"))
        if named:
            sym = c["symbol"]
            colour = o.get("colour")
            if depth_occ and colour in ("white", "black") and sym.isupper() != (colour == "white"):
                notes.append(f"{sq}: depth brightness says {colour}, Claude says {sym}; keeping Claude's colour")
            if not depth_occ:
                notes.append(f"{sq}: Claude sees {sym} that the depth camera missed; counted as occupied")
            layout[sq] = sym
            conf[sq] = float(c.get("confidence", 0.5))
        elif depth_occ or c is not None or sq in obstructions:
            layout[sq] = "?"
            conf[sq] = 0.0
            notes.append(f"{sq}: {obstructions.get(sq) or 'something nobody can name'}; left alone, nothing placed there")
    return layout, conf, notes


def enforce_counts(layout: dict[str, str], conf: dict[str, float]) -> list[str]:
    """Make the read consistent with the set: per colour at most SET_COUNTS of each kind. The least confident surplus
    pieces become the most similar kind that is still short. Changes `layout` in place; returns what changed."""
    changes = []
    for white in (True, False):
        def count(k):
            return sum(1 for s in layout.values() if s != "?" and s.lower() == k and s.isupper() == white)

        for _ in range(16):
            over = [k for k in SET_COUNTS if count(k) > SET_COUNTS[k]]
            if not over:
                break
            k = over[0]
            squares = sorted((sq for sq, s in layout.items() if s != "?" and s.lower() == k and s.isupper() == white), key=lambda sq: conf.get(sq, 0))
            sq = squares[0]
            short = [t for t in CONFUSABLE[k] if count(t) < SET_COUNTS[t]] or [t for t in SET_COUNTS if count(t) < SET_COUNTS[t]]
            if not short:
                break
            new = short[0].upper() if white else short[0]
            changes.append(f"{sq}: {layout[sq]} -> {new} (more {('white ' if white else 'black ') + CR.PIECE_NAME[k]}s than the set has)")
            layout[sq] = new
            conf[sq] = conf.get(sq, 0) * 0.5
    return changes


def start_target(slots_clear: bool) -> dict[str, str]:
    t = L.start_target()
    if not slots_clear:  # the spare queens' spots have not been checked: do not plan to place anything there
        t = {k: v for k, v in t.items() if L.is_square(k)}
    return t


async def grab_colour(m, name: str):
    """Colour image only (the table camera has no frame in the frame system, so chess_vision.capture cannot place it)."""
    import cv2
    import numpy as np
    from viam.components.camera import Camera

    from amperes.integrations.viam_control import timed

    imgs, _ = await timed(Camera.from_robot(m, name).get_images(), 20)
    for im in imgs:
        if "jpeg" in str(im.mime_type) or "png" in str(im.mime_type):
            return cv2.imdecode(np.frombuffer(im.data, np.uint8), cv2.IMREAD_COLOR)
    raise RuntimeError(f"{name} returned no colour image")


async def recognise(sess: CR.ChessSession, out_dir: Path, rnd: int, log=print, effort: str = "high") -> dict[str, Any]:
    """Look, read the depth state, ask Claude, fuse. Saves the labelled photos and the read."""
    import cv2

    from amperes.integrations import chess_claude_vision as CCV
    from amperes.integrations import chess_vision as CV

    await sess.look(f"arrange_r{rnd}", rnd)
    obs = await sess.observe(save_debug=True)
    cap = await CV.capture(sess.m, sess.spec.camera_names[0])
    labelled = CCV.label_squares(cap["colour"], sess.grid["centres"])
    side = []
    if len(sess.spec.camera_names) > 1:
        try:
            side.append(await grab_colour(sess.m, sess.spec.camera_names[1]))
        except Exception as exc:
            log(f"table camera unavailable: {type(exc).__name__}: {str(exc)[:80]}")
    img_dir = out_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(img_dir / f"round{rnd}_overhead_labelled.jpg"), labelled)
    for i, s in enumerate(side):
        cv2.imwrite(str(img_dir / f"round{rnd}_side{i}.jpg"), s)
    occupied = sorted(sq for sq, o in obs.items() if o.get("occupied"))
    log(f"round {rnd}: depth sees {len(occupied)} pieces; asking Claude what they are")
    read: dict[str, dict[str, Any]] = {}
    call: dict[str, Any] = {}
    obstructions: dict[str, str] = {}
    if CCV.available():
        try:
            r = CCV.read_board(labelled, side, effort=effort)
            read, obstructions = r["pieces"], r.get("obstructions", {})
            call = {**r["call"], "notes": r["notes"], "off_board": r["off_board"], "obstructions": obstructions}
            log(f"Claude: {r['board_fen']} ({r['call']['latency_s']:.0f} s); obstructed: {sorted(obstructions)}")
        except Exception as exc:
            log(f"Claude read failed: {type(exc).__name__}: {str(exc)[:100]}")
    else:
        log("no ANTHROPIC_API_KEY: piece types unknown, nothing will be moved")
    layout, conf, notes = fuse(obs, read, obstructions)
    changes = enforce_counts(layout, conf)
    rec = {"round": rnd, "t": time.time(), "occupied": occupied, "claude": read, "claude_call": call, "layout": layout,
           "confidence": conf, "notes": notes + changes}
    (out_dir / f"round{rnd}_recognised.json").write_text(json.dumps(rec, indent=1, default=str))
    for n in notes + changes:
        log("  " + n)
    return rec


HELD_CONTEXT = ("This photo comes from a camera standing on the table beside the chessboard. The robot arm's gripper (a white "
                "and grey body with two black fingers) is holding one chess piece up in the air above the board. Identify "
                "only the piece held between the gripper's fingers; ignore the pieces standing on the board. If the held "
                "piece is hidden or out of the picture, answer '?'.")


def _overrides_path():
    from amperes import store

    return store.home() / "robot" / "piece_overrides.json"


def load_overrides() -> dict[str, str]:
    """A person's word on what a piece is (square -> FEN letter): beats the camera."""
    try:
        return json.loads(_overrides_path().read_text())
    except (FileNotFoundError, ValueError):
        return {}


def move_override(src: str, dst: str, symbol: str | None = None) -> None:
    """The piece moved: its known identity moves with it. A piece the robot itself placed is remembered as what it
    placed (it knew the piece when it picked it), so later surveys do not re-guess it."""
    o = load_overrides()
    known = o.pop(src, None) or symbol
    if known and L.is_square(dst):
        o[dst] = known
    if L.is_square(src):
        o.pop(src, None)
    _overrides_path().write_text(json.dumps(o))


def board_from(world: L.World) -> chess.Board:
    board = chess.Board(None)
    for loc, sym in world.physical.items():
        if L.is_square(loc) and sym != "?":
            board.set_piece_at(chess.parse_square(loc), chess.Piece.from_symbol(sym))
    return board


def make_hand_guard(sess: CR.ChessSession, log, wait_s: float = 20.0):
    """Before the gripper goes down: the wrist camera looks straight at the spot. Anything more than 11 cm above the
    board in the middle of the view (no chess piece is taller than 9 cm) is taken to be a hand: hold still until it
    has gone, and stop the session if it stays."""
    import numpy as np

    from amperes.integrations import chess_vision as CV

    async def guard(label: str):
        t0 = time.time()
        warned = False
        while True:
            try:
                cap = await CV.capture(sess.m, sess.spec.camera_names[0])
            except Exception:
                return  # no picture: rely on the arm's own collision stop rather than block the run
            d = cap["depth"].astype(np.float32)
            h, w = d.shape
            core = d[int(h * 0.2):int(h * 0.8), int(w * 0.2):int(w * 0.8)]
            valid = core[core > 0]
            if valid.size < 1000:
                return
            board = float(np.percentile(valid, 90))  # the farthest surface in view: the board
            hand = float(((valid > 0) & (valid < board - 110.0)).mean())
            if hand < 0.02:
                return
            if not warned:
                log(f"      HAND (or something tall) under the gripper before {label}: holding still")
                warned = True
            if time.time() - t0 > wait_s:
                raise CR.SafetyError("something stayed under the gripper; stopping")
            await asyncio.sleep(1.0)

    return guard


def make_inspector(sess: CR.ChessSession, cal: CR.BoardCal, world: L.World, log, seen: list[dict[str, Any]], min_conf: float = 0.7):
    """Like a person picking a piece up and looking at it: with the piece lifted, the table camera photographs it, Claude
    names it, and a piece that is not what the plan assumed is carried to its own home (or a free middle square)."""
    import math

    from amperes.integrations import chess_claude_vision as CCV

    async def inspect(i: int, src_pose: dict[str, float], dst_pose: dict[str, float]):
        op = getattr(sess, "_current_op", None)
        if op is None or not CCV.available() or len(sess.spec.camera_names) < 2:
            return None
        try:
            img = await grab_colour(sess.m, sess.spec.camera_names[1])
            # off the event loop: the telemetry watchdog must keep running while Claude looks
            r = await asyncio.to_thread(CCV.identify_pieces, {"held": img}, HELD_CONTEXT, "low")
        except Exception as exc:
            log(f"      could not look at the lifted piece: {type(exc).__name__}: {str(exc)[:80]}")
            return None
        h = r.get("held") or {}
        sym, conf = h.get("symbol"), float(h.get("confidence", 0.0))
        try:
            import cv2

            cv2.imwrite(str(sess.spec.out_dir / "images" / f"held_{i:03d}_{op.src}_{sym}.jpg"), img)
        except Exception:
            pass
        seen.append({"trial": i, "src": op.src, "planned": op.symbol, "seen": sym, "confidence": conf, "reason": h.get("reason", ""),
                     "latency_s": (r.get("_call") or {}).get("latency_s")})
        log(f"      lifted piece looks like {sym} ({conf:.2f}): {str(h.get('reason', ''))[:90]}")
        if sym not in CCV.SYMBOLS or conf < min_conf or sym == op.symbol:
            return None
        phys = {k: v for k, v in world.physical.items() if k != op.src}
        grip_h = src_pose["z"] - (cal.board_top_z + CR.TOOL_LENGTH_MM)  # the piece stays held at the height it was picked
        x0, y0 = L.location_xy(cal, op.src)

        def dist(sq: str) -> float:
            x, y = cal.square_xy(sq)
            return math.hypot(x - x0, y - y0)

        homes = sorted((s for s in L.HOME[sym] if s not in phys), key=dist)
        middle = sorted((chess.square_name(s) for s in chess.SQUARES if 2 <= chess.square_rank(s) <= 5 and chess.square_name(s) not in phys), key=dist)
        for cand in homes + middle:
            if L.spot_penetration(cal, phys, cand, grip_h)[0] > 0:
                continue
            world.physical[op.src] = sym
            log(f"      it is a {sym}, not a {op.symbol}: taking it to {cand} instead of {op.dst}")
            op.symbol, op.dst, op.note = sym, cand, (op.note + " re-identified when lifted").strip()
            pose = sess._pose_for(op, "dst", board_from(world))
            return {**pose, "z": src_pose["z"], "_name": cand}
        log("      no clean spot for it; keeping the plan")
        return None

    return inspect


async def recognise_by_survey(sess: CR.ChessSession, cal: CR.BoardCal, out_dir: Path, rnd: int, log=print) -> dict[str, Any]:
    """The survey (six close views over the board and 10 cm around it) instead of the single look."""
    from amperes.integrations import chess_survey as SV

    res = await SV.survey(sess, cal, out_dir / f"survey_r{rnd}", log)
    pieces = res["pieces"]
    rec = {"round": rnd, "t": time.time(), "layout": res["layout"], "lying": res.get("lying", []),
           "pieces": [{k: p.get(k) for k in ("id", "x", "y", "height", "square", "on_board", "upright", "reachable", "colour", "symbol",
                                             "confidence", "reason", "seen", "colour_note")} for p in pieces],
           "notes": [f"{p['id']} at ({p['x']:.0f}, {p['y']:.0f}) h {p['height']:.0f} mm: " + ("lying down" if not p["upright"] else
                     "unnamed" if p["symbol"] == "?" else "across a line" if p["on_board"] and not p["square"] else "out of reach")
                     for p in res["obstacles"]]}
    (out_dir / f"round{rnd}_survey.json").write_text(json.dumps(rec, indent=1, default=str))
    named = sorted(f"{p['square'] or 'off'}:{p['symbol']}" for p in pieces if p["symbol"] != "?")
    log(f"round {rnd}: {len(pieces)} pieces found, {len(named)} named: {' '.join(named)}")
    for n in rec["notes"]:
        log("  " + n)
    return rec


async def execute(sess: CR.ChessSession, cal: CR.BoardCal, world: L.World, ops: list[CR.PieceOp], log=print, settle_s: float = 0.2,
                  measured: dict[str, tuple[float, float]] | None = None, skipped: list | None = None) -> tuple[int, dict | None]:
    """Run the operations as supervised pick-and-place trials; stop at the first one that does not succeed, and after a
    piece was re-identified when lifted (the rest of the plan assumed otherwise: plan again)."""
    from amperes.integrations.viam_control import TrialConfig

    skipped = skipped if skipped is not None else []
    done = 0
    for op in ops:
        if sess.stop_event.is_set():
            return done, {"op": asdict(op), "reason": "stopped"}
        board = board_from(world)
        sess.game.board = board
        sess._current_op = op
        src, dst = sess._pose_for(op, "src", board), sess._pose_for(op, "dst", board)
        if measured and op.src in measured:
            # pick the piece where the survey saw it, not at the square centre: pieces stand up to 2 cm off centre and
            # the fingers then close on air
            mx, my = measured[op.src]
            if math.dist((mx, my), (src["x"], src["y"])) <= 30.0:
                src = {**src, "x": mx, "y": my}
        cfg = TrialConfig(name="chess_arrange", approach_mm=cal.transport_z - src["z"], settle_s=settle_s, move_timeout_s=40, trials=1)
        log(f"  [{done + 1}/{len(ops)}] {op.symbol} {op.src} -> {op.dst} {op.note}")
        base_src = dict(src)
        src_prev_z = src["z"]
        tries = [(0, 0, 0), (0, 0, -3), (6, 0, -3), (-6, 0, -3), (0, 6, -3), (0, -6, -3), (10, 10, -3), (-10, -10, -3), (10, -10, -3), (-10, 10, -3)]  # empty grip: retry at once, nearby
        for attempt in range(len(tries)):
            idx = len(sess.session["trials"])
            sess.session["moving"] = True
            try:
                r = await sess.ctl.trial(idx, src, dst, cfg, (op.src, op.dst))
            finally:
                sess.session["moving"] = False
            sess.session["trials"].append({**asdict(r), "config": asdict(cfg), "chess": {
                "uci": op.src + op.dst, "san": f"arrange {op.symbol}", "fen_before": board.fen(), **asdict(op),
                "piece": ("white " if op.symbol.isupper() else "black ") + CR.PIECE_NAME.get(op.symbol.lower(), "piece")}})
            log(f"      -> {r.outcome} {r.category or ''} {('(' + r.error + ')') if r.error else ''}")
            if r.category != "GRASP_FAILURE" or attempt == len(tries) - 1:
                break
            dx, dy, dz = tries[attempt + 1]
            z = max(base_src["z"] + dz, cal.board_top_z + CR.TOOL_LENGTH_MM + cal.floor_clearance_mm + 1.0)
            src = {**base_src, "x": base_src["x"] + dx, "y": base_src["y"] + dy, "z": z}
            dst = {**dst, "z": dst["z"] + (z - src_prev_z)}  # place at the height it is now held at
            src_prev_z = z
            log(f"      the fingers met nothing: retrying at once ({attempt + 2}/{len(tries)}, shift {dx:+d},{dy:+d} mm, {dz:+d} mm)")
            cfg = TrialConfig(name="chess_arrange", approach_mm=cal.transport_z - src["z"], settle_s=settle_s, move_timeout_s=40, trials=1)
        if r.category == "GRASP_FAILURE":  # missed on every try: leave this piece for now, carry on with the others
            log(f"      could not get hold of the {op.symbol} at {op.src}; skipping it for now")
            skipped.append(op.src)
            world.physical[op.src] = "?"
            continue
        if r.outcome != "success":
            return done, {"op": asdict(op), "outcome": r.outcome, "category": r.category, "error": r.error}
        world.apply(op)
        move_override(op.src, op.dst, op.symbol)
        done += 1
        if "re-identified" in op.note:
            return done, {"op": asdict(op), "replan": True}
    return done, None


def centring_ops(cal: CR.BoardCal, pieces: list[dict[str, Any]], target: dict[str, str], tol_mm: float = 8.0) -> list[CR.PieceOp]:
    """Pieces already on their own start square but off its centre by more than `tol_mm` (measured by the survey) are
    picked where they actually stand ('xy:' spot) and put back exactly on the centre."""
    from amperes.integrations import chess_survey as SV

    ops = []
    for p in pieces:
        sq, sym = p.get("square"), p.get("symbol")
        if not sq or sym in (None, "?", ".") or target.get(sq) != sym or not p.get("upright"):
            continue
        u, v = SV.to_board(cal, p["x"], p["y"])
        f, r = "abcdefgh".index(sq[0]), int(sq[1]) - 1
        off = math.hypot(u - f * cal.square_mm, v - r * cal.square_mm)
        if off > tol_mm:
            ops.append(CR.PieceOp("centre", sym, f"xy:{p['x']:.0f},{p['y']:.0f}", sq, f"{off:.0f} mm off centre"))
    return ops


def plan_for(cal: CR.BoardCal, layout: dict[str, str], slots_clear: bool) -> L.Plan:
    """Unidentified pieces ('?') stay in the layout: the planner never moves them, never places on them, and keeps the
    gripper clear of them as if they were kings."""
    return L.plan_reset(cal, dict(layout), target=start_target(slots_clear), use_slots=slots_clear)


async def run(out_dir: Path, ack: str = "", plan_only: bool = False, slots_clear: bool = False, max_rounds: int = 12,
              log=print, stop: threading.Event | None = None, inspect: bool = False, survey: bool = True, righting: bool = False) -> dict[str, Any]:
    cal = CR.BoardCal.load()
    world = L.World(cal, physical={})
    # plan-only still moves the arm to the look pose (the camera must see the board), so it needs the acknowledgement too
    spec = CR.ChessSpec(out_dir=out_dir, execute=True, operator_ack=ack, auto=False, settle_s=0.2)
    game = CR.Game(None, robot_color=None, world=world)
    sess = CR.ChessSession(spec, cal, game, stop or threading.Event(), log=log)
    sess.position_unknown = True
    if survey:  # the survey looks, and picks pieces, up to 10 cm beyond the board's edges
        sess.env = cal.envelope(margin_mm=130.0)
    await sess.connect()
    if survey:
        sess.ctl.env = sess.env
    await sess.go_home()
    seen: list[dict[str, Any]] = []
    sess.ctl.guard = make_hand_guard(sess, log)
    if inspect and not plan_only:
        sess.ctl.inspect = make_inspector(sess, cal, world, log, seen)
    rounds: list[dict[str, Any]] = []
    target = start_target(slots_clear)
    look = True
    skipped_spots: list[str] = []
    try:
        for rnd in range(max_rounds):
            if look:
                rec = await (recognise_by_survey(sess, cal, out_dir, rnd, log) if survey else recognise(sess, out_dir, rnd, log))
                world.physical = {k: v for k, v in rec["layout"].items()}
                for sq, sym in load_overrides().items():
                    if sq in world.physical:
                        world.physical[sq] = sym
                for k in list(world.physical):
                    if any(math.dist(L.location_xy(cal, k), L.location_xy(cal, sk)) < 25 for sk in skipped_spots):
                        world.physical[k] = "?"
            if righting and look and rec.get("lying"):
                from amperes.integrations import chess_righting as RT

                stood = 0
                for shape in rec["lying"]:
                    others = {k: v for k, v in world.physical.items() if math.dist(L.location_xy(cal, k), shape["centre"]) > 45}
                    mx, my = shape["centre"]
                    if L.spot_penetration(cal, {**others, f"xy:{mx:.0f},{my:.0f}": "p"}, f"xy:{mx:.0f},{my:.0f}", RT.FINGERTIP_LOW_MM + 3.0)[0] > 0:
                        log(f"  fallen piece at ({mx:.0f}, {my:.0f}) is too crowded to pick up yet")
                        continue
                    r = await RT.isolate_and_right(sess, cal, shape, world.physical, log)
                    log(f"      -> {'done' if r['ok'] else r['error']}")
                    stood += bool(r["ok"])
                if stood:
                    look = True
                    rounds.append({"round": rnd, "stood_up": stood})
                    continue
            plan = plan_for(cal, dict(world.physical), slots_clear)
            on_board = {k: v for k, v in world.physical.items() if v != "?" and L.is_square(k)}
            matches = on_board == {k: v for k, v in target.items() if L.is_square(k)}
            info = {"round": rnd, "looked": look, "plan": L.plan_to_dict(plan), "matches_start": matches,
                    "unknown": [k for k, v in world.physical.items() if v == "?"]}
            log(f"round {rnd}: {'board matches the start position' if matches else f'{len(plan.ops)} operations planned'}"
                + (f", missing {plan.missing}" if plan.missing else "") + (f", {len(plan.tight)} tight" if plan.tight else ""))
            if matches or not plan.ops or plan_only:
                rounds.append(info)
                break
            measured = {p["square"]: (p["x"], p["y"]) for p in (rec.get("pieces") or []) if p.get("square") and p.get("upright")}
            done, failure = await execute(sess, cal, world, plan.ops, log, measured=measured, skipped=skipped_spots)
            info.update(done=done, failure=failure)
            rounds.append(info)
            if failure and failure.get("replan"):
                look = False  # the layout is known: plan again without looking
                continue
            look = True
            if done == 0:
                log("no progress this round; stopping so a person can look")
                break
        if not plan_only and rounds and not rounds[-1].get("matches_start"):
            final = await (recognise_by_survey(sess, cal, out_dir, len(rounds), log) if survey else recognise(sess, out_dir, len(rounds), log))
            rounds.append({"round": len(rounds), "final_layout": final["layout"]})
    except CR.SafetyError as exc:
        log(f"stopped: {exc}")
        rounds.append({"stopped": str(exc)})
    finally:
        await sess.close()
    summary: dict[str, Any] = {"rounds": rounds, "out_dir": str(out_dir), "inspections": seen}
    if sess.session["trials"]:
        summary["import"] = CR.import_chess_session(sess.session, out_dir)
    (out_dir / "arrange_summary.json").write_text(json.dumps(summary, indent=1, default=str))
    return summary


if __name__ == "__main__":
    import argparse
    import signal

    from amperes import store

    ap = argparse.ArgumentParser(description="Set the board up in the start position by itself.")
    ap.add_argument("--ack", default="", help="operator acknowledgement, typed for the person at the e-stop (required to move)")
    ap.add_argument("--plan-only", action="store_true", help="look, recognise and plan; move only to the look pose")
    ap.add_argument("--slots-clear", action="store_true", help="the spots beside the a-file are empty: use them (spare queens' reserve)")
    ap.add_argument("--rounds", type=int, default=12)
    ap.add_argument("--inspect", action="store_true", help="photograph each lifted piece with the table camera (slow)")
    ap.add_argument("--no-righting", action="store_true", help="leave fallen pieces lying")
    ap.add_argument("--look", action="store_true", help="recognise from the single look pose instead of the six-view survey")
    a = ap.parse_args()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    out = store.artifacts_dir() / "robot_sessions" / ("arrange_" + time.strftime("%Y%m%dT%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    res = asyncio.run(run(out, a.ack, a.plan_only, a.slots_clear, a.rounds, stop=stop, inspect=a.inspect, survey=not a.look, righting=not a.no_righting))
    print(json.dumps({k: v for k, v in res.items() if k != "import"}, indent=1, default=str)[:4000])
