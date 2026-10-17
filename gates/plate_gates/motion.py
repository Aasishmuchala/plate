"""Camera-lock and region-motion gates for a clip, built on phase correlation of luma regions.

Consolidates couple-dining/clip_gate.py (first-vs-last-frame shift per static region, "moved" only when every
region shifts) and the MOTION / CAMERA blocks of car-entry/tools_v4_gate.py (mean abs luma diff per region over
time). Regions are given in native clip pixels; the old hard-coded 3840x2160 region tables are parameters now.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .video import frame_at, probe

__all__ = ["phase_shift", "camera_lock", "region_motion"]

Box = tuple[int, int, int, int]  # (x, y, w, h) in native pixels


def phase_shift(a: np.ndarray, b: np.ndarray) -> tuple[int, int]:
    """Integer (dx, dy) such that rolling `a` by (dx along x, dy along y) reproduces `b`.

    Sign convention: the shift that maps `a` onto `b`, so content moving right between a and b gives dx > 0
    and content moving down gives dy > 0. Both inputs are mean-removed, Hanning-windowed 2-D float arrays;
    the normalised cross-power spectrum peak is wrapped to a signed offset.
    (couple-dining/clip_gate.py used conj(b), i.e. the opposite sign.)
    """
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    if a.ndim != 2 or a.shape != b.shape:
        raise ValueError(f"phase_shift needs two 2-D arrays of equal shape, got {a.shape} and {b.shape}")
    h, w = a.shape
    a = a - a.mean()
    b = b - b.mean()
    win = np.outer(np.hanning(h), np.hanning(w)).astype(np.float32)
    fa, fb = np.fft.fft2(a * win), np.fft.fft2(b * win)
    cross = np.conj(fa) * fb
    cross /= np.abs(cross) + 1e-6
    r = np.fft.ifft2(cross).real
    iy, ix = np.unravel_index(int(np.argmax(r)), r.shape)
    dy = int(iy) if iy <= h // 2 else int(iy) - h
    dx = int(ix) if ix <= w // 2 else int(ix) - w
    return dx, dy


def _luma(clip: Path, t_s: float) -> np.ndarray:
    return cv2.cvtColor(frame_at(clip, t_s), cv2.COLOR_RGB2GRAY).astype(np.float32)


def _crop(luma: np.ndarray, box: Box) -> np.ndarray:
    x, y, w, h = (int(v) for v in box)
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > luma.shape[1] or y + h > luma.shape[0]:
        raise ValueError(f"region {box} lies outside the {luma.shape[1]}x{luma.shape[0]} frame")
    return luma[y : y + h, x : x + w]


def camera_lock(
    clip: Path,
    regions: list[Box],
    t0: float = 0.0,
    t1: float | None = None,
    tol_px: int = 2,
) -> dict:
    """Phase-correlate each static region between t0 and t1 (default: 0.05 s before the end).

    verdict is "moved" only when every region shifted by more than tol_px on either axis (the clip_gate rule:
    one still region means the camera held); otherwise "locked". Shifts use phase_shift's sign convention.
    """
    clip = Path(clip)
    if not regions:
        raise ValueError("camera_lock needs at least one region")
    dur = probe(clip)["duration_s"]
    if t1 is None:
        t1 = max(dur - 0.05, 0.0)
    a, b = _luma(clip, t0), _luma(clip, t1)
    out = []
    moved = []
    for box in regions:
        ra, rb = _crop(a, box), _crop(b, box)
        dx, dy = phase_shift(ra, rb)
        mad = float(np.abs(ra - rb).mean() / 255.0)
        moved.append(abs(dx) > tol_px or abs(dy) > tol_px)
        out.append({"box": tuple(int(v) for v in box), "dx": dx, "dy": dy, "mad": mad})
    verdict = "moved" if all(moved) else "locked"
    return {"regions": out, "verdict": verdict, "duration_s": dur}


def region_motion(clip: Path, regions: dict[str, Box], times: list[float]) -> dict[str, list[float]]:
    """Mean abs luma diff /255 of each region at times[1:] against times[0] (list has len(times)-1 entries)."""
    clip = Path(clip)
    if len(times) < 2:
        raise ValueError("region_motion needs at least two times")
    base = _luma(clip, times[0])
    later = [_luma(clip, t) for t in times[1:]]
    out: dict[str, list[float]] = {}
    for name, box in regions.items():
        ref = _crop(base, box)
        out[name] = [float(np.abs(_crop(g, box) - ref).mean() / 255.0) for g in later]
    return out
