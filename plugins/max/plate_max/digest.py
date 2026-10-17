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
moved with Affect Pivot Only renders from its object), at the hero frame (camera_digest(frame=)).

Every camera field is read through a tuple of candidate property names, first readable wins (lens
shift: every readable candidate is read and the largest magnitude wins, so an alias cannot hide a
non-zero shift). The names are the best known V-Ray Physical Camera names and are NOT verified on a
Max box yet (TODO-verify, plugins/max/ONBOX.md step 1): a miss is a digest warning, and
packet.build_camera_block refuses any camera that reads as shifted, tilted or distorted.

The scene digest (digest_version "1", method content-v1) is one canonical text line per geometry
node, light, the environment, the units and the render setup, sorted, so it does not depend on node
order and a resave with no change keeps the hash (PACKET.md section 5). The legend's per-node
records and scene_globals() (view_hash) are still the interim digest_version "0".
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bridge.scene import at_time
from .rt import runtime

SCENE_HASH_METHOD = "content-v1"
# manifest scene_hash.digest_version of the content digest scene_hash_payload() hashes.
SCENE_DIGEST_VERSION = "1"
# digest_version of the interim records: legend node digests and scene_globals() (view_hash).
DIGEST_VERSION = "0"
SCENE_DIGEST_HEADER = "plate-scene-digest 1"

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
DEFAULT_FILM_MM = 36.0

# V-Ray Physical Camera property candidates, all TODO-verify on the box (ONBOX.md step 1). MAXScript
# is case-insensitive, so "iso" and "ISO" are one property there; both are listed because the fake
# (and any other host) is not.
FOCAL_NAMES = ("focal_length_mm", "focal_length")  # TODO-verify; "fov" is the last resort (derived)
FOV_NAMES = ("fov",)  # TODO-verify: horizontal FOV in degrees
SPECIFY_FOV_NAMES = ("specify_fov",)  # TODO-verify: when on, V-Ray renders from fov, not focal_length
FILM_NAMES = ("film_width", "film_gate", "apertureWidth")  # TODO-verify, default 36 mm
ZOOM_NAMES = ("zoom_factor",)  # TODO-verify
F_NUMBER_NAMES = ("f_number", "fnumber")  # TODO-verify
FOCUS_NAMES = ("focus_distance", "targetDistance")  # TODO-verify
SPECIFY_FOCUS_NAMES = ("specify_focus",)  # TODO-verify: off means focus at the target distance
TARGET_DISTANCE_NAMES = ("target_distance", "targetDistance")  # TODO-verify
DOF_NAMES = ("use_dof", "dof_on")  # TODO-verify
EXPOSURE_ON_NAMES = ("exposure",)  # TODO-verify: 0/False none, 1/True physical, 2 EV (V-Ray 5+)
EV_NAMES = ("ev", "exposure_value")  # TODO-verify: the EV-mode exposure value (assumed EV100)
ISO_NAMES = ("iso", "ISO")  # TODO-verify
SHUTTER_SPEED_NAMES = ("shutter_speed",)  # TODO-verify: V-Ray stores 1/seconds (200 = 1/200 s)
SHUTTER_SECONDS_NAMES = ("shutter_s",)  # TODO-verify: a host that stores seconds directly
CAMERA_TYPE_NAMES = ("type", "camera_type")  # TODO-verify: 0 still, 1 movie (shutter angle), 2 video
SHUTTER_ANGLE_NAMES = ("shutter_angle",)  # TODO-verify: degrees, movie camera only
WHITE_BALANCE_NAMES = (  # TODO-verify: Kelvin; V-Ray's white_balance may be a colour swatch instead
    "white_balance_temperature",
    "whiteBalance_temp",
    "temperature",
    "white_balance",
)
# TODO-verify which axis "lens_shift" is (older V-Ray builds used it for the vertical shift). The
# axis only matters for the record: v0.1 refuses a camera with any non-zero shift candidate.
SHIFT_H_NAMES = ("lens_shift", "horizontal_shift", "horizontal_offset", "lens_shift_x")
SHIFT_V_NAMES = ("vertical_shift", "vertical_offset", "lens_shift_y")  # TODO-verify
TILT_NAMES = (("vertical_tilt", "vertical"), ("horizontal_tilt", "horizontal"))  # TODO-verify
TILT_AUTO_NAMES = ("auto_vertical_tilt", "guess_vertical_tilt")  # TODO-verify
DISTORTION_NAMES = ("distortion",)  # TODO-verify
DISTORTION_TYPE_NAMES = ("distortion_type",)  # TODO-verify

