"""Push-in crop pair: the start and end crops of a move, cut from the source view only.

cut_crops is amphi-plaza/cut_crops.py byte for byte in its geometry: w = int(W / z) // 2 * 2 (even), centred on
(cx, cy), clamped inside the frame, no resampling (native pixels). The one addition is validation of the zoom
pair, 1.0 <= z0 < z1 <= 3.0: a push-in goes in, never out, and past 3x a 4K plate stops being a crop of the
render and becomes a magnifier. The reference case: relit 5504x3072 at (3633, 2174), 1.5x -> 2.3x gives boxes
(1799, 1024, 3668, 2048) and (2437, 1507, 2392, 1334) as (x0, y0, w, h).
"""

from __future__ import annotations

import numpy as np
from PIL import Image

__all__ = ["Z_MIN", "Z_MAX", "box_for", "validate_zooms", "cut_crops"]

Z_MIN = 1.0
Z_MAX = 3.0

Box = tuple[int, int, int, int]  # (x0, y0, w, h) in source pixels


def box_for(width: int, height: int, cx: float, cy: float, zoom: float) -> Box:
    """(x0, y0, w, h) of the centred, clamped, even-sized crop at `zoom` (cut_crops geometry)."""
    w, h = int(width / zoom) // 2 * 2, int(height / zoom) // 2 * 2
    x0 = int(min(max(cx - w // 2, 0), width - w))
    y0 = int(min(max(cy - h // 2, 0), height - h))
    return x0, y0, w, h


def validate_zooms(z0: float, z1: float) -> None:
    """Raise ValueError unless 1.0 <= z0 < z1 <= 3.0."""
    if not (Z_MIN <= z0 < z1 <= Z_MAX):
        raise ValueError(f"zoom pair must satisfy {Z_MIN} <= z0 < z1 <= {Z_MAX}, got {z0} -> {z1}")


def cut_crops(
    img: Image.Image | np.ndarray, cx: float, cy: float, z0: float, z1: float
) -> tuple[Image.Image | np.ndarray, Image.Image | np.ndarray, tuple[Box, Box]]:
    """(start, end, (start_box, end_box)) for a push-in from zoom z0 to z1 centred on (cx, cy).

    A PIL image in gives PIL crops out; an ndarray (H, W[, C]) in gives ndarray copies. Boxes are
    (x0, y0, w, h) in native pixels. Raises ValueError for a zoom pair outside 1.0 <= z0 < z1 <= 3.0.
    """
    validate_zooms(z0, z1)
    if isinstance(img, Image.Image):
        width, height = img.size
    else:
        height, width = np.asarray(img).shape[:2]
    boxes = (box_for(width, height, cx, cy, z0), box_for(width, height, cx, cy, z1))
    crops = []
    for x0, y0, w, h in boxes:
        if isinstance(img, Image.Image):
            crops.append(img.crop((x0, y0, x0 + w, y0 + h)))
        else:
            crops.append(np.ascontiguousarray(np.asarray(img)[y0 : y0 + h, x0 : x0 + w]))
    return crops[0], crops[1], boxes
