# Phase 3 product decision

The inputs were the repository state after Phase 2 (38 tests passing; clean run by `scripts/reproduce_all.sh`), the public
OpenAI signals in `research/OPENAI_PUBLIC_SIGNALS.md`, and the tool review in `COMPETITIVE_GAP.md`. **No customer interviews
have taken place**, so "evidence of a real customer problem" rests only on public job postings and is scored conservatively.

Scores run from 1 (weak) to 5 (strong).

| Criterion | A. Episode QC & usable-data yield | B. Workcell failure / downtime | C. Reproduction & regression | D. Throughput bottlenecks | E. Combined |
|---|---|---|---|---|---|
| Evidence of real problem | 3: Robotics Software Engineer and Data Lead postings name data QC and dataset validation | 3: Field Engineer posting names recurring failures and downtime across workcells | 2: evaluation tooling is named, but reproduction of *physical* failures is not | 3: Ops PM posting names throughput, cycle time and downtime | 3 |
| Demonstrable on a small approved dataset | 5: needs only exported episodes | 3: needs fault/maintenance logs too | 2: needs a simulator or replayable logs | 3: needs session/station events | 3 |
| Feasibility (what exists now) | 5: built, 26 rules, evaluated | 4: built (events, incidents, MTTA/MTTR) | 4: built for sim only | 4: built (B001–B007) | 4 |
| Integration effort (5 = low) | 4: one export | 3: two exports | 1: simulator or replay access | 3 | 2 |
| Differentiation | 2: single rules are easy to script | 2: overlaps fleet tools | 3: rigorous protocol, but sim-only | 2 | 3: the join of episode quality and ops events is the unusual part |
| Measurability | 5: precision on reviewed findings, yield | 4: MTTR, recurrence | 4: reproduction rate | 3 | 4 |
| Recurring usage | 4: every collection batch | 4 | 3: per policy change | 3 | 4 |
| Risk of duplicating internal tooling (5 = low risk) | 2 | 2 | 3 | 2 | 3 |
| Expansion potential | 4: toward data ops | 3 | 4: toward eval infrastructure | 3 | 4 |
| **Total (/45)** | **34** | **28** | **26** | **26** | **30** |

## Decision

- **Primary wedge: A.** The product is a read-only usable-data yield audit of exported episodes. Each episode gets exactly
  one evidence-backed status; failed tasks count as usable; recording and infrastructure failures are kept separate; yield
  is reported with its numerator, denominator and rule version.
- **Supporting capability: B, limited to linking.** Operational events (faults, recording errors, configuration changes,
  station availability) are joined to the episodes they affect. This attributes unusable episodes to recorded
  operational events and catches recurrence after resolution.

**Not sold in the first pilot:** the simulator reproduction and intervention loop (C). It stays in the repository as a
reproducible test bench, because it requires access a first pilot should not ask for.

## Why this wedge, stated with its weakness

A team's own engineers can write each check. What is less common is one deterministic classification per episode that
joins data-quality evidence with the operational event log, and makes the yield number auditable to the row. The
COMPETITIVE_GAP review found that fleet tools see uptime but not data usability, and that dataset tools see episodes but not
workcell faults. That is a hypothesis about a gap, not a validated one. The pilot must test whether this join surfaces
issues the team's current process misses, at a precision their engineers accept.

## Implemented in Phase 3 for this wedge

- An operational event model with 8 explicit fault classes, and recording-error events attached to episodes. A completed grasp
  with a recording failure is classed `infrastructure_error`, not "success".
- **Yield-loss attribution:** every non-usable episode is attributed once, to its linked operational event or else its most
  severe finding, with episode-hours, sessions and workcells. This is the wedge's core output, added after an internal
  documentation review found that it was missing.
- Yield, throughput and reliability metrics with explicit denominators, where missing inputs are reported as `not_computable`.
- Bottleneck rules B001–B007 with minimum sample sizes, and cross-session repetition versus association versus hypothesis.
- Human review with precision among reviewed findings, a counterbalanced workflow-timing study, an assumption-driven
  economic model, shadow mode with a source-unchanged proof, and the pilot readiness report.
