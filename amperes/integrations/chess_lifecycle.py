"""A whole match the way a person handles it: set the pieces up, play until the game ends, put everything back.

The robot always knows where every piece is, on the board or beside it, because it put each one there. This module
holds that physical state (`World`), plans the piece operations for a move (captures to a slot beside the board,
castling, en passant, and promotion with the set's spare queen), decides when a game is over, and plans the reset:
every piece back to its start square and both spare queens back to their reserve spots, in an order the gripper can
physically do.

Gripper model (the same geometry as ChessSession.wrist_angle_for / BoardCal.grasp_z): the body is 110 x 75 mm with its
underside 50 mm above the fingertips; the fingers open to about 85 mm and descend on the board diagonal; the grip is
10 mm above the board, raised beside tall neighbours while leaving at least 15 mm of the piece to hold. A pick or place
is "clean" when no neighbour taller than the body's underside sits under the body, and no neighbour's base is in the
fingers' way. The planner orders operations so every one is clean whenever such an order exists, moving a blocking
piece to a buffer square when needed.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import chess

from amperes.integrations.chess_robot import BODY_CLEAR_MM, PIECE_HEIGHT, RESERVE_SLOT, BoardCal, PieceOp, offboard_slot

# --- off-board slots -----------------------------------------------------------------------------------------------
# Slots use the graveyard numbering of BoardCal.graveyard_xy: slot n is line n // 8 (0 = 2.3 squares outside the a-file),
# position n % 8 along the edge from the rank-1 end. Only slots inside MAX_REACH_MM of the arm base are used. The spare
# queens live in RESERVE_SLOT (outer line, 5th and 6th from the rank-1 end, about 550 and 594 mm from the base).
MAX_REACH_MM = 640.0
RESERVE = {"Q": "reserveQ", "q": "reserveq"}  # the spare queens' home spots
GRAVE_LINES = 2  # lines of slots beside the a-file; more need a clear table further out

HOME = {"R": ["a1", "h1"], "N": ["b1", "g1"], "B": ["c1", "f1"], "Q": ["d1"], "K": ["e1"],
        "r": ["a8", "h8"], "n": ["b8", "g8"], "b": ["c8", "f8"], "q": ["d8"], "k": ["e8"],
        "P": [f + "2" for f in "abcdefgh"], "p": [f + "7" for f in "abcdefgh"]}
PRIORITY = {"p": 0, "r": 1, "n": 2, "b": 3, "q": 4, "k": 5}  # short pieces first, so tall ones never crowd a later grip


def slot_number(name: str) -> int:
    return offboard_slot(name)


def is_square(name: str) -> bool:
    return len(name) == 2 and name[0] in "abcdefgh" and name[1] in "12345678"


_POS: dict[tuple[int, str], tuple[float, float]] = {}


def location_xy(cal: BoardCal, name: str) -> tuple[float, float]:
    key = (id(cal), name)
    p = _POS.get(key)
    if p is None:
        if name.startswith("xy:"):  # a piece found off the board by the survey, at arm coordinates x, y
            xs, ys = name[3:].split(",")
            p = (float(xs), float(ys))
        else:
            p = cal.square_xy(name) if is_square(name) else cal.graveyard_xy(slot_number(name))
        if len(_POS) > 20000:
            _POS.clear()
        _POS[key] = p
    return p


def capture_slots(cal: BoardCal, lines: int = GRAVE_LINES) -> list[str]:
    """Slots for captured pieces, in fill order (inner line first, from the rank-1 end), within reach, minus reserves."""
    out = []
    reserved = set(RESERVE_SLOT.values())
    for n in range(lines * 8):
        if n in reserved:
            continue
        if math.hypot(*cal.graveyard_xy(n)) <= MAX_REACH_MM:
            out.append(f"grave{n}")
    return out


def all_slots(cal: BoardCal, lines: int = GRAVE_LINES) -> list[str]:
    return capture_slots(cal, lines) + list(RESERVE.values())


# --- gripper clearance -----------------------------------------------------------------------------------------------
BODY_HALF_LONG, BODY_HALF_SHORT = 55.0, 37.5
PIECE_RADIUS = 17.0      # widest part of a piece, near the base
FINGER_OFFSET = 47.5     # finger centre from the grip centre along the finger axis (85 mm open + finger thickness)
FINGER_HALF_ALONG, FINGER_HALF_ACROSS = 5.0, 10.0


def piece_height(cal: BoardCal, symbol: str) -> float:
    if symbol == "?":  # a piece nobody could name: as tall as a king for clearance, and never picked up
        return cal.king_height_mm
    return PIECE_HEIGHT[symbol.lower()] * cal.king_height_mm / 95.0


def grip_height(cal: BoardCal, symbol: str, tallest_neighbour: float) -> float:
    """Fingertip height above the board, as BoardCal.grasp_z computes it."""
    h = cal.grasp_mm
    if tallest_neighbour > BODY_CLEAR_MM + h:
        h = min(tallest_neighbour - BODY_CLEAR_MM + 5.0, piece_height(cal, symbol) - 15.0)
        h = max(h, cal.grasp_mm)
    return h


def neighbours(cal: BoardCal, physical: dict[str, str], loc: str, skip: tuple[str, ...] = ()) -> list[tuple[float, float, float, str]]:
    """Pieces within 1.5 squares of `loc` (board squares and slots alike): (x, y, height, location)."""
    x, y = location_xy(cal, loc)
    reach = 1.5 * cal.square_mm
    out = []
    for other, sym in physical.items():
        if other == loc or other in skip:
            continue
        ox, oy = location_xy(cal, other)
        if math.hypot(ox - x, oy - y) <= reach:
            out.append((ox, oy, piece_height(cal, sym), other))
    return out


def tallest_near(cal: BoardCal, physical: dict[str, str], loc: str, skip: tuple[str, ...] = ()) -> float:
    return max((h for _, _, h, _ in neighbours(cal, physical, loc, skip)), default=0.0)


def spot_penetration(cal: BoardCal, physical: dict[str, str], loc: str, grip_h: float, skip: tuple[str, ...] = ()) -> tuple[float, list[str]]:
    """How far (mm) the gripper at `loc`, on the board diagonal, overlaps a neighbour: the body over a piece taller than
    its underside, or a finger into a piece's base. 0 means clean. Also returns the offending locations."""
    x, y = location_xy(cal, loc)
    a = math.radians(cal.theta_deg)  # fingers on the board diagonal, the runtime's default wrist angle
    ux, uy = math.cos(a), math.sin(a)
    vx, vy = -uy, ux
    underside = grip_h + BODY_CLEAR_MM
    worst, who = 0.0, []
    for nx, ny, nh, name in neighbours(cal, physical, loc, skip):
        dx, dy = nx - x, ny - y
        along, across = abs(dx * ux + dy * uy), abs(dx * vx + dy * vy)
        pen = 0.0
        if nh > underside - 2.0:  # 2 mm margin
            pen = max(pen, min(BODY_HALF_LONG + PIECE_RADIUS - along, BODY_HALF_SHORT + PIECE_RADIUS - across))
        if nh > grip_h:  # the fingers reach from the grip up to the body: any piece taller than the grip meets them
            pen = max(pen, min(FINGER_HALF_ALONG + PIECE_RADIUS - abs(along - FINGER_OFFSET), FINGER_HALF_ACROSS + PIECE_RADIUS - across))
        if pen > 0:
            who.append(name)
            worst = max(worst, pen)
    return worst, who


