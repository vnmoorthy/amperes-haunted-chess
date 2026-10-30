"""SQLite experiment/provenance store plus the artifact directory.

Location: `$AMPERES_HOME` (default `./amperes_data`) containing `amperes.db` and `artifacts/`.
Summary records live in SQLite; raw episode artifacts are gzip JSON files under `artifacts/`.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from amperes.provenance import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
  dataset_id TEXT PRIMARY KEY, name TEXT, kind TEXT, adapter TEXT, source_uri TEXT, created_at TEXT,
  synthetic INTEGER, note TEXT, n_records INTEGER DEFAULT 0, n_rejected INTEGER DEFAULT 0, status TEXT,
  import_report TEXT);
CREATE TABLE IF NOT EXISTS experiments (
  experiment_id TEXT PRIMARY KEY, name TEXT, config TEXT, created_at TEXT, status TEXT, error TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, experiment_id TEXT, kind TEXT, policy_label TEXT, policy_config TEXT,
  verifier_config TEXT, split TEXT, n_episodes INTEGER, status TEXT, error TEXT, started_at TEXT,
  finished_at TEXT, wall_s REAL, environment TEXT);
CREATE TABLE IF NOT EXISTS conditions (
  condition_id TEXT PRIMARY KEY, parent_id TEXT, split TEXT, changed TEXT, env_params TEXT, seed INTEGER,
  description TEXT);
CREATE TABLE IF NOT EXISTS episodes (
  episode_id TEXT PRIMARY KEY, dataset_id TEXT, run_id TEXT, condition_id TEXT, split TEXT, seed INTEGER,
  task_id TEXT, task_version TEXT, policy_id TEXT, policy_version TEXT, policy_label TEXT, session_id TEXT,
  workcell_id TEXT, device_id TEXT, operator_id TEXT, outcome TEXT, outcome_provenance TEXT, verified INTEGER,
  reason TEXT, category TEXT, rule_id TEXT, steps INTEGER, sim_duration_s REAL, wall_time_s REAL,
  terminated_by TEXT, collision INTEGER, replay_class TEXT, quality_status TEXT, artifact_path TEXT,
  content_digest TEXT, start_time REAL, end_time REAL, synthetic INTEGER, created_at TEXT, config_id TEXT,
  attempt_index INTEGER);
CREATE INDEX IF NOT EXISTS ix_ep_run ON episodes(run_id);
CREATE INDEX IF NOT EXISTS ix_ep_ds ON episodes(dataset_id);
CREATE TABLE IF NOT EXISTS interventions (
  intervention_id TEXT PRIMARY KEY, experiment_id TEXT, name TEXT, target_category TEXT, delta TEXT,
  rationale TEXT, policy_label TEXT, dev_run_id TEXT, dev_summary TEXT, selected INTEGER DEFAULT 0,
  role TEXT, frozen_at TEXT);
CREATE TABLE IF NOT EXISTS comparisons (
  comparison_id TEXT PRIMARY KEY, experiment_id TEXT, split TEXT, baseline_run TEXT, treatment_run TEXT,
  treatment_name TEXT, summary TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS reproductions (
  repro_id TEXT PRIMARY KEY, experiment_id TEXT, original_episode_id TEXT, mode TEXT, new_run_id TEXT,
  new_episode_id TEXT, replay_class TEXT, eligible INTEGER, classification TEXT, criteria TEXT,
  comparison TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS regression_suites (
  suite_id TEXT PRIMARY KEY, experiment_id TEXT, name TEXT, created_at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS regression_cases (
  case_id TEXT PRIMARY KEY, suite_id TEXT, kind TEXT, condition_id TEXT, env_params TEXT, seed INTEGER,
  task_id TEXT, task_version TEXT, source_episode_id TEXT, source_policy_label TEXT, source_outcome TEXT,
  expected TEXT, provenance TEXT, split TEXT, incident_id TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS regression_results (
  result_id TEXT PRIMARY KEY, suite_id TEXT, case_id TEXT, run_id TEXT, policy_label TEXT, episode_id TEXT,
  outcome TEXT, passed INTEGER, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS quality_findings (
  finding_id TEXT PRIMARY KEY, dataset_id TEXT, episode_id TEXT, rule_id TEXT, rule_version TEXT,
  dimension TEXT, severity TEXT, record_ref TEXT, t REAL, observed TEXT, expected TEXT, explanation TEXT,
  recommendation TEXT, deterministic INTEGER, needs_review INTEGER, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_qf_ep ON quality_findings(episode_id);
CREATE TABLE IF NOT EXISTS incidents (
  incident_id TEXT PRIMARY KEY, dataset_id TEXT, group_key TEXT, category TEXT, kind TEXT, title TEXT,
  severity TEXT, status TEXT, first_seen REAL, last_seen REAL, n_occurrences INTEGER, n_episodes INTEGER,
  n_sessions INTEGER, scope TEXT, evidence TEXT, grouping_reason TEXT, facts TEXT, hypotheses TEXT,
  next_actions TEXT, owner TEXT, resolution TEXT, regression_case_ids TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS incident_members (
  incident_id TEXT, episode_id TEXT, ref_kind TEXT, ref_id TEXT, t REAL, session_id TEXT);
CREATE TABLE IF NOT EXISTS incident_history (
  incident_id TEXT, at TEXT, actor TEXT, from_status TEXT, to_status TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS ops_events (
  event_id TEXT PRIMARY KEY, dataset_id TEXT, t REAL, t_end REAL, workcell_id TEXT, device_id TEXT,
  session_id TEXT, event_class TEXT, event_type TEXT, code TEXT, message TEXT, episode_id TEXT,
  incident_ref TEXT, provenance TEXT, content_digest TEXT);
CREATE INDEX IF NOT EXISTS ix_ops_ds ON ops_events(dataset_id);
CREATE TABLE IF NOT EXISTS reviews (
  review_id TEXT PRIMARY KEY, target_kind TEXT, target_id TEXT, rule_id TEXT, rule_version TEXT,
  decision TEXT, reviewer TEXT, notes TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS workflow_tasks (
  task_id TEXT PRIMARY KEY, study_id TEXT, reviewer TEXT, condition TEXT, sample_id TEXT, task_type TEXT,
  started_at TEXT, finished_at TEXT, duration_s REAL, confirmed_findings INTEGER, incorrect_findings INTEGER,
  missing_info TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS human_labels (
  label_id TEXT PRIMARY KEY, episode_id TEXT, dataset_id TEXT, outcome TEXT, category TEXT, note TEXT, reviewer TEXT,
  created_at TEXT, agrees_with_verifier INTEGER);
CREATE INDEX IF NOT EXISTS ix_hl_ep ON human_labels(episode_id);
"""

