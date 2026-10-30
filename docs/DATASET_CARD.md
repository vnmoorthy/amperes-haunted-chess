# Dataset card: Amperes real-robot data, 29 Oct 2026: xArm6 workspace scans, chess moves, and a third-party LeRobot set

> REAL data. Facts below come only from the Amperes store; anything it does not hold is marked "not recorded".

Generated 2026-10-30T02:16:45+00:00 · card amperes.dataset_card/1 · ruleset 1.0.0 · code e7403a5ec0e8f424

| stat | value |
|---|---|
| datasets | 5 |
| episodes | 34 |
| frames (obs.joint_pos samples) | 4989 across 34 episodes |
| images referenced | 862 |
| images found at source path | 862 |
| real episodes | 34 |
| synthetic episodes | 0 |
| episode artifacts unreadable | 0 |
| success rate (Wilson 95% CI) | 88.2% = 15/17 valid outcomes, CI [65.7%, 96.7%] |

## 1. Summary

### `ds_20261030T015653_a6ecc1` — ROBOT chess game 1 (16 plies, 2 captures, castling seen) chess-sfhack1-main-20261030T014536 [REAL]

- What: import via jsonl@1.0.0; 10 episodes (10 real, 0 synthetic); note: real arm vs human; Stockfish chose; wrist-camera move detection and verification
- Who/where: workcells sfhack1-main; devices arm; operators (pseudonymous) op-onsite; sessions 1
- When: 2026-10-30T01:46:07+00:00 to 2026-10-30T01:53:50+00:00 (0.129 h span)
- Embodiment: 6-joint arm + parallel gripper + wrist RGB-D camera (event-provided)
- Cameras: cam (62 images), cam2 (62 images)
- Success rate: 100.0% (10/10, CI [72.2%, 100.0%])

### `ds_20261030T000302_48e7a3` — ROBOT workspace scan scan-sfhack1-main-20261030T000008 [REAL]

- What: import via jsonl@1.0.0; 2 episodes (2 real, 0 synthetic); note: real arm workspace scan: RGB-D at known poses, reach accuracy, video
- Who/where: workcells sfhack1-main; devices arm; operators (pseudonymous) op-onsite; sessions 1
- When: 2026-10-30T00:00:08+00:00 to 2026-10-30T00:03:00+00:00 (0.048 h span)
- Embodiment: 6-joint arm + parallel gripper + wrist RGB-D camera (event-provided)
- Cameras: cam (578 images)
- Success rate: 100.0% (2/2, CI [34.2%, 100.0%])

### `ds_20261029T235741_9996cc` — ROBOT workspace scan scan-sfhack1-main-20261029T235546 [REAL]

- What: import via jsonl@1.0.0; 3 episodes (3 real, 0 synthetic); note: real arm workspace scan: RGB-D at known poses, reach accuracy, video
- Who/where: workcells sfhack1-main; devices arm; operators (pseudonymous) op-onsite; sessions 1
- When: 2026-10-29T23:55:47+00:00 to 2026-10-29T23:57:39+00:00 (0.031 h span)
- Embodiment: 6-joint arm + parallel gripper + wrist RGB-D camera (event-provided)
- Cameras: cam (132 images)
- Success rate: 100.0% (3/3, CI [43.9%, 100.0%])

### `ds_20261030T014143_4c6dee` — ROBOT chess chess-sfhack1-main-20261030T013851 [REAL]

- What: import via jsonl@1.0.0; 2 episodes (2 real, 0 synthetic); note: real arm chess moves vs a human (Stockfish chooses)
- Who/where: workcells sfhack1-main; devices arm; operators (pseudonymous) op-onsite; sessions 1
- When: 2026-10-30T01:39:02+00:00 to 2026-10-30T01:40:01+00:00 (0.016 h span)
- Embodiment: 6-joint arm + parallel gripper + wrist RGB-D camera (event-provided)
- Cameras: cam (14 images), cam2 (14 images)
- Success rate: 0.0% (0/2, CI [0.0%, 65.8%])

### `ds_20261030T012532_365bd4` — EXTERNAL LeRobot v3: exidekat/chessbot-lerobot (SO-100 chess teleop) [REAL]

