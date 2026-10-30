"""Deterministic incident grouping, status workflow with history, regression linking, and export.

Grouping keys (no text similarity, no model):
- data_quality:      (dataset, rule_id) for findings with severity critical/major
- task_failure:      (dataset, task_id, policy_id, policy_version, failure category)
- operational_fault: (dataset, event_class, code) for fault / recording_error events
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from amperes import store
from amperes.provenance import new_id, now_iso
from amperes.schema import read_episode

STATUSES = ("open", "investigating", "reproduced", "resolved", "closed", "unresolved")
TRANSITIONS = {
    "open": {"investigating", "reproduced", "resolved", "closed", "unresolved"},
    "investigating": {"reproduced", "resolved", "unresolved", "open"},
    "reproduced": {"investigating", "resolved", "unresolved"},
    "resolved": {"closed", "open"},
    "unresolved": {"investigating", "closed", "open"},
    "closed": {"open"},
}
SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2, "info": 3}

NEXT_ACTIONS = {
    "T003_time_gap": ["Pull recorder logs for the gap windows", "Check whether gaps cluster by session/device"],
    "C007_missing_samples": ["Inspect the producing driver for dropped messages", "Compare with other sessions on the same device"],
    "C004_truncated": ["Check recorder stop/crash logs at the truncation time"],
    "S001_non_finite": ["Locate the first non-finite sample and the producing component"],
    "T001_non_monotonic": ["Check for clock resets / NTP steps at the first backwards timestamp"],
    "O002_label_contradicts_state": ["Have a reviewer inspect the episode replay or video", "Audit the labelling step"],
    "D001_duplicate_content": ["Find the export step that writes episodes twice"],
    "SLIPPAGE": ["Sweep object mass/friction around the failing condition", "Test a grip-force or motion-profile intervention"],
    "COLLISION": ["Compare transport height with obstacle geometry", "Test an obstacle-aware path intervention"],
    "GRASP_FAILURE": ["Compare object-estimate error with ground truth at grasp time", "Test grasp verification + retry"],
    "REACH_FAILURE": ["Compare object-estimate error with ground truth", "Test perception averaging"],
}


def _facts(kind: str, title: str, members: list[dict[str, Any]]) -> list[str]:
    eps = {m["episode_id"] for m in members if m["episode_id"]}
    sess = Counter(m["session_id"] for m in members if m["session_id"])
    facts = [f"{len(members)} occurrence(s) across {len(eps)} episode(s): {title}."]
    if sess:
        top, n = sess.most_common(1)[0]
        facts.append(f"{len(sess)} session(s) affected; {n} occurrence(s) in session {top}.")
    else:
        facts.append("No session identifiers recorded for these occurrences.")
    return facts


def _hypotheses(kind: str, key: str) -> list[dict[str, str]]:
    base = {"status": "unverified", "source": "deterministic template (no model)"}
    if kind == "data_quality":
        return [{**base, "text": "A shared recording or export component may produce this defect.",
                 "evidence_needed": "Recorder/driver logs for the affected windows; occurrence by device and software version."}]
    if kind == "task_failure":
        return [{**base, "text": f"Conditions shared by the failing episodes may trigger {key}.",
                 "evidence_needed": "Controlled reproduction and a sweep of the shared condition parameters."}]
    return [{**base, "text": "The recurring fault code may have a common upstream cause.",
             "evidence_needed": "Device/config/software version per occurrence; vendor documentation for the code."}]


def build_incidents(dataset_id: str, actor: str = "amperes") -> list[dict[str, Any]]:
    """(Re)group a dataset's evidence into incidents. Idempotent: existing incidents keep status and history."""
    groups: dict[str, dict[str, Any]] = {}
    fq = store.query("SELECT f.*, e.session_id, e.workcell_id, e.start_time FROM quality_findings f LEFT JOIN episodes e "
                     "ON e.episode_id=f.episode_id WHERE f.dataset_id=? AND f.severity IN ('critical','major')", [dataset_id])
    for f in fq:
        g = groups.setdefault(f"dq:{f['rule_id']}", {"kind": "data_quality", "category": f["rule_id"], "severity": f["severity"],
                                                     "title": f["explanation"].split(";")[0][:120] and f"rule {f['rule_id']} ({f['dimension']})",
                                                     "members": [], "evidence": []})
        g["severity"] = min(g["severity"], f["severity"], key=SEVERITY_RANK.get)
        t = (f["start_time"] or 0) + (f["t"] or 0) if f["start_time"] is not None else f["t"]
        g["members"].append({"episode_id": f["episode_id"], "ref_kind": "finding", "ref_id": f["finding_id"], "t": t,
                             "session_id": f["session_id"]})
        if len(g["evidence"]) < 20:
            g["evidence"].append({"finding_id": f["finding_id"], "episode_id": f["episode_id"], "observed": store.loads(f["observed"])
                                  if f["observed"] and f["observed"][:1] in "[{" else f["observed"], "expected": f["expected"],
                                  "record_ref": f["record_ref"]})
    tf = store.query("SELECT * FROM episodes WHERE dataset_id=? AND outcome IN ('failure','timeout') AND category IS NOT NULL", [dataset_id])
    for e in tf:
        key = f"tf:{e['task_id']}:{e['policy_id']}:{e['policy_version']}:{e['category']}"
        g = groups.setdefault(key, {"kind": "task_failure", "category": e["category"], "severity": "major",
                                    "title": f"{e['category']} on task {e['task_id']} (policy {e['policy_id']} {e['policy_version'] or ''})".strip(),
                                    "members": [], "evidence": []})
        g["members"].append({"episode_id": e["episode_id"], "ref_kind": "episode", "ref_id": e["episode_id"], "t": e["start_time"],
                             "session_id": e["session_id"]})
        if len(g["evidence"]) < 20:
            g["evidence"].append({"episode_id": e["episode_id"], "outcome": e["outcome"], "rule_id": e["rule_id"],
                                  "outcome_provenance": e["outcome_provenance"], "replay_class": e["replay_class"]})
    ev = store.query("SELECT * FROM ops_events WHERE dataset_id=? AND event_type IN ('fault','recording_error') ORDER BY t", [dataset_id])
    for x in ev:
        key = f"of:{x['event_class']}:{x['code']}"
        g = groups.setdefault(key, {"kind": "operational_fault", "category": x["event_class"], "severity": "major",
                                    "title": f"{x['event_class']} code {x['code']}", "members": [], "evidence": []})
        g["members"].append({"episode_id": x["episode_id"], "ref_kind": "event", "ref_id": x["event_id"], "t": x["t"],
                             "session_id": x["session_id"]})
        if len(g["evidence"]) < 20:
            g["evidence"].append({"event_id": x["event_id"], "t": x["t"], "workcell_id": x["workcell_id"], "device_id": x["device_id"],
                                  "message": x["message"], "provenance": x["provenance"]})

    existing = {r["group_key"]: r for r in store.query("SELECT * FROM incidents WHERE dataset_id=?", [dataset_id])}
    out = []
    for key, g in groups.items():
        m = g["members"]
        ts = [x["t"] for x in m if x["t"] is not None]
        sessions = {x["session_id"] for x in m if x["session_id"]}
        reason = {"data_quality": "same rule_id", "task_failure": "same task, policy version and failure category",
                  "operational_fault": "same event_class and fault code"}[g["kind"]]
        related = store.query("SELECT incident_id, dataset_id, status, n_occurrences FROM incidents WHERE group_key=? AND dataset_id<>?",
                              [key, dataset_id])
        row = {"dataset_id": dataset_id, "group_key": key, "category": g["category"], "kind": g["kind"], "title": g["title"],
               "severity": g["severity"], "first_seen": min(ts) if ts else None, "last_seen": max(ts) if ts else None,
               "n_occurrences": len(m), "n_episodes": len({x["episode_id"] for x in m if x["episode_id"]}),
               "n_sessions": len(sessions), "scope": {"sessions": sorted(sessions)[:50], "related_incidents": related},
               "evidence": g["evidence"], "grouping_reason": reason, "facts": _facts(g["kind"], g["title"], m),
               "hypotheses": _hypotheses(g["kind"], g["category"]),
               "next_actions": NEXT_ACTIONS.get(g["category"], ["Review the attached evidence and assign an owner"]),
               "updated_at": now_iso()}
        if key in existing:
            inc = existing[key]
            iid = inc["incident_id"]
            store.execute("UPDATE incidents SET " + ",".join(f"{k}=?" for k in row) + " WHERE incident_id=?",
                          [store._enc(v) for v in row.values()] + [iid])
            if inc["status"] in ("resolved", "closed") and row["last_seen"] and inc["last_seen"] is not None \
                    and row["last_seen"] > inc["last_seen"]:
                transition(iid, "open", actor, "new occurrence after resolution (recurred)")
        else:
            iid = new_id("inc")
            store.insert("incidents", {"incident_id": iid, "status": "open", "owner": None, "resolution": None,
                                       "regression_case_ids": [], "created_at": now_iso(), **row})
            store.insert("incident_history", {"incident_id": iid, "at": now_iso(), "actor": actor, "from_status": None,
                                              "to_status": "open", "note": f"created by deterministic grouping ({reason})"})
        store.execute("DELETE FROM incident_members WHERE incident_id=?", [iid])
        store.insert_many("incident_members", [{"incident_id": iid, **x} for x in m])
        out.append(get(iid))
    return out