_LOCK = threading.Lock()


def home() -> Path:
    return Path(os.environ.get("AMPERES_HOME", "amperes_data")).resolve()


def artifacts_dir() -> Path:
    return home() / "artifacts"


def db_path() -> Path:
    return home() / "amperes.db"


@contextmanager
def connect():
    home().mkdir(parents=True, exist_ok=True)
    with _LOCK:
        con = sqlite3.connect(db_path(), timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.executescript(SCHEMA)
            _migrate(con)
            yield con
            con.commit()
        finally:
            con.close()


MIGRATIONS = {"episodes": {"config_id": "TEXT", "attempt_index": "INTEGER"}}


def _migrate(con: sqlite3.Connection) -> None:
    """Add columns introduced after a database was created (additive only; never drops data)."""
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def _enc(v: Any) -> Any:
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, sort_keys=True, default=str)
    if isinstance(v, bool):
        return int(v)
    return v


def insert(table: str, row: dict[str, Any], replace: bool = False) -> None:
    cols = list(row)
    sql = f"INSERT {'OR REPLACE ' if replace else ''}INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    with connect() as con:
        con.execute(sql, [_enc(row[c]) for c in cols])


def insert_many(table: str, rows: Iterable[dict[str, Any]], replace: bool = False, ignore: bool = False) -> int:
    rows = list(rows)
    if not rows:
        return 0
    cols = list(rows[0])
    sql = f"INSERT {'OR REPLACE ' if replace else 'OR IGNORE ' if ignore else ''}INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    with connect() as con:
        con.executemany(sql, [[_enc(r.get(c)) for c in cols] for r in rows])
    return len(rows)


