from __future__ import annotations

import math
import random

import pytest
from fake_pymxs import FakeMatrix3

import plate_spec
from plate_max import digest, packet


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


@pytest.mark.parametrize("where", [(0, 0), (1, 2), (0, 3), (2, 3), (3, 0)])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_entry_anywhere_in_the_pose_is_refused(where, value):
    m = [[1.0, 0.0, 0.0, 5.0], [0.0, 1.0, 0.0, 6.0], [0.0, 0.0, 1.0, 7.0], [0, 0, 0, 1]]
    assert digest.rotation_error(m) is None
    m[where[0]][where[1]] = value
    assert digest.rotation_error(m) == digest.NON_FINITE_POSE
    assert digest.non_finite_entries(m) == [where]


def test_a_nan_rotation_is_not_mistaken_for_an_orthonormal_one():
    """max(0.0, nan) is 0.0 and nan > tol is False: the old check passed a NaN axis."""
    m = [[float("nan"), 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0, 0, 0, 1]]
    assert digest.rotation_error(m) is not None
    with pytest.raises(packet.ManifestError, match="non-finite"):
        packet.build_camera_block({**_example_camera_digest(), "world_from_camera": m})


def _example_camera_digest():
    return {
        "name": "C",
        "world_from_camera": [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0, 0, 0, 1]],
        "focal_mm": 24.0,
        "film_gate_mm": 36.0,
        "zoom_factor": 1.0,
        "f_number": 8.0,
        "focus_distance": 1.0,
        "resolution": [160, 90],
        "frame_range": [0, 0],
        "hero_frame": 0,
        "fps": 25.0,
    }


@pytest.mark.parametrize("film", [-36.0, 0.0, float("nan")])
def test_a_film_gate_that_reads_but_is_not_positive_is_kept_as_read(scene, film):
    scene.cameras["CAM_main"].film_width = film
    out = digest.camera_digest("CAM_main")
    assert out["film_gate_mm"] == film or (math.isnan(film) and math.isnan(out["film_gate_mm"]))
    assert out["read_from"]["film_gate_mm"] == "film_width"
    assert out["intrinsics_px"] is None and out["fov_deg"] is None
    assert not any("assumed" in w for w in out["warnings"])


def test_only_an_unreadable_film_gate_is_assumed(scene):
    del scene.cameras["CAM_main"].film_width
    out = digest.camera_digest("CAM_main")
    assert out["film_gate_mm"] == digest.DEFAULT_FILM_MM
    assert any("assumed 36.0 mm" in w for w in out["warnings"])


@pytest.mark.parametrize(("attr", "value"), [("focal_length", 0.0), ("zoom_factor", 0.0)])
def test_a_zero_focal_or_zoom_is_read_not_divided_by(scene, attr, value):
    setattr(scene.cameras["CAM_main"], attr, value)  # the fake keeps its fov: the disagreement branch
    out = digest.camera_digest("CAM_main")
    assert out[{"focal_length": "focal_mm", "zoom_factor": "zoom_factor"}[attr]] == 0.0
    assert out["intrinsics_px"] is None


@pytest.mark.parametrize(("speed", "seconds"), [(0, math.inf), (-50, -0.02), (math.inf, 0.0)])
def test_a_shutter_speed_that_is_not_positive_gives_a_shutter_that_is_not_either(scene, speed, seconds):
    scene.cameras["CAM_main"].shutter_speed = speed
    assert digest.camera_digest("CAM_main")["exposure"]["shutter_s"] == pytest.approx(seconds)


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
    # content-v1 (T9) replaced the interim file-meta-v0: PACKET.md 5 says a resave with no change
    # keeps the hash, so the file's size and mtime are recorded for humans and not hashed
    assert d["scene_hash_method"] == "content-v1" and d["digest_version"] == "1"
    h = digest.scene_hash(d)
    assert len(h) == 64 and h == digest.scene_hash(dict(d))
    assert h == digest.scene_hash({**d, "size_bytes": 1235, "mtime_utc": "2030-01-01T00:00:00Z"})
    scene.objects[0].objecttransform.row4 = (1.0, 2.0, 3.0)
    assert digest.scene_hash(digest.scene_digest()) != h
    # a hand-built digest without lines still hashes the old file-meta triple
    legacy = {"max_file": "a.max", "size_bytes": 1, "mtime_utc": "2026-01-01T00:00:00Z"}
    assert digest.scene_hash(legacy) != digest.scene_hash({**legacy, "size_bytes": 2})


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


