"""Scout poses: where the Scout action puts its temporary cameras. Pure geometry, standard library.

Copied from MaxDirector core/scout.py (scout_poses and _dominant_feature, with the vector helpers
from core/anchors.py and the BBox, CameraState and ScoutView shapes from core/models.py) so that
plate_max carries no MaxDirector import. A scout set covers a room with variety: four wide eye-level
corners (to read the space), one elevated three-quarter view, one plan, and up to two 35 mm views
composed on the dominant vertical feature (low hero and high). Z-up by default (3ds Max); "Y" swaps
the axes.

Doctrine: a scout pose is a new camera angle. Its preview and any packet made from it are V-Ray
renders of the real geometry, never a generated view; the Scout action only makes a packet from one
when it is asked to explicitly.

look_at_rows(eye, look, up) is MaxDirector's maxbridge/authoring._look_at_tm as pure rows: the Max
Matrix3 rows (local X, Y, Z in world, then the position) of a camera at eye looking at look.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

Vec3 = tuple  # (x, y, z) floats

UP_Z = "Z"
UP_Y = "Y"
GEOMETRY = "geometry"


# --------------------------------------------------------------------------- vectors (MaxDirector anchors)


def add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def scale(a: Vec3, s: float) -> Vec3:
    return (a[0] * s, a[1] * s, a[2] * s)


def dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def length(a: Vec3) -> float:
    return math.sqrt(dot(a, a))


def normalize(a: Vec3) -> Vec3:
    n = length(a)
    return a if n == 0.0 else scale(a, 1.0 / n)


# --------------------------------------------------------------------------- shapes (MaxDirector models)


@dataclass(frozen=True)
class BBox:
    lo: Vec3
    hi: Vec3

    @property
    def center(self) -> Vec3:
        return tuple((a + b) / 2.0 for a, b in zip(self.lo, self.hi, strict=True))

    @property
    def size(self) -> Vec3:
        return tuple(b - a for a, b in zip(self.lo, self.hi, strict=True))

    @property
    def diagonal(self) -> float:
        return sum(s * s for s in self.size) ** 0.5

    def union(self, other: BBox) -> BBox:
        return BBox(
            tuple(min(a, b) for a, b in zip(self.lo, other.lo, strict=True)),
            tuple(max(a, b) for a, b in zip(self.hi, other.hi, strict=True)),
        )

    def to_dict(self) -> dict[str, list[float]]:
        return {"lo": list(self.lo), "hi": list(self.hi)}

    @staticmethod
    def coerce(value: Any) -> BBox:
        """A BBox from a BBox (or anything with .lo and .hi, such as MaxDirector's), {"lo", "hi"} or a
        (lo, hi) pair; lo and hi are sorted per axis."""
        if hasattr(value, "lo") and hasattr(value, "hi"):
            lo, hi = value.lo, value.hi
        elif isinstance(value, dict):
            lo, hi = value["lo"], value["hi"]
        else:
            lo, hi = value
        lo3 = tuple(float(v) for v in lo)
        hi3 = tuple(float(v) for v in hi)
        if len(lo3) != 3 or len(hi3) != 3:
            raise ValueError("a bounding box needs two 3-vectors (lo, hi)")
        return BBox(
            tuple(min(a, b) for a, b in zip(lo3, hi3, strict=True)),
            tuple(max(a, b) for a, b in zip(lo3, hi3, strict=True)),
        )


@dataclass(frozen=True)
class CameraState:
    """A resolved camera pose: position, look-at target, focal length in mm (36 mm film), up vector."""

    pos: Vec3
    look_at: Vec3
    fov_mm: float
    up: Vec3 = (0.0, 0.0, 1.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pos": list(self.pos),
            "look_at": list(self.look_at),
            "fov_mm": self.fov_mm,
            "up": list(self.up),
        }


@dataclass
class ScoutView:
    """One scout camera: an id, a label and its known world pose (thumb_path is filled by the bridge)."""

    id: int
    label: str
    pose: CameraState
    thumb_path: str = ""


@dataclass(frozen=True)
class ScoutNode:
    """A scene node reduced to what the feature-directed views need (MaxDirector's NodeInfo subset)."""

    name: str
    bbox: BBox | None = None
    category: str = GEOMETRY
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    @staticmethod
    def coerce(value: Any) -> ScoutNode:
        """A ScoutNode from a ScoutNode, a dict {name, bbox, category?} or any object with .name and
        .bbox (MaxDirector's NodeInfo: category may be an enum whose .value is the string)."""
        if isinstance(value, ScoutNode):
            return value
        if isinstance(value, dict):
            name, bbox, category = value.get("name", ""), value.get("bbox"), value.get("category", GEOMETRY)
        else:
            name = getattr(value, "name", "")
            bbox = getattr(value, "bbox", None)
            category = getattr(value, "category", GEOMETRY)
        category = str(getattr(category, "value", category))
        return ScoutNode(str(name), BBox.coerce(bbox) if bbox is not None else None, category)


# --------------------------------------------------------------------------- poses


def _up(up_axis: str) -> Vec3:
    return (0.0, 0.0, 1.0) if str(up_axis).upper() == UP_Z else (0.0, 1.0, 0.0)


def _dominant_feature(nodes: list[ScoutNode], up_i: int, bounds: BBox) -> ScoutNode | None:
    """The geometry node with the largest vertical extent: usually the hero wall, window or tall
    feature (the floor has a huge horizontal but a tiny vertical extent, so it loses). Anything taller
    than 1.2 times the whole scene is skipped."""
    best, best_h = None, 0.0
    span = bounds.size[up_i] or 1.0
    for n in nodes:
        if n.category != GEOMETRY or n.bbox is None:
            continue
        h = n.bbox.size[up_i]
        if h > best_h and h < span * 1.2:
            best, best_h = n, h
    return best


def scout_poses(
    bounds: Any,
    up_axis: str = UP_Z,
    meters_to_units: float = 1.0,
    eye_m: float = 1.6,
    nodes: list[Any] | None = None,
) -> list[ScoutView]:
    """The scout views over bounds (a BBox, {"lo","hi"} or (lo, hi) in scene units): ids 0-3 the
    eye-level corners (24 mm, inset 8% off the walls), 4 the elevated three-quarter (28 mm), 5 the
    plan (18 mm), 6 and 7 the feature views (35 mm at 1.1 m and 2.3 m) when a dominant vertical node
    exists. meters_to_units is scene units per metre (1000 for a millimetre scene). Deterministic."""
    bounds = BBox.coerce(bounds)
    scout_nodes = [ScoutNode.coerce(n) for n in nodes or []]
    c = bounds.center
    lo, hi = bounds.lo, bounds.hi
    diag = bounds.diagonal
    up = _up(up_axis)
    up_i = 2 if str(up_axis).upper() == UP_Z else 1
    floor = lo[up_i]
    eye = floor + eye_m * meters_to_units

    def _at_height(p: Vec3, h: float) -> Vec3:
        q = list(p)
        q[up_i] = h
        return tuple(q)

    ax = [i for i in range(3) if i != up_i]
    corners = [
        (lo[ax[0]], lo[ax[1]]),
        (hi[ax[0]], lo[ax[1]]),
        (hi[ax[0]], hi[ax[1]]),
        (lo[ax[0]], hi[ax[1]]),
    ]
    views: list[ScoutView] = []
    look = _at_height(c, eye)
    for i, (u, v) in enumerate(corners):
        p = [0.0, 0.0, 0.0]
        p[ax[0]], p[ax[1]] = u, v
        p[up_i] = eye
        pos = add(tuple(p), scale(sub(look, tuple(p)), 0.08))  # inset off the wall
        views.append(ScoutView(i, f"corner_{i}", CameraState(pos, look, fov_mm=24.0, up=up)))

    p0 = list(views[0].pose.pos)
    p0[up_i] = floor + eye + 0.4 * diag
    views.append(ScoutView(4, "high_3q", CameraState(tuple(p0), c, fov_mm=28.0, up=up)))

    top = list(c)
    top[up_i] = hi[up_i] + 0.6 * diag
    plan_up = (0.0, 1.0, 0.0) if str(up_axis).upper() == UP_Z else (0.0, 0.0, 1.0)
    views.append(ScoutView(5, "plan", CameraState(tuple(top), c, fov_mm=18.0, up=plan_up)))

    feature = _dominant_feature(scout_nodes, up_i, bounds)
    if feature is not None and feature.bbox is not None:
        fc = feature.bbox.center
        toward = sub(c, fc)  # stand between the feature and the room centre
        d = length(toward)
        dir_ = normalize(toward) if d > 1e-6 else normalize(sub(c, lo))
        standoff = max(0.35 * diag, feature.bbox.diagonal)
        for j, (label, h_m) in enumerate((("feature_low", 1.1), ("feature_high", 2.3))):
            pos = add(fc, scale(dir_, standoff))
            pos = _at_height(pos, floor + h_m * meters_to_units)
            views.append(ScoutView(6 + j, label, CameraState(pos, fc, fov_mm=35.0, up=up)))
    return views


def look_at_rows(eye: Vec3, look: Vec3, up: Vec3 = (0.0, 0.0, 1.0)) -> tuple[Vec3, Vec3, Vec3, Vec3]:
    """Max Matrix3 rows (local X, Y, Z axes in world, then the position) of a camera at eye looking at
    look. Max cameras look down local -Z, so local Z points from the target back to the eye. A view
    parallel to up picks a stable right vector instead (MaxDirector authoring._look_at_tm)."""
    z = normalize(sub(eye, look))
    if length(z) < 1e-9:
        raise ValueError("eye and look-at coincide: the camera has no view direction")
    x = cross(up, z)
    if length(x) < 1e-6:  # looking straight along up
        x = cross((0.0, 1.0, 0.0), z)
        if length(x) < 1e-6:
            x = (1.0, 0.0, 0.0)
    x = normalize(x)
    y = normalize(cross(z, x))
    return x, y, z, tuple(float(v) for v in eye)
