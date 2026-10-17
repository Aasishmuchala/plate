"""Motion: the clay sequence a motion packet carries (PACKET.md section 10), rendered from the real
camera path through V-Ray.

  clay_override   V-Ray's global material override set to a 0.7 grey matte (PLATE_clay_matte_0.7),
                  with glass and light-emitting materials kept by excluding their nodes (decided by
                  material class: a VRayMtl with refraction is glass, a VRayLightMtl or a self-lit
                  VRayMtl is a light); clay_override(False) puts every setting back
  camera_frames   world_from_camera (plate-cam-v0, from objecttransform like every packet) plus focal
                  and focus distance per frame, evaluated at each frame
  check_frame0    frame 0 of the path must be the hero still's camera within 1e-6, else
                  FRAME0_MISMATCH: the motion packet claims frame0_matches_hero by construction
  sequence_size   the 720p sequence size with exactly the render's aspect (w * H == h * W)
  render_sequence per frame: the clay beauty from the VFB as a 16-bit PNG, and the Z and object
                  Cryptomatte passes as float EXRs from V-Ray's split render-channel output (every
                  other render element is switched off for the sequence and switched back after)

Every V-Ray property and vfbControl argument below is a candidate list from the V-Ray docs, unverified
on a Max box (TODO-verify, plugins/max/ONBOX.md). Settings changed here are restored whatever happens.
"""

from __future__ import annotations

import math
import shutil
import struct
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import digest
from ..exr_header import ExrHeaderError, read_header
from ..rt import runtime
from . import elements
from .render import (
    RAW_SAVE,
    SAVE_IMAGE,
    SET_SAVE_FORMAT,
    SPLIT_SAVE,
    VFB_ON,
    _Settings,
    _vfb,
    is_exr,
)
from .scene import class_name, get_camera, get_prop, has_prop, rt_attr, set_prop

# --------------------------------------------------------------------------- codes

FRAME0_MISMATCH = "FRAME0_MISMATCH"
CLAY_UNAVAILABLE = "CLAY_UNAVAILABLE"
CAMERA_FRAMES = "CAMERA_FRAMES"
ELEMENTS_MISSING = "ELEMENTS_MISSING"
SEQUENCE_UNSUPPORTED = "SEQUENCE_UNSUPPORTED"
SEQUENCE_FAILED = "SEQUENCE_FAILED"
CANCELLED = "CANCELLED"

FRAME0_TOL = 1e-6  # PACKET.md section 8 rule 8: the hero frame matrix within 1e-6
DEFAULT_SEQUENCE = (1280, 720)

# --------------------------------------------------------------------------- V-Ray names (TODO-verify)

CLAY_MATERIAL = "PLATE_clay_matte_0.7"
CLAY_ALBEDO = 0.7
# Global switches > Override mtl. The spec's candidates first, then the V-Ray 3+ documented names.
OVERRIDE_ON = ("override_mtl_on", "options_overrideMtl_on")  # TODO-verify
OVERRIDE_MTL = ("override_mtl", "overrideMtl", "options_overrideMtl_mtl")  # TODO-verify
# The override's exclude list holds NODES (not materials): glass and light nodes go in it.
OVERRIDE_EXCLUDE = (
    "override_mtl_exclude",
    "options_overrideMtl_excludeList",
    "options_overrideMtl_exclude",
)  # TODO-verify: the property name and that a Python list of nodes marshals to its array
MATERIAL_CLASS = "VRayMtl"  # TODO-verify: rt.VRayMtl() creates a material without a node
DIFFUSE = ("diffuse", "Diffuse")  # TODO-verify: Color 0..255
REFLECTION = ("reflection", "Reflection")  # TODO-verify: Color 0..255 (black: matte)
GLASS_CLASSES = ("VRayMtl",)
REFRACTION = ("refraction", "Refraction")  # TODO-verify: Color; any channel > 0 refracts
REFRACTION_MAP = ("texmap_refraction",)  # TODO-verify
REFRACTION_MAP_ON = ("texmap_refraction_on",)  # TODO-verify
SELF_ILLUMINATION = ("selfIllumination", "self_illumination")  # TODO-verify: Color
LIGHT_MATERIAL_CLASSES = ("VRayLightMtl",)
SUB_MATERIAL_ARRAYS = ("materialList", "coatMtl")  # Multimaterial, VRayBlendMtl coats (TODO-verify)
SUB_MATERIAL_SINGLE = ("baseMtl",)  # VRayBlendMtl base (TODO-verify)
SUB_MATERIAL_DEPTH = 4

