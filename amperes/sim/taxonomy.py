"""Deterministic failure taxonomy for pick-and-place episodes.

Each classification separates (1) observed evidence, (2) the deterministic rule that fired, (3) an optional
hypothesis slot (unused by default), (4) confidence, and (5) what evidence would be needed to establish causality.
"""

from __future__ import annotations

import numpy as np

from amperes.schema import Diagnosis, Episode, Outcome
from amperes.sim.env import C_LEFT_OBJECT, C_RIGHT_OBJECT
from amperes.sim.verifier import arr

TAXONOMY_VERSION = "1.0.0"
REACH_XY_TOL = 0.015

CATEGORIES = {
    "INVALID_ACTION": "Policy emitted a non-finite or malformed action",
    "ENVIRONMENT_ERROR": "Simulator became numerically unstable",
    "INFRASTRUCTURE_ERROR": "Policy exception or wall-clock timeout in the harness",
    "COLLISION": "Robot or object contacted a prohibited obstacle",
    "TIMEOUT": "Step budget exhausted before the policy finished",
    "REACH_FAILURE": "Tool never got within tolerance of the object before grasping",
    "GRASP_FAILURE": "Tool reached the object but never achieved a lifting two-finger grasp",
    "SLIPPAGE": "Object was lifted, then lost two-finger contact while the gripper was still commanded closed",
    "RELEASE_FAILURE": "Object still in finger contact at the end of the episode",
    "PLACEMENT_FAILURE": "Object was released but did not settle inside the target region",
    "PLANNING_FAILURE": "Motion service could not plan or execute a commanded pose (real arm)",
    "POSE_ERROR": "Arm settled farther from the commanded pose than the tolerance (real arm)",
    "CAMERA_NO_FRAME": "A required camera observation was not captured (real arm)",
    "UNKNOWN": "No rule matched; needs manual review",
}


def _diag(cat, rule, conf, ev, causal, alts=()):
    return Diagnosis(category=cat, rule_id=rule, taxonomy_version=TAXONOMY_VERSION, confidence=conf,
                     observed_evidence=ev, alternatives=list(alts), causal_note=causal)


