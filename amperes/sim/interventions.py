"""Targeted interventions (policy-config deltas) and the pre-registered selection rule.

Selection rule (fixed before any held-out evaluation, recorded in the experiment config):
  1. Target category = most frequent verified failure category in the *baseline dev* run
     (excluding INVALID_ACTION, ENVIRONMENT_ERROR, INFRASTRUCTURE_ERROR, UNKNOWN).
  2. Evaluate every catalog candidate for that category on the same dev conditions.
  3. Rank by dev successes (desc), then dev regressions = baseline successes broken (asc), then mean episode
     duration (asc). Rank 1 is `selected`; rank 2 is kept as the `alternative` ablation.
  4. A `control` (identical config under a new label) is also evaluated to check the harness produces no spurious
     differences.
The held-out set is never passed to selection code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from amperes.sim.policies import ScriptedConfig

EXCLUDED = {"INVALID_ACTION", "ENVIRONMENT_ERROR", "INFRASTRUCTURE_ERROR", "UNKNOWN"}
SELECTION_RULE = ("target=most frequent verified dev failure category; rank candidates by dev successes desc, "
                  "dev regressions asc, mean duration asc; rank1=selected, rank2=alternative")


@dataclass(frozen=True)
class Intervention:
    name: str
    delta: dict[str, Any]
    rationale: str
    mechanism: str
    extra: dict[str, Any] = field(default_factory=dict)

    def config(self, base: ScriptedConfig) -> ScriptedConfig:
        return base.model_copy(update=self.delta)


CATALOG: dict[str, list[Intervention]] = {
    "COLLISION": [
        Intervention("obstacle_aware_transport", {"obstacle_aware": True},
                     "Raise transport height so the carried object's bottom clears the observed obstacle top by "
                     "obstacle_clearance (0.03 m).", "adaptive path clearance"),
        Intervention("fixed_high_transport", {"transport_height": 0.24},
                     "Always transport at 0.24 m TCP height regardless of the obstacle.", "fixed path clearance"),
    ],
    "SLIPPAGE": [
        Intervention("grip_force_12N", {"grip_force": 12.0}, "Double squeeze force (friction-limited hold).", "grip force"),
        Intervention("grip_force_20N", {"grip_force": 20.0}, "Higher squeeze force.", "grip force"),
        Intervention("gentle_transport", {"max_speed": 0.25, "max_accel": 1.0},
                     "Lower acceleration reduces inertial load on the grasp.", "motion profile"),
    ],
    "GRASP_FAILURE": [
        Intervention("grip_force_12N", {"grip_force": 12.0}, "Higher squeeze force for heavy/slippery objects.", "grip force"),
        Intervention("grasp_check_retry", {"grasp_check": True, "max_grasp_retries": 2, "perception_samples": 4},
                     "Check finger gap after closing; re-perceive and retry when inconsistent with object width.",
                     "recovery strategy"),
    ],
    "REACH_FAILURE": [
        Intervention("perception_avg_8", {"perception_samples": 8},
                     "Average 8 object-position observations before approaching.", "perception filtering"),
        Intervention("grasp_check_retry", {"grasp_check": True, "max_grasp_retries": 2, "perception_samples": 4},
                     "Detect a missed grasp from the finger gap and retry.", "recovery strategy"),
    ],
    "PLACEMENT_FAILURE": [
        Intervention("low_release", {"place_clearance": 0.003}, "Release closer to the table.", "release height"),
        Intervention("long_release_settle", {"release_settle_s": 0.8}, "Wait longer before retreating.", "release timing"),
    ],
    "TIMEOUT": [
        Intervention("faster_motion", {"max_speed": 0.5, "max_accel": 5.0}, "Faster motion profile.", "motion profile"),
        Intervention("longer_phase_timeout", {"phase_timeout_s": 6.0}, "Allow slower phases.", "phase timing"),
    ],
}


def target_category(dev_results: list[dict[str, Any]]) -> tuple[str | None, dict[str, int]]:
    counts: dict[str, int] = {}
    for r in dev_results:
        c = r.get("category")
        if r["outcome"] != "success" and c and c not in EXCLUDED:
            counts[c] = counts.get(c, 0) + 1
    if not counts:
        return None, counts
    best = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return best, counts


def rank_candidates(evals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """evals: [{name, comparison (paired vs baseline dev)}]."""
    def key(e):
        c = e["comparison"]
        return (-c["treatment"]["successes"], c["broken_by_treatment"], c["treatment"]["mean_duration_all_s"] or 0.0)

    return sorted(evals, key=key)
