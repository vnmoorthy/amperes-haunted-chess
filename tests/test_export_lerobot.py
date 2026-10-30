import json
import math
import struct
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from PIL import Image

from amperes.export_lerobot import decode_depth, export_lerobot, main
from amperes.fixtures import SYNTHETIC_NOTE
from amperes.ingest.pipeline import import_dataset
from amperes.schema import Episode, Event, Outcome, Ref, RobotInfo, SessionInfo, SourceInfo, Stream, episode_to_json

T0 = 1_790_000_000.0
POSE_A = [350.0, -150.0, 130.0, 0.0, 0.0, -1.0, 0.0]
POSE_B = [350.0, 150.0, 130.0, 0.0, 0.0, -1.0, 0.0]
DEPTH = np.array([[0, 500, 1000, 1500], [2000, 2500, 3000, 3500], [4000, 4500, 5000, 65535]], dtype=np.uint16)


def write_jsonl(path: Path, eps) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    with open(path / "episodes.jsonl", "w") as fh:
        for e in eps:
            fh.write(episode_to_json(e) + "\n")
    return path


def synthetic_ep(eid: str, label="success", joint_dims=6, streams=None) -> Episode:
    """Fixture-style (ops package) episode: joint_pos in rad, gripper, joint_cmd; marked SYNTHETIC."""
    t = [T0 + i / 10 for i in range(30)]
    st = streams if streams is not None else {
        "obs.joint_pos": Stream(kind="observation", t=t, values=[[0.1 * i] * joint_dims for i in range(30)], units="rad", expected_rate_hz=10),
        "obs.gripper": Stream(kind="observation", t=t, values=[[0.04] for _ in t], units="m"),
        "act.joint_cmd": Stream(kind="action", t=t, values=[[0.1 * i + 0.01] * joint_dims for i in range(30)], units="rad")}
    return Episode(episode_id=eid, source=SourceInfo(dataset_id="fx", adapter="fixture", source_record_id=eid, synthetic=True,
                                                     synthetic_note=SYNTHETIC_NOTE),
                   task=Ref(id="pick_place_bin", version="2.1"), robot=RobotInfo(embodiment="6dof-arm", device_id="arm-01"),
                   session=SessionInfo(session_id="s1", workcell_id="wc-A"), start_time=t[0], end_time=t[-1], time_base="unix_s",
                   streams=st, outcome=Outcome(label=label, provenance="operator"))


def real_ep(eid: str, img_dir: Path) -> Episode:
    """viam_control-style pick-place trial: 2 s long, obs end at 1.2 s (gap), two pose commands, one grab, snapshots."""
    t = [round(T0 + i / 10, 3) for i in range(13)]
    tele = lambda d: [[float(10 * i + k) for k in range(d)] for i in range(13)]  # noqa: E731
    img_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 6), (10, 20, 30)).save(img_dir / f"{eid}_cam_pre_grasp.jpg")
    Image.new("RGB", (8, 6), (1, 2, 3)).save(img_dir / f"{eid}_cam2_after_lift.jpg")
    # real viam cameras write the magic bytes DEPTHMAP (the Go constant is *named* VERSIONX); both must decode
    (img_dir / f"{eid}_cam_pre_grasp_depth.dep").write_bytes(b"DEPTHMAP" + struct.pack(">QQ", 4, 3) + DEPTH.astype(">u2").tobytes())
    ev = lambda t_, src, name: Event(t=t_, type="annotation", code="CAMERA_SNAPSHOT", message=name, source=src)  # noqa: E731
    return Episode(
        episode_id=eid,
        source=SourceInfo(dataset_id="robot", adapter="amperes.viam_control@1.0.0", source_uri="viam://test", source_record_id=eid, synthetic=False),
        task=Ref(id="pick_place_real", version="1.0.0"), policy=Ref(id="scripted_pick_place_real", version="baseline"),
        robot=RobotInfo(embodiment="6-joint arm + parallel gripper", device_id="arm", config_id="baseline"),
        session=SessionInfo(session_id="robot-test", workcell_id="test", operator_id="op-onsite"),
        start_time=T0, end_time=T0 + 2.0, time_base="unix_s",
        streams={"obs.joint_pos": Stream(kind="observation", t=t, values=tele(6), units="deg", expected_rate_hz=3.0),
                 "obs.ee_pose": Stream(kind="observation", t=t, values=tele(7), units="mm, orientation-vector, deg"),
                 "act.target_pose": Stream(kind="action", t=[T0 + 0.5, T0 + 1.5], values=[POSE_A, POSE_B]),
                 "act.gripper": Stream(kind="action", t=[T0 + 1.0], values=[[1.0]], valid_range=[0, 1])},
        outcome=Outcome(label="failure", provenance="amperes_verifier", verified=True, reason="GRASP_FAILURE",
                        verifier_id="gripper_signals", measurements={"criteria": {"grab_reported": False}}),
        events=[ev(T0 + 0.5, "cam", f"{eid}_cam_pre_grasp.jpg"), ev(T0 + 0.5, "cam", f"{eid}_cam_pre_grasp_depth.dep"),
                ev(T0 + 1.5, "cam2", f"{eid}_cam2_after_lift.jpg"), ev(T0 + 2.0, "cam", f"{eid}_cam_after_place.jpg")],  # last file missing
        conditions={"trial": 0, "source": "a", "dest": "b", "dry_run": False, "object": "cube",
                    "instruction": "Pick up the cube at spot A and place it at spot B."})


