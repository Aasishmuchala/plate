"""Sub-pixel registration of an engine result to its plate: OpenCV ECC, coarse to fine (BUILD_PLAN decision 4).

``estimate`` aligns ``result`` to ``plate`` (both grayscale, same size). ECC runs on a 1024-wide downscale,
started from zero and from a phase-correlation guess (whichever converges with the higher correlation wins),
then is refined at full resolution; the refinement is kept only if it stays within +/-2 px of the coarse
answer. ECC is invariant to gain and offset, so a relit result still registers.

Sign convention: ``(dx, dy)`` is how far the result's content moved relative to the plate (content shifted
3 px to the right gives dx = +3). ``aligned(x, y) = result(x + dx, y + dy)`` puts it back on the plate.

``residual_px`` is what a global warp fails to explain: after alignment, the plate and the result are split
into textured tiles, each tile's leftover shift is measured by phase correlation, and the residual is the
median tile shift in full-resolution pixels. ``ok`` means residual <= 1.0 px and shift <= 8.0 px; a larger
shift is camera drift. ``ring_mask`` (True = pixels expected unchanged) restricts ECC and the tiles to the
ring, so the edited region cannot pull the estimate. Registration uses ECC and phase correlation only (no
learned matchers: the licence registry keeps those out).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

__all__ = [
    "COARSE_WIDTH",
    "DRIFT_MAX_PX",
    "REFINE_WINDOW_PX",
    "RESIDUAL_MAX_PX",
    "Registration",
    "ecc",
    "estimate",
    "to_gray",
    "warp_to_plate",
]

COARSE_WIDTH = 1024
REFINE_WINDOW_PX = 2.0
RESIDUAL_MAX_PX = 1.0
DRIFT_MAX_PX = 8.0

_MOTIONS = {
    "translation": cv2.MOTION_TRANSLATION,
    "euclidean": cv2.MOTION_EUCLIDEAN,
    "affine": cv2.MOTION_AFFINE,
}
_GAUSS_FILT = 5
_TILE_MIN_STD = 0.01  # tiles flatter than this (in [0, 1] units) carry no shift information
_TILE_MIN_RESPONSE = 0.15
_RING_TILE_COVER = 0.9
_MAX_TILES = 256
_MIN_RING_PX = 64


@dataclass
class Registration:
    """Full registration result; ``as_tuple()`` is the ``(dx, dy, residual_px, ok)`` that ``ecc`` returns."""

    dx: float
    dy: float
    residual_px: float | None
    ok: bool
    shift_px: float
    camera_drift: bool
    converged: bool
    cc: float | None
    method: str
    refined: bool
    tiles: int
    residual_p90_px: float | None
    # 2x3 at full resolution, mapping plate coordinates into the result (cv2 WARP_INVERSE_MAP).
    warp: list[list[float]] = field(default_factory=list)

    def as_tuple(self) -> tuple[float, float, float | None, bool]:
        return self.dx, self.dy, self.residual_px, self.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "dx": self.dx,
            "dy": self.dy,
            "shift_px": self.shift_px,
            "residual_px": self.residual_px,
            "residual_p90_px": self.residual_p90_px,
            "ok": self.ok,
            "camera_drift": self.camera_drift,
            "converged": self.converged,
            "ecc_cc": self.cc,
            "method": self.method,
            "refined": self.refined,
            "tiles": self.tiles,
            "warp": self.warp,
        }


def to_gray(img: np.ndarray) -> np.ndarray:
    """float32 (H, W) in [0, 1]: Rec.709 luma weights on display values; uint8/uint16/float accepted."""
    a = np.asarray(img)
    if a.dtype == np.uint8:
        f = a.astype(np.float32) / 255.0
    elif a.dtype == np.uint16:
        f = a.astype(np.float32) / 65535.0
    elif np.issubdtype(a.dtype, np.floating):
        f = a.astype(np.float32)
    else:
        raise ValueError(f"unsupported image dtype {a.dtype}")
    if f.ndim == 3:
        if f.shape[2] == 1:
            f = f[..., 0]
        else:
            f = f[..., 0] * 0.2126 + f[..., 1] * 0.7152 + f[..., 2] * 0.0722
    if f.ndim != 2:
        raise ValueError(f"expected an (H, W) or (H, W, C) image, got {a.shape}")
    return np.ascontiguousarray(f, dtype=np.float32)


def _criteria(max_iter: int, eps: float) -> tuple[int, int, float]:
    return (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, int(max_iter), float(eps))


def _run_ecc(
    template: np.ndarray,
    moving: np.ndarray,
    init: np.ndarray,
    motion: int,
    criteria: tuple[int, int, float],
    mask: np.ndarray | None,
) -> tuple[float, np.ndarray] | None:
    warp = init.astype(np.float32).copy()  # findTransformECC writes into the warp it is given
    try:
        cc, warp = cv2.findTransformECC(template, moving, warp, motion, criteria, mask, _GAUSS_FILT)
    except cv2.error:
        return None
    if not np.isfinite(warp).all() or not math.isfinite(cc):
        return None
    return float(cc), warp


def _phase(template: np.ndarray, moving: np.ndarray) -> tuple[float, float, float]:
    """(dx, dy, response) by phase correlation. cv2 5.x windows its inputs in place: always pass copies."""
    h, w = template.shape
    win = cv2.createHanningWindow((w, h), cv2.CV_32F)
    (sx, sy), resp = cv2.phaseCorrelate(template.copy(), moving.copy(), win)
    return float(sx), float(sy), float(resp)


def _translation(dx: float, dy: float) -> np.ndarray:
    return np.array([[1.0, 0.0, dx], [0.0, 1.0, dy]], np.float32)


def _scale_warp(warp: np.ndarray, s: float) -> np.ndarray:
    """A coarse-level warp (coordinates x * s) expressed at full resolution: the linear part is unchanged,
    the translation divides by s."""
    out = warp.astype(np.float64).copy()
    out[:, 2] /= s
    return out.astype(np.float32)


def warp_to_plate(img: np.ndarray, warp: np.ndarray, interpolation: int = cv2.INTER_LANCZOS4) -> np.ndarray:
    """Resample ``img`` onto the plate grid: ``out(x) = img(warp . x)``, edges replicated (never reflected)."""
    h, w = img.shape[:2]
    return cv2.warpAffine(
        img,
        np.asarray(warp, np.float32),
        (w, h),
        flags=interpolation | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _tile_residuals(
    plate: np.ndarray, aligned: np.ndarray, ring: np.ndarray | None, margin: int
) -> np.ndarray:
    """Leftover shift magnitude (pixels) of every textured tile, by phase correlation, at most ~256 tiles."""
    H, W = plate.shape
    t = int(min(128, max(32, min(H, W) // 3)))
    n = max(1, ((H - 2 * margin) // t) * ((W - 2 * margin) // t))
    stride = t * max(1, math.ceil(math.sqrt(n / _MAX_TILES)))
    vals = []
    for y in range(margin, H - margin - t + 1, stride):
        for x in range(margin, W - margin - t + 1, stride):
            p = plate[y : y + t, x : x + t]
            if float(p.std()) < _TILE_MIN_STD:
                continue
            if ring is not None and float(ring[y : y + t, x : x + t].mean()) < _RING_TILE_COVER:
                continue
            sx, sy, resp = _phase(p, aligned[y : y + t, x : x + t])
            if resp < _TILE_MIN_RESPONSE:
                continue
            vals.append(math.hypot(sx, sy))
    return np.asarray(vals, np.float64)


def estimate(
    plate_gray: np.ndarray,
    result_gray: np.ndarray,
    motion: str = "translation",
    max_iter: int = 200,
    eps: float = 1e-6,
    ring_mask: np.ndarray | None = None,
    *,
    coarse_width: int = COARSE_WIDTH,
    refine_window: float = REFINE_WINDOW_PX,
    residual_max: float = RESIDUAL_MAX_PX,
    drift_max: float = DRIFT_MAX_PX,
) -> Registration:
    """Register ``result_gray`` to ``plate_gray`` (see the module docstring). Never raises on a failed fit:
    a non-converging ECC returns ``ok=False`` with the phase-correlation shift as the best guess."""
    if motion not in _MOTIONS:
        raise ValueError(f"motion must be one of {sorted(_MOTIONS)}, got {motion!r}")
    plate = to_gray(plate_gray)
    result = to_gray(result_gray)
    if plate.shape != result.shape:
        raise ValueError(f"plate {plate.shape} and result {result.shape} must be the same size")
    H, W = plate.shape
    ring = None
    if ring_mask is not None:
        ring = np.asarray(ring_mask)
        if ring.ndim == 3:
            ring = ring[..., 0]
        if ring.shape != plate.shape:
            raise ValueError(f"ring_mask {ring.shape} must match the plate {plate.shape}")
        ring = ring > (0.5 if np.issubdtype(ring.dtype, np.floating) else 0)
        if int(ring.sum()) < _MIN_RING_PX:
            return Registration(0.0, 0.0, None, False, 0.0, False, False, None, "no_ring", False, 0, None)

    mtype = _MOTIONS[motion]
    crit = _criteria(max_iter, eps)

    # Coarse level.
    s = min(1.0, coarse_width / W)
    if s < 1.0:
        size = (max(16, int(round(W * s))), max(16, int(round(H * s))))
        s = size[0] / W
        pc = cv2.resize(plate, size, interpolation=cv2.INTER_AREA)
        rc = cv2.resize(result, size, interpolation=cv2.INTER_AREA)
        ringc = None
        if ring is not None:
            # A coarse pixel counts as ring only if it is (almost) entirely ring at full resolution.
            ringc = cv2.resize(ring.astype(np.float32), size, interpolation=cv2.INTER_AREA) > 0.99
    else:
        pc, rc, ringc = plate, result, ring
    maskc = None if ringc is None else ringc.astype(np.uint8)

    px, py, _resp = _phase(pc, rc)
    inits = [np.eye(2, 3, dtype=np.float32)]
    if math.hypot(px, py) > 0.5:
        init = np.eye(2, 3, dtype=np.float32)
        init[0, 2], init[1, 2] = px, py
        inits.append(init)
    best = None
    for init in inits:
        fit = _run_ecc(pc, rc, init, mtype, crit, maskc)
        if fit is not None and (best is None or fit[0] > best[0]):
            best = fit

    if best is None:
        dx, dy = px / s, py / s
        shift = math.hypot(dx, dy)
        return Registration(
            dx=dx,
            dy=dy,
            residual_px=None,
            ok=False,
            shift_px=shift,
            camera_drift=shift > drift_max,
            converged=False,
            cc=None,
            method="phase_fallback",
            refined=False,
            tiles=0,
            residual_p90_px=None,
            warp=_translation(dx, dy).tolist(),
        )

    cc, warp_c = best
    warp = _scale_warp(warp_c, s)
    refined = False
    method = "ecc"
    if s < 1.0:
        mask_full = None if ring is None else ring.astype(np.uint8)
        fine = _run_ecc(plate, result, warp, mtype, crit, mask_full)
        if fine is not None and np.abs(fine[1][:, 2] - warp[:, 2]).max() <= refine_window:
            cc, warp = fine
            refined = True
            method = "ecc_coarse_fine"
        else:
            method = "ecc_coarse"

    dx, dy = float(warp[0, 2]), float(warp[1, 2])
    shift = math.hypot(dx, dy)

    # Residual: leftover per-tile shift after the global warp, at full resolution (a coarse-level measure
    # is too noisy: 0.2 coarse px is 0.8 px on a 3840 plate).
    aligned = warp_to_plate(result, warp)
    margin = int(math.ceil(shift)) + 2
    vals = _tile_residuals(plate, aligned, ring, margin)
    if vals.size:
        residual = float(np.median(vals))
        p90 = float(np.percentile(vals, 90))
    else:
        residual = p90 = None
    ok = residual is not None and residual <= residual_max and shift <= drift_max
    return Registration(
        dx=dx,
        dy=dy,
        residual_px=residual,
        ok=bool(ok),
        shift_px=shift,
        camera_drift=shift > drift_max,
        converged=True,
        cc=float(cc),
        method=method,
        refined=refined,
        tiles=int(vals.size),
        residual_p90_px=p90,
        warp=np.asarray(warp, np.float64).tolist(),
    )


def ecc(
    plate_gray: np.ndarray,
    result_gray: np.ndarray,
    motion: str = "translation",
    max_iter: int = 200,
    eps: float = 1e-6,
    ring_mask: np.ndarray | None = None,
) -> tuple[float, float, float | None, bool]:
    """``(dx, dy, residual_px, ok)``; ``estimate`` returns the full record."""
    return estimate(plate_gray, result_gray, motion, max_iter, eps, ring_mask).as_tuple()
