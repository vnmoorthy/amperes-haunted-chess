# Chess against Stockfish on the event arm

A human plays a real board; Stockfish picks the robot's reply; the xArm6 picks the piece up and places it. Every piece
move is a labelled pick-and-place episode, and every robot turn adds two overhead board photos labelled with the exact
position (FEN). Code: `amperes/integrations/chess_robot.py`, HTTP: `amperes/robot_api.py`, page: `/chess`.

## Why this is good data

- **Known object, known target, independent label.** The piece type is known from the game, the target square from the
  move, and the human sees whether the piece landed. Outcomes come from gripper signals (grab reported, still holding
  after the lift), the same verifier as `ROBOT_LAB.md`, plus the human's label on the Label tab.
- **Natural variety.** Six piece shapes, 64 squares, crowded and empty neighbourhoods, captures to a side area, castling.
- **A vision dataset for free.** Before and after each robot move, the wrist camera photographs the board from
  ~38 cm above its centre; `moves.jsonl` pairs each photo with `fen_before` / `fen_after`.

## Safety (unchanged from `ROBOT_LAB.md`)

Dry run by default; motion needs *Execute* plus the operator phrase. The envelope is the board plus the capture area,
with a floor 5 mm above the board surface and a ceiling at the look pose. Every target is checked before it reaches the
SDK; moves go through the motion service; the telemetry watchdog stops the arm after 10 s without data during motion
(`WATCHDOG_S` in `viam_control.py`); STOP halts it mid-move. *End game + import* pressed while the arm is moving stops
it the same way, so end a game only when the phase reads *your move* or *game over*. Fingers descend on the board diagonal (`theta = board angle + 45°`) so they clear neighbouring pieces.

## Calibration (3 teach points, about 3 minutes)

1. Put the board flat inside the arm's reach, with an empty strip beside it for captured pieces.
2. In the Viam app, jog the arm with the gripper **open and turned 45°** so the fingertips touch the board at the centre
   of **a1**. On `/chess` click *Teach a1* (or `python -m amperes.cli robot chess teach a1`).
3. Same at **h1**, then at the first captured-piece spot (*Teach capture area*; the CLI point is `graveyard`). a8 follows from a1 → h1 → a8 being
   counter-clockwise seen from above, which is true of every real board.
4. Enter the king's height in mm (all piece heights scale from Staunton proportions) and save.

Grasp height is 45 % of the piece height; transport height clears the king by 25 mm.

## Autonomous play (what ran on 29 Oct)

With `auto` on, the robot needs no typed input. After each of its moves it lifts the wrist camera ~56 cm over the board
centre (the only height at which the D435 sees all 64 squares), locks the printed grid (OpenCV corner detection in the
empty middle ranks, affine lattice, labels anchored on the pieces, best of six frames), and polls about every 0.7 s
(`poll_s` in `watch_human_move`): each
square's occupancy comes from depth height above the fitted board plane, its colour from the piece pixels' brightness.
Squares the camera misreads at rest are listed once and ignored. The human's move is the legal move that explains the
changed squares (from- and to-square), beats every other legal move, and holds for two consecutive frames; castling
and captures are matched the same way. Stockfish then replies, the arm plays it, and the camera checks the result
before the next watch. A confident gripper is never overridden by a doubtful camera read (an earlier version retried a
move that had already succeeded).

Calibration lessons from the night, all in the code now: the fingertips are 200 mm below the flange (the frame system
says 150); the board plane is the frame system's table (-23 mm), not the camera's depth (30 mm bias); pieces are gripped
10 mm above the board by the base, not at a fraction of their height; captured pieces go in a line parallel to the
board edge 12.5 cm outside it (stepping along the file axis walked the second one into the a1 rook).

## Playing

- `/chess`: start a game (robot black means you move first), type moves in SAN (`Nf3`, `O-O`) or UCI (`g1f3`), press
  Play. The robot replies on its own; the page shows the plan, each piece operation's outcome, and the board photos.
- CLI: `python -m amperes.cli robot chess play --execute --ack "..." --skill 5 --think 0.5`.
- A failed pick does not stop the game: the game state follows the engine, you fix the piece by hand, and the failed
  episode stays in the data with its category.
- *End game + import* writes the session (`chess_<time>/episodes.jsonl`, `moves.jsonl`, `images/`) and imports it; the
  episodes then appear in Episodes, Quality, Incidents and the Label queue, and can be synced to Atlas or exported.

## Limitations

- Piece types are not recognised from the overhead camera alone (see "Recognising pieces" below); during a game they
  don't need to be, because the robot knows the position from the moves.
- Under-promotion, or a second promotion once both spare queens are used, needs a hand swap unless a captured piece of
  that type is beside the board. Pieces knocked over are reported by the Claude check, not fixed.
- Teach accuracy sets placement accuracy (about ±3 mm by eye); squares smaller than 40 mm will need a tighter grasp.

## Recognising pieces (added 30 Oct)

`amperes/integrations/chess_claude_vision.py` asks Claude (claude-opus-5-5, structured JSON output, server-side fallback
on a declined request) to read the board from the wrist camera's overhead photo with every square's name drawn in its
corner, plus the table camera's angled view when it has one. Measured on the 12 robot-move frames from 29 Oct whose
position is known:

