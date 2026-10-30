# Competitive gap: Amperes Failure Lab

Compiled 2026-10-29. Every Amperes capability below is **planned or being built** (see
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)). None has been measured. "Unverified" marks a hypothesis about
users or the market that has no evidence yet. Descriptions of other tools come from their current official docs,
where we could fetch them. A tool not doing something in its docs does not prove it can't do it; plugins and user
code often fill gaps.

## What Amperes proposes (for reference)

- **Import.** Canonical `amperes.episode/1` JSONL, plus a CSV telemetry+event adapter. **No LeRobotDataset, MCAP,
  rosbag2 or robomimic HDF5 importer is in the plan.**
- **Deterministic data-quality rules.** Completeness, timestamp integrity, NaN, range, stale signals,
  outcome-label contradictions, duplicates.
- **Per-episode class.** invalid / suspicious / valid-failed-task / infra-error / verified-success / unknown. A
  failed task is not treated as unusable data.
- **Operations layer.** Operational events (workcell, session, fault, recovery, maintenance, config change).
  Incidents with status history and evidence. Usable-data yield, throughput, downtime, bottleneck findings.
  Human review and shadow mode.
- **Simulation layer.** A MuJoCo gantry pick-and-place sim with a scripted controller, an independent
  ground-truth verifier, a failure taxonomy, reproduction, perturbations, an intervention tested on a held-out
  set, and a regression suite.
- No LLM in the core loop. Local-first (SQLite + artifact directory).

---

## Tool-by-tool

### MuJoCo
- **Already does:** general-purpose physics engine with optimization-based contact, MJCF/URDF models, C API, Python
  bindings, GPU backends (MJX, MuJoCo Warp). Its overview documents no task-success verification, failure
  taxonomy, or evaluation tooling. https://mujoco.readthedocs.io/en/latest/overview.html
- **Amperes adds:** nothing to MuJoCo itself. Amperes is a MuJoCo *user*. It adds a task verifier, failure
  taxonomy, perturbation sets and a regression suite around one simple scene.
- **Evidence for the distinction:** none needed against MuJoCo. Against frameworks built on MuJoCo, see below.

