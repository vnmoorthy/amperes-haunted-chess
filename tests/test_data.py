import csv
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from amperes import diagnostics, incidents, ops, review, store
from amperes.cli import main as cli
from amperes.economics import estimate
from amperes.ingest.pipeline import import_dataset, source_fingerprint, validate_only
from amperes.quality import check_episode, dataset_findings, episode_status
from amperes.schema import Episode, Outcome, SessionInfo, SourceInfo, Stream, episode_to_json

T0 = 1_790_000_000.0


def ep(eid="e1", n=50, rate=10.0, label="success", session="s1", **kw) -> Episode:
    t = [T0 + i / rate for i in range(n)]
    e = Episode(episode_id=eid, source=SourceInfo(dataset_id="t", adapter="test", source_record_id=eid, synthetic=True),
                task={"id": "pick", "version": "1"}, policy={"id": "p", "version": "1"},
                session=SessionInfo(session_id=session, workcell_id="wc1"), start_time=t[0], end_time=t[-1], time_base="unix_s",
                streams={"obs.q": Stream(kind="observation", t=list(t), values=[[0.01 * i, 0.5] for i in range(n)], expected_rate_hz=rate,
                                         valid_range=[-1, 1]),
                         "act.u": Stream(kind="action", t=list(t), values=[[0.0, 0.1] for _ in range(n)], expected_rate_hz=rate)},
                outcome=None if label is None else Outcome(label=label, provenance="source_label"))
    for k, v in kw.items():
        setattr(e, k, v)
    return e


def rules(e):
    return {f.rule_id for f in check_episode(e)}


def write_jsonl(path: Path, eps, extra_lines=()):
    path.mkdir(parents=True, exist_ok=True)
    with open(path / "episodes.jsonl", "w") as fh:
        for e in eps:
            fh.write(episode_to_json(e) + "\n")
        for line in extra_lines:
            fh.write(line + "\n")
    return path


# ----------------------------------------------------------------------------- schema / import
def test_schema_validation_errors_are_specific(tmp_path):
    bad = json.loads(episode_to_json(ep()))
    bad["streams"]["obs.q"]["kind"] = "camera"
    d = write_jsonl(tmp_path / "ds", [ep("ok")], [json.dumps(bad), "{not json", "[1,2]"])
    r = validate_only(d)
    assert r["valid_records"] == 1 and r["invalid_records"] == 3
    fields = [e["field"] for x in r["errors"] for e in x["errors"]]
    assert "streams.obs.q.kind" in fields
    assert any("invalid JSON" in e["message"] for x in r["errors"] for e in x["errors"])


def test_import_valid_and_malformed_records(tmp_path):
    d = write_jsonl(tmp_path / "ds", [ep("a"), ep("b", label="failure")], ["{broken"])
    r = import_dataset(d, name="t")
    assert r["episodes_imported"] == 2 and r["records_rejected"] == 1 and r["source_unchanged"]
    assert store.query("SELECT COUNT(*) n FROM episodes WHERE dataset_id=?", [r["dataset_id"]])[0]["n"] == 2
    again = import_dataset(d, name="t", dataset_id=r["dataset_id"] + "x")
    ids1 = {x["episode_id"] for x in store.query("SELECT episode_id FROM episodes WHERE dataset_id=?", [r["dataset_id"]])}
    ids2 = {x["episode_id"] for x in store.query("SELECT episode_id FROM episodes WHERE dataset_id=?", [again["dataset_id"]])}
    assert ids1.isdisjoint(ids2)  # ids are stable per (dataset, source record) and never collide across datasets


