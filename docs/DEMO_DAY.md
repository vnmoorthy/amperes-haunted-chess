# Demo day: Fri 30 Oct (submit by 3:00 PM, live demo 3:30 PM)

Judging: functionality 40%, ambition 20%, creativity 20%, modularity 15%, Halloween 5%. Three minutes of live demo at the
station, two minutes of questions. No slides.

## The 3-minute demo: "Haunted chess"

Setup before the judges arrive: board in the start position, the spare queens in their reserve spots beside the a-file,
cam2 on its stand, the console on http://127.0.0.1:8765/chess, Mac volume up, "Spooky voice" ticked.

| Time | What happens | What you say |
|---|---|---|
| 0:00 | Press *Start game*: robot plays white, Execute on, voice on. The arm rises to look at the board. | "This is a haunted chess robot. A Viam xArm, two depth cameras, Stockfish for the moves, and Claude as its eyes and its voice." |
| 0:15 | The robot plays its first move; the voice taunts while the arm moves. | "It says its move out loud, picks the piece up and puts it down, slowly near the board." |
| 0:50 | A judge plays black's reply by hand. The page shows "watching for your move", then the move it saw. | "Nobody types anything. The wrist camera sees which squares changed and works out the legal move you made, castling and captures included." |
| 1:20 | The robot replies (ideally a capture: the taken piece goes to a slot beside the board). | "Captured pieces go beside the board, and it remembers exactly where. It also keeps the spare queens there, for promotion." |
| 1:50 | Open the Amperes console: the moves just played are episodes with a status and evidence. | "Every move is also robot training data. Amperes audits each episode: last night it caught the gripper saying 'got it' while the camera showed the pawn never moved." |
| 2:20 | Back on the chess page: *Show reset plan* (or press *Reset board* if time allows). | "When the game ends it puts everything back like a person would, lifting the king aside to slip a pawn in. In simulation it reset 600 of 600 random scrambles of all 34 pieces without touching a neighbour." |
| 2:50 | Close. | "Every piece of this is a module another Viam builder can reuse: the planner, the vision, the data audit." |

**If the arm or the network fails:** keep talking and switch to the recorded evidence. Game 1 photos and episodes are
in the console (dataset `ds_20261030T015653_a6ecc1`), and *Show reset plan* works offline.
`python -m amperes.integrations.chess_sim --scrambles 50 --games 5` runs a live simulation in about 15 s.

**If you have more time (Q&A or after):** a robot-vs-robot game from the start position with "Stop after 8
half-moves" and "Reset the board when the game ends" ticked shows the whole cycle hands-free.

## Before the demo (in this order)

1. The machine is online in the Viam app (sfhack1). Then `.venv/bin/python -m amperes.cli serve --port 8765`.
2. Board check from the look pose: the grid overlay must line up with the squares (the board may have moved overnight).
   If it moved, re-teach a1, h1 and the graveyard spot.
3. Corner check: hover over a1 and h8 with the gripper open, 2 cm up, and look from the side. Last night's corner picks
   missed; fix the offset before the demo.
4. One rehearsal move with Execute on, a person at the e-stop.
5. The spare queens on their reserve spots: `reserveQ` and `reserveq` are the 5th and 6th spots of the outer slot line,
   counted from the rank-1 end (see *Beside the board* on the page, or `GET /api/chess/reset/plan`).

## Q&A cheat sheet (honest answers, with numbers)

- **"How does it see your move?"** Depth camera over the board: which squares changed height and colour, matched against
  every legal move; the move must hold for two frames. Game 1: 10 of 10 robot moves confirmed by the camera, and the
  human's moves were seen without typing, castling included.
- **"Can it recognise the pieces?"** From straight above, rooks and pawns (and kings and queens) look alike: our height
  rules confused them last night. Claude reading the overhead photo with labelled squares got 94% of squares and 87% of
  pieces right on 12 labelled frames from last night; most errors were knights and bishops read as pawns, the same
  top-down problem. During a game the robot does not need to recognise types: it knows the position from the moves.
  Claude also checks the board for anomalies: it spotted a knight lying on its side that our labels had missed.
- **"What happens after the game?"** A planner puts every piece home, short pieces first, and when a tall piece crowds a
  spot it lifts it aside, places the short one, and puts it back. Simulated: 600 of 600 random scrambles of all 34 pieces
  reached the exact start position with zero gripper contact; 40 of 40 complete engine games reset cleanly with four
  lines of slots beside the board (34 of 40 fit with the default two lines). Not yet run end to end on the real arm.
- **"Promotion?"** The pawn leaves the board and the spare queen comes in from its reserve spot, like a person does.
  Simulated: 3 promotions in 40 engine games.
- **"What failed?"** Last night: 37 piece-move episodes, 19 verified successes, 12 failures, 6 invalid. Corner squares
  were the weak spot; a camera "centring" feature pulled picks 25 mm off and was removed. Placement runs: 20 of 23.
- **"Is it safe?"** Dry run unless Execute is ticked and the operator types the acknowledgement; every target is checked
  against a box around the board; it moves slowly within 12 cm of the board and in 5 mm steps near pieces; a telemetry
  watchdog stops the arm when data stops; STOP works mid-move.
- **"Why Claude?"** It is the part that knows what chess pieces look like in real life, it answers with a confidence per
  square, and it writes the taunts. The robot never waits on it during a move.

## Submission form text (paste and edit)

**Project name:** Haunted Chess: a robot that plays, tidies up, and audits its own data

**One-liner:** A Viam xArm that plays chess against you with Stockfish, sees your moves with a depth camera, recognises
pieces with Claude, taunts you in a spooky voice, puts every piece back after the game, and turns every move into
audited robot-learning data.

**What it does:**
- Plays real chess on a real board: Stockfish picks the move, the arm picks the piece up and places it, slowly near the
  board, and the camera confirms the move happened.
- Watches the human play: the wrist depth camera detects which legal move you made, including captures and castling.
- Handles a whole match like a person: captured pieces go beside the board to tracked spots, promotion uses the set's
  spare queen, and after the game a planner puts all 34 pieces back in an order the gripper can physically manage.
- Recognises pieces with Claude: square-labelled photos from both cameras, a confidence per square, and an anomaly check
  (fallen pieces, pieces off the board).
- Halloween: a creaky voice announces each move with a line Claude writes on the fly; checkmate gets sung.
- Audits its own data: every move is an episode in Amperes with a status and evidence, exportable to LeRobot.

**How we built it:** Python, Viam Python SDK (arm, gripper, motion service, two RealSense cameras), Stockfish via
python-chess, OpenCV for the board grid and depth, the Claude API (claude-opus-5-5) for piece recognition and commentary,
FastAPI for the console, MongoDB Atlas for cross-station search, LeRobot format for export.

**Measured results:** game 1: 10 of 10 robot moves camera-confirmed; 186 commanded poses at 0.34 mm max error; Claude
board reads 94% of squares right on last night's frames; reset planner 600 of 600 simulated scrambles with zero gripper
contact.

**Reusable for other robot builders:**
- `chess_lifecycle`: the match planner. It is pure Python and needs only three taught points and the gripper's size.
- `chess_claude_vision`: board and piece recognition from any camera image.
- `chess_sim`: offline proof of any plan.
- The Amperes audit: any Viam robot's episodes, quality checks and LeRobot export.

See README "Chess robot toolkit".

**Team / contact:** (fill in)