def op_cost(cal: BoardCal, physical: dict[str, str], src: str, dst: str, symbol: str) -> tuple[float, dict[str, Any]]:
    """Clearance cost of picking `symbol` at `src` and placing it at `dst` with the board as it stands (the piece is
    held at one height for both, clearing the taller neighbourhood, like ChessSession._pose_for)."""
    tallest = max(tallest_near(cal, physical, src), tallest_near(cal, physical, dst, skip=(src,)))
    gh = grip_height(cal, symbol, tallest)
    p_pick, who_pick = spot_penetration(cal, physical, src, gh)
    p_place, who_place = spot_penetration(cal, physical, dst, gh, skip=(src,))
    return max(p_pick, p_place), {"grip_mm": round(gh, 1), "pick": who_pick, "place": who_place}


# --- the physical world ----------------------------------------------------------------------------------------------
def start_physical(spare_queens: bool = True) -> dict[str, str]:
    phys = {chess.square_name(sq): p.symbol() for sq, p in chess.Board().piece_map().items()}
    if spare_queens:
        phys.update({RESERVE["Q"]: "Q", RESERVE["q"]: "q"})
    return phys


def start_target() -> dict[str, str]:
    return start_physical(True)


@dataclass
class World:
    """Where every physical piece is: board squares and off-board slots (location -> FEN symbol)."""

    cal: BoardCal
    physical: dict[str, str] = field(default_factory=start_physical)
    lines: int = GRAVE_LINES
    unknown: list[str] = field(default_factory=list)  # pieces taken off the board by a person, location unknown

    def free_capture_slot(self, taken: set[str] | None = None) -> str | None:
        taken = taken or set()
        best = None
        for s in capture_slots(self.cal, self.lines):
            if s in self.physical or s in taken:
                continue
            cost, _ = op_cost(self.cal, self.physical, s, s, "p")
            if cost <= 0:
                return s
            best = best or s
        return best

    def apply(self, op: PieceOp) -> None:
        sym = self.physical.pop(op.src, None)
        if sym is None:
            raise ValueError(f"no piece at {op.src} for {op}")
        if op.dst in self.physical:
            raise ValueError(f"{op.dst} is occupied ({self.physical[op.dst]}) for {op}")
        self.physical[op.dst] = sym

    def board_fen(self) -> str:
        b = chess.Board(None)
        for loc, sym in self.physical.items():
            if is_square(loc):
                b.set_piece_at(chess.parse_square(loc), chess.Piece.from_symbol(sym))
        return b.board_fen()

    def off_board(self) -> dict[str, str]:
        return {loc: s for loc, s in self.physical.items() if not is_square(loc)}

    def human_move(self, board_before: chess.Board, move: chess.Move) -> None:
        """A person made `move` by hand: update the squares; a piece they captured left the board for an unknown spot."""
        for op in move_ops(self, board_before, move, allocate=False):
            if op.dst.startswith("grave") or op.dst == "hand":
                self.unknown.append(self.physical.pop(op.src))
            elif op.kind == "promote_in":
                continue
            else:
                self.physical[op.dst] = self.physical.pop(op.src)
        if move.promotion:  # the person swapped the pawn for a piece themselves
            sq = chess.square_name(move.to_square)
            self.physical[sq] = chess.Piece(move.promotion, board_before.turn).symbol()

    def to_dict(self) -> dict[str, Any]:
        return {"physical": dict(self.physical), "board_fen": self.board_fen(), "off_board": self.off_board(), "unknown": list(self.unknown)}