# ---- T9: the camera at a frame, candidate names, PACKET 3.3 intrinsics


def _random_rotation(rng):
    """A proper rotation (orthonormal, det +1) from Gram-Schmidt on random vectors."""

    def norm(v):
        n = math.sqrt(sum(c * c for c in v))
        return [c / n for c in v]

    x = norm([rng.gauss(0, 1) for _ in range(3)])
    t = [rng.gauss(0, 1) for _ in range(3)]
    d = sum(a * b for a, b in zip(t, x, strict=True))
    y = norm([a - d * b for a, b in zip(t, x, strict=True)])
    z = [x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0]]
    return x, y, z


def test_max_rows_agree_with_plate_spec_on_20_random_orthonormal_matrices():
    rng = random.Random(2026)
    for _ in range(20):
        x, y, z = _random_rotation(rng)
        rows = (tuple(x), tuple(y), tuple(z), tuple(rng.uniform(-1e4, 1e4) for _ in range(3)))
        ours = digest.world_from_camera_from_max_rows(rows)
        assert ours == plate_spec.world_from_camera_from_max_rows(rows)
        assert digest.rotation_error(ours) is None


def _example_camera():
    return plate_spec.load_example("tierA_still_clubhouse")["camera"]


def _set_example_pose(scene, focal=24.0):
    """The fake camera posed and lensed as the clubhouse example: rows are the transpose of the
    example's world_from_camera (PACKET.md 3.2), 3840x2160, 36 mm film."""
    ex = _example_camera()
    m = ex["world_from_camera"]
    rows = tuple(tuple(m[i][j] for i in range(3)) for j in range(4))
    cam = scene.cameras["CAM_main"]
    cam.objecttransform = FakeMatrix3(rows)
    cam.focal_length, cam.film_width, cam.zoom_factor = focal, 36.0, 1.0
    cam.fov = digest._hfov_deg(focal, 36.0)
    cam.focus_distance, cam.f_number = ex["focus_distance"], ex["f_number"]
    scene.renderWidth, scene.renderHeight, scene.renderPixelAspect = 3840, 2160, 1.0
    return ex


def test_camera_extraction_equals_the_clubhouse_example(scene):
    ex = _set_example_pose(scene)
    recipe = {"safe_frame": ex["safe_frame"], "overscan_aspect": ex["overscan"]["engine_aspect"]}
    out = digest.camera_digest("CAM_main", recipe=recipe)
    assert out["warnings"] == []
    assert out["intrinsics_px"] == ex["intrinsics_px"]
    assert {k: round(v, 4) for k, v in out["fov_deg"].items()} == ex["fov_deg"]
    assert out["world_from_camera"] == ex["world_from_camera"]
    assert out["resolution"] == [3840, 2160] and out["safe_frame"] == ex["safe_frame"]
    block = packet.build_camera_block(out)
    for key in ("intrinsics_px", "fov_deg", "world_from_camera", "resolution", "safe_frame", "lens_shift"):
        assert block[key] == ex[key], key
    assert block["overscan"]["engine_aspect"] == "16:9"
    assert block["exposure"]["ev100"] == ex["exposure"]["ev100"]
    # 1000 units down -Z lands on the principal point (the example's rotation is rounded to 6 dp)
    ahead = [row[3] - 1000.0 * row[2] for row in ex["world_from_camera"][:3]]
    assert plate_spec.project_point(block, ahead)[:2] == pytest.approx((1920.0, 1080.0), abs=1e-2)