def classify(ep: Episode, outcome: Outcome) -> Diagnosis | None:
    if outcome.label == "success":
        return None
    m = outcome.measurements
    term = m.get("terminated_by")
    if outcome.label == "unknown":
        return _diag("UNKNOWN", "T0_unverifiable", "low", {"reason": outcome.reason},
                     "Outcome could not be verified from the available streams.")
    if term == "invalid_action":
        return _diag("INVALID_ACTION", "T1_invalid_action", "high", {"terminated_by": term},
                     "Directly observed: the policy output failed validation.")
    if term == "sim_instability":
        return _diag("ENVIRONMENT_ERROR", "T2_sim_instability", "high", {"terminated_by": term},
                     "Simulator reported bad accelerations or non-finite state; the task result is not meaningful.")
    if term in ("policy_exception", "wallclock_timeout"):
        return _diag("INFRASTRUCTURE_ERROR", "T3_harness", "high",
                     {"terminated_by": term, "error": ep.extra.get("error")},
                     "Harness-level failure; says nothing about policy competence.")

    t, obj = arr(ep, "state.object_pos")
    _, tcp = arr(ep, "state.tcp_pos")
    _, c = arr(ep, "state.contacts")
    c = c[:, 0].astype(int)
    both = ((c & C_LEFT_OBJECT) > 0) & ((c & C_RIGHT_OBJECT) > 0)
    h = float(ep.replay.env_params["object_half_size"])
    grip = np.full(len(t), np.nan)
    if "act.command" in ep.streams:
        _, a = arr(ep, "act.command")
        n = min(len(a), len(t))
        grip[:n] = a[:n, 3]

    if m.get("obstacle_contact_steps", 0) > 0:
        ev = {"obstacle_contact_steps": m["obstacle_contact_steps"], "first_contact_t": m["first_obstacle_contact_t"],
              "obstacle_height_m": ep.replay.env_params.get("obstacle_height"),
              "max_object_lift_m": round(m["max_object_lift_m"], 4)}
        alts = ["SLIPPAGE"] if m["lifted"] and not m["final_in_target_region"] else []
        return _diag("COLLISION", "T4_obstacle_contact", "high" if not alts else "medium", ev,
                     "Contact is directly observed. Whether transport height, path planning, or a perception error "
                     "caused it requires comparing the planned path to the obstacle geometry.", alts)
    if term == "max_steps":
        return _diag("TIMEOUT", "T5_step_budget", "high",
                     {"steps": m["n_state_rows"], "lifted": m["lifted"], "final_in_target": m["final_in_target_region"],
                      "last_policy_phase(policy-reported)": _last_phase(ep)},
                     "Budget exhaustion is observed; the slow phase is only known from policy-reported annotations.")

    if not m["lifted"]:
        pre = np.arange(len(t)) < (np.argmax(both) if both.any() else len(t))
        xy_err = np.hypot(tcp[pre, 0] - obj[pre, 0], tcp[pre, 1] - obj[pre, 1]) if pre.any() else np.array([np.inf])
        low = tcp[pre, 2] < h + 0.01 if pre.any() else np.array([False])
        min_err = float(np.min(xy_err[low])) if low.any() else float(np.min(xy_err))
        ev = {"min_tcp_object_xy_error_m_near_grasp_height": round(min_err, 4),
              "both_finger_contact_ever": bool(both.any()),
              "max_object_lift_m": round(m["max_object_lift_m"], 4)}
        ev.update(_perception_error(ep))
        if min_err > REACH_XY_TOL and not both.any():
            return _diag("REACH_FAILURE", "T6_reach_error", "high", ev,
                         "Position error at grasp height is observed. Perception error is a candidate cause only if "
                         "the recorded object estimate deviates from ground truth by a similar amount.",
                         ["GRASP_FAILURE"])
        alts = ["SLIPPAGE(during lift)"] if both.any() else []
        return _diag("GRASP_FAILURE", "T7_no_lifting_grasp", "high" if not alts else "medium", ev,
                     "No lifting two-finger grasp was observed. Insufficient friction-limited holding force is a "
                     "candidate when contact occurred; confirming it needs force or slip measurements.", alts)

    lift_k = int(np.argmax((both) & (obj[:, 2] - h >= 0.02)))
    slip_k = None
    for k in range(lift_k, len(t)):
        if not np.isnan(grip[k]) and grip[k] > 0 and not both[k] and obj[k, 2] - h > 0.004:
            slip_k = k
            break
    if slip_k is not None:
        return _diag("SLIPPAGE", "T8_contact_lost_while_closed", "high",
                     {"first_lift_t": round(float(t[lift_k]), 3), "contact_lost_t": round(float(t[slip_k]), 3),
                      "object_lift_at_loss_m": round(float(obj[slip_k, 2] - h), 4),
                      "grip_command_normalized": round(float(grip[slip_k]), 3),
                      "final_xy_dist_to_target_m": round(m["final_xy_dist_to_target_m"], 4),
                      "condition_context": {k: ep.replay.env_params.get(k) for k in ("object_mass", "object_friction")}},
                     "Loss of contact while closed is observed. Mass/friction/acceleration are candidate contributors; "
                     "a controlled sweep of each is required before attributing cause.")
    if not m["released_at_end"]:
        return _diag("RELEASE_FAILURE", "T9_not_released", "high", {"final_object_pos": m["final_object_pos"]},
                     "Finger contact at the end of the episode is observed.")
    if not (m["in_region_whole_final_window"] and m["final_resting_on_table"]):
        return _diag("PLACEMENT_FAILURE", "T10_outside_target", "high",
                     {"final_xy_dist_to_target_m": round(m["final_xy_dist_to_target_m"], 4),
                      "final_in_target_region": m["final_in_target_region"],
                      "final_resting_on_table": m["final_resting_on_table"],
                      "final_speed_mps": round(m["final_speed_mps"], 4)},
                     "Final pose outside the region is observed; the release dynamics that produced it are not.")
    return _diag("UNKNOWN", "T99_no_rule", "low", {"reason": outcome.reason}, "No deterministic rule matched.")


def _last_phase(ep: Episode) -> str | None:
    phases = [e.value for e in ep.events if e.type == "policy_phase"]
    return phases[-1] if phases else None


def _perception_error(ep: Episode) -> dict:
    """Compare the policy's (noisy) object estimate with ground truth at the first observation. Observed, not causal."""
    if "obs.object_pos_est" not in ep.streams:
        return {}
    _, est = arr(ep, "obs.object_pos_est")
    _, obj = arr(ep, "state.object_pos")
    e = np.hypot(est[0, 0] - obj[0, 0], est[0, 1] - obj[0, 1])
    return {"first_object_estimate_xy_error_m": round(float(e), 4)}
