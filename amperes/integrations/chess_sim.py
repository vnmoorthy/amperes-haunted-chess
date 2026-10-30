"""Offline proof of the match lifecycle: no robot, the same gripper clearance model as the planner.

    python -m amperes.integrations.chess_sim --scrambles 300 --games 100

Runs (a) random scrambles of all 34 pieces (32 + the two spare queens) over the board and the slots beside it, each
followed by the arrangement plan; (b) Stockfish-vs-Stockfish games where every capture goes to a slot and promotions
use the spare queen, each followed by the reset; (c) Fool's mate, Scholar's mate and a promotion race. Every operation
is replayed on a virtual board that refuses a pick from an empty spot or a place onto an occupied one, and every
pick/place is scored for gripper clearance at the moment it happens."""

from __future__ import annotations

import argparse
import json
import random
import shutil
import time
from dataclasses import asdict
from typing import Any

import chess

from amperes.integrations import chess_lifecycle as L
from amperes.integrations.chess_robot import BoardCal, PieceOp


def default_cal() -> BoardCal:
    """The calibration taught on 29 Oct (used when no chessboard.json is around, e.g. in tests)."""
    try:
        return BoardCal.load()
    except Exception:
        return BoardCal(a1={"x": 209.2, "y": 167.4, "z": 178.0}, h1={"x": 223.2, "y": -213.6, "z": 178.0},
                        graveyard={"x": 205.0, "y": 292.0, "z": 178.0}, king_height_mm=85.0, grip_fraction=0.55, grasp_mm=10.0)


def replay(cal: BoardCal, physical: dict[str, str], ops: list[PieceOp]) -> dict[str, Any]:
    """Execute `ops` on a copy of `physical`; count invalid steps and clearance violations."""
    phys = dict(physical)
    invalid, violations = [], []
    for i, op in enumerate(ops):
        if phys.get(op.src) != op.symbol:
            invalid.append(f"#{i} {op.symbol} {op.src}->{op.dst}: source holds {phys.get(op.src)}")
            continue
        if op.dst in phys:
            invalid.append(f"#{i} {op.symbol} {op.src}->{op.dst}: destination holds {phys[op.dst]}")
            continue
        cost, info = L.op_cost(cal, phys, op.src, op.dst, op.symbol)
        if cost > 0:
            violations.append({"i": i, "op": f"{op.symbol} {op.src}->{op.dst}", "cost_mm": round(cost, 1), **info})
        phys[op.dst] = phys.pop(op.src)
    return {"final": phys, "invalid": invalid, "violations": violations}


def scramble(rng: random.Random, cal: BoardCal, lines: int = L.GRAVE_LINES, off_board: int | None = None) -> dict[str, str]:
    """All 34 pieces placed at random: some in the slots beside the board, the rest on random squares."""
    pieces = list(L.start_target().values())
    rng.shuffle(pieces)
    slots = L.all_slots(cal, lines)
    k = rng.randint(0, min(len(slots), 12)) if off_board is None else off_board
    spots = rng.sample(slots, k) + rng.sample([chess.square_name(s) for s in chess.SQUARES], len(pieces) - k)
    return dict(zip(spots, pieces))


def _engine():
    path = shutil.which("stockfish") or "/opt/homebrew/bin/stockfish"
    import chess.engine

    return chess.engine.SimpleEngine.popen_uci(path)