### robosuite (v1.5)
- **Already does:** MuJoCo-based benchmark environments, many robot embodiments (including humanoids), composite
  controllers, teleoperation devices, and demo collection and replay
  (https://robosuite.ai/docs/overview.html). Each environment defines task success in code. For example, `Lift`
  has `_check_success()`, which the reward uses
  (https://github.com/ARISE-Initiative/robosuite/blob/master/robosuite/environments/manipulation/lift.py).
- **Amperes adds:** a taxonomy of *why* an episode failed (not just whether), deterministic reproduction of a
  failure from its recorded seed and perturbation, and a regression suite built from reproduced failures.
- **Evidence needed:** proof that robosuite users want failure-cause labels beyond success/failure, and do not
  already write them per project. *Unverified.* Amperes' gantry scene is simpler than robosuite's arms, so a
  robosuite adapter would be needed for the result to matter to those users.

### RoboCasa / RoboCasa365
- **Already does:** RoboCasa365 v1.0 (2026-02-18; v1.0.1 2026-05-12) has 365 tasks, 2,500+ kitchen scenes, 600+ h of
  human demos and 1,600+ h of synthetic demos. It offers multi-task, foundation-model and lifelong-learning
  benchmarks and a leaderboard. A subtask annotation pipeline generates per-frame labels from sim state
  (2026-10-14). https://robocasa.ai/
- **Amperes adds:** failure taxonomy, reproduction and regression tests. Scene diversity, task breadth and
  benchmark scale are **not** what Amperes offers.
- **Evidence needed:** proof that RoboCasa's per-frame subtask labels do not already let users find where a policy
  failed. Plausibly they do, which would weaken the distinction. *Unverified.*

### LeRobot (Hugging Face): format and tools
- **Already does:**
  - LeRobotDataset v3: Parquet for state, action and timestamps; MP4 per camera; metadata for schema, FPS,
    normalization stats, tasks and per-episode offsets; streaming from the Hub.
    https://huggingface.co/docs/lerobot/en/lerobot-dataset-v3
  - `lerobot-edit-dataset`: delete episodes, split, merge, add/remove features, modify tasks, convert to video,
    re-encode, recompute stats, info.
  - Dataset Visualizer: synced cameras and state/action charts. Its "Advanced" view offers dataset statistics,
    **filtering to flag problematic episodes**, first/last-frame overviews and action insights.
  - `lerobot-dataset-viz` renders episodes in Rerun or Foxglove.
  - Source for the last three items: https://huggingface.co/docs/lerobot/main/using_dataset_tools
- **Amperes adds:**
  - A documented, deterministic rule set with one episode class each (e.g. *valid data, failed task* vs.
    *invalid data*).
  - Outcome-label contradiction checks.
  - Linking episodes to operational events (faults, maintenance, config changes).
  - Incident grouping and usable-data yield.
- **Evidence needed:**
  - What the visualizer's "flag problematic episodes" filter actually detects. Not checked; it may overlap with
    Amperes' rules. *Unverified.*
  - A LeRobotDataset importer, which is not in the plan. Without one, LeRobot users would have to convert to
    Amperes' format, which is a real barrier.

### robomimic
- **Already does:**
  - HDF5 datasets with per-demo states, actions, rewards, dones and observations, plus "filter keys" naming demo
    subsets.
  - `get_dataset_info.py` lists filter keys and checks actions fall in the normalized range.
  - Train/val split tooling and validation during training.
  - Source for the above: https://robomimic.github.io/docs/datasets/overview.html
  - Multi-Human datasets label operators by proficiency ("worse/okay/better").
    https://robomimic.github.io/docs/datasets/robomimic_v0.1.html
- **Amperes adds:** broader rule coverage (timestamps, staleness, label contradictions) and an episode validity
  class that is separate from operator skill.
- **Evidence needed:** proof that robomimic users hit data defects that its action-range check misses. *Unverified.*
  No HDF5 importer is planned.

### Foxglove
- **Already does:**
  - Data platform indexing MCAP/ROS 1 bag recordings by device, time and topic, with metadata and attachments.
  - Devices with custom properties, and sessions.
  - **Events:** structured, typed annotations on time ranges, created in the app or by API.
  - Search and Gallery, webhooks on new recordings/devices/events, and the Foxlet on-robot upload agent.
  - Source for the above: https://docs.foxglove.dev/docs/data
  - An April 2026 launch added data search and curation (tagging and enriching events), self-hosting and
    bring-your-own-storage. https://www.businesswire.com/news/home/20260421818840/en/ (403 to our fetcher; we
    relied on a search summary.)
- **Amperes adds:** rules that *generate* findings automatically, per-episode validity classes, incidents that
  group recurring findings across sessions, and yield metrics. Foxglove's docs describe events and webhooks but no
  built-in rule engine or data-quality checks.
- **Evidence needed:**
  - Whether Foxglove customers already script this on top of the events API. Likely, and *unverified*.
  - If so, Amperes' realistic form is a rule pack that *writes Foxglove events*, not a replacement.
  - No MCAP importer is planned.

### Rerun
- **Already does:**
  - A catalog of recordings as queryable segments within datasets.
  - Dataframe queries (DataFusion) against an open-source local server or a commercial Rerun Hub.
    https://rerun.io/docs/reference/dataframes
  - Its "Refine" guide shows derived layers, **including quality verdicts and per-frame blur scores**, added
    without modifying the raw recording, then queried across the dataset. https://rerun.io/learn/refine
- **Amperes adds:** a predefined robotics rule set and taxonomy, incident workflow, and operations-to-yield
  metrics. Rerun supplies the substrate (layers and queries) where such verdicts could live.
- **Evidence needed:** proof that teams want a *ready-made* rule set rather than writing verdict layers themselves.
  *Unverified.* The overlap is substantial.

### Weights & Biases / MLflow
- **Already does:**
  - W&B (docs now under CoreWeave): experiment tracking, sweeps, artifacts that version datasets and models with
    lineage and external references, registry and reports. https://docs.coreweave.com/models/ and
    https://docs.coreweave.com/models/artifacts/
  - MLflow: tracking, model registry, deployment, and GenAI tracing and evaluation. No robotics-specific features
    in its docs. https://mlflow.org/docs/latest/
- **Amperes adds:** robotics episode semantics that these tools do not model (validity classes, operational
  events, incidents).
- **Evidence needed:** none against these tools. Amperes should *log into* them, not compete. A team already
  versions data in W&B Artifacts, so Amperes must attach to that lineage to be useful. *Unverified.*

### ROS 2 rosbag2
- **Already does:**
  - `ros2 bag` verbs: record, play, info, convert, list, reindex, burst.
  - MCAP (default) and sqlite3 storage, zstd compression, snapshot mode, and topic/time filtering.
  - `info` shows size, duration and message counts. No data-quality validation is documented.
  - Source for the above: https://github.com/ros2/rosbag2/blob/rolling/README.md
- **Amperes adds:** post-hoc quality rules and episode classes over recorded data.
- **Evidence needed:** proof that ROS 2 teams lack such checks today. Many likely have scripts. *Unverified.* No
  rosbag2/MCAP importer is planned.

### Fleet monitoring: Formant, InOrbit
- **Already does:**
  - **Formant:** ROS and non-ROS telemetry ingestion, events and notifications (Slack, PagerDuty, webhooks,
    SMS), threshold-triggered incidents, analytics (uptime, incident rate, time to resolution, custom SQL),
    teleoperation and intervention requests. https://docs.formant.io/ and
    https://docs.formant.io/docs/incident-management
  - **InOrbit:** robot agent, status definitions (OK/WARNING/ERROR) and incident definitions with severities
    SEV0–3 and notification channels, mission tracking, KPI definitions. https://developer.inorbit.ai/docs
- **Amperes adds:** a link from operational events to *dataset usability*, meaning how many collected
  episodes/hours are usable for training and what caused the losses. The fleet tools measure robot uptime and
  incidents, not whether the data is fit to train on.
- **Evidence needed:** whether data-collection teams use these platforms at all, and whether they already compute
  yield from them with custom SQL. *Unverified.* Formant's custom SQL could plausibly cover it.

### Generic data validation: Great Expectations, Pandera
- **Already does:**
  - **GX Core:** Expectations, Suites, Validation Definitions, Checkpoints, Actions (e.g. alerts) and Data Docs
    over files, SQL and Spark. https://docs.greatexpectations.io/docs/core/introduction/gx_overview
  - **Pandera:** DataFrame schemas and checks across pandas, Polars, PySpark, Ibis and others, with lazy
    validation, grouped error reports and hypothesis tests. https://pandera.readthedocs.io/en/stable/
- **Amperes adds:** robotics-specific semantics (episode boundaries, success-label contradictions, stale-signal
  rules tied to control rate) and an episode class.
- **Evidence needed:** honestly, most of Amperes' individual rules can be written as Pandera/GX custom checks in
  days. The rule *engine* is not a differentiator. Only a vetted robotics rule library plus episode semantics
  might be. *Unverified.*

### Robot policy evaluation: SIMPLER, AutoEval, RoboArena
- **Already does:**
  - **SIMPLER:** simulated evaluation of real-robot policies (Google Robot, WidowX) whose results correlate with
    real-world performance (Pearson, MMRV), plus robustness tests under visual shifts. https://simpler-env.github.io/
  - **AutoEval:** autonomous real-world evaluation with learned success classifiers and reset policies, a
    submission queue, and per-episode reports with videos on WidowX cells. https://auto-eval.github.io/ and
    https://arxiv.org/abs/2503.24278
  - **RoboArena:** distributed, double-blind pairwise real-world evaluation on DROID across institutions,
    aggregated into a policy ranking. https://arxiv.org/abs/2506.18123
- **Amperes adds:** a failure taxonomy, reproduction and regression tests. However, Amperes evaluates **only its
  own scripted controller in its own toy scene**. It does not evaluate learned policies or real robots.
- **Evidence needed:** Amperes would have to run on a team's real evaluation logs (e.g. AutoEval-style reports)
  and show that failure grouping changes a decision. *Unverified.* Today the distinction is in kind (diagnosis vs.
  ranking), not in demonstrated value.

---

## Summary of overlap

| Amperes capability (planned) | Closest existing coverage | Overlap |
|---|---|---|
| Episode format and import | LeRobotDataset v3, MCAP/rosbag2, robomimic HDF5 | High. Amperes adds a format, which is a cost to users, not a benefit. |
| Deterministic quality rules | Pandera/GX, LeRobot visualizer filters, Rerun verdict layers | Medium–high |
| Episode validity class (failed task ≠ bad data) | Nothing found as a documented default | Low (possible gap; *unverified*) |
| Ops events → usable-data yield | Formant/InOrbit (uptime, incidents) and Foxglove events, but not linked to data usability | Low–medium (possible gap; *unverified*) |
| Incidents with status history | Formant, InOrbit, generic ticketing | High |
| Sim failure taxonomy / reproduction / regression | robosuite/RoboCasa success checks, SIMPLER, AutoEval | Medium. Amperes' sim is the weakest part relative to these. |

## Why would a well-resourced robotics team use Amperes instead of building a small internal tool?

**Today the answer is weak.**

- **The pieces are easy to build in-house.** A well-resourced team already has logs, a ticket system, dashboards
  and engineers whose job descriptions include writing exactly this kind of tool. OpenAI's public Field Engineer
  and Robotics Software Engineer postings ask for lightweight analysis scripts, diagnostic tools and
  quality-control processes ([research/OPENAI_PUBLIC_SIGNALS.md](research/OPENAI_PUBLIC_SIGNALS.md), A1, A2). The
  rules Amperes proposes are individually simple and can be written with Pandera or SQL in days. Incidents
  duplicate the team's ticketing system.
- **Amperes would add costs.** It introduces a new schema, has no importers for the formats teams actually use
  (LeRobotDataset, MCAP/rosbag2, HDF5), and needs access to sensitive data that such teams guard closely.
- **The evidence is synthetic.** The simulation layer is a toy gantry scene, and the defect corpus is injected
  defects. Nothing yet shows Amperes finds a real problem that an internal script would miss.
- **Possible reasons a team would still use it (all unverified):**
  - Faster time to a first answer for a small or new team.
  - A tested rule library and taxonomy the team doesn't have to design.
  - An evaluation built by someone other than the team being evaluated.

**The narrowest capability that could make it genuinely more valuable.** A read-only **usable-data yield audit**
with these properties:
- It reads a team's *existing* episode data (LeRobotDataset v3 or MCAP first) and its *existing* operational
  event/ticket export, with no schema migration.
- It assigns each episode exactly one deterministic class with machine-checkable evidence. The key split is
  "invalid data" vs. "valid data, failed task" vs. "infra error".
- It attributes every unusable episode-hour to an operational cause: fault, config change, maintenance, station,
  or shift.

The gap it targets sits between existing tools. Fleet tools know uptime but not whether the data is usable.
Dataset tools know episodes but not workcell faults. The audit is worth buying only if it meets all of these
conditions, which no current evidence shows:
- On the team's own approved sample, it finds unusable or mislabelled episodes their current process missed.
- It does so at a precision they accept.
- It takes less setup time than an engineer would spend scripting the same check.

A secondary narrow option is to publish the labelled defect corpus as a **benchmark for teams' internal
checkers**. It has value even to teams that build their own tooling, but it is a different product.

## Gaps in Amperes' own plan exposed by this review

1. No LeRobotDataset, MCAP/rosbag2 or robomimic HDF5 importer (only canonical JSONL and CSV).
2. The simulator is a Cartesian gantry, not an articulated arm, so sim findings do not transfer to robosuite or
   RoboCasa users without an adapter.
3. There is no way to export findings as Foxglove events or Rerun layers, or to attach them to W&B artifact
   lineage.
4. Detector metrics cover injected defects only; they say nothing about defect rates in real data (already
   noted in IMPLEMENTATION_PLAN.md §4).
