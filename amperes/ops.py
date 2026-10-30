"""Operational event model, data-yield metrics, bottleneck analyzer, and cross-session correlation.

Principles enforced here:
- Every rate states its numerator, denominator, window and filters.
- A metric whose inputs are missing is returned as `not_computable` with the reason. Missing is never zero.
- Associations are labelled as associations; no rule asserts a cause.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any

from amperes import store
from amperes.ingest.base import RecordError
from amperes.stats import wilson

YIELD_RULE_VERSION = "yield/1.0"
DATA_VALID_STATUSES = ("verified_success", "valid_success_unverified", "valid_failed_task", "unknown_outcome")
EVENT_TYPES = {
    "session_start", "session_end", "attempt_start", "attempt_end", "station_down", "station_up", "fault",
    "recovery", "maintenance_start", "maintenance_end", "config_change", "manual_intervention", "reset", "restart",
    "recording_error", "incident_ack", "incident_resolved",
}
FAULT_CLASSES = (
    "robot_task_failure", "data_recording_failure", "invalid_data", "hardware_fault", "software_fault",
    "operator_interruption", "infrastructure_failure", "unclassified",
)
DEFAULT_CLASS = {"recording_error": "data_recording_failure", "manual_intervention": "operator_interruption",
                 "fault": "unclassified", "restart": "unclassified", "reset": "unclassified"}
EVENT_COLUMNS = ["event_id", "timestamp", "end_timestamp", "workcell_id", "device_id", "session_id", "episode_id",
                 "event_type", "event_class", "code", "message", "incident_ref"]


def normalize_event(obj: dict[str, Any], dataset_id: str, ref: str) -> dict[str, Any]:
    et = (obj.get("event_type") or "").strip()
    if et not in EVENT_TYPES:
        raise ValueError(f"{ref}: event_type {et!r} not in {sorted(EVENT_TYPES)}")
    try:
        t = float(obj["timestamp"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"{ref}: timestamp is required and must be numeric seconds")
    t_end = obj.get("end_timestamp")
    t_end = float(t_end) if t_end not in (None, "") else None
    cls = (obj.get("event_class") or "").strip() or DEFAULT_CLASS.get(et, "operational")
    if et in DEFAULT_CLASS and cls not in FAULT_CLASSES:
        raise ValueError(f"{ref}: event_class {cls!r} not in {list(FAULT_CLASSES)}")
    clean = lambda k: (str(obj.get(k)).strip() or None) if obj.get(k) not in (None, "") else None  # noqa: E731
    row = {"t": t, "t_end": t_end, "workcell_id": clean("workcell_id"), "device_id": clean("device_id"),
           "session_id": clean("session_id"), "event_class": cls, "event_type": et, "code": clean("code"),
           "message": clean("message"), "episode_id": clean("episode_id"), "incident_ref": clean("incident_ref")}
    digest = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()[:16]
    # Event ids are scoped to the dataset: identical content imported into two datasets must not collide.
    row.update({"event_id": f"{dataset_id}/{clean('event_id') or 'ev_' + digest}", "dataset_id": dataset_id, "provenance": ref,
                "content_digest": digest})
    return row


def read_events_csv(path: Path, dataset_id: str) -> tuple[list[dict[str, Any]], list[RecordError]]:
    events, errors = [], []
    with open(path, newline="", encoding="utf-8") as fh:
        for i, row in enumerate(csv.DictReader(fh), 2):
            ref = f"{path.name}:row {i}"
            try:
                events.append(normalize_event(row, dataset_id, ref))
            except ValueError as exc:
                errors.append(RecordError(ref, str(exc)))
    return events, errors


def dedupe_events(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Identical event content (same digest) is one event; duplicates never inflate counts."""
    seen, out = set(), []
    for e in events:
        if e["content_digest"] in seen:
            continue
        seen.add(e["content_digest"])
        out.append(e)
    return out, len(events) - len(out)


# ------------------------------------------------------------------------------------------------ helpers
def _nc(reason: str) -> dict[str, Any]:
    return {"status": "not_computable", "reason": reason}


