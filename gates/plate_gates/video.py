"""Video plumbing for the gates: locate ffmpeg/ffprobe, probe a clip, grab a frame by time, write a test clip.

Consolidates the ffprobe/ffmpeg shell-outs that clip_gate.py, pushin_gate.py and tools_v4_gate.py each
repeated (seeds: couple-dining/clip_gate.py, couple-dining/pushin_gate.py, car-entry/tools_v4_gate.py in
the historical project folders). Probing and frame grabs go through OpenCV so the gates need no ffmpeg
binary; ffprobe is used only for the audio-stream question and is optional (PLATE_FFMPEG / PLATE_FFPROBE
override the PATH lookup; every call is an argument list, never a shell).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np

__all__ = ["find_tool", "ffmpeg", "ffprobe", "probe", "frame_at", "write_test_clip"]


def find_tool(name: str, env_var: str) -> Path | None:
    """Locate an external binary: the env override first, then PATH.

    An env value that does not resolve returns None; it never falls through to PATH (no silent substitution).
    """
    override = os.environ.get(env_var, "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
        found = shutil.which(override)
        return Path(found) if found else None
    found = shutil.which(name)
    return Path(found) if found else None


def ffmpeg() -> Path | None:
    return find_tool("ffmpeg", "PLATE_FFMPEG")


def ffprobe() -> Path | None:
    return find_tool("ffprobe", "PLATE_FFPROBE")


def _has_audio(clip: Path) -> bool | None:
    tool = ffprobe()
    if tool is None:
        return None
    cmd = [str(tool), "-v", "error", "-select_streams", "a:0"]
    cmd += ["-show_entries", "stream=codec_name", "-of", "csv=p=0", str(clip)]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        return None
    return bool(res.stdout.strip())


def _open(clip: Path) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(str(clip))
    if not cap.isOpened():
        cap.release()
        raise FileNotFoundError(f"cannot open video: {clip}")
    return cap


def _count_frames(clip: Path) -> int:
    cap = _open(clip)
    n = 0
    while cap.grab():
        n += 1
    cap.release()
    return n


def probe(clip: Path) -> dict:
    """Return width, height, fps, duration_s, nb_frames and has_audio (None when ffprobe is absent)."""
    clip = Path(clip)
    cap = _open(clip)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS))
    nb = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    if nb <= 0:  # some containers do not report a count: walk the stream
        nb = _count_frames(clip)
    duration = nb / fps if fps > 0 else 0.0
    return {
        "width": width,
        "height": height,
        "fps": fps,
        "duration_s": duration,
        "nb_frames": nb,
        "has_audio": _has_audio(clip),
    }


def frame_at(clip: Path, t_s: float) -> np.ndarray:
    """RGB uint8 frame at time t_s (frame index = round(t*fps), clamped to the last frame)."""
    clip = Path(clip)
    info = probe(clip)
    last = max(info["nb_frames"] - 1, 0)
    idx = min(max(int(round(t_s * info["fps"])), 0), last)
    cap = _open(clip)
    try:
        # Step back from the target if the container over-reports the count or the seek lands past EOF.
        for target in range(idx, -1, -1):
            cap.set(cv2.CAP_PROP_POS_FRAMES, target)
            ok, bgr = cap.read()
            if ok and bgr is not None:
                return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        raise ValueError(f"no decodable frame in {clip}")
    finally:
        cap.release()


def write_test_clip(path: Path, frames: list[np.ndarray], fps: float = 24) -> Path:
    """Write RGB uint8 frames to an mp4v file (OpenCV only; no ffmpeg binary needed). Returns the path."""
    if not frames:
        raise ValueError("write_test_clip needs at least one frame")
    path = Path(path)
    h, w = frames[0].shape[:2]
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), float(fps), (w, h))
    if not writer.isOpened():
        raise RuntimeError(f"cannot open VideoWriter for {path}")
    try:
        for f in frames:
            if f.shape[:2] != (h, w):
                raise ValueError("all frames must share one size")
            writer.write(cv2.cvtColor(np.ascontiguousarray(f), cv2.COLOR_RGB2BGR))
    finally:
        writer.release()
    return path
