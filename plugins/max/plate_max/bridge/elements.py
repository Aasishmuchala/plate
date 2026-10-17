"""The V-Ray render elements a packet needs (PACKET.md 4.2), added or fixed, reported, never raised.

ensure_default_set(mgr) makes the current render-element manager hold the default set every tier A
packet carries (Z depth unclamped and unfiltered, normals, Cryptomatte by object and by material at
six ranks, wire colour, diffuse filter); ensure_optin(mgr, names) adds the per-shot extras (world
position, one light select per light, velocity, raw lighting, raw GI, reflection, refraction,
specular, self-illumination). Every class is looked up with getattr(rt, name, None): an uninstalled
or renamed class is reported missing, not raised, and the returned layer list holds only what the
manager really has, so the manifest's elements[].layer is truthful. manifest_elements() turns the
report into the schema's elements[] entries.

Every class, property and enum value here comes from the V-Ray docs and scout 01 and is unverified
on a Max box (TODO-verify, ONBOX.md steps 1-3). An element that already exists is kept (its own
elementname is its layer) but its data-pass properties are enforced (Z unclamped, Cryptomatte not
denoised, ...), because the manifest states them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..rt import runtime
from .scene import class_name, get_prop, has_prop, rt_attr, set_prop

BEAUTY_LAYER = "RGBA"  # V-Ray's raw EXR keeps the beauty in the default R,G,B,A channels (TODO-verify)

ELEMENT_NAME = ("elementname", "elementName")  # TODO-verify
ENABLED = ("enabled",)  # TODO-verify
FILTER = ("filterOn", "filtering")  # TODO-verify: "Filtering" checkbox of every V-Ray element
DENOISE = ("denoise", "denoiser_enabled")  # TODO-verify: per-element VFB denoise switch
ZDEPTH_CLAMP = ("zdepth_clamp", "clamp_zdepth")  # TODO-verify
ZDEPTH_MIN = ("zdepth_min",)  # TODO-verify
ZDEPTH_MAX = ("zdepth_max",)  # TODO-verify
ZDEPTH_CLIP = (
    "zdepth_use_camera_clip_boudaries",
    "zdepth_use_camera_clip_boundaries",
)  # TODO-verify (V-Ray's spelling)
ZDEPTH_UNCLAMPED_MAX = 1.0e9  # scene units: "large", far beyond any scene (TODO-verify the spinner range)
CRYPTO_ID_TYPE = ("idType", "id_type")  # TODO-verify
CRYPTO_ID_OBJECT = 0  # TODO-verify: node name
CRYPTO_ID_MATERIAL = 2  # TODO-verify: node material name (the enum order is the on-box question)
CRYPTO_LEVELS = ("num_levels", "numLevels")  # TODO-verify
CRYPTO_RANK_LEVELS = 6
SAMPLER_TYPE = ("type", "sampler_type")  # TODO-verify: 0 = point
SAMPLER_COORDS = ("coord_system", "coordsys")  # TODO-verify: 0 = world
LIGHT_SELECT_LIGHTS = ("lights", "lightList")  # TODO-verify
LIGHT_SELECT_MODE = ("mode", "lightselect_mode")  # TODO-verify: read, not set, until the enum is known

# dtype the raw EXR should carry per element kind (decision: half beauty, float data). The real
# dtype is whatever V-Ray wrote: callers override from the EXR header's channel list (T19).
DTYPES = {
    "beauty": "half",
    "zdepth": "float",
    "cryptomatte_object": "float",
    "cryptomatte_material": "float",
    "world_position": "float",
}
ZDEPTH_KIND = "planar"  # TODO-verify: ONBOX.md step 2 (a plane at known depth, off-axis pixel)
NORMALS_SPACE = "camera"  # TODO-verify: ONBOX.md step 3 (+X right, +Y up, +Z toward the viewer)
NORMALS_ENCODING = "signed"  # TODO-verify


@dataclass(frozen=True)
class Rule:
    """Set the first existing property in names to value; required rules that find no property are
    reported as unverified (the manifest would state something the scene was not told)."""

    names: tuple[str, ...]
    value: Any
    required: bool = True


@dataclass(frozen=True)
class ElementSpec:
    element: str  # the schema's element kind
    cls: str  # the V-Ray class, rt.<cls>
    layer: str  # elementname given to a new element (its EXR layer name)
    rules: tuple[Rule, ...] = ()
    match: tuple[Rule, ...] = ()  # what an existing element must read to count as this one
    channels: tuple[str, ...] = ("R", "G", "B")
    filtered: bool = True  # expected Filtering state when the element does not say


_NO_DENOISE = Rule(DENOISE, False, required=False)


def _crypto(element: str, layer: str, id_type: int) -> ElementSpec:
    return ElementSpec(
        element,
        "VRayCryptomatte",
        layer,
        rules=(
            Rule(CRYPTO_ID_TYPE, id_type),
            Rule(CRYPTO_LEVELS, CRYPTO_RANK_LEVELS),
            Rule(DENOISE, False),
        ),
        match=(Rule(CRYPTO_ID_TYPE, id_type),),
    )


DEFAULT_SET: tuple[ElementSpec, ...] = (
    ElementSpec(
        "zdepth",
        "VRayZDepth",
        "VRayZDepth",
        rules=(
            Rule(ZDEPTH_CLAMP, False),
            Rule(ZDEPTH_MIN, 0.0),
            Rule(ZDEPTH_MAX, ZDEPTH_UNCLAMPED_MAX),
            Rule(ZDEPTH_CLIP, False, required=False),
            Rule(FILTER, False),
            _NO_DENOISE,
        ),
        channels=("Z",),  # TODO-verify the channel name V-Ray writes for VRayZDepth
        filtered=False,
    ),
    ElementSpec("normals", "VRayNormals", "VRayNormals", rules=(_NO_DENOISE,), channels=("X", "Y", "Z")),
    _crypto("cryptomatte_object", "CryptoObject", CRYPTO_ID_OBJECT),
    _crypto("cryptomatte_material", "CryptoMaterial", CRYPTO_ID_MATERIAL),
    ElementSpec("wire_colour", "VRayWireColor", "VRayWireColor", rules=(_NO_DENOISE,)),
    ElementSpec("diffuse_filter", "VRayDiffuseFilter", "VRayDiffuseFilter", rules=(_NO_DENOISE,)),
)
DEFAULT_ELEMENTS = tuple(spec.element for spec in DEFAULT_SET)

OPTIN: dict[str, ElementSpec] = {
    spec.cls: spec
    for spec in (
        ElementSpec(
            "world_position",
            "VRaySamplerInfo",
            "VRayWorldPosition",
            rules=(
                Rule(SAMPLER_TYPE, 0),
                Rule(SAMPLER_COORDS, 0),
                Rule(FILTER, False),
                Rule(DENOISE, False, False),
            ),
            channels=("X", "Y", "Z"),
            filtered=False,
        ),
        ElementSpec(
            "velocity",
            "VRayVelocity",
            "VRayVelocity",
            rules=(Rule(FILTER, False), _NO_DENOISE),
            filtered=False,
        ),
        ElementSpec("raw_lighting", "VRayRawLighting", "VRayRawLighting"),
        ElementSpec("raw_gi", "VRayRawGlobalIllumination", "VRayRawGlobalIllumination"),
        ElementSpec("reflection", "VRayReflection", "VRayReflection"),
        ElementSpec("refraction", "VRayRefraction", "VRayRefraction"),
        ElementSpec("specular", "VRaySpecular", "VRaySpecular"),
        ElementSpec("self_illumination", "VRaySelfIllumination", "VRaySelfIllumination"),
    )
}
LIGHT_SELECT_CLASS = "VRayLightSelect"
OPTIN_CLASSES = (*OPTIN, LIGHT_SELECT_CLASS)


class LayerList(list):
    """The EXR layer names the manager now yields (a list[str]), with the report attached:
    created / reused layers, missing classes, rules changed or unverified, and entries (one dict per
    layer: element, layer, cls, filtered, light_name?) for manifest_elements()."""

    def __init__(self) -> None:
        super().__init__()
        self.created: list[str] = []
        self.reused: list[str] = []
        self.missing: list[str] = []
        self.changed: list[dict[str, Any]] = []
        self.unverified: list[str] = []
        self.unknown: list[str] = []
        self.entries: list[dict[str, Any]] = []

    @property
    def ok(self) -> bool:
        """True when nothing asked for is missing or unknown."""
        return not self.missing and not self.unknown

    def as_dict(self) -> dict[str, Any]:
        return {
            "layers": list(self),
            "created": list(self.created),
            "reused": list(self.reused),
            "missing": list(self.missing),
            "unknown": list(self.unknown),
            "changed": [dict(c) for c in self.changed],
            "unverified": list(self.unverified),
        }


# --------------------------------------------------------------------------- manager access


def current_manager() -> Any:
    """rt.maxOps.GetCurRenderElementMgr() (the production render's element list)."""
    return runtime().maxOps.GetCurRenderElementMgr()


def list_elements(mgr: Any) -> list[Any]:
    try:
        count = int(mgr.NumRenderElements())
    except Exception:
        return []
    out = []
    for index in range(count):  # 0-based, as GetRenderElement is in Max
        try:
            out.append(mgr.GetRenderElement(index))
        except Exception:
            continue
    return out


def layer_of(element: Any, default: str = "") -> str:
    name = get_prop(element, ELEMENT_NAME)
    return str(name) if name else default


def _matches(element: Any, spec: ElementSpec) -> bool:
    if class_name(element) != spec.cls:
        return False
    for rule in spec.match:
        value = get_prop(element, rule.names)
        if value is None:
            return False
        try:
            if int(value) != int(rule.value):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _apply_rules(element: Any, spec: ElementSpec, layer: str, report: LayerList, new: bool) -> None:
    for rule in spec.rules:
        name = next((n for n in rule.names if has_prop(element, n)), None)
        if name is None:
            if rule.required:
                report.unverified.append(f"{layer}: no {'/'.join(rule.names)} property")
            continue
        try:
            before = getattr(element, name)
        except Exception:
            before = None
        if before == rule.value:
            continue
        if set_prop(element, (name,), rule.value) is None:
            report.unverified.append(f"{layer}: {name} could not be set to {rule.value!r}")
        elif not new:
            report.changed.append({"layer": layer, "property": name, "from": before, "to": rule.value})


def _enable(element: Any) -> None:
    if get_prop(element, ENABLED) is False:
        set_prop(element, ENABLED, True)


def _entry(element: Any, spec: ElementSpec, layer: str, **extra: Any) -> dict[str, Any]:
    filtered = get_prop(element, FILTER)
    return {
        "element": spec.element,
        "layer": layer,
        "cls": spec.cls,
        "channels": list(spec.channels),
        "filtered": spec.filtered if filtered is None else bool(filtered),
        **extra,
    }


def _ensure(
    mgr: Any,
    spec: ElementSpec,
    report: LayerList,
    layer: str | None = None,
    extra_rules: tuple[Rule, ...] = (),
) -> tuple[Any, str | None]:
    """(element, layer) for spec: an existing element (rules enforced) or a new one; (None, None)
    when the class is missing or the manager refuses it (both reported)."""
    target_layer = layer or spec.layer
    existing = [e for e in list_elements(mgr) if _matches(e, spec)]
    if layer is not None:
        existing = [e for e in existing if layer_of(e) == layer]
    if existing:
        element = existing[0]
        actual = layer_of(element, target_layer)
        _apply_rules(element, spec, actual, report, new=False)
        for rule in extra_rules:
            _apply_extra(element, rule, actual, report)
        _enable(element)
        report.reused.append(actual)
        return element, actual
    factory = rt_attr(spec.cls)
    if not callable(factory):
        report.missing.append(spec.cls if layer is None else f"{spec.cls} ({layer})")
        return None, None
    try:
        element = factory()
    except Exception:
        report.missing.append(spec.cls)
        return None, None
    set_prop(element, ELEMENT_NAME, target_layer)
    _apply_rules(element, spec, target_layer, report, new=True)
    for rule in extra_rules:
        _apply_extra(element, rule, target_layer, report)
    _enable(element)
    try:
        added = mgr.AddRenderElement(element)
    except Exception:
        added = False
    if added is False:
        report.missing.append(f"{spec.cls}: the element manager refused it")
        return None, None
    actual = layer_of(element, target_layer)
    report.created.append(actual)
    return element, actual


def _apply_extra(element: Any, rule: Rule, layer: str, report: LayerList) -> None:
    if set_prop(element, rule.names, rule.value) is None and rule.required:
        report.unverified.append(f"{layer}: no {'/'.join(rule.names)} property")


def _activate(mgr: Any) -> None:
    try:
        mgr.SetElementsActive(True)
    except Exception:
        pass


# --------------------------------------------------------------------------- the two entry points


def ensure_default_set(mgr: Any = None) -> LayerList:
    """Make mgr (default: the current manager) hold the PACKET.md 4.2 default set. Returns the EXR
    layers in manifest order: the beauty (RGBA) first, then one layer per default element that the
    manager really holds; .missing names every class that could not be added."""
    mgr = current_manager() if mgr is None else mgr
    report = LayerList()
    report.append(BEAUTY_LAYER)
    report.entries.append(
        {
            "element": "beauty",
            "layer": BEAUTY_LAYER,
            "cls": None,
            "channels": ["R", "G", "B", "A"],
            "filtered": True,
        }
    )
    for spec in DEFAULT_SET:
        element, layer = _ensure(mgr, spec, report)
        if element is not None:
            report.append(layer)
            report.entries.append(_entry(element, spec, layer))
    _activate(mgr)
    return report


def _light_layer(light_name: str) -> str:
    """LS_<light name> with everything but letters, digits, '_' and '-' replaced (EXR channel names
    use '.' as the separator, and ':' cannot reach a file name)."""
    return "LS_" + (re.sub(r"[^A-Za-z0-9_-]+", "_", light_name).strip("_") or "light")


def ensure_optin(mgr: Any = None, names: Any = (), lights: Any = None) -> LayerList:
    """Add the opt-in elements named by V-Ray class (OPTIN_CLASSES). VRayLightSelect gets one element
    per light (lights default: rt.lights), layer LS_<light>, light_name recorded for the manifest.
    Unknown names land in .unknown and missing classes in .missing; nothing raises."""
    mgr = current_manager() if mgr is None else mgr
    report = LayerList()
    for name in names:
        if name == LIGHT_SELECT_CLASS:
            _ensure_light_selects(mgr, report, lights)
            continue
        spec = OPTIN.get(name)
        if spec is None:
            report.unknown.append(str(name))
            continue
        element, layer = _ensure(mgr, spec, report)
        if element is not None:
            report.append(layer)
            report.entries.append(_entry(element, spec, layer))
    _activate(mgr)
    return report


def _ensure_light_selects(mgr: Any, report: LayerList, lights: Any) -> None:
    spec = ElementSpec("light_select", LIGHT_SELECT_CLASS, "LS_light", rules=(_NO_DENOISE,))
    if lights is None:
        lights = list(rt_attr("lights") or [])
    used: set[str] = set()
    for light in lights:
        light_name = str(getattr(light, "name", ""))
        layer = _light_layer(light_name)
        if layer in used:  # two lights whose names sanitise alike: keep both layers distinct
            n = 2
            while f"{layer}_{n}" in used:
                n += 1
            layer = f"{layer}_{n}"
        used.add(layer)
        element, actual = _ensure(
            mgr, spec, report, layer=layer, extra_rules=(Rule(LIGHT_SELECT_LIGHTS, [light]),)
        )
        if element is not None:
            report.append(actual)
            report.entries.append(
                _entry(
                    element, spec, actual, light_name=light_name, mode=get_prop(element, LIGHT_SELECT_MODE)
                )
            )


# --------------------------------------------------------------------------- manifest entries


def manifest_elements(
    *reports: LayerList,
    beauty_denoised: bool = False,
    dtypes: dict[str, str] | None = None,
    channels: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """The schema's files.beauty_exr.elements[] for the layers in reports (ensure_default_set first,
    then any ensure_optin). dtypes / channels override per layer name with what the EXR header
    really says (the plugin's guesses are DTYPES and the spec channel names). Only the beauty can be
    denoised; data passes never are (PACKET.md 4.2)."""
    dtypes = dtypes or {}
    channels = channels or {}
    out: list[dict[str, Any]] = []
    for report in reports:
        for entry in report.entries:
            kind, layer = entry["element"], entry["layer"]
            item: dict[str, Any] = {
                "element": kind,
                "layer": layer,
                "channels": list(channels.get(layer) or entry["channels"]),
                "dtype": dtypes.get(layer, DTYPES.get(kind, "half")),
                "filtered": bool(entry["filtered"]),
                "denoised": bool(beauty_denoised) if kind == "beauty" else False,
            }
            if kind == "zdepth":
                item.update(
                    {
                        "filtered": False,
                        "depth_kind": ZDEPTH_KIND,
                        "normalized": False,
                        "clamped": False,
                        "background_value": None,
                    }
                )
            elif kind == "normals":
                item.update({"space": NORMALS_SPACE, "encoding": NORMALS_ENCODING})
            elif kind in ("cryptomatte_object", "cryptomatte_material"):
                item["channels"] = list(
                    channels.get(layer)
                    or [f"{layer}{rank:02d}.{c}" for rank in range(CRYPTO_RANK_LEVELS // 2) for c in "RGBA"]
                )
                item["rank_levels"] = CRYPTO_RANK_LEVELS
            elif kind == "world_position":
                item["space"] = "world"
            elif kind == "light_select":
                item["light_name"] = entry.get("light_name") or layer
            out.append(item)
    return out