# V-Ray split render channels: each element written as its own file by V-Ray's own writer (which
# keeps the Cryptomatte manifest), named <stem>.<channel>.exr or <stem>.<channel>.<frame>.exr.
SPLIT_FILE = ("output_splitfilename",)  # TODO-verify
SPLIT_RGB = ("output_splitRGB",)  # TODO-verify: off, the clay beauty comes from the VFB
SPLIT_ALPHA = ("output_splitAlpha",)  # TODO-verify
SPLIT_STEM = "seq"
PNG16_FORMAT = ("png", 16)  # TODO-verify: the #setsaveformat arguments for a 16-bit PNG

SEQUENCE_PATTERNS = {
    "clay_seq": "motion/clay.%04d.png",
    "id_seq": "motion/id.%04d.exr",
    "z_seq": "motion/z.%04d.exr",
}
SEQUENCE_HASHES = {
    "clay_seq": "motion/clay.blake3",
    "id_seq": "motion/id.blake3",
    "z_seq": "motion/z.blake3",
}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

Progress = Callable[[int, int, str], None]


class MotionError(RuntimeError):
    """Stops a motion render with a code (FRAME0_MISMATCH, CLAY_UNAVAILABLE, ...) and the reason."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- the clay override


@dataclass
class ClayReport:
    """What clay_override did. keeps_glass / keeps_lights are what really happened (they go into the
    manifest's motion.clay_override), not what was asked for."""

    on: bool
    material: str
    switch: str | None = None
    material_prop: str | None = None
    exclude_prop: str | None = None
    kept_glass: list[str] = field(default_factory=list)
    kept_lights: list[str] = field(default_factory=list)
    keeps_glass: bool = False
    keeps_lights: bool = False
    warnings: list[str] = field(default_factory=list)

    def manifest_block(self) -> dict[str, Any]:
        return {"material": self.material, "keeps_glass": self.keeps_glass, "keeps_lights": self.keeps_lights}

    def as_dict(self) -> dict[str, Any]:
        return {
            "on": self.on,
            "material": self.material,
            "switch": self.switch,
            "material_prop": self.material_prop,
            "exclude_prop": self.exclude_prop,
            "kept_glass": list(self.kept_glass),
            "kept_lights": list(self.kept_lights),
            "keeps_glass": self.keeps_glass,
            "keeps_lights": self.keeps_lights,
            "warnings": list(self.warnings),
        }


_active: _Settings | None = None  # the renderer settings clay_override(True) changed


def _positive(value: Any) -> bool:
    """A Color (r, g, b), a sequence or a number with any channel above zero."""
    if value is None:
        return False
    try:
        return any(float(getattr(value, a)) > 0 for a in ("r", "g", "b"))
    except (AttributeError, TypeError, ValueError):
        pass
    try:
        return any(float(v) > 0 for v in value)
    except (TypeError, ValueError):
        pass
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _sub_materials(mat: Any) -> list[Any]:
    out: list[Any] = []
    for name in SUB_MATERIAL_ARRAYS + SUB_MATERIAL_SINGLE:
        if not has_prop(mat, name):
            continue
        try:
            value = getattr(mat, name)
        except Exception:
            continue
        if value is None:
            continue
        if name in SUB_MATERIAL_SINGLE:
            out.append(value)
            continue
        try:
            out.extend(v for v in list(value) if v is not None)
        except Exception:  # not an array after all
            out.append(value)
    return out


def material_kind(mat: Any, depth: int = 0) -> str | None:
    """'glass', 'light' or None for a material, by class: a VRayMtl with refraction (a colour above
    black, or an enabled refraction map) is glass; a VRayLightMtl, or a VRayMtl with self-illumination,
    is a light. Multi/sub and blend materials are glass or light when any sub-material is (glass
    first: keeping a glazed multi-material out of the clay matters more than its frame)."""
    if mat is None or depth > SUB_MATERIAL_DEPTH:
        return None
    cls = class_name(mat)
    kind: str | None = None
    if cls in LIGHT_MATERIAL_CLASSES:
        kind = "light"
    elif cls in GLASS_CLASSES:
        refracts = _positive(get_prop(mat, REFRACTION))
        if not refracts and get_prop(mat, REFRACTION_MAP) is not None:
            refracts = get_prop(mat, REFRACTION_MAP_ON) is not False
        if refracts:
            return "glass"
        if _positive(get_prop(mat, SELF_ILLUMINATION)):
            kind = "light"
    for sub in _sub_materials(mat):
        found = material_kind(sub, depth + 1)
        if found == "glass":
            return "glass"
        kind = kind or found
    return kind


def _geometry_nodes() -> list[Any]:
    rt = runtime()
    geometry_class = getattr(rt, "GeometryClass", None)
    out = []
    for node in list(getattr(rt, "objects", [])):
        if geometry_class is not None:
            try:
                if rt.superClassOf(node) != geometry_class:
                    continue
            except Exception:
                pass
        out.append(node)
    return out


def clay_exclusions() -> tuple[list[Any], list[Any]]:
    """(glass nodes, light-material nodes) of the scene, by material class (material_kind)."""
    glass: list[Any] = []
    lights: list[Any] = []
    for node in _geometry_nodes():
        try:
            mat = getattr(node, "material", None)
        except Exception:
            continue
        kind = material_kind(mat)
        if kind == "glass":
            glass.append(node)
        elif kind == "light":
            lights.append(node)
    return glass, lights


def _clay_material(name: str) -> Any:
    factory = rt_attr(MATERIAL_CLASS)
    if not callable(factory):
        raise MotionError(CLAY_UNAVAILABLE, f"rt.{MATERIAL_CLASS} is missing: V-Ray is not installed?")
    try:
        mat = factory()
    except Exception as exc:
        raise MotionError(CLAY_UNAVAILABLE, f"rt.{MATERIAL_CLASS}() failed: {exc}") from exc
    grey = round(CLAY_ALBEDO * 255.0, 3)
    color = rt_attr("Color")
    value = color(grey, grey, grey) if callable(color) else (grey, grey, grey)
    black = color(0, 0, 0) if callable(color) else (0, 0, 0)
    set_prop(mat, DIFFUSE, value)
    set_prop(mat, REFLECTION, black)
    try:
        mat.name = name
    except Exception:
        pass
    return mat


def clay_override(
    on: bool,
    material_name: str = CLAY_MATERIAL,
    keep_glass: bool = True,
    keep_lights: bool = True,
) -> ClayReport:
    """Turn V-Ray's material override on (a 0.7 grey matte named material_name; glass and light nodes
    excluded when keep_glass / keep_lights) or off (every renderer setting the last call changed is put
    back). Raises MotionError(CLAY_UNAVAILABLE) when there is no override switch or no VRayMtl class:
    a sequence that is not clay must not be labelled clay."""
    global _active
    if not on:
        if _active is not None:
            _active.restore()
            _active = None
        return ClayReport(on=False, material=material_name)
    if _active is not None:  # a second on without an off: put the first one back before redoing it
        _active.restore()
        _active = None
    renderer = getattr(rt_attr("renderers"), "current", None)
    report = ClayReport(on=True, material=material_name)
    switch = next((n for n in OVERRIDE_ON if has_prop(renderer, n)), None)
    slot = next((n for n in OVERRIDE_MTL if has_prop(renderer, n)), None)
    if switch is None or slot is None:
        missing = "/".join(OVERRIDE_ON) if switch is None else "/".join(OVERRIDE_MTL)
        raise MotionError(CLAY_UNAVAILABLE, f"the renderer has no material override ({missing})")
    mat = _clay_material(material_name)
    settings = _Settings(renderer)
    try:
        glass, light_nodes = clay_exclusions()
        excluded = (glass if keep_glass else []) + (light_nodes if keep_lights else [])
        # always set (an empty list too): the artist's own exclude list must not leak into the clay.
        # TODO-verify that V-Ray's list is in exclude mode (it may also offer include mode).
        exclude_prop = settings.set(OVERRIDE_EXCLUDE, list(excluded))
        if exclude_prop is None and excluded:
            report.warnings.append(
                f"{'/'.join(OVERRIDE_EXCLUDE)}: no exclude list on this renderer; "
                "glass and light materials are overridden too"
            )
        report.exclude_prop = exclude_prop
        kept = exclude_prop is not None
        report.kept_glass = [str(n.name) for n in glass] if keep_glass and kept else []
        report.kept_lights = [str(n.name) for n in light_nodes] if keep_lights and kept else []
        report.keeps_glass = keep_glass and (not glass or kept)
        report.keeps_lights = keep_lights and (not light_nodes or kept)
        report.material_prop = settings.set((slot,), mat)
        report.switch = settings.set((switch,), True)
        if report.material_prop is None or report.switch is None:
            raise MotionError(CLAY_UNAVAILABLE, f"could not set {slot} / {switch} on the renderer")
    except Exception:
        settings.restore()
        raise
    _active = settings
    return report


@contextmanager
def clay(
    material_name: str = CLAY_MATERIAL, keep_glass: bool = True, keep_lights: bool = True
) -> Iterator[ClayReport]:
    """clay_override(True, ...) for the block, clay_override(False) after, whatever happens."""
    report = clay_override(True, material_name, keep_glass, keep_lights)
    try:
        yield report
    finally:
        clay_override(False, material_name)


# --------------------------------------------------------------------------- the camera path


def _camera_name(cam: Any) -> str:
    node = get_camera(cam)
    if node is None:
        raise MotionError(CAMERA_FRAMES, f"camera {cam!r} not found in the scene")
    return str(getattr(node, "name", cam))


def camera_frames(cam: Any, f0: int, f1: int) -> list[dict[str, Any]]:
    """[{frame, world_from_camera, focal_length_mm?, focus_distance?}] for f0..f1, each read at its
    frame with the same reader as the hero still (digest.camera_digest: objecttransform, PACKET.md
    3.2), so frame 0 equals the hero's camera by construction when the camera has not moved. A frame
    whose matrix is unreadable, scaled or mirrored raises MotionError(CAMERA_FRAMES)."""
    f0, f1 = int(f0), int(f1)
    if f1 <= f0:
        raise MotionError(CAMERA_FRAMES, f"frame range {f0}..{f1}: a motion packet needs at least two frames")
    name = _camera_name(cam)
    out: list[dict[str, Any]] = []
    for frame in range(f0, f1 + 1):
        try:
            d = digest.camera_digest(name, frame=frame)
        except digest.CameraNotFound as exc:
            raise MotionError(CAMERA_FRAMES, f"camera {name!r} not found") from exc
        matrix = d.get("world_from_camera")
        if matrix is None:
            raise MotionError(CAMERA_FRAMES, f"frame {frame}: the camera transform is unreadable")
        problem = digest.rotation_error(matrix)
        if problem:
            raise MotionError(CAMERA_FRAMES, f"frame {frame}: {problem}")
        entry: dict[str, Any] = {"frame": frame, "world_from_camera": [list(r) for r in matrix]}
        focal = d.get("focal_mm")
        if focal and float(focal) > 0:
            entry["focal_length_mm"] = float(focal)
        focus = d.get("focus_distance")
        if focus and float(focus) > 0:
            entry["focus_distance"] = float(focus)
        out.append(entry)
    return out


def matrix_deviation(a: Any, b: Any) -> float:
    """Largest absolute element difference of two 4x4 matrices."""
    return max(abs(float(a[i][j]) - float(b[i][j])) for i in range(4) for j in range(4))


def check_frame0(frames: list[dict[str, Any]], hero_matrix: Any, tol: float = FRAME0_TOL) -> float:
    """The deviation of the path's first frame from the hero still's world_from_camera; above tol it
    raises MotionError(FRAME0_MISMATCH): the motion would not start on the approved still."""
    if not frames:
        raise MotionError(FRAME0_MISMATCH, "no camera frames")
    deviation = matrix_deviation(frames[0]["world_from_camera"], hero_matrix)
    if deviation > tol:
        raise MotionError(
            FRAME0_MISMATCH,
            f"frame {frames[0]['frame']} differs from the hero still's camera by {deviation:.3g} "
            f"(tolerance {tol:g}): start the move on the hero frame, or Send the still again",
        )
    return deviation


def sequence_size(width: int, height: int, target: tuple[int, int] = DEFAULT_SEQUENCE) -> tuple[int, int]:
    """The sequence size for a width x height render: target itself when it has exactly the render's
    aspect (1280x720 for 16:9), else the exact-aspect size (a whole multiple of the reduced ratio)
    whose width is closest to target's. A ratio too coarse to fit is the render's own size; nothing
    is ever larger than the render."""
    width, height = int(width), int(height)
    tw, th = int(target[0]), int(target[1])
    if width < 1 or height < 1 or tw < 1 or th < 1:
        raise ValueError("sizes must be positive")
    if tw * height == th * width and tw <= width:
        return tw, th
    g = math.gcd(width, height)
    bw, bh = width // g, height // g
    k = min(g, max(1, round(tw / bw)))
    k = max(k, min(g, max(math.ceil(16 / bh), math.ceil(16 / bw))))  # schema minimum 16
    return bw * k, bh * k


# --------------------------------------------------------------------------- file checks (stdlib)


def png_info(path: Path | str) -> tuple[int, int, int] | None:
    """(width, height, bit depth) from a PNG's IHDR, None when the file is not a PNG."""
    try:
        with Path(path).open("rb") as fh:
            head = fh.read(33)
    except OSError:
        return None
    if len(head) < 33 or head[:8] != PNG_SIGNATURE or head[12:16] != b"IHDR":
        return None
    width, height, depth = struct.unpack(">IIB", head[16:25])
    return width, height, depth


def _check_exr(path: Path, width: int, height: int, what: str, crypto_layer: str | None = None) -> None:
    if not is_exr(path):
        raise MotionError(SEQUENCE_FAILED, f"{path.name}: not an OpenEXR file ({what})")
    try:
        header = read_header(path)
    except ExrHeaderError as exc:
        raise MotionError(SEQUENCE_FAILED, f"{path.name}: {exc}") from exc
    box = header.parts[0].data_window
    if box != (0, 0, width - 1, height - 1):
        raise MotionError(
            SEQUENCE_FAILED, f"{path.name}: data window {box}, the sequence is {width}x{height}"
        )
    if not header.channels:
        raise MotionError(SEQUENCE_FAILED, f"{path.name}: no channels ({what})")
    loose = sorted({ch.dtype for ch in header.channels if ch.dtype != "float"})
    if loose:
        raise MotionError(
            SEQUENCE_FAILED,
            f"{path.name}: {what} channels are {', '.join(loose)}; data sequences are 32-bit float",
        )
    if crypto_layer is not None:
        ranks = [ch.name for ch in header.channels if ch.name.startswith(crypto_layer + "00.")]
        if not ranks:
            raise MotionError(
                SEQUENCE_FAILED,
                f"{path.name}: no {crypto_layer}00.* channels (found {', '.join(c.name for c in header.channels)})",
            )


def _split_file(staging: Path, layer: str) -> Path | None:
    """The split render-channel file V-Ray wrote for layer: <stem>.<layer>.exr or
    <stem>.<layer>.<frame>.exr (TODO-verify which), the newest one when several match."""
    hits = []
    for p in staging.iterdir():
        if not p.is_file() or p.suffix.lower() != ".exr":
            continue
        parts = p.name[: -len(p.suffix)].split(".")
        if layer in parts[1:]:
            hits.append(p)
    if not hits:
        return None
    return max(hits, key=lambda p: p.stat().st_mtime)


def _clear(folder: Path) -> None:
    for p in folder.iterdir():
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass
        else:
            shutil.rmtree(p, ignore_errors=True)


# --------------------------------------------------------------------------- the sequence


@dataclass
class SequenceResult:
    patterns: dict[str, str]
    first: int
    last: int
    width: int
    height: int
    clay_bit_depth: int | None = None
    frames: int = 0
    time_s: float = 0.0
    layers: dict[str, str] = field(default_factory=dict)  # zdepth / cryptomatte_object -> EXR layer
    warnings: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)

    @property
    def clay_dtype(self) -> str:
        return "uint16" if self.clay_bit_depth == 16 else "uint8"

    def as_dict(self) -> dict[str, Any]:
        return {
            "patterns": dict(self.patterns),
            "first": self.first,
            "last": self.last,
            "width": self.width,
            "height": self.height,
            "clay_bit_depth": self.clay_bit_depth,
            "frames": self.frames,
            "time_s": self.time_s,
            "layers": dict(self.layers),
            "warnings": list(self.warnings),
            "settings": dict(self.settings),
        }


