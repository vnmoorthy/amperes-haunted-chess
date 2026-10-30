import re
from pathlib import Path

import pytest

from amperes import ops, store
from amperes.ingest.pipeline import import_dataset
from amperes.integrations import viam_live as V

SRC = Path(V.__file__).read_text()


def test_recorder_source_contains_no_command_calls():
    forbidden = r"\.(move_to_position|move_to_joint_positions|move|open|grab|stop|do_command|set_power|set_velocity|move_on_map|move_on_globe)\("
    assert not re.findall(forbidden, SRC)
    called = set(re.findall(r"await (?:arm|grip|cam|robot)\.(\w+)\(", SRC)) | set(re.findall(r"read\((?:arm|grip|robot)\.(\w+)\(", SRC))
    called |= set(re.findall(r"(?:arm|grip|cam|robot)\.(\w+)\(\)", SRC))
    assert called and called <= V.ALLOWED_METHODS


def test_read_only_wrapper_blocks_commands():
    class Fake:
        def get_joint_positions(self):
            return "ok"

        def move_to_joint_positions(self, *a):
            raise AssertionError("must never be reached")

    w = V.ReadOnly(Fake(), "arm")
    assert w.get_joint_positions() == "ok"
    for name in ("move_to_joint_positions", "do_command", "stop", "open", "grab"):
        with pytest.raises(PermissionError):
            getattr(w, name)
    with pytest.raises(PermissionError):
        w.x = 1


def _recording(t0=1_790_000_000.0):
    rec = V.Recording(machine="sfhack-test", started=t0)
    joints = [0.0, -84.0, 1.3, 0.0, 0.0, 0.0]
    for i in range(120):  # 40 s at 3 Hz; arm moves between 10 s and 20 s
        t = t0 + i / 3
        moving = 10 <= i / 3 <= 20
        j = [v + (i / 3 - 10) * 5 if moving and k == 0 else v for k, v in enumerate(joints)]
        failed = i in (40, 41)
        rec.ticks.append({"t": t, "joints": None if failed else j, "pose": None if failed else [178, 0, 445, 1, 0, 0, 0],
                          "arm_moving": None if failed else float(moving), "grip_moving": 0.0, "latency_ms": 1000.0 if failed else 80.0})
        if failed:
            rec.events.append({"timestamp": t, "event_type": "recording_error", "event_class": "data_recording_failure",
                               "code": "VIAM_READ_TIMEOUTERROR", "message": "x", "workcell_id": "sfhack-test", "device_id": "arm",
                               "session_id": "viam-test"})
    for i in range(20):
        rec.camera.append({"t": t0 + 2 * i, "bytes": 90000.0, "hash": float(1000 + i)})
    rec.events.insert(0, {"timestamp": t0, "event_type": "session_start", "event_class": "", "code": "", "message": "",
                          "workcell_id": "sfhack-test", "device_id": None, "session_id": "viam-test"})
    rec.ended = t0 + 40
    rec.events.append({"timestamp": rec.ended, "event_type": "session_end", "event_class": "", "code": "", "message": "",
                       "workcell_id": "sfhack-test", "device_id": None, "session_id": "viam-test"})
    return rec


def test_canonical_conversion_segmentation_and_audit(tmp_path):
    cfg = V.RecorderConfig(rate_hz=3.0)
    rec = _recording()
    segs = V.segment(rec, cfg)
    assert [k for *_, k in segs] == ["motion"] and segs[0][0] < rec.started + 10.5 < segs[0][1]
    summary = V.to_canonical(rec, cfg, tmp_path / "cap")
    assert summary["commands_sent"] == 0 and summary["failed_ticks"] == 2 and summary["episodes"] == 1
    r = import_dataset(tmp_path / "cap", adapter="jsonl", synthetic=False, ctx=V.IMPORT_RULE_SETTINGS)
    rules = {x["rule_id"] for x in store.query("SELECT rule_id FROM quality_findings WHERE dataset_id=?", [r["dataset_id"]])}
    assert "C003_no_actions" not in rules and "C005_missing_outcome" not in rules
    assert "C007_missing_samples" in rules  # the two failed reads are visible as missing samples
    ep = store.query("SELECT quality_status, synthetic FROM episodes WHERE dataset_id=?", [r["dataset_id"]])[0]
    assert ep["quality_status"] == "infrastructure_error" and ep["synthetic"] == 0  # failed reads fall inside the motion episode
    m = ops.ops_metrics([r["dataset_id"]])
    assert m["throughput"]["session_hours_recorded"]["status"] == "computed"
    assert m["yield"]["data_validity"]["denominator"] == 1


