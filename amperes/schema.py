"""Canonical episode data contract: `amperes.episode/1`.

One schema for simulator-generated and imported episodes. Rules:
- A field that the source did not provide is `None` (unknown). Nothing is filled in by guessing.
- Streams are columnar: `t` (seconds) plus `values` (one row per timestamp). A missing sample value is `null`.
- `outcome.provenance` records *who* asserted the outcome; `outcome.verified` is true only when an independent
  verifier (not the source label) checked it against measured state.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "amperes.episode/1"

StreamKind = Literal["observation", "action", "state", "telemetry"]
OutcomeLabel = Literal["success", "failure", "timeout", "invalid", "unknown"]
OutcomeProvenance = Literal["source_label", "amperes_verifier", "operator", "unknown"]
EventType = Literal[
    "fault",
    "recovery",
    "reset",
    "manual_intervention",
    "config_change",
    "recording_error",
    "maintenance",
    "policy_phase",
    "annotation",
    "settle_start",
    "error",
]
ReplayClass = Literal["A", "B", "C", "D", "E"]

REPLAY_CLASS_MEANING = {
    "A": "Faithful replay from saved simulator state (initial-state digest verified)",
    "B": "Replay from saved initial conditions and environment configuration",
    "C": "Re-execution with policy and configuration, but incomplete initial conditions",
    "D": "Diagnostic analysis of a historical log only; no replay capability",
    "E": "Not reproducible with the currently available data",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Ref(_Strict):
    id: str | None = None
    version: str | None = None


class RobotInfo(_Strict):
    embodiment: str | None = None
    device_id: str | None = None
    config_id: str | None = None


class SessionInfo(_Strict):
    session_id: str | None = None
    workcell_id: str | None = None
    operator_id: str | None = Field(None, description="Pseudonymous operator/team id. Never a personal name.")


class SourceInfo(_Strict):
    dataset_id: str
    adapter: str
    source_uri: str | None = None
    source_record_id: str | None = None
    imported_at: str | None = None
    content_sha256: str | None = None
    synthetic: bool = Field(..., description="True for simulator output and generated fixtures.")
    synthetic_note: str | None = None


class Stream(_Strict):
    kind: StreamKind
    t: list[float]
    values: list[list[float | None]]
    units: str | None = None
    expected_rate_hz: float | None = None
    valid_range: list[float] | None = Field(None, description="[low, high] applying to every dimension")
    source: str | None = None

    @field_validator("valid_range")
    @classmethod
    def _range(cls, v):
        if v is not None and (len(v) != 2 or v[0] > v[1]):
            raise ValueError("valid_range must be [low, high] with low <= high")
        return v

    @property
    def dims(self) -> int:
        return len(self.values[0]) if self.values else 0


class Event(_Strict):
    t: float | None = None
    type: EventType
    code: str | None = None
    message: str | None = None
    source: str | None = None
    value: Any = None


class Outcome(_Strict):
    label: OutcomeLabel = "unknown"
    provenance: OutcomeProvenance = "unknown"
    verified: bool = False
    reason: str | None = None
    verifier_id: str | None = None
    verifier_version: str | None = None
    measurements: dict[str, Any] = Field(default_factory=dict)


class Diagnosis(_Strict):
    category: str
    rule_id: str
    taxonomy_version: str
    confidence: Literal["high", "medium", "low"]
    observed_evidence: dict[str, Any]
    alternatives: list[str] = Field(default_factory=list)
    causal_note: str
    hypothesis: dict[str, Any] | None = Field(None, description="Optional model-generated hypothesis; never authoritative")


class ReplayInfo(_Strict):
    replay_class: ReplayClass | None = None
    sim_task_id: str | None = None
    sim_task_version: str | None = None
    simulator_version: str | None = None
    env_params: dict[str, Any] | None = None
    seed: int | None = None
    policy_config: dict[str, Any] | None = None
    initial_state_digest: str | None = None
    terminated_by: str | None = None
    notes: str | None = None


class Episode(_Strict):
    schema_version: Literal["amperes.episode/1"] = SCHEMA_VERSION
    episode_id: str
    source: SourceInfo
    task: Ref | None = None
    policy: Ref | None = None
    robot: RobotInfo | None = None
    session: SessionInfo | None = None
    start_time: float | None = Field(None, description="Unix seconds; None if unknown")
    end_time: float | None = None
    time_base: Literal["unix_s", "episode_relative_s"] = "episode_relative_s"
    streams: dict[str, Stream] = Field(default_factory=dict)
    action_spec: dict[str, Any] | None = None
    events: list[Event] = Field(default_factory=list)
    outcome: Outcome | None = None
    diagnosis: Diagnosis | None = None
    conditions: dict[str, Any] = Field(default_factory=dict)
    replay: ReplayInfo | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    def content_digest(self) -> str:
        """Digest of the recorded content (streams + events), independent of ids and import metadata."""
        payload = {
            "streams": {k: [s.t, s.values] for k, s in sorted(self.streams.items())},
            "events": [e.model_dump() for e in self.events],
        }
        return hashlib.sha256(_dumps(payload).encode()).hexdigest()


def _clean(o: Any) -> Any:
    if isinstance(o, float):
        if math.isnan(o):
            return "NaN"
        if math.isinf(o):
            return "Infinity" if o > 0 else "-Infinity"
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def _dumps(o: Any) -> str:
    return json.dumps(_clean(o), sort_keys=True, separators=(",", ":"))


def _restore(o: Any) -> Any:
    if isinstance(o, str) and o in ("NaN", "Infinity", "-Infinity"):
        return float(o.replace("Infinity", "inf"))
    if isinstance(o, dict):
        return {k: _restore(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_restore(v) for v in o]
    return o


def episode_to_json(ep: Episode) -> str:
    return _dumps(ep.model_dump())


def episode_from_obj(obj: dict[str, Any]) -> Episode:
    # Non-finite numbers are serialized as strings so files stay strict JSON; restore them only inside stream values.
    if isinstance(obj.get("streams"), dict):
        for s in obj["streams"].values():
            if isinstance(s, dict) and "values" in s:
                s["values"] = _restore(s["values"])
    return Episode.model_validate(obj)


def write_episode(ep: Episode, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(episode_to_json(ep))


def read_episode(path: Path) -> Episode:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return episode_from_obj(json.load(f))


def json_schema() -> dict[str, Any]:
    return Episode.model_json_schema()
