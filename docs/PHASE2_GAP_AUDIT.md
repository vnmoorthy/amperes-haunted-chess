# Phase 2 gap audit (audit of the Phase 1 build)

The audit was run on 2026-10-29 against the Phase 1 code. That code covers `amperes/sim/*`, `schema.py`, `store.py`, `stats.py`,
`regression.py`, `report.py` and `cli.py` (demo/report/reproduce/regress). Commands used: `pytest tests/test_sim.py` (18
passed) and `python -m amperes.cli demo --n-dev 400 --n-heldout 1000`, which produced `exp_20261029T200230_5161fe` in 71 s.

## A. Implemented and verified

| Capability | Code | Verification |
|---|---|---|
| Pick-and-place env, reset/step, seeded noise | `sim/env.py` | `test_env_reset_and_step`, `test_fixed_seed_is_bitwise_reproducible` |
| Scripted baseline + registry (no code from files) | `sim/policies.py::make_policy` | `test_timeout_and_policy_exception` |
| Ground-truth verifier, 4 outcomes | `sim/verifier.py::verify` | `test_verifier_success_and_failures`, `test_verifier_ignores_policy_claims` |
| Deterministic taxonomy with evidence | `sim/taxonomy.py::classify` | `test_classification[COLLISION/SLIPPAGE]` |
| Canonical recorder + gzip JSON round trip incl. NaN | `schema.py`, `sim/runner.py` | `test_episode_roundtrip_with_nan` |
| Reproduction with pre-declared criteria | `sim/reproduce.py` | `test_reproduction_and_regression`; 40/40 exact in the full run |
| Dev/held-out separation and frozen selection | `sim/variations.py::assert_disjoint`, `sim/experiment.py` | `test_heldout_disjoint_from_dev` |
| Paired statistics | `stats.py` | `test_stats` |
| Regression suite | `regression.py` | `test_reproduction_and_regression` |
| Report consistent with DB | `report.py` | `test_report_consistency` |

## B. Partially implemented at audit time

- **No interface.** There was a CLI only. The UI was added in Phase 2 (`amperes/ui/index.html`).
- **Only simulator-generated episodes.** No ingestion path existed for external records, which is the main Phase 2 gap.
- **Optional LLM diagnostician** was not implemented. It is still not implemented; only a disabled provider hook exists (`diagnostics.py`).
- **`episodes.policy_version` was `NULL`** for every simulator episode, because the runner did not return it. Fixed in `runner.py` / `store.record_run_episodes`.

## C. Broken or untested behaviour found (all fixed, each with a regression test or a documented check)

1. **Open-loop replay was not exact (2/6)** because actions were stored rounded to 1e-6, and contact dynamics amplified the
   error. Actions are now stored at full float64 precision (`runner.py`). Result: 40/40 exact.
2. **A policy that throws before stepping was labelled `unknown`, not `invalid`.** The verifier now checks termination
   before missing streams (`verifier.py`). Caught by `test_timeout_and_policy_exception`.
3. **Reproduction runs overwrote the original condition's `split`** (`INSERT OR REPLACE` on `conditions`). Changed to `INSERT OR IGNORE`.
4. **`ProcessPoolExecutor` crashed when the caller had no `__main__` guard** (macOS spawn). The CLI is guarded. This is documented
   for library users, and the pool is used only for 8 or more episodes.
5. **The simulator export declared `obs.finger_gap ∈ [0, 0.1]`.** Soft joint limits produce 0.1004 and −0.007, which caused
   41 spurious `S002` findings in the Phase 2 detector evaluation. The declaration was fixed to [−0.01, 0.11]; the rule itself was unchanged.

## D. Claims not supported by recorded evidence

- "Improves robot policy performance": supported **only in simulation** for a scripted controller. Reports and the UI say "simulation only".
- "Relevant to OpenAI": the only evidence is public job postings (`docs/research/OPENAI_PUBLIC_SIGNALS.md`). There is no
  evidence of demand, and no OpenAI system or data has been used.
- "Saves engineering time": no measurement exists. The Phase 3 study instrument was added, but no data has been collected.
- The Phase 1 report's regression table could be misread as independent evidence. It now carries the note that the suite
  comes from dev data and that step 6 is the independent test.

## E. Missing for a real robotics engineering workflow

- Importers for the formats teams actually use: LeRobotDataset, MCAP/rosbag2, robomimic HDF5.
- Image/video quality checks (frame drops, exposure, sync), when camera data is the main modality.
- Multi-user access, authentication, and integration with the team's ticket tracker.
- Exporting findings as annotations into existing viewers (Foxglove events, Rerun layers).
- Articulated-arm simulation (robosuite/RoboCasa) and a learned-policy adapter, if the simulator loop is to matter to VLA teams.
- Continuous or scheduled ingestion. Today it is batch only.

## F. Reuse rather than recreate

| Tool | Role for Amperes |
|---|---|
| MuJoCo | Physics. Already used directly |
| robosuite / RoboCasa | Use for articulated arms and diverse scenes instead of extending the toy gantry. Do not build more scenes |
| LeRobot (format, visualizer) | The first adapter to write; their visualizer should stay the viewer |
| Foxglove / Rerun | Visualization targets for findings; Amperes should not grow a trajectory viewer |
| Pandera / Great Expectations | Conceptual overlap for tabular checks. Amperes' added value is episode semantics (sessions, outcomes, replay), not generic column checks |
| W&B / MLflow | Experiment tracking; Amperes reports could log there rather than compete |

Sources and details: `docs/COMPETITIVE_GAP.md`.

## G. Three highest-value changes for pilot readiness (and what was done)

1. **An ingestion path for exported records + data-quality rules** that keeps failed tasks as usable data. **Done** (`ingest/`, `quality.py`).
2. **A measurable evaluation of the rules** on labelled data, with a "not evaluated" status where examples are missing. **Done** on a
   synthetic corpus (`evaluation.py`); still needed on customer-labelled data.
3. **Incident triage connected to replay and regression**, with explicit replay classes A–E. **Done** (`incidents.py`,
   `reproduce.replay_class`). Real-format adapters (E above) remain the top open item.

## H. Risks that could make Amperes uncompetitive

1. Teams already employ engineers to write exactly these scripts. This is stated in public OpenAI postings.
2. There is no importer for the formats teams use, so integration friction exceeds the value of the rules.
3. Rules that fire on real data with poor precision would create review work instead of saving it.
4. Fleet tools (Formant, InOrbit) or dataset tools (LeRobot) could add yield-style episode classification cheaply.
