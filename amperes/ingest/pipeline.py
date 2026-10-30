"""Import → validate → index pipeline. Read-only with respect to the source: files are only opened for reading.

Imported data is normalised into `$AMPERES_HOME/artifacts/datasets/<dataset_id>/` and indexed in SQLite.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from amperes import store
from amperes.ingest.base import get_adapter
from amperes.ops import dedupe_events
from amperes.provenance import new_id, now_iso
from amperes.quality import (RULESET_VERSION, check_episode, dataset_findings, episode_status, missing_channels,
                             quality_summary)
from amperes.schema import Episode, write_episode


def source_fingerprint(path: Path) -> dict[str, str]:
    """SHA-256 of every source file; used to prove shadow mode leaves the source untouched."""
    files = sorted(p for p in (path.rglob("*") if path.is_dir() else [path]) if p.is_file())
    return {str(p.relative_to(path) if path.is_dir() else p.name): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def stable_episode_id(dataset_id: str, source_id: str) -> str:
    return "ep_" + hashlib.sha256(f"{dataset_id}/{source_id}".encode()).hexdigest()[:16]


def verify_imported(ep: Episode) -> None:
    """If the record carries enough simulator state, run the independent verifier. A label that agrees becomes
    verified; a label that disagrees stays unverified and rule O002 reports the contradiction."""
    from amperes.quality import _recording_incomplete

    if not (ep.outcome and ep.replay and ep.replay.sim_task_id == "pick_place_box" and "state.object_pos" in ep.streams):
        return
    if _recording_incomplete(ep):
        return
    from amperes.sim.verifier import verify

    try:
        v = verify(ep)
    except Exception:
        return
    agree = {"timeout": "failure"}.get(v.label, v.label) == {"timeout": "failure"}.get(ep.outcome.label, ep.outcome.label)
    if v.label in ("success", "failure", "timeout") and agree:
        ep.outcome.verified = True
        ep.outcome.verifier_id, ep.outcome.verifier_version = v.verifier_id, v.verifier_version


def analyze(eps: list[Episode], ctx: dict | None = None):
    for ep in eps:
        verify_imported(ep)
    findings = []
    for ep in eps:
        findings += check_episode(ep, ctx)
    findings += missing_channels(eps, ctx)
    findings += dataset_findings(eps, ctx)
    statuses = {ep.episode_id: episode_status(ep, findings) for ep in eps}
    return findings, statuses


def import_dataset(path: str | Path, name: str | None = None, adapter: str = "auto", dataset_id: str | None = None,
                   synthetic: bool | None = None, note: str | None = None, ctx: dict | None = None) -> dict[str, Any]:
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    t0 = time.perf_counter()
    before = source_fingerprint(path)
    ad = get_adapter(adapter, path)
    dataset_id = dataset_id or new_id("ds")
    out_dir = store.artifacts_dir() / "datasets" / dataset_id / "episodes"
    eps: list[Episode] = []
    rejected = []
    n_records = 0
    for rec in ad.parse(path, dataset_id):
        n_records += 1
        if rec.episode is None:
            rejected.append({"record_ref": rec.record_ref, "errors": [e.as_dict() for e in rec.errors]})
            continue
        ep = rec.episode
        src_id = ep.source.source_record_id or ep.episode_id
        ep.extra["source_episode_id"] = ep.episode_id
        ep.extra["import_warnings"] = [e.as_dict() for e in rec.errors]
        ep.episode_id = stable_episode_id(dataset_id, f"{src_id}@{rec.record_ref}")
        ep.source.dataset_id = dataset_id
        ep.source.imported_at = now_iso()
        ep.source.content_sha256 = ep.content_digest()
        if synthetic is not None:
            ep.source.synthetic = synthetic or ep.source.synthetic
        eps.append(ep)
    events, ev_errors = ad.parse_events(path, dataset_id)
    events, n_dup_events = dedupe_events(events)
    link_events(eps, events)
    findings, statuses = analyze(eps, ctx)
    created = now_iso()
    rows = []
    for ep in eps:
        p = out_dir / f"{ep.episode_id}.json.gz"
        write_episode(ep, p)
        s, r, o = ep.session, ep.robot, ep.outcome
        rows.append({
            "episode_id": ep.episode_id, "dataset_id": dataset_id, "run_id": None,
            "condition_id": ep.conditions.get("condition_id"), "split": ep.conditions.get("split"),
            "seed": ep.replay.seed if ep.replay else None, "task_id": ep.task.id if ep.task else None,
            "task_version": ep.task.version if ep.task else None, "policy_id": ep.policy.id if ep.policy else None,
            "policy_version": ep.policy.version if ep.policy else None, "policy_label": None,
            "session_id": s.session_id if s else None, "workcell_id": s.workcell_id if s else None,
            "device_id": r.device_id if r else None, "operator_id": s.operator_id if s else None,
            "config_id": r.config_id if r else None, "attempt_index": ep.conditions.get("attempt_index"),
            "outcome": o.label if o else None, "outcome_provenance": o.provenance if o else None,
            "verified": int(bool(o and o.verified)), "reason": o.reason if o else None,
            "category": ep.diagnosis.category if ep.diagnosis else None,
            "rule_id": ep.diagnosis.rule_id if ep.diagnosis else None,
            "steps": max((len(st.t) for st in ep.streams.values() if st.kind == "action"), default=0),
            "sim_duration_s": None, "wall_time_s": None,
            "terminated_by": ep.replay.terminated_by if ep.replay else None, "collision": None,
            "replay_class": _replay_class(ep), "quality_status": statuses[ep.episode_id], "artifact_path": str(p),
            "content_digest": ep.source.content_sha256, "start_time": ep.start_time, "end_time": ep.end_time,
            "synthetic": int(ep.source.synthetic), "created_at": created})
    store.insert_many("episodes", rows)
    store.insert_many("quality_findings", [f.as_row(dataset_id, new_id("qf"), created) for f in findings])
    store.insert_many("ops_events", events, ignore=True)
    after = source_fingerprint(path)
    report = {
        "dataset_id": dataset_id, "adapter": f"{ad.name}@{ad.version}", "source": str(path),
        "records_seen": n_records, "episodes_imported": len(eps), "records_rejected": len(rejected),
        "rejected": rejected[:200], "events_imported": len(events), "duplicate_events_dropped": n_dup_events,
        "event_errors": [e.as_dict() for e in ev_errors][:200], "ruleset_version": RULESET_VERSION, "rule_settings": ctx or {},
        "quality": quality_summary(statuses, findings), "processing_s": round(time.perf_counter() - t0, 3),
        "source_unchanged": before == after, "source_files": len(before),
    }
    any_synth = any(ep.source.synthetic for ep in eps)
    store.insert("datasets", {"dataset_id": dataset_id, "name": name or path.name, "kind": "import", "adapter": report["adapter"],
                              "source_uri": str(path), "created_at": created, "synthetic": int(any_synth),
                              "note": note, "n_records": n_records, "n_rejected": len(rejected), "status": "imported",
                              "import_report": report})
    return report


ATTACHED_EVENT_TYPES = {"recording_error", "fault", "recovery", "reset", "manual_intervention"}


def link_events(eps: list[Episode], events: list[dict[str, Any]]) -> None:
    """Point operational events at Amperes episode ids and copy episode-scoped ones into the episode record.
    A recording_error attached this way makes the episode an infrastructure/recording failure, whatever its task outcome."""
    from amperes.schema import Event

    by_src = {ep.extra.get("source_episode_id"): ep for ep in eps}
    for ev in events:
        ep = by_src.get(ev["episode_id"]) if ev["episode_id"] else None
        if ep is None:
            continue
        ev["episode_id"] = ep.episode_id
        if ev["event_type"] in ATTACHED_EVENT_TYPES:
            ep.events.append(Event(t=ev["t"], type=ev["event_type"], code=ev["code"], message=ev["message"],
                                   source=f"ops_event:{ev['provenance']}", value={"event_class": ev["event_class"]}))


def validate_only(path: str | Path, adapter: str = "auto") -> dict[str, Any]:
    """Schema validation without importing anything (no database or artifact writes)."""
    path = Path(path).resolve()
    ad = get_adapter(adapter, path)
    ok, bad, warn = 0, [], []
    for rec in ad.parse(path, "validate"):
        if rec.episode is None:
            bad.append({"record_ref": rec.record_ref, "errors": [e.as_dict() for e in rec.errors]})
        else:
            ok += 1
            if rec.errors:
                warn.append({"record_ref": rec.record_ref, "episode_id": rec.episode.episode_id,
                             "dropped_rows": [e.as_dict() for e in rec.errors]})
    events, ev_err = ad.parse_events(path, "validate")
    return {"adapter": f"{ad.name}@{ad.version}", "valid_records": ok, "invalid_records": len(bad), "errors": bad[:100],
            "records_with_dropped_rows": warn[:100], "events_valid": len(events),
            "event_errors": [e.as_dict() for e in ev_err][:100]}


def _replay_class(ep: Episode) -> str:
    from amperes.sim.reproduce import replay_class

    try:
        return replay_class(ep)[0]
    except Exception:
        return "E"
