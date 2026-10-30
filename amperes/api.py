"""Local FastAPI service + static UI. Start with `python -m amperes.cli serve` (binds 127.0.0.1 by default)."""

from __future__ import annotations

import json
import threading
import traceback
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from amperes import diagnostics, incidents, labels, ops, review, store
from amperes.provenance import environment, new_id, now_iso

app = FastAPI(title="Amperes Failure Lab", version="0.3.0")
UI = Path(__file__).parent / "ui" / "index.html"
_JOB_LOCK = threading.Lock()
JOBS: dict[str, dict[str, Any]] = {}


def _404(what: str):
    raise HTTPException(status_code=404, detail=f"{what} not found")


def _run_job(kind: str, fn, *args) -> dict[str, Any]:
    if not _JOB_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="another job is running; one job at a time (local resource guard)")
    jid = new_id("job")
    JOBS[jid] = {"job_id": jid, "kind": kind, "status": "running", "started_at": now_iso(), "log": [], "result": None, "error": None}

    def target():
        try:
            JOBS[jid]["result"] = fn(*args, log=JOBS[jid]["log"].append)
            JOBS[jid]["status"] = "completed"
        except Exception as exc:
            JOBS[jid]["status"] = "failed"
            JOBS[jid]["error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}"
        finally:
            JOBS[jid]["finished_at"] = now_iso()
            _JOB_LOCK.release()

    threading.Thread(target=target, daemon=True).start()
    return JOBS[jid]


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(UI.read_text())


@app.get("/api/health")
def health():
    return {"ok": True, "environment": environment(), "data_home": str(store.home()), "diagnostic_model": diagnostics.status(),
            "atlas_configured": _atlas_configured()}


def _atlas_configured() -> bool:
    import os as _os

    if _os.environ.get("MONGODB_URI"):
        return True
    env = Path(__file__).resolve().parents[1] / ".env"
    return env.exists() and any(line.startswith("MONGODB_URI=") and line.strip() != "MONGODB_URI=" for line in env.read_text().splitlines())


@app.get("/api/overview")
def overview():
    eps = store.query("SELECT e.quality_status, e.outcome, e.synthetic, e.start_time, d.kind FROM episodes e "
                      "LEFT JOIN datasets d ON d.dataset_id=e.dataset_id")
    imported = [e for e in eps if e["kind"] == "import"]
    sim = [e for e in eps if e["kind"] != "import"]
    findings = store.query("SELECT severity, rule_id FROM quality_findings WHERE severity<>'info'")
    n_info = store.query("SELECT COUNT(*) n FROM quality_findings WHERE severity='info'")[0]["n"]
    inc = store.query("SELECT kind, category, status, n_occurrences, n_sessions FROM incidents")
    by_day: dict[str, Counter] = {}
    for e in imported:
        if e["start_time"]:
            from datetime import datetime, timezone

            day = datetime.fromtimestamp(e["start_time"], timezone.utc).strftime("%Y-%m-%d")
            by_day.setdefault(day, Counter())[e["quality_status"]] += 1
    return {
        "episodes_processed": {"imported": len(imported), "simulated": len(sim)},
        "imported_status_counts": dict(Counter(e["quality_status"] for e in imported)),
        "quality_findings": {"total": len(findings), "by_severity": dict(Counter(f["severity"] for f in findings)), "info_notes": n_info},
        "incidents_open": sum(i["status"] in ("open", "investigating", "reproduced", "unresolved") for i in inc),
        "recurring_categories": [{"kind": i["kind"], "category": i["category"], "occurrences": i["n_occurrences"],
                                  "sessions": i["n_sessions"]} for i in sorted(inc, key=lambda x: -x["n_occurrences"]) if i["n_occurrences"] > 1][:10],
        "requires_review": sum(e["quality_status"] == "suspicious" for e in imported),
        "daily_status": {d: dict(c) for d, c in sorted(by_day.items())},
        "synthetic_present": any(e["synthetic"] for e in eps),
        "experiments": store.query("SELECT experiment_id, status, created_at FROM experiments ORDER BY created_at DESC LIMIT 5"),
    }


# ------------------------------------------------------------------------------------------------ datasets
class ImportReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    name: str | None = None
    adapter: str = "auto"
    synthetic: bool = False


@app.get("/api/datasets")
def datasets():
    rows = store.query("SELECT dataset_id, name, kind, adapter, source_uri, synthetic, n_records, n_rejected, created_at, note "
                       "FROM datasets ORDER BY created_at DESC")
    return rows


@app.post("/api/datasets/validate")
def validate(req: ImportReq):
    from amperes.ingest.pipeline import validate_only

    try:
        return validate_only(req.path, req.adapter)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/datasets/import")
def import_(req: ImportReq):
    from amperes.ingest.pipeline import import_dataset

    try:
        r = import_dataset(req.path, name=req.name, adapter=req.adapter, synthetic=True if req.synthetic else None)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc))
    r["incidents"] = len(incidents.build_incidents(r["dataset_id"]))
    return r


