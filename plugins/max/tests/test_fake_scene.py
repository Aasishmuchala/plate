"""The stateful fake pymxs: scenes from JSON, nodes, cameras, the render-element manager and the
side-effecting calls (render, vfbControl, saveMaxFile, vrayExportVRScene, execute). These pin the
FAKE's behaviour so the tests built on it can trust it; none of it says anything about real Max."""

from __future__ import annotations

import struct
import zlib

import pytest
from fake_pymxs import (
    FakeCamera,
    FakeLight,
    FakeNode,
    FakeRuntime,
    fake_scene,
    load_scene,
    make_node,
    scene_description,
    scene_names,
)

from plate_max import digest, rt
from plate_max.policy import classify_object

LABELLED_PREFIXES = ("ARCH_", "GLZ_", "SIGN_", "VEG_", "SKY", "PEOPLE", "WATER", "HARD_")


def _png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    length, tag = struct.unpack(">I4s", data[8:16])
    assert tag == b"IHDR" and length == 13
    width, height = struct.unpack(">II", data[16:24])
    # every chunk CRC must hold: a hand-rolled PNG is easy to get wrong
    pos = 8
    while pos < len(data):
        (n,) = struct.unpack(">I", data[pos : pos + 4])
        body = data[pos + 4 : pos + 8 + n]
        (crc,) = struct.unpack(">I", data[pos + 8 + n : pos + 12 + n])
        assert zlib.crc32(body) & 0xFFFFFFFF == crc
        pos += 12 + n
    return width, height


# ---- JSON scenes


def test_the_three_scene_files_exist():
    assert scene_names() == ["clubhouse", "scaled_camera", "shifted_camera"]


def test_clubhouse_has_three_cameras_and_forty_nodes(clubhouse):
    assert sorted(c.name for c in clubhouse.cameras) == ["CAM_aerial", "CAM_entry", "CAM_front"]
    nodes = digest.scene_nodes()
    assert len(nodes) == 40
    labelled = [n for n in nodes if n["layer"] != "0"]
    assert len(labelled) == 37
    assert {p for p in LABELLED_PREFIXES if any(n["layer"].startswith(p) for n in labelled)} == set(
        LABELLED_PREFIXES
    )


def test_clubhouse_unlabelled_nodes_are_three_and_cover_four_percent(clubhouse):
    nodes = digest.scene_nodes()
    unlabelled = [n for n in nodes if classify_object(n["layer"], n["tags"])[1] == "unlabelled"]
    assert sorted(n["name"] for n in unlabelled) == ["Box042", "Sphere007", "Teapot01"]
    assert sum(clubhouse.pixel_shares[n["name"]] for n in unlabelled) == pytest.approx(0.04)
    assert sum(clubhouse.pixel_shares.values()) <= 1.0


def test_clubhouse_runtime_state(clubhouse):
    assert (clubhouse.renderWidth, clubhouse.renderHeight, clubhouse.renderPixelAspect) == (3840, 2160, 1.0)
    assert str(clubhouse.classOf(clubhouse.renderers.current)) == "V_Ray_7_hotfix_3"
    assert clubhouse.getActiveCamera().name == "CAM_front"
    assert clubhouse.frameRate == 25.0
    assert [light.name for light in clubhouse.lights] == ["Sun01", "Dome01"]
    # cameras and lights are scene objects too; scene_nodes() still keeps geometry only
    assert len(clubhouse.objects) == 40 + 3 + 2


@pytest.mark.parametrize("name", ["CAM_front", "CAM_aerial", "CAM_entry"])
def test_every_clubhouse_camera_is_a_proper_rotation(clubhouse, name):
    out = digest.camera_digest(name)
    assert digest.rotation_error(out["world_from_camera"]) is None
    assert out["warnings"] == []