def engine_game(eng, rng: random.Random, cal: BoardCal, ply_cap: int = 200, board: chess.Board | None = None, lines: int = L.GRAVE_LINES,
                world: L.World | None = None) -> dict[str, Any]:
    import chess.engine

    world = world or L.World(cal, lines=lines)
    skills = (rng.randint(0, 12), rng.randint(0, 12))
    limit = chess.engine.Limit(time=0.01)

    def choose(b: chess.Board) -> chess.Move:
        """Like the robot: among the engine's five best moves, the one the hand can make most cleanly."""
        eng.configure({"Skill Level": skills[0] if b.turn == chess.WHITE else skills[1]})
        try:
            cands = [i["pv"][0] for i in eng.analyse(b, limit, multipv=5) if i.get("pv")]
        except Exception:
            cands = []
        if not cands:
            return eng.play(b, limit).move
        return min(enumerate(cands), key=lambda km: (L.move_clearance(world, b, km[1]), km[0]))[1]

    def score(b: chess.Board):
        try:
            info = eng.analyse(b, chess.engine.Limit(time=0.005))
            return info["score"].white().score(mate_score=10000)
        except Exception:
            return None

    violations = []

    def on_ops(mv, ops):
        r = replay(cal, world.physical, ops)
        violations.extend(r["violations"])

    rec = L.play_game(world, choose, L.Adjudicator(ply_cap=ply_cap), score, board=board, on_ops=on_ops)
    return {**rec, "skills": skills, "play_violations": violations}


def check_reset(cal: BoardCal, physical: dict[str, str], lines: int = L.GRAVE_LINES) -> dict[str, Any]:
    t = time.time()
    plan = L.plan_reset(cal, physical, lines=lines)
    r = replay(cal, physical, plan.ops)
    target = L.start_target()
    exact = r["final"] == target or (not plan.missing and {k: v for k, v in r["final"].items() if k in target} == target)
    return {"ok": plan.ok and exact and not r["invalid"], "exact": exact, "ops": len(plan.ops), "violations": len(r["violations"]),
            "violation_list": r["violations"][:5], "invalid": r["invalid"][:5], "missing": plan.missing, "unreachable": plan.unreachable,
            "plan_s": round(time.time() - t, 2)}


MINIATURES = {
    "fools_mate": "f3 e5 g4 Qh4#",
    "scholars_mate": "e4 e5 Bc4 Nc6 Qh5 Nf6 Qxf7#",
    "promotion_race": None,  # from a position, played by the engine
}
PROMOTION_FEN = "8/P5k1/8/8/8/8/1p4K1/8 w - - 0 1"


def run_miniature(cal: BoardCal, name: str, eng=None) -> dict[str, Any]:
    world = L.World(cal)
    board = chess.Board()
    out: dict[str, Any] = {"name": name}
    if name == "promotion_race":
        board = chess.Board(PROMOTION_FEN)
        # the physical world: the pieces of that position on the board; everything else beside it
        world.physical = {chess.square_name(s): p.symbol() for s, p in board.piece_map().items()}
        others = [s for s in L.start_target().values()]
        for s in world.physical.values():
            others.remove(s)
        # spare queens in reserve; the rest of the captured army in slots, as far as they go (the overflow is "unknown")
        others.remove("Q")
        others.remove("q")
        world.physical.update({L.RESERVE["Q"]: "Q", L.RESERVE["q"]: "q"})
        room = L.capture_slots(cal)[:-4]  # leave slots free for the pawns that promote
        for slot, s in zip(room, others):
            world.physical[slot] = s
        world.unknown = others[len(room):]
        rec = engine_game(eng, random.Random(7), cal, ply_cap=60, board=board, world=world) if eng else None
        out["game"] = {"moves": [m["san"] for m in rec["moves"]], "verdict": rec["verdict"],
                       "promotions": [m for m in rec["moves"] if any(o["kind"].startswith("promote") for o in m["ops"])][:2]} if rec else None
    else:
        for san in MINIATURES[name].split():
            mv = board.parse_san(san)
            for op in L.move_ops(world, board, mv):
                world.apply(op)
            board.push(mv)
        out["game"] = {"moves": MINIATURES[name].split(), "verdict": L.Adjudicator().verdict(board)}
    out["off_board"] = world.off_board()
    phys = dict(world.physical)
    out["reset"] = check_reset(cal, phys) if not world.unknown else {"skipped": f"{len(world.unknown)} pieces not in slots"}
    if not world.unknown:
        out["reset_ops"] = [f"{o.symbol} {o.src}->{o.dst}" for o in L.plan_reset(cal, phys).ops]
    return out


