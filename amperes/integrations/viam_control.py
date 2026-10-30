"""Safe, supervised pick-and-place trials on a Viam arm → canonical Amperes episodes.

SAFETY MODEL (all enforced in code; see tests/test_viam_control.py):
1. Human authorization: motion requires `execute=True` AND an operator acknowledgement string naming the machine.
   Default is DRY RUN: the plan is validated and recorded, nothing moves.
2. Taught envelope: targets must lie inside the box spanned by poses a human taught (by jogging the arm and running
   `robot teach`), expanded by small margins; z never goes below the lowest taught pose minus 3 mm, and never below the
   configured table plane + clearance. Anything else is refused before reaching the SDK.
3. Bounded steps: each commanded move is limited in length (mm) and orientation change (deg) vs the current pose.
4. Watchdog: a separate read-only telemetry loop; if reads fail/stall during motion the run calls stop() and aborts.
5. Stop: `stop()` (UI STOP button / Ctrl-C) halts the arm and gripper and ends the run.
6. Collision-aware planning: transit moves go through the machine's motion service, which knows the configured
   obstacles (table, walls, ceiling); the arm's own move_to_position is used only if the motion service is unavailable.
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

POSE_KEYS = ("x", "y", "z", "o_x", "o_y", "o_z", "theta")


def waypoints_path() -> Path:
    from amperes import store

    p = store.home() / "robot" / "waypoints.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_waypoints() -> dict[str, dict[str, float]]:
    p = waypoints_path()
    return json.loads(p.read_text()) if p.exists() else {}


def save_waypoint(name: str, pose: dict[str, float], meta: dict[str, Any]) -> dict[str, Any]:
    if name not in ("home", "a", "b"):
        raise ValueError("waypoint name must be one of: home, a, b (a and b are the two object spots)")
    wps = load_waypoints()
    wps[name] = {**{k: round(float(pose[k]), 3) for k in POSE_KEYS}, "_taught_at": time.time(), **meta}
    waypoints_path().write_text(json.dumps(wps, indent=2))
    return wps[name]


@dataclass
class Envelope:
    """Axis-aligned safety box (mm, in the frame poses were taught in) + step limits."""

    x: tuple[float, float]
    y: tuple[float, float]
    z: tuple[float, float]
    max_step_mm: float = 260.0
    max_theta_change_deg: float = 45.0

    @classmethod
    def from_waypoints(cls, wps: dict[str, dict[str, float]], approach_mm: float, table_z: float | None,
                       table_clearance_mm: float = 15.0, margin_mm: float = 40.0, tool_length_mm: float = 150.0) -> "Envelope":
        """`table_z` is the table's top SURFACE in world mm. Poses are arm-flange poses; when the tool points down the
        fingertips are `tool_length_mm` below the flange, so the flange floor is surface + tool length + clearance."""
        need = {"home", "a", "b"} - set(wps)
        if need:
            raise ValueError(f"teach these waypoints first: {sorted(need)}")
        xs = [wps[k]["x"] for k in ("home", "a", "b")]
        ys = [wps[k]["y"] for k in ("home", "a", "b")]
        zs = [wps[k]["z"] for k in ("home", "a", "b")]
        z_lo = min(zs) - 3.0
        frames = {wps[k].get("_frame", "world") for k in ("home", "a", "b")}
        if len(frames) != 1:
            raise ValueError(f"waypoints were taught in different frames {frames}; re-teach all three")
        if table_z is not None and frames == {"world"}:
            pointing_down = all(wps[k]["o_z"] < -0.7 for k in ("home", "a", "b"))
            z_lo = max(z_lo, table_z + table_clearance_mm + (tool_length_mm if pointing_down else 0.0))
        # Moves between human-taught spots (and their approach points) are allowed; nothing much longer than that.
        pts = [(wps[k]["x"], wps[k]["y"], wps[k]["z"] + (approach_mm if k != "home" else 0)) for k in ("home", "a", "b")]
        longest = max(math.dist(p, q) for p in pts for q in pts)
        return cls(x=(min(xs) - margin_mm, max(xs) + margin_mm), y=(min(ys) - margin_mm, max(ys) + margin_mm),
                   z=(z_lo, max(zs) + approach_mm + margin_mm), max_step_mm=max(150.0, longest + 60.0))

    def check(self, target: dict[str, float], current: dict[str, float] | None) -> list[str]:
        problems = []
        for axis in ("x", "y", "z"):
            lo, hi = getattr(self, axis)
            if not lo <= target[axis] <= hi:
                problems.append(f"{axis}={target[axis]:.1f} outside [{lo:.1f}, {hi:.1f}]")
        if current is not None:
            d = math.dist([target[k] for k in "xyz"], [current[k] for k in "xyz"])
            if d > self.max_step_mm:
                problems.append(f"step {d:.0f} mm > {self.max_step_mm:.0f} mm")
            dt = abs((target["theta"] - current["theta"] + 180) % 360 - 180)
            if dt > self.max_theta_change_deg:
                problems.append(f"orientation change {dt:.0f}° > {self.max_theta_change_deg:.0f}°")
        return problems


@dataclass
class TrialConfig:
    """One 'policy' for the scripted pick-and-place. Interventions are changes to these fields."""

    name: str = "baseline"
    approach_mm: float = 80.0       # height above a spot for approach/retreat
    grasp_z_offset_mm: float = 0.0  # + = grasp higher than the taught pose, - = lower (bounded by the envelope)
    settle_s: float = 0.3           # pause after reaching the grasp pose before closing
    move_timeout_s: float = 20.0    # per-move budget; exceeding it is a TIMEOUT failure
    trials: int = 6
    use_motion_service: bool = True

    def label(self) -> str:
        return f"{self.name}(approach={self.approach_mm:g},dz={self.grasp_z_offset_mm:g},settle={self.settle_s:g})"


@dataclass
class TrialResult:
    index: int
    source: str
    dest: str
    outcome: str = "unknown"
    category: str | None = None
    grab_reported: bool | None = None
    holding_after_lift: bool | None = None
    grip_position: float | None = None  # finger position after the grab (G2: -2 closed on nothing, 840 open)
    steps: list[dict[str, Any]] = field(default_factory=list)
    started: float = 0.0
    ended: float = 0.0
    error: str | None = None


class SafetyError(RuntimeError):
    pass


async def timed(coro, timeout_s: float):
    """Like asyncio.wait_for, but never waits for the cancelled call to acknowledge: a broken WebRTC data channel
    can swallow the cancellation and wait_for would then hang forever (seen on sfhack1)."""
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=timeout_s)
    if not done:
        task.cancel()
        raise asyncio.TimeoutError(f"call exceeded {timeout_s:g} s")
    return task.result()


class Controller:
    """Drives arm/gripper (or fakes in tests) through the safety checks. All motion goes through `_move`."""

    def __init__(self, arm, gripper, motion=None, arm_resource=None, execute: bool = False, envelope: Envelope | None = None,
                 stop_event: threading.Event | None = None, log=print, frame: str = "world", direct_vertical: bool = False):
        self.arm, self.gripper, self.motion, self.arm_resource = arm, gripper, motion, arm_resource
        # direct_vertical: purely vertical moves (same x, y, orientation) go to the arm as a straight Cartesian move instead
        # of the motion planner, which models the gripper claws as touching the table at grasp height. The envelope floor
        # still applies, and it is only enabled when the arm's base frame coincides with the world frame.
        self.direct_vertical = direct_vertical
        self.constraints = None  # optional viam motion Constraints (e.g. allowed frame collisions) for planned moves
        self.open_position = None  # if set (0-850), "open" is a partial open via the gripper's DoCommand {"set": ...}
        # speed profile: fast when high above the work surface, slow near it (set per move through the arm's DoCommand)
        self.speed_profile = None  # {"surface_z": float, "slow_zone_mm": 120, "fast": 30.0, "slow": 8.0}
        self._speed_set = None
        # "world": poses read and commanded through the motion service (collision-aware, world frame);
        # "base": arm-frame poses via the arm API (used only when no motion service is available).
        self.frame = frame if motion is not None else "base"
        self.execute, self.env, self.stop_event, self.log = execute, envelope, stop_event or threading.Event(), log
        self.actions: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self._planned: dict[str, float] | None = None  # dry run: where the arm would be after the last planned move
        self.snap = None  # optional async (tag, trial, depth) camera hook, set by run_session
        self.refine = None  # optional async (src_pose) -> (dx, dy) | None: camera-based grasp centring before the descent
        # optional async (trial, src_pose, dst_pose) -> new dst pose | None: look at the lifted piece and re-route it (chess
        # arrangement: a piece found to be something else goes to that piece's home instead)
        self.inspect = None
        # optional async (label) -> None: looks under the gripper before it goes down; waits for or raises on a hand
        self.guard = None

    async def pose(self) -> dict[str, float]:
        if not self.execute and self._planned is not None:
            return dict(self._planned)
        if self.frame == "world":
            pif = await timed(self.motion.get_pose(component_name=self.arm_resource, destination_frame="world"), 8)
            p = pif.pose
        else:
            p = await timed(self.arm.get_end_position(), 8)
        return {k: float(getattr(p, k)) for k in POSE_KEYS}

    def _check_stop(self):
        if self.stop_event.is_set():
            raise SafetyError("stop requested")

    async def stop(self):
        self.stop_event.set()
        if self.execute:
            for dev in (self.arm, self.gripper):
                try:
                    await timed(dev.stop(), 3)
                except Exception:
                    pass

    async def _move(self, target: dict[str, float], label: str, timeout_s: float) -> float:
        self._check_stop()
        cur = await self.pose()
        dt = (target["theta"] - cur["theta"] + 180.0) % 360.0 - 180.0
        surface = (self.speed_profile or {}).get("surface_z")
        high = surface is None or cur["z"] - surface > 100.0
        if self.env and abs(dt) > 90.0 and high and not label.endswith("_halfturn"):
            # a big wrist turn is split: half in place first (only when high above the pieces), then the move
            await self._move({**cur, "theta": cur["theta"] + dt / 2.0}, label + "_halfturn", timeout_s)
            cur = await self.pose()
        problems = self.env.check(target, cur) if self.env else ["no envelope (teach waypoints first)"]
        rec = {"t": time.time(), "kind": "move", "label": label, "target": {k: round(target[k], 2) for k in POSE_KEYS},
               "from": {k: round(cur[k], 2) for k in "xyz"}, "executed": False, "refused": problems or None}
        self.actions.append(rec)
        if problems:
            self.events.append({"timestamp": rec["t"], "event_type": "fault", "event_class": "software_fault",
                                "code": "SAFETY_REFUSAL", "message": f"{label}: " + "; ".join(problems)})
            raise SafetyError(f"refused {label}: " + "; ".join(problems))
        if not self.execute:
            self._planned = dict(target)
            return 0.0
        from viam.proto.common import Pose, PoseInFrame

        t0 = time.time()
        if self.speed_profile:
            prof = self.speed_profile
            low = min(cur["z"], target["z"]) - prof["surface_z"] < prof.get("slow_zone_mm", 120.0)
            want = prof.get("slow", 8.0) if low else prof.get("fast", 30.0)
            if want != self._speed_set:
                try:
                    await timed(self.arm.do_command({"set_speed": float(want)}), 5)
                    self._speed_set = want
                except Exception:
                    pass
            rec["speed_deg_s"] = want
        dest = Pose(**{k: target[k] for k in POSE_KEYS})
        vertical = self.direct_vertical == "all" or (self.direct_vertical and abs(target["x"] - cur["x"]) < 2 and abs(target["y"] - cur["y"]) < 2
                                                     and abs((target["theta"] - cur["theta"] + 180) % 360 - 180) < 2 and cur["o_z"] < -0.95)
        rec["planner"] = "arm.move_to_position" if (vertical or self.frame != "world") else "motion.move"
        if self.frame == "world" and not vertical:
            await timed(self.motion.move(component_name=self.arm_resource, destination=PoseInFrame(reference_frame="world", pose=dest),
                                         constraints=self.constraints), timeout_s)
        else:
            await timed(self.arm.move_to_position(dest), timeout_s)
        rec["executed"] = True
        rec["duration_s"] = round(time.time() - t0, 3)
        self._check_stop()
        return rec["duration_s"]

    slow_zone_mm = 40.0   # the last part of a descent is done in small steps
    slow_step_mm = 5.0

    async def _descend(self, target: dict[str, float], label: str, timeout_s: float) -> None:
        """Descend to `target`: one move to `slow_zone_mm` above it, then `slow_step_mm` steps (gentle near the pieces)."""
        cur = await self.pose()
        if not self.execute or cur["z"] - target["z"] <= self.slow_zone_mm:
            await self._move(target, label, timeout_s)
            return
        await self._move({**target, "z": target["z"] + self.slow_zone_mm}, label + "_fast", timeout_s)
        z = target["z"] + self.slow_zone_mm
        k = 0
        while z - target["z"] > 0.5:
            z = max(target["z"], z - self.slow_step_mm)
            k += 1
            await self._move({**target, "z": z}, f"{label}_step{k}", timeout_s)

    async def _gripper(self, op: str) -> Any:
        self._check_stop()
        rec = {"t": time.time(), "kind": "gripper", "label": op, "executed": False}
        self.actions.append(rec)
        if not self.execute:
            return None
        if op == "open" and self.open_position is not None:
            r = await timed(self.gripper.do_command({"set": float(self.open_position)}), 15)
            r = None
        else:
            r = await timed(getattr(self.gripper, op)(), 15)
        rec["executed"] = True
        rec["result"] = r if isinstance(r, bool) else None
        return r

    empty_grip_below = 60.0  # finger position below this after a grab: the fingers met nothing (G2 reads -2 fully closed)

    async def grip_position(self) -> float | None:
        if not self.execute:
            return None
        try:
            r = await timed(self.gripper.is_holding_something(), 5)
            pos = (getattr(r, "meta", None) or {}).get("position")
            return None if pos is None else float(pos)
        except Exception:
            return None

    async def holding(self) -> bool | None:
        if not self.execute:
            return None
        try:
            r = await timed(self.gripper.is_holding_something(), 5)
            return bool(getattr(r, "is_holding_something", r))
        except Exception:
            return None

    async def trial(self, i: int, src: dict, dst: dict, cfg: TrialConfig, names: tuple[str, str]) -> TrialResult:
        res = TrialResult(index=i, source=names[0], dest=names[1], started=time.time())
        above = lambda p: {**p, "z": p["z"] + cfg.approach_mm}  # noqa: E731
        grasp = {**src, "z": src["z"] + cfg.grasp_z_offset_mm}
        place = {**dst, "z": dst["z"] + cfg.grasp_z_offset_mm}
        n0 = len(self.actions)
        try:
            await self._gripper("open")
            await self._move(above(src), "approach_source", cfg.move_timeout_s)
            if self.snap and self.execute:
                await self.snap("pre_grasp", i, True)
            if self.refine and self.execute:  # chess: look down from here and centre the descent on the piece
                shift = await self.refine(src)
                if shift:
                    grasp = {**grasp, "x": grasp["x"] + shift[0], "y": grasp["y"] + shift[1]}
                    src = {**src, "x": src["x"] + shift[0], "y": src["y"] + shift[1]}
                    await self._move(above(src), "approach_source_refined", cfg.move_timeout_s)
            if self.guard and self.execute:
                await self.guard("grasp")
            await self._descend(grasp, "descend_grasp", cfg.move_timeout_s)
            await asyncio.sleep(cfg.settle_s if self.execute else 0)
            res.grab_reported = await self._gripper("grab")
            res.grip_position = await self.grip_position()
            empty = res.grab_reported is False or (res.grip_position is not None and res.grip_position < self.empty_grip_below)
            if self.execute and empty:  # nothing between the fingers: never carry air to the destination
                await self._gripper("open")
                await self._move(above(src), "retreat_empty", cfg.move_timeout_s)
                res.outcome, res.category = "failure", "GRASP_FAILURE"
                res.error = f"the fingers closed on nothing (position {res.grip_position})"
                res.ended, res.steps = time.time(), self.actions[n0:]
                return res
            await self._move(above(src), "lift", cfg.move_timeout_s)
            res.holding_after_lift = await self.holding()
            pos_lift = await self.grip_position()
            if pos_lift is not None:  # the finger gap is the reliable signal; the holding flag has said "yes" on air
                res.holding_after_lift = pos_lift >= self.empty_grip_below
            if self.execute and res.holding_after_lift is False:  # it slipped out during the lift: stop here
                await self._gripper("open")
                res.outcome, res.category = "failure", "SLIPPAGE"
                res.error = f"the piece slipped out during the lift (position {pos_lift})"
                res.ended, res.steps = time.time(), self.actions[n0:]
                return res
            if self.snap and self.execute:
                await self.snap("after_lift", i)
            if self.inspect and self.execute and res.holding_after_lift is not False:
                new_dst = await self.inspect(i, src, dst)
                if new_dst:
                    dst = {k: v for k, v in new_dst.items() if not k.startswith("_")}
                    place = {**dst, "z": dst["z"] + cfg.grasp_z_offset_mm}
                    res.dest = new_dst.get("_name", res.dest)
            await self._move(above(dst), "transport", cfg.move_timeout_s)
            if self.guard and self.execute:
                await self.guard("place")
            await self._descend(place, "lower_place", cfg.move_timeout_s)
            await self._gripper("open")
            await self._move(above(dst), "retreat", cfg.move_timeout_s)
            if not self.execute:
                res.outcome, res.category = "unknown", None
            elif res.grab_reported is False:
                res.outcome, res.category = "failure", "GRASP_FAILURE"
            elif res.holding_after_lift is False:
                res.outcome, res.category = "failure", "SLIPPAGE"
            else:
                res.outcome = "success"
        except asyncio.TimeoutError:
            res.outcome, res.category, res.error = "timeout", "TIMEOUT", "move exceeded its time budget"
        except SafetyError as exc:
            res.outcome, res.category, res.error = "invalid", "SAFETY_STOP", str(exc)
        except Exception as exc:
            if self.stop_event.is_set():  # a move interrupted by STOP surfaces as an SDK error
                res.outcome, res.category, res.error = "invalid", "SAFETY_STOP", f"stopped during motion ({type(exc).__name__})"
            else:
                res.outcome, res.category, res.error = "invalid", "INFRASTRUCTURE_ERROR", f"{type(exc).__name__}: {str(exc)[:200]}"
        res.ended = time.time()
        res.steps = self.actions[n0:]
        return res


async def teach(name: str) -> dict[str, Any]:
    """READ-ONLY: record the arm's current pose as waypoint `name` (a human jogs the arm there first)."""
    from viam.components.arm import Arm
    from viam.robot.client import RobotClient
    from viam.services.motion import MotionClient

    from amperes.integrations.viam_live import _env
    import os

    address = _env()
    opts = client_options()
    m = await asyncio.wait_for(RobotClient.at_address(address, opts), 20)
    try:
        arm = Arm.from_robot(m, "arm")
        base = await arm.get_end_position()
        base_d = {k: float(getattr(base, k)) for k in POSE_KEYS}
        world_d, frame = None, "base"
        try:
            pif = await MotionClient.from_robot(m, "builtin").get_pose(component_name="arm", destination_frame="world")
            world_d, frame = {k: float(getattr(pif.pose, k)) for k in POSE_KEYS}, "world"
        except Exception:
            pass
        jp = await arm.get_joint_positions()
        pose = world_d or base_d
        saved = save_waypoint(name, pose, {"_frame": frame, "_base_pose": base_d, "_joints": list(jp.values),
                                           "_world_minus_base_mm": [round(world_d[k] - base_d[k], 2) for k in "xyz"] if world_d else None})
        return {"name": name, "frame": frame, **{k: saved[k] for k in POSE_KEYS}, "world_minus_base_mm": saved["_world_minus_base_mm"]}
    finally:
        await m.close()


