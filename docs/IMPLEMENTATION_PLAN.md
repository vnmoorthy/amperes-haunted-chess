# Implementation Plan — Amperes Failure Lab

Status legend: `PLANNED` → `DONE (verified by <test/command>)` / `PARTIAL` / `NOT DONE`.
The "Actual status" column is updated as phases complete; see the phase final reports for evidence.

## 0. Discovery (2026-10-29)

| Item | Finding |
|---|---|
| Repository | Empty except `.env` (Viam credentials), `.gitignore`, `test_connection.py`. Not a git repo → source revision is recorded as a SHA-256 of the `amperes/` package instead of a commit hash. |
| Hardware | Apple M3, 8 cores, 8 GB RAM, no CUDA GPU. |
| Disk | **~6 GB free (99 % used)** — artifacts must stay small (gzip JSONL, decimated traces, no video). |
| Python | 3.12.4 in `.venv` (Anaconda base). |
| Simulator | `mujoco` 3.17.0 installs from wheel and steps correctly (smoke-tested). |
| Other deps | numpy 2.5, pydantic 2.14, fastapi 0.162, uvicorn, matplotlib 3.11, pytest 9.2, httpx. No scipy (stats implemented directly). |
| Robot hardware | A Viam machine (`sfhack1`: arm, gripper, camera, motion service) is reachable via `test_connection.py`. **Not used for any motion.** Optional read-only integration only. |

## 1. Architecture decisions

1. **Custom minimal MuJoCo scene instead of robosuite/RoboCasa.** A Cartesian gantry (x/y/z slide joints with
   position servos) and a force-controlled parallel two-finger gripper picking a box and placing it in a target
   region. Reason: zero extra dependencies, ~0.3 s/episode on CPU, every physical parameter perturbable, and
   real contact-based grasping (slip, collision, release failures are physical, not scripted). Trade-off: not an
   articulated arm; documented in `LIMITATIONS.md`. robosuite/RoboCasa are the right tools for scene diversity and
   are evaluated in `COMPETITIVE_GAP.md` rather than re-implemented.
2. **One canonical episode format from day one** (`amperes.episode/1`). Simulator episodes are written in the same
   schema that external data is imported into, so quality control, incidents, and reports work identically on
   simulated and imported records. Unknown fields stay `null` with an explicit `unknown` marker, never invented.
3. **Verifier reads simulator ground truth only** (object pose, contacts), never the policy's internal state or any
   language model output.
4. **Deterministic rules first** for failure taxonomy, data-quality checks, and incident grouping. No LLM in the core
   loop; an optional diagnostic-model interface is disabled by default.
5. **SQLite + content-addressed artifact directory** (`artifacts/`). Runs are immutable; reruns create new runs.
6. **No frontend build step.** FastAPI serves a single static HTML/JS page.

## 2. Phase plan (combined Phase 1–3 specifications) — status as of 2026-10-29

| # | Phase | Deliverable | Actual status |
|---|---|---|---|
| P1.1 | Env + baseline | `sim/env.py`, `sim/policies.py` | DONE (`test_env_reset_and_step`, 5/5 nominal) |
| P1.2 | Verifier + recorder | `sim/verifier.py`, `schema.py`, `sim/runner.py` | DONE (`test_verifier_*`, round-trip test) |
| P1.3 | Taxonomy + variations | `sim/taxonomy.py`, `sim/variations.py` | DONE (`test_classification`, `test_heldout_disjoint_from_dev`) |
| P1.4 | Reproduction | `sim/reproduce.py` | DONE (40/40 exact, both modes) |
| P1.5 | Intervention + held-out + regression | `sim/interventions.py`, `sim/experiment.py`, `regression.py`, `stats.py` | DONE (862 → 949/1000 held-out) |
| P1.6 | Store, CLI, API | `store.py`, `cli.py`, `cli_data.py`, `api.py` | DONE (`test_api_and_cli_errors`) |
| P2.1 | Ingestion | `ingest/` (JSONL, CSV package) | DONE; LeRobot/MCAP/robomimic NOT DONE |
| P2.2 | Quality rules | `quality.py` (26 rules) | DONE (`test_*_rules`) |
| P2.3 | Incidents + replay classes | `incidents.py`, `reproduce.replay_class` | DONE |
| P2.4 | Labelled corpus + detector metrics | `fixtures.py`, `evaluation.py` | DONE (synthetic only) |
| P3.1 | Event model + yield + loss attribution | `ops.py` | DONE |
| P3.2 | Bottlenecks + correlation | `ops.py` | DONE (synthetic only) |
| P3.3 | Review + shadow mode + workflow timing | `review.py`, `cli_data.c_shadow` | DONE as instruments; NO human data collected |
| P3.4 | Pilot report + economic model | `pilot_report.py`, `economics.py` | DONE |
| UI | Single-page interface | `ui/index.html` | DONE (checked in the in-app browser) |
| LLM | Optional diagnostician | `diagnostics.py` | NOT DONE: disabled hook only, no provider |

## Decisions and deviations

- The three specifications arrived during one session and were built as one system, with one canonical schema, instead of three rewrites.
- The simulator scene is a custom MuJoCo gantry rather than robosuite, chosen for zero extra dependencies and 0.02 s per episode. It is documented as a limitation.
- No git repository: provenance uses a SHA-256 digest of the `amperes/` sources (`provenance.code_digest`).
- Corrections made after evidence: actions are logged at full precision (replay went from 2/6 to 40/40 exact); the finger-gap
  range was re-declared (41 false positives removed); truncation checks ignore action streams; event ids are scoped per
  dataset (a re-import had silently dropped all events); yield-loss attribution was added after a documentation review found
  the wedge output missing; the simulator is excluded from customer reports unless requested.
- Docs on OpenAI signals, the competitive gap, customer discovery and the pilot/economics material were drafted by a research
  sub-agent from public sources and the repository, then cross-checked against the code.

## 3. Compute budget

Smoke test: 5 episodes. Full demo target: ≤ 10 minutes wall-clock on 4 worker processes; episode counts chosen after
measuring per-episode runtime. Trajectories are decimated to 10 Hz for storage (control runs at 50 Hz), gzip-compressed.

## 4. Risks

- MuJoCo soft contacts can creep under sustained grasp load → may produce slip that is a simulator artifact. Mitigated
  with elliptic friction cones and `impratio`; still listed as a simulator limitation.
- Synthetic defect corpus measures detector behaviour on *known injected* defects only; it says nothing about the
  distribution of defects in real customer data.
- Workflow time-savings require human reviewers; the instrument is built, but no measurement exists until people use it.