def test_a_35mm_camera_follows_the_packet_formula(scene):
    _set_example_pose(scene, focal=35.0)
    out = digest.camera_digest("CAM_main")
    k = out["intrinsics_px"]
    assert k["fx"] == 35.0 * 3840 / 36.0 and k["fy"] == k["fx"]
    assert k["principal_point"] == [1920.0, 1080.0]
    assert out["fov_deg"]["horizontal"] == pytest.approx(math.degrees(2 * math.atan(36 / 70)))
    block = packet.build_camera_block(out)
    assert block["intrinsics_px"] == k
    # the digest keeps full precision; the manifest block rounds the FOVs to 4 dp
    assert {key: round(v, 4) for key, v in out["fov_deg"].items()} == block["fov_deg"]


def test_pixel_aspect_and_zoom_enter_the_intrinsics(scene):
    scene.renderPixelAspect = 1.25
    cam = scene.cameras["CAM_main"]
    cam.zoom_factor = 2.0
    del cam.fov
    out = digest.camera_digest("CAM_main")
    assert out["intrinsics_px"]["fx"] == 24.0 * 2.0 * 3840 / 36.0
    assert out["intrinsics_px"]["fy"] == out["intrinsics_px"]["fx"] * 1.25
    assert out["fov_deg"]["horizontal"] == pytest.approx(math.degrees(2 * math.atan(36 / (2 * 48))))


def test_camera_digest_reads_under_attime_at_the_given_frame(scene):
    from contextlib import contextmanager

    cam = scene.cameras["CAM_main"]
    frames = []
    still = cam.objecttransform

    @contextmanager
    def attime(frame):  # what pymxs.attime does: every read inside sees the scene at frame
        frames.append(frame)
        cam.objecttransform = FakeMatrix3(((1, 0, 0), (0, 1, 0), (0, 0, 1), (float(frame), 0, 0)))
        cam.focal_length = 50.0
        try:
            yield
        finally:
            cam.objecttransform, cam.focal_length = still, 24.0

    scene.attime = attime
    del cam.fov
    out = digest.camera_digest("CAM_main", frame=7)
    assert frames == [7]
    assert out["hero_frame"] == 7
    assert [out["world_from_camera"][i][3] for i in range(3)] == [7.0, 0.0, 0.0]
    assert out["focal_mm"] == 50.0
    assert scene.currentTime.frame == 0  # the time slider never moved
    assert digest.camera_digest("CAM_main")["focal_mm"] == 24.0


def test_without_attime_the_slider_moves_and_comes_back(scene):
    seen = []
    cam = scene.cameras["CAM_main"]
    original = type(cam).__getattribute__

    class Spy(type(cam)):
        def __getattribute__(self, name):
            if name == "objecttransform":
                seen.append(scene.currentTime.frame)
            return original(self, name)

    cam.__class__ = Spy
    out = digest.camera_digest("CAM_main", frame=12)
    assert 12 in seen and out["hero_frame"] == 12
    assert scene.currentTime.frame == 0


def test_every_field_falls_back_through_its_candidates(scene):
    cam = scene.cameras["CAM_main"]
    for name in ("film_width", "f_number", "focus_distance", "use_dof", "shutter_speed", "white_balance"):
        delattr(cam, name)
    cam.properties.update(
        {
            "film_gate": 24.0,
            "fnumber": 4.0,
            "targetDistance": 3200.0,
            "dof_on": True,
            "shutter_s": 0.01,
            "whiteBalance_temp": 5200.0,
        }
    )
    del cam.fov
    out = digest.camera_digest("CAM_main")
    assert out["film_gate_mm"] == 24.0 and out["f_number"] == 4.0
    assert out["focus_distance"] == 3200.0 and out["dof_enabled"] is True
    assert out["exposure"]["shutter_s"] == 0.01 and out["white_balance_k"] == 5200.0
    assert out["read_from"]["film_gate_mm"] == "film_gate"
    assert out["read_from"]["exposure.shutter_s"] == "shutter_s"
    assert out["intrinsics_px"]["fx"] == 24.0 * 3840 / 24.0
    assert out["warnings"] == []


