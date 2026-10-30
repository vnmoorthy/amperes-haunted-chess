# Pilot acceptance criteria

**Rule 1: nothing is promised before the customer baseline exists.** No yield gain, time saving, precision level or
ROI is promised before the customer's own baseline (B1–B4 below) is written down and signed off.

**Rule 2: every threshold is PROPOSED.** Thresholds are opening positions, agreed or changed jointly at kickoff.
They are then frozen before Amperes sees the dataset, in two files:
- `rules.json`, passed with `--rules-config` and recorded in `import_report.rule_settings`;
- `analysis.json`, passed with `--analysis-config` and recorded in the report's `analysis_settings`.

A criterion that was not measured is reported as "not evaluated", never as passed.

What exists so far is synthetic only (see [OPENAI_RELEVANCE.md](OPENAI_RELEVANCE.md) §2). None of it counts toward
these criteria.

## Baseline (customer-supplied, before any import)

| ID | Baseline item | Form |
|---|---|---|
| B1 | Current definition of "usable episode" and the current yield figure for the pilot dataset, with how it is computed | Written definition plus number, or "not tracked". Mapped to `usable_statuses` in `analysis.json`. |
| B2 | Known-issue list for the pilot dataset (episodes or sessions already known to be bad, and why) | Episode/session IDs with reasons, or "none recorded" |
| B3 | Current time to triage one batch the size of the pilot sample, existing workflow | Customer estimate now; measured as condition A in C8 |
| B4 | Hours the customer will budget for setup, review and the study | Hours by role |

## Criteria

| ID | Criterion | How measured (code / artifact) | Proposed threshold | Falsified if |
|---|---|---|---|---|
| C1 | Read-only on source; no robot commands | `shadow` output `source_unchanged`, `robot_commands_issued` | `true` and `0` on every run (not negotiable) | Any run shows `source_unchanged: false` |
| C2 | Import completeness after mapping | `validate` / `import`: `records_rejected` / `records_seen`, and records with dropped rows; every rejection has a reason | ≤ 2% rejected (proposed) | Above the threshold, or any rejection without a reason |
| C3 | Precision on the customer's data | `review add` on a random rule-stratified sample of findings; `review report <dataset>` → `precision_among_reviewed`. `info` notes are excluded automatically and counted in `info_notes_excluded`. | Overall ≥ 0.80 (proposed). Any rule < 0.50 is added to `disabled_rules` and reported | Overall below the threshold |
| C4 | New, real issues beyond the baseline | Findings or incidents reviewed `correct_actionable` that touch episodes **not** in B2 | ≥ 1 confirmed actionable issue not in B2 (proposed; may be raised once B2 is known) | Zero |
| C5 | Recall against known issues (only issue types the schema can express) | Share of B2 episodes that receive a finding or an `infrastructure_error`, `invalid_data` or `suspicious` status | ≥ 0.80 (proposed) | Below the threshold. Not evaluated if B2 is empty |
| C6 | Failed tasks are not discarded as bad data | Reviewers mark a sample of B1-usable failed episodes; share classed `valid_failed_task` | ≥ 0.95 (proposed) | Below the threshold |
| C7 | Yield-loss attribution is right | Report section "Yield loss attribution" (`ops.yield_loss_attribution`) attributes each non-usable episode once: linked event, else most severe non-info finding, else status. Reviewers judge the attributed cause on a random sample of non-usable episodes. | ≥ 0.80 of sampled attributions confirmed (proposed) | Below the threshold |
| C8 | Workflow time | `study report <study_id>`: status must be `measured` (≥ 10 completed tasks per condition), with `median_time_saved_per_task_s` | Set **after** condition A's median is known (proposed: ≥ 20% of that median) | Status `measured` and saving below the threshold. If the status is not `measured`: not evaluated, and no time claim is made |
| C9 | Net effort (stop condition) | Customer-logged hours on Amperes (setup + review + study overhead) vs. B4 and vs. C8 savings | Within the B4 budget. If C8 is measured, hours saved ≥ hours spent | Hours spent exceed B4 or exceed measured savings |
| C10 | Deletion at the end | `delete-dataset <id> --yes` output counts. This also removes every `reports/pilot/` directory whose name contains the id (single- and multi-dataset), the dataset's incident export files, and `workflow_tasks` rows whose `sample_id` starts with `<id>:`. **Still manual:** any copies outside `amperes_data/`. | All removed, with a deletion record signed by both sides | Any residual copy |

## Explicitly not promised

- A yield increase. The economic model treats it as an assumption (see [ECONOMIC_VALUE_MODEL.md](ECONOMIC_VALUE_MODEL.md)).
- Any ROI. ROI is stated only from a `measured` workflow study.
- Recall on defect types the customer's export does not describe (undeclared ranges, rates or staleness limits are
  not checked).
- Causes. Yield-loss attribution is record linkage, and bottleneck and correlation outputs are associations.
- Any transfer of simulator results to physical robots.
- Regression cases from customer logs. Imported log episodes are replay class D (all 480 in the synthetic package;
  `amperes_data/amperes.db`, table `episodes`, dataset `ds_20261029T210352_1e9285`), and `incidents to-regression`
  needs class A or B.