def test_scaled_and_shifted_scenes_extend_the_clubhouse(install_scene):
    scaled = install_scene("scaled_camera")
    assert [c.name for c in scaled.cameras] == ["CAM_scaled"]
    assert len(digest.scene_nodes()) == 40
    assert any("not orthonormal" in w for w in digest.camera_digest("CAM_scaled")["warnings"])
    shifted = install_scene("shifted_camera")
    assert [c.name for c in shifted.cameras] == ["CAM_shifted"]
    assert digest.camera_digest("CAM_shifted")["lens_shift"]["horizontal"] == 0.12


def test_extends_replaces_keys_and_keeps_the_rest():
    desc = scene_description("shifted_camera")
    assert desc["renderer"] == "V_Ray_7_hotfix_3" and len(desc["nodes"]) == 40
    assert [c["name"] for c in desc["cameras"]] == ["CAM_shifted"]


def test_two_loads_do_not_share_materials(install_scene):
    a = install_scene("clubhouse")
    b = load_scene("clubhouse")
    mat_a = next(n.material for n in a.objects if getattr(n, "material", None) is not None)
    mat_b = next(n.material for n in b.objects if getattr(n, "material", None) is not None)
    assert mat_a is not mat_b and mat_a.name == mat_b.name


# ---- nodes and cameras


def test_node_carries_the_pymxs_node_state():
    node = FakeNode("Box001", properties={"custom": 7}, children=["x"], base_class="Editable_Poly")
    assert node.transform is node.objecttransform
    assert tuple(node.wirecolor) == (134, 88, 199) and node.wirecolor.r == 134
    assert node.isHidden is False and node.children == ["x"]
    assert node.baseObject.classname == "Editable_Poly" and node.baseobject.classname == "Editable_Poly"
    assert node.custom == 7
    with pytest.raises(AttributeError):
        node.nothing_like_it  # noqa: B018


def test_transform_rows_are_point3_like():
    node = make_node("n", "L", "M", x=5.0)
    row4 = node.objecttransform.row4
    assert (row4.x, row4.y, row4.z) == (5.0, 0.0, 0.0)
    assert row4 == (5.0, 0.0, 0.0) and row4[0] == 5.0


def test_unreadable_properties_raise_like_pymxs():
    cam = FakeCamera("C", unreadable=["focal_length", "fov"])
    with pytest.raises(AttributeError):
        cam.focal_length  # noqa: B018
    with pytest.raises(AttributeError):
        cam.focal_length_mm  # noqa: B018 (an alias of an unreadable value is unreadable)
    assert cam.film_width == 36.0


def test_camera_aliases_are_one_stored_value():
    cam = FakeCamera("C")
    cam.focal_length = 50.0
    assert cam.focal_length_mm == 50.0
    cam.iso = 400
    assert cam.ISO == 400
    cam.white_balance_temperature = 5600
    assert cam.white_balance == 5600
    cam.lens_shift_x, cam.lens_shift_y = 0.1, -0.2
    assert (cam.horizontal_shift, cam.vertical_shift) == (0.1, -0.2)
    assert cam.targeted is False and cam.zoom_factor == 1.0 and cam.fov == pytest.approx(54.43, abs=0.01)


def test_camera_is_a_node_and_the_pivot_is_a_decoy():
    cam = FakeCamera("C", ((1, 0, 0), (0, 1, 0), (0, 0, 1), (10, 20, 30)))
    assert isinstance(cam, FakeNode) and isinstance(FakeLight("L"), FakeNode)
    assert cam.transform.row4 != cam.objecttransform.row4
    assert fake_scene().classOf(cam) == "VRayPhysicalCamera"


def test_cameras_collection_iterates_nodes_and_indexes_by_name():
    rt_ = fake_scene()
    assert [c.name for c in rt_.cameras] == ["CAM_main"]
    assert rt_.cameras["CAM_main"] is rt_.getNodeByName("CAM_main")
    assert "CAM_main" in rt_.cameras and rt_.cameras["CAM_main"] in rt_.cameras


