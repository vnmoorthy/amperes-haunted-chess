"""HTTP layer for the workspace scan and the chess game (mounted by amperes.api). Thin: state lives in the integrations."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from amperes import store

router = APIRouter()
SCAN: dict[str, Any] = {"status": None, "stop": None, "thread": None}
CHESS: dict[str, Any] = {"runner": None}


def _machine() -> str:
    try:
        from amperes.integrations.viam_live import _env

        return _env().split(".")[0]
    except Exception:
        return "sfhack1-main"


@router.get("/chess", response_class=HTMLResponse)
def chess_page():
    return (Path(__file__).parent / "ui" / "chess.html").read_text()


# ---------------------------------------------------------------------------------------------------- workspace scan
class ScanReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sweeps: int = Field(1, ge=1, le=30)
    x_mm: tuple[float, float] = (250.0, 450.0)
    y_mm: tuple[float, float] = (-200.0, 200.0)
    heights_mm: list[float] = Field(default=[320.0, 420.0], min_length=1, max_length=4)
    nx: int = Field(3, ge=1, le=6)
    ny: int = Field(3, ge=1, le=6)
    settle_s: float = Field(0.6, ge=0, le=3)
    video_hz: float = Field(2.0, ge=0, le=5)
    max_minutes: float = Field(25.0, ge=1, le=120)
    execute: bool = False
    ack: str = ""


@router.get("/api/robot/scan")
def scan_status():
    from amperes.integrations import viam_tasks as VT

    cfg = VT.ScanConfig()
    return {"default": {"box": VT.describe_box(cfg), "poses": len(VT.generate_poses(cfg)), "config": cfg.__dict__},
            "run": SCAN["status"].snapshot() if SCAN["status"] else {"running": False}}


@router.post("/api/robot/scan")
def scan_start(req: ScanReq):
    from amperes.integrations import viam_tasks as VT
    from amperes.integrations.viam_live import LiveStatus

    if SCAN["thread"] is not None and SCAN["thread"].is_alive():
        raise HTTPException(409, "a scan is already running")
    cfg = VT.ScanConfig(x_mm=tuple(req.x_mm), y_mm=tuple(req.y_mm), heights_mm=tuple(req.heights_mm), nx=req.nx, ny=req.ny,
                        settle_s=req.settle_s, sweeps=req.sweeps, video_hz=req.video_hz, max_minutes=req.max_minutes)
    try:
        VT.scan_envelope(cfg, -23.0)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    out = store.artifacts_dir() / "robot_sessions" / ("scan_" + time.strftime("%Y%m%dT%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    SCAN["status"], SCAN["stop"] = LiveStatus(out / "live.json"), threading.Event()
    SCAN["status"].update(running=True, phase="connecting", execute=req.execute, box=VT.describe_box(cfg), out_dir=str(out))
    spec = VT.ScanSpec(config=cfg, out_dir=out, execute=req.execute, operator_ack=req.ack)

    def job():
        try:
            VT.run_scan_and_import(spec, SCAN["status"], SCAN["stop"], log=lambda m: SCAN["status"].update(last_log=str(m)))
        except Exception as exc:
            SCAN["status"].update(running=False, done=True, error=f"{type(exc).__name__}: {exc}")

    SCAN["thread"] = threading.Thread(target=job, daemon=True)
    SCAN["thread"].start()
    return {"started": True, "box": VT.describe_box(cfg), "execute": req.execute, "out_dir": str(out)}


@router.post("/api/robot/scan/stop")
def scan_stop():
    if SCAN["stop"] is not None:
        SCAN["stop"].set()
    return {"stop_requested": True}


# ---------------------------------------------------------------------------------------------------------- chess
class ChessRunner:
    """One thread + event loop owning the robot connection; commands arrive through a queue."""

    def __init__(self, spec, cal, game, engine, voice: bool = False, auto_reset: bool = False, max_plies: int | None = None):
        from amperes.integrations import chess_lifecycle as L

        self.spec, self.cal, self.game, self.engine = spec, cal, game, engine
        self.voice, self.auto_reset = voice, auto_reset
        self.adjudicator = L.Adjudicator(ply_cap=max_plies)
        self.stop_event = threading.Event()
        self.state: dict[str, Any] = {"phase": "connecting", "error": None, "summary": None, "connected": False}
        self.lock = threading.Lock()
        self.loop = None
        self.queue = None
        self.sess = None
        self.thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)
        self.thread.start()

    async def _main(self):
        from amperes.integrations import chess_robot as CR

        self.loop, self.queue = asyncio.get_running_loop(), asyncio.Queue()
        try:
            self.sess = await CR.ChessSession(self.spec, self.cal, self.game, self.stop_event, log=self._log).connect()
            self.sess.voice = self.voice
            from amperes.integrations import chess_arrange as _A

            self.sess.ctl.guard = _A.make_hand_guard(self.sess, self._log)  # never descend onto a hand
            try:
                await self.sess.go_home()  # unwind the wrist and rise to the look pose before the first move
            except Exception as exc:
                self._log(f"go home: {type(exc).__name__}: {exc}")
            self.state.update(connected=True, phase="your move" if self.game.board.turn != self.game.robot_color else "robot to move")
            both = self.game.robot_color is None
            if self.spec.auto and (both or self.game.board.turn == self.game.robot_color):
                self.queue.put_nowait("robot_move")
            elif self.spec.auto:
                self.queue.put_nowait("watch")
            while not self.stop_event.is_set():
                cmd = await self.queue.get()
                if cmd == "end":
                    break
                if cmd == "reset":
                    await self._reset()
                    continue
                if cmd == "robot_move":
                    self.state["phase"] = "robot moving"
                    try:
                        await self.sess.play_robot_move()
                    except Exception as exc:
                        import traceback

                        self.state["error"] = f"{type(exc).__name__}: {exc}"
                        self.state["robot_error"] = traceback.format_exc()[-1500:]
                        self.state["phase"] = "stopped"
                        continue
                    self.state["phase"] = "stopped" if self.stop_event.is_set() else "game over" if self._game_over() else "your move"
                    if self.state["phase"] == "game over":
                        self._on_game_over()
                    if self.spec.auto and self.state["phase"] == "your move":
                        if both:
                            self.state["phase"] = "robot to move"
                            await asyncio.sleep(1.0)
                            self.queue.put_nowait("robot_move")
                        else:
                            self.queue.put_nowait("watch")
                if cmd == "watch":
                    # autonomous: watch the board through the wrist camera until the human's move is seen, then reply
                    self.state["phase"] = "watching for your move"
                    seen, failed = None, False
                    try:
                        seen = await self.sess.watch_human_move(timeout_s=900, log=self._log)
                    except Exception as exc:
                        failed, self.state["error"] = True, f"{type(exc).__name__}: {exc}"
                        self.state["watch_failures"] = self.state.get("watch_failures", 0) + 1
                    if self.stop_event.is_set():
                        self.state["phase"] = "stopped"
                    elif failed and self.state["watch_failures"] <= 5:
                        self._log(f"watch failed ({self.state['error'][:60]}); retrying")
                        await asyncio.sleep(2)
                        self.queue.put_nowait("watch")
                    elif seen is None:
                        self.state["phase"] = "your move"  # not seen within the timeout: type it, or press Robot moves after fixing the board
                    else:
                        if self.voice and self.game.history:
                            from amperes.integrations import chess_voice as V

                            V.announce("human_move", self.game.history[-1].get("san", ""), self.game.board.fen(), "The human")
                        if self._game_over():
                            self.state["phase"] = "game over"
                            self._on_game_over()
                        else:
                            self.queue.put_nowait("robot_move")
        except Exception as exc:
            self.state.update(error=f"{type(exc).__name__}: {exc}", phase="failed")
        finally:
            if self.sess is not None:
                try:
                    await self.sess.close()
                except Exception:
                    pass
                if self.sess.session.get("trials"):
                    try:
                        self.state["summary"] = CR.import_chess_session(self.sess.session, self.spec.out_dir)
                    except Exception as exc:
                        self.state["error"] = f"import failed: {type(exc).__name__}: {exc}"
            self.engine.close()
            self.state.update(connected=False, phase=self.state["phase"] if self.state["phase"] in ("failed", "stopped") else "ended")

    def _game_over(self) -> bool:
        verdict = self.adjudicator.verdict(self.game.board)
        if verdict:
            self.state["verdict"] = verdict
        return verdict is not None

    def _on_game_over(self):
        if self.voice:
            from amperes.integrations import chess_voice as V

            V.announce("game_over", use_claude=False)
        if self.auto_reset and self.game.world is not None and self.spec.auto:
            self.queue.put_nowait("reset")

    async def _reset(self):
        """Put every piece back to its start square and the spare queens back in reserve, from the tracked layout.
        Each operation is a supervised pick-and-place trial; the first failure stops the reset (the layout is then
        uncertain, so a person checks it before resetting again)."""
        from dataclasses import asdict

        import chess

        from amperes.integrations import chess_lifecycle as L
        from amperes.integrations import chess_robot as CR
        from amperes.integrations.viam_control import TrialConfig

        world = self.game.world
        if world is None:
            self.state["error"] = "this game does not track the pieces; reset by hand"
            return
        plan = L.plan_reset(self.cal, world.physical, lines=world.lines)
        self.state.update(phase="resetting", reset={"plan": L.plan_to_dict(plan), "done": 0, "failed": None, "unknown": list(world.unknown)})
        if self.voice:
            from amperes.integrations import chess_voice as V

            V.announce("reset", use_claude=False)
        for i, op in enumerate(plan.ops):
            if self.stop_event.is_set():
                self.state["phase"] = "stopped"
                return
            board = chess.Board(None)
            for loc, sym in world.physical.items():
                if L.is_square(loc):
                    board.set_piece_at(chess.parse_square(loc), chess.Piece.from_symbol(sym))
            self.sess._current_op = op
            src, dst = self.sess._pose_for(op, "src", board), self.sess._pose_for(op, "dst", board)
            cfg = TrialConfig(name="chess_reset", approach_mm=self.cal.transport_z - src["z"], settle_s=self.spec.settle_s,
                              move_timeout_s=self.spec.move_timeout_s, trials=1)
            idx = len(self.sess.session["trials"])
            self.sess.session["moving"] = True
            try:
                r = await self.sess.ctl.trial(idx, src, dst, cfg, (op.src, op.dst))
            finally:
                self.sess.session["moving"] = False
            self.sess.session["trials"].append({**asdict(r), "config": asdict(cfg), "chess": {
                "uci": op.src + op.dst, "san": f"reset {op.symbol}", "fen_before": board.fen(), **asdict(op),
                "piece": ("white " if op.symbol.isupper() else "black ") + CR.PIECE_NAME[op.symbol.lower()]}})
            if r.outcome != "success" and self.spec.execute:
                self.state["reset"]["failed"] = {"op": asdict(op), "category": r.category, "error": r.error}
                self.state["phase"] = "reset paused"
                return
            world.apply(op)
            self.state["reset"]["done"] = i + 1
        self.state["phase"] = "board reset"
        self.sess.session["moves"].append({"by": "robot", "uci": "reset", "san": "reset", "t": time.time(), "images_before": [],
                                           "images_after": [], "ops": [o.__dict__ for o in plan.ops]})
        if self.voice:
            from amperes.integrations import chess_voice as V

            V.announce("reset_done", use_claude=False)

    def _log(self, msg):
        self.state["last_log"] = str(msg)

    def enqueue(self, cmd: str):
        if self.loop is None or self.queue is None:
            raise HTTPException(409, "chess session is still connecting")
        self.loop.call_soon_threadsafe(self.queue.put_nowait, cmd)

    def stop(self):
        self.stop_event.set()
        if self.loop is not None and self.queue is not None:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, "end")

    def status(self) -> dict[str, Any]:
        from amperes.integrations import chess_voice as V

        sess = self.sess.session if self.sess is not None else {}
        world = self.game.world.to_dict() if self.game.world is not None else None
        return {**self.state, **self.game.status(), "execute": self.spec.execute, "auto": self.spec.auto, "watch": sess.get("last_watch"),
                "world": world, "voice_line": V.last_line(), "auto_reset": self.auto_reset,
                "robot_moves": sum(1 for m in sess.get("moves", []) if m.get("by") != "human"),
                "piece_ops": len(sess.get("trials", [])), "last_move": ([m for m in sess.get("moves", []) if m.get("by") != "human"] or [None])[-1],
                "trials": [{k: t[k] for k in ("index", "outcome", "category", "error")} | {"op": t["chess"]["kind"], "piece": t["chess"]["piece"],
                                                                                            "src": t["chess"]["src"], "dst": t["chess"]["dst"]}
                           for t in sess.get("trials", [])][-12:],
                "out_dir": str(self.spec.out_dir)}


class ChessNewReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    color: str = Field("black", pattern="^(white|black|both)$")  # both: the arm plays every move (engine vs engine)
    skill: int = Field(5, ge=0, le=20)
    think_s: float = Field(0.5, ge=0.05, le=10)
    settle_s: float = Field(0.4, ge=0, le=3)
    execute: bool = False
    ack: str = ""
    fen: str | None = None
    auto: bool = True  # see the human's move through the wrist camera and reply without typed input
    track: bool = True  # know where every piece is (slots beside the board, spare queens) so the board can be reset
    voice: bool = True  # Halloween commentary through the Mac's speaker
    auto_reset: bool = False  # when the game ends, put every piece back on its own
    max_plies: int | None = Field(None, ge=2, le=600)  # stop robot-vs-robot games after this many half-moves


def _new_world(cal, fen: str | None):
    """The physical layout at the start of a game: the position's pieces on their squares and the spare queens in
    reserve. Pieces missing from a FEN start are somewhere off the board, location unknown."""
    import json as _json

    import chess

    from amperes.integrations import chess_lifecycle as L
    from amperes.integrations import chess_robot as CR

    p = CR.calibration_path()
    lines = int((_json.loads(p.read_text()) if p.exists() else {}).get("grave_lines", L.GRAVE_LINES))
    if not fen:
        return L.World(cal, lines=lines)
    board = chess.Board(fen)
    phys = {chess.square_name(s): pc.symbol() for s, pc in board.piece_map().items()}
    full = [pc.symbol() for pc in chess.Board().piece_map().values()]
    for s in phys.values():
        if s in full:
            full.remove(s)
    phys.update({L.RESERVE["Q"]: "Q", L.RESERVE["q"]: "q"})
    return L.World(cal, physical=phys, lines=lines, unknown=full)


class ChessMoveReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    move: str = Field(..., min_length=2, max_length=10)


class ChessCalReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    king_height_mm: float | None = Field(None, ge=30, le=200)
    grip_fraction: float | None = Field(None, ge=0.2, le=0.8)


def _runner() -> ChessRunner:
    r = CHESS["runner"]
    if r is None:
        raise HTTPException(409, "no chess game; start one first")
    return r


@router.get("/api/chess")
def chess_status():
    from amperes.integrations import chess_robot as CR

    cal: dict[str, Any]
    try:
        c = CR.BoardCal.load()
        cal = {"ok": True, "description": c.describe(), "envelope": c.envelope().__dict__, "look_pose": c.look_pose(),
               "king_height_mm": c.king_height_mm, "grip_fraction": c.grip_fraction}
    except ValueError as exc:
        cal = {"ok": False, "error": str(exc)}
    p = CR.calibration_path()
    taught = {k: {kk: v[kk] for kk in ("x", "y", "z")} for k, v in (json.loads(p.read_text()) if p.exists() else {}).items()
              if isinstance(v, dict) and "x" in v}
    r = CHESS["runner"]
    return {"calibration": cal, "taught": taught, "required_ack": CR.required_ack(_machine()),
            "game": r.status() if r is not None else None}


@router.post("/api/chess/teach/{point}")
def chess_teach(point: str):
    from amperes.integrations import chess_robot as CR

    r = CHESS["runner"]
    if r is not None and r.state.get("phase") == "robot moving":
        raise HTTPException(409, "the arm is moving; teach between moves")
    try:
        return asyncio.run(CR.teach(point))
    except (ValueError, RuntimeError, ConnectionError) as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}")


@router.post("/api/chess/cal")
def chess_cal(req: ChessCalReq):
    from amperes.integrations import chess_robot as CR

    CR.BoardCal.save_settings(king_height_mm=req.king_height_mm, grip_fraction=req.grip_fraction)
    return chess_status()


@router.post("/api/chess/new")
def chess_new(req: ChessNewReq):
    import chess

    from amperes.integrations import chess_robot as CR

    r = CHESS["runner"]
    if r is not None and r.thread.is_alive():
        raise HTTPException(409, "a game is already running; end it first")
    try:
        cal = CR.BoardCal.load()
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    out = store.artifacts_dir() / "robot_sessions" / ("chess_" + time.strftime("%Y%m%dT%H%M%S"))
    spec = CR.ChessSpec(out_dir=out, execute=req.execute, operator_ack=req.ack, robot_color=req.color, skill=req.skill, think_s=req.think_s,
                        settle_s=req.settle_s, fen=req.fen, auto=req.auto)
    if req.execute and req.ack.strip() != CR.required_ack(_machine()):
        raise HTTPException(400, f"motion needs the operator acknowledgement exactly: '{CR.required_ack(_machine())}'")
    try:
        engine = CR.Engine(skill=req.skill, think_s=req.think_s)
    except Exception as exc:
        raise HTTPException(500, f"Stockfish not available: {type(exc).__name__}: {exc}")
    try:
        world = _new_world(cal, req.fen) if req.track else None
        game = CR.Game(engine, robot_color=None if req.color == "both" else (chess.WHITE if req.color == "white" else chess.BLACK), fen=req.fen,
                       world=world)
    except ValueError as exc:
        engine.close()
        raise HTTPException(400, f"bad FEN: {exc}")
    CHESS["runner"] = ChessRunner(spec, cal, game, engine, voice=req.voice, auto_reset=req.auto_reset, max_plies=req.max_plies)
    return {"started": True, "execute": req.execute, "robot": req.color, "out_dir": str(out), "board": cal.describe(),
            "tracking": world.to_dict() if world is not None else None}


@router.get("/api/chess/reset/plan")
def chess_reset_plan(fen: str | None = None):
    """The reset plan without moving anything: from the running game's tracked layout, or from a FEN (pieces missing
    from it assumed in the slots beside the board, spare queens in reserve)."""
    from amperes.integrations import chess_lifecycle as L
    from amperes.integrations import chess_robot as CR

    try:
        cal = CR.BoardCal.load()
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    r = CHESS["runner"]
    if fen is None and r is not None and r.game.world is not None:
        world = r.game.world
    else:
        try:
            world = _new_world(cal, fen)
        except ValueError as exc:
            raise HTTPException(400, f"bad FEN: {exc}")
        for slot, sym in zip(L.capture_slots(cal, world.lines), list(world.unknown)):
            world.physical[slot] = sym
        world.unknown = world.unknown[len(L.capture_slots(cal, world.lines)):]
    plan = L.plan_reset(cal, world.physical, lines=world.lines)
    return {"layout": world.to_dict(), "plan": L.plan_to_dict(plan), "slots": {s: [round(v) for v in L.location_xy(cal, s)]
                                                                              for s in L.all_slots(cal, world.lines)}}


@router.post("/api/chess/reset")
def chess_reset():
    r = _runner()
    if r.state.get("phase") in ("robot moving", "watching for your move", "resetting"):
        raise HTTPException(409, f"cannot reset now (phase: {r.state.get('phase')})")
    if r.game.world is None:
        raise HTTPException(400, "this game does not track the pieces (track=false); reset by hand")
    r.enqueue("reset")
    return r.status()


@router.post("/api/chess/human")
def chess_human(req: ChessMoveReq):
    r = _runner()
    if r.state.get("phase") not in ("your move", "robot to move", "watching for your move") or r.game.board.turn == r.game.robot_color:
        raise HTTPException(409, f"not your move (phase: {r.state.get('phase')})")
    try:
        mv = r.game.human_move(req.move)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not r.game.board.is_game_over():
        r.state["phase"] = "robot to move"
        r.enqueue("robot_move")
    else:
        r.state["phase"] = "game over"
    return {"accepted": mv.uci(), **r.status()}


@router.post("/api/chess/watch")
def chess_watch():
    r = _runner()
    if r.game.board.turn == r.game.robot_color or r.state.get("phase") in ("robot moving", "watching for your move"):
        raise HTTPException(409, f"cannot watch now (phase: {r.state.get('phase')})")
    r.state["watch_failures"] = 0
    r.enqueue("watch")
    return r.status()


@router.post("/api/chess/robot")
def chess_robot_move():
    r = _runner()
    if (r.game.robot_color is not None and r.game.board.turn != r.game.robot_color) or r.state.get("phase") == "robot moving":
        raise HTTPException(409, "not the robot's turn")
    r.state["phase"] = "robot to move"
    r.enqueue("robot_move")
    return r.status()


@router.post("/api/chess/end")
def chess_end():
    r = _runner()
    r.stop() if r.state.get("phase") in ("robot moving", "watching for your move") else r.enqueue("end")
    r.thread.join(timeout=90)
    return r.status()


@router.post("/api/chess/stop")
def chess_stop():
    r = CHESS["runner"]
    if r is not None:
        r.stop()
    return {"stop_requested": True, "note": "the arm and gripper are sent stop() immediately, including mid-move"}


@router.get("/api/chess/board.svg")
def chess_board_svg():
    import chess
    import chess.svg

    r = CHESS["runner"]
    board = r.game.board if r is not None else chess.Board()
    last = board.peek() if board.move_stack else None
    flipped = r is not None and r.game.robot_color == chess.WHITE
    return Response(chess.svg.board(board, lastmove=last, flipped=flipped, size=420), media_type="image/svg+xml")


@router.get("/api/chess/images/{filename}")
def chess_image(filename: str):
    r = _runner()
    root = (Path(r.spec.out_dir) / "images").resolve()
    p = (root / filename).resolve()
    if p.parent != root or not p.is_file() or p.suffix.lower() not in (".jpg", ".jpeg", ".png"):
        raise HTTPException(404, "no such image")
    return FileResponse(p)
