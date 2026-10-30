# 3-minute storyboard (deck: deck/Amperes_Haunted_Chess.pptx)

Start the robot game before you speak: robot plays White, *Spooky voice* on. Let it play 1.d4 during slide 1.

| Time | Slide | Say | Do |
|---|---|---|---|
| 0:00–0:15 | 1 Haunted Chess | "This is a haunted chess robot. It plays you, it sees you, it talks back, and every move it makes becomes training data." | The arm plays its opening move; the voice taunts. |
| 0:15–0:35 | 2 Robot data fails silently | "Robot-learning teams pay for every station hour, and some episodes are silently unusable. Chess gives us ground truth for free: known piece, known target, a rulebook as judge." | Invite a judge to play Black. |
| 0:35–1:05 | 3 Live demo | "Watch: I play a move by hand. No typing. The wrist camera finds the legal move that explains what changed. Stockfish answers. Claude writes the taunt." | Judge moves; robot replies within seconds. |
| 1:05–1:25 | 4 How it sees | "It locks the printed grid, all 64 squares, and reads which squares hold a piece from depth." | Point at the labelled overhead view on /chess. |
| 1:25–1:50 | 5 Sets up the board | "It can also set the board up itself: six close views, every piece in arm coordinates, named by Claude and cross-checked by height, and a planner that lifts a tall piece aside like a person." | Mention 600/600 simulated resets, 14/14 real picks. |
| 1:50–2:15 | 6 Catches its mistakes | "Real failures we hit today: a grip on air is retried instantly; a move the camera doesn't see is never recorded; a 15 mm camera bias was measured on pieces it placed itself and corrected; and it holds still if a hand is under the gripper." | If a retry happens live, point at it: that's the feature. |
| 2:15–2:30 | 7 Architecture | "Sensors left, brains middle, data right. One Python repo, 130 tests." | |
| 2:30–2:45 | 8–9 Data and results | "Every move is an audited episode, exported to LeRobot. All numbers here were measured on this arm." | Show the episodes after *End game + import* if time allows. |
| 2:45–3:00 | 10 Built to be reused | "Every module works on any Viam arm. Play it, fork it, haunt it." | Leave the repo link up. |

## Q&A (2 minutes), honest answers

- **Can it stand fallen pieces up?** No. A top-down parallel gripper can't; we tried two methods and turned them off.
- **How good is piece recognition?** Hard from straight above (rook vs pawn). The robot remembers what it placed, height breaks king/queen ties, and a person can correct a label in one line.
- **Is it safe?** Execute needs an operator phrase; envelope check on every target; slow and gentle in the last 6 cm; hand guard; watchdog; STOP and the e-stop.
- **What is the business?** Amperes audits robot-learning data: which episodes are usable and why the rest aren't. The chess robot is the proof on a real arm.
