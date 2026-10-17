"""Geometry-lock check: how well a relit/generated still keeps the plate's edges, plus a red/cyan overlay.

Consolidates amphi-plaza/align_check.py and the edge-agreement block of car-entry/tools_shot1_qc.py
(historical project folders). The plate path and the 1920 px evaluation width are parameters now. The score
is the share of the test image's edge pixels that land on the plate's edges (dilated by 1 px so a one-pixel
shift still counts); 1.0 means identical edges.

This is image-only agreement: ADVISORY (thresholds.yaml image_edge_agreement >= 0.88, triage only). The hard
edge-recall gate (gbuffer_edge_recall_r2 >= 0.90) exists only against G-buffer edges of a Tier A packet.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter, ImageOps

__all__ = ["edge_map", "edge_agreement"]

EDGE_THRESHOLD = 0.18


def _to_gray(im: Image.Image | np.ndarray) -> Image.Image:
    if isinstance(im, Image.Image):
        return im.convert("L")
    arr = np.asarray(im)
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    return Image.fromarray(arr).convert("L")


def _to_rgb(im: Image.Image | np.ndarray) -> Image.Image:
    if isinstance(im, Image.Image):
        return im.convert("RGB")
    arr = np.asarray(im)
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    return Image.fromarray(arr).convert("RGB")


def edge_map(gray_pil_or_ndarray: Image.Image | np.ndarray) -> np.ndarray:
    """Binary {0,1} float32 edge map: autocontrast, FIND_EDGES, min-max normalise, threshold at 0.18."""
    g = _to_gray(gray_pil_or_ndarray)
    e = np.asarray(ImageOps.autocontrast(g).filter(ImageFilter.FIND_EDGES)).astype(np.float32)
    e = (e - e.min()) / (e.max() - e.min() + 1e-6)
    return (e > EDGE_THRESHOLD).astype(np.float32)


def edge_agreement(
    plate: Image.Image | np.ndarray,
    relit: Image.Image | np.ndarray,
    width: int | None = 1920,
) -> tuple[float, np.ndarray]:
    """Return (agreement, overlay). Both images are brought to `width` (plate aspect; None keeps native size).

    overlay is an RGB uint8 array: red = plate edges only, cyan = relit edges only, white = both.
    """
    p = _to_rgb(plate)
    r = _to_rgb(relit)
    if width is not None and p.width != width:
        p = p.resize((int(width), max(int(round(width * p.height / p.width)), 1)), Image.LANCZOS)
    if r.size != p.size:
        r = r.resize(p.size, Image.LANCZOS)
    ep, er = edge_map(p), edge_map(r)
    epd = np.asarray(Image.fromarray((ep * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(3)))
    epd = epd.astype(np.float32) / 255
    agree = float((er * epd).sum() / max(float(er.sum()), 1.0))
    h, w = ep.shape
    overlay = np.zeros((h, w, 3), np.uint8)
    overlay[..., 0] = (ep * 255).astype(np.uint8)
    overlay[..., 1] = (er * 255).astype(np.uint8)
    overlay[..., 2] = (er * 255).astype(np.uint8)
    return agree, overlay
