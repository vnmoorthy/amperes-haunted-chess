"""Failure reproduction engine with explicit replay classes (A–E) and pre-declared reproduction criteria.

Modes for simulator episodes:
- `rerun`: closed-loop re-execution of the recorded policy config on the recorded condition and seed.
- `action_replay`: open-loop replay of the recorded action sequence on the recorded condition and seed.

Classification of each attempt (criteria fixed before running):
- `exact`: replay class A, same verifier outcome and failure category, and max object-trajectory deviation ≤ 1e-6 m
  over identical length.
- `symptom_reproduced`: same outcome and category, but not exact (deviation, length mismatch, or class B/C).
- `outcome_only`: same verifier outcome label, different failure category. Not counted as a reproduction.
- `not_reproduced`: different outcome label.
Imported historical logs without replay info are class D/E and are never re-executed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import mujoco
import numpy as np

from amperes import store
from amperes.provenance import new_id, now_iso
from amperes.schema import Episode, read_episode
from amperes.sim.env import TASK_VERSION, EnvParams, PickPlaceEnv
from amperes.sim.runner import run_episode, state_digest
from amperes.sim.verifier import arr

EXACT_TOL_M = 1e-6
CRITERIA = {
    "exact": f"class A, same outcome & category, identical length, max |Δ object_pos| <= {EXACT_TOL_M} m",
    "symptom_reproduced": "same outcome & category, not exact",
    "outcome_only": "same outcome label, different category (not counted as reproduction)",
    "not_reproduced": "different outcome label",
    "eligible": "verifier outcome in {failure, timeout}; category not INFRASTRUCTURE_ERROR; replay class A/B/C",
}
NONDETERMINISM_NOTES = [
    "MuJoCo is deterministic for identical model, initial state, and control inputs on the same build and CPU.",
    "Cross-platform or cross-version bitwise equality is not guaranteed (floating-point and solver changes).",
    "Observation noise is drawn from a NumPy PCG64 generator seeded per condition.",
]


def replay_class(ep: Episode) -> tuple[str, list[str]]:
    """Decide what kind of reproduction the available data supports."""
    notes = []
    r = ep.replay
    if r is None or r.env_params is None or r.seed is None:
        if r is not None and r.policy_config:
            return "C", ["policy config present but initial conditions incomplete"]
        has_streams = bool(ep.streams)
        return ("D" if has_streams else "E"), ["no replayable initial conditions in the record"]
    if r.sim_task_id != "pick_place_box":
        return "D", [f"no registered simulator for task {r.sim_task_id!r}"]
    try:
        params = EnvParams(**r.env_params)
    except Exception as exc:
        return "E", [f"recorded env_params do not validate: {exc}"]
    if r.sim_task_version != TASK_VERSION:
        notes.append(f"task version differs: recorded {r.sim_task_version}, current {TASK_VERSION}")
    if r.simulator_version != mujoco.__version__:
        notes.append(f"simulator version differs: recorded {r.simulator_version}, current {mujoco.__version__}")
    if r.initial_state_digest:
        env = PickPlaceEnv(params, r.seed)
        env.reset()
        if state_digest(env) == r.initial_state_digest and not notes:
            return "A", ["initial-state digest matches regenerated state"]
        notes.append("initial-state digest mismatch" if state_digest(env) != r.initial_state_digest else "")
    return "B", [n for n in notes if n] or ["initial conditions available without a state digest"]


def eligible(ep: Episode, cls: str) -> tuple[bool, str]:
    if ep.outcome is None or ep.outcome.label not in ("failure", "timeout"):
        return False, "original outcome is not a failure/timeout"
    if ep.diagnosis and ep.diagnosis.category == "INFRASTRUCTURE_ERROR":
        return False, "infrastructure errors are not policy failures"
    if cls not in ("A", "B", "C"):
        return False, f"replay class {cls} cannot be re-executed"
    return True, "eligible"


def _compare(orig: Episode, new: Episode, cls: str) -> dict[str, Any]:
    _, a = arr(orig, "state.object_pos")
    _, b = arr(new, "state.object_pos")
    n = min(len(a), len(b))
    dev = float(np.max(np.abs(a[:n] - b[:n]))) if n else float("inf")
    same_outcome = orig.outcome.label == new.outcome.label
    oc = orig.diagnosis.category if orig.diagnosis else None
    nc = new.diagnosis.category if new.diagnosis else None
    same_cat = oc == nc
    if same_outcome and same_cat and cls == "A" and len(a) == len(b) and dev <= EXACT_TOL_M:
        cls_out = "exact"
    elif same_outcome and same_cat:
        cls_out = "symptom_reproduced"
    elif same_outcome:
        cls_out = "outcome_only"
    else:
        cls_out = "not_reproduced"
    return {
        "classification": cls_out,
        "original": {"outcome": orig.outcome.label, "category": oc, "rows": len(a)},
        "reproduced": {"outcome": new.outcome.label, "category": nc, "rows": len(b)},
        "max_object_pos_deviation_m": dev,
        "length_equal": len(a) == len(b),
        "discrepancies": [d for d in (
            None if same_outcome else f"outcome {orig.outcome.label} -> {new.outcome.label}",
            None if same_cat else f"category {oc} -> {nc}",
            None if len(a) == len(b) else f"trajectory length {len(a)} -> {len(b)}",
            None if dev <= EXACT_TOL_M else f"max object deviation {dev:.3g} m") if d],
    }


def reproduce(episode_path: str | Path, modes=("rerun", "action_replay"), experiment_id: str | None = None,
              persist: bool = True) -> list[dict[str, Any]]:
    orig = read_episode(Path(episode_path))
    cls, cls_notes = replay_class(orig)
    ok, why = eligible(orig, cls)
    out = []
    if not ok:
        rec = {"repro_id": new_id("repro"), "experiment_id": experiment_id, "original_episode_id": orig.episode_id,
               "mode": None, "new_run_id": None, "new_episode_id": None, "replay_class": cls, "eligible": 0,
               "classification": "ineligible", "criteria": CRITERIA,
               "comparison": {"reason": why, "replay_class_notes": cls_notes}, "created_at": now_iso()}
        if persist:
            store.insert("reproductions", rec)
        return [rec]
    r = orig.replay
    cond = {"condition_id": orig.conditions.get("condition_id"), "split": "reproduction",
            "env_params": r.env_params, "seed": r.seed, "parent_id": orig.conditions.get("condition_id"),
            "changed": orig.conditions.get("changed"), "description": f"reproduction of {orig.episode_id}"}
    for mode in modes:
        run_id = new_id("run_repro")
        spec = {"run_id": run_id, "episode_id": f"{run_id}-ep", "condition": cond,
                "artifacts_dir": str(store.artifacts_dir() / "runs"), "policy_config": r.policy_config}
        if mode == "action_replay":
            spec["replay_actions"] = orig.streams["act.command"].values
            spec["replay_source"] = orig.episode_id
            spec["replay_done_at"] = len(spec["replay_actions"]) if r.terminated_by == "policy_done" else None
        res = run_episode(spec)
        new = read_episode(Path(res["artifact"]))
        cmp = _compare(orig, new, cls)
        cmp["replay_class_notes"] = cls_notes
        cmp["nondeterminism_notes"] = NONDETERMINISM_NOTES
        cmp["config_differences"] = {} if mode == "rerun" else {"policy": "open-loop action replay"}
        rec = {"repro_id": new_id("repro"), "experiment_id": experiment_id, "original_episode_id": orig.episode_id,
               "mode": mode, "new_run_id": run_id, "new_episode_id": res["episode_id"], "replay_class": cls,
               "eligible": 1, "classification": cmp["classification"], "criteria": CRITERIA,
               "comparison": cmp, "created_at": now_iso()}
        if persist:
            store.insert("reproductions", rec)
            store.insert("runs", {"run_id": run_id, "experiment_id": experiment_id, "kind": f"reproduction:{mode}",
                                  "policy_label": None, "policy_config": r.policy_config, "split": "reproduction",
                                  "n_episodes": 1, "status": "completed", "started_at": now_iso(),
                                  "finished_at": now_iso(), "wall_s": res["wall_time_s"]})
            store.record_run_episodes([res], [cond], None, dataset_id=run_id)
        out.append(rec)
    return out


def reproduction_rates(records: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"criteria": CRITERIA}
    for mode in ("rerun", "action_replay"):
        rs = [r for r in records if r["mode"] == mode and r["eligible"]]
        exact = sum(r["classification"] == "exact" for r in rs)
        sym = sum(r["classification"] in ("exact", "symptom_reproduced") for r in rs)
        out[mode] = {"eligible_attempts": len(rs), "exact": exact, "exact_or_symptom": sym,
                     "exact_rate": exact / len(rs) if rs else None,
                     "symptom_rate": sym / len(rs) if rs else None}
    out["ineligible"] = sum(1 for r in records if not r["eligible"])
    return out
