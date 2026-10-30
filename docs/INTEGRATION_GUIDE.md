# Integration guide

For a limited evaluation, send exported records. No source code, model weights or robot access is required.

## Data contract

The contract is `amperes.episode/1`. `python -m amperes.cli schema` prints the JSON Schema.

### Required for any evaluation

| Field | Why |
|---|---|
| `episode_id` (unique per source) | Stable identity. Amperes derives `ep_<sha256(dataset/source_id@record)>`, so re-imports never collide |
| At least one observation stream and one action stream, with timestamps in seconds | Completeness and temporal checks |
| `outcome.label` ∈ {success, failure, timeout, invalid, unknown}, plus `outcome.provenance` (source_label / operator / …) | Yield and failure triage. A missing label is reported as such, not guessed |

### Optional, and what each unlocks

| Field | Unlocks |
|---|---|
| `start_time`, `end_time` (Unix s) | Truncation (C004), clock checks (C006/T006), throughput, cycle time |
| `expected_rate_hz` per stream | Gap (T003), rate (T004) and misalignment (T005) checks |
| `valid_range` per stream | Out-of-range check (S002) |
| `max_unchanged_s` per stream | Stale-signal check (S004). It is heuristic and needs review |
| `action_spec.max_delta_per_step`, `low`, `high` | Discontinuity (S003) and saturation (S005) checks |
| `task.id/version`, `policy.id/version`, `session.session_id/workcell_id/operator_id`, `robot.device_id/config_id` | Grouping, comparisons, per-session and per-config analysis |
| `outcome.measurements.criteria: {name: bool}` | Label-vs-evidence contradiction check (O002) on non-simulator data |
| `replay` (simulator task, env params, seed, policy config, initial-state digest) | Replay classes A/B and regression cases |
| Operational events (below) | Availability, downtime, MTTA/MTTR, recovery time, recurrence, bottlenecks |

### What cannot be inferred

- Downtime, availability or per-hour rates when session and station events are absent. These are reported as `not_computable`.
- Whether a success is real when only a label exists. These episodes are `valid_success_unverified`.
- Causes of faults. Amperes reports repetitions and associations only.
- Anything about video or images. Camera content is not inspected.

## Supported formats

1. **Canonical JSONL** (`--adapter jsonl`): one episode object per line, optionally gzip. Operational events go in a sibling `events.jsonl`.
2. **CSV package** (`--adapter csv_package`): a directory containing:
   - `episodes.csv`: `episode_id, task_id, task_version, policy_id, policy_version, embodiment, device_id, config_id, session_id, workcell_id, operator_id, start_time, end_time, outcome_label, outcome_provenance, outcome_reason, attempt_index`
   - `telemetry.csv` (long format): `episode_id, timestamp, stream, v0..vN`. An empty cell is a missing value; `nan` is NaN.
   - `streams.csv` (optional): `stream, kind, units, expected_rate_hz, valid_low, valid_high, max_unchanged_s`
   - `events.csv` (optional): `event_id, timestamp, end_timestamp, workcell_id, device_id, session_id, episode_id, event_type, event_class, code, message, incident_ref`
     - `event_type` is one of session_start/end, attempt_start/end, station_down/up, fault, recovery, maintenance_start/end, config_change, manual_intervention, reset, restart, recording_error, incident_ack, incident_resolved.
     - `event_class` (for faults) is one of robot_task_failure, data_recording_failure, invalid_data, hardware_fault, software_fault, operator_interruption, infrastructure_failure, unclassified.

**Not supported yet:** LeRobotDataset (parquet + video), MCAP/rosbag2, robomimic HDF5. Export to the CSV package or JSONL, or
write an adapter (`amperes/ingest/base.py`: implement `detect`, `parse`, `parse_events`, then `register()`).

## Worked example (`examples/sample_package`, synthetic)

```bash
.venv/bin/python -m amperes.cli validate examples/sample_package
```

Actual output (abridged), exit code 1:

