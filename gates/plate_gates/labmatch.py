"""Reinhard colour transfer in CIE Lab: match the per-channel mean/std of a source image to a reference.

Ported from ~/Desktop/test/car-entry/tools_labmatch.py (the CLI wrapper became cli.py's job). Deterministic
grade transfer: no re-synthesis, geometry untouched. Uses color.srgb_to_lab / lab_to_srgb.

Std guard: a channel whose source or reference std is below 1e-3 is not rescaled (gain 1.0); its mean is still
moved to the reference, so a flat grey slab still lands on the reference level instead of exploding on a divide.

reinhard() can measure the statistics on a pixel selection (a mask, or one mask per image) while transferring to
every source pixel; the compositor uses that to match a generated layer to the unchanged plate ring.
"""

from __future__ import annotations

import numpy as np

from .color import lab_to_srgb, srgb_to_lab

__all__ = ["reinhard", "reinhard_lab", "lab_match", "STD_GUARD"]

STD_GUARD = 1e-3


def reinhard_lab(
    src_lab: np.ndarray,
    ref_lab: np.ndarray,
    strength: float = 1.0,
    src_sel: np.ndarray | None = None,
    ref_sel: np.ndarray | None = None,
) -> np.ndarray:
    """Per-channel mean/std transfer of Lab `src_lab` toward `ref_lab`; float64 Lab of src's shape.

    src_sel / ref_sel (bool, shape of the respective image) restrict which pixels feed the statistics; the
    transfer is applied to all of src. strength blends src (0) to the full transfer (1).
    """
    out = src_lab.astype(np.float64, copy=True)
    ss = src_lab[src_sel] if src_sel is not None else src_lab.reshape(-1, 3)
    rs = ref_lab[ref_sel] if ref_sel is not None else ref_lab.reshape(-1, 3)
    if len(ss) == 0 or len(rs) == 0:
        raise ValueError("reinhard: empty pixel selection for the statistics")
    for c in range(3):
        ms, sd_s = float(ss[:, c].mean()), float(ss[:, c].std())
        mr, sd_r = float(rs[:, c].mean()), float(rs[:, c].std())
        gain = sd_r / sd_s if (sd_s >= STD_GUARD and sd_r >= STD_GUARD) else 1.0
        matched = (src_lab[..., c] - ms) * gain + mr
        out[..., c] = src_lab[..., c] * (1 - strength) + matched * strength
    return out


def reinhard(
    src_rgb8: np.ndarray,
    ref_rgb8: np.ndarray,
    strength: float = 1.0,
    mask: np.ndarray | tuple[np.ndarray | None, np.ndarray | None] | None = None,
) -> np.ndarray:
    """Return `src_rgb8` graded toward `ref_rgb8` (both RGB uint8, any size) as RGB uint8.

    strength 0 returns the source (to within rounding), 1 is the full per-channel mean/std transfer.
    mask: None uses every pixel of both images; one bool array (same shape as BOTH images, or as src when ref
    is the same size) selects the pixels the statistics are read from; a (src_mask, ref_mask) tuple gives each
    image its own selection. The result always covers every source pixel.
    """
    ls = srgb_to_lab(src_rgb8)
    lr = srgb_to_lab(ref_rgb8)
    if isinstance(mask, tuple):
        src_sel, ref_sel = mask
    else:
        src_sel = ref_sel = mask
    res = lab_to_srgb(reinhard_lab(ls, lr, strength, _bool(src_sel), _bool(ref_sel)))
    return np.clip(res + 0.5, 0, 255).astype(np.uint8)


def _bool(m: np.ndarray | None) -> np.ndarray | None:
    return None if m is None else np.asarray(m).astype(bool)


def lab_match(src_rgb8: np.ndarray, ref_rgb8: np.ndarray, strength: float = 1.0) -> np.ndarray:
    """Seed name for reinhard() without a mask."""
    return reinhard(src_rgb8, ref_rgb8, strength)
