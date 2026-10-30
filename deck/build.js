const pptxgen = require("pptxgenjs");
const { applyTheme } = require(process.env.SKILL + "/scripts/apply_theme.js");
const THEME = { name: "Haunted Night", headFontFace: "Cambria", bodyFontFace: "Calibri",
  colors: { dk1: "1A1026", lt1: "F7F2EA", dk2: "2B1B3D", lt2: "C9BEDA", accent1: "FF8C00", accent2: "8E5CC4", accent3: "4CC9A0",
            accent4: "F2C14E", accent5: "E05A6B", accent6: "6FA8DC", hlink: "FF8C00", folHlink: "C9BEDA" } };
const pres = new pptxgen(); pres.layout = "LAYOUT_WIDE"; pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
pres.title = "Amperes Haunted Chess"; pres.author = "vnmoorthy";
const C = pres.SchemeColor; const W = 13.333, H = 7.5;
pres.defineSlideMaster({ title: "NIGHT", background: { color: "1A1026" },
  objects: [{ text: { text: "AMPERES · HAUNTED CHESS", options: { x: 0.6, y: 6.95, w: 6, h: 0.35, fontSize: 10, color: C.background2, charSpacing: 3 } } }],
  slideNumber: { x: 12.3, y: 6.95, w: 0.5, h: 0.35, fontSize: 10, color: C.background2 },
  placeholders: [{ placeholder: { options: { name: "title", type: "title", x: 0.6, y: 0.45, w: 12.1, h: 0.9, fontSize: 38, bold: true, color: C.background1, fontFace: "Cambria" }, text: "" } }] });