def main(argv: list[str] | None = None) -> dict[str, Any]:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scrambles", type=int, default=100)
    ap.add_argument("--games", type=int, default=20)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--ply-cap", type=int, default=200)
    ap.add_argument("--lines", type=int, default=L.GRAVE_LINES)
    ap.add_argument("--json", help="write the full report here")
    a = ap.parse_args(argv)
    cal = default_cal()
    rng = random.Random(a.seed)
    report: dict[str, Any] = {"slots": {s: [round(v) for v in L.location_xy(cal, s)] for s in L.all_slots(cal, a.lines)}}

    t = time.time()
    sc = [check_reset(cal, scramble(rng, cal, a.lines), a.lines) for _ in range(a.scrambles)]
    report["scrambles"] = {"n": len(sc), "ok": sum(r["ok"] for r in sc), "with_violations": sum(r["violations"] > 0 for r in sc),
                           "violations": sum(r["violations"] for r in sc), "unreachable": sum(r["unreachable"] for r in sc),
                           "mean_ops": round(sum(r["ops"] for r in sc) / max(1, len(sc)), 1), "max_ops": max((r["ops"] for r in sc), default=0),
                           "mean_plan_s": round(sum(r["plan_s"] for r in sc) / max(1, len(sc)), 2),
                           "failures": [r for r in sc if not r["ok"] or r["violations"]][:5], "seconds": round(time.time() - t, 1)}
    print("scrambles:", json.dumps({k: v for k, v in report["scrambles"].items() if k != "failures"}))

    games = []
    eng = None
    if a.games:
        eng = _engine()
        t = time.time()
        for g in range(a.games):
            try:
                rec = engine_game(eng, rng, cal, a.ply_cap, lines=a.lines)
            except RuntimeError as exc:  # out of slots
                games.append({"error": str(exc)})
                continue
            res = check_reset(cal, rec["world"]["physical"], a.lines)
            games.append({"plies": len(rec["moves"]), "verdict": rec["verdict"], "skills": rec["skills"],
                          "captures": sum(1 for m in rec["moves"] for o in m["ops"] if o["kind"] == "capture"),
                          "promotions": sum(1 for m in rec["moves"] for o in m["ops"] if o["kind"] == "promote_in"),
                          "hand_swaps": sum(1 for m in rec["moves"] for o in m["ops"] if "by hand" in o["note"]),
                          "play_violations": len(rec["play_violations"]), "reset": res})
        ok = [g for g in games if "error" not in g]
        report["games"] = {"n": len(games), "out_of_slots": len(games) - len(ok), "reset_ok": sum(g["reset"]["ok"] for g in ok),
                           "reset_violations": sum(g["reset"]["violations"] for g in ok),
                           "mean_plies": round(sum(g["plies"] for g in ok) / max(1, len(ok)), 1),
                           "mean_captures": round(sum(g["captures"] for g in ok) / max(1, len(ok)), 1),
                           "promotions": sum(g["promotions"] for g in ok), "hand_swaps": sum(g["hand_swaps"] for g in ok),
                           "mean_reset_ops": round(sum(g["reset"]["ops"] for g in ok) / max(1, len(ok)), 1),
                           "play_ops_with_contact": sum(g["play_violations"] for g in ok),
                           "verdicts": sorted({g["verdict"]["reason"] for g in ok}),
                           "failures": [g for g in games if "error" in g or not g["reset"]["ok"]][:5], "seconds": round(time.time() - t, 1)}
        print("games:", json.dumps({k: v for k, v in report["games"].items() if k != "failures"}))
    report["miniatures"] = [run_miniature(cal, n, eng) for n in MINIATURES]
    for m in report["miniatures"]:
        print(m["name"], json.dumps(m.get("game", {}).get("verdict") if m.get("game") else None), "reset:",
              json.dumps({k: m["reset"].get(k) for k in ("ok", "ops", "violations")} if "ok" in m["reset"] else m["reset"]))
    if eng:
        eng.quit()
    if a.json:
        with open(a.json, "w") as f:
            json.dump(report, f, indent=1, default=str)
    return report


if __name__ == "__main__":
    main()