def get(incident_id: str) -> dict[str, Any]:
    rows = store.query("SELECT * FROM incidents WHERE incident_id=?", [incident_id])
    if not rows:
        raise ValueError(f"unknown incident {incident_id}")
    inc = rows[0]
    for k in ("scope", "evidence", "facts", "hypotheses", "next_actions", "regression_case_ids"):
        inc[k] = store.loads(inc[k])
    inc["history"] = store.query("SELECT at, actor, from_status, to_status, note FROM incident_history WHERE incident_id=? ORDER BY rowid",
                                 [incident_id])
    inc["members"] = store.query("SELECT episode_id, ref_kind, ref_id, t, session_id FROM incident_members WHERE incident_id=?", [incident_id])
    return inc


def transition(incident_id: str, to_status: str, actor: str, note: str = "", owner: str | None = None,
               resolution: str | None = None) -> dict[str, Any]:
    inc = get(incident_id)
    if to_status not in STATUSES:
        raise ValueError(f"unknown status {to_status!r}")
    if to_status not in TRANSITIONS[inc["status"]]:
        raise ValueError(f"transition {inc['status']} -> {to_status} not allowed; allowed: {sorted(TRANSITIONS[inc['status']])}")
    store.execute("UPDATE incidents SET status=?, updated_at=?, owner=COALESCE(?, owner), resolution=COALESCE(?, resolution) "
                  "WHERE incident_id=?", [to_status, now_iso(), owner, resolution, incident_id])
    store.insert("incident_history", {"incident_id": incident_id, "at": now_iso(), "actor": actor, "from_status": inc["status"],
                                      "to_status": to_status, "note": note})
    return get(incident_id)


