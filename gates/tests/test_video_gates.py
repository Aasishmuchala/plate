"""landing (aspect-correct), landing_legacy, duration_check and flicker on small synthetic clips."""

from __future__ import annotations

import cv2
import numpy as np
import procedural as P
import pytest
from PIL import Image

from plate_gates import io as pio
from plate_gates import video

needs_ffmpeg = pytest.mark.skipif(
    pio.ffmpeg() is None or pio.ffprobe() is None, reason="ffmpeg / ffprobe not found"
)

W, H = 320, 180


def _still43(seed: int) -> np.ndarray:
    return cv2.cvtColor(P.scene(480, seed, aspect=4 / 3), cv2.COLOR_BGR2RGB)


def _clip_from(start: np.ndarray, end: np.ndarray, tmp_path, name="push.mp4", n=12, fps=24):
    f0, f1 = P._fit_43_to_169(start, W, H, 0.0), P._fit_43_to_169(end, W, H, 0.0)
    frames = [
        np.clip(f0.astype(np.float32) * (1 - t) + f1.astype(np.float32) * t, 0, 255).astype(np.uint8)
        for t in np.linspace(0, 1, n)
    ]
    return pio.write_clip(tmp_path / name, frames, fps=fps), frames


@needs_ffmpeg
def test_landing_is_near_zero_when_the_clip_is_the_stills_cropped_to_its_aspect(tmp_path):
    start, end = _still43(0), _still43(1)
    clip, _ = _clip_from(start, end, tmp_path)
    res = video.landing(clip, start, end)
    assert set(res) == {"first", "last", "last_vs_start"}
    # residual ~1.4 levels is the cv2-area vs PIL-Lanczos resampling difference, not a landing error
    assert res["first"] < 2.5 and res["last"] < 2.5
    assert res["last_vs_start"] > 3 * res["last"]


@needs_ffmpeg
def test_landing_is_aspect_correct_where_the_legacy_squash_is_not(tmp_path):
    start, end = _still43(0), _still43(1)
    clip, _ = _clip_from(start, end, tmp_path)
    fixed = video.landing(clip, start, end)
    squashed = video.landing_legacy(clip, start, end)  # reads the mp4 through OpenCV
    assert set(squashed) == {"first", "last", "last_vs_start"}
    assert squashed["first"] > 5 * fixed["first"]  # squash bias: a 4:3 still forced into 16:9


@needs_ffmpeg
def test_landing_accepts_paths_pil_images_and_16bit_arrays(tmp_path):
    start, end = _still43(0), _still43(1)
    clip, _ = _clip_from(start, end, tmp_path)
    Image.fromarray(start).save(tmp_path / "s.png")
    Image.fromarray(end).save(tmp_path / "e.png")
    by_path = video.landing(clip, tmp_path / "s.png", str(tmp_path / "e.png"))
    by_pil = video.landing(clip, Image.fromarray(start), Image.fromarray(end))
    by_u16 = video.landing(clip, start.astype(np.uint16) * 257, end.astype(np.float32) / 255.0)
    for other in (by_pil, by_u16):
        for k in by_path:
            assert other[k] == pytest.approx(by_path[k], abs=0.05)


@needs_ffmpeg
def test_landing_without_an_end_still_leaves_last_empty(tmp_path):
    start, end = _still43(0), _still43(1)
    clip, _ = _clip_from(start, end, tmp_path)
    res = video.landing(clip, start)
    assert res["last"] is None and res["first"] < 2.5 and res["last_vs_start"] > 3


@needs_ffmpeg
def test_landing_reads_the_actual_last_frame(tmp_path):
    frames = [np.full((90, 160, 3), v, np.uint8) for v in (10, 10, 10, 200)]
    clip = pio.write_clip(tmp_path / "x.mp4", frames, fps=24)
    res = video.landing(clip, np.full((90, 160, 3), 10, np.uint8), np.full((90, 160, 3), 200, np.uint8))
    assert res["first"] < 1.5 and res["last"] < 1.5  # the 200 frame is the last one, not the one before it


@needs_ffmpeg
def test_duration_check_against_the_shot_length(tmp_path):
    frames = [np.zeros((90, 160, 3), np.uint8)] * 48
    clip = pio.write_clip(tmp_path / "two.mp4", frames, fps=24)
    ok = video.duration_check(clip, 2.0)
    assert ok["ok"] is True and ok["duration_s"] == pytest.approx(2.0, abs=0.01) and ok["nb_frames"] == 48
    assert ok["fps"] == "24/1" and ok["required_s"] == 2.0
    short = video.duration_check(clip, 2.5)
    assert short["ok"] is False


@needs_ffmpeg
def test_flicker_flat_ramp_and_pumping(tmp_path):
    base = np.full((90, 160, 3), 100, np.uint8)
    steady = pio.write_clip(tmp_path / "steady.mp4", [base] * 16, fps=24)
    ramp = pio.write_clip(
        tmp_path / "ramp.mp4", [np.full((90, 160, 3), 100 + i, np.uint8) for i in range(16)], fps=24
    )
    pump = pio.write_clip(
        tmp_path / "pump.mp4",
        [np.full((90, 160, 3), 100 + (12 if i % 2 else 0), np.uint8) for i in range(16)],
        fps=24,
    )
    s, r, p = video.flicker(steady), video.flicker(ramp), video.flicker(pump)
    assert set(s) == {"mean_abs_luma_delta", "p95", "frames"} and s["frames"] == 16
    assert s["mean_abs_luma_delta"] < 0.1 and s["p95"] < 0.1
    assert r["mean_abs_luma_delta"] == pytest.approx(1.0, abs=0.2)
    assert p["mean_abs_luma_delta"] == pytest.approx(12.0, abs=0.7) and p["p95"] > 11


@needs_ffmpeg
def test_flicker_single_frame_clip(tmp_path):
    clip = pio.write_clip(tmp_path / "one.mp4", [np.zeros((90, 160, 3), np.uint8)], fps=24)
    assert video.flicker(clip) == {"mean_abs_luma_delta": 0.0, "p95": 0.0, "frames": 1}


def test_fit_still_centre_crops_to_the_clip_aspect_then_resizes():
    still = np.zeros((300, 400, 3), np.uint8)  # 4:3
    still[:, :50] = 255  # a white band on the left edge: must be cropped away only when the clip is narrower
    fit = video._fit_still(still, (320, 180))
    assert fit.shape == (180, 320, 3)
    assert fit[:, :10].mean() > 200  # 4:3 -> 16:9 trims top and bottom, keeps the full width
    tall = video._fit_still(
        np.dstack([np.tile(np.arange(100, dtype=np.uint8)[:, None], (1, 400))] * 3), (100, 100)
    )
    assert tall.shape == (100, 100, 3)