- What: import via jsonl@1.0.0; 17 episodes (17 real, 0 synthetic); note: third-party LeRobot v3 dataset (unknown (LeRobot v3), 17 episodes, 15.0 fps); outcomes unlabelled
- Who/where: workcells external; devices unknown (LeRobot v3); operators (pseudonymous) unknown; sessions 1
- When: 1970-01-01T00:00:00+00:00 to 1970-01-01T00:00:16+00:00 (0.005 h span)
- Embodiment: unknown (LeRobot v3) (LeRobot v3 dataset)
- Cameras: not recorded
- Success rate: no valid outcomes

## 2. Composition

**Episodes by task**

| task | episodes |
|---|---|
| chess_move_real@1.0.0 | 12 |
| workspace_scan_real@1.0.0 | 5 |
| pick_up_black_pawn_from_red_d7_and_place_on_blue_d5@lerobot | 4 |
| pick_up_black_pawn_from_red_c7_and_place_on_blue_c5@lerobot | 3 |
| pick_up_black_pawn_from_red_d5_and_place_on_blue_d4@lerobot | 3 |
| pick_up_black_pawn_from_red_e7_and_place_on_blue_e6@lerobot | 2 |
| pick_up_black_pawn_from_red_e6_and_place_on_blue_e5@lerobot | 1 |
| pick_up_black_pawn_from_red_a7_and_place_on_blue_a6@lerobot | 1 |
| pick_up_black_pawn_from_red_g7_and_place_on_blue_g6@lerobot | 1 |
| pick_up_black_pawn_from_red_b7_and_place_on_blue_b5@lerobot | 1 |
| pick_up_black_pawn_from_red_h7_and_place_on_blue_h5@lerobot | 1 |

**Episodes by policy**

| policy | episodes |
|---|---|
| teleoperation@unknown | 17 |
| stockfish_plus_scripted_pick_place@1.0.0 | 12 |
| scripted_workspace_scan@scan(x=250..450,y=-200..200,z=320,420,grid=3x3,settle=0.3) | 3 |
| scripted_workspace_scan@scan(x=250..450,y=-200..200,z=300,360,420,grid=4x4,settle=0.3) | 2 |

**Episodes by config**

| config | episodes |
|---|---|
| not recorded | 17 |
| chess | 12 |
| scan | 5 |

**Episodes by outcome**

| outcome | episodes |
|---|---|
| unknown | 17 |
| success | 15 |
| failure | 2 |

**Episodes by outcome provenance**

| provenance | episodes |
|---|---|
| amperes_verifier | 17 |
| unknown | 17 |

**Failure category (diagnosis category, else outcome reason; failure/timeout/invalid episodes)**

| category | episodes |
|---|---|
| PIECE_NOT_MOVED | 1 |
| GRASP_FAILURE | 1 |

**Episodes by quality status**

| quality_status | episodes |
|---|---|
| suspicious | 21 |
| verified_success | 10 |
| infrastructure_error | 2 |
| invalid_data | 1 |

**Verified outcomes:** 17 of 34 (50.0%); verifiers: gripper_signals (12), pose_error (5)

**Streams present**

| stream | kind | episodes | samples | dims | units | declared Hz | observed median Hz |
|---|---|---|---|---|---|---|---|
| act.gripper | action | 12 | 36 | 1 | not recorded | not recorded | 0.106 |
| act.joint_target | action | 17 | 3391 | 6 | rad (LeRobot action) | 15 | 15 |
| act.target_pose | action | 17 | 258 | 7 | mm, orientation-vector, deg | not recorded | 0.69 |
| obs.cam.frame | observation | 2 | 338 | 1 | frame index: images/scan-sfhack1-main-20261030T000008-s000_cam_video_<n:05d>.jpg, frame index: images/scan-sfhack1-main-20261030T000008-s001_cam_video_<n:05d>.jpg | 2 | 1.996 |
| obs.ee_pose | observation | 17 | 1598 | 7 | mm, orientation-vector, deg | 2, 3 | 3.021 |
| obs.ee_pose_settled | observation | 5 | 186 | 7 | mm, orientation-vector, deg | not recorded | 0.775 |
| obs.joint_pos | observation | 34 | 4989 | 6 | deg, rad (LeRobot observation.state) | 2, 3, 15 | 15 |
| obs.joint_pos_settled | observation | 5 | 186 | 6 | deg | not recorded | 0.775 |

