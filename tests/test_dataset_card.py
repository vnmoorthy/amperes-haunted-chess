import json
import re
from pathlib import Path

import pytest

from amperes import labels, review, store
from amperes.dataset_card import NOT_RECORDED, build_card, main, markdown
from amperes.ingest.pipeline import import_dataset
from amperes.schema import Episode, Event, Outcome, Ref, RobotInfo, SessionInfo, SourceInfo, Stream, episode_to_json
from amperes.stats import wilson

T0 = 1_793_300_000.0
N_TEL = 30


def real_ep(eid: str, label="success", with_meta=True, cams=("cam", "cam", "cam2")) -> Episode:
    t = [T0 + i / 3.0 for i in range(N_TEL)]
    streams = {
        "obs.joint_pos": Stream(kind="observation", t=t, values=[[10.0 + i, 0, 0, 0, 0, 0] for i in range(N_TEL)], units="deg",
                                expected_rate_hz=3.0, valid_range=[-360.0, 360.0], source="viam:arm.get_joint_positions"),
        "obs.ee_pose": Stream(kind="observation", t=t, values=[[300.0, 0, 100.0, 0, 0, -1, 0] for _ in range(N_TEL)],
                              units="mm, orientation-vector, deg", expected_rate_hz=3.0, source="viam:arm.get_end_position"),
        "act.target_pose": Stream(kind="action", t=[t[0], t[10], t[20]], values=[[300.0, 0, 150.0, 0, 0, -1, 0]] * 3,
                                  units="mm, orientation-vector, deg", source="amperes scripted pick-place"),
        "act.gripper": Stream(kind="action", t=[t[5], t[25]], values=[[1.0], [0.0]], valid_range=[0.0, 1.0]),
    }
    exts = {"cam": [".jpg", ".dep"], "cam2": [".jpg"]}
    seen = {}
    events = []
    for c in cams:
        ext = exts[c][seen.get(c, 0) % len(exts[c])]
        seen[c] = seen.get(c, 0) + 1
        events.append(Event(t=t[2], type="annotation", code="CAMERA_SNAPSHOT", message=f"{eid}_{c}_before_grasp{seen[c]}{ext}", source=c))
    crit = {"grab_reported": label == "success", "holding_after_lift": label == "success"}
    return Episode(
        episode_id=eid,
        source=SourceInfo(dataset_id="robot", adapter="amperes.viam_control@1.0.0", source_uri="viam://sfhack1-main", source_record_id=eid, synthetic=False),
        task=Ref(id="pick_place_real", version="1.0.0"), policy=Ref(id="scripted_pick_place_real", version="baseline") if with_meta else None,
        robot=RobotInfo(embodiment="6-joint arm + parallel gripper", device_id="arm", config_id="baseline") if with_meta else None,
        session=SessionInfo(session_id="robot-sfhack1-main-1", workcell_id="sfhack1-main", operator_id="op-onsite") if with_meta else None,
        start_time=t[0] if with_meta else None, end_time=t[-1] if with_meta else None, time_base="unix_s", streams=streams, events=events,
        outcome=Outcome(label=label, provenance="amperes_verifier", verified=True, reason=None if label == "success" else "GRASP_FAILURE",
                        verifier_id="gripper_signals", verifier_version="1.0.0", measurements={"criteria": crit}),
        conditions={"trial": 1, "dry_run": False, "config": {"name": "baseline"}})


def frame_ep(eid: str, n: int = 3) -> Episode:
    """viam_tasks-style workspace-scan episode: a frame-index stream whose samples are files images/<eid>_cam_video_<n>.jpg."""
    ep = real_ep(eid, label="unknown", cams=("cam",))
    ep.source.adapter = "amperes.viam_tasks@1.0.0"
    ep.streams["obs.cam.frame"] = Stream(kind="observation", t=[T0 + i for i in range(n)], values=[[float(i)] for i in range(n)],
                                         units=f"frame index: images/{eid}_cam_video_<n:05d>.jpg", expected_rate_hz=1.0, source="viam:cam.get_images color")
    ep.outcome = Outcome(label="unknown", provenance="unknown", verifier_id="pose_error", verifier_version="1.0.0")
    return ep


