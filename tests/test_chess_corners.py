"""Corner calibration: find a printed square's centre in an image, and fit corrected teach points."""

import numpy as np
import pytest

from amperes.integrations import chess_corners as C
from amperes.integrations import chess_robot as CR


def checkerboard(sq=96, off=(13, -9), piece=True):
    img = np.zeros((480, 640, 3), np.uint8)
    img[:] = (40, 90, 30)  # dark green
    for i in range(-2, 9):
        for j in range(-2, 8):
            if (i + j) % 2 == 0:
                x0, y0 = int(320 + off[0] + (i - 0.5) * sq - 3 * sq), int(240 + off[1] + (j - 0.5) * sq - 3 * sq)
                img[max(0, y0):max(0, y0 + sq), max(0, x0):max(0, x0 + sq)] = (235, 235, 235)
    if piece:  # a dark piece standing in the middle of the centre square
        yy, xx = np.ogrid[:480, :640]
        img[(xx - 320 - off[0]) ** 2 + (yy - 240 - off[1]) ** 2 < 25 ** 2] = (20, 20, 20)
    return img


@pytest.mark.parametrize("off", [(13, -9), (-20, 6), (0, 0)])
def test_square_centre_found_despite_a_piece(off):
    img = checkerboard(off=off)
    found = C.square_centre_px(img, (320, 240), 96)
    assert found is not None
    assert found["u"] == pytest.approx(320 + off[0], abs=3) and found["v"] == pytest.approx(240 + off[1], abs=3)


def test_corrected_fit_recovers_a_shift():
    cal = CR.BoardCal(a1={"x": 209.2, "y": 167.4, "z": 178.0}, h1={"x": 223.2, "y": -213.6, "z": 178.0},
                      graveyard={"x": 205.0, "y": 292.0, "z": 178.0})
    offsets = {sq: {"dx": 6.0, "dy": -4.0} for sq in C.CORNERS}
    fit = C.corrected(cal, offsets)
    assert fit["a1"][0] == pytest.approx(215.2, abs=0.01) and fit["a1"][1] == pytest.approx(163.4, abs=0.01)
    assert fit["scale"] == pytest.approx(1.0, abs=1e-6) and max(fit["residual_mm"]) < 0.01