# Light parameters that change pixels, read when the light has them (TODO-verify per light class).
LIGHT_PARAM_NAMES = (
    "on",
    "enabled",
    "multiplier",
    "intensity",
    "intensity_multiplier",
    "color",
    "colour",
    "color_temperature",
    "type",
    "size0",
    "size1",
    "size_multiplier",
    "turbidity",
    "ozone",
    "invisible",
    "texmap_on",
)


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


def _probe(node: Any, names: tuple[str, ...]) -> tuple[str | None, Any]:
    """(name, value) of the first candidate that reads as something other than undefined; silent."""
    for name in names:
        try:
            value = getattr(node, name)
        except Exception:
            continue
        if value is not None:
            return name, value
    return None, None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _candidate_number(
    rd: _Reader, node: Any, names: tuple[str, ...], read_from: dict[str, Any], field: str
) -> float | None:
    """First candidate that reads; None plus a warning naming every candidate when none does (a
    value that is not a number is its own warning). read_from[field] records the name used."""
    name, value = _probe(node, names)
    read_from[field] = name
    if name is None:
        rd.warnings.append(f"{'/'.join(names)}: no readable property (name unverified on this build)")
        return None
    number = _as_float(value)
    if number is None:
        rd.warnings.append(f"{name}: not a number ({value!r})")
    return number


def _largest_shift(
    rd: _Reader, node: Any, names: tuple[str, ...], read_from: dict[str, Any], field: str
) -> float:
    """Every readable shift candidate is read and the largest magnitude wins: an alias or a second
    shift property can never hide a non-zero shift. None readable: a warning and 0.0."""
    found: dict[str, float] = {}
    for name in names:
        try:
            value = getattr(node, name)
        except Exception:
            continue
        number = _as_float(value)
        if number is not None:
            found[name] = number
    read_from[field] = sorted(found) or None
    if not found:
        rd.warnings.append(
            f"{'/'.join(names)}: no readable property (name unverified on this build); assumed 0.0"
        )
        return 0.0
    return max(found.values(), key=abs)


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rt_value(name: str) -> Any:
    """getattr(rt, name) or None (an undefined MAXScript global may raise anything)."""
    try:
        return getattr(runtime(), name)
    except Exception:
        return None


def _recipe_value(recipe: Any, *keys: str) -> Any:
    """recipe[key] or recipe.key for the first key present (a dict or a recipes.CameraRecipe)."""
    if recipe is None:
        return None
    for key in keys:
        value = recipe.get(key) if isinstance(recipe, dict) else getattr(recipe, key, None)
        if value is not None:
            return value
    return None


def _safe_rect(rect: Any) -> dict[str, int] | None:
    """{x, y, width, height} from a recipe rectangle ({x, y, width|w, height|h} or (x, y, w, h))."""
    if rect is None:
        return None
    if isinstance(rect, (list, tuple)):
        x, y, w, h = rect
    else:

        def get(key: str) -> Any:
            return rect.get(key) if isinstance(rect, dict) else getattr(rect, key, None)

        x, y = get("x") or 0, get("y") or 0
        w = get("width") if get("width") is not None else get("w")
        h = get("height") if get("height") is not None else get("h")
    return {"x": int(x), "y": int(y), "width": int(w), "height": int(h)}


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


