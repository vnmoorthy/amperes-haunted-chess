# Amperes Failure Lab: 3-minute talk track

Deck: [deck/Amperes_Failure_Lab.pptx](../deck/Amperes_Failure_Lab.pptx) ([PDF](../deck/Amperes_Failure_Lab.pdf)), 10 slides.
Runbook for the station: [DEMO_DAY.md](DEMO_DAY.md).

**The one idea to land:** the Failure Lab is the product. The chess robot is the live proof on real hardware. Every
move it makes is a trial that the lab records, labels and triages.

**Before you say a word.** On http://localhost:8765/chess, start a game: *Robot plays* "both sides (robot vs robot)",
*Call out every move* ticked, *Execute (move the real arm)* ticked with the operator acknowledgement, *Stop after* 20
half-moves. Press *Start game* as the judges walk up. (To continue the rehearsal position instead, with Black to move,
start it from the terminal with its FEN: [DEMO_DAY.md](DEMO_DAY.md#continue-the-rehearsal-game).)
The arm unwinds its wrist, rises to look at the board and makes its first move as you open. In today's robot-vs-robot
games a new move started about every 16 s (median; a quarter took longer than 22 s), so expect 8 to 11 moves during
the talk.

**What the audience will see** is what the 2:33 PM rehearsal showed: robot vs robot, every move checked by the wrist
camera and then called out loud ("Black knight takes D4.", "White knight takes D4.", "Black pawn takes D4."). The
rehearsal ran 6 half-moves: all 6 confirmed by the camera, 9 pick-and-places, 0 failures, then "Game over: stopped
after 6 half-moves."

**Pace.** About 400 spoken words in 3:00. When the robot calls a move, pause for a beat and let it finish. Its voice
is part of the demo.

| # | Slide | Time | Length |
|---|---|---|---|
| 1 | Amperes Failure Lab | 0:00–0:15 | 15 s |
| 2 | Robots fail quietly | 0:15–0:30 | 15 s |
| 3 | What the lab does | 0:30–0:50 | 20 s |
| 4 | Why chess is the proof | 0:50–1:05 | 15 s |
| 5 | The robot and how it works | 1:05–1:25 | 20 s |
| 6 | What the lab caught on the real arm | 1:25–2:00 | 35 s |
| 7 | Safety around people and pieces | 2:00–2:15 | 15 s |
| 8 | Live demo | 2:15–2:35 | 20 s |
| 9 | Built to reuse | 2:35–2:50 | 15 s |
| 10 | Close and links | 2:50–3:00 | 10 s |
| | **Total** | | **3:00** |

---

### 1 · Amperes Failure Lab (0:00–0:15)

- **On screen:** FAILURE LAB, the line *Every robot failure, caught, explained and fixed.*, "Live: a Viam xArm6 that
  plays chess", and the event arm and board seen from the table camera.
- **Say:** "This is Amperes Failure Lab. It finds out why a robot failed, and proves the fix worked. The proof is
  right here: this arm is playing chess against itself, and calling out every move."
- **The arm:** first move under way (White from the start position, Black from the rehearsal position). The call comes
  once the move is made ("White pawn to E4.", or whichever move Stockfish picks), usually as you finish the sentence.
  Glance at the arm when it speaks.

### 2 · Robots fail quietly (0:15–0:30)

- **On screen:** *Robots fail quietly*: small failures all day · evidence in four places · found too late. Beside it,
  a real case from this arm on 29 Oct: arm "motion complete", gripper "holding a piece", wrist camera "pawn still on
  its square", lab verdict `PIECE_NOT_MOVED`.
- **Say:** "Robots fail quietly. Yesterday, on this arm, the gripper said it was holding the piece. The arm finished
  its motion. The camera showed the pawn still on its square. Each signal looked fine alone."
- **The arm:** move 1 lands, the wrist camera checks the board, the move is called, and Black's reply starts.

### 3 · What the lab does (0:30–0:50)

- **On screen:** five steps, *Record → Label → Triage → Verify → Export*, each with one line, a dark screenshot of
  the Incidents tab, and a local-first footnote (SQLite store on this machine; Claude and Atlas calls are opt-in).
- **Say:** "The lab records every attempt: images, depth, poses, the gripper reading, the outcome. It labels each
  failure, traces the cause, checks the fix on replay, and exports clean datasets. On this arm, over two days: 193
  real trials, 38 failures labelled, 13 invalid ones kept out of the training data."
- **If asked about privacy:** the data stays on this laptop unless we opt in to Atlas sync or Claude piece recognition.
- **The arm:** moves 2 and 3. Nothing to point at yet. Let the calls punctuate the list.

### 4 · Why chess is the proof (0:50–1:05)

- **On screen:** 32 pieces, 6 shapes · millimetre placement · a person's hands next to the arm · the rules of chess
  and the wrist camera say whether each move really happened.
- **Say:** "Why chess? It's an honest pick-and-place test. Thirty-two small pieces in six shapes. Millimetre
  placement. A person's hands right next to the arm. And every move can be checked, by the rules and by the wrist
  camera."
- **The arm:** moves 3 and 4. If a piece is in the air, gesture at it on "millimetre placement".

### 5 · The robot and how it works (1:05–1:25)

- **On screen:** photo (`assets/robot_view.jpg`) plus the architecture. Viam machine sfhack1 (xArm6 + G2 gripper,
  wrist RealSense, tripod RealSense, motion service with modelled obstacles) → chess session (Stockfish moves, Claude
  vision checked against depth heights, safety checks, wrist-camera check of every move) → Failure Lab (every
  pick-and-place is a trial).
- **Say:** "The robot is a UFactory xArm6 with a G2 gripper, a RealSense on the wrist and another on a tripod,
  running on Viam. Stockfish picks the moves. Claude names the pieces, and every name is checked against the height
  the depth camera measures."
- **The arm:** moves 4 and 5. Each time it rises to the look pose between moves, the wrist camera is checking the
  board. Point at the wrist camera once.

### 6 · What the lab caught on the real arm (1:25–2:00)

- **On screen:** seven rows, *failure → cause → fix*. Highlight the first two (the ones you say out loud):
  1. Grasping air beside pieces → survey camera bias of about 15 mm, measured on pieces the arm placed itself →
     fitted correction: 4.5 mm mean error, then 14 of 14 pick-and-places in a row.
  2. Moves recorded that never happened (gripper closed on air) → the finger position flags an empty grip, the arm
     retries a few mm over and lower at once, and the wrist camera checks the board after every move.
  3. Retried grips pressed pieces into the board → the piece is set down at the height it was actually gripped; the
     last 60 mm above the board runs at 10°/s, transit at 75°/s.
  4. Collision stops on long fast swings → big wrist turns split into two half-turns, transit routed through the
     motion service.
  5. Board-reset planner looping (the same piece back and forth) → loop guard: visited states, forced follow-up moves,
     an operation limit.
  6. A move typed into the console was ignored while the camera watched → typing a move now ends the watch.
  7. Pieces misread (a rook as a pawn, a bishop misread) → Claude vision plus a measured-height check, a confidence
     gate, and operator corrections that stay with the piece as it moves.
- **Say:** "What the lab caught on this arm over two days. The arm was grasping air beside pieces. The lab traced it to a
  15-millimetre camera bias, measured on pieces the arm placed itself. A fitted correction: 4.5 millimetres mean
  error, then 14 pick-and-places in a row. It was also recording moves that never happened: the gripper had closed on
  air. Now an empty grip is retried on the spot, and if camera and gripper agree a move didn't happen, it's redone,
  never recorded. Five more on the slide, each traced and fixed."
- **The arm:** moves 5 to 7. If a grip retry happens now, stop and point at it: "That's the empty-grip check, live."
  If you are running long, drop the second story and go straight to "Five more on the slide".

### 7 · Safety around people and pieces (2:00–2:15)

- **On screen:** hand guard (anything more than 110 mm above the board under the gripper: hold) · safety envelope
  (at most 100° of wrist turn per move, clearance margins) · slow zone (last 60 mm at 10°/s) · telemetry watchdog ·
  operator acknowledgement with the e-stop in reach.
- **Say:** "Before every descent, the wrist camera looks under the gripper: a hand there, and it holds still. The
  last six centimetres are slow, a watchdog stops it if telemetry drops, and one of us stays at the e-stop."
- **The arm:** moves 7 and 8. Each descent shows the slow last 6 cm, so point at one. Never put a hand near the board
  to demonstrate the guard.

### 8 · Live demo (2:15–2:35)

- **On screen:** the slide (*Robot vs robot, every move called out loud*) for a moment, then switch to the console at
  http://localhost:8765/chess: the board, the "Called: …" line under it, and the *Piece operations (episodes)* table
  with each trial's outcome.
- **Say:** "Now watch the arm. It's playing both sides, the wrist camera checks every move, and then it calls the
  move. On the console, every piece it moves shows up as a trial, with its outcome. When the game ends, one click
  imports every trial into the lab, next to the 193 already there."
- **The arm:** moves 8 to 10. Point at the newest row in *Piece operations* as it appears. In the rehearsal every row
  read success: 9 pick-and-places, 0 failures.

### 9 · Built to reuse (2:35–2:50)

- **On screen:** any Viam arm, gripper and camera, picked by name (`AMPERES_ARM`, `AMPERES_GRIPPER`,
  `AMPERES_MOTION`, `AMPERES_CAMERAS`) · ingests any recorded Viam session · LeRobot export · the same failure, found
  across sessions in MongoDB Atlas · 132 tests.
- **Say:** "None of this is tied to chess. Any Viam arm works, and the lab exports LeRobot datasets. MongoDB Atlas is
  the fleet memory: a missed grasp from today found six with identical evidence in other sessions, one from last
  night."
- **The arm:** keeps playing. Don't turn around.

### 10 · Close and links (2:50–3:00)

- **On screen:** *The lab is the product. The chess robot is the proof.* Code: github.com/vnmoorthy/amperes-haunted-chess
  · landing page: vnmoorthy.github.io/amperes-haunted-chess
- **Say:** "If the lab can debug a chess-playing robot in two days, it can debug your pick-and-place cell. Amperes
  Failure Lab. Thank you."
- **The arm:** still playing. It stops itself at the 20-half-move cap, around the end of the questions. Press
  *End game + import* only once the phase reads *game over*: pressed while the arm moves, it stops the arm mid-move.

---

## Q&A prep (2 minutes)

Answer in two or three sentences, then stop.

1. **What is real, and what is demo?**
   Everything on the arm is live: a real xArm6, real cameras, real Stockfish moves, nothing scripted. Since the first
   chess game on 29 Oct the lab has recorded 193 real trials on this arm: 142 succeeded, 38 failures labelled, 13
   invalid ones kept out of the training data. Measured on this arm: 14 of 14 pick-and-places in a row after the camera
   fix, 4.5 mm mean position error, and 10 of 10 pick-and-places in our first game against a person yesterday. The
   600-of-600 board-reset result is simulation, and we label it that way.

2. **What still fails?**
   It can't stand a fallen piece back up: a top-down parallel gripper can't do that. From straight above, a rook and a
   pawn look alike, so piece identity needs the height check and sometimes an operator's correction. Speed is capped
   by the arm's own collision detection, because faster swings tripped it.

3. **Why not just read the logs?**
   Logs record what you commanded, not what happened: the pawn case on slide 2 looked fine in every single log. The
   lab joins the gripper reading, the before and after images, the poses and the outcome of each attempt, gives it one
   status with its evidence, and groups repeats into incidents you can re-test.

4. **Does it work beyond chess?**
   Chess is the workload, not the product. The arm, gripper and cameras are picked by name, so any Viam arm, gripper
   and camera works.
   The lab ingests any recorded Viam session, and it already pulled in a third-party LeRobot dataset (17 SO-100
   teleop episodes) through the same pipeline. Datasets export in LeRobot format.

5. **Is it safe to stand next to?**
   Nothing moves unless Execute is ticked and the operator types the acknowledgement with the e-stop in reach. Every
   target is checked against an envelope, including at most 100° of wrist turn per move. Before each descent the wrist
   camera looks for anything more than 110 mm above the board and holds; if it stays 20 seconds, the session stops.
   The last 60 mm runs at 10°/s, a watchdog stops the arm after 10 s without telemetry, and STOP works mid-move.

6. **How do you know a move really happened?**
   Two signals. The gripper's finger position after the grab: fully closed means it grabbed air, so it retries at once,
   a few millimetres over and lower. Then the wrist camera compares the board before and after. If both say the move
   didn't happen, it's redone, and after three rounds it stops and asks a person. If they disagree, the trial is
   flagged for review in the lab.

7. **How good is piece recognition?**
   From straight above it's hard. Claude, reading the overhead photo with every square labelled plus the table
   camera's view, got 94% of squares and 87% of pieces right on 12 known frames from yesterday. The depth height check settles ties, unsure pieces are
   left for another look, and an operator's correction stays with the piece as it moves. During a game it doesn't need
   to recognise anything, because it knows the position from the moves.

8. **What does MongoDB do here?**
   Atlas is the fleet memory, and it's opt-in. Each station syncs episode index rows, quality findings, incidents,
   events and one failure-signature vector per episode, never raw streams, and Atlas Vector Search finds the same
   failure in other sessions. All 39 robot sessions are synced: a missed grasp from today matched six missed grasps
   with identical recorded evidence, one from the evening before. The signatures are deterministic vectors built from
   the recorded evidence, not a learned embedding.

9. **Who is it for, and what's next?**
   Teams running pick-and-place cells or collecting robot-learning data. We have no customers yet. Next: run the lab
   in shadow mode on another team's recorded sessions, train a small local piece recogniser on the held-piece photos
   the robot already saves, and turn each incident into a regression test the arm re-runs.

## If something goes wrong on stage

- **A grip misses.** Don't apologise. It retries a few millimetres over on its own. Point at it: "That's the
  empty-grip check, live."
- **The page asks for help with a piece** ("please help the piece on …"). The robot refused to record a move that
  didn't happen. Say so. Stand the piece on its square, then press *Robot moves now*.
- **The arm stops** (collision stop, telemetry watchdog). Keep talking. The newest row in *Piece operations* already
  shows the category the lab will file. Press *End game + import* and continue with the console.
- **It holds still before a descent.** That's the hand guard. Clear the area and it carries on. After 20 s it stops
  the session.
- **No voice.** Turn up the Mac volume. The called move is still printed under the board ("Called: …"). Carry on.
- **Arm or network down.** Switch to the 30-second fallback in [DEMO_DAY.md](DEMO_DAY.md#30-second-fallback-arm-unavailable).
- **Anything unsafe.** Press *STOP THE ARM*, then the physical e-stop. Nobody reaches under the gripper while it moves.