# --- piece operations for one move -----------------------------------------------------------------------------------
def move_ops(world: World, board: chess.Board, move: chess.Move, allocate: bool = True) -> list[PieceOp]:
    """Physical operations for `move` on `board` (before pushing it), using the world's slots: a captured piece goes to
    a free slot first; promotion sends the pawn off and brings the spare piece in, as a person does."""
    piece = board.piece_at(move.from_square)
    if piece is None:
        raise ValueError(f"no piece on {chess.square_name(move.from_square)}")
    src, dst = chess.square_name(move.from_square), chess.square_name(move.to_square)
    taken: set[str] = set()

    def slot() -> str:
        if not allocate:
            return "hand"
        s = world.free_capture_slot(taken)
        if s is None:
            raise RuntimeError("no free slot beside the board for a captured piece: clear the captured pieces first")
        taken.add(s)
        return s

    ops: list[PieceOp] = []
    if board.is_en_passant(move):
        cap = move.to_square + (-8 if piece.color == chess.WHITE else 8)
        ops.append(PieceOp("capture", board.piece_at(cap).symbol(), chess.square_name(cap), slot(), "en passant"))
    elif board.is_capture(move):
        ops.append(PieceOp("capture", board.piece_at(move.to_square).symbol(), dst, slot()))
    if move.promotion:
        new = chess.Piece(move.promotion, piece.color).symbol()
        spare = None
        if allocate:
            reserve = RESERVE.get(new)
            if reserve and world.physical.get(reserve) == new:
                spare = reserve
            else:
                spare = next((loc for loc, s in world.physical.items() if s == new and not is_square(loc)), None)
        if spare:
            ops.append(PieceOp("promote_out", piece.symbol(), src, slot(), f"promotion to {new}: pawn leaves the board"))
            ops.append(PieceOp("promote_in", new, spare, dst, "promotion: spare piece comes in"))
        else:
            ops.append(PieceOp("move", piece.symbol(), src, dst, f"promotion: swap the pawn for {new} by hand"))
    else:
        ops.append(PieceOp("move", piece.symbol(), src, dst))
    if board.is_castling(move):
        rank = "1" if piece.color == chess.WHITE else "8"
        rook = "R" if piece.color == chess.WHITE else "r"
        if chess.square_file(move.to_square) == 6:
            ops.append(PieceOp("castle_rook", rook, "h" + rank, "f" + rank))
        else:
            ops.append(PieceOp("castle_rook", rook, "a" + rank, "d" + rank))
    return ops


