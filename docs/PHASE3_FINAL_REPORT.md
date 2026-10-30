# Phase 3 final report: robotics data-operations intelligence

## Selected wedge

The **primary wedge** is a read-only usable-data yield audit. Each exported episode gets one evidence-backed status, and
yield is reported with its numerator, denominator and rule version. The **supporting capability** links operational
events to the episodes they affected, so that each unusable episode is attributed to a recorded event or finding. The
scoring and rationale are in `PHASE3_PRODUCT_DECISION.md`: A scored 34/45, the combined option E 30, B 28, C and D 26.
The simulator loop stays as a test bench and is not part of the first offer.

## Implemented capabilities

| Capability | Where | Verified by |
|---|---|---|
| Operational event model: 17 event types and 8 fault classes kept distinct; duplicate events dropped by content digest; events linked to episodes | `ops.py`, `ingest/pipeline.py::link_events` | `test_ops_metrics_reconcile_and_planted_patterns`, `test_reimport_keeps_events_per_dataset` |
| A completed task with a recording failure is unusable data; a failed task with clean data is usable | `quality.episode_status` | same test: 43 `infrastructure_error`, 67 `valid_failed_task` |
| Yield, strict (verified-only) yield, and data-quality loss counted in distinct episodes | `ops.ops_metrics` | `test_ops_metrics_reconcile_and_planted_patterns`, `test_operator_filter_and_custom_usability` |
| **Yield-loss attribution** (each lost episode attributed once, with episode-hours) | `ops.yield_loss_attribution` | `test_yield_loss_attribution_links_events` |
| Throughput per recorded session-hour; reliability (availability, downtime, incident frequency, recovery wait, MTTA, MTTR, repeat-after-resolution), with `not_computable` when inputs are missing | `ops.ops_metrics`, `ops.reliability` | `test_metrics_not_computable_without_sessions` |
| Bottleneck rules B001–B007, with minimum samples and Wilson-interval comparisons; customer-tunable thresholds | `ops.bottlenecks` | planted-pattern test |
| Cross-session correlation: repetition vs association vs hypothesis, each kept separate | `ops.correlate` | planted-pattern test |
| Human review with 6 decisions, latest-wins, precision among reviewed, no recall | `review.py` | `test_review_statistics` |
| Counterbalanced existing-vs-assisted timing study | `review.plan_study / study_report` | `test_workflow_study_and_economics` |
| Shadow mode: read-only, source-unchanged proof, no robot commands, deletion command | `cli_data.c_shadow` | `scripts/reproduce_all.sh` output (`source_unchanged: true`) |
| Pilot readiness report (JSON + Markdown) with evidence classes; simulation excluded unless requested | `pilot_report.py` | `test_pilot_report_consistency_and_synthetic_label`, `test_pilot_report_excludes_simulation_unless_asked` |
| Economic model that refuses to compute without inputs | `economics.py` | `test_workflow_study_and_economics`, `test_economics_net_and_statements` |

## Exact commands

```bash
.venv/bin/python -m pytest -q tests/
```

```bash
scripts/reproduce_all.sh
```

```bash
scripts/demo_walkthrough.sh
```

For a customer sample: `validate <path>` → `shadow <path> --name X [--rules-config rules.json]` →
`pilot-report <dataset> [--analysis-config a.json] [--study S] [--assumptions costs.json]` → `delete-dataset <dataset> --yes`.

## Tests and outcomes

**45 passed, 0 failed.** Each Phase 3 section 14 check maps to a test:

| Check | Test |
|---|---|
| Metrics reconcile with source records | `test_ops_metrics_reconcile_and_planted_patterns`, `test_pilot_report_consistency_and_synthetic_label` |
| Missing data is not treated as zero | `test_metrics_not_computable_without_sessions`; wc-C availability `not_computable` |
| Invalid episodes vs valid failed tasks | `test_valid_failed_task_is_preserved`; ops test |
| Time calculations use correct timestamps and denominators | ops test (480 eligible, session-hours from session events) |
| Duplicate events do not inflate incidents | ops test: GRIPPER_OVERCURRENT stays at 8 despite 8 duplicated rows |
| Human-review statistics | `test_review_statistics` |
| Economic estimates need explicit assumptions | `test_workflow_study_and_economics`, `test_economics_net_and_statements` |
| Shadow mode does not modify the source | ops test (fingerprint before = after); shadow output |
| Deletion works as documented | `test_dataset_deletion`, `test_deletion_removes_reports_and_study_rows` |
| Reports identify synthetic data | `test_pilot_report_consistency_and_synthetic_label` |
| Simulator and regression workflows still work | `tests/test_sim.py` (18), `test_replay_eligibility_and_incident_regression` |

## Measured results (synthetic 3-workcell package, shadow mode)

- **Yield:** 89.4% = 429/480 (Wilson 95% CI 86.3–91.8).
- **Loss attribution:** 51 unusable episodes = 43 linked to `data_recording_failure:CAM_FRAME_DROP` (all on wc-B, 6
  sessions) + 8 with a missing outcome label (C005, one wc-C session).
- **Throughput:** 64.3 attempts, 63.2 completed and 57.5 usable episodes per recorded session-hour (7.47 h). Median
  episode duration is 31.2 s.
- **Reliability:**
  - Availability: wc-A 83.9% (0.40 h down / 2.49 h); wc-B 100% (no downtime events); wc-C not computable.
  - Incident frequency is 6.8 per session-hour (51 faults). Recovery wait totals 1,440 s.
  - MTTA is 3,600 s and MTTR 129,285 s, each with n = 1.
  - Repeat-after-resolution: 1/1. INC-7 recurred.
- **Associations:** CAM_FRAME_DROP appears in 6/6 cfg-v2 sessions and 0/18 others. Cycle time on wc-B rose from a median of
  29.9 s to 37.5 s after the change. Both are labelled associations.
- **Planted patterns:** all six surfaced. B002 flagged 5 of the 6 affected wc-B sessions.

## Workflow results

**None measured.** The counterbalanced study (`study plan/start/finish/report`) has 0 completed tasks, and 0 of 453
findings have been reviewed. No time-saving claim is made.

## Metrics that could not be evaluated

- Precision and recall on real or customer-labelled data.
- Time saved per triage task, and the economic value derived from it.
- Recurring-incident reduction over time.
- Availability for workcells without station events.
- Verified-only yield for label-only exports (0% by construction).

## Limitations and unresolved issues

These are listed in `LIMITATIONS.md` and `PHASE2_FINAL_REPORT.md` (Known issues). The main ones:

- All data is synthetic or simulated.
- The analyses were written by the same author who planted the patterns.
- There are no importers for LeRobot, MCAP or robomimic.
- The API has no authentication.
- Imported logs cannot be replayed.

## Proposed pilot and acceptance criteria

See `PHASE3_PILOT_OFFER.md` and `PILOT_ACCEPTANCE_CRITERIA.md`: one workflow, one approved export, 4 weeks, thresholds
agreed in week 1, a stop condition if review effort exceeds the value found, and a final decision meeting. Pricing is a
proposal to negotiate.

## Next customer-discovery actions

1. Run the `PHASE3_CUSTOMER_DISCOVERY.md` interview with 5–8 data-acquisition, field and evaluation engineers. Ask each for
   a recent incident walk-through.
2. Ask each team for an approved 500–2,000-episode export plus its event log, and record which formats arrive. This decides
   which adapter to build first.
3. Score stated interest, observed behaviour, data-sharing willingness and willingness to pay separately (sheet in the
   discovery doc). Do not build a larger platform until at least one team shares data.

## Strongest remaining differentiation risk

A well-resourced team can build the individual checks in days. This includes teams whose public postings (paraphrased in
`research/OPENAI_PUBLIC_SIGNALS.md`) describe hiring engineers to script the detection of recurring failures. Amperes is only worth adopting if the linked, auditable yield audit
finds issues the team's process misses, with less setup than writing it. That has not been shown on any real dataset.
