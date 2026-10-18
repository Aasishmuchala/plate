"""Display-image IO: 8/16-bit PNG and 16-bit TIFF in, 16-bit TIFF / 8- and 16-bit PNG out.

Every write goes to ``<name>.<ext>.part`` and is moved into place with ``os.replace`` (5 retries x 50 ms,
because Windows holds a lock for a moment after a viewer, an indexer or an AV scan touches the file). PNG is
read and written through cv2 on bytes (``cv2.imread``/``imwrite`` fail on non-ASCII Windows paths); TIFF goes
through tifffile. EXR lives in ``plate_pixels.exr``.

A client file is checked before it is decoded. ``declared_size(path)`` reads the width and height from the
header alone (``imghdr``; a TIFF whose first IFD sits past the first MiB goes through tifffile's page header),
and ``read(path, max_pixels=N)`` refuses a file whose header declares more than ``N`` pixels with
``TooManyPixels`` before a pixel is allocated: a 52 KB PNG can declare 4000 x 4000 and a 775 KB one 16000 x
16000. ``read`` applies the EXIF Orientation tag of a JPEG (cv2's ``IMREAD_UNCHANGED`` ignores it, so a phone
photo would come out sideways); PNG, TIFF and the rest are read as stored.
"""

from __future__ import annotations

import io
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
from . import imghdr
from .icc import SRGB_ICC

__all__ = [
    "HEADER_PROBE_BYTES",
    "Image",
    "SRGB_ICC",
    "TooManyPixels",
    "apply_orientation",
    "atomic_target",
    "check_pixel_budget",
    "declared_size",
    "exif_orientation",
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
# a PNG, JPEG, WebP or BMP header is in the first MiB (a JPEG's EXIF and ICC segments come first)
HEADER_PROBE_BYTES = 1 << 20
EXIF_ORIENTATION_TAG = 0x0112
TOO_MANY_PIXELS = "too_many_pixels"


@dataclass
class Image:
    """A decoded display image. ``rgb`` is float32 (H, W, 3): in [0, 1] when ``encoding`` is ``srgb``
    (display-referred, as stored), scene values when ``linear`` (float TIFF). ``alpha`` is float32 (H, W)."""

    rgb: np.ndarray
    alpha: np.ndarray | None
    bit_depth: int
    encoding: str  # 'srgb' | 'linear'
    icc: bytes | None


class TooManyPixels(ValueError):
    """A file whose header declares more pixels than the caller allows; nothing was decoded. The message starts
    with ``too_many_pixels:`` so the pixels worker's 400 body still names the cause to the daemon."""

    def __init__(self, width: int, height: int, limit: int):
        super().__init__(
            f"{TOO_MANY_PIXELS}: the file declares {width} x {height} = {width * height} px; the cap is {limit}"
        )
        self.width, self.height, self.limit = width, height, limit


# --------------------------------------------------------------------------- header-first checks


def declared_size(path: str | Path) -> tuple[int, int] | None:
    """``(width, height)`` the file's header declares, without decoding a pixel; None when it cannot be read
    from the header (an unknown format, a truncated header)."""
    p = Path(path)
    with p.open("rb") as fh:
        head = fh.read(HEADER_PROBE_BYTES)
    size = imghdr.header_size(head)
    if size is None and imghdr.magic(head) == "tiff":  # the first IFD can sit at the end of the file
        try:
            with tifffile.TiffFile(p) as tf:
                page = tf.pages[0]
                size = (int(page.imagewidth), int(page.imagelength))
        except Exception:  # a damaged TIFF is the decoder's to name
            size = None
    return size


def check_pixel_budget(path: str | Path, max_pixels: int) -> tuple[int, int] | None:
    """Raise ``TooManyPixels`` when the header of ``path`` declares more than ``max_pixels``; else the size."""
    size = declared_size(path)
    if size is not None and size[0] * size[1] > max_pixels:
        raise TooManyPixels(size[0], size[1], max_pixels)
    return size


# --------------------------------------------------------------------------- EXIF orientation


def exif_orientation(raw: bytes) -> int:
    """The EXIF Orientation (1 to 8) of a JPEG's bytes; 1 for any other format, no tag or an unreadable one."""
    if raw[:3] != b"\xff\xd8\xff":
        return 1
    try:
        from PIL import Image as PILImage  # lazy: only a JPEG pays for it; open() reads the header only

        with PILImage.open(io.BytesIO(raw)) as im:
            value = int(im.getexif().get(EXIF_ORIENTATION_TAG, 1))
    except Exception:  # a bomb warning, a corrupt EXIF block: the decoder will name a real problem
        return 1
    return value if 1 <= value <= 8 else 1


def apply_orientation(a: np.ndarray, orientation: int) -> np.ndarray:
    """``a`` (H, W[, C]) turned the way EXIF ``orientation`` says to show it (PIL's ``exif_transpose``:
    2 mirror, 3 rotate 180, 4 flip, 5 transpose, 6 rotate 90 clockwise, 7 transverse, 8 rotate 90 anticlockwise)."""
    if orientation == 2:
        out = a[:, ::-1]
    elif orientation == 3:
        out = a[::-1, ::-1]
    elif orientation == 4:
        out = a[::-1, :]
    elif orientation == 5:
        out = np.swapaxes(a, 0, 1)
    elif orientation == 6:
        out = np.swapaxes(a, 0, 1)[:, ::-1]
    elif orientation == 7:
        out = np.swapaxes(a, 0, 1)[::-1, ::-1]
    elif orientation == 8:
        out = np.swapaxes(a, 0, 1)[::-1, :]
    else:
        return a
    return np.ascontiguousarray(out)


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


def read(path: str | Path, *, max_pixels: int | None = None) -> Image:
    """Read a PNG, TIFF or other cv2-decodable image (JPEG client plates) into an :class:`Image`.

    ``max_pixels`` refuses (``TooManyPixels``) a file whose header declares more before anything is decoded.
    A JPEG's EXIF Orientation is applied, so the image comes out the way a viewer shows it."""
    p = Path(path)
    if max_pixels is not None:
        check_pixel_budget(p, max_pixels)
    if p.suffix.lower() in (".tif", ".tiff"):
        return _read_tiff(p)
    raw = p.read_bytes()
    icc = _icc.read_png_icc(raw) if raw[:8] == _icc.PNG_SIGNATURE else None
    decoded = _decode_cv(raw, p)
    orientation = exif_orientation(raw)
    if orientation != 1:
        decoded = apply_orientation(decoded, orientation)
    rgb_raw, alpha_raw = _split_channels(decoded)
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
