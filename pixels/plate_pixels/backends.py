"""Model backends for the pixels service: one small class per route, each with a graceful STUB fallback.

Seeded from ``cv_sidecar/backends.py``. Stub mode is the default: a backend answers with a neutral,
clearly-flagged ``"stub": true`` result and imports no ``torch``. ``PLATE_PIXELS_REAL=1`` flips the
contract: a backend whose packages or weights are missing raises ``ModelUnavailable`` at construction
instead of stubbing. Availability is probed with ``importlib.util.find_spec`` (which does not import the
package), so even the real-mode probe never pulls ``torch`` into this process. Real inference is not wired
in this scaffold; it lands with the separate ``cuda`` extra on the render box (docs/pixels.md).

Every backend calls ``assert_commercial_safe`` in ``__init__``, so a blocked model can never be built.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any, ClassVar

from .licences import assert_commercial_safe

REAL_ENV = "PLATE_PIXELS_REAL"
MODELS_ENV = "PLATE_MODELS"


class ModelUnavailable(RuntimeError):
    """A model is not installed or its weights are missing and ``PLATE_PIXELS_REAL=1``."""


class InputError(ValueError):
    """The request referenced an image that is missing or unreadable (HTTP 400)."""


def real_required() -> bool:
    return os.environ.get(REAL_ENV) == "1"


def models_dir() -> Path:
    """Model cache root: ``PLATE_MODELS`` if set, else the per-user cache folder (Windows and macOS)."""
    override = os.environ.get(MODELS_ENV)
    if override:
        return Path(override)
    from platformdirs import user_cache_dir

    return Path(user_cache_dir("Plate", appauthor=False)) / "models"


def image_shape(path: str) -> tuple[int, int]:
    """(height, width) of an image, read from the header only."""
    # Pillow's open() is binary; imported by name so the portability lint's text-mode heuristic skips it.
    from PIL import UnidentifiedImageError
    from PIL.Image import open as open_image

    try:
        with open_image(path) as im:
            width, height = im.size
    except (OSError, UnidentifiedImageError) as exc:
        raise InputError(f"cannot read image {path!r}: {exc}") from exc
    return height, width


class Backend:
    """Base class: licence gate, stub/real switch, and the stub result envelope."""

    model_id: ClassVar[str]
    packages: ClassVar[tuple[str, ...]] = ()

    def __init__(self) -> None:
        assert_commercial_safe(self.model_id)
        self.available = False
        if real_required():
            self._require_real()
            self.available = True

    def _require_real(self) -> None:
        missing = [name for name in self.packages if importlib.util.find_spec(name) is None]
        if missing:
            raise ModelUnavailable(f"{self.model_id}: not installed ({', '.join(missing)}) and {REAL_ENV}=1")
        weights = models_dir() / self.model_id
        if not weights.is_dir():
            raise ModelUnavailable(f"{self.model_id}: no weights at {weights} and {REAL_ENV}=1")

    def stub(self, **fields: Any) -> dict[str, Any]:
        return {"stub": True, "model": self.model_id, **fields}

    def _run_real(self) -> dict[str, Any]:
        raise NotImplementedError(f"{self.model_id}: real inference is wired with the cuda extra, not yet")


class Depth(Backend):
    """Depth Anything V2 Small: relative depth for composition and layer comparison."""

    model_id = "depth_anything_v2_small"
    packages = ("torch", "transformers")

    def infer(self, png: str) -> dict[str, Any]:
        height, width = image_shape(png)
        if not self.available:
            return self.stub(shape=[height, width], depth_png=None)
        return self._run_real()


class Calibration(Backend):
    """GeoCalib: focal length, roll and pitch from a single image."""

    model_id = "geocalib"
    packages = ("torch", "geocalib")

    def calibrate(self, png: str) -> dict[str, Any]:
        if not self.available:
            return self.stub(fov_deg=60.0, roll_deg=0.0, pitch_deg=0.0)
        return self._run_real()


class Segment(Backend):
    """SAM 2.1: point-prompted masks."""

    model_id = "sam2.1"
    packages = ("torch", "sam2")

    def segment(self, png: str, points: list[list[float]]) -> dict[str, Any]:
        height, width = image_shape(png)
        if not self.available:
            return self.stub(mask_png=None, shape=[height, width])
        return self._run_real()


class Flow(Backend):
    """RAFT optical flow between two frames (evaluated at a fixed width)."""

    model_id = "raft"
    packages = ("torch", "torchvision")

    def flow(self, a: str, b: str) -> dict[str, Any]:
        if not self.available:
            return self.stub(eval_width=960, median_px=0.0)
        return self._run_real()


class TextLock(Backend):
    """PaddleOCR: text boxes for the signage text-lock."""

    model_id = "paddleocr"
    packages = ("paddleocr",)

    def boxes(self, png: str) -> dict[str, Any]:
        if not self.available:
            return self.stub(boxes=[])
        return self._run_real()


class Upscale(Backend):
    """Real-ESRGAN: AI layers only, never the plate (BUILD_PLAN section 1)."""

    model_id = "real_esrgan"
    packages = ("torch", "realesrgan")

    def upscale(self, png: str, scale: float) -> dict[str, Any]:
        if not self.available:
            return self.stub(note="AI layers only; never the plate")
        return self._run_real()


class Lpips(Backend):
    """LPIPS perceptual distance between two images."""

    model_id = "lpips"
    packages = ("torch", "lpips")

    def distance(self, a: str, b: str) -> dict[str, Any]:
        if not self.available:
            return self.stub(distance=None)
        return self._run_real()


class Backends:
    """The set of backends one server instance owns (built lazily, never at import time)."""

    def __init__(self) -> None:
        self.depth = Depth()
        self.calibration = Calibration()
        self.segment = Segment()
        self.flow = Flow()
        self.textlock = TextLock()
        self.upscale = Upscale()
        self.lpips = Lpips()

    def all(self) -> list[Backend]:
        return [
            self.depth,
            self.calibration,
            self.segment,
            self.flow,
            self.textlock,
            self.upscale,
            self.lpips,
        ]

    def by_model_id(self) -> dict[str, Backend]:
        return {backend.model_id: backend for backend in self.all()}
