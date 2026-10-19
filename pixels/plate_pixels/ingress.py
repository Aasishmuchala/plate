"""Ingress transform: an engine result back onto the plate's pixel grid (BUILD_PLAN decision 4).

``restore`` decodes the vendor image, undoes the ``SizeMap`` (resample to the padded rectangle, drop the pad,
put a region crop back in its place), registers it to the plate with ECC (``plate_pixels.register``) and
applies the warp when the fit is good. The output is the plate's size and the plate's depth; compositing
inside the mask (outside-mask pixels are the plate by construction) and the numeric tone match happen
downstream, on this output.

A result whose aspect differs from what was sent (vendor preset snapping) is centre-cropped to the sent aspect
before resampling, so the frame is never squashed; the crop is reported. Resampling to plate pixels uses
``cv2.INTER_LANCZOS4``, except a strong reduction (the vendor answered at a tier more than 1.25x larger than
what was sent) which uses ``cv2.INTER_AREA``: unfiltered Lanczos aliases a generator's fine foliage there. Only
AI pixels are ever resampled here: the plate is read, never resized.

``decode_result`` reads the declared size from the header first (``plate_pixels.imghdr``) and refuses a result over
``max_pixels`` (``PLATE_RESULT_MAX_PIXELS``, default 64 000 000) before anything is allocated: a small PNG can
declare a decode of many gigabytes. An empty or undecodable result is an ``IngressError`` that names its byte
length and magic.

The result's format is recorded, not judged (router plan T152, decision 86): ``result_info`` names the container
(``raw_format``: ``png``, ``jpeg``, ``webp``, ``bmp``, ``tiff``, ``unknown``; ``array`` for an in-memory array),
the decoded bit depth (8, 16 or 32), the channel count (1, 3 or 4), whether an alpha channel was dropped and
whether it was fully transparent, and whether the container is lossy (JPEG). ``decode_result_with_info`` returns it
beside the pixels and ``restore`` merges it into its report. A float result with any non-finite value (NaN, inf)
is an ``IngressError`` before anything is clipped: a NaN must never be cast into the composite.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import cv2
import numpy as np

from .egress import SizeMap
from .imghdr import header_size, magic
from .imghdr import max_pixels as default_max_pixels
from .register import estimate, to_gray, warp_to_plate

__all__ = ["IngressError", "decode_result", "decode_result_with_info", "restore", "result_info"]

_ASPECT_SNAP = 1e-3  # relative aspect difference above which the result is centre-cropped before resampling
_AREA_BELOW = 0.8  # a reduction to less than this scale is box-filtered (INTER_AREA) instead of Lanczos
_WARP_MIN_PX = 0.02  # a smaller shift is left alone rather than resampled for nothing


class IngressError(ValueError):
    """The engine result or the size map cannot be mapped back onto the plate."""


def result_info(data: bytes | bytearray | memoryview | None, decoded: np.ndarray) -> dict[str, Any]:
    """What the engine handed back, from its bytes and the array they decoded to (before any channel is dropped):
    ``{raw_format, raw_bit_depth, raw_channels, alpha_dropped, alpha_transparent, lossy}``. ``data`` None means
    the result was an in-memory array (``raw_format: "array"``)."""
    a = np.asarray(decoded)
    fmt = "array" if data is None else (magic(data) or "unknown")
    channels = 1 if a.ndim == 2 else int(a.shape[2])
    has_alpha = a.ndim == 3 and channels == 4
    return {
        "raw_format": fmt,
        "raw_bit_depth": int(a.dtype.itemsize * 8),
        "raw_channels": channels,
        "alpha_dropped": bool(has_alpha),
        "alpha_transparent": bool(has_alpha and a.size > 0 and a[..., 3].max() == 0),
        "lossy": fmt == "jpeg",
    }


def decode_result(
    result: bytes | bytearray | memoryview | np.ndarray, *, max_pixels: int | None = None
) -> np.ndarray:
    """float32 (H, W, 3) RGB in [0, 1] from encoded image bytes (PNG/JPEG/WebP via cv2) or an RGB array.

    Encoded bytes are checked before the decode: empty is refused, and so is a header that declares more than
    ``max_pixels`` (default ``imghdr.max_pixels()``, i.e. ``PLATE_RESULT_MAX_PIXELS``). A float result with a
    non-finite value is refused (``decode_result_with_info``)."""
    return decode_result_with_info(result, max_pixels=max_pixels)[0]


def decode_result_with_info(
    result: bytes | bytearray | memoryview | np.ndarray, *, max_pixels: int | None = None
) -> tuple[np.ndarray, dict[str, Any]]:
    """``(decode_result's array, result_info)``: the pixels and what format they came in."""
    if isinstance(result, (bytes, bytearray, memoryview)):
        data = bytes(result)
        if not data:
            raise IngressError("engine result is empty (0 bytes)")
        cap = default_max_pixels() if max_pixels is None else int(max_pixels)
        size = header_size(data)
        if size is not None and size[0] * size[1] > cap:
            w, h = size
            raise IngressError(
                f"engine result declares {w}x{h} = {w * h} px; the cap is {cap} (PLATE_RESULT_MAX_PIXELS)"
            )
        try:
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
        except cv2.error:
            img = None
        if img is None:
            raise IngressError(
                f"cannot decode the engine result ({len(data)} bytes, magic {magic(data) or 'unknown'})"
            )
        if img.ndim == 3 and img.shape[2] not in (3, 4):
            raise IngressError(f"engine result has {img.shape[2]} channels (1, 3 or 4 expected)")
        info = result_info(data, img)
        if img.ndim == 2:
            img = np.repeat(img[..., None], 3, axis=2)
        elif img.shape[2] == 4:
            img = img[..., 2::-1]  # BGRA -> RGB (alpha dropped)
        else:
            img = img[..., ::-1]  # BGR -> RGB
        a = img
    else:
        a = np.asarray(result)
        if a.ndim == 2:
            a = np.repeat(a[..., None], 3, axis=2)
        if a.ndim != 3 or a.shape[2] not in (3, 4):
            raise IngressError(f"engine result must be (H, W, 3), got {a.shape}")
        info = result_info(None, a)
        a = a[..., :3]
    if a.dtype == np.uint8:
        f = a.astype(np.float32) / 255.0
    elif a.dtype == np.uint16:
        f = a.astype(np.float32) / 65535.0
    elif np.issubdtype(a.dtype, np.floating):
        finite = np.isfinite(a).all(axis=2)
        if not finite.all():
            n, total = int(finite.size - np.count_nonzero(finite)), int(finite.size)
            raise IngressError(f"engine result has {n} non-finite pixels of {total}")
        f = np.clip(a.astype(np.float32), 0.0, 1.0)
    else:
        raise IngressError(f"unsupported result dtype {a.dtype}")
    return np.ascontiguousarray(f), info


def _plate_float(plate: np.ndarray) -> np.ndarray:
    a = np.asarray(plate)
    if a.ndim == 2:
        a = np.repeat(a[..., None], 3, axis=2)
    a = a[..., :3]
    if a.dtype == np.uint8:
        return a.astype(np.float32) / 255.0
    if a.dtype == np.uint16:
        return a.astype(np.float32) / 65535.0
    if np.issubdtype(a.dtype, np.floating):
        return np.clip(a.astype(np.float32), 0.0, 1.0)
    raise IngressError(f"unsupported plate dtype {a.dtype}")


def _output(rgb: np.ndarray, plate_dtype: np.dtype) -> np.ndarray:
    """Promote to the plate's depth: integer plates get uint16, float plates keep their float type."""
    f = np.clip(rgb, 0.0, 1.0)
    if np.issubdtype(plate_dtype, np.floating):
        return f.astype(plate_dtype)
    return np.round(f * 65535.0).astype(np.uint16)


def _plate_at_output_depth(plate: np.ndarray) -> np.ndarray:
    """The plate itself in ``_output``'s dtype, without a float detour: uint16 as is, uint8 x 257 (exact),
    float clipped to [0, 1] in its own type. Pixels outside a region crop come from here, bit for bit."""
    a = np.asarray(plate)
    if a.ndim == 2:
        a = np.repeat(a[..., None], 3, axis=2)
    a = a[..., :3]
    if a.dtype == np.uint16:
        return a.copy()
    if a.dtype == np.uint8:
        return a.astype(np.uint16) * np.uint16(257)
    return np.clip(a, 0.0, 1.0).astype(a.dtype)


def _aspect_crop(img: np.ndarray, aspect: float) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """Centre-crop ``img`` to ``aspect`` (width / height). Returns (crop, (left, top, right, bottom))."""
    h, w = img.shape[:2]
    if abs((w / h) / aspect - 1.0) <= _ASPECT_SNAP:
        return img, (0, 0, 0, 0)
    if w / h > aspect:
        nw = max(1, int(round(h * aspect)))
        left = (w - nw) // 2
        return img[:, left : left + nw], (left, 0, w - nw - left, 0)
    nh = max(1, int(round(w / aspect)))
    top = (h - nh) // 2
    return img[top : top + nh], (0, top, 0, h - nh - top)


def _resample(img: np.ndarray, size: tuple[int, int]) -> tuple[np.ndarray, str]:
    h, w = img.shape[:2]
    if (w, h) == size:
        return img, "none"
    if max(size[0] / w, size[1] / h) < _AREA_BELOW:
        return cv2.resize(img, size, interpolation=cv2.INTER_AREA), "area"
    return cv2.resize(img, size, interpolation=cv2.INTER_LANCZOS4), "lanczos4"


def restore(
    result_rgb8: bytes | np.ndarray,
    sizemap: SizeMap | Mapping[str, Any],
    plate_rgb16: np.ndarray,
    *,
    ring_mask: np.ndarray | None = None,
    motion: str = "translation",
) -> tuple[np.ndarray, dict[str, Any]]:
    """Return ``(rgb at plate resolution, report)``.

    ``result_rgb8`` is the vendor's encoded bytes or a decoded RGB array; ``sizemap`` the ``SizeMap`` (or its
    dict) from ``egress.prepare``; ``plate_rgb16`` the same display master that was sent. ``ring_mask`` (plate
    size, True = expected unchanged) confines registration to the ring. The output dtype follows the plate:
    uint16 for integer plates, the plate's float type (values in [0, 1]) for float plates.

    Report: ``shift_px``, ``residual_px``, ``registered`` (residual <= 1 px and shift <= 8 px),
    ``camera_drift`` (shift > 8 px), plus ``dx``, ``dy``, ``warp_applied``, the full ``registration`` record,
    ``result_size``, ``aspect_crop``, ``resample``, the ``region`` and the result's format (``result_info``:
    ``raw_format``, ``raw_bit_depth``, ``raw_channels``, ``alpha_dropped``, ``alpha_transparent``, ``lossy``).
    """
    sm = sizemap if isinstance(sizemap, SizeMap) else SizeMap.from_dict(sizemap)
    plate_arr = np.asarray(plate_rgb16)
    plate = _plate_float(plate_arr)
    H, W = plate.shape[:2]
    if (W, H) != sm.src:
        raise IngressError(f"plate is {W} x {H} but the size map was made for {sm.src[0]} x {sm.src[1]}")

    res, info = decode_result_with_info(result_rgb8)
    rh, rw = res.shape[:2]
    pw, ph = sm.padded_size
    res, aspect_crop = _aspect_crop(res, sm.dst[0] / sm.dst[1])
    padded, resample = _resample(res, (pw, ph))
    left, top, _, _ = sm.pad
    cw, ch = sm.content_size
    content = padded[top : top + ch, left : left + cw]
    x0, y0, x1, y1 = sm.content

    ring = None
    if ring_mask is not None:
        ring = np.asarray(ring_mask)
        if ring.ndim == 3:
            ring = ring[..., 0]
        if ring.shape != (H, W):
            raise IngressError(f"ring_mask {ring.shape} must match the plate {(H, W)}")
        ring = ring[y0:y1, x0:x1]
    plate_c = plate[y0:y1, x0:x1]
    reg = estimate(to_gray(plate_c), to_gray(content), motion=motion, ring_mask=ring)

    warp_applied = False
    if reg.ok and reg.shift_px >= _WARP_MIN_PX:
        content = warp_to_plate(np.ascontiguousarray(content), np.asarray(reg.warp, np.float32))
        warp_applied = True

    if sm.region is not None:
        out = _plate_at_output_depth(plate_arr)
        out[y0:y1, x0:x1] = _output(content, plate_arr.dtype)
    else:
        out = _output(np.ascontiguousarray(content), plate_arr.dtype)

    report: dict[str, Any] = {
        "shift_px": reg.shift_px,
        "residual_px": reg.residual_px,
        "registered": bool(reg.ok),
        "camera_drift": bool(reg.camera_drift),
        "dx": reg.dx,
        "dy": reg.dy,
        "warp_applied": warp_applied,
        "registration": reg.to_dict(),
        "result_size": [rw, rh],
        "aspect_crop": list(aspect_crop),
        "resample": resample,
        "region": None if sm.region is None else list(sm.region),
        "sizemap_mode": sm.mode,
        **info,
    }
    return out, report