# --- game end ----------------------------------------------------------------------------------------------------------
@dataclass
class Adjudicator:
    """Ends engine-vs-engine games the way people do: resign when lost for a while, agree a draw when nothing happens."""

    ply_cap: int | None = None
    resign_cp: int = 600
    resign_plies: int = 4
    draw_cp: int = 25
    draw_after_ply: int = 120
    draw_plies: int = 12
    _history: list[int] = field(default_factory=list)

    def update(self, score_white_cp: int | None) -> None:
        if score_white_cp is not None:
            self._history.append(int(score_white_cp))

    def verdict(self, board: chess.Board) -> dict[str, Any] | None:
        out = board.outcome(claim_draw=False)
        if out is not None:
            return {"result": out.result(), "reason": out.termination.name.lower().replace("_", " ")}
        if board.can_claim_threefold_repetition():
            return {"result": "1/2-1/2", "reason": "threefold repetition"}
        if board.can_claim_fifty_moves():
            return {"result": "1/2-1/2", "reason": "fifty-move rule"}
        h = self._history
        if len(h) >= self.resign_plies and all(s >= self.resign_cp for s in h[-self.resign_plies:]):
            return {"result": "1-0", "reason": "black resigns"}
        if len(h) >= self.resign_plies and all(s <= -self.resign_cp for s in h[-self.resign_plies:]):
            return {"result": "0-1", "reason": "white resigns"}
        if len(board.move_stack) >= self.draw_after_ply and len(h) >= self.draw_plies and all(abs(s) <= self.draw_cp for s in h[-self.draw_plies:]):
            return {"result": "1/2-1/2", "reason": "draw agreed"}
        if self.ply_cap and len(board.move_stack) >= self.ply_cap:
            return {"result": "*", "reason": f"stopped after {self.ply_cap} plies"}
        return None


# --- the reset / arrangement planner ---------------------------------------------------------------------------------
@dataclass
class Plan:
    ops: list[PieceOp]
    tight: list[dict[str, Any]]          # operations that could not be made clean (with their cost)
    missing: dict[str, int]              # pieces the target needs that are nowhere to be found
    extra: list[str]                     # pieces that have no target (e.g. unknown or surplus), left where they are
    unreachable: bool = False            # the planner gave up before reaching the target
    final: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.unreachable and not self.missing

    def summary(self) -> dict[str, Any]:
        return {"ops": len(self.ops), "tight": len(self.tight), "missing": self.missing, "extra": self.extra,
                "unreachable": self.unreachable, "ok": self.ok}


