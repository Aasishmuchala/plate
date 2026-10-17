"""composite_masked / match_to_ring: outside = plate by construction, feather inside, tone match on the ring."""

from __future__ import annotations

import numpy as np
import pytest

from plate_gates import masks
from plate_gates.color import ciede2000, srgb_to_lab
from plate_gates.composite import composite_masked, match_to_ring


def _img16(h=120, w=160, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.random((h, w, 3)) * 50000 + 5000).astype(np.uint16)


def test_outside_the_mask_is_the_plate_exactly():
    plate, ai = _img16(seed=1), _img16(seed=2)
    m = masks.rect(plate.shape[:2], (40, 30, 120, 90))
    for tone in (True, False):
        out = composite_masked(plate, ai, m, feather_px=8, tone_match_ring=tone)
        assert out.dtype == np.uint16 and out.shape == plate.shape
        assert np.array_equal(out[~m], plate[~m])
    assert plate is not composite_masked(plate, ai, m)


def test_deep_inside_is_the_ai_layer_and_the_edge_blends():
    plate = np.full((100, 100, 3), 10000, np.uint16)
    ai = np.full((100, 100, 3), 50000, np.uint16)
    m = masks.rect((100, 100), (20, 20, 80, 80))
    out = composite_masked(plate, ai, m, feather_px=10, tone_match_ring=False)
    assert (out[50, 50] == 50000).all()
    edge = out[50, 20, 0]  # first inside column: alpha = 0.5 / 10
    assert edge == pytest.approx(10000 + 0.05 * 40000, abs=2)
    assert np.all(np.diff(out[50, 20:35, 0].astype(int)) >= 0)  # monotone ramp toward the AI value


def test_feather_zero_is_a_hard_paste():
    plate = np.full((60, 60, 3), 1000, np.uint16)
    ai = np.full((60, 60, 3), 60000, np.uint16)
    m = masks.rect((60, 60), (10, 10, 50, 50))
    out = composite_masked(plate, ai, m, feather_px=0, tone_match_ring=False)
    assert (out[m] == 60000).all() and (out[~m] == 1000).all()


def test_tone_match_removes_a_global_grade_measured_on_the_ring():
    plate = _img16(seed=3)
    ai = np.clip(plate.astype(np.int32) * 1.08 + 1500, 0, 65535).astype(np.uint16)  # same picture, regraded
    m = masks.rect(plate.shape[:2], (50, 30, 110, 90))
    matched = match_to_ring(plate, ai, m)
    band = masks.ring(m, 0, 24)

    def ring_de(layer):
        return float(ciede2000(srgb_to_lab(layer[band] / 257.0), srgb_to_lab(plate[band] / 257.0)).mean())

    assert ring_de(ai) > 2.0
    assert ring_de(matched) < 0.15 * ring_de(ai)
    out = composite_masked(plate, ai, m, feather_px=6, tone_match_ring=True)
    raw = composite_masked(plate, ai, m, feather_px=6, tone_match_ring=False)
    assert np.array_equal(out[~m], raw[~m])
    assert not np.array_equal(out[m], raw[m])


def test_uint8_inputs_are_promoted_and_shapes_are_checked():
    plate8 = np.full((40, 40, 3), 100, np.uint8)
    ai8 = np.full((40, 40, 3), 200, np.uint8)
    m = masks.rect((40, 40), (10, 10, 30, 30))
    out = composite_masked(plate8, ai8, m, feather_px=0, tone_match_ring=False)
    assert out.dtype == np.uint16 and out[20, 20, 0] == 200 * 257 and out[0, 0, 0] == 100 * 257
    with pytest.raises(ValueError):
        composite_masked(plate8, ai8[:30], m)
    with pytest.raises(ValueError):
        composite_masked(plate8, ai8, m[:30])
    with pytest.raises(ValueError):
        composite_masked(plate8.astype(np.float32), ai8, m)


def test_empty_mask_returns_the_plate_and_full_frame_mask_skips_the_ring():
    plate, ai = _img16(40, 40, 4), _img16(40, 40, 5)
    assert np.array_equal(composite_masked(plate, ai, np.zeros((40, 40), bool)), plate)
    full = np.ones((40, 40), bool)
    out = composite_masked(
        plate, ai, full, feather_px=5, tone_match_ring=True
    )  # no ring: no tone match, no crash
    assert np.array_equal(out[20, 20], ai[20, 20])
    assert np.array_equal(match_to_ring(plate, ai, full), ai)


def test_accepts_uint8_and_float_mask_encodings():
    plate, ai = _img16(seed=6), _img16(seed=7)
    m = masks.rect(plate.shape[:2], (40, 30, 120, 90))
    a = composite_masked(plate, ai, m)
    b = composite_masked(plate, ai, m.astype(np.uint8) * 255)
    c = composite_masked(plate, ai, m.astype(np.float32))
    assert np.array_equal(a, b) and np.array_equal(a, c)