def scan_ep(eid: str, img_dir: Path) -> Episode:
    """viam_tasks-style workspace scan: frame-index stream obs.cam.frame -> images/<eid>_cam_video_<n>.jpg."""
    t = [T0 + 10 + i / 10 for i in range(11)]
    for n in range(3):
        Image.new("RGB", (8, 6), (n, n, n)).save(img_dir / f"{eid}_cam_video_{n:05d}.jpg")
    return Episode(
        episode_id=eid, source=SourceInfo(dataset_id="robot", adapter="amperes.viam_tasks@1.0.0", source_uri="viam://test",
                                          source_record_id=eid, synthetic=False, synthetic_note="DRY RUN: no motion was executed"),
        task=Ref(id="workspace_scan", version="1.0.0"), robot=RobotInfo(embodiment="6-joint arm + parallel gripper"),
        start_time=t[0], end_time=t[-1], time_base="unix_s",
        streams={"obs.joint_pos": Stream(kind="observation", t=t, values=[[1.0] * 6 for _ in t], units="deg"),
                 "obs.ee_pose": Stream(kind="observation", t=t, values=[POSE_A for _ in t]),
                 "obs.ee_pose_settled": Stream(kind="observation", t=t, values=[[1.0] for _ in t]),
                 "obs.cam.frame": Stream(kind="observation", t=[t[0], t[5], t[10]], values=[[0], [1], [2]]),
                 "act.target_pose": Stream(kind="action", t=[t[0]], values=[POSE_A]),
                 "act.gripper": Stream(kind="action", t=[t[0]], values=[[0.0]])},
        outcome=Outcome(label="unknown", provenance="unknown"), conditions={"dry_run": True})


@pytest.fixture
def two_datasets(tmp_path):
    syn = [synthetic_ep("s1"), synthetic_ep("s2", label="failure"),
           synthetic_ep("s3", streams={"act.u": Stream(kind="action", t=[T0, T0 + 1], values=[[0.0], [1.0]])}),
           synthetic_ep("s4", joint_dims=7)]
    ds_syn = import_dataset(write_jsonl(tmp_path / "syn", syn), name="fixture", synthetic=True)["dataset_id"]
    sess = tmp_path / "session"
    real = [real_ep("robot-test-t000", sess / "images"), scan_ep("robot-test-scan000", sess / "images")]
    ds_real = import_dataset(write_jsonl(sess, real), name="ROBOT pick-place", adapter="jsonl", synthetic=False)["dataset_id"]
    return ds_syn, ds_real


