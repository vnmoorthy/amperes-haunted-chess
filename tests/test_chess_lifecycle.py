"""Match lifecycle: captures to slots, promotion with the spare queen, game end, and the clearance-ordered reset."""

import random

import chess
import pytest

from amperes.integrations import chess_lifecycle as L
from amperes.integrations import chess_robot as CR
from amperes.integrations import chess_sim as S


@pytest.fixture(scope="module")
def cal():
    # the board taught on 29 Oct (a1 near the arm, files running towards -y); independent of the local calibration file
    return CR.BoardCal(a1={"x": 209.2, "y": 167.4, "z": 178.0}, h1={"x": 223.2, "y": -213.6, "z": 178.0},
                       graveyard={"x": 205.0, "y": 292.0, "z": 178.0}, king_height_mm=85.0, grip_fraction=0.55, grasp_mm=10.0)


def play(world, board, sans):
    for san in sans.split():
        mv = board.parse_san(san)
        for op in L.move_ops(world, board, mv):
            world.apply(op)
        board.push(mv)


def test_slots_are_reachable_and_off_the_board(cal):
    slots = L.all_slots(cal)
    assert "reserveQ" in slots and "reserveq" in slots
    assert len(L.capture_slots(cal)) >= 10
    for s in slots:
        x, y = L.location_xy(cal, s)
        assert (x * x + y * y) ** 0.5 <= L.MAX_REACH_MM
        # at least two squares outside the a-file, i.e. clear of the board
        assert min(((x - a) ** 2 + (y - b) ** 2) ** 0.5 for a, b in (cal.square_xy(f"a{r}") for r in range(1, 9))) > 1.9 * cal.square_mm
    env = cal.envelope()
    for s in slots:
        x, y = L.location_xy(cal, s)
        assert env.x[0] <= x <= env.x[1] and env.y[0] <= y <= env.y[1]


def test_capture_goes_to_a_free_slot_and_world_tracks_it(cal):
    w, b = L.World(cal), chess.Board()
    play(w, b, "e4 d5 exd5")
    off = w.off_board()
    assert sorted(off.values()) == ["Q", "p", "q"]  # the captured pawn plus the two spare queens
    assert w.board_fen() == b.board_fen()


def test_castling_and_en_passant_ops(cal):
    w, b = L.World(cal), chess.Board()
    play(w, b, "e4 Nf6 Nf3 Nc6 Bc4 Ne5")
    ops = L.move_ops(w, b, b.parse_san("O-O"))
    assert [(o.kind, o.src, o.dst) for o in ops] == [("move", "e1", "g1"), ("castle_rook", "h1", "f1")]
    w2, b2 = L.World(cal), chess.Board()
    play(w2, b2, "e4 a6 e5 d5")
    ops = L.move_ops(w2, b2, b2.parse_san("exd6"))
    assert ops[0].kind == "capture" and ops[0].src == "d5" and ops[0].dst.startswith("grave")


def test_promotion_uses_the_spare_queen_like_a_person(cal):
    board = chess.Board("8/P5k1/8/8/8/8/6K1/8 w - - 0 1")
    w = L.World(cal, physical={"a7": "P", "g7": "k", "g2": "K", "reserveQ": "Q", "reserveq": "q"})
    ops = L.move_ops(w, board, chess.Move.from_uci("a7a8q"))
    assert [(o.kind, o.symbol, o.src) for o in ops] == [("promote_out", "P", "a7"), ("promote_in", "Q", "reserveQ")]
    assert ops[1].dst == "a8" and ops[0].dst.startswith("grave")
    for op in ops:
        w.apply(op)
    assert w.physical["a8"] == "Q" and "reserveQ" not in w.physical


def test_second_promotion_takes_a_captured_queen_or_asks_for_a_hand_swap(cal):
    board = chess.Board("8/P5k1/8/8/8/8/6K1/8 w - - 0 1")
    w = L.World(cal, physical={"a7": "P", "g7": "k", "g2": "K", "grave3": "Q"})  # spare already used, a queen was captured
    ops = L.move_ops(w, board, chess.Move.from_uci("a7a8q"))
    assert ops[-1].src == "grave3" and ops[-1].kind == "promote_in"
    w2 = L.World(cal, physical={"a7": "P", "g7": "k", "g2": "K"})
    ops2 = L.move_ops(w2, board, chess.Move.from_uci("a7a8n"))
    assert len(ops2) == 1 and "by hand" in ops2[0].note


