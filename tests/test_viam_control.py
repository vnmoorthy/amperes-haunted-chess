import asyncio
import inspect
import re
import threading
from pathlib import Path

import pytest

from amperes import store
from amperes.ingest.pipeline import import_dataset
from amperes.integrations import viam_control as VC

WPS = {"home": {"x": 300, "y": 0, "z": 400, "o_x": 0, "o_y": 0, "o_z": -1, "theta": 0, "_frame": "base"},
       "a": {"x": 350, "y": -150, "z": 50, "o_x": 0, "o_y": 0, "o_z": -1, "theta": 0, "_frame": "base"},
       "b": {"x": 350, "y": 150, "z": 50, "o_x": 0, "o_y": 0, "o_z": -1, "theta": 0, "_frame": "base"}}


class P:
    def __init__(self, d):
        for k in VC.POSE_KEYS:
            setattr(self, k, d[k])


class FakeArm:
    def __init__(self, delay=0.0):
        self.p = dict(WPS["home"])
        self.moves, self.stops, self.delay = [], 0, delay

    async def get_end_position(self):
        return P(self.p)

    async def move_to_position(self, pose):
        await asyncio.sleep(self.delay)
        self.p = {k: getattr(pose, k) for k in VC.POSE_KEYS}
        self.moves.append(self.p)

    async def stop(self):
        self.stops += 1


class FakeGripper:
    def __init__(self, grab=True, holding=True):
        self.g, self.h, self.ops = grab, holding, []

    async def open(self):
        self.ops.append("open")

    async def grab(self):
        self.ops.append("grab")
        return self.g

    async def is_holding_something(self):
        return self.h

    async def stop(self):
        self.ops.append("stop")


def ctl(execute=True, arm=None, grip=None, stop=None):
    env = VC.Envelope.from_waypoints(WPS, 80, table_z=None)
    return VC.Controller(arm or FakeArm(), grip or FakeGripper(), motion=None, execute=execute, envelope=env,
                         stop_event=stop or threading.Event(), frame="base")


def run(c, cfg=None):
    return asyncio.run(c.trial(0, WPS["a"], WPS["b"], cfg or VC.TrialConfig(settle_s=0), ("a", "b")))


def test_envelope_requires_taught_points_and_refuses_outside():
    with pytest.raises(ValueError):
        VC.Envelope.from_waypoints({"home": WPS["home"]}, 80, None)
    env = VC.Envelope.from_waypoints(WPS, 80, table_z=None)
    assert env.z[0] == pytest.approx(47.0)  # never below the lowest taught pose - 3 mm
    assert env.check({**WPS["a"], "z": 20}, None)  # below the floor -> refused
    assert env.check({**WPS["a"], "x": 900}, None)
    assert not env.check(WPS["b"], WPS["a"])


def test_refused_target_never_reaches_the_sdk():
    arm = FakeArm()
    c = ctl(arm=arm)
    with pytest.raises(VC.SafetyError):
        asyncio.run(c._move({**WPS["a"], "z": -200}, "bad", 5))
    assert arm.moves == [] and c.events[0]["code"] == "SAFETY_REFUSAL"


def test_dry_run_moves_nothing():
    arm, grip = FakeArm(), FakeGripper()
    r = run(ctl(execute=False, arm=arm, grip=grip))
    assert arm.moves == [] and grip.ops == [] and r.outcome == "unknown"
    assert [a["label"] for a in r.steps if a["kind"] == "move"] == ["approach_source", "descend_grasp", "lift", "transport", "lower_place", "retreat"]  # dry run: no stepped descent


def test_outcomes_from_gripper_evidence():
    assert run(ctl()).outcome == "success"
    r = run(ctl(grip=FakeGripper(grab=False)))
    assert (r.outcome, r.category) == ("failure", "GRASP_FAILURE")
    r = run(ctl(grip=FakeGripper(grab=True, holding=False)))
    assert (r.outcome, r.category) == ("failure", "SLIPPAGE")


def test_stop_and_timeout():
    stop = threading.Event()
    stop.set()
    arm = FakeArm()
    r = run(ctl(arm=arm, stop=stop))
    assert r.category == "SAFETY_STOP" and arm.moves == []
    r = run(ctl(arm=FakeArm(delay=0.2)), VC.TrialConfig(settle_s=0, move_timeout_s=0.05))
    assert r.category == "TIMEOUT"


def test_motion_only_inside_guarded_methods():
    src = Path(VC.__file__).read_text()
    allowed = inspect.getsource(VC.Controller._move) + inspect.getsource(VC.Controller._gripper) + inspect.getsource(VC.Controller.stop)
    for pat in (r"move_to_position\(", r"\.motion\.move\(", r"getattr\(self\.gripper, op\)\(", r"dev\.stop\("):
        for m in re.finditer(pat, src):
            line = src[src.rfind("\n", 0, m.start()) + 1: src.find("\n", m.end())]
            assert line in allowed, f"motion call outside guarded methods: {line.strip()}"


def test_operator_ack_required(monkeypatch):
    monkeypatch.setattr("amperes.integrations.viam_live._env", lambda: "sfhack1-main.x.viam.cloud")
    with pytest.raises(VC.SafetyError):
        asyncio.run(VC.run_session(VC.RunSpec(config=VC.TrialConfig(), execute=True, operator_ack="yes")))
    assert VC.required_ack("sfhack1-main") == "I am at sfhack1-main with the e-stop in reach"


def test_session_to_canonical_and_audit(tmp_path):
    c_ok, c_fail = ctl(), ctl(grip=FakeGripper(grab=False))
    t0 = run(c_ok)
    t1 = asyncio.run(c_fail.trial(1, WPS["b"], WPS["a"], VC.TrialConfig(settle_s=0), ("b", "a")))
    from dataclasses import asdict

    def stretch(tr, base, seconds=19 / 3):  # fake trials take ms; give them real-time timestamps (3 Hz, 20 samples)
        d = asdict(tr)
        span = (d["ended"] - d["started"]) or 1e-9
        f = lambda t: base + (t - d["started"]) / span * seconds  # noqa: E731
        d["steps"] = [{**a, "t": f(a["t"])} for a in d["steps"]]
        d["started"], d["ended"] = base, base + seconds
        return d

    d0, d1 = stretch(t0, 1_790_000_000.0), stretch(t1, 1_790_000_010.0)
    sess = {"machine": "sfhack-test", "started": d0["started"] - 1, "ended": d1["ended"] + 1, "config": asdict(VC.TrialConfig()),
            "execute": True,
            "telemetry": [{"t": d["started"] + i / 3, "joints": [float(i)] * 6, "pose": [300 + i, 0, 400, 0, 0, -1, 0]}
                          for d in (d0, d1) for i in range(20)],
            "images": [], "trials": [d0, d1], "events": []}
    summ = VC.session_to_canonical(sess, tmp_path / "s")
    assert summ["trials"] == 2 and summ["outcomes"] == {"success": 1, "failure": 1}
    r = import_dataset(tmp_path / "s", adapter="jsonl", ctx={"required_metadata": ["task.id"]})
    st = {x["outcome"]: x["quality_status"] for x in store.query("SELECT outcome, quality_status FROM episodes WHERE dataset_id=?", [r["dataset_id"]])}
    assert st["failure"] == "valid_failed_task"  # a real failed grasp is still usable data
    assert store.query("SELECT COUNT(*) n FROM episodes WHERE dataset_id=? AND verified=1", [r["dataset_id"]])[0]["n"] == 2
