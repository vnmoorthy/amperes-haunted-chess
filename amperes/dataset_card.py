"""Datasheet-style dataset card: Markdown plus a JSON sidecar, built only from facts in the store.

Every number is read from SQLite or the episode artifacts; a fact the store does not hold is printed as
"not recorded". The Markdown is rendered from the JSON facts, so the two never disagree.
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

from amperes import labels, store
from amperes.provenance import environment, now_iso
from amperes.quality import RULES, RULESET_VERSION, USABLE_STATUSES_DEFAULT
from amperes.schema import Episode, Stream, read_episode
from amperes.stats import wilson

CARD_VERSION = "amperes.dataset_card/1"
NOT_RECORDED = "not recorded"
FRAME_STREAM = "obs.joint_pos"
VALID_OUTCOMES = ("success", "failure", "timeout")
ROBOT_ADAPTERS = ("amperes.viam_control", "amperes.viam_tasks")
DRY_RUN_NOTE = "DRY RUN"
FRAME_FILE_PATTERN = re.compile(r"images/(?P<prefix>\S*?)<n:(?P<width>\d+)d>(?P<ext>\.\w+)")  # obs.<cam>.frame units

STATUS_MEANINGS = {
    "infrastructure_error": "explicit recording/harness error events, or a harness-invalid simulator run",
    "invalid_data": "any deterministic critical finding",
    "suspicious": "any major finding, or any finding that requires human review",
    "verified_success": "outcome success, independently verified",
    "valid_success_unverified": "outcome success asserted only by the source label",
    "valid_failed_task": "outcome failure/timeout with no serious data defect (still usable data)",
    "unknown_outcome": "no outcome, or outcome 'unknown'",
}
DATASET_RULES = {  # rules computed over the whole dataset (not in quality.RULES)
    "C008_missing_channel": "A stream present in >= 90 % of the dataset's episodes is absent from this one",
    "D001_duplicate_content": "Recorded content is byte-identical to another episode",
    "D002_duplicate_source_id": "Source record id appears more than once",
    "D003_mixed_versions_in_session": "One session contains several policy versions",
    "D004_session_duration_shift": "A session's median episode duration is far from the other sessions (robust z > 3.5)",
}
VERIFIER_NOTES = {
    "gripper_signals": "the gripper's own grab/holding reports; evidence from the actuator under test, not an independent sensor",
    "pose_error": "commanded target pose vs the pose the motion service reports after settling (reach error within tol_mm); evidence from "
                  "the arm's own kinematics/encoders, not an external sensor, so it checks the controller, not what happened in the scene",
    "pick_place_verifier": "simulator ground-truth object pose; independent of the policy label but only exists for synthetic runs",
}
SAFETY_MODEL = [  # docs/ROBOT_LAB.md, enforced in amperes/integrations/viam_control.py
    "dry run by default: motion needs --execute plus the typed operator acknowledgement phrase (e-stop in reach)",
    "taught safety box: targets stay within the taught spots (home, a, b) + 40 mm and the approach height; floor limits apply",
    "per-move limits: roughly the taught-spot distance, gripper rotation <= 45 deg per move",
    "collision-aware planning through the machine's motion service (table, walls, ceiling)",
    "watchdog: telemetry ~3 Hz; 4 s without telemetry during motion stops the arm",
    "STOP: UI button or Ctrl-C sends stop() to arm and gripper immediately",
    "a teammate stays at the arm with the e-stop in reach for every executed run (procedure, not code)",
]
CAMERA_MIMES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".dep": "image/vnd.viam.dep", ".png": "image/png"}


def _iso(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def _fmt(v: Any, digits: int = 3) -> str:
    if v is None or v == "" or v == [] or v == {}:
        return NOT_RECORDED
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        if not math.isfinite(v):
            return NOT_RECORDED
        return f"{v:.{digits}f}".rstrip("0").rstrip(".") if abs(v) < 1e6 else f"{v:.4g}"
    if isinstance(v, (list, tuple, set)):
        return ", ".join(_fmt(x) for x in v)
    return str(v)


def _pct(v: float | None) -> str:
    return NOT_RECORDED if v is None else f"{100 * v:.1f}%"


def _ref(id_: str | None, version: str | None) -> str | None:
    return None if id_ is None else (f"{id_}@{version}" if version else id_)


def _load(row: dict[str, Any]) -> Episode | None:
    try:
        return read_episode(store.safe_artifact_path(row["artifact_path"]))
    except (OSError, ValueError, TypeError):  # missing/corrupt artifact, path outside the store, NULL path
        return None


def _finite(vals) -> list[float]:
    return [v for v in vals if v is not None and math.isfinite(v)]


def _dist(vals: list[float]) -> dict[str, Any] | None:
    vals = _finite(vals)
    if not vals:
        return None
    return {"n": len(vals), "min": round(min(vals), 3), "median": round(median(vals), 3), "mean": round(mean(vals), 3),
            "max": round(max(vals), 3), "total": round(sum(vals), 3)}


def _rate(k: int, n: int) -> dict[str, Any] | None:
    if n < 1:
        return None
    lo, hi = wilson(k, n)
    return {"successes": k, "valid_outcomes": n, "rate": round(k / n, 4), "ci95": [round(lo, 4), round(hi, 4)],
            "method": "Wilson 95% interval over outcomes in {success, failure, timeout}"}


def _own_id(ep: Episode) -> str:
    return ep.extra.get("source_episode_id") or ep.source.source_record_id or ep.episode_id


def _frame_files(ep: Episode, cam: str, s: Stream) -> tuple[list[str], str]:
    """One image file per frame-index sample: the pattern declared in the stream's units, else viam_tasks' naming."""
    m = FRAME_FILE_PATTERN.search(s.units or "")
    prefix, width, ext = (m["prefix"], int(m["width"]), m["ext"].lower()) if m else (f"{_own_id(ep)}_{cam}_video_", 5, ".jpg")
    return [f"{prefix}{int(v[0]):0{width}d}{ext}" for v in s.values if v and v[0] is not None and math.isfinite(v[0])], ext


def _dry_run(ep: Episode | None) -> bool:
    return bool(ep and (ep.conditions.get("dry_run") or (ep.source.synthetic_note or "").startswith(DRY_RUN_NOTE)))


def _episode_facts(ep: Episode | None, row: dict[str, Any]) -> dict[str, Any]:
    cams: Counter = Counter()
    mimes: Counter = Counter()
    files: list[str] = []
    snaps = frames = 0
    spans = []
    for e in ep.events if ep else []:
        if e.type == "annotation" and e.code == "CAMERA_SNAPSHOT" and e.message:
            cams[e.source or "camera"] += 1
            mimes[CAMERA_MIMES.get(Path(e.message).suffix.lower(), "unknown")] += 1
            files.append(e.message)
            snaps += 1
    for k, s in ep.streams.items() if ep else []:
        m = re.fullmatch(r"obs\.(.+)\.frame", k)
        if m:
            ff, ext = _frame_files(ep, m.group(1), s)
            cams[m.group(1)] += len(s.t)
            mimes[CAMERA_MIMES.get(ext, "unknown")] += len(s.t)
            frames += len(s.t)
            files += ff
        if len(s.t) > 1:
            spans += _finite([s.t[-1] - s.t[0]])
    if row["start_time"] is not None and row["end_time"] is not None and math.isfinite(row["end_time"] - row["start_time"]):
        dur, basis = row["end_time"] - row["start_time"], "start_end_time"
    else:
        dur, basis = (max(spans), "stream_span") if spans else (None, None)
    o = ep.outcome if ep else None
    return {"cameras": dict(cams), "mimes": dict(mimes), "files": files, "snapshots": snaps, "frame_samples": frames,
            "duration_s": dur, "duration_basis": basis, "unreadable": ep is None, "row_synthetic": bool(row["synthetic"]),
            "adapter": ep.source.adapter if ep else None, "synthetic_note": ep.source.synthetic_note if ep else None,
            "embodiment": ep.robot.embodiment if ep and ep.robot else None,
            "verifier_id": o.verifier_id if o else None, "provenance": o.provenance if o else None,
            "dry_run": _dry_run(ep), "capture_note": ep.extra.get("capture") if ep else None,
            "safety_refusals": sum(1 for e in ep.events if e.code == "SAFETY_REFUSAL") if ep else 0,
            "frames": len(ep.streams[FRAME_STREAM].t) if ep and FRAME_STREAM in ep.streams else None}


def _dataset_facts(d: dict[str, Any], rows: list[dict[str, Any]], ef: list[dict[str, Any]]) -> dict[str, Any]:
    cams: Counter = Counter()
    for f in ef:
        cams.update(f["cameras"])
    starts = _finite(r["start_time"] for r in rows)
    ends = _finite(r["end_time"] for r in rows)
    span = {"start": min(starts) if starts else None, "end": max(ends) if ends else None}
    span.update(start_iso=_iso(span["start"]), end_iso=_iso(span["end"]),
                hours=round((span["end"] - span["start"]) / 3600, 3) if starts and ends else None)
    valid = [r for r in rows if r["outcome"] in VALID_OUTCOMES]
    rep = store.loads(d["import_report"]) or {}
    distinct = lambda col: sorted({r[col] for r in rows if r[col] is not None})  # noqa: E731
    return {"dataset_id": d["dataset_id"], "name": d["name"], "kind": d["kind"], "adapter": d["adapter"],
            "synthetic": bool(d["synthetic"]), "note": d["note"], "created_at": d["created_at"], "status": d["status"],
            "source_uri": d["source_uri"], "n_records": d["n_records"], "n_rejected": d["n_rejected"],
            "episodes": len(rows), "real_episodes": sum(1 for r in rows if not r["synthetic"]),
            "synthetic_episodes": sum(1 for r in rows if r["synthetic"]),
            "unreadable_artifacts": sum(1 for f in ef if f["unreadable"]),
            "workcells": distinct("workcell_id"), "devices": distinct("device_id"), "operators": distinct("operator_id"),
            "sessions": len(distinct("session_id")), "embodiments": sorted({f["embodiment"] for f in ef if f["embodiment"]}),
            "cameras": dict(sorted(cams.items())), "time_span": span,
            "success_rate": _rate(sum(r["outcome"] == "success" for r in valid), len(valid)),
            "episode_adapters": dict(Counter(_fmt(f["adapter"]) for f in ef)),
            "rule_settings": rep.get("rule_settings") or {}, "ruleset_version": rep.get("ruleset_version")}


def _streams(eps: list[Episode]) -> dict[str, Any]:
    acc: dict[str, dict[str, Any]] = {}
    dts: dict[str, list[float]] = defaultdict(list)
    for ep in eps:
        for k, s in ep.streams.items():
            a = acc.setdefault(k, {"kind": s.kind, "episodes": 0, "samples": 0, "dims": set(), "units": set(), "declared_rate_hz": set(), "sources": set()})
            a["episodes"] += 1
            a["samples"] += len(s.t)
            a["dims"].add(s.dims)
            for key, v in (("units", s.units), ("declared_rate_hz", s.expected_rate_hz), ("sources", s.source)):
                if v is not None:
                    a[key].add(v)
            if len(s.t) > 1:
                dts[k] += [b - x for x, b in zip(s.t, s.t[1:]) if b > x and math.isfinite(b - x)]
    out = {}
    for k, a in sorted(acc.items()):
        out[k] = {"kind": a["kind"], "episodes": a["episodes"], "samples": a["samples"], "dims": sorted(a["dims"]),
                  "units": sorted(a["units"]), "declared_rate_hz": sorted(a["declared_rate_hz"]), "sources": sorted(a["sources"])[:5],
                  "observed_median_rate_hz": round(1.0 / median(dts[k]), 3) if dts[k] else None}
    return out


def _quality(dataset_ids: list[str]) -> dict[str, Any]:
    q = ",".join("?" * len(dataset_ids))
    by_rule: dict[str, dict[str, Any]] = {}
    for r in store.query(f"SELECT rule_id, severity, COUNT(*) n, SUM(needs_review) review FROM quality_findings "
                         f"WHERE dataset_id IN ({q}) GROUP BY rule_id, severity ORDER BY rule_id", dataset_ids):
        d = by_rule.setdefault(_fmt(r["rule_id"]), {"findings": 0, "episodes": 0, "needs_review": 0, "by_severity": {},
                                                    "description": RULES[r["rule_id"]].description if r["rule_id"] in RULES
                                                    else DATASET_RULES.get(r["rule_id"], NOT_RECORDED)})
        d["findings"] += r["n"]
        d["needs_review"] += r["review"] or 0
        d["by_severity"][_fmt(r["severity"])] = r["n"]
    for r in store.query(f"SELECT rule_id, COUNT(DISTINCT episode_id) eps FROM quality_findings WHERE dataset_id IN ({q}) GROUP BY rule_id", dataset_ids):
        by_rule[_fmt(r["rule_id"])]["episodes"] = r["eps"]  # distinct per rule, not summed over severities
    sev = Counter()
    for d in by_rule.values():
        sev.update(d["by_severity"])
    affected = store.query(f"SELECT COUNT(DISTINCT episode_id) n FROM quality_findings WHERE dataset_id IN ({q}) AND severity<>'info'", dataset_ids)[0]["n"]
    incs = store.query(f"SELECT kind, category, status, COUNT(*) n, SUM(n_episodes) eps FROM incidents WHERE dataset_id IN ({q}) "
                       f"GROUP BY kind, category, status ORDER BY kind, category", dataset_ids)
    by_cat: dict[str, dict[str, Any]] = {}
    for i in incs:
        c = by_cat.setdefault(_fmt(i["category"]), {"kind": _fmt(i["kind"]), "incidents": 0, "episodes": 0, "by_status": {}})
        c["incidents"] += i["n"]
        c["episodes"] += i["eps"] or 0
        c["by_status"][_fmt(i["status"])] = c["by_status"].get(_fmt(i["status"]), 0) + i["n"]
    return {"ruleset_version": RULESET_VERSION, "findings": sum(sev.values()), "findings_by_rule": by_rule,
            "findings_by_severity": dict(sev), "episodes_with_non_info_findings": affected,
            "incidents": sum(c["incidents"] for c in by_cat.values()), "incidents_by_category": by_cat,
            "incidents_by_kind": dict(sum((Counter({_fmt(i["kind"]): i["n"]}) for i in incs), Counter())),
            "status_meanings": STATUS_MEANINGS, "usable_statuses_default": list(USABLE_STATUSES_DEFAULT)}


def _reviews(dataset_ids: list[str]) -> dict[str, Any]:
    q = ",".join("?" * len(dataset_ids))
    targets = {r["finding_id"]: "finding" for r in store.query(f"SELECT finding_id FROM quality_findings WHERE dataset_id IN ({q})", dataset_ids)}
    targets.update({r["incident_id"]: "incident" for r in store.query(f"SELECT incident_id FROM incidents WHERE dataset_id IN ({q})", dataset_ids)})
    rows = [r for r in store.query("SELECT * FROM reviews ORDER BY created_at, rowid") if r["target_id"] in targets]
    latest = {r["target_id"]: r for r in rows}
    return {"reviews": len(rows), "targets_reviewed": len(latest), "reviewers": len({r["reviewer"] for r in rows}),
            "by_decision_latest": dict(Counter(r["decision"] for r in latest.values())),
            "by_target_kind": dict(Counter(r["target_kind"] for r in latest.values())),
            "by_rule": {k: dict(v) for k, v in sorted(_by_rule(latest.values()).items())},
            "first_review_at": min((r["created_at"] for r in rows), default=None),
            "last_review_at": max((r["created_at"] for r in rows), default=None),
            "human_labels": _human_labels(dataset_ids)}


def _by_rule(reviews) -> dict[str, Counter]:
    out: dict[str, Counter] = defaultdict(Counter)
    for r in reviews:
        if r["rule_id"]:
            out[r["rule_id"]][r["decision"]] += 1
    return out


def _human_labels(dataset_ids: list[str]) -> dict[str, Any]:
    """Human outcome labels (amperes.labels: append-only, latest per episode wins) for these datasets."""
    hs = [labels.summary(d) for d in dataset_ids]
    comp, agree = sum(h["agreement"]["comparable"] for h in hs), sum(h["agreement"]["agree"] for h in hs)
    lo, hi = wilson(agree, comp)
    return {"labels": sum(h["labels_total"] for h in hs), "episodes_labelled": sum(h["labelled"] for h in hs),
            "reviewers": len({r for h in hs for r in h["reviewers"]}),
            "by_outcome_latest": dict(sum((Counter(h["by_outcome"]) for h in hs), Counter())),
            "by_category_latest": dict(sum((Counter(h["by_category"]) for h in hs), Counter())),
            "agreement": {"comparable": comp, "agree": agree, "rate": round(agree / comp, 4) if comp else None,
                          "ci95": [round(lo, 4), round(hi, 4)] if comp else None, "note": labels.AGREEMENT_NOTE}}


def _collection(datasets: list[dict[str, Any]], ef: list[dict[str, Any]]) -> dict[str, Any]:
    robot = [f for f in ef if f["adapter"] and f["adapter"].split("@")[0] in ROBOT_ADAPTERS]
    dry = [f for f in robot if f["dry_run"]]
    dry_real = sum(1 for f in dry if not f["row_synthetic"])
    return {"episode_adapters": dict(Counter(_fmt(f["adapter"]) for f in ef)),
            "import_adapters": dict(Counter(_fmt(d["adapter"]) for d in datasets)),
            "synthetic_notes": sorted({f["synthetic_note"] for f in ef if f["synthetic_note"]}),
            "capture_notes": sorted({f["capture_note"] for f in ef if f["capture_note"]}),
            "robot_sessions": {"episodes": len(robot), "executed": len(robot) - len(dry), "dry_run": len(dry),
                               "dry_run_counted_as_real": dry_real,
                               "safety_refusal_events": sum(f["safety_refusals"] for f in robot),
                               "dry_run_handling": "dry runs carry synthetic_note 'DRY RUN: no motion was executed' and conditions.dry_run=true; "
                                                   "the robot CLI imports them with the SYNTHETIC flag"
                                                   + (f"; WARNING: {dry_real} dry-run episode(s) here are flagged real and counted as real episodes"
                                                      if dry_real else "; none of the dry runs here is counted as a real episode"),
                               "operator_acknowledgement": "required by viam_control before any executed run; the phrase itself is not stored per episode",
                               "safety_model": SAFETY_MODEL} if robot else None}


def _limitations(f: dict[str, Any]) -> list[str]:
    st, comp, coll = f["stats"], f["composition"], f["collection"]
    out = []
    if st["unreadable_artifacts"]:
        out.append(f"{st['unreadable_artifacts']} of {st['episodes']} episode artifact(s) could not be read; stream, camera, adapter and "
                   "duration facts exclude them, store-row facts (task, outcome, quality status) include them.")
    sr = st["success_rate"]
    if sr:
        w = sr["ci95"][1] - sr["ci95"][0]
        out.append(f"Sample size: {st['episodes']} episodes, {sr['valid_outcomes']} with a valid outcome; the 95% interval on the "
                   f"success rate spans {100 * w:.0f} percentage points. Treat rates as directional" + (" (n < 20)." if sr["valid_outcomes"] < 20 else "."))
    else:
        out.append(f"Sample size: {st['episodes']} episodes and no valid outcome labels, so no success rate can be stated.")
    dev = sorted({d for ds in f["datasets"] for d in ds["devices"]})
    wc = sorted({w for ds in f["datasets"] for w in ds["workcells"]})
    out.append(f"Embodiment coverage: {len(dev)} device id(s) ({_fmt(dev)}) on {len(wc)} workcell(s) ({_fmt(wc)}); "
               f"embodiments {_fmt(sorted({e for ds in f['datasets'] for e in ds['embodiments']}))}. Results do not transfer across arms or cells.")
    pol = list(comp["by_policy"])
    if any("scripted" in p for p in pol):
        out.append(f"Policies are scripted ({_fmt([p for p in pol if 'scripted' in p])}): hand-written motion sequences, not a learned policy. "
                   "Action streams show what the script commanded, not demonstrations from a human or a trained model.")
    elif pol:
        out.append(f"Policies recorded: {_fmt(pol)}; whether they are learned or scripted is {NOT_RECORDED}.")
    im = comp["images"]
    snaps, frames = im["snapshot_events"], im["frame_stream_samples"]
    if snaps or frames:
        parts = [f"Vision: {snaps} camera snapshot(s) at scripted moments and {frames} frame-stream sample(s) (one low-rate video frame per sample)"]
        if not frames:
            parts.append("no continuous video is included")
        if im["by_mime"].get(CAMERA_MIMES[".dep"]):
            parts.append(f"depth files ({CAMERA_MIMES['.dep']}) are raw Viam depth maps, not point clouds")
        found, ref = st["images"]["found_on_disk"], st["images"]["value"]
        if found is not None and found < ref:
            parts.append(f"only {found} of {ref} referenced image files were found at the source path")
        out.append("; ".join(parts) + ".")
    else:
        out.append("Vision: no camera images are referenced by these episodes.")
    ver = comp["by_verifier"]
    prov = comp["by_outcome_provenance"]
    for vid, n in ver.items():
        out.append(f"Outcome verifier '{vid}' ({n} episodes): {VERIFIER_NOTES.get(vid, 'independence ' + NOT_RECORDED)}.")
    if prov.get("source_label"):
        out.append(f"{prov['source_label']} episode(s) carry outcomes asserted by the source label only; nothing independent checked them.")
    if prov.get("unknown"):
        out.append(f"{prov['unknown']} episode(s) have unknown outcome provenance (e.g. read-only capture or dry run); they cannot be counted as success or failure.")
    if st["synthetic_episodes"]:
        out.append(f"{st['synthetic_episodes']} of {st['episodes']} episodes are SYNTHETIC (simulator output, generated fixtures or dry runs). "
                   "They show tool behaviour, not physical-robot behaviour, and must not be mixed into real-robot statistics.")
    rs = coll["robot_sessions"]
    if rs and rs["executed"]:
        out.append("Real-arm episodes were executed inside a taught safety box with scripted motion (pick-place a<->b or a workspace scan); "
                   "the workspace, objects and lighting were not varied.")
    if rs and rs["dry_run_counted_as_real"]:
        out.append(f"{rs['dry_run_counted_as_real']} dry-run episode(s) (no motion executed) are flagged real in the store; "
                   "exclude them from real-robot statistics.")
    out.append(f"Usable-data statuses by default: {_fmt(f['quality']['usable_statuses_default'])}; 'valid_failed_task' episodes are failures "
               "with clean data and are intended for training on failure, not to be discarded.")
    return out


def build_card(dataset_ids: list[str], out_path: Path | None = None, title: str | None = None) -> dict[str, Any]:
    ids = list(dict.fromkeys([dataset_ids] if isinstance(dataset_ids, str) else dataset_ids))
    if not ids:
        raise ValueError("at least one dataset id is required")
    q = ",".join("?" * len(ids))
    ds = store.query(f"SELECT * FROM datasets WHERE dataset_id IN ({q})", ids)
    if len(ds) != len(ids):
        raise ValueError(f"unknown dataset id(s): {sorted(set(ids) - {d['dataset_id'] for d in ds})}")
    ds.sort(key=lambda d: ids.index(d["dataset_id"]))
    rows = store.query(f"SELECT * FROM episodes WHERE dataset_id IN ({q}) ORDER BY dataset_id, episode_id", ids)
    eps = [_load(r) for r in rows]
    ef = [_episode_facts(e, r) for e, r in zip(eps, rows)]
    bad = [r["episode_id"] for r, e in zip(rows, eps) if e is None]
    by_ds = defaultdict(list)
    for i, r in enumerate(rows):
        by_ds[r["dataset_id"]].append(i)
    datasets = [_dataset_facts(d, [rows[i] for i in by_ds[d["dataset_id"]]], [ef[i] for i in by_ds[d["dataset_id"]]]) for d in ds]
    valid = [r for r in rows if r["outcome"] in VALID_OUTCOMES]
    frames = [f["frames"] for f in ef if f["frames"] is not None]
    images = Counter()
    mimes = Counter()
    for f in ef:
        images.update(f["cameras"])
        mimes.update(f["mimes"])
    on_disk = _images_on_disk(ds, rows, ef)
    count = lambda col: dict(Counter(_fmt(r[col]) for r in rows).most_common())  # noqa: E731
    fail = Counter(_fmt(r["category"] or r["reason"]) for r in rows if r["outcome"] in ("failure", "timeout", "invalid"))
    facts: dict[str, Any] = {
        "card_version": CARD_VERSION, "title": title or ((ds[0]["name"] or ds[0]["dataset_id"]) if len(ds) == 1 else f"{len(ds)} datasets"),
        "generated_at": now_iso(), "dataset_ids": ids,
        "stats": {"datasets": len(ds), "episodes": len(rows),
                  "frames": {"value": sum(frames) if frames else None, "stream": FRAME_STREAM, "episodes_with_stream": len(frames)},
                  "images": {"value": sum(images.values()), "found_on_disk": on_disk},
                  "real_episodes": sum(1 for r in rows if not r["synthetic"]), "synthetic_episodes": sum(1 for r in rows if r["synthetic"]),
                  "unreadable_artifacts": len(bad), "unreadable_episode_ids": bad[:20],
                  "success_rate": _rate(sum(r["outcome"] == "success" for r in valid), len(valid))},
        "datasets": datasets,
        "composition": {"by_task": dict(Counter(_fmt(_ref(r["task_id"], r["task_version"])) for r in rows).most_common()),
                        "by_policy": dict(Counter(_fmt(_ref(r["policy_id"], r["policy_version"])) for r in rows).most_common()),
                        "by_config": count("config_id"), "by_outcome": count("outcome"), "by_outcome_provenance": count("outcome_provenance"),
                        "by_failure_category": dict(fail.most_common()), "by_quality_status": count("quality_status"),
                        "by_verifier": dict(Counter(f["verifier_id"] for f in ef if f["verifier_id"]).most_common()),
                        "verified": {"n": sum(1 for r in rows if r["verified"]), "of": len(rows),
                                     "share": round(sum(1 for r in rows if r["verified"]) / len(rows), 4) if rows else None},
                        "streams": _streams([e for e in eps if e is not None]),
                        "images": {"by_camera": dict(sorted(images.items())), "by_mime": dict(sorted(mimes.items())),
                                   "snapshot_events": sum(f["snapshots"] for f in ef), "frame_stream_samples": sum(f["frame_samples"] for f in ef)},
                        "duration_s": _dist([f["duration_s"] for f in ef if f["duration_s"] is not None]),
                        "duration_basis": dict(Counter(f["duration_basis"] for f in ef if f["duration_basis"]))},
        "collection": _collection(ds, ef),
        "quality": _quality(ids),
        "review": _reviews(ids),
        "provenance": {**environment(), "ruleset_version": RULESET_VERSION, "dataset_ids": ids,
                       "export_timestamp": None, "imports": [{"dataset_id": d["dataset_id"], "created_at": d["created_at"],
                                                              "adapter": d["adapter"], "source_uri": d["source_uri"]} for d in ds],
                       "episode_content_digests": sum(1 for r in rows if r["content_digest"])},
        "license": "[license]", "contact": "[contact]",
    }
    facts["provenance"]["export_timestamp"] = facts["generated_at"]
    facts["uses_and_limitations"] = _limitations(facts)
    md = markdown(facts)
    out = {"markdown": md, "facts": facts, "path": None}
    if out_path is not None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md, encoding="utf-8")
        Path(str(out_path) + ".json").write_text(json.dumps(facts, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")
        out["path"] = str(out_path)
    return out


def _images_on_disk(ds, rows, ef) -> int | None:
    """Count referenced image files that exist under <source_uri>/images; bare file names only (as labels.image_path)."""
    src = {d["dataset_id"]: d["source_uri"] for d in ds}
    n, checked = 0, False
    for r, f in zip(rows, ef):
        base = src.get(r["dataset_id"])
        if not f["files"] or not base or not Path(base).is_dir():
            continue
        checked = True
        n += sum(1 for fn in f["files"] if Path(fn).name == fn and not fn.startswith(".") and (Path(base) / "images" / fn).is_file())
    return n if checked else None


def _cell(v: Any) -> str:
    return _fmt(v).replace("|", "\\|").replace("\n", " ")


def _table(header: list[str], body: list[list[Any]]) -> list[str]:
    if not body:
        return [f"_{NOT_RECORDED}_"]
    return [f"| {' | '.join(header)} |", f"|{'---|' * len(header)}", *[f"| {' | '.join(_cell(c) for c in row)} |" for row in body]]


def _counts(d: dict[str, int], label: str) -> list[str]:
    return _table([label, "episodes"], [[k, v] for k, v in d.items()])


def markdown(f: dict[str, Any]) -> str:
    st, comp, q, rv, pv = f["stats"], f["composition"], f["quality"], f["review"], f["provenance"]
    flag = ("EMPTY (no episodes)" if not st["episodes"] else "MIXED (real and synthetic)" if st["real_episodes"] and st["synthetic_episodes"]
            else "SYNTHETIC" if st["synthetic_episodes"] else "REAL")
    sr = st["success_rate"]
    L = [f"# Dataset card: {f['title']}", "", f"> {flag} data. Facts below come only from the Amperes store; anything it does not hold is marked \"{NOT_RECORDED}\".",
         "", f"Generated {f['generated_at']} · card {f['card_version']} · ruleset {q['ruleset_version']} · code {pv['code_digest']}", "",
         *_table(["stat", "value"], [
             ["datasets", st["datasets"]], ["episodes", st["episodes"]],
             [f"frames ({st['frames']['stream']} samples)", f"{st['frames']['value']} across {st['frames']['episodes_with_stream']} episodes" if st["frames"]["value"] is not None else None],
             ["images referenced", st["images"]["value"]], ["images found at source path", st["images"]["found_on_disk"]],
             ["real episodes", st["real_episodes"]], ["synthetic episodes", st["synthetic_episodes"]],
             ["episode artifacts unreadable", st["unreadable_artifacts"]],
             ["success rate (Wilson 95% CI)", f"{_pct(sr['rate'])} = {sr['successes']}/{sr['valid_outcomes']} valid outcomes, CI [{_pct(sr['ci95'][0])}, {_pct(sr['ci95'][1])}]" if sr else "no valid outcomes"]]),
         "", "## 1. Summary", ""]
    for d in f["datasets"]:
        ts = d["time_span"]
        L += [f"### `{d['dataset_id']}` — {_fmt(d['name'])} [{'EMPTY' if not d['episodes'] else 'SYNTHETIC' if d['synthetic'] else 'REAL'}]", "",
              f"- What: {_fmt(d['kind'])} via {_fmt(d['adapter'])}; {d['episodes']} episodes ({d['real_episodes']} real, {d['synthetic_episodes']} synthetic"
              + (f", {d['unreadable_artifacts']} unreadable" if d["unreadable_artifacts"] else "") + f"); note: {_fmt(d['note'])}",
              f"- Who/where: workcells {_fmt(d['workcells'])}; devices {_fmt(d['devices'])}; operators (pseudonymous) {_fmt(d['operators'])}; sessions {d['sessions'] or NOT_RECORDED}",
              f"- When: {_fmt(ts['start_iso'])} to {_fmt(ts['end_iso'])}" + (f" ({_fmt(ts['hours'])} h span)" if ts["hours"] is not None else ""),
              f"- Embodiment: {_fmt(d['embodiments'])}",
              f"- Cameras: {_fmt([f'{k} ({v} images)' for k, v in d['cameras'].items()])}",
              f"- Success rate: " + (f"{_pct(d['success_rate']['rate'])} ({d['success_rate']['successes']}/{d['success_rate']['valid_outcomes']}, CI [{_pct(d['success_rate']['ci95'][0])}, {_pct(d['success_rate']['ci95'][1])}])" if d["success_rate"] else "no valid outcomes"),
              ""]
    L += ["## 2. Composition", "", "**Episodes by task**", "", *_counts(comp["by_task"], "task"), "", "**Episodes by policy**", "", *_counts(comp["by_policy"], "policy"),
          "", "**Episodes by config**", "", *_counts(comp["by_config"], "config"), "", "**Episodes by outcome**", "", *_counts(comp["by_outcome"], "outcome"),
          "", "**Episodes by outcome provenance**", "", *_counts(comp["by_outcome_provenance"], "provenance"),
          "", "**Failure category (diagnosis category, else outcome reason; failure/timeout/invalid episodes)**", "", *_counts(comp["by_failure_category"], "category"),
          "", "**Episodes by quality status**", "", *_counts(comp["by_quality_status"], "quality_status"),
          "", f"**Verified outcomes:** {comp['verified']['n']} of {comp['verified']['of']} ({_pct(comp['verified']['share'])}); verifiers: "
          + (", ".join(f"{k} ({v})" for k, v in comp["by_verifier"].items()) or NOT_RECORDED),
          "", "**Streams present**", "",
          *_table(["stream", "kind", "episodes", "samples", "dims", "units", "declared Hz", "observed median Hz"],
                  [[k, s["kind"], s["episodes"], s["samples"], s["dims"], s["units"], s["declared_rate_hz"], s["observed_median_rate_hz"]] for k, s in comp["streams"].items()]),
          "", "**Images per camera**", "", *_table(["camera", "images"], [[k, v] for k, v in comp["images"]["by_camera"].items()]),
          "", f"By type: {_fmt([f'{k} ({v})' for k, v in comp['images']['by_mime'].items()])}; snapshot events {comp['images']['snapshot_events']}, frame-stream samples {comp['images']['frame_stream_samples']}.",
          "", "**Episode duration (s)**", ""]
    du = comp["duration_s"]
    L += [f"- n {du['n']}, min {_fmt(du['min'])}, median {_fmt(du['median'])}, mean {_fmt(du['mean'])}, max {_fmt(du['max'])}, total {_fmt(du['total'] / 3600, 3)} h "
          f"(basis: {_fmt([f'{k} ({v})' for k, v in comp['duration_basis'].items()])})" if du else f"- {NOT_RECORDED}"]
    co = f["collection"]
    L += ["", "## 3. Collection process", "",
          f"- Episode adapters (source.adapter): {_fmt([f'{k} ({v})' for k, v in co['episode_adapters'].items()])}",
          f"- Import adapters (datasets): {_fmt([f'{k} ({v})' for k, v in co['import_adapters'].items()])}",
          f"- Synthetic notes: {_fmt(co['synthetic_notes'])}", f"- Capture notes: {_fmt(co['capture_notes'])}"]
    rs = co["robot_sessions"]
    if rs:
        L += [f"- Robot sessions: {rs['episodes']} episodes ({rs['executed']} executed, {rs['dry_run']} dry run); safety refusal events {rs['safety_refusal_events']}",
              f"  - Dry runs: {rs['dry_run_handling']}", f"  - Operator acknowledgement: {rs['operator_acknowledgement']}",
              "  - Safety model (docs/ROBOT_LAB.md):", *[f"    - {s}" for s in rs["safety_model"]]]
    else:
        L.append(f"- Robot sessions: none (no episodes from {', '.join(ROBOT_ADAPTERS)})")
    for d in f["datasets"]:
        if d["rule_settings"]:
            L.append(f"- Rule settings for `{d['dataset_id']}`: {json.dumps(d['rule_settings'], sort_keys=True)}")
    L += ["", "## 4. Preprocessing and quality control", "",
          f"Ruleset {q['ruleset_version']}: {q['findings']} findings, {q['episodes_with_non_info_findings']} episodes with non-info findings; "
          f"by severity {_fmt([f'{k} ({v})' for k, v in q['findings_by_severity'].items()])}.", "", "**Findings by rule**", "",
          *_table(["rule", "findings", "episodes", "needs review", "by severity", "checks"],
                  [[k, v["findings"], v["episodes"], v["needs_review"], ", ".join(f"{s} {n}" for s, n in v["by_severity"].items()), v["description"]]
                   for k, v in q["findings_by_rule"].items()]),
          "", f"**Incidents** ({q['incidents']} total)", "",
          *_table(["category", "kind", "incidents", "episodes", "status"],
                  [[k, v["kind"], v["incidents"], v["episodes"], ", ".join(f"{s} {n}" for s, n in v["by_status"].items())] for k, v in q["incidents_by_category"].items()]),
          "", "**Quality status meanings** (precedence top to bottom)", "", *[f"- `{k}`: {v}" for k, v in q["status_meanings"].items()],
          "", f"Usable by default: {_fmt(q['usable_statuses_default'])}. No composite score is computed; dimensions are reported separately.",
          "", "## 5. Human review", ""]
    hl = rv["human_labels"]
    if not rv["reviews"] and not hl["labels"]:
        L.append("- No human review recorded for these datasets (no finding/incident reviews, no human outcome labels).")
    if rv["reviews"]:
        L += [f"- {rv['reviews']} review records on {rv['targets_reviewed']} targets by {rv['reviewers']} reviewer(s), {_fmt(rv['first_review_at'])} to {_fmt(rv['last_review_at'])}",
              f"- Latest decision per target: {_fmt([f'{k} ({v})' for k, v in rv['by_decision_latest'].items()])}",
              f"- Targets: {_fmt([f'{k} ({v})' for k, v in rv['by_target_kind'].items()])}",
              *[f"- {k}: {_fmt([f'{d} ({n})' for d, n in v.items()])}" for k, v in rv["by_rule"].items()]]
    if hl["labels"]:
        ag = hl["agreement"]
        L += [f"- Human outcome labels: {hl['labels']} on {hl['episodes_labelled']} episode(s) by {hl['reviewers']} reviewer(s); latest per episode: "
              f"{_fmt([f'{k} ({v})' for k, v in hl['by_outcome_latest'].items()])}; categories {_fmt([f'{k} ({v})' for k, v in hl['by_category_latest'].items()])}",
              f"- Agreement with the stored outcome: {ag['agree']}/{ag['comparable']} comparable ({_pct(ag['rate'])}"
              + (f", CI [{_pct(ag['ci95'][0])}, {_pct(ag['ci95'][1])}]" if ag["ci95"] else "") + f"); {ag['note']}"]
    L += ["", "## 6. Uses and limitations", "", *[f"- {x}" for x in f["uses_and_limitations"]],
          "", "## 7. Provenance", "",
          f"- Dataset ids: {_fmt(pv['dataset_ids'])}", f"- Export timestamp: {pv['export_timestamp']}",
          f"- Amperes {pv['amperes_version']}, code digest {pv['code_digest']}, git {_fmt(pv['git_revision'])}, ruleset {pv['ruleset_version']}",
          f"- Python {pv['python']} on {pv['platform']} ({pv['machine']})",
          f"- Dependencies: {_fmt([f'{k} {_fmt(v)}' for k, v in pv['dependencies'].items()])}",
          f"- Episodes with content digests: {pv['episode_content_digests']}",
          *[f"- Import `{i['dataset_id']}`: {_fmt(i['created_at'])} via {_fmt(i['adapter'])} from {_fmt(i['source_uri'])}" for i in pv["imports"]],
          "", "## 8. License and contact", "", f"- License: {f['license']}", f"- Contact: {f['contact']}", ""]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m amperes.dataset_card", description="Datasheet-style dataset card from the Amperes store")
    p.add_argument("dataset_ids", nargs="+")
    p.add_argument("--out", type=Path, help="write <out> (Markdown) and <out>.json")
    p.add_argument("--title")
    a = p.parse_args(argv)
    try:
        r = build_card(a.dataset_ids, a.out, a.title)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(r["path"] or r["markdown"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
