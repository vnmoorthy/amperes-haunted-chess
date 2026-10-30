"""Opt-in MongoDB Atlas sync: a shared, queryable store of episode status, findings, incidents and events across
stations and teams, with Atlas Vector Search over deterministic failure signatures.

Data flow (explicit opt-in: nothing is sent unless you run `atlas sync` with MONGODB_URI set):
- sent: episode index rows (ids, status, outcome label, session/workcell/device ids, times), findings (rule, severity,
  observed/expected, explanation), incidents (facts, hypotheses, history), operational events, signature vectors;
- never sent: raw stream values, environment variables or credentials. Re-running a sync upserts (idempotent).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from amperes import incidents as inc_mod
from amperes import store
from amperes.provenance import now_iso
from amperes.signatures import DIMS, SIGNATURE_VERSION, signatures

DB = "amperes"

VECTOR_INDEX = "failure_signature_index"


def _uri() -> str:
    # Read .env fresh on every call (no os.environ caching): a rotated password in .env takes effect without a restart.
    uri = None
    try:
        from dotenv import dotenv_values

        uri = dotenv_values(Path(__file__).resolve().parents[2] / ".env").get("MONGODB_URI")
    except ImportError:
        pass
    uri = uri or os.environ.get("MONGODB_URI")
    if not uri:
        raise RuntimeError("MONGODB_URI is not set (add it to .env). Atlas sync is opt-in and off by default.")
    return uri


def client():
    from pymongo import MongoClient

    return MongoClient(_uri(), serverSelectionTimeoutMS=10_000, appname="amperes")


def _friendly(fn):
    """Turn driver errors into a short RuntimeError without credentials (auth, network, permissions)."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        from pymongo.errors import OperationFailure, PyMongoError

        try:
            return fn(*a, **kw)
        except OperationFailure as exc:
            msg = str(exc).split(", full error")[0]
            hint = (" — the database user/password in MONGODB_URI was rejected (changed or rotated?). "
                    "Run: .venv/bin/python scripts/set_mongodb_uri.py") if "auth" in msg.lower() else ""
            raise RuntimeError(f"MongoDB Atlas: {msg}{hint}") from None
        except PyMongoError as exc:
            raise RuntimeError(f"MongoDB Atlas unreachable: {type(exc).__name__} — check Network Access (IP allow-list) "
                               "and your internet connection") from None

    return wrapper


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in row.items():
        if isinstance(v, str) and v[:1] in "[{":
            try:
                v = json.loads(v)
            except ValueError:
                pass
        out[k] = v
    return out


@_friendly
def ping() -> dict[str, Any]:
    with client() as c:
        return {"ping": c.admin.command("ping"), "databases": c.list_database_names()}


def documents(dataset_id: str) -> dict[str, list[dict[str, Any]]]:
    """Build the documents to sync (pure function over the local store; unit-testable without a server)."""
    ds = store.query("SELECT dataset_id, name, kind, adapter, synthetic, n_records, n_rejected, created_at, note FROM datasets "
                     "WHERE dataset_id=?", [dataset_id])
    if not ds:
        raise ValueError(f"unknown dataset {dataset_id}")
    eps = store.query("SELECT episode_id, dataset_id, task_id, task_version, policy_id, policy_version, session_id, workcell_id, "
                      "device_id, config_id, outcome, outcome_provenance, verified, category, quality_status, replay_class, "
                      "start_time, end_time, synthetic FROM episodes WHERE dataset_id=?", [dataset_id])
    sigs = signatures([dataset_id])
    for e in eps:
        s = sigs.get(e["episode_id"])
        e["_id"] = e["episode_id"]
        e["failure_signature"] = s["vector"] if s else None
        e["signature_version"] = SIGNATURE_VERSION
    findings = [_clean({**f, "_id": f["finding_id"]}) for f in store.query("SELECT * FROM quality_findings WHERE dataset_id=?", [dataset_id])]
    incs = []
    for r in store.query("SELECT incident_id FROM incidents WHERE dataset_id=?", [dataset_id]):
        i = inc_mod.get(r["incident_id"])
        i["_id"] = i["incident_id"]
        i.pop("members", None)
        incs.append(i)
    events = [{**e, "_id": e["event_id"]} for e in store.query("SELECT * FROM ops_events WHERE dataset_id=?", [dataset_id])]
    meta = {**ds[0], "_id": dataset_id, "synced_at": now_iso(), "synthetic": bool(ds[0]["synthetic"])}
    return {"datasets": [meta], "episodes": eps, "findings": findings, "incidents": incs, "ops_events": events}


