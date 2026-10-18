"""CAS objects as the UI reads them: `GET /api/v0/assets/<hash>?fmt=png|rgba16f|thumb` (router plan T25).

An asset is found by its BLAKE3 key in a project's CAS (`locate`: the named project, else every indexed
project); a key the CAS does not hold falls back to the packets index, where an ingested packet names its
files by reference (decision 32: the plate's pixels are never copied into the CAS). Formats:

- no `fmt`: the stored bytes, with the media type of the stored extension (the route streams the file, so
  Range and conditional requests work);
- `png`: PNG bytes. A stored PNG is served as it is (16-bit stays 16-bit); another image (TIFF, JPEG) is
  re-encoded as PNG at its own bit depth;
- `rgba16f`: raw little-endian IEEE binary16, RGBA interleaved, rows top-first, no header, exactly
  `width * height * 8` bytes, holding the image's sRGB code values / full scale (`ui/src/viewer/README.md`,
  transport); alpha is 1.0 where the image has none. `X-Plate-Width` and `X-Plate-Height` carry the size.
  A 16-bit TIFF plate decodes at 16 bits (cv2 `IMREAD_UNCHANGED`);
- `thumb`: an 8-bit PNG at most `THUMB_PX` on its long edge (INTER_AREA). A smaller image keeps its size:
  nothing is ever enlarged.

Only images decode; asking for a pixel format of anything else (JSON, an EXR, a video) raises
`AssetFormatError`.

`mask_png(data)` is the other direction: an uploaded mask (`POST /assets?kind=mask`) becomes an 8-bit
single-channel PNG whatever it arrived as.

Nothing decodes before its header has been read (`check_header`, router plan T136): a 410 KiB PNG can declare
12000 x 12000 x 3 and ask for 432 MB. `plate_pixels.imghdr.header_size` names the declared size from the first
bytes; no header it knows is `BadImage`, a size over `UPLOAD_MAX_PIXELS` is `TooManyPixels`, and `cv2.imdecode`
runs only after both. Every upload and every stored-asset decode goes through `_imdecode`, which is where the
check lives.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from plate_pixels import imghdr

from .cas import CAS
from .config import check_project_id

__all__ = [
    "FORMATS",
    "MASK_MAX_BYTES",
    "MEDIA_TYPES",
    "NOT_AN_IMAGE_HEADER",
    "THUMB_PX",
    "UPLOAD_MAX_PIXELS",
    "Asset",
    "AssetFormatError",
    "AssetNotFound",
    "BadImage",
    "BadMask",
    "TooManyPixels",
    "check_header",
    "decode_rgba",
    "locate",
    "mask_png",
    "render",
    "rgba16f",
    "thumb_png",
]

THUMB_PX = 320
MASK_MAX_BYTES = 32 * 1024 * 1024
UPLOAD_MAX_PIXELS = 100_000_000  # declared width x height an upload or stored asset may ask a decoder for
NOT_AN_IMAGE_HEADER = "not a PNG/JPEG/WebP/BMP/TIFF header"
FORMATS = ("png", "rgba16f", "thumb")
KEY_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_EXTS = frozenset({"png", "tif", "tiff", "jpg", "jpeg"})
MEDIA_TYPES = {
    "png": "image/png",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "json": "application/json",
    "exr": "image/x-exr",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
}


class AssetNotFound(LookupError):
    pass


class AssetFormatError(ValueError):
    pass


class BadMask(ValueError):
    """An uploaded mask that is not an 8-bit grey, RGB or RGBA image."""


class BadImage(ValueError):
    """Bytes whose first bytes are no image header PLATE reads, or a PNG or JPEG reference that does not decode."""


class TooManyPixels(ValueError):
    """An image whose header declares more than `UPLOAD_MAX_PIXELS`; nothing was decoded."""

    def __init__(self, declared: tuple[int, int], limit: int):
        self.declared, self.limit = declared, limit
        super().__init__(f"the image declares {declared[0]} x {declared[1]} pixels; the limit is {limit}")


@dataclass(frozen=True)
class Asset:
    key: str
    project_id: str
    path: Path
    by_reference: bool = False  # True: a packet file named by the index, not a CAS object

    @property
    def ext(self) -> str:
        return self.path.suffix.lstrip(".").lower()

    @property
    def media_type(self) -> str:
        return MEDIA_TYPES.get(self.ext, "application/octet-stream")


def _packet_file(index: Any, project_id: str, key: str) -> Path | None:
    """The ingested packet file whose BLAKE3 is `key` in this project's index, if it is still on disk."""
    if not index.db_path(project_id).is_file():  # never create an index for a name that is not a project
        return None
    with index.project(project_id) as conn:
        rows = conn.execute(
            "SELECT b.relpath, p.path FROM blobs b JOIN packets p ON p.packet_id = b.packet_id "
            "WHERE b.blake3 = ? AND p.status = 'ingested'",
            (key,),
        ).fetchall()
    for relpath, packet_path in rows:
        rel = Path(relpath)
        if rel.is_absolute() or ".." in rel.parts:
            continue  # the ingest never writes these; refuse them anyway
        path = Path(packet_path) / rel
        if path.is_file():
            return path
    return None


def locate(
    key: str,
    project_ids: list[str],
    cas_for: Callable[[str], CAS],
    project_id: str | None = None,
    *,
    index: Any = None,
) -> Asset:
    """The asset `key`: the CAS of `project_id` when given (else of the first indexed project that has it);
    when no CAS holds it and `index` is given, the ingested packet file it names (`by_reference=True`)."""
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise AssetNotFound(f"{key!r} is not a BLAKE3 key")
    candidates = [project_id] if project_id else project_ids
    for pid in candidates:
        path = cas_for(pid).find(key)
        if path is not None:
            return Asset(key, pid, path)
    if index is not None:
        for pid in candidates:
            check_project_id(pid)
            path = _packet_file(index, pid, key)
            if path is not None:
                return Asset(key, pid, path, by_reference=True)
    raise AssetNotFound(key)


