import json
import math
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from amperes import store
from amperes.regression import create_suite, make_case, run_suite
from amperes.schema import Episode, Outcome, ReplayInfo, SourceInfo, Stream, read_episode, write_episode
from amperes.sim import variations as V
from amperes.sim.env import C_LEFT_OBJECT, C_OBJECT_OBSTACLE, C_RIGHT_OBJECT, EnvParams, PickPlaceEnv
from amperes.sim.policies import ScriptedConfig
from amperes.sim.reproduce import reproduce
from amperes.sim.runner import run_episode, state_digest
from amperes.sim.taxonomy import classify
from amperes.sim.verifier import verify
from amperes.stats import mcnemar_exact, paired_comparison, wilson


def spec(run_dir, cond, cfg=None, **kw):
    s = {"run_id": "t", "episode_id": f"t-{cond['condition_id']}-{len(kw)}", "condition": cond,
         "policy_config": (cfg or ScriptedConfig()).model_dump(), "artifacts_dir": str(run_dir)}
    s.update(kw)
    return s


def cond(**changed):
    return V.make_condition(changed, seed=3, split="test", parent_id=None, description="test")


# 1. environment reset/step
def test_env_reset_and_step():
    env = PickPlaceEnv(EnvParams(), seed=0)
    obs = env.reset()
    assert set(obs) >= {"tcp_pos", "finger_gap", "object_pos_est", "target_pos"}
    assert np.allclose(obs["object_pos_est"], env.object_pos())  # zero noise at nominal
    obs2, info = env.step([0.5, 0, 0, 0])
    assert env.step_count == 1 and not info["unstable"]
    assert obs2["tcp_cmd"][0] > obs["tcp_cmd"][0]


# 2. reproducibility with fixed seeds
def test_fixed_seed_is_bitwise_reproducible(run_dir):
    c = cond(obs_noise_std=0.01, object_mass=0.5)
    a = run_episode(spec(run_dir, c, episode_id="a"))
    b = run_episode(spec(run_dir, c, episode_id="b"))
    ea, eb = read_episode(Path(a["artifact"])), read_episode(Path(b["artifact"]))
    assert ea.streams["state.object_pos"].values == eb.streams["state.object_pos"].values
    assert a["initial_state_digest"] == b["initial_state_digest"]


# 3. policy action validation
def test_invalid_action_is_recorded_as_invalid(run_dir):
    r = run_episode(spec(run_dir, cond(), replay_actions=[[0, 0, 0, 0], [float("nan"), 0, 0, 0]], episode_id="nan"))
    assert r["terminated_by"] == "invalid_action"
    assert r["outcome"] == "invalid" and r["category"] == "INVALID_ACTION"


def test_out_of_range_action_is_clipped_and_flagged():
    env = PickPlaceEnv(EnvParams(), 0)
    env.reset()
    _, info = env.step([5.0, 0, 0, 0])
    assert info["clipped"] and not info["invalid_action"]


# 4. verifier on constructed episodes
def _constructed(final_xy, lifted=True, released=True, obstacle=False):
    p = EnvParams().model_dump()
    t = [i * 0.02 for i in range(60)]
    z = [0.0205] * 10 + [0.12 if lifted else 0.0205] * 20 + [0.0205] * 30
    xy = [(0.0, -0.12)] * 30 + [final_xy] * 30
    both = C_LEFT_OBJECT | C_RIGHT_OBJECT
    con = [[0]] * 10 + [[both]] * 25 + [[both if not released else 0]] * 25
    if obstacle:
        con[20] = [both | C_OBJECT_OBSTACLE]
    return Episode(
        episode_id="constructed", source=SourceInfo(dataset_id="t", adapter="test", synthetic=True),
        streams={"state.object_pos": Stream(kind="state", t=t, values=[[x, y, zz] for (x, y), zz in zip(xy, z)]),
                 "state.contacts": Stream(kind="state", t=t, values=con),
                 "state.tcp_pos": Stream(kind="state", t=t, values=[[x, y, zz] for (x, y), zz in zip(xy, z)])},
        replay=ReplayInfo(env_params=p, seed=0, sim_task_id="pick_place_box", terminated_by="policy_done"))


def test_verifier_success_and_failures():
    assert verify(_constructed((0.0, 0.14))).label == "success"
    out = verify(_constructed((0.0, 0.0)))
    assert out.label == "failure" and out.reason == "CHECK_FAILED:in_target_region_final_window"
    assert verify(_constructed((0.0, 0.14), lifted=False)).reason == "CHECK_FAILED:grasped_and_lifted"
    assert verify(_constructed((0.0, 0.14), released=False)).reason == "CHECK_FAILED:released"
    assert verify(_constructed((0.0, 0.14), obstacle=True)).reason == "PROHIBITED_CONTACT"


def test_verifier_refuses_without_state():
    ep = _constructed((0, 0.14))
    del ep.streams["state.contacts"]
    out = verify(ep)
    assert out.label == "unknown" and not out.verified


def test_verifier_ignores_policy_claims():
    ep = _constructed((0.0, 0.0))
    ep.outcome = Outcome(label="success", provenance="source_label")  # a claim, not evidence
    assert verify(ep).label == "failure"


