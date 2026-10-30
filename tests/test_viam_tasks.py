import asyncio
import json
import math
import threading
from dataclasses import asdict

import pytest

from amperes import store
from amperes.ingest.pipeline import import_dataset
from amperes.integrations import viam_tasks as VT
from amperes.integrations.viam_control import POSE_KEYS, SafetyError
from tests.test_viam_control import FakeArm, FakeGripper, P


class FakeMotion:
    """World-frame motion service: moves the fake arm; refuses poses in `unreachable`."""

    def __init__(self, arm, unreachable=()):
        self.arm, self.unreachable, self.moves = arm, list(unreachable), []

    async def get_pose(self, component_name, destination_frame):
        class R:
            pose = P(self.arm.p)
        return R()

    async def move(self, component_name, destination, constraints=None):
        d = {k: getattr(destination.pose, k) for k in POSE_KEYS}
        if any(math.dist([d[k] for k in "xyz"], [u[k] for k in "xyz"]) < 1 for u in self.unreachable):
            raise RuntimeError("motion: unable to plan path to the destination")
        await self.arm.move_to_position(destination.pose)
        self.moves.append(d)
        return True


def ctl(execute=True, unreachable=(), stop=None, arm=None):
    cfg = VT.ScanConfig()
    arm = arm or FakeArm()
    arm.p = {**VT.scan_home(cfg), "x": 178.0, "z": 443.0}
    env = VT.scan_envelope(cfg, -23.0)
    motion = FakeMotion(arm, unreachable)
    c = VT.ScanController(arm, FakeGripper(), motion, "arm", execute, env, stop or threading.Event(), frame="world")
    return c, cfg, motion


def test_grid_is_serpentine_and_every_step_is_allowed():
    cfg = VT.ScanConfig()
    poses = VT.generate_poses(cfg)
    assert len(poses) == cfg.nx * cfg.ny * len(cfg.heights_mm) + (cfg.nx * cfg.ny * len(cfg.heights_mm)) // cfg.revisit_every
    env = VT.scan_envelope(cfg, -23.0)
    prev = VT.scan_home(cfg)
    for label, p in poses:
        assert not env.check(p, prev), (label, env.check(p, prev))
        assert p["o_z"] == -1.0
        prev = p
    assert sum(l == "centre" for l, _ in poses) == 4


def test_envelope_keeps_fingertips_above_the_table():
    env = VT.scan_envelope(VT.ScanConfig(), -23.0)
    assert env.z[0] == pytest.approx(-23 + 150 + 80)
    with pytest.raises(ValueError):
        VT.scan_envelope(VT.ScanConfig(heights_mm=(150.0,)), -23.0)
    assert env.check({**VT.scan_home(VT.ScanConfig()), "z": 150}, None)


def test_dry_run_moves_nothing_and_records_the_plan():
    c, cfg, motion = ctl(execute=False)
    r = asyncio.run(c.sweep(0, VT.generate_poses(cfg), cfg))
    assert motion.moves == [] and r.outcome == "unknown" and len(r.poses) == len(VT.generate_poses(cfg))
    assert all(p["status"] == "ok" and p["error_mm"] == 0 for p in r.poses)


def test_planning_failure_is_recorded_and_sweep_continues():
    cfg = VT.ScanConfig()
    poses = VT.generate_poses(cfg)
    c, cfg, motion = ctl(unreachable=[poses[2][1]])
    c.snap = None
    r = asyncio.run(c.sweep(0, poses, cfg))
    assert r.outcome == "failure" and r.category == "PLANNING_FAILURE"
    assert [p["status"] for p in r.poses].count("planning_failure") == 1
    assert len(motion.moves) == len(poses) - 1


def test_refusal_and_stop_abort_the_sweep():
    c, cfg, motion = ctl()
    with pytest.raises(SafetyError):
        asyncio.run(c._move({**VT.scan_home(cfg), "z": 100}, "too_low", 5))
    stop = threading.Event()
    stop.set()
    c, cfg, motion = ctl(stop=stop)
    r = asyncio.run(c.sweep(0, VT.generate_poses(cfg), cfg))
    assert r.category == "SAFETY_STOP" and motion.moves == []


