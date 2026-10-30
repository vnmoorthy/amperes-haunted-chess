"""Arranging from a recognised board: depth decides occupancy and colour, Claude the type; unknown pieces never move."""

import chess

from amperes.integrations import chess_arrange as A
from amperes.integrations import chess_lifecycle as L
from amperes.integrations import chess_sim as S
from tests.test_chess_lifecycle import cal  # noqa: F401  (fixture)


def test_fuse_counts_either_sensor_and_keeps_claudes_colour():
    obs = {"e2": {"occupied": True, "colour": "white", "visible": True}, "e4": {"occupied": True, "colour": "black", "visible": True},
           "d4": {"occupied": False, "visible": True}, "a1": {"occupied": True, "colour": "white", "visible": True}}
    read = {"e2": {"symbol": "P", "confidence": 0.9}, "e4": {"symbol": "N", "confidence": 0.7}, "d4": {"symbol": "q", "confidence": 0.95}}
    layout, conf, notes = A.fuse(obs, read, {"f4": "a white pawn lying on its side"})
    # e4 keeps Claude's colour; d4 counts although depth missed it; a1 unnamed and f4 obstructed are no-go squares
    assert layout == {"e2": "P", "e4": "N", "d4": "q", "a1": "?", "f4": "?"}
    assert any("d4" in n for n in notes) and any("e4" in n for n in notes) and any("f4" in n for n in notes)


def test_counts_follow_the_set():
    layout = {"e1": "K", "d1": "K", "a2": "P", "b2": "P", "c2": "P", "d2": "P", "e2": "P", "f2": "P", "g2": "P", "h2": "P", "a3": "P"}
    conf = {sq: 0.9 for sq in layout} | {"d1": 0.4, "a3": 0.3}
    changes = A.enforce_counts(layout, conf)
    assert layout["d1"] == "Q" and layout["a3"] == "R" and len(changes) == 2


def test_unknown_pieces_are_never_moved_or_covered(cal):  # noqa: F811
    layout = dict(L.start_target())
    layout = {k: v for k, v in layout.items() if L.is_square(k)}
    layout.pop("e2")
    layout["e2"] = "?"          # something nobody could name on a pawn home
    layout.pop("d2")
    layout["d4"] = "P"          # a pawn to bring home next to it
    plan = A.plan_for(cal, layout, slots_clear=False)
    assert all(o.src != "e2" and o.dst != "e2" for o in plan.ops)
    assert all(L.is_square(o.dst) for o in plan.ops)  # no unseen spot beside the board is used
    r = S.replay(cal, layout, plan.ops)
    assert not r["invalid"] and r["final"]["d2"] == "P"


def test_start_position_needs_nothing(cal):  # noqa: F811
    layout = {k: v for k, v in L.start_target().items() if L.is_square(k)}
    plan = A.plan_for(cal, layout, slots_clear=False)
    assert plan.ops == [] and plan.ok


def test_lifted_piece_that_is_something_else_goes_to_its_own_home(cal, tmp_path, monkeypatch):  # noqa: F811
    import asyncio

    import numpy as np

    from amperes.integrations import chess_claude_vision as CCV
    from amperes.integrations import chess_robot as CR

    world = L.World(cal, physical={"e4": "P", "e1": "K"})
    game = CR.Game(None, robot_color=None, world=world)
    sess = CR.ChessSession(CR.ChessSpec(out_dir=tmp_path), cal, game)
    (tmp_path / "images").mkdir()
    sess._current_op = CR.PieceOp("reset", "P", "e4", "e2")  # the plan thinks it is a pawn

    async def fake_grab(m, name):
        return np.zeros((48, 64, 3), np.uint8)

    monkeypatch.setattr(A, "grab_colour", fake_grab)
    monkeypatch.setattr(CCV, "available", lambda: True)
    monkeypatch.setattr(CCV, "identify_pieces", lambda crops, context="", effort="low": {"held": {"symbol": "R", "confidence": 0.9, "reason": "notched flat top"}})
    seen = []
    inspect = A.make_inspector(sess, cal, world, lambda *_: None, seen)
    src = sess._pose_for(sess._current_op, "src", A.board_from(world))
    new = asyncio.run(inspect(0, src, None))
    assert new["_name"] in ("a1", "h1") and new["z"] == src["z"]  # a rook goes to a rook home, held at the same height
    assert world.physical["e4"] == "R" and sess._current_op.symbol == "R" and "re-identified" in sess._current_op.note
    assert seen and seen[0]["planned"] == "P" and seen[0]["seen"] == "R"
    # a confident match, or an unsure answer, keeps the plan
    monkeypatch.setattr(CCV, "identify_pieces", lambda crops, context="", effort="low": {"held": {"symbol": "?", "confidence": 0.2, "reason": "hidden"}})
    sess._current_op = CR.PieceOp("reset", "K", "e1", "e1")
    assert asyncio.run(inspect(1, src, None)) is None
