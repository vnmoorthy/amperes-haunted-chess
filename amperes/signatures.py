"""Deterministic failure signatures and similar-episode search.

A signature is a fixed-length numeric vector built only from recorded evidence: which quality rules fired (by
severity weight), the episode's quality status, the classes of operational events linked to it, the simulator failure
category if any, and the missing-sample fraction. No language model or learned embedding is involved, so two episodes
are "similar" only when the same evidence is present. Used locally (cosine) and as the vector for Atlas Vector Search.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from amperes import store

SIGNATURE_VERSION = "sig/1"
RULE_IDS = [
    "C001_required_metadata", "C002_no_observations", "C003_no_actions", "C004_truncated", "C005_missing_outcome",
    "C006_time_bounds", "C007_missing_samples", "C008_missing_channel", "C009_source_rows_dropped",
    "T001_non_monotonic", "T002_duplicate_timestamps", "T003_time_gap", "T004_rate_mismatch",
    "T005_obs_action_misalignment", "T006_clock_plausibility", "S001_non_finite", "S002_out_of_range",
    "S003_action_discontinuity", "S004_stale_signal", "S005_command_saturation", "O002_label_contradicts_state",
    "D001_duplicate_content", "D002_duplicate_source_id",
]
STATUSES = ["infrastructure_error", "invalid_data", "suspicious", "verified_success", "valid_success_unverified",
            "valid_failed_task", "unknown_outcome"]
EVENT_CLASSES = ["robot_task_failure", "data_recording_failure", "invalid_data", "hardware_fault", "software_fault",
                 "operator_interruption", "infrastructure_failure", "unclassified"]
CATEGORIES = ["COLLISION", "SLIPPAGE", "GRASP_FAILURE", "REACH_FAILURE", "PLACEMENT_FAILURE", "RELEASE_FAILURE", "TIMEOUT",
              "INVALID_ACTION", "ENVIRONMENT_ERROR", "INFRASTRUCTURE_ERROR"]
SEV_W = {"critical": 1.0, "major": 0.7, "minor": 0.3, "info": 0.0}
DIMS = len(RULE_IDS) + len(STATUSES) + len(EVENT_CLASSES) + len(CATEGORIES)


def _vec(rules: dict[str, float], status: str | None, ev_counts: dict[str, int], category: str | None) -> list[float]:
    v = [rules.get(r, 0.0) for r in RULE_IDS]
    v += [1.0 if status == s else 0.0 for s in STATUSES]
    v += [min(1.0, math.log1p(ev_counts.get(c, 0)) / math.log(10)) for c in EVENT_CLASSES]
    v += [1.0 if category == c else 0.0 for c in CATEGORIES]
    return v


def signatures(dataset_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """episode_id → {vector, dataset_id, status, category, evidence}. Episodes with no evidence at all are skipped."""
    where = ("WHERE dataset_id IN (%s)" % ",".join("?" * len(dataset_ids))) if dataset_ids else ""
    params = list(dataset_ids or [])
    eps = store.query(f"SELECT episode_id, dataset_id, quality_status, category, reason, session_id, workcell_id FROM episodes {where}", params)
    rules: dict[str, dict[str, float]] = defaultdict(dict)
    for f in store.query(f"SELECT episode_id, rule_id, severity FROM quality_findings {where}", params):
        if f["episode_id"]:
            rules[f["episode_id"]][f["rule_id"]] = max(rules[f["episode_id"]].get(f["rule_id"], 0.0), SEV_W.get(f["severity"], 0.0))
    evc: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in store.query(f"SELECT episode_id, event_class FROM ops_events {where} {'AND' if where else 'WHERE'} episode_id IS NOT NULL "
                         "AND event_type IN ('fault','recording_error','manual_intervention','reset','restart')", params):
        evc[e["episode_id"]][e["event_class"]] += 1
    out = {}
    for e in eps:
        eid = e["episode_id"]
        # robot trials (chess, pick-and-place) record their failure category as the reason
        cat = e["category"] or (e["reason"] if e["reason"] in CATEGORIES else None)
        v = _vec(rules.get(eid, {}), e["quality_status"], evc.get(eid, {}), cat)
        if not any(v[: len(RULE_IDS)]) and not evc.get(eid) and not cat:
            continue  # nothing wrong recorded: not a failure signature
        out[eid] = {"vector": v, "dataset_id": e["dataset_id"], "status": e["quality_status"], "category": cat,
                    "session_id": e["session_id"], "workcell_id": e["workcell_id"],
                    "evidence": {"rules": rules.get(eid, {}), "event_classes": dict(evc.get(eid, {}))}}
    return out


def _cos(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def similar_local(episode_id: str, k: int = 10, dataset_ids: list[str] | None = None) -> dict[str, Any]:
    sigs = signatures(dataset_ids)
    if episode_id not in sigs:
        own = store.query("SELECT dataset_id FROM episodes WHERE episode_id=?", [episode_id])
        if not own:
            raise ValueError(f"unknown episode {episode_id}")
        sigs.update({k_: v for k_, v in signatures([own[0]["dataset_id"]]).items()})
        if episode_id not in sigs:
            return {"episode_id": episode_id, "matches": [], "note": "episode has no recorded failure evidence"}
    q = sigs[episode_id]["vector"]
    ranked = sorted(((k_, _cos(q, s["vector"])) for k_, s in sigs.items() if k_ != episode_id), key=lambda x: -x[1])[:k]
    return {"episode_id": episode_id, "signature_version": SIGNATURE_VERSION, "engine": "local cosine",
            "query_evidence": sigs[episode_id]["evidence"],
            "matches": [{"episode_id": k_, "score": round(sc, 4), **{f: sigs[k_][f] for f in ("dataset_id", "status", "category", "session_id", "workcell_id")},
                         "evidence": sigs[k_]["evidence"]} for k_, sc in ranked],
            "note": "Similarity = shared recorded evidence (rules, status, event classes, failure category); not a causal claim."}
