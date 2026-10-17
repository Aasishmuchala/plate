"""Reads the camera, the scene and the node list out of 3ds Max through pymxs.

Every runtime read is wrapped: a missing attribute (pymxs raises for unknown properties) yields
None and a line in digest["warnings"], so one odd camera cannot sink the Send. No pymxs call is
made at import time (rt.runtime() is lazy). Seed: MaxDirector's maxbridge/digest.py (guarded reads)
and MaxGaffer's maxbridge/render.py (render settings).

Camera convention plate-cam-v0 (spec/PACKET.md 3.1-3.2): a Max transform is a Matrix3 in
row-vector form, row1..row3 = the local X, Y, Z axes in world space, row4 = the position. Max
cameras already look down local -Z with +Y up, so the transform IS camera-to-world and the
plate-cam-v0 column-vector matrix is just its transpose (see world_from_camera_from_max_rows). The
pose is read from the node's objecttransform (the object, not the pivot: a camera whose pivot was
moved with Affect Pivot Only renders from its object).

Property names for lens shift, tilt and distortion are the best known V-Ray Physical Camera names
and are NOT verified on a Max box yet (P0 spike): the candidates are listed below, a miss is a
digest warning, and packet.build_camera_block refuses any camera that reads as shifted, tilted or
distorted.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .rt import runtime

SCENE_HASH_METHOD = "file-meta-v0"
# manifest scene_hash.digest_version for the interim file-meta digest ("1" is reserved for the
# real node/transform/material digest the P0 spike defines).
DIGEST_VERSION = "0"

# Metres per Max SystemType unit (the schema also allows "relative", which only tier C uses).
_METERS_PER_TYPE = {
    "millimeters": 0.001,
    "centimeters": 0.01,
    "meters": 1.0,
    "kilometers": 1000.0,
    "inches": 0.0254,
    "feet": 0.3048,
    "miles": 1609.344,
}

ORTHO_TOL = 1e-4

# V-Ray Physical Camera property candidates, first readable wins (unverified on-box).
SHIFT_H_NAMES = ("horizontal_shift", "horizontal_offset")
SHIFT_V_NAMES = ("vertical_shift", "vertical_offset")
TILT_NAMES = (("vertical_tilt", "vertical"), ("horizontal_tilt", "horizontal"))
TILT_AUTO_NAMES = ("auto_vertical_tilt", "guess_vertical_tilt")
DISTORTION_NAMES = ("distortion",)
DISTORTION_TYPE_NAMES = ("distortion_type",)


class CameraNotFound(LookupError):
    """No node with that name in the open scene."""


# --------------------------------------------------------------------------- guarded reads


class _Reader:
    """getattr with a warning instead of an exception."""

    def __init__(self, warnings: list[str]):
        self.warnings = warnings

    def get(self, obj: Any, name: str, label: str | None = None) -> Any:
        label = label or name
        try:
            value = getattr(obj, name)
        except Exception as exc:  # pymxs raises RuntimeError/AttributeError for unknown properties
            self.warnings.append(f"{label}: unreadable ({type(exc).__name__})")
            return None
        if value is None:
            self.warnings.append(f"{label}: undefined")
        return value

    def num(self, obj: Any, name: str, label: str | None = None) -> float | None:
        value = self.get(obj, name, label)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            self.warnings.append(f"{label or name}: not a number ({value!r})")
            return None

    def flag(self, obj: Any, name: str, label: str | None = None) -> bool | None:
        value = self.get(obj, name, label)
        return None if value is None else bool(value)


def _frame_of(value: Any) -> int | None:
    """A pymxs time value (or a plain number) as a whole frame number."""
    if value is None:
        return None
    raw = getattr(value, "frame", value)
    try:
        return int(round(float(raw)))
    except (TypeError, ValueError):
        return None


def _xyz(point: Any) -> tuple[float, float, float]:
    """A Point3 (.x .y .z) or any 3-sequence as three floats."""
    try:
        return float(point.x), float(point.y), float(point.z)
    except AttributeError:
        a = tuple(point)
        return float(a[0]), float(a[1]), float(a[2])


def _matrix_rows(matrix: Any) -> list[tuple[float, float, float]]:
    return [_xyz(getattr(matrix, f"row{i}")) for i in (1, 2, 3, 4)]


def _first_number(rd: _Reader, node: Any, names: tuple[str, ...], default: float = 0.0) -> float:
    """First readable numeric property among names; a miss is a warning and the default."""
    for name in names:
        try:
            value = getattr(node, name)
        except Exception:
            continue
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    rd.warnings.append(
        f"{'/'.join(names)}: no readable property (name unverified on this build); assumed {default}"
    )
    return default


def _first_flag(rd: _Reader, node: Any, names: tuple[str, ...]) -> bool:
    for name in names:
        try:
            value = getattr(node, name)
        except Exception:
            continue
        if value is not None:
            return bool(value)
    rd.warnings.append(
        f"{'/'.join(names)}: no readable property (name unverified on this build); assumed off"
    )
    return False


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- camera math


def world_from_camera_from_max_rows(rows: Any) -> list[list[float]]:
    """PACKET.md 3.2: Max Matrix3 rows (local X, Y, Z axes in world, then position) to the
    plate-cam-v0 world_from_camera, a row-major 4x4 for column vectors: M[i][j] = rows[j][i]."""
    if len(rows) != 4 or any(len(r) != 3 for r in rows):
        raise ValueError("expected 4 rows of 3 (Max Matrix3 row1..row4)")
    m = [[float(rows[j][i]) for j in range(4)] for i in range(3)]
    m.append([0, 0, 0, 1])
    return m


def rotation_error(m: Any) -> str | None:
    """None when the upper 3x3 is a proper rotation (orthonormal, det +1) within 1e-4."""
    r = [[float(m[i][j]) for j in range(3)] for i in range(3)]
    worst = 0.0
    for i in range(3):
        for j in range(3):
            dot = sum(r[k][i] * r[k][j] for k in range(3))
            worst = max(worst, abs(dot - (1.0 if i == j else 0.0)))
    if worst > ORTHO_TOL:
        return f"rotation is not orthonormal (max |R^T R - I| = {worst:.2e}); scaled or sheared camera node?"
    det = (
        r[0][0] * (r[1][1] * r[2][2] - r[1][2] * r[2][1])
        - r[0][1] * (r[1][0] * r[2][2] - r[1][2] * r[2][0])
        + r[0][2] * (r[1][0] * r[2][1] - r[1][1] * r[2][0])
    )
    if det < 0:
        return "rotation has det < 0 (mirrored camera)"
    return None


def _hfov_deg(focal_mm: float, film_mm: float, zoom: float = 1.0) -> float:
    return math.degrees(2 * math.atan(film_mm / (2 * focal_mm * zoom)))


# --------------------------------------------------------------------------- camera digest


def camera_digest(camera_name: str) -> dict[str, Any]:
    """Everything the manifest's camera block needs, read at the scene's current time (the caller
    sets the time slider to the hero frame first; hero_frame here is the frame that was read)."""
    rt = runtime()
    warnings: list[str] = []
    rd = _Reader(warnings)

    node = rt.getNodeByName(camera_name, exact=True)
    if node is None:
        raise CameraNotFound(camera_name)

    transform = rd.get(node, "objecttransform", "objecttransform")
    world_from_camera = None
    if transform is not None:
        try:
            world_from_camera = world_from_camera_from_max_rows(_matrix_rows(transform))
        except Exception as exc:
            warnings.append(f"objecttransform: unreadable ({type(exc).__name__})")
    if world_from_camera is not None:
        problem = rotation_error(world_from_camera)
        if problem:
            warnings.append(f"objecttransform: {problem}")

    focal = rd.num(node, "focal_length", "focal_length")
    film = rd.num(node, "film_width", "film_width")
    zoom = rd.num(node, "zoom_factor", "zoom_factor")
    fov = rd.num(node, "fov", "fov")
    if film is None:
        film = 36.0
        warnings.append("film_width: assumed 36.0 mm")
    if zoom is None:
        zoom = 1.0
    if focal is None and fov is not None and 0 < fov < 180:
        focal = film / (2 * math.tan(math.radians(fov) / 2))
        warnings.append("focal_length: derived from fov and the film gate")
    if focal and fov is not None and abs(_hfov_deg(focal, film, zoom) - fov) > 0.05:
        warnings.append(
            f"fov: {fov:.4f} deg disagrees with focal/film/zoom ({_hfov_deg(focal, film, zoom):.4f})"
        )

    iso = rd.num(node, "ISO", "ISO")
    shutter_speed = rd.num(node, "shutter_speed", "shutter_speed")  # V-Ray: 1/seconds (200 = 1/200 s)
    shutter_s = 1.0 / shutter_speed if shutter_speed else None

    white_balance = rd.get(node, "white_balance", "white_balance")
    kelvin = None
    if white_balance is not None:
        try:
            kelvin = float(white_balance)
        except (TypeError, ValueError):
            warnings.append("white_balance: not a Kelvin number (colour swatch?); sent as neutral")

    width = rd.get(rt, "renderWidth", "renderWidth")
    height = rd.get(rt, "renderHeight", "renderHeight")
    resolution = [int(width), int(height)] if width is not None and height is not None else None
    pixel_aspect = rd.num(rt, "renderPixelAspect", "renderPixelAspect")

    anim = rd.get(rt, "animationRange", "animationRange")
    frame_range = None
    if anim is not None:
        start, end = _frame_of(getattr(anim, "start", None)), _frame_of(getattr(anim, "end", None))
        if start is not None and end is not None:
            frame_range = [start, end]
    current = _frame_of(rd.get(rt, "currentTime", "currentTime"))
    hero_frame = current if current is not None else (frame_range[0] if frame_range else None)
    fps = rd.num(rt, "frameRate", "frameRate")

    safe_frame = None
    if resolution:
        safe_frame = {"x": 0, "y": 0, "width": resolution[0], "height": resolution[1]}

    return {
        "name": str(getattr(node, "name", camera_name)),
        "node_type": _class_name(rt, node),
        "world_from_camera": world_from_camera,
        "fov_deg": fov,
        "focal_mm": focal,
        "film_gate_mm": film,
        "zoom_factor": zoom,
        "focus_distance": rd.num(node, "focus_distance", "focus_distance"),
        "f_number": rd.num(node, "f_number", "f_number"),
        "dof_enabled": bool(rd.flag(node, "use_dof", "use_dof")),
        "exposure": {"iso": iso, "shutter_s": shutter_s},
        "white_balance_k": kelvin,
        "lens_shift": {
            "horizontal": _first_number(rd, node, SHIFT_H_NAMES),
            "vertical": _first_number(rd, node, SHIFT_V_NAMES),
            "raw_unit": "vray_physical",
        },
        "tilt": {
            **{label: _first_number(rd, node, (name,)) for name, label in TILT_NAMES},
            "auto": _first_flag(rd, node, TILT_AUTO_NAMES),
        },
        "distortion": {
            "amount": _first_number(rd, node, DISTORTION_NAMES),
            "type": _first_number(rd, node, DISTORTION_TYPE_NAMES),
        },
        "pixel_aspect": pixel_aspect if pixel_aspect is not None else 1.0,
        "resolution": resolution,
        "safe_frame": safe_frame,
        "frame_range": frame_range,
        "hero_frame": hero_frame,
        "fps": fps,
        "warnings": warnings,
    }


def _class_name(rt: Any, node: Any) -> str | None:
    try:
        return str(rt.classOf(node))
    except Exception:
        return None


# --------------------------------------------------------------------------- scene digest


def scene_digest() -> dict[str, Any]:
    """The open .max file's identity plus the scene units. File metadata only (not the contents)."""
    rt = runtime()
    warnings: list[str] = []
    rd = _Reader(warnings)

    name = size = mtime = None
    file_name = rd.get(rt, "maxFileName", "maxFileName")
    file_dir = rd.get(rt, "maxFilePath", "maxFilePath")
    if file_name:
        name = str(file_name)
        try:
            stat = (Path(str(file_dir or "")) / name).stat()
            size, mtime = int(stat.st_size), _utc(stat.st_mtime)
        except OSError:
            warnings.append("max file: not found on disk (unsaved scene?)")

    units = rd.get(rt, "units", "units")
    system = scale = None
    if units is not None:
        system = str(getattr(units, "SystemType", "")).lstrip("#").lower() or None
        scale = rd.num(units, "SystemScale", "units.SystemScale")
    meters_per_unit = None
    if system in _METERS_PER_TYPE and scale:
        meters_per_unit = _METERS_PER_TYPE[system] * scale
    elif units is not None:
        warnings.append(f"units: unknown system type {system!r}")

    return {
        "max_file": name,
        "size_bytes": size,
        "mtime_utc": mtime,
        "units_system": system,
        "meters_per_unit": meters_per_unit,
        "scene_hash_method": SCENE_HASH_METHOD,
        "warnings": warnings,
    }