def write_session(d: Path, eps: list[Episode]) -> Path:
    (d / "images").mkdir(parents=True)
    with open(d / "episodes.jsonl", "w") as fh:
        for e in eps:
            fh.write(episode_to_json(e) + "\n")
            for ev in e.events:
                if ev.code == "CAMERA_SNAPSHOT" and Path(ev.message).name == ev.message:
                    (d / "images" / ev.message).write_bytes(b"\xff\xd8" if ev.message.endswith(".jpg") else b"VERSIONX")
            for k, s in e.streams.items():
                if k.endswith(".frame"):
                    for v in s.values:
                        (d / "images" / f"{e.episode_id}_{k.split('.')[1]}_video_{int(v[0]):05d}.jpg").write_bytes(b"\xff\xd8")
    return d


@pytest.fixture
def data(tmp_path):
    from amperes.fixtures import ops_package

    src = tmp_path / "ops"
    ops_package(src)
    synth = import_dataset(src, name="fixture ops", synthetic=True)["dataset_id"]
    robot = import_dataset(write_session(tmp_path / "robot", [real_ep("r1"), real_ep("r2", label="failure")]), name="arm session",
                           adapter="jsonl", synthetic=False, ctx={"required_metadata": ["task.id"]})["dataset_id"]
    bare = import_dataset(write_session(tmp_path / "bare", [real_ep("b1", with_meta=False, cams=())]), name="bare", adapter="jsonl",
                          synthetic=False)["dataset_id"]
    return synth, robot, bare


def test_card_counts_match_store_and_flags(data, tmp_path):
    synth, robot, bare = data
    ids = [synth, robot]
    fid = store.query("SELECT finding_id FROM quality_findings WHERE dataset_id=? AND severity<>'info' LIMIT 1", [synth])[0]["finding_id"]
    review.record_review("finding", fid, "incorrect", "rev1")
    review.record_review("finding", fid, "correct_actionable", "rev1")
    out = build_card(ids, tmp_path / "card.md", title="Pilot card")
    md, f = out["markdown"], out["facts"]
    q = "SELECT COUNT(*) n FROM episodes WHERE dataset_id IN (?,?)"
    assert f["stats"]["episodes"] == store.query(q, ids)[0]["n"]
    assert f["stats"]["real_episodes"] == 2 and f["stats"]["synthetic_episodes"] == store.query(q + " AND synthetic=1", ids)[0]["n"]
    status = {r["quality_status"]: r["n"] for r in store.query("SELECT quality_status, COUNT(*) n FROM episodes WHERE dataset_id IN (?,?) GROUP BY 1", ids)}
    assert f["composition"]["by_quality_status"] == status
    outcome = {r["outcome"] if r["outcome"] is not None else NOT_RECORDED: r["n"]
               for r in store.query("SELECT outcome, COUNT(*) n FROM episodes WHERE dataset_id IN (?,?) GROUP BY 1", ids)}
    assert f["composition"]["by_outcome"] == outcome and outcome[NOT_RECORDED] > 0  # null outcomes are named, never "None"
    k, n = outcome["success"], sum(outcome.get(o, 0) for o in ("success", "failure", "timeout"))
    assert f["stats"]["success_rate"]["ci95"] == [round(x, 4) for x in wilson(k, n)]
    assert f["stats"]["success_rate"]["successes"] == k and f["stats"]["success_rate"]["valid_outcomes"] == n
    # frames = obs.joint_pos samples (fixture has that stream too); images per camera from CAMERA_SNAPSHOT events
    frames = f["composition"]["streams"]["obs.joint_pos"]["samples"]
    assert f["stats"]["frames"]["value"] == frames and frames >= 2 * N_TEL
    assert f["composition"]["images"]["by_camera"] == {"cam": 4, "cam2": 2}
    assert f["composition"]["images"]["by_mime"] == {"image/jpeg": 4, "image/vnd.viam.dep": 2}
    assert f["stats"]["images"]["value"] == 6 and f["stats"]["images"]["found_on_disk"] == 6
    assert f["composition"]["by_verifier"] == {"gripper_signals": 2}
    assert f["composition"]["verified"]["n"] == store.query(q + " AND verified=1", ids)[0]["n"]
    assert f["composition"]["by_failure_category"]["GRASP_FAILURE"] == 1
    rules = {r["rule_id"]: r["n"] for r in store.query("SELECT rule_id, COUNT(*) n FROM quality_findings WHERE dataset_id IN (?,?) GROUP BY 1", ids)}
    assert {k: v["findings"] for k, v in f["quality"]["findings_by_rule"].items()} == rules
    assert f["review"]["reviews"] == 2 and f["review"]["by_decision_latest"] == {"correct_actionable": 1}
    assert f["collection"]["robot_sessions"]["executed"] == 2 and f["collection"]["robot_sessions"]["dry_run"] == 0
    ds = {d["dataset_id"]: d for d in f["datasets"]}
    assert ds[synth]["synthetic"] is True and ds[robot]["synthetic"] is False
    assert ds[robot]["workcells"] == ["sfhack1-main"] and ds[robot]["cameras"] == {"cam": 4, "cam2": 2}
    assert ds[robot]["time_span"]["hours"] == pytest.approx((N_TEL - 1) / 3.0 / 3600, abs=1e-3)
    # markdown: flags, no leaked None, numbers agree with the JSON sidecar
    assert "# Dataset card: Pilot card" in md and "MIXED (real and synthetic)" in md
    assert f"`{synth}` — fixture ops [SYNTHETIC]" in md and f"`{robot}` — arm session [REAL]" in md
    assert re.search(r"\bNone\b", md) is None
    assert f"| episodes | {f['stats']['episodes']} |" in md
    assert f"| images referenced | 6 |" in md and "| cam | 4 |" in md
    assert f"{f['stats']['success_rate']['successes']}/{f['stats']['success_rate']['valid_outcomes']} valid outcomes" in md
    assert "gripper_signals" in md and "scripted" in md and "e-stop" in md and "[license]" in md and "[contact]" in md
    side = json.loads(Path(out["path"] + ".json").read_text())
    assert side == f and Path(out["path"]).read_text() == md
    assert f["provenance"]["export_timestamp"] == f["generated_at"] and f["provenance"]["dataset_ids"] == ids


