"""Reinhard colour transfer in CIE Lab: match the per-channel mean/std of a source image to a reference.

Ported from ~/Desktop/test/car-entry/tools_labmatch.py (the CLI wrapper became cli.py's job). Deterministic
grade transfer: no re-synthesis, geometry untouched. Uses colour.srgb_to_lab / lab_to_srgb. A flat source
channel (zero std) is shifted to the reference mean instead of dividing by zero.
"""

from __future__ import annotations

import numpy as np

from .color import lab_to_srgb, srgb_to_lab


def lab_match(src_rgb8: np.ndarray, ref_rgb8: np.ndarray, strength: float = 1.0) -> np.ndarray:
    """Return `src_rgb8` graded toward `ref_rgb8` (both RGB uint8, any size) as RGB uint8.

    strength 0 returns the source (to within rounding), 1 is the full per-channel mean/std transfer.
    """
    ls = srgb_to_lab(src_rgb8)
    lr = srgb_to_lab(ref_rgb8)
    out = ls.copy()
    for c in range(3):
        ms, ss = ls[..., c].mean(), ls[..., c].std()
        mr, sr = lr[..., c].mean(), lr[..., c].std()
        gain = sr / ss if ss > 1e-9 else 1.0
        matched = (ls[..., c] - ms) * gain + mr
        out[..., c] = ls[..., c] * (1 - strength) + matched * strength
    res = lab_to_srgb(out)
    return np.clip(res + 0.5, 0, 255).astype(np.uint8)