def check_header(data: bytes) -> tuple[int, int]:
    """The `(width, height)` the header of `data` declares, read without decoding a pixel.

    `BadImage` when the first bytes are no header `plate_pixels.imghdr` reads (an empty body, a GIF, a clip, a
    truncated header); `TooManyPixels` when `width * height` passes `UPLOAD_MAX_PIXELS`."""
    size = imghdr.header_size(data)
    if size is None:
        raise BadImage(NOT_AN_IMAGE_HEADER)
    if size[0] * size[1] > UPLOAD_MAX_PIXELS:
        raise TooManyPixels(size, UPLOAD_MAX_PIXELS)
    return size


def _imdecode(data: bytes) -> np.ndarray | None:
    """cv2 `IMREAD_UNCHANGED` decode, after `check_header` (so `BadImage` / `TooManyPixels` before any decode);
    None for a header that is fine on bytes cv2 cannot read."""
    check_header(data)
    try:
        return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    except cv2.error:
        return None


def _decode(data: bytes) -> np.ndarray:
    """The image as stored (cv2 `IMREAD_UNCHANGED`: BGR(A) order, uint8 or uint16)."""
    try:
        img = _imdecode(data)
    except BadImage as e:
        raise AssetFormatError("the asset is not a decodable image") from e
    if img is None:
        raise AssetFormatError("the asset is not a decodable image")
    if img.dtype not in (np.uint8, np.uint16):
        raise AssetFormatError(f"unsupported image sample type {img.dtype}")
    return img


def decode_rgba(data: bytes) -> tuple[np.ndarray, int]:
    """(RGBA array (H, W, 4) of the stored dtype, full scale 255 or 65535)."""
    img = _decode(data)
    full = 65535 if img.dtype == np.uint16 else 255
    if img.ndim == 2:
        img = np.repeat(img[..., None], 3, axis=2)
    if img.shape[2] == 3:
        rgba = np.concatenate([img[..., ::-1], np.full(img.shape[:2] + (1,), full, img.dtype)], axis=2)
    else:
        rgba = np.concatenate([img[..., 2::-1], img[..., 3:4]], axis=2)
    return np.ascontiguousarray(rgba), full


def rgba16f(data: bytes) -> tuple[bytes, int, int]:
    """(raw little-endian half RGBA, width, height): code values / full scale."""
    rgba, full = decode_rgba(data)
    h, w = rgba.shape[:2]
    half = (rgba.astype(np.float32) / float(full)).astype("<f2")
    return half.tobytes(), w, h


def thumb_png(data: bytes, max_px: int = THUMB_PX) -> bytes:
    rgba, full = decode_rgba(data)
    rgb = rgba[..., :3]
    if full == 65535:
        rgb = np.clip(np.rint(rgb.astype(np.float32) / 257.0), 0, 255).astype(np.uint8)
    h, w = rgb.shape[:2]
    s = min(1.0, float(max_px) / float(max(h, w)))
    if s < 1.0:
        rgb = cv2.resize(rgb, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".png", np.ascontiguousarray(rgb[..., ::-1]))
    if not ok:
        raise AssetFormatError("cv2 could not encode the thumbnail")
    return bytes(buf)


def _png(asset: Asset, data: bytes) -> bytes:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data
    ok, buf = cv2.imencode(".png", _decode(data))  # at the image's own bit depth and channel count
    if not ok:
        raise AssetFormatError("cv2 could not encode the PNG")
    return bytes(buf)


def mask_png(data: bytes) -> tuple[bytes, int, int]:
    """(8-bit single-channel PNG bytes, width, height) of an uploaded mask.

    Accepts 8-bit grey, or 8-bit RGB / RGBA (the red channel is the mask). The header is read first
    (`BadImage` for bytes with no image header, `TooManyPixels` over `UPLOAD_MAX_PIXELS`); a decodable image
    that is not a mask raises `BadMask`."""
    img = _imdecode(data)  # reads the header before decoding
    if img is None:
        raise BadMask("the body is not a decodable image")
    if img.dtype != np.uint8:
        raise BadMask(f"masks are 8-bit images, got {img.dtype}")
    if img.ndim == 3:
        if img.shape[2] not in (3, 4):
            raise BadMask(f"masks have 1, 3 or 4 channels, got {img.shape[2]}")
        img = img[..., 2]  # cv2 decodes BGR(A): index 2 is the PNG's first (red) channel
    h, w = img.shape[:2]
    ok, buf = cv2.imencode(".png", np.ascontiguousarray(img))
    if not ok:
        raise BadMask("cv2 could not encode the mask")
    return bytes(buf), w, h


def render(asset: Asset, fmt: str | None) -> tuple[bytes, str, dict[str, Any]]:
    """(body, media type, extra headers) of `asset` in `fmt` (None = the stored bytes)."""
    data = asset.path.read_bytes()
    if fmt is None or fmt == "":
        return data, asset.media_type, {}
    if fmt not in FORMATS:
        raise AssetFormatError(f"fmt must be one of {list(FORMATS)}")
    if asset.ext not in IMAGE_EXTS:
        raise AssetFormatError(f"{asset.ext} assets have no {fmt} form")
    if fmt == "png":
        return _png(asset, data), "image/png", {}
    if fmt == "thumb":
        return thumb_png(data), "image/png", {}
    body, w, h = rgba16f(data)
    return body, "application/octet-stream", {"X-Plate-Width": str(w), "X-Plate-Height": str(h)}
