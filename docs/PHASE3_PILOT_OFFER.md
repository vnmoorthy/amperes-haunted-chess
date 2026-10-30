# Phase 3 pilot offer (draft for discussion)

This is a draft, not a contract. All thresholds and the price are **proposals** to be agreed jointly. No customer
has accepted or reviewed this offer.

- Stages: [PILOT_PLAYBOOK.md](PILOT_PLAYBOOK.md)
- Criteria: [PILOT_ACCEPTANCE_CRITERIA.md](PILOT_ACCEPTANCE_CRITERIA.md)
- Economics: [ECONOMIC_VALUE_MODEL.md](ECONOMIC_VALUE_MODEL.md)

## 1. One workflow

**Weekly usable-data triage for one collection program.** The question each week: of last week's episodes, which
are usable, where did the lost ones go (yield-loss attribution with episode-hours), and which failures recur?
Nothing else is in scope.

## 2. One approved dataset

- **What:** one export covering a bounded window (proposed: one week, one site, up to 3 workcells), approved in
  writing by the customer data owner.
- **Format:** the CSV package or `amperes.episode/1` JSONL ([INTEGRATION_GUIDE.md](INTEGRATION_GUIDE.md)).
- **Mapping:** done in Stage 1. No other adapter exists.

## 3. Evaluation window

Proposed 4 weeks:

| Week | Stages |
|---|---|
| 1 | Baseline and mapping (Stages 0–1) |
| 2 | Shadow run and review (Stages 2–3) |
| 3 | Workflow study (Stage 4) |
| 4 | Report and decision meeting (Stage 5); deletion within 5 working days after (Stage 6) |

## 4. Documented baseline (before Amperes sees the data)

Baseline B1–B4 from the acceptance criteria:
- B1: the current usable definition and yield
- B2: the known-issue list
- B3: current triage time
- B4: hours budget

These are signed off by the customer technical lead. Without B1–B4 the pilot does not start.

## 5. Data boundaries

- **Local only.** Processing runs on a customer-controlled machine.
  - No model provider ships with Amperes (`amperes/diagnostics.py`), and the code contains no network client.
  - The local UI/API binds to 127.0.0.1 unless `--allow-remote` is given (`amperes/cli.py`).
- **Read-only.** The source fingerprint is checked before and after the run (`source_unchanged`), and no robot
  commands are issued.
- **Access.** Reports and findings stay on that machine. Reviewers use pseudonymous IDs (e-mail addresses are
  rejected by `review add`).
- **No simulator content.** Reports about the customer dataset carry no simulator results unless `--experiment` or
  `--include-latest-sim` is passed. The section otherwise reads "NOT INCLUDED".
- **Deletion.** `delete-dataset --yes` removes:
  - the dataset's records and reviews;
  - its `workflow_tasks` rows;
  - its artifact directory, its `reports/pilot/<id>/` directory and its incident export files.

  Multi-dataset report directories and any copies outside `amperes_data/` must be removed by hand. Both sides sign a
  deletion record.

## 6. Jointly agreed success criteria (thresholds PROPOSED for discussion)

| Criterion | Proposed threshold |
|---|---|
| C1 Read-only | always true (not negotiable) |
| C3 Precision among reviewed (info notes excluded) | ≥ 0.80 overall; rules < 0.50 moved to `disabled_rules` |
| C4 Confirmed actionable issues not in B2 | ≥ 1 |
| C5 Recall on B2 known issues (expressible types only) | ≥ 0.80 |
| C6 Valid failed tasks kept usable | ≥ 0.95 |
| C7 Sampled yield-loss attributions confirmed by reviewers | ≥ 0.80 |
| C8 Median triage time saved (study status `measured`) | set after the condition-A median is known; proposed ≥ 20% |
| C9 Net effort | customer hours ≤ B4 budget, and ≤ measured hours saved if C8 is measured |

The thresholds and settings are frozen at the end of week 1, before the shadow run:
- `rules.json`, recorded as `import_report.rule_settings`;
- `analysis.json` (usable statuses, `rejected_tolerance`, `min_session_episodes`), recorded as
  `analysis_settings`.

## 7. Named responsibilities (roles)

| Role | Side | Responsible for |
|---|---|---|
| Decision owner | Customer | Scope, budget, final decision |
| Data owner | Customer | Approving the dataset and boundaries, signing the deletion record |
| Technical lead | Customer | Export, stream declarations, event mapping, config files, baseline B1–B3 |
| Reviewers (≥ 2) | Customer | Review decisions and attribution checks; study tasks under both conditions |
| Pilot engineer | Amperes | Mapping support, runs, report generation, deletion |
| Evaluation lead | Amperes | Freezing criteria, writing the pass/fail record, disclosing limitations |

## 8. Exclusions

- Live or streaming integration, ticketing-system integration, and multi-site rollout.
- Any change to customer data or systems.
- Robot control.
- Causal claims. Attribution is record linkage; bottleneck and correlation outputs are associations.
- Regression tests from customer logs (replay class D).
- Simulator results applied to physical robots.
- Evaluating individual operators. An `operator_id` filter exists, but it is used only if the customer opts in, and
  only with pseudonymous IDs.
- Hardware test data and safety analysis.
- Any ROI not derived from a `measured` workflow study.

## 9. Stop condition

The pilot stops early if any of these happens:
1. **The tool adds more work than it removes.** Customer-logged hours on Amperes exceed the B4 budget. Or, once C8
   is measured, hours spent exceed hours saved.
2. **Precision is too low.** After the first review batch (proposed: 30 findings, info notes excluded), precision
   among reviewed is below 0.50.
3. **Read-only fails.** `source_unchanged` is ever false.
4. **The customer stops,** for any reason. Deletion follows within 5 working days.

## 10. Final decision meeting (end of week 4)

- **Attendees:** decision owner, technical lead, reviewers, evaluation lead.
- **Agenda:**
  - Each criterion: pass, fail or not evaluated, with its artifact.
  - Limitations.
  - The economic estimate, quoted only if the study status is `measured` and a saving was measured.
- **Outcomes:**
  - **Continue:** the paid recurring weekly audit.
  - **Extend:** one named gap, with a new end date.
  - **Stop:** deletion.
- The decision is recorded in writing.

## 11. Pricing (proposal, negotiable)

We have no pricing evidence. The figures below are placeholders for negotiation, not quotes.

| Item | Proposal |
|---|---|
| 4-week pilot | Fixed fee, set at scoping and capped below the customer's own estimate of building the same 4-week scope internally. Alternatively no fee, in exchange for the decision meeting, written reviewer feedback, and permission to cite anonymised aggregate results. |
| Follow-on (if Continue) | Recurring fee per weekly audit of one program, with the pilot fee credited. The amount is proposed only after the pilot has measured customer hours. |
| Refund / exit | If C1 fails, or the pilot stops under stop-condition 1 or 2, no fee is due. |
