import asyncio
import json
import math
import threading
from dataclasses import asdict

import chess
import pytest

from amperes import store
from amperes.ingest.pipeline import import_dataset
from amperes.integrations import chess_robot as CR
from amperes.integrations.viam_control import TrialConfig
from tests.test_viam_control import FakeArm, FakeGripper
from tests.test_viam_tasks import FakeMotion

ANG = math.radians(10)
A1 = {"x": 260.0, "y": -190.0, "z": -23 + 150 + 2, "o_x": 0.0, "o_y": 0.0, "o_z": -1.0, "theta": 0.0}
H1 = {**A1, "x": A1["x"] + 7 * 55 * math.cos(ANG), "y": A1["y"] + 7 * 55 * math.sin(ANG)}
GRAVE = {**A1, "x": A1["x"] - 90, "y": A1["y"]}


def cal():
    return CR.BoardCal(a1=A1, h1=H1, graveyard=GRAVE)


class FakeEngine:
    def __init__(self, moves):
        self.moves = list(moves)

    def choose(self, board):
        return chess.Move.from_uci(self.moves.pop(0))


def test_board_geometry_right_handed_and_rotated():
    c = cal()
    assert c.square_mm == pytest.approx(55.0)
    assert c.board_top_z == pytest.approx(-21.0)
    a8 = c.square_xy("a8")  # +90° counter-clockwise from the file axis, seen from above
    exp = (A1["x"] - 7 * 55 * math.sin(ANG), A1["y"] + 7 * 55 * math.cos(ANG))
    assert a8 == pytest.approx(exp, abs=1e-6)
    assert c.theta_deg == pytest.approx(55.0)
    assert c.grasp_z("p") <= c.grasp_z("k") < c.transport_z < c.look_pose()["z"]
    env = c.envelope()
    assert not env.check(c.grasp_pose("h8", "k"), c.grasp_pose("a1", "p"))
    assert env.check({**c.grasp_pose("e4", "p"), "z": c.board_top_z + 150}, None)  # fingertips on the board: refused
    assert not env.check(c.graveyard_pose("p", 15), None)


def test_plan_ops_capture_castling_en_passant():
    b = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    ops = CR.plan_ops(b, chess.Move.from_uci("e1g1"), 0)
    assert [(o.kind, o.src, o.dst) for o in ops] == [("move", "e1", "g1"), ("castle_rook", "h1", "f1")]
    b = chess.Board("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 2")
    ops = CR.plan_ops(b, chess.Move.from_uci("e5d6"), 3)
    assert [(o.kind, o.symbol, o.src, o.dst) for o in ops] == [("capture", "p", "d5", "grave3"), ("move", "P", "e5", "d6")]
    b = chess.Board("4k3/8/8/8/8/8/3q4/4K3 w - - 0 1")
    ops = CR.plan_ops(b, chess.Move.from_uci("e1d2"), 0)
    assert ops[0].kind == "capture" and ops[0].symbol == "q" and ops[1].dst == "d2"


def test_game_flow_and_illegal_moves():
    g = CR.Game(FakeEngine(["e7e5"]), robot_color=chess.BLACK)
    with pytest.raises(ValueError):
        g.human_move("e5")  # illegal for white
    g.human_move("e4")
    mv, ops = g.robot_move()
    assert mv.uci() == "e7e5" and ops[0].symbol == "p"
    g.commit_robot_move(mv, ops, [{"outcome": "success"}])
    assert g.board.fen().startswith("rnbqkbnr/pppp1ppp/8/4p3/4P3") and g.status()["turn"] == "white"


def _session(execute=True, grab=True):
    arm = FakeArm()
    c = cal()
    arm.p = dict(c.look_pose())
    motion = FakeMotion(arm)
    spec = CR.ChessSpec(out_dir=None, execute=execute)
    game = CR.Game(FakeEngine(["e7e5", "d8h4"]), robot_color=chess.BLACK)
    s = CR.ChessSession(spec, c, game, threading.Event())
    s.ctl = CR.Controller(arm, FakeGripper(grab=grab), motion, "arm", execute, c.envelope(), s.stop_event, frame="world")
    s.session = {"machine": "t", "session_id": "sid", "started": 1.0, "execute": execute, "config": asdict(TrialConfig(name="chess")),
                 "calibration": asdict(c), "envelope": asdict(c.envelope()), "robot_color": "black", "object_name": "chess piece",
                 "telemetry": [], "images": [], "trials": [], "events": [], "moves": []}
    return s, arm, motion