def ensure_vector_index(db) -> str:
    from pymongo.operations import SearchIndexModel

    coll = db["episodes"]
    existing = {ix["name"] for ix in coll.list_search_indexes()}
    if VECTOR_INDEX in existing:
        return "exists"
    coll.create_search_index(SearchIndexModel(name=VECTOR_INDEX, type="vectorSearch", definition={"fields": [
        {"type": "vector", "path": "failure_signature", "numDimensions": DIMS, "similarity": "cosine"},
        {"type": "filter", "path": "dataset_id"}, {"type": "filter", "path": "workcell_id"}]}))
    return "created (builds asynchronously; queries work once it is READY)"


@_friendly
def sync(dataset_id: str) -> dict[str, Any]:
    from pymongo import ReplaceOne

    docs = documents(dataset_id)
    with client() as c:
        db = c[DB]
        counts = {}
        for coll, rows in docs.items():
            if rows:
                res = db[coll].bulk_write([ReplaceOne({"_id": r["_id"]}, r, upsert=True) for r in rows], ordered=False)
                counts[coll] = {"upserted": res.upserted_count, "modified": res.modified_count, "matched": res.matched_count}
        try:
            idx = ensure_vector_index(db)
        except Exception as exc:  # e.g. tier without search support
            idx = f"vector index not created: {type(exc).__name__}: {str(exc)[:160]}"
    return {"dataset_id": dataset_id, "database": DB, "collections": counts, "vector_index": idx,
            "not_sent": ["raw stream values", "credentials", "environment variables"]}


@_friendly
def similar(episode_id: str, k: int = 10, workcell_id: str | None = None) -> dict[str, Any]:
    """Atlas Vector Search over failure signatures from every synced dataset (cross-station / cross-team)."""
    with client() as c:
        coll = c[DB]["episodes"]
        q = coll.find_one({"_id": episode_id}, {"failure_signature": 1})
        if not q or not q.get("failure_signature"):
            return {"episode_id": episode_id, "matches": [], "note": "episode not synced or has no failure evidence"}
        stage: dict[str, Any] = {"index": VECTOR_INDEX, "path": "failure_signature", "queryVector": q["failure_signature"],
                                 "numCandidates": max(100, 20 * k), "limit": k + 1}
        if workcell_id:
            stage["filter"] = {"workcell_id": workcell_id}
        rows = list(coll.aggregate([{"$vectorSearch": stage},
                                    {"$project": {"failure_signature": 0, "score": {"$meta": "vectorSearchScore"}}}]))
    # Same match shape as signatures.similar_local, so the UI and CLI treat both engines alike.
    matches = [{"episode_id": r["_id"], "score": round(r["score"], 4), "status": r.get("quality_status"), "category": r.get("category"),
                "session_id": r.get("session_id"), "workcell_id": r.get("workcell_id"), "dataset_id": r.get("dataset_id"),
                "synthetic": bool(r.get("synthetic"))} for r in rows if r["_id"] != episode_id][:k]
    return {"episode_id": episode_id, "engine": "MongoDB Atlas Vector Search", "signature_version": SIGNATURE_VERSION,
            "matches": matches, "note": "Searches every dataset synced to this cluster; similarity = shared recorded evidence."}