@app.get("/api/datasets/{dataset_id}")
def dataset(dataset_id: str):
    rows = store.query("SELECT * FROM datasets WHERE dataset_id=?", [dataset_id])
    if not rows:
        _404("dataset")
    d = rows[0]
    d["import_report"] = store.loads(d["import_report"])
    return d


@app.delete("/api/datasets/{dataset_id}")
def delete_dataset(dataset_id: str, confirm: bool = False):
    if not confirm:
        raise HTTPException(400, "deletion is permanent; pass ?confirm=true")
    if not store.query("SELECT 1 FROM datasets WHERE dataset_id=?", [dataset_id]):
        _404("dataset")
    return {"deleted": dataset_id, "removed": store.delete_dataset(dataset_id)}


@app.post("/api/samples/load")
def load_samples():
    """Generate and import the SYNTHETIC sample packages (clearly labelled)."""
    from amperes import fixtures
    from amperes.ingest.pipeline import import_dataset

    base = store.artifacts_dir() / "fixtures"
    out = {}
    ops_dir = base / "ops_package"
    if not (ops_dir / "episodes.csv").exists():
        fixtures.ops_package(ops_dir)
    r = import_dataset(ops_dir, name="SAMPLE (synthetic) 3-workcell collection program", synthetic=True,
                       note=fixtures.SYNTHETIC_NOTE)
    incidents.build_incidents(r["dataset_id"])
    out["ops_package"] = r["dataset_id"]
    dc = base / "defect_corpus"
    if not (dc / "labels.json").exists():
        fixtures.defect_corpus(dc, store.artifacts_dir() / "fixture_work")
    r2 = import_dataset(dc, name="SAMPLE (synthetic) labelled defect corpus", adapter="jsonl", synthetic=True,
                        note=fixtures.SYNTHETIC_NOTE)
    incidents.build_incidents(r2["dataset_id"])
    out["defect_corpus"] = r2["dataset_id"]
    return out


# ------------------------------------------------------------------------------------------------ episodes
FILTERS = ("dataset_id", "quality_status", "outcome", "task_id", "policy_id", "policy_version", "workcell_id", "session_id",
           "device_id", "operator_id", "split", "category", "run_id")


@app.get("/api/episodes")
def episodes(dataset_id: str | None = None, quality_status: str | None = None, outcome: str | None = None,
             task_id: str | None = None, policy_id: str | None = None, policy_version: str | None = None,
             workcell_id: str | None = None, session_id: str | None = None, device_id: str | None = None,
             operator_id: str | None = None, split: str | None = None, category: str | None = None, run_id: str | None = None,
             t0: float | None = None, t1: float | None = None, limit: int = Query(100, le=1000), offset: int = 0):
    vals = locals()
    where, params = [], []
    for k in FILTERS:
        if vals.get(k) is not None:
            where.append(f"{k}=?")
            params.append(vals[k])
    if t0 is not None:
        where.append("start_time>=?")
        params.append(t0)
    if t1 is not None:
        where.append("start_time<=?")
        params.append(t1)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    total = store.query(f"SELECT COUNT(*) n FROM episodes{w}", params)[0]["n"]
    rows = store.query(f"SELECT episode_id, dataset_id, run_id, split, task_id, policy_id, policy_version, policy_label, session_id, "
                       f"workcell_id, device_id, outcome, outcome_provenance, verified, category, quality_status, replay_class, "
                       f"start_time, end_time, sim_duration_s, synthetic FROM episodes{w} ORDER BY episode_id LIMIT ? OFFSET ?",
                       params + [limit, offset])
    return {"total": total, "rows": rows}


