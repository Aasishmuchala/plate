"""register.py: phase_shift sign, sub-pixel shift, and the measure() shift / residual split."""

from __future__ import annotations

import cv2
import numpy as np
import procedural as P
import pytest

from plate_gates import register


def _shift(img, dx, dy):
    m = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(
        img, m, (img.shape[1], img.shape[0]), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
    )


@pytest.fixture(scope="module")
def plate():
    return cv2.cvtColor(P.scene(640, 0), cv2.COLOR_BGR2RGB)


def test_phase_shift_sign_matches_motion_reexport():
    from plate_gates import motion

    assert motion.phase_shift is register.phase_shift
    rng = np.random.default_rng(1)
    a = rng.random((96, 96)).astype(np.float32)
    assert register.phase_shift(a, np.roll(a, (4, -2), axis=(0, 1))) == (-2, 4)


def test_subpixel_shift_recovers_fractions():
    rng = np.random.default_rng(2)
    a = cv2.GaussianBlur(rng.random((128, 128)).astype(np.float32), (0, 0), 3)
    b = _shift(a, 2.5, -1.25)
    dx, dy, resp = register.subpixel_shift(a, b)
    assert dx == pytest.approx(2.5, abs=0.35) and dy == pytest.approx(-1.25, abs=0.35) and resp > 0.3
    with pytest.raises(ValueError):
        register.subpixel_shift(np.zeros((8, 8), np.float32), np.zeros((8, 9), np.float32))


def test_measure_identity_is_zero(plate):
    r = register.measure(plate, plate.copy())
    assert r["shift_px"] == 0.0 and r["residual_px"] == 0.0 and r["matched_frac"] == 1.0
    assert r["backend"] == "phasecorr+edge-distance"


def test_pure_translation_is_all_shift_and_no_residual(plate):
    r = register.measure(plate, _shift(plate, 3, 0))
    assert r["shift_px"] == pytest.approx(3.0, abs=0.1) and r["dx"] == pytest.approx(3.0, abs=0.1)
    assert r["residual_px"] < 0.3
    big = register.measure(plate, _shift(plate, 9, 2))
    assert big["shift_px"] > 8  # camera drift: over the 8 px limit
    assert big["residual_px"] < 0.3


def test_scale_error_is_residual_not_shift(plate):
    zoomed = cv2.resize(plate, None, fx=1.02, fy=1.02, interpolation=cv2.INTER_CUBIC)[:360, :640]
    assert register.measure(plate, zoomed)["residual_px"] > 1.0


def test_mild_blur_is_within_one_pixel(plate):
    r = register.measure(plate, cv2.GaussianBlur(plate, (0, 0), 2.0))
    assert r["shift_px"] < 0.2 and r["residual_px"] <= 1.0


def test_results_are_in_native_pixels_regardless_of_evaluation_width(plate):
    big = cv2.resize(plate, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)  # 1280 wide
    r = register.measure(big, _shift(big, 6, 0), width=640)
    assert r["shift_px"] == pytest.approx(6.0, abs=0.4)


def test_measure_accepts_uint16_and_float_and_resizes_the_test(plate):
    small = cv2.resize(plate, (320, 180), interpolation=cv2.INTER_AREA)
    assert register.measure(plate, small)["shift_px"] < 0.5
    assert (
        register.measure(plate.astype(np.uint16) * 257, (plate / 255.0).astype(np.float32))["shift_px"] == 0.0
    )
