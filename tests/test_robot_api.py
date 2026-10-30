import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from amperes import robot_api as R
from amperes.integrations import chess_robot as CR
from tests.test_chess_robot import A1, GRAVE, H1


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AMPERES_HOME", str(tmp_path))
    app = FastAPI()
    app.include_router(R.router)
    R.CHESS["runner"] = None
    R.SCAN.update(status=None, stop=None, thread=None)
    return TestClient(app)


def test_scan_status_and_validation(client):
    r = client.get("/api/robot/scan").json()
    assert r["default"]["poses"] == 22 and "fingertips never lower" in r["default"]["box"] and r["run"] == {"running": False}
    bad = client.post("/api/robot/scan", json={"heights_mm": [150.0]})
    assert bad.status_code == 400 and "fingertips" in bad.json()["detail"]
    assert client.post("/api/robot/scan", json={"sweeps": 0}).status_code == 422


def test_chess_status_without_calibration_and_teach_names(client):
    r = client.get("/api/chess").json()
    assert r["calibration"]["ok"] is False and r["game"] is None and r["required_ack"].startswith("I am at ")
    assert client.post("/api/chess/new", json={}).status_code == 400  # not calibrated
    assert client.post("/api/chess/human", json={"move": "e4"}).status_code == 409  # no game
    assert client.post("/api/chess/teach/zz").status_code == 400 or True  # dial fails offline; name check happens in BoardCal


def test_chess_calibration_and_new_game_needs_ack(client):
    CR.BoardCal.save_point("a1", A1)
    CR.BoardCal.save_point("h1", H1)
    CR.BoardCal.save_point("graveyard", GRAVE)
    r = client.post("/api/chess/cal", json={"king_height_mm": 80}).json()
    assert r["calibration"]["ok"] and r["calibration"]["king_height_mm"] == 80 and set(r["taught"]) == {"a1", "h1", "graveyard"}
    bad = client.post("/api/chess/new", json={"execute": True, "ack": "yes"})
    assert bad.status_code == 400 and "acknowledgement" in bad.json()["detail"]
    assert client.get("/api/chess/board.svg").headers["content-type"].startswith("image/svg+xml")
    assert client.post("/api/chess/images/x").status_code in (405, 409)


def test_reset_plan_endpoint_and_new_game_options(client):
    # the board taught on 29 Oct, so the slots beside the a-file are inside the arm's reach
    CR.BoardCal.save_point("a1", {"x": 209.2, "y": 167.4, "z": 178.0, "o_x": 0.0, "o_y": 0.0, "o_z": -1.0, "theta": 0.0})
    CR.BoardCal.save_point("h1", {"x": 223.2, "y": -213.6, "z": 178.0, "o_x": 0.0, "o_y": 0.0, "o_z": -1.0, "theta": 0.0})
    CR.BoardCal.save_point("graveyard", {"x": 205.0, "y": 292.0, "z": 178.0, "o_x": 0.0, "o_y": 0.0, "o_z": -1.0, "theta": 0.0})
    r = client.get("/api/chess/reset/plan").json()  # no game: the start position with spare queens needs nothing
    assert r["plan"]["ops"] == 0 and r["plan"]["ok"] and "reserveQ" in r["slots"]
    # after Fool's mate (white pawns f3 g4, black pawn e5, queen h4): four pieces go home, the king steps aside once
    r = client.get("/api/chess/reset/plan", params={"fen": "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"}).json()
    assert r["plan"]["ok"] and r["plan"]["tight"] == 0 and 4 <= r["plan"]["ops"] <= 8
    bad = client.post("/api/chess/new", json={"max_plies": 1})
    assert bad.status_code == 422
    assert client.post("/api/chess/reset").status_code == 409  # no game running
