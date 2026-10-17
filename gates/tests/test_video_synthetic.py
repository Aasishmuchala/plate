"""probe / frame_at / find_tool on a synthetic 320x180 24 fps clip (no ffmpeg needed)."""

from __future__ import annotations

import numpy as np
import pytest

from plate_gates.video import ffprobe, find_tool, frame_at, probe, write_test_clip

W, H, FPS, N = 320, 180, 24, 48


def _frames() -> list[np.ndarray]:
    x = np.linspace(0, 255, W, dtype=np.float32)[None, :].repeat(H, axis=0)
    out = []
    for i in range(N):
        g = np.clip(x * 0.5 + i * 2.0, 0, 255).astype(np.uint8)
        out.append(np.stack([g, g, g], axis=-1))
    return out


@pytest.fixture()
def clip(tmp_path):
    return write_test_clip(tmp_path / "clip.mp4", _frames(), fps=FPS)


def test_probe_reports_geometry_and_timing(clip):
    info = probe(clip)
    assert info["width"] == W
    assert info["height"] == H
    assert abs(info["fps"] - FPS) < 0.5
    assert abs(info["duration_s"] - 2.0) < 0.1
    assert info["nb_frames"] == N


def test_frame_at_matches_the_written_frame(clip):
    frames = _frames()
    f0 = frame_at(clip, 0.0)
    assert f0.shape == (H, W, 3) and f0.dtype == np.uint8
    assert abs(float(f0.mean()) - float(frames[0].mean())) < 3
    # a middle frame by time: t = 1.0 s is frame 24
    mid = frame_at(clip, 1.0)
    assert abs(float(mid.mean()) - float(frames[24].mean())) < 3


def test_frame_at_clamps_past_the_end(clip):
    frames = _frames()
    last = frame_at(clip, 99.0)
    assert abs(float(last.mean()) - float(frames[-1].mean())) < 3


def test_has_audio_is_false_for_a_silent_clip(clip):
    if ffprobe() is None:
        pytest.skip("ffprobe not available")
    assert probe(clip)["has_audio"] is False


def test_has_audio_is_none_without_ffprobe(clip, monkeypatch):
    monkeypatch.setenv("PLATE_FFPROBE", "definitely-not-a-real-binary-name")
    assert probe(clip)["has_audio"] is None


def test_find_tool_env_override_wins_and_never_falls_through(tmp_path, monkeypatch):
    fake = tmp_path / "myffmpeg.exe"
    fake.write_bytes(b"")
    monkeypatch.setenv("PLATE_FFMPEG", str(fake))
    assert find_tool("ffmpeg", "PLATE_FFMPEG") == fake
    monkeypatch.setenv("PLATE_FFMPEG", str(tmp_path / "missing-binary"))
    assert find_tool("ffmpeg", "PLATE_FFMPEG") is None


def test_write_test_clip_rejects_empty_and_ragged_input(tmp_path):
    with pytest.raises(ValueError):
        write_test_clip(tmp_path / "a.mp4", [])
    with pytest.raises(ValueError):
        write_test_clip(
            tmp_path / "b.mp4", [np.zeros((10, 10, 3), np.uint8), np.zeros((12, 10, 3), np.uint8)]
        )
