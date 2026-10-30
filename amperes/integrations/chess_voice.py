"""Move calls: the robot says every move out loud, in plain words, as soon as the move is made.

"White knight takes F7, check." Every move on both sides, whoever made it: the arm, a person seen by the wrist camera,
or a move typed into the console; then the result when the game ends. The Mac's `say` speaks the calls in order on one
background thread, so the arm never waits and no call cuts off another. Turn it off with AMPERES_VOICE=0; choose another
voice with AMPERES_VOICE_NAME.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time
from typing import Any

VOICE = "Samantha"  # clear US English, installed on every Mac
LINES = {"reset": "Resetting the board.", "reset_done": "The board is ready."}
RESULTS = {"1-0": "White wins.", "0-1": "Black wins.", "1/2-1/2": "Draw."}
NAMES = {"K": "king", "Q": "queen", "R": "rook", "B": "bishop", "N": "knight"}

_state: dict[str, Any] = {"last": None, "worker": None}
_lines: queue.Queue = queue.Queue(maxsize=16)
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("AMPERES_VOICE", "1") != "0" and shutil.which("say") is not None


def _spoken(san: str) -> str:
    """Say chess notation the way a person would ('Nxf7+' -> 'knight takes F7, check'; 'e4' -> 'pawn to E4')."""
    if not san:
        return ""
    check = ", checkmate" if "#" in san else ", check" if "+" in san else ""
    s = san.rstrip("+#")
    if s.startswith("O-O-O"):
        return "castles queenside" + check
    if s.startswith("O-O"):
        return "castles kingside" + check
    s, promo = (s.split("=") + [""])[:2]
    text = f"{NAMES.get(s[0], 'pawn')} {'takes' if 'x' in s else 'to'} {s[-2:].upper()}"  # squares in capitals: "A3", not "a 3"
    if promo:
        text += f", promotes to {NAMES.get(promo[0], promo)}"
    return text + check


def move_line(san: str, fen: str = "") -> str:
    """The call for one move. The side that moved is read from `fen`, the position after the move."""
    parts = fen.split()
    side = ("White " if parts[1] == "b" else "Black ") if len(parts) > 1 else ""
    text = side + _spoken(san)
    return text[:1].upper() + text[1:] + "." if text else ""


def result_line(verdict: dict[str, Any] | None) -> str:
    """'Game over: checkmate. White wins.' from an Adjudicator verdict."""
    v = verdict or {}
    reason = (v.get("reason") or "").replace(" plies", " half-moves")
    return " ".join(x for x in (f"Game over: {reason}." if reason else "Game over.", RESULTS.get(v.get("result", ""), "")) if x)


def _voice() -> list[str]:
    name = os.environ.get("AMPERES_VOICE_NAME", VOICE)
    return ["-v", name] if name else []


def _say_lines() -> None:
    while True:
        text = _lines.get()
        try:
            subprocess.run(["say", *_voice(), "-r", "175", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass


def speak(text: str) -> None:
    """Queue a line; one background thread says the lines in order."""
    if not enabled() or not text:
        return
    with _lock:
        w = _state.get("worker")
        if w is None or not w.is_alive():
            w = threading.Thread(target=_say_lines, daemon=True)
            _state["worker"] = w
            w.start()
    try:
        _lines.put_nowait(text)
    except queue.Full:
        pass


def announce(event: str, san: str = "", fen: str = "", verdict: dict[str, Any] | None = None) -> None:
    """Say the call for `event`: 'move' (its SAN and the position after it), 'game_over', 'reset' or 'reset_done'."""
    if not enabled():
        return
    line = move_line(san, fen) if event == "move" else result_line(verdict) if event == "game_over" else LINES.get(event, "")
    if line:
        _state["last"] = {"t": time.time(), "event": event, "line": line}
        speak(line)


def last_line() -> dict[str, Any] | None:
    return _state.get("last")