# ------------------------------------------------------------------------------------------------ live session
@dataclass
class RunSpec:
    config: TrialConfig
    execute: bool = False
    # A/B: when set, trials cycle through these configs in pairs (a→b, b→a) so direction and drift hit every arm equally.
    configs: list[TrialConfig] | None = None
    operator_ack: str = ""
    table_z: float | None = -23.0  # table box centre z=-123 mm, 200 mm thick -> top surface at -23 mm (frame system)
    object_name: str = "object"
    camera_names: tuple[str, ...] = ("cam", "cam2")


def required_ack(machine: str) -> str:
    return f"I am at {machine} with the e-stop in reach"


WATCHDOG_S = 10.0  # seconds without telemetry during motion before the arm is stopped (link hiccups; collision detection is on)


def client_options():
    """RobotClient options used by every connection here. The SDK's own connection check tore the link down and its
    session heartbeat did not keep actuator sessions alive (direct gripper/arm calls failed with SESSION_EXPIRED), so
    both are off: liveness is our telemetry watchdog, and STOP is explicit."""
    import os

    from viam.robot.client import RobotClient

    opts = RobotClient.Options.with_api_key(api_key=os.environ["VIAM_API_KEY"], api_key_id=os.environ["VIAM_API_KEY_ID"])
    opts.check_connection_interval = 0
    opts.attempt_reconnect_interval = 0
    opts.disable_sessions = True
    return opts


