"""CIEDE2000 against the Sharma/Wu/Dalal (2005) reference pairs, plus the sRGB <-> Lab conversions."""

from __future__ import annotations

import numpy as np
import pytest

from plate_gates.color import ciede2000, hex_to_rgb8, lab_to_srgb, srgb_to_lab

SHARMA = [
    ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
    ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
    ((50.0000, 2.5000, 0.0000), (50.0000, 0.0000, -2.5000), 4.3065),
    ((50.0000, 2.5000, 0.0000), (73.0000, 25.0000, -18.0000), 27.1492),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
]


@pytest.mark.parametrize(("lab1", "lab2", "expected"), SHARMA)
def test_sharma_pairs(lab1, lab2, expected):
    got = float(ciede2000(np.array([lab1]), np.array([lab2]))[0])
    assert got == pytest.approx(expected, abs=2e-4)


def test_symmetric_and_zero_on_identity():
    lab = srgb_to_lab(np.random.default_rng(1).integers(0, 256, (50, 3), dtype=np.uint8))
    assert np.allclose(ciede2000(lab, lab), 0.0)
    other = lab[::-1]
    assert np.allclose(ciede2000(lab, other), ciede2000(other, lab))


def test_srgb_white_is_l100():
    lab = srgb_to_lab(np.array([[255, 255, 255]], np.uint8))[0]
    assert abs(lab[0] - 100.0) < 0.01
    assert abs(lab[1]) < 0.01 and abs(lab[2]) < 0.01


def test_srgb_black_is_l0():
    assert abs(srgb_to_lab(np.zeros((1, 3), np.uint8))[0, 0]) < 1e-9


def test_lab_roundtrip_on_32_cube_grid():
    levels = np.round(np.linspace(0, 255, 32)).astype(np.uint8)
    r, g, b = np.meshgrid(levels, levels, levels, indexing="ij")
    grid = np.stack([r, g, b], -1).reshape(-1, 3)
    back = np.round(lab_to_srgb(srgb_to_lab(grid))).astype(np.int32)
    assert back.shape == grid.shape
    assert (back == grid.astype(np.int32)).all()


def test_lab_to_srgb_range_is_clipped_to_0_255():
    out = lab_to_srgb(np.array([[100.0, 120.0, 120.0], [0.0, -120.0, -120.0]]))
    assert out.min() >= 0.0 and out.max() <= 255.0


def test_hex_to_rgb8():
    got = hex_to_rgb8("#383E42")
    assert got.shape == (1, 3) and got.dtype == np.uint8
    assert got.tolist() == [[0x38, 0x3E, 0x42]]
    assert hex_to_rgb8("ff0080").tolist() == [[255, 0, 128]]
