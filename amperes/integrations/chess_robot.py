"""Chess on the Viam arm: Stockfish chooses the robot's move, the arm plays it on a real board against a human.

Why this is here: every robot move is a supervised pick-and-place with a known object (the piece), a known target
(the square), an independent label (the game state the human agrees to), and natural variety in positions, piece shapes
and board crowding. Each robot turn also yields an overhead board photo labelled with the exact position (FEN).

Safety is `viam_control`'s model unchanged: dry run by default, motion needs `execute` + the operator acknowledgement,
every target is checked against an envelope (the board plus the graveyard spot, floor 5 mm above the board surface),
moves go through the motion service, telemetry watchdog, STOP mid-move. All motion still happens inside
`Controller._move` / `Controller._gripper`.

Calibration: the operator jogs the open gripper (fingers on the diagonal) so the fingertips touch the board at the centre
of **a1**, teaches it, then the same at **h1**, then a spot beside the board for captured pieces (**graveyard**).
a8 follows from the right-handed board convention (a1 → h1 → a8 is counter-clockwise seen from above).
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import chess
import numpy as np

from amperes.integrations.viam_control import (client_options, POSE_KEYS, WATCHDOG_S, Controller, Envelope, SafetyError, TrialConfig, dial_robot,
                                               required_ack, timed)

TOOL_LENGTH_MM = 150.0
BODY_CLEAR_MM = 50.0  # gripper body underside above the fingertips (module geometry: case 40-150, claws 88-200 from the flange)
CAM_OFFSET = (83.0, 14.0)  # wrist camera position relative to the flange in the arm frame at theta 0 (measured)
DOWN = {"o_x": 0.0, "o_y": 0.0, "o_z": -1.0}
# Viam resource names, as configured on the event machine; another machine sets these in its environment
ARM = os.environ.get("AMPERES_ARM", "arm")
GRIPPER = os.environ.get("AMPERES_GRIPPER", "gripper")
MOTION = os.environ.get("AMPERES_MOTION", "builtin")
CAMERAS = tuple(c.strip() for c in os.environ.get("AMPERES_CAMERAS", "cam,cam2").split(",") if c.strip())  # wrist first
# Staunton proportions (3.75" king); scaled by the calibrated king height. Grasp at `grip_fraction` of the piece height.
PIECE_HEIGHT = {"k": 95.0, "q": 85.0, "b": 70.0, "n": 60.0, "r": 55.0, "p": 50.0}
PIECE_NAME = {"k": "king", "q": "queen", "b": "bishop", "n": "knight", "r": "rook", "p": "pawn"}


def calibration_path() -> Path:
    from amperes import store

    p = store.home() / "robot" / "chessboard.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class BoardCal:
    """Flange poses taught with the fingertips touching the board surface at the centre of a1 and h1, plus a graveyard spot."""

    a1: dict[str, float]
    h1: dict[str, float]
    graveyard: dict[str, float]
    king_height_mm: float = 95.0
    grip_fraction: float = 0.45
    clear_mm: float = 25.0          # transport height above the tallest piece
    floor_clearance_mm: float = 5.0  # fingertips never closer than this to the board surface during play

    @classmethod
    def load(cls) -> "BoardCal":
        p = calibration_path()
        if not p.exists():
            raise ValueError("chessboard not calibrated: teach a1, h1 and graveyard first (robot chess teach <name>)")
        d = json.loads(p.read_text())
        need = {"a1", "h1", "graveyard"} - set(d)
        if need:
            raise ValueError(f"chessboard calibration incomplete; teach {sorted(need)}")
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @staticmethod
    def save_point(name: str, pose: dict[str, float], meta: dict[str, Any] | None = None) -> dict[str, Any]:
        if name not in ("a1", "h1", "graveyard"):
            raise ValueError("chess teach names: a1, h1, graveyard")
        p = calibration_path()
        d = json.loads(p.read_text()) if p.exists() else {}
        d[name] = {**{k: round(float(pose[k]), 3) for k in POSE_KEYS}, "_taught_at": time.time(), **(meta or {})}
        p.write_text(json.dumps(d, indent=2))
        return d[name]

    @staticmethod
    def save_settings(**kw) -> dict[str, Any]:
        p = calibration_path()
        d = json.loads(p.read_text()) if p.exists() else {}
        d.update({k: v for k, v in kw.items() if v is not None})
        p.write_text(json.dumps(d, indent=2))
        return d

    # --- geometry -------------------------------------------------------------------------------------------------
    @property
    def board_top_z(self) -> float:
        return (self.a1["z"] + self.h1["z"]) / 2 - TOOL_LENGTH_MM

    @property
    def file_axis(self) -> tuple[float, float]:
        return ((self.h1["x"] - self.a1["x"]) / 7, (self.h1["y"] - self.a1["y"]) / 7)

    @property
    def rank_axis(self) -> tuple[float, float]:
        fx, fy = self.file_axis
        return (-fy, fx)  # +90° counter-clockwise seen from above

    @property
    def square_mm(self) -> float:
        return math.hypot(*self.file_axis)

    @property
    def theta_deg(self) -> float:
        fx, fy = self.file_axis
        return math.degrees(math.atan2(fy, fx)) + 45.0  # fingers on the board diagonal: they clear neighbouring pieces

    def square_xy(self, sq: str) -> tuple[float, float]:
        f, r = chess.square_file(chess.parse_square(sq)), chess.square_rank(chess.parse_square(sq))
        fx, fy = self.file_axis
        rx, ry = self.rank_axis
        return (self.a1["x"] + fx * f + rx * r, self.a1["y"] + fy * f + ry * r)

    def piece_height(self, symbol: str) -> float:
        return PIECE_HEIGHT[symbol.lower()] * self.king_height_mm / 95.0

    grasp_mm: float = 8.0  # fingertips this far above the board for every piece: the 30 mm finger pads then squeeze the base

    def grasp_z(self, symbol: str, tallest_neighbour_mm: float = 0.0) -> float:
        """Fingertips `grasp_mm` above the board (pieces are held by the base); next to a tall neighbour the gripper body
        (50 mm above the fingertips) must clear it, so the grip moves up the piece, leaving at least 15 mm to hold."""
        h = self.grasp_mm
        if tallest_neighbour_mm > BODY_CLEAR_MM + h:
            h = min(tallest_neighbour_mm - BODY_CLEAR_MM + 5.0, self.piece_height(symbol) - 15.0)
            h = max(h, self.grasp_mm)
        return self.board_top_z + TOOL_LENGTH_MM + h

    @property
    def transport_z(self) -> float:
        # a carried piece hangs below the fingertips by (1 - grip_fraction) of its height; clear the king with that in hand
        hang = (1.0 - self.grip_fraction) * self.king_height_mm
        return self.board_top_z + TOOL_LENGTH_MM + self.king_height_mm + hang + self.clear_mm

    def grasp_pose(self, sq: str, symbol: str, tallest_neighbour_mm: float = 0.0) -> dict[str, float]:
        x, y = self.square_xy(sq)
        return {"x": x, "y": y, "z": self.grasp_z(symbol, tallest_neighbour_mm), **DOWN, "theta": self.theta_deg}

    def graveyard_xy(self, slot: int) -> tuple[float, float]:
        """Captured pieces line up PARALLEL to the board edge (along the rank axis), one square apart, starting 2.3 squares
        outside the a-file; the ninth and later go one more square outward. (Stepping along the file axis walked the
        second captured piece back onto the board next to the a1 rook.)"""
        fx, fy = self.file_axis
        rx, ry = self.rank_axis
        a1x, a1y = self.square_xy("a1")
        out = 2.3 + (slot // 8)          # squares outside the a-file
        along = slot % 8                  # squares along the edge, from rank 1 towards rank 8
        return (a1x - fx * out + rx * along, a1y - fy * out + ry * along)

    def graveyard_pose(self, symbol: str, slot: int) -> dict[str, float]:
        x, y = self.graveyard_xy(slot)
        return {"x": x, "y": y, "z": self.grasp_z(symbol), **DOWN, "theta": self.theta_deg}

    def offboard_pose(self, symbol: str, name: str) -> dict[str, float]:
        """Pose for an off-board spot: 'grave<N>' (captured pieces), 'reserveQ' / 'reserveq' (the spare queens), or
        'xy:<x>,<y>' (a piece the survey found beside the board, at arm coordinates)."""
        if name.startswith("xy:"):
            xs, ys = name[3:].split(",")
            return {"x": float(xs), "y": float(ys), "z": self.grasp_z(symbol), **DOWN, "theta": self.theta_deg}
        return self.graveyard_pose(symbol, offboard_slot(name))

    look_height_mm: float = 527.0  # flange above the (tool-corrected) board top at the look pose: ~57 px squares, all 64 in frame

    def look_pose(self) -> dict[str, float]:
        """Wrist camera centred over the board (the camera sits 83 mm ahead of and 14 mm beside the flange), high enough for
        all 64 squares; fingers square to the files so the photo is axis-aligned."""
        cx, cy = [(a + b) / 2 for a, b in zip(self.square_xy("a1"), self.square_xy("h8"))]
        theta = self.theta_deg - 45.0
        a = math.radians(-theta)  # the camera offset measured at theta 0 rotates with the wrist (verified at theta -88: (-11, 83))
        ox, oy = CAM_OFFSET[0] * math.cos(a) - CAM_OFFSET[1] * math.sin(a), CAM_OFFSET[0] * math.sin(a) + CAM_OFFSET[1] * math.cos(a)
        return {"x": cx - ox, "y": cy - oy, "z": max(self.transport_z + 40.0, self.board_top_z + TOOL_LENGTH_MM + self.look_height_mm),
                **DOWN, "theta": theta}

    def envelope(self, margin_mm: float = 40.0) -> Envelope:
        pts = [self.square_xy(s) for s in ("a1", "h1", "a8", "h8")] + [self.graveyard_xy(k) for k in (0, 7, 8, 15)]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        look = self.look_pose()
        longest = max(math.dist(p, q) for p in pts for q in pts)
        return Envelope(x=(min(xs) - margin_mm, max(xs) + margin_mm), y=(min(ys) - margin_mm, max(ys) + margin_mm),
                        z=(self.board_top_z + TOOL_LENGTH_MM + self.floor_clearance_mm, look["z"] + margin_mm),
                        max_step_mm=longest + 80.0, max_theta_change_deg=100.0)

    def describe(self) -> str:
        return (f"board: {self.square_mm:.0f} mm squares, top at z={self.board_top_z:.0f} mm, files along "
                f"({self.file_axis[0]:.1f}, {self.file_axis[1]:.1f}) mm; king {self.king_height_mm:g} mm; grasp at "
                f"{self.grip_fraction:.0%} height; transport {self.transport_z - self.board_top_z - TOOL_LENGTH_MM:.0f} mm above the board")


RESERVE_SLOT = {"reserveQ": 12, "reserveq": 13}  # the spare queens' spots: outer slot line, 5th and 6th from the rank-1 end


def offboard_slot(name: str) -> int:
    if name in RESERVE_SLOT:
        return RESERVE_SLOT[name]
    if name.startswith("grave"):
        return int(name[5:])
    raise ValueError(f"not an off-board spot: {name}")


# --- game logic ---------------------------------------------------------------------------------------------------
@dataclass
class PieceOp:
    """One physical pick-and-place: piece `symbol` from `src` to `dst` ('grave' = graveyard slot)."""

    kind: str          # move | capture | castle_rook
    symbol: str        # python-chess symbol, e.g. 'N' (white knight), 'p' (black pawn)
    src: str
    dst: str
    note: str = ""


def plan_ops(board: chess.Board, move: chess.Move, grave_slot: int) -> list[PieceOp]:
    """Physical operations for `move` on `board` (before pushing it). Captured piece goes to the graveyard first."""
    ops: list[PieceOp] = []
    piece = board.piece_at(move.from_square)
    if piece is None:
        raise ValueError(f"no piece on {chess.square_name(move.from_square)}")
    if board.is_en_passant(move):
        cap_sq = move.to_square + (-8 if piece.color == chess.WHITE else 8)
        ops.append(PieceOp("capture", board.piece_at(cap_sq).symbol(), chess.square_name(cap_sq), f"grave{grave_slot}", "en passant"))
    elif board.is_capture(move):
        ops.append(PieceOp("capture", board.piece_at(move.to_square).symbol(), chess.square_name(move.to_square), f"grave{grave_slot}"))
    ops.append(PieceOp("move", piece.symbol(), chess.square_name(move.from_square), chess.square_name(move.to_square),
                       "promotion: swap the piece by hand" if move.promotion else ""))
    if board.is_castling(move):
        rank = "1" if piece.color == chess.WHITE else "8"
        if chess.square_file(move.to_square) == 6:
            ops.append(PieceOp("castle_rook", "R" if piece.color == chess.WHITE else "r", "h" + rank, "f" + rank))
        else:
            ops.append(PieceOp("castle_rook", "R" if piece.color == chess.WHITE else "r", "a" + rank, "d" + rank))
    return ops


class Engine:
    """Stockfish through python-chess; `FakeEngine` in tests."""

    def __init__(self, path: str | None = None, skill: int = 5, think_s: float = 0.5):
        import shutil

        import chess.engine

        self.path = path or shutil.which("stockfish") or "/opt/homebrew/bin/stockfish"
        self.engine = chess.engine.SimpleEngine.popen_uci(self.path)
        try:
            self.engine.configure({"Skill Level": max(0, min(20, skill))})
        except Exception:
            pass
        self.limit = chess.engine.Limit(time=think_s)

    def choose(self, board: chess.Board, feasible=None) -> chess.Move:
        """Best move by the engine; with `feasible(move) -> score` (lower is better, 0 = clean), the best-scoring of the
        engine's top five is played, ties broken by engine order. The arm's reach, not the eval, decides crowded picks."""
        if feasible is None:
            return self.engine.play(board, self.limit).move
        try:
            infos = self.engine.analyse(board, self.limit, multipv=5)
            cands = [i["pv"][0] for i in infos if i.get("pv")]
        except Exception:
            cands = []
        if not cands:
            return self.engine.play(board, self.limit).move
        scored = sorted(((feasible(m), k, m) for k, m in enumerate(cands)), key=lambda t: (t[0], t[1]))
        return scored[0][2]

    def close(self):
        try:
            self.engine.quit()
        except Exception:
            pass


class Game:
    def __init__(self, engine, robot_color: bool = chess.BLACK, fen: str | None = None, world=None):
        self.board = chess.Board(fen) if fen else chess.Board()
        self.engine, self.robot_color = engine, robot_color
        self.history: list[dict[str, Any]] = []
        self.grave_slot = 32 - len(self.board.piece_map())  # pieces already off the board occupy the first slots
        self.world = world  # chess_lifecycle.World: where every physical piece is (slots, spare queens), when tracked

    def human_move(self, text: str) -> chess.Move:
        try:
            mv = self.board.parse_san(text)
        except ValueError:
            mv = chess.Move.from_uci(text)
        if mv not in self.board.legal_moves:
            raise ValueError(f"illegal move {text} in {self.board.fen()}")
        san = self.board.san(mv)
        if self.world is not None:
            self.world.human_move(self.board, mv)
        self.board.push(mv)
        self.history.append({"ply": len(self.history) + 1, "by": "human", "uci": mv.uci(), "san": san, "fen_after": self.board.fen()})
        return mv

    feasibility = None  # optional callable(move) -> crowding score set by the session

    def robot_move(self) -> tuple[chess.Move, list[PieceOp]]:
        if self.robot_color is not None and self.board.turn != self.robot_color:
            raise ValueError("not the robot's turn")
        mv = self.engine.choose(self.board, self.feasibility) if self.feasibility else self.engine.choose(self.board)
        if self.world is not None:
            from amperes.integrations import chess_lifecycle as L

            ops = L.move_ops(self.world, self.board, mv)  # captures to free slots, promotion with the spare queen
        else:
            ops = plan_ops(self.board, mv, self.grave_slot)
            self.grave_slot += sum(op.kind == "capture" for op in ops)
        return mv, ops

    def commit_robot_move(self, mv: chess.Move, ops: list[PieceOp], results: list[dict[str, Any]]):
        san = self.board.san(mv)
        fen_before = self.board.fen()
        if self.world is not None:  # the game state follows the engine; so does the tracked layout
            for op in ops:
                try:
                    self.world.apply(op)
                except ValueError:
                    pass
        self.board.push(mv)
        self.history.append({"ply": len(self.history) + 1, "by": "robot", "uci": mv.uci(), "san": san, "fen_before": fen_before,
                             "fen_after": self.board.fen(), "ops": [asdict(o) for o in ops],
                             "outcomes": [r["outcome"] for r in results]})

    def status(self) -> dict[str, Any]:
        b = self.board
        return {"fen": b.fen(), "turn": "white" if b.turn else "black",
                "robot": "both" if self.robot_color is None else ("white" if self.robot_color else "black"),
                "move_number": b.fullmove_number, "check": b.is_check(), "game_over": b.is_game_over(),
                "result": b.result(claim_draw=True) if b.is_game_over() else None, "history": self.history,
                "legal_moves": [m.uci() for m in b.legal_moves][:80]}


# --- robot session -------------------------------------------------------------------------------------------------
@dataclass
class ChessSpec:
    out_dir: Path
    execute: bool = False
    operator_ack: str = ""
    robot_color: str = "black"
    skill: int = 5
    think_s: float = 0.5
    settle_s: float = 0.4
    move_timeout_s: float = 25.0
    camera_names: tuple[str, ...] = CAMERAS  # wrist camera + the fixed camera on the stand
    engine_path: str | None = None
    fen: str | None = None
    auto: bool = True  # the robot watches for the human's move with the wrist camera and replies on its own


class ChessSession:
    """Long-lived: connect once, then `play_robot_move()` per robot turn; the human plays between turns."""

    def __init__(self, spec: ChessSpec, cal: BoardCal, game: Game, stop_event: threading.Event | None = None, log=print, live=None):
        self.spec, self.cal, self.game, self.log, self.live = spec, cal, game, log, live
        self.stop_event = stop_event or threading.Event()
        self.env = cal.envelope()
        self.m = self.tm = None
        self.ctl: Controller | None = None
        self.cams: list = []
        self.session: dict[str, Any] = {}
        self._tasks: list = []
        self._done = asyncio.Event()
        self.watchdog = {"last_ok": time.time(), "fails": 0, "halted": False}
        self.grid = None            # image-space square centres at the look pose (locked at the first observation)
        self.look_pose_used = None  # look pose after centring the camera over the board

    async def connect(self):
        from viam.components.arm import Arm
        from viam.components.camera import Camera
        from viam.components.gripper import Gripper
        from viam.robot.client import RobotClient
        from viam.services.motion import MotionClient

        from amperes.integrations.viam_live import _env
        import os

        address = _env()
        machine = address.split(".")[0]
        if self.spec.execute and self.spec.operator_ack.strip() != required_ack(machine):
            raise SafetyError(f"motion needs the operator acknowledgement exactly: '{required_ack(machine)}'")
        opts = client_options()
        self.m = await dial_robot(address, opts, "control", self.log)
        self.tm = await dial_robot(address, opts, "telemetry", self.log)
        sid = f"chess-{machine}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
        self.session = {"machine": machine, "session_id": sid, "started": time.time(), "execute": self.spec.execute,
                        "config": asdict(TrialConfig(name="chess", settle_s=self.spec.settle_s)), "calibration": asdict(self.cal),
                        "envelope": asdict(self.env), "robot_color": self.spec.robot_color, "object_name": "chess piece",
                        "telemetry": [], "images": [], "trials": [], "events": [], "moves": []}
        arm, motion = Arm.from_robot(self.m, ARM), MotionClient.from_robot(self.m, MOTION)
        # Every move goes through the motion service with the constraint below. (The arm's own move_to_position also plans,
        # with the same claw geometry and no way to pass constraints, so "direct" moves do not help here.)
        direct = False
        self.ctl = Controller(arm, Gripper.from_robot(self.m, GRIPPER), motion, ARM, self.spec.execute, self.env, self.stop_event, self.log,
                              frame="world", direct_vertical=direct)
        self.ctl.open_position = None  # full opening: the 10/10 game used it; a 55 mm opening halved the tolerance and missed pieces
        # move like a careful hand: quick while high, slow in the last 12 cm above the board (flange z, model frame)
        # fast and gentle, like a person: brisk through the air, one smooth slow move for the last 4 cm (30 Oct: the 5 mm
        # stop-start steps were slow and jerky; 60 deg/s tripped the arm's collision detection on a long swing; 45 with short routed moves)
        self.ctl.speed_profile = {"surface_z": self.cal.board_top_z + TOOL_LENGTH_MM, "slow_zone_mm": 60.0, "fast": 75.0, "slow": 10.0}
        self.ctl.slow_step_mm = self.ctl.slow_zone_mm
        self.ctl.open_position = 600.0  # 60 mm: wide enough for any piece, not reaching into the neighbouring squares
        # The gripper module reports a "claws" box that extends 50 mm below the fingertips, so the planner refuses every grasp
        # pose (fingertips 20-40 mm above the board). Allow that one pair: the envelope floor (fingertips >= 5 mm above the
        # board) is the real guard, and the moves are vertical over a flat, empty-of-obstacles board.
        from viam.proto.service.motion import CollisionSpecification, Constraints

        allowed = [CollisionSpecification.AllowedFrameCollisions(frame1=a, frame2=b)
                   for a, b in ((GRIPPER, "table"), (GRIPPER + ":claws", "table:table"), (GRIPPER + ":claws", "table"), (GRIPPER, "table:table"))]
        self.ctl.constraints = Constraints(collision_specification=[CollisionSpecification(allows=allowed)])
        self.ctl.snap = self._snap
        self.ctl.refine = None  # camera grasp centring is OFF: it pulled every pick 25 mm toward the wrong blob (game 3)
        self.game.feasibility = self.crowding
        for cn in self.spec.camera_names:
            try:
                self.cams.append((cn, Camera.from_robot(self.m, cn)))
            except Exception:
                pass
        self._t_arm = Arm.from_robot(self.tm, ARM)
        self._address, self._opts = address, opts
        self._tasks.append(asyncio.create_task(self._telemetry()))
        (self.spec.out_dir / "images").mkdir(parents=True, exist_ok=True)
        return self

    async def _telemetry(self):
        from viam.components.arm import Arm

        w = self.watchdog
        while not self._done.is_set():
            if self.stop_event.is_set() and not w["halted"]:
                w["halted"] = True
                await self.ctl.stop()
            t0 = time.time()
            try:
                jp, p = await timed(asyncio.gather(self._t_arm.get_joint_positions(), self._t_arm.get_end_position()), 2.5)
                self.session["telemetry"].append({"t": round(t0, 3), "joints": list(jp.values), "pose": [getattr(p, k) for k in POSE_KEYS]})
                w["last_ok"], w["fails"] = time.time(), 0
            except Exception:
                w["fails"] += 1
                self.session["telemetry"].append({"t": round(t0, 3), "joints": None, "pose": None})
                if w["fails"] % 3 == 0:
                    try:
                        new = await dial_robot(self._address, self._opts, "telemetry re-dial", self.log, attempts=1)
                        old, self.tm = self.tm, new
                        self._t_arm = Arm.from_robot(self.tm, ARM)
                        asyncio.create_task(old.close())
                    except Exception:
                        pass
            moving = self.session.get("moving", False)
            if self.spec.execute and moving and time.time() - w["last_ok"] > WATCHDOG_S and not w["halted"]:
                self.session["events"].append({"timestamp": time.time(), "event_type": "fault", "event_class": "infrastructure_failure",
                                               "code": "WATCHDOG_TELEMETRY_LOST", "message": f"no telemetry for {WATCHDOG_S:g} s during motion; stopping"})
                w["halted"] = True
                await self.ctl.stop()
            await asyncio.sleep(max(0.0, 0.33 - (time.time() - t0)))

    async def _snap(self, tag: str, trial: int, depth: bool = False) -> list[str]:
        files: list[str] = []
        try:
            cur = await self.ctl.pose()
            if cur["o_z"] > -0.7:
                return files
        except Exception:
            return files
        for cn, cam in self.cams:
            try:
                r = await timed(cam.get_images(), 8)
                imgs = r[0] if isinstance(r, tuple) else r
                for im in imgs:
                    is_depth = "dep" in str(im.mime_type)
                    if is_depth and not depth:
                        continue
                    ext = "dep" if is_depth else "jpg" if "jpeg" in str(im.mime_type) else "png"
                    fn = f"{self.session['session_id']}-t{trial:03d}_{cn}_{tag}{'_depth' if is_depth else ''}.{ext}"
                    (self.spec.out_dir / "images" / fn).write_bytes(im.data)
                    self.session["images"].append({"t": time.time(), "camera": cn, "tag": tag, "trial": trial, "mime": str(im.mime_type), "file": fn})
                    files.append(fn)
            except Exception:
                self.session["events"].append({"timestamp": time.time(), "event_type": "recording_error", "event_class": "data_recording_failure",
                                               "code": "CAMERA_NO_FRAME", "message": f"{cn} returned no frame ({tag}, trial {trial})"})
        return files

    # gripper body seen from above: 110 mm along the finger (closing) axis, 75 mm across; its underside sits ~50 mm above
    # the fingertips, so neighbours taller than that are hit if the body overhangs them
    BODY_HALF_LONG, BODY_HALF_SHORT, BODY_CLEAR_MM = 55.0, 37.5, 50.0

    def wrist_angle_for(self, sq: str, board=None) -> float:
        """Pick the wrist angle (board diagonal by default) whose body footprint overhangs no tall neighbouring piece."""
        board = board if board is not None else self.game.board
        try:
            centre = chess.parse_square(sq)
        except ValueError:
            return self.cal.theta_deg
        cx, cy = self.cal.square_xy(sq)
        tall = []
        for d in (-9, -8, -7, -1, 1, 7, 8, 9):
            n = centre + d
            if not 0 <= n < 64 or abs(chess.square_file(n) - chess.square_file(centre)) > 1:
                continue
            piece = board.piece_at(n)
            if piece and self.cal.piece_height(piece.symbol()) * 0.45 + 15 > self.BODY_CLEAR_MM:  # body at grasp height meets its waist
                tall.append((chess.square_name(n), *self.cal.square_xy(chess.square_name(n))))
        if not tall:
            return self.cal.theta_deg
        board_angle = math.radians(self.cal.theta_deg - 45.0)
        best = None
        for off in (45.0, -45.0, 0.0, 90.0):  # diagonals first (they clear orthogonal neighbours), then along files/ranks
            a = board_angle + math.radians(off)
            ux, uy = math.cos(a), math.sin(a)      # long axis
            vx, vy = -uy, ux                        # short axis
            worst = 0.0
            for _, nx, ny in tall:
                dx, dy = nx - cx, ny - cy
                along, across = abs(dx * ux + dy * uy), abs(dx * vx + dy * vy)
                # overlap of the body rectangle (grown by the piece radius 17 mm) with the neighbour's centre
                pen = min(self.BODY_HALF_LONG + 17 - along, self.BODY_HALF_SHORT + 17 - across)
                worst = max(worst, pen)
            if best is None or worst < best[0]:
                best = (worst, off)
            if worst <= 0:
                break
        if best[0] > 0:
            self.log(f"{sq}: every wrist angle overhangs a tall neighbour ({[t[0] for t in tall]}); using the least bad")
        return self.cal.theta_deg - 45.0 + best[1]

    def tallest_neighbour(self, sq: str, board=None) -> float:
        board = board if board is not None else self.game.board
        try:
            c = chess.parse_square(sq)
        except ValueError:
            return 0.0
        tallest = 0.0
        for d in (-9, -8, -7, -1, 1, 7, 8, 9):
            n = c + d
            if 0 <= n < 64 and abs(chess.square_file(n) - chess.square_file(c)) <= 1:
                p = board.piece_at(n)
                if p:
                    tallest = max(tallest, self.cal.piece_height(p.symbol()))
        return tallest

    def _pose_for(self, op: PieceOp, which: str, board=None) -> dict[str, float]:
        sq = op.src if which == "src" else op.dst
        world = getattr(self.game, "world", None)
        if world is not None:
            # the tracked physical layout includes the pieces beside the board, so slot neighbours count too
            from amperes.integrations import chess_lifecycle as L

            tallest = max(L.tallest_near(self.cal, world.physical, op.src), L.tallest_near(self.cal, world.physical, op.dst, skip=(op.src,)))
        else:
            # the piece is held at one height for pick and place: clear the tallest neighbour of either square
            tallest = max(self.tallest_neighbour(op.src, board), self.tallest_neighbour(op.dst, board) if not op.dst.startswith("grave") else 0.0)
        if not (len(sq) == 2 and sq[0] in "abcdefgh" and sq[1] in "12345678"):
            pose = self.cal.offboard_pose(op.symbol, sq)
            return {**pose, "z": self.cal.grasp_z(op.symbol, tallest)}
        return {**self.cal.grasp_pose(sq, op.symbol, tallest), "theta": self.wrist_angle_for(sq, board)}

    async def _refine(self, src: dict[str, float]):
        """At the approach pose: find the piece under the gripper in the wrist image and return the xy shift to centre on it."""
        from amperes.integrations import chess_vision as CV

        op = getattr(self, "_current_op", None)
        if op is None or not self.cams:
            return None
        try:
            cap = await CV.capture(self.m, self.spec.camera_names[0])
            cap = CV.pin_camera(cap) if False else cap
            colour = "white" if op.symbol.isupper() else "black"
            z_top = self.cal.board_top_z + self.cal.piece_height(op.symbol) - (TOOL_LENGTH_MM - 150.0)  # model board + piece height
            r = CV.refine_grasp_xy(cap, (src["x"], src["y"]), z_top + 50.0, colour)  # +50: real fingertips vs model (tool 200)
            self.session["events"].append({"timestamp": time.time(), "event_type": "annotation", "event_class": "grasp_refinement",
                                           "code": "GRASP_REFINED" if r.get("ok") else "GRASP_REFINE_FAILED",
                                           "message": f"{op.src}: " + (f"shift {r['shift_mm']:.0f} mm" if r.get("ok") else r.get("reason", ""))})
            if r.get("ok"):
                self.log(f"camera centred the grasp on {op.src}: shift {r['shift_mm']:.0f} mm (raw {r['raw_shift_mm']:.0f})")
                return (r["dx"], r["dy"])
            self.log(f"grasp refinement on {op.src}: {r.get('reason')}")
        except Exception as exc:
            self.log(f"grasp refinement failed: {type(exc).__name__}: {str(exc)[:60]}")
        return None

    def crowding(self, mv: chess.Move) -> int:
        """How many tall pieces (bishop or taller) stand orthogonally next to the squares the gripper must descend on.
        With the physical layout tracked, the planner's clearance model scores the move's operations instead."""
        world = getattr(self.game, "world", None)
        if world is not None:
            from amperes.integrations import chess_lifecycle as L

            return L.move_clearance(world, self.game.board, mv)
        board = self.game.board
        squares = [mv.from_square, mv.to_square]
        if board.is_capture(mv):
            squares.append(mv.to_square)
        if board.is_castling(mv):
            squares += [chess.parse_square("h1" if mv.to_square == chess.G1 else "a1" if mv.to_square == chess.C1 else "h8" if mv.to_square == chess.G8 else "a8")]
        score = 0
        for sq in squares:
            for d in (-8, -1, 1, 8):
                n = sq + d
                if 0 <= n < 64 and abs(chess.square_file(n) - chess.square_file(sq)) <= 1 and n != mv.from_square:
                    p = board.piece_at(n)
                    if p and self.cal.piece_height(p.symbol()) >= 65:
                        score += 2
                    elif p:
                        score += 0  # pawns, knights, rooks clear the body; the 55 mm open fingers clear them too
        return score

    async def go_home(self) -> None:
        """Rise to the look pose from wherever the arm stopped, turning the wrist at most ~90 degrees per move (after an
        aborted move it can be far round; the envelope refuses a single turn over 100 degrees)."""
        cur = await self.ctl.pose()
        look = self.look_pose_used or self.cal.look_pose()
        d = (look["theta"] - cur["theta"] + 180.0) % 360.0 - 180.0  # shortest signed turn
        self.session["moving"] = True
        try:
            if abs(d) > 90.0:  # turn half-way over the board centre (raising in place at a far corner can be out of reach)
                await self.ctl._move({**look, "z": look["z"] - 60.0, "theta": cur["theta"] + d / 2.0}, "unwind", self.spec.move_timeout_s)
            await self.ctl._move(look, "home", self.spec.move_timeout_s)
        finally:
            self.session["moving"] = False

    async def look(self, tag: str, trial: int) -> list[str]:
        """Overhead photo of the board from the (camera-centred) look pose, labelled by the FEN in the move record."""
        self.session["moving"] = True
        try:
            await self.ctl._move(self.look_pose_used or self.cal.look_pose(), "look", self.spec.move_timeout_s)
        finally:
            self.session["moving"] = False
        if not self.spec.execute:
            return []
        if self.look_pose_used is None and self.m is not None:
            await self.centre_camera()
        await asyncio.sleep(0.3)
        return await self._snap(tag, trial, True)

    async def play_robot_move(self, _retry: bool = False) -> dict[str, Any]:
        """Engine chooses; the arm executes each piece op as a supervised pick-and-place trial; the camera verifies."""
        mv, ops = self.game.robot_move() if not _retry else (self._pending_move, self._pending_ops)
        if not _retry:
            self._all_ops = list(ops)
        self._pending_move, self._pending_ops = mv, ops
        fen_before = self.game.board.fen()
        self.log(f"robot plays {self.game.board.san(mv)} ({mv.uci()}): " + "; ".join(f"{o.kind} {o.symbol} {o.src}->{o.dst}" for o in ops))
        rec: dict[str, Any] = {"uci": mv.uci(), "san": self.game.board.san(mv), "fen_before": fen_before, "ops": [asdict(o) for o in ops],
                               "t": time.time(), "trials": [], "images_before": [], "images_after": []}
        recent_watch = time.time() - (self.session.get("last_watch") or {}).get("t", 0) < 180
        prev = [m for m in self.session["moves"] if m.get("by") != "human"]
        recent_after = bool(prev) and time.time() - prev[-1].get("t", 0) < 240 and bool(prev[-1].get("images_after"))
        rec["images_before"] = [] if ((recent_watch or recent_after) and self.look_pose_used) else await self.look("board_before", len(self.session["trials"]))
        results = []
        for op in ops:
            i = len(self.session["trials"])
            self._current_op = op
            src, dst = self._pose_for(op, "src"), self._pose_for(op, "dst")
            cfg = TrialConfig(name=f"chess_{op.kind}", approach_mm=self.cal.transport_z - src["z"], grasp_z_offset_mm=0.0,
                              settle_s=self.spec.settle_s, move_timeout_s=self.spec.move_timeout_s, trials=1)
            base, dst_base = dict(src), dict(dst)
            floor = self.cal.board_top_z + TOOL_LENGTH_MM + self.cal.floor_clearance_mm + 1.0
            # a missed or slipped grip is retried at once: nudged a few mm, and lower down the piece (a tall piece held
            # high beside the king slipped out on 30 Oct)
            for k, (dx, dy, dz) in enumerate([(0, 0, 0), (0, 0, -8), (5, 0, -8), (-5, 0, -8), (0, 5, -8), (0, -5, -8)]):
                src = {**base, "x": base["x"] + dx, "y": base["y"] + dy, "z": max(floor, base["z"] + dz)}
                # put the piece down at the same height it is held at (+3 mm: let it settle, never press it into the board)
                dst = {**dst_base, "z": dst_base["z"] + (src["z"] - base["z"]) + 3.0}
                cfg = TrialConfig(name=f"chess_{op.kind}", approach_mm=self.cal.transport_z - src["z"], grasp_z_offset_mm=0.0,
                                  settle_s=self.spec.settle_s, move_timeout_s=self.spec.move_timeout_s, trials=1)
                self.session["moving"] = True
                try:
                    r = await self.ctl.trial(i, src, dst, cfg, (op.src, op.dst))
                finally:
                    self.session["moving"] = False
                if r.category not in ("GRASP_FAILURE", "SLIPPAGE") or self.stop_event.is_set():
                    break
                self.log(f"  {op.symbol} {op.src}: {r.category}, retrying ({k + 2}/6)")
            d = {**asdict(r), "config": asdict(cfg), "chess": {"uci": mv.uci(), "san": rec["san"], "fen_before": fen_before, **asdict(op),
                                                             "piece": ("white " if op.symbol.isupper() else "black ") + PIECE_NAME[op.symbol.lower()]}}
            self.session["trials"].append(d)
            results.append(d)
            rec["trials"].append(i)
            self.log(f"  {op.kind} {op.symbol} {op.src}->{op.dst}: {r.outcome} {r.category or ''} grab={r.grab_reported} holding={r.holding_after_lift} {r.error or ''}")
            if r.category in ("SAFETY_STOP", "INFRASTRUCTURE_ERROR", "TIMEOUT"):
                break
        rec["images_after"] = await self.look("board_after", len(self.session["trials"]) - 1) if not self.stop_event.is_set() else []
        ok = all(r["outcome"] == "success" for r in results) and len(results) == len(ops)
        # Camera verification: does the board now show the move? The gripper's holding sensor is not trustworthy here.
        if self.spec.execute and self.m is not None and not self.stop_event.is_set():
            try:
                from amperes.integrations import chess_vision as CV

                obs = await self.observe(save_debug=True)
                self.game.board.push(mv)
                after = CV.mismatches(obs, CV.expected_state(self.game.board))
                self.game.board.pop()
                before = CV.mismatches(obs, CV.expected_state(self.game.board))
                src, dst = chess.square_name(mv.from_square), chess.square_name(mv.to_square)
                moved = src not in after and dst not in after
                stayed = src not in before and dst not in before
                rec["vision"] = {"after_mismatches": after, "before_mismatches": before, "moved": moved, "stayed": stayed}
                self.log(f"camera: move {'confirmed' if moved else 'NOT seen' if stayed else 'unclear'} (after {len(after)} vs before {len(before)} mismatches)")
                if moved:
                    ok = True
                    for r in results:
                        if r["outcome"] != "success":
                            r["outcome"], r["category"], r["error"] = "success", None, None
                        r["vision_verified"] = True
                elif stayed and not all(r.get("grab_reported") and r.get("holding_after_lift") for r in results):
                    ok = False
                    for r in results:
                        r["outcome"], r["category"], r["vision_verified"] = "failure", r.get("category") or "PIECE_NOT_MOVED", True
                elif stayed:
                    for r in results:  # gripper confident, camera disagrees: keep the gripper's verdict, flag for human review
                        r["vision_verified"], r["needs_review"] = False, "camera did not see the piece on the destination square"
            except Exception as exc:
                self.log(f"camera verification failed: {type(exc).__name__}: {str(exc)[:80]}")
        rec["executed_ok"] = ok if self.spec.execute else None
        gripper_ok = all(r.get("grab_reported") and r.get("holding_after_lift") for r in results) and len(results) == len(ops)
        # retry only when the gripper AND the camera agree the piece did not move; a camera "stayed" against a confident
        # gripper is recorded as unverified rather than acted on (a second grab on an empty square knocks pieces)
        failed = [op for op, r in zip(ops, results) if r["outcome"] != "success"] + ops[len(results):]
        moved = rec.get("vision", {}).get("moved")
        if self.spec.execute and failed and "vision" in rec and not moved and not self.stop_event.is_set():
            # the piece did not move: never record a move that did not happen; redo the operations that failed
            self._move_retries = getattr(self, "_move_retries", 0) + 1
            self.session["moves"].append({**rec, "retry": True})
            if self._move_retries > 3:
                self._move_retries = 0
                raise RuntimeError(f"could not make {rec['san']} after 3 rounds of retries: please help the piece on "
                                   f"{failed[0].src} (the game has not moved on)")
            self.log(f"{rec['san']} did not happen ({failed[0].symbol} on {failed[0].src}): trying again ({self._move_retries}/3)")
            done_ops = [op for op in ops if op not in failed]
            for op in done_ops:  # already done (e.g. the capture): keep the tracked layout in step
                if self.game.world is not None:
                    try:
                        self.game.world.apply(op)
                    except ValueError:
                        pass
            self._pending_ops = failed
            return await self.play_robot_move(_retry=True)
        self._move_retries = 0
        return self._commit(mv, ops if not _retry else self._all_ops, results, rec)

    def _commit(self, mv, ops, results, rec):
        self.game.commit_robot_move(mv, ops, results)  # the game state follows the engine; an unfixed failure is corrected by hand
        rec["fen_after"] = self.game.board.fen()
        self.session["moves"].append(rec)
        if getattr(self, "voice", False):
            from amperes.integrations import chess_voice as V

            V.announce("move", rec["san"], rec["fen_after"])  # called once the move is made, never for a move that failed
        if self.live:
            self.live.update(**self.game.status(), last_move=rec, trials=len(self.session["trials"]))
        return rec

    async def centre_camera(self) -> dict[str, float]:
        """At the look pose: find the printed grid in the image and anchor its labels on the pieces. Locks the grid for the game."""
        from amperes.integrations import chess_vision as CV

        pose = dict(self.look_pose_used or self.cal.look_pose())
        grid, candidates = None, []
        for attempt in range(6):  # the corner detector sometimes locks onto a false small pattern: take the best of several frames
            cap = await CV.capture(self.m, self.spec.camera_names[0])
            capp = CV.pin_camera(cap)
            approx = {sq: CV.pixel_of([*self.cal.square_xy(sq), CV.VISION_BOARD_Z], capp["K"], capp["R"], capp["t"]) for sq in ("a1", "h1", "a8")}
            g = CV.grid_from_image(cap["colour"], approx)
            if g.get("method") == "corners":
                g = CV.anchor_by_extent(g, cap["colour"], approx["a1"]) if getattr(self, "position_unknown", False) else CV.anchor_grid(g, cap["colour"], cap["depth"], self.game.board)
            am, vis = g.get("anchor_mismatches"), g.get("anchor_visible") or 0
            if getattr(self, "position_unknown", False) and g.get("anchor_method") == "extent":
                am, vis = 0, g.get("anchor_visible") or 0
            geometry_ok = 45 <= g.get("square_px", 0) <= 70 and g.get("fit_rms_px", 99) < 2.0
            # the human may already have moved (2 squares differ) and a square or two misread: allow up to 8 mismatches
            plausible = geometry_ok and am is not None and am <= 8 and vis >= 56
            if geometry_ok and not plausible:
                candidates.append(((99, -vis, g.get("fit_rms_px", 9)), g))  # geometry right, labels doubtful: last resort
            self.log(f"grid attempt {attempt + 1}: {g.get('method')} {g.get('square_px', 0):.0f} px, rms {g.get('fit_rms_px', 0):.1f}, "
                     f"anchor mismatches {am}, visible {vis}{'' if plausible else ' -> rejected'}")
            if plausible:
                candidates.append(((am, -vis, g.get("fit_rms_px", 9)), g))
                if am <= 1 and vis == 64:
                    break
            await asyncio.sleep(0.4)
        if candidates:
            grid = min(candidates, key=lambda c: c[0])[1]
        if grid is None:
            saved = json.loads(calibration_path().read_text()).get("last_grid")
            if saved and saved.get("look_pose") == {k: round(pose[k], 1) for k in ("x", "y", "z", "theta")}:
                grid = {**saved["grid"], "centres": {k: tuple(v) for k, v in saved["grid"]["centres"].items()}, "method": "saved"}
                self.log("using the last good grid saved for this look pose")
            else:
                grid = g
                self.log("WARNING: no plausible grid lock; move detection may be unreliable")
        else:
            def plain(o):  # numpy scalars/arrays -> JSON-native
                if hasattr(o, "tolist"):
                    return o.tolist()
                if isinstance(o, dict):
                    return {k: plain(v) for k, v in o.items()}
                if isinstance(o, (list, tuple)):
                    return [plain(v) for v in o]
                return o
            try:
                BoardCal.save_settings(last_grid={"look_pose": {k: round(pose[k], 1) for k in ("x", "y", "z", "theta")},
                                                  "grid": plain({k: v for k, v in grid.items() if k != "debug"})})
            except Exception as exc:
                self.log(f"could not save the grid: {type(exc).__name__}")
        self.grid = grid
        c = grid["centres"]
        bu, bv = np.mean([c[sq][0] for sq in c]), np.mean([c[sq][1] for sq in c])
        # One nudge so the board centre sits on the image centre (all 64 squares in view, anchor unambiguous). Image u runs
        # along the rank axis and v along the file axis here (or swapped when files_along_u); pixels grow with the index.
        if grid.get("method") == "corners" and grid.get("square_px") and not getattr(self, "_nudged", False):
            self._nudged = True
            mm_px = self.cal.square_mm / grid["square_px"]
            du, dv = (bu - 320.0) * mm_px, (bv - 240.0) * mm_px
            fa = np.array(self.cal.file_axis) / self.cal.square_mm
            ra = np.array(self.cal.rank_axis) / self.cal.square_mm
            u_axis, v_axis = (fa, ra) if grid.get("files_along_u") else (ra, fa)
            shift = u_axis * du + v_axis * dv
            if 15.0 < float(np.hypot(*shift)) < 90.0:
                pose = {**pose, "x": pose["x"] + float(shift[0]), "y": pose["y"] + float(shift[1])}
                self.log(f"nudging the camera {np.hypot(*shift):.0f} mm to centre the board (off by {du:.0f}, {dv:.0f} mm)")
                self.session["moving"] = True
                try:
                    await self.ctl._move(pose, "look_centre", self.spec.move_timeout_s)
                finally:
                    self.session["moving"] = False
                await asyncio.sleep(0.5)
                self.look_pose_used = pose
                previous = grid
                relocked = await self.centre_camera()  # re-lock the grid from the centred view
                if self.grid.get("fit_rms_px", 99) > 2.0 or not (45 <= self.grid.get("square_px", 0) <= 70):
                    self.grid = previous  # the re-lock failed: the pre-nudge grid is only ~1 square-width off, keep it
                    self.log("re-lock after the nudge failed; keeping the previous grid")
                return relocked
        self.log(f"grid {grid.get('method')} ({grid.get('corners', 0)} corners, rms {grid.get('fit_rms_px', 0):.1f} px, {grid.get('square_px', 0):.0f} px squares, "
                 f"anchor mismatches {grid.get('anchor_mismatches')}, visible {grid.get('anchor_visible')}); board centre at pixel ({bu:.0f}, {bv:.0f})")
        self.look_pose_used = pose
        self.session["look_pose"] = pose
        self.session["grid"] = {k: v for k, v in self.grid.items() if k not in ("centres", "debug")} | {"centres": {k: [round(a, 1) for a in v] for k, v in self.grid["centres"].items()}}
        return pose

    async def observe(self, save_debug: bool = False) -> dict[str, Any]:
        """One wrist-camera observation from the look pose: per-square occupancy/colour from the printed grid + depth (no motion)."""
        from amperes.integrations import chess_vision as CV

        if self.grid is None:
            await self.centre_camera()
        cap = await CV.capture(self.m, self.spec.camera_names[0])
        state = CV.state_from_image(cap["colour"], cap["depth"], self.grid["centres"], self.grid["square_px"])
        if save_debug:
            CV.draw_state(cap["colour"], self.grid["centres"], state, self.spec.out_dir / "images" / "watch_debug.jpg")
        return state

    async def watch_human_move(self, timeout_s: float = 600.0, poll_s: float = 0.7, log=None) -> dict[str, Any] | None:
        """Wait at the look pose until the camera sees a position that is exactly one legal move away, twice in a row."""
        from amperes.integrations import chess_vision as CV

        log = log or self.log
        if self.game.robot_color is None or self.game.board.turn == self.game.robot_color:
            raise ValueError("it is the robot's turn")
        t0, last, stable = time.time(), None, 0
        start_ply = len(self.game.board.move_stack)  # a move typed into the console while watching ends the watch
        await self.look("watch_start", len(self.session["trials"]))
        baseline: set[str] | None = None  # squares the camera already misreads before the human moves: ignore them
        while time.time() - t0 < timeout_s and not self.stop_event.is_set():
            if len(self.game.board.move_stack) != start_ply:
                break
            try:
                obs = await self.observe(save_debug=True)
            except Exception as exc:
                log(f"observe failed: {type(exc).__name__}: {str(exc)[:80]}")
                await asyncio.sleep(poll_s)
                continue
            if len(self.game.board.move_stack) != start_ply:
                break
            if baseline is None:
                baseline = set(CV.mismatches(obs, CV.expected_state(self.game.board)))
                flicker: dict[str, int] = {}
                if baseline:
                    log(f"ignoring squares the camera misreads at rest: {sorted(baseline)}")
            m = CV.match_human_move(self.game.board, obs, ignore=baseline)
            # squares that keep misreading while no move is seen join the ignore set (lighting flicker on a few squares)
            for sq in m.get("raw_squares", []):
                flicker[sq] = flicker.get(sq, 0) + 1
                if flicker[sq] >= 3 and sq not in baseline and m["best_mismatches"] > 0:
                    baseline.add(sq)
                    log(f"also ignoring {sq} (misread {flicker[sq]} polls in a row)")
            self.session["last_watch"] = {**m, "t": time.time()}
            # a move is "seen" when exactly one legal move explains the picture better than both the unchanged position and
            # every other move (a square the camera persistently misreads then cancels out)
            # seen: one legal move explains at least two squares of difference (its from- and to-square), leaves at most one
            # unexplained square outside the known-noisy ones, and beats every other move
            if (m["best"] is not None and (m.get("explained") or 0) >= 2 and m["best_mismatches"] <= 2
                    and (m["second_mismatches"] is None or m["best_mismatches"] < m["second_mismatches"])):
                stable = stable + 1 if last == m["best"] else 1
                last = m["best"]
                if stable >= 2:
                    if len(self.game.board.move_stack) != start_ply:
                        break
                    mv = self.game.human_move(m["best"])
                    log(f"saw your move: {mv.uci()} ({m['visible_squares']} squares visible)")
                    self.session["moves"].append({"by": "human", "uci": mv.uci(), "t": time.time(), "detected": m, "fen_after": self.game.board.fen()})
                    return {"uci": mv.uci(), **m}
            else:
                stable, last = 0, None
            await asyncio.sleep(poll_s)
        if len(self.game.board.move_stack) != start_ply:
            log("a move was entered in the console; stopped watching")
        return None

    async def close(self):
        self._done.set()
        await asyncio.sleep(0)
        for t in self._tasks:
            t.cancel()
        if self.ctl is not None:
            self.session["events"] += self.ctl.events
            self.session["actions"] = self.ctl.actions
        self.session["ended"] = time.time()
        for c in (self.m, self.tm):
            if c is not None:
                try:
                    await c.close()
                except Exception:
                    pass


def chess_session_to_canonical(session: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """Each piece op → one canonical episode (task chess_move_real) with the move, piece and FEN as conditions."""
    from amperes.schema import Episode, Event, Outcome, Ref, RobotInfo, SessionInfo, SourceInfo, Stream, episode_to_json

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "images").mkdir(exist_ok=True)
    sid = session["session_id"]
    eps = []
    for tr in session["trials"]:
        cfg, ch = tr["config"], tr["chess"]
        s, e = tr["started"], tr["ended"] or tr["started"]
        tel = [x for x in session["telemetry"] if s <= x["t"] <= e]
        moves = [a for a in tr["steps"] if a["kind"] == "move"]
        grips = [a for a in tr["steps"] if a["kind"] == "gripper"]
        eid = f"{sid}-t{tr['index']:03d}"
        streams = {
            "obs.joint_pos": Stream(kind="observation", t=[x["t"] for x in tel], values=[x["joints"] or [None] * 6 for x in tel],
                                    units="deg", expected_rate_hz=3.0, valid_range=[-360.0, 360.0], source="viam:arm.get_joint_positions"),
            "obs.ee_pose": Stream(kind="observation", t=[x["t"] for x in tel], values=[x["pose"] or [None] * 7 for x in tel],
                                  units="mm, orientation-vector, deg", expected_rate_hz=3.0, source="viam:arm.get_end_position"),
            "act.target_pose": Stream(kind="action", t=[a["t"] for a in moves], values=[[a["target"][k] for k in POSE_KEYS] for a in moves],
                                      units="mm, orientation-vector, deg", source="amperes chess pick-place"),
            "act.gripper": Stream(kind="action", t=[a["t"] for a in grips], values=[[1.0 if a["label"] == "grab" else 0.0] for a in grips],
                                  valid_range=[0.0, 1.0], source="amperes chess pick-place (1=grab, 0=open)"),
        }
        crit = {k: v for k, v in (("grab_reported", tr["grab_reported"]), ("holding_after_lift", tr["holding_after_lift"])) if v is not None}
        events = [Event(t=a["t"], type="fault", code="SAFETY_REFUSAL", message="; ".join(a["refused"]), source="amperes safety")
                  for a in tr["steps"] if a.get("refused")]
        events += [Event(t=x["timestamp"], type=x["event_type"] if x["event_type"] in ("fault", "recording_error") else "annotation",
                         code=x["code"], message=x["message"], source="amperes robot session")
                   for x in session["events"] if s <= x["timestamp"] <= e and x["code"] != "SAFETY_REFUSAL"]
        if tr["error"] and tr["category"] == "INFRASTRUCTURE_ERROR":
            events.append(Event(t=e, type="error", code="SDK_ERROR", message=tr["error"], source="amperes robot session"))
        events += [Event(t=im["t"], type="annotation", code="CAMERA_SNAPSHOT", message=im["file"], source=im["camera"])
                   for im in session["images"] if im["trial"] == tr["index"]]
        dst = ch["dst"] if not ch["dst"].startswith("grave") else "the captured-pieces area"
        ep = Episode(
            episode_id=eid,
            source=SourceInfo(dataset_id="robot", adapter="amperes.chess_robot@1.0.0", source_uri=f"viam://{session['machine']}",
                              source_record_id=eid, synthetic=False, synthetic_note=None if session["execute"] else "DRY RUN: no motion was executed"),
            task=Ref(id="chess_move_real", version="1.0.0"), policy=Ref(id="stockfish_plus_scripted_pick_place", version="1.0.0"),
            robot=RobotInfo(embodiment="6-joint arm + parallel gripper + wrist RGB-D camera (event-provided)", device_id="arm", config_id="chess"),
            session=SessionInfo(session_id=sid, workcell_id=session["machine"], operator_id="op-onsite"),
            start_time=s, end_time=e, time_base="unix_s", streams=streams,
            outcome=Outcome(label=tr["outcome"], provenance="amperes_verifier" if session["execute"] else "unknown",
                            verified=bool(session["execute"] and crit), reason=tr["category"] or tr["error"],
                            verifier_id="gripper_signals", verifier_version="1.0.0",
                            measurements={"gripper_grab_reported": tr["grab_reported"], "gripper_holding_after_lift": tr["holding_after_lift"],
                                          **({"criteria": crit} if crit else {})}),
            events=events,
            conditions={"trial": tr["index"], "source": ch["src"], "dest": ch["dst"], "attempt_index": 1, "config": cfg, "dry_run": not session["execute"],
                        "object": ch["piece"], "move_uci": ch["uci"], "move_san": ch["san"], "op": ch["kind"], "fen_before": ch["fen_before"],
                        "robot_color": session["robot_color"], "board": {k: session["calibration"][k] for k in ("king_height_mm", "grip_fraction")},
                        "instruction": f"Move the {ch['piece']} from {ch['src']} to {dst}."},
            extra={"error": tr["error"], "move_durations_s": [a.get("duration_s") for a in moves], "note": ch.get("note", "")},
        )
        eps.append(ep)
    with open(out_dir / "episodes.jsonl", "w") as fh:
        for ep in eps:
            fh.write(episode_to_json(ep) + "\n")
    evs = [{"timestamp": session["started"], "event_type": "session_start", "workcell_id": session["machine"], "session_id": sid},
           {"timestamp": session.get("ended", time.time()), "event_type": "session_end", "workcell_id": session["machine"], "session_id": sid}]
    evs += [{**{k: x[k] for k in ("event_type", "event_class", "code", "message")}, "timestamp": x["timestamp"],
             "workcell_id": session["machine"], "session_id": sid, "device_id": "arm"}
            for x in session["events"] if x["event_type"] in ("fault", "recording_error")]
    with open(out_dir / "events.jsonl", "w") as fh:
        for x in evs:
            fh.write(json.dumps(x) + "\n")
    with open(out_dir / "moves.jsonl", "w") as fh:  # overhead board photos labelled with the position before/after each robot move
        for mv in session["moves"]:
            fh.write(json.dumps(mv) + "\n")
    summary = {"session_id": sid, "task": "chess", "executed": session["execute"], "robot_color": session["robot_color"],
               "robot_moves": len(session["moves"]), "piece_ops": len(session["trials"]),
               "outcomes": {o: sum(t["outcome"] == o for t in session["trials"]) for o in {t["outcome"] for t in session["trials"]}},
               "board_photos": sum(len(m.get("images_before") or []) + len(m.get("images_after") or []) for m in session["moves"]),
               "images": len(session["images"]), "events": len(session["events"]), "out_dir": str(out_dir)}
    (out_dir / "session_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def import_chess_session(session: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    from amperes import incidents
    from amperes.ingest.pipeline import import_dataset

    summary = chess_session_to_canonical(session, out_dir)
    if session["trials"]:
        r = import_dataset(out_dir, name=f"{'ROBOT' if session['execute'] else 'DRY-RUN'} chess {summary['session_id']}", adapter="jsonl",
                           synthetic=not session["execute"], note="real arm chess moves vs a human (Stockfish chooses)" if session["execute"]
                           else "dry run: plan validated, no motion", ctx={"required_metadata": ["task.id"]})
        summary.update(dataset_id=r["dataset_id"], incidents=len(incidents.build_incidents(r["dataset_id"])), quality=r["quality"]["status_counts"])
    (out_dir / "session_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return summary


async def teach(name: str) -> dict[str, Any]:
    """READ-ONLY: record the arm's current world pose as chess point `name` (a1, h1 or graveyard)."""
    from viam.robot.client import RobotClient
    from viam.services.motion import MotionClient

    from amperes.integrations.viam_live import _env
    import os

    address = _env()
    opts = client_options()
    m = await dial_robot(address, opts, "teach")
    try:
        pif = await MotionClient.from_robot(m, MOTION).get_pose(component_name=ARM, destination_frame="world")
        pose = {k: float(getattr(pif.pose, k)) for k in POSE_KEYS}
        saved = BoardCal.save_point(name, pose, {"_frame": "world"})
        return {"name": name, **{k: saved[k] for k in POSE_KEYS}}
    finally:
        await m.close()
