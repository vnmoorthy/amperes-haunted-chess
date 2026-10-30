# Demo day runbook: Fri 30 Oct (submit by 3:00 PM, live demo 3:30 PM)

Three minutes at the station, then two minutes of questions. Judging weights functionality most (40%), then ambition
and creativity (20% each), then modularity (15%), plus 5% for the event theme. The talk track, slide by slide, is in
[STORYBOARD.md](STORYBOARD.md). If there is no screen for the deck, give the same talk with the console on the laptop
and the arm playing behind you.

**The story in one line:** Amperes Failure Lab is the product. The chess robot is the live proof on real hardware:
every move it makes is a trial the lab records, labels and triages.

**The one rule for ending a game:** press *End game + import* only when the phase reads *your move* or *game over*.
Pressed while the arm is moving, it stops the arm mid-move, like STOP, and can leave a piece in the gripper.

| When | What |
|---|---|
| 2:33 PM | Rehearsal done: robot vs robot from a mid-game position, 6 half-moves, all 6 confirmed by the wrist camera and called out loud, 9 pick-and-places, 0 failures |
| 2:45 PM | Viam machine online, console up, both pages open, board check |
| 3:00 PM | Submission in (text at the end of this file) |
| 3:20 PM | Board ready: either still in the rehearsal position (to continue it) or in the start position; spare queens in reserve, table clear, teammate at the e-stop |
| 3:29 PM | Start the robot-vs-robot game |
| 3:30 PM | Talk (3:00), then questions (2:00) |

## Before the demo (in this order)

1. sfhack1 shows **Live** in the Viam app.
2. Console: `.venv/bin/python -m amperes.cli serve --port 8765`. If it is already running (a game may be open on it),
   leave it alone. Don't restart it.
3. Open http://localhost:8765/chess (the chess console) and http://localhost:8765/ (the Failure Lab). Switch both to
   the dark theme with the theme button in each page's header.
4. Board check from the look pose: the grid overlay must line up with the printed squares. If the board moved,
   re-teach with *Teach a1*, *Teach h1* and *Teach capture area* on `/chess`. Jog in the Viam app with the gripper open
   and turned 45°, fingertips touching the board at the centre of the square.
5. Board: either leave it in the rehearsal position and continue that game (see *Continue the rehearsal game* below),
   or put it in the start position. If the last tracked game is still open, *Show reset plan* then *Reset board* puts
   it back (Execute must have been on for that game). Otherwise reset it by hand. Put the spare queens on their reserve
   spots: `reserveQ` and `reserveq` are the 5th and 6th spots of the outer slot line, counted from the rank-1 end (see
   *Beside the board* on the page, or `GET /api/chess/reset/plan`).
6. Mac volume up. The robot speaks through the Mac's `say`. Calls are off only if the server was started with
   `AMPERES_VOICE=0`.
7. Rehearsal (done at 2:33 PM: 6 half-moves from a mid-game position, every move confirmed by the wrist camera and
   called out loud, for example "Black knight takes D4." and "White knight takes D4.", then "Game over: stopped after
   6 half-moves."; 9 pick-and-places, 0 failures). To rehearse again: a robot-vs-robot game (settings below) with
   *Stop after* 4 half-moves and a person at the e-stop. Check that every move is called out loud and gets a row in
   *Piece operations (episodes)*. When the phase reads *game over*, press *End game + import*: a new dataset
   "ROBOT chess …" appears in the Failure Lab. A new game can't start while one is open ("a game is already running;
   end it first").
8. If you are not continuing the rehearsal position, put the board back in the start position (by hand is quickest
   after a few half-moves).
9. Refresh the numbers you will quote (see *Numbers to quote*; the snippet only reads).
10. Clear the table around the board: bags, cups, cables. Nothing taller than a king inside the arm's reach.

## Start the robot-vs-robot game (about 30 s before you speak)

On `/chess`, card *New game*:

| Control | Setting |
|---|---|
| Robot plays | both sides (robot vs robot) |
| Skill / Think | 5 / 0.5 s (the defaults) |
| Call out every move | ticked |
| Reset the board when the game ends | unticked during the talk |
| Stop after | 20 half-moves (about 5 to 7 minutes of play, enough for the talk and the questions) |
| Execute (move the real arm) | ticked |
| Operator acknowledgement | type it exactly; the box shows the phrase as its placeholder |

Press *Start game*. The phase goes from *connecting* to *robot moving*. The arm unwinds its wrist, rises to the look
pose and plays. Each move is called once it is made, for example "White knight takes F7, check.", and the last call
is printed under the board ("Called: …"). When the game ends (checkmate, draw, resignation or the half-move cap) the
robot says the result. In today's robot-vs-robot games a new move started about every 16 s (median; a quarter took
longer than 22 s).

