// Amperes Failure Lab deck (10 slides, 16:9) in the Amperes console design system.
// Build (from deck/):
//   python3 make_brand.py                        # brand/bg.png, brand/wordmark.png, brand/logo.png
//   SKILL=<pptx skill dir> node build.js         # SKILL is optional; it applies the theme colours
//   python3 postprocess.py Amperes_Failure_Lab.pptx   # fade transition + fade-in entrances on every slide
//   soffice --headless --convert-to pdf Amperes_Failure_Lab.pptx && pdftoppm -png -r 80 Amperes_Failure_Lab.pdf previews/slide
//   python3 contact_sheet.py && cp Amperes_Failure_Lab.pdf ../docs/assets/
// Images: ../assets/screenshots/*.png (console; slides 3 and 8 prefer the *-crop.png versions and fall back to the
// full screenshots), ../assets/table_camera.jpg, ../assets/robot_view.jpg, brand/*.png.
const fs = require("fs");
const path = require("path");
const pptxgen = require("pptxgenjs");

const SKILL = process.env.SKILL || "";
let applyTheme = null;
try { if (SKILL) ({ applyTheme } = require(path.join(SKILL, "scripts/apply_theme.js"))); } catch (e) { applyTheme = null; }

const ROOT = path.resolve(__dirname, "..");
const OUT = path.join(__dirname, "Amperes_Failure_Lab.pptx");
const A = (p) => path.join(ROOT, p);

// Tokens from amperes/ui/index.html (dark theme). Colour is used only for status.
const K = { bg: "000000", panel: "0A0A0B", panel2: "111214", ink: "E6E7EA", ink2: "CDCED1", muted: "7D7F84", dim: "5A5C61",
  line: "1F2023", line2: "2A2B2F", line3: "3A3B40", bad: "EF8A80", warn: "D9B26A", ok: "86C9A0", silver: "F5F5F6" };
const FONT = "Arial";
const MONO = "Menlo", CODE = "9AA0A8"; // only where code is shown (env vars, paths, commands)
const THEME = { name: "Amperes", headFontFace: FONT, bodyFontFace: FONT,
  colors: { dk1: "000000", lt1: "E6E7EA", dk2: "0A0A0B", lt2: "CDCED1", accent1: "F5F5F6", accent2: "9AA0A8", accent3: "86C9A0",
    accent4: "D9B26A", accent5: "8FB3D9", accent6: "C99AA6", hlink: "E6E7EA", folHlink: "CDCED1" } };

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE"; // 13.333 x 7.5 in
pres.theme = { headFontFace: FONT, bodyFontFace: FONT };
pres.title = "Amperes Failure Lab";
pres.subject = "Failure analysis for robot manipulation, proven live on a Viam xArm6 that plays chess";
pres.author = "vnmoorthy";
pres.company = "Amperes";
const W = 13.333, X0 = 0.75, X1 = 12.583;

// ---------- helpers ----------
function dims(p) {
  const b = fs.readFileSync(p);
  if (b.readUInt32BE(0) === 0x89504e47) return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) };
  let i = 2;
  while (i + 9 < b.length) {
    if (b[i] !== 0xff) { i++; continue; }
    const m = b[i + 1];
    if (m === 0xd8 || m === 0x01 || (m >= 0xd0 && m <= 0xd7)) { i += 2; continue; }
    const len = b.readUInt16BE(i + 2);
    if (m >= 0xc0 && m <= 0xcf && ![0xc4, 0xc8, 0xcc].includes(m)) return { w: b.readUInt16BE(i + 7), h: b.readUInt16BE(i + 5) };
    i += 2 + len;
  }
  throw new Error("cannot read image size: " + p);
}
const T = (s, text, o) => s.addText(text, Object.assign({ fontFace: FONT, isTextBox: true, margin: 0, valign: "top", color: K.ink2, fontSize: 14 }, o));
const kicker = (s, text, o = {}) => T(s, text.toUpperCase(), Object.assign({ x: X0, y: 0.55, w: 9, h: 0.3, fontSize: 11, color: K.muted, charSpacing: 3 }, o));
const title = (s, text) => T(s, text, { x: X0, y: 0.86, w: X1 - X0, h: 0.72, fontSize: 34, color: K.ink, objectName: "title" });
const box = (s, x, y, w, h, o = {}) => s.addShape(pres.shapes.ROUNDED_RECTANGLE, Object.assign({ x, y, w, h, rectRadius: 0.14,
  fill: { color: K.panel }, line: { color: K.line2, width: 0.75 } }, o));
const dot = (s, x, y, d, color, name) => s.addShape(pres.shapes.OVAL, { x, y, w: d, h: d, fill: { color }, line: { color, width: 0 }, objectName: name });
const hline = (s, x, y, w, color = K.line, name) => s.addShape(pres.shapes.LINE, { x, y, w, h: 0, line: { color, width: 0.75 }, objectName: name });

// The first of these repo-relative images that exists, else the last (framed() then draws a placeholder).
const pick = (...files) => files.find((f) => fs.existsSync(A(f))) || files[files.length - 1];