def scene_hash_payload(digest: dict[str, Any]) -> bytes:
    """Canonical bytes of the interim file-meta digest. sha256 of this is scene_hash() below; the
    manifest's scene_hash is the BLAKE3 of the same bytes, computed by a caller that has blake3."""
    return json.dumps(
        {
            "max_file": digest.get("max_file"),
            "mtime_utc": digest.get("mtime_utc"),
            "size_bytes": digest.get("size_bytes"),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def scene_hash(digest: dict[str, Any]) -> str:
    """sha256 hex of the (file name, size, mtime) triple, method "file-meta-v0"."""
    return hashlib.sha256(scene_hash_payload(digest)).hexdigest()


# --------------------------------------------------------------------------- nodes for the legend


def scene_nodes() -> list[dict[str, Any]]:
    """Scene geometry as [{name, material, layer, tags, transform, material_uid}] for
    legend.build_id_legend. Tags come from the node's user property "plate_tags" (comma separated).
    transform is the node's objecttransform rows (it goes into the node's digest record) and
    material_uid is the material's handle, so two different materials sharing a name can be told
    apart. Non-geometry nodes are skipped when the runtime can tell (superClassOf == GeometryClass).
    The caller keeps only the nodes visible in the camera (those with Cryptomatte coverage)."""
    rt = runtime()
    geometry_class = getattr(rt, "GeometryClass", None)
    nodes: list[dict[str, Any]] = []
    for node in list(getattr(rt, "objects", [])):
        if geometry_class is not None:
            try:
                if rt.superClassOf(node) != geometry_class:
                    continue
            except Exception:
                pass
        layer = getattr(getattr(node, "layer", None), "name", None)
        mat = getattr(node, "material", None)
        material = getattr(mat, "name", None)
        tags: list[str] = []
        try:
            raw = rt.getUserProp(node, "plate_tags")
        except Exception:
            raw = None
        if raw:
            tags = sorted({t.strip().lower() for t in str(raw).split(",") if t.strip()})
        try:
            transform = [list(r) for r in _matrix_rows(node.objecttransform)]
        except Exception:
            transform = None
        material_uid = None
        if mat is not None:
            try:
                material_uid = int(rt.getHandleByAnim(mat))
            except Exception:
                material_uid = None
        nodes.append(
            {
                "name": str(node.name),
                "material": str(material) if material else None,
                "layer": str(layer) if layer else None,
                "tags": tags,
                "transform": transform,
                "material_uid": material_uid,
            }
        )
    return nodes


# --------------------------------------------------------------------------- global state for view_hash


def scene_globals() -> dict[str, Any]:
    """The state that affects every pixel of every camera (PACKET.md section 5): lights, the
    environment, the renderer. Interim digest_version "0": light and environment property names are
    unverified on a Max box, every miss is a warning, and nothing here is hashed by this module."""
    rt = runtime()
    warnings: list[str] = []
    rd = _Reader(warnings)

    lights = []
    for light in list(getattr(rt, "lights", [])):
        try:
            transform = [list(r) for r in _matrix_rows(light.objecttransform)]
        except Exception:
            transform = None
        lights.append(
            {
                "name": str(getattr(light, "name", "")),
                "class": _class_name(rt, light),
                "transform": transform,
                "on": _first_flag(rd, light, ("on", "enabled")),
                "multiplier": _first_number(
                    rd, light, ("multiplier", "intensity_multiplier", "intensity"), 1.0
                ),
            }
        )
    lights.sort(key=lambda entry: entry["name"])

    env_map = rd.get(rt, "environmentMap", "environmentMap")
    renderers = getattr(rt, "renderers", None)
    return {
        "digest_version": DIGEST_VERSION,
        "lights": lights,
        "environment": {
            "map": _class_name(rt, env_map) if env_map is not None else None,
            "use_map": bool(rd.flag(rt, "useEnvironmentMap", "useEnvironmentMap")),
        },
        "renderer": _class_name(rt, getattr(renderers, "current", None)) if renderers is not None else None,
        "warnings": warnings,
    }