def test_csv_package_import_and_bad_rows(tmp_path):
    d = tmp_path / "pkg"
    d.mkdir()
    with open(d / "episodes.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["episode_id", "task_id", "policy_id", "session_id", "start_time", "end_time", "outcome_label"])
        w.writerow(["x1", "pick", "p", "s1", T0, T0 + 1, "success"])
        w.writerow(["", "pick", "p", "s1", T0, T0 + 1, "success"])
    with open(d / "telemetry.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["episode_id", "timestamp", "stream", "v0", "v1"])
        for i in range(11):
            w.writerow(["x1", T0 + i * 0.1, "obs.q", i, ""])
            w.writerow(["x1", T0 + i * 0.1, "act.u", 0.1, 0.2])
        w.writerow(["x1", "not-a-number", "obs.q", 1, ""])
    (d / "streams.csv").write_text("stream,kind,units,expected_rate_hz,valid_low,valid_high,max_unchanged_s\nobs.q,observation,m,10,,,\nact.u,action,,10,,,\n")
    r = import_dataset(d)
    assert r["episodes_imported"] == 1 and r["records_rejected"] == 1
    e = store.query("SELECT * FROM episodes WHERE dataset_id=?", [r["dataset_id"]])[0]
    assert e["session_id"] == "s1" and e["outcome"] == "success" and e["verified"] == 0
    from amperes.schema import read_episode

    full = read_episode(Path(e["artifact_path"]))
    assert full.streams["obs.q"].kind == "observation" and len(full.streams["obs.q"].values[0]) == 1
    assert full.extra["import_warnings"] and "telemetry.csv:row" in full.extra["import_warnings"][0]["record_ref"]
    assert full.robot.device_id is None  # absent in source -> stays unknown, never invented


# ----------------------------------------------------------------------------- rules
def test_clean_episode_has_no_defect_findings():
    assert rules(ep()) <= {"O001_unverified_success"}
    assert episode_status(ep(), check_episode(ep())) == "valid_success_unverified"


def test_timestamp_rules():
    e = ep()
    e.streams["obs.q"].t[10], e.streams["obs.q"].t[11] = e.streams["obs.q"].t[11], e.streams["obs.q"].t[10]
    assert "T001_non_monotonic" in rules(e)
    e = ep()
    e.streams["obs.q"].t[5] = e.streams["obs.q"].t[4]
    assert "T002_duplicate_timestamps" in rules(e)
    e = ep()
    s = e.streams["obs.q"]
    s.t, s.values = s.t[:10] + s.t[20:], s.values[:10] + s.values[20:]
    assert "T003_time_gap" in rules(e)
    e = ep()
    e.streams["act.u"].t = [t + 0.03 for t in e.streams["act.u"].t]
    assert "T005_obs_action_misalignment" in rules(e)


def test_numerical_rules():
    e = ep()
    e.streams["obs.q"].values[7] = [float("nan"), 0.5]
    fs = check_episode(e)
    f = next(x for x in fs if x.rule_id == "S001_non_finite")
    assert f.severity == "critical" and f.record_ref == "obs.q[7]" and f.rule_version and f.deterministic
    assert episode_status(e, fs) == "invalid_data"
    e = ep()
    e.streams["obs.q"].values[3] = [5.0, 0.5]
    assert "S002_out_of_range" in rules(e)
    e = ep()
    e.streams["obs.q"].values[3] = [None, 0.5]
    assert "C007_missing_samples" in rules(e)


def test_outcome_label_validation_uses_declared_criteria():
    e = ep(label="success")
    e.outcome.measurements = {"criteria": {"object_in_bin": False, "released": True}}
    fs = check_episode(e)
    assert "O002_label_contradicts_state" in {f.rule_id for f in fs}
    assert episode_status(e, fs) == "invalid_data"
    e.outcome.measurements = {"criteria": {"object_in_bin": True, "released": True}}
    assert "O002_label_contradicts_state" not in rules(e)


def test_valid_failed_task_is_preserved():
    e = ep(label="failure")
    assert episode_status(e, check_episode(e)) == "valid_failed_task"


def test_duplicate_detection():
    a, b = ep("a"), ep("b")
    c = ep("c")
    c.streams["obs.q"].values[0] = [0.9, 0.9]
    c.source.source_record_id = "a"
    fs = dataset_findings([a, b, c])
    by = {(f.rule_id, f.episode_id) for f in fs}
    assert ("D001_duplicate_content", "b") in by and ("D002_duplicate_source_id", "c") in by
    assert ("D001_duplicate_content", "a") not in by  # the first copy is not flagged


def test_truncated_and_missing_outcome():
    e = ep()
    e.end_time = e.end_time + 5
    assert "C004_truncated" in rules(e)
    e = ep(label=None)
    fs = check_episode(e)
    assert "C005_missing_outcome" in {f.rule_id for f in fs} and episode_status(e, fs) == "suspicious"


# ----------------------------------------------------------------------------- incidents & replay & regression
def _import_with_gaps(tmp_path, n=6):
    eps = []
    for i in range(n):
        e = ep(f"g{i}", session=f"s{i % 2}")
        if i < 4:
            s = e.streams["obs.q"]
            s.t, s.values = s.t[:10] + s.t[25:], s.values[:10] + s.values[25:]
        eps.append(e)
    return import_dataset(write_jsonl(tmp_path / "gaps", eps))


def test_incident_grouping_status_history(tmp_path):
    r = _import_with_gaps(tmp_path)
    incs = incidents.build_incidents(r["dataset_id"])
    gap = next(i for i in incs if i["category"] == "T003_time_gap")
    assert gap["n_episodes"] == 4 and gap["n_sessions"] == 2 and gap["grouping_reason"] == "same rule_id"
    assert gap["facts"] and gap["hypotheses"][0]["status"] == "unverified"
    incidents.transition(gap["incident_id"], "investigating", "alice", "looking")
    with pytest.raises(ValueError):
        incidents.transition(gap["incident_id"], "closed", "alice")  # investigating -> closed not allowed
    x = incidents.transition(gap["incident_id"], "resolved", "alice", resolution="fixed recorder")
    assert [h["to_status"] for h in x["history"]] == ["open", "investigating", "resolved"]
    again = incidents.build_incidents(r["dataset_id"])  # regrouping is idempotent and keeps status
    assert next(i for i in again if i["category"] == "T003_time_gap")["status"] == "resolved"
    assert len(again) == len(incs)
    out = incidents.export(gap["incident_id"], tmp_path / "exp")
    assert "Possible causes (unverified hypotheses)" in Path(out["markdown"]).read_text()


def test_replay_eligibility_and_incident_regression(tmp_path, run_dir):
    r = _import_with_gaps(tmp_path)
    assert {x["replay_class"] for x in store.query("SELECT replay_class FROM episodes WHERE dataset_id=?", [r["dataset_id"]])} == {"D"}
    gap = next(i for i in incidents.build_incidents(r["dataset_id"]) if i["category"] == "T003_time_gap")
    res = incidents.to_regression_case(gap["incident_id"])
    assert res["created"] is False and res["member_replay_classes"] == {"D": 4}
    # simulator failures are replayable (class A) and become regression cases
    from amperes.sim import variations as V
    from amperes.sim.experiment import run_policy
    from amperes.sim.policies import ScriptedConfig

    c = V.make_condition({"obstacle_height": 0.14}, 1, "dev", None, "t")
    run = run_policy(None, "baseline_dev", ScriptedConfig(), [c], "baseline", workers=1)
    inc = next(i for i in incidents.build_incidents(run["run_id"]) if i["kind"] == "task_failure")
    res = incidents.to_regression_case(inc["incident_id"])
    assert res["created"] and res["replay_class"] == "A"
    from amperes.regression import run_suite

    out = run_suite(res["suite_id"], ScriptedConfig(obstacle_aware=True), "fix")
    assert out["status_counts"]["fixed"] == 1
    assert res["case_id"] in incidents.get(inc["incident_id"])["regression_case_ids"]


# ----------------------------------------------------------------------------- phase 3: ops package end-to-end
@pytest.fixture
def ops_ds(tmp_path):
    from amperes.fixtures import ops_package

    d = tmp_path / "ops"
    ops_package(d)
    before = source_fingerprint(d)
    r = import_dataset(d, synthetic=True)
    incidents.build_incidents(r["dataset_id"])
    return r, d, before


def test_ops_metrics_reconcile_and_planted_patterns(ops_ds):
    r, d, before = ops_ds
    ds = r["dataset_id"]
    assert source_fingerprint(d) == before  # source untouched
    assert r["duplicate_events_dropped"] == 8
    m = ops.ops_metrics([ds])
    y = m["yield"]["usable_data_yield"]
    db = store.query("SELECT quality_status, COUNT(*) n FROM episodes WHERE dataset_id=? GROUP BY quality_status", [ds])
    counts = {x["quality_status"]: x["n"] for x in db}
    assert y["denominator"] == sum(counts.values()) == 480
    assert y["numerator"] == counts.get("valid_success_unverified", 0) + counts.get("valid_failed_task", 0) + counts.get("verified_success", 0)
    assert counts["valid_failed_task"] > 0 and counts["infrastructure_error"] > 0  # failed tasks stay usable; recording failures do not
    av = m["reliability"]["availability_by_workcell"]
    assert av["wc-C"]["status"] == "not_computable"  # missing is not zero
    assert av["wc-A"]["status"] == "computed" and av["wc-A"]["value"] < 1
    assert m["reliability"]["repeat_failure_after_resolution"]["numerator"] == 1
    gr = next(i for i in store.query("SELECT * FROM incidents WHERE dataset_id=?", [ds]) if i["category"] == "hardware_fault")
    assert gr["n_occurrences"] == 8  # duplicated fault rows did not inflate the incident
    rules_ = {b["rule"] for b in ops.bottlenecks([ds])}
    assert {"B003_config_fault_association", "B004_cycle_time_after_change"} <= rules_
    assoc = ops.correlate([ds])["statistical_association"]
    assert any(a["code"] == "CAM_FRAME_DROP" and a["config_id"] == "cfg-v2" for a in assoc)


def test_reimport_keeps_events_per_dataset(ops_ds):
    r, d, _ = ops_ds
    r2 = import_dataset(d, synthetic=True)
    n = lambda ds: store.query("SELECT COUNT(*) n FROM ops_events WHERE dataset_id=?", [ds])[0]["n"]  # noqa: E731
    assert n(r["dataset_id"]) == n(r2["dataset_id"]) == r["events_imported"] > 0


def test_metrics_not_computable_without_sessions(tmp_path):
    r = import_dataset(write_jsonl(tmp_path / "ns", [ep("a"), ep("b")]))
    m = ops.ops_metrics([r["dataset_id"]])
    assert m["throughput"]["completed_episodes_per_hour"]["status"] == "not_computable"
    assert m["reliability"]["availability_by_workcell"]["status"] == "not_computable"


def test_review_statistics(tmp_path):
    r = _import_with_gaps(tmp_path)
    fs = store.query("SELECT finding_id FROM quality_findings WHERE dataset_id=? AND rule_id='T003_time_gap'", [r["dataset_id"]])
    review.record_review("finding", fs[0]["finding_id"], "incorrect", "rev1")
    review.record_review("finding", fs[0]["finding_id"], "correct_actionable", "rev1", "changed my mind")  # latest wins
    review.record_review("finding", fs[1]["finding_id"], "incorrect", "rev2")
    review.record_review("finding", fs[2]["finding_id"], "duplicate", "rev2")
    rep = review.review_report(r["dataset_id"])["by_rule"]["T003_time_gap"]
    assert rep["reviewed"] == 3 and rep["confirmed"] == 1 and rep["false_positives"] == 1 and rep["duplicates"] == 1
    assert rep["precision_among_reviewed"] == {"value": 0.5, "numerator": 1, "denominator": 2}
    with pytest.raises(ValueError):
        review.record_review("finding", fs[0]["finding_id"], "correct_actionable", "someone@example.com")


def test_workflow_study_and_economics(tmp_path):
    r = _import_with_gaps(tmp_path)
    st = review.plan_study(r["dataset_id"], ["r1", "r2"], sample_size=6)
    assert {t["condition"] for t in st["tasks"] if t["reviewer"] == "r1"} == {"A", "B"}
    assert [t["condition"] for t in st["tasks"] if t["reviewer"] == "r1"] != [t["condition"] for t in st["tasks"] if t["reviewer"] == "r2"]
    assert review.study_report(st["study_id"])["status"].startswith("not measured")
    for t in st["tasks"]:
        review.start_task(t["task_id"])
        review.finish_task(t["task_id"], 2, 0, duration_s=600 if t["condition"] == "A" else 420)
    rep = review.study_report(st["study_id"])
    assert rep["median_time_saved_per_task_s"] == 180 and rep["status"].startswith("preliminary")
    none = estimate({}, {"median_time_saved_per_task_s": None, "study_status": "not measured"})
    assert none["roi_statement"].startswith("No ROI")
    est = estimate({"engineer_cost_per_hour": 100, "triage_tasks_per_week": 10, "weeks_per_year": 50},
                   {"median_time_saved_per_task_s": 180, "study_status": rep["status"]})
    assert est["items"]["direct_triage_time_savings"]["gross_value_per_year"]["central"] == pytest.approx(180 / 3600 * 10 * 50 * 100)
    assert est["items"]["potential_yield_increase"]["status"] == "not_computable"


def test_pilot_report_consistency_and_synthetic_label(ops_ds):
    from amperes import pilot_report

    r, _, _ = ops_ds
    rep = pilot_report.build([r["dataset_id"]])
    y = rep["sections"]["usable_data_yield"]["usable_data_yield"]
    n_usable = store.query("SELECT COUNT(*) n FROM episodes WHERE dataset_id=? AND quality_status IN "
                           "('verified_success','valid_success_unverified','valid_failed_task')", [r["dataset_id"]])[0]["n"]
    assert y["numerator"] == n_usable
    assert rep["synthetic_data"] and "SYNTHETIC" in pilot_report.markdown(rep)
    assert rep["sections"]["workflow_time"]["class"] == "MISSING"


# ----------------------------------------------------------------------------- deletion, model default, API/CLI errors
def test_dataset_deletion(tmp_path):
    r = _import_with_gaps(tmp_path)
    ds = r["dataset_id"]
    incidents.build_incidents(ds)
    art = store.artifacts_dir() / "datasets" / ds
    assert art.exists()
    removed = store.delete_dataset(ds)
    assert removed["episodes"] == 6 and removed["artifact_dirs"] == 1 and not art.exists()
    for t in ("episodes", "quality_findings", "incidents", "ops_events", "human_labels", "datasets"):
        assert store.query(f"SELECT COUNT(*) n FROM {t} WHERE dataset_id=?", [ds])[0]["n"] == 0


def test_diagnostic_model_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AMPERES_DIAGNOSTIC_PROVIDER", raising=False)
    st = diagnostics.status()
    assert st["enabled"] is False and st["data_flow"] == "no data leaves this machine"
    monkeypatch.setenv("AMPERES_DIAGNOSTIC_PROVIDER", "anything")
    assert diagnostics.status()["enabled"] is False  # still needs explicit allow flag + registered provider
    assert "model_hypothesis" not in diagnostics.summarize({"facts": ["x"], "hypotheses": []})


def test_api_and_cli_errors(tmp_path, capsys):
    from amperes.api import app

    c = TestClient(app)
    assert c.get("/api/episodes/nope").status_code == 404
    assert c.post("/api/datasets/import", json={"path": str(tmp_path / "missing")}).status_code == 400
    assert c.post("/api/datasets/import", json={"path": "x", "bogus": 1}).status_code == 422
    assert c.delete("/api/datasets/whatever").status_code == 400  # no confirm
    assert c.get("/api/health").json()["diagnostic_model"]["enabled"] is False
    assert cli(["delete-dataset", "nope", "--yes"]) == 2
    assert cli(["reproduce", "nope"]) == 2
    assert cli(["import", str(tmp_path / "missing")]) == 2
    assert cli(["serve", "--host", "0.0.0.0"]) == 2  # local-first: refuses remote bind without --allow-remote
    r = _import_with_gaps(tmp_path)
    ok = c.get(f"/api/episodes?dataset_id={r['dataset_id']}").json()
    assert ok["total"] == 6
    detail = c.get(f"/api/episodes/{ok['rows'][0]['episode_id']}").json()
    assert "source_values" in detail and "derived_diagnostics" in detail


# ----------------------------------------------------------------------------- fixes from the documentation review
def test_yield_loss_attribution_links_events(ops_ds):
    r, _, _ = ops_ds
    a = ops.ops_metrics([r["dataset_id"]])["yield_loss_attribution"]
    rows = {x["cause"]: x for x in a["rows"]}
    assert a["lost_episodes"] == sum(x["episodes"] for x in a["rows"]) == 51  # each lost episode attributed once
    assert rows["event data_recording_failure:CAM_FRAME_DROP"]["episodes"] == 43
    assert rows["event data_recording_failure:CAM_FRAME_DROP"]["workcells"] == ["wc-B"]
    assert rows["finding C005_missing_outcome"]["episodes"] == 8
    assert rows["event data_recording_failure:CAM_FRAME_DROP"]["episode_hours"] > 0


def test_operator_filter_and_custom_usability(ops_ds):
    r, _, _ = ops_ds
    ds = r["dataset_id"]
    allm = ops.ops_metrics([ds])["yield"]["usable_data_yield"]["denominator"]
    one = ops.ops_metrics([ds], filters={"operator_id": "op-1"})["yield"]["usable_data_yield"]["denominator"]
    assert 0 < one < allm
    strict = ops.ops_metrics([ds], usable_statuses=("verified_success",))["yield"]["usable_data_yield"]
    assert strict["numerator"] == 0  # CSV labels are never independently verified


def test_pilot_report_excludes_simulation_unless_asked(ops_ds):
    from amperes import pilot_report

    r, _, _ = ops_ds
    rep = pilot_report.build([r["dataset_id"]])
    assert rep["sections"]["reproduction_and_regression"]["class"] == "NOT INCLUDED"
    with pytest.raises(ValueError):
        pilot_report.build([r["dataset_id"]], analysis={"bogus": 1})


def test_rules_config_disables_rule(tmp_path):
    e = ep(label=None)
    d = write_jsonl(tmp_path / "rc", [e])
    (tmp_path / "rules.json").write_text(json.dumps({"disabled_rules": ["C005_missing_outcome"]}))
    assert cli(["import", str(d), "--rules-config", str(tmp_path / "rules.json"), "--no-incidents", "--brief"]) == 0
    assert not store.query("SELECT 1 FROM quality_findings WHERE rule_id='C005_missing_outcome'")
    (tmp_path / "bad.json").write_text(json.dumps({"not_a_setting": 1}))
    assert cli(["import", str(d), "--rules-config", str(tmp_path / "bad.json")]) == 2


def test_deletion_removes_reports_and_study_rows(tmp_path):
    from amperes import pilot_report

    r = _import_with_gaps(tmp_path)
    ds = r["dataset_id"]
    incs = incidents.build_incidents(ds)
    incidents.export(incs[0]["incident_id"], store.artifacts_dir() / "reports" / "incidents")
    pilot_report.write(pilot_report.build([ds]), store.artifacts_dir() / "reports" / "pilot" / ds)
    other = import_dataset(write_jsonl(tmp_path / "other", [ep("z")]))["dataset_id"]
    multi = store.artifacts_dir() / "reports" / "pilot" / f"{other}_{ds}"
    pilot_report.write(pilot_report.build([other, ds]), multi)
    review.plan_study(ds, ["r1"], sample_size=4)
    removed = store.delete_dataset(ds)
    assert removed["report_files"] == 2 and removed["workflow_tasks"] == 2
    assert not (store.artifacts_dir() / "reports" / "pilot" / ds).exists() and not multi.exists()


def test_economics_net_and_statements():
    est = estimate({"engineer_cost_per_hour": 100, "triage_tasks_per_week": 10, "weeks_per_year": 50,
                    "amperes_operating_hours_per_week": 1}, {"median_time_saved_per_task_s": -60, "study_status": "preliminary (x)"})
    d = est["items"]["direct_triage_time_savings"]
    assert d["no_saving_measured"] and d["preliminary"] and d["net_value_per_year"]["central"] < 0
    assert "not faster" in est["roi_statement"] and "PRELIMINARY" in est["roi_statement"]
    only_yield = estimate({"eligible_episodes_per_week": 100, "target_yield_gain_pp": 2, "value_per_usable_episode": 1,
                           "weeks_per_year": 50}, {"median_time_saved_per_task_s": None})
    assert only_yield["roi_statement"].startswith("No measured ROI")
