"""OCIO colour core: re-derive the display image from the linear EXR and compare it with the stored master.

``Pipeline.from_manifest(manifest)`` reads ``manifest["colour"]``:

- ``max_colour_mode == "ocio"``: build ``working_space -> display/view`` with OpenColorIO. The config is, in
  order: the file named by ``PLATE_OCIO_CONFIG``; the built-in config whose name equals the manifest's
  ``ocio.config_name`` (so the view names the sender used exist); the built-in ``studio-config-latest``.
- ``legacy_gamma``: the sRGB OETF (the exact inverse is the sRGB EOTF).
- ``not_applicable`` (tier C): no linear source exists; ``rederive_display`` raises :class:`ColourError`.

Display values are "8-bit floats": float32 on the 0..255 scale, unrounded, which is what
``plate_gates.color.srgb_to_lab`` takes. Linear values are the scene-linear floats in the EXR. ``invertible``
says whether ``display_to_linear`` may be used; nothing may pretend to undo a view transform that is not
invertible (a tone-mapped view loses information above the shoulder).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import PyOpenColorIO as OCIO

__all__ = ["ColourError", "Pipeline", "delta_e00_p99", "srgb_eotf", "srgb_oetf"]

OCIO_CONFIG_ENV = "PLATE_OCIO_CONFIG"
LATEST_BUILTIN = "studio-config-latest"

# manifest working_space -> OCIO colour space names / aliases to try, in order
WORKING_SPACE_NAMES: dict[str, tuple[str, ...]] = {
    "lin_rec709": ("Linear Rec.709 (sRGB)", "lin_rec709", "Utility - Linear - sRGB"),
    "acescg": ("ACEScg", "ACES - ACEScg", "lin_ap1"),
}


class ColourError(RuntimeError):
    """The colour pipeline cannot do what was asked (no linear source, not invertible, bad config)."""


# --------------------------------------------------------------------------- analytic sRGB


def srgb_oetf(lin: np.ndarray) -> np.ndarray:
    """Linear [0, 1] -> sRGB encoded [0, 1] (input clipped to [0, 1])."""
    v = np.clip(np.asarray(lin, dtype=np.float64), 0.0, 1.0)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * np.power(v, 1.0 / 2.4) - 0.055)


def srgb_eotf(enc: np.ndarray) -> np.ndarray:
    """sRGB encoded [0, 1] -> linear [0, 1] (input clipped to [0, 1])."""
    v = np.clip(np.asarray(enc, dtype=np.float64), 0.0, 1.0)
    return np.where(v <= 0.04045, v / 12.92, np.power((v + 0.055) / 1.055, 2.4))


# --------------------------------------------------------------------------- config loading


def _builtin_names() -> set[str]:
    return {row[0] for row in OCIO.BuiltinConfigRegistry().getBuiltinConfigs()}


def _load_config(config_name: str | None) -> tuple[Any, str]:
    """(config, source) where source says which of the three routes was taken."""
    env = os.environ.get(OCIO_CONFIG_ENV)
    if env:
        path = Path(env)
        if not path.is_file():
            raise ColourError(f"{OCIO_CONFIG_ENV}={env!r} is not a file")
        return OCIO.Config.CreateFromFile(str(path)), f"file:{path.name}"
    if config_name and config_name in _builtin_names():
        return OCIO.Config.CreateFromBuiltinConfig(config_name), f"builtin:{config_name}"
    return OCIO.Config.CreateFromBuiltinConfig(LATEST_BUILTIN), f"builtin:{LATEST_BUILTIN}"


def _working_space(config: Any, working_space: str) -> str:
    for name in WORKING_SPACE_NAMES.get(working_space, ()):
        cs = config.getColorSpace(name)
        if cs is not None:
            return cs.getName()
    role = config.getRoleColorSpace("scene_linear")
    if role:
        return role
    raise ColourError(f"working space {working_space!r} has no colour space in this OCIO config")


# --------------------------------------------------------------------------- the pipeline


def _apply(cpu: Any, rgb: np.ndarray) -> np.ndarray:
    a = np.array(rgb, dtype=np.float32, copy=True, order="C")
    if a.ndim < 1 or a.shape[-1] != 3:
        raise ValueError(f"expected (..., 3) RGB, got {a.shape}")
    cpu.applyRGB(a)
    return a


@dataclass
class Pipeline:
    mode: str  # 'ocio' | 'legacy_gamma' | 'not_applicable'
    working_space: str
    invertible: bool = False
    config_name: str | None = None
    config_source: str | None = None
    display: str | None = None
    view: str | None = None
    # True when the manifest's display or view is not in the config and the config default was used
    substituted: bool = False
    notes: list[str] = field(default_factory=list)
    _forward: Any = None
    _inverse: Any = None

    # ----- construction
    @classmethod
    def from_manifest(cls, manifest: Mapping[str, Any]) -> Pipeline:
        colour = manifest["colour"]
        mode = colour["max_colour_mode"]
        working = colour.get("working_space", "display_only")
        if mode == "not_applicable":
            return cls(mode=mode, working_space=working, invertible=False)
        if mode == "legacy_gamma":
            return cls(mode=mode, working_space=working, invertible=True, config_source="analytic:srgb")
        if mode != "ocio":
            raise ColourError(f"unknown max_colour_mode {mode!r}")

        spec = colour.get("ocio") or {}
        config, source = _load_config(spec.get("config_name"))
        config_name = source.split(":", 1)[1] if source.startswith("builtin:") else spec.get("config_name")
        src = _working_space(config, working)
        notes: list[str] = []
        display = spec.get("display") or config.getDefaultDisplay()
        if display not in list(config.getDisplays()):
            notes.append(f"display {display!r} not in config; using {config.getDefaultDisplay()!r}")
            display = config.getDefaultDisplay()
        view = spec.get("view") or config.getDefaultView(display)
        if view not in list(config.getViews(display)):
            notes.append(
                f"view {view!r} not in config for {display!r}; using {config.getDefaultView(display)!r}"
            )
            view = config.getDefaultView(display)
        substituted = bool(notes)

        try:
            processor = config.getProcessor(src, display, view, OCIO.TRANSFORM_DIR_FORWARD)
            forward = processor.getDefaultCPUProcessor()
        except OCIO.Exception as exc:
            raise ColourError(f"cannot build {src!r} -> {display!r}/{view!r}: {exc}") from exc

        inverse = None
        invertible = False
        if not processor.isNoOp():
            try:
                group = processor.createGroupTransform()
                group.setDirection(OCIO.TRANSFORM_DIR_INVERSE)
                inverse = config.getProcessor(group).getDefaultCPUProcessor()
                invertible = True
            except OCIO.Exception as exc:
                notes.append(f"view transform is not invertible: {exc}")
        else:
            notes.append("view transform is a no-op")
        return cls(
            mode=mode,
            working_space=working,
            invertible=invertible,
            config_name=config_name,
            config_source=source,
            display=display,
            view=view,
            substituted=substituted,
            notes=notes,
            _forward=forward,
            _inverse=inverse,
        )

    # ----- conversions (display values are float32 on the 0..255 scale, unrounded)
    def rederive_display(self, exr_rgb_linear: np.ndarray) -> np.ndarray:
        """Scene-linear RGB (..., 3) -> display RGB (..., 3), float32 on 0..255 and clipped to that range."""
        if self.mode == "not_applicable":
            raise ColourError("tier C has no linear source to re-derive a display image from")
        if self.mode == "legacy_gamma":
            out = srgb_oetf(exr_rgb_linear) * 255.0
        else:
            out = np.clip(_apply(self._forward, exr_rgb_linear), 0.0, 1.0) * 255.0
        return np.asarray(out, dtype=np.float32)

    def display_to_linear(self, rgb8bitfloat: np.ndarray) -> np.ndarray:
        """Display RGB on the 0..255 scale -> scene-linear float32. Raises ColourError unless ``invertible``."""
        if not self.invertible:
            raise ColourError("this view transform is not invertible; display_to_linear is unavailable")
        scaled = np.asarray(rgb8bitfloat, dtype=np.float64) / 255.0
        if self.mode == "legacy_gamma":
            return srgb_eotf(scaled).astype(np.float32)
        return _apply(self._inverse, scaled.astype(np.float32))

    def delta_e00_p99(self, a: np.ndarray, b: np.ndarray, mask: np.ndarray | None = None) -> float:
        return delta_e00_p99(a, b, mask)

    def describe(self) -> dict[str, Any]:
        """What the gate report records about the colour path actually used."""
        return {
            "mode": self.mode,
            "working_space": self.working_space,
            "config_name": self.config_name,
            "config_source": self.config_source,
            "display": self.display,
            "view": self.view,
            "invertible": self.invertible,
            "substituted": self.substituted,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- comparison


def delta_e00_p99(a: np.ndarray, b: np.ndarray, mask: np.ndarray | None = None, q: float = 99.0) -> float:
    """99th-percentile CIEDE2000 between two display images (RGB on the 0..255 scale), over ``mask`` if given.

    An empty selection returns 0.0 (nothing differs because nothing is compared).
    """
    try:
        from plate_gates.color import ciede2000, srgb_to_lab
    except ImportError as exc:  # pragma: no cover - gates/ is on pythonpath in the repo and on the box
        raise ColourError("plate_gates is not importable; put gates/ on PYTHONPATH") from exc
    a = np.asarray(a)
    b = np.asarray(b)
    if a.shape != b.shape:
        raise ValueError(f"shape mismatch {a.shape} vs {b.shape}")
    de = ciede2000(srgb_to_lab(a), srgb_to_lab(b))
    if mask is not None:
        m = np.asarray(mask, dtype=bool)
        if m.shape != de.shape:
            raise ValueError(f"mask shape {m.shape} does not match image {de.shape}")
        de = de[m]
    if de.size == 0:
        return 0.0
    return float(np.percentile(de, q))
