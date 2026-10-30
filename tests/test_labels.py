import struct

import pytest
from fastapi.testclient import TestClient

from amperes import labels, store
from amperes.ingest.pipeline import import_dataset
from amperes.schema import Episode, Event, Outcome, SessionInfo, SourceInfo, Stream, episode_to_json
from amperes.stats import wilson

T0 = 1_790_000_000.0
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9"
DEP = b"VERSIONX" + struct.pack(">QQ", 4, 2) + struct.pack(">8H", 0, 500, 600, 700, 800, 900, 1000, 0)


def ep(eid, label, t, verified=True, snaps=()):
    ts = [t + i / 3 for i in range(12)]
    return Episode(episode_id=eid, source=SourceInfo(dataset_id="r", adapter="test", source_record_id=eid, synthetic=False),
                   task={"id": "pick_place_real", "version": "1"}, session=SessionInfo(session_id="s", workcell_id="arm"),
                   start_time=ts[0], end_time=ts[-1], time_base="unix_s",
                   streams={"obs.q": Stream(kind="observation", t=ts, values=[[0.1 * i] for i in range(12)], expected_rate_hz=3),
                            "act.u": Stream(kind="action", t=ts, values=[[0.0] for _ in ts], expected_rate_hz=3)},
                   outcome=Outcome(label=label, provenance="amperes_verifier" if verified else "unknown", verified=verified,
                                   measurements={"criteria": {"ok": label == "success"}} if verified else {}),
                   events=[Event(t=t, type="annotation", code="CAMERA_SNAPSHOT", message=f, source="cam") for f in snaps],
                   conditions={"instruction": f"Pick up the block ({eid})."})


@pytest.fixture
def session(tmp_path):
    d = tmp_path / "sess"
    (d / "images").mkdir(parents=True)
    for f in ("e1_cam_pre_grasp.jpg", "e1_cam_after_place.jpg", "e2_cam_after_lift.jpg"):
        (d / "images" / f).write_bytes(JPG)
    (d / "images" / "e1_cam_pre_grasp_depth.dep").write_bytes(DEP)
    (d / "images" / "unreferenced.jpg").write_bytes(JPG)
    (tmp_path / "secret.txt").write_text("x")
    eps = [ep("e1", "success", T0, snaps=("e1_cam_pre_grasp_depth.dep", "e1_cam_after_place.jpg", "e1_cam_pre_grasp.jpg")),
           ep("e2", "failure", T0 + 100, snaps=("e2_cam_after_lift.jpg",)), ep("e3", "unknown", T0 + 200, verified=False)]
    with open(d / "episodes.jsonl", "w") as fh:
        for e in eps:
            fh.write(episode_to_json(e) + "\n")
    r = import_dataset(d, name="robot", adapter="jsonl")
    rows = store.query("SELECT episode_id, outcome FROM episodes WHERE dataset_id=? ORDER BY start_time", [r["dataset_id"]])
    return r["dataset_id"], {x["outcome"]: x["episode_id"] for x in rows}


def test_label_roundtrip_queue_and_summary(session):
    from amperes.api import app

    ds, ids = session
    c = TestClient(app)
    q = c.get("/api/labels/queue").json()
    assert [x["episode_id"] for x in q] == [ids["unknown"], ids["failure"], ids["success"]]  # newest first
    first = next(x for x in q if x["episode_id"] == ids["success"])
    assert first["instruction"] == "Pick up the block (e1)." and first["images"] == ["e1_cam_pre_grasp.jpg", "e1_cam_after_place.jpg"]
    r = c.post(f"/api/episodes/{ids['success']}/label", json={"outcome": "success", "reviewer": "rev1", "note": "clean"})
    assert r.status_code == 200 and r.json()["agrees_with_verifier"] == 1 and r.json()["verifier_outcome"] == "success"
    d = c.get(f"/api/episodes/{ids['success']}").json()
    assert d["human_label"]["outcome"] == "success" and d["human_label"]["reviewer"] == "rev1" and len(d["human_labels"]) == 1
    assert len(c.get(f"/api/labels/queue?dataset_id={ds}").json()) == 2
    # human disagrees with the verifier; latest label wins but history is kept
    c.post(f"/api/episodes/{ids['failure']}/label", json={"outcome": "failure", "category": "SLIPPAGE", "reviewer": "rev1"})
    r = c.post(f"/api/episodes/{ids['failure']}/label", json={"outcome": "success", "reviewer": "rev2", "note": "it was in the bin"})
    assert r.json()["agrees_with_verifier"] == 0 and r.json()["category"] is None
    d = c.get(f"/api/episodes/{ids['failure']}").json()
    assert len(d["human_labels"]) == 2 and d["human_label"]["reviewer"] == "rev2"
    # 'unknown' on either side is not comparable
    r = c.post(f"/api/episodes/{ids['unknown']}/label", json={"outcome": "failure", "category": "DROP", "reviewer": "rev1"})
    assert r.json()["agrees_with_verifier"] is None
    assert c.get("/api/labels/queue").json() == []
    s = c.get(f"/api/labels/summary?dataset_id={ds}").json()
    assert (s["labelled"], s["unlabelled"], s["labels_total"]) == (3, 0, 4)
    a = s["agreement"]
    assert (a["comparable"], a["agree"], a["rate"]) == (2, 1, 0.5) and a["ci95"] == pytest.approx(list(wilson(1, 2)))
    assert [x["episode_id"] for x in s["disagreements"]] == [ids["failure"]] and s["disagreements"][0]["verifier_outcome"] == "failure"
    assert s["by_outcome"] == {"success": 2, "failure": 1} and s["by_category"] == {"DROP": 1}
    assert labels.agreement("failure", "timeout") == 1 and labels.agreement("success", "invalid") is None