// An image contained in a box, in a hairline card (like the console's img style). Returns the card rect.
function framed(s, file, bx, by, bw, bh, o = {}) {
  const pad = o.pad ?? 0.1, name = o.name || path.basename(file);
  const full = A(file);
  if (!fs.existsSync(full)) {
    box(s, bx, by, bw, bh, { objectName: (o.group || "") + " frame " + name, line: { color: K.line2, width: 0.75, dashType: "dash" } });
    T(s, "Screenshot pending: " + file, { x: bx + 0.3, y: by + bh / 2 - 0.2, w: bw - 0.6, h: 0.4, fontSize: 12, color: K.muted, align: "center" });
    return { x: bx, y: by, w: bw, h: bh };
  }
  const d = dims(full), ar = d.w / d.h;
  let iw = bw - 2 * pad, ih = iw / ar;
  if (ih > bh - 2 * pad) { ih = bh - 2 * pad; iw = ih * ar; }
  const cw = iw + 2 * pad, ch = ih + 2 * pad;
  const cx = o.align === "left" ? bx : o.align === "right" ? bx + bw - cw : bx + (bw - cw) / 2;
  const cy = o.valign === "top" ? by : o.valign === "bottom" ? by + bh - ch : by + (bh - ch) / 2;
  const g = o.group ? o.group + " " : "";
  box(s, cx, cy, cw, ch, { rectRadius: 0.12, objectName: g + "frame " + name });
  s.addImage({ path: full, x: cx + pad, y: cy + pad, w: iw, h: ih, objectName: g + "image " + name, altText: o.alt || name });
  return { x: cx, y: cy, w: cw, h: ch };
}

// ---------- layouts ----------
pres.defineSlideMaster({
  title: "AMP_TITLE",
  background: { path: path.join(__dirname, "brand/bg.png") },
});
pres.defineSlideMaster({
  title: "AMP_CONTENT",
  background: { path: path.join(__dirname, "brand/bg.png") },
  objects: [
    { image: { path: path.join(__dirname, "brand/logo.png"), x: X0, y: 6.93, w: 0.26, h: 0.26 } },
    { text: { text: "AMPERES   FAILURE LAB", options: { x: X0 + 0.38, y: 6.93, w: 5, h: 0.26, fontFace: FONT, fontSize: 9, color: K.muted, charSpacing: 3, valign: "middle", margin: 0 } } },
  ],
  slideNumber: { x: X1 - 0.6, y: 6.93, w: 0.6, h: 0.26, fontFace: FONT, fontSize: 9, color: K.muted, align: "right", valign: "middle", margin: 0 },
});
const sections = new Set();
const section = (t) => { if (!sections.has(t)) { pres.addSection({ title: t }); sections.add(t); } return t; };
const content = (t) => pres.addSlide({ masterName: "AMP_CONTENT", sectionTitle: section(t) });

// ---------- 1. Title ----------
section("Opening");
let s = pres.addSlide({ masterName: "AMP_TITLE", sectionTitle: "Opening" });
{
  s.addImage({ path: path.join(__dirname, "brand/logo.png"), x: X0, y: 1.5, w: 0.74, h: 0.74, objectName: "a1 logo", altText: "Amperes logo" });
  const wm = dims(path.join(__dirname, "brand/wordmark.png")), ww = 5.7;
  s.addImage({ path: path.join(__dirname, "brand/wordmark.png"), x: X0, y: 2.62, w: ww, h: ww * wm.h / wm.w, objectName: "a1 wordmark", altText: "AMPERES" });
  T(s, "FAILURE LAB", { x: X0 + 0.02, y: 3.42, w: 5.6, h: 0.36, fontSize: 15, color: K.ink2, charSpacing: 7, objectName: "a1 product" });
  T(s, "Every robot failure, caught, explained and fixed.", { x: X0, y: 4.12, w: 6.1, h: 1.05, fontSize: 28, color: K.ink, objectName: "a2 tagline" });
  dot(s, X0 + 0.02, 5.6, 0.15, K.ok, "a3 live dot");
  T(s, "Live: a Viam xArm6 that plays chess", { x: X0 + 0.32, y: 5.5, w: 5.6, h: 0.36, fontSize: 17, color: K.ink2, valign: "middle", objectName: "a3 live" });
  const r = framed(s, "assets/table_camera.jpg", 7.3, 1.45, X1 - 7.3, 4.25, { align: "right", name: "table camera photo", group: "a2",
    alt: "The xArm6 and the chessboard on the event table, seen by the table camera" });
  T(s, "The event arm and board, from the table camera", { x: r.x, y: r.y + r.h + 0.14, w: r.w, h: 0.3, fontSize: 11, color: K.muted, objectName: "a2 caption" });
}
s.addNotes("This is Amperes Failure Lab. It records every attempt a robot makes, names each failure, traces it to a root cause and proves the fix. To show it on real hardware, over these two days we built a chess-playing robot on a Viam xArm6, and every move it makes is a trial the lab can triage.");

