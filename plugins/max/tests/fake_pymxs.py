"""A stand-in for pymxs.runtime so plate_max runs under pytest on macOS and Windows.

It is a small stateful 3ds Max: nodes with a layer, a material, a transform, a wire colour and
user properties; cameras with V-Ray Physical Camera property names; lights; the render settings, the
animation range and the time slider; a render-element manager that holds element objects; and the
side-effecting calls (render, vfbControl, saveMaxFile, vrayExportVRScene, execute) that record their
arguments and write small stand-in files, so a test can check what was asked and what landed on
disk. Scenes load from JSON descriptions in tests/scenes (load_scene), or from fake_scene() below.
No file in plate_max imports this module.

Every property name here is a HYPOTHESIS copied from the MaxDirector and MaxGaffer bridges (scout 01)
and the V-Ray docs; none is verified on a Max box (plugins/max/ONBOX.md, T9). Where the real name is
unknown the fake offers the candidates as aliases of one stored value (focal_length and
focal_length_mm, ISO and iso, ...), and FakeNode(unreadable=[...]) makes a property raise
AttributeError the way pymxs does for an unknown one, so candidate-name fallbacks can be tested.
Rows of a transform are Point3-like (x, y, z, and also indexable), exactly as a Matrix3's row1..row4.
"""

from __future__ import annotations

import json
import math
import struct
import zlib
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

SCENES_DIR = Path(__file__).resolve().parent / "scenes"

IDENTITY_ROWS = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0, 0.0))


# --------------------------------------------------------------------------- value types


class FakePoint3(NamedTuple):
    x: float
    y: float
    z: float


class FakeColor(NamedTuple):
    """pymxs Color: channels 0..255."""

    r: float
    g: float
    b: float


class FakeName(str):
    """rt.Name("x"): a MAXScript #x name. Equal to the plain string, repr like MAXScript."""

    def __repr__(self) -> str:
        return f"#{str(self)}"


class FakeMatrix3:
    def __init__(self, rows=IDENTITY_ROWS):
        self.row1, self.row2, self.row3, self.row4 = (FakePoint3(*(float(v) for v in r)) for r in rows)

    @property
    def rows(self):
        return (self.row1, self.row2, self.row3, self.row4)


class FakeLayer:
    def __init__(self, name: str, ishidden: bool = False):
        self.name = name
        self.ishidden = ishidden


class FakeMaterial:
    def __init__(self, name: str, classname: str = "VRayMtl"):
        self.name = name
        self.classname = classname


class FakeBaseObject:
    def __init__(self, classname: str):
        self.classname = classname


class FakeRenderer:
    """rt.renderers.current: classOf() gives the class name, e.g. V_Ray_7_hotfix_3."""

    def __init__(self, classname: str = "V_Ray_7"):
        self.classname = classname
        self.name = classname

    def __str__(self) -> str:
        return self.classname


class FakeBitmap:
    def __init__(self, width: int, height: int, path: str | None = None):
        self.width, self.height, self.filename = width, height, path


# --------------------------------------------------------------------------- nodes


class FakeNode:
    """A scene node. properties is a bag of extra readable attributes (node.<name> falls back to it).
    unreadable names raise AttributeError on read, like a pymxs property the object does not have."""

    super_class = "GeometryClass"
    _unreadable: frozenset = frozenset()

    def __init__(
        self,
        name: str,
        layer: Any = None,
        material: Any = None,
        user_props: dict | None = None,
        objecttransform: FakeMatrix3 | None = None,
        *,
        transform: FakeMatrix3 | None = None,
        wirecolor: tuple | None = None,
        isHidden: bool = False,  # noqa: N803 (pymxs spelling)
        children: list | None = None,
        base_class: str = "Box",
        properties: dict | None = None,
        unreadable: Any = (),
    ):
        self.name = name
        self.layer = layer
        self.material = material
        self.user_props = dict(user_props or {})
        self.objecttransform = objecttransform if objecttransform is not None else FakeMatrix3()
        self.transform = transform if transform is not None else self.objecttransform
        self.wirecolor = FakeColor(*(wirecolor or (134, 88, 199)))
        self.isHidden = isHidden
        self.children = list(children or [])
        self.parent = None
        self.base_class = base_class
        self.properties = dict(properties or {})
        self._unreadable = frozenset(unreadable)

    def __getattribute__(self, name: str) -> Any:
        if not name.startswith("_") and name in object.__getattribute__(self, "_unreadable"):
            raise AttributeError(f"unknown property {name!r}")
        return object.__getattribute__(self, name)

    def __getattr__(self, name: str) -> Any:  # only reached when normal lookup fails
        props = self.__dict__.get("properties")
        if props is not None and name in props:
            return props[name]
        raise AttributeError(name)

    @property
    def baseObject(self) -> FakeBaseObject:  # noqa: N802
        return FakeBaseObject(self.base_class)

    baseobject = baseObject