async def dial_robot(address: str, opts, what: str, log=print, attempts: int = 3):
    """One dial at a time with retries: parallel dials through the cloud proxy fail with DeadlineExceeded."""
    from viam.robot.client import RobotClient

    from viam.components.arm import Arm

    last = None
    for attempt in range(attempts):
        try:
            c = await asyncio.wait_for(RobotClient.at_address(address, opts), 40)
            try:  # a dial can "succeed" with a dead WebRTC data channel: prove it with a real read first
                await timed(c.refresh(), 10)  # the resource list can lag a module restart (gripper/camera missing)
                await timed(Arm.from_robot(c, "arm").get_end_position(), 8)
            except Exception as exc:
                asyncio.ensure_future(c.close())
                raise ConnectionError(f"connection unusable ({type(exc).__name__})") from exc
            log(f"connected ({what}, attempt {attempt + 1})")
            return c
        except Exception as exc:
            last = exc
            log(f"dial failed ({what}, attempt {attempt + 1}): {type(exc).__name__}")
            await asyncio.sleep(2)
    raise ConnectionError(f"could not connect ({what}) after {attempts} attempts: {last}")


async def run_session(spec: RunSpec, live=None, stop_event: threading.Event | None = None, log=print) -> dict[str, Any]:
    """Connect, validate, run N alternating A→B / B→A trials, record telemetry + images, return raw session data."""
    from viam.components.arm import Arm
    from viam.components.camera import Camera
    from viam.components.gripper import Gripper
    from viam.robot.client import RobotClient
    from viam.services.motion import MotionClient

    from amperes.integrations.viam_live import _env

    address = _env()
    import os

    machine_name = address.split(".")[0]
    if spec.execute and spec.operator_ack.strip() != required_ack(machine_name):
        raise SafetyError(f"motion needs the operator acknowledgement exactly: '{required_ack(machine_name)}'")
    wps = load_waypoints()
    arms = spec.configs or [spec.config]
    env = Envelope.from_waypoints(wps, max(c.approach_mm for c in arms), spec.table_z)
    total = spec.config.trials if not spec.configs else sum(c.trials for c in arms)
    opts = client_options()
    stop_event = stop_event or threading.Event()
    m = await dial_robot(address, opts, "control", log)
    tm = await dial_robot(address, opts, "telemetry", log)  # separate telemetry/watchdog connection
    session = {"machine": machine_name, "started": time.time(), "config": asdict(spec.config), "execute": spec.execute,
               "object_name": spec.object_name,
               "envelope": asdict(env), "waypoints": wps, "telemetry": [], "images": [], "trials": [], "events": []}
    try:
        arm, grip = Arm.from_robot(m, "arm"), Gripper.from_robot(m, "gripper")
        motion = None
        if spec.config.use_motion_service:
            try:
                motion = MotionClient.from_robot(m, "builtin")
            except Exception:
                motion = None
        frame = next(iter({wps[k].get("_frame", "world") for k in ("home", "a", "b")}))
        if frame == "world" and motion is None:
            raise SafetyError("waypoints were taught in the world frame but the motion service is unavailable; re-teach")
        ctl = Controller(arm, grip, motion if frame == "world" else None, "arm", spec.execute, env,
                         stop_event, log, frame=frame)
        t_arm = Arm.from_robot(tm, "arm")
        cams = []
        for cn in spec.camera_names:
            try:
                cams.append((cn, Camera.from_robot(tm, cn)))
            except Exception:
                pass
        watchdog = {"last_ok": time.time(), "fails": 0, "halted": False}
        done = asyncio.Event()

        async def telemetry():
            # Runs for the whole session. Also the STOP path: a stop request (UI button / Ctrl-C / watchdog) halts the
            # arm immediately, even in the middle of a move, instead of waiting for the current move to finish.
            while not done.is_set():
                if stop_event.is_set() and not watchdog["halted"]:
                    watchdog["halted"] = True
                    await ctl.stop()
                t0 = time.time()
                try:
                    jp, p = await asyncio.wait_for(asyncio.gather(t_arm.get_joint_positions(), t_arm.get_end_position()), 2.5)
                    session["telemetry"].append({"t": round(t0, 3), "joints": list(jp.values),
                                                 "pose": [getattr(p, k) for k in POSE_KEYS]})
                    watchdog["last_ok"], watchdog["fails"] = time.time(), 0
                except Exception:
                    watchdog["fails"] += 1
                    session["telemetry"].append({"t": round(t0, 3), "joints": None, "pose": None})
                if spec.execute and time.time() - watchdog["last_ok"] > WATCHDOG_S and not watchdog["halted"]:
                    session["events"].append({"timestamp": time.time(), "event_type": "fault", "event_class": "infrastructure_failure",
                                              "code": "WATCHDOG_TELEMETRY_LOST", "message": f"no telemetry for {WATCHDOG_S:g} s during motion; stopping"})
                    watchdog["halted"] = True
                    await ctl.stop()
                await asyncio.sleep(max(0.0, 0.33 - (time.time() - t0)))

        async def snap(tag: str, trial: int, depth: bool = False):
            # Privacy + usefulness: only photograph the work area, i.e. when the tool points down (wrist camera).
            try:
                cur = await ctl.pose()
                if cur["o_z"] > -0.7:
                    return
            except Exception:
                return
            for cn, cam in cams:
                try:
                    r = await asyncio.wait_for(cam.get_images(), 6)
                    imgs = r[0] if isinstance(r, tuple) else r
                    for im in imgs:
                        is_depth = "dep" in str(im.mime_type)
                        if is_depth and not depth:
                            continue
                        session["images"].append({"t": time.time(), "camera": cn, "tag": tag + ("_depth" if is_depth else ""),
                                                  "trial": trial, "mime": str(im.mime_type), "data": im.data})
                except Exception:
                    session["events"].append({"timestamp": time.time(), "event_type": "recording_error",
                                              "event_class": "data_recording_failure", "code": "CAMERA_NO_FRAME",
                                              "message": f"{cn} returned no frame ({tag}, trial {trial})"})

        tele = asyncio.create_task(telemetry())
        names = [("a", "b"), ("b", "a")]
        if live:
            live.update(running=True, phase="moving to home" if spec.execute else "dry run",
                        config=" vs ".join(c.label() for c in arms), execute=spec.execute, trials_done=0, trials_total=total, results=[])
        try:
            await ctl._move({**wps["home"]}, "home", spec.config.move_timeout_s)
            for i in range(total):
                if stop_event.is_set():
                    break
                s, d = names[i % 2]
                cfg_i = arms[(i // 2) % len(arms)]
                ctl.snap = snap
                r = await ctl.trial(i, wps[s], wps[d], cfg_i, (s, d))
                await snap("after_place", i)
                session["trials"].append({**asdict(r), "config": asdict(cfg_i)})
                log(f"trial {i}: {s}->{d} {r.outcome} {r.category or ''} grab={r.grab_reported} holding={r.holding_after_lift} {r.error or ''}")
                if live:
                    live.update(trials_done=i + 1, results=[{**{k: t[k] for k in ("index", "source", "dest", "outcome", "category", "error")},
                                                             "config": t["config"]["name"]}
                                                            for t in session["trials"]], phase=f"trial {i + 1}/{spec.config.trials}")
                if r.category in ("SAFETY_STOP", "INFRASTRUCTURE_ERROR"):
                    break
            if not stop_event.is_set():
                await ctl._move({**wps["home"]}, "home", spec.config.move_timeout_s)
        finally:
            done.set()
            await asyncio.sleep(0)
            tele.cancel()
            session["events"] += ctl.events
            session["actions"] = ctl.actions
    finally:
        session["ended"] = time.time()
        for c in (m, tm):
            try:
                await c.close()
            except Exception:
                pass
    return session


def session_to_canonical(session: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """Each trial → one canonical episode: commanded actions, observed telemetry, gripper/camera evidence, outcome."""
    from amperes.schema import Episode, Event, Outcome, Ref, RobotInfo, SessionInfo, SourceInfo, Stream, episode_to_json

    out_dir.mkdir(parents=True, exist_ok=True)
    img_dir = out_dir / "images"
    img_dir.mkdir(exist_ok=True)
    sid = f"robot-{session['machine']}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(session['started']))}"
    eps = []
    for tr in session["trials"]:
        cfg = tr.get("config") or session["config"]
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
                                      units="mm, orientation-vector, deg", source="amperes scripted pick-place"),
            "act.gripper": Stream(kind="action", t=[a["t"] for a in grips], values=[[1.0 if a["label"] == "grab" else 0.0] for a in grips],
                                  valid_range=[0.0, 1.0], source="amperes scripted pick-place (1=grab, 0=open)"),
        }
        evidence = {"gripper_grab_reported": tr["grab_reported"], "gripper_holding_after_lift": tr["holding_after_lift"]}
        crit = {k: v for k, v in (("grab_reported", tr["grab_reported"]), ("holding_after_lift", tr["holding_after_lift"])) if v is not None}
        if tr["outcome"] in ("success", "failure") and crit:
            evidence["criteria"] = crit
        events = [Event(t=a["t"], type="fault", code="SAFETY_REFUSAL", message="; ".join(a["refused"]), source="amperes safety")
                  for a in tr["steps"] if a.get("refused")]
        events += [Event(t=x["timestamp"], type=x["event_type"] if x["event_type"] in ("fault", "recording_error") else "annotation",
                         code=x["code"], message=x["message"], source="amperes robot session")
                   for x in session["events"] if s <= x["timestamp"] <= e and x["code"] != "SAFETY_REFUSAL"]
        if tr["error"] and tr["category"] == "INFRASTRUCTURE_ERROR":
            events.append(Event(t=e, type="error", code="SDK_ERROR", message=tr["error"], source="amperes robot session"))
        for im in session["images"]:
            if im["trial"] == tr["index"]:
                ext = "jpg" if "jpeg" in im["mime"] else "dep" if "dep" in im["mime"] else "png"
                fn = f"{eid}_{im['camera']}_{im['tag']}.{ext}"
                (img_dir / fn).write_bytes(im["data"])
                events.append(Event(t=im["t"], type="annotation", code="CAMERA_SNAPSHOT", message=fn, source=im["camera"]))
        ep = Episode(
            episode_id=eid,
            source=SourceInfo(dataset_id="robot", adapter="amperes.viam_control@1.0.0", source_uri=f"viam://{session['machine']}",
                              source_record_id=eid, synthetic=False,
                              synthetic_note=None if session["execute"] else "DRY RUN: no motion was executed"),
            task=Ref(id="pick_place_real", version="1.0.0"), policy=Ref(id="scripted_pick_place_real", version=TrialConfig(**cfg).label()),
            robot=RobotInfo(embodiment="6-joint arm + parallel gripper (event-provided)", device_id="arm", config_id=cfg["name"]),
            session=SessionInfo(session_id=sid, workcell_id=session["machine"], operator_id="op-onsite"),
            start_time=s, end_time=e, time_base="unix_s", streams=streams,
            outcome=Outcome(label=tr["outcome"], provenance="amperes_verifier" if session["execute"] else "unknown",
                            verified=bool(session["execute"] and crit), reason=tr["category"] or tr["error"],
                            verifier_id="gripper_signals", verifier_version="1.0.0", measurements=evidence),
            events=events, conditions={"trial": tr["index"], "source": tr["source"], "dest": tr["dest"],
                                       "attempt_index": 1, "config": cfg, "dry_run": not session["execute"],
                                       "object": session.get("object_name", "object"),
                                       "instruction": f"Pick up the {session.get('object_name', 'object')} at spot {tr['source'].upper()} "
                                                      f"and place it at spot {tr['dest'].upper()}."},
            extra={"error": tr["error"], "move_durations_s": [a.get("duration_s") for a in moves]},
        )
        eps.append(ep)
    with open(out_dir / "episodes.jsonl", "w") as fh:
        for ep in eps:
            fh.write(episode_to_json(ep) + "\n")
    evs = [{"timestamp": session["started"], "event_type": "session_start", "workcell_id": session["machine"], "session_id": sid},
           {"timestamp": session["ended"], "event_type": "session_end", "workcell_id": session["machine"], "session_id": sid}]
    evs += [{**{k: x[k] for k in ("event_type", "event_class", "code", "message")}, "timestamp": x["timestamp"],
             "workcell_id": session["machine"], "session_id": sid, "device_id": "arm"}
            for x in session["events"] if x["event_type"] in ("fault", "recording_error")]
    with open(out_dir / "events.jsonl", "w") as fh:
        for x in evs:
            fh.write(json.dumps(x) + "\n")
    by_cfg: dict[str, dict[str, int]] = {}
    for t in session["trials"]:
        name = (t.get("config") or session["config"])["name"]
        by_cfg.setdefault(name, {}).setdefault(t["outcome"], 0)
        by_cfg[name][t["outcome"]] += 1
    summary = {"session_id": sid, "configs": sorted(by_cfg), "executed": session["execute"],
               "trials": len(session["trials"]), "outcomes": {o: sum(t["outcome"] == o for t in session["trials"])
                                                             for o in {t["outcome"] for t in session["trials"]}},
               "by_config": by_cfg,
               "images": len(session["images"]), "events": len(session["events"]), "out_dir": str(out_dir)}
    (out_dir / "session_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def ab_summary(trials: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-config success counts with Wilson 95% intervals. Small samples are labelled as such; no p-value theatre."""
    from amperes.stats import wilson

    out: dict[str, Any] = {}
    for t in trials:
        name = (t.get("config") or {}).get("name", "baseline")
        d = out.setdefault(name, {"n": 0, "success": 0, "failure": 0, "timeout": 0, "invalid": 0, "categories": {}})
        d["n"] += 1
        d[t["outcome"]] = d.get(t["outcome"], 0) + 1
        if t.get("category"):
            d["categories"][t["category"]] = d["categories"].get(t["category"], 0) + 1
    for d in out.values():
        valid = d["success"] + d["failure"] + d["timeout"]
        d["success_rate"] = d["success"] / valid if valid else None
        d["ci95"] = list(wilson(d["success"], valid)) if valid else None
        d["note"] = "small sample: directional evidence only" if valid < 20 else ""
    return out


def run_and_import(spec: RunSpec, out_dir: Path, live=None, stop_event: threading.Event | None = None, log=print) -> dict[str, Any]:
    from amperes import incidents
    from amperes.ingest.pipeline import import_dataset

    session = asyncio.run(run_session(spec, live, stop_event, log))
    summary = session_to_canonical(session, out_dir)
    summary["ab"] = ab_summary(session["trials"])
    if session["trials"]:
        r = import_dataset(out_dir, name=f"{'ROBOT' if spec.execute else 'DRY-RUN'} pick-place {summary['session_id']}", adapter="jsonl",
                           synthetic=not spec.execute, note="real arm trials" if spec.execute else "dry run: plan validated, no motion",
                           ctx={"required_metadata": ["task.id"]})
        summary.update(dataset_id=r["dataset_id"], incidents=len(incidents.build_incidents(r["dataset_id"])),
                       quality=r["quality"]["status_counts"])
    (out_dir / "session_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    if live:
        live.update(running=False, done=True, summary=summary)
    return summary
