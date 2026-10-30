# Customer discovery: assumptions to validate

Status 2026-10-29: **none of these assumptions has been tested.** No interviews have been run and no customer data
has been seen. Public job postings (see [research/OPENAI_PUBLIC_SIGNALS.md](research/OPENAI_PUBLIC_SIGNALS.md))
show what one company hires for. They are not evidence for any assumption below, and in particular not for
willingness to pay.

Interview script and scoring sheet: **[PHASE3_CUSTOMER_DISCOVERY.md](PHASE3_CUSTOMER_DISCOVERY.md)**.
Competitive context: [COMPETITIVE_GAP.md](COMPETITIVE_GAP.md).

## Plan

- **Sample:** 10–12 interviews, covering at least 4 organisations and all 4 personas (data acquisition,
  research engineering, ops/field, TPM). At most 3 interviews per organisation.
- **Thresholds are set before interviewing.** The thresholds below were chosen in advance and are judgement
  calls, not statistics. Do not change them after seeing results; if they turn out to be wrong, record why.
- **What counts as evidence:** only *observed behaviour*, *data sharing* and *payment* scores from the scoring
  sheet. *Stated interest* never counts on its own.

## Assumptions

### Problem
| ID | Assumption | How tested | Falsified if |
|---|---|---|---|
| P1 | Teams regularly lose usable data to operational problems (faults, config drift, sensor issues) and find out late. | Incident walk-through (Q4–11): recency, detection lag, cost | Fewer than 5 of 10 interviewees can describe such an incident from the last 60 days, **or** most say it was caught within the same shift |
| P2 | Failed-task episodes are confused with invalid data, in either direction: good failures discarded, or bad data kept. | Q17, Q12–14, research-engineer add-on | Fewer than 3 interviewees describe a concrete case of either confusion |
| P3 | Diagnosing a recurring failure takes significant engineer time because records can't be joined. | Q7–9, Q16, ops add-on | The median reported diagnosis time is under 1 hour, **or** records are already joinable in their existing tools |
| P4 | Failures are rarely turned into regression tests. | Q20 | Most teams already turn understood failures into tests routinely, in under a day |

### User
| ID | Assumption | How tested | Falsified if |
|---|---|---|---|
| U1 | The primary user is the ops/field or data-acquisition engineer, not the researcher. | Who describes the pain most concretely; who owns incidents (Q19, Q24) | The pain and ownership consistently sit with research or ML infra instead. Re-target, don't stop |
| U2 | One person or role owns "usable data yield". | Q18, Q24, TPM add-on | No one owns it and no one reports it. The problem may then be real but unbuyable |
| U3 | The right customers are small to mid-size teams, not the largest labs. | Compare observed-behaviour scores by team size; ask what they've built (Q21) | Small teams report no pain, **or** large teams show the only high observed-behaviour and payment scores |

### Data availability
| ID | Assumption | How tested | Falsified if |
|---|---|---|---|
| D1 | Teams record enough per episode (timestamps, signals, outcome labels) for deterministic checks. | Q15, Q17 | Most lack timestamps or outcome labels at episode level |
| D2 | Operational events exist in machine-readable form and can be joined to episodes by time, station or session. | Q16; ask to see a schema (not data) | Events live only in chat or free-text tickets for most teams |
| D3 | At least some teams will share an approved, sanitised sample. | Data-sharing score; actual sample received within 4 weeks | No sample or schema received from any organisation within 4 weeks of the interviews |

### Integration
| ID | Assumption | How tested | Falsified if |
|---|---|---|---|
| I1 | A local, read-only tool is deployable under their security constraints. | Q22 | Most require vendor security review or on-prem procurement before any trial |
| I2 | Importers for 1–2 formats cover most teams. Candidates: LeRobotDataset v3, MCAP/rosbag2. | Q15 tallies | Formats are fragmented with no format shared by 3 or more teams, **or** formats are proprietary internal ones |
| I3 | Output must land in existing tools (tickets, Foxglove/Rerun, W&B), not in a new UI. | Q19, Q22 | Interviewees ask for a separate UI. That would make the assumption wrong in the convenient direction, so double-check it |

### Willingness to pay
| ID | Assumption | How tested | Falsified if |
|---|---|---|---|
| W1 | Someone with budget would fund a scoped paid pilot. | Payment score; Q24 | No interviewee scores 2 or more on willingness to pay |
| W2 | The value can be expressed as recovered usable hours or saved engineer time. | Q9, Q18; ask how they'd judge a 2-week trial (Q23) | They can't name any metric a trial could move |
| W3 | Buying beats building for at least some teams. | Q21: what they've built, how long it took, who maintains it | Every team with the problem has already built an adequate internal tool, or says it would take under 2 weeks |

### Differentiation
| ID | Assumption | How tested | Falsified if |
|---|---|---|---|
| X1 | Existing tools (Foxglove, Rerun, LeRobot, Formant/InOrbit, Pandera/GX) do not already link operational events to data usability. | Q21 plus a docs check per tool named | Interviewees show it already done in a tool they use |
| X2 | The validity classes (invalid / suspicious / valid-failed-task / infra-error / verified-success / unknown) match how teams think. | In the close (after the pitch-free questions), show the class list only and ask them to sort their last incident into it | Most can't place their incident, or they use materially different categories |
| X3 | On a team's own approved sample, the audit finds unusable or mislabelled episodes their current process missed, at a precision they accept. | Shadow-mode run on a sample, with findings reviewed by the team | Zero new true findings, **or** reviewed precision below the bar the team set in advance |
| X4 | Setup takes less time than scripting the same checks internally. | Time the setup on a real sample; compare with their estimate for building it (Q21) | Setup takes longer than their build estimate |

## Decision rule (pre-registered)

- **Continue to a pilot** only if all of these hold: P1 holds; D3 is met by at least 1 organisation; W1 is met by
  at least 1 interviewee; and X3 is tested on a real sample. Otherwise, narrow the scope (see COMPETITIVE_GAP.md,
  "narrowest capability") or stop.
- Record outcomes in this file with dates and anonymised interview IDs. Do not record which organisation
  said what without consent.
