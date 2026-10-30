"""Detector evaluation against the labelled SYNTHETIC defect corpus.

Per defect type d with expected rule set R(d):
  TP = labelled-d episodes where any rule in R(d) fired
  FN = labelled-d episodes where none fired
  FP = episodes NOT labelled with any defect expecting that rule, where the rule fired
Precision/recall are reported with numerator and denominator; a metric with an empty denominator is "not evaluated".
Valid-record preservation: clean episodes (no injected defect) whose status is not invalid/suspicious.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from amperes import store
from amperes.ingest.pipeline import import_dataset
from amperes.provenance import now_iso


def _ratio(num: int, den: int) -> dict[str, Any]:
    return {"value": num / den, "numerator": num, "denominator": den} if den else {"value": "not evaluated", "numerator": num, "denominator": 0}


def evaluate_corpus(corpus_dir: Path) -> dict[str, Any]:
    labels = json.loads((corpus_dir / "labels.json").read_text())["labels"]
    t0 = time.perf_counter()
    rep = import_dataset(corpus_dir, name="SYNTHETIC defect corpus", adapter="jsonl", synthetic=True,
                         note="labelled synthetic fixture for detector evaluation")
    elapsed = time.perf_counter() - t0
    ds = rep["dataset_id"]
    eps = store.query("SELECT episode_id, artifact_path, quality_status FROM episodes WHERE dataset_id=?", [ds])
    from amperes.schema import read_episode

    src_of = {}
    for e in eps:
        src_of[e["episode_id"]] = read_episode(Path(e["artifact_path"])).extra.get("source_episode_id")
    fired = defaultdict(set)
    for f in store.query("SELECT episode_id, rule_id FROM quality_findings WHERE dataset_id=? AND episode_id IS NOT NULL", [ds]):
        fired[src_of.get(f["episode_id"])].add(f["rule_id"])
    status = {src_of[e["episode_id"]]: e["quality_status"] for e in eps}
    by_defect = {}
    all_rules = sorted({r for lab in labels.values() for r in lab["expected_rules"]})
    for d in sorted({x for lab in labels.values() for x in lab["defects"]}):
        ids = [k for k, lab in labels.items() if d in lab["defects"]]
        rules = set(labels[ids[0]]["expected_rules"])
        tp = sum(bool(fired[i] & rules) for i in ids)
        by_defect[d] = {"expected_rules": sorted(rules), "n": len(ids), "detected": tp, "missed": len(ids) - tp,
                        "recall": _ratio(tp, len(ids)), "detectable_by_design": labels[ids[0]]["detectable"],
                        "status_as_expected": sum(status.get(i) == labels[i]["expected_status"] for i in ids)}
    from amperes.quality import RULES

    info_rules = {r for r in {x for s in fired.values() for x in s} if r in RULES and _is_info(r, ds)}
    by_rule, informational = {}, {}
    for r in sorted(set(all_rules) | {x for s in fired.values() for x in s}):
        pos = [k for k, lab in labels.items() if r in lab["expected_rules"]]
        secondary = [k for k, lab in labels.items() if r in lab.get("secondary_rules", []) and k not in pos]
        neg = [k for k in labels if k not in pos and k not in secondary]
        tp = sum(r in fired[k] for k in pos)
        fp = sum(r in fired[k] for k in neg)
        fn = len(pos) - tp
        row = {"tp": tp, "fp": fp, "fn": fn, "precision": _ratio(tp, tp + fp), "recall": _ratio(tp, len(pos)),
               "fp_episode_ids": [k for k in neg if r in fired[k]][:10], "labelled_positives": len(pos),
               "secondary_detections": sum(r in fired[k] for k in secondary)}
        if r in info_rules:
            informational[r] = {"fired_on": sum(r in fired[k] for k in labels), "note": "severity=info; not a defect detector"}
        else:
            by_rule[r] = row
    clean = [k for k, lab in labels.items() if not lab["defects"]]
    kept = sum(status.get(k) not in ("invalid_data", "suspicious") for k in clean)
    clean_failed = [k for k in clean if labels[k]["expected_status"] == "valid_failed_task"]
    out = {
        "evaluated_at": now_iso(), "dataset_id": ds, "corpus": str(corpus_dir), "synthetic": True,
        "n_episodes": len(labels), "n_clean": len(clean), "n_defective": len(labels) - len(clean),
        "by_defect": by_defect, "by_rule": by_rule, "informational_rules": informational,
        "valid_record_preservation": _ratio(kept, len(clean)),
        "valid_failed_tasks_kept_as_valid_failed": _ratio(sum(status.get(k) == "valid_failed_task" for k in clean_failed), len(clean_failed)),
        "clean_false_positive_episodes": [k for k in clean if fired[k] - info_rules],
        "processing_time_s": round(elapsed, 3), "per_episode_ms": round(1000 * elapsed / max(1, len(labels)), 2),
        "import_report": {k: rep[k] for k in ("episodes_imported", "records_rejected", "processing_s")},
        "caveat": "Measured on injected synthetic defects only; says nothing about defect prevalence or detectability in real customer data.",
    }
    return out


def _is_info(rule_id: str, ds: str) -> bool:
    rows = store.query("SELECT DISTINCT severity FROM quality_findings WHERE dataset_id=? AND rule_id=?", [ds, rule_id])
    return bool(rows) and all(r["severity"] == "info" for r in rows)


def markdown(ev: dict[str, Any]) -> str:
    def r(x):
        v = x["value"]
        return f"{v:.3f} ({x['numerator']}/{x['denominator']})" if isinstance(v, float) else f"{v} ({x['numerator']}/{x['denominator']})"

    L = ["# Detector evaluation (SYNTHETIC labelled corpus)", "", f"> {ev['caveat']}", "",
         f"- Episodes: {ev['n_episodes']} ({ev['n_clean']} clean, {ev['n_defective']} with one injected defect)",
         f"- Processing: {ev['processing_time_s']} s total, {ev['per_episode_ms']} ms/episode (import + all rules)",
         f"- Valid-record preservation (clean episodes not marked invalid/suspicious): {r(ev['valid_record_preservation'])}",
         f"- Clean failed tasks kept as `valid_failed_task`: {r(ev['valid_failed_tasks_kept_as_valid_failed'])}", "",
         "## By injected defect", "", "| defect | expected rule | n | detected | recall | detectable by design | status as expected |",
         "|---|---|---|---|---|---|---|"]
    for d, x in ev["by_defect"].items():
        L.append(f"| {d} | {', '.join(x['expected_rules'])} | {x['n']} | {x['detected']} | {r(x['recall'])} | "
                 f"{'yes' if x['detectable_by_design'] else '**no** (no state evidence)'} | {x['status_as_expected']}/{x['n']} |")
    L += ["", "## By rule", "", "| rule | TP | FP | FN | precision | recall | secondary detections* |", "|---|---|---|---|---|---|---|"]
    for k, x in ev["by_rule"].items():
        L.append(f"| {k} | {x['tp']} | {x['fp']} | {x['fn']} | {r(x['precision'])} | {r(x['recall'])} | {x['secondary_detections']} |")
    L += ["", "*Secondary detections: the rule fired on an episode whose fixture construction genuinely created that "
          "condition (declared in labels.json as `secondary_rules`); counted neither as TP nor FP.", "",
          "Informational rules (severity `info`, excluded from precision): " +
          (", ".join(f"{k} fired on {v['fired_on']}" for k, v in ev["informational_rules"].items()) or "none")]
    return "\n".join(L)
