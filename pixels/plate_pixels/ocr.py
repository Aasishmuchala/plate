"""OCR boxes for the signage text-lock: where is the lettering in the plate?

    OcrBackend.detect(rgb8) -> [{"bbox": (x0, y0, x1, y1), "text": str, "conf": float}, ...]

`rgb8` is a uint8 RGB array (H, W, 3); bbox is in its pixels with x1 and y1 exclusive. The text-lock gate
(plate_gates.textlock) dilates these boxes and copies the plate's pixels back over them, so the AI can never
rewrite a sign. Boxes are advisory input to a lock: a missed word is unlocked text, a spurious box only locks a
few extra plate pixels, so backends should lean towards over-detecting.

* StubOcr is the default and the fixture backend. It reads a sidecar `<image>.ocr.json` (a JSON list of boxes,
  or {"boxes": [...]}) next to the image it was built for, or takes boxes directly; without either it finds
  nothing. No model, no weights, deterministic.
* PaddleOcr wraps PaddleOCR (Apache-2.0) behind the optional `gpu` extra. paddleocr is imported inside detect,
  so importing this module never needs it; when it is missing detect raises NotImplementedError with the install
  hint. It is written against the documented 2.x and 3.x result shapes and is verified on the render box only.

plate_pixels.backends keeps the worker's float-RGB OcrBackend for the /ocr route; this module is the uint8 API
the gates call, so the gates package stays free of the worker's licence registry.
"""

from __future__ import annotations

import json
import math
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["OcrBackend", "StubOcr", "PaddleOcr", "normalise_box", "sidecar_path", "PADDLE_HINT"]

PADDLE_HINT = (
    "PaddleOCR is not installed. It belongs to the optional 'gpu' extra: install the extra on the render box "
    "(uv sync --extra gpu) and paddlepaddle for the box's CUDA as described in pixels/requirements-cuda.txt. "
    "Without it use StubOcr (sidecar <image>.ocr.json)."
)


def normalise_box(raw: Mapping[str, Any]) -> dict[str, Any]:
    """One detection as {"bbox": (x0, y0, x1, y1) ints, "text": str, "conf": float}.

    Accepts bbox / box as four numbers (x0, y0, x1, y1) in any corner order; floats are widened outwards
    (floor of the low corner, ceil of the high one) so the integer box still covers the detection.
    """
    box = raw.get("bbox", raw.get("box"))
    if box is None or len(box) != 4:
        raise ValueError(f"OCR box needs a 4-number 'bbox', got {raw!r}")
    xa, ya, xb, yb = (float(v) for v in box)
    if not all(math.isfinite(v) for v in (xa, ya, xb, yb)):
        raise ValueError(f"OCR box has a non-finite coordinate: {raw!r}")
    bbox = (
        int(math.floor(min(xa, xb))),
        int(math.floor(min(ya, yb))),
        int(math.ceil(max(xa, xb))),
        int(math.ceil(max(ya, yb))),
    )
    return {"bbox": bbox, "text": str(raw.get("text", "")), "conf": float(raw.get("conf", 1.0))}


def sidecar_path(image_path: str | Path) -> Path:
    """`<image>.ocr.json`: the image file name with `.ocr.json` appended (plate.png -> plate.png.ocr.json)."""
    return Path(str(image_path) + ".ocr.json")


def _rgb8(rgb8: np.ndarray) -> np.ndarray:
    a = np.asarray(rgb8)
    if a.dtype != np.uint8 or a.ndim != 3 or a.shape[2] != 3:
        raise ValueError(f"detect() takes a uint8 RGB array of shape (h, w, 3), got {a.dtype} {a.shape}")
    return a


class OcrBackend(ABC):
    """Text boxes for the text-lock. Subclasses implement detect()."""

    name: str = "ocr"

    @abstractmethod
    def detect(self, rgb8: np.ndarray) -> list[dict[str, Any]]:
        """Boxes found in `rgb8` (uint8, H x W x 3): [{bbox: (x0, y0, x1, y1), text, conf}]."""


class StubOcr(OcrBackend):
    """Fixture OCR: boxes from `boxes`, else from the sidecar `<image_path>.ocr.json`, else none."""

    name = "stub"

    def __init__(
        self, image_path: str | Path | None = None, boxes: Iterable[Mapping[str, Any]] | None = None
    ):
        self.image_path = None if image_path is None else Path(image_path)
        self._boxes = None if boxes is None else [normalise_box(b) for b in boxes]

    def detect(self, rgb8: np.ndarray) -> list[dict[str, Any]]:
        _rgb8(rgb8)
        if self._boxes is not None:
            return [dict(b) for b in self._boxes]
        if self.image_path is None:
            return []
        sidecar = sidecar_path(self.image_path)
        if not sidecar.is_file():
            return []
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            raise ValueError(f"{sidecar}: not valid JSON ({exc})") from exc
        if isinstance(data, Mapping):
            data = data.get("boxes")
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
            raise ValueError(f"{sidecar}: expected a JSON list of boxes")
        return [normalise_box(b) for b in data]


class PaddleOcr(OcrBackend):
    """PaddleOCR text detection + recognition (render box, `gpu` extra). Unverified on this Mac."""

    name = "paddleocr"

    def __init__(self, lang: str = "en"):
        self.lang = lang
        self._engine: Any = None

    def _load(self) -> Any:
        if self._engine is None:
            try:
                from paddleocr import PaddleOCR
            except ImportError as exc:
                raise NotImplementedError(PADDLE_HINT) from exc
            self._engine = PaddleOCR(lang=self.lang)
        return self._engine

    def detect(self, rgb8: np.ndarray) -> list[dict[str, Any]]:
        img = _rgb8(rgb8)
        engine = self._load()
        bgr = np.ascontiguousarray(img[..., ::-1])  # PaddleOCR reads OpenCV-style BGR
        if hasattr(engine, "predict"):  # 3.x: list of result mappings with rec_texts / rec_scores / rec_polys
            found = []
            for res in engine.predict(bgr):
                texts = res.get("rec_texts", [])
                scores = res.get("rec_scores", [])
                polys = res.get("rec_polys", res.get("dt_polys", []))
                for text, score, poly in zip(texts, scores, polys, strict=False):
                    found.append((poly, text, score))
        else:  # 2.x: [[ [quad, (text, conf)], ... ]]
            found = []
            for page in engine.ocr(bgr, cls=False) or []:
                for quad, (text, score) in page or []:
                    found.append((quad, text, score))
        boxes = []
        for poly, text, score in found:
            pts = np.asarray(poly, dtype=np.float64).reshape(-1, 2)
            boxes.append(
                normalise_box(
                    {
                        "bbox": (pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max()),
                        "text": text,
                        "conf": float(score),
                    }
                )
            )
        return boxes
