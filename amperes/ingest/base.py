"""Adapter interface for customer-owned data sources.

An adapter turns source records into canonical `Episode` objects (and optional operational events) without
modifying the source. It must never invent values: anything the source does not state stays `None`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Protocol

from amperes.schema import Episode


@dataclass
class RecordError:
    record_ref: str  # e.g. "episodes.jsonl:12" or "telemetry.csv:row 40"
    message: str
    field: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"record_ref": self.record_ref, "field": self.field, "message": self.message}


@dataclass
class ParsedRecord:
    record_ref: str
    episode: Episode | None
    errors: list[RecordError] = field(default_factory=list)


class Adapter(Protocol):
    name: str
    version: str

    def detect(self, path: Path) -> bool: ...

    def parse(self, path: Path, dataset_id: str) -> Iterator[ParsedRecord]: ...

    def parse_events(self, path: Path, dataset_id: str) -> tuple[list[dict[str, Any]], list[RecordError]]: ...


ADAPTERS: dict[str, Adapter] = {}


def register(adapter: Adapter) -> Adapter:
    ADAPTERS[adapter.name] = adapter
    return adapter


def get_adapter(name: str, path: Path) -> Adapter:
    from amperes.ingest import csv_package, jsonl  # noqa: F401  (registration side effect)

    if name != "auto":
        if name not in ADAPTERS:
            raise ValueError(f"unknown adapter {name!r}; available: {sorted(ADAPTERS)}")
        return ADAPTERS[name]
    for a in ADAPTERS.values():
        if a.detect(path):
            return a
    raise ValueError(f"no adapter recognises {path}; pass --adapter explicitly ({sorted(ADAPTERS)})")
