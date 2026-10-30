"""Survey logic offline: poses inside the envelope, merging views, squares from arm coordinates, height cross-check."""

import math

from amperes.integrations import chess_survey as SV
from tests.test_chess_lifecycle import cal  # noqa: F401  (fixture)


def test_poses_cover_the_board_and_stay_safe(cal):  # noqa: F811
    env = cal.envelope(margin_mm=130.0)
    poses = SV.survey_poses(cal)
    assert len(poses) == 6
    for p in poses:
        assert not env.check({k: v for k, v in p.items() if not k.startswith("_")}, None)
        assert math.hypot(p["x"], p["y"]) < 640
    s = cal.square_mm
    us, vs = sorted({p["_u"] for p in poses}), sorted({p["_v"] for p in poses})
    assert us[0] - SV.FOV_HALF_NARROW <= -0.5 * s - SV.MARGIN_MM + 1 and us[-1] + SV.FOV_HALF_NARROW >= 7.5 * s + SV.MARGIN_MM - 1
    assert vs[0] - SV.FOV_HALF_WIDE <= -0.5 * s - SV.MARGIN_MM + 1 and vs[-1] + SV.FOV_HALF_WIDE >= 7.5 * s + SV.MARGIN_MM - 1


def test_square_of_and_board_area(cal):  # noqa: F811
    x, y = cal.square_xy("e4")
    assert SV.square_of(cal, x + 5, y - 5) == "e4"
    fx, fy = cal.file_axis
    assert SV.square_of(cal, x + fx / 2, y + fy / 2) is None  # on the line between e4 and f4
    ax, ay = cal.square_xy("a1")
    rx, ry = cal.rank_axis
    off = (ax - 1.5 * fx, ay - 1.5 * fy)  # a square and a half outside the a-file
    assert SV.square_of(cal, *off) is None and not SV.on_board_area(cal, *off)


def test_merge_keeps_the_most_central_view():
    views = [[{"x": 100.0, "y": 50.0, "height": 40.0, "u": 600.0, "v": 400.0, "area": 300}],
             [{"x": 105.0, "y": 52.0, "height": 44.0, "u": 330.0, "v": 250.0, "area": 320}]]
    m = SV.merge(views)
    assert len(m) == 1 and m[0]["view"] == 1 and m[0]["seen"] == 2 and m[0]["height"] == 42.0


def test_height_check_trusts_kings_and_flags_mismatches():
    pieces = [{"id": f"p{i}", "upright": True, "symbol": "p", "confidence": 0.9, "height": 38.5} for i in range(4)]
    pieces += [{"id": "k1", "upright": True, "symbol": "q", "confidence": 0.5, "height": 72.0},   # a king's height
               {"id": "x1", "upright": True, "symbol": "r", "confidence": 0.8, "height": 53.0}]   # too tall for a rook
    scale = SV.check_heights(pieces, log=lambda *_: None)
    assert abs(scale - 38.5 / 45.0) < 1e-6
    assert pieces[4]["symbol"] == "k" and pieces[4]["confidence"] == 0.7
    assert pieces[5]["symbol"] == "r" and pieces[5]["confidence"] <= 0.3