The same request from a terminal, if the page misbehaves (only when no game is open):

```bash
curl -s -X POST http://localhost:8765/api/chess/new -H 'content-type: application/json' \
  -d '{"color":"both","skill":5,"think_s":0.5,"voice":true,"auto_reset":false,"max_plies":20,"execute":true,"ack":"<operator acknowledgement>"}'
```

Fields (`ChessNewReq` in `amperes/robot_api.py`): `color` white|black|both, `skill` 0–20, `think_s`, `settle_s`,
`execute`, `ack`, `fen` (start from a position), `auto` (the camera watches the human's move), `track` (follow every
piece so the board can be reset), `voice`, `auto_reset`, `max_plies`.

### Continue the rehearsal game

The rehearsal stopped at move 16 with Black to move:

`r3k2r/pppb1pp1/5q1p/2Q5/3p4/P7/P3PPPP/2R1KB1R b Kkq - 3 16`

- White: king e1, queen c5, rooks c1 and h1, bishop f1, pawns a2, a3, e2, f2, g2, h2.
- Black: king e8, queen f6, rooks a8 and h8, bishop d7, pawns a7, b7, c7, d4, f7, g7, h6.

1. Check the physical board against that list (and the diagram on `/chess`). Fix any piece by hand.
2. If the rehearsal game is still open and its phase reads *game over*, press *End game + import* first.
3. The *New game* card on `/chess` has no FEN field, so start this one from a terminal: the API's `fen` field takes the
   position, with *Robot plays* both sides as `"color":"both"`:

```bash
curl -s -X POST http://localhost:8765/api/chess/new -H 'content-type: application/json' \
  -d '{"color":"both","fen":"r3k2r/pppb1pp1/5q1p/2Q5/3p4/P7/P3PPPP/2R1KB1R b Kkq - 3 16","skill":5,"think_s":0.5,"voice":true,"auto_reset":false,"max_plies":20,"execute":true,"ack":"<operator acknowledgement>"}'
```

The first call is Black's move. The `/chess` page follows the game as usual.

## While it plays: what to show

- **Slides 1–7:** deck full screen. The arm is the live demo behind you. With a second screen, keep `/chess` on it.
- **Slide 8, `/chess`:** the phase line, the board with the last move, the "Called: …" line, and
  *Piece operations (episodes)*, with one row per pick-and-place trial (#, op, piece, from→to, outcome, and the
  category when it failed). *Robot's last move* shows the move, its piece operations and the wrist camera's photos.
  *Beside the board* shows captured pieces and the spare queens where the robot put them.
