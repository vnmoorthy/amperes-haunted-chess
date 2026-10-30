# Before the demo (Fri 30 Oct, 3:30 PM) and where the robot work stands

## State at the pause (Thu ~9:45 PM)

- Board, approximately: white rook **a2** (my mistake: it was correctly on a1), knight b1, bishop c1, pawns b2 d2 f2,
  a tall white piece (king or queen) on **h1**; black queen d8, knight g8, pawns a7–e7, g7, h7, tall black piece
  (king) on **h8**. Seventeen pieces are off the board. Set it up by hand before the demo; the robot does not need to.
- Nothing is running on the arm. The console (`python -m amperes.cli serve --port 8765`) can be restarted any time.

## What works and is demonstrable

1. **Autonomous chess** (`/chess`, `color=white, auto=true`): the robot saw the human's moves through the wrist camera
   (a6, h6, b5, bxc4 …), Stockfish replied, the arm played, the camera verified. Game 1: 10 of 10 moves. Game 2 ran
   five more pairs incl. a capture to the side area and castling detection; a link hiccup tripped the watchdog, as
   designed. Datasets `ds_20261030T015653_a6ecc1` (game 1) and `ds_20261030T022255_a0ba73` (game 2).
2. **Workspace scans**: 186 poses, 0.34 mm max error, 0.22 mm repeatability, 372 RGB-D snapshots.
3. **Close scan + piece atlas**: the camera flies to every piece; close-ups are labelled; `amperes_data/robot/piece_atlas.jpg`.
4. **Clearance-ordered placement**: 20 of 23 placements succeeded across the reset runs; corner squares (a1/h1/h8) are the
   weak spot, and labels must come from a person or a better classifier, not from height alone.
5. **The data product**: QC on every episode (the gripper's false "holding" signals were caught by the camera), the
   dataset card (`docs/DATASET_CARD.md`), a LeRobot v2.1 export, Atlas sync, and a Hugging Face LeRobot v3 set imported
   and audited.

## To do in the morning

- Update the deck with the measured numbers (`docs/HACKATHON_PITCH.md`, section "What actually ran").
- Decide the live demo: safest is engine-vs-engine from a sparse endgame (every piece with empty neighbours), 2–3
  moves, with the page showing the camera verification; keep the human-vs-robot game as recorded evidence.
- If the arm is available: calibrate the corner offset (hover test over a1 and h8 with a piece there), then retry corner
  picks; keep pieces with tall neighbours out of the demo position.
- Fill the deck placeholders (team names, contact).