// ---------- 2. Robots fail quietly ----------
s = content("Opening");
kicker(s, "The problem");
title(s, "Robots fail quietly");
{
  const rows = [
    ["01", "Small failures, all day", "A missed grasp, a slip, a collision stop, a move logged that never happened."],
    ["02", "Evidence in four places", "Camera, gripper, arm controller and the operator's memory. Nobody lines them up."],
    ["03", "Found too late", "In the dataset, in the demo, or in front of the customer."],
  ];
  rows.forEach(([n, h, b], i) => {
    const y = 2.0 + i * 1.52, g = "a" + (i + 1);
    T(s, n, { x: X0, y, w: 0.6, h: 0.3, fontSize: 12, color: K.muted, charSpacing: 2, objectName: g + " num " + n });
    T(s, h, { x: X0 + 0.62, y: y - 0.05, w: 5.1, h: 0.42, fontSize: 21, color: K.ink, objectName: g + " head " + n });
    T(s, b, { x: X0 + 0.62, y: y + 0.45, w: 5.1, h: 0.75, fontSize: 15, color: K.ink2, objectName: g + " body " + n });
  });
  const px = 6.85, py = 2.0, pw = X1 - 6.85, ph = 4.4;
  box(s, px, py, pw, ph, { objectName: "a4 case panel" });
  T(s, "A REAL CASE FROM THIS ARM, 29 OCT", { x: px + 0.35, y: py + 0.3, w: pw - 0.7, h: 0.3, fontSize: 11, color: K.muted, charSpacing: 3, objectName: "a4 case kicker" });
  const sig = [["Arm", "Motion complete", K.ok], ["Gripper sensor", "Holding a piece", K.ok], ["Wrist camera", "Pawn still on its square", K.bad]];
  sig.forEach(([src, val, col], i) => {
    const y = py + 0.85 + i * 0.66;
    hline(s, px + 0.35, y, pw - 0.7, K.line2, "a4 rule " + i);
    T(s, src, { x: px + 0.35, y: y + 0.16, w: 1.9, h: 0.36, fontSize: 13, color: K.muted, valign: "middle", objectName: "a4 src " + i });
    T(s, val, { x: px + 2.3, y: y + 0.14, w: 2.75, h: 0.4, fontSize: 16, color: K.ink, valign: "middle", objectName: "a4 val " + i });
    dot(s, px + pw - 0.55, y + 0.27, 0.16, col, "a4 status " + i);
  });
  hline(s, px + 0.35, py + 0.85 + 3 * 0.66, pw - 0.7, K.line2, "a4 rule end");
  T(s, [
    { text: "Lab verdict: ", options: { color: K.muted } },
    { text: "PIECE_NOT_MOVED", options: { color: K.bad, bold: true } },
  ], { x: px + 0.35, y: py + 3.0, w: pw - 0.7, h: 0.36, fontSize: 15, valign: "middle", objectName: "a5 verdict" });
  T(s, "Each signal looks fine alone. Side by side they disagree: the move never happened.", { x: px + 0.35, y: py + 3.48, w: pw - 0.7, h: 0.7, fontSize: 14, color: K.ink2, objectName: "a5 lesson" });
}
s.addNotes("Robots rarely fail loudly. Yesterday the gripper said it was holding a pawn while the wrist camera showed the pawn had not moved. Each signal looked fine on its own; only side by side do you see the failure, and without that a move that never happened goes into the record.");

// ---------- 3. What the lab does ----------
s = content("Product");
kicker(s, "The product");
title(s, "Amperes Failure Lab, from failure to fix");
{
  const steps = [
    ["Record", "Images, depth, poses, gripper reading, outcome."],
    ["Label", "Missed grasp, slip, collision stop, refused motion."],
    ["Triage", "Recurring failures grouped, traced to a root cause."],
    ["Verify", "Replay and regression show the fix really helped."],
    ["Export", "Clean, labelled episodes as LeRobot datasets."],
  ];
  const y0 = 1.98, pitch = 0.76, cx = X0 + 0.21;
  s.addShape(pres.shapes.LINE, { x: cx, y: y0 + 0.21, w: 0, h: pitch * 4, line: { color: K.line2, width: 1 }, objectName: "a1 connector" });
  steps.forEach(([h, b], i) => {
    const y = y0 + i * pitch, g = "a" + (i + 1);
    s.addShape(pres.shapes.OVAL, { x: X0, y, w: 0.42, h: 0.42, fill: { color: K.panel2 }, line: { color: K.line3, width: 0.75 }, objectName: g + " step dot " + h });
    T(s, String(i + 1), { x: X0, y, w: 0.42, h: 0.42, fontSize: 13, color: K.ink, align: "center", valign: "middle", objectName: g + " step num " + h });
    T(s, h, { x: X0 + 0.65, y: y - 0.02, w: 4.9, h: 0.34, fontSize: 18, color: K.ink, objectName: g + " step head " + h });
    T(s, b, { x: X0 + 0.65, y: y + 0.34, w: 5.2, h: 0.32, fontSize: 14, color: K.ink2, objectName: g + " step body " + h });
  });
  // The lab's own counts for the chess robot (amperes_data/amperes.db, counted 2:39 PM 30 Oct).
  const stats = [["193", "chess trials since 29 Oct", K.ink, 1.4], ["142", "succeeded", K.ok, 1.1],
    ["38", "failures labelled", K.bad, 1.3], ["13", "invalid, kept out of training data", K.warn, 1.65]];
  let sx = X0;
  stats.forEach(([n, l, c, w]) => {
    T(s, n, { x: sx, y: 5.88, w, h: 0.4, fontSize: 24, color: c, objectName: "a7 stat " + n });
    T(s, l, { x: sx, y: 6.28, w: w - 0.1, h: 0.36, fontSize: 11, color: K.ink2, objectName: "a7 stat label " + n });
    sx += w + 0.1;
  });
  const inc = pick("assets/screenshots/failure-lab-incidents-crop.png", "assets/screenshots/failure-lab-incidents.png");
  const r = framed(s, inc, 6.85, 2.0, X1 - 6.85, 3.7, { align: "left", valign: "top", name: "incidents screenshot", group: "a6",
    alt: "Failure Lab console, Incidents tab: an incident with its measured facts and hypotheses, dark mode" });
  T(s, "Incidents tab: a critical finding, measured facts kept apart from hypotheses", { x: r.x, y: r.y + r.h + 0.12, w: X1 - r.x, h: 0.26, fontSize: 11, color: K.muted, objectName: "a6 caption" });
  T(s, [{ text: "Local-first. ", options: { color: K.ink, bold: true } },
    { text: "The SQLite store stays on this machine; only opt-in Atlas sync and Claude piece recognition send data out.", options: { color: K.ink2 } }],
    { x: r.x, y: r.y + r.h + 0.46, w: X1 - r.x, h: 0.46, fontSize: 13, objectName: "a6 local" });
}
s.addNotes("The lab closes that loop. It records each attempt with images, depth, poses and the gripper reading, labels the failure, groups recurring ones into incidents with a root cause, checks the fix with replay and regression, and exports a clean dataset. Since the first chess game on 29 Oct it has recorded 193 chess trials in 36 sessions: 142 succeeded; 38 failures are labelled, 30 missed grasps, 7 slips and 1 piece not moved; and 13 invalid trials, 8 safety stops and 5 infrastructure errors, are kept out of training data. Two trials were flagged critical because the recorded outcome contradicted independent verification. It is local-first: the SQLite store stays on this machine, and only the opt-in Atlas sync and Claude piece recognition send data out.");