def _dist(cal: BoardCal, a: str, b: str) -> float:
    (ax, ay), (bx, by) = location_xy(cal, a), location_xy(cal, b)
    return math.hypot(ax - bx, ay - by)


def plan_reset(cal: BoardCal, physical: dict[str, str], target: dict[str, str] | None = None, lines: int = GRAVE_LINES,
               max_ops: int = 120, use_slots: bool = True) -> Plan:
    """Operations taking `physical` (location -> symbol, squares and slots) to `target` (default: the start position with
    the spare queens in reserve). Each step takes the clean operation of the shortest piece kind that does not make any
    other pending operation unclean; a wrong piece on a needed home goes to its own home or to a buffer spot first."""
    target = dict(target or start_target())
    phys = dict(physical)
    ops: list[PieceOp] = []
    tight: list[dict[str, Any]] = []
    # buffer spots for pieces moved aside: the middle ranks, plus the slots beside the board when they are known to be clear
    buffers = [chess.square_name(s) for s in chess.SQUARES if 2 <= chess.square_rank(s) <= 5] + (capture_slots(cal, lines) if use_slots else [])

    need: dict[str, int] = {}
    for sym in target.values():
        need[sym] = need.get(sym, 0) + 1
    have: dict[str, int] = {}
    for sym in phys.values():
        have[sym] = have.get(sym, 0) + 1
    missing = {s: n - have.get(s, 0) for s, n in need.items() if n > have.get(s, 0)}
    surplus = {s: have[s] - need.get(s, 0) for s in have if have[s] > need.get(s, 0)}

    def settled(loc: str) -> bool:
        return target.get(loc) is not None and phys.get(loc) == target[loc]

    def pending() -> list[tuple[str, str]]:
        """(symbol, location) of pieces not on a target that wants them."""
        return [(s, loc) for loc, s in phys.items() if not settled(loc)]

    def free_homes(sym: str) -> list[str]:
        return [loc for loc, s in target.items() if s == sym and loc not in phys]

    def blocked_homes(sym: str) -> list[str]:
        return [loc for loc, s in target.items() if s == sym and loc in phys and phys[loc] != sym]

    def do(kind: str, sym: str, src: str, dst: str, note: str = "") -> None:
        cost, info = op_cost(cal, phys, src, dst, sym)
        if cost > 0:
            tight.append({"op": f"{sym} {src}->{dst}", "cost_mm": round(cost, 1), **info})
        ops.append(PieceOp(kind, sym, src, dst, note))
        phys[dst] = phys.pop(src)

    def dirty_count(state: dict[str, str], near: str) -> int:
        """Pending operations near `near` that have no clean home left (only those can change by one placement)."""
        n = 0
        nx, ny = location_xy(cal, near)
        reach = 2.6 * cal.square_mm
        for loc, s in state.items():
            if target.get(loc) is not None and state.get(loc) == target[loc]:
                continue
            if all(t != s for t in target.values()):
                continue
            homes = [h for h, t in target.items() if t == s and h not in state]
            homes_near = [h for h in homes if math.dist(location_xy(cal, h), (nx, ny)) <= reach]
            if not homes_near and math.dist(location_xy(cal, loc), (nx, ny)) > reach:
                continue
            if homes and min(op_cost(cal, state, loc, h, s)[0] for h in homes) > 0:
                n += 1
        return n

    def lift_crowder(cands: list) -> bool:
        """A person's trick: lift a piece that crowds the next placement onto a clean buffer spot that does not crowd it
        too; the lifted piece comes back later (the short piece goes first because of PRIORITY)."""
        for c in sorted(cands, key=lambda c: (c[1], c[2])):
            _, info = op_cost(cal, phys, c[5], c[6], c[4])
            crowders = list(dict.fromkeys(info["pick"] + info["place"]))
            for loc in crowders:
                if loc not in phys or loc == c[5] or phys[loc] == "?":
                    continue
                occ = phys[loc]
                spots = sorted((b for b in buffers if b not in phys and target.get(b) is None), key=lambda x: _dist(cal, loc, x))
                for spot in spots[:24]:
                    if op_cost(cal, phys, loc, spot, occ)[0] > 0:
                        continue
                    after = dict(phys)
                    after[spot] = after.pop(loc)
                    cost_after, info_after = op_cost(cal, after, c[5], c[6], c[4])
                    # the placement must get cleaner: fully clean, or one crowder fewer (two kings can crowd one square)
                    if cost_after > 0 and len(set(info_after["pick"] + info_after["place"])) >= len(crowders):
                        continue
                    if tuple(sorted(after.items())) in seen:
                        continue
                    do("clear", occ, loc, spot, f"lift {occ} off {loc} to make room for {c[4]} on {c[6]}")
                    forced.append((c[4], c[5], c[6]))
                    return True
        return False

    seen: set[tuple] = set()
    forced: list[tuple[str, str, str]] = []  # the move a lifted piece made room for: it goes next, never the lifted piece back
    limit = min(max_ops, 2 * len(phys) + 4)
    for _ in range(limit):
        seen.add(tuple(sorted(phys.items())))
        if forced:
            fs, fsrc, fdst = forced.pop()
            if phys.get(fsrc) == fs and fdst not in phys:
                cost, _ = op_cost(cal, phys, fsrc, fdst, fs)
                if cost <= 0:
                    do("reset", fs, fsrc, fdst)
                    continue
                cand = (True, PRIORITY[fs.lower()], _dist(cal, fsrc, fdst), cost, fs, fsrc, fdst)
                if lift_crowder([cand]):  # still crowded: lift the next crowder too
                    continue
                do("reset", fs, fsrc, fdst, "tight: check this one")
                continue
        todo = pending()
        # surplus pieces with nowhere to go stay put
        todo = [(s, loc) for s, loc in todo if any(t == s for t in target.values())]
        if not todo:
            break
        cands = []
        for s, loc in todo:
            for h in free_homes(s):
                cost, _ = op_cost(cal, phys, loc, h, s)
                cands.append((cost > 0, PRIORITY[s.lower()], _dist(cal, loc, h), cost, s, loc, h))
        cands.sort(key=lambda c: c[:3])
        chosen = None
        clean = [c for c in cands if not c[0]]
        fresh = []
        for c in clean:  # never walk back into a state already visited (that is how plans loop)
            trial = dict(phys)
            trial[c[6]] = trial.pop(c[5])
            if tuple(sorted(trial.items())) not in seen:
                fresh.append((c, trial))
        if fresh:
            for c, trial in fresh[:8]:
                if dirty_count(trial, c[6]) <= dirty_count(phys, c[6]):
                    chosen = c
                    break
            chosen = chosen or fresh[0][0]
            do("reset", chosen[4], chosen[5], chosen[6])
            continue
        # nothing clean to place: free a home held by a wrong piece, or move a piece that crowds a pending operation
        moved = False
        for s, loc in sorted(todo, key=lambda t: PRIORITY[t[0].lower()]):
            for h in blocked_homes(s):
                occ = phys[h]
                if occ == "?":
                    continue  # an unidentified piece is never moved
                own = [x for x in free_homes(occ)]
                spots = sorted(own, key=lambda x: _dist(cal, h, x)) + sorted((b for b in buffers if b not in phys and target.get(b) is None),
                                                                              key=lambda x: _dist(cal, h, x))
                for spot in spots:
                    if op_cost(cal, phys, h, spot, occ)[0] <= 0:
                        do("clear" if spot not in own else "reset", occ, h, spot, f"make room on {h}")
                        moved = True
                        break
                if moved:
                    break
            if moved:
                break
        if moved:
            continue
        if cands and lift_crowder(cands):
            continue
        # a wrong piece on a needed home that cannot be picked cleanly: lift whatever crowds it first
        clears = []
        for s, loc in todo:
            for h in blocked_homes(s):
                occ = phys[h]
                if occ == "?":
                    continue
                spot = next(iter(sorted((b for b in buffers if b not in phys and target.get(b) is None), key=lambda x: _dist(cal, h, x))), None)
                if spot is not None:
                    clears.append((True, PRIORITY[occ.lower()], _dist(cal, h, spot), op_cost(cal, phys, h, spot, occ)[0], occ, h, spot))
        if clears and lift_crowder(clears):
            continue
        if cands:  # accept the least-bad operation and record it
            best = min(cands, key=lambda c: (c[3], c[1], c[2]))
            do("reset", best[4], best[5], best[6], "tight: check this one")
            continue
        # every home of every pending piece is held by a wrong piece and nothing can move cleanly
        s, loc = todo[0]
        h = next((x for t, _ in todo for x in blocked_homes(t) if phys[x] != "?"), None)
        spot = next((b for b in buffers if b not in phys and target.get(b) is None), None)
        if h is None or spot is None:
            return Plan(ops, tight, missing, [f"{s}@{loc}" for s, loc in pending()], unreachable=True, final=phys)
        do("clear", phys[h], h, spot, f"make room on {h} (tight)")
    else:
        return Plan(ops, tight, missing, [f"{s}@{loc}" for s, loc in pending()], unreachable=True, final=phys)
    extra = [f"{s}@{loc}" for s, loc in pending() if surplus.get(s)]
    return Plan(ops, tight, missing, extra, unreachable=False, final=phys)


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    return {**plan.summary(), "ops_list": [asdict(o) for o in plan.ops], "tight_ops": plan.tight}