def test_export_layout_frames_images_and_metadata(two_datasets, tmp_path):
    ds_syn, ds_real = two_datasets
    out = tmp_path / "lerobot"
    s = export_lerobot([ds_syn, ds_real], out, fps=2.0)
    assert s["total_episodes"] == 4 and s["total_tasks"] == 3 and s["synthetic_episodes"] == 2 and s["dry_run_episodes"] == 1
    reasons = {x["episode_id"]: x["reason"] for x in s["skipped"]}
    assert len(reasons) == 2 and any("no observation stream" in r for r in reasons.values()) and any("7 dims" in r for r in reasons.values())
    assert s["images_missing"] == 1 and "robot-test-t000_cam_after_place.jpg" in s["images_missing_examples"][0]
    for f in ("meta/info.json", "meta/episodes.jsonl", "meta/tasks.jsonl", "meta/stats.json", "README.md",
              *(f"data/chunk-000/episode_{i:06d}.parquet" for i in range(4))):
        assert (out / f).exists(), f
    info = json.loads((out / "meta/info.json").read_text())
    assert info["codebase_version"] == "v2.1" and info["fps"] == 2.0 and info["total_frames"] == s["total_frames"]
    assert info["data_path"] == "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet" and info["chunks_size"] == 1000
    assert info["features"]["observation.state"] == {"dtype": "float32", "shape": [6], "names": [f"joint_{i}" for i in range(6)]}
    assert info["features"]["observation.images.cam"]["dtype"] == "image" and info["features"]["observation.images.cam"]["shape"] == [6, 8, 3]
    assert info["features"]["observation.images.cam_depth"]["shape"] == [3, 4, 1] and "observation.images.cam2" in info["features"]
    assert "per-frame" in info["amperes"]["note"] and info["amperes"]["code_digest"] and info["amperes"]["exported_at"]
    assert {d["dataset_id"] for d in info["amperes"]["datasets"]} == {ds_syn, ds_real}
    # parquet schema
    tab = pq.read_table(out / "data/chunk-000/episode_000000.parquet")
    sch = tab.schema
    for k in ("observation.state", "observation.ee_pose", "action", "action.gripper"):
        assert sch.field(k).type == pa.list_(pa.float32())
    assert sch.field("timestamp").type == pa.float32() and sch.field("frame_index").type == pa.int64()
    assert sch.field("index").type == pa.int64() and sch.field("task_index").type == pa.int64()
    assert sch.field("observation.images.cam").type == pa.string() and sch.field("observation.images.cam_depth").type == pa.string()
    # episodes.jsonl
    eps = [json.loads(l) for l in (out / "meta/episodes.jsonl").read_text().splitlines()]
    assert [e["episode_index"] for e in eps] == [0, 1, 2, 3]
    real = next(e for e in eps if e["source_episode_id"] == "robot-test-t000")
    scan = next(e for e in eps if e["source_episode_id"] == "robot-test-scan000")
    assert real["outcome"] == "failure" and real["failure_category"] == "GRASP_FAILURE" and real["verified"] is True
    assert real["tasks"] == ["Pick up the cube at spot A and place it at spot B."] and real["dataset_id"] == ds_real
    assert real["synthetic"] is False and real["dry_run"] is False and real["length"] == 5 and isinstance(real["quality_status"], str)
    assert real["conditions"]["object"] == "cube" and real["nan_frames"] == {"observation.state": 1, "observation.ee_pose": 1, "action": 1, "action.gripper": 2}
    assert scan["tasks"] == ["workspace_scan"] and scan["dry_run"] is True and scan["image_frames"]["observation.images.cam"] >= 3
    for e in eps:
        if e["dataset_id"] == ds_syn:
            assert e["synthetic"] is True and e["synthetic_note"] == SYNTHETIC_NOTE and e["units_converted"] == {"observation.state": "rad->deg"}
    tasks = [json.loads(l) for l in (out / "meta/tasks.jsonl").read_text().splitlines()]
    assert {t["task"] for t in tasks} == {"pick_place_bin", real["tasks"][0], "workspace_scan"} and [t["task_index"] for t in tasks] == [0, 1, 2]
    # real episode frames: obs nearest-within-1/fps, forward-filled actions, image paths
    r = pq.read_table(out / f"data/chunk-000/episode_{real['episode_index']:06d}.parquet").to_pydict()
    assert r["timestamp"] == pytest.approx([0, 0.5, 1.0, 1.5, 2.0]) and r["frame_index"] == [0, 1, 2, 3, 4]
    assert r["episode_index"] == [real["episode_index"]] * 5 and r["index"] == list(range(real["index_start"], real["index_start"] + 5))
    assert all(math.isnan(v) for v in r["action"][0]) and r["action"][1] == POSE_A and r["action"][2] == POSE_A and r["action"][3] == POSE_B
    assert [g[0] for g in r["action.gripper"]] == [0, 0, 1, 1, 1]
    assert r["observation.state"][0] == [0, 1, 2, 3, 4, 5] and r["observation.state"][3] == [120, 121, 122, 123, 124, 125]  # t=1.5 -> sample at 1.2
    assert all(math.isnan(v) for v in r["observation.state"][4]) and all(math.isnan(v) for v in r["observation.ee_pose"][4])
    assert r["observation.images.cam"][1] == f"images/cam/episode_{real['episode_index']:06d}/frame_000001.jpg"
    assert (out / r["observation.images.cam"][1]).exists() and r["observation.images.cam"][4] == ""
    assert r["observation.images.cam2"][3].endswith("frame_000003.jpg") and (out / r["observation.images.cam2"][3]).exists()
    dp = r["observation.images.cam_depth"][1]
    assert dp == f"images/cam/episode_{real['episode_index']:06d}/frame_000001_depth.png"
    back = np.array(Image.open(out / dp))
    assert back.dtype == np.uint16 and back.tolist() == DEPTH.tolist()
    # frame-index stream images (viam_tasks style)
    sc = pq.read_table(out / f"data/chunk-000/episode_{scan['episode_index']:06d}.parquet").to_pydict()
    assert sc["observation.images.cam"][0].endswith("frame_000000.jpg") and sc["observation.images.cam"][1].endswith("frame_000001.jpg")
    assert "observation.images.cam_depth" in sc and set(sc["observation.images.cam_depth"]) == {""}
    # synthetic episode: rad -> deg, NaN ee_pose, NaN action, gripper 0
    syn = next(e for e in eps if e["source_episode_id"] == "s1")
    t1 = pq.read_table(out / f"data/chunk-000/episode_{syn['episode_index']:06d}.parquet").to_pydict()
    assert t1["observation.state"][1] == pytest.approx([math.degrees(0.1 * 5)] * 6, abs=1e-3)  # t=0.5 -> sample 5
    assert all(math.isnan(v) for v in t1["observation.ee_pose"][0]) and all(math.isnan(v) for v in t1["action"][-1])
    assert set(g[0] for g in t1["action.gripper"]) == {0.0}
    # stats and README
    st = json.loads((out / "meta/stats.json").read_text())
    assert len(st["observation.state"]["min"]) == 6 and st["observation.state"]["count"] == [s["total_frames"]]
    assert st["action"]["min"][1] == -150.0 and st["action"]["max"][1] == 150.0 and st["observation.ee_pose"]["finite_count"][0] < s["total_frames"]
    readme = (out / "README.md").read_text()
    assert "SYNTHETIC" in readme and ds_syn in readme and ds_real in readme and "GRASP_FAILURE" in readme
    assert info["amperes"]["code_digest"] in readme and "Skipped episodes (2)" in readme


