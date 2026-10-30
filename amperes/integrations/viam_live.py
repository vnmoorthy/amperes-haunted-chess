"""Read-only Viam telemetry recorder → canonical Amperes episodes.

SAFETY: this module never commands a robot. Components are wrapped in `ReadOnly`, which only forwards the methods in
`ALLOWED_METHODS` (reads and status). Anything else raises PermissionError before reaching the SDK. There is no
`do_command`, no motion, no gripper open/grab, no stop. `tests/test_viam_live.py` checks this statically.

What it records (wall-clock Unix time):
- obs.joint_pos (deg), obs.ee_pose (mm / orientation vector + theta), obs.arm_is_moving, obs.gripper_is_moving
- obs.camera_frame = [byte size, 32-bit content hash] probed every `camera_every_s` (identical hashes = frozen feed)
- telemetry.read_latency_ms per tick
- events: session_start/end, recording_error (failed reads / camera timeouts), fault (resource not READY),
  station_down/up (telemetry link lost / restored)

Episodes are segmented from motion (arm `is_moving` or joint change); with no motion, fixed observation windows are
used. Outcomes are `unknown`: read-only monitoring cannot see task success. Commanded actions are not visible either,
so imports disable C003 (no action stream) via the rule settings below.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ALLOWED_METHODS = frozenset({"get_joint_positions", "get_end_position", "is_moving", "get_images", "get_machine_status"})
# Read-only monitoring structurally has no action stream and no task outcome; flagging those on every episode would be
# noise, so they are disabled for these imports (and recorded as rule settings in the import report).
IMPORT_RULE_SETTINGS = {"disabled_rules": ["C003_no_actions", "C005_missing_outcome"], "required_metadata": ["task.id"]}
RESOURCE_STATE = {0: "UNSPECIFIED", 1: "UNCONFIGURED", 2: "CONFIGURING", 3: "READY", 4: "REMOVING", 5: "UNHEALTHY"}


class ReadOnly:
    """Forwards only allow-listed read methods to a Viam component or client."""

    def __init__(self, target: Any, label: str):
        object.__setattr__(self, "_t", target)
        object.__setattr__(self, "_label", label)

    def __getattr__(self, name: str):
        if name not in ALLOWED_METHODS:
            raise PermissionError(f"read-only Viam adapter: '{self._label}.{name}' is not an allowed read method")
        return getattr(object.__getattribute__(self, "_t"), name)

    def __setattr__(self, name, value):
        raise PermissionError("read-only wrapper is immutable")


@dataclass
class RecorderConfig:
    seconds: float = 60.0
    rate_hz: float = 3.0  # measured: the cloud-relayed link sustained ~3.1 Hz for 3 concurrent reads
    read_timeout_s: float = 1.0
    camera_every_s: float = 2.0
    camera_timeout_s: float = 3.0
    status_every_s: float = 5.0
    arm: str = "arm"
    gripper: str | None = "gripper"
    camera: str | None = "cam"
    motion_threshold_deg: float = 0.3
    pad_s: float = 1.0
    merge_gap_s: float = 2.0
    min_episode_s: float = 1.0
    idle_window_s: float = 20.0
    link_down_after: int = 5  # consecutive failed ticks before declaring the telemetry link down


@dataclass
class Recording:
    machine: str
    started: float
    ended: float = 0.0
    ticks: list[dict[str, Any]] = field(default_factory=list)
    camera: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


def _env():
    # Fresh read of .env each capture (overrides stale values a long-running server may hold).
    try:
        from dotenv import dotenv_values

        for k, v in dotenv_values(Path(__file__).resolve().parents[2] / ".env").items():
            if k.startswith("VIAM_") and v:
                os.environ[k] = v
    except ImportError:
        pass
    missing = [k for k in ("VIAM_API_KEY", "VIAM_API_KEY_ID", "VIAM_MACHINE_ADDRESS") if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"missing {missing} in environment/.env")
    return os.environ["VIAM_MACHINE_ADDRESS"]


def _h32(b: bytes) -> int:
    return int(hashlib.sha256(b).hexdigest()[:8], 16)


class LiveStatus:
    """Thread-safe live view for the UI; written to <out>/live.json every second."""

    def __init__(self, path: Path | None):
        self.path = path
        self.lock = threading.Lock()
        self.d: dict[str, Any] = {"running": False}

    def update(self, **kw):
        with self.lock:
            self.d.update(kw)
            if self.path:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.d, default=str))
                tmp.replace(self.path)

    def snapshot(self):
        with self.lock:
            return dict(self.d)


async def record(cfg: RecorderConfig, live: LiveStatus | None = None, stop: threading.Event | None = None) -> Recording:
    from viam.components.arm import Arm
    from viam.components.camera import Camera
    from viam.components.gripper import Gripper
    from viam.robot.client import RobotClient

    address = _env()
    live = live or LiveStatus(None)
    stop = stop or threading.Event()
    opts = RobotClient.Options.with_api_key(api_key=os.environ["VIAM_API_KEY"], api_key_id=os.environ["VIAM_API_KEY_ID"])
    # The SDK's background health check uses a 1 s timeout; on a slow relayed link it tore the connection down mid-capture
    # (observed: StreamTerminatedError then FileNotFoundError on every read). Disable it and reconnect explicitly below,
    # so each outage is measured as station_down/station_up instead of silently ending the capture.
    opts.check_connection_interval = 0
    opts.attempt_reconnect_interval = 0

    async def dial():
        return await asyncio.wait_for(RobotClient.at_address(address, opts), 15)

    machine = await dial()
    # The camera gets its own connection: in testing, slow/failed frame transfers on a shared connection took down the
    # arm telemetry link (51/59 failed ticks with the camera vs 1/63 without). Isolation keeps one bad sensor from
    # corrupting every other stream.
    cam_machine = await dial() if cfg.camera else None
    rec = Recording(machine=address.split(".")[0], started=time.time())
    wc = rec.machine

    def ev(t, et, cls="", code="", msg="", comp=""):
        rec.events.append({"timestamp": t, "event_type": et, "event_class": cls, "code": code, "message": msg,
                           "workcell_id": wc, "device_id": comp or None, "session_id": session})

    session = f"viam-{wc}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(rec.started))}"
    try:
        robot = ReadOnly(machine, "machine")
        arm = ReadOnly(Arm.from_robot(machine, cfg.arm), cfg.arm)
        grip = ReadOnly(Gripper.from_robot(machine, cfg.gripper), cfg.gripper) if cfg.gripper else None
        cam = ReadOnly(Camera.from_robot(cam_machine, cfg.camera), cfg.camera) if cfg.camera else None
        ev(rec.started, "session_start", msg="read-only Amperes telemetry capture")
        ev(rec.started, "station_up", msg="telemetry link connected")
        period = 1.0 / cfg.rate_hz
        next_status = rec.started
        fails_in_row, link_down, down_since = 0, False, 0.0
        latencies: list[float] = []
        res_state: dict[str, str] = {}
        read_ok = read_fail = 0
        cam_stats = {"ok": 0, "no_frame": 0, "reconnects": 0}
        reconnects = {"arm": 0}

        async def camera_loop():
            nonlocal cam, cam_machine
            misses = 0
            while time.time() - rec.started < cfg.seconds and not stop.is_set():
                if misses >= 3:  # re-dial the camera's own connection after 3 consecutive misses
                    misses = 0
                    try:
                        await cam_machine.close()
                    except Exception:
                        pass
                    try:
                        cam_machine = await dial()
                        cam = ReadOnly(Camera.from_robot(cam_machine, cfg.camera), cfg.camera)
                        cam_stats["reconnects"] += 1
                    except Exception:
                        await asyncio.sleep(1.0)
                        continue
                tc = time.time()
                try:
                    r = await asyncio.wait_for(cam.get_images(), cfg.camera_timeout_s)
                    imgs = r[0] if isinstance(r, tuple) else r
                    data = imgs[0].data if imgs else b""
                    rec.camera.append({"t": round(tc, 4), "bytes": float(len(data)), "hash": float(_h32(data)) if data else None})
                    cam_stats["ok"] += 1
                    misses = 0
                except Exception as exc:
                    misses += 1
                    rec.camera.append({"t": round(tc, 4), "bytes": None, "hash": None})
                    cam_stats["no_frame"] += 1
                    ev(tc, "recording_error", "data_recording_failure", "CAMERA_NO_FRAME",
                       f"camera '{cfg.camera}' returned no frame within {cfg.camera_timeout_s}s ({type(exc).__name__})", cfg.camera)
                await asyncio.sleep(max(0.0, cfg.camera_every_s - (time.time() - tc)))

        cam_task = asyncio.create_task(camera_loop()) if cam is not None else None
        live.update(running=True, session_id=session, machine=wc, started=rec.started, seconds=cfg.seconds,
                    rate_hz=cfg.rate_hz, events=[], joints_recent=[], error=None)

        async def read(coro):
            try:
                return await asyncio.wait_for(coro, cfg.read_timeout_s), None
            except Exception as exc:  # timeouts and transport errors are evidence, not crashes
                return None, type(exc).__name__

        while time.time() - rec.started < cfg.seconds and not stop.is_set():
            t0 = time.time()
            (j, je), (p, pe), (m, me), (gm, ge) = await asyncio.gather(
                read(arm.get_joint_positions()), read(arm.get_end_position()), read(arm.is_moving()),
                read(grip.is_moving()) if grip else asyncio.sleep(0, result=(None, None)))
            lat = (time.time() - t0) * 1000
            errs = [(n, e) for n, e in (("joint_positions", je), ("end_position", pe), ("arm.is_moving", me), ("gripper.is_moving", ge)) if e]
            tick = {"t": round(t0, 4), "joints": list(j.values) if j is not None else None,
                    "pose": [p.x, p.y, p.z, p.o_x, p.o_y, p.o_z, p.theta] if p is not None else None,
                    "arm_moving": None if m is None else float(bool(m)), "grip_moving": None if gm is None else float(bool(gm)),
                    "latency_ms": round(lat, 1)}
            rec.ticks.append(tick)
            latencies.append(lat)
            if errs:
                read_fail += 1
                fails_in_row += 1
                ev(t0, "recording_error", "data_recording_failure", f"VIAM_READ_{errs[0][1].upper()}",
                   "failed reads: " + ", ".join(f"{n} ({e})" for n, e in errs), cfg.arm)
                if fails_in_row >= cfg.link_down_after and not link_down:
                    link_down = True
                    down_since = t0
                    ev(t0, "station_down", msg=f"{fails_in_row} consecutive failed ticks")
                if link_down and fails_in_row % cfg.link_down_after == 0:
                    try:
                        await machine.close()
                    except Exception:
                        pass
                    try:
                        machine = await dial()
                        robot = ReadOnly(machine, "machine")
                        arm = ReadOnly(Arm.from_robot(machine, cfg.arm), cfg.arm)
                        grip = ReadOnly(Gripper.from_robot(machine, cfg.gripper), cfg.gripper) if cfg.gripper else None
                        reconnects["arm"] += 1
                    except Exception:
                        pass
            else:
                read_ok += 1
                if link_down:
                    link_down = False
                    ev(t0, "station_up", msg=f"telemetry link restored after {t0 - down_since:.1f} s")
                fails_in_row = 0
            if t0 >= next_status:
                next_status = t0 + cfg.status_every_s
                st, se = await read(robot.get_machine_status())
                if st is not None:
                    for r_ in st.resources:
                        name = f"{r_.name.subtype}:{r_.name.name}"
                        state = RESOURCE_STATE.get(int(r_.state), str(r_.state))
                        if res_state.get(name) != state and not r_.name.name.startswith("$"):
                            if state != "READY":
                                ev(t0, "fault", "software_fault", f"RESOURCE_{state}", f"{name}: {(r_.error or '')[:200]}", name)
                            elif name in res_state:
                                ev(t0, "recovery", "", f"RESOURCE_{res_state[name]}", f"{name} back to READY", name)
                        res_state[name] = state
            ok_rate = read_ok / max(1, read_ok + read_fail)
            lat_sorted = sorted(latencies[-200:])
            live.update(elapsed=round(time.time() - rec.started, 1), ticks=len(rec.ticks), read_ok=read_ok, read_fail=read_fail,
                        read_success_rate=ok_rate, latency_p50_ms=lat_sorted[len(lat_sorted) // 2],
                        latency_p95_ms=lat_sorted[int(len(lat_sorted) * 0.95) - 1] if len(lat_sorted) > 1 else lat_sorted[0],
                        achieved_hz=len(rec.ticks) / max(1e-6, time.time() - rec.started),
                        reconnects=reconnects["arm"], link_down=link_down,
                        camera={**cam_stats, "last": rec.camera[-1] if rec.camera else None, "enabled": cam is not None},
                        arm_moving=tick["arm_moving"], joints=tick["joints"], pose=tick["pose"],
                        joints_recent=[[x["t"], x["joints"]] for x in rec.ticks[-80:]],
                        events=rec.events[-25:], resources=res_state)
            await asyncio.sleep(max(0.0, period - (time.time() - t0)))
        if cam_task:
            stop.set() if stop.is_set() else None
            try:
                await asyncio.wait_for(cam_task, cfg.camera_timeout_s + 1)
            except Exception:
                cam_task.cancel()
        rec.camera.sort(key=lambda x: x["t"])
        rec.events.sort(key=lambda x: x["timestamp"])
        rec.ended = time.time()
        ev(rec.ended, "session_end", msg="capture finished" if not stop.is_set() else "capture stopped by user")
        return rec
    finally:
        await machine.close()
        if cam_machine is not None:
            await cam_machine.close()


# ------------------------------------------------------------------------------------------------ to canonical episodes
def segment(rec: Recording, cfg: RecorderConfig) -> list[tuple[float, float, str]]:
    ticks = rec.ticks
    moving = []
    prev = None
    for x in ticks:
        mv = bool(x["arm_moving"])
        if x["joints"] is not None and prev is not None:
            mv = mv or max(abs(a - b) for a, b in zip(x["joints"], prev)) > cfg.motion_threshold_deg
        if x["joints"] is not None:
            prev = x["joints"]
        moving.append(mv)
    wins = []
    for x, mv in zip(ticks, moving):
        if mv:
            s, e = x["t"] - cfg.pad_s, x["t"] + cfg.pad_s
            if wins and s - wins[-1][1] <= cfg.merge_gap_s:
                wins[-1][1] = e
            else:
                wins.append([s, e])
    # Windows end at the last sample, not at rec.ended: the connection teardown after the final read is not recording time.
    t_lo, t_hi = rec.started, (ticks[-1]["t"] if ticks else rec.started)
    out = [(max(t_lo, s), min(t_hi, e), "motion") for s, e in wins if e - s >= cfg.min_episode_s]
    if not out:
        t = t_lo
        while t < t_hi - 1e-6:
            end = min(t + cfg.idle_window_s, t_hi)
            if t_hi - end < cfg.idle_window_s / 2:  # fold a short remainder into this window instead of a stub episode
                end = t_hi
            out.append((t, end, "idle_window"))
            t = end
    # never emit an episode with no samples (e.g. the teardown interval after the last read)
    return [w for w in out if any(w[0] <= x["t"] <= w[1] for x in ticks)]


def to_canonical(rec: Recording, cfg: RecorderConfig, out_dir: Path) -> dict[str, Any]:
    """Write episodes.jsonl + events.jsonl (canonical format) and return a summary."""
    from amperes.schema import Episode, Outcome, Ref, RobotInfo, SessionInfo, SourceInfo, Stream, episode_to_json

    out_dir.mkdir(parents=True, exist_ok=True)
    session = next((e["session_id"] for e in rec.events if e["event_type"] == "session_start"), "viam-session")
    episodes = []
    for i, (s, e, kind) in enumerate(segment(rec, cfg)):
        tk = [x for x in rec.ticks if s <= x["t"] <= e]
        cm = [x for x in rec.camera if s <= x["t"] <= e]
        eid = f"{session}-e{i:03d}"
        streams = {
            "obs.joint_pos": Stream(kind="observation", t=[x["t"] for x in tk], values=[x["joints"] or [None] * 6 for x in tk],
                                    units="deg", expected_rate_hz=cfg.rate_hz, valid_range=[-360.0, 360.0], source=f"viam:{cfg.arm}.get_joint_positions"),
            "obs.ee_pose": Stream(kind="observation", t=[x["t"] for x in tk], values=[x["pose"] or [None] * 7 for x in tk],
                                  units="mm, orientation-vector, deg", expected_rate_hz=cfg.rate_hz, source=f"viam:{cfg.arm}.get_end_position"),
            "obs.arm_is_moving": Stream(kind="observation", t=[x["t"] for x in tk], values=[[x["arm_moving"]] for x in tk],
                                        expected_rate_hz=cfg.rate_hz, valid_range=[0.0, 1.0], source=f"viam:{cfg.arm}.is_moving"),
            "telemetry.read_latency_ms": Stream(kind="telemetry", t=[x["t"] for x in tk], values=[[x["latency_ms"]] for x in tk],
                                                units="ms", source="amperes recorder"),
        }
        if cfg.gripper:
            streams["obs.gripper_is_moving"] = Stream(kind="observation", t=[x["t"] for x in tk], values=[[x["grip_moving"]] for x in tk],
                                                      expected_rate_hz=cfg.rate_hz, valid_range=[0.0, 1.0], source=f"viam:{cfg.gripper}.is_moving")
        if cfg.camera and cm:
            streams["obs.camera_frame"] = Stream(kind="observation", t=[x["t"] for x in cm], values=[[x["bytes"], x["hash"]] for x in cm],
                                                 units="bytes, sha256[:8] as int", expected_rate_hz=1.0 / cfg.camera_every_s,
                                                 source=f"viam:{cfg.camera}.get_images;max_unchanged_s={3 * cfg.camera_every_s}")
        ep = Episode(
            episode_id=eid,
            source=SourceInfo(dataset_id="viam", adapter="amperes.viam_live@1.0.0", source_uri=f"viam://{rec.machine}",
                              source_record_id=eid, synthetic=False,
                              synthetic_note=None),
            task=Ref(id=f"viam_{kind}", version=None), policy=None,
            robot=RobotInfo(embodiment=f"{len(tk[0]['joints']) if tk and tk[0]['joints'] else 'unknown'}-joint arm (model not visible to this API key)",
                            device_id=cfg.arm, config_id=None),
            session=SessionInfo(session_id=session, workcell_id=rec.machine, operator_id=None),
            start_time=s, end_time=e, time_base="unix_s", streams=streams,
            outcome=Outcome(label="unknown", provenance="unknown", verified=False,
                            reason="read-only monitoring cannot observe task success"),
            conditions={"segment": kind}, extra={"capture": "read-only Viam telemetry; no commands sent"})
        episodes.append(ep)
        for x in rec.events:
            if x["event_type"] in ("recording_error", "fault") and s <= x["timestamp"] <= e:
                x.setdefault("episode_id", eid)
    with open(out_dir / "episodes.jsonl", "w") as fh:
        for ep in episodes:
            fh.write(episode_to_json(ep) + "\n")
    with open(out_dir / "events.jsonl", "w") as fh:
        for x in rec.events:
            fh.write(json.dumps(x) + "\n")
    lat = [x["latency_ms"] for x in rec.ticks]
    summary = {
        "machine": rec.machine, "session_id": session, "duration_s": round((rec.ended or rec.started) - rec.started, 2),
        "ticks": len(rec.ticks), "achieved_hz": round(len(rec.ticks) / max(1e-6, (rec.ended or rec.started) - rec.started), 2),
        "failed_ticks": sum(1 for x in rec.ticks if x["joints"] is None or x["pose"] is None),
        "latency_ms_p50": statistics.median(lat) if lat else None,
        "latency_ms_p95": sorted(lat)[int(0.95 * len(lat)) - 1] if len(lat) > 1 else (lat[0] if lat else None),
        "camera_probes": len(rec.camera), "camera_no_frame": sum(1 for x in rec.camera if x["bytes"] is None),
        "episodes": len(episodes), "segments": [ep.conditions["segment"] for ep in episodes],
        "events": dict(sorted(__import__("collections").Counter(f"{x['event_type']}:{x['code']}" for x in rec.events).items())),
        "commands_sent": 0, "out_dir": str(out_dir),
    }
    (out_dir / "capture_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def capture_and_import(cfg: RecorderConfig, out_dir: Path, live: LiveStatus | None = None,
                       stop: threading.Event | None = None, log=print) -> dict[str, Any]:
    from amperes import incidents
    from amperes.ingest.pipeline import import_dataset

    live = live or LiveStatus(out_dir / "live.json")
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        rec = asyncio.run(record(cfg, live, stop))
        summary = to_canonical(rec, cfg, out_dir)
        r = import_dataset(out_dir, name=f"LIVE Viam capture {summary['session_id']}", adapter="jsonl", synthetic=False,
                           note="read-only telemetry captured from a Viam machine; outcomes unknown", ctx=IMPORT_RULE_SETTINGS)
        n_inc = len(incidents.build_incidents(r["dataset_id"]))
        summary.update(dataset_id=r["dataset_id"], incidents=n_inc, quality=r["quality"]["status_counts"])
        live.update(running=False, done=True, summary=summary)
        log(json.dumps(summary, indent=2, default=str))
        return summary
    except Exception as exc:
        live.update(running=False, done=True, error=f"{type(exc).__name__}: {exc}")
        raise
