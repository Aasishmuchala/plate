from __future__ import annotations

import math
import random

import pytest
from fake_pymxs import FakeMatrix3

import plate_spec
from plate_max import digest


def _random_rows(rng):
    return tuple(tuple(rng.uniform(-5, 5) for _ in range(3)) for _ in range(4))


def test_max_rows_to_matrix_agrees_with_plate_spec_on_random_inputs():
    rng = random.Random(7)
    for _ in range(300):
        rows = _random_rows(rng)
        assert digest.world_from_camera_from_max_rows(rows) == plate_spec.world_from_camera_from_max_rows(
            rows
        )


def test_max_rows_rejects_the_wrong_shape():
    with pytest.raises(ValueError):
        digest.world_from_camera_from_max_rows([(1, 0, 0)] * 3)


def test_camera_digest_reads_the_fake_camera(scene):
    cam = digest.camera_digest("CAM_main")
    m = cam["world_from_camera"]
    assert m[3] == [0, 0, 0, 1]
    assert [m[i][3] for i in range(3)] == [12000.0, -8500.0, 1600.0]
    # camera forward (-Z column) is the world direction the fake scene aims along
    assert (-m[0][2], -m[1][2], -m[2][2]) == pytest.approx(
        (math.cos(math.radians(30)), math.sin(math.radians(30)), 0.0)
    )
    assert digest.rotation_error(m) is None
    assert cam["focal_mm"] == 24.0 and cam["film_gate_mm"] == 36.0
    assert cam["resolution"] == [3840, 2160] and cam["pixel_aspect"] == 1.0
    assert cam["frame_range"] == [0, 0] and cam["hero_frame"] == 0 and cam["fps"] == 25.0
    assert cam["exposure"]["iso"] == 100 and cam["exposure"]["shutter_s"] == pytest.approx(1 / 125)
    assert cam["white_balance_k"] == 6500
    assert cam["safe_frame"] == {"x": 0, "y": 0, "width": 3840, "height": 2160}
    assert cam["node_type"] == "VRayPhysicalCamera"
    assert cam["warnings"] == []


def test_pose_comes_from_objecttransform_not_the_pivot(scene):
    cam = scene.cameras["CAM_main"]
    assert cam.transform.row4 != cam.objecttransform.row4  # the decoy differs
    m = digest.camera_digest("CAM_main")["world_from_camera"]
    assert [m[i][3] for i in range(3)] == list(cam.objecttransform.row4)


def test_a_camera_without_objecttransform_has_no_pose(scene):
    del scene.cameras["CAM_main"].objecttransform
    out = digest.camera_digest("CAM_main")
    assert out["world_from_camera"] is None  # never falls back to the pivot transform
    assert any("objecttransform" in w for w in out["warnings"])


def test_lens_shift_tilt_and_distortion_are_read(scene):
    cam = scene.cameras["CAM_main"]
    cam.horizontal_shift, cam.vertical_tilt, cam.distortion = 0.1, 2.5, 0.03
    cam.auto_vertical_tilt = True
    out = digest.camera_digest("CAM_main")
    assert out["lens_shift"]["horizontal"] == 0.1
    assert out["tilt"] == {"vertical": 2.5, "horizontal": 0.0, "auto": True}
    assert out["distortion"] == {"amount": 0.03, "type": 0.0}


def test_unreadable_lens_properties_warn_instead_of_crashing(scene):
    cam = scene.cameras["CAM_main"]
    for name in ("horizontal_shift", "vertical_tilt", "distortion"):
        delattr(cam, name)
    out = digest.camera_digest("CAM_main")
    assert out["lens_shift"]["horizontal"] == 0.0
    assert any("unverified" in w for w in out["warnings"])


def test_missing_attributes_become_none_plus_a_warning(scene):
    cam = scene.cameras["CAM_main"]
    del cam.focus_distance
    del cam.ISO
    cam.white_balance = object()  # a colour swatch, not a Kelvin number
    out = digest.camera_digest("CAM_main")
    assert out["focus_distance"] is None
    assert out["exposure"]["iso"] is None
    assert out["white_balance_k"] is None
    joined = " | ".join(out["warnings"])
    assert "focus_distance" in joined and "ISO" in joined and "white_balance" in joined


def test_focal_is_derived_from_fov_when_the_camera_has_no_focal_length(scene):
    cam = scene.cameras["CAM_main"]
    del cam.focal_length
    cam.fov = 73.7398
    out = digest.camera_digest("CAM_main")
    assert out["focal_mm"] == pytest.approx(24.0, abs=1e-3)
    assert any("derived from fov" in w for w in out["warnings"])


def test_a_scaled_camera_is_reported(scene):
    scene.cameras["CAM_main"].objecttransform = FakeMatrix3(((2, 0, 0), (0, 2, 0), (0, 0, 2), (0, 0, 0)))
    out = digest.camera_digest("CAM_main")
    assert any("not orthonormal" in w for w in out["warnings"])


def test_unknown_camera_raises(scene):
    with pytest.raises(digest.CameraNotFound):
        digest.camera_digest("nope")


def test_scene_digest_and_hash(scene, tmp_path):
    max_file = tmp_path / "WOT_clubhouse_v12.max"
    max_file.write_bytes(b"x" * 1234)
    scene.maxFilePath = str(tmp_path)
    scene.maxFileName = max_file.name
    d = digest.scene_digest()
    assert d["max_file"] == "WOT_clubhouse_v12.max" and d["size_bytes"] == 1234
    assert d["mtime_utc"].endswith("Z") and d["mtime_utc"].count(":") == 2
    assert d["units_system"] == "millimeters" and d["meters_per_unit"] == 0.001
    assert d["scene_hash_method"] == "file-meta-v0"
    h = digest.scene_hash(d)
    assert len(h) == 64 and h == digest.scene_hash(dict(d))
    assert h != digest.scene_hash({**d, "size_bytes": 1235})


def test_scene_digest_without_a_saved_file_warns(scene):
    d = digest.scene_digest()
    assert d["max_file"] is None
    assert d["size_bytes"] is None


def test_units_scale_is_applied(scene):
    scene.units.SystemType = "#Inches"
    scene.units.SystemScale = 2.0
    assert digest.scene_digest()["meters_per_unit"] == pytest.approx(0.0508)


def test_scene_nodes_skip_non_geometry_and_read_tags(scene):
    scene.objects.append(scene.cameras["CAM_main"])
    nodes = digest.scene_nodes()
    assert len(nodes) == 6
    arch = next(n for n in nodes if n["name"] == "ARCH_walls")
    assert (
        arch["material"] == "MAT_brick" and arch["layer"] == "ARCH_walls" and arch["tags"] == ["architecture"]
    )
    assert arch["transform"] == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [0.0, 0.0, 0.0]]
    assert isinstance(arch["material_uid"], int)


def test_scene_globals_lists_lights_and_renderer(scene):
    g = digest.scene_globals()
    assert [light["name"] for light in g["lights"]] == ["Sun01"]
    assert g["lights"][0]["multiplier"] == 1.5 and g["lights"][0]["on"] is True
    assert g["lights"][0]["class"] == "VRayLight"
    assert g["renderer"] == "V_Ray_7"
    assert g["environment"] == {"map": None, "use_map": False}
    assert g["digest_version"] == "0"
