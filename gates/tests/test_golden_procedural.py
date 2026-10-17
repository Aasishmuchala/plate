"""Procedural goldens: committed, generator-based, always run (the client-derived PNG goldens are gitignored).

Images come from golden/procedural.py (numpy PCG64 + cv2 primitives, nothing read from disk); the expected
numbers live in golden/expected_procedural.json with per-metric tolerances (ssim 0.005, recalls 0.01, dE 0.05,
texture 2 %, landing 0.1, shifts exact). Regenerate with `pytest gates/tests/test_golden_procedural.py
--update-goldens`; CI never passes the flag. Each frozen section is paired with semantic assertions that hold
whatever the frozen numbers are, so a regenerated JSON cannot silently bless a broken metric.
"""

from __future__ import annotations

import procedural as P
import pytest

from plate_gates.io import ffmpeg

HAVE_FFMPEG = ffmpeg() is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not found (set PLATE_FFMPEG)")


@pytest.fixture(scope="module")
def computed(tmp_path_factory, update_goldens):
    data = P.sections(tmp_path_factory.mktemp("procedural"), ffmpeg_ok=HAVE_FFMPEG)
    if update_goldens:
        if not HAVE_FFMPEG:
            pytest.fail("--update-goldens needs ffmpeg for the landing / flicker sections")
        P.write_expected(data)
    return data


@pytest.fixture(scope="module")
def expected(computed):
    if not P.EXPECTED.is_file():
        pytest.fail("expected_procedural.json missing: run this file with --update-goldens")
    return P.read_expected()


def _assert_frozen(computed, expected, section, case):
    assert case in expected[section], f"{section}.{case} not frozen"
    bad = P.compare_to_expected(computed[section][case], expected[section][case], f"{section}.{case}")
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize("case", [*P.COMPARE_CASES, "cross_scene"])
def test_compare_frozen(computed, expected, case):
    _assert_frozen(computed, expected, "compare", case)


@pytest.mark.parametrize("case", list(P.TEXTURE_CASES))
def test_texture_frozen(computed, expected, case):
    _assert_frozen(computed, expected, "texture", case)


def test_maskdiff_frozen(computed, expected):
    _assert_frozen(computed, expected, "maskdiff", "edit_inside_mask")


def test_composite_masked_frozen(computed, expected):
    _assert_frozen(computed, expected, "composite_masked", "brighter_ai_with_edit")


def test_labmatch_frozen(computed, expected):
    _assert_frozen(computed, expected, "labmatch", "scene_to_tinted_scene")


def test_camera_lock_frozen(computed, expected):
    _assert_frozen(computed, expected, "camera_lock", "pan_after_half")


@needs_ffmpeg
def test_landing_frozen(computed, expected):
    _assert_frozen(computed, expected, "landing", "aspect_correct_43_in_169")


@needs_ffmpeg
def test_flicker_and_duration_frozen(computed, expected):
    _assert_frozen(computed, expected, "flicker", "steady_vs_pumping")


def test_frozen_file_documents_the_tolerances_the_comparison_uses(expected):
    assert expected["_tolerances"] == P.TOLERANCE_DOC


# --- semantic assertions that do not depend on the frozen numbers -------------------------------------------


def test_identity_is_perfect(computed):
    c = computed["compare"]["identity"]
    assert c["ssim_gray"] > 0.999 and c["long_recall_r0"] > 0.999 and c["de00_blur_median"] < 1e-6


def test_three_px_shift_is_caught_strictly_and_forgiven_at_r4(computed):
    c = computed["compare"]["shift3"]
    assert c["long_recall_r4"] > 0.95 and c["long_recall_r0"] < 0.8
    # r=2 dilation makes every shift in -5..-1 a near-tie, so the argmax is content dependent: bound it, not pin it
    assert -5 <= c["best_shift_dx"] <= -1 and -2 <= c["best_shift_dy"] <= 2


def test_scaled_long_edge_cutoff_keeps_more_edges(computed):
    scaled, fixed = computed["compare"]["shift3_scaled_long_edges"], computed["compare"]["shift3"]
    # 640 wide: cut-off round(30*640/1500) = 13 px instead of 30, so more edge pixels qualify
    assert scaled["long_recall_r2"] != fixed["long_recall_r2"]


def test_tint_moves_colour_not_structure(computed):
    c = computed["compare"]["tint"]
    assert c["de00_blur_median"] > 3 and c["long_recall_r2"] > 0.99 and c["ssim_gray"] > 0.99


def test_blur_loses_long_edge_recall_but_not_precision(computed):
    c = computed["compare"]["blur2"]
    assert c["long_recall_r0"] < 0.5 and c["long_precision_r2"] > 0.95


def test_added_window_costs_precision_not_recall(computed):
    c = computed["compare"]["window_added"]
    assert c["long_precision_r2"] < 0.95 <= c["long_recall_r2"] + 0.02


def test_crunch_reads_far_sharper_than_the_scene(computed):
    soft, crunch = computed["texture"]["soft"], computed["texture"]["crunch"]
    assert crunch["lapvar"] > 3 * soft["lapvar"] and crunch["fine_ratio"] > soft["fine_ratio"]


def test_maskdiff_outside_is_untouched(computed):
    c = computed["maskdiff"]["edit_inside_mask"]
    assert c["outside_frac_identical"] == 1.0 and c["inside_frac_identical"] < 0.05


def test_composite_outside_is_the_plate_and_tone_match_closes_the_ring(computed):
    c = computed["composite_masked"]["brighter_ai_with_edit"]
    assert c["outside_identical"] is True and c["outside_max_abs_diff"] == 0.0
    assert c["de00_ring_matched_mean"] < 0.1 * c["de00_ring_raw_mean"]


def test_reinhard_full_strength_lands_on_the_reference_mean(computed):
    c = computed["labmatch"]["scene_to_tinted_scene"]
    assert c["de00_mean_to_ref_s1.0"] < 0.5 < c["de00_mean_to_ref_s0.5"]
    assert (
        abs(c["flat_grey_result_mean"] - 180) <= 1
    )  # the std guard shifts a flat slab instead of dividing by 0


def test_camera_lock_sees_the_pan_in_every_textured_region(computed):
    c = computed["camera_lock"]["pan_after_half"]
    assert c["verdict"] == "moved"
    assert [c[f"region{i}_dx"] for i in range(3)] == [5, 5, 5]
    assert [c[f"region{i}_dy"] for i in range(3)] == [0, 0, 0]


@needs_ffmpeg
def test_landing_reads_back_the_offsets_and_last_is_closer_to_end_than_start(computed):
    c = computed["landing"]["aspect_correct_43_in_169"]
    assert 3.0 < c["first"] < 5.5 and 5.0 < c["last"] < 7.5
    assert c["last_vs_start"] > 1.5 * c["last"]


@needs_ffmpeg
def test_flicker_separates_steady_from_pumping_and_duration_check_gates(computed):
    c = computed["flicker"]["steady_vs_pumping"]
    assert c["steady_mean_abs_luma_delta"] < 0.5 and c["pumping_mean_abs_luma_delta"] > 6
    assert c["duration_ok"] is True and c["duration_short_ok"] is False
