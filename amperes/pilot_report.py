"""Pilot readiness report: one exportable JSON + Markdown document per dataset (or set of datasets).

Every section declares its evidence class:
  MEASURED      computed from the imported/generated records
  REVIEWED      human reviewer decisions recorded in Amperes
  ASSOCIATION   statistical association, not causation
  HYPOTHESIS    template or model-generated possible cause, unverified
  ASSUMPTION    user-supplied input (e.g. costs)
  MISSING       data needed for a conclusion that is not present
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from amperes import incidents as inc_mod
from amperes import store
from amperes.economics import estimate
from amperes.ops import bottlenecks, correlate, ops_metrics
from amperes.provenance import environment, now_iso
from amperes.quality import RULESET_VERSION
from amperes.review import review_report, study_report


def _missing(metrics: dict[str, Any], prefix="") -> list[str]:
    out = []
    for k, v in metrics.items():
        if isinstance(v, dict) and v.get("status") == "not_computable":
            out.append(f"{prefix}{k}: {v['reason']}")
        elif isinstance(v, dict):
            out += _missing(v, f"{prefix}{k}.")
    return out


ANALYSIS_DEFAULTS = {"usable_statuses": ["verified_success", "valid_success_unverified", "valid_failed_task"],
                     "rejected_tolerance": 0.05, "min_session_episodes": 10}


def build(dataset_ids: list[str], assumptions: dict[str, Any] | None = None, study_id: str | None = None,
          experiment_id: str | None = None, include_latest_simulation: bool = False,
          analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    """Simulator results are included only when an experiment id is given or explicitly requested: a report about a
    customer dataset must not silently carry unrelated simulation evidence."""
    unknown = set(analysis or {}) - set(ANALYSIS_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown analysis settings {sorted(unknown)}; allowed: {sorted(ANALYSIS_DEFAULTS)}")
    cfg = {**ANALYSIS_DEFAULTS, **(analysis or {})}
    ds = store.query("SELECT * FROM datasets WHERE dataset_id IN (%s)" % ",".join("?" * len(dataset_ids)), dataset_ids)
    if len(ds) != len(dataset_ids):
        raise ValueError("unknown dataset id(s)")
    for d in ds:
        d["import_report"] = store.loads(d["import_report"])
    metrics = ops_metrics(dataset_ids, usable_statuses=tuple(cfg["usable_statuses"]))
    incs = []
    for d in dataset_ids:
        incs += [inc_mod.get(r["incident_id"]) for r in store.query("SELECT incident_id FROM incidents WHERE dataset_id=? "
                                                                     "ORDER BY n_occurrences DESC", [d])]
    reviews = {d: review_report(d) for d in dataset_ids}
    study = study_report(study_id) if study_id else None
    exp = None
    q = "SELECT experiment_id, summary FROM experiments WHERE status='completed' " + ("AND experiment_id=? " if experiment_id else "") + \
        "ORDER BY created_at DESC LIMIT 1"
    rows = store.query(q, [experiment_id] if experiment_id else []) if (experiment_id or include_latest_simulation) else []
    if rows:
        s = json.loads(rows[0]["summary"])
        exp = {"experiment_id": s["experiment_id"], "reproduction": s["step3_reproduction"]["rates"],
               "selected_intervention": s["step5_intervention"]["selected"],
               "heldout": {k: {"baseline": f"{v['heldout']['baseline']['successes']}/{v['heldout']['n_pairs']}",
                               "treatment": f"{v['heldout']['treatment']['successes']}/{v['heldout']['n_pairs']}",
                               "fixed": v["heldout"]["fixed_by_treatment"], "broken": v["heldout"]["broken_by_treatment"],
                               "diff_ci95": v["heldout"]["success_rate_diff_ci95_paired_bootstrap"],
                               "mcnemar_p": v["heldout"]["mcnemar_exact_p"]} for k, v in s["step6_comparison"].items()},
               "regression": {k: v["status_counts"] for k, v in s["step7_regression"]["results"].items()},
               "scope": "MuJoCo simulation only"}
    measured = {"median_time_saved_per_task_s": study and study.get("median_time_saved_per_task_s"),
                "study_status": study["status"] if study else "no workflow study recorded"}
    econ = estimate(assumptions or {}, measured)
    synthetic = any(d["synthetic"] for d in ds)
    report = {
        "generated_at": now_iso(), "environment": environment(), "ruleset_version": RULESET_VERSION, "analysis_settings": cfg,
        "synthetic_data": synthetic,
        "data_notice": "Contains SYNTHETIC fixture or simulation data — not customer or physical-robot evidence." if synthetic else
        "Customer-supplied records processed locally.",
        "sections": {
            "dataset_provenance": {"class": "MEASURED", "datasets": [{k: d[k] for k in ("dataset_id", "name", "kind", "adapter", "source_uri",
                                                                                        "created_at", "synthetic", "n_records", "n_rejected", "note")}
                                                                     for d in ds]},
            "processing_status": {"class": "MEASURED", "imports": [{k: (d["import_report"] or {}).get(k) for k in
                                                                    ("episodes_imported", "records_rejected", "events_imported",
                                                                     "duplicate_events_dropped", "processing_s", "source_unchanged")}
                                                                   for d in ds]},
            "data_quality": {"class": "MEASURED", "by_dataset": [(d["import_report"] or {}).get("quality") for d in ds]},
            "usable_data_yield": {"class": "MEASURED", **metrics["yield"], "data_quality_loss": metrics["data_quality_loss"]},
            "yield_loss_attribution": {"class": "MEASURED (record linkage, not root cause)", **metrics["yield_loss_attribution"]},
            "throughput": {"class": "MEASURED", **metrics["throughput"]},
            "operational_reliability": {"class": "MEASURED", **metrics["reliability"]},
            "incidents": {"class": "MEASURED (facts) + HYPOTHESIS (possible causes)", "n": len(incs),
                          "open": sum(i["status"] in ("open", "investigating", "unresolved") for i in incs),
                          "items": [{k: i[k] for k in ("incident_id", "kind", "title", "severity", "status", "n_occurrences",
                                                       "n_episodes", "n_sessions", "facts", "hypotheses", "grouping_reason",
                                                       "regression_case_ids")} for i in incs[:30]]},
            "recurring_patterns": {"class": "MEASURED (repetition) / ASSOCIATION / HYPOTHESIS", **correlate(dataset_ids)},
            "bottlenecks": {"class": "ASSOCIATION unless marked observed",
                            "findings": bottlenecks(dataset_ids, rejected_tolerance=cfg["rejected_tolerance"],
                                                    min_session_episodes=cfg["min_session_episodes"])},
            "human_review": {"class": "REVIEWED", **{d: r["overall"] for d, r in reviews.items()}},
            "reproduction_and_regression": {"class": "MEASURED (simulation)", "experiment": exp} if exp else
            {"class": "NOT INCLUDED", "reason": "simulator evidence is included only with --experiment or --include-latest-sim"},
            "workflow_time": {"class": ("MEASURED" if study["status"] == "measured" else "MEASURED (PRELIMINARY)"
                                        if study["status"].startswith("preliminary") else "MISSING") if study else "MISSING",
                              "study": study or "no workflow study recorded — time savings are not claimed"},
            "economic_estimate": {"class": "ASSUMPTION", **econ},
        },
    }
    report["missing_data"] = {"class": "MISSING", "items": _missing(metrics)}
    report["limitations"] = [
        "Quality rules only check what the schema declares (ranges, rates, staleness limits); undeclared constraints are not checked.",
        "Bottleneck and correlation findings are associations; none establishes a cause.",
        "Detector precision/recall figures come from a synthetic injected-defect corpus written by the same team as the rules.",
        "Workflow time savings are not claimed unless a counterbalanced study with completed tasks exists.",
        "Simulation results (reproduction, interventions) do not transfer to physical robots without real evaluation.",
    ]
    report["next_steps"] = ["Agree usability criteria and rule thresholds with the customer before evaluating on their data.",
                            "Run a counterbalanced workflow study with at least 10 tasks per condition.",
                            "Have customer engineers review a random sample of findings to estimate precision on their data."]
    return report


def markdown(r: dict[str, Any]) -> str:
    S = r["sections"]
    y = S["usable_data_yield"]["usable_data_yield"]
    L = ["# Pilot readiness report", "", f"> {r['data_notice']}", "", f"Generated {r['generated_at']} · ruleset {r['ruleset_version']} · "
         f"code {r['environment']['code_digest']}", "", "Evidence classes: MEASURED, REVIEWED, ASSOCIATION, HYPOTHESIS, ASSUMPTION, MISSING.", "",
         "## Dataset provenance [MEASURED]", ""]
    for d in S["dataset_provenance"]["datasets"]:
        L.append(f"- `{d['dataset_id']}` {d['name']} — {d['adapter']}, {d['n_records']} records, {d['n_rejected']} rejected, synthetic={bool(d['synthetic'])}")
    L += ["", "## Usable-data yield [MEASURED]", ""]
    if y.get("status") == "computed":
        L.append(f"**{100 * y['value']:.1f}%** = {y['numerator']} usable / {y['denominator']} eligible episodes "
                 f"(rule {y['rule_version']}; usable statuses {y['usable_statuses']}; {y['eligibility']}; "
                 f"95% CI [{100 * y['ci95'][0]:.1f}, {100 * y['ci95'][1]:.1f}]).")
    else:
        L.append(f"Not computable: {y['reason']}")
    L += ["", "Status counts: " + ", ".join(f"{k}={v}" for k, v in S["usable_data_yield"]["status_counts"].items()),
          "", "Data-quality loss (distinct episodes): " + ", ".join(f"{k}={v}" for k, v in S["usable_data_yield"]["data_quality_loss"].items() if k != "unit"),
          ""]
    ya = S["yield_loss_attribution"]
    L += [f"## Yield loss attribution [MEASURED linkage] — {ya['lost_episodes']} of {ya['eligible']} eligible episodes not usable", "",
          "| attributed to | basis | episodes | share of lost | episode-hours | sessions | workcells |", "|---|---|---|---|---|---|---|"]
    for row in ya["rows"]:
        L.append(f"| {row['cause']} | {row['basis']} | {row['episodes']} | {100 * row['share_of_lost']:.0f}% | "
                 f"{'n/a' if row['episode_hours'] is None else f'{row['episode_hours']:.3f}'} | {row['sessions']} | {', '.join(row['workcells'])} |")
    L += ["", f"_{ya['note']}_", "", "## Throughput [MEASURED]", ""]
    for k, v in S["throughput"].items():
        if isinstance(v, dict):
            L.append(f"- {k}: " + (f"{v.get('value'):.4g}" if isinstance(v.get("value"), (int, float)) else str(v.get("value")))
                     + (f" ({v['numerator']}/{v['denominator']} {v.get('unit', '')})" if "numerator" in v else "")
                     + ("" if v.get("status") != "not_computable" else f" — not computable: {v['reason']}"))
    rel = S["operational_reliability"]
    L += ["", "## Operational reliability [MEASURED]", ""]
    av = rel["availability_by_workcell"]
    if isinstance(av, dict) and "status" not in av:
        for wc, v in sorted(av.items()):
            L.append(f"- availability {wc}: " + (f"{100 * v['value']:.2f}% (downtime {v['downtime_h']:.3f} h / {v['scheduled_h']:.2f} h)"
                                                 if v["status"] == "computed" else f"not computable — {v['reason']}"))
    for k in ("incident_frequency", "recovery_wait", "mean_time_to_acknowledge_s", "mean_time_to_resolve_s", "repeat_failure_after_resolution"):
        v = rel[k]
        L.append(f"- {k}: " + (json.dumps({kk: vv for kk, vv in v.items() if kk not in ("definition", "recurred")}, default=str)
                               if v.get("status") == "computed" else f"not computable — {v['reason']}"))
    L += ["", f"## Incidents [MEASURED facts · HYPOTHESIS causes] — {S['incidents']['n']} total, {S['incidents']['open']} open", "",
          "| incident | kind | title | occurrences | sessions | status |", "|---|---|---|---|---|---|"]
    for i in S["incidents"]["items"][:15]:
        L.append(f"| {i['incident_id']} | {i['kind']} | {i['title']} | {i['n_occurrences']} | {i['n_sessions']} | {i['status']} |")
    rp = S["recurring_patterns"]
    L += ["", "## Recurring patterns", "", "**Directly observed repetition [MEASURED]**"]
    L += [f"- {x['event_class']}:{x['code']} — {x['occurrences']} occurrences, {len(x['sessions'])} sessions, workcells {x['workcells']}"
          for x in rp["directly_observed_repetition"][:10]] or ["- none"]
    L += ["", "**Statistical association [ASSOCIATION]**"]
    L += [f"- {x['code']} with {x['config_id']}: sessions {x['sessions_with_code_given_config']} vs other configs "
          f"{x['sessions_with_code_other_configs']} — {x['note']}" for x in rp["statistical_association"]] or ["- none"]
    L += ["", "**Proposed explanations [HYPOTHESIS]**"]
    L += [f"- {x['about']}: {x['hypothesis']} Test: {x['test_to_verify']}" for x in rp["proposed_causal_explanations"]] or ["- none"]
    L += ["", "## Bottleneck findings", ""]
    for b in S["bottlenecks"]["findings"]:
        L.append(f"- **{b['rule']}** [{b['kind'].upper()}] evidence {json.dumps(b['observed_evidence'], default=str)}; "
                 f"baseline {json.dumps(b['comparison_baseline'], default=str)}; n={b['sample_size']}. {b['confidence_and_limitations']} "
                 f"Next: {b['recommended_investigation']}")
    if not S["bottlenecks"]["findings"]:
        L.append("- none met the minimum sample size and comparison rules")
    L += ["", "## Human review [REVIEWED]", "", "Info-level notes (e.g. O001 unverified success) are excluded from review denominators.", ""]
    for d, v in S["human_review"].items():
        if d == "class":
            continue
        p = v["precision_among_reviewed"]
        L.append(f"- {d}: reviewed {v['reviewed']}/{v['findings']} findings; confirmed {v['confirmed']}, false positives {v['false_positives']}, "
                 f"duplicates {v['duplicates']}, unresolved {v['unresolved']}; precision among reviewed: "
                 + (f"{p['value']:.2f} ({p['numerator']}/{p['denominator']})" if isinstance(p["value"], float) else "not evaluated"))
    rr = S["reproduction_and_regression"]
    L += ["", "## Reproduction and regression [MEASURED — simulation]", ""]
    if rr.get("experiment"):
        e = rr["experiment"]
        L.append(f"- Experiment `{e['experiment_id']}`: closed-loop exact reproduction {e['reproduction']['rerun']['exact']}/"
                 f"{e['reproduction']['rerun']['eligible_attempts']}; selected intervention `{e['selected_intervention']}`.")
        for k, v in e["heldout"].items():
            L.append(f"- held-out {k}: {v['baseline']} → {v['treatment']} (fixed {v['fixed']}, broken {v['broken']}, McNemar p={v['mcnemar_p']})")
    else:
        L.append(f"- Not included: {rr['reason']}")
    wt = S["workflow_time"]
    L += ["", f"## Workflow time [{wt['class']}]", "", f"- {wt['study'] if isinstance(wt['study'], str) else wt['study']['status']}",
          "", "## Economic estimate [ASSUMPTION]", "", f"- {S['economic_estimate']['roi_statement']}"]
    for k, v in S["economic_estimate"]["items"].items():
        L.append(f"- {k}: {v['status']}" + (f" — {v['reason']}" if v["status"] != "computed" else f" — {v.get('calculation')}"))
    L += ["", "## Missing data [MISSING]", *[f"- {m}" for m in r["missing_data"]["items"]], "", "## Limitations",
          *[f"- {x}" for x in r["limitations"]], "", "## Next steps", *[f"- {x}" for x in r["next_steps"]]]
    return "\n".join(L)


def write(r: dict[str, Any], out_dir: Path) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "pilot_readiness.json").write_text(json.dumps(r, indent=2, default=str))
    (out_dir / "pilot_readiness.md").write_text(markdown(r))
    return {"json": str(out_dir / "pilot_readiness.json"), "markdown": str(out_dir / "pilot_readiness.md")}
