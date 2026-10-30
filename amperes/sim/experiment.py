"""End-to-end failure → reproduction → intervention → held-out evaluation → regression experiment."""

from __future__ import annotations

import traceback
from typing import Any

from amperes import store
from amperes.provenance import environment, new_id, now_iso
from amperes.regression import create_suite, make_case, run_suite
from amperes.sim import variations as V
from amperes.sim.interventions import CATALOG, EXCLUDED, SELECTION_RULE, rank_candidates, target_category
from amperes.sim.policies import ScriptedConfig
from amperes.sim.reproduce import reproduce, reproduction_rates
from amperes.sim.runner import run_batch
from amperes.sim.verifier import VerifierConfig
from amperes.stats import outcome_summary, paired_comparison


def run_policy(exp_id: str, kind: str, cfg: ScriptedConfig, conds: list[dict[str, Any]], label: str,
               workers: int | None = None) -> dict[str, Any]:
    run_id = new_id(f"run_{kind}")
    store.insert("runs", {"run_id": run_id, "experiment_id": exp_id, "kind": kind, "policy_label": label,
                          "policy_config": cfg.model_dump(), "verifier_config": VerifierConfig().model_dump(),
                          "split": conds[0]["split"] if conds else None, "n_episodes": len(conds),
                          "status": "running", "started_at": now_iso(), "environment": environment()})
    specs = [{"run_id": run_id, "episode_id": f"{run_id}-{i:04d}", "condition": c, "policy_config": cfg.model_dump(),
              "artifacts_dir": str(store.artifacts_dir() / "runs")} for i, c in enumerate(conds)]
    try:
        results = run_batch(specs, workers)
    except Exception as exc:
        store.execute("UPDATE runs SET status='failed', error=?, finished_at=? WHERE run_id=?",
                      [f"{type(exc).__name__}: {exc}", now_iso(), run_id])
        raise
    store.insert("datasets", {"dataset_id": run_id, "name": f"{kind} / {label}", "kind": "sim_run",
                              "adapter": "amperes.sim", "created_at": now_iso(), "synthetic": 1,
                              "n_records": len(results), "status": "completed",
                              "note": "MuJoCo simulation output (synthetic)"})
    store.record_run_episodes(results, conds, label, dataset_id=run_id)
    store.execute("UPDATE runs SET status='completed', finished_at=?, wall_s=? WHERE run_id=?",
                  [now_iso(), round(sum(r["wall_time_s"] for r in results), 3), run_id])
    return {"run_id": run_id, "label": label, "results": results, "summary": outcome_summary(results)}


def _sweep_analysis(neigh_conds, neigh_results, rep_category) -> dict[str, Any]:
    sweeps: dict[str, list] = {}
    jitter = []
    for c, r in zip(neigh_conds, neigh_results):
        if c["description"].startswith("sweep"):
            (k, v), = c["changed"].items()
            sweeps.setdefault(k, []).append({"value": v, "outcome": r["outcome"], "category": r["category"],
                                             "episode_id": r["episode_id"]})
        else:
            jitter.append({"changed": c["changed"], "outcome": r["outcome"], "category": r["category"]})
    out = {}
    for k, pts in sweeps.items():
        pts.sort(key=lambda p: p["value"])
        transitions = [{"between": [a["value"], b["value"]], "from": a["category"] or a["outcome"],
                        "to": b["category"] or b["outcome"]}
                       for a, b in zip(pts, pts[1:]) if (a["category"] or a["outcome"]) != (b["category"] or b["outcome"])]
        out[k] = {"points": pts, "same_category_fraction": sum(p["category"] == rep_category for p in pts) / len(pts),
                  "observed_transitions": transitions}
    n_j = len(jitter)
    return {"sweeps": out, "jitter": {"n": n_j, "same_category": sum(j["category"] == rep_category for j in jitter),
                                       "points": jitter},
            "note": "Transitions are observed outcome changes between adjacent sweep values in simulation; "
                    "they bound, but do not establish, a causal threshold."}