def _rate(num: int, den: float, unit: str, definition: str, **kw) -> dict[str, Any]:
    if not den:
        return _nc(f"denominator ({unit}) is zero or unknown")
    return {"status": "computed", "value": num / den, "numerator": num, "denominator": round(den, 4), "unit": unit,
            "definition": definition, **kw}


def _load(dataset_ids: list[str], t0: float | None, t1: float | None, filters: dict[str, str]):
    q = "SELECT * FROM episodes WHERE dataset_id IN (%s)" % ",".join("?" * len(dataset_ids))
    eps = store.query(q, dataset_ids)
    evs = store.query("SELECT * FROM ops_events WHERE dataset_id IN (%s) ORDER BY t" % ",".join("?" * len(dataset_ids)), dataset_ids)

    def keep(r, tkey):
        t = r.get(tkey)
        if (t0 is not None or t1 is not None) and t is None:
            return False
        if t0 is not None and t < t0 or t1 is not None and t > t1:
            return False
        return all(r.get(k) == v for k, v in filters.items() if k in r)

    return [e for e in eps if keep(e, "start_time")], [e for e in evs if keep(e, "t")]


def session_hours(evs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    starts = {}
    for e in evs:
        sid = e["session_id"]
        if not sid:
            continue
        if e["event_type"] == "session_start":
            starts[sid] = e
        elif e["event_type"] == "session_end" and sid in starts:
            s = starts.pop(sid)
            out[sid] = {"start": s["t"], "end": e["t"], "hours": (e["t"] - s["t"]) / 3600.0,
                        "workcell_id": s["workcell_id"] or e["workcell_id"]}
    return out


def duplicate_episode_ids(dataset_ids: list[str]) -> set[str]:
    q = "SELECT episode_id FROM quality_findings WHERE rule_id IN ('D001_duplicate_content','D002_duplicate_source_id') AND dataset_id IN (%s)"
    return {r["episode_id"] for r in store.query(q % ",".join("?" * len(dataset_ids)), dataset_ids)}


# ------------------------------------------------------------------------------------------------ metrics
def ops_metrics(dataset_ids: list[str], t0: float | None = None, t1: float | None = None,
                filters: dict[str, str] | None = None,
                usable_statuses=("verified_success", "valid_success_unverified", "valid_failed_task")) -> dict[str, Any]:
    filters = filters or {}
    eps, evs = _load(dataset_ids, t0, t1, filters)
    window = {"t0": t0, "t1": t1, "filters": filters, "dataset_ids": dataset_ids}
    dups = duplicate_episode_ids(dataset_ids)
    eligible = [e for e in eps if e["episode_id"] not in dups]
    usable = [e for e in eligible if e["quality_status"] in usable_statuses]
    sh = session_hours(evs)
    hours = sum(s["hours"] for s in sh.values())
    eps_in_sessions = [e for e in eligible if e["session_id"] in sh]
    completed = [e for e in eps_in_sessions if e["outcome"] in ("success", "failure", "timeout")]
    usable_in_sessions = [e for e in eps_in_sessions if e["quality_status"] in usable_statuses]

    durations = [e["end_time"] - e["start_time"] for e in eligible if e["start_time"] is not None and e["end_time"] is not None]
    gaps = []
    by_s = defaultdict(list)
    for e in eligible:
        if e["session_id"] and e["start_time"] is not None and e["end_time"] is not None:
            by_s[e["session_id"]].append(e)
    for lst in by_s.values():
        lst.sort(key=lambda e: e["start_time"])
        gaps += [b["start_time"] - a["end_time"] for a, b in zip(lst, lst[1:])]
    attempt_events = [e for e in evs if e["event_type"] == "attempt_start"]
    with_idx = [e for e in eligible if e.get("attempt_index") is not None]
    repeated = sum(e["attempt_index"] > 1 for e in with_idx)

    throughput = {
        "session_hours_recorded": {"status": "computed" if sh else "not_computable", "value": round(hours, 4),
                                   "n_sessions": len(sh), "reason": None if sh else "no session_start/session_end pairs"},
        "attempts_per_hour": _rate(len(attempt_events) if attempt_events else len(eps_in_sessions), hours, "session-hours",
                                   "attempt_start events (or episodes when no attempt events) / recorded session hours"),
        "completed_episodes_per_hour": _rate(len(completed), hours, "session-hours",
                                             "episodes with outcome in {success,failure,timeout} inside recorded sessions / session hours"),
        "usable_episodes_per_hour": _rate(len(usable_in_sessions), hours, "session-hours",
                                          f"episodes with quality_status in {list(usable_statuses)} inside recorded sessions / session hours"),
        "median_episode_duration_s": {"status": "computed", "value": median(durations), "n": len(durations)} if durations
        else _nc("episodes lack start_time/end_time"),
        "median_time_between_episodes_s": {"status": "computed", "value": median(gaps), "n": len(gaps)} if gaps
        else _nc("needs >= 2 timed episodes in one session"),
        "repeated_attempts": {"status": "computed", "value": repeated, "n_with_attempt_index": len(with_idx),
                              "definition": "episodes whose attempt_index > 1"} if with_idx
        else _nc("no attempt_index recorded"),
    }
    yield_ = {
        "usable_data_yield": {**_rate(len(usable), len(eligible), "eligible episodes",
                                      "usable / eligible", rule_version=YIELD_RULE_VERSION),
                              "usable_statuses": list(usable_statuses),
                              "eligibility": "all episodes in window and filters, excluding content/source-id duplicates (D001/D002)",
                              "excluded_duplicates": len([e for e in eps if e["episode_id"] in dups]),
                              "ci95": list(wilson(len(usable), len(eligible))) if eligible else None},
        "strict_yield_verified_only": _rate(sum(e["quality_status"] in ("verified_success",) or
                                                (e["quality_status"] == "valid_failed_task" and e["verified"]) for e in eligible),
                                            len(eligible), "eligible episodes",
                                            "episodes whose outcome was independently verified and data is valid / eligible"),
        "data_validity": _rate(sum(e["quality_status"] in DATA_VALID_STATUSES for e in eligible), len(eligible), "eligible episodes",
                               "episodes whose recorded data has no serious defect, regardless of task outcome / eligible",
                               statuses=list(DATA_VALID_STATUSES)),
        "status_counts": dict(Counter(e["quality_status"] for e in eligible)),
        "note": "A failed task with valid data counts as usable. Each episode is counted once regardless of defect count.",
    }
    loss_rules = {"missing_required_fields": ("C001_required_metadata", "C002_no_observations", "C003_no_actions", "C005_missing_outcome"),
                  "invalid_temporal_alignment": ("T001_non_monotonic", "T003_time_gap", "T005_obs_action_misalignment")}
    findings = store.query("SELECT episode_id, rule_id FROM quality_findings WHERE dataset_id IN (%s)" % ",".join("?" * len(dataset_ids)), dataset_ids)
    elig_ids = {e["episode_id"] for e in eligible}
    loss = {"rejected_invalid": sum(e["quality_status"] == "invalid_data" for e in eligible),
            "requires_manual_review": sum(e["quality_status"] == "suspicious" for e in eligible),
            "infrastructure_or_recording_error": sum(e["quality_status"] == "infrastructure_error" for e in eligible),
            "unknown_outcome": sum(e["outcome"] in (None, "unknown") for e in eligible)}
    for k, rules in loss_rules.items():
        loss[k] = len({f["episode_id"] for f in findings if f["rule_id"] in rules and f["episode_id"] in elig_ids})
    resolved = store.query("SELECT DISTINCT m.episode_id FROM incident_members m JOIN incidents i ON i.incident_id=m.incident_id "
                           "WHERE i.status IN ('resolved','closed') AND i.dataset_id IN (%s)" % ",".join("?" * len(dataset_ids)), dataset_ids)
    loss["in_resolved_incidents"] = len({r["episode_id"] for r in resolved} & elig_ids)
    loss["unit"] = "distinct episodes (an episode with several defects counts once per row)"
    return {"window": window, "n_episodes_loaded": len(eps), "n_events_loaded": len(evs), "throughput": throughput,
            "yield": yield_, "data_quality_loss": loss,
            "yield_loss_attribution": yield_loss_attribution(eligible, usable_statuses, dataset_ids),
            "reliability": reliability(evs, sh)}


LOSS_EVENT_TYPES = ("recording_error", "fault", "manual_intervention", "reset", "restart")


def yield_loss_attribution(eligible: list[dict[str, Any]], usable_statuses, dataset_ids: list[str]) -> dict[str, Any]:
    """Attribute each non-usable episode once: to its earliest linked operational event (by episode_id) if any,
    otherwise to its most severe data-quality finding, otherwise to its status. Linkage, not root cause."""
    lost = [e for e in eligible if e["quality_status"] not in usable_statuses]
    ph = ",".join("?" * len(dataset_ids))
    evs = defaultdict(list)
    for x in store.query(f"SELECT * FROM ops_events WHERE dataset_id IN ({ph}) AND episode_id IS NOT NULL ORDER BY t", dataset_ids):
        if x["event_type"] in LOSS_EVENT_TYPES:
            evs[x["episode_id"]].append(x)
    rank = {"critical": 0, "major": 1, "minor": 2, "info": 3}
    fnd = defaultdict(list)
    for f in store.query(f"SELECT episode_id, rule_id, severity FROM quality_findings WHERE dataset_id IN ({ph}) AND severity<>'info'", dataset_ids):
        fnd[f["episode_id"]].append(f)
    groups: dict[str, dict[str, Any]] = {}
    for e in lost:
        if evs.get(e["episode_id"]):
            x = evs[e["episode_id"]][0]
            cause, basis = f"event {x['event_class']}:{x['code']}", "linked operational event"
        elif fnd.get(e["episode_id"]):
            f = min(fnd[e["episode_id"]], key=lambda f: (rank[f["severity"]], f["rule_id"]))
            cause, basis = f"finding {f['rule_id']}", "most severe data-quality finding"
        else:
            cause, basis = f"status {e['quality_status']}", "episode status only"
        g = groups.setdefault(cause, {"cause": cause, "basis": basis, "episodes": 0, "episode_hours": 0.0, "timed": 0,
                                      "sessions": set(), "workcells": set(), "statuses": Counter(), "examples": []})
        g["episodes"] += 1
        g["statuses"][e["quality_status"]] += 1
        if e["start_time"] is not None and e["end_time"] is not None:
            g["episode_hours"] += (e["end_time"] - e["start_time"]) / 3600
            g["timed"] += 1
        if e["session_id"]:
            g["sessions"].add(e["session_id"])
        if e["workcell_id"]:
            g["workcells"].add(e["workcell_id"])
        if len(g["examples"]) < 5:
            g["examples"].append(e["episode_id"])
    rows = []
    for g in sorted(groups.values(), key=lambda g: -g["episodes"]):
        rows.append({**{k: g[k] for k in ("cause", "basis", "episodes", "examples")},
                     "share_of_lost": g["episodes"] / len(lost),
                     "episode_hours": round(g["episode_hours"], 4) if g["timed"] == g["episodes"] else None,
                     "sessions": len(g["sessions"]), "workcells": sorted(g["workcells"]), "statuses": dict(g["statuses"])})
    return {"lost_episodes": len(lost), "eligible": len(eligible), "rows": rows,
            "note": "Each non-usable episode is attributed once (linked event first, else most severe finding). "
                    "This is record linkage, not an established root cause. episode_hours is null when any episode lacks timestamps."}


def _intervals(evs, start_type, end_type, key):
    open_, out = {}, []
    for e in evs:
        k = e.get(key)
        if e["event_type"] == start_type:
            open_[k] = e
        elif e["event_type"] == end_type and k in open_:
            s = open_.pop(k)
            out.append((s, e))
    return out, open_


def reliability(evs: list[dict[str, Any]], sh: dict[str, dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    sched = defaultdict(float)
    for s in sh.values():
        if s["workcell_id"]:
            sched[s["workcell_id"]] += s["hours"]
    downs, still_down = _intervals(evs, "station_down", "station_up", "workcell_id")
    tracked = {e["workcell_id"] for e in evs if e["event_type"] in ("station_down", "station_up")}
    down_h = defaultdict(float)
    for s, e in downs:
        down_h[s["workcell_id"]] += (e["t"] - s["t"]) / 3600
    avail = {}
    for wc, h in sched.items():
        if wc not in tracked:
            avail[wc] = _nc("no station_down/station_up events recorded for this workcell; downtime unknown")
        else:
            avail[wc] = {"status": "computed", "value": max(0.0, 1 - down_h[wc] / h) if h else None,
                         "downtime_h": round(down_h[wc], 4), "scheduled_h": round(h, 4),
                         "definition": "1 - recorded station downtime / recorded session hours"}
    out["availability_by_workcell"] = avail or _nc("no recorded sessions with workcell ids")
    out["open_downtime_intervals"] = len(still_down)
    faults = [e for e in evs if e["event_type"] in ("fault", "recording_error")]
    total_h = sum(sched.values())
    out["incident_frequency"] = _rate(len(faults), total_h, "session-hours", "fault + recording_error events / recorded session hours",
                                      by_class=dict(Counter(e["event_class"] for e in faults)))
    rec_waits = []
    for i, f in enumerate(faults):
        nxt = next((e for e in evs if e["event_type"] == "recovery" and e["t"] >= f["t"] and
                    e["workcell_id"] == f["workcell_id"] and (e["code"] in (None, f["code"]))), None)
        if nxt:
            rec_waits.append(nxt["t"] - f["t"])
    out["recovery_wait"] = {"status": "computed", "value": round(sum(rec_waits), 2), "unit": "s (total)",
                            "total_s": round(sum(rec_waits), 2), "mean_s": round(mean(rec_waits), 2),
                            "n_faults_with_recovery": len(rec_waits), "n_faults": len(faults)} if rec_waits else \
        _nc("no fault→recovery pairs on the same workcell")
    first = {}
    for e in evs:
        if e["incident_ref"] and e["event_type"] in ("fault", "recording_error"):
            first.setdefault(e["incident_ref"], e["t"])
    acks = {e["incident_ref"]: e["t"] for e in evs if e["event_type"] == "incident_ack" and e["incident_ref"]}
    resolves = {e["incident_ref"]: e for e in evs if e["event_type"] == "incident_resolved" and e["incident_ref"]}
    tta = [acks[k] - first[k] for k in acks if k in first]
    ttr = [resolves[k]["t"] - first[k] for k in resolves if k in first]
    out["mean_time_to_acknowledge_s"] = {"status": "computed", "value": mean(tta), "n": len(tta)} if tta else _nc("no incident_ack events linked to faults")
    out["mean_time_to_resolve_s"] = {"status": "computed", "value": mean(ttr), "n": len(ttr)} if ttr else _nc("no incident_resolved events linked to faults")
    recurred = []
    for ref, r in resolves.items():
        codes = {e["code"] for e in evs if e["incident_ref"] == ref and e["event_type"] in ("fault", "recording_error")}
        wcs = {e["workcell_id"] for e in evs if e["incident_ref"] == ref and e["event_type"] in ("fault", "recording_error")}
        again = [e for e in faults if e["t"] > r["t"] and e["code"] in codes and e["workcell_id"] in wcs]
        if again:
            recurred.append({"incident_ref": ref, "resolved_at": r["t"], "recurrences": len(again), "first_recurrence_t": again[0]["t"]})
    out["repeat_failure_after_resolution"] = {**_rate(len(recurred), len(resolves), "resolved incidents",
                                                      "resolved incidents whose fault code recurred on the same workcell afterwards"),
                                              "recurred": recurred} if resolves else _nc("no incident_resolved events")
    out["recurring_fault_codes"] = dict(Counter(f"{e['event_class']}:{e['code']}" for e in faults).most_common(10))
    return out


# ------------------------------------------------------------------------------------------------ bottlenecks
MIN_SESSION_EPISODES = 10


def _finding(rule, evidence, baseline, window, n, consequence, limits, action, needed, kind="association"):
    return {"rule": rule, "kind": kind, "observed_evidence": evidence, "comparison_baseline": baseline, "time_window": window,
            "sample_size": n, "possible_consequence": consequence, "confidence_and_limitations": limits,
            "recommended_investigation": action, "information_needed_to_verify": needed}


def bottlenecks(dataset_ids: list[str], t0=None, t1=None, rejected_tolerance: float = 0.05,
                min_session_episodes: int = MIN_SESSION_EPISODES) -> list[dict[str, Any]]:
    """`rejected_tolerance` and `min_session_episodes` are proposals to agree with the customer before evaluation."""
    eps, evs = _load(dataset_ids, t0, t1, {})
    window = {"t0": t0, "t1": t1, "datasets": dataset_ids}
    out = []
    n = len(eps)
    if n:
        bad = sum(e["quality_status"] in ("invalid_data", "infrastructure_error") for e in eps)
        pending = sum(e["quality_status"] == "suspicious" for e in eps)
        lo, hi = wilson(bad, n)
        if n >= 20 and lo > rejected_tolerance:
            out.append(_finding("B001_high_rejected_share",
                                {"rejected_invalid_or_recording_error": bad, "suspicious_pending_review": pending, "n": n,
                                 "rejected_rate": bad / n, "ci95": [lo, hi]},
                                f"pre-declared tolerance: {rejected_tolerance:.0%} rejected (invalid_data + infrastructure_error); "
                                "suspicious episodes are pending review and not counted here", window, n,
                                "Collection time is spent producing episodes that cannot be used.",
                                "Wilson lower bound exceeds the tolerance; tolerance is a proposal to agree with the customer.",
                                "Break down unusable episodes by rule and session.", "Customer-agreed tolerance.", kind="observed"))
    # B002 / B007: per-session rates vs all other sessions (Wilson interval separation, min sample size)
    by_s = defaultdict(list)
    for e in eps:
        if e["session_id"]:
            by_s[e["session_id"]].append(e)
    gap_eps = {r["episode_id"] for r in store.query(
        "SELECT episode_id FROM quality_findings WHERE rule_id IN ('T003_time_gap','C007_missing_samples','C004_truncated') "
        "AND dataset_id IN (%s)" % ",".join("?" * len(dataset_ids)), dataset_ids)}
    usable_set = ("verified_success", "valid_success_unverified", "valid_failed_task")
    for metric, pred, rule, consequence in (
            ("missing-observation rate", lambda e: e["episode_id"] in gap_eps, "B002_session_missing_observations",
             "Episodes from this session are more often incomplete."),
            ("usable yield", lambda e: e["quality_status"] in usable_set, "B007_session_low_yield",
             "This session produced fewer usable episodes than comparable sessions.")):
        for sid, lst in by_s.items():
            others = [e for s2, l2 in by_s.items() if s2 != sid for e in l2]
            if len(lst) < min_session_episodes or len(others) < min_session_episodes:
                continue
            k, k2 = sum(map(pred, lst)), sum(map(pred, others))
            a, b = wilson(k, len(lst)), wilson(k2, len(others))
            worse = a[0] > b[1] if rule.startswith("B002") else a[1] < b[0]
            if worse:
                out.append(_finding(rule, {"session_id": sid, "count": k, "n": len(lst), "rate": k / len(lst), "ci95": list(a),
                                           "workcells": sorted({e["workcell_id"] for e in lst if e["workcell_id"]})},
                                    {"other_sessions_rate": k2 / len(others), "ci95": list(b), "n": len(others)}, window,
                                    len(lst), consequence,
                                    f"95 % Wilson intervals do not overlap ({metric}); sessions may differ in task mix or operator.",
                                    "Review the session's event log and configuration.", "Session task mix, operator notes, station config."))
    # B003: configuration associated with repeated faults (faults per session-hour by config)
    sh = session_hours(evs)
    configs = _session_configs(dataset_ids)
    faults_by_cfg, hours_by_cfg = Counter(), defaultdict(float)
    for sid, s in sh.items():
        cfg = configs.get(sid)
        if cfg:
            hours_by_cfg[cfg] += s["hours"]
            faults_by_cfg[cfg] += sum(1 for e in evs if e["session_id"] == sid and e["event_type"] in ("fault", "recording_error"))
    if len(hours_by_cfg) >= 2:
        rates = {c: faults_by_cfg[c] / h for c, h in hours_by_cfg.items() if h > 0}
        for c, r in rates.items():
            rest_f = sum(v for k, v in faults_by_cfg.items() if k != c)
            rest_h = sum(v for k, v in hours_by_cfg.items() if k != c)
            if rest_h and faults_by_cfg[c] >= 5 and r > 2 * (rest_f / rest_h):
                out.append(_finding("B003_config_fault_association", {"config_id": c, "faults": faults_by_cfg[c],
                                                                      "session_hours": round(hours_by_cfg[c], 3), "faults_per_hour": round(r, 3)},
                                    {"other_configs_faults_per_hour": round(rest_f / rest_h, 3), "session_hours": round(rest_h, 3)},
                                    window, faults_by_cfg[c], "A configuration may be associated with more faults.",
                                    "Rate ratio > 2 with >= 5 faults; association only — configs may also differ in workload.",
                                    "Compare fault codes before/after the configuration was introduced.",
                                    "Change records and the fault codes' meaning."))
    # B004: cycle time after a configuration change (same workcell, before vs after)
    for ch in (e for e in evs if e["event_type"] == "config_change" and e["workcell_id"]):
        wc_eps = sorted((e for e in eps if e["workcell_id"] == ch["workcell_id"] and e["start_time"] is not None
                         and e["end_time"] is not None), key=lambda e: e["start_time"])
        before = [e["end_time"] - e["start_time"] for e in wc_eps if e["start_time"] < ch["t"]][-30:]
        after = [e["end_time"] - e["start_time"] for e in wc_eps if e["start_time"] >= ch["t"]][:30]
        if len(before) >= 10 and len(after) >= 10:
            mb, ma = median(before), median(after)
            if ma > 1.15 * mb:
                out.append(_finding("B004_cycle_time_after_change", {"workcell_id": ch["workcell_id"], "change_code": ch["code"],
                                                                     "change_t": ch["t"], "median_after_s": round(ma, 3), "n_after": len(after)},
                                    {"median_before_s": round(mb, 3), "n_before": len(before)}, window, len(before) + len(after),
                                    "Cycle time rose after this change.",
                                    "Temporal association only; other changes in the same period are not excluded.",
                                    "Inspect the change and any concurrent events.", "Full change log for the workcell."))
    # B005: share of recorded session time spent waiting for recovery
    rel = reliability(evs, sh)
    rw = rel.get("recovery_wait", {})
    hours = sum(s["hours"] for s in sh.values())
    if rw.get("status") == "computed" and hours and rw["total_s"] / (hours * 3600) > 0.05:
        out.append(_finding("B005_recovery_time_share", {"recovery_wait_s": rw["total_s"], "faults_with_recovery": rw["n_faults_with_recovery"]},
                            {"session_time_s": round(hours * 3600, 1), "threshold": "5 % of session time"}, window, rw["n_faults_with_recovery"],
                            "Recorded recovery waits consume a material share of station time.", "Measured from event timestamps.",
                            "Rank fault codes by recovery time.", "Whether recovery events are logged consistently.", kind="observed"))
    # B006: unresolved incidents
    inc = store.query("SELECT status FROM incidents WHERE dataset_id IN (%s)" % ",".join("?" * len(dataset_ids)), dataset_ids)
    if len(inc) >= 5:
        open_ = sum(i["status"] in ("open", "investigating", "unresolved") for i in inc)
        if open_ / len(inc) > 0.5:
            out.append(_finding("B006_unresolved_incidents", {"open_or_unresolved": open_, "n": len(inc)}, {"threshold": "50 %"}, window,
                                len(inc), "Most recorded incidents have no resolution.", "Counts from the Amperes incident table.",
                                "Triage the open incidents by occurrence count.", "Ownership for each incident.", kind="observed"))
    return out


def _session_configs(dataset_ids: list[str]) -> dict[str, str]:
    """Session → robot config id, only when every episode in the session reports the same single config."""
    cfgs = defaultdict(set)
    for r in store.query("SELECT session_id, config_id FROM episodes WHERE dataset_id IN (%s) AND session_id IS NOT NULL"
                         % ",".join("?" * len(dataset_ids)), dataset_ids):
        cfgs[r["session_id"]].add(r["config_id"])
    return {s: next(iter(c)) for s, c in cfgs.items() if len(c) == 1 and None not in c}


# ------------------------------------------------------------------------------------------------ correlation
def correlate(dataset_ids: list[str], t0=None, t1=None, filters: dict[str, str] | None = None) -> dict[str, Any]:
    eps, evs = _load(dataset_ids, t0, t1, filters or {})
    faults = [e for e in evs if e["event_type"] in ("fault", "recording_error")]
    groups = defaultdict(list)
    for e in faults:
        groups[(e["event_class"], e["code"])].append(e)
    repetition = []
    for (cls, code), lst in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        sessions = sorted({e["session_id"] for e in lst if e["session_id"]})
        wcs = sorted({e["workcell_id"] for e in lst if e["workcell_id"]})
        if len(sessions) >= 2 or len(wcs) >= 2:
            repetition.append({"kind": "directly_observed_repetition", "event_class": cls, "code": code, "occurrences": len(lst),
                               "sessions": sessions, "workcells": wcs, "first_t": lst[0]["t"], "last_t": lst[-1]["t"],
                               "grouping_reason": "identical (event_class, code) — no text similarity used",
                               "source_events": [e["event_id"] for e in lst][:50]})
    configs = _session_configs(dataset_ids)
    assoc = []
    sh = session_hours(evs)
    for item in repetition:
        cfg_hits = Counter(configs.get(s) for s in item["sessions"] if configs.get(s))
        if not cfg_hits:
            continue
        cfg, hits = cfg_hits.most_common(1)[0]
        cfg_sessions = [s for s in sh if configs.get(s) == cfg]
        other_sessions = [s for s in sh if configs.get(s) and configs.get(s) != cfg]
        affected_other = [s for s in item["sessions"] if s in other_sessions]
        if len(cfg_sessions) >= 2 and other_sessions:
            a, b = hits / len(cfg_sessions), len(affected_other) / len(other_sessions)
            assoc.append({"kind": "statistical_association", "code": item["code"], "config_id": cfg,
                          "sessions_with_code_given_config": f"{hits}/{len(cfg_sessions)}",
                          "sessions_with_code_other_configs": f"{len(affected_other)}/{len(other_sessions)}",
                          "rate_given_config": a, "rate_other_configs": b,
                          "note": "Session-level co-occurrence; not evidence that the configuration causes the fault."})
    gaps_by_path = defaultdict(set)
    for r in store.query("SELECT f.episode_id, f.rule_id, e.device_id, e.workcell_id FROM quality_findings f JOIN episodes e "
                         "ON e.episode_id=f.episode_id WHERE f.rule_id IN ('C001_required_metadata','C008_missing_channel') "
                         "AND f.dataset_id IN (%s)" % ",".join("?" * len(dataset_ids)), dataset_ids):
        gaps_by_path[(r["rule_id"], r["workcell_id"], r["device_id"])].add(r["episode_id"])
    persistent = [{"kind": "directly_observed_repetition", "rule_id": k[0], "workcell_id": k[1], "device_id": k[2],
                   "episodes": len(v)} for k, v in gaps_by_path.items() if len(v) >= 3]
    rel = reliability(evs, sh)
    hyp = [{"kind": "proposed_causal_explanation", "about": f"{x['event_class']}:{x['code']}",
            "hypothesis": "Recurring code across sessions suggests a shared upstream cause (unverified).",
            "test_to_verify": "Check whether the code's occurrences share a device, configuration, or software version; "
                              "attempt reproduction under controlled conditions."} for x in repetition[:5]]
    return {"directly_observed_repetition": repetition, "statistical_association": assoc,
            "persistent_missing_fields_by_export_path": persistent,
            "resolved_then_recurred": rel.get("repeat_failure_after_resolution", {}),
            "proposed_causal_explanations": hyp,
            "limitations": "Grouping uses exact codes and identifiers only. Associations are not causes."}
