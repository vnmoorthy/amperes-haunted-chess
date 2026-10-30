"""Regression suite: reproducible cases with provenance, rerunnable against any registered policy config."""

from __future__ import annotations

from typing import Any

from amperes import store
from amperes.provenance import new_id, now_iso
from amperes.sim.env import TASK_ID, TASK_VERSION
from amperes.sim.policies import ScriptedConfig
from amperes.sim.runner import run_batch

CASE_KINDS = ("known_failure", "guard", "nominal", "incident")


def make_case(suite_id: str, kind: str, condition: dict[str, Any], source: dict[str, Any], split: str,
              incident_id: str | None = None) -> dict[str, Any]:
    if kind not in CASE_KINDS:
        raise ValueError(f"unknown case kind {kind}")
    return {
        "case_id": new_id("case"), "suite_id": suite_id, "kind": kind,
        "condition_id": condition["condition_id"], "env_params": condition["env_params"],
        "seed": condition["seed"], "task_id": TASK_ID, "task_version": TASK_VERSION,
        "source_episode_id": source.get("episode_id"), "source_policy_label": source.get("policy_label"),
        "source_outcome": source.get("outcome"),
        "expected": {"outcome": "success", "rule": "verifier outcome must be 'success'"},
        "provenance": {"source_run_id": source.get("run_id"), "source_category": source.get("category"),
                       "source_rule_id": source.get("rule_id"), "created_from": kind},
        "split": split, "incident_id": incident_id, "created_at": now_iso(),
    }


def create_suite(experiment_id: str | None, name: str, cases: list[dict[str, Any]], note: str = "") -> str:
    suite_id = cases[0]["suite_id"] if cases else new_id("suite")
    store.insert("regression_suites", {"suite_id": suite_id, "experiment_id": experiment_id, "name": name,
                                       "created_at": now_iso(), "note": note})
    store.insert_many("regression_cases", cases)
    return suite_id


def run_suite(suite_id: str, policy_cfg: ScriptedConfig, label: str, workers: int | None = None) -> dict[str, Any]:
    cases = store.query("SELECT * FROM regression_cases WHERE suite_id=? ORDER BY case_id", [suite_id])
    if not cases:
        raise ValueError(f"suite {suite_id} has no cases")
    run_id = new_id("run_regr")
    conds = [{"condition_id": c["condition_id"], "env_params": store.loads(c["env_params"]), "seed": c["seed"],
              "split": "regression", "parent_id": c["condition_id"], "changed": None,
              "description": f"regression case {c['case_id']}"} for c in cases]
    specs = [{"run_id": run_id, "episode_id": f"{run_id}-{i:04d}", "condition": cond,
              "policy_config": policy_cfg.model_dump(), "artifacts_dir": str(store.artifacts_dir() / "runs")}
             for i, cond in enumerate(conds)]
    started = now_iso()
    results = run_batch(specs, workers)
    store.insert("runs", {"run_id": run_id, "experiment_id": None, "kind": "regression", "policy_label": label,
                          "policy_config": policy_cfg.model_dump(), "split": "regression", "n_episodes": len(results),
                          "status": "completed", "started_at": started, "finished_at": now_iso(),
                          "wall_s": sum(r["wall_time_s"] for r in results)})
    store.insert("datasets", {"dataset_id": run_id, "name": f"regression {suite_id} / {label}", "kind": "sim_run",
                              "adapter": "amperes.sim", "created_at": now_iso(), "synthetic": 1,
                              "n_records": len(results), "status": "completed", "note": "simulation"})
    store.record_run_episodes(results, conds, label, dataset_id=run_id)
    rows, counts = [], {"fixed": 0, "still_failing": 0, "regressed": 0, "still_passing": 0}
    for c, r in zip(cases, results):
        passed = r["outcome"] == "success"
        was_ok = c["source_outcome"] == "success"
        status = ("still_passing" if passed else "regressed") if was_ok else ("fixed" if passed else "still_failing")
        counts[status] += 1
        rows.append({"result_id": new_id("rr"), "suite_id": suite_id, "case_id": c["case_id"], "run_id": run_id,
                     "policy_label": label, "episode_id": r["episode_id"], "outcome": r["outcome"],
                     "passed": int(passed), "status": status, "created_at": now_iso()})
    store.insert_many("regression_results", rows)
    by_kind: dict[str, dict[str, int]] = {}
    for c, row in zip(cases, rows):
        k = by_kind.setdefault(c["kind"], {"n": 0, "passed": 0})
        k["n"] += 1
        k["passed"] += row["passed"]
    return {"suite_id": suite_id, "run_id": run_id, "policy_label": label, "n_cases": len(cases),
            "passed": sum(r["passed"] for r in rows), "status_counts": counts, "by_kind": by_kind,
            "new_failures": [{"case_id": c["case_id"], "kind": c["kind"], "condition_id": c["condition_id"],
                              "episode_id": row["episode_id"], "outcome": row["outcome"]}
                             for c, row in zip(cases, rows) if row["status"] == "regressed"]}
