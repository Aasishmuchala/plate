"""masks.py: erode, rect, feather_inside (exactly 0 outside), ring."""

from __future__ import annotations

import numpy as np
import pytest

from plate_gates import masks


def test_as_bool_accepts_every_encoding():
    b = np.zeros((4, 4), bool)
    b[1:3, 1:3] = True
    for enc in (
        b,
        b.astype(np.uint8),
        b.astype(np.uint8) * 255,
        b.astype(np.float32),
        b.astype(np.float32) * 0.9,
    ):
        assert np.array_equal(masks.as_bool(enc), b)
    assert np.array_equal(masks.as_bool(np.stack([b, b, b], -1)), b)  # 3-channel mask: first channel
    assert not masks.as_bool(np.full((3, 3), 100, np.uint8)).any()  # 0/255 convention: 100 is below the cut


def test_rect_is_clamped_and_exclusive():
    m = masks.rect((10, 20), (5, 2, 12, 6))
    assert m.shape == (10, 20) and m.sum() == 7 * 4
    assert m[2, 5] and m[5, 11] and not m[6, 5] and not m[2, 12]
    clamped = masks.rect((10, 20), (-5, -5, 100, 100))
    assert clamped.all()
    assert not masks.rect((10, 20), (30, 30, 40, 40)).any()


def test_erode_shrinks_by_px_and_can_vanish():
    m = masks.rect((40, 40), (10, 10, 30, 30))  # 20 x 20
    e = masks.erode(m, 3)
    assert e.sum() == 14 * 14 and e[13, 13] and not e[12, 12]
    assert masks.erode(m, 0).sum() == m.sum()
    assert not masks.erode(m, 12).any()
    assert masks.erode(m.astype(np.uint8) * 255, 3).dtype == bool


def test_feather_is_exactly_zero_outside_and_one_deep_inside():
    m = masks.rect((120, 160), (30, 20, 130, 100))
    a = masks.feather_inside(m, 8)
    assert a.dtype == np.float32 and a.shape == m.shape
    assert (a[~m] == 0.0).all()  # not "small": exactly zero
    assert (a[m] > 0).all()
    assert a[60, 80] == 1.0
    assert a[60, 30] < a[60, 34] < a[60, 38] <= 1.0  # ramps up going inward
    assert a[60, 30 + 8] == pytest.approx(1.0, abs=1e-6)  # fully opaque by px pixels inside
    # monotone along an inward line
    assert np.all(np.diff(a[60, 30:50]) >= 0)


def test_feather_ramp_values_follow_the_distance_to_the_edge():
    m = masks.rect((40, 80), (10, 10, 70, 30))
    a = masks.feather_inside(m, 4)
    # first inside column is 1 px from an outside pixel -> (1 - 0.5) / 4
    assert a[20, 10] == pytest.approx(0.125, abs=1e-6)
    assert a[20, 11] == pytest.approx(0.375, abs=1e-6)
    assert a[20, 12] == pytest.approx(0.625, abs=1e-6)
    assert a[20, 14] == 1.0


def test_feather_zero_px_is_the_hard_mask_and_borders_are_not_feathered():
    m = masks.rect((30, 30), (5, 5, 25, 25))
    assert np.array_equal(masks.feather_inside(m, 0), m.astype(np.float32))
    full_left = masks.rect((30, 30), (0, 5, 25, 25))  # touches the frame edge: no ramp there
    a = masks.feather_inside(full_left, 6)
    assert a[15, 0] == 1.0 and a[15, 24] < 1.0
    assert (masks.feather_inside(np.ones((8, 8), bool), 5) == 1.0).all()


def test_ring_is_the_band_outside_the_mask():
    m = masks.rect((100, 100), (40, 40, 60, 60))
    r = masks.ring(m, 0, 10)
    assert not (r & m).any()
    assert r[50, 39] and r[50, 31] and not r[50, 29] and not r[0, 0]
    inner = masks.ring(m, 4, 10)
    assert not inner[50, 38] and inner[50, 35]
    assert not masks.ring(np.zeros((10, 10), bool), 0, 5).any()
    with pytest.raises(ValueError):
        masks.ring(m, 5, 5)