def _alias(real: str) -> property:
    """A second name for one stored value (a property-name candidate)."""

    def getter(self):
        return getattr(self, real)

    def setter(self, value):
        setattr(self, real, value)

    return property(getter, setter)


class FakeCamera(FakeNode):
    """VRayPhysicalCamera property names (focal_length, film_width, ISO, shutter_speed, ...) plus the
    candidate aliases focal_length_mm, iso, white_balance_temperature, lens_shift_x, lens_shift_y."""

    super_class = "camera"

    focal_length_mm = _alias("focal_length")
    iso = _alias("ISO")
    white_balance_temperature = _alias("white_balance")
    lens_shift_x = _alias("horizontal_shift")
    lens_shift_y = _alias("vertical_shift")

    def __init__(self, name="CAM_main", rows=IDENTITY_ROWS, **overrides: Any):
        node_kw = {
            k: overrides.pop(k)
            for k in ("layer", "wirecolor", "isHidden", "children", "properties", "unreadable", "user_props")
            if k in overrides
        }
        super().__init__(name, objecttransform=FakeMatrix3(rows), base_class="VRayPhysicalCamera", **node_kw)
        # the pose V-Ray renders from is objecttransform; transform (the pivot) is a decoy that a
        # wrong reader would pick up
        self.transform = FakeMatrix3(tuple(rows[:3]) + ((rows[3][0] + 500.0, rows[3][1], rows[3][2]),))
        self.targeted = False
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
        stored = self.__dict__  # not getattr: an unreadable property must not stop the constructor
        if "fov" not in overrides and stored.get("focal_length"):
            stored["fov"] = math.degrees(
                2 * math.atan(stored["film_width"] / (2 * stored["focal_length"] * stored["zoom_factor"]))
            )


class FakeLight(FakeNode):
    super_class = "light"

    def __init__(self, name, on=True, multiplier=1.0, objecttransform=None, **kw: Any):
        kw.setdefault("base_class", "VRayLight")
        super().__init__(name, objecttransform=objecttransform, **kw)
        self.on = on
        self.multiplier = multiplier


@lru_cache(maxsize=None)
def shared_material(name):
    """One object per material name, as in a real scene (two nodes using MAT_x share one material)."""
    return FakeMaterial(name)


