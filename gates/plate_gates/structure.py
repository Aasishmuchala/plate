"""Structural similarity and edge metrics: SSIM, Canny, long-edge filtering, tolerance IoU, best shift.

Ported from the structure section of ~/Desktop/test/vizmaker-dossier/tools/fidelity_bench.py.
Kernels, sigmas and constants are unchanged; the only edit is that long_edges keeps components with
a lookup table instead of one full-image compare per component (identical output, much faster).
"""

from __future__ import annotations

import cv2
import numpy as np

LONG = 30  # px of connected edge length considered a structural edge at the evaluation width


def ssim_gray(a: np.ndarray, b: np.ndarray) -> float:
    """Gaussian-window SSIM (11x11, sigma 1.5, K1 .01, K2 .03) on two grayscale images."""
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    C1 = (0.01 * 255) ** 2
    C2 = (0.03 * 255) ** 2
    k = (11, 11)
    mu1 = cv2.GaussianBlur(a, k, 1.5)
    mu2 = cv2.GaussianBlur(b, k, 1.5)
    s11 = cv2.GaussianBlur(a * a, k, 1.5) - mu1**2
    s22 = cv2.GaussianBlur(b * b, k, 1.5) - mu2**2
    s12 = cv2.GaussianBlur(a * b, k, 1.5) - mu1 * mu2
    m = ((2 * mu1 * mu2 + C1) * (2 * s12 + C2)) / ((mu1**2 + mu2**2 + C1) * (s11 + s22 + C2))
    return float(m.mean())


def canny(gray: np.ndarray, sigma: float = 1.2) -> np.ndarray:
    """Median-adaptive Canny edge map (bool). Thresholds are floored so flat images do not explode."""
    g = cv2.GaussianBlur(gray, (0, 0), sigma)
    med = np.median(g)
    lo = int(max(0, 0.66 * med))
    hi = int(min(255, 1.33 * med))
    lo = max(lo, 20)
    hi = max(hi, lo * 2 + 10)
    return cv2.Canny(g, lo, hi) > 0


def long_edges(e: np.ndarray, minlen: int = LONG) -> np.ndarray:
    """Keep only 8-connected edge components with at least `minlen` pixels."""
    n, lab, stats, _ = cv2.connectedComponentsWithStats(e.astype(np.uint8), connectivity=8)
    good = stats[:, cv2.CC_STAT_AREA] >= minlen
    good[0] = False  # label 0 is background
    return good[lab]


def tol_metrics(er: np.ndarray, et: np.ndarray, r: int) -> dict:
    """IoU / recall / precision of test edges `et` against reference edges `er` within r px (r=0 strict)."""
    if r == 0:
        inter = (er & et).sum()
        uni = (er | et).sum()
        return dict(
            iou=float(inter / max(uni, 1)),
            recall=float(inter / max(er.sum(), 1)),
            precision=float(inter / max(et.sum(), 1)),
        )
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    dr = cv2.dilate(er.astype(np.uint8), k) > 0
    dt = cv2.dilate(et.astype(np.uint8), k) > 0
    rec = (er & dt).sum() / max(er.sum(), 1)
    prec = (et & dr).sum() / max(et.sum(), 1)
    # symmetric tolerance-IoU: matched ref + matched test over union count
    mr = (er & dt).sum()
    mt = (et & dr).sum()
    iou = (mr + mt) / 2 / max(((er.sum() + et.sum()) - (mr + mt) / 2), 1)
    return dict(iou=float(iou), recall=float(rec), precision=float(prec))


def best_shift(er: np.ndarray, et: np.ndarray, rng: int = 8, r: int = 2) -> dict:
    """(dx, dy) within +-rng px that maximises edge recall at tolerance r (global-misalignment probe)."""
    best = (-1.0, 0, 0)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    dt = cv2.dilate(et.astype(np.uint8), k) > 0
    for dy in range(-rng, rng + 1):
        for dx in range(-rng, rng + 1):
            M = np.float32([[1, 0, dx], [0, 1, dy]])
            s = cv2.warpAffine(dt.astype(np.uint8), M, (dt.shape[1], dt.shape[0])) > 0
            rec = (er & s).sum() / max(er.sum(), 1)
            if rec > best[0]:
                best = (float(rec), dx, dy)
    return dict(recall_r2=best[0], dx=best[1], dy=best[2])
