# Experiment protocol (simulator)

## Research question

Can a targeted change to a baseline pick-and-place controller, selected only from development-set failures, reduce the
failure rate on held-out perturbed conditions without breaking conditions that previously succeeded?

## Task and verifier

- **Task:** `pick_place_box@1.0.0`. A Cartesian gantry (x/y/z position servos, kp = 4000) with a force-controlled
  parallel gripper picks a box and places it in a square target region. The physics timestep is 2 ms and control runs at 50 Hz. The step budget is 600 control steps (12 s), followed by a 0.5 s settle.
- **Verifier:** `pick_place_verifier@1.0.0` reads only ground-truth `state.*` streams. Success requires all of the following:
  - the object was lifted at least 0.02 m with two-finger contact;
  - it stayed inside the target region for the final 0.4 s;
  - it is resting on the table and its speed is at most 0.03 m/s;
  - it is released;
  - there was no obstacle contact.
- **Outcomes:** `success`, `failure`, `timeout`, `invalid`, each with a machine-readable reason and its measurements.

## Baseline

`scripted_pick_place@1.0.0` (config hash `#` in reports) is a classical state machine with these phases: perceive (1 sample),
move above, descend, close (6 N per finger), lift, transport at 0.12 m TCP height, lower, release, retreat. It uses a trapezoidal
velocity profile at 0.35 m/s and 3 m/s². The baseline is tuned for nominal conditions only. It is not weakened on purpose: it
succeeds on 5/5 nominal runs.

## Conditions

- **Nominal:** the default `EnvParams`, repeated 5 times to check determinism.
- **Perturbation space:** object x/y/yaw/size/mass/friction, target x/y, obstacle height (0–0.15 m) and object-estimate noise
  (0–0.02 m). Each condition perturbs 1–3 factors, sampled uniformly within the bounds.
- **Dev set:** 400 conditions from master seed 1,000. Used for failure discovery and for intervention selection.
- **Held-out set:** 1,000 conditions from master seed 2,000,000. `assert_disjoint` enforces that no condition id or seed is shared with dev.

## Metrics (declared before the run)

- **Primary:** verified success rate on held-out conditions, compared pairwise on identical conditions.
- **Secondary:** fixed and broken counts (discordant pairs), collision rate, timeout rate, mean and median episode duration,
  wall-clock compute, and the regression-suite status counts.

## Intervention selection (pre-registered in `interventions.SELECTION_RULE`)

1. The target category is the most frequent verified failure category on the baseline dev run, excluding invalid, environment, infrastructure and unknown categories.
2. Every catalogue candidate for that category is evaluated on the same dev conditions.
3. Candidates are ranked by dev successes (descending), then dev regressions (ascending), then mean duration (ascending).
   Rank 1 is `selected`; rank 2 is the `alternative` ablation; a `control` arm reruns the unchanged config.
4. The selection is frozen (a timestamp is stored in `interventions.frozen_at`) before any held-out episode runs.

## Statistics

- Wilson 95% intervals for every success rate.
- Exact two-sided McNemar test on discordant pairs.
- 95% paired-bootstrap CI (10,000 resamples, seed 0) for the difference in success rate.
- No relative-percentage improvements are reported, and the raw denominators are always shown.

## Reproduction criteria (declared in `reproduce.CRITERIA`)

- **Eligible:** a verified failure or timeout, not an infrastructure error, replay class A, B or C.
- **Exact:** class A, same outcome and category, identical length, and max |Δ object position| ≤ 1e-6 m.
- **Symptom reproduced:** same outcome and category, but not exact.
- **Reproduction rate:** exact reproductions / eligible attempts, reported separately for closed-loop reruns and open-loop action replay.

## Reference run results (`exp_20261029T210214_4d7bb7`, 78 s)

| Item | Result |
|---|---|
| Baseline dev | 355/400 (88.8%, CI 85.3–91.5); failures: COLLISION 27, SLIPPAGE 8, GRASP_FAILURE 7, REACH_FAILURE 3 |
| Reproduction | rerun 40/40 exact; action replay 40/40 exact |
| Sweep around the representative collision | obstacle height: success→COLLISION between 0.094 and 0.104 m |
| Dev selection | obstacle_aware_transport 380/400 (25 fixed, 0 broken) > fixed_high_transport 363/400 (24 fixed, 16 broken) |
| **Held-out, selected** | **862/1000 → 949/1000; fixed 87, broken 0; Δ +8.7 pp [7.0, 10.5]; McNemar p = 1.3e-26** |
| Held-out, alternative | 862 → 911/1000; fixed 82, **broken 33**; Δ +4.9 pp [2.8, 7.0] |
| Held-out, control | 862 → 862; 0 discordant pairs |
| Nominal (all arms) | 5/5 |
| Mean episode duration (held-out) | baseline 3.85 s, selected 3.52 s, alternative 4.15 s |
| Regression suite (101 dev cases: 40 reproduced failures, 60 guards, 1 nominal) | baseline 61/101; selected 84/101 (23 fixed, 17 still failing, **0 regressed**); alternative 81/101 (22 fixed, **2 regressed**); control 61/101 |

## Known confounders

- Dev and held-out conditions come from the same perturbation distribution, so held-out tests interpolation, not distribution shift.
- Failures after the intervention (slippage and grasp failures on heavy objects) are a different category and are left unaddressed.
- The intervention uses privileged knowledge of obstacle height. A real system would need perception for this.
- The scripted baseline is deterministic, so variance comes only from conditions, not from policy stochasticity.
- One early implementation detail mattered: actions logged at 1e-6 rounding broke open-loop replay (2/6 exact). Full-precision action logging fixed it (40/40).