**Images per camera**

| camera | images |
|---|---|
| cam | 786 |
| cam2 | 76 |

By type: image/jpeg (612), image/vnd.viam.dep (250); snapshot events 524, frame-stream samples 338.

**Episode duration (s)**

- n 34, min 10.467, median 17.603, mean 22.858, max 87.471, total 0.216 h (basis: start_end_time (34))

## 3. Collection process

- Episode adapters (source.adapter): amperes.viam_tasks@1.0.0 (5), amperes.lerobot_v3@1.0.0 (17), amperes.chess_robot@1.0.0 (12)
- Import adapters (datasets): jsonl@1.0.0 (5)
- Synthetic notes: not recorded
- Capture notes: not recorded
- Robot sessions: 5 episodes (5 executed, 0 dry run); safety refusal events 0
  - Dry runs: dry runs carry synthetic_note 'DRY RUN: no motion was executed' and conditions.dry_run=true; the robot CLI imports them with the SYNTHETIC flag; none of the dry runs here is counted as a real episode
  - Operator acknowledgement: required by viam_control before any executed run; the phrase itself is not stored per episode
  - Safety model (docs/ROBOT_LAB.md):
    - dry run by default: motion needs --execute plus the typed operator acknowledgement phrase (e-stop in reach)
    - taught safety box: targets stay within the taught spots (home, a, b) + 40 mm and the approach height; floor limits apply
    - per-move limits: roughly the taught-spot distance, gripper rotation <= 45 deg per move
    - collision-aware planning through the machine's motion service (table, walls, ceiling)
    - watchdog: telemetry ~3 Hz; 4 s without telemetry during motion stops the arm
    - STOP: UI button or Ctrl-C sends stop() to arm and gripper immediately
    - a teammate stays at the arm with the e-stop in reach for every executed run (procedure, not code)
- Rule settings for `ds_20261030T015653_a6ecc1`: {"required_metadata": ["task.id"]}
- Rule settings for `ds_20261030T000302_48e7a3`: {"required_metadata": ["task.id"]}
- Rule settings for `ds_20261029T235741_9996cc`: {"required_metadata": ["task.id"]}
- Rule settings for `ds_20261030T014143_4c6dee`: {"required_metadata": ["task.id"]}
- Rule settings for `ds_20261030T012532_365bd4`: {"required_metadata": ["task.id"]}

## 4. Preprocessing and quality control

Ruleset 1.0.0: 34 findings, 24 episodes with non-info findings; by severity major (33), critical (1).

**Findings by rule**

| rule | findings | episodes | needs review | by severity | checks |
|---|---|---|---|---|---|
| C004_truncated | 4 | 4 | 0 | major 4 | Episode has enough samples and streams cover the declared duration |
| C005_missing_outcome | 17 | 17 | 0 | major 17 | Outcome label present |
| O002_label_contradicts_state | 1 | 1 | 0 | critical 1 | Source outcome label agrees with independent verification |
| T003_time_gap | 8 | 4 | 0 | major 8 | No gaps larger than 3x the declared sampling interval |
| T004_rate_mismatch | 4 | 2 | 0 | major 4 | Median sampling interval matches the declared rate (±20 %) |

**Incidents** (9 total)

| category | kind | incidents | episodes | status |
|---|---|---|---|---|
| C004_truncated | data_quality | 3 | 4 | open 3 |
| C005_missing_outcome | data_quality | 1 | 17 | open 1 |
| O002_label_contradicts_state | data_quality | 1 | 1 | open 1 |
| T003_time_gap | data_quality | 2 | 4 | open 2 |
| T004_rate_mismatch | data_quality | 1 | 2 | open 1 |
| data_recording_failure | operational_fault | 1 | 0 | open 1 |

**Quality status meanings** (precedence top to bottom)

- `infrastructure_error`: explicit recording/harness error events, or a harness-invalid simulator run
- `invalid_data`: any deterministic critical finding
- `suspicious`: any major finding, or any finding that requires human review
- `verified_success`: outcome success, independently verified
- `valid_success_unverified`: outcome success asserted only by the source label
- `valid_failed_task`: outcome failure/timeout with no serious data defect (still usable data)
- `unknown_outcome`: no outcome, or outcome 'unknown'