@pytest.mark.parametrize("line", ["f3 e5 g4 Qh4#", "e4 e5 Bc4 Nc6 Qh5 Nf6 Qxf7#"])
def test_reset_after_miniatures_is_complete_and_clean(cal, line):
    w, b = L.World(cal), chess.Board()
    play(w, b, line)
    assert L.Adjudicator().verdict(b)["reason"] == "checkmate"
    plan = L.plan_reset(cal, w.physical)
    r = S.replay(cal, w.physical, plan.ops)
    assert plan.ok and not r["invalid"] and not r["violations"]
    assert r["final"] == L.start_target()


def test_reset_lifts_the_king_to_slip_a_pawn_in(cal):
    # black pawn back to e7 next to a king already home: a person lifts the king, places the pawn, puts the king back
    phys = {**L.start_target()}
    phys.pop("e7")
    phys["e5"] = "p"
    plan = L.plan_reset(cal, phys)
    kinds = [(o.kind, o.symbol) for o in plan.ops]
    assert ("clear", "k") in kinds and plan.ok and not plan.tight


def test_random_scrambles_reach_the_start_position_cleanly(cal):
    rng = random.Random(5)
    for _ in range(12):
        phys = S.scramble(rng, cal)
        plan = L.plan_reset(cal, phys)
        r = S.replay(cal, phys, plan.ops)
        assert plan.ok and not r["invalid"], plan.summary()
        assert r["final"] == L.start_target()
        assert not r["violations"], r["violations"][:2]


def test_missing_pieces_are_reported_not_invented(cal):
    phys = dict(L.start_target())
    phys.pop("reserveq")
    phys.pop("b8")
    plan = L.plan_reset(cal, phys)
    assert plan.missing == {"q": 1, "n": 1}


def test_human_capture_goes_to_unknown(cal):
    w, b = L.World(cal), chess.Board()
    play(w, b, "e4 d5")
    mv = b.parse_san("exd5")
    w.human_move(b, mv)
    b.push(mv)
    assert w.unknown == ["p"] and w.board_fen() == b.board_fen()


def test_adjudicator_resigns_and_caps():
    adj = L.Adjudicator(ply_cap=3)
    b = chess.Board()
    for s in (700, 800, 900, 950):
        adj.update(s)
    assert adj.verdict(b) == {"result": "1-0", "reason": "black resigns"}
    b2 = chess.Board()
    for m in ("e4", "e5", "Nf3"):
        b2.push_san(m)
    assert L.Adjudicator(ply_cap=3).verdict(b2)["reason"].startswith("stopped")


def test_game_with_world_plans_slots_and_tracks(cal):
    class Eng:
        def __init__(self, moves):
            self.moves = list(moves)

        def choose(self, board, feasible=None):
            return chess.Move.from_uci(self.moves.pop(0))

    w = L.World(cal)
    g = CR.Game(Eng(["e2e4", "e4d5"]), robot_color=None, world=w)
    mv, ops = g.robot_move()
    g.commit_robot_move(mv, ops, [{"outcome": "success"}])
    g.human_move("d5")
    mv, ops = g.robot_move()
    assert ops[0].kind == "capture" and ops[0].dst.startswith("grave")
    g.commit_robot_move(mv, ops, [{"outcome": "success"}] * len(ops))
    assert w.board_fen() == g.board.board_fen()
    assert "p" in w.off_board().values()


def test_session_pose_for_reserve_slot_and_slot_neighbours(cal, tmp_path):
    w = L.World(cal)
    g = CR.Game(None, robot_color=None, world=w)
    sess = CR.ChessSession(CR.ChessSpec(out_dir=tmp_path), cal, g)
    op = CR.PieceOp("promote_in", "Q", "reserveQ", "a8")
    src = sess._pose_for(op, "src")
    x, y = cal.graveyard_xy(CR.RESERVE_SLOT["reserveQ"])
    assert src["x"] == pytest.approx(x) and src["y"] == pytest.approx(y)
    # a pawn picked from the slot beside the spare queen is gripped higher than the bare 10 mm
    w.physical["grave4"] = "p"
    pose = sess._pose_for(CR.PieceOp("reset", "p", "grave4", "e7"), "src")
    assert pose["z"] > cal.grasp_z("p")
