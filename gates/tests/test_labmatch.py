"""Reinhard Lab transfer."""

from __future__ import annotations

import numpy as np

from plate_gates.labmatch import lab_match


def _photo_like(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.random((48, 64, 3)) * 255).astype(np.uint8)


def test_identity_transfer_is_noop():
    x = _photo_like(0)
    out = lab_match(x, x)
    assert out.dtype == np.uint8 and out.shape == x.shape
    assert np.abs(out.astype(int) - x.astype(int)).max() <= 1


def test_flat_grey_moves_to_reference_level():
    a = np.full((20, 20, 3), 60, np.uint8)
    b = np.full((20, 20, 3), 180, np.uint8)
    assert abs(float(lab_match(a, b).mean()) - 180.0) <= 2


def test_strength_zero_returns_source():
    x = _photo_like(1)
    y = _photo_like(2)
    out = lab_match(x, y, strength=0.0)
    assert np.abs(out.astype(int) - x.astype(int)).max() <= 1


def test_full_strength_moves_channel_means_toward_reference():
    from plate_gates.color import srgb_to_lab

    dark = (np.random.default_rng(3).random((40, 40, 3)) * 80).astype(np.uint8)
    bright = (140 + np.random.default_rng(4).random((40, 40, 3)) * 100).astype(np.uint8)
    out = lab_match(dark, bright)
    l_out = srgb_to_lab(out)[..., 0].mean()
    l_ref = srgb_to_lab(bright)[..., 0].mean()
    l_src = srgb_to_lab(dark)[..., 0].mean()
    assert abs(l_out - l_ref) < abs(l_src - l_ref) * 0.2
