# Robot lab: supervised pick-and-place on the event arm

## Safety model (enforced in `amperes/integrations/viam_control.py`, tested in `tests/test_viam_control.py`)

1. **Dry run by default.** Motion needs `--execute` (or the UI's *Execute* button) **and** the typed phrase
   `I am at sfhack1-main with the e-stop in reach`.
2. **Taught safety box.** The arm only goes where a human has already put it:
   - the box spans the three taught spots (home, a, b) plus 40 mm, and the approach height above them;
   - it never goes lower than the lowest taught spot minus 3 mm, nor below the table surface (z = −23 mm) plus 15 mm,
     plus the 150 mm gripper length when the gripper points down;
   - each move is limited to roughly the distance between taught spots, and the gripper may rotate at most 45° per move.
3. **Collision-aware planning.** Moves go through the machine's motion service, which knows the configured table, walls and ceiling.
4. **Watchdog.** A separate telemetry connection samples the arm about 3 times a second. Four seconds without telemetry during motion stops the arm.
5. **STOP.** The UI STOP button, or Ctrl-C on the command line, sends `stop()` to the arm and gripper immediately, even mid-move.
6. **People.** A teammate stays at the arm with the e-stop in reach for every executed run. This is your rule, not code; the code just asks you to confirm it.

## Workspace scan (no taught spots needed)

`python -m amperes.cli robot scan --sweeps 3 --execute --ack "..."` sweeps a box above the table (default 25–45 cm in
front of the base, ±20 cm, flange heights 320 and 420 mm, 3×3 grid, the centre pose revisited every 4 poses) and records,
per sweep, one episode with: wrist-camera colour + depth at every pose, the pose the arm actually reached (`obs.ee_pose_settled`,
`poses.jsonl`), commanded-vs-achieved error, planning failures per pose, transit durations, ~3 Hz proprioception and
2 fps wrist video (`obs.cam.frame`, `frames.jsonl`). The envelope is the scan box itself (`scan_envelope`), whose floor keeps
the fingertips at least 8 cm above the table; the operator clears that box before acknowledging. Dial, watchdog, STOP
and the acknowledgement are the same as for pick-and-place. Outcome is verified from the achieved pose (`pose_error`
verifier), which is independent of the command.

## Steps (about 10 minutes the first time)

1. Make sure sfhack1 shows **Live** in the Viam app. Place a small rigid object (a block or cup) at spot **a**.
2. In the Viam app, use the **Control** tab to jog the arm, gripper open, so the fingers straddle the object at grasp
   height. In Amperes, click **Teach** next to **a** (or run `python -m amperes.cli robot teach a`).
3. Jog to an empty spot about 15–25 cm away at the same height, then teach **b**. Jog up to a safe pose above both and teach **home**.
4. Click **Dry run** to validate the plan: nothing moves, and any refusal names the rule it broke.
5. Type the acknowledgement phrase and click **Execute**, starting with 2 trials. The object goes a→b, then b→a, so no manual reset is needed.
6. Run an **A/B**, for example baseline versus `{"name":"lower_grasp","grasp_z_offset_mm":-5}`. Configs alternate in pairs, so
   direction and drift affect both arms equally.
7. Open the episodes, incidents and yield audit; then **Sync to MongoDB Atlas**.

## What each trial records

- **Commands sent:** every move target (`act.target_pose`) and gripper command (`act.gripper`), with timestamps.
- **What the arm did:** its joint and end-effector telemetry over the trial.
- **Gripper evidence:** whether `grab()` reported holding something, and whether `is_holding_something` was still true after the lift, where the gripper supports it.
- **Camera snapshots:** from the wrist `cam` and the fixed `cam2`: colour and depth before the grasp, and colour after the
  lift and after the place. Snapshots are taken only while the gripper points down. That check uses the wrist pose, so aim
  `cam2` at the table, never at the room.
- **Outcome:** `success`, or one of `GRASP_FAILURE`, `SLIPPAGE`, `TIMEOUT`, `SAFETY_STOP`, `INFRASTRUCTURE_ERROR`, with the evidence behind it.

The outcome comes from the gripper's own signals, which is not an independent check. An overhead `cam2` comparing images
of the drop zone before and after each trial is the next step toward independent verification.
