# Phase 2 final report: customer-testable data reliability

## What changed

| Area | Added | Files |
|---|---|---|
| Ingestion | Canonical contract `amperes.episode/1` for imported and simulated data; `Adapter` protocol; JSONL and CSV-package adapters; stable episode ids; source fingerprinting; field-level validation errors; dropped-row reporting (`C009`) | `schema.py`, `ingest/` |
| Data quality | 26 rules over completeness, temporal, signal, outcome and dataset dimensions, each with a version, severity, evidence, expectation, recommendation, determinism flag and review flag; seven episode statuses; per-dimension summary with **no composite score** | `quality.py` |
| Incidents | Exact-key grouping (rule / task+policy+category / fault class+code); status workflow with enforced transitions and history; recurrence-after-resolution reopen; export to JSON/Markdown; incident → regression case | `incidents.py` |
| Reproducibility | Replay classes A–E, decided from the record; ineligible cases are recorded with a reason | `sim/reproduce.py::replay_class` |
| Evaluation harness | Labelled synthetic defect corpus (104 episodes, 16 defect types); precision and recall with numerators and denominators | `fixtures.py`, `evaluation.py` |
| Interface | Overview, Episodes (source values vs derived diagnostics), Quality, Incidents, Operations, Replay & regression, Evaluation, Export; synthetic-data banner; sample-data loader | `api.py`, `ui/index.html` |
| Security | Local-only bind, read-only source, deletion, optional model hook disabled by default | `DATA_SECURITY.md`, `diagnostics.py` |

## What was tested, and how

```bash
.venv/bin/python -m pytest -q tests/
```

The result is 45 passed (Phase 1: 18 tests; Phase 2/3: 27 tests). The Phase 2 list is covered as follows:

| Requirement | Test(s) in `tests/test_data.py` |
|---|---|
| Schema validation | `test_schema_validation_errors_are_specific` |
| Importing valid records / malformed records | `test_import_valid_and_malformed_records`, `test_csv_package_import_and_bad_rows` |
| Timestamp / numerical / outcome checks | `test_timestamp_rules`, `test_numerical_rules`, `test_outcome_label_validation_uses_declared_criteria` |
| Duplicates | `test_duplicate_detection`, `test_reimport_keeps_events_per_dataset` |
| Valid unsuccessful episodes preserved | `test_valid_failed_task_is_preserved` |
| Finding provenance | `test_numerical_rules` (rule id/version/record_ref/deterministic) |
| Incident creation, grouping, history | `test_incident_grouping_status_history` |
| Replay eligibility; regression from incidents | `test_replay_eligibility_and_incident_regression` |
| Report consistency | `test_pilot_report_consistency_and_synthetic_label` |
| Deletion | `test_dataset_deletion`, `test_deletion_removes_reports_and_study_rows` |
| Optional model disabled by default | `test_diagnostic_model_disabled_by_default` |
| API/CLI error handling | `test_api_and_cli_errors`, `test_rules_config_disables_rule` |

End-to-end: `scripts/reproduce_all.sh` (clean setup, about 2.5 min) and `scripts/demo_walkthrough.sh` (about 10 s).

## Detector precision and recall (synthetic corpus, `eval-detectors`)

- 104 episodes: 40 clean (16 of them valid failed tasks), 64 with one injected defect each. Processing takes about 19 ms per episode.
- **Valid-record preservation:** 40/40. **Failed tasks kept as `valid_failed_task`:** 16/16.
- **Recall:** 4/4 for each of the 15 defect types that the record makes detectable. Precision is 1.0 for every
  defect rule (numerators and denominators are in `detector_eval.md`).
- **`label_flip_without_state`: 0/4.** This is undetectable by design, because the record has no evidence to contradict the label.
- **Caveats:** the corpus and rules share an author. Before the final numbers, two declarations were corrected: the
  simulator's finger-gap range (41 false positives) and truncation ignoring action streams. The rule logic was not changed
  to fit the corpus. Treat these numbers as a regression baseline for the rules, not as accuracy on real data.

## Known issues

- `O001_unverified_success` (info) dominates finding counts on label-only data, e.g. 397 of 453 findings on the synthetic
  package. It is excluded from review denominators and from the overview count, but it is still stored.
- Imported logs are always replay class D. Incident → regression works only for simulator-backed records.
- Strict (verified-only) yield is 0% for CSV imports, because nothing in a label-only export can be verified.
- `D004_session_duration_shift` is heuristic, and on the synthetic package it fires on the sessions after the change. It is labelled as needing review.
- The `ProcessPoolExecutor` path requires a `__main__` guard in library callers on macOS.

## Unsupported integrations

LeRobotDataset (parquet + mp4), MCAP / rosbag2, robomimic HDF5, ROS live topics, Foxglove/Rerun export, ticketing systems,
and image/video quality checks. None of these is claimed.

## Evidence of differentiation

There is no customer evidence. The only internal evidence is structural: Amperes keeps "invalid data", "valid failed
task", "recording/infrastructure failure" and "unverified success" separate, and links operational events to the episodes
they affected. `COMPETITIVE_GAP.md` documents what each existing tool already covers.

## Most important remaining risks

1. Real data may produce too many low-value findings, adding review work instead of removing it.
2. Integration cost: there is no adapter for the formats teams actually store.
3. Teams can script the checks themselves; the value must come from the linked, auditable classification and the workflow.

## Narrowest pilot to pursue next

A read-only usable-data yield audit on one customer-approved export (about 500–2,000 episodes plus the event log covering
the same window). Customer engineers review a random sample of findings, and the pilot measures precision and
integration time. Details are in `PHASE3_PILOT_OFFER.md`.
