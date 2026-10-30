"""Halloween commentary: the robot speaks a short spooky line after each move.

Claude writes the line from the move and the position (claude-opus-5-5, low effort, a few seconds); when there is no
key, no network, or it takes too long, a canned line is used instead. The Mac speaks it with a creaky voice; checkmate
gets the voice that sings. Never blocks the arm: everything runs on a background thread, and a new line cuts off the
previous one. Turn it off with AMPERES_VOICE=0.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import threading
import time
from typing import Any

VOICE = "Albert"        # creaky and ghostly
DOOM_VOICE = "Bad News"  # sings in a minor key: for checkmate and resignations
CANNED = {
    "robot_move": ["{san}. The crypt creaks open.", "{san}. Your pieces grow cold.", "I play {san}. Can you hear the bones rattle?",
                   "{san}. Something moves in the dark.", "{san}. The candles flicker."],
    "capture": ["{san}. Another soul for my graveyard.", "{san}. Into the crypt it goes.", "{san}. Rest in pieces."],
    "check": ["{san}. Check. I can smell your fear.", "{san}, check. Nowhere to hide."],
    "human_move": ["{san}. Bold. Foolish, but bold.", "Ah, {san}. I saw that coming from beyond the grave.",
                   "{san}. My circuits tingle."],
    "game_over": ["Checkmate. Your king joins the undead.", "The game is over. Mwahahaha."],
    "reset": ["Now I shall raise the fallen pieces from their graves.", "Back to your coffins, all of you."],
    "reset_done": ["The board is ready. Who dares to play next?"],
}

_state: dict[str, Any] = {"proc": None, "last": None}
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("AMPERES_VOICE", "1") != "0" and shutil.which("say") is not None


def canned(event: str, san: str = "") -> str:
    return random.choice(CANNED.get(event, CANNED["robot_move"])).format(san=_spoken(san))


def _spoken(san: str) -> str:
    """Say chess notation the way a person would ('Nxf7+' -> 'knight takes f7, check')."""
    if not san:
        return ""
    if san.startswith("O-O-O"):
        return "castles queenside"
    if san.startswith("O-O"):
        return "castles kingside"
    names = {"K": "king", "Q": "queen", "R": "rook", "B": "bishop", "N": "knight"}
    s = san.replace("+", "").replace("#", "")
    is_piece = bool(s) and s[0] in names
    body = (s[1:] if is_piece else s).replace("x", " takes ").replace("=", " promotes to ")
    for k, v in names.items():
        body = body.replace(" promotes to " + k, " promotes to " + v)
    words = " ".join(body.split())
    text = f"{names[s[0]]} {words}" if is_piece else words  # pawn moves are said by square: "e4", "e takes d5"
    return text + (", checkmate" if "#" in san else ", check" if "+" in san else "")


def claude_line(event: str, san: str, fen: str, who: str, timeout_s: float = 15.0) -> str | None:
    """One spooky sentence from Claude, or None (no key, error or too slow)."""
    from amperes.integrations import chess_claude_vision as CCV

    if not CCV.available():
        return None
    out: dict[str, str] = {}

    def ask():
        try:
            client = CCV._client()
            msg = client.messages.create(
                model=CCV.MODEL, max_tokens=300, output_config={"effort": "low"},
                messages=[{"role": "user", "content": (
                    f"You are a haunted chess robot at a Halloween hackathon demo. {who} just played {san} "
                    f"({event.replace('_', ' ')}); position FEN {fen}. Say one short, spooky, funny line about it, at most 14 "
                    "words, family friendly, to be spoken aloud. Name the move the way a person says it. Output only the line.")}])
            if msg.stop_reason == "refusal":
                return
            text = next((b.text for b in msg.content if b.type == "text"), "").strip().strip('"')
            if text:
                out["line"] = text
        except Exception:
            pass

    t = threading.Thread(target=ask, daemon=True)
    t.start()
    t.join(timeout_s)
    return out.get("line")


def speak(text: str, voice: str = VOICE) -> None:
    if not enabled() or not text:
        return
    with _lock:
        p = _state.get("proc")
        if p is not None and p.poll() is None:
            p.terminate()
        try:
            _state["proc"] = subprocess.Popen(["say", "-v", voice, "-r", "165", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            _state["proc"] = None


def announce(event: str, san: str = "", fen: str = "", who: str = "The robot", use_claude: bool = True) -> None:
    """Speak a line for `event` (robot_move, capture, check, human_move, game_over, reset, reset_done) in the background."""
    if not enabled():
        return

    def run():
        line = claude_line(event, san, fen, who) if use_claude and san else None
        line = line or canned(event, san)
        _state["last"] = {"t": time.time(), "event": event, "line": line}
        speak(line, DOOM_VOICE if event == "game_over" else VOICE)

    threading.Thread(target=run, daemon=True).start()


def event_for(san: str) -> str:
    return "game_over" if "#" in san else "check" if "+" in san else "capture" if "x" in san else "robot_move"


def last_line() -> dict[str, Any] | None:
    return _state.get("last")
