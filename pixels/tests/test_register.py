"""ECC registration: sign convention, sub-pixel accuracy, coarse-to-fine, drift, ring masks, honest failure."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from plate_pixels import register


def _field(h: int, w: int, seed: int) -> np.ndarray:
    """A deterministic textured gray image in [0.1, 0.9]: fine and coarse noise plus hard-edged blocks."""
    rng = np.random.default_rng(seed)
    f = cv2.GaussianBlur(rng.random((h, w)).astype(np.float32), (0, 0), 2.0)
    f += 3 * cv2.GaussianBlur(rng.random((h, w)).astype(np.float32), (0, 0), 9.0)
    f = (f - f.min()) / (f.max() - f.min())
    for _ in range(12):
        x, y = int(rng.integers(0, w - 40)), int(rng.integers(0, h - 40))
        f[y : y + int(rng.integers(10, 40)), x : x + int(rng.integers(10, 40))] = float(rng.random())
    return (0.1 + 0.8 * f).astype(np.float32)


def _shifted_pair(dx: float, dy: float, h: int = 360, w: int = 640, seed: int = 1):
    """(plate, result) where the result's content moved by (+dx, +dy) relative to the plate."""
    big = _field(h + 80, w + 80, seed)
    plate = big[40 : 40 + h, 40 : 40 + w]
    if float(dx).is_integer() and float(dy).is_integer():
        result = big[40 - int(dy) : 40 - int(dy) + h, 40 - int(dx) : 40 - int(dx) + w]
    else:
        m = np.float32([[1, 0, -dx], [0, 1, -dy]])  # inverse map: result(x) = big(x - d)
        moved = cv2.warpAffine(big, m, big.shape[::-1], flags=cv2.INTER_LANCZOS4 | cv2.WARP_INVERSE_MAP)
        result = moved[40 : 40 + h, 40 : 40 + w]
    return np.ascontiguousarray(plate), np.ascontiguousarray(result)


def test_identity_registers_with_zero_shift():
    plate, _ = _shifted_pair(0, 0)
    dx, dy, residual, ok = register.ecc(plate, plate.copy())
    assert ok
    assert abs(dx) < 0.01 and abs(dy) < 0.01
    assert residual is not None and residual < 0.05


@pytest.mark.parametrize(("dx", "dy"), [(3, 0), (0, -3), (3, -2), (-2, 1)])
def test_integer_shift_recovered_with_sign(dx, dy):
    plate, result = _shifted_pair(dx, dy)
    rx, ry, residual, ok = register.ecc(plate, result)
    assert rx == pytest.approx(dx, abs=0.1)
    assert ry == pytest.approx(dy, abs=0.1)
    assert ok and residual is not None and residual <= 1.0


def test_subpixel_shift_recovered():
    plate, result = _shifted_pair(1.5, -0.75)
    rx, ry, _, ok = register.ecc(plate, result)
    assert ok
    assert rx == pytest.approx(1.5, abs=0.1)
    assert ry == pytest.approx(-0.75, abs=0.1)


def test_nine_px_is_camera_drift_not_ok():
    plate, result = _shifted_pair(9, 0)
    reg = register.estimate(plate, result)
    assert reg.dx == pytest.approx(9.0, abs=0.1)
    assert reg.shift_px > 8.0
    assert reg.camera_drift
    assert not reg.ok


def test_coarse_to_fine_on_a_wide_plate():
    """2048 wide: ECC runs on a 1024-wide downscale, then refines at full resolution within +/-2 px."""
    plate, result = _shifted_pair(-1.5, 2.25, h=1152, w=2048, seed=2)
    reg = register.estimate(plate, result)
    assert reg.refined and reg.method == "ecc_coarse_fine"
    assert reg.dx == pytest.approx(-1.5, abs=0.1)
    assert reg.dy == pytest.approx(2.25, abs=0.1)
    assert reg.ok and reg.residual_px <= 1.0


def test_relit_result_still_registers():
    """ECC is gain/offset invariant: a gamma-and-gain relight does not move the estimate."""
    plate, result = _shifted_pair(2, 0)
    relit = np.clip(result**1.3 * 0.8 + 0.05, 0, 1)
    rx, ry, residual, ok = register.ecc(plate, relit)
    assert ok
    assert rx == pytest.approx(2.0, abs=0.1) and abs(ry) < 0.1


def test_ring_mask_keeps_the_edit_out():
    plate, result = _shifted_pair(2, 0)
    edited = result.copy()
    edited[100:300, 200:500] = np.random.default_rng(1).random((200, 300)).astype(np.float32)
    ring = np.ones(plate.shape, bool)
    ring[90:310, 190:510] = False
    reg = register.estimate(plate, edited, ring_mask=ring)
    assert reg.ok
    assert reg.dx == pytest.approx(2.0, abs=0.05)


def test_local_warp_fails_on_residual():
    """Half the frame moved 4 px more than the other half: no global shift explains it."""
    plate, result = _shifted_pair(2, 0)
    _, right = _shifted_pair(6, 0)
    warped = result.copy()
    warped[:, 320:] = right[:, 320:]
    reg = register.estimate(plate, warped)
    assert reg.residual_px is not None and reg.residual_px > 1.0
    assert not reg.ok and not reg.camera_drift


def test_uncorrelated_result_is_not_registered_and_does_not_raise():
    plate, _ = _shifted_pair(0, 0)
    noise = np.random.default_rng(5).random(plate.shape).astype(np.float32)
    reg = register.estimate(plate, noise)
    assert not reg.ok
    assert reg.residual_px is None or reg.residual_px > 1.0


def test_inputs_are_not_mutated():
    """cv2 5.x phaseCorrelate windows its inputs in place; the module must pass copies."""
    plate, result = _shifted_pair(3, 0)
    p0, r0 = plate.copy(), result.copy()
    register.estimate(plate, result)
    assert np.array_equal(plate, p0) and np.array_equal(result, r0)


def test_rgb_and_integer_inputs_accepted():
    plate, result = _shifted_pair(3, 0)
    p16 = np.round(np.repeat(plate[..., None], 3, axis=2) * 65535).astype(np.uint16)
    r8 = np.round(np.repeat(result[..., None], 3, axis=2) * 255).astype(np.uint8)
    rx, _, _, ok = register.ecc(p16, r8)
    assert ok and rx == pytest.approx(3.0, abs=0.1)


def test_euclidean_motion_on_a_pure_shift():
    plate, result = _shifted_pair(2, 1)
    rx, ry, _, ok = register.ecc(plate, result, motion="euclidean")
    assert ok
    assert rx == pytest.approx(2.0, abs=0.15) and ry == pytest.approx(1.0, abs=0.15)


def test_bad_arguments_raise():
    plate, result = _shifted_pair(0, 0)
    with pytest.raises(ValueError):
        register.ecc(plate, result[:-1])
    with pytest.raises(ValueError):
        register.ecc(plate, result, motion="homography")
    with pytest.raises(ValueError):
        register.ecc(plate, result, ring_mask=np.ones((10, 10), bool))


def test_empty_ring_is_reported_not_guessed():
    plate, result = _shifted_pair(0, 0)
    reg = register.estimate(plate, result, ring_mask=np.zeros(plate.shape, bool))
    assert not reg.ok and reg.method == "no_ring" and reg.residual_px is None


def test_report_is_json_safe():
    import json

    plate, result = _shifted_pair(3, 0)
    json.dumps(register.estimate(plate, result).to_dict(), allow_nan=False)
