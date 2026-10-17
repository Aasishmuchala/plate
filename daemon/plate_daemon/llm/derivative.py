"""Pixel derivatives for the judge: what an LLM is allowed to see of a plate and a candidate.

Decision 3/8 and custody.md: on an NDA project the judge sees only downscaled, cropped, metadata-stripped
derivatives. `build_pairs` makes them:

* crop to the gate-flagged tiles (fractional boxes of the plate frame, as `plate_gates.crops.crop_tiles` reads
  them); with no tiles, the whole frame;
* downscale only, never upscale: an image is shrunk to at most `max_width` wide (1024 for NDA, 1536
  otherwise) and a crop narrower than that is sent at its native size;
* re-encode as an 8-bit sRGB PNG with no ancillary chunks (no text, no EXIF, no time, no ICC profile) and run
  it through `strip_metadata_png` as well;
* no file names: the images are labelled "plate crop 2/3" and the derivative carries only those labels.

The plate is the ground truth and the candidate is cropped by the same fractions, so a candidate whose size
differs from the plate's still shows the same region.
"""

from __future__ import annotations

import base64
import io
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from plate_pixels import imageio
from plate_pixels.icc import strip_metadata_png

__all__ = [
    "MAX_WIDTH_NDA",
    "MAX_WIDTH_OPEN",
    "Derivative",
    "build_pairs",
    "image_block",
    "load_rgb8",
    "max_width",
    "to_png",
]

MAX_WIDTH_NDA = 1024
MAX_WIDTH_OPEN = 1536

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class Derivative:
    """One image headed for the model: a label (never a file name), PNG bytes and its pixel size."""

    label: str
    png: bytes
    width: int
    height: int


def max_width(nda: bool) -> int:
    return MAX_WIDTH_NDA if nda else MAX_WIDTH_OPEN


def load_rgb8(path: str | Path) -> Image.Image:
    """Any image `plate_pixels.imageio.read` understands (8/16-bit PNG, TIFF, JPEG) as an 8-bit RGB image.
    Alpha is dropped; a 16-bit or float source is clipped to [0, 1] and quantised."""
    img = imageio.read(path)
    arr = np.clip(img.rgb, 0.0, 1.0)
    return Image.fromarray(np.rint(arr * 255.0).astype(np.uint8))


def _check_box(box: Sequence[float]) -> Box:
    if len(box) != 4:
        raise ValueError(f"a crop box is (x0, y0, x1, y1) as fractions of the frame, got {box!r}")
    x0, y0, x1, y1 = (float(v) for v in box)
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        raise ValueError(f"crop box {box!r} must satisfy 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1 (fractions)")
    return x0, y0, x1, y1


def _crop(im: Image.Image, box: Box) -> Image.Image:
    x0, y0, x1, y1 = box
    left, top = int(math.floor(x0 * im.width)), int(math.floor(y0 * im.height))
    right = max(int(math.ceil(x1 * im.width)), left + 1)
    bottom = max(int(math.ceil(y1 * im.height)), top + 1)
    return im.crop((left, top, min(right, im.width), min(bottom, im.height)))


def _shrink(im: Image.Image, width: int) -> Image.Image:
    if im.width <= width:
        return im
    return im.resize((width, max(round(im.height * width / im.width), 1)), Image.Resampling.LANCZOS)


def to_png(im: Image.Image) -> bytes:
    """An 8-bit RGB PNG with only image chunks (PIL writes none of the metadata ones; the strip is belt and braces)."""
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="PNG", optimize=False)
    return strip_metadata_png(buf.getvalue())


def build_pairs(
    plate_path: str | Path,
    candidate_path: str | Path,
    crops: Sequence[Sequence[float]] = (),
    *,
    nda: bool,
) -> list[Derivative]:
    """[plate crop 1/n, candidate crop 1/n, plate crop 2/n, ...] for the judge, or the whole frame pair
    when `crops` is empty. Raises ValueError for a malformed box."""
    width = max_width(nda)
    plate = load_rgb8(plate_path)
    candidate = load_rgb8(candidate_path)
    boxes = [_check_box(b) for b in crops] or [(0.0, 0.0, 1.0, 1.0)]
    out: list[Derivative] = []
    n = len(boxes)
    for i, box in enumerate(boxes, 1):
        tag = f" {i}/{n}" if n > 1 or crops else ""
        scope = "crop" if crops else "frame"
        for who, im in (("plate", plate), ("candidate", candidate)):
            tile = _shrink(_crop(im, box), width)
            out.append(Derivative(f"{who} {scope}{tag}", to_png(tile), tile.width, tile.height))
    return out


def image_block(d: Derivative) -> dict:
    """The Messages API image content block for a derivative."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(d.png).decode("ascii"),
        },
    }