Usable by default: verified_success, valid_success_unverified, valid_failed_task. No composite score is computed; dimensions are reported separately.

## 5. Human review

- No human review recorded for these datasets (no finding/incident reviews, no human outcome labels).

## 6. Uses and limitations

- Sample size: 34 episodes, 17 with a valid outcome; the 95% interval on the success rate spans 31 percentage points. Treat rates as directional (n < 20).
- Embodiment coverage: 2 device id(s) (arm, unknown (LeRobot v3)) on 2 workcell(s) (external, sfhack1-main); embodiments 6-joint arm + parallel gripper + wrist RGB-D camera (event-provided), unknown (LeRobot v3) (LeRobot v3 dataset). Results do not transfer across arms or cells.
- Policies are scripted (stockfish_plus_scripted_pick_place@1.0.0, scripted_workspace_scan@scan(x=250..450,y=-200..200,z=320,420,grid=3x3,settle=0.3), scripted_workspace_scan@scan(x=250..450,y=-200..200,z=300,360,420,grid=4x4,settle=0.3)): hand-written motion sequences, not a learned policy. Action streams show what the script commanded, not demonstrations from a human or a trained model.
- Vision: 524 camera snapshot(s) at scripted moments and 338 frame-stream sample(s) (one low-rate video frame per sample); depth files (image/vnd.viam.dep) are raw Viam depth maps, not point clouds.
- Outcome verifier 'gripper_signals' (12 episodes): the gripper's own grab/holding reports; evidence from the actuator under test, not an independent sensor.
- Outcome verifier 'pose_error' (5 episodes): commanded target pose vs the pose the motion service reports after settling (reach error within tol_mm); evidence from the arm's own kinematics/encoders, not an external sensor, so it checks the controller, not what happened in the scene.
- 17 episode(s) have unknown outcome provenance (e.g. read-only capture or dry run); they cannot be counted as success or failure.
- Real-arm episodes were executed inside a taught safety box with scripted motion (pick-place a<->b or a workspace scan); the workspace, objects and lighting were not varied.
- Usable-data statuses by default: verified_success, valid_success_unverified, valid_failed_task; 'valid_failed_task' episodes are failures with clean data and are intended for training on failure, not to be discarded.

## 7. Provenance

- Dataset ids: ds_20261030T015653_a6ecc1, ds_20261030T000302_48e7a3, ds_20261029T235741_9996cc, ds_20261030T014143_4c6dee, ds_20261030T012532_365bd4
- Export timestamp: 2026-10-30T02:16:45+00:00
- Amperes 0.3.0, code digest e7403a5ec0e8f424, git not recorded, ruleset 1.0.0
- Python 3.12.4 on macOS-27.2-arm64-arm-64bit (arm64)
- Dependencies: mujoco 3.17.0, numpy 2.5.4, pydantic 2.14.2, fastapi 0.162.0, matplotlib 3.11.2
- Episodes with content digests: 34
- Import `ds_20261030T015653_a6ecc1`: 2026-10-30T01:56:53+00:00 via jsonl@1.0.0 from /Users/moorthy/Downloads/Projects/robo hackathon/amperes_data/artifacts/robot_sessions/chess_20261029T184536
- Import `ds_20261030T000302_48e7a3`: 2026-10-30T00:03:02+00:00 via jsonl@1.0.0 from /Users/moorthy/Downloads/Projects/robo hackathon/amperes_data/artifacts/robot_sessions/scan_20261029T165955
- Import `ds_20261029T235741_9996cc`: 2026-10-29T23:57:41+00:00 via jsonl@1.0.0 from /Users/moorthy/Downloads/Projects/robo hackathon/amperes_data/artifacts/robot_sessions/scan_20261029T165526
- Import `ds_20261030T014143_4c6dee`: 2026-10-30T01:41:43+00:00 via jsonl@1.0.0 from /Users/moorthy/Downloads/Projects/robo hackathon/amperes_data/artifacts/robot_sessions/chess_20261029T183842
- Import `ds_20261030T012532_365bd4`: 2026-10-30T01:25:32+00:00 via jsonl@1.0.0 from /Users/moorthy/Downloads/Projects/robo hackathon/amperes_data/artifacts/external/exidekat_chessbot-lerobot

## 8. License and contact

- License: [license]
- Contact: [contact]
