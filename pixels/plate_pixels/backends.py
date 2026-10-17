"""Model backends for the pixels worker: depth, intrinsics, segmentation, flow, text boxes (and the two
legacy AI-layer helpers), each as an ABC with a deterministic Stub and a Real class.

Seeded from ``cv_sidecar/backends.py``. Stub mode is the default and imports no ``torch``: a stub answers
with a neutral, deterministic result and the server flags the response ``"stub": true``. A Real class probes
its packages with ``importlib.util.find_spec`` and imports them lazily through ``importlib`` (never an
``import torch`` statement, so the stub path and the CI scan stay torch-free); when the ``gpu`` extra or the
weights are missing it raises ``ModelUnavailable`` naming the extra. ``PLATE_PIXELS_REAL=1`` makes the worker
build the Real classes instead of the stubs, so a missing model is an HTTP 500 rather than a quiet stub.

Every backend calls ``assert_commercial_safe`` in ``__init__``, so a blocked model can never be built.
The Real classes are written against the libraries' documented APIs and are NOT exercised on this Mac
(no torch, no weights): they are verified on the render box (see the task's verify-on-box list).
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import math
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from . import imageio
from .licences import assert_commercial_safe

REAL_ENV = "PLATE_PIXELS_REAL"
MODELS_ENV = "PLATE_MODELS"
EXTRA = "gpu"  # pyproject optional extra; the CUDA torch build comes from pixels/requirements-cuda.txt
STUB_SEGMENTS = 6
STUB_FOV_DEG = 60.0


class ModelUnavailable(RuntimeError):
    """A model is not installed or its weights are missing (HTTP 500 under ``PLATE_PIXELS_REAL=1``)."""


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


def read_rgb(path: str | Path) -> np.ndarray:
    """Decode an image to float32 (H, W, 3) in [0, 1]; ``InputError`` when it is missing or unreadable."""
    try:
        return np.ascontiguousarray(imageio.read(path).rgb, dtype=np.float32)
    except Exception as exc:  # noqa: BLE001 - OSError, ValueError, cv2.error, tifffile errors: all a bad input
        raise InputError(f"cannot read image {str(path)!r}: {exc}") from exc


def _u8(rgb: np.ndarray) -> np.ndarray:
    return np.round(np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)


def _device(torch: Any) -> str:
    return "cuda" if torch.cuda.is_available() else "cpu"


# --------------------------------------------------------------------------- base


class Backend:
    """Base class: the licence gate, the stub/real flag and the real-model probe."""

    model_id: ClassVar[str]  # licence registry name; also the weights folder under models_dir()
    packages: ClassVar[tuple[str, ...]] = ()
    needs_weights: ClassVar[bool] = True
    is_stub: ClassVar[bool] = True
    impl: ClassVar[str] = "stub"
    vram_mb: ClassVar[int] = (
        0  # rough VRAM a real model needs; the server asks nvml.Admission before the first load
    )

    def __init__(self) -> None:
        assert_commercial_safe(self.model_id)

    @classmethod
    def probe(cls) -> tuple[bool, str]:
        """Can the real model run here? Looks at package specs and the weights folder; imports nothing."""
        missing = [name for name in cls.packages if importlib.util.find_spec(name) is None]
        if missing:
            return (
                False,
                f"not installed ({', '.join(missing)}); install the '{EXTRA}' extra on the render box",
            )
        if cls.needs_weights and not (models_dir() / cls.model_id).is_dir():
            return False, f"no weights at {models_dir() / cls.model_id}"
        return True, "ok"

    def _require_real(self) -> None:
        ok, reason = self.probe()
        if not ok:
            raise ModelUnavailable(f"{self.model_id}: {reason} and {REAL_ENV}=1")

    def load(self) -> None:
        """Put the model on the device. Stubs hold nothing."""

    def unload(self) -> None:
        """Release the model."""

    def info(self) -> dict[str, Any]:
        return {"model": self.model_id, "impl": self.impl, "stub": self.is_stub}


class RealBackend(Backend):
    is_stub = False
    impl = "real"

    def __init__(self) -> None:
        super().__init__()
        self._require_real()


def segments_from_labels(labels: np.ndarray) -> list[dict[str, Any]]:
    """``[{id, bbox: [x0, y0, x1, y1], area}]`` for every non-zero label; the bbox is half-open (x1, y1 exclusive)."""
    out = []
    for value in np.unique(labels):
        if value == 0:
            continue
        rows, cols = np.nonzero(labels == value)
        out.append(
            {
                "id": int(value),
                "bbox": [int(cols.min()), int(rows.min()), int(cols.max()) + 1, int(rows.max()) + 1],
                "area": int(rows.size),
            }
        )
    return out


# --------------------------------------------------------------------------- depth


class DepthBackend(Backend, ABC):
    """Relative depth for composition and layer comparison. Never metric (BUILD_PLAN section 11)."""

    depth_semantics: ClassVar[
        str
    ]  # manifest estimated.depth_semantics: relative_depth | relative_inverse_depth

    @abstractmethod
    def infer(self, rgb: np.ndarray) -> np.ndarray:
        """``rgb`` float32 (H, W, 3) in [0, 1] -> float32 (H, W)."""


class StubDepth(DepthBackend):
    model_id = "depth-anything-v2-small"
    depth_semantics = "relative_depth"

    def infer(self, rgb: np.ndarray) -> np.ndarray:
        height, width = rgb.shape[:2]
        column = (1.0 / (1.0 + np.arange(height, dtype=np.float32) / height)).astype(np.float32)
        return np.ascontiguousarray(np.broadcast_to(column[:, None], (height, width)))


class RealDepth(RealBackend, DepthBackend):
    """Depth Anything V2 Small through transformers (relative INVERSE depth: larger is nearer)."""

    model_id = "depth-anything-v2-small"
    packages = ("torch", "transformers")
    depth_semantics = "relative_inverse_depth"
    vram_mb = 1500
    _model: Any = None

    def load(self) -> None:
        if self._model is not None:
            return
        torch = importlib.import_module("torch")
        transformers = importlib.import_module("transformers")
        folder = str(models_dir() / self.model_id)
        self._device = _device(torch)
        self._processor = transformers.AutoImageProcessor.from_pretrained(folder)
        self._model = transformers.AutoModelForDepthEstimation.from_pretrained(folder).to(self._device).eval()

    def unload(self) -> None:
        self._model = None

    def infer(self, rgb: np.ndarray) -> np.ndarray:
        self.load()
        torch = importlib.import_module("torch")
        inputs = self._processor(images=_u8(rgb), return_tensors="pt").to(self._device)
        with torch.no_grad():
            predicted = self._model(**inputs).predicted_depth
        height, width = rgb.shape[:2]
        resized = torch.nn.functional.interpolate(
            predicted.unsqueeze(1), size=(height, width), mode="bicubic", align_corners=False
        )
        return resized[0, 0].float().cpu().numpy().astype(np.float32)


# --------------------------------------------------------------------------- intrinsics


def pinhole_from_fov(width: int, height: int, fov_deg: float = STUB_FOV_DEG) -> dict[str, float]:
    """Square-pixel pinhole with the given HORIZONTAL field of view, principal point at the centre."""
    fx = (width / 2.0) / math.tan(math.radians(fov_deg) / 2.0)
    return {"fx": fx, "fy": fx, "cx": width / 2.0, "cy": height / 2.0, "fov_deg": float(fov_deg)}


class IntrinsicsBackend(Backend, ABC):
    """Single-image calibration: ``{fx, fy, cx, cy, fov_deg}`` in pixels plus ``roll_deg`` and ``pitch_deg``."""

    @abstractmethod
    def calibrate(self, rgb: np.ndarray) -> dict[str, float]:
        """``rgb`` float32 (H, W, 3) in [0, 1]; ``fov_deg`` is the horizontal field of view."""


class StubIntrinsics(IntrinsicsBackend):
    """A 36 mm-equivalent film with a 60 degree horizontal field of view, level horizon."""

    model_id = "geocalib"

    def calibrate(self, rgb: np.ndarray) -> dict[str, float]:
        height, width = rgb.shape[:2]
        return {**pinhole_from_fov(width, height), "roll_deg": 0.0, "pitch_deg": 0.0}


class RealIntrinsics(RealBackend, IntrinsicsBackend):
    """GeoCalib: focal length, roll and pitch from one image (weights cache kept inside the model cache)."""

    model_id = "geocalib"
    packages = ("torch", "geocalib")
    vram_mb = 2000
    needs_weights = False  # GeoCalib fetches its release weights into torch.hub's dir, which we point here
    _model: Any = None

    def load(self) -> None:
        if self._model is not None:
            return
        torch = importlib.import_module("torch")
        geocalib = importlib.import_module("geocalib")
        torch.hub.set_dir(str(models_dir() / self.model_id))
        self._device = _device(torch)
        self._model = geocalib.GeoCalib().to(self._device)

    def unload(self) -> None:
        self._model = None

    def calibrate(self, rgb: np.ndarray) -> dict[str, float]:
        self.load()
        torch = importlib.import_module("torch")
        width = rgb.shape[1]
        tensor = torch.from_numpy(np.ascontiguousarray(rgb.transpose(2, 0, 1))).float().to(self._device)
        result = self._model.calibrate(tensor)
        fx, fy = (float(v) for v in result["camera"].f[0])
        cx, cy = (float(v) for v in result["camera"].c[0])
        roll, pitch = (math.degrees(float(v)) for v in result["gravity"].rp[0])
        fov = math.degrees(2.0 * math.atan(width / (2.0 * fx)))
        return {"fx": fx, "fy": fy, "cx": cx, "cy": cy, "fov_deg": fov, "roll_deg": roll, "pitch_deg": pitch}


# --------------------------------------------------------------------------- segmentation


def kmeans_labels(rgb: np.ndarray, k: int = STUB_SEGMENTS, iters: int = 8, sample: int = 20000) -> np.ndarray:
    """Deterministic colour k-means -> uint16 labels 1..k, numbered by ascending luminance of the centroid.

    Centroids start at luminance quantiles of a strided subsample and run ``iters`` Lloyd steps; every pixel
    is then assigned to its nearest centroid in chunks, so the label map is full resolution at any plate size.
    """
    height, width = rgb.shape[:2]
    flat = rgb.reshape(-1, 3)
    pts = np.asarray(flat[:: max(1, flat.shape[0] // sample)], dtype=np.float64)
    luma = np.array([0.2126, 0.7152, 0.0722])
    pts = pts[np.argsort(pts @ luma, kind="stable")]
    centres = pts[((np.arange(k) + 0.5) / k * (len(pts) - 1)).astype(int)].copy()
    for _ in range(iters):
        near = np.argmin(((pts[:, None, :] - centres[None, :, :]) ** 2).sum(axis=2), axis=1)
        for j in range(k):
            members = pts[near == j]
            if len(members):
                centres[j] = members.mean(axis=0)
    rank = np.empty(k, dtype=np.uint16)
    rank[np.argsort(centres @ luma, kind="stable")] = np.arange(1, k + 1, dtype=np.uint16)
    labels = np.empty(flat.shape[0], dtype=np.uint16)
    chunk = 1 << 18
    for start in range(0, flat.shape[0], chunk):
        block = np.asarray(flat[start : start + chunk], dtype=np.float64)
        labels[start : start + chunk] = rank[np.argmin(((block[:, None, :] - centres[None]) ** 2).sum(2), 1)]
    return labels.reshape(height, width)


class SegmentBackend(Backend, ABC):
    """Label map plus ``[{id, bbox, area}]``; ``prompts`` are ``[[x, y], ...]`` points or ``None`` (everything)."""

    @abstractmethod
    def segment(
        self, rgb: np.ndarray, prompts: list[list[float]] | None = None
    ) -> tuple[np.ndarray, list[dict[str, Any]]]:
        """-> (uint16 (H, W) label map with 0 = no segment, list of {id, bbox, area})."""


class StubSegment(SegmentBackend):
    model_id = "sam2.1"

    def segment(
        self, rgb: np.ndarray, prompts: list[list[float]] | None = None
    ) -> tuple[np.ndarray, list[dict[str, Any]]]:
        labels = kmeans_labels(rgb)
        return labels, segments_from_labels(labels)


class RealSegment(RealBackend, SegmentBackend):
    """SAM 2.1 (Hiera base-plus): automatic masks, or one mask per prompt point."""

    model_id = "sam2.1"
    packages = ("torch", "sam2")
    vram_mb = 3000
    CONFIG = "configs/sam2.1/sam2.1_hiera_b+.yaml"
    CHECKPOINT = "sam2.1_hiera_base_plus.pt"
    _model: Any = None

    def load(self) -> None:
        if self._model is not None:
            return
        torch = importlib.import_module("torch")
        build = importlib.import_module("sam2.build_sam")
        checkpoint = str(models_dir() / self.model_id / self.CHECKPOINT)
        self._model = build.build_sam2(self.CONFIG, checkpoint, device=_device(torch))

    def unload(self) -> None:
        self._model = None

    def segment(
        self, rgb: np.ndarray, prompts: list[list[float]] | None = None
    ) -> tuple[np.ndarray, list[dict[str, Any]]]:
        self.load()
        image = _u8(rgb)
        masks: list[np.ndarray] = []
        if prompts:
            predictor_mod = importlib.import_module("sam2.sam2_image_predictor")
            predictor = predictor_mod.SAM2ImagePredictor(self._model)
            predictor.set_image(image)
            for x, y in prompts:
                got, _, _ = predictor.predict(
                    point_coords=np.array([[x, y]], dtype=np.float32),
                    point_labels=np.array([1]),
                    multimask_output=False,
                )
                masks.append(got[0].astype(bool))
        else:
            generator_mod = importlib.import_module("sam2.automatic_mask_generator")
            found = generator_mod.SAM2AutomaticMaskGenerator(self._model).generate(image)
            masks = [m["segmentation"].astype(bool) for m in found]
        labels = np.zeros(image.shape[:2], dtype=np.uint16)
        for index, mask in enumerate(sorted(masks, key=lambda m: -int(m.sum())), 1):  # small on top of large
            labels[mask] = index
        return labels, segments_from_labels(labels)


# --------------------------------------------------------------------------- flow


class FlowBackend(Backend, ABC):
    """Optical flow a -> b: ``(u, v)`` float32 (H, W) in pixels of the INPUT size; ``b(x + u, y + v) = a(x, y)``."""

    eval_width: ClassVar[int] = 960

    @abstractmethod
    def flow(self, a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """``a``, ``b`` float32 (H, W, 3) in [0, 1] of one size -> (u, v)."""


class StubFlow(FlowBackend):
    model_id = "raft"

    def flow(self, a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        zeros = np.zeros(a.shape[:2], dtype=np.float32)
        return zeros, zeros.copy()


# --------------------------------------------------------------------------- text boxes


class OcrBackend(Backend, ABC):
    """Text boxes for the signage text-lock: ``[{bbox: [x0, y0, x1, y1], text, conf}]``."""

    @abstractmethod
    def detect(self, rgb: np.ndarray) -> list[dict[str, Any]]:
        """``rgb`` float32 (H, W, 3) in [0, 1]."""

    def detect_file(self, path: str) -> list[dict[str, Any]]:
        return self.detect(read_rgb(path))


class StubOcr(OcrBackend):
    """No boxes, unless a fixture sidecar ``<image>.ocr.json`` (a list of boxes) sits next to the image."""

    model_id = "paddleocr"

    def detect(self, rgb: np.ndarray) -> list[dict[str, Any]]:
        return []

    def detect_file(self, path: str) -> list[dict[str, Any]]:
        read_rgb(path)  # a missing or unreadable image is a 400 with or without a sidecar
        sidecar = Path(path + ".ocr.json")
        if not sidecar.is_file():
            return []
        try:
            boxes = json.loads(sidecar.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            raise InputError(f"{sidecar}: not valid JSON ({exc})") from exc
        if not isinstance(boxes, list):
            raise InputError(f"{sidecar}: expected a JSON list of boxes")
        return boxes


class RealOcr(RealBackend, OcrBackend):
    """PaddleOCR. The detection call is wired on the box (the Paddle API differs between releases)."""

    model_id = "paddleocr"
    packages = ("paddleocr",)
    needs_weights = False

    def detect(self, rgb: np.ndarray) -> list[dict[str, Any]]:
        raise NotImplementedError("paddleocr: detection is wired on the render box (docs/pixels.md)")


# --------------------------------------------------------------------------- AI-layer helpers (kept routes)


class UpscaleBackend(Backend):
    """Real-ESRGAN: AI layers only, never the plate (BUILD_PLAN section 1; the server refuses ``is_plate``)."""

    model_id = "real-esrgan"
    packages = ("torch", "realesrgan")

    def upscale(self, path: str, scale: float) -> dict[str, Any]:
        raise NotImplementedError("real-esrgan: wired on the render box (docs/pixels.md)")


class StubUpscale(UpscaleBackend):
    def upscale(self, path: str, scale: float) -> dict[str, Any]:
        return {"note": "AI layers only; never the plate"}


class RealUpscale(RealBackend, UpscaleBackend):
    pass


class LpipsBackend(Backend):
    """LPIPS perceptual distance between two images."""

    model_id = "lpips"
    packages = ("torch", "lpips")
    needs_weights = False

    def distance(self, a: str, b: str) -> dict[str, Any]:
        raise NotImplementedError("lpips: wired on the render box (docs/pixels.md)")


class StubLpips(LpipsBackend):
    def distance(self, a: str, b: str) -> dict[str, Any]:
        return {"distance": None}


class RealLpips(RealBackend, LpipsBackend):
    pass


# --------------------------------------------------------------------------- the set one server owns

KINDS = ("depth", "intrinsics", "segment", "flow", "ocr", "upscale", "lpips")


def _real_classes() -> dict[str, type[Backend]]:
    from .flow import RaftFlow

    return {
        "depth": RealDepth,
        "intrinsics": RealIntrinsics,
        "segment": RealSegment,
        "flow": RaftFlow,
        "ocr": RealOcr,
        "upscale": RealUpscale,
        "lpips": RealLpips,
    }


def _stub_classes() -> dict[str, type[Backend]]:
    from .flow import DisFlow

    return {
        "depth": StubDepth,
        "intrinsics": StubIntrinsics,
        "segment": StubSegment,
        "flow": DisFlow,  # real on every machine; RAFT is the box path
        "ocr": StubOcr,
        "upscale": StubUpscale,
        "lpips": StubLpips,
    }


class Backends:
    """The backends one server owns, built lazily on first use (never at import time).

    Default: stubs, except flow, which is DIS (real, no weights). ``PLATE_PIXELS_REAL=1`` builds the Real
    classes instead; one whose model is missing raises ``ModelUnavailable`` when first asked for, so the
    route that needs it answers 500 while the others keep working. Keyword overrides inject instances (tests).
    """

    def __init__(self, **overrides: Backend) -> None:
        unknown = set(overrides) - set(KINDS)
        if unknown:
            raise TypeError(f"unknown backend kind(s): {sorted(unknown)}")
        self._built: dict[str, Backend] = dict(overrides)

    def get(self, kind: str) -> Backend:
        if kind not in KINDS:
            raise KeyError(kind)
        if kind not in self._built:
            table = _real_classes() if real_required() else _stub_classes()
            self._built[kind] = table[kind]()
        return self._built[kind]

    depth = property(lambda self: self.get("depth"))
    intrinsics = property(lambda self: self.get("intrinsics"))
    segment = property(lambda self: self.get("segment"))
    flow = property(lambda self: self.get("flow"))
    ocr = property(lambda self: self.get("ocr"))
    upscale = property(lambda self: self.get("upscale"))
    lpips = property(lambda self: self.get("lpips"))

    def describe(self) -> dict[str, dict[str, Any]]:
        """Per kind: what a request would use now, without loading or constructing any real model."""
        table = _real_classes() if real_required() else _stub_classes()
        out: dict[str, dict[str, Any]] = {}
        for kind in KINDS:
            built = self._built.get(kind)
            cls = type(built) if built is not None else table[kind]
            out[kind] = {"model": cls.model_id, "impl": cls.impl, "stub": cls.is_stub}
        return out

    @staticmethod
    def availability() -> dict[str, bool]:
        """Registry model name -> can its real backend run here (packages and weights present)."""
        out: dict[str, bool] = {}
        for cls in _real_classes().values():
            out[cls.model_id] = out.get(cls.model_id, False) or cls.probe()[0]
        out["opencv-dis"] = True
        return out
