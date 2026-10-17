"""Thin-lens circle of confusion per pixel, and the edge-recall weight derived from it.

BUILD_PLAN decision 14: the G-buffer edge recall gate is "weighted by circle of confusion from the packet's
f-number and focus distance so DOF-soft regions are not penalised". A geometry edge that the render itself
blurs into a 6 px disc cannot be asked of the AI pixels either, so its weight in the recall goes to zero.

    A      = f / N                                   aperture diameter, mm
    c_mm   = A * |Z - Zf| / Z * f / (Zf - f)         blur disc on the sensor, Z and Zf in mm
    c_px   = c_mm * width_px / film_width_mm         the same disc in rendered pixels at the evaluation width
    w      = clip(1 - c_px / c_max, 0, 1)            c_max = 6 px at 2048 wide, scaled with the width

Worked number (also in docs/gates/gbuffer.md): f 35 mm, N 2.8, Zf 2.5 m, Z 4 m -> A 12.5 mm,
c_mm = 12.5 * 1.5 / 4 * 35 / 2465 = 0.0666 mm -> at 2048 px / 36 mm = 3.79 px -> w = 0.37.

Depth and focus distance come in scene units and are converted with `meters_per_unit` (manifest `units`).
The camera block is the manifest's `camera` object: `focal_length_mm`, `film_width_mm`, `f_number`,
`focus_distance`, `dof_enabled`. A whole manifest is accepted too (its `camera` and `units` are picked out).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np

__all__ = ["C_MAX_PX_AT_2048", "REFERENCE_WIDTH", "c_max_px", "camera_params", "coc_mm", "coc_px", "weights"]

REFERENCE_WIDTH = 2048
C_MAX_PX_AT_2048 = 6.0


def c_max_px(width_px: int) -> float:
    """The CoC (px) at which an edge's weight reaches zero: 6 px at 2048 wide, proportional elsewhere."""
    return C_MAX_PX_AT_2048 * float(width_px) / float(REFERENCE_WIDTH)


def camera_params(camera: Mapping[str, Any], meters_per_unit: float | None = None) -> dict[str, Any]:
    """Pull the thin-lens numbers out of a manifest camera block (or a whole manifest).

    Returns {f_mm, N, Zf_mm, film_width_mm, dof_enabled, meters_per_unit}. `meters_per_unit` is taken from the
    argument, then the manifest `units` block, then the camera block, then 1.0. Raises ValueError when DOF is
    enabled but the f-number or focus distance is missing or degenerate (Zf <= f).
    """
    cam = camera
    units: Mapping[str, Any] | None = None
    if "camera" in camera and isinstance(camera["camera"], Mapping):
        cam = camera["camera"]
        u = camera.get("units")
        units = u if isinstance(u, Mapping) else None
    mpu = meters_per_unit
    if mpu is None and units is not None:
        mpu = units.get("meters_per_unit")
    if mpu is None:
        mpu = cam.get("meters_per_unit", 1.0)
    mpu = float(mpu)
    if not mpu > 0:
        raise ValueError(f"meters_per_unit must be positive, got {mpu}")
    f_mm = float(cam["focal_length_mm"])
    film = float(cam.get("film_width_mm", 36.0))
    if not f_mm > 0 or not film > 0:
        raise ValueError("focal_length_mm and film_width_mm must be positive")
    dof = bool(cam.get("dof_enabled", False))
    n = cam.get("f_number")
    zf = cam.get("focus_distance")
    if dof:
        if n is None or zf is None:
            raise ValueError("dof_enabled camera needs f_number and focus_distance")
        n = float(n)
        zf_mm = float(zf) * mpu * 1000.0
        if not n > 0:
            raise ValueError(f"f_number must be positive, got {n}")
        if zf_mm <= f_mm:
            raise ValueError(f"focus distance {zf_mm:.1f} mm must exceed the focal length {f_mm} mm")
    else:
        n = float(n) if n is not None else None
        zf_mm = float(zf) * mpu * 1000.0 if zf is not None else None
    return {
        "f_mm": f_mm,
        "N": n,
        "Zf_mm": zf_mm,
        "film_width_mm": film,
        "dof_enabled": dof,
        "meters_per_unit": mpu,
    }


def coc_mm(z: np.ndarray, camera: Mapping[str, Any], meters_per_unit: float | None = None) -> np.ndarray:
    """Blur disc diameter on the sensor (mm) per pixel, float32. Zero everywhere when DOF is off.

    Non-finite or non-positive depth (nothing rendered) gives 0: those pixels never carry a reference edge,
    the depth rule excludes them (gbuffer.py), so their weight is moot. A background depth (1e10) is a valid
    far point and gets the far-limit disc A * f / (Zf - f).
    """
    p = camera_params(camera, meters_per_unit)
    zz = np.asarray(z, dtype=np.float64)
    if zz.ndim == 3 and zz.shape[2] == 1:
        zz = zz[..., 0]
    if not p["dof_enabled"]:
        return np.zeros(zz.shape, dtype=np.float32)
    a = p["f_mm"] / p["N"]
    zf = p["Zf_mm"]
    z_mm = zz * (p["meters_per_unit"] * 1000.0)
    valid = np.isfinite(z_mm) & (z_mm > 0)
    safe = np.where(valid, z_mm, 1.0)
    c = a * np.abs(safe - zf) / safe * p["f_mm"] / (zf - p["f_mm"])
    c = np.where(valid, c, 0.0)
    return c.astype(np.float32)


def coc_px(
    z: np.ndarray,
    camera: Mapping[str, Any],
    width_px: int | None = None,
    meters_per_unit: float | None = None,
) -> np.ndarray:
    """Circle of confusion in rendered pixels at `width_px` (default: the width of `z`), float32 (H, W).

    `z` must already be at the evaluation width (nearest-resampled by gbuffer.resample_z): the disc scales
    with `width_px / film_width_mm`, so the pixel width passed here is the width the edges were measured at.
    """
    p = camera_params(camera, meters_per_unit)
    c = coc_mm(z, camera, meters_per_unit)
    w = int(width_px) if width_px is not None else int(c.shape[1])
    return (c * (float(w) / p["film_width_mm"])).astype(np.float32)


def weights(
    z: np.ndarray,
    camera: Mapping[str, Any],
    width_px: int | None = None,
    meters_per_unit: float | None = None,
    c_max: float | None = None,
) -> np.ndarray:
    """Edge weight w = clip(1 - c_px / c_max, 0, 1), float32 (H, W). `c_max` defaults to c_max_px(width).

    DOF off -> every weight is exactly 1.0 (the recall is then unweighted and the report says so).
    """
    c = coc_px(z, camera, width_px, meters_per_unit)
    w = int(width_px) if width_px is not None else int(c.shape[1])
    cm = float(c_max) if c_max is not None else c_max_px(w)
    if not cm > 0:
        raise ValueError(f"c_max must be positive, got {cm}")
    return np.clip(1.0 - c / cm, 0.0, 1.0).astype(np.float32)