def test_scan_session_to_canonical_imports_as_valid(tmp_path):
    cfg = VT.ScanConfig(nx=2, ny=2, heights_mm=(320.0,), revisit_every=2, video_hz=2.0)
    c, _, motion = ctl()
    snaps = []

    async def snap(tag, sweep, depth=False):
        snaps.append(tag)
        return [f"x-s{sweep:03d}_cam_{tag}.jpg"]

    c.snap = snap
    r = asyncio.run(c.sweep(0, VT.generate_poses(cfg), cfg))
    assert r.outcome == "success" and len(snaps) == 6
    d = asdict(r)
    base, seconds = 1_790_000_000.0, 19 / 3
    span = (d["ended"] - d["started"]) or 1e-9
    f = lambda t: base + (t - d["started"]) / span * seconds  # noqa: E731
    d["steps"] = [{**a, "t": f(a["t"])} for a in d["steps"]]
    d["poses"] = [{**p, "t_cmd": f(p["t_cmd"]), **({"t_settled": f(p["t_settled"])} if "t_settled" in p else {})} for p in d["poses"]]
    d["started"], d["ended"] = base, base + seconds
    img = tmp_path / "s" / "images"
    img.mkdir(parents=True)
    video = []
    for n in range(14):
        fn = f"sid_cam_video_{n:06d}.jpg"
        (img / fn).write_bytes(b"\xff\xd8jpg")
        video.append({"t": base + n * 0.5, "n": n, "file": fn})
    session = {"machine": "sfhack-test", "session_id": "sid", "started": base - 1, "ended": base + seconds + 1, "config": asdict(cfg),
               "execute": True, "envelope": asdict(VT.scan_envelope(cfg, -23.0)), "box_description": "box", "poses_planned": 6,
               "telemetry": [{"t": base + i / 3, "joints": [float(i)] * 6, "pose": [300 + i, 0, 400, 0, 0, -1, 0]} for i in range(20)],
               "images": [{"t": base + 1, "camera": "cam", "tag": "p00_g00_z320", "sweep": 0, "mime": "image/jpeg", "file": "x.jpg"}],
               "video": video, "sweeps": [d], "events": []}
    summ = VT.scan_session_to_canonical(session, tmp_path / "s")
    assert summ["sweeps"] == 1 and summ["poses_ok"] == 6 and summ["video_frames"] == 14
    ep = json.loads((tmp_path / "s" / "episodes.jsonl").read_text())
    assert ep["task"]["id"] == "workspace_scan_real" and ep["outcome"]["verified"] is True
    assert set(ep["streams"]) >= {"obs.joint_pos", "obs.ee_pose", "act.target_pose", "obs.ee_pose_settled", "obs.cam.frame"}
    assert (img / "sid-s000_cam_video_00000.jpg").exists() and not (img / "sid_cam_video_000000.jpg").exists()
    rows = [json.loads(l) for l in (tmp_path / "s" / "poses.jsonl").read_text().splitlines()]
    assert len(rows) == 6 and all(r["episode_id"] == "sid-s000" for r in rows)
    res = import_dataset(tmp_path / "s", adapter="jsonl", ctx={"required_metadata": ["task.id"]})
    st = store.query("SELECT outcome, quality_status, verified FROM episodes WHERE dataset_id=?", [res["dataset_id"]])
    assert st[0]["outcome"] == "success" and st[0]["verified"] == 1 and st[0]["quality_status"] == "verified_success"


def test_reach_stats_repeatability():
    poses = [{"label": "centre", "status": "ok", "achieved": {"x": 350 + dx, "y": 0, "z": 420}, "error_mm": abs(dx), "move_s": 1.0}
             for dx in (-1.0, 1.0, -1.0, 1.0)]
    s = VT._reach_stats(poses)
    assert s["centre_revisits"] == 4 and s["repeatability_mm_std"] == pytest.approx(1.1547, abs=1e-3) and s["error_mm_max"] == 1.0