def _downsample(t, v, n=300):
    if len(t) <= n:
        return t, v
    step = len(t) / n
    idx = [int(i * step) for i in range(n)] + [len(t) - 1]
    return [t[i] for i in idx], [v[i] for i in idx]


@app.get("/api/episodes/{episode_id}")
def episode(episode_id: str, streams: bool = True):
    from amperes.schema import read_episode

    rows = store.query("SELECT * FROM episodes WHERE episode_id=?", [episode_id])
    if not rows:
        _404("episode")
    row = rows[0]
    ep = read_episode(store.safe_artifact_path(row["artifact_path"]))
    findings = store.query("SELECT finding_id, rule_id, rule_version, dimension, severity, record_ref, t, observed, expected, explanation, "
                           "recommendation, deterministic, needs_review FROM quality_findings WHERE episode_id=?", [episode_id])
    src = {"source": ep.source.model_dump(), "task": ep.task and ep.task.model_dump(), "policy": ep.policy and ep.policy.model_dump(),
           "robot": ep.robot and ep.robot.model_dump(), "session": ep.session and ep.session.model_dump(),
           "start_time": ep.start_time, "end_time": ep.end_time, "time_base": ep.time_base,
           "outcome_as_recorded": ep.outcome and ep.outcome.model_dump(exclude={"measurements"}), "conditions": ep.conditions,
           "events": [e.model_dump() for e in ep.events if e.type != "policy_phase"][:100],
           "policy_reported_phases": [e.model_dump() for e in ep.events if e.type == "policy_phase"][:50],
           "streams": {k: {"kind": s.kind, "n": len(s.t), "units": s.units, "expected_rate_hz": s.expected_rate_hz,
                           "valid_range": s.valid_range,
                           **(dict(zip(("t", "values"), _downsample(s.t, s.values))) if streams else {})}
                       for k, s in ep.streams.items()}}
    derived = {"quality_status": row["quality_status"], "findings": findings,
               "diagnosis": ep.diagnosis and ep.diagnosis.model_dump(),
               "verifier_measurements": ep.outcome.measurements if ep.outcome else None,
               "replay_class": row["replay_class"]}
    hl = labels.labels_for(episode_id)
    return {"episode_id": episode_id, "index": row, "source_values": src, "derived_diagnostics": derived,
            "human_labels": hl, "human_label": hl[-1] if hl else None}


# ------------------------------------------------------------------------------------------------ human labels
class LabelReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["success", "failure", "unknown"]
    category: str | None = None
    note: str = Field("", max_length=2000)
    reviewer: str = Field(..., min_length=1, max_length=80)


@app.post("/api/episodes/{episode_id}/label")
def label_episode(episode_id: str, req: LabelReq):
    try:
        return labels.record_label(episode_id, req.outcome, req.reviewer, req.category, req.note)
    except LookupError:
        _404("episode")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/episodes/{episode_id}/images/{filename}")
def episode_image(episode_id: str, filename: str):
    """Read-only: serves only snapshot files that this episode references, from its own session images directory."""
    try:
        p = labels.image_path(episode_id, filename)
    except LookupError:
        _404("image")
    if p.suffix.lower() == ".dep":
        try:
            return Response(labels.depth_png(p.read_bytes()), media_type="image/png")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    return FileResponse(p, media_type=labels.IMAGE_TYPES[p.suffix.lower()])


