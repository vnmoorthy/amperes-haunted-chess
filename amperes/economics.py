"""Economic value model. Computes only from measured workflow differences plus explicit, user-supplied assumptions.

Assumptions file (JSON), every field optional:
{
  "engineer_cost_per_hour": 150,
  "triage_tasks_per_week": 40, "weeks_per_year": 48,
  "eligible_episodes_per_week": 2000, "value_per_usable_episode": {"low": 0.5, "central": 2, "high": 5},
  "target_yield_gain_pp": {"low": 0.5, "central": 2, "high": 4},
  "amperes_setup_hours": 16, "amperes_operating_hours_per_week": 2
}
Scenario values may be numbers or {"low","central","high"} triples; scenarios are produced only when every input of
a calculation has a triple or a single number (used for all three).
"""

from __future__ import annotations

from typing import Any

SCEN = ("low", "central", "high")


def _tri(v):
    if v is None:
        return None
    if isinstance(v, dict):
        if not all(k in v for k in SCEN):
            raise ValueError("scenario inputs need low, central and high")
        return {k: float(v[k]) for k in SCEN}
    return {k: float(v) for k in SCEN}


def estimate(assumptions: dict[str, Any], measured: dict[str, Any]) -> dict[str, Any]:
    a = {k: _tri(v) if not isinstance(v, str) else v for k, v in assumptions.items()}
    out: dict[str, Any] = {"label": "ESTIMATE — depends on the assumptions shown; not a measured saving",
                           "assumptions": assumptions, "measured_inputs": measured, "items": {}}
    saved_s = measured.get("median_time_saved_per_task_s")
    study_status = measured.get("study_status", "not measured")
    need = ["engineer_cost_per_hour", "triage_tasks_per_week", "weeks_per_year"]
    missing = [k for k in need if a.get(k) is None]
    if saved_s is None:
        out["items"]["direct_triage_time_savings"] = {"status": "not_computable", "reason": f"workflow study: {study_status}"}
    elif missing:
        out["items"]["direct_triage_time_savings"] = {"status": "not_computable", "reason": f"missing assumptions {missing}"}
    else:
        hours = {s: saved_s / 3600 * a["triage_tasks_per_week"][s] * a["weeks_per_year"][s] for s in SCEN}
        cost = {s: hours[s] * a["engineer_cost_per_hour"][s] for s in SCEN}
        overhead = None
        if a.get("amperes_operating_hours_per_week") is not None:
            overhead = {s: a["amperes_operating_hours_per_week"][s] * a["weeks_per_year"][s] * a["engineer_cost_per_hour"][s] for s in SCEN}
        net = {s: cost[s] - overhead[s] for s in SCEN} if overhead else None
        out["items"]["direct_triage_time_savings"] = {
            "status": "computed", "measured_seconds_saved_per_task": saved_s, "study_status": study_status,
            "preliminary": not str(study_status).startswith("measured"),
            "no_saving_measured": saved_s <= 0,
            "extrapolated_hours_per_year": hours, "gross_value_per_year": cost, "amperes_operating_cost_per_year": overhead,
            "net_value_per_year": net,
            "calculation": "seconds saved per task (measured) / 3600 × tasks per week × weeks per year × engineer cost/hour",
            "kind": "extrapolated from a measured per-task difference"}
    yneed = ["eligible_episodes_per_week", "target_yield_gain_pp", "value_per_usable_episode", "weeks_per_year"]
    ymiss = [k for k in yneed if a.get(k) is None]
    if ymiss:
        out["items"]["potential_yield_increase"] = {"status": "not_computable", "reason": f"missing assumptions {ymiss}"}
    else:
        val = {s: a["eligible_episodes_per_week"][s] * a["target_yield_gain_pp"][s] / 100 * a["weeks_per_year"][s]
               * a["value_per_usable_episode"][s] for s in SCEN}
        out["items"]["potential_yield_increase"] = {
            "status": "computed", "value_per_year": val, "kind": "hypothetical — yield gain is an assumption, not measured",
            "calculation": "eligible episodes/week × assumed yield gain × weeks × assumed value per usable episode",
            "warning": "Additional usable episodes do not have a fixed value; this line is the least reliable."}
    out["items"]["recurring_incident_reduction"] = {"status": "not_computable",
                                                    "reason": "requires a before/after incident-rate comparison over a longer pilot"}
    out["double_counting_guard"] = ("By construction: labor savings appear only in direct_triage_time_savings; the yield line "
                                    "uses value per usable episode and no labor terms. Inputs not used by any formula are ignored.")
    if a.get("amperes_setup_hours") is not None and a.get("engineer_cost_per_hour") is not None:
        out["one_time_setup_cost"] = {s: a["amperes_setup_hours"][s] * a["engineer_cost_per_hour"][s] for s in SCEN}
    computed = [k for k, v in out["items"].items() if v["status"] == "computed"]
    if "direct_triage_time_savings" in computed:
        d = out["items"]["direct_triage_time_savings"]
        out["roi_statement"] = ("Direct triage value is extrapolated from a measured per-task difference"
                                + (" (PRELIMINARY study)" if d["preliminary"] else "")
                                + ("; the assisted workflow was not faster, so no saving is claimed" if d["no_saving_measured"] else "")
                                + ". Any yield line is hypothetical.")
    elif computed:
        out["roi_statement"] = "No measured ROI: only the hypothetical yield line could be computed from assumptions."
    else:
        out["roi_statement"] = "No ROI computed: inputs are insufficient."
    return out
