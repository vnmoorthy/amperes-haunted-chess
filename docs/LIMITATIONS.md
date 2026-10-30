# Limitations

## What has been demonstrated

- In MuJoCo simulation, a deterministic harness can verify outcomes from ground-truth state, classify failures, reproduce
  them exactly (40/40), choose an intervention from dev data only, and measure it on 1,000 held-out conditions with paired
  statistics. It also exposed a trade-off (an alternative intervention broke 33 conditions).
- On a **synthetic** corpus with injected defects, the quality rules flagged every defect that the record made detectable.
  They left clean episodes and valid failed tasks unflagged.
- On a **synthetic** 3-workcell operations package, the operations analytics surfaced all six planted patterns (P1–P6).
  P1's per-session missing-observation check flagged 5 of the 6 affected sessions. The analytics returned
  `not_computable` where inputs were missing (wc-C availability). The patterns were planted by the same author who wrote the detectors.

## What remains unproven

- **Value on real data.** No customer or physical-robot records have been processed. The detection rates, false-positive
  rates and incident structure on real data are unknown.
- **Time savings.** The counterbalanced study instrument exists (`review.py`). No human has used it, so no time-saving
  claim is made anywhere.
- **Differentiation.** Most individual rules are straightforward to write internally (see `PHASE3_DIFFERENTIATION_TEST.md`).
- **Detector accuracy.** The corpus and the rules have the same author. Two declarations were corrected after the first
  evaluation run: the simulator's finger-gap range, and truncation checks ignoring action streams. The corrections were
  principled, but the evaluation is not blind.

## Simulator limitations

- A Cartesian gantry, not an articulated arm. There is no inverse kinematics, joint limits or self-collision of a real manipulator.
- Contacts use MuJoCo's soft contact model. Slip and penetration behaviour (e.g. the finger overshoot of about 0.5 mm) are
  model properties, not hardware measurements.
- Perception is ground truth plus Gaussian noise. There is no camera, lighting or occlusion.
- The intervention relies on privileged obstacle height.
- Determinism is verified on one machine and build (Apple M3, MuJoCo 3.17.0). Cross-platform bitwise reproducibility is not claimed.

## Generalization limitations

- Held-out conditions come from the same perturbation distribution as dev, so they test interpolation, not distribution shift.
- One task, one embodiment, one scripted baseline. No learned or VLA policy has been evaluated.
- Simulated improvements do not imply improvements on physical robots.

## Data and integration limitations

- Supported formats: canonical JSONL and the documented CSV package. **There are no LeRobotDataset, MCAP/rosbag2 or robomimic
  HDF5 adapters.** These would be the first integration work for most robotics teams.
- Rules check only what the data contract declares, such as ranges, rates and `max_unchanged_s`. A wrongly declared range
  produces noise. This was observed with our own simulator export before the declaration was fixed.
- Clock-plausibility checks are heuristic. Camera and video streams are not inspected.
- Operational metrics need session start/end and availability events. Without them, the rates are reported as
  `not_computable` rather than estimated.

## Live Viam capture

- Read-only telemetry from one hackathon arm (`sfhack1`), with captures of 20–60 s, mostly with the arm stationary
  (`idle_window` episodes). It shows telemetry-path health, not task performance.
- The arm model is not visible to the machine API key; the event page states the provided arms are UFactory xArm or UR.
- The finding that failures came from the client, not the robot, holds for this network and SDK (viam-sdk 0.85.1) on 2026-10-29.

## Hardware

- No robot commands are sent by Amperes. The Viam recorder can call only five read methods (enforced and tested). Any future hardware execution would need an explicit safety design and human
  authorization, and would be disabled by default.
