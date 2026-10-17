"""Mask-diff by ID: is everything outside the editable mask still the plate, bit for bit?

    outside_mask_check(plate16, out16, editable_mask) -> {de00_max_outside, identical_outside, feather_band_p99, ...}

The editable mask is the union of the Cryptomatte ids an edit may touch (see plate_pixels.exr.id_mask). The
compositor copies everything outside it from the plate by slicing (composite.composite_masked), so the
expected result is exactly 0: identical_outside is bit equality of the 16-bit arrays, not a tolerance, and
de00_max_outside is the worst CIEDE2000 over the outside pixels (0.0 when they are identical). A single leaked
pixel flips identical_outside to False.

feather_band_p99 is the 99th percentile of CIEDE2000 between plate and output over the feather band, the 12 px
ring INSIDE the mask boundary (the frame border does not count as outside, as in masks.feather_inside). It says
how far the output has departed from the plate by the time the feather is over; a seam-free, tone-matched edit
keeps it small. Thresholds: outside_mask.de00 = 0 and feather_band.de00_p99 <= 1.0 (thresholds.yaml, hard).

Both images are sRGB-encoded 16-bit (uint8 is promoted by x257, like composite._as_u16).
"""

from __future__ import annotations

import cv2
import numpy as np

from .color import ciede2000, srgb_to_lab
from .masks import as_bool

__all__ = ["outside_mask_check", "feather_band", "BAND_PX"]

BAND_PX = 12
_TO_8 = 255.0 / 65535.0


def _as_u16(img: np.ndarray, name: str) -> np.ndarray:
    a = np.asarray(img)
    if a.ndim != 3 or a.shape[2] != 3:
        raise ValueError(f"{name} must be an RGB array of shape (h, w, 3)")
    if a.dtype == np.uint16:
        return a
    if a.dtype == np.uint8:
        return a.astype(np.uint16) * 257
    raise ValueError(f"{name} must be uint16 (or uint8), got {a.dtype}")


def feather_band(mask: np.ndarray, band_px: float = BAND_PX) -> np.ndarray:
    """bool band INSIDE the mask: pixels at most band_px (Euclidean) from the nearest outside pixel."""
    m = as_bool(mask)
    if not m.any() or band_px <= 0:
        return np.zeros_like(m)
    padded = np.pad(m, 1, mode="edge")  # a mask running off the frame is not "outside" there
    d = cv2.distanceTransform(padded.astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)[1:-1, 1:-1]
    return m & (d <= float(band_px))


def _de00(a16: np.ndarray, b16: np.ndarray) -> np.ndarray:
    """CIEDE2000 between two (n, 3) uint16 sRGB pixel lists."""
    return ciede2000(srgb_to_lab(a16.astype(np.float64) * _TO_8), srgb_to_lab(b16.astype(np.float64) * _TO_8))


def outside_mask_check(
    plate16: np.ndarray, out16: np.ndarray, editable_mask: np.ndarray, band_px: float = BAND_PX
) -> dict:
    """Outside-the-mask identity and feather-band drift; see the module docstring.

    Returns de00_max_outside (float), identical_outside (bool), feather_band_p99 (float, 0.0 for an empty
    band), n_changed_outside (int) and n_band_px (int).
    """
    plate = _as_u16(plate16, "plate16")
    out = _as_u16(out16, "out16")
    m = as_bool(editable_mask)
    if plate.shape != out.shape or m.shape != plate.shape[:2]:
        raise ValueError(f"shape mismatch: plate {plate.shape}, out {out.shape}, mask {m.shape}")

    changed = np.any(plate != out, axis=-1)
    leak = changed & ~m
    n_leak = int(leak.sum())
    de_max = float(_de00(plate[leak], out[leak]).max()) if n_leak else 0.0

    band = feather_band(m, band_px)
    n_band = int(band.sum())
    p99 = 0.0
    if n_band and bool(changed[band].any()):
        p99 = float(np.percentile(_de00(plate[band], out[band]), 99))
    return {
        "de00_max_outside": de_max,
        "identical_outside": n_leak == 0,
        "feather_band_p99": p99,
        "n_changed_outside": n_leak,
        "n_band_px": n_band,
    }
