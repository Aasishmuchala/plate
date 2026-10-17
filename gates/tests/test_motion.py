"""phase_shift, camera_lock and region_motion on synthetic mp4v clips (320x180, 24 fps, 48 frames)."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from plate_gates.motion import camera_lock, phase_shift, region_motion
from plate_gates.video import write_test_clip

W, H, FPS, N = 320, 180, 24, 48


def _texture(width: int, height: int, seed: int = 7, sigma: float = 2.0) -> np.ndarray:
    """Smoothed noise in 40..215: survives mp4v compression better than raw noise."""
    rng = np.random.default_rng(seed)
    n = cv2.GaussianBlur(rng.random((height, width)).astype(np.float32), (0, 0), sigma)
    n = (n - n.min()) / (n.max() - n.min())
    g = (40 + 175 * n).astype(np.uint8)
    return np.stack([g, g, g], axis=-1)


def test_phase_shift_recovers_a_roll_with_the_documented_sign():
    rng = np.random.default_rng(0)
    a = rng.random((128, 128)).astype(np.float32)
    b = np.roll(np.roll(a, 5, axis=1), -3, axis=0)
    assert phase_shift(a, b) == (5, -3)  # the shift that maps a onto b
    assert phase_shift(b, a) == (-5, 3)
    assert phase_shift(a, a) == (0, 0)


def test_phase_shift_rejects_mismatched_shapes():
    with pytest.raises(ValueError):
        phase_shift(np.zeros((8, 8), np.float32), np.zeros((8, 9), np.float32))


@pytest.fixture(scope="module")
def clip_static_bg_moving_square(tmp_path_factory):
    bg = _texture(W, H)
    frames = []
    for i in range(N):
        f = bg.copy()
        x = 10 + int(i * (W - 50) / (N - 1))
        f[70:94, x : x + 24] = 255
        frames.append(f)
    return write_test_clip(tmp_path_factory.mktemp("motionA") / "a.mp4", frames, fps=FPS)


@pytest.fixture(scope="module")
def clip_whole_frame_translating(tmp_path_factory):
    canvas = _texture(W + 6, H, seed=11)
    frames = []
    for i in range(N):
        s = int(round(6 * i / (N - 1)))  # content drifts 6 px to the right over the clip
        frames.append(np.ascontiguousarray(canvas[:, 6 - s : 6 - s + W]))
    return write_test_clip(tmp_path_factory.mktemp("motionB") / "b.mp4", frames, fps=FPS)


def test_camera_lock_reports_locked_for_static_background(clip_static_bg_moving_square):
    regions = [(10, 4, 130, 60), (180, 120, 130, 56)]
    res = camera_lock(clip_static_bg_moving_square, regions)
    assert res["verdict"] == "locked"
    assert abs(res["duration_s"] - 2.0) < 0.1
    assert len(res["regions"]) == 2
    for r in res["regions"]:
        assert abs(r["dx"]) <= 1 and abs(r["dy"]) <= 1
        assert r["mad"] < 0.03


def test_camera_lock_reports_moved_when_every_region_shifts(clip_whole_frame_translating):
    regions = [(10, 4, 130, 60), (180, 4, 130, 60), (90, 110, 140, 66)]
    res = camera_lock(clip_whole_frame_translating, regions)
    assert res["verdict"] == "moved"
    for r in res["regions"]:
        assert abs(r["dx"] - 6) <= 1  # content moved right: positive dx (a -> b convention)
        assert abs(r["dy"]) <= 1


def test_two_still_regions_keep_the_verdict_locked(clip_static_bg_moving_square):
    res = camera_lock(clip_static_bg_moving_square, [(10, 4, 130, 60), (180, 120, 130, 56)], tol_px=2)
    assert res["verdict"] == "locked"
    assert not any(r["moved"] for r in res["regions"])


@pytest.fixture(scope="module")
def clip_left_half_slides(tmp_path_factory):
    """Right half of the frame is a static texture; the left half slides 8 px: only one region moves."""
    left = _texture(W + 8, H, seed=21)
    right = _texture(W, H, seed=22)
    frames = []
    for i in range(N):
        s = int(round(8 * i / (N - 1)))
        f = right.copy()
        f[:, : W // 2] = left[:, 8 - s : 8 - s + W // 2]
        frames.append(f)
    return write_test_clip(tmp_path_factory.mktemp("motionC") / "c.mp4", frames, fps=FPS)


def test_any_moving_region_makes_the_verdict_moved(clip_left_half_slides):
    # clip_gate.py demanded ALL regions move; one sliding region means the camera (or the content) moved
    regions = [(180, 10, 130, 60), (10, 10, 130, 60)]  # first static, second sliding
    res = camera_lock(clip_left_half_slides, regions)
    assert res["verdict"] == "moved"
    assert [r["moved"] for r in res["regions"]] == [False, True]


def test_camera_lock_phasecorr_samples_and_reports_max_shift(clip_left_half_slides):
    from plate_gates.video import camera_lock_phasecorr

    res = camera_lock_phasecorr(clip_left_half_slides, [(180, 10, 130, 60), (10, 10, 130, 60)], samples=6)
    assert res["verdict"] == "moved" and res["samples"] == 6 and res["backend"] == "phasecorr"
    still, sliding = res["regions"]
    assert still["max_shift_px"] <= 1 and 6 <= sliding["max_shift_px"] <= 9
    with pytest.raises(ValueError):
        camera_lock_phasecorr(clip_left_half_slides, [(0, 0, 50, 50)], samples=1)


def test_flat_regions_are_unknown_not_locked(tmp_path):
    flat = [np.full((H, W, 3), 128, np.uint8) for _ in range(12)]
    clip = write_test_clip(tmp_path / "flat.mp4", flat, fps=FPS)
    res = camera_lock(clip, [(10, 10, 100, 60)])
    assert res["verdict"] == "unknown" and res["regions"][0]["textured"] is False


def test_camera_lock_validates_regions(clip_static_bg_moving_square):
    with pytest.raises(ValueError):
        camera_lock(clip_static_bg_moving_square, [])
    with pytest.raises(ValueError):
        camera_lock(clip_static_bg_moving_square, [(300, 0, 100, 50)])


def test_region_motion_separates_moving_from_static(clip_static_bg_moving_square):
    regions = {"band": (0, 60, 320, 50), "sky": (10, 4, 130, 50)}
    res = region_motion(clip_static_bg_moving_square, regions, [0.0, 1.0, 1.9])
    assert set(res) == {"band", "sky"}
    assert len(res["band"]) == 2 and len(res["sky"]) == 2
    assert min(res["band"]) > 0.02
    assert max(res["sky"]) < 0.01


def test_region_motion_needs_two_times(clip_static_bg_moving_square):
    with pytest.raises(ValueError):
        region_motion(clip_static_bg_moving_square, {"a": (0, 0, 10, 10)}, [0.0])
