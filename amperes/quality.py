"""Episode data-quality rule engine.

Each rule declares its dimension, version, whether it is deterministic, and an `applies` precondition so it only runs
when the schema provides the needed fields. Findings carry the observed value, the violated expectation, an
explanation, and a recommended next action.

Episode quality status (precedence top to bottom):
  infrastructure_error     explicit recording/harness error events, or a harness-invalid simulator run
  invalid_data             any deterministic critical finding
  suspicious               any major finding, or any finding that requires human review
  verified_success         outcome success, independently verified
  valid_success_unverified outcome success asserted only by the source label
  valid_failed_task        outcome failure/timeout with no serious data defect (still usable data)
  unknown_outcome          no outcome, or outcome 'unknown'
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Callable

import numpy as np

from amperes.schema import Episode

RULESET_VERSION = "1.0.0"
SEVERITIES = ("critical", "major", "minor", "info")
STATUSES = ("infrastructure_error", "invalid_data", "suspicious", "verified_success", "valid_success_unverified",
            "valid_failed_task", "unknown_outcome")
USABLE_STATUSES_DEFAULT = ("verified_success", "valid_success_unverified", "valid_failed_task")
DIMENSIONS = ("completeness", "temporal", "signal", "outcome", "dataset")


@dataclass
class Finding:
    rule_id: str
    rule_version: str
    dimension: str
    severity: str
    episode_id: str | None
    record_ref: str | None
    t: float | None
    observed: Any
    expected: str
    explanation: str
    recommendation: str
    deterministic: bool
    needs_review: bool
    extra: dict[str, Any] = field(default_factory=dict)

    def as_row(self, dataset_id: str, finding_id: str, created_at: str) -> dict[str, Any]:
        return {"finding_id": finding_id, "dataset_id": dataset_id, "episode_id": self.episode_id,
                "rule_id": self.rule_id, "rule_version": self.rule_version, "dimension": self.dimension,
                "severity": self.severity, "record_ref": self.record_ref, "t": self.t,
                "observed": _jsonable(self.observed), "expected": self.expected, "explanation": self.explanation,
                "recommendation": self.recommendation, "deterministic": int(self.deterministic),
                "needs_review": int(self.needs_review), "created_at": created_at}


def _jsonable(v):
    if isinstance(v, float) and not math.isfinite(v):
        return str(v)
    if isinstance(v, (np.floating, np.integer)):
        return v.item()
    return v


@dataclass
class Rule:
    rule_id: str
    version: str
    dimension: str
    deterministic: bool
    description: str
    applies: Callable[[Episode], bool]
    check: Callable[[Episode, dict], list[Finding]]


RULES: dict[str, Rule] = {}


def rule(rule_id, dimension, description, deterministic=True, version="1.0.0", applies=lambda ep: True):
    def deco(fn):
        RULES[rule_id] = Rule(rule_id, version, dimension, deterministic, description, applies, fn)
        return fn
    return deco


def _f(r: Rule | str, ep: Episode | None, severity, observed, expected, explanation, recommendation,
       record_ref=None, t=None, needs_review=None, **extra) -> Finding:
    r = RULES[r] if isinstance(r, str) else r
    return Finding(r.rule_id, r.version, r.dimension, severity, ep.episode_id if ep else None, record_ref, t, observed,
                   expected, explanation, recommendation, r.deterministic,
                   (not r.deterministic) if needs_review is None else needs_review, extra)


def _vals(s) -> np.ndarray:
    return np.array([[np.nan if v is None else v for v in row] for row in s.values], dtype=float) if s.values else np.zeros((0, 0))


def _none_mask(s) -> np.ndarray:
    return np.array([[v is None for v in row] for row in s.values], dtype=bool) if s.values else np.zeros((0, 0), bool)


def _kinds(ep: Episode, kind: str) -> dict:
    return {k: s for k, s in ep.streams.items() if s.kind == kind}


def _max_unchanged(s) -> float | None:
    if s.source and "max_unchanged_s=" in s.source:
        try:
            return float(s.source.split("max_unchanged_s=")[1].split(";")[0])
        except ValueError:
            return None
    return None


# ----------------------------------------------------------------------------------------------- completeness
@rule("C001_required_metadata", "completeness", "Task and policy identifiers needed for comparisons are present")
def _c001(ep, ctx):
    req = ctx.get("required_metadata", ("task.id", "policy.id"))
    missing = [f for f in req if getattr(getattr(ep, f.split(".")[0]) or object(), f.split(".")[1], None) is None]
    if not missing:
        return []
    return [_f("C001_required_metadata", ep, "minor", missing, f"fields present: {list(req)}",
               "Required identifiers are missing; the episode cannot be grouped or compared by these keys.",
               "Populate the identifiers in the export, or mark them as intentionally unavailable in the contract.")]


@rule("C002_no_observations", "completeness", "At least one non-empty observation stream")
def _c002(ep, ctx):
    obs = _kinds(ep, "observation")
    if any(len(s.t) > 0 for s in obs.values()):
        return []
    return [_f("C002_no_observations", ep, "critical", {"observation_streams": list(obs)}, ">= 1 non-empty observation stream",
               "No observation samples were recorded.", "Check the recorder; this episode cannot be used for learning or diagnosis.")]


@rule("C003_no_actions", "completeness", "At least one non-empty action stream")
def _c003(ep, ctx):
    acts = _kinds(ep, "action")
    if any(len(s.t) > 0 for s in acts.values()):
        return []
    return [_f("C003_no_actions", ep, "critical", {"action_streams": list(acts)}, ">= 1 non-empty action stream",
               "No action samples were recorded.", "Check the action logger; without actions the episode is not usable for policy learning.")]


@rule("C004_truncated", "completeness", "Episode has enough samples and streams cover the declared duration")
def _c004(ep, ctx):
    out = []
    lens = {k: len(s.t) for k, s in ep.streams.items() if s.kind in ("observation", "action")}
    min_rows = ctx.get("min_rows", 10)
    if lens and max(lens.values()) < min_rows:
        out.append(_f("C004_truncated", ep, "critical", lens, f">= {min_rows} samples", "Episode is empty or nearly empty.",
                      "Exclude from training; investigate why recording stopped."))
        return out
    # Only observation/state streams: a controller's action stream legitimately ends before recording stops.
    ends = {k: s.t[-1] for k, s in ep.streams.items() if s.t and s.kind in ("observation", "state")}
    if ep.time_base == "unix_s" and ep.end_time is not None and ends:
        base_tol = ctx.get("end_tolerance_s", 0.5)

        def tol(k):  # a stream sampled every Δ can legitimately end up to ~Δ before the episode does
            r = ep.streams[k].expected_rate_hz
            return max(base_tol, 1.5 / r) if r else base_tol

        short = {k: round(ep.end_time - e, 3) for k, e in ends.items() if ep.end_time - e > tol(k)}
        tol = base_tol
        if short:
            out.append(_f("C004_truncated", ep, "major", {"seconds_missing_before_end_time": short},
                          f"streams reach end_time within {tol} s", "Streams stop before the declared episode end.",
                          "Inspect the final segment; the episode may be truncated by a recorder stop or crash.",
                          t=min(ends.values())))
    elif ends and len(ends) > 1:
        spread = max(ends.values()) - min(ends.values())
        if spread > ctx.get("stream_end_spread_s", 1.0):
            first = min(ends, key=ends.get)
            out.append(_f("C004_truncated", ep, "major", {"stream_end_times": ends}, "streams end within 1.0 s of each other",
                          f"Stream {first} stops {spread:.2f} s before the others.", "Inspect that stream's recorder.",
                          record_ref=first, t=ends[first]))
    return out


@rule("C005_missing_outcome", "completeness", "Outcome label present")
def _c005(ep, ctx):
    if ep.outcome is not None and ep.outcome.label != "unknown":
        return []
    return [_f("C005_missing_outcome", ep, "major", None if ep.outcome is None else ep.outcome.label, "outcome label in {success, failure, timeout, invalid}",
               "Outcome is missing or unknown; the episode cannot be counted as success or failure.",
               "Obtain the label from the operator log, or verify the outcome from recorded state.")]


@rule("C006_time_bounds", "completeness", "start/end metadata consistent with stream timestamps",
      applies=lambda ep: ep.time_base == "unix_s")
def _c006(ep, ctx):
    out = []
    if ep.start_time is not None and ep.end_time is not None and ep.end_time < ep.start_time:
        out.append(_f("C006_time_bounds", ep, "critical", {"start_time": ep.start_time, "end_time": ep.end_time},
                      "end_time >= start_time", "Episode end precedes its start.", "Fix the session clock or export."))
    if ep.start_time is not None:
        firsts = {k: s.t[0] for k, s in ep.streams.items() if s.t}
        early = {k: round(ep.start_time - v, 3) for k, v in firsts.items() if v < ep.start_time - 0.5}
        if early:
            out.append(_f("C006_time_bounds", ep, "major", {"seconds_before_start_time": early},
                          "stream timestamps >= start_time - 0.5 s", "Samples are timestamped before the episode started.",
                          "Check clock synchronisation between recorder and session controller.", needs_review=True))
    return out


@rule("C007_missing_samples", "completeness", "No null values inside streams")
def _c007(ep, ctx):
    out = []
    for k, s in ep.streams.items():
        m = _none_mask(s)
        if m.size and m.any():
            rows = np.where(m.any(axis=1))[0]
            frac = len(rows) / len(s.t)
            out.append(_f("C007_missing_samples", ep, "critical" if frac > 0.2 else "major",
                          {"rows_with_missing": int(len(rows)), "fraction": round(frac, 4), "first_t": s.t[rows[0]]},
                          "no null sample values", f"{len(rows)} of {len(s.t)} samples in {k} have missing values.",
                          "Inspect the recorder for dropped messages in this segment.", record_ref=k, t=s.t[rows[0]]))
    return out


@rule("C009_source_rows_dropped", "completeness", "Every source row for the episode was parsed",
      applies=lambda ep: bool(ep.extra.get("import_warnings")))
def _c009(ep, ctx):
    w = ep.extra["import_warnings"]
    return [_f("C009_source_rows_dropped", ep, "major", {"dropped_rows": len(w), "first": w[0]["record_ref"]},
               "all source rows parse", f"{len(w)} source row(s) could not be parsed and were dropped during import.",
               "Fix the export for these rows; the episode is missing the samples they contained.",
               record_ref=w[0]["record_ref"])]


# ----------------------------------------------------------------------------------------------- temporal
@rule("T001_non_monotonic", "temporal", "Timestamps strictly non-decreasing per stream")
def _t001(ep, ctx):
    out = []
    for k, s in ep.streams.items():
        t = np.asarray(s.t)
        bad = np.where(np.diff(t) < 0)[0]
        if len(bad):
            i = int(bad[0]) + 1
            out.append(_f("T001_non_monotonic", ep, "critical", {"count": int(len(bad)), "first_index": i, "t_prev": float(t[i - 1]), "t": float(t[i])},
                          "t[i] >= t[i-1]", f"Timestamps go backwards {len(bad)} time(s) in {k}.",
                          "Check for clock resets or out-of-order message writes; re-sort only if order is provably a write artefact.",
                          record_ref=f"{k}[{i}]", t=float(t[i])))
    return out


@rule("T002_duplicate_timestamps", "temporal", "No duplicate timestamps within a stream")
def _t002(ep, ctx):
    out = []
    for k, s in ep.streams.items():
        t = np.asarray(s.t)
        d = np.where(np.diff(t) == 0)[0]
        if len(d):
            out.append(_f("T002_duplicate_timestamps", ep, "major", {"count": int(len(d)), "first_t": float(t[d[0]])},
                          "unique timestamps per stream", f"{len(d)} duplicated timestamps in {k}.",
                          "Check for duplicated writes or a clock with insufficient resolution.", record_ref=k, t=float(t[d[0]])))
    return out


@rule("T003_time_gap", "temporal", "No gaps larger than 3x the declared sampling interval",
      applies=lambda ep: any(s.expected_rate_hz for s in ep.streams.values()))
def _t003(ep, ctx):
    out = []
    for k, s in ep.streams.items():
        if not s.expected_rate_hz or len(s.t) < 2:
            continue
        dt = np.diff(np.asarray(s.t))
        lim = ctx.get("gap_factor", 3.0) / s.expected_rate_hz
        g = np.where(dt > lim)[0]
        if len(g):
            j = int(g[np.argmax(dt[g])])
            out.append(_f("T003_time_gap", ep, "major", {"gaps": int(len(g)), "largest_gap_s": round(float(dt[j]), 4),
                                                         "at_t": float(s.t[j]), "missing_samples_est": int(np.sum(dt[g] * s.expected_rate_hz - 1))},
                          f"inter-sample interval <= {lim:.4f} s ({s.expected_rate_hz} Hz declared)",
                          f"{len(g)} gap(s) in {k}; largest {dt[j]:.3f} s at t={s.t[j]:.3f}.",
                          "Check recorder logs for dropped data in this window.", record_ref=k, t=float(s.t[j])))
    return out


@rule("T004_rate_mismatch", "temporal", "Median sampling interval matches the declared rate (±20 %)",
      applies=lambda ep: any(s.expected_rate_hz for s in ep.streams.values()))
def _t004(ep, ctx):
    out = []
    for k, s in ep.streams.items():
        if not s.expected_rate_hz or len(s.t) < 5:
            continue
        dt = np.diff(np.asarray(s.t))
        dt = dt[dt > 0]
        if not len(dt):
            continue
        rate = 1.0 / float(np.median(dt))
        if abs(rate - s.expected_rate_hz) / s.expected_rate_hz > 0.2:
            out.append(_f("T004_rate_mismatch", ep, "major", {"observed_rate_hz": round(rate, 3), "declared_rate_hz": s.expected_rate_hz},
                          "observed median rate within 20 % of declared", f"{k} runs at {rate:.2f} Hz, declared {s.expected_rate_hz} Hz.",
                          "Confirm the declared rate in the data contract or fix the recorder configuration.", record_ref=k))
    return out


@rule("T005_obs_action_misalignment", "temporal", "Action timestamps align with observation timestamps",
      applies=lambda ep: bool(_kinds(ep, "action")) and bool(_kinds(ep, "observation")))
def _t005(ep, ctx):
    out = []
    obs = [s for s in _kinds(ep, "observation").values() if s.expected_rate_hz and len(s.t) > 1]
    if not obs:
        return out
    ref = obs[0]
    ot = np.asarray(ref.t)
    half = 0.5 / ref.expected_rate_hz
    for k, s in _kinds(ep, "action").items():
        if not s.expected_rate_hz:
            continue  # event-driven command streams (e.g. discrete move targets) are not sampled; alignment is undefined
        at = np.asarray(s.t)
        if not len(at) or not len(ot):
            continue
        idx = np.clip(np.searchsorted(ot, at), 1, len(ot) - 1)
        nearest = np.minimum(np.abs(ot[idx] - at), np.abs(ot[idx - 1] - at))
        off = float(np.median(nearest))
        if off > half * 0.5:
            out.append(_f("T005_obs_action_misalignment", ep, "major", {"median_offset_s": round(off, 5), "half_interval_s": round(half, 5)},
                          f"median |t_action - nearest t_obs| <= {half * 0.5:.4f} s",
                          f"Actions in {k} are offset from observations by a median {off * 1000:.1f} ms.",
                          "Check whether actions and observations are stamped by different clocks or at different pipeline stages.",
                          record_ref=k))
    return out


@rule("T006_clock_plausibility", "temporal", "Unix timestamps fall in a plausible range", deterministic=False,
      applies=lambda ep: ep.time_base == "unix_s")
def _t006(ep, ctx):
    lo, hi = ctx.get("plausible_unix_range", (1.5e9, 2.2e9))
    bad = {k: s.t[0] for k, s in ep.streams.items() if s.t and not (lo <= s.t[0] <= hi)}
    if not bad:
        return []
    return [_f("T006_clock_plausibility", ep, "major", bad, f"unix seconds in [{lo:.3g}, {hi:.3g}]",
               "Timestamps are outside the plausible range for this collection program (possible unset clock).",
               "Confirm the recorder's clock source; requires review before rejecting.")]


# ----------------------------------------------------------------------------------------------- signal
@rule("S001_non_finite", "signal", "No NaN/Inf in streams")
def _s001(ep, ctx):
    out = []
    for k, s in ep.streams.items():
        v = _vals(s)
        nm = _none_mask(s)
        bad = ~np.isfinite(v) & ~nm if v.size else np.zeros((0, 0), bool)
        if bad.size and bad.any():
            rows = np.where(bad.any(axis=1))[0]
            out.append(_f("S001_non_finite", ep, "critical", {"rows": int(len(rows)), "first_t": s.t[rows[0]]},
                          "finite values", f"{len(rows)} samples in {k} contain NaN or Inf.",
                          "Exclude the episode from training; inspect the producing sensor driver or computation.",
                          record_ref=f"{k}[{rows[0]}]", t=s.t[rows[0]]))
    return out


@rule("S002_out_of_range", "signal", "Values within declared valid_range",
      applies=lambda ep: any(s.valid_range for s in ep.streams.values()))
def _s002(ep, ctx):
    out = []
    for k, s in ep.streams.items():
        if not s.valid_range:
            continue
        v = _vals(s)
        lo, hi = s.valid_range
        with np.errstate(invalid="ignore"):
            bad = (v < lo - 1e-9) | (v > hi + 1e-9)
        if bad.size and bad.any():
            rows = np.where(bad.any(axis=1))[0]
            out.append(_f("S002_out_of_range", ep, "major", {"rows": int(len(rows)), "min": float(np.nanmin(v)), "max": float(np.nanmax(v))},
                          f"values in [{lo}, {hi}]", f"{len(rows)} samples in {k} are outside the declared range.",
                          "Check units and calibration; confirm the declared range in the data contract.",
                          record_ref=f"{k}[{rows[0]}]", t=s.t[rows[0]]))
    return out


@rule("S003_action_discontinuity", "signal", "Per-step action change within declared max_delta_per_step",
      applies=lambda ep: bool(ep.action_spec and ep.action_spec.get("max_delta_per_step")) and bool(_kinds(ep, "action")))
def _s003(ep, ctx):
    out = []
    lim = np.asarray(ep.action_spec["max_delta_per_step"], dtype=float)
    for k, s in _kinds(ep, "action").items():
        v = _vals(s)
        if len(v) < 2 or v.shape[1] != len(lim):
            continue
        d = np.abs(np.diff(v, axis=0))
        with np.errstate(invalid="ignore"):
            bad = np.where((d > lim + 1e-9).any(axis=1))[0]
        if len(bad):
            i = int(bad[0]) + 1
            out.append(_f("S003_action_discontinuity", ep, "major", {"count": int(len(bad)), "max_delta": [round(float(x), 4) for x in np.nanmax(d, axis=0)]},
                          f"|Δaction| <= {lim.tolist()} per step", f"{len(bad)} action jumps exceed the declared limit in {k}.",
                          "Inspect the controller output at these steps (possible spikes, mode switches, or dropped commands).",
                          record_ref=f"{k}[{i}]", t=s.t[i]))
    return out


@rule("S004_stale_signal", "signal", "Streams with a declared max_unchanged_s update at least that often", deterministic=False,
      applies=lambda ep: any(_max_unchanged(s) for s in ep.streams.values()))
def _s004(ep, ctx):
    out = []
    for k, s in ep.streams.items():
        lim = _max_unchanged(s)
        if not lim or len(s.t) < 3:
            continue
        v = _vals(s)
        same = np.all(np.diff(v, axis=0) == 0, axis=1)
        run_start, best, best_start = None, 0.0, None
        for i, sm in enumerate(same):
            if sm and run_start is None:
                run_start = i
            if (not sm or i == len(same) - 1) and run_start is not None:
                end = i + 1 if sm else i
                dur = s.t[end] - s.t[run_start]
                if dur > best:
                    best, best_start = dur, run_start
                run_start = None
        if best > lim:
            out.append(_f("S004_stale_signal", ep, "major", {"unchanged_for_s": round(best, 3), "from_t": s.t[best_start]},
                          f"value changes at least every {lim} s (declared)",
                          f"{k} repeated an identical value for {best:.2f} s. This may be a stale feed or a genuinely static signal.",
                          "Review the segment; do not conclude a sensor fault from this finding alone.",
                          record_ref=k, t=s.t[best_start]))
    return out


@rule("S005_command_saturation", "signal", "Actions are not pinned at their bounds for long stretches", deterministic=False,
      applies=lambda ep: bool(ep.action_spec and ep.action_spec.get("low")) and bool(_kinds(ep, "action")))
def _s005(ep, ctx):
    out = []
    lo, hi = np.asarray(ep.action_spec["low"], float), np.asarray(ep.action_spec["high"], float)
    for k, s in _kinds(ep, "action").items():
        v = _vals(s)
        if len(v) < 10 or v.shape[1] != len(lo):
            continue
        with np.errstate(invalid="ignore"):
            sat = ((np.abs(v - lo) < 1e-9) | (np.abs(v - hi) < 1e-9)).mean(axis=0)
        dims = [i for i, f in enumerate(sat) if f >= ctx.get("saturation_fraction", 0.3)]
        if dims:
            out.append(_f("S005_command_saturation", ep, "minor", {"dims": dims, "fraction_at_bound": [round(float(sat[i]), 3) for i in dims]},
                          "< 30 % of samples at an action bound", f"Action dims {dims} sit at their limits for a large share of {k}.",
                          "Check for clipping or a saturated controller; needs review.", record_ref=k))
    return out


def _recording_incomplete(ep: Episode) -> bool:
    """The verifier needs the final state; a recording that stops before end_time cannot verify the outcome."""
    s = ep.streams.get("state.object_pos")
    return bool(ep.time_base == "unix_s" and ep.end_time is not None and s and s.t and ep.end_time - s.t[-1] > 0.5)


# ----------------------------------------------------------------------------------------------- outcome
@rule("O001_unverified_success", "outcome", "Successes carry independent verification")
def _o001(ep, ctx):
    o = ep.outcome
    if not o or o.label != "success" or o.verified:
        return []
    return [_f("O001_unverified_success", ep, "info", {"provenance": o.provenance}, "success verified against measured state",
               "Success is asserted by the source label only.", "Add verification measurements (e.g. final object pose) to the export.",
               needs_review=False)]


@rule("O002_label_contradicts_state", "outcome", "Source outcome label agrees with independent verification",
      applies=lambda ep: ep.outcome is not None and ep.outcome.label in ("success", "failure", "timeout"))
def _o002(ep, ctx):
    o = ep.outcome
    crit = (o.measurements or {}).get("criteria")
    independent = None
    basis = None
    if isinstance(crit, dict) and crit and all(isinstance(v, bool) for v in crit.values()):
        independent = "success" if all(crit.values()) else "failure"
        basis = {"declared_criteria": crit}
    elif ep.replay and ep.replay.sim_task_id == "pick_place_box" and "state.object_pos" in ep.streams \
            and not _recording_incomplete(ep):
        from amperes.sim.verifier import verify

        v = verify(ep)
        if v.label in ("success", "failure", "timeout"):
            independent, basis = v.label, {"verifier": f"{v.verifier_id}@{v.verifier_version}", "reason": v.reason}
    if independent is None:
        return []
    label = "failure" if o.label == "timeout" else o.label
    indep = "failure" if independent == "timeout" else independent
    if label == indep:
        return []
    return [_f("O002_label_contradicts_state", ep, "critical", {"label": o.label, "independent": independent, **basis},
               "source label equals independently verified outcome",
               f"The record says '{o.label}' but the measured state indicates '{independent}'.",
               "Treat the label as wrong until a reviewer confirms; do not train on it as labelled.")]


# ----------------------------------------------------------------------------------------------- dataset level
def dataset_findings(eps: list[Episode], ctx: dict | None = None) -> list[Finding]:
    ctx = ctx or {}
    out: list[Finding] = []
    seen_digest: dict[str, str] = {}
    for ep in eps:
        d = ep.content_digest()
        if d in seen_digest:
            out.append(Finding("D001_duplicate_content", "1.0.0", "dataset", "major", ep.episode_id, None, None,
                               {"duplicate_of": seen_digest[d], "content_sha256": d[:16]}, "unique recorded content",
                               f"Recorded content is byte-identical to {seen_digest[d]}.",
                               "Keep one copy; check the export for duplicated writes.", True, False))
        else:
            seen_digest[d] = ep.episode_id
    by_src = defaultdict(list)
    for ep in eps:
        if ep.source.source_record_id:
            by_src[(ep.source.source_uri, ep.source.source_record_id)].append(ep.episode_id)
    for (_, rid), ids in by_src.items():
        for dup in ids[1:]:
            out.append(Finding("D002_duplicate_source_id", "1.0.0", "dataset", "major", dup, None, None,
                               {"source_record_id": rid, "first": ids[0]}, "unique source record ids",
                               f"Source record id {rid!r} appears {len(ids)} times.", "Resolve which record is authoritative.", True, False))
    by_session = defaultdict(set)
    for ep in eps:
        if ep.session and ep.session.session_id and ep.policy:
            by_session[ep.session.session_id].add((ep.policy.id, ep.policy.version))
    for sid, versions in by_session.items():
        if len(versions) > 1:
            out.append(Finding("D003_mixed_versions_in_session", "1.0.0", "dataset", "minor", None, f"session:{sid}", None,
                               sorted(map(str, versions)), "one policy version per session unless a config_change event explains it",
                               f"Session {sid} contains {len(versions)} policy versions.",
                               "Confirm the change was intended and recorded.", False, True))
    out += _session_shift(eps, ctx)
    return out


def _session_shift(eps, ctx) -> list[Finding]:
    """Heuristic: a session whose median episode duration is far from other sessions (robust z > 3.5)."""
    min_eps, min_sessions = ctx.get("shift_min_episodes", 8), ctx.get("shift_min_sessions", 4)
    dur = defaultdict(list)
    for ep in eps:
        sid = ep.session.session_id if ep.session else None
        lens = [s.t[-1] - s.t[0] for s in ep.streams.values() if len(s.t) > 1]
        if sid and lens:
            dur[sid].append(max(lens))
    meds = {s: median(v) for s, v in dur.items() if len(v) >= min_eps}
    if len(meds) < min_sessions:
        return []
    allm = np.array(list(meds.values()))
    mad = float(np.median(np.abs(allm - np.median(allm)))) or 1e-9
    out = []
    for s, m in meds.items():
        z = 0.6745 * (m - float(np.median(allm))) / mad
        if abs(z) > 3.5:
            out.append(Finding("D004_session_duration_shift", "1.0.0", "dataset", "minor", None, f"session:{s}", None,
                               {"session_median_s": round(m, 3), "all_sessions_median_s": round(float(np.median(allm)), 3), "robust_z": round(z, 2),
                                "n_episodes": len(dur[s])}, "|robust z| <= 3.5 vs other sessions",
                               f"Episodes in session {s} have an unusual median duration.",
                               "Compare with session notes; this is an association, not a cause.", False, True))
    return out


def missing_channels(eps: list[Episode], ctx: dict | None = None) -> list[Finding]:
    """C008: a stream present in >= 90 % of a dataset's episodes is absent from this one."""
    if len(eps) < 5:
        return []
    cnt = Counter(k for ep in eps for k in ep.streams)
    common = {k for k, c in cnt.items() if c / len(eps) >= 0.9}
    out = []
    for ep in eps:
        miss = sorted(common - set(ep.streams))
        if miss:
            out.append(Finding("C008_missing_channel", "1.0.0", "completeness", "major", ep.episode_id, None, None, miss,
                               "every stream present in >= 90 % of the dataset", f"Streams {miss} are missing from this episode.",
                               "Check the export path for this episode's session or device.", True, False))
    return out


