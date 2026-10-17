"""OCR text-lock: lettering in the plate is never regenerated, it is pasted back.

    masks(plate_rgb8, legend, crypto_object, ocr) -> bool (H, W)   what must stay the plate
    apply(plate16, out16, textmask)               -> out16         the output with those pixels restored

Generators rewrite signs and logos into plausible gibberish. The lock is the union of two sources:

* OCR boxes found in the plate (plate_pixels.ocr.OcrBackend), each dilated by 6 px so the halo and the
  anti-aliased glyph edges are inside it;
* every pixel of a signage-class object id (Tier A: the Cryptomatte object layer plus the legend's `class:
  signage` entries), even where OCR reads nothing, so a sign is locked as a whole.

apply() copies the plate's pixels inside the mask over the output, bit for bit; everything else in the output is
untouched. Locking more than the text is always safe (a locked pixel is a plate pixel); locking less is the
failure, so the thresholds below lean towards over-coverage.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from .masks import as_bool, rect
from .material_de import id_to_u32, legend_entries

__all__ = ["masks", "apply", "box_mask", "signage_ids", "DILATE_PX", "ID_COVERAGE"]

DILATE_PX = 6
ID_COVERAGE = 0.05  # a pixel with this much signage coverage is locked (anti-aliased sign edges included)


def box_mask(
    shape: tuple[int, int], boxes: Sequence[dict[str, Any]], dilate_px: int = DILATE_PX, min_conf: float = 0.0
) -> np.ndarray:
    """bool (H, W): union of the boxes' (x0, y0, x1, y1) rectangles grown by dilate_px on every side, clamped
    to the frame. Boxes under `min_conf` are ignored; corner order does not matter."""
    out = np.zeros((int(shape[0]), int(shape[1])), dtype=bool)
    grow = int(dilate_px)
    for box in boxes:
        if float(box.get("conf", 1.0)) < min_conf:
            continue
        xa, ya, xb, yb = (int(v) for v in box["bbox"])
        out |= rect(
            out.shape, (min(xa, xb) - grow, min(ya, yb) - grow, max(xa, xb) + grow, max(ya, yb) + grow)
        )
    return out


def signage_ids(legend: Any) -> list[int]:
    """32-bit Cryptomatte ids of the legend's object entries whose class is signage."""
    ids = []
    for entry in legend_entries(legend):
        if entry.get("class") == "signage" and entry.get("kind") in (None, "object"):
            ids.append(id_to_u32(entry["id"]))
    return ids


def masks(
    plate_rgb8: np.ndarray,
    legend: Any,
    crypto_object: tuple[np.ndarray, np.ndarray] | None,
    ocr: Any,
    *,
    dilate_px: int = DILATE_PX,
    min_conf: float = 0.0,
    id_coverage: float = ID_COVERAGE,
) -> np.ndarray:
    """bool (H, W) text-lock mask for a plate: OCR boxes dilated by `dilate_px`, union signage-class ids.

    `crypto_object` is the (ranks, coverage) pair of plate_pixels.exr.read_cryptomatte(..., "object") or None
    (Tier C has no Cryptomatte: the lock is then OCR only). `ocr` is an OcrBackend or None (no OCR boxes).
    """
    plate = np.asarray(plate_rgb8)
    if plate.dtype != np.uint8 or plate.ndim != 3 or plate.shape[2] != 3:
        raise ValueError(f"plate_rgb8 must be a uint8 RGB array (h, w, 3), got {plate.dtype} {plate.shape}")
    h, w = plate.shape[:2]
    lock = np.zeros((h, w), dtype=bool)
    if ocr is not None:
        lock |= box_mask((h, w), ocr.detect(plate), dilate_px, min_conf)
    if crypto_object is not None:
        ranks, cov = (np.asarray(a) for a in crypto_object)
        if ranks.ndim == 2:
            ranks, cov = ranks[..., None], cov[..., None]
        if ranks.shape[:2] != (h, w) or ranks.shape != cov.shape:
            raise ValueError(f"Cryptomatte shape {ranks.shape}/{cov.shape} does not match the plate {(h, w)}")
        ids = signage_ids(legend)
        if ids:
            share = np.where(np.isin(ranks, np.asarray(ids, dtype=np.uint32)), cov, 0.0).sum(axis=2)
            lock |= share >= id_coverage
    return lock


def apply(plate16: np.ndarray, out16: np.ndarray, textmask: np.ndarray) -> np.ndarray:
    """Copy of `out16` with the plate's pixels inside `textmask` restored (bit-exact). Inputs are not modified.

    plate16 and out16 must share one dtype and shape (the 16-bit sRGB pair of the compositor).
    """
    plate = np.asarray(plate16)
    out = np.asarray(out16)
    m = as_bool(textmask)
    if plate.shape != out.shape or plate.dtype != out.dtype or m.shape != plate.shape[:2]:
        raise ValueError(
            f"plate {plate.dtype}{plate.shape}, out {out.dtype}{out.shape} and mask {m.shape} must agree"
        )
    result = out.copy()
    result[m] = plate[m]
    return result
