"""io.py: float sRGB loading (8/16-bit PNG, TIFF, JPG, gray, RGBA), ffprobe/ffmpeg probing and frame pipes."""

from __future__ import annotations

from fractions import Fraction

import cv2
import numpy as np
import pytest
import tifffile

from plate_gates import io as pio

needs_ffmpeg = pytest.mark.skipif(
    pio.ffmpeg() is None or pio.ffprobe() is None, reason="ffmpeg / ffprobe not found"
)


def _ramp(h: int = 24, w: int = 40) -> np.ndarray:
    x = np.linspace(0, 1, w, dtype=np.float32)[None, :, None].repeat(h, 0).repeat(3, 2)
    x[..., 1] = 1 - x[..., 1]
    return x


def _write_png(path, arr, bgr=True):
    ok, buf = cv2.imencode(".png", cv2.cvtColor(arr, cv2.COLOR_RGB2BGR) if (bgr and arr.ndim == 3) else arr)
    assert ok
    path.write_bytes(buf.tobytes())


def test_load_8bit_png_is_rgb_float_0_1(tmp_path):
    rgb8 = (_ramp() * 255).round().astype(np.uint8)
    _write_png(tmp_path / "a.png", rgb8)
    out = pio.load_rgb(tmp_path / "a.png")
    assert out.dtype == np.float32 and out.shape == (24, 40, 3)
    assert np.allclose(out, rgb8 / 255.0, atol=1e-6)
    assert out[0, 0, 0] == 0.0 and out[0, -1, 0] == 1.0  # R rises left to right: channel order survived


def test_load_16bit_png_keeps_precision(tmp_path):
    rgb16 = (_ramp() * 65535).round().astype(np.uint16)
    _write_png(tmp_path / "a16.png", rgb16)
    out = pio.load_rgb(tmp_path / "a16.png")
    assert np.allclose(out, rgb16 / 65535.0, atol=1e-6)
    assert len(np.unique(out[0, :, 0])) == 40  # more than 8-bit could resolve on a 40-step ramp


def test_load_tiff16_and_float_tiff(tmp_path):
    rgb16 = (_ramp() * 65535).round().astype(np.uint16)
    tifffile.imwrite(str(tmp_path / "a.tif"), rgb16)
    assert np.allclose(pio.load_rgb(tmp_path / "a.tif"), rgb16 / 65535.0, atol=1e-6)
    tifffile.imwrite(str(tmp_path / "f.tiff"), _ramp() * 1.5, photometric="rgb")  # float, clipped to 0..1
    assert pio.load_rgb(tmp_path / "f.tiff").max() == 1.0
    planar = np.moveaxis((_ramp() * 255).astype(np.uint8), -1, 0)
    tifffile.imwrite(str(tmp_path / "planar.tif"), planar, photometric="rgb", planarconfig="separate")
    assert pio.load_rgb(tmp_path / "planar.tif").shape == (24, 40, 3)


def test_load_jpg_gray_and_rgba(tmp_path):
    rgb8 = (_ramp(48, 64) * 255).astype(np.uint8)
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb8, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 95])
    (tmp_path / "a.jpg").write_bytes(buf.tobytes())
    assert np.abs(pio.load_rgb(tmp_path / "a.jpg") - rgb8 / 255.0).mean() < 0.02
    gray = (np.linspace(0, 255, 30, dtype=np.uint8)[None, :]).repeat(10, 0)
    _write_png(tmp_path / "g.png", gray)
    g = pio.load_rgb(tmp_path / "g.png")
    assert g.shape == (10, 30, 3) and np.array_equal(g[..., 0], g[..., 2])
    rgba = np.dstack([rgb8, np.full(rgb8.shape[:2], 7, np.uint8)])
    ok, buf = cv2.imencode(".png", cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))
    (tmp_path / "rgba.png").write_bytes(buf.tobytes())
    assert pio.load_rgb(tmp_path / "rgba.png").shape == (48, 64, 3)


def test_load_resizes_to_width_keeping_aspect(tmp_path):
    _write_png(tmp_path / "a.png", (_ramp(60, 100) * 255).astype(np.uint8))
    assert pio.load_rgb(tmp_path / "a.png", width=50).shape == (30, 50, 3)
    assert pio.load_rgb(tmp_path / "a.png", width=200).shape == (120, 200, 3)
    assert pio.load_rgb(tmp_path / "a.png", width=100).shape == (60, 100, 3)


