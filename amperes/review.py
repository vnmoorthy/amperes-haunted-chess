"""Human review of findings/incidents, and a counterbalanced workflow-timing study (existing vs assisted).

Reviews are append-only; the latest decision per target counts. Precision is computed only among reviewed findings.
Recall is never derived from reviewer feedback (reviewers only see what was flagged).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean, median
from typing import Any

import numpy as np

from amperes import store
from amperes.provenance import new_id, now_iso

DECISIONS = ("correct_actionable", "correct_not_actionable", "incorrect", "duplicate", "insufficient_evidence",
             "needs_investigation")
CONFIRMED = ("correct_actionable", "correct_not_actionable")
UNRESOLVED = ("insufficient_evidence", "needs_investigation")
CONDITIONS = {"A": "existing workflow (raw records, own tools)", "B": "Amperes-assisted (findings, incidents, evidence links)"}
MIN_TASKS_PER_CONDITION = 10


def record_review(target_kind: str, target_id: str, decision: str, reviewer: str, notes: str = "") -> dict[str, Any]:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    if not reviewer or "@" in reviewer:
        raise ValueError("reviewer must be a pseudonymous id (no e-mail addresses)")
    if target_kind == "finding":
        rows = store.query("SELECT rule_id, rule_version FROM quality_findings WHERE finding_id=?", [target_id])
    elif target_kind == "incident":
        rows = store.query("SELECT category AS rule_id, NULL AS rule_version FROM incidents WHERE incident_id=?", [target_id])
    else:
        raise ValueError("target_kind must be 'finding' or 'incident'")
    if not rows:
        raise ValueError(f"unknown {target_kind} {target_id}")
    row = {"review_id": new_id("rev"), "target_kind": target_kind, "target_id": target_id, "rule_id": rows[0]["rule_id"],
           "rule_version": rows[0]["rule_version"], "decision": decision, "reviewer": reviewer, "notes": notes,
           "created_at": now_iso()}
    store.insert("reviews", row)
    return row


def review_report(dataset_id: str) -> dict[str, Any]:
    findings = store.query("SELECT finding_id, rule_id FROM quality_findings WHERE dataset_id=? AND severity<>'info'", [dataset_id])
    n_info = store.query("SELECT COUNT(*) n FROM quality_findings WHERE dataset_id=? AND severity='info'", [dataset_id])[0]["n"]
    ids = {f["finding_id"]: f["rule_id"] for f in findings}
    latest: dict[str, dict] = {}
    for r in store.query("SELECT * FROM reviews WHERE target_kind='finding' ORDER BY created_at, rowid"):
        if r["target_id"] in ids:
            latest[r["target_id"]] = r
    per_rule = defaultdict(lambda: Counter())
    for fid, rule in ids.items():
        per_rule[rule]["findings"] += 1
        if fid in latest:
            per_rule[rule]["reviewed"] += 1
            per_rule[rule][latest[fid]["decision"]] += 1
    total = Counter()
    for c in per_rule.values():
        total.update(c)

    def summarize(c: Counter) -> dict[str, Any]:
        conf = sum(c[d] for d in CONFIRMED)
        judged = conf + c["incorrect"]
        return {"findings": c["findings"], "reviewed": c["reviewed"],
                "coverage": c["reviewed"] / c["findings"] if c["findings"] else None,
                "confirmed": conf, "confirmed_actionable": c["correct_actionable"], "false_positives": c["incorrect"],
                "duplicates": c["duplicate"], "unresolved": sum(c[d] for d in UNRESOLVED),
                "precision_among_reviewed": {"value": conf / judged, "numerator": conf, "denominator": judged} if judged
                else {"value": "not evaluated", "numerator": 0, "denominator": 0}}

    return {"dataset_id": dataset_id, "overall": summarize(total), "by_rule": {k: summarize(v) for k, v in sorted(per_rule.items())},
            "info_notes_excluded": n_info,
            "recall": "not evaluated — reviewer feedback only covers flagged items",
            "rule_noise_ranking": sorted(((k, v["incorrect"] + v["duplicate"]) for k, v in per_rule.items()), key=lambda x: -x[1])[:10]}


# ------------------------------------------------------------------------------------------------ workflow study
def plan_study(dataset_id: str, reviewers: list[str], sample_size: int = 20, seed: int = 0) -> dict[str, Any]:
    """Two comparable halves (stratified by quality status); each reviewer does one half per condition,
    with condition order alternated across reviewers (AB / BA) to reduce order effects."""
    eps = store.query("SELECT episode_id, quality_status FROM episodes WHERE dataset_id=? ORDER BY episode_id", [dataset_id])
    if len(eps) < 2 or not reviewers:
        raise ValueError("need >= 2 episodes and >= 1 reviewer")
    rng = np.random.default_rng(seed)
    by_status = defaultdict(list)
    for e in eps:
        by_status[e["quality_status"]].append(e["episode_id"])
    half1, half2 = [], []
    for _, ids in sorted(by_status.items()):
        ids = list(rng.permutation(ids))[: max(1, sample_size * len(ids) // len(eps))]
        half1 += ids[::2]
        half2 += ids[1::2]
    study_id = new_id("study")
    tasks = []
    for i, rv in enumerate(reviewers):
        order = [("A", "S1"), ("B", "S2")] if i % 2 == 0 else [("B", "S1"), ("A", "S2")]
        if i % 4 >= 2:
            order = order[::-1]
        for pos, (cond, sample) in enumerate(order):
            tasks.append({"task_id": new_id("wt"), "study_id": study_id, "reviewer": rv, "condition": cond,
                          "sample_id": f"{dataset_id}:{sample}", "task_type": f"triage_sample(order={pos + 1})",
                          "started_at": None, "finished_at": None, "duration_s": None, "confirmed_findings": None,
                          "incorrect_findings": None, "missing_info": None, "notes": None})
    store.insert_many("workflow_tasks", tasks)
    return {"study_id": study_id, "conditions": CONDITIONS, "samples": {"S1": half1, "S2": half2}, "tasks": tasks,
            "instructions": "For each task: start, inspect the sample, write an incident note and (if possible) a reproducible "
                            "case, then finish with counts. Condition A uses your current tools only."}


def start_task(task_id: str) -> None:
    if not store.execute("UPDATE workflow_tasks SET started_at=? WHERE task_id=? AND started_at IS NULL", [now_iso(), task_id]):
        raise ValueError(f"task {task_id} unknown or already started")


def finish_task(task_id: str, confirmed: int, incorrect: int, missing_info: str = "", notes: str = "",
                duration_s: float | None = None) -> None:
    rows = store.query("SELECT started_at FROM workflow_tasks WHERE task_id=?", [task_id])
    if not rows or not rows[0]["started_at"]:
        raise ValueError(f"task {task_id} not started")
    from datetime import datetime

    end = now_iso()
    dur = duration_s if duration_s is not None else (datetime.fromisoformat(end) - datetime.fromisoformat(rows[0]["started_at"])).total_seconds()
    store.execute("UPDATE workflow_tasks SET finished_at=?, duration_s=?, confirmed_findings=?, incorrect_findings=?, missing_info=?, "
                  "notes=? WHERE task_id=?", [end, dur, confirmed, incorrect, missing_info, notes, task_id])


def study_report(study_id: str) -> dict[str, Any]:
    tasks = store.query("SELECT * FROM workflow_tasks WHERE study_id=?", [study_id])
    if not tasks:
        raise ValueError(f"unknown study {study_id}")
    out = {"study_id": study_id, "conditions": CONDITIONS, "by_condition": {}}
    for c in ("A", "B"):
        done = [t for t in tasks if t["condition"] == c and t["duration_s"] is not None]
        out["by_condition"][c] = {"tasks_planned": sum(t["condition"] == c for t in tasks), "tasks_completed": len(done),
                                  "median_duration_s": median(t["duration_s"] for t in done) if done else None,
                                  "mean_duration_s": mean(t["duration_s"] for t in done) if done else None,
                                  "confirmed_findings": sum(t["confirmed_findings"] or 0 for t in done),
                                  "incorrect_findings": sum(t["incorrect_findings"] or 0 for t in done),
                                  "missing_info_notes": [t["missing_info"] for t in done if t["missing_info"]]}
    a, b = out["by_condition"]["A"], out["by_condition"]["B"]
    if a["tasks_completed"] and b["tasks_completed"]:
        out["median_time_saved_per_task_s"] = a["median_duration_s"] - b["median_duration_s"]
        small = min(a["tasks_completed"], b["tasks_completed"]) < MIN_TASKS_PER_CONDITION
        out["status"] = "preliminary (fewer than %d tasks per condition)" % MIN_TASKS_PER_CONDITION if small else "measured"
    else:
        out["median_time_saved_per_task_s"] = None
        out["status"] = "not measured — no completed tasks in one or both conditions"
    out["caveat"] = "Counterbalanced AB/BA order across reviewers; samples are split by quality status. Not causal with small n."
    return out