| Mode | Squares right | Pieces right | Time per read |
|---|---|---|---|
| Whole-board read, overhead + table camera | 93.8% | 87.3% | 46 s |
| Whole-board read, overhead only | 93.4% | 87.4% | 35 s |
| Check against a believed position | flags real anomalies (a knight lying on its side) | caught 2 of 6 one-move-stale beliefs | 10 s |

Most errors are knights and bishops read as pawns: from straight above their tops look alike, which is also why last
night's height rules failed. The table camera on its new stand sees the pieces' profiles; during last night's games it
sat at table height and saw only the near edge, so it added nothing then. The check mode leans towards confirming what
it is told, so it is used for anomalies (fallen pieces, pieces off the board), not to detect moves; the depth camera
does that.

## A whole match: slots, spare queens, promotion, reset (added 30 Oct)

`amperes/integrations/chess_lifecycle.py` tracks every physical piece, on the board and beside it. Captured pieces go to
spots in lines parallel to the a-file, 2.3 squares outside it, one square apart, only within 640 mm of the arm base. The
set's two spare queens live on `reserveQ` and `reserveq` (the 5th and 6th spots of the outer line from the rank-1 end).
Promotion is done the way a person does it: the pawn leaves the board, the spare queen comes in.

After the game, `plan_reset` puts everything back: short pieces first, a wrong piece on a home square moved aside first,
and when a tall piece crowds a spot (a pawn next to a king leaves the gripper body 5 mm too low), the king is lifted onto
a free square, the pawn placed, and the king put back. Fool's mate resets in 6 operations, Scholar's mate in 11.
`amperes/integrations/chess_sim.py` replays every plan on a virtual board with the same gripper model:

| Simulation | Result |
|---|---|
| Random scrambles of all 34 pieces (board and slots), final code | 600 of 600 exact start positions, zero gripper contact |
| Stockfish-vs-Stockfish games then reset, 2 slot lines (12 spots) | 34 of 40 fit the slots; every reset clean |
| Same with 4 slot lines (23 spots) | 40 of 40; 3 promotions with the spare queen; every reset clean |

The robot's move choice uses the same model: among Stockfish's five best moves it plays the one the hand can make
cleanly, which cut contact moments during simulated play from 32 to 4. The number of slot lines is the `grave_lines`
setting in `chessboard.json` (default 2); more lines need clear table further out. On the console, *Show reset plan*
and *Reset board* use the tracked layout; "Reset the board when the game ends" runs it on its own.

## Reading and resetting the board (added late on 29 Oct)

- `python -m amperes.integrations.chess_scan`: autonomous close scan. From the look pose it reads occupancy, then flies
  the wrist camera over every occupied square at 35 cm and classifies the piece from the close-up depth (height classes;
  a king's or queen's top is inside the sensor's minimum range, which itself reads as "tall"), saving a labelled close-up
  per piece and `recognized.json`. Type accuracy from 56 cm is poor (narrow crowns); the close pass is the fix.
- `python -m amperes.integrations.chess_reset pawns|pieces`: sets the board up from a staged pattern (pieces on the light
  squares of ranks 3-6 in a fixed layout), placing pawn rows first and then rooks, knights, bishops, queen, king, so every
  destination's neighbours are short or empty; the grip rises beside tall neighbours.
- Gripper limits that shape all of this: open span ~85 mm, body 75 x 110 mm with its underside 50 mm above the fingertips.
  On 54 mm squares a piece can only be picked or placed when its orthogonal neighbours are empty or shorter than the
  body's clearance; the start position is therefore built in a clearance-preserving order, never piece by piece at random.

## Arranging the board by itself (added 30 Oct)

`python -m amperes.integrations.chess_arrange --ack "<operator phrase>"` sets up the start position from whatever is on the
board, in rounds:

1. **Look.** From the look pose the wrist camera locks the printed grid; depth marks the squares with something on them.
2. **Read.** Claude reads every piece from the overhead photo with each square's name drawn in it, plus the table
   camera's view. A square counts as occupied if either the depth camera or Claude sees something there (on 30 Oct the
   depth camera missed two standing pawns); Claude's colour wins when it names a piece (the brightness test called a
   cream pawn black). Fallen pieces, pieces across a border and anything nobody can name become no-go squares: never
   moved, never placed on, kept clear of as if they were kings. The set's make-up settles impossible reads (two white
   kings: the less certain one is the queen).
3. **Plan** with the lifecycle planner (short pieces first, crowding pieces lifted aside).
4. **Move, looking at each piece.** After every lift the table camera photographs the held piece and Claude names it, the
   way a person glances at a piece in their hand. A piece that is not what the plan assumed goes to its own home, or to a
   free middle square, and the rest is planned again. Every lifted-piece photo is kept (`images/held_*.jpg`) with what
   Claude said: labelled training data for a local recogniser later.
5. **Look again** after a failed pick or at the end of the plan, until the board shows the start position.

The spots beside the board are only used with `--slots-clear`; without it the two spare queens stay wherever they are.
`--plan-only` stops after the plan (it still moves the arm to the look pose, so it needs the acknowledgement too).