const sec = (t) => pres.addSection({ title: t });
const title = (s, t) => s.addText(t, { x: 0.6, y: 0.45, w: 12.1, h: 0.9, fontSize: 38, bold: true, color: "F7F2EA", fontFace: "Cambria", isTextBox: true, margin: 0, objectName: "title" });
const notes = (s, n) => s.addNotes(n);
function card(s, x, y, w, h, head, body, accent) {
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h, rectRadius: 0.12, fill: { color: "2B1B3D" }, line: { color: "3D2A55", width: 1 }, objectName: "card " + head });
  s.addShape(pres.shapes.OVAL, { x: x + 0.3, y: y + 0.3, w: 0.5, h: 0.5, fill: { color: accent }, objectName: "dot " + head });
  s.addText(head, { x: x + 0.95, y: y + 0.27, w: w - 1.2, h: 0.55, fontSize: 18, bold: true, color: C.background1, margin: 0, isTextBox: true, fontFace: "Calibri" });
  s.addText(body, { x: x + 0.3, y: y + 0.95, w: w - 0.6, h: h - 1.15, fontSize: 14, color: C.background2, valign: "top", margin: 0, isTextBox: true });
}
function stat(s, x, y, big, label, col) {
  s.addText(big, { x, y, w: 3.8, h: 1.1, fontSize: 60, bold: true, color: col, fontFace: "Cambria", margin: 0, isTextBox: true });
  s.addText(label, { x, y: y + 1.1, w: 3.8, h: 0.7, fontSize: 14, color: C.background2, margin: 0, valign: "top", isTextBox: true });
}
// 1 title
sec("Opening");
let s = pres.addSlide({ masterName: "NIGHT", sectionTitle: "Opening" });
s.addImage({ path: "../assets/table_camera.jpg", x: 6.9, y: 0.9, w: 5.9, h: 4.42, objectName: "arm photo", altText: "The xArm over the chessboard, seen by the table camera", rounding: false });
s.addText("🎃 AMPERES", { x: 0.6, y: 1.2, w: 6, h: 0.5, fontSize: 18, bold: true, color: C.accent1, charSpacing: 4, isTextBox: true, margin: 0 });
s.addText("Haunted Chess", { x: 0.6, y: 1.75, w: 6.3, h: 1.3, fontSize: 60, bold: true, color: C.background1, fontFace: "Cambria", isTextBox: true, margin: 0 });
s.addText("A robot arm that sees your move, answers with Stockfish, taunts you, sets the board up itself, and audits every move as training data.", { x: 0.6, y: 3.15, w: 6.0, h: 1.5, fontSize: 18, color: C.background2, isTextBox: true, margin: 0 });
s.addText("github.com/vnmoorthy/amperes-haunted-chess", { x: 0.6, y: 5.6, w: 6.3, h: 0.4, fontSize: 14, color: C.accent1, isTextBox: true, margin: 0 });
notes(s, "Hook: this is a haunted chess robot. It plays you, it sees you, it talks back, and it learns from its own mistakes.");
// 2 problem
sec("Why");
s = pres.addSlide({ masterName: "NIGHT", sectionTitle: "Why" }); title(s, "Robot data fails silently");
card(s, 0.6, 1.7, 3.85, 4.6, "Hours of episodes", "Robot-learning teams pay for every station hour. Some episodes come out unusable: missed grips, wrong labels, camera gaps. Nobody notices until training.", C.accent5);
card(s, 4.75, 1.7, 3.85, 4.6, "Chess is the test bench", "Known object, known target, an independent label from the game. Six piece shapes, crowded squares, captures, castling: real manipulation, judged by a rulebook.", C.accent1);
card(s, 8.9, 1.7, 3.85, 4.6, "So we built both", "A robot that plays real chess, and an audit layer that turns every pick-and-place into an episode with a verdict and the evidence behind it.", C.accent3);
notes(s, "The problem is unusable robot data. Chess gives us ground truth for free.");
// 3 demo
sec("Product");
s = pres.addSlide({ masterName: "NIGHT", sectionTitle: "Product" }); title(s, "What you see in the live demo");
const steps = [["1", "Robot opens", "Stockfish picks; the arm plays, fast in the air, gentle near pieces"], ["2", "You reply by hand", "No typing: the wrist camera finds the legal move you made"], ["3", "It answers and taunts", "Claude writes a spooky line; the voice reads it as the arm moves"], ["4", "Every move is data", "Episodes with verdicts in the Amperes console, LeRobot export"]];
steps.forEach(([n, h, b], i) => { const x = 0.6 + i * 3.1;
  s.addShape(pres.shapes.OVAL, { x: x + 1.0, y: 1.8, w: 0.9, h: 0.9, fill: { color: C.accent1 }, objectName: "step " + n });
  s.addText(n, { x: x + 1.0, y: 1.8, w: 0.9, h: 0.9, fontSize: 28, bold: true, color: C.text1, align: "center", valign: "middle", isTextBox: true, margin: 0 });
  s.addText(h, { x, y: 3.0, w: 2.9, h: 0.6, fontSize: 20, bold: true, color: C.background1, align: "center", isTextBox: true, margin: 0 });
  s.addText(b, { x: x + 0.1, y: 3.65, w: 2.7, h: 1.6, fontSize: 14, color: C.background2, align: "center", valign: "top", isTextBox: true, margin: 0 }); });
s.addText("“C four! I offer a pawn, like candy from a haunted house.”", { x: 0.6, y: 5.6, w: 12.1, h: 0.6, fontSize: 18, italic: true, color: C.accent4, align: "center", isTextBox: true, margin: 0 });
notes(s, "Walk the four beats. Quote the actual line the robot said today.");
// 4 seeing
s = pres.addSlide({ masterName: "NIGHT", sectionTitle: "Product" }); title(s, "How it sees your move");
s.addImage({ path: "../assets/robot_view.jpg", x: 0.6, y: 1.55, w: 6.2, h: 4.65, objectName: "overhead view", altText: "The robot's overhead view with every square labelled" });
s.addText([{ text: "Locks the printed grid from 56 cm: all 64 squares, 0.5 px fit", options: { bullet: true, breakLine: true } },
  { text: "Depth says which squares hold a piece; brightness says whose", options: { bullet: true, breakLine: true } },
  { text: "Finds the one legal move that explains the change, seen twice", options: { bullet: true, breakLine: true } },
  { text: "Castling, captures and en passant matched the same way", options: { bullet: true } }],
  { x: 7.2, y: 1.7, w: 5.5, h: 4.3, fontSize: 18, color: C.background1, paraSpaceAfter: 14, valign: "top", isTextBox: true });
