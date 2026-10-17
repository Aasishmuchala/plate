"""Crop helpers: push-in start/end crops and 100% inspection sheets.

Consolidates amphi-plaza/cut_crops.py (16:9 push-in crop pair cut from the source view only),
couple-dining/crops.py (fractional-box 100% crop sheet) and the gate100 block of car-entry/tools_shot1_qc.py
(regions given in 3840x2160 coordinates, scaled to each image). Hard-coded paths, region tables and the macOS
font path are parameters now; labels use PIL's default font unless PLATE_FONT points at a real font file.
"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# bare name: tests/test_portability.py reads any X.open(path) as the builtin text-mode open
from PIL.Image import open as _pil_open

__all__ = ["crop_pair", "crop_tiles", "region_tiles", "label_font", "load_rgb"]

BG = (14, 14, 16)
LABEL = (212, 162, 89)
GAP = 6


def load_rgb(path: str | Path) -> Image.Image:
    """Open an image file as a fully loaded RGB image (the file handle is closed on return)."""
    with _pil_open(path) as im:
        return im.convert("RGB")


def label_font(size: int = 22) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """PLATE_FONT (a font file) if it exists, else PIL's built-in font at the requested size."""
    override = os.environ.get("PLATE_FONT", "").strip()
    if override and Path(override).is_file():
        try:
            return ImageFont.truetype(override, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def _crop_at(
    im: Image.Image, cx: float, cy: float, zoom: float
) -> tuple[Image.Image, tuple[int, int, int, int]]:
    if zoom < 1.0:
        raise ValueError(f"zoom must be >= 1.0, got {zoom}")
    w_full, h_full = im.size
    w, h = max(int(w_full / zoom) // 2 * 2, 2), max(int(h_full / zoom) // 2 * 2, 2)
    x0 = int(min(max(cx - w // 2, 0), w_full - w))
    y0 = int(min(max(cy - h // 2, 0), h_full - h))
    return im.crop((x0, y0, x0 + w, y0 + h)), (x0, y0, w, h)


def crop_pair(
    img: Image.Image, cx: float, cy: float, zoom_start: float, zoom_end: float
) -> tuple[tuple[Image.Image, tuple[int, int, int, int]], tuple[Image.Image, tuple[int, int, int, int]]]:
    """Start and end crops of a push-in, each (image, (x0, y0, w, h)) in native pixels.

    Width/height are even, the centre (cx, cy) is clamped so the box stays inside the frame (cut_crops rule).
    """
    im = img.convert("RGB")
    return _crop_at(im, cx, cy, zoom_start), _crop_at(im, cx, cy, zoom_end)


def _fit_width(im: Image.Image, width: int, only_down: bool) -> Image.Image:
    if im.width == width or (only_down and im.width <= width):
        return im
    return im.resize((width, max(int(round(im.height * width / im.width)), 1)), Image.LANCZOS)


def crop_tiles(
    images: list[Image.Image],
    boxes_frac: list[tuple[float, float, float, float]],
    labels: list[str],
    tile_w: int = 900,
) -> Image.Image:
    """Grid with one row per image and one column per fractional box (x0, y0, x1, y1 in 0-1).

    Each crop is shown at 100% and downscaled only when wider than tile_w. Width = n_boxes * (tile_w + 6).
    """
    if not images or not boxes_frac:
        raise ValueError("crop_tiles needs at least one image and one box")
    if len(labels) != len(images):
        raise ValueError("crop_tiles needs one label per image")
    font = label_font(22)
    rows = []
    for im in images:
        im = im.convert("RGB")
        w, h = im.size
        tiles = []
        for b in boxes_frac:
            c = im.crop((int(b[0] * w), int(b[1] * h), int(b[2] * w), int(b[3] * h)))
            tiles.append(_fit_width(c, tile_w, only_down=True))
        rows.append(tiles)
    th = max(t.height for tiles in rows for t in tiles)
    sheet = Image.new("RGB", (len(boxes_frac) * (tile_w + GAP), len(rows) * (th + 34)), BG)
    draw = ImageDraw.Draw(sheet)
    for r, (label, tiles) in enumerate(zip(labels, rows, strict=True)):
        y = r * (th + 34)
        draw.text((6, y + 4), label, fill=LABEL, font=font)
        for i, t in enumerate(tiles):
            sheet.paste(t, (i * (tile_w + GAP), y + 30))
    return sheet


def region_tiles(
    images: list[Image.Image],
    regions: dict[str, tuple[int, int, int, int]],
    ref_size: tuple[int, int] = (3840, 2160),
    tile_w: int = 760,
    labels: list[str] | None = None,
) -> Image.Image:
    """Grid with one row per image and one column per named region (x0, y0, x1, y1 in ref_size coordinates).

    Boxes scale from ref_size to each image's own size; every tile is resized to tile_w wide. Width =
    n_regions * (tile_w + 6).
    """
    if not images or not regions:
        raise ValueError("region_tiles needs at least one image and one region")
    labels = list(labels) if labels is not None else [f"image {i + 1}" for i in range(len(images))]
    if len(labels) != len(images):
        raise ValueError("region_tiles needs one label per image")
    font = label_font(22)
    bar = 38
    rw, rh = ref_size
    names = list(regions)
    grid = []
    for im in images:
        im = im.convert("RGB")
        sx, sy = im.width / rw, im.height / rh
        row = []
        for box in regions.values():
            c = im.crop((int(box[0] * sx), int(box[1] * sy), int(box[2] * sx), int(box[3] * sy)))
            row.append(_fit_width(c, tile_w, only_down=False))
        grid.append(row)
    th = max(t.height for row in grid for t in row)
    sheet = Image.new("RGB", (len(names) * (tile_w + GAP), len(images) * (th + bar + GAP)), BG)
    draw = ImageDraw.Draw(sheet)
    for i, row in enumerate(grid):
        for j, t in enumerate(row):
            x, y = j * (tile_w + GAP), i * (th + bar + GAP)
            sheet.paste(t, (x, y))
            draw.text((x + 6, y + th + 4), f"{labels[i][:38]} | {names[j]}", fill=LABEL, font=font)
    return sheet