def focal_from_fov(fov_deg: float, film_mm: float) -> float:
    """focal = film_width / (2 tan(fov / 2)): the focal length of a horizontal FOV on a film gate."""
    return film_mm / (2 * math.tan(math.radians(fov_deg) / 2))


def intrinsics(
    focal_mm: float, film_mm: float, zoom: float, width: int, height: int, pixel_aspect: float = 1.0
) -> dict[str, Any]:
    """PACKET.md 3.3 at full float precision: fx = focal * zoom * width / film, fy = fx * pixel
    aspect, the principal point at the frame centre (v0.1: no shift), the horizontal FOV from the
    film gate (fitted horizontally) and the vertical FOV from the aspect. The single implementation
    behind camera_digest and packet.build_camera_block."""
    hfov = _hfov_deg(focal_mm, film_mm, zoom)
    vfov = math.degrees(2 * math.atan(math.tan(math.radians(hfov) / 2) * height / (width * pixel_aspect)))
    fx = focal_mm * zoom * width / film_mm
    fy = fx * pixel_aspect
    cx, cy = width / 2, height / 2
    return {
        "fov_deg": {"horizontal": hfov, "vertical": vfov},
        "intrinsics_px": {
            "fx": fx,
            "fy": fy,
            "principal_point": [cx, cy],
            "K": [[fx, 0, cx], [0, fy, cy], [0, 0, 1]],
        },
    }


# --------------------------------------------------------------------------- camera digest


def camera_digest(camera_name: str, frame: int | None = None, recipe: Any = None) -> dict[str, Any]:
    """Everything the manifest's camera block needs (PACKET.md 3), read at frame under
    pymxs.attime (frame None: the scene's current time, which is then the hero frame).

    recipe (a dict or a recipes.CameraRecipe) supplies the deliverable safe_frame ({x, y, width|w,
    height|h}) and the overscan engine aspect ("overscan_aspect", or "overscan": {"engine_aspect"});
    without one the safe frame is the whole rendered frame. fov_deg and intrinsics_px are computed
    exactly as PACKET.md 3.3 (None while focal, film or the resolution is unknown); fov_node_deg is
    the node's own fov reading; read_from names the property each lens field came from."""
    rt = runtime()
    warnings: list[str] = []
    rd = _Reader(warnings)
    read_from: dict[str, Any] = {}

    node = rt.getNodeByName(camera_name, exact=True)
    if node is None:
        raise CameraNotFound(camera_name)

    with at_time(frame):
        transform = rd.get(node, "objecttransform", "objecttransform")
        world_from_camera = None
        if transform is not None:
            try:
                world_from_camera = world_from_camera_from_max_rows(_matrix_rows(transform))
            except Exception as exc:
                warnings.append(f"objecttransform: unreadable ({type(exc).__name__})")
        lens = _read_lens(rd, node, read_from)
    if world_from_camera is not None:
        problem = rotation_error(world_from_camera)
        if problem:
            warnings.append(f"objecttransform: {problem}")

    width = rd.get(rt, "renderWidth", "renderWidth")
    height = rd.get(rt, "renderHeight", "renderHeight")
    resolution = [int(width), int(height)] if width is not None and height is not None else None
    pixel_aspect = rd.num(rt, "renderPixelAspect", "renderPixelAspect")
    pixel_aspect = pixel_aspect if pixel_aspect is not None else 1.0

    anim = rd.get(rt, "animationRange", "animationRange")
    frame_range = None
    if anim is not None:
        start, end = _frame_of(getattr(anim, "start", None)), _frame_of(getattr(anim, "end", None))
        if start is not None and end is not None:
            frame_range = [start, end]
    if frame is not None:
        hero_frame = _frame_of(frame)
    else:
        current = _frame_of(rd.get(rt, "currentTime", "currentTime"))
        hero_frame = current if current is not None else (frame_range[0] if frame_range else None)
    fps = rd.num(rt, "frameRate", "frameRate")

    focal, film, zoom = lens["focal_mm"], lens["film_gate_mm"], lens["zoom_factor"]
    fov_deg = intrinsics_px = None
    if resolution and focal and film and zoom and min(focal, film, zoom) > 0:
        derived = intrinsics(focal, film, zoom, resolution[0], resolution[1], pixel_aspect)
        fov_deg, intrinsics_px = derived["fov_deg"], derived["intrinsics_px"]

    safe_frame = _safe_rect(_recipe_value(recipe, "safe_frame"))
    if safe_frame is None and resolution:
        safe_frame = {"x": 0, "y": 0, "width": resolution[0], "height": resolution[1]}

    out: dict[str, Any] = {
        "name": str(getattr(node, "name", camera_name)),
        "node_type": _class_name(rt, node),
        "world_from_camera": world_from_camera,
        **lens,
        "fov_deg": fov_deg,
        "intrinsics_px": intrinsics_px,
        "pixel_aspect": pixel_aspect,
        "resolution": resolution,
        "safe_frame": safe_frame,
        "frame_range": frame_range,
        "hero_frame": hero_frame,
        "fps": fps,
        "read_from": read_from,
        "warnings": warnings,
    }
    overscan = _recipe_value(recipe, "overscan")
    aspect = _recipe_value(recipe, "overscan_aspect", "engine_aspect")
    if overscan is None and aspect:
        overscan = {"engine_aspect": str(aspect)}
    if overscan:
        out["overscan"] = dict(overscan)
    return out