notes(s, "Every square labelled by the robot itself. It finds your move as the legal move that explains what changed.");
// 5 setup
s = pres.addSlide({ masterName: "NIGHT", sectionTitle: "Product" }); title(s, "It sets up the board by itself");
card(s, 0.6, 1.7, 3.85, 3.0, "Six-view survey", "The wrist camera visits six poses over the board and 10 cm around it; every piece becomes a 3D blob in arm coordinates.", C.accent2);
card(s, 4.75, 1.7, 3.85, 3.0, "Named twice", "Claude names each zoomed piece; its measured height must agree. A king is 85 mm, a pawn 45.", C.accent6);
card(s, 8.9, 1.7, 3.85, 3.0, "Clearance planner", "Short pieces first; a tall neighbour is lifted aside and put back, like a person does.", C.accent3);
stat(s, 0.6, 5.0, "600/600", "simulated scrambles of 34 pieces reset with zero gripper contact", C.accent1);
stat(s, 4.75, 5.0, "14/14", "real pick-and-places in a row after calibration", C.accent3);
stat(s, 8.9, 5.0, "4.5 mm", "survey position error, down from 15 mm", C.accent4);
notes(s, "This is the ambitious part: recognition plus planning plus manipulation.");
// 6 self-correction
s = pres.addSlide({ masterName: "NIGHT", sectionTitle: "Product" }); title(s, "It catches its own mistakes");
const rows = [["Empty grip", "Finger gap reads -2 on air vs 12+ mm on a piece: retry at once, nudged and lower", C.accent5],
  ["Phantom move", "If the camera does not see the move, it is redone, never recorded as played", C.accent1],
  ["Camera bias", "Measured on pieces the arm placed itself: a 15 mm offset, corrected by a fitted transform", C.accent6],
  ["Hands", "Before every descent the camera checks under the gripper and holds still for anything tall", C.accent3]];
rows.forEach(([h, b, col], i) => { const y = 1.65 + i * 1.25;
  s.addShape(pres.shapes.OVAL, { x: 0.7, y: y + 0.15, w: 0.6, h: 0.6, fill: { color: col }, objectName: "icon " + h });
  s.addText(h, { x: 1.6, y, w: 3.0, h: 0.9, fontSize: 22, bold: true, color: C.background1, valign: "middle", isTextBox: true, margin: 0 });
  s.addText(b, { x: 4.7, y, w: 8.0, h: 0.9, fontSize: 16, color: C.background2, valign: "middle", isTextBox: true, margin: 0 }); });
notes(s, "Robustness is the functionality story: real failures we hit today and how the robot now handles them.");
// 7 architecture
sec("Architecture");
s = pres.addSlide({ masterName: "NIGHT", sectionTitle: "Architecture" }); title(s, "Architecture");
const box = (x, y, w, h, t, col) => { s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h, rectRadius: 0.1, fill: { color: "2B1B3D" }, line: { color: col, width: 2 }, objectName: "box " + t });
  s.addText(t, { x, y, w, h, fontSize: 14, color: C.background1, align: "center", valign: "middle", isTextBox: true, margin: 4 }); };