def test_load_reads_non_ascii_paths_and_rejects_missing_or_bad_files(tmp_path):
    d = tmp_path / "café テスト"
    d.mkdir()
    _write_png(d / "a.png", (_ramp() * 255).astype(np.uint8))
    assert pio.load_rgb(d / "a.png").shape == (24, 40, 3)
    with pytest.raises(FileNotFoundError):
        pio.load_rgb(tmp_path / "nope.png")
    (tmp_path / "junk.png").write_bytes(b"not an image")
    with pytest.raises(ValueError):
        pio.load_rgb(tmp_path / "junk.png")


def test_to_uint8_rounds_and_clamps():
    assert pio.to_uint8(np.array([0.0, 0.5, 1.0, 1.2, -0.2], np.float32)).tolist() == [0, 128, 255, 255, 0]
    assert pio.to_uint8(np.array([0, 257, 65535], np.uint16)).tolist() == [0, 1, 255]
    a = np.array([1, 2], np.uint8)
    assert pio.to_uint8(a) is a


def test_find_tool_env_override_wins_and_never_falls_through(tmp_path, monkeypatch):
    fake = tmp_path / "myffmpeg.exe"
    fake.write_bytes(b"")
    monkeypatch.setenv("PLATE_FFMPEG", str(fake))
    assert pio.find_tool("ffmpeg", "PLATE_FFMPEG") == fake
    monkeypatch.setenv("PLATE_FFMPEG", str(tmp_path / "missing-binary"))
    assert pio.find_tool("ffmpeg", "PLATE_FFMPEG") is None


def test_missing_ffmpeg_raises_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATE_FFMPEG", str(tmp_path / "missing-binary"))
    with pytest.raises(FileNotFoundError, match="PLATE_FFMPEG"):
        pio.write_clip(tmp_path / "x.mp4", [np.zeros((8, 8, 3), np.uint8)])


def _clip(tmp_path, values=(10, 60, 110, 160), fps=24, name="c.mp4"):
    frames = []
    for v in values:
        f = np.full((90, 160, 3), v, np.uint8)
        f[:, :40, 0] = 255 - v
        frames.append(f)
    return pio.write_clip(tmp_path / name, frames, fps=fps), frames


@needs_ffmpeg
def test_probe_returns_exact_rational_fps_and_geometry(tmp_path):
    clip, _ = _clip(tmp_path, fps=24)
    info = pio.probe(clip)
    assert info["fps"] == Fraction(24, 1) and isinstance(info["fps"], Fraction)
    assert (info["width"], info["height"], info["nb_frames"]) == (160, 90, 4)
    assert info["duration_s"] == pytest.approx(4 / 24, abs=1e-3)
    assert info["codec"] == "h264"
    ntsc, _ = _clip(tmp_path, fps=Fraction(24000, 1001), name="ntsc.mp4")
    assert pio.probe(ntsc)["fps"] == Fraction(24000, 1001)


@needs_ffmpeg
def test_probe_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        pio.probe(tmp_path / "missing.mp4")


@needs_ffmpeg
def test_frame_at_is_an_exact_pipe_read(tmp_path):
    clip, frames = _clip(tmp_path)
    for i in range(4):
        got = pio.frame_at(clip, i / 24.0)
        assert got.shape == (90, 160, 3) and got.dtype == np.uint8
        assert np.abs(got.astype(int) - frames[i]).max() <= 2  # lossless RGB->YUV444 rounding only
    assert not list(tmp_path.glob("*.png"))  # no temp files


@needs_ffmpeg
def test_frame_at_past_the_end_raises(tmp_path):
    clip, _ = _clip(tmp_path)
    with pytest.raises(ValueError):
        pio.frame_at(clip, 9.0)


@needs_ffmpeg
def test_iter_gray_frames_streams_every_frame_scaled(tmp_path):
    clip, _ = _clip(tmp_path)
    full = list(pio.iter_gray_frames(clip))
    assert len(full) == 4 and full[0].shape == (90, 160) and full[0].dtype == np.uint8
    small = list(pio.iter_gray_frames(clip, width=80))
    assert len(small) == 4 and small[0].shape == (44, 80)
    means = [float(f.mean()) for f in full]
    assert means == sorted(means)


@needs_ffmpeg
def test_write_clip_validates_input(tmp_path):
    with pytest.raises(ValueError):
        pio.write_clip(tmp_path / "a.mp4", [])
    with pytest.raises(ValueError):
        pio.write_clip(tmp_path / "b.mp4", [np.zeros((8, 8, 3), np.uint8), np.zeros((8, 10, 3), np.uint8)])
    with pytest.raises(ValueError):
        pio.write_clip(tmp_path / "c.mp4", [np.zeros((8, 8, 3), np.float32)])
