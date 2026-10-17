"""texture.stats / band_check against thresholds.yaml, and the advisory tone band (committed, no client data)."""

from __future__ import annotations

import cv2
import numpy as np
import procedural as P
import pytest

from plate_gates import texture, tone


def _rgb(width: int, seed: int = 0, crunch: bool = False) -> np.ndarray:
    img = P.scene(width, seed)
    if crunch:
        img = P.crunch(img)
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def test_stats_accepts_uint8_uint16_and_float_and_records_the_width():
    rgb = _rgb(640)
    a = texture.stats(rgb, width=None)
    assert a["eval_width"] == 640
    b = texture.stats(rgb.astype(np.uint16) * 257, width=None)
    c = texture.stats(rgb.astype(np.float32) / 255.0, width=None)
    for k in ("lapvar", "fine_ratio", "luma_mean", "luma_std", "sat_mean"):
        assert b[k] == pytest.approx(a[k], rel=1e-4) and c[k] == pytest.approx(a[k], rel=1e-3)
    assert texture.stats(rgb, width=1024)["eval_width"] == 1024
    assert texture.texture_stats(rgb, width=1024)["lapvar"] == texture.stats(rgb, width=1024)["lapvar"]
    with pytest.raises(ValueError):
        texture.stats(np.zeros((4, 4), np.uint8))
    with pytest.raises(ValueError):
        texture.texture_stats(rgb.astype(np.float32))  # the seed name stays strict about uint8


def test_band_check_splits_hard_lapvar_from_calibration_needed_fine_ratio():
    inside = texture.band_check({"lapvar": 450.0, "fine_ratio": 0.5, "eval_width": 3840})
    assert inside["verdict"] == "pass" and inside["pass"] is True and inside["reasons"] == []
    crunchy = texture.band_check({"lapvar": 3500.0, "fine_ratio": 0.69, "eval_width": 3840})
    assert crunchy["verdict"] == "fail" and crunchy["pass"] is False
    assert crunchy["reasons"] == ["lapvar_high"] and crunchy["advisory_reasons"] == ["fine_high"]
    soft = texture.band_check({"lapvar": 120.0, "fine_ratio": 0.3, "eval_width": 3840})
    assert soft["reasons"] == ["lapvar_low"] and soft["advisory_reasons"] == ["fine_low"]
    fine_only = texture.band_check({"lapvar": 450.0, "fine_ratio": 0.9, "eval_width": 3840})
    assert fine_only["verdict"] == "advisory" and fine_only["pass"] is True
    assert [c["status"] for c in crunchy["checks"]] == ["fail", "advisory"]


def test_movie_grade_references_from_the_scout_pass_the_corrected_band():
    # Aparna t33.5 (lapvar 434, fine 0.53) and t46.0 (377, 0.45) measured under the current formula
    for lap, fine in ((434.0, 0.53), (377.0, 0.45)):
        assert (
            texture.band_check({"lapvar": lap, "fine_ratio": fine, "eval_width": 3840})["verdict"] == "pass"
        )
    # the rejected GPT still (3453, 0.69) fails
    assert texture.band_check({"lapvar": 3453.0, "fine_ratio": 0.69, "eval_width": 3840})["verdict"] == "fail"


def test_band_check_downgrades_to_advisory_off_the_band_resolution():
    res = texture.band_check({"lapvar": 3500.0, "fine_ratio": 0.9, "eval_width": 1024})
    assert res["verdict"] == "advisory" and res["pass"] is True
    assert res["reasons"] == [] and set(res["advisory_reasons"]) == {"lapvar_high", "fine_high"}
    assert "3840" in res["notes"][0] and "1024" in res["notes"][0]
    assert texture.band_check({"lapvar": 450.0, "fine_ratio": 0.5})["notes"] == []  # no eval_width recorded


def test_upscaled_content_reads_soft_at_the_band_resolution():
    # the band is valid for native-4K frames only: upsizing a 1024 px render collapses the Laplacian variance
    res = texture.band_check(texture.stats(_rgb(1024, crunch=True), width=3840))
    assert res["verdict"] == "fail" and res["reasons"] == ["lapvar_low"]


def test_crunched_scene_trips_the_hard_band_at_native_width():
    res = texture.band_check(texture.stats(_rgb(1024, crunch=True), width=None))
    assert res["notes"] and res["reasons"] == []  # evaluated at 1024: downgraded, not failed
    assert res["advisory_reasons"][0] == "lapvar_high"


def test_texture_band_defaults_come_from_the_threshold_file():
    assert texture.texture_band({"lapvar": 450.0, "fine_ratio": 0.6})["pass"] is True
    assert texture.texture_band({"lapvar": 450.0, "fine_ratio": 0.61})["reasons"] == ["fine_high"]
    assert texture.texture_band({"lapvar": 450.0, "fine_ratio": 0.38})["reasons"] == ["fine_low"]
    assert texture.texture_band({"lapvar": 450.0, "fine_ratio": 0.38}, fine=(0.35, 0.45))["pass"] is True


def test_tone_stats_match_texture_stats():
    rgb = _rgb(640)
    t = tone.tone_stats(rgb, width=None)
    s = texture.stats(rgb, width=None)
    assert t == {"luma": s["luma_mean"], "contrast": s["luma_std"], "sat": s["sat_mean"]}


def test_tone_check_is_advisory_only():
    inside = tone.tone_check({"luma": 110.0, "contrast": 56.0, "sat": 60.0})
    assert inside["verdict"] == "pass" and all(c["status"] == "pass" for c in inside["checks"])
    off = tone.tone_check({"luma": 140.0, "contrast": 40.0, "sat": 90.0})
    assert off["verdict"] == "advisory" and [c["status"] for c in off["checks"]] == ["advisory"] * 3
    assert [c["metric_id"] for c in off["checks"]] == ["tone.luma", "tone.contrast", "tone.sat"]
