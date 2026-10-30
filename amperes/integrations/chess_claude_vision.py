"""Claude as the robot's second pair of eyes for chess pieces.

From straight above, a rook and a pawn (or a king and a queen) look alike: last night's height rules confused them.
Claude reads the pieces from photos the way a person does, combining the overhead wrist photo (with every square
labelled) and the table camera's side view, and says how sure it is about each one. It is used for whole-board reads
before arranging, and for the squares the local recognisers are unsure about.

Needs ANTHROPIC_API_KEY, from the environment or the project's .env. Model claude-opus-5-5, with the server-side
fallback enabled so a declined request is retried on Anthropic's recommended substitute instead of failing.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
SYMBOLS = ["K", "Q", "R", "B", "N", "P", "k", "q", "r", "b", "n", "p"]
NAMES = {"k": "king", "q": "queen", "r": "rook", "b": "bishop", "n": "knight", "p": "pawn"}
FILES = "abcdefgh"
ALL_SQUARES = [f + r for r in "12345678" for f in FILES]

SET_DESCRIPTION = (
    "The set is plastic Staunton: white pieces are cream, black pieces are black; the king is 85 mm tall. "
    "The set has one spare queen per colour, so two queens of one colour can be on the board after a promotion. "
    "The board is a green-and-white roll-up mat with 54 mm squares and a-h / 1-8 printed on its border.")

_env_loaded = False
_env_lock = threading.Lock()


def _load_env() -> None:
    """Read KEY=VALUE lines of the project's .env into os.environ (values already set win). Thread-safe: callers in
    other threads wait until the file has been read."""
    global _env_loaded
    with _env_lock:
        if _env_loaded:
            return
        p = Path(__file__).resolve().parents[2] / ".env"
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        _env_loaded = True


def available() -> bool:
    _load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def _jpeg_bytes(img: Any, max_side: int = 1280) -> bytes:
    if isinstance(img, (bytes, bytearray)):
        return bytes(img)
    if isinstance(img, (str, Path)):
        return Path(img).read_bytes()
    import cv2

    arr = np.asarray(img)
    h, w = arr.shape[:2]
    if max(h, w) > max_side:
        s = max_side / max(h, w)
        arr = cv2.resize(arr, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise ValueError("could not encode image")
    return buf.tobytes()


def _image_block(img: Any) -> dict[str, Any]:
    data = base64.standard_b64encode(_jpeg_bytes(img)).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def _client():
    import anthropic

    _load_env()
    return anthropic.Anthropic(timeout=300.0, max_retries=2)


def _ask(content: list[dict[str, Any]], schema: dict[str, Any], effort: str, max_tokens: int = 16000) -> tuple[dict[str, Any], dict[str, Any]]:
    """One structured-output request. Returns (parsed JSON, call info)."""
    t0 = time.time()
    # streamed: a careful whole-board read can think for minutes, longer than a plain request may stay open
    with _client().beta.messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": content}],
    ) as stream:
        resp = stream.get_final_message()
    info = {"model": resp.model, "latency_s": round(time.time() - t0, 2), "stop_reason": resp.stop_reason,
            "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the request ({getattr(resp.stop_details, 'category', None)})")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off (max_tokens)")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise RuntimeError(f"no text in the response (stop_reason {resp.stop_reason})")
    try:
        return json.loads(text), info
    except json.JSONDecodeError as exc:  # structured output should prevent this; keep the failure explicit
        raise RuntimeError(f"unparseable answer: {exc}") from exc


# --- whole-board read ------------------------------------------------------------------------------------------------
BOARD_SCHEMA = {
    "type": "object",
    "properties": {
        "pieces": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "square": {"type": "string", "enum": ALL_SQUARES},
                    "symbol": {"type": "string", "enum": SYMBOLS + ["?"]},
                    "confidence": {"type": "number"},
                },
                "required": ["square", "symbol", "confidence"],
                "additionalProperties": False,
            },
        },
        "obstructions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"square": {"type": "string", "enum": ALL_SQUARES}, "what": {"type": "string"}},
                "required": ["square", "what"],
                "additionalProperties": False,
            },
        },
        "off_board": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["pieces", "obstructions", "off_board", "notes"],
    "additionalProperties": False,
}


def label_squares(colour_bgr: np.ndarray, centres: dict[str, tuple[float, float]]) -> np.ndarray:
    """Copy of an overhead photo with each square's name written in its corner (away from the piece in the middle)."""
    import cv2

    out = colour_bgr.copy()
    a1, b1, a2 = (np.array(centres[s], dtype=float) for s in ("a1", "b1", "a2"))
    fvec, rvec = b1 - a1, a2 - a1
    size = float(np.hypot(*fvec))
    scale = max(0.3, min(0.55, size / 120.0))
    for sq, (u, v) in centres.items():
        p = np.array([u, v], dtype=float) - 0.36 * fvec - 0.36 * rvec  # towards the square's a1-side corner
        org = (int(p[0]) - int(6 * scale * 2), int(p[1]) + int(5 * scale * 2))
        cv2.putText(out, sq, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(out, sq, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 215, 255), 1, cv2.LINE_AA)
    return out


