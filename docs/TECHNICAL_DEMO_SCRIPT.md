# Five-minute technical demo

Everything runs offline and uses no paid API. Every dataset shown is **synthetic** or **simulated**; say so at the start.

## Setup (before the meeting, about 2.5 minutes)

```bash
scripts/reproduce_all.sh
```

## Live (about 10 s of compute)

```bash
scripts/demo_walkthrough.sh
```

Then open the UI for the same data:

```bash
.venv/bin/python -m amperes.cli serve
```

## Talk track, with the results measured on 2026-10-29

| # | Show | What the audience sees (actual output) |
|---|---|---|
| 1 | **Import** a sample: a fictional 3-workcell program, 480 episodes, 150 events, in shadow mode | `shadow` returns `source_unchanged: true`, `robot_commands_issued: 0`, and prints the delete command |
| 2 | **Processing**: one status per episode | 362 `valid_success_unverified` · 67 `valid_failed_task` · 43 `infrastructure_error` · 8 `suspicious` |
| 3 | **A known data defect** with evidence | `C004_truncated`: `obs.joint_pos` and `obs.gripper` stop 14.4 s before the episode's `end_time` |
| 4 | **A valid failed task vs an invalid record**, plus an episode left unflagged | A failed grasp with clean data has **no findings** and stays usable. A truncated recording with a `CAM_FRAME_DROP` event is `infrastructure_error`. A clean success shows only the info-level "unverified success" note |
| 5 | **Open an incident** | `CAM_FRAME_DROP`: 43 occurrences, 43 episodes, 6 sessions. Grouped by "same event_class and fault code". Facts and the unverified hypothesis are shown separately |
| 6 | **Grouping repeated symptoms** | Association: the code appears in 6/6 sessions on `cfg-v2` and 0/18 on other configs, labelled "not evidence that the configuration causes the fault". Status moves open → investigating, and the history is recorded |
| 7 | **Replay** | The imported incident cannot become a regression case: all 43 members are replay class **D** (log only). A simulator collision reproduces **exactly** in both modes (max deviation 0.0 m) |
| 8 | **Regression case** | A simulator incident becomes a case. Against baseline: `still_failing: 1`. Against `{"obstacle_aware": true}`: `fixed: 1` |
| 9 | **Export** | `pilot_readiness.md` / `.json` show yield 89.4% (429/480, CI 86.3–91.8). **Loss attribution:** 51 unusable = 43 linked to `CAM_FRAME_DROP` (wc-B) + 8 missing outcome labels. wc-C availability is "not computable"; workflow time is "MISSING"; simulation is "NOT INCLUDED" |
| ✱ | **A limitation, shown honestly** | Label flips in records without state evidence: detected **0/4**, by design. The tool cannot check a label it has no evidence for |

## UI path (if screens are preferred)

Overview → Operations (yield, availability, bottlenecks) → Incidents (CAM_FRAME_DROP, facts vs hypotheses, status change)
→ Episodes (filter by `infrastructure_error`, open one, red markers on the stream chart) → Evaluation (held-out table:
862 → 949/1000, the alternative broke 33) → Export.

## What not to say

- That it saves time. This is not measured.
- That it improves real robots. This is simulation only.
- That OpenAI or any customer wants it. The only evidence is public job postings.