// ---------- 4. Why chess ----------
s = content("Product");
kicker(s, "Why chess");
title(s, "Chess is an honest test for pick-and-place");
{
  const tiles = [
    ["32 pieces, 6 shapes", "Small parts that look alike from straight above."],
    ["Millimetre placement", "A few millimetres off and the gripper touches a neighbour."],
    ["A person at the board", "Human hands work right next to the arm, every game."],
    ["Every action checkable", "The rules of chess and two cameras say whether each move really happened."],
  ];
  const tw = 3.1, th = 1.72, gx = 0.3, gy = 0.3;
  tiles.forEach(([h, b], i) => {
    const x = X0 + (i % 2) * (tw + gx), y = 1.98 + Math.floor(i / 2) * (th + gy), g = "a" + (i + 1);
    box(s, x, y, tw, th, { objectName: g + " tile " + h });
    T(s, h, { x: x + 0.28, y: y + 0.26, w: tw - 0.5, h: 0.4, fontSize: 18, color: K.ink, objectName: g + " tile head" });
    T(s, b, { x: x + 0.28, y: y + 0.74, w: tw - 0.5, h: 0.8, fontSize: 14, color: K.ink2, objectName: g + " tile body" });
  });
  T(s, "If the lab can debug this in two days, it can debug your pick-and-place cell.", { x: X0, y: 6.02, w: 6.6, h: 0.72, fontSize: 19, color: K.ink, objectName: "a6 statement" });
  const r = framed(s, "assets/robot_view.jpg", 7.45, 1.98, X1 - 7.45, 3.74, { align: "right", valign: "top", name: "overhead view", group: "a5",
    alt: "The robot's overhead view of the board with every square labelled" });
  T(s, "The robot's own overhead view: every square located and labelled", { x: r.x, y: r.y + r.h + 0.14, w: r.w, h: 0.3, fontSize: 11, color: K.muted, objectName: "a5 caption" });
}
s.addNotes("Why chess? It is a hard, honest test for a pick-and-place robot: 32 small pieces in six shapes, millimetre placement, and a person's hands right next to the arm. And every single action can be checked against the rules and the cameras. If the lab can debug this in two days, it can debug your cell.");

// ---------- 5. The robot + architecture ----------
s = content("Product");
kicker(s, "The robot");
title(s, "How the robot and the lab fit together");
{
  const cw = 2.58, gap = 0.5, xs = [0, 1, 2, 3].map((i) => X0 + i * (cw + gap)), top = 2.3, H = 3.6;
  const heads = ["Hardware", "Viam", "Decisions", "Failure Lab"];
  heads.forEach((h, i) => T(s, h.toUpperCase(), { x: xs[i], y: 1.9, w: cw, h: 0.28, fontSize: 11, color: i === 3 ? K.ink : K.muted, charSpacing: 3, objectName: "a" + (i + 1) + " col " + h }));
  const col = (i, items, hi) => {
    const n = items.length, g = 0.22, bh = (H - g * (n - 1)) / n;
    items.forEach(([h, b], j) => {
      const y = top + j * (bh + g), grp = "a" + (i + 1);
      box(s, xs[i], y, cw, bh, { rectRadius: 0.1, fill: { color: hi ? K.panel2 : K.panel }, line: { color: hi ? K.ink2 : K.line2, width: hi ? 1 : 0.75 }, objectName: grp + " box " + h });
      T(s, h, { x: xs[i] + 0.18, y: y + 0.13, w: cw - 0.3, h: 0.3, fontSize: 14, color: K.ink, bold: true, objectName: grp + " box head" });
      T(s, b, { x: xs[i] + 0.18, y: y + 0.44, w: cw - 0.3, h: bh - 0.5, fontSize: 11, color: K.ink2, objectName: grp + " box body" });
    });
  };
  col(0, [["UFactory xArm6", "Six-axis arm"], ["G2 gripper", "Reports its finger position"], ["RealSense, wrist", "Colour + depth over the board"], ["RealSense, tripod", "Second view of the table"]]);
  col(1, [["Machine sfhack1", "Arm, gripper and cameras picked by name"], ["Motion service", "Routed moves, not straight lines"], ["Modelled obstacles", "Table, walls and ceiling in the plan"]]);
  col(2, [["Stockfish", "Picks every move"], ["Claude vision", "Names each piece"], ["Depth heights", "Check every name"], ["Chess session", "Verifies, retries, holds for hands"]]);
  col(3, [["Every move a trial", "Images, depth, poses, gripper reading, outcome"], ["Local store", "SQLite under amperes_data/"], ["Console", "Label, triage, replay, export"]], true);
  const midY = top + H / 2;
  [[0, true], [1, true], [2, false]].forEach(([i, both]) => s.addShape(pres.shapes.LINE, { x: xs[i] + cw + 0.08, y: midY, w: gap - 0.16, h: 0,
    line: { color: K.ink2, width: 1.25, endArrowType: "triangle", beginArrowType: both ? "triangle" : undefined }, objectName: "a" + (i + 2) + " arrow " + i }));
  T(s, "Commands and telemetry flow through Viam; every result lands in the lab with its evidence.", { x: X0, y: 6.15, w: X1 - X0, h: 0.4, fontSize: 14, color: K.ink2, objectName: "a5 flow note" });
}
s.addNotes("Here is the rig: an xArm6 with a G2 gripper, one RealSense on the wrist and one on a tripod, all on Viam. Viam's motion service routes moves around modelled obstacles. Stockfish picks moves, Claude vision names pieces, and depth-measured heights check every name. Every move is recorded as a trial, and the trials land in the Failure Lab when the game ends.");

