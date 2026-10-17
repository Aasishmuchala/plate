"""Texture gate: Laplacian variance and fine-detail ratio of a still, and the pass band they must sit in.

Consolidates the stats() helper of car-entry/tools_v4_gate.py (lapvar, fine ratio, luma mean/std, saturation),
which was evaluated at 3840 px wide against Corona/Vantage references. The band (lapvar 300-600, fine ratio
0.35-0.45) is the render-crispness window from the car-entry lesson: an AI still that reads 3x sharper than
the render has invented detail. Thresholds live in thresholds.py; the defaults here mirror them.
"""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

__all__ = ["texture_stats", "texture_band"]


def texture_stats(rgb8: np.ndarray, width: int | None = 3840) -> dict:
    """lapvar, fine_ratio, luma_mean, luma_std, sat_mean of an RGB uint8 image.

    The image is resized to `width` (Lanczos, aspect kept) only when width is not None. lapvar is the
    Laplacian variance of float32 luma; fine_ratio = std(g - blur(1.5)) / std(g - blur(6.0)); sat_mean is the
    mean HSV saturation (0-255).
    """
    rgb8 = np.asarray(rgb8)
    if rgb8.ndim != 3 or rgb8.shape[2] != 3 or rgb8.dtype != np.uint8:
        raise ValueError("texture_stats expects an RGB uint8 array of shape (h, w, 3)")
    im = Image.fromarray(rgb8)
    if width is not None and im.width != width:
        im = im.resize((int(width), max(int(round(width * im.height / im.width)), 1)), Image.LANCZOS)
    g = np.asarray(im.convert("L"), dtype=np.float32)
    lapvar = float(cv2.Laplacian(g, cv2.CV_32F).var())
    hp = g - cv2.GaussianBlur(g, (0, 0), 1.5)
    bp = g - cv2.GaussianBlur(g, (0, 0), 6.0)
    fine = float(hp.std() / (bp.std() + 1e-6))
    hsv = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2HSV)
    return {
        "lapvar": lapvar,
        "fine_ratio": fine,
        "luma_mean": float(g.mean()),
        "luma_std": float(g.std()),
        "sat_mean": float(hsv[..., 1].mean()),
    }


def texture_band(
    stats: dict,
    lapvar: tuple[float, float] = (300.0, 600.0),
    fine: tuple[float, float] = (0.35, 0.45),
) -> dict:
    """Check stats against the band. reasons are drawn from lapvar_low, lapvar_high, fine_low, fine_high."""
    reasons: list[str] = []
    if stats["lapvar"] < lapvar[0]:
        reasons.append("lapvar_low")
    if stats["lapvar"] > lapvar[1]:
        reasons.append("lapvar_high")
    if stats["fine_ratio"] < fine[0]:
        reasons.append("fine_low")
    if stats["fine_ratio"] > fine[1]:
        reasons.append("fine_high")
    return {"pass": not reasons, "reasons": reasons}