# 5. timeout and invalid-run handling
def test_timeout_and_policy_exception(run_dir):
    r = run_episode(spec(run_dir, cond(max_steps=60), episode_id="to"))
    assert r["outcome"] == "timeout" and r["category"] == "TIMEOUT"
    bad = spec(run_dir, cond(), episode_id="exc")
    bad["policy_config"] = {"policy_id": "not_registered"}
    r = run_episode(bad)
    assert r["outcome"] == "invalid" and r["category"] == "INFRASTRUCTURE_ERROR" and "unknown policy_id" in r["error"]


# 6. serialization round trip (including non-finite values)
def test_episode_roundtrip_with_nan(tmp_path):
    ep = _constructed((0, 0.14))
    ep.streams["state.tcp_pos"].values[3] = [float("nan"), None, float("inf")]
    write_episode(ep, tmp_path / "e.json.gz")
    back = read_episode(tmp_path / "e.json.gz")
    v = back.streams["state.tcp_pos"].values[3]
    assert math.isnan(v[0]) and v[1] is None and math.isinf(v[2])
    assert back.content_digest() == ep.content_digest()


# 7. failure classification on real physics
@pytest.mark.parametrize("changed,expected", [({"obstacle_height": 0.14}, "COLLISION"),
                                              ({"object_mass": 0.8}, "SLIPPAGE")])
def test_classification(run_dir, changed, expected):
    r = run_episode(spec(run_dir, cond(**changed), episode_id=expected))
    assert r["category"] == expected
    ep = read_episode(Path(r["artifact"]))
    assert ep.diagnosis.observed_evidence and ep.diagnosis.causal_note


# 8. configuration validation
def test_config_validation():
    with pytest.raises(ValidationError):
        EnvParams(object_mass=50)
    with pytest.raises(ValidationError):
        ScriptedConfig(unknown_field=1)
    with pytest.raises(ValidationError):
        V.make_condition({"object_friction": 9.0}, 0, "dev", None, "bad")


def test_heldout_disjoint_from_dev():
    V.assert_disjoint(V.dev_set(50), V.heldout_set(50))
    with pytest.raises(RuntimeError):
        V.assert_disjoint(V.dev_set(5), V.dev_set(5))


# 9. run persistence
def test_run_persistence(run_dir):
    c = cond()
    r = run_episode(spec(run_dir, c, episode_id="persist"))
    store.record_run_episodes([r], [c], "baseline", dataset_id="t")
    rows = store.query("SELECT outcome, verified, replay_class FROM episodes WHERE episode_id='persist'")
    assert rows == [{"outcome": "success", "verified": 1, "replay_class": "A"}]


# 10. comparison statistics
def test_stats():
    assert mcnemar_exact(0, 7) == pytest.approx(2 * 0.5**7)
    assert mcnemar_exact(0, 0) is None
    lo, hi = wilson(0, 10)
    assert lo == 0 and 0.2 < hi < 0.35
    base = [{"condition_id": str(i), "outcome": "success" if i < 5 else "failure", "sim_duration_s": 1, "wall_time_s": 0}
            for i in range(10)]
    treat = [dict(b, outcome="success") for b in base]
    c = paired_comparison(base, treat)
    assert c["fixed_by_treatment"] == 5 and c["broken_by_treatment"] == 0 and c["success_rate_diff"] == 0.5
    with pytest.raises(ValueError):
        paired_comparison(base, treat[:3])


# 11. regression case creation and replay; reproduction of a failure
def test_reproduction_and_regression(run_dir):
    c = cond(obstacle_height=0.14)
    r = run_episode(spec(run_dir, c, episode_id="fail"))
    recs = reproduce(r["artifact"], persist=False)
    assert {x["mode"]: x["classification"] for x in recs} == {"rerun": "exact", "action_replay": "exact"}
    suite = "suite_test"
    case = make_case(suite, "known_failure", c, {**r, "policy_label": "baseline"}, "dev")
    create_suite(None, "t", [case])
    base = run_suite(suite, ScriptedConfig(), "baseline")
    fixed = run_suite(suite, ScriptedConfig(obstacle_aware=True), "obstacle_aware")
    assert base["status_counts"]["still_failing"] == 1
    assert fixed["status_counts"]["fixed"] == 1


def test_successful_episode_is_not_eligible_for_reproduction(run_dir):
    r = run_episode(spec(run_dir, cond(), episode_id="ok"))
    rec = reproduce(r["artifact"], persist=False)
    assert rec[0]["classification"] == "ineligible"


# 12. exported report consistency with underlying records
def test_report_consistency(amperes_home):
    from amperes.report import write_report
    from amperes.sim.experiment import run_experiment

    s = run_experiment(n_dev=30, n_heldout=30, nominal_repeats=2, max_repro=3, max_guards=5, workers=1, log=lambda *_: None)
    out = write_report(s)
    summ = json.loads((out / "summary.json").read_text())
    rid = summ["runs"]["heldout"]["baseline"]
    db = store.query("SELECT COUNT(*) n, SUM(outcome='success') s FROM episodes WHERE run_id=?", [rid])[0]
    assert db["n"] == summ["step6_heldout_summaries"]["baseline"]["n"] == 30
    assert db["s"] == summ["step6_heldout_summaries"]["baseline"]["successes"]
    assert "simulation" in (out / "report.md").read_text().lower()
    ctrl = summ["step6_comparison"]["control:unchanged_config"]["heldout"]
    assert ctrl["fixed_by_treatment"] == ctrl["broken_by_treatment"] == 0  # harness adds no spurious differences