def test_missing_facts_say_not_recorded(data):
    _, _, bare = data
    out = build_card([bare])
    md, d = out["markdown"], out["facts"]["datasets"][0]
    assert d["workcells"] == [] and d["time_span"]["start_iso"] is None and d["embodiments"] == []
    assert f"- Who/where: workcells {NOT_RECORDED}; devices {NOT_RECORDED}; operators (pseudonymous) {NOT_RECORDED}; sessions {NOT_RECORDED}" in md
    assert f"- When: {NOT_RECORDED} to {NOT_RECORDED}" in md and f"- Embodiment: {NOT_RECORDED}" in md and f"- Cameras: {NOT_RECORDED}" in md
    assert out["facts"]["composition"]["duration_basis"] == {"stream_span": 1}  # measured from streams, labelled as such
    assert "REAL data" in md and "No human review recorded" in md and re.search(r"\bNone\b", md) is None
    assert out["path"] is None
    with pytest.raises(ValueError):
        build_card(["nope"])
    assert main(["nope"]) == 2


def test_cli_writes_files(data, tmp_path, capsys):
    _, robot, _ = data
    assert main([robot, "--out", str(tmp_path / "c" / "card.md")]) == 0
    assert (tmp_path / "c" / "card.md").exists() and (tmp_path / "c" / "card.md.json").exists()
    assert str(tmp_path / "c" / "card.md") in capsys.readouterr().out


@pytest.fixture
def edge(tmp_path):
    dry = real_ep("d1", label="unknown", cams=())  # dry run imported through the generic path: synthetic is NOT forced
    dry.source.synthetic_note, dry.conditions["dry_run"] = "DRY RUN: no motion was executed", True
    dry.outcome = Outcome(label="unknown", provenance="unknown", verifier_id="gripper_signals")
    pipe = real_ep("p1", with_meta=False, cams=())  # no start/end time, NaN at the end of one stream, a pipe in a units string
    pipe.streams["act.gripper"].units = "open|closed"
    pipe.streams["obs.ee_pose"].t[-1] = float("nan")
    trav = real_ep("t1", cams=())
    trav.events.append(Event(t=T0, type="annotation", code="CAMERA_SNAPSHOT", message="../../etc/hosts", source="cam"))
    return import_dataset(write_session(tmp_path / "edge", [dry, frame_ep("f1"), pipe, trav]), name="edge", adapter="jsonl", synthetic=False)["dataset_id"]


