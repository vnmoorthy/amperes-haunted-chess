# Pilot brief: usable-data audit for a robotics data-collection program

*Prepared for a robotics data-acquisition / evaluation team (written with OpenAI Robotics in mind). This is a proposal.
OpenAI has not requested, reviewed or endorsed it.*

## The problem, as public job postings describe it

OpenAI's public robotics postings (sources and dates in `research/OPENAI_PUBLIC_SIGNALS.md`) describe these
responsibilities, paraphrased:

- keeping many deployed workcells running through changing configurations, with little tolerance for downtime;
- finding recurring failures across workcells and their issue history;
- designing data collection and quality-control processes, and tooling to automate policy evaluation;
- tracking throughput, cycle time, downtime and data quality, with root-cause analysis of recurring issues.

The question behind all four: **of the episodes a program collected this week, how many are usable, and what caused the rest?**

## What Amperes does today (prototype, verified on simulated, synthetic and hackathon-robot data)

- Gives every exported episode one evidence-backed status. A failed task with clean data stays usable. A completed task
  with a recording failure does not.
- Attributes each unusable episode to a linked event (fault code, recording error, config change) or a specific
  data-quality rule, with episode-hours per cause.
- Separates robot faults from data-pipeline faults. On a live hackathon arm it showed that dropped reads came from the
  telemetry client, not the robot.
- Finds stations or sessions with the same failure signature (deterministic; optional Atlas Vector Search).
- Reports yield, throughput and availability with explicit denominators. It says "not computable" instead of guessing.

## Proposed 4-week shadow pilot

| Week | Activity | Output |
|---|---|---|
| 1 | Agree the definition of "usable", the rule thresholds and the stop conditions; receive one approved export | `rules.json` + `analysis.json` frozen and recorded in every report |
| 2 | Shadow-mode import (read-only; source fingerprint proved unchanged); yield audit and incidents | Pilot readiness report |
| 3 | Your engineers review a random sample of findings; a counterbalanced timing study (with vs without Amperes) | Measured precision on *your* data; measured triage time |
| 4 | Decision meeting | Expand, change, or stop. Deletion of all imported data on request |

**What we need from you:** an export of about 500–2,000 episodes (timestamps, streams, outcome labels if any) plus the
fault/maintenance/config log for the same window, and 2–3 engineer-hours a week for review.
**What we do not need:** source code, model weights, network access to robots, or write permissions of any kind.

## Success criteria (proposed, to be agreed before any data is processed)

- Most episodes in the approved export are processed without manual fixes.
- Reviewer-confirmed precision on sampled findings is at or above a level you set in week 1.
- Recurring causes are found that your current process had not already identified (reviewer-judged).
- Measured triage time per incident is lower in the assisted condition, at a sample size you consider adequate.

**Stop condition:** if review effort exceeds the value of the confirmed findings, we stop and say so in the report.

## Limits we will state up front

- There are no LeRobotDataset, MCAP or robomimic importers yet. Format conversion would be part of week 1.
- Rules check only what the data declares. Video content is not inspected.
- Simulation results in the repository say nothing about your robots.
