"""Claude vision and voice helpers, offline: the API is mocked, nothing is sent anywhere."""

import numpy as np

from amperes.integrations import chess_claude_vision as CCV
from amperes.integrations import chess_voice as V


def test_board_fen_and_compare():
    pieces = {"e4": {"symbol": "P", "confidence": 0.9}, "e5": {"symbol": "p", "confidence": 0.8}, "a1": {"symbol": "?", "confidence": 0.2}}
    assert CCV.board_fen(pieces) == "8/8/8/4p3/4P3/8/8/8"
    c = CCV.compare(pieces, "8/8/8/4p3/4P3/8/8/R7")
    assert c["squares_correct"] == 63 and c["wrong"] == {"a1": {"truth": "R", "read": "."}}


def test_read_board_and_check_position_parse(monkeypatch):
    calls = []

    def fake_ask(content, schema, effort, max_tokens=16000):
        calls.append((len(content), effort))
        if "pieces" in schema["properties"] and "off_board" in schema["properties"]:
            return {"pieces": [{"square": "e4", "symbol": "P", "confidence": 0.9}, {"square": "e4", "symbol": "B", "confidence": 0.3}],
                    "off_board": "", "notes": ""}, {"latency_s": 0.1}
        return {"differences": [{"square": "e4", "expected": "P", "seen": "B", "confidence": 0.7},
                                {"square": "d4", "expected": ".", "seen": ".", "confidence": 0.9}], "off_board": "", "notes": ""}, {"latency_s": 0.1}

    monkeypatch.setattr(CCV, "_ask", fake_ask)
    img = np.zeros((40, 40, 3), np.uint8)
    r = CCV.read_board(img, [img])
    assert r["pieces"] == {"e4": {"symbol": "P", "confidence": 0.9}} and r["board_fen"] == "8/8/8/8/4P3/8/8/8"
    chk = CCV.check_position(img, "8/8/8/8/4P3/8/8/8")
    assert chk["matches"] is False and [d["square"] for d in chk["differences"]] == ["e4"]


def test_label_squares_draws_without_changing_size():
    img = np.full((480, 640, 3), 128, np.uint8)
    centres = {f + r: (60 + 57 * "abcdefgh".index(f), 40 + 57 * (int(r) - 1)) for f in "abcdefgh" for r in "12345678"}
    out = CCV.label_squares(img, centres)
    assert out.shape == img.shape and (out != img).any()


def test_voice_phrasing_and_disabled(monkeypatch):
    assert V._spoken("Nxf7+") == "knight takes f7, check"
    assert V._spoken("exd8=Q#") == "e takes d8 promotes to queen, checkmate"
    assert V._spoken("O-O") == "castles kingside"
    assert V.event_for("Qxf7#") == "game_over" and V.event_for("Bxc6") == "capture"
    monkeypatch.setenv("AMPERES_VOICE", "0")
    assert not V.enabled()
    V.announce("robot_move", "e4", "", use_claude=False)  # no thread, no sound