def test_cameras_filter_overwrite_and_cli(two_datasets, tmp_path, capsys):
    ds_syn, ds_real = two_datasets
    out = tmp_path / "one_cam"
    s = export_lerobot([ds_real], out, fps=2.0, cameras=("cam",))
    assert s["images_ignored_cameras"] == {"cam2": 1} and "observation.images.cam2" not in json.loads((out / "meta/info.json").read_text())["features"]
    assert s["total_episodes"] == 2 and s["skipped"] == []
    with pytest.raises(FileExistsError):
        export_lerobot([ds_real], out)
    assert export_lerobot([ds_real], out, overwrite=True)["total_episodes"] == 2
    with pytest.raises(ValueError):
        export_lerobot(["nope"], tmp_path / "x")
    assert main([str(tmp_path / "cli"), ds_syn, ds_real, "--fps", "3", "--cameras", "cam"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["total_episodes"] == 4 and printed["fps"] == 3.0 and len(printed["skipped"]) == 2
    assert main([str(tmp_path / "cli"), ds_real]) == 2  # refuses to overwrite silently
    assert main([str(tmp_path / "cli2"), "nope"]) == 2


def test_decode_depth_rejects_garbage():
    for bad in (b"not a depth file", b"VERSIONX", b"DEPTHMAPX" + struct.pack(">Q", 4), b"VERSIONX" + struct.pack(">QQ", 100, 100) + b"\x00" * 10):
        with pytest.raises(ValueError):
            decode_depth(bad)
    for magic in (b"VERSIONX", b"DEPTHMAP"):
        assert decode_depth(magic + struct.pack(">QQ", 4, 3) + DEPTH.astype(">u2").tobytes()).tolist() == DEPTH.tolist()


def plain_ep(eid: str, streams=None, events=(), **kw) -> Episode:
    t = [T0 + i / 10 for i in range(11)]
    st = {"obs.joint_pos": Stream(kind="observation", t=t, values=[[1.0] * 6 for _ in t], units="deg"),
          "obs.ee_pose": Stream(kind="observation", t=t, values=[POSE_A for _ in t]),
          "act.target_pose": Stream(kind="action", t=[t[0]], values=[POSE_A]), "act.gripper": Stream(kind="action", t=[t[0]], values=[[0.0]])}
    st.update(streams or {})
    d = dict(episode_id=eid, source=SourceInfo(dataset_id="x", adapter="t", source_uri="viam://m", source_record_id=eid, synthetic=False),
             task=Ref(id="task"), robot=RobotInfo(embodiment="arm"), start_time=t[0], end_time=t[-1], time_base="unix_s", streams=st,
             events=list(events), outcome=Outcome(label="success", provenance="operator"))
    d.update(kw)
    return Episode(**d)


def snap(t, src, name):
    return Event(t=t, type="annotation", code="CAMERA_SNAPSHOT", message=name, source=src)


def test_image_names_are_plain_files_inside_the_session_images_dir(tmp_path):
    sess = tmp_path / "sess"
    (sess / "images").mkdir(parents=True)
    secret = tmp_path / "SECRET.txt"
    secret.write_text("TOPSECRET")
    (sess / "leak.jpg").write_bytes(b"not inside images/")
    Image.new("RGB", (4, 4)).save(sess / "images" / "e1_cam_ok.jpg")
    e = plain_ep("e1", events=[snap(T0, "cam", str(secret)), snap(T0 + 0.5, "cam", "../leak.jpg"), snap(T0 + 1.0, "cam", "e1_cam_ok.jpg")])
    ds = import_dataset(write_jsonl(sess, [e]), name="trav", adapter="jsonl", synthetic=False)["dataset_id"]
    out = tmp_path / "out"
    s = export_lerobot([ds], out, fps=2.0)
    files = sorted(p.relative_to(out).as_posix() for p in (out / "images").rglob("*") if p.is_file())
    assert files == ["images/cam/episode_000000/frame_000002.jpg"]
    assert s["images_missing"] == 2 and all("rejected" in m for m in s["images_missing_examples"])
    assert "TOPSECRET" not in (out / "README.md").read_text() and not any(b"TOPSECRET" in (out / f).read_bytes() for f in files)
    r = pq.read_table(out / "data/chunk-000/episode_000000.parquet").to_pydict()
    assert r["observation.images.cam"] == ["", "", "images/cam/episode_000000/frame_000002.jpg"]


def test_nan_frame_index_and_all_missing_camera(tmp_path):
    sess = tmp_path / "sess"
    (sess / "images").mkdir(parents=True)
    Image.new("RGB", (4, 4)).save(sess / "images" / "e2_cam_video_00000.jpg")
    e = plain_ep("e2", streams={"obs.cam.frame": Stream(kind="observation", t=[T0, T0 + 0.5], values=[[0.0], [float("nan")]])},
                 events=[snap(T0, "cam2", "e2_cam2_gone.jpg")])
    ds = import_dataset(write_jsonl(sess, [e]), name="nanframe", adapter="jsonl", synthetic=False)["dataset_id"]
    s = export_lerobot([ds], tmp_path / "out", fps=2.0)
    # frame 0 (t=T0) is the nearest image within 1/fps of grid points 0 and 1; the NaN index is dropped, not fatal
    assert s["skipped"] == [] and s["total_episodes"] == 1 and s["image_frames"] == {"observation.images.cam": 2} and s["images_missing"] == 1
    feats = json.loads((tmp_path / "out/meta/info.json").read_text())["features"]
    assert "observation.images.cam" in feats and "observation.images.cam2" not in feats  # no frames -> no feature with a null shape


def test_overwrite_only_replaces_a_previous_export_and_validates_first(two_datasets, tmp_path):
    _, ds_real = two_datasets
    out = tmp_path / "out"
    export_lerobot([ds_real], out, fps=2.0)
    with pytest.raises(ValueError):
        export_lerobot(["nope"], out, overwrite=True)
    assert (out / "meta/info.json").exists()  # bad input never wipes the previous export
    with pytest.raises(ValueError):
        export_lerobot([], tmp_path / "empty")
    precious = tmp_path / "docs"
    precious.mkdir()
    (precious / "notes.txt").write_text("keep")
    with pytest.raises(FileExistsError):
        export_lerobot([ds_real], precious, overwrite=True)
    assert (precious / "notes.txt").read_text() == "keep"


def test_relative_time_base_bogus_end_time_and_ragged_rows(tmp_path):
    rel = [i / 10 for i in range(11)]
    e_rel = plain_ep("rel", time_base="episode_relative_s", start_time=T0, end_time=T0 + 1.0,
                     streams={"obs.joint_pos": Stream(kind="observation", t=rel, values=[[2.0] * 6 for _ in rel], units="deg"),
                              "obs.ee_pose": Stream(kind="observation", t=rel, values=[POSE_A for _ in rel]),
                              "act.target_pose": Stream(kind="action", t=[0.0], values=[POSE_B]), "act.gripper": Stream(kind="action", t=[0.0], values=[[1.0]])})
    e_huge = plain_ep("huge", end_time=T0 + 1e7)
    e_rag = plain_ep("rag", streams={"obs.joint_pos": Stream(kind="observation", t=[T0, T0 + 0.1], values=[[1.0] * 6, [1.0] * 5], units="deg")})
    ds = import_dataset(write_jsonl(tmp_path / "sess", [e_rel, e_huge, e_rag]), name="edge", adapter="jsonl", synthetic=False)["dataset_id"]
    s = export_lerobot([ds], tmp_path / "out", fps=2.0)
    assert s["total_episodes"] == 1 and s["nan_frames"] == {"observation.state": 0, "observation.ee_pose": 0, "action": 0, "action.gripper": 0}
    reasons = sorted(x["reason"] for x in s["skipped"])
    assert len(reasons) == 2 and "exceeds" in reasons[0] and "ragged" in reasons[1]
    r = pq.read_table(tmp_path / "out/data/chunk-000/episode_000000.parquet").to_pydict()
    assert r["observation.state"] == [[2.0] * 6] * 3 and r["action"] == [POSE_B] * 3 and r["timestamp"] == pytest.approx([0, 0.5, 1.0])


def test_latest_human_label_is_exported_without_replacing_outcome(two_datasets, tmp_path):
    from amperes import labels, store

    _, ds_real = two_datasets
    eid = store.query("SELECT episode_id FROM episodes WHERE dataset_id=? ORDER BY start_time", [ds_real])[0]["episode_id"]
    labels.record_label(eid, "failure", "rev-1", "SLIPPAGE", "stale")
    labels.record_label(eid, "success", "rev-2", None, "latest")
    s = export_lerobot([ds_real], tmp_path / "out", fps=2.0)
    assert s["human_labelled_episodes"] == 1
    eps = {e["episode_id"]: e for e in (json.loads(l) for l in (tmp_path / "out/meta/episodes.jsonl").read_text().splitlines())}
    h = eps[eid]["human_label"]
    assert h["outcome"] == "success" and h["category"] is None and h["reviewer"] == "rev-2" and h["n_labels"] == 2 and h["agrees_with_verifier"] is False
    assert eps[eid]["outcome"] == "failure" and eps[eid]["failure_category"] == "GRASP_FAILURE"
    assert all(e["human_label"] is None for e in eps.values() if e["episode_id"] != eid)
    assert "human_label" in (tmp_path / "out/README.md").read_text()


def test_fixture_ops_package_exports_with_synthetic_flags(tmp_path):
    from amperes.fixtures import ops_package

    d = tmp_path / "ops"
    ops_package(d)
    ds = import_dataset(d, synthetic=True)["dataset_id"]
    s = export_lerobot([ds], tmp_path / "ops_out", fps=1.0)
    assert s["total_episodes"] == 480 and s["skipped"] == [] and s["synthetic_episodes"] == 480 and s["state_rad_to_deg_episodes"] == 480
    assert s["nan_frames"]["observation.ee_pose"] == s["total_frames"] and s["nan_frames"]["action"] == s["total_frames"]
    assert s["nan_frames"]["observation.state"] > 0  # the planted truncated recordings leave trailing frames without observations
    assert s["image_frames"] == {} and s["total_tasks"] == 1
    eps = [json.loads(l) for l in (tmp_path / "ops_out/meta/episodes.jsonl").read_text().splitlines()]
    assert len(eps) == 480 and all(e["synthetic"] for e in eps) and {e["outcome"] for e in eps} <= {"success", "failure", None}
    assert sum(e["quality_status"] == "infrastructure_error" for e in eps) > 0  # kept, but labelled so a buyer can filter
    assert "SYNTHETIC DATA WARNING" in (tmp_path / "ops_out/README.md").read_text()
    assert len(list((tmp_path / "ops_out/data/chunk-000").glob("*.parquet"))) == 480
