"""Independent outcome verifier for the pick-and-place task.

Reads only ground-truth simulator state streams (`state.*`) and episode termination metadata. It never reads the
policy's internal state, the policy's phase annotations, or any model-generated text.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from amperes.schema import Episode, Outcome
from amperes.sim.env import C_LEFT_OBJECT, C_OBJECT_OBSTACLE, C_RIGHT_OBJECT, C_ROBOT_OBSTACLE


class VerifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verifier_id: str = "pick_place_verifier"
    version: str = "1.0.0"
    lift_height: float = Field(0.02, gt=0, description="object bottom above table (m) to count as lifted")
    settle_speed: float = Field(0.03, gt=0, description="max final object speed (m/s)")
    stay_window_s: float = Field(0.4, gt=0, description="object must stay in target region for this final window")
    resting_z_tol: float = Field(0.006, gt=0)
    forbid_obstacle_contact: bool = True


REQUIRED_STREAMS = ("state.object_pos", "state.contacts", "state.tcp_pos")


def arr(ep: Episode, name: str) -> tuple[np.ndarray, np.ndarray]:
    s = ep.streams[name]
    return np.asarray(s.t, dtype=float), np.asarray([[np.nan if v is None else v for v in row] for row in s.values])


def measure(ep: Episode, cfg: VerifierConfig) -> dict[str, Any]:
    p = (ep.replay.env_params if ep.replay else None) or ep.conditions.get("env_params") or {}
    h = float(p["object_half_size"])
    tx, ty, tr = float(p["target_x"]), float(p["target_y"]), float(p["target_half_size"])
    t, obj = arr(ep, "state.object_pos")
    _, contacts = arr(ep, "state.contacts")
    contacts = contacts[:, 0].astype(int)
    both = ((contacts & C_LEFT_OBJECT) > 0) & ((contacts & C_RIGHT_OBJECT) > 0)
    any_finger = (contacts & (C_LEFT_OBJECT | C_RIGHT_OBJECT)) > 0
    bottom = obj[:, 2] - h
    lifted = both & (bottom >= cfg.lift_height)
    obstacle = (contacts & (C_OBJECT_OBSTACLE | C_ROBOT_OBSTACLE)) > 0
    in_region = (np.abs(obj[:, 0] - tx) <= tr) & (np.abs(obj[:, 1] - ty) <= tr)
    dt = np.diff(t)
    speed = np.zeros(len(t))
    if len(t) > 1:
        speed[1:] = np.linalg.norm(np.diff(obj, axis=0), axis=1) / np.where(dt > 0, dt, np.nan)
    win = t >= (t[-1] - cfg.stay_window_s)
    return {
        "n_state_rows": int(len(t)),
        "duration_s": float(t[-1] - t[0]) if len(t) else 0.0,
        "terminated_by": ep.replay.terminated_by if ep.replay else None,
        "lifted": bool(lifted.any()),
        "first_lift_t": float(t[np.argmax(lifted)]) if lifted.any() else None,
        "max_object_lift_m": float(np.nanmax(bottom)),
        "both_finger_contact_ever": bool(both.any()),
        "obstacle_contact_steps": int(obstacle.sum()),
        "first_obstacle_contact_t": float(t[np.argmax(obstacle)]) if obstacle.any() else None,
        "final_object_pos": [round(float(v), 5) for v in obj[-1]],
        "final_xy_dist_to_target_m": float(np.hypot(obj[-1, 0] - tx, obj[-1, 1] - ty)),
        "final_in_target_region": bool(in_region[-1]),
        "in_region_whole_final_window": bool(in_region[win].all()),
        "final_resting_on_table": bool(abs(obj[-1, 2] - h) <= cfg.resting_z_tol),
        "final_speed_mps": float(np.nanmax(speed[win])) if win.any() else float(speed[-1]),
        "released_at_end": bool(not any_finger[-1]),
        "target_region": {"x": tx, "y": ty, "half_size": tr},
    }


def verify(ep: Episode, cfg: VerifierConfig | None = None) -> Outcome:
    cfg = cfg or VerifierConfig()
    missing = [s for s in REQUIRED_STREAMS if s not in ep.streams or not ep.streams[s].t]
    has_params = bool((ep.replay and ep.replay.env_params) or ep.conditions.get("env_params"))
    base = {"verifier_id": cfg.verifier_id, "verifier_version": cfg.version, "provenance": "amperes_verifier"}
    term = ep.replay.terminated_by if ep.replay else None
    if term in ("sim_instability", "policy_exception", "wallclock_timeout", "invalid_action") and missing:
        return Outcome(label="invalid", verified=True, reason=f"RUN_INVALID:{term}",
                       measurements={"terminated_by": term, "missing_streams": missing}, **base)
    if missing or not has_params:
        return Outcome(label="unknown", verified=False, reason="INSUFFICIENT_STATE_FOR_VERIFICATION",
                       measurements={"missing_streams": missing, "env_params_present": has_params}, **base)
    m = measure(ep, cfg)
    term = m["terminated_by"]
    checks = {
        "grasped_and_lifted": m["lifted"],
        "in_target_region_final_window": m["in_region_whole_final_window"],
        "resting_on_table": m["final_resting_on_table"],
        "settled": m["final_speed_mps"] <= cfg.settle_speed,
        "released": m["released_at_end"],
        "no_prohibited_contact": (m["obstacle_contact_steps"] == 0) or not cfg.forbid_obstacle_contact,
    }
    m["checks"] = checks
    if term in ("sim_instability", "policy_exception", "wallclock_timeout") or term == "invalid_action":
        return Outcome(label="invalid", verified=True, reason=f"RUN_INVALID:{term}", measurements=m, **base)
    if term == "max_steps":
        return Outcome(label="timeout", verified=True, reason="STEP_BUDGET_EXHAUSTED", measurements=m, **base)
    if not checks["no_prohibited_contact"]:
        return Outcome(label="failure", verified=True, reason="PROHIBITED_CONTACT", measurements=m, **base)
    if all(checks.values()):
        return Outcome(label="success", verified=True, reason="ALL_CHECKS_PASSED", measurements=m, **base)
    first_failed = next(k for k, v in checks.items() if not v)
    return Outcome(label="failure", verified=True, reason=f"CHECK_FAILED:{first_failed}", measurements=m, **base)