def _read_lens(rd: _Reader, node: Any, read_from: dict[str, Any]) -> dict[str, Any]:
    """The lens, exposure and white balance fields of camera_digest (called inside at_time)."""
    warnings = rd.warnings

    film_name, film_value = _probe(node, FILM_NAMES)
    film = _as_float(film_value)
    read_from["film_gate_mm"] = film_name
    if film is None or film <= 0:
        film = DEFAULT_FILM_MM
        warnings.append(f"film_width: assumed {DEFAULT_FILM_MM} mm ({'/'.join(FILM_NAMES)} unreadable)")
    zoom_name, zoom_value = _probe(node, ZOOM_NAMES)
    zoom = _as_float(zoom_value)
    zoom = zoom if zoom is not None else 1.0
    read_from["zoom_factor"] = zoom_name

    fov_name, fov_value = _probe(node, FOV_NAMES)
    fov = _as_float(fov_value)
    focal_name, focal_value = _probe(node, FOCAL_NAMES)
    focal = _as_float(focal_value)
    read_from["focal_mm"] = focal_name
    fov_usable = fov is not None and 0 < fov < 180
    _, specify_fov = _probe(node, SPECIFY_FOV_NAMES)
    if specify_fov and fov_usable:
        focal = focal_from_fov(fov, film)
        read_from["focal_mm"] = fov_name
        warnings.append("focal_length: derived from fov (specify_fov is on)")
        if zoom != 1.0:
            warnings.append("zoom_factor with specify_fov: how V-Ray combines the two is unverified")
    elif focal is None:
        if fov_usable:
            focal = focal_from_fov(fov, film)
            read_from["focal_mm"] = fov_name
            warnings.append("focal_length: derived from fov and the film gate")
        else:
            warnings.append(f"{'/'.join(FOCAL_NAMES + FOV_NAMES)}: no readable focal length or fov")
    elif fov is not None and abs(_hfov_deg(focal, film, zoom) - fov) > 0.05:
        warnings.append(
            f"fov: {fov:.4f} deg disagrees with focal/film/zoom ({_hfov_deg(focal, film, zoom):.4f})"
        )

    focus = _candidate_number(rd, node, FOCUS_NAMES, read_from, "focus_distance")
    specify_focus_name, specify_focus = _probe(node, SPECIFY_FOCUS_NAMES)
    if specify_focus_name is not None and not specify_focus:
        target_name, target = _probe(node, TARGET_DISTANCE_NAMES)
        if _as_float(target) is not None:
            focus = _as_float(target)
            read_from["focus_distance"] = target_name

    kelvin = None
    wb_name, wb_value = _probe(node, WHITE_BALANCE_NAMES)
    read_from["white_balance_k"] = wb_name
    if wb_name is None:
        warnings.append(f"{'/'.join(WHITE_BALANCE_NAMES)}: no readable white balance; sent as neutral")
    else:
        kelvin = _as_float(wb_value)
        if kelvin is None:
            warnings.append(f"{wb_name}: not a Kelvin number (colour swatch?); sent as neutral")

    dof_name, dof = _probe(node, DOF_NAMES)
    read_from["dof_enabled"] = dof_name
    if dof_name is None:
        warnings.append(f"{'/'.join(DOF_NAMES)}: no readable property (name unverified on this build)")

    return {
        "fov_node_deg": fov,
        "focal_mm": focal,
        "film_gate_mm": film,
        "zoom_factor": zoom,
        "focus_distance": focus,
        "f_number": _candidate_number(rd, node, F_NUMBER_NAMES, read_from, "f_number"),
        "dof_enabled": bool(dof),
        "exposure": _read_exposure(rd, node, read_from),
        "white_balance_k": kelvin,
        "lens_shift": {
            "horizontal": _largest_shift(rd, node, SHIFT_H_NAMES, read_from, "lens_shift.horizontal"),
            "vertical": _largest_shift(rd, node, SHIFT_V_NAMES, read_from, "lens_shift.vertical"),
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
    }


def _read_exposure(rd: _Reader, node: Any, read_from: dict[str, Any]) -> dict[str, Any]:
    """{iso, shutter_s, enabled, ev}. shutter_speed is V-Ray's 1/seconds; a movie camera (type 1)
    exposes by shutter angle at the scene frame rate. enabled is None when the exposure switch is
    unreadable (then the camera block treats iso + shutter as physical exposure, as before)."""
    iso = _candidate_number(rd, node, ISO_NAMES, read_from, "exposure.iso")
    shutter_s = None
    name, value = _probe(node, SHUTTER_SPEED_NAMES)
    if name is not None:
        speed = _as_float(value)
        if speed is None:
            rd.warnings.append(f"{name}: not a number ({value!r})")
        elif speed > 0:
            shutter_s = 1.0 / speed
    else:
        name, value = _probe(node, SHUTTER_SECONDS_NAMES)
        shutter_s = _as_float(value)
        if name is None:
            rd.warnings.append(
                f"{'/'.join(SHUTTER_SPEED_NAMES + SHUTTER_SECONDS_NAMES)}: no readable shutter"
            )
    read_from["exposure.shutter_s"] = name

    _, camera_type = _probe(node, CAMERA_TYPE_NAMES)
    camera_type = _as_int(camera_type)
    if camera_type == 1:
        _, angle = _probe(node, SHUTTER_ANGLE_NAMES)
        fps = _as_float(_rt_value("frameRate"))
        if _as_float(angle) is not None and fps:
            shutter_s = float(angle) / 360.0 / fps
            read_from["exposure.shutter_s"] = SHUTTER_ANGLE_NAMES[0]
        else:
            rd.warnings.append("movie camera: shutter_angle unreadable; exposure sent as none")
            shutter_s = None
    elif camera_type == 2:
        rd.warnings.append("video camera type: its latency is not a still shutter; exposure sent as none")
        shutter_s = None

    exposure_name, exposure_on = _probe(node, EXPOSURE_ON_NAMES)
    read_from["exposure.enabled"] = exposure_name
    enabled: bool | None = None
    ev = None
    if exposure_name is not None:
        mode = _as_int(exposure_on)
        enabled = bool(mode)
        if mode == 2:
            _, ev_value = _probe(node, EV_NAMES)
            ev = _as_float(ev_value)
    return {"iso": iso, "shutter_s": shutter_s, "enabled": enabled, "ev": ev}


def _class_name(rt: Any, node: Any) -> str | None:
    try:
        return str(rt.classOf(node))
    except Exception:
        return None


# --------------------------------------------------------------------------- scene digest


def _f6(value: Any) -> str:
    """A float at 6 decimals, -0 written as 0 so the sign of nothing cannot change a hash."""
    number = _as_float(value)
    if number is None or math.isnan(number) or math.isinf(number):
        return "?"
    text = f"{number:.6f}"
    return "0.000000" if text == "-0.000000" else text


def _field(value: Any) -> str:
    """One digest field: None as "", with \\, |, CR and LF escaped so a name cannot forge a field."""
    if value is None:
        return ""
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "\\n").replace("\r", "\\r")


