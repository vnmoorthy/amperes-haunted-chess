"""Human outcome labels for episodes: append-only history (latest wins), agreement with the stored verifier outcome,
and read-only access to the camera snapshots an episode references (Viam raw depth maps are rendered to PNG)."""

from __future__ import annotations

import struct
import zlib
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from amperes import store
from amperes.provenance import new_id, now_iso
from amperes.stats import wilson

OUTCOMES = ("success", "failure", "unknown")
CATEGORIES = ("GRASP_FAILURE", "SLIPPAGE", "DROP", "COLLISION", "MISPLACED", "TIMEOUT", "SAFETY_STOP", "INFRASTRUCTURE_ERROR", "OTHER")
SNAPSHOT_PREFERENCE = ("pre_grasp", "after_place", "after_lift")
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".dep": "image/png"}
DEPTH_MAGIC = (b"DEPTHMAP", b"VERSIONX")  # Viam rimage raw depth: the bytes spell DEPTHMAP (the Go constant is MagicNumIntVersionX)
AGREEMENT_NOTE = "latest label per episode vs the stored outcome; 'unknown' on either side is not comparable; timeout counts as failure"


def agreement(human: str, stored: str | None) -> int | None:
    v = "failure" if stored == "timeout" else stored
    if human == "unknown" or v not in ("success", "failure"):
        return None
    return int(human == v)


def record_label(episode_id: str, outcome: str, reviewer: str, category: str | None = None, note: str = "") -> dict[str, Any]:
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")
    if category is not None and category not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES}")
    reviewer = (reviewer or "").strip()
    if not reviewer or "@" in reviewer:
        raise ValueError("reviewer must be a pseudonymous id (no e-mail addresses)")
    rows = store.query("SELECT dataset_id, outcome, verified FROM episodes WHERE episode_id=?", [episode_id])
    if not rows:
        raise LookupError(f"unknown episode {episode_id}")
    r = rows[0]
    row = {"label_id": new_id("hl"), "episode_id": episode_id, "dataset_id": r["dataset_id"], "outcome": outcome,
           "category": None if outcome == "success" else category, "note": note or "", "reviewer": reviewer,
           "created_at": now_iso(), "agrees_with_verifier": agreement(outcome, r["outcome"])}
    store.insert("human_labels", row)
    return {**row, "verifier_outcome": r["outcome"], "verified": r["verified"]}


def labels_for(episode_id: str) -> list[dict[str, Any]]:
    return store.query("SELECT * FROM human_labels WHERE episode_id=? ORDER BY created_at, rowid", [episode_id])


def snapshot_images(ep) -> list[str]:
    """CAMERA_SNAPSHOT filenames, pre-grasp / after-place / after-lift first, colour before depth."""
    names = dict.fromkeys(e.message for e in ep.events if e.code == "CAMERA_SNAPSHOT" and e.message)
    rank = lambda n: (n.endswith(".dep"), next((i for i, tag in enumerate(SNAPSHOT_PREFERENCE) if tag in n), len(SNAPSHOT_PREFERENCE)), n)  # noqa: E731
    return sorted(names, key=rank)


