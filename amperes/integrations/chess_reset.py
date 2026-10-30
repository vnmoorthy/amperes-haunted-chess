"""Set the board up from a staged pattern: pieces are placed by hand on the light squares of ranks 3-6 in a fixed layout,
and the arm moves each to its start square in an order that keeps every destination's neighbours short or empty
(pawn rows first; then rooks, knights, bishops, queen, king). The staging squares have empty orthogonal neighbours, so
every pick has clearance too. Each move is a supervised pick-and-place trial recorded like a chess move."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import chess

from amperes.integrations import chess_robot as CR
from amperes.integrations.viam_control import TrialConfig

STAGE_PAWNS = {"white": ["b3", "d3", "f3", "h3", "a4", "c4", "e4", "g4"], "black": ["b5", "d5", "f5", "h5", "a6", "c6", "e6", "g6"]}
# batch 2: which piece stands on which staging square (same squares, fixed order)
STAGE_PIECES = {"white": {"b3": "R", "d3": "N", "f3": "B", "h3": "Q", "a4": "K", "c4": "B", "e4": "N", "g4": "R"},
                "black": {"b5": "r", "d5": "n", "f5": "b", "h5": "q", "a6": "k", "c6": "b", "e6": "n", "g6": "r"}}
HOME = {"R": ["a1", "h1"], "N": ["b1", "g1"], "B": ["c1", "f1"], "Q": ["d1"], "K": ["e1"],
        "r": ["a8", "h8"], "n": ["b8", "g8"], "b": ["c8", "f8"], "q": ["d8"], "k": ["e8"]}
ORDER = ["R", "N", "B", "Q", "K"]  # per colour: rooks first, king last


def plan(batch: str) -> tuple[chess.Board, list[CR.PieceOp]]:
    """Physical board at the start of the batch plus the ordered moves."""
    board = chess.Board(None)
    ops: list[CR.PieceOp] = []
    if batch == "pawns":
        for colour, squares in STAGE_PAWNS.items():
            sym = "P" if colour == "white" else "p"
            rank = "2" if colour == "white" else "7"
            for sq in squares:
                board.set_piece_at(chess.parse_square(sq), chess.Piece.from_symbol(sym))
            # nearest free home square by file distance, alternating from the middle out keeps neighbours empty longest
            homes = [f + rank for f in "abcdefgh"]
            for sq in squares:
                target = min(homes, key=lambda h: (abs(ord(h[0]) - ord(sq[0])), h))
                homes.remove(target)
                ops.append(CR.PieceOp("reset", sym, sq, target))
    else:
        for colour, layout in STAGE_PIECES.items():
            for sq, sym in layout.items():
                board.set_piece_at(chess.parse_square(sq), chess.Piece.from_symbol(sym))
        for colour, layout in STAGE_PIECES.items():
            homes = {k: list(v) for k, v in HOME.items()}
            for kind in ORDER:
                sym = kind if colour == "white" else kind.lower()
                for sq, s in layout.items():
                    if s == sym:
                        target = homes[sym].pop(0)
                        ops.append(CR.PieceOp("reset", sym, sq, target))
    return board, ops


HOME_ALL = {**HOME, "P": [f + "2" for f in "abcdefgh"], "p": [f + "7" for f in "abcdefgh"]}
PLACE_ORDER = ["P", "p", "R", "r", "N", "n", "B", "b", "Q", "q", "K", "k"]  # pawn rows first, kings last


def _free_buffer(board: chess.Board, near: str) -> str | None:
    """A free square in ranks 3-6 whose orthogonal neighbours are empty, nearest to `near`."""
    nf, nr = ord(near[0]) - 97, int(near[1]) - 1
    best = None
    for sq in chess.SQUARES:
        if board.piece_at(sq) is not None or not 2 <= chess.square_rank(sq) <= 5:
            continue
        clear = all(board.piece_at(n) is None for n in (sq - 8, sq + 8, sq - 1, sq + 1)
                    if 0 <= n < 64 and abs(chess.square_file(n) - chess.square_file(sq)) <= 1)
        if not clear:
            continue
        d = abs(chess.square_file(sq) - nf) + abs(chess.square_rank(sq) - nr)
        if best is None or d < best[0]:
            best = (d, chess.square_name(sq))
    return best[1] if best else None


def plan_from_board(start: chess.Board) -> list[CR.PieceOp]:
    """Moves that take the recognised position to the start position, in a clearance-preserving order, moving wrong
    occupants of a home square to a buffer square first. Simulated on a copy so later moves see the updated board."""
    board = start.copy()
    ops: list[CR.PieceOp] = []

    def move(sym: str, src: str, dst: str, kind: str = "reset"):
        ops.append(CR.PieceOp(kind, sym, src, dst))
        board.set_piece_at(chess.parse_square(dst), board.remove_piece_at(chess.parse_square(src)))

    for sym in PLACE_ORDER:
        homes = [h for h in HOME_ALL[sym]]
        # pieces of this kind not already home
        at_home = [h for h in homes if (p := board.piece_at(chess.parse_square(h))) and p.symbol() == sym]
        todo = [chess.square_name(sq) for sq, p in board.piece_map().items() if p.symbol() == sym and chess.square_name(sq) not in at_home]
        free_homes = [h for h in homes if h not in at_home]
        for src in todo:
            if not free_homes:
                break
            dst = min(free_homes, key=lambda h: abs(ord(h[0]) - ord(src[0])) + abs(int(h[1]) - int(src[1])))
            occupant = board.piece_at(chess.parse_square(dst))
            if occupant is not None:
                buf = _free_buffer(board, dst)
                if buf is None:
                    continue
                move(occupant.symbol(), dst, buf, "clear")
            move(sym, src, dst)
            free_homes.remove(dst)
    return ops


async def run(batch: str, out_dir: Path, log=print, stop: threading.Event | None = None, settle_s: float = 0.6,
              from_board: chess.Board | None = None, ack: str = "") -> dict[str, Any]:
    cal = CR.BoardCal.load()
    if from_board is not None:
        board, ops = from_board.copy(), plan_from_board(from_board)
    else:
        board, ops = plan(batch)
    spec = CR.ChessSpec(out_dir=out_dir, execute=True, operator_ack=ack, auto=False, settle_s=settle_s)
    sess = CR.ChessSession(spec, cal, CR.Game(None, robot_color=None, fen=board.fen()), stop or threading.Event(), log=log)
    sess.position_unknown = True
    await sess.connect()
    results = []
    try:
        for i, op in enumerate(ops):
            if sess.stop_event.is_set():
                break
            sess.game.board = board  # neighbours are judged on the physical board as it stands now
            sess._current_op = op
            src, dst = sess._pose_for(op, "src", board), sess._pose_for(op, "dst", board)
            cfg = TrialConfig(name=f"reset_{batch}", approach_mm=cal.transport_z - src["z"], settle_s=settle_s, move_timeout_s=40, trials=1)
            log(f"[{i + 1}/{len(ops)}] {op.symbol} {op.src} -> {op.dst} (grip {src['z'] - 150 - cal.board_top_z:.0f} mm up, wrist {src['theta']:.0f}/{dst['theta']:.0f})")
            sess.session["moving"] = True
            r = await sess.ctl.trial(i, src, dst, cfg, (op.src, op.dst))
            sess.session["moving"] = False
            d = {**asdict(r), "config": asdict(cfg), "chess": {"uci": op.src + op.dst, "san": f"reset {op.symbol}", "fen_before": board.fen(), **asdict(op),
                                                             "piece": ("white " if op.symbol.isupper() else "black ") + CR.PIECE_NAME[op.symbol.lower()]}}
            sess.session["trials"].append(d)
            results.append((op, r.outcome, r.category))
            log(f"    -> {r.outcome} {r.category or ''} grab={r.grab_reported} holding={r.holding_after_lift}")
            if r.outcome == "success":
                board.set_piece_at(chess.parse_square(op.dst), board.remove_piece_at(chess.parse_square(op.src)))
            elif r.category in ("SAFETY_STOP", "INFRASTRUCTURE_ERROR", "TIMEOUT"):
                break
        sess.session["moves"].append({"by": "robot", "uci": "reset", "san": batch, "t": time.time(), "images_before": [], "images_after": [],
                                      "ops": [asdict(o) for o in ops], "results": [(o.src, o.dst, oc, c) for o, oc, c in results]})
    finally:
        await sess.close()
    summary = CR.import_chess_session(sess.session, out_dir) if sess.session["trials"] else {}
    summary["results"] = [(o.src + "->" + o.dst, oc, c) for o, oc, c in results]
    summary["board_after"] = board.board_fen()
    return summary


if __name__ == "__main__":
    import argparse
    import json
    import signal
    import sys

    ap = argparse.ArgumentParser()
    ap.add_argument("batch", choices=["pawns", "pieces", "scan"])
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--recognized", help="recognized.json from chess_scan (batch 'scan')")
    ap.add_argument("--ack", default="", help="operator acknowledgement, typed by the person at the e-stop (required to move)")
    a = ap.parse_args()
    from_board = None
    if a.batch == "scan":
        rec = json.load(open(a.recognized))
        from_board = chess.Board(None)
        for sq, c in rec["pieces"].items():
            from_board.set_piece_at(chess.parse_square(sq), chess.Piece.from_symbol(c["symbol"]))
    if a.plan_only:
        b, ops = (from_board, plan_from_board(from_board)) if from_board is not None else plan(a.batch)
        print(b)
        print([(o.kind, o.symbol, o.src, o.dst) for o in ops])
        sys.exit(0)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    from amperes import store

    out = store.artifacts_dir() / "robot_sessions" / ("reset_" + time.strftime("%Y%m%dT%H%M%S"))
    print(json.dumps(asyncio.run(run(a.batch, out, stop=stop, from_board=from_board, ack=a.ack)), indent=1, default=str))