const arrow = (x1, y1, x2, y2) => s.addShape(pres.shapes.LINE, { x: x1, y: y1, w: x2 - x1, h: y2 - y1, line: { color: "C9BEDA", width: 1.5, endArrowType: "triangle" }, flipV: y2 < y1 });
box(0.6, 1.7, 2.6, 0.9, "Wrist RealSense", "6FA8DC"); box(0.6, 3.0, 2.6, 0.9, "Tripod RealSense", "6FA8DC"); box(0.6, 4.3, 2.6, 0.9, "xArm6 + gripper", "6FA8DC");
box(4.0, 1.7, 2.6, 0.9, "Vision + survey", "8E5CC4"); box(4.0, 3.0, 2.6, 0.9, "Claude vision + voice", "8E5CC4"); box(4.0, 4.3, 2.6, 0.9, "Viam motion service", "8E5CC4");
box(7.4, 2.35, 2.6, 0.9, "Session: safety, retries, hand guard", "FF8C00"); box(7.4, 3.65, 2.6, 0.9, "Stockfish + planner", "FF8C00");
box(10.6, 1.7, 2.2, 0.9, "Web console", "4CC9A0"); box(10.6, 3.0, 2.2, 0.9, "Episodes + QC", "4CC9A0"); box(10.6, 4.3, 2.2, 0.9, "LeRobot · Atlas", "4CC9A0");
arrow(3.2, 2.15, 4.0, 2.15); arrow(3.2, 3.45, 4.0, 3.45); arrow(6.6, 2.15, 7.4, 2.8); arrow(6.6, 3.45, 7.4, 2.8); arrow(7.4, 4.1, 6.6, 4.75); arrow(4.0, 4.75, 3.2, 4.75);
arrow(10.0, 2.8, 10.6, 2.15); arrow(10.0, 2.8, 10.6, 3.45); arrow(11.7, 3.9, 11.7, 4.3);
s.addText("All Python, one repo, 130 tests. Every module is reusable on any Viam arm.", { x: 0.6, y: 5.7, w: 12.1, h: 0.5, fontSize: 16, color: C.accent4, align: "center", isTextBox: true, margin: 0 });
notes(s, "Sensors on the left, intelligence in the middle, the data product on the right.");
// 8 data
s = pres.addSlide({ masterName: "NIGHT", sectionTitle: "Architecture" }); title(s, "Every move becomes audited data");
card(s, 0.6, 1.7, 5.9, 4.5, "Episode + verdict + evidence", "Each pick-and-place is an episode: usable, suspicious, invalid or failed-but-valid, with the evidence. Yesterday it caught the gripper reporting “got it” while the camera showed the pawn never moved.", C.accent1);
card(s, 6.85, 1.7, 5.9, 4.5, "Ready for robot learning", "Exported to LeRobot format, synced to MongoDB Atlas, searchable across stations. Third-party LeRobot datasets from Hugging Face go through the same audit.", C.accent3);
notes(s, "This is the Amperes business: usable-data audits for robot fleets. The chess robot is its proof.");
// 9 results
sec("Results");
s = pres.addSlide({ masterName: "NIGHT", sectionTitle: "Results" }); title(s, "Measured on the event arm");
stat(s, 0.6, 1.8, "10/10", "robot moves confirmed by camera in game 1", C.accent1);
stat(s, 4.75, 1.8, "0.34 mm", "max error over 186 commanded poses", C.accent6);
stat(s, 8.9, 1.8, "130", "automated tests passing", C.accent3);
stat(s, 0.6, 4.2, "~3 mm", "board calibration offset, measured and corrected", C.accent4);
stat(s, 4.75, 4.2, "6 views", "board survey in under a minute", C.accent2);
stat(s, 8.9, 4.2, "0 contact", "in 600 simulated board resets", C.accent5);
notes(s, "Numbers, not adjectives. Everything here was measured on sfhack1.");
// 10 close
sec("Close");
s = pres.addSlide({ masterName: "NIGHT", sectionTitle: "Close" }); title(s, "Built to be reused");
s.addText([{ text: "chess_lifecycle: the clearance-aware planner, pure Python", options: { bullet: true, breakLine: true } },
  { text: "chess_survey: multi-view scan with a fitted camera correction", options: { bullet: true, breakLine: true } },
  { text: "chess_vision: grid lock and human-move detection", options: { bullet: true, breakLine: true } },
  { text: "Amperes audit: QC and LeRobot export for any robot", options: { bullet: true } }],
  { x: 0.6, y: 1.7, w: 7.2, h: 3.4, fontSize: 18, color: C.background1, paraSpaceAfter: 12, valign: "top", isTextBox: true });
s.addImage({ path: "../assets/hero.png", x: 8.1, y: 1.8, w: 4.6, h: 2.18, objectName: "hero", altText: "Haunted Chess banner" });
s.addText("Play it. Fork it. Haunt it. 🎃", { x: 0.6, y: 5.3, w: 12.1, h: 0.8, fontSize: 32, bold: true, color: C.accent1, fontFace: "Cambria", isTextBox: true, margin: 0 });
s.addText("github.com/vnmoorthy/amperes-haunted-chess", { x: 0.6, y: 6.1, w: 12.1, h: 0.4, fontSize: 16, color: C.background2, isTextBox: true, margin: 0 });
notes(s, "Close with modularity and the repo link. Invite the judge to play a move.");
(async () => { await pres.writeFile({ fileName: "Amperes_Haunted_Chess.pptx" }); await applyTheme("Amperes_Haunted_Chess.pptx", THEME); console.log("written"); })();