// ---------- 6. Failures caught on the real arm ----------
s = content("Proof");
kicker(s, "The proof, 29\u201330\u00A0Oct");
title(s, "What the lab caught on the real arm");
{
  const sx = X0, sy = 1.95, sw = 3.5, sh = 4.7;
  box(s, sx, sy, sw, sh, { objectName: "a1 stat panel" });
  T(s, "CAMERA BIAS", { x: sx + 0.32, y: sy + 0.3, w: sw - 0.6, h: 0.28, fontSize: 11, color: K.muted, charSpacing: 3, objectName: "a1 stat kicker" });
  T(s, "~15\u00A0mm", { x: sx + 0.32, y: sy + 0.66, w: sw - 0.6, h: 0.55, fontSize: 28, color: K.muted, objectName: "a1 stat before" });
  T(s, "before: grasping air beside pieces", { x: sx + 0.32, y: sy + 1.22, w: sw - 0.6, h: 0.3, fontSize: 12, color: K.muted, objectName: "a1 stat before label" });
  T(s, "4.5\u00A0mm", { x: sx + 0.32, y: sy + 1.72, w: sw - 0.5, h: 0.95, fontSize: 60, color: K.ink, objectName: "a2 stat big" });
  T(s, "mean survey error after a fitted correction", { x: sx + 0.32, y: sy + 2.7, w: sw - 0.6, h: 0.5, fontSize: 13, color: K.ink2, objectName: "a2 stat big label" });
  T(s, "14 / 14", { x: sx + 0.32, y: sy + 3.32, w: sw - 0.5, h: 0.75, fontSize: 44, color: K.ok, objectName: "a3 stat run" });
  T(s, "pick-and-places in a row", { x: sx + 0.32, y: sy + 4.1, w: sw - 0.6, h: 0.3, fontSize: 13, color: K.ink2, objectName: "a3 stat run label" });

  const lx = 4.65, fx = 4.88, fw = 2.95, xx = 8.18, xw = X1 - 8.18 - 0.02, y0 = 1.95;
  T(s, "FAILURE CAUGHT", { x: fx, y: y0, w: fw, h: 0.26, fontSize: 10, color: K.muted, charSpacing: 3, objectName: "a4 head fail" });
  T(s, "FIX", { x: xx, y: y0, w: xw, h: 0.26, fontSize: 10, color: K.muted, charSpacing: 3, objectName: "a4 head fix" });
  dot(s, lx + 0.02, y0 + 0.075, 0.11, K.bad, "a4 head fail dot");
  dot(s, xx - 0.23, y0 + 0.075, 0.11, K.ok, "a4 head fix dot");
  const rows = [
    ["Grasping air beside pieces", "Camera bias measured on pieces the arm placed itself; fitted correction"],
    ["Retried grips pressed pieces into the board", "Place height follows the real grip; last 60\u00A0mm at 10\u00A0deg/s, transit 75\u00A0deg/s"],
    ["Moves recorded that never happened", "Empty-grip check, instant retries at small offsets, camera must confirm"],
    ["Collision stops on long, fast swings", "Big wrist turns split into two half-turns; motion routed in shorter moves"],
    ["Board-reset planner looping", "Loop guard: remembered states, forced follow-up moves, an operation limit"],
    ["Typed move ignored while the camera watched", "The watch now ends as soon as a move is entered"],
    ["Pieces mislabelled (a rook read as a pawn)", "Claude vision, height check, confidence gate, corrections that follow the piece"],
  ];
  const rh = 0.63, ry0 = y0 + 0.36;
  rows.forEach(([f, x], i) => {
    const y = ry0 + i * rh, g = "a" + (4 + Math.floor(i / 2));
    hline(s, lx, y, X1 - lx, K.line2, g + " row rule " + i);
    T(s, f, { x: fx, y: y + 0.09, w: fw, h: rh - 0.12, fontSize: 14, color: K.ink, objectName: g + " fail " + i });
    T(s, x, { x: xx, y: y + 0.09, w: xw, h: rh - 0.12, fontSize: 14, color: K.ink2, objectName: g + " fix " + i });
  });
  hline(s, lx, ry0 + rows.length * rh, X1 - lx, K.line2, "a7 row rule end");
}
s.addNotes("This is the heart of it: seven real failures the lab surfaced on the arm over these two days, each with its fix. The biggest: the arm was grasping air beside pieces. Measuring pieces the arm had placed itself showed a camera bias of about 15 millimetres; a fitted correction brought it to 4.5, and the arm then made 14 pick-and-places in a row.");

