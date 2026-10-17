"""Reinhard Lab transfer."""

from __future__ import annotations

import numpy as np
import pytest

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


def test_reinhard_is_lab_match_without_a_mask():
    from plate_gates.labmatch import reinhard

    x, y = _photo_like(5), _photo_like(6)
    assert np.array_equal(reinhard(x, y, 0.7), lab_match(x, y, 0.7))


def test_std_guard_flat_channels_shift_the_mean_without_exploding():
    from plate_gates.labmatch import STD_GUARD, reinhard

    assert STD_GUARD == 1e-3
    flat = np.full((20, 20, 3), 60, np.uint8)
    textured = _photo_like(7)
    out = reinhard(flat, textured)  # source std = 0: no divide by zero, no NaN
    assert out.dtype == np.uint8 and np.isfinite(out).all()
    assert out.reshape(-1, 3).std(axis=0).max() < 1.0  # spatially flat: gain 1, only the mean moved
    # flat reference: a textured source must not be flattened to a slab
    out2 = reinhard(textured, np.full((20, 20, 3), 128, np.uint8))
    assert out2.reshape(-1, 3).std(axis=0).min() > 5


def test_mask_restricts_where_the_statistics_are_measured():
    from plate_gates.color import srgb_to_lab
    from plate_gates.labmatch import reinhard

    src = np.full((40, 80, 3), 90, np.uint8)
    src[:, 40:] = 200  # half dark, half light
    ref = np.full((40, 80, 3), 90, np.uint8)
    ref[:, 40:] = 90
    left = np.zeros((40, 80), bool)
    left[:, :40] = True
    # stats from the dark half only -> the dark half already equals the reference, nothing moves
    out = reinhard(src, ref, mask=left)
    assert np.abs(out.astype(int) - src.astype(int)).max() <= 1
    # whole-image statistics instead pull everything toward the grey reference
    whole = reinhard(src, ref)
    assert srgb_to_lab(whole)[..., 0].mean() < srgb_to_lab(src)[..., 0].mean() - 10
    # (src_mask, ref_mask) tuple: different selections per image
    both = reinhard(src, ref, mask=(left, left))
    assert np.abs(both.astype(int) - src.astype(int)).max() <= 1
    with pytest.raises(ValueError):
        reinhard(src, ref, mask=np.zeros((40, 80), bool))  # empty selection
