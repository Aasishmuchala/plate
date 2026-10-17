"""Frame sources for delivery: image files, a printf pattern, a folder, or in-memory arrays.

`open_frames(source)` returns a `Frames` that knows the frame size and whether any frame has alpha and
yields `Frame` records one at a time (nothing is held beyond the frame being encoded). A `source` is

    a path to an image               one frame
    a pattern  shot_%04d.png         numbered frames from the lowest of `start_number`, 0, 1 up to the first gap
    a folder                         its images in name order
    a list of paths and/or arrays    in that order
    an iterable of arrays            float [0, 1] (encoding as declared), uint8 or uint16; (H, W, 3 or 4)

Files go through `plate_pixels.imageio` (PNG, TIFF, JPEG; 8 or 16 bit) or OpenImageIO (EXR, always linear;
its `chromaticities` or ASWF `colorInteropID` say which primaries, and a frame in anything but Rec.709, such as
the ACEScg EXRs `plate export` writes for an ACEScg packet, is converted to Rec.709 so the sRGB encode and the
Rec.709 video tags that follow are true; an EXR that names no primaries is taken as Rec.709).
Display images are sRGB-encoded unless the file is a float TIFF. Nothing here resizes, resamples or
upscales: a frame whose size differs from the first is an error.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .guard import DeliveryError

__all__ = ["VIDEO_SUFFIXES", "Frame", "Frames", "is_video_file", "open_frames", "to_display16"]

VIDEO_SUFFIXES = frozenset({".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi"})
IMAGE_SUFFIXES = frozenset({".png", ".tif", ".tiff", ".jpg", ".jpeg", ".exr"})
_MAX_PATTERN_FRAMES = 1_000_000


@dataclass
class Frame:
    """`rgb` float32 (H, W, 3); `alpha` float32 (H, W) in [0, 1] or None; `encoding` 'srgb' or 'linear'."""

    rgb: np.ndarray
    alpha: np.ndarray | None
    encoding: str

    @property
    def size(self) -> tuple[int, int]:
        return int(self.rgb.shape[1]), int(self.rgb.shape[0])


def is_video_file(source: Any) -> bool:
    if not isinstance(source, (str, os.PathLike)):
        return False
    p = Path(source)
    return p.suffix.lower() in VIDEO_SUFFIXES and p.is_file()


# ---------------------------------------------------------------------------- loading one frame


def _from_array(a: np.ndarray, encoding: str) -> Frame:
    a = np.asarray(a)
    if a.ndim != 3 or a.shape[2] not in (3, 4):
        raise DeliveryError(f"a frame array is (H, W, 3 or 4), not {a.shape}")
    if a.dtype == np.uint8:
        f, enc = a.astype(np.float32) / 255.0, "srgb"
    elif a.dtype == np.uint16:
        f, enc = a.astype(np.float32) / 65535.0, "srgb"
    elif np.issubdtype(a.dtype, np.floating):
        f, enc = a.astype(np.float32), encoding
    else:
        raise DeliveryError(f"unsupported frame dtype {a.dtype}")
    alpha = np.ascontiguousarray(f[..., 3]) if f.shape[2] == 4 else None
    return Frame(rgb=np.ascontiguousarray(f[..., :3]), alpha=alpha, encoding=enc)


def _exr_primaries(spec: Any) -> tuple[float, ...] | None:
    """The chromaticities an EXR declares (`chromaticities`, else the ASWF `colorInteropID` of a space we know)."""
    chroma = spec.getattribute("chromaticities")
    if chroma is not None and len(chroma) == 8:
        return tuple(float(v) for v in chroma)
    from ..export import INTEROP_CHROMATICITIES

    return INTEROP_CHROMATICITIES.get(spec.get_string_attribute("colorInteropID"))


def _read_exr(path: Path) -> Frame:
    import OpenImageIO as oiio

    inp = oiio.ImageInput.open(str(path))
    if inp is None:
        raise DeliveryError(f"cannot open {path.name}: {oiio.geterror()}")
    try:
        spec = inp.spec()
        data = inp.read_image("float")
    finally:
        inp.close()
    if data is None:
        raise DeliveryError(f"cannot read {path.name}")
    data = np.asarray(data, dtype=np.float32).reshape(spec.height, spec.width, spec.nchannels)
    if spec.nchannels not in (3, 4):
        raise DeliveryError(f"{path.name}: {spec.nchannels} channels; delivery takes RGB or RGBA")
    rgb = data[..., :3]
    primaries = _exr_primaries(spec)
    if primaries is not None:
        from ..export import REC709_CHROMATICITIES, primaries_matrix

        matrix = primaries_matrix(primaries, REC709_CHROMATICITIES)
        if not np.allclose(matrix, np.eye(3)):
            rgb = (rgb.astype(np.float64) @ matrix.T).astype(np.float32)
    return Frame(
        rgb=np.ascontiguousarray(rgb),
        alpha=np.ascontiguousarray(data[..., 3]) if spec.nchannels == 4 else None,
        encoding="linear",
    )


def _read_file(path: Path) -> Frame:
    if not path.is_file():
        raise DeliveryError(f"frame {path.name} does not exist")
    if path.suffix.lower() == ".exr":
        return _read_exr(path)
    from plate_pixels import imageio

    try:
        img = imageio.read(path)
    except ValueError as e:
        raise DeliveryError(str(e)) from e
    return Frame(rgb=img.rgb, alpha=img.alpha, encoding=img.encoding)


# ---------------------------------------------------------------------------- sources


def _expand_pattern(pattern: str, start_number: int | None) -> list[Path]:
    starts = [start_number] if start_number is not None else [0, 1]
    for start in starts:
        try:
            first = Path(pattern % start)
        except (TypeError, ValueError) as e:
            raise DeliveryError(f"{pattern!r} is not a printf frame pattern") from e
        if not first.is_file():
            continue
        out, n = [], start
        while n - start < _MAX_PATTERN_FRAMES and (p := Path(pattern % n)).is_file():
            out.append(p)
            n += 1
        return out
    raise DeliveryError(f"no frames match {pattern!r}")


def _expand_folder(folder: Path) -> list[Path]:
    files = sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in IMAGE_SUFFIXES
    )
    if not files:
        raise DeliveryError(f"{folder.name}: no image frames in the folder")
    return files


def _is_array(x: Any) -> bool:
    return isinstance(x, np.ndarray)


class Frames:
    """Frame refs (paths or arrays) plus the size and alpha facts taken from the first frame. Single pass:
    iterate it once, in the encode that consumes it."""

    def __init__(self, refs: Iterable[Any], encoding: str, count: int | None):
        self._encoding = encoding
        self._count = count
        self._iter = iter(refs)
        first = next(self._iter, None)
        if first is None:
            raise DeliveryError("no frames to deliver")
        self._first_frame = self._load(first)
        self.width, self.height = self._first_frame.size
        self.has_alpha = self._first_frame.alpha is not None

    @property
    def count(self) -> int | None:
        return self._count

    def _load(self, ref: Any) -> Frame:
        if isinstance(ref, Frame):
            return ref
        if _is_array(ref):
            return _from_array(ref, self._encoding)
        return _read_file(Path(ref))

    def __iter__(self) -> Iterator[Frame]:
        yield self._first_frame
        for ref in self._iter:
            frame = self._load(ref)
            if frame.size != (self.width, self.height):
                raise DeliveryError(
                    f"frame size changed from {self.width}x{self.height} to {frame.size[0]}x{frame.size[1]}; "
                    "delivery never resizes"
                )
            yield frame


def open_frames(source: Any, *, start_number: int | None = None, encoding: str = "srgb") -> Frames:
    """See the module docstring. `encoding` applies to float arrays only ('srgb' or 'linear')."""
    if encoding not in ("srgb", "linear"):
        raise DeliveryError(f"encoding is 'srgb' or 'linear', not {encoding!r}")
    if _is_array(source):
        if source.ndim == 4:  # a stack of frames
            return Frames(list(source), encoding, len(source))
        return Frames([source], encoding, 1)
    if isinstance(source, (str, os.PathLike)):
        text = os.fspath(source)
        if "%" in Path(text).name:
            refs = _expand_pattern(text, start_number)
        else:
            p = Path(text)
            if p.is_dir():
                refs = _expand_folder(p)
            elif p.suffix.lower() in IMAGE_SUFFIXES:
                refs = [p]
            else:
                raise DeliveryError(f"{p.name}: not an image, a frame pattern or a folder of frames")
        return Frames(refs, encoding, len(refs))
    if isinstance(source, (list, tuple)):
        return Frames(list(source), encoding, len(source))
    if isinstance(source, Iterable):
        return Frames(source, encoding, None)
    raise DeliveryError(f"cannot read frames from a {type(source).__name__}")


# ---------------------------------------------------------------------------- conversions


def to_display16(frame: Frame, *, with_alpha: bool) -> bytes:
    """The frame as little-endian uint16 RGB (`rgb48le`) or RGBA (`rgba64le`) bytes, display-referred
    (a linear frame goes through the sRGB OETF). Alpha is straight, as the display files store it."""
    rgb = frame.rgb
    if frame.encoding == "linear":
        from plate_pixels.ocio import srgb_oetf

        rgb = srgb_oetf(rgb).astype(np.float32)
    planes = [np.clip(rgb, 0.0, 1.0)]
    if with_alpha:
        a = frame.alpha if frame.alpha is not None else np.ones(rgb.shape[:2], np.float32)
        planes.append(np.clip(a, 0.0, 1.0)[..., None])
    out = np.concatenate(planes, axis=2) if len(planes) > 1 else planes[0]
    return np.ascontiguousarray(np.rint(out * 65535.0).astype("<u2")).tobytes()


def alpha_is_opaque(frame: Frame) -> bool:
    return frame.alpha is None or bool(np.all(frame.alpha >= 1.0 - 1e-6))
