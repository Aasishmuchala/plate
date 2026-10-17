"""Commercial-safe licence registry for every model the pixels service may ever run.

Seeded from the licence notes in ``cv_sidecar/backends.py`` and the BUILD_PLAN decision on commercial-safe
weights. Allowed entries are the only models a backend can be constructed for; blocked entries stay in the
registry so CI can prove they are refused. ``assert_commercial_safe`` is called in every backend's
``__init__``, so a non-commercial model can never be loaded by accident.
"""

from __future__ import annotations

from dataclasses import dataclass


class LicenceError(Exception):
    """Raised when a model id is unknown or its licence is not commercial-safe."""


@dataclass(frozen=True)
class ModelLicence:
    id: str
    name: str
    licence: str
    commercial_ok: bool
    weights_source: str
    note: str = ""


def _ok(id: str, name: str, licence: str, weights_source: str, note: str = "") -> ModelLicence:
    return ModelLicence(id, name, licence, True, weights_source, note)


def _blocked(id: str, name: str, licence: str, weights_source: str, note: str = "") -> ModelLicence:
    return ModelLicence(id, name, licence, False, weights_source, note)


_ENTRIES: tuple[ModelLicence, ...] = (
    # --- allowed (commercial_ok=True) ---
    _ok(
        "depth_anything_v2_small",
        "Depth Anything V2 Small",
        "Apache-2.0",
        "DepthAnything/Depth-Anything-V2-Small (Hugging Face)",
        "only the Small checkpoint is Apache-2.0; Base and Large are non-commercial",
    ),
    _ok(
        "geocalib",
        "GeoCalib",
        "Apache-2.0 (code); CC-BY-4.0 (weights)",
        "cvg/GeoCalib (GitHub release weights)",
        "weights are CC-BY-4.0: keep the attribution (per the cv_sidecar notes)",
    ),
    _ok(
        "sam2.1",
        "Segment Anything 2.1",
        "Apache-2.0",
        "facebook/sam2.1-hiera-* (Hugging Face)",
    ),
    _ok(
        "raft",
        "RAFT optical flow",
        "BSD-3-Clause",
        "torchvision.models.optical_flow (RAFT weights)",
    ),
    _ok(
        "paddleocr",
        "PaddleOCR",
        "Apache-2.0",
        "PaddlePaddle/PaddleOCR (official model downloads)",
        "signage text-lock",
    ),
    _ok(
        "real_esrgan",
        "Real-ESRGAN",
        "BSD-3-Clause",
        "xinntao/Real-ESRGAN (GitHub release weights)",
        "AI layers only; never the plate",
    ),
    _ok(
        "lpips",
        "LPIPS",
        "BSD-2-Clause",
        "richzhang/PerceptualSimilarity (bundled linear layers)",
    ),
    _ok(
        "open_clip",
        "OpenCLIP",
        "MIT",
        "mlfoundations/open_clip (checkpoint chosen per use)",
        "code is MIT; confirm the licence of the chosen pretrained checkpoint",
    ),
    _ok(
        "qwen_image_edit_2509",
        "Qwen-Image-Edit-2509",
        "Apache-2.0",
        "Qwen/Qwen-Image-Edit-2509 (Hugging Face)",
        "confirm the licence file in the weights repo before any NDA claim",
    ),
    _ok(
        "wan2.2_vace",
        "Wan 2.2 VACE",
        "Apache-2.0",
        "Wan-AI/Wan2.2 VACE (Hugging Face)",
        "previs only, scheduled GPU windows",
    ),
    # --- blocked (commercial_ok=False) ---
    _blocked(
        "depth_anything_v2_base",
        "Depth Anything V2 Base",
        "CC-BY-NC-4.0",
        "DepthAnything/Depth-Anything-V2-Base (Hugging Face)",
        "non-commercial weights; use the Small checkpoint",
    ),
    _blocked(
        "depth_anything_v2_large",
        "Depth Anything V2 Large",
        "CC-BY-NC-4.0",
        "DepthAnything/Depth-Anything-V2-Large (Hugging Face)",
        "non-commercial weights; use the Small checkpoint",
    ),
    _blocked(
        "unidepth",
        "UniDepth",
        "CC-BY-NC-4.0",
        "lpiccinelli-eth/UniDepth (GitHub, Hugging Face)",
        "non-commercial weights",
    ),
    _blocked(
        "superpoint",
        "SuperPoint",
        "Magic Leap research licence (non-commercial)",
        "magicleap/SuperPointPretrainedNetwork (GitHub)",
        "research-only licence",
    ),
    _blocked(
        "flux1_dev",
        "FLUX.1 [dev]",
        "FLUX.1 [dev] Non-Commercial License",
        "black-forest-labs/FLUX.1-dev (Hugging Face)",
        "non-commercial weights; outputs feed paid client work",
    ),
    _blocked(
        "matanyone",
        "MatAnyone",
        "S-Lab License 1.0 (non-commercial)",
        "pq-yang/MatAnyone (GitHub, Hugging Face)",
        "off until cleared in writing",
    ),
    _blocked(
        "rvm",
        "Robust Video Matting",
        "GPL-3.0",
        "PeterL1n/RobustVideoMatting (GitHub)",
        "stays isolated inside STAFFAGE, never imported here",
    ),
)

REGISTRY: dict[str, ModelLicence] = {entry.id: entry for entry in _ENTRIES}


def assert_commercial_safe(model_id: str) -> ModelLicence:
    """Return the registry entry, or raise ``LicenceError`` for an unknown or non-commercial id."""
    entry = REGISTRY.get(model_id)
    if entry is None:
        raise LicenceError(
            f"{model_id}: unregistered is not commercial-safe — "
            "add it to the licence registry with a reviewed licence first"
        )
    if not entry.commercial_ok:
        raise LicenceError(f"{model_id}: {entry.licence} is not commercial-safe — {entry.note}")
    return entry


def commercial_models() -> list[str]:
    """Ids of every model a backend may be constructed for, in registry order."""
    return [entry.id for entry in REGISTRY.values() if entry.commercial_ok]
