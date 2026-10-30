"""Episode execution, recording (canonical schema), verification and bounded parallel batch runs."""

from __future__ import annotations

import hashlib
import os
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from amperes.provenance import environment
from amperes.schema import Episode, Event, Ref, ReplayInfo, SourceInfo, Stream, write_episode
from amperes.sim.env import ACTION_SPEC, CONTROL_DT, MAX_GRIP_FORCE, EnvParams, PickPlaceEnv
from amperes.sim.policies import ActionReplayPolicy, make_policy
from amperes.sim.taxonomy import classify
from amperes.sim.verifier import VerifierConfig, verify

MAX_WORKERS = max(1, min(4, (os.cpu_count() or 2) - 1))
DEFAULT_WALLCLOCK_S = 30.0


def _r(v) -> list[float]:
    return [round(float(x), 6) for x in np.asarray(v).reshape(-1)]


def state_digest(env: PickPlaceEnv) -> str:
    return hashlib.sha256(env.data.qpos.tobytes() + env.data.qvel.tobytes()).hexdigest()[:16]


def run_episode(spec: dict[str, Any]) -> dict[str, Any]:
    """Run one episode. Never raises: harness errors become `invalid` episodes with the error recorded."""
    cond = spec["condition"]
    params = EnvParams(**cond["env_params"])
    seed = int(cond["seed"])
    vcfg = VerifierConfig(**spec.get("verifier_config", {}))
    wall_limit = float(spec.get("wallclock_timeout_s", DEFAULT_WALLCLOCK_S))
    rec: dict[str, list] = {k: [] for k in ("t", "tcp", "cmd", "gap", "est", "obj", "quat", "fq", "con", "act")}
    events: list[Event] = []
    error = None
    terminated_by = "policy_done"
    t0 = time.perf_counter()
    env = None
    policy = None
    try:
        env = PickPlaceEnv(params, seed)
        if spec.get("replay_actions") is not None:
            policy = ActionReplayPolicy(spec["replay_actions"], spec.get("replay_source", "?"), spec.get("replay_done_at"))
        else:
            policy = make_policy(spec["policy_config"])
        obs = env.reset()
        init_digest = state_digest(env)
        policy.reset()

        def record(o):
            gt = env.ground_truth()
            rec["t"].append(round(env.step_count * CONTROL_DT, 6))
            rec["tcp"].append(_r(o["tcp_pos"]))
            rec["cmd"].append(_r(o["tcp_cmd"]))
            rec["gap"].append(_r(o["finger_gap"]))
            rec["est"].append(_r(o["object_pos_est"]))
            rec["obj"].append(_r(gt["object_pos"]))
            rec["quat"].append(_r(gt["object_quat"]))
            rec["fq"].append(_r(gt["finger_q"]))
            rec["con"].append([gt["contacts"]])

        done = False
        while env.step_count < params.max_steps:
            record(obs)
            action, done = policy.act(obs)
            a = np.asarray(action, dtype=float).reshape(-1)
            # Actions are stored at full float64 precision: rounding them breaks bitwise open-loop replay
            # (contact dynamics amplify 1e-7 differences), which was observed in an early run of this harness.
            rec["act"].append([float(x) for x in a] if a.shape == (4,) else [float("nan")] * 4)
            obs, info = env.step(a)
            if info["invalid_action"]:
                terminated_by = "invalid_action"
                break
            if info["unstable"]:
                terminated_by = "sim_instability"
                break
            if time.perf_counter() - t0 > wall_limit:
                terminated_by = "wallclock_timeout"
                break
            if done:
                break
        if not done and terminated_by == "policy_done":
            terminated_by = "max_steps"
        events.extend(Event(t=e["t"], type="policy_phase", value=e["value"], code=e.get("note"), source="policy")
                      for e in policy.annotations())
        # Settle period: environment-commanded hold with gripper open, so the verifier can check stability.
        events.append(Event(t=round(env.step_count * CONTROL_DT, 6), type="settle_start", source="environment"))
        if terminated_by in ("policy_done", "max_steps"):
            for _ in range(params.settle_steps):
                record(obs)
                obs, info = env.step([0.0, 0.0, 0.0, -8.0 / MAX_GRIP_FORCE])
                if info["unstable"]:
                    terminated_by = "sim_instability"
                    break
        record(obs)
    except Exception as exc:  # harness/policy failure is evidence, not a crash
        terminated_by = "policy_exception"
        error = f"{type(exc).__name__}: {exc}"
        events.append(Event(t=None, type="error", code="POLICY_EXCEPTION", message=error[:500], source="harness"))
        init_digest = None
        events.append(Event(t=None, type="annotation", code="TRACEBACK", message=traceback.format_exc(limit=3)[-1500:],
                            source="harness"))
    wall = time.perf_counter() - t0

    n_act = len(rec["act"])
    t = rec["t"]
    streams = {}
    if t:
        streams = {
            "obs.tcp_pos": Stream(kind="observation", t=t, values=rec["tcp"], units="m", expected_rate_hz=50.0, valid_range=[-0.35, 0.4]),
            "obs.tcp_cmd": Stream(kind="observation", t=t, values=rec["cmd"], units="m", expected_rate_hz=50.0, valid_range=[-0.35, 0.4]),
            # Soft joint limits let fingers overshoot ~0.5 mm past fully open (0.1004 m) and interpenetrate up to ~7 mm
            # when closing on nothing; the declared range covers that physical behaviour (an earlier [0, 0.1]
            # declaration produced spurious out-of-range findings).
            "obs.finger_gap": Stream(kind="observation", t=t, values=rec["gap"], units="m", expected_rate_hz=50.0, valid_range=[-0.01, 0.11]),
            "obs.object_pos_est": Stream(kind="observation", t=t, values=rec["est"], units="m", expected_rate_hz=50.0),
            "act.command": Stream(kind="action", t=t[:n_act], values=rec["act"], units="normalized", expected_rate_hz=50.0, valid_range=[-1.0, 1.0]),
            "state.object_pos": Stream(kind="state", t=t, values=rec["obj"], units="m", expected_rate_hz=50.0, source="simulator ground truth"),
            "state.object_quat": Stream(kind="state", t=t, values=rec["quat"], units="quat wxyz", expected_rate_hz=50.0, source="simulator ground truth"),
            "state.tcp_pos": Stream(kind="state", t=t, values=rec["tcp"], units="m", expected_rate_hz=50.0, source="simulator ground truth"),
            "state.finger_q": Stream(kind="state", t=t, values=rec["fq"], units="m", expected_rate_hz=50.0, source="simulator ground truth"),
            "state.contacts": Stream(kind="state", t=t, values=rec["con"], units="bitmask", expected_rate_hz=50.0, source="simulator ground truth"),
        }
    meta = env.metadata() if env is not None else {}
    pol_id = getattr(policy, "policy_id", spec.get("policy_config", {}).get("policy_id"))
    pol_ver = getattr(policy, "policy_version", None)
    ep = Episode(
        episode_id=spec["episode_id"],
        source=SourceInfo(dataset_id=spec["run_id"], adapter="amperes.sim", synthetic=True,
                          synthetic_note="MuJoCo simulation; not physical-robot data"),
        task=Ref(id=meta.get("task_id"), version=meta.get("task_version")),
        policy=Ref(id=pol_id, version=pol_ver),
        robot=None,
        session=None,
        start_time=None,
        end_time=None,
        time_base="episode_relative_s",
        streams=streams,
        action_spec=ACTION_SPEC,
        events=events,
        conditions={k: cond.get(k) for k in ("condition_id", "parent_id", "split", "changed", "description")},
        replay=ReplayInfo(
            replay_class="A" if init_digest else "E",
            sim_task_id=meta.get("task_id"), sim_task_version=meta.get("task_version"),
            simulator_version=meta.get("simulator_version"), env_params=params.model_dump(), seed=seed,
            policy_config=policy.config_dict() if policy is not None else spec.get("policy_config"),
            initial_state_digest=init_digest, terminated_by=terminated_by,
        ),
        extra={"wall_time_s": round(wall, 4), "error": error, "environment": environment(),
               "replay_of": spec.get("replay_source")},
    )
    outcome = verify(ep, vcfg)
    ep.outcome = outcome
    ep.diagnosis = classify(ep, outcome)
    path = Path(spec["artifacts_dir"]) / spec["run_id"] / "episodes" / f"{spec['episode_id']}.json.gz"
    write_episode(ep, path)
    m = outcome.measurements
    return {
        "episode_id": ep.episode_id,
        "run_id": spec["run_id"],
        "condition_id": cond["condition_id"],
        "split": cond.get("split"),
        "seed": seed,
        "policy_id": pol_id,
        "policy_version": pol_ver,
        "outcome": outcome.label,
        "reason": outcome.reason,
        "category": ep.diagnosis.category if ep.diagnosis else None,
        "rule_id": ep.diagnosis.rule_id if ep.diagnosis else None,
        "steps": n_act,
        "sim_duration_s": round(n_act * CONTROL_DT, 4),
        "wall_time_s": round(wall, 4),
        "terminated_by": terminated_by,
        "collision": bool(m.get("obstacle_contact_steps", 0) > 0),
        "lifted": m.get("lifted"),
        "final_xy_dist_to_target_m": m.get("final_xy_dist_to_target_m"),
        "initial_state_digest": init_digest,
        "artifact": str(path),
        "error": error,
    }


def run_batch(specs: list[dict[str, Any]], workers: int | None = None) -> list[dict[str, Any]]:
    """Run episodes with a bounded process pool. Order of results matches order of specs."""
    workers = max(1, min(workers or MAX_WORKERS, MAX_WORKERS))
    if workers == 1 or len(specs) < 8:
        return [run_episode(s) for s in specs]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(run_episode, specs, chunksize=8))