def _value_text(value: Any) -> str:
    """A light parameter as text: bools 1/0, numbers at 6 dp, colours and points as a,b,c, else str()."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return _f6(value)
    for keys in (("r", "g", "b"), ("x", "y", "z")):
        if all(hasattr(value, k) for k in keys):
            return ",".join(_f6(getattr(value, k)) for k in keys)
    return _field(value).replace(";", "\\;")


def _transform_text(node: Any) -> str:
    try:
        rows = _matrix_rows(node.objecttransform)
    except Exception:
        return "?"
    return ",".join(_f6(v) for row in rows for v in row)


def _mesh_fingerprint(node: Any, polygon_count: Any) -> str | None:
    """verts,faces,bbox (world min xyz, max xyz) at 6 dp, or None when nothing is readable.
    getPolygonCount <node> returns #(faces, verts) in MAXScript (TODO-verify the order and the cost on
    the heavy scene); the world bounding box is node.min / node.max (TODO-verify for V-Ray proxies and
    Forest Pack, which may report their preview mesh)."""
    verts = faces = None
    if callable(polygon_count):
        try:
            counts = list(polygon_count(node))
            faces, verts = int(counts[0]), int(counts[1])
        except Exception:
            verts = faces = None
    if verts is None:
        try:
            verts, faces = int(node.numVerts), int(node.numFaces)  # TODO-verify: Editable Mesh only
        except Exception:
            verts = faces = None
    try:
        bbox = ",".join(_f6(v) for v in (*_xyz(node.min), *_xyz(node.max)))
    except Exception:
        bbox = None
    if verts is None and bbox is None:
        return None
    counts_text = f"{verts},{faces}" if verts is not None else "?,?"
    return f"{counts_text},{bbox if bbox is not None else '?'}"


def _wirecolor_text(node: Any) -> str:
    colour = getattr(node, "wirecolor", None)
    if colour is None:
        return ""
    try:
        return ",".join(str(int(round(float(getattr(colour, k))))) for k in ("r", "g", "b"))
    except Exception:
        return _field(colour)


def _is_hidden(node: Any) -> bool:
    try:
        hidden = bool(getattr(node, "isHidden", False))
        return hidden or bool(getattr(getattr(node, "layer", None), "ishidden", False))
    except Exception:
        return False


def _geometry(rt: Any) -> list[Any]:
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


def _render_elements_text(rt: Any) -> str:
    """Sorted class=elementname of every render element ("(off)" when disabled); "?" if unreadable."""
    try:
        mgr = rt.maxOps.GetCurRenderElementMgr()
        count = int(mgr.NumRenderElements())
    except Exception:
        return "?"
    items = []
    for index in range(count):
        try:
            element = mgr.GetRenderElement(index)
        except Exception:
            continue
        on = getattr(element, "enabled", True)
        name = getattr(element, "elementname", "")
        items.append(f"{_class_name(rt, element) or '?'}={_field(name)}{'' if on else '(off)'}")
    return ",".join(sorted(items))


def scene_digest_lines(warnings: list[str] | None = None) -> list[str]:
    """The canonical digest lines (digest_version "1"), sorted:

      node|name|layer|material|transform (12 floats, 6 dp)|verts,faces,bbox (6 dp)|wirecolor|hidden
      light|name|class|param=value;...|transform
      env|map class|use_map
      units|system|scale
      render|width|height|pixel_aspect|renderer|elements

    Geometry nodes only: cameras are in view_hash's camera block and helpers render nothing."""
    rt = runtime()
    warnings = warnings if warnings is not None else []
    polygon_count = _rt_value("getPolygonCount")
    lines: list[str] = []
    no_mesh: list[str] = []
    for node in _geometry(rt):
        name = str(getattr(node, "name", ""))
        mesh = _mesh_fingerprint(node, polygon_count)
        if mesh is None:
            no_mesh.append(name)
        fields = (
            "node",
            _field(name),
            _field(getattr(getattr(node, "layer", None), "name", None)),
            _field(getattr(getattr(node, "material", None), "name", None)),
            _transform_text(node),
            mesh or "?",
            _wirecolor_text(node),
            "1" if _is_hidden(node) else "0",
        )
        lines.append("|".join(fields))
    if no_mesh:
        warnings.append(
            f"mesh fingerprint unreadable on {len(no_mesh)} node(s), e.g. {', '.join(sorted(no_mesh)[:3])}: "
            "their topology does not enter the scene hash"
        )

    for light in list(getattr(rt, "lights", [])):
        params = []
        for pname in LIGHT_PARAM_NAMES:
            try:
                value = getattr(light, pname)
            except Exception:
                continue
            if value is not None:
                params.append(f"{pname}={_value_text(value)}")
        fields = (
            "light",
            _field(getattr(light, "name", "")),
            _field(_class_name(rt, light)),
            ";".join(params),
            _transform_text(light),
        )
        lines.append("|".join(fields))

    env_map = _rt_value("environmentMap")
    env_class = _class_name(rt, env_map) if env_map is not None else None
    lines.append(f"env|{_field(env_class)}|{'1' if _rt_value('useEnvironmentMap') else '0'}")
    units = _rt_value("units")
    system = str(getattr(units, "SystemType", "")).lstrip("#").lower() or None
    lines.append(f"units|{_field(system)}|{_f6(getattr(units, 'SystemScale', None))}")
    renderers = _rt_value("renderers")
    renderer = _class_name(rt, getattr(renderers, "current", None)) if renderers is not None else None
    lines.append(
        "|".join(
            (
                "render",
                _field(_rt_value("renderWidth")),
                _field(_rt_value("renderHeight")),
                _f6(_rt_value("renderPixelAspect")),
                _field(renderer),
                _render_elements_text(rt),
            )
        )
    )
    return sorted(lines)