def test_lookups_and_classes():
    rt_ = fake_scene()
    assert rt_.getNodeByName("Sun01").name == "Sun01"
    assert rt_.getNodeByName("ARCH_walls") is rt_.objects[0]
    assert rt_.getNodeByName("missing") is None
    assert rt_.classOf(rt_.lights[0]) == "VRayLight"
    assert rt_.superClassOf(rt_.cameras["CAM_main"]) == "camera"
    assert rt_.superClassOf(rt_.objects[0]) == rt_.GeometryClass
    with pytest.raises(TypeError):
        rt_.classOf(None)
    assert rt_.getActiveCamera().name == "CAM_main"
    rt_.active_camera_name = "nope"
    assert rt_.getActiveCamera().name == "CAM_main"


def test_name_and_isproperty():
    rt_ = fake_scene()
    name = rt_.Name("focal_length")
    assert name == "focal_length" and repr(name) == "#focal_length"
    cam = rt_.cameras["CAM_main"]
    assert rt_.isProperty(cam, name) is True
    assert rt_.isProperty(cam, rt_.Name("no_such_thing")) is False
    del cam.focal_length
    assert rt_.isProperty(cam, name) is False


def test_slider_time_moves_the_current_frame():
    rt_ = fake_scene()
    rt_.sliderTime = 12
    assert rt_.currentTime.frame == 12 and rt_.sliderTime.frame == 12
    rt_.sliderTime = rt_.animationRange.end
    assert rt_.currentTime.frame == 0


# ---- the render-element manager


ELEMENT_CLASSES = (
    "VRayZDepth",
    "VRayNormals",
    "VRayCryptomatte",
    "VRayWireColor",
    "VRayDiffuseFilter",
    "VRaySamplerInfo",
    "VRayLightSelect",
    "VRayVelocity",
    "VRayRawLighting",
    "VRayRawGlobalIllumination",
    "VRayReflection",
    "VRayRefraction",
    "VRaySpecular",
    "VRaySelfIllumination",
)


def test_element_manager_adds_counts_and_clears():
    rt_ = fake_scene()
    mgr = rt_.maxOps.GetCurRenderElementMgr()
    assert mgr is rt_.maxOps.GetCurRenderElementMgr() and mgr.NumRenderElements() == 0
    z = rt_.VRayZDepth(elementname="VRayZDepth", zdepth_clamp=False)
    mgr.AddRenderElement(z)
    mgr.AddRenderElement(rt_.VRayNormals())
    assert mgr.NumRenderElements() == 2
    assert mgr.GetRenderElement(0) is z and mgr.GetRenderElement(0).zdepth_clamp is False
    assert rt_.classOf(mgr.GetRenderElement(1)) == "VRayNormals"
    with pytest.raises(IndexError):
        mgr.GetRenderElement(2)
    mgr.SetElementsActive(False)
    assert mgr.GetElementsActive() is False
    assert mgr.RemoveRenderElement(z) is True and mgr.RemoveRenderElement(z) is False
    mgr.RemoveAllRenderElements()
    assert mgr.NumRenderElements() == 0


@pytest.mark.parametrize("name", ELEMENT_CLASSES)
def test_every_element_class_is_constructible(name):
    rt_ = fake_scene()
    element = getattr(rt_, name)()
    assert rt_.classOf(element) == name and element.elementname == name
    assert element.enabled is True and element.filterOn is True


def test_cryptomatte_defaults_and_overrides():
    rt_ = fake_scene()
    element = rt_.VRayCryptomatte(idType=2, num_levels=6)
    assert (element.idType, element.num_levels) == (2, 6)
    assert rt_.VRayCryptomatte().idType == 0


def test_a_hidden_class_is_missing_like_an_uninstalled_element():
    rt_ = fake_scene()
    rt_.hide_class("VRayVelocity")
    assert getattr(rt_, "VRayVelocity", None) is None
    assert getattr(rt_, "VRayNormals", None) is not None
    assert getattr(rt_, "NotAVRayClass", None) is None
    assert getattr(rt_, "ColorPipelineMgr", None) is None


