# Hackathon pitch: Amperes, the usable-data audit for robot fleets

Event: *Robot Hackathon: Harnessing Physical AI & Data* (Viam × MongoDB). Demos Fri 3:30 PM, awards 5:00 PM.
The published page lists no judging criteria and no judges; it says only that creative MongoDB Atlas use "will stand out".
This pitch is built for that, and for the usual sponsor-hackathon criteria: real hardware, sponsor tech, rigor, impact.

## One-line pitch

**Robot-learning teams spend station-hours collecting episodes, and some of them are silently unusable. Amperes plugs
into any Viam robot read-only, tells you which episodes are usable, why the others aren't, and whether the cause is the
robot or the data pipeline.**

## The 3-minute script

| Time | Say | Show |
|---|---|---|
| 0:00 | "Robot-learning teams pay for every hour a station collects data. Some episodes come out unusable: dropped camera frames, gaps, missing labels. Nobody finds out until training. The cause is buried across three different logs." | Title slide |
| 0:25 | "Amperes is a read-only audit layer. It cannot move your robot; that is enforced in code. We pointed it at this event's arm, sfhack1, over Viam." | **Live robot** tab: press *Capture 60 s* while a teammate moves the arm from the Viam app |
| 0:50 | "It measures the link, the arm and the camera live, then audits every episode with 26 deterministic rules. Each finding comes with its evidence." | Tiles (read success, Hz, p50/p95 latency, camera frames), the event feed, then *Open yield audit* |
| 1:20 | "Here is the real finding from this venue. With default SDK settings, 51 of 59 reads failed and the camera looked dead. Amperes showed the failures were in the **telemetry path**, not the robot: with the client fixed, 154 of 155 reads and 19 of 20 frames came through. The p95 latency through the cloud relay is about 1 s, so record on the local network. If you had trained on that first capture, you'd have blamed the robot." | Episodes tab: an `infrastructure_error` episode with red failed-read markers |
| 1:50 | "Across a fleet, it turns logs into answers. In this 3-workcell sample, 51 episodes are unusable. 43 of them trace to camera frame drops that only appear after one config change: 6 of 6 sessions versus 0 of 18. And failed grasps stay usable training data, which most pipelines throw away." | Operations tab: yield 89.4% (429/480) and the loss-attribution table. Label it as synthetic sample data |
| 2:15 | "MongoDB Atlas is the shared memory across stations. Every episode gets a failure signature built from evidence, with no LLM, and Atlas Vector Search answers: has any other station seen this exact failure?" | Episode → *Find similar failures* → *…across all stations (Atlas)* |
| 2:35 | "It's also rigorous about fixes. In simulation, our selected fix took held-out success from 862 to 949 out of 1000 and broke nothing. The obvious alternative fix broke 33 runs that used to pass. Amperes catches that before it ships." | Evaluation tab |
| 2:50 | "No code or model weights needed: a read-only export, four weeks, shadow mode. That's the pilot we're offering robotics data teams." | Pilot slide |

## What actually ran on the event arm (29 Oct, measured)

- **Workspace scans (xArm6 + wrist D435):** 3 sessions, 6 sweep episodes, 186 commanded poses reached with 0.34 mm max
  error and 0.22 mm repeatability on 12 centre revisits; 372 RGB-D snapshots from known camera poses; continuous 2 fps
  wrist video and ~3 Hz proprioception. One sweep is flagged `invalid`: the telemetry watchdog stopped it (a link hiccup),
  and the dataset says so instead of hiding it.
- **Chess against a human, then engine vs engine:** 37 piece-move episodes, 19 verified successes, 12 failures and
  6 invalid. The failures are the product's point: the gripper's holding sensor said "grabbed" while the wrist camera
  showed the pawn still on its square (PIECE_NOT_MOVED), a capture dropped a piece into the a1 rook, and a camera
  "centring" feature pulled picks 25 mm off (both fixed and documented). The 16-ply game 1 ran 10 of 10 moves,
  camera-confirmed, with the robot seeing the human's moves through the wrist camera (castling included).
- **Third-party data through the same pipeline:** `exidekat/chessbot-lerobot` (SO-100, 17 teleop episodes) imported from
  Hugging Face in LeRobot v3 format; QC flags every episode as unlabelled, which is exactly what a buyer needs to know.
- **Deliverables from the night:** `docs/DATASET_CARD.md` (datasheet over all real datasets), a LeRobot v2.1 export of
  game 1 (`amperes_data/exports/lerobot_chess_game1`, 10 episodes, 662 frames, depth included), everything synced to
  MongoDB Atlas, and `docs/CHESS.md` with the hardware facts and the mistakes.
- **Hardware facts learned the hard way (all in code now):** fingertips 200 mm below the flange, not 150; the board is at
  the frame system's -23 mm while the camera's depth reads it 30 mm high; the gripper's 85 mm span and 75 x 110 mm
  body cannot work a square with tall neighbours; the Python SDK needs sessions off and abandonable timeouts.

## Run-of-show checklist (Friday)

1. Before 3:00 PM: start the UI with `.venv/bin/python -m amperes.cli serve` and open http://127.0.0.1:8765.
2. Confirm `.env` has the Viam keys and the machine address. Run one 60 s capture as a rehearsal.
3. **Ask a teammate to move the arm** (Viam app → Control tab, or your own program) during the live capture. Amperes
   only observes, so without motion every episode is an `idle_window`.
4. If you're on a phone hotspot or the venue Wi-Fi is congested, expect the latency tiles to look worse. That is itself the
   demo point; say so.
5. Atlas is configured (cluster `robohack`; 8 real sfhack1 captures + the synthetic sample are already synced). After the
   live capture, click **Sync to MongoDB Atlas** on its summary, open an episode and click **…across all stations
   (Atlas Vector Search)**. Verified on 2026-10-29: a new capture's recording failure matched earlier sfhack1 captures at 0.99.
6. Have the backup ready: `scripts/demo_walkthrough.sh` reproduces every non-live number offline in about 10 s.

## Q&A prep (honest answers)

- **"Couldn't a team write these checks themselves?"** Individually, yes. The product is the linked, auditable
  classification: each episode gets one status, each loss gets one attributed cause, robot faults are kept separate from
  pipeline faults, and you get a shadow-mode pilot that needs no access to your code or weights.
- **"Is this real data?"** The live tab, yes: read-only telemetry from sfhack1. The 3-workcell sample is synthetic and
  labelled as such. The fix-evaluation numbers are from MuJoCo simulation.
- **"Does it control the robot?"** No. Only five read methods can be called; anything else raises before it reaches the SDK
  (`tests/test_viam_live.py`).
- **"Why MongoDB?"** Stations and teams need one place to query episode health and find recurring failure patterns. A
  document store with vector search fits deterministic failure signatures with no embedding model.
- **"Customers?"** None yet. Public robotics job postings describe this exact work (`docs/research/OPENAI_PUBLIC_SIGNALS.md`).
  That shows the problem exists, not that anyone wants to buy. The pilot is designed to test that.
