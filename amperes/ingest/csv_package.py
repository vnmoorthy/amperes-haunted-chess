"""CSV package adapter for timestamped telemetry exported from existing logging systems.

A package is a directory with:
- `episodes.csv` (required): one row per episode. Columns (all but episode_id optional):
  episode_id, task_id, task_version, policy_id, policy_version, embodiment, device_id, config_id, session_id,
  workcell_id, operator_id, start_time, end_time, outcome_label, outcome_provenance, outcome_reason
- `telemetry.csv` (required): long format, one row per (episode, stream, timestamp):
  episode_id, timestamp, stream, v0, v1, ... (empty cell = missing value, `nan` = NaN)
- `streams.csv` (optional): stream, kind, units, expected_rate_hz, valid_low, valid_high, max_unchanged_s
- `events.csv` (optional): operational events, see amperes.ops.EVENT_COLUMNS
Timestamps are seconds. If `start_time` is present they are interpreted as Unix seconds.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any, Iterator

from pydantic import ValidationError

from amperes.ingest.base import ParsedRecord, RecordError, register
from amperes.ingest.jsonl import validation_errors
from amperes.schema import Episode, Outcome, Ref, RobotInfo, SessionInfo, SourceInfo, Stream

KINDS = {"observation", "action", "state", "telemetry"}


def _f(v: str | None) -> float | None:
    if v is None or v.strip() == "":
        return None
    return float(v)


def _num(v: str) -> float | None:
    s = v.strip()
    if s == "":
        return None
    if s.lower() in ("nan", "inf", "-inf", "infinity", "-infinity"):
        return float(s.lower().replace("infinity", "inf"))
    return float(s)


def _s(v: str | None) -> str | None:
    return v.strip() if v and v.strip() else None


class CsvPackageAdapter:
    name = "csv_package"
    version = "1.0.0"

    def detect(self, path: Path) -> bool:
        return path.is_dir() and (path / "episodes.csv").exists() and (path / "telemetry.csv").exists()

    def _stream_meta(self, path: Path, errors: list[RecordError]) -> dict[str, dict[str, Any]]:
        meta: dict[str, dict[str, Any]] = {}
        f = path / "streams.csv"
        if not f.exists():
            return meta
        with open(f, newline="", encoding="utf-8") as fh:
            for i, row in enumerate(csv.DictReader(fh), 2):
                ref = f"streams.csv:row {i}"
                try:
                    kind = _s(row.get("kind")) or "telemetry"
                    if kind not in KINDS:
                        raise ValueError(f"kind must be one of {sorted(KINDS)}")
                    lo, hi = _f(row.get("valid_low")), _f(row.get("valid_high"))
                    meta[row["stream"]] = {"kind": kind, "units": _s(row.get("units")),
                                           "expected_rate_hz": _f(row.get("expected_rate_hz")),
                                           "valid_range": [lo, hi] if lo is not None and hi is not None else None,
                                           "max_unchanged_s": _f(row.get("max_unchanged_s"))}
                except (ValueError, KeyError) as exc:
                    errors.append(RecordError(ref, str(exc)))
        return meta

    def parse(self, path: Path, dataset_id: str) -> Iterator[ParsedRecord]:
        meta_errors: list[RecordError] = []
        smeta = self._stream_meta(path, meta_errors)
        if meta_errors:
            yield ParsedRecord("streams.csv", None, meta_errors)
        tele: dict[str, dict[str, list]] = {}
        bad_rows: dict[str, list[RecordError]] = {}
        with open(path / "telemetry.csv", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            vcols = sorted([c for c in (reader.fieldnames or []) if c and c.startswith("v") and c[1:].isdigit()],
                           key=lambda c: int(c[1:]))
            missing = {"episode_id", "timestamp", "stream"} - set(reader.fieldnames or [])
            if missing or not vcols:
                yield ParsedRecord("telemetry.csv", None, [RecordError("telemetry.csv:header",
                                   f"missing required columns: {sorted(missing) or 'v0..vN'}")])
                return
            for i, row in enumerate(reader, 2):
                ep = _s(row.get("episode_id"))
                try:
                    if not ep:
                        raise ValueError("empty episode_id")
                    t = float(row["timestamp"])
                    if math.isnan(t):
                        raise ValueError("timestamp is NaN")
                    vals = [_num(row.get(c) or "") for c in vcols]
                    while vals and vals[-1] is None and len(vals) > 1 and all(v is None for v in vals[len(vals) - 1:]):
                        # trailing empty columns belong to wider streams; trimmed per stream below
                        break
                    s = tele.setdefault(ep, {}).setdefault(row["stream"], [])
                    s.append((t, vals))
                except (ValueError, TypeError) as exc:
                    bad_rows.setdefault(ep or "?", []).append(RecordError(f"telemetry.csv:row {i}", str(exc)))
        with open(path / "episodes.csv", newline="", encoding="utf-8") as fh:
            for i, row in enumerate(csv.DictReader(fh), 2):
                ref = f"episodes.csv:row {i}"
                sid = _s(row.get("episode_id"))
                if not sid:
                    yield ParsedRecord(ref, None, [RecordError(ref, "episode_id is required", "episode_id")])
                    continue
                errors = list(bad_rows.get(sid, []))
                try:
                    streams = {}
                    for name, rows in tele.get(sid, {}).items():
                        m = smeta.get(name, {})
                        width = max((max((j for j, v in enumerate(vals) if v is not None), default=-1) + 1 for _, vals in rows), default=1)
                        width = max(width, 1)
                        streams[name] = Stream(kind=m.get("kind", "telemetry"), t=[r[0] for r in rows],
                                               values=[r[1][:width] for r in rows], units=m.get("units"),
                                               expected_rate_hz=m.get("expected_rate_hz"), valid_range=m.get("valid_range"),
                                               source=f"csv:{name}")
                        if m.get("max_unchanged_s"):
                            streams[name].source = f"csv:{name};max_unchanged_s={m['max_unchanged_s']}"
                    label = _s(row.get("outcome_label"))
                    outcome = None
                    if label:
                        outcome = Outcome(label=label, provenance=_s(row.get("outcome_provenance")) or "source_label",
                                          verified=False, reason=_s(row.get("outcome_reason")))
                    start = _f(row.get("start_time"))
                    conditions = {}
                    if _s(row.get("attempt_index")):
                        conditions["attempt_index"] = int(row["attempt_index"])
                    ep = Episode(
                        conditions=conditions,
                        episode_id=sid,
                        source=SourceInfo(dataset_id=dataset_id, adapter=f"{self.name}@{self.version}",
                                          source_uri=str(path), source_record_id=sid, synthetic=False),
                        task=Ref(id=_s(row.get("task_id")), version=_s(row.get("task_version"))),
                        policy=Ref(id=_s(row.get("policy_id")), version=_s(row.get("policy_version"))),
                        robot=RobotInfo(embodiment=_s(row.get("embodiment")), device_id=_s(row.get("device_id")),
                                        config_id=_s(row.get("config_id"))),
                        session=SessionInfo(session_id=_s(row.get("session_id")), workcell_id=_s(row.get("workcell_id")),
                                            operator_id=_s(row.get("operator_id"))),
                        start_time=start, end_time=_f(row.get("end_time")),
                        time_base="unix_s" if start is not None else "episode_relative_s",
                        streams=streams, outcome=outcome)
                    yield ParsedRecord(ref, ep, errors)
                except ValidationError as exc:
                    yield ParsedRecord(ref, None, errors + validation_errors(exc, ref))
                except ValueError as exc:
                    yield ParsedRecord(ref, None, errors + [RecordError(ref, str(exc))])

    def parse_events(self, path: Path, dataset_id: str):
        from amperes.ops import read_events_csv

        f = path / "events.csv"
        return read_events_csv(f, dataset_id) if f.exists() else ([], [])


register(CsvPackageAdapter())