def test_label_validation(session):
    from amperes.api import app

    ds, ids = session
    c = TestClient(app)
    eid = ids["success"]
    assert c.post(f"/api/episodes/{eid}/label", json={"outcome": "success", "reviewer": "someone@example.com"}).status_code == 400
    assert c.post(f"/api/episodes/{eid}/label", json={"outcome": "success", "reviewer": ""}).status_code == 422
    assert c.post(f"/api/episodes/{eid}/label", json={"outcome": "maybe", "reviewer": "r"}).status_code == 422
    assert c.post(f"/api/episodes/{eid}/label", json={"outcome": "failure", "category": "BAD", "reviewer": "r"}).status_code == 400
    assert c.post("/api/episodes/nope/label", json={"outcome": "success", "reviewer": "r"}).status_code == 404
    assert c.post(f"/api/episodes/{eid}/label", json={"outcome": "success", "reviewer": "r", "extra": 1}).status_code == 422
    assert store.query("SELECT COUNT(*) n FROM human_labels")[0]["n"] == 0


def test_image_route_serves_only_referenced_files(session, tmp_path):
    from amperes.api import app

    ds, ids = session
    c = TestClient(app)
    e1 = ids["success"]
    r = c.get(f"/api/episodes/{e1}/images/e1_cam_pre_grasp.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content == JPG
    r = c.get(f"/api/episodes/{e1}/images/e1_cam_pre_grasp_depth.dep")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content.startswith(b"\x89PNG")
    assert struct.unpack(">II", r.content[16:24]) == (4, 2)
    for bad in ("..%2F..%2Fsecret.txt", "%2E%2E%2Fepisodes.jsonl", "..%2Fimages%2Fe1_cam_pre_grasp.jpg", "unreferenced.jpg",
                "e2_cam_after_lift.jpg", "missing.jpg", ".hidden.jpg", "e1_cam_pre_grasp.txt"):
        assert c.get(f"/api/episodes/{e1}/images/{bad}").status_code == 404, bad
    assert c.get(f"/api/episodes/{e1}/images/../../secret.txt").status_code == 404
    assert c.get("/api/episodes/nope/images/e1_cam_pre_grasp.jpg").status_code == 404
    with pytest.raises(LookupError):
        labels.image_path(e1, "../secret.txt")
    assert (tmp_path / "secret.txt").exists()


def test_depth_png_normalization():
    png = labels.depth_png(DEP)
    assert png.startswith(b"\x89PNG") and struct.unpack(">II", png[16:24]) == (4, 2)
    import zlib

    n = struct.unpack(">I", png[33:37])[0]
    rows = zlib.decompress(png[41:41 + n])
    px = [list(rows[1 + 5 * i: 5 * (i + 1)]) for i in range(2)]
    assert px[0][0] == 0 and px[1][3] == 0  # no-return pixels stay black
    assert px[0][1] > px[0][2] > px[0][3] > px[1][0] > px[1][1] > px[1][2]  # nearer = brighter
    with pytest.raises(ValueError):
        labels.depth_png(b"NOTDEPTH" + DEP[8:])
    with pytest.raises(ValueError):
        labels.depth_png(DEP[:-2])


def test_depth_png_accepts_real_viam_depthmap_magic(session, tmp_path):
    """Files written by viam's camera start with the bytes DEPTHMAP (the Go constant is *named* VERSIONX)."""
    from amperes.api import app

    real = b"DEPTHMAP" + DEP[8:]
    assert labels.depth_png(real)[41:] == labels.depth_png(DEP)[41:]
    (tmp_path / "sess" / "images" / "e1_cam_pre_grasp_depth.dep").write_bytes(real)
    r = TestClient(app).get(f"/api/episodes/{session[1]['success']}/images/e1_cam_pre_grasp_depth.dep")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    with pytest.raises(ValueError):
        labels.depth_png(b"DEPTHMAPX" + DEP[9:])


def test_image_route_rejects_symlinks_nul_bytes_and_survives_missing_artifacts(session, tmp_path):
    from amperes.api import app

    ds, ids = session
    c, e1 = TestClient(app), ids["success"]
    (tmp_path / "sess" / "images" / "e1_link.jpg").symlink_to(tmp_path / "secret.txt")  # name passes the prefix rule
    assert c.get(f"/api/episodes/{e1}/images/e1_link.jpg").status_code == 404
    for bad in ("e1_\x00.jpg", "/etc/hosts", "e1_/../secret.txt"):
        with pytest.raises(LookupError):
            labels.image_path(e1, bad)
    assert c.get(f"/api/episodes/{e1}/images/e1_%00.jpg").status_code == 404
    # a deleted/corrupt artifact must neither 500 the image route nor take the whole queue down
    p = store.query("SELECT artifact_path FROM episodes WHERE episode_id=?", [e1])[0]["artifact_path"]
    store.safe_artifact_path(p).write_bytes(b"not gzip")
    assert c.get(f"/api/episodes/{e1}/images/e1_cam_pre_grasp.jpg").status_code == 404
    q = c.get("/api/labels/queue").json()
    broken = next(x for x in q if x["episode_id"] == e1)
    assert len(q) == 3 and broken["images"] == [] and broken["instruction"] is None and broken["outcome"] == "success"
    assert c.post(f"/api/episodes/{e1}/label", json={"outcome": "success", "reviewer": "r"}).status_code == 200


def test_queue_puts_real_datasets_before_synthetic_ones(session, tmp_path):
    from amperes.api import app

    ds, _ = session
    d = tmp_path / "synth"
    d.mkdir()
    with open(d / "episodes.jsonl", "w") as fh:
        for e in (ep("s1", "success", T0 + 1000), ep("s2", "failure", T0 + 2000)):  # newer than every real episode
            fh.write(episode_to_json(e) + "\n")
    synth = import_dataset(d, name="synthetic", adapter="jsonl", synthetic=True)["dataset_id"]
    c = TestClient(app)
    q = c.get("/api/labels/queue").json()
    assert [x["dataset_id"] for x in q] == [ds] * 3 + [synth] * 2 and all(bool(x["synthetic"]) == (x["dataset_id"] == synth) for x in q)
    assert q[0]["start_time"] > q[2]["start_time"] and q[3]["start_time"] > q[4]["start_time"]  # newest first within each block
    assert [x["dataset_id"] for x in c.get(f"/api/labels/queue?dataset_id={synth}").json()] == [synth, synth]


def test_reviewer_is_stripped(session):
    from amperes.api import app

    c, eid = TestClient(app), session[1]["success"]
    assert c.post(f"/api/episodes/{eid}/label", json={"outcome": "success", "reviewer": "   "}).status_code == 400
    assert c.post(f"/api/episodes/{eid}/label", json={"outcome": "success", "reviewer": "  rev1 "}).json()["reviewer"] == "rev1"
    assert c.get(f"/api/episodes/{eid}").json()["human_label"]["reviewer"] == "rev1"


def test_delete_dataset_removes_labels(session):
    ds, ids = session
    labels.record_label(ids["success"], "success", "rev1")
    assert store.query("SELECT COUNT(*) n FROM human_labels WHERE dataset_id=?", [ds])[0]["n"] == 1
    assert store.delete_dataset(ds)["human_labels"] == 1
    assert store.query("SELECT COUNT(*) n FROM human_labels")[0]["n"] == 0
