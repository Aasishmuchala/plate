"""Mask primitives for the compositor and the gates: erode, rectangle, inside-feather, surrounding ring.

Masks come in as bool, 0/1 or 0/255 uint8, or float alpha (>= 0.5 is inside) and are normalised once by
as_bool. The feather is computed INSIDE the mask (a distance transform of the mask itself), so alpha is
exactly 0 outside it and outside-mask pixels can be copied from the plate untouched (plan decision 4).
"""

from __future__ import annotations

import cv2
import numpy as np

__all__ = ["as_bool", "erode", "rect", "feather_inside", "ring"]


def as_bool(mask: np.ndarray) -> np.ndarray:
    """Any mask encoding -> bool (H, W). uint8: > 127; other ints: > 0; floats: >= 0.5; bool: unchanged."""
    m = np.asarray(mask)
    if m.ndim == 3:
        m = m[..., 0]
    if m.dtype == bool:
        return m
    if m.dtype == np.uint8:
        return m > 127 if m.max(initial=0) > 1 else m > 0
    if np.issubdtype(m.dtype, np.floating):
        return m >= 0.5
    return m > 0


def erode(mask: np.ndarray, px: int = 3) -> np.ndarray:
    """Shrink the mask by px pixels (square kernel, matches compare.erode_mask). Returns bool, may be empty."""
    m = as_bool(mask)
    if px <= 0:
        return m.copy()
    k = np.ones((2 * int(px) + 1, 2 * int(px) + 1), np.uint8)
    return cv2.erode(m.astype(np.uint8), k) > 0


def rect(shape: tuple[int, int], box: tuple[int, int, int, int]) -> np.ndarray:
    """bool (H, W) mask that is True inside box=(x0, y0, x1, y1) (x1, y1 exclusive), clamped to the frame."""
    h, w = int(shape[0]), int(shape[1])
    x0, y0, x1, y1 = (int(v) for v in box)
    m = np.zeros((h, w), bool)
    m[max(y0, 0) : max(min(y1, h), 0), max(x0, 0) : max(min(x1, w), 0)] = True
    return m


def feather_inside(mask: np.ndarray, px: float) -> np.ndarray:
    """float32 alpha: 0 outside the mask, ramping linearly to 1 over px pixels INSIDE it, 1 deeper in.

    Alpha at an inside pixel is clip((d - 0.5) / px, 0, 1) with d the Euclidean distance to the nearest
    outside pixel, so the ramp starts at the mask edge (half a pixel out from the first inside pixel's centre).
    The frame border does not count as outside: a mask running off the image is not feathered there.
    px <= 0 gives the hard mask. Alpha is exactly 0.0 wherever the mask is False.
    """
    m = as_bool(mask)
    if px <= 0:
        return m.astype(np.float32)
    padded = np.pad(m, 1, mode="edge")  # replicate the border so it is not read as "outside"
    d = cv2.distanceTransform(padded.astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)[1:-1, 1:-1]
    alpha = np.clip((d - 0.5) / float(px), 0.0, 1.0).astype(np.float32)
    alpha[~m] = 0.0
    return alpha


def ring(mask: np.ndarray, inner_px: float = 0, outer_px: float = 24) -> np.ndarray:
    """bool band OUTSIDE the mask: pixels farther than inner_px and at most outer_px from it.

    inner_px = 0 starts right at the mask edge. This is the unchanged-plate neighbourhood the tone match reads.
    """
    m = as_bool(mask)
    if outer_px <= inner_px:
        raise ValueError("ring needs outer_px > inner_px")
    if not m.any():
        return np.zeros_like(m)
    d = cv2.distanceTransform((~m).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    return (~m) & (d > float(inner_px)) & (d <= float(outer_px))