def scene_digest() -> dict[str, Any]:
    """The open .max file's identity (for humans: name, size, mtime), the scene units, and the
    content digest lines that scene_hash_payload() hashes (PACKET.md section 5)."""
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

    lines = scene_digest_lines(warnings)
    return {
        "max_file": name,
        "size_bytes": size,
        "mtime_utc": mtime,
        "units_system": system,
        "meters_per_unit": meters_per_unit,
        "scene_hash_method": SCENE_HASH_METHOD,
        "digest_version": SCENE_DIGEST_VERSION,
        "lines": lines,
        "warnings": warnings,
    }


def scene_hash_payload(digest: dict[str, Any]) -> bytes:
    """Canonical bytes of the scene digest: a header line, then the sorted digest lines, UTF-8 with
    LF. The manifest's scene_hash is the BLAKE3 of these bytes, computed by the daemon
    (bridge.hashing.hash_scene). A digest without lines (built by hand, pre content-v1) falls back
    to the old file-meta triple."""
    lines = digest.get("lines")
    if lines is not None:
        return ("\n".join([SCENE_DIGEST_HEADER, *sorted(lines)]) + "\n").encode("utf-8")
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
    """sha256 hex of scene_hash_payload (a stdlib identity for logs; the manifest carries BLAKE3)."""
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
    nodes: list[dict[str, Any]] = []
    for node in _geometry(rt):
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
