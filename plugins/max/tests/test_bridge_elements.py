"""bridge.elements on the fake render-element manager: the default set, the opt-ins, the report."""

from __future__ import annotations

import copy

import pytest

import plate_spec
from plate_max.bridge import elements

DEFAULT_LAYERS = [
    "RGBA",
    "VRayZDepth",
    "VRayNormals",
    "CryptoObject",
    "CryptoMaterial",
    "VRayWireColor",
    "VRayDiffuseFilter",
]


def _by_layer(mgr):
    return {e.elementname: e for e in mgr.elements}


def test_the_default_set_lands_in_the_manager(scene):
    layers = elements.ensure_default_set()
    assert isinstance(layers, list) and layers == DEFAULT_LAYERS  # 7 layers: beauty + 6 elements
    mgr = scene.element_manager
    assert mgr.NumRenderElements() == 6 and mgr.active is True
    assert [e.classname for e in mgr.elements] == [
        "VRayZDepth",
        "VRayNormals",
        "VRayCryptomatte",
        "VRayCryptomatte",
        "VRayWireColor",
        "VRayDiffuseFilter",
    ]
    assert layers.created == DEFAULT_LAYERS[1:] and layers.ok and layers.unverified == []
    el = _by_layer(mgr)
    z = el["VRayZDepth"]
    assert (z.zdepth_clamp, z.zdepth_min, z.zdepth_max, z.filterOn) == (False, 0.0, 1.0e9, False)
    assert (el["CryptoObject"].idType, el["CryptoMaterial"].idType) == (0, 2)
    assert el["CryptoObject"].num_levels == 6 and el["CryptoMaterial"].denoise is False
    assert all(e.enabled for e in mgr.elements)


def test_a_second_call_reuses_and_adds_nothing(scene):
    elements.ensure_default_set()
    again = elements.ensure_default_set(scene.element_manager)
    assert again == DEFAULT_LAYERS and again.created == [] and again.reused == DEFAULT_LAYERS[1:]
    assert scene.element_manager.NumRenderElements() == 6


def test_existing_elements_keep_their_names_and_get_the_data_rules(scene):
    mgr = scene.element_manager
    mgr.AddRenderElement(scene.VRayZDepth(elementname="Z_artist", zdepth_clamp=True, zdepth_max=50.0))
    mgr.AddRenderElement(scene.VRayCryptomatte(elementname="crypto_obj", idType=0, denoise=True))
    layers = elements.ensure_default_set(mgr)
    assert layers[:4] == ["RGBA", "Z_artist", "VRayNormals", "crypto_obj"]
    assert mgr.NumRenderElements() == 6
    changed = {(c["layer"], c["property"]): (c["from"], c["to"]) for c in layers.changed}
    assert changed[("Z_artist", "zdepth_clamp")] == (True, False)
    assert changed[("Z_artist", "zdepth_max")] == (50.0, 1.0e9)
    assert changed[("crypto_obj", "denoise")] == (True, False)
    assert _by_layer(mgr)["Z_artist"].zdepth_clamp is False


def test_a_disabled_element_is_switched_back_on(scene):
    mgr = scene.element_manager
    mgr.AddRenderElement(scene.VRayNormals(enabled=False))
    elements.ensure_default_set(mgr)
    assert _by_layer(mgr)["VRayNormals"].enabled is True


def test_a_missing_class_is_reported_not_raised(scene):
    scene.hide_class("VRayWireColor")
    layers = elements.ensure_default_set()
    assert "VRayWireColor" not in layers and layers.missing == ["VRayWireColor"]
    assert not layers.ok
    assert scene.element_manager.NumRenderElements() == 5
    assert layers.as_dict()["missing"] == ["VRayWireColor"]


def test_a_property_the_class_lacks_is_unverified_not_fatal(scene):
    del scene.element_props["VRayZDepth"]["zdepth_clamp"]
    layers = elements.ensure_default_set()
    assert "VRayZDepth" in layers
    assert any("zdepth_clamp" in u for u in layers.unverified)


def test_opt_ins_world_position_light_selects_and_unknown_names(scene):
    scene.lights.append(type(scene.lights[0])("Lamp:Desk 01"))
    layers = elements.ensure_optin(None, ["VRaySamplerInfo", "VRayLightSelect", "VRayVelocity", "VRayBogus"])
    assert layers == ["VRayWorldPosition", "LS_Sun01", "LS_Lamp_Desk_01", "VRayVelocity"]
    assert layers.unknown == ["VRayBogus"] and not layers.ok
    el = _by_layer(scene.element_manager)
    assert el["LS_Sun01"].lights == [scene.lights[0]]
    names = [e.get("light_name") for e in layers.entries if e["element"] == "light_select"]
    assert names == ["Sun01", "Lamp:Desk 01"]
    assert all(":" not in layer and "." not in layer for layer in layers)
    again = elements.ensure_optin(None, ["VRayLightSelect"])
    assert again.created == [] and len(again) == 2


def test_light_names_that_sanitise_alike_stay_distinct(scene):
    light = type(scene.lights[0])
    scene.lights[:] = [light("Key A"), light("Key:A")]
    layers = elements.ensure_optin(None, ["VRayLightSelect"])
    assert list(layers) == ["LS_Key_A", "LS_Key_A_2"]


def test_manifest_elements_validate_in_a_manifest(scene):
    default = elements.ensure_default_set()
    extra = elements.ensure_optin(None, ["VRaySamplerInfo", "VRayLightSelect", "VRayReflection"])
    entries = elements.manifest_elements(default, extra, beauty_denoised=True)
    kinds = [e["element"] for e in entries]
    assert kinds[:7] == list(("beauty",) + elements.DEFAULT_ELEMENTS)
    z = entries[1]
    assert z["dtype"] == "float" and z["clamped"] is False and z["filtered"] is False
    crypto = entries[3]
    assert (
        crypto["channels"][:2] == ["CryptoObject00.R", "CryptoObject00.G"] and len(crypto["channels"]) == 12
    )
    assert entries[0]["denoised"] is True and not any(e["denoised"] for e in entries[1:])
    light = next(e for e in entries if e["element"] == "light_select")
    assert light["light_name"] == "Sun01"
    manifest = copy.deepcopy(plate_spec.load_example("tierA_still_clubhouse"))
    manifest["files"]["beauty_exr"]["elements"] = entries
    errors = plate_spec.validate_manifest(manifest)
    assert errors == []


def test_header_dtypes_and_channels_override_the_guesses(scene):
    default = elements.ensure_default_set()
    entries = elements.manifest_elements(
        default, dtypes={"RGBA": "float", "VRayNormals": "float"}, channels={"VRayZDepth": ["R"]}
    )
    assert entries[0]["dtype"] == "float" and entries[2]["dtype"] == "float"
    assert entries[1]["channels"] == ["R"]


@pytest.mark.parametrize("name", elements.OPTIN_CLASSES)
def test_every_opt_in_class_is_known_to_the_fake(scene, name):
    layers = elements.ensure_optin(None, [name])
    assert layers.ok and len(layers) >= 1