def read_board(overhead_labelled: Any = None, side_views: list[Any] | None = None, hint_fen: str | None = None,
               effort: str = "high") -> dict[str, Any]:
    """Whole-board read. `overhead_labelled`: the wrist camera's top-down photo with square names drawn on it
    (label_squares). `side_views`: photos from the table camera or other angles, unlabelled. Returns
    {"pieces": {square: {"symbol", "confidence"}}, "board_fen", "off_board", "notes", "call"}."""
    if overhead_labelled is None and not side_views:
        raise ValueError("need at least one image")
    content: list[dict[str, Any]] = []
    k = 1
    if overhead_labelled is not None:
        content += [{"type": "text", "text": f"Image {k}: the robot's wrist camera, straight above the board. Every square "
                                               "has its name (a1 to h8) written in small yellow text in one corner."},
                    _image_block(overhead_labelled)]
        k += 1
    for img in side_views or []:
        content += [{"type": "text", "text": f"Image {k}: a camera on the table seeing the same board at an angle, at the same moment."},
                    _image_block(img)]
        k += 1
    task = ("These photos show one physical chessboard at one moment, seen by a robot that is about to move pieces. "
            + SET_DESCRIPTION + " List every piece standing on a square of the board: the square, the piece as a FEN "
            "letter (uppercase K Q R B N P for white, lowercase k q r b n p for black, '?' if you cannot tell), and your "
            "confidence from 0 to 1 that both the square and the piece are right. Decide the square from the overhead "
            "image's labels when it is there; decide what each piece is from whichever view shows its shape best (the "
            "side view shows the silhouette: a king's cross, a queen's crown, a bishop's mitre, a knight's head, a "
            "rook's notched flat top, a pawn's small ball). Give a low confidence wherever the views disagree or a "
            "piece is hidden. Pieces lying on their side or standing off the board are not on a square: describe them "
            "in off_board. Separately, under obstructions, list every square that anything other than an upright piece "
            "centred on it touches or overhangs: a piece lying on its side (every square it covers), a piece standing "
            "across a square border (both squares), a hand, a tool. The robot will never put a piece there. Report what "
            "you see, not what a legal game position would be.")
    if hint_fen:
        task += (f" The robot's own estimate of the position is {hint_fen}; it may be wrong. Where you disagree with it, "
                 "trust the photos and say so in notes.")
    content.append({"type": "text", "text": task})
    data, info = _ask(content, BOARD_SCHEMA, effort, max_tokens=64000)
    pieces: dict[str, dict[str, Any]] = {}
    for p in data.get("pieces", []):
        sq = p["square"]
        if sq in pieces and pieces[sq]["confidence"] >= p["confidence"]:
            continue
        pieces[sq] = {"symbol": p["symbol"], "confidence": float(p["confidence"])}
    return {"pieces": pieces, "board_fen": board_fen(pieces), "off_board": data.get("off_board", ""),
            "obstructions": {o["square"]: o["what"] for o in data.get("obstructions", [])},
            "notes": data.get("notes", ""), "call": info}


def board_fen(pieces: dict[str, dict[str, Any]]) -> str:
    """FEN board part from {square: {"symbol"}} ('?' pieces are left out)."""
    rows = []
    for r in "87654321":
        row, empty = "", 0
        for f in FILES:
            s = pieces.get(f + r, {}).get("symbol")
            if s and s != "?":
                row += (str(empty) if empty else "") + s
                empty = 0
            else:
                empty += 1
        rows.append(row + (str(empty) if empty else ""))
    return "/".join(rows)


# --- check a believed position ----------------------------------------------------------------------------------------
CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "differences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "square": {"type": "string", "enum": ALL_SQUARES},
                    "expected": {"type": "string", "enum": SYMBOLS + ["."]},
                    "seen": {"type": "string", "enum": SYMBOLS + [".", "?"]},
                    "confidence": {"type": "number"},
                },
                "required": ["square", "expected", "seen", "confidence"],
                "additionalProperties": False,
            },
        },
        "off_board": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["differences", "off_board", "notes"],
    "additionalProperties": False,
}


def _expected_list(board_fen_part: str) -> str:
    import chess

    b = chess.Board(board_fen_part.split()[0] + " w - - 0 1")
    by_colour = {True: [], False: []}
    for sq, p in sorted(b.piece_map().items()):
        by_colour[p.color].append(f"{NAMES[p.symbol().lower()]} {chess.square_name(sq)}")
    return "White: " + ", ".join(by_colour[True]) + ". Black: " + ", ".join(by_colour[False]) + "."