@app.get("/api/labels/queue")
def label_queue(dataset_id: str | None = None, limit: int = Query(20, ge=1, le=200)):
    return labels.queue(dataset_id, limit)


@app.get("/api/labels/summary")
def label_summary(dataset_id: str | None = None):
    return labels.summary(dataset_id)


@app.get("/api/episodes/{episode_id}/similar")
def similar_episodes(episode_id: str, k: int = Query(10, le=50), engine: str = "local"):
    from amperes.signatures import similar_local

    try:
        if engine == "atlas":
            from amperes.integrations import atlas

            return atlas.similar(episode_id, k)
        return similar_local(episode_id, k)
    except ValueError:
        _404("episode")
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/findings")
def findings(dataset_id: str | None = None, rule_id: str | None = None, severity: str | None = None, limit: int = Query(200, le=2000)):
    where, p = [], []
    for k, v in (("dataset_id", dataset_id), ("rule_id", rule_id), ("severity", severity)):
        if v:
            where.append(f"{k}=?")
            p.append(v)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    return store.query(f"SELECT * FROM quality_findings{w} ORDER BY severity, rule_id LIMIT ?", p + [limit])


@app.get("/api/quality/{dataset_id}")
def quality(dataset_id: str):
    d = dataset(dataset_id)
    return {"dataset": {k: d[k] for k in ("dataset_id", "name", "synthetic", "n_records", "n_rejected")},
            "summary": (d["import_report"] or {}).get("quality"), "rejected_records": (d["import_report"] or {}).get("rejected", [])[:50],
            "rules": store.query("SELECT rule_id, dimension, severity, COUNT(*) n, COUNT(DISTINCT episode_id) episodes, deterministic, "
                                 "MAX(needs_review) needs_review FROM quality_findings WHERE dataset_id=? GROUP BY rule_id, severity "
                                 "ORDER BY n DESC", [dataset_id])}


# ------------------------------------------------------------------------------------------------ incidents
class StatusReq(BaseModel):
    to: str
    actor: str = Field(..., min_length=1)
    note: str = ""
    owner: str | None = None
    resolution: str | None = None


@app.get("/api/incidents")
def list_incidents(dataset_id: str | None = None):
    q = "SELECT incident_id, dataset_id, kind, category, title, severity, status, first_seen, last_seen, n_occurrences, n_episodes, n_sessions FROM incidents"
    return store.query(q + (" WHERE dataset_id=?" if dataset_id else "") + " ORDER BY n_occurrences DESC", [dataset_id] if dataset_id else [])


@app.post("/api/incidents/build/{dataset_id}")
def build_incidents(dataset_id: str):
    return [{k: i[k] for k in ("incident_id", "title", "status", "n_occurrences")} for i in incidents.build_incidents(dataset_id)]


@app.get("/api/incidents/{incident_id}")
def get_incident(incident_id: str):
    try:
        inc = incidents.get(incident_id)
    except ValueError:
        _404("incident")
    inc["summary"] = diagnostics.summarize(inc)
    return inc


@app.post("/api/incidents/{incident_id}/status")
def incident_status(incident_id: str, req: StatusReq):
    try:
        return incidents.transition(incident_id, req.to, req.actor, req.note, req.owner, req.resolution)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/incidents/{incident_id}/regression")
