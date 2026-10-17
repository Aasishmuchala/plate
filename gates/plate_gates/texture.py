"""Texture gate: Laplacian variance and fine-detail ratio of a still, and the pass band they must sit in.

Consolidates the stats() helper of car-entry/tools_v4_gate.py (lapvar, fine ratio, luma mean/std, saturation),
which was evaluated at 3840 px wide against Corona/Vantage references: Lanczos to the evaluation width, then
the Laplacian variance of float32 luma (CV_32F, ksize 1), the fine-detail ratio
std(g - G(1.5)) / (std(g - G(6)) + 1e-6), mean luma, luma std (contrast) and mean HSV saturation.

The band comes from thresholds.yaml: lapvar 300-600 is hard (an AI still that reads 3x sharper than the render
has invented detail); the fine-ratio band 0.40-0.60 is calibration_needed (the plan's 0.35-0.45 contradicts the
formula: movie-grade refs measure 0.45-0.53). Both numbers are only valid at 3840 wide; band_check says so
instead of failing a still that was measured at another width.
"""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

from . import thresholds
from .io import to_uint8

__all__ = ["stats", "band_check", "texture_stats", "texture_band", "prepare_rgb"]


def prepare_rgb(rgb: np.ndarray) -> np.ndarray:
    """Accept uint8 / uint16 / float 0..1 (H, W, 3) and return uint8 (the formulas are defined on 8-bit luma)."""
    a = np.asarray(rgb)
    if a.ndim != 3 or a.shape[2] != 3:
        raise ValueError("texture stats expect an RGB array of shape (h, w, 3)")
    return to_uint8(a)


def stats(rgb: np.ndarray, width: int | None = 3840) -> dict:
    """lapvar, fine_ratio, luma_mean, luma_std, sat_mean (+ eval_width) of an RGB image.

    The image is resized to `width` (Lanczos, aspect kept) only when width is not None. lapvar is the
    Laplacian variance of float32 luma; fine_ratio = std(g - blur(1.5)) / std(g - blur(6.0)); sat_mean is the
    mean HSV saturation (0-255). eval_width records the width the numbers were measured at.
    """
    im = Image.fromarray(prepare_rgb(rgb))
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
        "eval_width": int(im.width),
    }


def band_check(st: dict) -> dict:
    """Score texture stats against thresholds.yaml.

    Returns {'verdict': 'pass'|'fail'|'advisory', 'pass': bool, 'reasons': [...], 'advisory_reasons': [...],
    'checks': [{metric_id, value, status}], 'notes': [...]}. reasons holds hard failures (lapvar_low /
    lapvar_high) and `pass` is False only for those; advisory_reasons holds fine_low / fine_high. When the stats
    carry an eval_width different from the band's 3840, every miss is downgraded to advisory and a note says why.
    """
    checks, reasons, advisory_reasons, notes = [], [], [], []
    ts = thresholds.load()
    off_resolution = False
    if "eval_width" in st:
        want = ts.get("texture.lapvar").resolution_px
        if want and int(st["eval_width"]) != int(want):
            off_resolution = True
            notes.append(f"texture band is valid at {want} px wide; evaluated at {st['eval_width']}")
    for metric_id, key, low, high in (
        ("texture.lapvar", "lapvar", "lapvar_low", "lapvar_high"),
        ("texture.fine_ratio", "fine_ratio", "fine_low", "fine_high"),
    ):
        status, thr = ts.check(metric_id, st[key])
        if status != "pass" and off_resolution:
            status = "advisory"
        checks.append({"metric_id": metric_id, "value": float(st[key]), "status": status})
        if status != "pass":
            lo, hi = thr.range  # type: ignore[misc]
            code = low if st[key] < lo else high
            (reasons if status == "fail" else advisory_reasons).append(code)
    verdict = "fail" if reasons else ("advisory" if advisory_reasons else "pass")
    return {
        "verdict": verdict,
        "pass": not reasons,
        "reasons": reasons,
        "advisory_reasons": advisory_reasons,
        "checks": checks,
        "notes": notes,
    }


def texture_stats(rgb8: np.ndarray, width: int | None = 3840) -> dict:
    """Seed name for stats(); strict about dtype: RGB uint8 of shape (h, w, 3) only."""
    a = np.asarray(rgb8)
    if a.ndim != 3 or a.shape[2] != 3 or a.dtype != np.uint8:
        raise ValueError("texture_stats expects an RGB uint8 array of shape (h, w, 3)")
    return stats(a, width=width)


def texture_band(
    st: dict,
    lapvar: tuple[float, float] | None = None,
    fine: tuple[float, float] | None = None,
) -> dict:
    """Check stats against an explicit band (default: the ranges in thresholds.yaml, regardless of width).

    reasons are drawn from lapvar_low, lapvar_high, fine_low, fine_high. Use band_check for the
    hard/advisory split and the resolution note.
    """
    ts = thresholds.load()
    lapvar = lapvar or ts.get("texture.lapvar").range
    fine = fine or ts.get("texture.fine_ratio").range
    reasons: list[str] = []
    if st["lapvar"] < lapvar[0]:
        reasons.append("lapvar_low")
    if st["lapvar"] > lapvar[1]:
        reasons.append("lapvar_high")
    if st["fine_ratio"] < fine[0]:
        reasons.append("fine_low")
    if st["fine_ratio"] > fine[1]:
        reasons.append("fine_high")
    return {"pass": not reasons, "reasons": reasons}