def to_regression_case(incident_id: str, actor: str = "amperes") -> dict[str, Any]:
    """Create a regression case from the incident's first replayable member episode (class A/B only)."""
    from amperes.regression import create_suite, make_case

    inc = get(incident_id)
    cand = store.query("SELECT e.* FROM incident_members m JOIN episodes e ON e.episode_id=m.episode_id WHERE m.incident_id=? "
                       "AND e.replay_class IN ('A','B') ORDER BY e.episode_id LIMIT 1", [incident_id])
    if not cand:
        classes = Counter(r["replay_class"] for r in store.query(
            "SELECT e.replay_class FROM incident_members m JOIN episodes e ON e.episode_id=m.episode_id WHERE m.incident_id=?", [incident_id]))
        return {"created": False, "reason": "no member episode is replayable (needs replay class A or B)",
                "member_replay_classes": dict(classes)}
    e = cand[0]
    ep = read_episode(store.safe_artifact_path(e["artifact_path"]))
    cond = {"condition_id": ep.conditions.get("condition_id") or e["episode_id"], "env_params": ep.replay.env_params,
            "seed": ep.replay.seed}
    suite_id = f"suite_incident_{incident_id}"
    case = make_case(suite_id, "incident", cond, {"episode_id": e["episode_id"], "policy_label": e["policy_label"],
                                                  "outcome": e["outcome"], "category": e["category"], "rule_id": e["rule_id"],
                                                  "run_id": e["run_id"]}, split="incident", incident_id=incident_id)
    if not store.query("SELECT 1 FROM regression_suites WHERE suite_id=?", [suite_id]):
        create_suite(None, f"incident {incident_id}", [case], note="created from incident")
    else:
        store.insert("regression_cases", case)
    ids = (inc["regression_case_ids"] or []) + [case["case_id"]]
    store.execute("UPDATE incidents SET regression_case_ids=?, updated_at=? WHERE incident_id=?", [json.dumps(ids), now_iso(), incident_id])
    store.insert("incident_history", {"incident_id": incident_id, "at": now_iso(), "actor": actor, "from_status": inc["status"],
                                      "to_status": inc["status"], "note": f"regression case {case['case_id']} created in {suite_id}"})
    return {"created": True, "suite_id": suite_id, "case_id": case["case_id"], "source_episode_id": e["episode_id"],
            "replay_class": e["replay_class"]}