def run_experiment(n_dev: int = 300, n_heldout: int = 600, nominal_repeats: int = 5, max_repro: int = 40,
                   max_guards: int = 60, workers: int | None = None, log=print) -> dict[str, Any]:
    exp_id = new_id("exp")
    config = {"n_dev": n_dev, "n_heldout": n_heldout, "nominal_repeats": nominal_repeats, "max_repro": max_repro,
              "max_guards": max_guards, "selection_rule": SELECTION_RULE, "baseline": ScriptedConfig().model_dump(),
              "verifier": VerifierConfig().model_dump(), "dev_master_seed": V.DEV_MASTER_SEED,
              "heldout_master_seed": V.HELDOUT_MASTER_SEED, "perturbation_space": V.PERTURBATION_SPACE,
              "primary_metric": "verified success rate on held-out perturbed conditions (paired, same conditions)"}
    store.insert("experiments", {"experiment_id": exp_id, "name": "pick_place failure-to-verified-improvement",
                                 "config": config, "created_at": now_iso(), "status": "running"})
    try:
        summary = _run(exp_id, config, workers, log)
        store.execute("UPDATE experiments SET status='completed', summary=? WHERE experiment_id=?",
                      [store._enc(summary), exp_id])
        return summary
    except Exception as exc:
        store.execute("UPDATE experiments SET status='failed', error=? WHERE experiment_id=?",
                      [f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}", exp_id])
        raise


def _run(exp_id: str, config: dict[str, Any], workers, log) -> dict[str, Any]:
    base = ScriptedConfig()
    base_label = "baseline:" + base.label()
    nominal = V.nominal(config["nominal_repeats"])
    dev = V.dev_set(config["n_dev"])
    held = V.heldout_set(config["n_heldout"])
    V.assert_disjoint(dev, held)

    log(f"[1/8] baseline: {len(nominal)} nominal + {len(dev)} dev episodes")
    b_nom = run_policy(exp_id, "baseline_nominal", base, nominal, base_label, workers)
    b_dev = run_policy(exp_id, "baseline_dev", base, dev, base_label, workers)

    log("[2/8] identifying failures")
    target, counts = target_category(b_dev["results"])
    dev_fail = [r for r in b_dev["results"] if r["outcome"] != "success"]
    rep = None
    if target:
        rep = sorted((r for r in dev_fail if r["category"] == target), key=lambda r: r["condition_id"])[0]
    rep_cond = next(c for c in dev if rep and c["condition_id"] == rep["condition_id"]) if rep else None

    log(f"[3/8] reproducing up to {config['max_repro']} dev failures (target category: {target})")
    eligible = sorted([r for r in dev_fail if r["category"] not in EXCLUDED], key=lambda r: r["condition_id"])
    if rep:
        eligible = [rep] + [r for r in eligible if r["episode_id"] != rep["episode_id"]]
    repro_records = []
    for r in eligible[: config["max_repro"]]:
        repro_records += reproduce(r["artifact"], experiment_id=exp_id)
    rates = reproduction_rates(repro_records)
    rep_repro = [x for x in repro_records if rep and x["original_episode_id"] == rep["episode_id"]]

    log("[4/8] controlled variations around the representative failure")
    neigh = {}
    if rep_cond:
        nconds = V.neighborhood(rep_cond)
        b_neigh = run_policy(exp_id, "baseline_neighborhood", base, nconds, base_label, workers)
        neigh = _sweep_analysis(nconds, b_neigh["results"], target)
        neigh["run_id"] = b_neigh["run_id"]
        neigh["n"] = len(nconds)

    log("[5/8] evaluating candidate interventions on dev only")
    evals = []
    for cand in CATALOG.get(target, []):
        cfg = cand.config(base)
        r = run_policy(exp_id, "candidate_dev", cfg, dev, f"{cand.name}:{cfg.label()}", workers)
        cmp = paired_comparison(b_dev["results"], r["results"])
        evals.append({"name": cand.name, "delta": cand.delta, "rationale": cand.rationale,
                      "mechanism": cand.mechanism, "cfg": cfg, "run_id": r["run_id"], "comparison": cmp})
    ranked = rank_candidates(evals)
    selected = ranked[0] if ranked else None
    alternative = ranked[1] if len(ranked) > 1 else None
    frozen_at = now_iso()
    for i, e in enumerate(ranked):
        store.insert("interventions", {
            "intervention_id": new_id("iv"), "experiment_id": exp_id, "name": e["name"], "target_category": target,
            "delta": e["delta"], "rationale": e["rationale"], "policy_label": f"{e['name']}:{e['cfg'].label()}",
            "dev_run_id": e["run_id"], "dev_summary": e["comparison"], "selected": int(i == 0),
            "role": "selected" if i == 0 else "alternative" if i == 1 else "rejected", "frozen_at": frozen_at})

    log("[6/8] held-out evaluation (selection frozen at %s)" % frozen_at)
    arms = [("baseline", base)]
    if selected:
        arms.append((f"selected:{selected['name']}", selected["cfg"]))
    if alternative:
        arms.append((f"alternative:{alternative['name']}", alternative["cfg"]))
    control_cfg = base.model_copy(update={"version": "1.0.0-control"})
    arms.append(("control:unchanged_config", control_cfg))
    held_runs, nom_runs = {}, {}
    for name, cfg in arms:
        held_runs[name] = run_policy(exp_id, "heldout_eval", cfg, held, f"{name}:{cfg.label()}", workers)
        nom_runs[name] = b_nom if name == "baseline" else run_policy(exp_id, "nominal_eval", cfg, nominal,
                                                                    f"{name}:{cfg.label()}", workers)
    comparisons = {}
    for name, _ in arms[1:]:
        cmp_h = paired_comparison(held_runs["baseline"]["results"], held_runs[name]["results"])
        cmp_n = paired_comparison(nom_runs["baseline"]["results"], nom_runs[name]["results"])
        comparisons[name] = {"heldout": cmp_h, "nominal": cmp_n}
        for split, cmp, run in (("heldout", cmp_h, held_runs[name]), ("nominal", cmp_n, nom_runs[name])):
            store.insert("comparisons", {"comparison_id": new_id("cmp"), "experiment_id": exp_id, "split": split,
                                         "baseline_run": (held_runs if split == "heldout" else nom_runs)["baseline"]["run_id"],
                                         "treatment_run": run["run_id"], "treatment_name": name, "summary": cmp,
                                         "created_at": now_iso()})

    log("[7/8] regression suite")
    suite_id = new_id("suite")
    cases = []
    exact_ids = {x["original_episode_id"] for x in repro_records if x["mode"] == "rerun" and x["classification"] == "exact"}
    dev_by_cond = {c["condition_id"]: c for c in dev}
    for r in b_dev["results"]:
        if r["episode_id"] in exact_ids:
            cases.append(make_case(suite_id, "known_failure", dev_by_cond[r["condition_id"]], {**r, "policy_label": base_label}, "dev"))
    guards = sorted([r for r in b_dev["results"] if r["outcome"] == "success"], key=lambda r: r["condition_id"])
    for r in guards[: config["max_guards"]]:
        cases.append(make_case(suite_id, "guard", dev_by_cond[r["condition_id"]], {**r, "policy_label": base_label}, "dev"))
    cases.append(make_case(suite_id, "nominal", nominal[0], {**b_nom["results"][0], "policy_label": base_label}, "nominal"))
    create_suite(exp_id, "pick_place dev regression suite", cases,
                 note="Built from dev-set evidence; passing it is not independent of selection.")
    regression = {name: run_suite(suite_id, cfg, f"{name}:{cfg.label()}", workers) for name, cfg in arms}

    log("[8/8] assembling evidence")
    summary = {
        "experiment_id": exp_id,
        "created_at": now_iso(),
        "environment": environment(),
        "config": {k: v for k, v in config.items() if k != "perturbation_space"},
        "perturbation_space": {k: list(v) for k, v in config["perturbation_space"].items()},
        "baseline_label": base_label,
        "runs": {"baseline_nominal": b_nom["run_id"], "baseline_dev": b_dev["run_id"],
                 "heldout": {k: v["run_id"] for k, v in held_runs.items()},
                 "nominal": {k: v["run_id"] for k, v in nom_runs.items()}},
        "step1_baseline": {"nominal": b_nom["summary"], "dev": b_dev["summary"],
                           "nominal_identical_initial_state": len({r["initial_state_digest"] for r in b_nom["results"]}) == 1},
        "step2_failures": {"dev_failure_counts": counts, "target_category": target,
                           "representative": rep and {k: rep[k] for k in ("episode_id", "condition_id", "outcome",
                                                                           "category", "rule_id", "artifact")},
                           "representative_condition": rep_cond},
        "step3_reproduction": {"rates": rates, "representative_attempts": rep_repro,
                               "n_attempted_episodes": len(eligible[: config["max_repro"]])},
        "step4_variations": neigh,
        "step5_intervention": {
            "selection_rule": SELECTION_RULE, "frozen_at": frozen_at,
            "candidates": [{k: e[k] for k in ("name", "delta", "rationale", "mechanism", "run_id")} |
                           {"dev": {"successes": e["comparison"]["treatment"]["successes"],
                                    "n": e["comparison"]["n_pairs"],
                                    "fixed": e["comparison"]["fixed_by_treatment"],
                                    "broken": e["comparison"]["broken_by_treatment"],
                                    "mean_duration_all_s": e["comparison"]["treatment"]["mean_duration_all_s"]}}
                           for e in ranked],
            "selected": selected and selected["name"], "alternative": alternative and alternative["name"]},
        "step6_comparison": comparisons,
        "step6_heldout_summaries": {k: v["summary"] for k, v in held_runs.items()},
        "step7_regression": {"suite_id": suite_id, "n_cases": len(cases), "results": regression},
    }
    return summary