def test_robot_move_executes_ops_through_the_controller_and_records():
    s, arm, motion = _session()
    s.game.human_move("e4")
    rec = asyncio.run(s.play_robot_move())
    assert rec["uci"] == "e7e5" and rec["executed_ok"] is True
    labels = [a["label"] for a in s.session["trials"][0]["steps"] if a["kind"] == "move"]
    assert labels[0] == "approach_source" and labels[-1] == "retreat" and "lift" in labels and "transport" in labels
    assert any(l.startswith("descend_grasp") for l in labels) and any(l.startswith("lower_place") for l in labels)  # stepped descent when executing
    grasp = [a for a in s.session["trials"][0]["steps"] if a["kind"] == "move" and a["label"].startswith("descend_grasp")][-1]["target"]
    assert grasp["z"] == pytest.approx(s.cal.grasp_z("p", s.tallest_neighbour("e7")), abs=0.01)  # queen/king/bishop beside e7: grip higher
    assert grasp["theta"] in [pytest.approx(s.cal.theta_deg - 45.0 + off, abs=0.01) for off in (45.0, -45.0, 0.0, 90.0)]  # neighbour-aware
    assert s.session["trials"][0]["chess"]["piece"] == "black pawn"
    assert motion.moves[0]["z"] == pytest.approx(s.cal.look_pose()["z"])  # looked at the board first
    s.game.human_move("Nf3")
    rec = asyncio.run(s.play_robot_move())  # Qh4, no capture
    assert rec["san"] == "Qh4" and len(s.session["trials"]) == 2


def test_dry_run_moves_nothing():
    s, arm, motion = _session(execute=False)
    s.game.human_move("e4")
    rec = asyncio.run(s.play_robot_move())
    assert motion.moves == [] and rec["executed_ok"] is None and s.session["trials"][0]["outcome"] == "unknown"


def test_chess_session_to_canonical_imports(tmp_path):
    s, arm, motion = _session(grab=False)
    s.game.human_move("e4")
    asyncio.run(s.play_robot_move())
    base, seconds = 1_790_000_000.0, 19 / 3
    tr = s.session["trials"][0]
    span = (tr["ended"] - tr["started"]) or 1e-9
    f = lambda t: base + (t - tr["started"]) / span * seconds  # noqa: E731
    tr["steps"] = [{**a, "t": f(a["t"])} for a in tr["steps"]]
    tr["started"], tr["ended"] = base, base + seconds
    s.session["telemetry"] = [{"t": base + i / 3, "joints": [float(i)] * 6, "pose": [300 + i, 0, 400, 0, 0, -1, 0]} for i in range(20)]
    s.session["ended"] = base + seconds + 1
    summ = CR.chess_session_to_canonical(s.session, tmp_path / "c")
    assert summ["piece_ops"] == 1 and summ["robot_moves"] == 1
    ep = json.loads((tmp_path / "c" / "episodes.jsonl").read_text())
    assert ep["task"]["id"] == "chess_move_real" and ep["conditions"]["move_san"] == "e5"
    assert ep["conditions"]["instruction"] == "Move the black pawn from e7 to e5."
    assert ep["outcome"]["label"] == "failure" and ep["outcome"]["reason"] == "GRASP_FAILURE"
    r = import_dataset(tmp_path / "c", adapter="jsonl", ctx={"required_metadata": ["task.id"]})
    st = store.query("SELECT outcome, quality_status FROM episodes WHERE dataset_id=?", [r["dataset_id"]])[0]
    assert st["outcome"] == "failure" and st["quality_status"] == "valid_failed_task"
    assert (tmp_path / "c" / "moves.jsonl").read_text().count("fen_before") == 1


def test_typed_move_ends_the_camera_watch(tmp_path):
    game = CR.Game(FakeEngine([]), robot_color=chess.WHITE)
    game.board.push_san("e4")  # the robot has moved: the person's turn
    s = CR.ChessSession(CR.ChessSpec(out_dir=tmp_path), cal(), game, threading.Event())
    polls = []

    async def look(*a, **k):
        return []

    async def observe(**k):
        polls.append(1)
        game.human_move("e5")  # typed into the console while the camera is watching
        return {}

    s.look, s.observe = look, observe
    s.session.update(trials=[], moves=[])  # filled in by connect() on the real arm
    assert asyncio.run(s.watch_human_move(timeout_s=5, poll_s=0.01)) is None
    assert polls == [1] and game.board.turn == chess.WHITE and [h["san"] for h in game.history] == ["e5"]