def export(incident_id: str, out_dir: Path) -> dict[str, str]:
    inc = get(incident_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    jp = out_dir / f"{incident_id}.json"
    jp.write_text(json.dumps(inc, indent=2, default=str))
    classes = Counter(r["replay_class"] for r in store.query(
        "SELECT e.replay_class FROM incident_members m JOIN episodes e ON e.episode_id=m.episode_id WHERE m.incident_id=?", [incident_id]))
    L = [f"# Incident {incident_id}: {inc['title']}", "", f"- Kind: {inc['kind']} · Severity: {inc['severity']} · Status: **{inc['status']}**",
         f"- Occurrences: {inc['n_occurrences']} · Episodes: {inc['n_episodes']} · Sessions: {inc['n_sessions']}",
         f"- First seen: {inc['first_seen']} · Last seen: {inc['last_seen']}", f"- Grouping reason: {inc['grouping_reason']}", "",
         "## Observed facts", *[f"- {f}" for f in inc["facts"]], "",
         "## Possible causes (unverified hypotheses)", *[f"- {h['text']} — evidence needed: {h['evidence_needed']}" for h in inc["hypotheses"]], "",
         "## Suggested next actions", *[f"- {a}" for a in inc["next_actions"]], "",
         "## Reproducibility", f"- Member replay classes: {dict(classes)} (A/B replayable; C partial; D log-only; E not reproducible)",
         "- Reproducing a symptom does not establish its original cause.", "",
         "## Evidence (first 20)", "```json", json.dumps(inc["evidence"], indent=1, default=str)[:6000], "```", "",
         "## History", *[f"- {h['at']} {h['actor']}: {h['from_status']} → {h['to_status']} {h['note'] or ''}" for h in inc["history"]],
         "", f"Regression cases: {inc['regression_case_ids'] or 'none'}"]
    mp = out_dir / f"{incident_id}.md"
    mp.write_text("\n".join(L))
    return {"json": str(jp), "markdown": str(mp)}