def query(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    with connect() as con:
        return [dict(r) for r in con.execute(sql, list(params)).fetchall()]


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with connect() as con:
        return con.execute(sql, list(params)).rowcount


def loads(v: str | None) -> Any:
    return json.loads(v) if v else None


def safe_artifact_path(path: str | Path) -> Path:
    """Resolve a path and refuse anything outside the artifact directory."""
    p = Path(path).resolve()
    root = artifacts_dir().resolve()
    if root != p and root not in p.parents:
        raise PermissionError(f"path outside artifact directory: {p}")
    return p


def record_run_episodes(results: list[dict[str, Any]], conds: list[dict[str, Any]], policy_label: str | None,
                        dataset_id: str) -> None:
    insert_many("conditions", [{
        "condition_id": c["condition_id"], "parent_id": c.get("parent_id"), "split": c.get("split"),
        "changed": c.get("changed"), "env_params": c["env_params"], "seed": c["seed"],
        "description": c.get("description")} for c in conds], ignore=True)
    from amperes.sim.env import TASK_ID, TASK_VERSION

    insert_many("episodes", [{
        "episode_id": r["episode_id"], "dataset_id": dataset_id, "run_id": r["run_id"],
        "condition_id": r["condition_id"], "split": r.get("split"), "seed": r["seed"], "task_id": TASK_ID,
        "task_version": TASK_VERSION, "policy_id": r.get("policy_id"), "policy_version": r.get("policy_version"),
        "policy_label": policy_label, "outcome": r["outcome"], "outcome_provenance": "amperes_verifier",
        "verified": 1, "reason": r["reason"], "category": r["category"], "rule_id": r["rule_id"],
        "steps": r["steps"], "sim_duration_s": r["sim_duration_s"], "wall_time_s": r["wall_time_s"],
        "terminated_by": r["terminated_by"], "collision": r["collision"],
        "replay_class": "A" if r.get("initial_state_digest") else "E", "artifact_path": r["artifact"],
        "synthetic": 1, "created_at": now_iso()} for r in results])


def delete_dataset(dataset_id: str) -> dict[str, int]:
    """Remove a dataset, its episodes, derived findings/incidents/events, and its artifact directory."""
    counts = {}
    inc = [r["incident_id"] for r in query("SELECT incident_id FROM incidents WHERE dataset_id=?", [dataset_id])]
    fnd = [r["finding_id"] for r in query("SELECT finding_id FROM quality_findings WHERE dataset_id=?", [dataset_id])]
    with connect() as con:
        for table, col, ids in (("incident_members", "incident_id", inc), ("incident_history", "incident_id", inc)):
            for i in ids:
                con.execute(f"DELETE FROM {table} WHERE {col}=?", [i])
        n_rev = 0
        for target in fnd + inc:
            n_rev += con.execute("DELETE FROM reviews WHERE target_id=?", [target]).rowcount
        counts["reviews"] = n_rev
        counts["workflow_tasks"] = con.execute("DELETE FROM workflow_tasks WHERE sample_id LIKE ?", [f"{dataset_id}:%"]).rowcount
        for table in ("quality_findings", "incidents", "ops_events", "human_labels", "episodes"):
            counts[table] = con.execute(f"DELETE FROM {table} WHERE dataset_id=?", [dataset_id]).rowcount
        counts["datasets"] = con.execute("DELETE FROM datasets WHERE dataset_id=?", [dataset_id]).rowcount
    counts["artifact_dirs"] = 0
    counts["report_files"] = 0
    for iid in inc:
        for ext in (".json", ".md"):
            f = artifacts_dir() / "reports" / "incidents" / f"{iid}{ext}"
            if f.exists():
                safe_artifact_path(f).unlink()
                counts["report_files"] += 1
    # Single- and multi-dataset pilot reports (directory names join the dataset ids) that include this dataset.
    pilot_dirs = [p for p in (artifacts_dir() / "reports" / "pilot").glob("*") if p.is_dir() and dataset_id in p.name]
    for d in (artifacts_dir() / "datasets" / dataset_id, artifacts_dir() / "runs" / dataset_id, *pilot_dirs):
        if d.exists():
            shutil.rmtree(safe_artifact_path(d))
            counts["artifact_dirs"] += 1
    return counts