def test_more_element_properties_can_be_taught():
    rt_ = fake_scene()
    rt_.add_element_props("VRayZDepth", depth_black=0.0)
    element = rt_.VRayZDepth()
    assert rt_.isProperty(element, rt_.Name("depth_black")) is True
    assert rt_.isProperty(fake_scene().VRayZDepth(), rt_.Name("depth_black")) is False


# ---- side effects


def test_render_records_kwargs_and_writes_a_valid_png(tmp_path):
    rt_ = fake_scene()
    out = tmp_path / "deep" / "out" / "frame.png"
    bitmap = rt_.render(camera=rt_.cameras["CAM_main"], outputfile=str(out), vfb=True, quiet=True, frame=3)
    call = rt_.render_calls[0]
    assert call["vfb"] is True and call["quiet"] is True and call["frame"] == 3
    assert call["camera"] is rt_.cameras["CAM_main"] and call["outputfile"] == str(out)
    assert _png_size(out) == (2, 2)
    assert (bitmap.width, bitmap.height) == (3840, 2160)


def test_render_without_an_output_file_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rt_ = fake_scene()
    bitmap = rt_.render(vfb=False, outputwidth=640, outputheight=360)
    assert (bitmap.width, bitmap.height, bitmap.filename) == (640, 360, None)
    assert list(tmp_path.iterdir()) == []


def test_vfb_control_saveimage_writes_a_png(tmp_path):
    rt_ = fake_scene()
    target = tmp_path / "display.png"
    rt_.vfbControl(rt_.Name("setsaveformat"), "tif16")
    rt_.vfbControl(rt_.Name("saveimage"), str(target))
    assert [str(a[0]) for a in rt_.vfb_calls] == ["setsaveformat", "saveimage"]
    assert _png_size(target) == (2, 2)


def test_save_max_file_backup_keeps_the_open_file_name(tmp_path):
    rt_ = fake_scene()
    rt_.maxFilePath, rt_.maxFileName = str(tmp_path), "scene.max"
    backup = tmp_path / "scene.plate-backup.20261017T120000.max"
    assert rt_.saveMaxFile(str(backup), useNewFile=False, quiet=True, clearNeedSaveFlag=False) is True
    assert backup.is_file()
    assert (rt_.maxFilePath, rt_.maxFileName) == (str(tmp_path), "scene.max")
    assert rt_.save_calls[0]["useNewFile"] is False
    rt_.saveMaxFile(str(tmp_path / "other.max"))
    assert rt_.maxFileName == "other.max"


def test_vrscene_export_and_execute_are_recorded(tmp_path):
    rt_ = fake_scene()
    target = tmp_path / "shot.vrscene"
    rt_.vrayExportVRScene(str(target), startFrame=0, endFrame=0)
    assert target.is_file() and rt_.vrscene_exports[0]["endFrame"] == 0
    rt_.execute_results["1 + 1"] = 2
    assert rt_.execute("1 + 1") == 2 and rt_.execute("print 1") is None
    assert rt_.executed == ["1 + 1", "print 1"]


def test_sysinfo_offers_a_memory_read():
    rt_ = fake_scene()
    assert rt_.sysInfo.getMAXMemoryInfo()[-1] == rt_.sysInfo.peak_working_set_bytes


def test_a_fresh_runtime_has_no_scene_and_the_seed_defaults():
    rt_ = FakeRuntime()
    assert rt_.objects == [] and len(rt_.cameras) == 0 and rt_.getActiveCamera() is None
    assert str(rt_.classOf(rt_.renderers.current)) == "V_Ray_7"
    assert rt_.units.SystemType == "millimeters"


def test_scene_fixture_installs_the_runtime(scene):
    assert rt.runtime() is scene
