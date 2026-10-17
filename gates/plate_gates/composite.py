"""Region composite: paste one rectangle of a patch into a base with a feathered edge.

Ported from car-entry/tools_composite_region.py (historical project folder). Everything outside the feathered
box stays the base pixel-for-pixel; this is the mask-locked compositing primitive (outside-mask delta = 0 by
construction) that the full mask pipeline later generalises. The CLI lives in tools_cli.py.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter

__all__ = ["composite_region"]


def composite_region(
    base_rgb8: np.ndarray,
    patch_rgb8: np.ndarray,
    box: tuple[int, int, int, int],
    feather_px: int = 60,
) -> np.ndarray:
    """Blend patch over base inside box=(x0, y0, x1, y1) in base pixels; returns uint8 of the base's shape.

    The patch is resized to the base with Lanczos if the sizes differ. The mask is the box rectangle blurred
    with a Gaussian of radius feather_px / 2 (feather_px = 0 gives a hard edge); the result is rounded with
    np.clip(res + 0.5, 0, 255).
    """
    base = np.asarray(base_rgb8)
    patch = np.asarray(patch_rgb8)
    if base.ndim != 3 or base.shape[2] != 3 or base.dtype != np.uint8:
        raise ValueError("base must be an RGB uint8 array of shape (h, w, 3)")
    h, w = base.shape[:2]
    if patch.shape[:2] != (h, w):
        patch = np.asarray(Image.fromarray(patch).resize((w, h), Image.LANCZOS))
    x0, y0, x1, y1 = (int(v) for v in box)
    m = Image.new("L", (w, h), 0)
    m.paste(255, (x0, y0, x1, y1))
    if feather_px > 0:
        m = m.filter(ImageFilter.GaussianBlur(feather_px / 2))
    a = np.asarray(m, np.float32)[..., None] / 255
    res = base.astype(np.float32) * (1 - a) + patch.astype(np.float32) * a
    return np.clip(res + 0.5, 0, 255).astype(np.uint8)