def check_position(overhead_labelled: Any, expected_fen: str, side_views: list[Any] | None = None,
                   effort: str = "low") -> dict[str, Any]:
    """Does the board in the photos match the position the robot believes? Lists only the squares that differ (a
    missing piece, an extra piece, a different piece), each with a confidence. Much faster and more reliable than a
    blind read, because only the differences need judging. Returns {"matches", "differences", "off_board", "notes",
    "call"}; "matches" counts differences with confidence >= 0.5."""
    content: list[dict[str, Any]] = [
        {"type": "text", "text": "Image 1: the robot's wrist camera straight above the board; every square has its name "
                                 "written in small yellow text in one corner."},
        _image_block(overhead_labelled)]
    for k, img in enumerate(side_views or [], start=2):
        content += [{"type": "text", "text": f"Image {k}: a camera on the table seeing the same board at an angle."}, _image_block(img)]
    content.append({"type": "text", "text": (
        "A robot believes this chessboard holds exactly these pieces: " + _expected_list(expected_fen) + " "
        + SET_DESCRIPTION + " Compare the photos with that belief, square by square. List only the squares where the "
        "photos disagree: a believed piece that is missing, a piece on a square believed empty, or a different piece or "
        "colour. For each, give the expected FEN letter ('.' for empty), what you see ('.' empty, '?' unsure), and your "
        "confidence from 0 to 1 that the square really differs. Return an empty list when everything matches. Describe "
        "pieces lying on their side or standing off the board in off_board.")})
    data, info = _ask(content, CHECK_SCHEMA, effort)
    diffs = [d for d in data.get("differences", []) if d["expected"] != d["seen"]]
    return {"matches": not any(d["confidence"] >= 0.5 for d in diffs), "differences": diffs,
            "off_board": data.get("off_board", ""), "notes": data.get("notes", ""), "call": info}


# --- single pieces ---------------------------------------------------------------------------------------------------
PIECES_SCHEMA = {
    "type": "object",
    "properties": {
        "pieces": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "symbol": {"type": "string", "enum": SYMBOLS + [".", "?"]},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "symbol", "confidence", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["pieces"],
    "additionalProperties": False,
}


def identify_pieces(crops: dict[str, Any], context: str = "", effort: str = "medium") -> dict[str, dict[str, Any]]:
    """Identify one piece per image. `crops`: id -> image (JPEG bytes, a path, or a BGR array), e.g. a close-up from
    the wrist camera or a crop around one piece from the table camera. Returns id -> {"symbol" (FEN letter, "." for no
    piece, "?" for unsure), "confidence", "reason"}, plus "_call" with latency and tokens."""
    if not crops:
        return {}
    content: list[dict[str, Any]] = []
    for cid, img in crops.items():
        content += [{"type": "text", "text": f"Image id {cid}:"}, _image_block(img)]
    content.append({"type": "text", "text": (
        "Each image shows one chess piece, or an empty square, photographed by a robot. " + SET_DESCRIPTION + " "
        + (context + " " if context else "") +
        "For every image id, give the piece as a FEN letter (uppercase K Q R B N P for white, lowercase k q r b n p for "
        "black), '.' if there is no piece, '?' if you cannot tell; your confidence from 0 to 1; and a short reason "
        "naming the feature you used (cross, crown, mitre, horse head, notched flat top, small ball, colour).")})
    data, info = _ask(content, PIECES_SCHEMA, effort)
    out: dict[str, dict[str, Any]] = {p["id"]: {"symbol": p["symbol"], "confidence": float(p["confidence"]), "reason": p["reason"]}
                                      for p in data.get("pieces", []) if p["id"] in crops}
    out["_call"] = info  # type: ignore[assignment]
    return out


# --- comparison helpers (evaluation and the robot's checks) ----------------------------------------------------------
def compare(pieces: dict[str, dict[str, Any]], truth_fen_board: str) -> dict[str, Any]:
    """Score a read against a known board: per-square agreement over all 64 squares and over occupied squares."""
    import chess

    truth = chess.Board(truth_fen_board + " w - - 0 1") if " " not in truth_fen_board else chess.Board(truth_fen_board)
    exp = {chess.square_name(s): p.symbol() for s, p in truth.piece_map().items()}
    seen = {sq: p["symbol"] for sq, p in pieces.items() if p.get("symbol") not in (None, "?", ".")}
    wrong = sorted({sq for sq in set(exp) | set(seen) if exp.get(sq) != seen.get(sq)})
    occupied_ok = sum(1 for sq, s in exp.items() if seen.get(sq) == s)
    return {"squares_correct": 64 - len(wrong), "occupied_correct": occupied_ok, "occupied": len(exp),
            "exact": not wrong, "wrong": {sq: {"truth": exp.get(sq, "."), "read": seen.get(sq, ".")} for sq in wrong}}