// ---------- 7. Safety ----------
s = content("Proof");
kicker(s, "Safety");
title(s, "Safe around people and pieces");
{
  const px = X0, py = 1.95, pw = 5.45, ph = 4.7;
  box(s, px, py, pw, ph, { objectName: "a1 schematic panel" });
  const bx0 = px + 0.35, bx1 = px + pw - 0.35, boardY = py + ph - 0.55, mm = 0.0135;
  // transit label
  T(s, "Transit: 75\u00A0deg/s", { x: bx0, y: py + 0.3, w: 2.5, h: 0.28, fontSize: 11, color: K.muted, objectName: "a1 transit label" });
  // gripper and wrist camera
  const gx = px + pw / 2 - 0.3;
  s.addShape(pres.shapes.RECTANGLE, { x: gx - 0.05, y: py + 0.55, w: 0.7, h: 0.18, fill: { color: K.line3 }, line: { color: K.line3, width: 0 }, objectName: "a1 flange" });
  s.addShape(pres.shapes.RECTANGLE, { x: gx, y: py + 0.73, w: 0.6, h: 0.5, fill: { color: K.line2 }, line: { color: K.line3, width: 0.75 }, objectName: "a1 gripper body" });
  s.addShape(pres.shapes.RECTANGLE, { x: gx + 0.04, y: py + 1.23, w: 0.1, h: 0.42, fill: { color: K.dim }, line: { color: K.dim, width: 0 }, objectName: "a1 finger l" });
  s.addShape(pres.shapes.RECTANGLE, { x: gx + 0.46, y: py + 1.23, w: 0.1, h: 0.42, fill: { color: K.dim }, line: { color: K.dim, width: 0 }, objectName: "a1 finger r" });
  s.addShape(pres.shapes.RECTANGLE, { x: gx + 0.66, y: py + 0.82, w: 0.3, h: 0.2, fill: { color: K.panel2 }, line: { color: K.ink2, width: 0.75 }, objectName: "a1 wrist cam" });
  T(s, "wrist depth camera", { x: gx + 1.05, y: py + 0.8, w: 1.6, h: 0.26, fontSize: 10, color: K.ink2, valign: "middle", objectName: "a1 wrist cam label" });
  const handTop = boardY - 110 * mm - 0.45;
  s.addShape(pres.shapes.LINE, { x: gx + 0.3, y: py + 1.75, w: 0, h: handTop - 0.1 - (py + 1.75), line: { color: K.muted, width: 1, dashType: "dash", endArrowType: "triangle" }, objectName: "a1 descent" });
  // slow zone (60\u00A0mm) and hand guard (110\u00A0mm), to scale
  const slowTop = boardY - 60 * mm, guardY = boardY - 110 * mm;
  s.addShape(pres.shapes.RECTANGLE, { x: bx0, y: slowTop, w: bx1 - bx0, h: boardY - slowTop, fill: { color: K.panel2 }, line: { color: K.line2, width: 0.5 }, objectName: "a2 slow zone" });
  T(s, "Last 60\u00A0mm: 10\u00A0deg/s", { x: bx0 + 0.12, y: slowTop + 0.08, w: 2.4, h: 0.26, fontSize: 11, color: K.ink2, objectName: "a2 slow label" });
  s.addShape(pres.shapes.LINE, { x: bx0, y: guardY, w: bx1 - bx0, h: 0, line: { color: K.warn, width: 1.25, dashType: "dash" }, objectName: "a3 guard line" });
  T(s, "Hand guard: 110\u00A0mm", { x: bx1 - 2.0, y: guardY - 0.32, w: 2.0, h: 0.26, fontSize: 11, color: K.warn, align: "right", objectName: "a3 guard label" });
  // a hand under the gripper, taller than the guard line: the arm holds
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: gx, y: handTop, w: 0.6, h: boardY - handTop - 0.02, rectRadius: 0.2, fill: { color: K.bg, transparency: 100 }, line: { color: K.warn, width: 1, dashType: "sysDot" }, objectName: "a3 hand" });
  T(s, "hand", { x: gx, y: guardY + 0.25, w: 0.6, h: 0.26, fontSize: 10, color: K.warn, align: "center", objectName: "a3 hand label" });
  T(s, "arm holds", { x: gx + 0.72, y: handTop - 0.36, w: 1.3, h: 0.26, fontSize: 11, color: K.warn, objectName: "a3 holds label" });
  // board and pieces
  s.addShape(pres.shapes.RECTANGLE, { x: bx0, y: boardY, w: bx1 - bx0, h: 0.1, fill: { color: K.line3 }, line: { color: K.line3, width: 0 }, objectName: "a1 board" });
  [[3.25, 0.62], [3.75, 0.45], [4.25, 0.7], [4.75, 0.5]].forEach(([dx, hh], i) =>
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: px + dx, y: boardY - hh, w: 0.26, h: hh, rectRadius: 0.06, fill: { color: K.dim }, line: { color: K.dim, width: 0 }, objectName: "a1 piece " + i }));
  T(s, "Side view, to scale", { x: bx0, y: boardY + 0.17, w: 3, h: 0.24, fontSize: 10, color: K.muted, objectName: "a1 scale note" });

  const rows = [
    ["Hand guard", "The wrist depth camera looks under the gripper before each descent. Anything over 110\u00A0mm up: hold."],
    ["Safety envelope", "Every target checked against clearance margins; no orientation change above 100\u00A0degrees in one move."],
    ["Gentle near the pieces", "Transit at 75\u00A0deg/s; the last 60\u00A0mm above the board at 10\u00A0deg/s."],
    ["Telemetry watchdog", "If arm telemetry stops during motion, the arm stops."],
    ["Operator in the loop", "Nothing moves until the operator types the acknowledgement, e-stop in reach. STOP works mid-move."],
  ];
  // Even rows, each under a hairline (the slide 8 list style); the first rule lines up with the panel top.
  const rx = 6.65, rw = X1 - rx, ry = 2.05, rp = 0.94;
  rows.forEach(([h, b], i) => {
    const y = ry + i * rp, g = "a" + (4 + i);
    hline(s, rx, y - 0.1, rw, K.line2, g + " safety rule " + i);
    T(s, h, { x: rx, y, w: rw, h: 0.3, fontSize: 17, color: K.ink, objectName: g + " safety head " + i });
    T(s, b, { x: rx, y: y + 0.3, w: rw, h: 0.5, fontSize: 13.5, color: K.ink2, objectName: g + " safety body " + i });
  });
}
s.addNotes("Safety is built in. Before every descent the wrist depth camera checks under the gripper and holds for anything taller than 110 millimetres, like a hand. The last 60 millimetres run at 10\u00A0degrees per second, a watchdog stops the arm if telemetry drops, and nothing moves without the operator's acknowledgement with the e-stop in reach.");