- **Questions, Failure Lab at http://localhost:8765/:**
  - *Incidents*: failures grouped across sessions, each with its evidence and a suggested next step. From the chess
    sessions: "label contradicts state" (the recorded outcome disagreed with the independent camera check, 29 Oct
    games), `WATCHDOG_TELEMETRY_LOST` (game 2, 29 Oct), `CAMERA_NO_FRAME` (several of today's sessions). Any incident
    exports as .md or .json.
  - *Episodes*: filter by dataset (chess games are named "ROBOT chess chess-sfhack1-main-…"). Open one to see the
    images, poses, gripper reading and outcome with its evidence.
  - *Label*: the review queue, success or failure plus a category, with keyboard shortcuts.
  - *Quality*: the rule findings behind each status (time gaps, truncated streams, label contradictions).
  - *Replay & regression*, *Evaluation*: the verify step (replays, regression suites, held-out comparisons). Today
    these run on the simulator.
  - *Robot lab*: supervised pick-and-place with the safety model (teach spots, dry run, execute, A/B).
  - *Live robot*: read-only telemetry capture from a Viam machine.
  - *Export*: incident reports, experiment bundles, pilot reports. LeRobot datasets come from the terminal:
    `.venv/bin/python -m amperes.export_lerobot <out_dir> <dataset_id>`.
  - **Atlas fleet memory (vector search, 15 s).** In a terminal:
    `.venv/bin/python -m amperes.cli atlas similar ep_3a3941af689d14f7 -k 6`
    (or Episodes → open that episode → *…across all stations (Atlas Vector Search)*). Say: "This missed grasp from
    today's game matches six missed grasps with identical recorded evidence in other sessions, one from last night,
    found by Atlas Vector Search over all 39 synced sessions." It reads only; it never touches the arm. To sync a new
    game first: `.venv/bin/python -m amperes.cli atlas sync <dataset_id>` (index rows, findings, incidents, events and
    signatures; no raw streams).
- **After the questions:** wait for the phase to read *game over* (the half-move cap; robot vs robot never shows
  *your move*), then press *End game + import* and wait for the phase to read *ended*. Never press it while the phase
  reads *robot moving*: that stops the arm mid-move. The game's trials appear as a new dataset in Episodes, Quality,
  Incidents and the Label queue.

## Switch to human vs robot

1. *End game + import*, only when the phase reads *your move* or *game over* (while the arm moves it would stop it
   mid-move). If the game was tracked and you want the robot to tidy up, press *Reset board* first, while the game is
   still open.
2. Board in the start position.
3. *New game*: *Robot plays* "black (you start)" (the visitor plays White) or "white (robot starts)". Keep *Call out
   every move* ticked, tick *Execute*, type the acknowledgement, leave *Stop after* empty, then *Start game*.
4. The visitor moves a piece by hand and takes the hand away. The page shows *watching for your move*. The wrist
   camera finds the legal move that explains what changed (captures and castling included), the move is called out,
   and the robot answers.
5. If the camera is slow, type the move in the box (`e4`, `Nf3`, `O-O` or `e2e4`) and press *Play* (it stays enabled
   while the camera watches). Typing a move ends the camera watch at once.
6. If the robot doesn't start its turn, press *Robot moves now*.

## Recovery

- **STOP THE ARM** (on `/chess`) sends `stop()` to the arm and gripper at once, even mid-move, and ends the game. The
  trials so far are imported. The e-stop on the arm is the real one.
- **Continue from the same position after a stop.** Read the position, fix the board by hand if needed, then start a
  new game with `"fen"` in the request above:
  `curl -s http://localhost:8765/api/chess | python3 -c 'import json,sys; print(json.load(sys.stdin)["game"]["fen"])'`
- **Gripper missed.** This is handled on its own. A grip that closes on nothing is retried right away, nudged 5 mm and
  lower down, up to six attempts per piece. If the camera then still sees the piece on its square, the move is redone
  (up to three rounds), never recorded. After that the page says "please help the piece on <square> (the game has not
  moved on)". Stand the piece upright on that square, take your hands away and press *Robot moves now*.
- **Camera disagrees with the gripper.** If the gripper held the piece but the camera did not see it arrive, the move
  stands (a second grab on an empty square knocks pieces over) and the trial is flagged "camera did not see the piece
  on the destination square". Check the board by eye. If a piece is off its square, press *STOP THE ARM*, fix it by
  hand and continue from the position. Label the trial on the Label tab later.