def test_v_ray_stores_shutter_as_one_over_seconds_and_a_movie_camera_uses_the_angle(scene):
    cam = scene.cameras["CAM_main"]
    cam.shutter_speed = 250
    assert digest.camera_digest("CAM_main")["exposure"]["shutter_s"] == pytest.approx(1 / 250)
    cam.properties.update({"type": 1, "shutter_angle": 180.0})
    out = digest.camera_digest("CAM_main")
    assert out["exposure"]["shutter_s"] == pytest.approx(0.5 / 25.0)
    assert out["read_from"]["exposure.shutter_s"] == "shutter_angle"


def test_an_exposure_switch_that_is_off_sends_no_exposure(scene):
    cam = scene.cameras["CAM_main"]
    cam.properties["exposure"] = False
    out = digest.camera_digest("CAM_main")
    assert out["exposure"]["enabled"] is False
    assert packet.build_camera_block(out)["exposure"] == {"mode": "none"}
    cam.properties.update({"exposure": 2, "ev": 13.5})
    block = packet.build_camera_block(digest.camera_digest("CAM_main"))
    assert block["exposure"] == {"mode": "ev", "ev100": 13.5}


def test_specify_fov_makes_the_fov_authoritative(scene):
    cam = scene.cameras["CAM_main"]
    cam.properties["specify_fov"] = True
    cam.fov = 90.0  # focal_length (24) is stale while specify_fov is on
    out = digest.camera_digest("CAM_main")
    assert out["focal_mm"] == pytest.approx(18.0)
    assert any("specify_fov" in w for w in out["warnings"])


def test_focus_follows_the_target_when_specify_focus_is_off(scene):
    cam = scene.cameras["CAM_main"]
    cam.properties.update({"specify_focus": False, "target_distance": 5500.0})
    assert digest.camera_digest("CAM_main")["focus_distance"] == 5500.0


@pytest.mark.parametrize("name", ["lens_shift", "horizontal_offset", "vertical_offset"])
def test_any_non_zero_shift_candidate_is_caught(scene, name):
    scene.cameras["CAM_main"].properties[name] = 0.15
    out = digest.camera_digest("CAM_main")
    assert 0.15 in (out["lens_shift"]["horizontal"], out["lens_shift"]["vertical"])
    with pytest.raises(packet.ManifestError, match="lens shift"):
        packet.build_camera_block(out)


def test_the_shifted_scene_is_refused_by_lint_before_render(install_scene):
    from plate_max import policy

    install_scene("shifted_camera")
    report = policy.lint_scene("CAM_shifted")
    assert "SHIFTED_CAMERA" in [p["code"] for p in report.problems]


# ---- T9: the content scene digest


def _digest_lines():
    return digest.scene_digest()["lines"]


def test_scene_digest_lines_cover_nodes_lights_env_units_render(scene):
    lines = _digest_lines()
    kinds = sorted({line.split("|", 1)[0] for line in lines})
    assert kinds == ["env", "light", "node", "render", "units"]
    arch = next(line for line in lines if line.startswith("node|ARCH_walls|"))
    fields = arch.split("|")
    assert fields[2:4] == ["ARCH_walls", "MAT_brick"]
    assert fields[4].split(",") == ["1.000000", "0.000000", "0.000000", "0.000000", "1.000000"] + [
        "0.000000",
        "0.000000",
        "0.000000",
        "1.000000",
        "0.000000",
        "0.000000",
        "0.000000",
    ]
    assert fields[6] == "134,88,199" and fields[7] == "0"
    light = next(line for line in lines if line.startswith("light|Sun01|"))
    assert "multiplier=1.500000" in light and "on=1" in light
    assert "render|3840|2160|1.000000|V_Ray_7|" in lines
    assert "units|millimeters|1.000000" in lines and "env||0" in lines


