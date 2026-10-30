"""Workspace scan on the Viam arm: a scripted sweep over a box above the table, photographing it from known poses.

What it produces (per sweep = one canonical episode):
- wrist-camera RGB + depth at every grid pose, with the pose the arm actually reached (hand-eye ground truth);
- commanded vs achieved pose for every visit (reach accuracy) and repeated visits to the centre pose (repeatability);
- transit/planning duration per move; planning failures recorded per pose, not hidden;
- continuous proprioception (joints + end-effector pose, ~3 Hz) and wrist-camera video frames (`video_hz`).

Safety is the same model as pick-place (`viam_control`): dry run by default, motion needs `execute` + the operator
acknowledgement, every target is checked against an envelope before it reaches the SDK, all moves go through the
motion service (collision-aware for the configured table/walls/ceiling), telemetry watchdog, STOP mid-move. The
difference is the envelope: instead of three taught spots it is the scan box the operator approved, whose floor keeps
the fingertips at least `clearance_mm` above the table surface. The operator must clear that box of objects first.
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from amperes.integrations.viam_control import (client_options, POSE_KEYS, WATCHDOG_S, Controller, Envelope, SafetyError, dial_robot, required_ack, timed)

TOOL_LENGTH_MM = 150.0  # gripper fingertips below the flange (frame system: gripper at z=150 on the arm)
DOWN = {"o_x": 0.0, "o_y": 0.0, "o_z": -1.0}


@dataclass
class ScanConfig:
    name: str = "scan"
    x_mm: tuple[float, float] = (250.0, 450.0)
    y_mm: tuple[float, float] = (-200.0, 200.0)
    heights_mm: tuple[float, ...] = (320.0, 420.0)  # flange z (world); fingertips are TOOL_LENGTH_MM lower
    nx: int = 3
    ny: int = 3
    theta_deg: float = 0.0
    settle_s: float = 0.6
    move_timeout_s: float = 25.0
    sweeps: int = 1
    revisit_every: int = 4          # revisit the centre pose after this many grid poses (repeatability samples)
    video_hz: float = 2.0           # continuous wrist-camera colour frames while the tool points down; 0 disables
    pose_tol_mm: float = 5.0        # achieved vs commanded beyond this is a POSE_ERROR
    clearance_mm: float = 80.0      # minimum fingertip height above the table surface anywhere in the box
    max_minutes: float = 25.0       # session time cap (disk/memory bound for video)

    def label(self) -> str:
        return (f"{self.name}(x={self.x_mm[0]:g}..{self.x_mm[1]:g},y={self.y_mm[0]:g}..{self.y_mm[1]:g},"
                f"z={','.join(f'{h:g}' for h in self.heights_mm)},grid={self.nx}x{self.ny},settle={self.settle_s:g})")


def scan_envelope(cfg: ScanConfig, table_z: float, margin_mm: float = 20.0) -> Envelope:
    floor = table_z + TOOL_LENGTH_MM + cfg.clearance_mm
    if min(cfg.heights_mm) < floor:
        raise ValueError(f"lowest scan height {min(cfg.heights_mm):g} mm puts the fingertips closer than {cfg.clearance_mm:g} mm "
                         f"to the table (flange floor is {floor:g} mm)")
    return Envelope(x=(cfg.x_mm[0] - margin_mm, cfg.x_mm[1] + margin_mm), y=(cfg.y_mm[0] - margin_mm, cfg.y_mm[1] + margin_mm),
                    z=(floor, max(cfg.heights_mm) + margin_mm), max_step_mm=350.0, max_theta_change_deg=45.0)


def _lin(lo: float, hi: float, n: int) -> list[float]:
    return [lo] if n <= 1 else [lo + (hi - lo) * i / (n - 1) for i in range(n)]


def scan_home(cfg: ScanConfig) -> dict[str, float]:
    return {"x": sum(cfg.x_mm) / 2, "y": sum(cfg.y_mm) / 2, "z": max(cfg.heights_mm), **DOWN, "theta": cfg.theta_deg}


def generate_poses(cfg: ScanConfig) -> list[tuple[str, dict[str, float]]]:
    """Serpentine grid per height (short moves), with the centre pose re-inserted every `revisit_every` poses."""
    centre = scan_home(cfg)
    out: list[tuple[str, dict[str, float]]] = []
    k = r = 0
    for hi, h in enumerate(cfg.heights_mm):
        ys = _lin(*cfg.y_mm, cfg.ny)
        if hi % 2:  # next layer starts above the corner the previous one ended on
            ys = ys[::-1]
        for y in ys:
            xs = _lin(*cfg.x_mm, cfg.nx)
            if r % 2:
                xs = xs[::-1]
            r += 1
            for x in xs:
                out.append((f"g{k:02d}_z{h:g}", {"x": x, "y": y, "z": h, **DOWN, "theta": cfg.theta_deg}))
                k += 1
                if cfg.revisit_every and k % cfg.revisit_every == 0:
                    out.append(("centre", dict(centre)))
    return out


@dataclass
class SweepResult:
    index: int
    outcome: str = "unknown"
    category: str | None = None
    poses: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    started: float = 0.0
    ended: float = 0.0
    error: str | None = None


class ScanController(Controller):
    """Adds `visit`: move, settle, read what the arm actually did, photograph. Motion still only via `_move`."""

    async def joints(self) -> list[float] | None:
        if not self.execute:
            return None
        try:
            jp = await timed(self.arm.get_joint_positions(), 3)
            return [float(v) for v in jp.values]
        except Exception:
            return None

    async def visit(self, label: str, target: dict[str, float], cfg: ScanConfig, sweep: int, idx: int) -> dict[str, Any]:
        rec: dict[str, Any] = {"sweep": sweep, "idx": idx, "label": label, "commanded": {k: round(target[k], 3) for k in POSE_KEYS},
                               "t_cmd": time.time(), "status": "unknown"}
        try:
            rec["move_s"] = await self._move(target, label, cfg.move_timeout_s)
        except asyncio.TimeoutError:
            rec.update(status="timeout")
            raise
        except SafetyError:
            rec.update(status="refused")
            raise
        except Exception as exc:  # the motion service could not plan/execute this pose: record it, keep scanning
            if self.stop_event.is_set():
                raise
            rec.update(status="planning_failure", error=f"{type(exc).__name__}: {str(exc)[:200]}")
            self.events.append({"timestamp": time.time(), "event_type": "fault", "event_class": "planning_failure",
                                "code": "PLANNING_FAILURE", "message": f"{label}: {rec['error']}"})
            return rec
        await asyncio.sleep(cfg.settle_s if self.execute else 0)
        rec["t_settled"] = time.time()
        achieved = await self.pose()
        rec["achieved"] = {k: round(achieved[k], 3) for k in POSE_KEYS}
        rec["error_mm"] = round(math.dist([achieved[k] for k in "xyz"], [target[k] for k in "xyz"]), 3)
        rec["joints"] = await self.joints()
        rec["status"] = "ok" if rec["error_mm"] <= cfg.pose_tol_mm else "pose_error"
        if self.snap and self.execute:
            rec["images"] = await self.snap(f"p{idx:02d}_{label}", sweep, True)
        return rec

    async def sweep(self, i: int, poses: list[tuple[str, dict[str, float]]], cfg: ScanConfig) -> SweepResult:
        res = SweepResult(index=i, started=time.time())
        n0 = len(self.actions)
        consecutive = 0
        try:
            for idx, (label, target) in enumerate(poses):
                self._check_stop()
                rec = await self.visit(label, target, cfg, i, idx)
                res.poses.append(rec)
                consecutive = consecutive + 1 if rec["status"] == "planning_failure" else 0
                if consecutive >= 3:
                    raise RuntimeError("three consecutive planning failures; aborting the sweep")
            if not self.execute:
                res.outcome = "unknown"
            elif any(p["status"] == "planning_failure" for p in res.poses):
                res.outcome, res.category = "failure", "PLANNING_FAILURE"
            elif any(p["status"] == "pose_error" for p in res.poses):
                res.outcome, res.category = "failure", "POSE_ERROR"
            elif any(not p.get("images") for p in res.poses):
                res.outcome, res.category = "failure", "CAMERA_NO_FRAME"
            else:
                res.outcome = "success"
        except asyncio.TimeoutError:
            res.outcome, res.category, res.error = "timeout", "TIMEOUT", "move exceeded its time budget"
        except SafetyError as exc:
            res.outcome, res.category, res.error = "invalid", "SAFETY_STOP", str(exc)
        except Exception as exc:
            if self.stop_event.is_set():
                res.outcome, res.category, res.error = "invalid", "SAFETY_STOP", f"stopped during motion ({type(exc).__name__})"
            else:
                res.outcome, res.category, res.error = "invalid", "INFRASTRUCTURE_ERROR", f"{type(exc).__name__}: {str(exc)[:200]}"
        res.ended = time.time()
        res.steps = self.actions[n0:]
        return res


@dataclass
class ScanSpec:
    config: ScanConfig
    out_dir: Path
    execute: bool = False
    operator_ack: str = ""
    table_z: float = -23.0
    camera_names: tuple[str, ...] = ("cam",)


def describe_box(cfg: ScanConfig, table_z: float = -23.0) -> str:
    """Plain words for the operator: what must be clear before the arm moves."""
    tips = min(cfg.heights_mm) - TOOL_LENGTH_MM - table_z
    return (f"The arm will sweep the area {cfg.x_mm[0] / 10:g}-{cfg.x_mm[1] / 10:g} cm in front of its base and "
            f"{abs(cfg.y_mm[0]) / 10:g} cm to each side, with the fingertips never lower than {tips / 10:g} cm above the table. "
            f"Clear that area of laptops, cables, hands and anything taller than {max(0.0, (tips - 50) / 10):g} cm.")


async def run_scan_session(spec: ScanSpec, live=None, stop_event: threading.Event | None = None, log=print) -> dict[str, Any]:
    from viam.components.arm import Arm
    from viam.components.camera import Camera
    from viam.components.gripper import Gripper
    from viam.robot.client import RobotClient
    from viam.services.motion import MotionClient

    from amperes.integrations.viam_live import _env
    import os

    cfg = spec.config
    address = _env()
    machine_name = address.split(".")[0]
    if spec.execute and spec.operator_ack.strip() != required_ack(machine_name):
        raise SafetyError(f"motion needs the operator acknowledgement exactly: '{required_ack(machine_name)}'")
    env = scan_envelope(cfg, spec.table_z)
    poses = generate_poses(cfg)
    home = scan_home(cfg)
    img_dir = spec.out_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    opts = client_options()
    stop_event = stop_event or threading.Event()
    m = await dial_robot(address, opts, "control", log)
    tm = await dial_robot(address, opts, "telemetry", log)                           # telemetry / watchdog
    cm = await dial_robot(address, opts, "video", log) if cfg.video_hz > 0 else None  # video
    sid = f"scan-{machine_name}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
    session: dict[str, Any] = {"machine": machine_name, "session_id": sid, "started": time.time(), "config": asdict(cfg),
                               "execute": spec.execute, "envelope": asdict(env), "box_description": describe_box(cfg, spec.table_z),
                               "poses_planned": len(poses), "telemetry": [], "images": [], "video": [], "sweeps": [], "events": []}
    try:
        arm, grip = Arm.from_robot(m, "arm"), Gripper.from_robot(m, "gripper")
        motion = MotionClient.from_robot(m, "builtin")
        ctl = ScanController(arm, grip, motion, "arm", spec.execute, env, stop_event, log, frame="world")
        t_arm = Arm.from_robot(tm, "arm")
        cams = []
        for cn in spec.camera_names:
            try:
                cams.append((cn, Camera.from_robot(m, cn)))
            except Exception:
                session["events"].append({"timestamp": time.time(), "event_type": "recording_error", "event_class": "data_recording_failure",
                                          "code": "CAMERA_MISSING", "message": f"{cn} not available"})
        vcam = None
        if cm is not None:
            try:
                vcam = Camera.from_robot(cm, spec.camera_names[0])
            except Exception:
                vcam = None
        watchdog = {"last_ok": time.time(), "fails": 0, "halted": False, "down": False}
        done = asyncio.Event()

        async def telemetry():
            nonlocal tm, t_arm
            while not done.is_set():
                if stop_event.is_set() and not watchdog["halted"]:
                    watchdog["halted"] = True
                    await ctl.stop()
                t0 = time.time()
                try:
                    jp, p = await timed(asyncio.gather(t_arm.get_joint_positions(), t_arm.get_end_position()), 2.5)
                    session["telemetry"].append({"t": round(t0, 3), "joints": list(jp.values), "pose": [getattr(p, k) for k in POSE_KEYS]})
                    watchdog["last_ok"], watchdog["fails"], watchdog["down"] = time.time(), 0, p.o_z < -0.7
                except Exception:
                    watchdog["fails"] += 1
                    session["telemetry"].append({"t": round(t0, 3), "joints": None, "pose": None})
                    if watchdog["fails"] % 3 == 0:  # the link, not the arm, is the usual culprit: re-dial before giving up
                        try:
                            new = await dial_robot(address, opts, "telemetry re-dial", log, attempts=1)
                            old, tm = tm, new
                            t_arm = Arm.from_robot(tm, "arm")
                            session["events"].append({"timestamp": time.time(), "event_type": "recording_error", "event_class": "data_recording_failure",
                                                      "code": "TELEMETRY_RECONNECT", "message": f"telemetry connection re-dialled after {watchdog['fails']} failed reads"})
                            asyncio.create_task(old.close())
                        except Exception:
                            pass
                if spec.execute and time.time() - watchdog["last_ok"] > WATCHDOG_S and not watchdog["halted"]:
                    session["events"].append({"timestamp": time.time(), "event_type": "fault", "event_class": "infrastructure_failure",
                                              "code": "WATCHDOG_TELEMETRY_LOST", "message": f"no telemetry for {WATCHDOG_S:g} s during motion; stopping"})
                    watchdog["halted"] = True
                    await ctl.stop()
                await asyncio.sleep(max(0.0, 0.33 - (time.time() - t0)))

        async def video():
            # Continuous wrist-camera colour frames, written straight to disk; only while the tool points at the table.
            n, period, misses = 0, 1.0 / cfg.video_hz, 0
            while not done.is_set():
                t0 = time.time()
                if watchdog["down"] and spec.execute:
                    try:  # colour only (~20 KB JPEG): the raw depth frame every tick starved the telemetry link
                        r = await timed(vcam.get_images(filter_source_names=["color"]), 4)
                        imgs = r[0] if isinstance(r, tuple) else r
                        im = next(i for i in imgs if "jpeg" in str(i.mime_type) or "png" in str(i.mime_type))
                        fn = f"{sid}_cam_video_{n:06d}.jpg"
                        (img_dir / fn).write_bytes(im.data)
                        session["video"].append({"t": t0, "n": n, "file": fn})
                        n += 1
                        misses = 0
                    except Exception:
                        misses += 1
                        if misses in (1, 10, 100):
                            session["events"].append({"timestamp": t0, "event_type": "recording_error", "event_class": "data_recording_failure",
                                                      "code": "VIDEO_NO_FRAME", "message": f"video frame read failed ({misses} in a row)"})
                await asyncio.sleep(max(0.0, period - (time.time() - t0)))

        async def snap(tag: str, sweep: int, depth: bool = False) -> list[str]:
            files: list[str] = []
            try:
                cur = await ctl.pose()
                if cur["o_z"] > -0.7:
                    return files
            except Exception:
                return files
            for cn, cam in cams:
                try:
                    r = await timed(cam.get_images(), 8)
                    imgs = r[0] if isinstance(r, tuple) else r
                    for im in imgs:
                        is_depth = "dep" in str(im.mime_type)
                        if is_depth and not depth:
                            continue
                        ext = "dep" if is_depth else "jpg" if "jpeg" in str(im.mime_type) else "png"
                        fn = f"{sid}-s{sweep:03d}_{cn}_{tag}{'_depth' if is_depth else ''}.{ext}"
                        (img_dir / fn).write_bytes(im.data)
                        session["images"].append({"t": time.time(), "camera": cn, "tag": tag, "sweep": sweep, "mime": str(im.mime_type), "file": fn})
                        files.append(fn)
                except Exception:
                    session["events"].append({"timestamp": time.time(), "event_type": "recording_error", "event_class": "data_recording_failure",
                                              "code": "CAMERA_NO_FRAME", "message": f"{cn} returned no frame ({tag}, sweep {sweep})"})
            return files

        ctl.snap = snap
        tasks = [asyncio.create_task(telemetry())]
        if vcam is not None and spec.execute:
            tasks.append(asyncio.create_task(video()))
        if live:
            live.update(running=True, phase="moving to scan home" if spec.execute else "dry run", config=cfg.label(), execute=spec.execute,
                        sweeps_done=0, sweeps_total=cfg.sweeps, poses_per_sweep=len(poses), results=[])
        deadline = time.time() + cfg.max_minutes * 60
        try:
            await ctl._move(home, "scan_home", cfg.move_timeout_s)
            for i in range(cfg.sweeps):
                if stop_event.is_set() or time.time() > deadline:
                    break
                if live:
                    live.update(phase=f"sweep {i + 1}/{cfg.sweeps}")
                r = await ctl.sweep(i, poses, cfg)
                session["sweeps"].append(asdict(r))
                ok = sum(p["status"] == "ok" for p in r.poses)
                log(f"sweep {i}: {r.outcome} {r.category or ''} poses_ok={ok}/{len(poses)} "
                    f"max_err={max([p.get('error_mm', 0) for p in r.poses] or [0]):.1f}mm {r.error or ''}")
                if live:
                    live.update(sweeps_done=i + 1, results=[{k: s[k] for k in ("index", "outcome", "category", "error")} | {"poses_ok": ok}
                                                            for s in session["sweeps"]])
                if r.category in ("SAFETY_STOP", "INFRASTRUCTURE_ERROR", "TIMEOUT"):
                    break
            if not stop_event.is_set():
                await ctl._move(home, "scan_home", cfg.move_timeout_s)
        finally:
            done.set()
            await asyncio.sleep(0)
            for t in tasks:
                t.cancel()
            session["events"] += ctl.events
            session["actions"] = ctl.actions
    finally:
        session["ended"] = time.time()
        for c in (m, tm, cm):
            if c is not None:
                try:
                    await c.close()
                except Exception:
                    pass
    return session


def _reach_stats(poses: list[dict[str, Any]]) -> dict[str, Any]:
    errs = [p["error_mm"] for p in poses if "error_mm" in p]
    centre = [p["achieved"] for p in poses if p["label"] == "centre" and "achieved" in p]
    rep = None
    if len(centre) >= 2:
        mean = [sum(c[k] for c in centre) / len(centre) for k in "xyz"]
        rep = round((sum(math.dist([c[k] for k in "xyz"], mean) ** 2 for c in centre) / (len(centre) - 1)) ** 0.5, 3)
    return {"visited": len(poses), "ok": sum(p["status"] == "ok" for p in poses),
            "planning_failures": sum(p["status"] == "planning_failure" for p in poses),
            "pose_errors": sum(p["status"] == "pose_error" for p in poses),
            "error_mm_max": round(max(errs), 3) if errs else None, "error_mm_mean": round(sum(errs) / len(errs), 3) if errs else None,
            "centre_revisits": len(centre), "repeatability_mm_std": rep,
            "move_s_mean": round(sum(p["move_s"] for p in poses if "move_s" in p) / max(1, sum("move_s" in p for p in poses)), 3)}


def scan_session_to_canonical(session: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """Each sweep → one canonical episode; plus poses.jsonl (reach table) and frames.jsonl (video index) sidecars."""
    from amperes.schema import Episode, Event, Outcome, Ref, RobotInfo, SessionInfo, SourceInfo, Stream, episode_to_json

    out_dir.mkdir(parents=True, exist_ok=True)
    img_dir = out_dir / "images"
    img_dir.mkdir(exist_ok=True)
    sid, cfg = session["session_id"], session["config"]
    label = ScanConfig(**{k: (tuple(v) if isinstance(v, list) else v) for k, v in cfg.items()}).label()
    eps, pose_rows, frame_rows = [], [], []
    claimed: set[int] = set()
    for sw in session["sweeps"]:
        s, e = sw["started"], sw["ended"] or sw["started"]
        eid = f"{sid}-s{sw['index']:03d}"
        tel = [x for x in session["telemetry"] if s <= x["t"] <= e]
        moves = [a for a in sw["steps"] if a["kind"] == "move"]
        settled = [p for p in sw["poses"] if "achieved" in p]
        frames = [f for f in session["video"] if s <= f["t"] <= e]
        for n, f in enumerate(frames):  # per-episode frame files: <eid>_cam_video_<n>.jpg
            new = f"{eid}_cam_video_{n:05d}.jpg"
            if (img_dir / f["file"]).exists():
                (img_dir / f["file"]).rename(img_dir / new)
            f["file"], f["episode_id"], f["n_episode"] = new, eid, n
            claimed.add(f["n"])
        streams = {
            "obs.joint_pos": Stream(kind="observation", t=[x["t"] for x in tel], values=[x["joints"] or [None] * 6 for x in tel],
                                    units="deg", expected_rate_hz=3.0, valid_range=[-360.0, 360.0], source="viam:arm.get_joint_positions"),
            "obs.ee_pose": Stream(kind="observation", t=[x["t"] for x in tel], values=[x["pose"] or [None] * 7 for x in tel],
                                  units="mm, orientation-vector, deg", expected_rate_hz=3.0, source="viam:arm.get_end_position"),
            "act.target_pose": Stream(kind="action", t=[a["t"] for a in moves], values=[[a["target"][k] for k in POSE_KEYS] for a in moves],
                                      units="mm, orientation-vector, deg", source="amperes scripted workspace scan"),
        }
        if settled:
            visit_hz = round(len(settled) / max(e - s, 1e-6), 4)  # one sample per visited pose: declare that cadence
            streams["obs.ee_pose_settled"] = Stream(kind="observation", t=[p["t_settled"] for p in settled],
                                                    values=[[p["achieved"][k] for k in POSE_KEYS] for p in settled], expected_rate_hz=visit_hz,
                                                    units="mm, orientation-vector, deg", source="viam:motion.get_pose(world) after settle")
            if any(p.get("joints") for p in settled):
                streams["obs.joint_pos_settled"] = Stream(kind="observation", t=[p["t_settled"] for p in settled],
                                                          values=[p["joints"] or [None] * 6 for p in settled], units="deg", expected_rate_hz=visit_hz,
                                                          source="viam:arm.get_joint_positions after settle")
        if frames:
            streams["obs.cam.frame"] = Stream(kind="observation", t=[f["t"] for f in frames], values=[[float(f["n_episode"])] for f in frames],
                                              units=f"frame index: images/{eid}_cam_video_<n:05d>.jpg", expected_rate_hz=float(cfg["video_hz"]),
                                              source="viam:cam.get_images color")
        events = [Event(t=a["t"], type="fault", code="SAFETY_REFUSAL", message="; ".join(a["refused"]), source="amperes safety")
                  for a in sw["steps"] if a.get("refused")]
        events += [Event(t=x["timestamp"], type=x["event_type"] if x["event_type"] in ("fault", "recording_error") else "annotation",
                         code=x["code"], message=x["message"], source="amperes robot session")
                   for x in session["events"] if s <= x["timestamp"] <= e and x["code"] != "SAFETY_REFUSAL"]
        if sw["error"] and sw["category"] == "INFRASTRUCTURE_ERROR":
            events.append(Event(t=e, type="error", code="SDK_ERROR", message=sw["error"], source="amperes robot session"))
        for im in session["images"]:
            if im["sweep"] == sw["index"]:
                events.append(Event(t=im["t"], type="annotation", code="CAMERA_SNAPSHOT", message=im["file"], source=im["camera"]))
        stats = _reach_stats(sw["poses"])
        for p in sw["poses"]:
            pose_rows.append({"episode_id": eid, "session_id": sid, **p})
        crit = {"poses_ok": stats["ok"], "poses_visited": stats["visited"], "error_mm_max": stats["error_mm_max"], "tol_mm": cfg["pose_tol_mm"]}
        ep = Episode(
            episode_id=eid,
            source=SourceInfo(dataset_id="robot", adapter="amperes.viam_tasks@1.0.0", source_uri=f"viam://{session['machine']}",
                              source_record_id=eid, synthetic=False, synthetic_note=None if session["execute"] else "DRY RUN: no motion was executed"),
            task=Ref(id="workspace_scan_real", version="1.0.0"), policy=Ref(id="scripted_workspace_scan", version=label),
            robot=RobotInfo(embodiment="6-joint arm + parallel gripper + wrist RGB-D camera (event-provided)", device_id="arm", config_id=cfg["name"]),
            session=SessionInfo(session_id=sid, workcell_id=session["machine"], operator_id="op-onsite"),
            start_time=s, end_time=e, time_base="unix_s", streams=streams,
            outcome=Outcome(label=sw["outcome"], provenance="amperes_verifier" if session["execute"] else "unknown",
                            verified=bool(session["execute"] and settled), reason=sw["category"] or sw["error"],
                            verifier_id="pose_error", verifier_version="1.0.0",
                            measurements={**stats, "criteria": crit} if session["execute"] else stats),
            events=events,
            conditions={"sweep": sw["index"], "attempt_index": 1, "config": cfg, "dry_run": not session["execute"],
                        "box": session["envelope"], "poses_planned": session["poses_planned"],
                        "instruction": f"Scan the workspace: visit {session['poses_planned']} poses over the table at "
                                       f"{', '.join(f'{h:g} mm' for h in cfg['heights_mm'])} with the camera pointing down, and photograph it from each."},
            extra={"error": sw["error"], "reach": stats, "video_frames": len(frames),
                   "planning_failures": [p["label"] for p in sw["poses"] if p["status"] == "planning_failure"]},
        )
        eps.append(ep)
    for f in session["video"]:
        if f["n"] not in claimed:
            f["episode_id"] = None
        frame_rows.append(f)
    with open(out_dir / "episodes.jsonl", "w") as fh:
        for ep in eps:
            fh.write(episode_to_json(ep) + "\n")
    with open(out_dir / "poses.jsonl", "w") as fh:
        for r in pose_rows:
            fh.write(json.dumps(r) + "\n")
    with open(out_dir / "frames.jsonl", "w") as fh:
        for r in frame_rows:
            fh.write(json.dumps(r) + "\n")
    evs = [{"timestamp": session["started"], "event_type": "session_start", "workcell_id": session["machine"], "session_id": sid},
           {"timestamp": session["ended"], "event_type": "session_end", "workcell_id": session["machine"], "session_id": sid}]
    evs += [{**{k: x[k] for k in ("event_type", "event_class", "code", "message")}, "timestamp": x["timestamp"],
             "workcell_id": session["machine"], "session_id": sid, "device_id": "arm"}
            for x in session["events"] if x["event_type"] in ("fault", "recording_error")]
    with open(out_dir / "events.jsonl", "w") as fh:
        for x in evs:
            fh.write(json.dumps(x) + "\n")
    summary = {"session_id": sid, "task": "workspace_scan", "config": label, "executed": session["execute"], "box": session["box_description"],
               "sweeps": len(session["sweeps"]), "outcomes": {o: sum(s["outcome"] == o for s in session["sweeps"]) for o in {s["outcome"] for s in session["sweeps"]}},
               "poses_visited": len(pose_rows), "poses_ok": sum(p["status"] == "ok" for p in pose_rows),
               "planning_failures": sum(p["status"] == "planning_failure" for p in pose_rows),
               "reach": _reach_stats(pose_rows) if pose_rows else None,
               "snapshots": len(session["images"]), "video_frames": len(frame_rows), "telemetry_samples": len(session["telemetry"]),
               "events": len(session["events"]), "out_dir": str(out_dir)}
    (out_dir / "session_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def run_scan_and_import(spec: ScanSpec, live=None, stop_event: threading.Event | None = None, log=print) -> dict[str, Any]:
    from amperes import incidents
    from amperes.ingest.pipeline import import_dataset

    session = asyncio.run(run_scan_session(spec, live, stop_event, log))
    summary = scan_session_to_canonical(session, spec.out_dir)
    if session["sweeps"]:
        r = import_dataset(spec.out_dir, name=f"{'ROBOT' if spec.execute else 'DRY-RUN'} workspace scan {summary['session_id']}", adapter="jsonl",
                           synthetic=not spec.execute, note="real arm workspace scan: RGB-D at known poses, reach accuracy, video"
                           if spec.execute else "dry run: plan validated, no motion", ctx={"required_metadata": ["task.id"]})
        summary.update(dataset_id=r["dataset_id"], incidents=len(incidents.build_incidents(r["dataset_id"])), quality=r["quality"]["status_counts"])
    (spec.out_dir / "session_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    if live:
        live.update(running=False, done=True, summary=summary)
    return summary