// ---------- 8. Live demo ----------
s = content("Proof");
kicker(s, "Live demo");
title(s, "Robot vs robot, every move called out loud");
{
  const shot = pick("assets/screenshots/chess-console-crop.png", "assets/screenshots/chess-console.png");
  const r = framed(s, shot, X0, 1.95, 6.75, 4.75, { align: "left", valign: "top", name: "chess console screenshot", group: "a1",
    alt: "The chess console during a game, dark mode" });
  const rx = r.x + r.w + 0.45, rw = X1 - rx;
  T(s, "CALLED OUT AFTER EVERY MOVE", { x: rx, y: 1.95, w: rw, h: 0.28, fontSize: 10, color: K.muted, charSpacing: 3, objectName: "a2 call kicker" });
  T(s, "“White knight to F3, check.”", { x: rx, y: 2.28, w: rw, h: 0.5, fontSize: 22, italic: true, color: K.ink, objectName: "a2 call quote" });
  // Rehearsal at 2:33 PM, 30 Oct: robot vs robot from a mid-game position, 6 half-moves.
  T(s, "6 / 6", { x: rx, y: 2.98, w: 1.25, h: 0.55, fontSize: 30, color: K.ok, objectName: "a3 rehearsal stat" });
  T(s, "moves confirmed by the wrist camera and called out loud in the 2:33 PM rehearsal", { x: rx + 1.35, y: 3.02, w: rw - 1.35, h: 0.45,
    fontSize: 12, color: K.ink2, objectName: "a3 rehearsal label" });
  const pts = [
    ["Only real moves count", "Gripper and camera must agree; retries at once."],
    ["Every move is a trial", "Lands in the Failure Lab when the game ends."],
    ["Or play it yourself", "Move a piece and the wrist camera sees it."],
  ];
  pts.forEach(([h, b], i) => {
    const y = 3.92 + i * 0.88, g = "a" + (4 + i);
    hline(s, rx, y - 0.13, rw, K.line2, g + " demo rule " + i);
    T(s, h, { x: rx, y: y, w: rw, h: 0.3, fontSize: 15, color: K.ink, bold: true, objectName: g + " demo head " + i });
    T(s, b, { x: rx, y: y + 0.34, w: rw, h: 0.3, fontSize: 14, color: K.ink2, objectName: g + " demo body " + i });
  });
}
s.addNotes("Now the live demo. The robot plays both sides by itself, Stockfish picking each move, and calls every move out loud in plain words, like 'White knight to F3, check.' A move only counts when the gripper reading and the camera agree. Each move is logged as a trial on the chess console and lands in the Failure Lab when the game ends. We rehearsed this at 2:33 PM from a mid-game position: 6 half-moves, all 6 confirmed by the wrist camera and called out loud, and all 9 pick-and-places succeeded. You can also play it: move a piece and the wrist camera sees it, or type a move. Operator note: end the game only when the console shows 'your move' or 'game over'; ending it while the arm is moving stops it mid-move.");

