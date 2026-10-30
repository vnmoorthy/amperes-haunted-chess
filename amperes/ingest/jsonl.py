"""Canonical JSONL adapter: one `amperes.episode/1` object per line (optionally gzip-compressed).

Operational events may be supplied in a sibling `events.jsonl` (see INTEGRATION_GUIDE.md).
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Iterator

from pydantic import ValidationError

from amperes.ingest.base import ParsedRecord, RecordError, register
from amperes.schema import SCHEMA_VERSION, episode_from_obj


def _open(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else open(path, encoding="utf-8")


def _files(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(p for p in path.iterdir() if p.name.endswith((".jsonl", ".jsonl.gz")) and not p.name.startswith("events"))
    return [path]


def validation_errors(exc: ValidationError, ref: str) -> list[RecordError]:
    out = []
    for e in exc.errors()[:20]:
        loc = ".".join(str(x) for x in e["loc"])
        out.append(RecordError(ref, f"{e['msg']} (got {str(e.get('input'))[:80]!r})", field=loc))
    return out


class JsonlAdapter:
    name = "jsonl"
    version = "1.0.0"

    def detect(self, path: Path) -> bool:
        return any(f.name.endswith((".jsonl", ".jsonl.gz")) for f in _files(path)) if path.exists() else False

    def parse(self, path: Path, dataset_id: str) -> Iterator[ParsedRecord]:
        for f in _files(path):
            with _open(f) as fh:
                for i, line in enumerate(fh, 1):
                    ref = f"{f.name}:{i}"
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError as exc:
                        yield ParsedRecord(ref, None, [RecordError(ref, f"invalid JSON: {exc.msg} at col {exc.colno}")])
                        continue
                    if not isinstance(obj, dict):
                        yield ParsedRecord(ref, None, [RecordError(ref, "record is not a JSON object")])
                        continue
                    if obj.get("schema_version") not in (None, SCHEMA_VERSION):
                        yield ParsedRecord(ref, None, [RecordError(ref, f"unsupported schema_version {obj.get('schema_version')!r}; expected {SCHEMA_VERSION}", "schema_version")])
                        continue
                    obj.setdefault("source", {})
                    if isinstance(obj["source"], dict):
                        obj["source"].setdefault("dataset_id", dataset_id)
                        obj["source"].setdefault("adapter", f"{self.name}@{self.version}")
                        obj["source"].setdefault("source_uri", str(f))
                        obj["source"].setdefault("source_record_id", obj.get("episode_id"))
                    try:
                        yield ParsedRecord(ref, episode_from_obj(obj))
                    except ValidationError as exc:
                        yield ParsedRecord(ref, None, validation_errors(exc, ref))

    def parse_events(self, path: Path, dataset_id: str) -> tuple[list[dict[str, Any]], list[RecordError]]:
        f = (path if path.is_dir() else path.parent) / "events.jsonl"
        if not f.exists():
            return [], []
        from amperes.ops import normalize_event

        events, errors = [], []
        with open(f, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                ref = f"{f.name}:{i}"
                try:
                    events.append(normalize_event(json.loads(line), dataset_id, ref))
                except (ValueError, json.JSONDecodeError) as exc:
                    errors.append(RecordError(ref, str(exc)))
        return events, errors


register(JsonlAdapter())