@contextmanager
def _only_elements(keep: set[str]) -> Iterator[list[str]]:
    """Every enabled render element whose layer is not in keep switched off for the block (their
    names are yielded), and switched back on after."""
    try:
        mgr = elements.current_manager()
    except Exception:
        yield []
        return
    switched: list[Any] = []
    names: list[str] = []
    for element in elements.list_elements(mgr):
        layer = elements.layer_of(element)
        if layer in keep or get_prop(element, elements.ENABLED) is False:
            continue
        if set_prop(element, elements.ENABLED, False) is not None:
            switched.append(element)
            names.append(layer)
    try:
        yield names
    finally:
        for element in switched:
            set_prop(element, elements.ENABLED, True)


def _notify(progress: Progress | None, done: int, total: int, message: str) -> None:
    if progress is None:
        return
    try:
        progress(done, total, message)
    except Exception:  # a broken progress display never stops a render
        pass


def render_sequence(
    cam: Any,
    f0: int,
    f1: int,
    w: int = DEFAULT_SEQUENCE[0],
    h: int = DEFAULT_SEQUENCE[1],
    out_pattern: dict[str, str] | None = None,
    packet_dir: Path | str | None = None,
    staging_dir: Path | str | None = None,
    progress: Progress | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> SequenceResult:
    """Render camera cam over f0..f1 at w x h into packet_dir: per frame the clay beauty (the VFB
    saved as a 16-bit PNG) and the Z and object Cryptomatte passes (V-Ray split render channels,
    float EXR), at the packet-relative out_pattern paths (default SEQUENCE_PATTERNS). Only the Z and
    object Cryptomatte elements render; the clay override is the caller's (clay()). Raises
    MotionError: SEQUENCE_UNSUPPORTED before anything renders when V-Ray cannot split channels,
    ELEMENTS_MISSING, SEQUENCE_FAILED for a render or a file that is wrong, CANCELLED."""
    rt = runtime()
    if packet_dir is None:
        raise ValueError("render_sequence needs packet_dir (the motion packet folder)")
    f0, f1, w, h = int(f0), int(f1), int(w), int(h)
    if f1 <= f0:
        raise MotionError(SEQUENCE_FAILED, f"frame range {f0}..{f1}: at least two frames")
    node = get_camera(cam)
    if node is None:
        raise MotionError(SEQUENCE_FAILED, f"camera {cam!r} not found")
    patterns = dict(SEQUENCE_PATTERNS)
    patterns.update(out_pattern or {})
    root = Path(packet_dir)
    for rel in patterns.values():
        if ":" in rel or "\\" in rel:
            raise ValueError(f"{rel!r}: sequence patterns are packet-relative POSIX without ':'")

    defaults = elements.ensure_default_set()
    by_element = {e["element"]: e["layer"] for e in defaults.entries}
    needed = {"zdepth": by_element.get("zdepth"), "cryptomatte_object": by_element.get("cryptomatte_object")}
    if not all(needed.values()):
        absent = [k for k, v in needed.items() if not v]
        raise MotionError(ELEMENTS_MISSING, f"render elements missing for the sequence: {', '.join(absent)}")
    z_layer, id_layer = str(needed["zdepth"]), str(needed["cryptomatte_object"])
    result = SequenceResult(
        patterns, f0, f1, w, h, layers={"zdepth": z_layer, "cryptomatte_object": id_layer}
    )
    result.warnings.extend(defaults.unverified)

    staging_root = Path(staging_dir) if staging_dir else root / ".plate-staging"
    staging = staging_root / "split"
    staging.mkdir(parents=True, exist_ok=True)
    renderer = getattr(rt_attr("renderers"), "current", None)
    vray = _Settings(renderer)
    size = _Settings(rt, check=False)
    total = f1 - f0 + 1
    started = time.perf_counter()
    depths: set[int] = set()
    try:
        try:
            rt.renderSceneDialog.close()  # an open render dialog blocks scripted setting changes
        except Exception:
            pass
        vray.set(RAW_SAVE, False)  # the artist's raw output path must not be overwritten per frame
        if (
            vray.set(SPLIT_SAVE, True) is None
            or vray.set(SPLIT_FILE, str(staging / f"{SPLIT_STEM}.exr")) is None
        ):
            raise MotionError(
                SEQUENCE_UNSUPPORTED,
                f"no split render-channel output ({'/'.join(SPLIT_SAVE)}, {'/'.join(SPLIT_FILE)}) on this renderer",
            )
        vray.set(SPLIT_RGB, False)
        vray.set(SPLIT_ALPHA, False)
        if vray.set(VFB_ON, True) is None:
            result.warnings.append(
                f"{'/'.join(VFB_ON)}: unreadable; the clay PNG needs the V-Ray frame buffer"
            )
        size.set(("renderWidth",), w)
        size.set(("renderHeight",), h)
        with _only_elements({z_layer, id_layer}) as switched_off:
            if switched_off:
                result.settings["elements_off"] = list(switched_off)
            for index, frame in enumerate(range(f0, f1 + 1)):
                if should_cancel is not None and should_cancel():
                    raise MotionError(CANCELLED, f"cancelled before frame {frame}")
                _notify(progress, index, total, f"frame {frame}")
                _clear(staging)
                clay_path = root.joinpath(*(patterns["clay_seq"] % frame).split("/"))
                z_path = root.joinpath(*(patterns["z_seq"] % frame).split("/"))
                id_path = root.joinpath(*(patterns["id_seq"] % frame).split("/"))
                for target in (clay_path, z_path, id_path):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        target.unlink()
                kwargs = {
                    "camera": node,
                    "frame": frame,
                    "outputwidth": w,
                    "outputheight": h,
                    "vfb": True,
                    "quiet": True,
                }
                try:
                    bitmap = rt.render(**kwargs)
                except Exception as exc:
                    raise MotionError(SEQUENCE_FAILED, f"frame {frame}: render() raised {exc}") from exc
                if bitmap is None:
                    raise MotionError(SEQUENCE_FAILED, f"frame {frame}: render() returned undefined")
                try:
                    rt.close(bitmap)
                except Exception:
                    pass
                if index == 0 and _vfb(SET_SAVE_FORMAT, *PNG16_FORMAT) is None:
                    result.warnings.append(
                        "vfbControl #setsaveformat: not available; the PNG bit depth is the VFB default"
                    )
                if _vfb(SAVE_IMAGE, str(clay_path)) is None or not clay_path.is_file():
                    raise MotionError(SEQUENCE_FAILED, f"frame {frame}: the VFB saved no clay PNG")
                info = png_info(clay_path)
                if info is None or (info[0], info[1]) != (w, h):
                    raise MotionError(
                        SEQUENCE_FAILED, f"frame {frame}: clay image {info}, expected a {w}x{h} PNG"
                    )
                depths.add(info[2])
                for layer, target, what, crypto in (
                    (z_layer, z_path, "Z", None),
                    (id_layer, id_path, "Cryptomatte", id_layer),
                ):
                    found = _split_file(staging, layer)
                    if found is None:
                        names = ", ".join(sorted(p.name for p in staging.iterdir())) or "nothing"
                        raise MotionError(
                            SEQUENCE_FAILED,
                            f"frame {frame}: V-Ray wrote no split {layer} file (found {names})",
                        )
                    shutil.move(str(found), str(target))
                    _check_exr(target, w, h, what, crypto)
                result.frames += 1
        result.settings.update(vray.applied)
    finally:
        vray.restore()
        size.restore()
        shutil.rmtree(staging_root, ignore_errors=True)
    if len(depths) > 1:
        raise MotionError(SEQUENCE_FAILED, f"clay PNGs mix bit depths {sorted(depths)}")
    result.clay_bit_depth = depths.pop() if depths else None
    if result.clay_bit_depth != 16:
        result.warnings.append(
            f"clay PNGs are {result.clay_bit_depth}-bit, not 16-bit (check #setsaveformat)"
        )
    result.time_s = round(time.perf_counter() - started, 3)
    _notify(progress, total, total, f"{total} frames rendered")
    return result