def incident_regression(incident_id: str):
    try:
        return incidents.to_regression_case(incident_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/export/incident/{incident_id}")
def export_incident(incident_id: str, fmt: str = "md"):
    try:
        out = incidents.export(incident_id, store.artifacts_dir() / "reports" / "incidents")
    except ValueError:
        _404("incident")
    return FileResponse(out["markdown" if fmt == "md" else "json"])


# ------------------------------------------------------------------------------------------------ ops & review
@app.get("/api/ops/{dataset_id}/metrics")
def ops_metrics(dataset_id: str, workcell_id: str | None = None, session_id: str | None = None, operator_id: str | None = None,
                t0: float | None = None, t1: float | None = None):
    f = {k: v for k, v in (("workcell_id", workcell_id), ("session_id", session_id), ("operator_id", operator_id)) if v}
    return ops.ops_metrics([dataset_id], t0, t1, f)


@app.get("/api/ops/{dataset_id}/bottlenecks")
def ops_bottlenecks(dataset_id: str):
    return ops.bottlenecks([dataset_id])


@app.get("/api/ops/{dataset_id}/correlate")
def ops_correlate(dataset_id: str):
    return ops.correlate([dataset_id])


class ReviewReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_kind: str
    target_id: str
    decision: str
    reviewer: str
    notes: str = ""


@app.post("/api/reviews")
def add_review(req: ReviewReq):
    try:
        return review.record_review(req.target_kind, req.target_id, req.decision, req.reviewer, req.notes)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/reviews/{dataset_id}")
def review_stats(dataset_id: str):
    return review.review_report(dataset_id)


# ------------------------------------------------------------------------------------------------ simulator workflow
class ExperimentReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n_dev: int = Field(200, ge=20, le=2000)
    n_heldout: int = Field(400, ge=20, le=5000)
    max_repro: int = Field(20, ge=1, le=200)


@app.get("/api/tasks")
def tasks():
    from amperes.sim.env import ACTION_SPEC, OBSERVATION_SPEC, TASK_ID, TASK_VERSION, EnvParams

    return [{"task_id": TASK_ID, "version": TASK_VERSION, "simulator": "mujoco", "env_params_schema": EnvParams.model_json_schema(),
             "action_spec": ACTION_SPEC, "observation_spec": OBSERVATION_SPEC}]


@app.get("/api/policies")
def policies():
    from amperes.sim.interventions import CATALOG
    from amperes.sim.policies import ScriptedConfig

    return {"registered": [{"policy_id": "scripted_pick_place", "kind": "scripted baseline controller (not learned)",
                            "default_config": ScriptedConfig().model_dump(), "label": ScriptedConfig().label()}],
            "intervention_catalog": {k: [{"name": i.name, "delta": i.delta, "rationale": i.rationale} for i in v] for k, v in CATALOG.items()}}


@app.get("/api/experiments")
def experiments():
    return store.query("SELECT experiment_id, name, status, error, created_at FROM experiments ORDER BY created_at DESC")


@app.post("/api/experiments")
def start_experiment(req: ExperimentReq):
    from amperes.report import write_report
    from amperes.sim.experiment import run_experiment

    def job(n_dev, n_heldout, max_repro, log):
        s = run_experiment(n_dev=n_dev, n_heldout=n_heldout, max_repro=max_repro, log=log)
        write_report(s)
        return {"experiment_id": s["experiment_id"]}

    return _run_job("experiment", job, req.n_dev, req.n_heldout, req.max_repro)


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    return JOBS.get(job_id) or _404("job")


@app.get("/api/experiments/{experiment_id}")
def experiment(experiment_id: str):
    rows = store.query("SELECT * FROM experiments WHERE experiment_id=?", [experiment_id])
    if not rows:
        _404("experiment")
    r = rows[0]
    r["config"], r["summary"] = store.loads(r["config"]), store.loads(r["summary"])
    r["runs"] = store.query("SELECT run_id, kind, policy_label, split, n_episodes, status, error, wall_s FROM runs WHERE experiment_id=?",
                            [experiment_id])
    return r


@app.get("/api/reproductions")
def reproductions(experiment_id: str | None = None, limit: int = 200):
    q = "SELECT * FROM reproductions" + (" WHERE experiment_id=?" if experiment_id else "") + " ORDER BY created_at DESC LIMIT ?"
    rows = store.query(q, ([experiment_id] if experiment_id else []) + [limit])
    for r in rows:
        r["comparison"] = store.loads(r["comparison"])
        r["criteria"] = store.loads(r["criteria"])
    return rows


@app.post("/api/reproduce/{episode_id}")
def reproduce(episode_id: str):
    from amperes.sim.reproduce import reproduce as repro

    rows = store.query("SELECT artifact_path, replay_class FROM episodes WHERE episode_id=?", [episode_id])
    if not rows:
        _404("episode")
    return repro(store.safe_artifact_path(rows[0]["artifact_path"]))


@app.get("/api/regression/suites")
def suites():
    return store.query("SELECT s.*, (SELECT COUNT(*) FROM regression_cases c WHERE c.suite_id=s.suite_id) n_cases FROM regression_suites s "
                       "ORDER BY created_at DESC")


@app.get("/api/regression/suites/{suite_id}")
def suite(suite_id: str):
    cases = store.query("SELECT * FROM regression_cases WHERE suite_id=?", [suite_id])
    if not cases:
        _404("suite")
    results = store.query("SELECT r.policy_label, r.status, COUNT(*) n FROM regression_results r WHERE suite_id=? GROUP BY r.policy_label, r.status",
                          [suite_id])
    return {"suite_id": suite_id, "cases": cases, "results_by_policy": results}


class SuiteRunReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_config: dict[str, Any] = Field(default_factory=dict)
    label: str | None = None


@app.post("/api/regression/suites/{suite_id}/run")
def run_suite(suite_id: str, req: SuiteRunReq):
    from amperes.regression import run_suite as rs
    from amperes.sim.policies import ScriptedConfig

    try:
        cfg = ScriptedConfig(**req.policy_config)
        return rs(suite_id, cfg, req.label or cfg.label(), workers=1)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ------------------------------------------------------------------------------------------------ exports
@app.get("/api/export/experiment/{experiment_id}")
def export_experiment(experiment_id: str, file: str = "bundle.zip"):
    from amperes.report import report_dir

    allowed = {"bundle.zip", "report.md", "summary.json", "manifest.json", "episodes.csv"}
    if file not in allowed and not file.endswith(".png"):
        raise HTTPException(400, f"file must be one of {sorted(allowed)} or a plot")
    p = store.safe_artifact_path(report_dir(experiment_id) / Path(file).name)
    if not p.exists():
        _404("report file")
    return FileResponse(p)


@app.get("/api/export/pilot/{dataset_id}")
def export_pilot(dataset_id: str, fmt: str = "md"):
    from amperes import pilot_report

    try:
        r = pilot_report.build([dataset_id])
    except ValueError:
        _404("dataset")
    out = pilot_report.write(r, store.artifacts_dir() / "reports" / "pilot" / dataset_id)
    return FileResponse(out["markdown" if fmt == "md" else "json"])


@app.get("/api/detector-eval")
def detector_eval():
    p = store.artifacts_dir() / "reports" / "detector_eval" / "detector_eval.json"
    if not p.exists():
        return {"status": "not evaluated", "how": "python -m amperes.cli eval-detectors"}
    return json.loads(p.read_text())


# ------------------------------------------------------------------------------------------------ live Viam capture (read-only)
LIVE: dict[str, Any] = {"status": None, "stop": None, "out": None}


class LiveReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seconds: float = Field(60, ge=5, le=1800)
    rate_hz: float = Field(3.0, gt=0.1, le=20)
    camera: bool = True


@app.post("/api/live/start")
def live_start(req: LiveReq):
    import time as _t

    from amperes.integrations.viam_live import LiveStatus, RecorderConfig, capture_and_import

    out = store.artifacts_dir() / "viam_sessions" / _t.strftime("%Y%m%dT%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    LIVE["status"], LIVE["stop"], LIVE["out"] = LiveStatus(out / "live.json"), threading.Event(), str(out)
    LIVE["status"].update(running=True, starting=True, seconds=req.seconds)
    cfg = RecorderConfig(seconds=req.seconds, rate_hz=req.rate_hz, camera="cam" if req.camera else None)

    def job(log):
        return capture_and_import(cfg, out, LIVE["status"], LIVE["stop"], log=log)

    return _run_job("viam_live_capture", job)


@app.post("/api/atlas/sync/{dataset_id}")
def atlas_sync(dataset_id: str):
    """Explicit, user-triggered sync of one dataset to the cluster in MONGODB_URI (no raw streams are sent)."""
    from amperes.integrations import atlas

    try:
        return atlas.sync(dataset_id)
    except ValueError:
        _404("dataset")
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/live/stop")
def live_stop():
    if LIVE["stop"] is None:
        raise HTTPException(400, "no live capture has been started")
    LIVE["stop"].set()
    return {"stopping": True}


@app.get("/api/live")
def live_status():
    if LIVE["status"] is None:
        return {"running": False, "never_started": True,
                "note": "Read-only: Amperes only calls get_joint_positions, get_end_position, is_moving, get_images, get_machine_status."}
    return LIVE["status"].snapshot()


# ------------------------------------------------------------------------------------------------ supervised robot trials
ROBOT: dict[str, Any] = {"status": None, "stop": None}


class RobotRunReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trials: int = Field(4, ge=1, le=50)
    approach_mm: float = Field(80, ge=30, le=200)
    grasp_z_offset_mm: float = Field(0, ge=-15, le=30)
    settle_s: float = Field(0.3, ge=0, le=3)
    ab: dict[str, Any] | None = None
    execute: bool = False
    ack: str = ""
    object_name: str = Field("object", max_length=60)


@app.get("/api/robot")
def robot_status():
    from amperes.integrations import viam_control as VC

    wps = VC.load_waypoints()
    machine = "sfhack1-main"
    try:
        from amperes.integrations.viam_live import _env

        machine = _env().split(".")[0]
    except Exception:
        pass
    env = None
    try:
        env = VC.Envelope.from_waypoints(wps, 80, -23.0).__dict__
    except ValueError as exc:
        env = {"error": str(exc)}
    return {"waypoints": {k: {kk: v[kk] for kk in ("x", "y", "z", "theta", "_frame")} for k, v in wps.items()},
            "envelope": env, "required_ack": VC.required_ack(machine),
            "run": ROBOT["status"].snapshot() if ROBOT["status"] else {"running": False}}


@app.post("/api/robot/teach/{name}")
def robot_teach(name: str):
    import asyncio

    from amperes.integrations import viam_control as VC

    try:
        return asyncio.run(VC.teach(name))
    except (ValueError, RuntimeError, ConnectionError) as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}")


@app.post("/api/robot/run")
def robot_run(req: RobotRunReq):
    import time as _t

    from amperes.integrations import viam_control as VC
    from amperes.integrations.viam_live import LiveStatus

    base = VC.TrialConfig(name="baseline", approach_mm=req.approach_mm, grasp_z_offset_mm=req.grasp_z_offset_mm,
                          settle_s=req.settle_s, trials=req.trials)
    configs = None
    if req.ab:
        allowed = {"name", "approach_mm", "grasp_z_offset_mm", "settle_s"}
        if set(req.ab) - allowed:
            raise HTTPException(400, f"ab may only change {sorted(allowed)}")
        configs = [base, VC.TrialConfig(**{**base.__dict__, "name": "fix", **req.ab})]
    out = store.artifacts_dir() / "robot_sessions" / _t.strftime("%Y%m%dT%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    ROBOT["status"], ROBOT["stop"] = LiveStatus(out / "live.json"), threading.Event()
    ROBOT["status"].update(running=True, phase="connecting", execute=req.execute)
    spec = VC.RunSpec(config=base, execute=req.execute, operator_ack=req.ack, configs=configs, object_name=req.object_name)

    def job(log):
        try:
            return VC.run_and_import(spec, out, ROBOT["status"], ROBOT["stop"], log=log)
        except Exception as exc:
            ROBOT["status"].update(running=False, done=True, error=f"{type(exc).__name__}: {exc}")
            raise

    return _run_job("robot_trials", job)


@app.post("/api/robot/stop")
def robot_stop():
    if ROBOT["stop"] is not None:
        ROBOT["stop"].set()
    return {"stop_requested": True, "note": "the arm and gripper are sent stop() immediately, including mid-move"}

from amperes.robot_api import router as _robot_router  # noqa: E402  (scan + chess endpoints, /chess page)

app.include_router(_robot_router)