// ---------- 9. Built to reuse ----------
s = content("Close");
kicker(s, "Reuse");
title(s, "Built to reuse on your robot");
{
  // Tests, beside the title (`.venv/bin/python -m pytest -q`).
  T(s, [{ text: "132", options: { fontSize: 36, color: K.ok } }, { text: "  tests passing", options: { fontSize: 14, color: K.ink2 } }],
    { x: X1 - 4, y: 0.86, w: 4, h: 0.72, align: "right", objectName: "tests passing" });
  const cards = [
    ["Any Viam robot", "Arm, gripper, motion and cameras are chosen by name, so any Viam arm, gripper and camera works.", "AMPERES_ARM  AMPERES_GRIPPER\nAMPERES_MOTION  AMPERES_CAMERAS"],
    ["Any recorded session", "The lab ingests recorded Viam sessions, JSONL and CSV packages, and LeRobot datasets.", "amperes/ingest\njsonl  csv_package  lerobot_v3"],
    ["Clean datasets out", "Labelled episodes export as LeRobot datasets, depth included. Failed attempts stay in, labelled.", "amperes/export_lerobot.py"],
  ];
  const gap = 0.3, cw = (X1 - X0 - 2 * gap) / 3, cy = 1.95, ch = 2.25;
  cards.forEach(([h, b, f], i) => {
    const x = X0 + i * (cw + gap), g = "a" + (i + 1);
    box(s, x, cy, cw, ch, { objectName: g + " reuse card " + i });
    T(s, h, { x: x + 0.3, y: cy + 0.28, w: cw - 0.6, h: 0.38, fontSize: 19, color: K.ink, objectName: g + " reuse head " + i });
    T(s, b, { x: x + 0.3, y: cy + 0.74, w: cw - 0.6, h: 0.8, fontSize: 14, color: K.ink2, objectName: g + " reuse body " + i });
    T(s, f, { x: x + 0.3, y: cy + 1.6, w: cw - 0.6, h: 0.42, fontSize: 11, color: CODE, fontFace: MONO, objectName: g + " reuse foot " + i });
  });
  // Bottom row on the same grid: fleet memory across columns 1-2, honest limits in column 3.
  const by = cy + ch + gap, bh = 6.65 - by;
  const aw = 2 * cw + gap;
  box(s, X0, by, aw, bh, { objectName: "a4 atlas card" });
  T(s, "Fleet memory on MongoDB Atlas", { x: X0 + 0.3, y: by + 0.28, w: aw - 0.6, h: 0.38, fontSize: 19, color: K.ink, objectName: "a4 atlas head" });
  T(s, "Opt-in. Atlas Vector Search finds the same failure across sessions: a missed grasp from 30 Oct matched 6 others with identical recorded evidence, one from the evening before. Signatures are deterministic, built from that evidence.",
    { x: X0 + 0.3, y: by + 0.74, w: aw - 0.6, h: 0.95, fontSize: 14, color: K.ink2, objectName: "a4 atlas body" });
  T(s, "python -m amperes.cli atlas sync <dataset_id>\npython -m amperes.cli atlas similar <episode_id> -k 6",
    { x: X0 + 0.3, y: by + 1.68, w: aw - 0.6, h: 0.4, fontSize: 11, color: CODE, fontFace: MONO, objectName: "a4 atlas foot" });
  const lx = X0 + 2 * (cw + gap), lw = cw;
  box(s, lx, by, lw, bh, { objectName: "a5 limits card" });
  T(s, "HONEST LIMITS", { x: lx + 0.3, y: by + 0.3, w: lw - 0.6, h: 0.26, fontSize: 10, color: K.warn, charSpacing: 3, objectName: "a5 limits kicker" });
  T(s, [
    { text: "Cannot stand a fallen piece back up.", options: { bullet: { indent: 14 }, breakLine: true } },
    { text: "Piece names from above sometimes need an operator fix.", options: { bullet: { indent: 14 }, breakLine: true } },
    { text: "Speed is capped by the arm's own collision detection.", options: { bullet: { indent: 14 } } },
  ], { x: lx + 0.3, y: by + 0.66, w: lw - 0.5, h: 1.42, fontSize: 13, color: K.ink2, paraSpaceAfter: 4, objectName: "a5 limits list" });
}
s.addNotes("Nothing underneath is chess-only. Components are chosen by name, so it works with any Viam arm, gripper and camera; the lab ingests recorded sessions and exports LeRobot datasets plus Markdown and JSON reports, and 132 tests pass. With MongoDB Atlas turned on, the lab becomes fleet memory: it syncs episode index rows, quality findings, incidents, events and one failure signature per episode, never the raw streams, and Atlas Vector Search finds the same failure in other sessions. All 39 robot sessions are synced. A missed grasp from a chess game on 30 Oct matched six missed grasps with identical recorded evidence in other sessions, one of them from the evening before. The signatures are deterministic vectors built from the recorded evidence, not a learned embedding. We are also clear about the limits: it cannot stand a fallen piece back up, and speed is capped by the arm's own collision detection.");

// ---------- 10. Close ----------
s = pres.addSlide({ masterName: "AMP_TITLE", sectionTitle: "Close" });
{
  const wm = dims(path.join(__dirname, "brand/wordmark.png")), ww = 6.6, wh = ww * wm.h / wm.w;
  s.addImage({ path: path.join(__dirname, "brand/logo.png"), x: (W - 0.8) / 2, y: 1.25, w: 0.8, h: 0.8, objectName: "a1 logo", altText: "Amperes logo" });
  s.addImage({ path: path.join(__dirname, "brand/wordmark.png"), x: (W - ww) / 2, y: 2.45, w: ww, h: wh, objectName: "a1 wordmark", altText: "AMPERES" });
  T(s, "FAILURE LAB", { x: 0, y: 3.3, w: W, h: 0.36, fontSize: 15, color: K.ink2, charSpacing: 7, align: "center", objectName: "a1 product" });
  T(s, "The lab is the product. The chess robot is the proof.", { x: 0, y: 3.95, w: W, h: 0.5, fontSize: 24, color: K.ink, align: "center", objectName: "a2 line" });
  const links = [["Code", "github.com/vnmoorthy/amperes-haunted-chess", "https://github.com/vnmoorthy/amperes-haunted-chess"],
    ["Landing page", "vnmoorthy.github.io/amperes-haunted-chess", "https://vnmoorthy.github.io/amperes-haunted-chess/"]];
  const lw = 5.0, lg = 0.4, lx0 = (W - 2 * lw - lg) / 2;
  links.forEach(([k, t, u], i) => {
    const x = lx0 + i * (lw + lg), y = 4.85;
    box(s, x, y, lw, 0.95, { rectRadius: 0.2, fill: { color: K.panel }, line: { color: K.line3, width: 0.75 }, objectName: "a3 link card " + i });
    T(s, k.toUpperCase(), { x: x + 0.3, y: y + 0.14, w: lw - 0.6, h: 0.26, fontSize: 10, color: K.muted, charSpacing: 3, objectName: "a3 link kicker " + i });
    T(s, [{ text: t, options: { hyperlink: { url: u, tooltip: u } } }], { x: x + 0.3, y: y + 0.45, w: lw - 0.6, h: 0.34, fontSize: 15, color: K.ink, objectName: "a3 link " + i });
  });
  // One centred line; the status dot is a character so it centres with the text.
  T(s, [{ text: "●  ", options: { color: K.ok } }, { text: "Live now: the arm playing both sides", options: { color: K.ink2 } }],
    { x: 0, y: 6.27, w: W, h: 0.32, fontSize: 14, align: "center", valign: "middle", objectName: "a4 live" });
}
s.addNotes("Amperes Failure Lab: every robot failure caught, explained and fixed. The code and the landing page are at these two links. Come play a move against the arm, or watch it play itself.");

(async () => {
  await pres.writeFile({ fileName: OUT });
  if (applyTheme) await applyTheme(OUT, THEME);
  console.log("written", OUT, applyTheme ? "(theme applied)" : "(theme not applied: set SKILL to the pptx skill directory)");
})();
