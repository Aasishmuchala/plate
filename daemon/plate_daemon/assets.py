"""CAS objects as the UI reads them: `GET /api/v0/assets/<hash>?fmt=png|rgba16f|thumb` (router plan T25).

An asset is found by its BLAKE3 key in a project's CAS (`locate`: the named project, else every indexed
project). Formats:

- no `fmt`: the stored bytes, with the media type of the stored extension;
- `png`: PNG bytes. A stored PNG is served as it is (16-bit stays 16-bit); another image (TIFF, JPEG) is
  re-encoded as PNG at its own bit depth;
- `rgba16f`: raw little-endian IEEE binary16, RGBA interleaved, rows top-first, no header, exactly
  `width * height * 8` bytes, holding the image's sRGB code values / full scale (`ui/src/viewer/README.md`,
  transport); alpha is 1.0 where the image has none. `X-Plate-Width` and `X-Plate-Height` carry the size;
- `thumb`: an 8-bit PNG at most `THUMB_PX` on its long edge (INTER_AREA). A smaller image keeps its size:
  nothing is ever enlarged.

Only images decode; asking for a pixel format of anything else (JSON, an EXR, a video) raises
`AssetFormatError`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .cas import CAS

__all__ = [
    "FORMATS",
    "MEDIA_TYPES",
    "THUMB_PX",
    "Asset",
    "AssetFormatError",
    "AssetNotFound",
    "decode_rgba",
    "locate",
    "render",
    "rgba16f",
    "thumb_png",
]

THUMB_PX = 320
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


@dataclass(frozen=True)
class Asset:
    key: str
    project_id: str
    path: Path

    @property
    def ext(self) -> str:
        return self.path.suffix.lstrip(".").lower()

    @property
    def media_type(self) -> str:
        return MEDIA_TYPES.get(self.ext, "application/octet-stream")


def locate(
    key: str, project_ids: list[str], cas_for: Callable[[str], CAS], project_id: str | None = None
) -> Asset:
    """The project CAS holding `key`: `project_id` when given, else the first indexed project that has it."""
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise AssetNotFound(f"{key!r} is not a BLAKE3 key")
    for pid in [project_id] if project_id else project_ids:
        path = cas_for(pid).find(key)
        if path is not None:
            return Asset(key, pid, path)
    raise AssetNotFound(key)


def decode_rgba(data: bytes) -> tuple[np.ndarray, int]:
    """(RGBA array (H, W, 4) of the stored dtype, full scale 255 or 65535)."""
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise AssetFormatError("the asset is not a decodable image")
    if img.dtype not in (np.uint8, np.uint16):
        raise AssetFormatError(f"unsupported image sample type {img.dtype}")
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
    rgba, _full = decode_rgba(data)
    ok, buf = cv2.imencode(".png", np.ascontiguousarray(rgba[..., [2, 1, 0, 3]]))
    if not ok:
        raise AssetFormatError("cv2 could not encode the PNG")
    return bytes(buf)


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