- **Holding still before a descent.** The log reads "HAND (or something tall) under the gripper … holding still".
  Clear the area and it carries on. After 20 s it stops the session.
- **The arm stops on its own** (the arm's collision detection, or the watchdog's "no telemetry for 10 s during
  motion"). Nothing clears arm errors automatically: check the arm's state in the Viam app and clear any arm error
  there by hand. Check what is in the gripper, then *End game + import* (the phase reads *stopped*, so the arm is not
  moving) and start again from the position.
- **Voice silent.** Mac volume and output device. The call is still printed under the board.
- **Wi-Fi drop while watching for a human move.** The watch retries on its own (up to five times). If the phase shows
  *failed*, *End game + import* and start again.

## 30-second fallback (arm unavailable)

Say: "The arm is offline, so here is what it recorded over two days: 193 real trials." Then:

1. **Failure Lab → Incidents (5 s):** failures from the chess sessions, grouped, each with evidence.
2. **Episodes → open a chess trial (10 s):** before and after images, the gripper reading, the outcome and why. Game 1
   from 29 Oct is dataset `ds_20261030T015653_a6ecc1`: 10 of 10 pick-and-places succeeded.
3. **Photos (10 s):** `assets/table_camera.jpg` and `assets/robot_view.jpg`, real frames from the robot's cameras.
4. **Close (5 s):** "Every failure you just saw was found, traced and fixed on this arm over two days."

With more time: *Show reset plan* on `/chess` works without the arm (it plans from the last game's tracked layout),
and `.venv/bin/python -m amperes.integrations.chess_sim --scrambles 50 --games 5` runs the reset-planner simulation in
about 15 s.

## Numbers to quote

| Claim | Number | Source |
|---|---|---|
| Pick-and-places in a row after the camera-bias fix (30 Oct) | 14 of 14 | README, *Measured on the event arm* |
| Survey position error after the fitted correction | 4.5 mm mean (was about 15 mm) | README |
| Pick-and-places in game 1 against a person (29 Oct) | 10 of 10 | README |
| Rehearsal, robot vs robot (30 Oct, 2:33 PM) | 6 of 6 half-moves confirmed by the wrist camera and called out loud; 9 pick-and-places, 0 failures | README |
| Workspace scans, commanded poses | 186 poses, 0.34 mm max error | README |
| Claude board reads on 12 known frames (29 Oct) | 94% of squares, 87% of pieces | [CHESS.md](CHESS.md) |
| Reset planner, simulated scrambles of all 34 pieces | 600 of 600, zero gripper contact (simulation) | CHESS.md |
| Atlas Vector Search, missed grasp `ep_3a3941af689d14f7` (30 Oct) | 6 missed grasps with identical recorded evidence in other sessions, one from 29 Oct; all 39 robot sessions synced | README, *Fleet memory on MongoDB Atlas* |
| Tests | 132 passing (`.venv/bin/python -m pytest -q`) | README |
| Chess trials in the lab since the first chess game (29 Oct), counted 2:39 PM 30 Oct | 193 in 36 sessions: 142 succeeded; 38 failed (30 missed grasps, 7 slips, 1 piece not moved); 13 invalid, kept out of training data (8 safety stops, 5 infrastructure errors); 2 flagged critical by rule O002 | `amperes_data/amperes.db`; console, `GET /api/datasets` and `/api/episodes` |

Refresh the last row just before the demo. It only reads, so it is safe while a game runs. Last run, it matched the
2:39 PM count: `36 sessions, 193 trials: {'success': 142, 'invalid': 13, 'failure': 38}`.

```bash
.venv/bin/python - <<'EOF'
import json, urllib.request, collections
B = "http://localhost:8765"
get = lambda p: json.load(urllib.request.urlopen(B + p, timeout=20))
ds = [d for d in get("/api/datasets") if d["name"].startswith("ROBOT chess")]
c = collections.Counter(e["outcome"] for d in ds for e in get(f"/api/episodes?dataset_id={d['dataset_id']}&limit=1000")["rows"])
print(len(ds), "sessions,", sum(c.values()), "trials:", dict(c))
EOF
```

## Submission form text (paste and edit)

**Project name:** Amperes Failure Lab

**One-liner:** Local-first failure analysis for robot manipulation, proven live on a real xArm6 that plays chess
against itself, calls out every move, and records every pick-and-place as a trial the lab can triage.

**What it does:**
- Records every robot attempt (images, depth, poses, the gripper reading, the outcome), labels each failure, groups
  repeats into incidents with a root cause, verifies fixes with replays and regression runs, and exports clean
  datasets in LeRobot format plus Markdown and JSON reports. Local-first: the SQLite store stays on the machine; only
  opt-in Atlas sync and Claude piece recognition send data out.
- Fleet memory on MongoDB Atlas (opt-in): each station syncs episode index rows, quality findings, incidents, events
  and one deterministic failure-signature vector per episode (no raw streams), and Atlas Vector Search finds the same
  failure across sessions. All 39 robot sessions are synced; a missed grasp from a 30 Oct game matched 6 missed grasps
  with identical recorded evidence in other sessions, one from the evening before.
- Proves it on real hardware with chess: 32 small pieces in six shapes, millimetre placement, people's hands next to
  the arm, and every move checkable by the rules of chess and the wrist camera.
- The robot sets up the start position from a messy table of upright pieces, plays a person (it sees your move through
  the wrist camera, or you type it), or plays both sides by itself, and calls out every move.
- It never records a move that didn't happen: an empty grip is retried at once, and the wrist camera checks the board
  after every move. It holds still if a hand is under the gripper, and resets the board after the game, tracking every
  piece, with spare queens kept beside the board for promotion.
- Failures the lab caught on the arm over two days, each traced to a cause and fixed: grasping air beside pieces (a 15 mm
  camera bias, now 4.5 mm mean error, then 14 of 14 pick-and-places in a row), moves recorded that never happened,
  retried grips pressing pieces into the board, collision stops on long swings, a looping reset planner, a typed move
  ignored while the camera watched, and misread pieces.

**How we built it:** Python, the Viam Python SDK (xArm6, G2 gripper, motion service with modelled obstacles, two
RealSense cameras), Stockfish via python-chess, OpenCV and depth for the board grid and move detection, the Claude API
(claude-opus-5-5) for piece recognition checked against measured heights, macOS speech for the move calls, FastAPI and
a single-page console, SQLite for the local store, LeRobot format for export, optional MongoDB Atlas sync with Atlas
Vector Search over deterministic failure signatures.

**Measured results:** 193 real chess trials recorded since the first chess game on 29 Oct (142 succeeded, 38 failures
labelled, 13 invalid kept out of training data); 14 of 14 pick-and-places in a row after the camera-bias fix; 4.5 mm
mean position error (was about 15 mm); 10 of 10 pick-and-places in game 1 against a person; rehearsal on 30 Oct: 6 of
6 robot-vs-robot moves confirmed by the wrist camera, 9 pick-and-places, 0 failures; 186 commanded poses at 0.34 mm
max error; Claude board reads right on 94% of squares and 87% of pieces on 12 known frames; reset planner 600 of 600
simulated scrambles with zero gripper contact; 132 tests passing.

**Reusable for other robot builders:**
- The Failure Lab: ingest any recorded Viam session, quality rules, incidents, labelling, LeRobot export.
- Any Viam arm, gripper and camera, chosen by name (`AMPERES_ARM`, `AMPERES_GRIPPER`, `AMPERES_MOTION`,
  `AMPERES_CAMERAS`).
- `chess_lifecycle` (match and reset planner, pure Python), `chess_claude_vision` (board and piece reads from any
  image), `chess_sim` (offline proof of any plan).

**Links:** https://github.com/vnmoorthy/amperes-haunted-chess · https://vnmoorthy.github.io/amperes-haunted-chess/

**Team / contact:** (fill in)