def queue(dataset_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Unlabelled episodes, real datasets before synthetic ones, newest first."""
    from amperes.schema import read_episode

    rows = store.query("SELECT e.episode_id, e.dataset_id, e.outcome, e.category, e.reason, e.verified, e.quality_status, e.start_time, "
                       "e.artifact_path, COALESCE(d.synthetic, e.synthetic, 0) synthetic FROM episodes e "
                       "LEFT JOIN datasets d ON d.dataset_id=e.dataset_id "
                       "WHERE NOT EXISTS (SELECT 1 FROM human_labels h WHERE h.episode_id=e.episode_id)"
                       f"{' AND e.dataset_id=?' if dataset_id else ''} "
                       "ORDER BY synthetic, e.start_time IS NULL, e.start_time DESC, e.created_at DESC, e.episode_id LIMIT ?",
                       ([dataset_id] if dataset_id else []) + [limit])
    out = []
    for r in rows:
        try:  # an unreadable artifact must not take the whole queue down; the row can still be labelled
            ep = read_episode(store.safe_artifact_path(r.pop("artifact_path")))
            cond, images = ep.conditions, snapshot_images(ep)[:2]
        except (OSError, ValueError):
            cond, images = {}, []
        out.append({**r, "category": verifier_category(r), "instruction": cond.get("instruction"), "object": cond.get("object"), "images": images})
    return out


def verifier_category(row: dict[str, Any]) -> str | None:
    """Diagnosis category when one exists, else the verifier's reason when it is a known failure category."""
    return row.get("category") or (row.get("reason") if row.get("reason") in CATEGORIES else None)


def summary(dataset_id: str | None = None) -> dict[str, Any]:
    w, p = (" WHERE h.dataset_id=?", [dataset_id]) if dataset_id else ("", [])
    rows = store.query("SELECT h.*, e.outcome verifier_outcome, e.category diag_category, e.reason verifier_reason, e.verified "
                       f"FROM human_labels h LEFT JOIN episodes e ON e.episode_id=h.episode_id{w} ORDER BY h.created_at, h.rowid", p)
    for r in rows:
        r["verifier_category"] = verifier_category({"category": r.pop("diag_category"), "reason": r.pop("verifier_reason")})
    latest = {r["episode_id"]: r for r in rows}
    n_eps = store.query("SELECT COUNT(*) n FROM episodes" + (" WHERE dataset_id=?" if dataset_id else ""), p)[0]["n"]
    comp = [r for r in latest.values() if r["agrees_with_verifier"] is not None]
    k = sum(r["agrees_with_verifier"] for r in comp)
    keys = ("episode_id", "outcome", "category", "reviewer", "note", "created_at", "verifier_outcome", "verifier_category", "verified")
    return {"dataset_id": dataset_id, "episodes": n_eps, "labelled": len(latest), "unlabelled": n_eps - len(latest),
            "labels_total": len(rows), "categories": list(CATEGORIES), "outcomes": list(OUTCOMES),
            "by_outcome": dict(Counter(r["outcome"] for r in latest.values())),
            "by_category": dict(Counter(r["category"] for r in latest.values() if r["category"])),
            "reviewers": sorted({r["reviewer"] for r in rows}),
            "agreement": {"comparable": len(comp), "agree": k, "rate": k / len(comp) if comp else None,
                          "ci95": list(wilson(k, len(comp))), "note": AGREEMENT_NOTE},
            "disagreements": [{x: r[x] for x in keys} for r in comp if not r["agrees_with_verifier"]]}


def image_path(episode_id: str, filename: str) -> Path:
    """Only a file this episode references (CAMERA_SNAPSHOT event, or its own frame files), inside the session's images/ directory."""
    from amperes.schema import read_episode

    if Path(filename).name != filename or filename.startswith(".") or Path(filename).suffix.lower() not in IMAGE_TYPES:
        raise LookupError("image")
    rows = store.query("SELECT e.artifact_path, d.source_uri FROM episodes e LEFT JOIN datasets d ON d.dataset_id=e.dataset_id "
                       "WHERE e.episode_id=?", [episode_id])
    if not rows or not rows[0]["source_uri"]:
        raise LookupError("image")
    try:
        ep = read_episode(store.safe_artifact_path(rows[0]["artifact_path"]))
        own = ep.extra.get("source_episode_id") or ep.source.source_record_id or ""
        if filename not in snapshot_images(ep) and not (own and filename.startswith(f"{own}_")):
            raise LookupError("image")
        root = (Path(rows[0]["source_uri"]) / "images").resolve()
        p = (root / filename).resolve()
        if p.parent != root or not p.is_file():
            raise LookupError("image")
    except (OSError, ValueError) as exc:  # unreadable artifact, NUL bytes in the name, ...
        raise LookupError("image") from exc
    return p


def depth_png(data: bytes) -> bytes:
    """Viam raw depth (8-byte magic, width/height as big-endian uint64, big-endian uint16 mm) -> 8-bit greyscale PNG, near = bright.
    Pixels with no return (0 mm) are black; the 1st-99th percentile of valid depths spans the grey range."""
    if len(data) < 24 or data[:8] not in DEPTH_MAGIC:
        raise ValueError("not a Viam raw depth map (DEPTHMAP header missing)")
    w, h = struct.unpack(">QQ", data[8:24])
    if not w or not h or w * h > 50_000_000 or len(data) != 24 + 2 * w * h:
        raise ValueError(f"depth map size mismatch: {w}x{h} with {len(data) - 24} payload bytes")
    d = np.frombuffer(data, dtype=">u2", offset=24).reshape(h, w).astype(np.float64)
    valid = d[d > 0]
    g = np.zeros((h, w), np.uint8)
    if valid.size:
        lo, hi = np.percentile(valid, [1, 99])
        g = np.where(d > 0, 255 - np.clip((d - lo) / max(hi - lo, 1.0), 0, 1) * 255, 0).astype(np.uint8)
    raw = zlib.compress(np.hstack([np.zeros((h, 1), np.uint8), g]).tobytes())
    chunk = lambda t, b: struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xFFFFFFFF)  # noqa: E731
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")