def test_edge_cases(edge):
    ds = edge
    out = build_card(ds)  # a bare id string is accepted
    f, md = out["facts"], out["markdown"]
    assert build_card([ds, ds])["facts"]["dataset_ids"] == [ds]
    assert f["stats"]["episodes"] == 4 and f["stats"]["real_episodes"] == 4 and f["stats"]["unreadable_artifacts"] == 0
    # an unflagged dry run is reported as counted-real, never claimed otherwise
    rs = f["collection"]["robot_sessions"]
    assert rs == {**rs, "episodes": 4, "executed": 3, "dry_run": 1, "dry_run_counted_as_real": 1} and "WARNING: 1 dry-run" in rs["dry_run_handling"]
    assert "1 dry-run episode(s) (no motion executed) are flagged real" in md
    # frame-index stream: one image per sample, found on disk; the traversal name is referenced but never resolved
    assert f["composition"]["images"] == {"by_camera": {"cam": 5}, "by_mime": {"image/jpeg": 4, "unknown": 1}, "snapshot_events": 2, "frame_stream_samples": 3}
    assert f["stats"]["images"] == {"value": 5, "found_on_disk": 4} and "only 4 of 5 referenced image files" in md
    assert f["composition"]["by_verifier"] == {"gripper_signals": 3, "pose_error": 1} and "pose_error" in md and "encoders" in md
    # NaN timestamps never reach the sidecar or the duration stats; pipes do not break the Markdown table
    json.dumps(f, allow_nan=False)
    assert f["composition"]["duration_s"]["n"] == 4 and f["composition"]["duration_basis"] == {"start_end_time": 3, "stream_span": 1}
    assert "| open\\|closed |" in md and re.search(r"\bNone\b", md) is None and markdown(f) == md
    # a missing artifact is reported, not fatal
    row = store.query("SELECT episode_id, artifact_path FROM episodes WHERE dataset_id=? ORDER BY episode_id LIMIT 1", [ds])[0]
    Path(row["artifact_path"]).unlink()
    f2 = build_card([ds])["facts"]
    assert f2["stats"]["episodes"] == 4 and f2["stats"]["unreadable_artifacts"] == 1 and f2["stats"]["unreadable_episode_ids"] == [row["episode_id"]]
    assert f2["datasets"][0]["unreadable_artifacts"] == 1 and sum(s["episodes"] for s in f2["composition"]["streams"].values()) < sum(s["episodes"] for s in f["composition"]["streams"].values())
    assert "1 of 4 episode artifact(s) could not be read" in markdown(f2)


def test_empty_dataset_human_labels_and_rule_episode_counts(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    (d / "episodes.jsonl").write_text("")
    e = import_dataset(d, name="empty ✓ ünïcode", adapter="jsonl", synthetic=False)["dataset_id"]
    out = build_card([e], tmp_path / "e.md")
    assert "> EMPTY (no episodes) data" in out["markdown"] and "[EMPTY]" in out["markdown"] and out["facts"]["stats"]["success_rate"] is None
    assert "empty ✓ ünïcode" in Path(out["path"]).read_text(encoding="utf-8") and re.search(r"\bNone\b", out["markdown"]) is None
    ds = import_dataset(write_session(tmp_path / "r", [real_ep("r1"), real_ep("r2", label="failure")]), name="r", adapter="jsonl", synthetic=False)["dataset_id"]
    eid = store.query("SELECT episode_id FROM episodes WHERE dataset_id=? AND outcome='success'", [ds])[0]["episode_id"]
    labels.record_label(eid, "failure", "rev1", "GRASP_FAILURE")
    labels.record_label(eid, "success", "rev1")  # latest label per episode wins
    # the same rule firing at two severities on one episode is one episode, not two
    cols = {"dimension": "completeness", "record_ref": None, "t": None, "observed": None, "expected": "x", "explanation": "x",
            "recommendation": "x", "deterministic": 1, "needs_review": 0, "created_at": "2026-01-01T00:00:00+00:00"}
    store.insert_many("quality_findings", [{"finding_id": f"qf_t{i}", "dataset_id": ds, "episode_id": eid, "rule_id": "C001_required_metadata",
                                            "rule_version": "1.0.0", "severity": s, **cols} for i, s in enumerate(("minor", "major"))])
    out = build_card([ds])
    hl = out["facts"]["review"]["human_labels"]
    assert hl["labels"] == 2 and hl["episodes_labelled"] == 1 and hl["by_outcome_latest"] == {"success": 1} and hl["by_category_latest"] == {}
    assert hl["agreement"]["comparable"] == 1 and hl["agreement"]["agree"] == 1 and hl["agreement"]["rate"] == 1.0
    assert "Human outcome labels: 2 on 1 episode(s)" in out["markdown"] and "No human review recorded" not in out["markdown"]
    q = out["facts"]["quality"]["findings_by_rule"]["C001_required_metadata"]
    assert q["findings"] == 2 and q["episodes"] == 1 and q["by_severity"] == {"minor": 1, "major": 1}
