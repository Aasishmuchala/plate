"""Compositing: the mask-locked 16-bit composite (composite_masked) and the legacy rectangle paste.

composite_masked is the ingress compositor of plan decision 4: the generated layer lands on the untouched plate
only inside the mask, with the feather INSIDE it (masks.feather_inside) and a numeric Lab tone match computed on
the unchanged plate ring. Outside the mask the output is the plate, copied by slicing, so outside-mask dE00 is
0 by construction.

composite_region is the older tools_composite_region.py port (rectangle, Gaussian feather that bleeds past the
box); it stays for the CLI and is retired from the pipeline. The CLI lives in tools_cli.py.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter

from .color import lab_to_srgb, srgb_to_lab
from .labmatch import reinhard_lab
from .masks import as_bool, feather_inside, ring

__all__ = ["composite_masked", "match_to_ring", "composite_region"]

_MIN_RING_PX = 64  # fewer ring pixels than this and the tone statistics are noise: skip the match


def _as_u16(img: np.ndarray, name: str) -> np.ndarray:
    a = np.asarray(img)
    if a.ndim != 3 or a.shape[2] != 3:
        raise ValueError(f"{name} must be an RGB array of shape (h, w, 3)")
    if a.dtype == np.uint16:
        return a
    if a.dtype == np.uint8:
        return a.astype(np.uint16) * 257
    raise ValueError(f"{name} must be uint16 (or uint8), got {a.dtype}")


def match_to_ring(plate16: np.ndarray, ai16: np.ndarray, mask: np.ndarray, ring_px: float = 24) -> np.ndarray:
    """The AI layer with a Reinhard Lab mean/std match applied, measured on the ring of ring_px pixels just
    outside the mask (the unchanged plate ring against the AI image's same pixels). uint16, plate's shape.

    Returns `ai16` unchanged (as uint16) when the ring is too small to measure (a mask that fills the frame).
    Only the mask + ring bounding box is converted, so 4K frames stay cheap.
    """
    plate = _as_u16(plate16, "plate16")
    ai = _as_u16(ai16, "ai16")
    m = as_bool(mask)
    if plate.shape != ai.shape or m.shape != plate.shape[:2]:
        raise ValueError(f"shape mismatch: plate {plate.shape}, ai {ai.shape}, mask {m.shape}")
    band = ring(m, 0, ring_px)
    if int(band.sum()) < _MIN_RING_PX:
        return ai
    ys, xs = np.nonzero(m | band)
    sl = (slice(ys.min(), ys.max() + 1), slice(xs.min(), xs.max() + 1))
    to8 = 255.0 / 65535.0
    lab_plate = srgb_to_lab(plate[sl].astype(np.float64) * to8)
    lab_ai = srgb_to_lab(ai[sl].astype(np.float64) * to8)
    sel = band[sl]
    matched = lab_to_srgb(reinhard_lab(lab_ai, lab_plate, 1.0, sel, sel)) / to8
    out = ai.copy()
    out[sl] = np.clip(np.rint(matched), 0, 65535).astype(np.uint16)
    return out


def composite_masked(
    plate16: np.ndarray,
    ai16: np.ndarray,
    mask: np.ndarray,
    feather_px: float = 8,
    tone_match_ring: bool = True,
    ring_px: float = 24,
) -> np.ndarray:
    """Composite `ai16` over `plate16` inside `mask`; returns uint16 RGB of the plate's shape.

    * alpha = masks.feather_inside(mask, feather_px): 0 outside, ramping to 1 over feather_px pixels inside;
    * tone_match_ring: the AI layer is first matched to the plate on the ring just outside the mask
      (match_to_ring); skipped when the ring is too small;
    * pixels outside the mask are copied from the plate, never blended.

    Both images are sRGB-encoded 16-bit (uint8 is promoted by x257) and must share one shape with the mask.
    """
    plate = _as_u16(plate16, "plate16")
    ai = _as_u16(ai16, "ai16")
    m = as_bool(mask)
    if plate.shape != ai.shape or m.shape != plate.shape[:2]:
        raise ValueError(f"shape mismatch: plate {plate.shape}, ai {ai.shape}, mask {m.shape}")
    out = plate.copy()
    if not m.any():
        return out
    ai_src = match_to_ring(plate, ai, m, ring_px) if tone_match_ring else ai
    a = feather_inside(m, feather_px)[m][:, None]
    blended = plate[m].astype(np.float32) * (1.0 - a) + ai_src[m].astype(np.float32) * a
    out[m] = np.clip(np.rint(blended), 0, 65535).astype(np.uint16)
    return out


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
