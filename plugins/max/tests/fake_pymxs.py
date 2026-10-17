"""A stand-in for pymxs.runtime so plate_max runs under pytest on macOS and Windows.

It models only the properties plate_max reads: render settings, units, the open file, the animation
range, camera nodes (V-Ray Physical Camera property names) and scene nodes with a layer, a
material and user properties. Rows of a transform are 3-tuples, exactly as a Matrix3's row1..row4
give x/y/z. No file in plate_max imports this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from types import SimpleNamespace
from typing import Any

IDENTITY_ROWS = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0, 0.0))


class FakeMatrix3:
    def __init__(self, rows=IDENTITY_ROWS):
        self.row1, self.row2, self.row3, self.row4 = (tuple(r) for r in rows)

    @property
    def rows(self):
        return (self.row1, self.row2, self.row3, self.row4)


class FakeCamera:
    """VRayPhysicalCamera property names (focal_length, film_width, ISO, shutter_speed, ...)."""

    def __init__(self, name="CAM_main", rows=IDENTITY_ROWS, **overrides: Any):
        self.name = name
        # the pose V-Ray renders from is objecttransform; transform (the pivot) is a decoy that a
        # wrong reader would pick up
        self.objecttransform = FakeMatrix3(rows)
        self.transform = FakeMatrix3(tuple(rows[:3]) + ((rows[3][0] + 500.0, rows[3][1], rows[3][2]),))
        self.focal_length = 35.0
        self.film_width = 36.0
        self.zoom_factor = 1.0
        self.focus_distance = 8.0
        self.f_number = 5.6
        self.ISO = 100
        self.shutter_speed = 200  # V-Ray: 1/s
        self.white_balance = 6500
        self.use_dof = False
        self.horizontal_shift = 0.0
        self.vertical_shift = 0.0
        self.vertical_tilt = 0.0
        self.horizontal_tilt = 0.0
        self.auto_vertical_tilt = False
        self.distortion = 0.0
        self.distortion_type = 0
        for key, value in overrides.items():
            setattr(self, key, value)
        if not hasattr(self, "fov") and self.focal_length:
            self.fov = math.degrees(
                2 * math.atan(self.film_width / (2 * self.focal_length * self.zoom_factor))
            )


@dataclass
class FakeNode:
    name: str
    layer: Any = None
    material: Any = None
    user_props: dict = field(default_factory=dict)
    objecttransform: Any = field(default_factory=FakeMatrix3)


@dataclass
class FakeLight:
    name: str
    on: bool = True
    multiplier: float = 1.0
    objecttransform: Any = field(default_factory=FakeMatrix3)


@lru_cache(maxsize=None)
def shared_material(name):
    """One object per material name, as in a real scene (two nodes using MAT_x share one material)."""
    return SimpleNamespace(name=name)


def _named(name):
    return SimpleNamespace(name=name) if name is not None else None


def make_node(name, layer, material, tags=(), x=0.0):
    props = {"plate_tags": ",".join(tags)} if tags else {}
    mat = material if material is None or not isinstance(material, str) else shared_material(material)
    return FakeNode(
        name=name,
        layer=_named(layer),
        material=mat,
        user_props=props,
        objecttransform=FakeMatrix3(((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (x, 0.0, 0.0))),
    )


class FakeRuntime:
    """Enough of pymxs.runtime for camera_digest, scene_digest and scene_nodes."""

    GeometryClass = "GeometryClass"

    def __init__(self):
        self.renderWidth = 3840
        self.renderHeight = 2160
        self.renderPixelAspect = 1.0
        self.animationRange = SimpleNamespace(start=SimpleNamespace(frame=0), end=SimpleNamespace(frame=0))
        self.currentTime = SimpleNamespace(frame=0)
        self.frameRate = 25.0
        self.maxFilePath = ""
        self.maxFileName = ""
        self.units = SimpleNamespace(SystemType="millimeters", SystemScale=1.0)
        self.objects: list[Any] = []
        self.lights: list[Any] = []
        self.cameras: dict[str, FakeCamera] = {}
        self.environmentMap = None
        self.useEnvironmentMap = False
        self.renderers = SimpleNamespace(current=SimpleNamespace(name="V_Ray_7"))

    def getNodeByName(self, name, exact=True):  # noqa: N802 (pymxs spelling)
        if name in self.cameras:
            return self.cameras[name]
        for node in self.objects:
            if node.name == name:
                return node
        return None

    def classOf(self, node):  # noqa: N802
        if isinstance(node, FakeCamera):
            return "VRayPhysicalCamera"
        if isinstance(node, FakeLight):
            return "VRayLight"
        if node is None:
            raise TypeError("classOf(undefined)")
        return getattr(node, "name", "Box") if isinstance(node, SimpleNamespace) else "Box"

    def getHandleByAnim(self, obj):  # noqa: N802
        return id(obj)

    def superClassOf(self, node):  # noqa: N802
        return "camera" if isinstance(node, FakeCamera) else self.GeometryClass

    def getUserProp(self, node, key):  # noqa: N802
        return getattr(node, "user_props", {}).get(key)

    def add_camera(self, cam: FakeCamera) -> FakeCamera:
        self.cameras[cam.name] = cam
        return cam


# The camera of fake_scene(): level, looking along world direction (cos 30, sin 30, 0), at (12000, -8500, 1600).
# Rows are the local X (right), Y (up) and Z (back, = -forward) axes in world space, then the position.
_C, _S = math.cos(math.radians(30)), math.sin(math.radians(30))
SCENE_CAMERA_ROWS = ((_S, -_C, 0.0), (0.0, 0.0, 1.0), (-_C, -_S, 0.0), (12000.0, -8500.0, 1600.0))

# (node name, layer, material, tags, pixel_share). Node name == layer name, as in the plan.
SCENE_NODES = (
    ("ARCH_walls", "ARCH_walls", "MAT_brick", ("architecture",), 0.40),
    ("GLAZING_south", "GLAZING_south", "MAT_glass", ("glazing",), 0.10),
    ("VEG_tree_01", "VEG_tree_01", "MAT_leaf", (), 0.20),
    ("SKY", "SKY", "MAT_sky", (), 0.15),
    ("PEOPLE_01", "PEOPLE_01", "MAT_people", (), 0.05),
    ("WATER_pool", "WATER_pool", "MAT_water", (), 0.10),
)


def fake_scene(camera_name="CAM_main") -> FakeRuntime:
    """One V-Ray style camera and six geometry nodes (ARCH_walls, GLAZING_south, VEG_tree_01, SKY,
    PEOPLE_01, WATER_pool), mm scene units, 3840x2160, one frame."""
    rt = FakeRuntime()
    rt.add_camera(
        FakeCamera(camera_name, SCENE_CAMERA_ROWS, focal_length=24.0, f_number=8.0, shutter_speed=125)
    )
    rt.objects = [
        make_node(n, layer, mat, tags, x=100.0 * i) for i, (n, layer, mat, tags, _) in enumerate(SCENE_NODES)
    ]
    rt.lights = [FakeLight("Sun01", multiplier=1.5)]
    return rt


def scene_pixel_shares() -> dict[str, float]:
    """Render-side coverage the fake scene would report, per node name."""
    return {n: share for n, _, _, _, share in SCENE_NODES}
