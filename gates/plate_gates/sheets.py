"""Contact sheets and the push-in landing gate.

Consolidates the contact-sheet block of car-entry/tools_v4_gate.py and couple-dining/pushin_gate.py
(historical project folders). pushin_gate reads the clip through OpenCV (no ffmpeg binary), compares the first
frame with the start still and the last frame with the end crop, and optionally writes the first|last over
start|end sheet for a visual check.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .crops import BG, LABEL, label_font, load_rgb
from .video import frame_at, probe

__all__ = ["contact_sheet", "pushin_gate"]


def contact_sheet(
    frames: list[Image.Image], labels: list[str], cols: int = 3, tile_w: int = 960, bar: int = 30
) -> Image.Image:
    """Grid of frames resized to tile_w wide, each with a label strip of height `bar` above it."""
    if not frames:
        raise ValueError("contact_sheet needs at least one frame")
    if len(labels) != len(frames):
        raise ValueError("contact_sheet needs one label per frame")
    font = label_font(22)
    tiles = []
    for f in frames:
        f = f.convert("RGB")
        tiles.append(f.resize((tile_w, max(int(round(tile_w * f.height / f.width)), 1)), Image.LANCZOS))
    th = max(t.height for t in tiles)
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tile_w, rows * (th + bar)), BG)
    draw = ImageDraw.Draw(sheet)
    for i, (t, label) in enumerate(zip(tiles, labels, strict=True)):
        x, y = (i % cols) * tile_w, (i // cols) * (th + bar)
        sheet.paste(t, (x, y + bar))
        draw.text((x + 8, y + 4), label, fill=LABEL, font=font)
    return sheet


def _as_rgb(src: str | Path | Image.Image | np.ndarray) -> Image.Image:
    if isinstance(src, Image.Image):
        return src.convert("RGB")
    if isinstance(src, np.ndarray):
        return Image.fromarray(src).convert("RGB")
    return load_rgb(src)


def _mad_255(a: Image.Image, b: Image.Image) -> float:
    return float(np.abs(np.asarray(a, np.float32) - np.asarray(b, np.float32)).mean())


def pushin_gate(
    clip: Path,
    start_still: str | Path | Image.Image | np.ndarray,
    end_crop: str | Path | Image.Image | np.ndarray,
    sheet_out: Path | None = None,
) -> dict:
    """Landing gate for a start+end-frame push-in clip.

    Mean abs diff (0-255 levels) of the first frame (t=0) vs the start still, the last frame (t=dur-0.05) vs
    the end crop, and the last frame vs the start still (which should be larger than vs the end crop); stills
    are resized to the clip size with Lanczos. sheet_out, when given, gets first|last over start|end.
    """
    clip = Path(clip)
    info = probe(clip)
    dur = info["duration_s"]
    f0 = Image.fromarray(frame_at(clip, 0.0))
    f1 = Image.fromarray(frame_at(clip, max(dur - 0.05, 0.0)))
    size = f0.size
    s = _as_rgb(start_still).resize(size, Image.LANCZOS)
    e = _as_rgb(end_crop).resize(size, Image.LANCZOS)
    if sheet_out is not None:
        labels = ["clip first frame", "clip last frame", "start still", "end crop"]
        contact_sheet([f0, f1, s, e], labels, cols=2, tile_w=960).save(sheet_out, quality=88)
    return {
        "duration_s": dur,
        "size": size,
        "first_vs_start_255": _mad_255(f0, s),
        "last_vs_end_255": _mad_255(f1, e),
        "last_vs_start_255": _mad_255(f1, s),
    }