```json
{
  "adapter": "csv_package@1.0.0",
  "valid_records": 3,
  "invalid_records": 0,
  "records_with_dropped_rows": [
    {"record_ref": "episodes.csv:row 4", "episode_id": "ep-003",
     "dropped_rows": [{"record_ref": "telemetry.csv:row 94", "field": null,
                       "message": "could not convert string to float: '1790000020.x'"}]}
  ],
  "events_valid": 4,
  "event_errors": [{"record_ref": "events.csv:row 6",
                    "message": "events.csv:row 6: event_type 'coffee_break' not in ['attempt_end', ...]"}]
}
```

A schema violation in canonical JSONL names the field path, for example
`{"record_ref": "episodes.jsonl:2", "field": "streams.obs.q.kind", "message": "Input should be 'observation', 'action', 'state' or 'telemetry' (got 'camera')"}`.
This case is checked by `tests/test_data.py::test_schema_validation_errors_are_specific`.

```bash
.venv/bin/python -m amperes.cli import examples/sample_package --brief
```

Result on this sample:
- ep-001 is `valid_success_unverified`;
- ep-002 is `valid_failed_task`;
- ep-003 is `suspicious` (C005 missing outcome and C009 one source row dropped).
- 4 events were imported.

## Customer-agreed settings

Thresholds are proposals until agreed. Pass them as files so the agreed values are recorded in every report:

```json
// rules.json  →  import / shadow / qc --rules-config rules.json   (recorded as import_report.rule_settings)
{"disabled_rules": ["S005_command_saturation"], "gap_factor": 4.0, "min_rows": 20, "end_tolerance_s": 1.0,
 "required_metadata": ["task.id", "policy.id", "policy.version"]}
```

```json
// analysis.json  →  ops ... / pilot-report --analysis-config analysis.json   (recorded as analysis_settings)
{"usable_statuses": ["verified_success", "valid_failed_task"], "rejected_tolerance": 0.03, "min_session_episodes": 15}
```

Unknown keys are rejected with exit code 2, so a typo cannot silently fall back to a default.

## Pilot commands

| Step | Command |
|---|---|
| Validate | `python -m amperes.cli validate <path>` |
| Ingest (shadow mode) | `python -m amperes.cli shadow <path> --name <name>` |
| Report | `python -m amperes.cli pilot-report <dataset_id> [--assumptions costs.json] [--study <study_id>]` |
| Review findings | `python -m amperes.cli review add <finding_id> --decision correct_actionable --reviewer r1` |
| Delete | `python -m amperes.cli delete-dataset <dataset_id> --yes` |

The export format (`pilot_readiness.json` and normalised episode `.json.gz` files) is plain JSON that can be processed
without Amperes.

## Viam machines (live, read-only)

Put `VIAM_API_KEY`, `VIAM_API_KEY_ID` and `VIAM_MACHINE_ADDRESS` in `.env`, then:

```bash
.venv/bin/python -m amperes.cli viam record --seconds 120 --rate 3
```

What you get, and what you don't:

- **Streams:** `obs.joint_pos` (deg), `obs.ee_pose`, `obs.arm_is_moving`, `obs.gripper_is_moving`, `obs.camera_frame`
  (byte size + content hash, for frozen-feed detection) and `telemetry.read_latency_ms`.
- **Events:** session start/end, `recording_error` (failed reads, camera no-frame), `fault` (resource not READY) and
  `station_down/up` (telemetry link lost / restored).
- **Episodes:** segmented from motion. With no motion, the capture is cut into 20 s observation windows.
- **Not available read-only:** commanded actions and task outcomes. C003 and C005 are disabled for these imports, and
  outcomes stay `unknown`. Combine with your controller's own log or labels for outcome-based yield.
- **Unverified hardware model:** a machine API key may not expose the component models, and the episode metadata says so.
- **Link quality matters:** through a cloud relay, expect p95 read latency around 1 s. Record on the robot's local network for 10 Hz or more.

## Live telemetry

Live telemetry is not needed for the first pilot. If it were needed later, the minimum would be read-only access to the
same fields as the CSV package, through an export job. Amperes needs no write or command permissions.