def test_scene_digest_is_stable_under_node_reordering(scene):
    base = digest.scene_hash_payload(digest.scene_digest())
    scene.objects.reverse()
    scene.lights.append(type(scene.lights[0])("Fill02", multiplier=0.5))
    with_fill = digest.scene_hash_payload(digest.scene_digest())
    scene.lights.reverse()
    assert digest.scene_hash_payload(digest.scene_digest()) == with_fill
    scene.lights.pop(0 if scene.lights[0].name == "Fill02" else 1)
    assert digest.scene_hash_payload(digest.scene_digest()) == base


@pytest.mark.parametrize(
    "change",
    [
        "transform",
        "material",
        "layer",
        "wirecolor",
        "hidden",
        "mesh",
        "light",
        "resolution",
        "renderer",
        "element",
        "units",
    ],
)
def test_scene_digest_changes_with_every_pixel_relevant_edit(scene, change):
    from fake_pymxs import FakeColor, FakeLayer, FakeMaterial, FakeRenderer

    scene.getPolygonCount = lambda node: [12, 8]
    node = scene.objects[2]
    node.properties.update({"min": (0.0, 0.0, 0.0), "max": (1.0, 1.0, 1.0)})
    before = digest.scene_hash_payload(digest.scene_digest())
    if change == "transform":
        node.objecttransform.row4 = (0.0, 0.0, 0.001)
    elif change == "material":
        node.material = FakeMaterial("MAT_leaf_v2")
    elif change == "layer":
        node.layer = FakeLayer("VEG_tree_02")
    elif change == "wirecolor":
        node.wirecolor = FakeColor(1, 2, 3)
    elif change == "hidden":
        node.isHidden = True
    elif change == "mesh":
        scene.getPolygonCount = lambda n: [14, 9] if n is node else [12, 8]
    elif change == "light":
        scene.lights[0].multiplier = 1.6
    elif change == "resolution":
        scene.renderWidth = 1920
    elif change == "renderer":
        scene.renderers.current = FakeRenderer("V_Ray_GPU_7")
    elif change == "element":
        scene.element_manager.AddRenderElement(scene.VRayZDepth())
    elif change == "units":
        scene.units.SystemScale = 10.0
    assert digest.scene_hash_payload(digest.scene_digest()) != before


def test_mesh_fingerprint_reads_counts_and_world_bbox(scene):
    scene.getPolygonCount = lambda node: [120, 64]  # MAXScript order: #(faces, verts)
    scene.objects[0].properties.update({"min": (-1.0, -2.0, 0.0), "max": (1.0, 2.0, 3.5)})
    d = digest.scene_digest()
    arch = next(line for line in d["lines"] if line.startswith("node|ARCH_walls|"))
    assert arch.split("|")[5] == "64,120,-1.000000,-2.000000,0.000000,1.000000,2.000000,3.500000"
    del scene.getPolygonCount
    d = digest.scene_digest()
    assert any("mesh fingerprint unreadable on 5 node(s)" in w for w in d["warnings"])


def test_field_separators_in_names_cannot_forge_a_digest_line(scene):
    from fake_pymxs import make_node

    scene.objects.append(make_node("A|B", "L", "M"))
    d = digest.scene_digest()
    assert any(line.startswith("node|A\\|B|L|M|") for line in d["lines"])
    one = digest.scene_hash_payload(d)
    assert one.startswith(b"plate-scene-digest 1\n")
    scene.objects[-1] = make_node("A", "B|L", "M")  # the same characters, split differently
    assert digest.scene_hash_payload(digest.scene_digest()) != one