def test_idle_capture_uses_observation_windows(tmp_path):
    rec = _recording()
    for x in rec.ticks:
        if x["joints"]:
            x["joints"] = [0.0, -84.0, 1.3, 0.0, 0.0, 0.0]
            x["arm_moving"] = 0.0
    segs = V.segment(rec, V.RecorderConfig(idle_window_s=20))
    assert [k for *_, k in segs] == ["idle_window", "idle_window"]
    rec.ended += 1.4  # teardown tail after the last read must not become its own (empty) episode
    segs = V.segment(rec, V.RecorderConfig(idle_window_s=20))
    assert len(segs) == 2 and segs[-1][1] == rec.ticks[-1]["t"]


def test_low_rate_stream_not_flagged_as_truncated(tmp_path):
    cfg = V.RecorderConfig(rate_hz=3.0, camera_every_s=2.0)
    rec = _recording()
    rec.camera = [c for c in rec.camera if c["t"] <= rec.started + 38.5]  # last frame ~1.5 s before the end
    V.to_canonical(rec, cfg, tmp_path / "c")
    r = import_dataset(tmp_path / "c", adapter="jsonl", ctx=V.IMPORT_RULE_SETTINGS)
    obs = store.query("SELECT observed FROM quality_findings WHERE dataset_id=? AND rule_id='C004_truncated'", [r["dataset_id"]])
    assert not any("obs.camera_frame" in (o["observed"] or "") for o in obs)


def test_similar_failures_and_atlas_documents(tmp_path, monkeypatch):
    from amperes.integrations import atlas
    from amperes.signatures import DIMS, similar_local

    cfg = V.RecorderConfig()
    V.to_canonical(_recording(), cfg, tmp_path / "a")
    V.to_canonical(_recording(1_790_000_500.0), cfg, tmp_path / "b")
    ra = import_dataset(tmp_path / "a", adapter="jsonl", ctx=V.IMPORT_RULE_SETTINGS)
    rb = import_dataset(tmp_path / "b", adapter="jsonl", ctx=V.IMPORT_RULE_SETTINGS)
    ea = store.query("SELECT episode_id FROM episodes WHERE dataset_id=?", [ra["dataset_id"]])[0]["episode_id"]
    res = similar_local(ea, 5)
    assert res["matches"] and res["matches"][0]["dataset_id"] == rb["dataset_id"] and res["matches"][0]["score"] == 1.0
    docs = atlas.documents(ra["dataset_id"])
    ep = docs["episodes"][0]
    assert len(ep["failure_signature"]) == DIMS and "streams" not in ep and ep["_id"] == ea
    assert docs["ops_events"] and all("_id" in d for d in docs["findings"])
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.setattr(atlas, "_uri", lambda: (_ for _ in ()).throw(RuntimeError("MONGODB_URI is not set")))
    with pytest.raises(RuntimeError):
        atlas.sync(ra["dataset_id"])


def test_robot_trial_reason_is_its_failure_category(monkeypatch):
    from amperes import signatures as S

    eps = [{"episode_id": "e1", "dataset_id": "d", "quality_status": "valid_failed_task", "category": None, "reason": "GRASP_FAILURE",
            "session_id": "s", "workcell_id": "w"},
           {"episode_id": "e2", "dataset_id": "d", "quality_status": "verified_success", "category": None, "reason": None,
            "session_id": "s", "workcell_id": "w"}]
    monkeypatch.setattr(S.store, "query", lambda sql, params=None: eps if "FROM episodes" in sql else [])
    sigs = S.signatures()
    assert set(sigs) == {"e1"} and sigs["e1"]["category"] == "GRASP_FAILURE"
