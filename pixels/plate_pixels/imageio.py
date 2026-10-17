"""Display-image IO: 8/16-bit PNG and 16-bit TIFF in, 16-bit TIFF / 8- and 16-bit PNG out.

Every write goes to ``<name>.<ext>.part`` and is moved into place with ``os.replace`` (5 retries x 50 ms,
because Windows holds a lock for a moment after a viewer, an indexer or an AV scan touches the file). PNG is
read and written through cv2 on bytes (``cv2.imread``/``imwrite`` fail on non-ASCII Windows paths); TIFF goes
through tifffile. EXR lives in ``plate_pixels.exr``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import tifffile

from . import icc as _icc
from .icc import SRGB_ICC

__all__ = [
    "Image",
    "SRGB_ICC",
    "atomic_target",
    "part_path",
    "read",
    "replace_with_retry",
    "write_png8",
    "write_png16",
    "write_tiff16",
]

TIFF_ICC_TAG = 34675
REPLACE_RETRIES = 5
REPLACE_DELAY_S = 0.05


@dataclass
class Image:
    """A decoded display image. ``rgb`` is float32 (H, W, 3): in [0, 1] when ``encoding`` is ``srgb``
    (display-referred, as stored), scene values when ``linear`` (float TIFF). ``alpha`` is float32 (H, W)."""

    rgb: np.ndarray
    alpha: np.ndarray | None
    bit_depth: int
    encoding: str  # 'srgb' | 'linear'
    icc: bytes | None


# --------------------------------------------------------------------------- atomic writes


def part_path(path: str | Path) -> Path:
    p = Path(path)
    return p.with_suffix(p.suffix + ".part")


def replace_with_retry(
    src: Path, dst: Path, retries: int = REPLACE_RETRIES, delay: float = REPLACE_DELAY_S
) -> None:
    """``os.replace`` that survives the short-lived sharing violations Windows raises as PermissionError."""
    for attempt in range(retries + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == retries:
                raise
            time.sleep(delay)


@contextmanager
def atomic_target(path: str | Path) -> Iterator[Path]:
    """Yield the ``.part`` path to write; on clean exit move it over ``path``, on error remove it."""
    dst = Path(path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = part_path(dst)
    try:
        yield part
        replace_with_retry(part, dst)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise


def _atomic_write_bytes(path: str | Path, data: bytes) -> None:
    with atomic_target(path) as part:
        with open(part, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())


# --------------------------------------------------------------------------- quantisation


def _as_hwc(a: np.ndarray, name: str) -> np.ndarray:
    a = np.asarray(a)
    if a.ndim == 2:
        a = a[..., None]
    if a.ndim != 3 or a.shape[2] not in (1, 3):
        raise ValueError(f"{name}: expected (H, W, 3), got {a.shape}")
    if a.shape[2] == 1:
        a = np.repeat(a, 3, axis=2)
    return a


def _quantise(a: np.ndarray, bits: int) -> np.ndarray:
    """Float in [0, 1] -> uint8/uint16 (round-to-nearest); integer input passes through, 8<->16 rescaled."""
    top = (1 << bits) - 1
    dtype = np.uint16 if bits == 16 else np.uint8
    if a.dtype == np.uint16:
        return a if bits == 16 else np.round(a.astype(np.float64) / 257.0).astype(np.uint8)
    if a.dtype == np.uint8:
        return a.astype(np.uint16) * 257 if bits == 16 else a
    f = np.clip(np.nan_to_num(a.astype(np.float64), nan=0.0), 0.0, 1.0)
    return np.round(f * top).astype(dtype)


def _planes(rgb: np.ndarray, alpha: np.ndarray | None, bits: int) -> np.ndarray:
    """Quantised (H, W, 3) or (H, W, 4) array in RGB(A) order."""
    base = _quantise(_as_hwc(rgb, "rgb"), bits)
    if alpha is None:
        return np.ascontiguousarray(base)
    al = np.asarray(alpha)
    if al.ndim == 3:
        al = al[..., 0]
    if al.shape != base.shape[:2]:
        raise ValueError(f"alpha shape {al.shape} does not match rgb {base.shape[:2]}")
    return np.ascontiguousarray(np.concatenate([base, _quantise(al, bits)[..., None]], axis=2))


# --------------------------------------------------------------------------- PNG


def _write_png(
    path: str | Path, rgb: np.ndarray, alpha: np.ndarray | None, icc: bytes | None, bits: int
) -> None:
    arr = _planes(rgb, alpha, bits)
    bgr = arr[..., [2, 1, 0] + ([3] if arr.shape[2] == 4 else [])]
    ok, buf = cv2.imencode(".png", np.ascontiguousarray(bgr))
    if not ok:
        raise OSError(f"cv2 could not encode a {bits}-bit PNG")
    data = bytes(buf)
    if icc:
        data = _icc.embed_png_icc(data, icc, name="sRGB")
    _atomic_write_bytes(path, data)


def write_png16(
    path: str | Path, rgb: np.ndarray, icc: bytes | None = SRGB_ICC, alpha: np.ndarray | None = None
) -> None:
    """16-bit PNG. ``rgb`` is float in [0, 1] (or uint8/uint16); ``icc`` is embedded as iCCP (None: no profile)."""
    _write_png(path, rgb, alpha, icc, 16)


def write_png8(
    path: str | Path, rgb: np.ndarray, icc: bytes | None = SRGB_ICC, alpha: np.ndarray | None = None
) -> None:
    """8-bit PNG. ``rgb`` is float in [0, 1] (or uint8/uint16); ``icc`` is embedded as iCCP (None: no profile)."""
    _write_png(path, rgb, alpha, icc, 8)


def _decode_cv(raw: bytes, path: Path) -> np.ndarray:
    buf = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError(f"cannot decode image {path}")
    return img


def _split_channels(img: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
    """cv2 BGR(A) / gray(A) -> (RGB (H,W,3), alpha (H,W) or None), dtype preserved."""
    if img.ndim == 2:
        return np.repeat(img[..., None], 3, axis=2), None
    c = img.shape[2]
    if c == 1:
        return np.repeat(img, 3, axis=2), None
    if c == 2:  # gray + alpha
        return np.repeat(img[..., :1], 3, axis=2), img[..., 1]
    if c == 3:
        return img[..., ::-1], None
    return img[..., 2::-1], img[..., 3]


def _normalise(a: np.ndarray) -> tuple[np.ndarray, int, str]:
    if a.dtype == np.uint8:
        return a.astype(np.float32) / 255.0, 8, "srgb"
    if a.dtype == np.uint16:
        return a.astype(np.float32) / 65535.0, 16, "srgb"
    if np.issubdtype(a.dtype, np.floating):
        return a.astype(np.float32), 8 * a.dtype.itemsize, "linear"
    raise ValueError(f"unsupported pixel type {a.dtype}")


def _read_tiff(path: Path) -> Image:
    with tifffile.TiffFile(path) as tf:
        series = tf.series[0]
        arr = series.asarray()
        axes = series.axes
        tag = tf.pages[0].tags.get(TIFF_ICC_TAG)
        icc = bytes(tag.value) if tag is not None else None
    if axes == "SYX":
        arr = np.moveaxis(arr, 0, -1)
    elif axes not in ("YX", "YXS"):
        raise ValueError(f"unsupported TIFF layout {axes!r} in {path}")
    if arr.ndim == 2:
        arr = arr[..., None]
    c = arr.shape[2]
    if c == 1:
        rgb_raw, alpha_raw = np.repeat(arr, 3, axis=2), None
    elif c == 2:
        rgb_raw, alpha_raw = np.repeat(arr[..., :1], 3, axis=2), arr[..., 1]
    elif c == 3:
        rgb_raw, alpha_raw = arr, None
    else:
        rgb_raw, alpha_raw = arr[..., :3], arr[..., 3]
    rgb, depth, enc = _normalise(rgb_raw)
    alpha = _normalise(alpha_raw)[0] if alpha_raw is not None else None
    return Image(rgb=rgb, alpha=alpha, bit_depth=depth, encoding=enc, icc=icc)


def read(path: str | Path) -> Image:
    """Read a PNG, TIFF or other cv2-decodable image (JPEG client plates) into an :class:`Image`."""
    p = Path(path)
    if p.suffix.lower() in (".tif", ".tiff"):
        return _read_tiff(p)
    raw = p.read_bytes()
    icc = _icc.read_png_icc(raw) if raw[:8] == _icc.PNG_SIGNATURE else None
    rgb_raw, alpha_raw = _split_channels(_decode_cv(raw, p))
    rgb, depth, enc = _normalise(rgb_raw)
    alpha = _normalise(alpha_raw)[0] if alpha_raw is not None else None
    return Image(rgb=np.ascontiguousarray(rgb), alpha=alpha, bit_depth=depth, encoding=enc, icc=icc)


# --------------------------------------------------------------------------- TIFF


def write_tiff16(
    path: str | Path, rgb: np.ndarray, icc: bytes | None = SRGB_ICC, alpha: np.ndarray | None = None
) -> None:
    """16-bit RGB(A) TIFF (zlib), the ICC profile in tag 34675. No Software or JSON ImageDescription tag."""
    arr = _planes(rgb, alpha, 16)
    kwargs: dict = {"photometric": "rgb", "compression": "zlib", "metadata": None, "software": False}
    if icc:
        kwargs["iccprofile"] = icc
    if arr.shape[2] == 4:
        kwargs["extrasamples"] = ("unassalpha",)
    with atomic_target(path) as part:
        with open(part, "wb") as fh:
            tifffile.imwrite(fh, arr, **kwargs)
            fh.flush()
            os.fsync(fh.fileno())
