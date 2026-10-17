"""Full-resolution regression on the historical project frames; skipped when PLATE_TEST_ROOT is absent."""

from __future__ import annotations

import pytest

from plate_gates.compare import compare, load

DOCUMENTED_KEYS = {
    "size",
    "aspect_ref",
    "aspect_test",
    "ssim_gray",
    "ssim_gray_blur8",
    "edge_px",
    "best_shift_long",
    "de00_blur",
    *{f"canny_all_r{r}" for r in (0, 2, 4, 8)},
    *{f"canny_long_r{r}" for r in (0, 2, 4, 8)},
}


def test_vantage_vs_gpt_still_at_1500(test_root):
    ref_path = test_root / "car-entry" / "refs" / "vantage_shot1" / "V1_t493.png"
    test_path = test_root / "car-entry" / "stills" / "S1_GPT_a.png"
    if not (ref_path.is_file() and test_path.is_file()):
        pytest.skip("car-entry frames not present")
    res = compare(load(ref_path, 1500), load(test_path, 1500))
    assert DOCUMENTED_KEYS <= set(res)
    assert res["size"][0] == 1500
    assert -8 <= res["best_shift_long"]["dx"] <= 8
    assert -8 <= res["best_shift_long"]["dy"] <= 8
    assert 0.0 <= res["ssim_gray"] <= 1.0
