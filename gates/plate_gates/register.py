"""Registration measurements for the gates: phase-correlation shifts and the post-shift edge residual.

The ingress transform (plate_pixels) corrects a generated layer's shift with ECC; this module is the gate-side
re-measurement that decides registration.shift_px (<= 8 hard) and registration.residual_px (<= 1.0 hard):

* phase_shift: integer (dx, dy) by normalised cross-power spectrum (shared with motion.camera_lock).
* subpixel_shift: the same peak refined to a fraction of a pixel (cv2.phaseCorrelate, Hann window).
* measure: global shift of a test image against the plate, then the RMS distance from the test's long
  structural edges to the plate's once that shift is removed. A pure translation leaves ~0 residual; local
  warps, scale and invented edges do not.

Sign convention everywhere: (dx, dy) is the translation that maps the first image onto the second, so content
moving right between a and b gives dx > 0 and content moving down gives dy > 0.
"""

from __future__ import annotations

import cv2
import numpy as np

from .io import to_uint8
from .structure import canny, long_edges

__all__ = ["phase_shift", "subpixel_shift", "measure"]


def phase_shift(a: np.ndarray, b: np.ndarray) -> tuple[int, int]:
    """Integer (dx, dy) such that rolling `a` by (dx along x, dy along y) reproduces `b`.

    Both inputs are mean-removed, Hanning-windowed 2-D float arrays; the normalised cross-power spectrum peak
    is wrapped to a signed offset. (couple-dining/clip_gate.py used conj(b), i.e. the opposite sign.)
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


def subpixel_shift(a: np.ndarray, b: np.ndarray) -> tuple[float, float, float]:
    """(dx, dy, response) with sub-pixel dx, dy by Hann-windowed phase correlation; response is the 0..1 peak."""
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    if a.ndim != 2 or a.shape != b.shape:
        raise ValueError(f"subpixel_shift needs two 2-D arrays of equal shape, got {a.shape} and {b.shape}")
    win = cv2.createHanningWindow((a.shape[1], a.shape[0]), cv2.CV_32F)
    (dx, dy), response = cv2.phaseCorrelate(a - a.mean(), b - b.mean(), win)
    return float(dx), float(dy), float(response)


def _gray(img: np.ndarray, width: int | None, size: tuple[int, int] | None) -> tuple[np.ndarray, float]:
    """float32 luma 0..255; resized to size=(w, h) if given, else to `width` wide. Returns (gray, scale to native)."""
    a = to_uint8(img)
    g = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY) if a.ndim == 3 else a
    native_w = g.shape[1]
    if size is not None and (g.shape[1], g.shape[0]) != size:
        g = cv2.resize(g, size, interpolation=cv2.INTER_AREA if size[0] < g.shape[1] else cv2.INTER_CUBIC)
    elif size is None and width and width < native_w:
        g = cv2.resize(
            g, (int(width), int(round(g.shape[0] * width / native_w))), interpolation=cv2.INTER_AREA
        )
    return g, native_w / g.shape[1]


def measure(ref_rgb: np.ndarray, test_rgb: np.ndarray, width: int = 960, capture_px: float = 8.0) -> dict:
    """Global shift and post-shift edge residual of `test_rgb` against `ref_rgb` (RGB uint8 / uint16 / float).

    Both images are evaluated at `width` (test resized to the plate's size first); results are reported in
    NATIVE plate pixels. Returns {dx, dy, shift_px, residual_px, matched_frac, backend}:

    * dx, dy: sub-pixel translation of the test relative to the plate (phase correlation);
    * shift_px: hypot(dx, dy), the quantity held to registration.shift_px (<= 8);
    * residual_px: RMS distance from each test long-edge pixel to the nearest plate long-edge pixel after
      removing (dx, dy), over pixels within capture_px; matched_frac is the share of test edge pixels that
      found a plate edge within capture_px (low means invented or missing structure, not misregistration).
    """
    ref_g, scale = _gray(ref_rgb, width, None)
    test_g, _ = _gray(test_rgb, None, (ref_g.shape[1], ref_g.shape[0]))
    dx, dy, _ = subpixel_shift(ref_g.astype(np.float32), test_g.astype(np.float32))
    # undo the shift: test(x) = ref(x - d)  =>  sample test at x + d
    m = np.float32([[1, 0, -dx], [0, 1, -dy]])
    aligned = cv2.warpAffine(
        test_g, m, (test_g.shape[1], test_g.shape[0]), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
    )
    er = long_edges(canny(ref_g), width=ref_g.shape[1])
    et = long_edges(canny(aligned), width=ref_g.shape[1])
    if er.sum() == 0 or et.sum() == 0:
        residual, matched = 0.0, 0.0
    else:
        dist = cv2.distanceTransform((~er).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)[et]
        near = dist <= capture_px
        matched = float(near.mean())
        residual = float(np.sqrt(np.mean(dist[near] ** 2))) if near.any() else float(capture_px)
    return {
        "dx": dx * scale,
        "dy": dy * scale,
        "shift_px": float(np.hypot(dx, dy) * scale),
        "residual_px": residual * scale,
        "matched_frac": matched,
        "backend": "phasecorr+edge-distance",
    }