def make_node(name, layer, material, tags=(), x=0.0, **kw: Any):
    props = {"plate_tags": ",".join(tags)} if tags else {}
    mat = material if material is None or not isinstance(material, str) else shared_material(material)
    return FakeNode(
        name,
        layer=FakeLayer(layer) if layer is not None else None,
        material=mat,
        user_props=props,
        objecttransform=FakeMatrix3(((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (x, 0.0, 0.0))),
        **kw,
    )


# --------------------------------------------------------------------------- render elements

# Properties each element class offers (name -> default). HYPOTHESIS: names from the T9 spec and the
# V-Ray docs; extend a scene's with FakeRuntime.add_element_props(). Every class also has
# elementname, enabled and filterOn.
ELEMENT_PROPS: dict[str, dict[str, Any]] = {
    "VRayZDepth": {"zdepth_min": 0.0, "zdepth_max": 1000.0, "zdepth_clamp": True},
    "VRayNormals": {},
    "VRayCryptomatte": {"idType": 0, "num_levels": 6, "denoise": True},
    "VRayWireColor": {},
    "VRayDiffuseFilter": {},
    "VRaySamplerInfo": {"type": 0},
    "VRayLightSelect": {"mode": 0, "lights": None},
    "VRayVelocity": {},
    "VRayRawLighting": {},
    "VRayRawGlobalIllumination": {},
    "VRayReflection": {},
    "VRayRefraction": {},
    "VRaySpecular": {},
    "VRaySelfIllumination": {},
}


class FakeRenderElement:
    def __init__(self, classname: str, known: dict[str, Any], **props: Any):
        self.classname = classname
        self.elementname = classname
        self.enabled = True
        self.filterOn = True
        for key, default in known.items():
            setattr(self, key, default)
        for key, value in props.items():
            setattr(self, key, value)


class FakeREMgr:
    """rt.maxOps.GetCurRenderElementMgr(): the list of elements the next render writes."""

    def __init__(self):
        self.elements: list[FakeRenderElement] = []
        self.active = True
        self.filenames: dict[int, str] = {}

    def NumRenderElements(self) -> int:  # noqa: N802
        return len(self.elements)

    def GetRenderElement(self, index: int) -> FakeRenderElement:  # noqa: N802 (0-based, as in Max)
        if not 0 <= index < len(self.elements):
            raise IndexError(f"render element index {index} out of range")
        return self.elements[index]

    def AddRenderElement(self, element: FakeRenderElement) -> bool:  # noqa: N802
        self.elements.append(element)
        return True

    def RemoveRenderElement(self, element: FakeRenderElement) -> bool:  # noqa: N802
        if element in self.elements:
            self.elements.remove(element)
            return True
        return False

    def RemoveAllRenderElements(self) -> None:  # noqa: N802
        self.elements.clear()
        self.filenames.clear()

    def SetElementsActive(self, on: bool) -> None:  # noqa: N802
        self.active = bool(on)

    def GetElementsActive(self) -> bool:  # noqa: N802
        return self.active

    def SetRenderElementFilename(self, index: int, path: str) -> None:  # noqa: N802
        self.filenames[index] = str(path)


class FakeCollection(dict):
    """A pymxs-style collection (rt.cameras): iterating gives the nodes, not the keys; also indexable
    by name."""

    def __iter__(self):
        return iter(list(self.values()))

    def __contains__(self, item):
        return item in self.keys() or any(item is v for v in self.values())


# --------------------------------------------------------------------------- stand-in files


def tiny_png(width: int = 2, height: int = 2, rgb: tuple[int, int, int] = (200, 120, 40)) -> bytes:
    """A valid 8-bit RGB PNG built with zlib and struct (the fake render writes this; no Pillow)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _write(path: Any, data: bytes) -> None:
    target = Path(str(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


# --------------------------------------------------------------------------- the runtime


class FakeSysInfo:
    """rt.sysInfo. getMAXMemoryInfo() is a HYPOTHESIS for the RSS read (returns byte counts)."""

    def __init__(self):
        self.username = "artist"
        self.computername = "FAKE-BOX"
        self.peak_working_set_bytes = 12 * 1024**3

    def getMAXMemoryInfo(self):  # noqa: N802
        return [self.peak_working_set_bytes // 2, self.peak_working_set_bytes]


class FakeMaxOps:
    def __init__(self, rt: FakeRuntime):
        self._rt = rt

    def GetCurRenderElementMgr(self) -> FakeREMgr:  # noqa: N802
        return self._rt.element_manager


class FakeRuntime:
    """Enough of pymxs.runtime for the digests, the packet writer and the render bridge."""

    GeometryClass = "GeometryClass"

    def __init__(self, renderer: str = "V_Ray_7"):
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
        self.cameras: FakeCollection = FakeCollection()
        self.environmentMap = None
        self.useEnvironmentMap = False
        self.renderers = SimpleNamespace(current=FakeRenderer(renderer))
        self.renderers.production = self.renderers.current
        self.active_camera_name: str | None = None
        self.sysInfo = FakeSysInfo()
        self.element_manager = FakeREMgr()
        self.maxOps = FakeMaxOps(self)
        self.element_props = {k: dict(v) for k, v in ELEMENT_PROPS.items()}
        self.missing_classes: set[str] = set()
        self.pixel_shares: dict[str, float] = {}
        # side-effect logs, one entry per call
        self.render_calls: list[dict[str, Any]] = []
        self.vfb_calls: list[tuple] = []
        self.save_calls: list[dict[str, Any]] = []
        self.vrscene_exports: list[dict[str, Any]] = []
        self.executed: list[str] = []
        self.execute_results: dict[str, Any] = {}

    # ----- scene building

    def add_camera(self, cam: FakeCamera) -> FakeCamera:
        self.cameras[cam.name] = cam
        return cam

    def add_node(self, node: FakeNode) -> FakeNode:
        self.objects.append(node)
        return node

    def add_element_props(self, classname: str, **props: Any) -> None:
        """Teach the fake more property names for one element class (T9's candidate names)."""
        self.element_props.setdefault(classname, {}).update(props)

    def hide_class(self, classname: str) -> None:
        """Make rt.<classname> missing, as an uninstalled V-Ray element class is."""
        self.missing_classes.add(classname)

    @classmethod
    def from_description(cls, desc: dict[str, Any]) -> FakeRuntime:
        rt = cls(desc.get("renderer", "V_Ray_7"))
        render = desc.get("render", {})
        rt.renderWidth = int(render.get("width", rt.renderWidth))
        rt.renderHeight = int(render.get("height", rt.renderHeight))
        rt.renderPixelAspect = float(render.get("pixel_aspect", 1.0))
        first, last = desc.get("frame_range", [0, 0])
        rt.animationRange = SimpleNamespace(
            start=SimpleNamespace(frame=first), end=SimpleNamespace(frame=last)
        )
        rt.currentTime = SimpleNamespace(frame=first)
        rt.frameRate = float(desc.get("fps", rt.frameRate))
        units = desc.get("units", {})
        rt.units = SimpleNamespace(
            SystemType=units.get("system", "millimeters"), SystemScale=float(units.get("scale", 1.0))
        )
        layers: dict[str, FakeLayer] = {}
        materials: dict[str, FakeMaterial] = {}

        def layer_of(name: str | None) -> FakeLayer | None:
            return None if name is None else layers.setdefault(name, FakeLayer(name))

        for spec in desc.get("cameras", []):
            cam = FakeCamera(
                spec["name"],
                spec.get("rows", IDENTITY_ROWS),
                layer=layer_of(spec.get("layer", "0")),
                isHidden=bool(spec.get("hidden", False)),
                **spec.get("props", {}),
            )
            rt.add_camera(cam)
            rt.objects.append(cam)
        for spec in desc.get("lights", []):
            light = FakeLight(
                spec["name"],
                multiplier=float(spec.get("multiplier", 1.0)),
                layer=layer_of(spec.get("layer", "0")),
            )
            rt.lights.append(light)
            rt.objects.append(light)
        for spec in desc.get("nodes", []):
            mat_name = spec.get("material")
            mat = materials.setdefault(mat_name, FakeMaterial(mat_name)) if mat_name else None
            node = FakeNode(
                spec["name"],
                layer=layer_of(spec.get("layer")),
                material=mat,
                user_props={"plate_tags": ",".join(spec["tags"])} if spec.get("tags") else {},
                objecttransform=FakeMatrix3(
                    ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (float(spec.get("x", 0.0)), 0.0, 0.0))
                ),
                wirecolor=spec.get("wirecolor"),
                isHidden=bool(spec.get("hidden", False)),
                base_class=spec.get("class", "Box"),
            )
            rt.objects.append(node)
            if "pixel_share" in spec:
                rt.pixel_shares[node.name] = float(spec["pixel_share"])
        rt.active_camera_name = desc.get("active_camera")
        return rt

    # ----- time

    @property
    def sliderTime(self):  # noqa: N802
        return self.currentTime

    @sliderTime.setter
    def sliderTime(self, value):  # noqa: N802
        frame = getattr(value, "frame", value)
        self.currentTime = SimpleNamespace(frame=frame)

    # ----- lookups

    def getNodeByName(self, name, exact=True):  # noqa: N802 (pymxs spelling)
        if name in self.cameras:
            return self.cameras[name]
        for node in list(self.objects) + list(self.lights):
            if node.name == name:
                return node
        return None

    def getActiveCamera(self):  # noqa: N802
        if self.active_camera_name and self.active_camera_name in self.cameras:
            return self.cameras[self.active_camera_name]
        return next(iter(self.cameras), None)

    def classOf(self, node):  # noqa: N802
        if node is None:
            raise TypeError("classOf(undefined)")
        for attr in ("base_class", "classname"):
            name = getattr(node, attr, None)
            if isinstance(name, str):
                return name
        return getattr(node, "name", "Box") if isinstance(node, SimpleNamespace) else "Box"

    def superClassOf(self, node):  # noqa: N802
        return getattr(node, "super_class", self.GeometryClass)

    def getHandleByAnim(self, obj):  # noqa: N802
        return id(obj)

    def getUserProp(self, node, key):  # noqa: N802
        return getattr(node, "user_props", {}).get(key)

    def Name(self, text):  # noqa: N802
        return FakeName(text)

    def isProperty(self, obj, name):  # noqa: N802
        try:
            getattr(obj, str(name).lstrip("#"))
        except Exception:
            return False
        return True

    def __getattr__(self, name: str) -> Any:
        """rt.VRayZDepth() and friends: a factory per render-element class, absent when hidden."""
        known = self.__dict__.get("element_props", {}).get(name)
        if known is not None and name not in self.__dict__.get("missing_classes", ()):
            return lambda **props: FakeRenderElement(name, known, **props)
        raise AttributeError(name)

    # ----- side effects

    def execute(self, script):
        self.executed.append(str(script))
        return self.execute_results.get(str(script))

    def render(self, *args, **kw):
        self.render_calls.append(dict(kw))
        width = int(kw.get("outputwidth") or self.renderWidth)
        height = int(kw.get("outputheight") or self.renderHeight)
        path = kw.get("outputfile")
        if path:
            _write(path, tiny_png())
        return FakeBitmap(width, height, str(path) if path else None)

    def vfbControl(self, *args):  # noqa: N802
        self.vfb_calls.append(args)
        if len(args) >= 2 and str(args[0]).lower() == "saveimage":
            _write(args[1], tiny_png())

    def saveMaxFile(self, path, **kw):  # noqa: N802
        self.save_calls.append({"path": str(path), **kw})
        _write(path, b"FAKE-MAX\n")
        if kw.get("useNewFile", True):
            target = Path(str(path))
            self.maxFilePath, self.maxFileName = str(target.parent), target.name
        return True

    def vrayExportVRScene(self, path, **kw):  # noqa: N802
        self.vrscene_exports.append({"path": str(path), **kw})
        _write(path, b"# fake vrscene\n")
        return True


# --------------------------------------------------------------------------- the seed scene

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


# --------------------------------------------------------------------------- JSON scenes


def scene_names() -> list[str]:
    return sorted(p.stem for p in SCENES_DIR.glob("*.json"))


def scene_description(name: str) -> dict[str, Any]:
    """The JSON description tests/scenes/<name>.json. "extends": "<other>" starts from that scene and
    replaces every top-level key the child sets (a child "cameras" list replaces the base's)."""
    desc = json.loads((SCENES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    base = desc.pop("extends", None)
    if base:
        return {**scene_description(base), **desc}
    return desc


def load_scene(name: str) -> FakeRuntime:
    """A FakeRuntime built from tests/scenes/<name>.json (not yet installed as the plate_max runtime)."""
    return FakeRuntime.from_description(scene_description(name))