def move_clearance(world: World, board: chess.Board, move: chess.Move) -> float:
    """Total gripper contact (mm) the move's operations would cause, replayed in order on the tracked layout: 0 means
    the hand can make the move cleanly. Used to prefer, among the engine's best moves, the ones the hand can make."""
    try:
        ops = move_ops(world, board, move)
    except RuntimeError:
        return 1000.0
    phys = dict(world.physical)
    total = 0.0
    for op in ops:
        if phys.get(op.src) is None or op.dst in phys:
            return 1000.0
        cost, _ = op_cost(world.cal, phys, op.src, op.dst, op.symbol)
        total += cost
        phys[op.dst] = phys.pop(op.src)
    return round(total, 1)


# --- a whole game in the world model (used by the simulator and the match runner) --------------------------------------
def play_game(world: World, choose: Callable[[chess.Board], chess.Move], adjudicator: Adjudicator | None = None,
              score: Callable[[chess.Board], int | None] | None = None, board: chess.Board | None = None,
              on_ops: Callable[[chess.Move, list[PieceOp]], None] | None = None, max_plies: int = 400) -> dict[str, Any]:
    """Both sides by the robot: each move's operations are planned on the world and applied. Returns the game record."""
    board = board or chess.Board()
    adj = adjudicator or Adjudicator()
    record: list[dict[str, Any]] = []
    verdict = None
    for _ in range(max_plies):
        verdict = adj.verdict(board)
        if verdict:
            break
        mv = choose(board)
        ops = move_ops(world, board, mv)
        if on_ops:
            on_ops(mv, ops)
        for op in ops:
            world.apply(op)
        record.append({"san": board.san(mv), "uci": mv.uci(), "ops": [asdict(o) for o in ops]})
        board.push(mv)
        if score:
            adj.update(score(board))
    return {"moves": record, "verdict": verdict or {"result": "*", "reason": "ply limit"}, "board": board.fen(),
            "world": world.to_dict()}