# ----------------------------------------------------------------------------------------------- engine
def check_episode(ep: Episode, ctx: dict | None = None) -> list[Finding]:
    ctx = ctx or {}
    out = []
    for r in RULES.values():
        if r.rule_id in ctx.get("disabled_rules", ()):
            continue
        try:
            if r.applies(ep):
                out += r.check(ep, ctx)
        except Exception as exc:  # a broken rule must not hide data; it becomes a finding itself
            out.append(Finding(r.rule_id, r.version, r.dimension, "major", ep.episode_id, None, None, repr(exc)[:200],
                               "rule executes", "Rule raised an error on this record.", "Report to Amperes maintainers.",
                               False, True))
    return out


def episode_status(ep: Episode, findings: list[Finding]) -> str:
    mine = [f for f in findings if f.episode_id == ep.episode_id]
    infra = any(e.type in ("recording_error", "error") for e in ep.events) or (
        ep.outcome is not None and ep.outcome.label == "invalid" and (ep.outcome.reason or "").startswith("RUN_INVALID"))
    if infra:
        return "infrastructure_error"
    if any(f.severity == "critical" and f.deterministic for f in mine):
        return "invalid_data"
    if any(f.severity == "major" or (f.needs_review and f.severity != "info") for f in mine):
        return "suspicious"
    o = ep.outcome
    if o is None or o.label == "unknown":
        return "unknown_outcome"
    if o.label == "success":
        return "verified_success" if o.verified else "valid_success_unverified"
    if o.label in ("failure", "timeout"):
        return "valid_failed_task"
    return "unknown_outcome"


def quality_summary(statuses: dict[str, str], findings: list[Finding]) -> dict[str, Any]:
    """Per-dimension counts of affected episodes. No composite score: dimensions are reported separately."""
    n = len(statuses)
    dims = {}
    for d in DIMENSIONS:
        fs = [f for f in findings if f.dimension == d]
        eps = {f.episode_id for f in fs if f.episode_id}
        dims[d] = {"findings": len(fs), "episodes_affected": len(eps),
                   "by_severity": dict(Counter(f.severity for f in fs)),
                   "by_rule": dict(Counter(f.rule_id for f in fs))}
    return {"ruleset_version": RULESET_VERSION, "n_episodes": n, "status_counts": dict(Counter(statuses.values())),
            "dimensions": dims, "composite_score": None,
            "composite_note": "Not provided: dimensions are reported separately so a serious defect is never averaged away."}
