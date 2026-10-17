"""texture_stats / texture_band against the committed plaque crops (car-entry lesson) and synthetic images."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from plate_gates.crops import load_rgb
from plate_gates.texture import texture_band, texture_stats

GOLDEN = Path(__file__).resolve().parent / "golden" / "texture"
VANTAGE = "g_tex_vantage_v1_t493_plaque.png"
GPT = "g_tex_gpt_s1a_plaque.png"

# Client-derived crops: gitignored, present only where the client projects are; skip elsewhere.
if not all((GOLDEN / n).is_file() for n in (VANTAGE, GPT)):
    pytest.skip("client-derived texture goldens absent (gitignored)", allow_module_level=True)


@pytest.fixture(scope="module")
def expected():
    return json.loads((GOLDEN / "expected_texture.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def stats():
    out = {}
    for name in (VANTAGE, GPT):
        rgb = np.asarray(load_rgb(GOLDEN / name))
        assert rgb.shape == (432, 768, 3)
        out[name] = texture_stats(rgb, width=None)
    return out


def test_ai_still_is_far_sharper_than_the_render(stats):
    # the car-entry lesson: GPT's plaque reads 3x+ the Laplacian variance of the Vantage render
    assert stats[GPT]["lapvar"] > 3 * stats[VANTAGE]["lapvar"]
    assert stats[GPT]["fine_ratio"] > stats[VANTAGE]["fine_ratio"]


def test_stats_match_frozen_values(stats, expected):
    for name, frozen in expected.items():
        for key in ("lapvar", "fine_ratio", "luma_mean", "luma_std", "sat_mean"):
            assert stats[name][key] == pytest.approx(frozen[key], rel=0.02), (name, key)


def test_measured_reference_values(stats):
    assert stats[VANTAGE]["lapvar"] == pytest.approx(923, rel=0.02)
    assert stats[VANTAGE]["fine_ratio"] == pytest.approx(0.474, rel=0.02)
    assert stats[GPT]["lapvar"] == pytest.approx(3528, rel=0.02)
    assert stats[GPT]["fine_ratio"] == pytest.approx(0.636, rel=0.02)


def test_gpt_plaque_fails_the_band_as_too_sharp(stats):
    res = texture_band(stats[GPT])
    assert res["pass"] is False
    assert "lapvar_high" in res["reasons"]
    assert "fine_high" in res["reasons"]


def test_smooth_gradient_fails_as_too_soft():
    ramp = np.tile(np.linspace(30, 220, 640, dtype=np.float32), (360, 1)).astype(np.uint8)
    res = texture_band(texture_stats(np.stack([ramp] * 3, axis=-1), width=None))
    assert res["pass"] is False
    assert "lapvar_low" in res["reasons"]


def _blurred_noise(sigma: float, lapvar_target: float) -> np.ndarray:
    """Noise blurred by sigma, centred on 128 and scaled so its Laplacian variance lands on the target."""
    rng = np.random.default_rng(5)
    n = cv2.GaussianBlur(rng.standard_normal((512, 512)).astype(np.float32), (0, 0), sigma)
    n -= n.mean()
    lap = float(cv2.Laplacian(n, cv2.CV_32F).var())
    n *= np.sqrt(lapvar_target / lap)
    g = np.clip(128 + n, 0, 255).astype(np.uint8)
    return np.stack([g, g, g], axis=-1)


def test_synthetic_image_inside_the_band_passes():
    # search the blur width: it sets the fine ratio, while the scale pins the Laplacian variance at 450
    chosen = None
    for sigma in np.arange(0.5, 3.01, 0.1):
        img = _blurred_noise(float(sigma), 450.0)
        s = texture_stats(img, width=None)
        if 350 <= s["lapvar"] <= 550 and 0.36 <= s["fine_ratio"] <= 0.44:
            chosen = (float(sigma), s)
            break
    assert chosen is not None, "no blur width landed inside lapvar 450+-100 and fine 0.40+-0.04"
    res = texture_band(chosen[1])
    assert res["pass"] is True and res["reasons"] == []


def test_band_reasons_cover_all_four_codes():
    assert texture_band({"lapvar": 100.0, "fine_ratio": 0.2})["reasons"] == ["lapvar_low", "fine_low"]
    assert texture_band({"lapvar": 900.0, "fine_ratio": 0.7})["reasons"] == ["lapvar_high", "fine_high"]
    assert texture_band({"lapvar": 450.0, "fine_ratio": 0.40})["pass"] is True
    assert texture_band({"lapvar": 450.0, "fine_ratio": 0.60})["pass"] is True  # corrected band: 0.40-0.60


def test_resize_to_width_changes_the_measurement():
    rgb = _blurred_noise(1.0, 450.0)
    native = texture_stats(rgb, width=None)
    up = texture_stats(rgb, width=1024)
    assert up["lapvar"] < native["lapvar"]  # upscaled content is softer
    with pytest.raises(ValueError):
        texture_stats(np.zeros((4, 4), np.uint8))
