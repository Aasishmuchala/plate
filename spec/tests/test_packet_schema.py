"""Shot Packet v0.1: the schema, the semantic rules and the reference math.

Every doctrine rule the spec claims to enforce has a mutation here that must be rejected.
"""

from __future__ import annotations

import copy
import json
import math

import jsonschema
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import plate_spec as ps


def _ex(name):
    return copy.deepcopy(ps.load_example(name))


def _legend(name):
    return json.loads((ps.load_example_dir(name) / "id_legend.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- the examples


def test_schema_is_valid_draft_2020_12():
    jsonschema.Draft202012Validator.check_schema(ps.load_schema())


@pytest.mark.parametrize("name", ps.EXAMPLE_NAMES)
def test_examples_validate_against_schema_and_semantics(name):
    assert ps.validate_manifest(ps.load_example(name)) == []
    assert ps.validate_packet_dir(ps.load_example_dir(name), check_files=False) == []


@pytest.mark.parametrize("name", ps.EXAMPLE_NAMES)
def test_materialized_example_passes_file_checks(name, tmp_path):
    root = ps.materialize_example(name, tmp_path / name)
    assert ps.validate_packet_dir(root, check_files=True) == []


def test_tampered_payload_fails_hash_check(tmp_path):
    root = ps.materialize_example("tierA_still_clubhouse", tmp_path / "p")
    p = root / "display.tif"
    p.write_bytes(p.read_bytes().replace(b"PLACEHOLDER", b"PLACEH0LDER"))
    errs = ps.validate_packet_dir(root, check_files=True)
    assert any("display_tiff16: blake3 mismatch" in e for e in errs)


def test_missing_sequence_frame_is_reported(tmp_path):
    root = ps.materialize_example("tierA_motion_drive_in", tmp_path / "p")
    (root / "motion" / "clay.0042.png").unlink()
    errs = ps.validate_packet_dir(root, check_files=True)
    assert any("clay_seq: missing frame motion/clay.0042.png" in e for e in errs)


def test_examples_cover_both_tiers_and_kinds():
    seen = {(ps.load_example(n)["tier"], ps.load_example(n)["kind"]) for n in ps.EXAMPLE_NAMES}
    assert seen == {("A", "still"), ("A", "motion"), ("C", "still")}


# --------------------------------------------------------------------------- mutations that must fail


def _el(m, element):
    return next(e for e in m["files"]["beauty_exr"]["elements"] if e["element"] == element)


def _set(path, value):
    def f(m):
        node = m
        for k in path[:-1]:
            node = node[k]
        node[path[-1]] = value

    return f


def _mut_z_filtered(m):
    _el(m, "zdepth")["filtered"] = True


def _mut_z_clamped(m):
    _el(m, "zdepth")["clamped"] = True


def _mut_z_half(m):
    _el(m, "zdepth")["dtype"] = "half"


def _mut_crypto_half(m):
    _el(m, "cryptomatte_object")["dtype"] = "half"


def _mut_crypto_denoised(m):
    _el(m, "cryptomatte_material")["denoised"] = True


def _mut_normals_world(m):
    _el(m, "normals")["space"] = "world"


def _mut_drop_normals(m):
    els = m["files"]["beauty_exr"]["elements"]
    m["files"]["beauty_exr"]["elements"] = [e for e in els if e["element"] != "normals"]


def _mut_scaled_camera(m):
    for i in range(3):
        for j in range(3):
            m["camera"]["world_from_camera"][i][j] *= 1.01


def _mut_mirrored_camera(m):
    for i in range(3):
        m["camera"]["world_from_camera"][i][0] *= -1


def _mut_bad_last_row(m):
    m["camera"]["world_from_camera"][3] = [0, 0, 1, 1]


def _mut_fov(m):
    m["camera"]["fov_deg"]["horizontal"] += 1.0


def _mut_fx(m):
    m["camera"]["intrinsics_px"]["fx"] *= 1.05


def _mut_skew(m):
    m["camera"]["intrinsics_px"]["K"][0][1] = 3.0


def _mut_safe_frame(m):
    m["camera"]["safe_frame"]["height"] = 5000


def _mut_ev(m):
    m["camera"]["exposure"]["ev100"] = 10.0


def _mut_dotdot(m):
    m["files"]["display_tiff16"]["path"] = "../elsewhere/display.tif"


def _mut_backslash(m):
    m["files"]["display_tiff16"]["path"] = "renders\\display.tif"


def _mut_absolute(m):
    m["files"]["display_tiff16"]["path"] = "/Volumes/nas/display.tif"


def _mut_drive_letter(m):
    m["files"]["display_tiff16"]["path"] = "D:/PLATE/display.tif"


def _mut_tiff8(m):
    m["files"]["display_tiff16"]["bit_depth"] = 8


def _mut_missing_beauty(m):
    del m["files"]["beauty_exr"]


def _mut_unknown_file_role(m):
    m["files"]["screenshot"] = copy.deepcopy(m["files"]["display_tiff16"])


def _mut_still_with_motion(m):
    m["motion"] = {"hero_still_packet_id": m["packet_id"]}


def _mut_estimated_on_tier_a(m):
    m["estimated"] = {"depth_is_metric": False}


def _mut_lock_and_edit(m):
    m["policy"]["locked_ids"].append(m["policy"]["editable_ids"][0])


def _mut_lint_failed(m):
    m["policy"]["lint"]["passed"] = False


def _mut_lint_over(m):
    m["policy"]["lint"]["unlabelled_share"] = 0.2
    m["id_legend"]["coverage"]["unlabelled_share"] = 0.2


def _mut_nda_grade_d(m):
    m["policy"]["custody"] = {"nda": True, "allowed_grades": ["C0", "C1", "D"]}


def _mut_lock_by_default_off(m):
    m["policy"]["lock_by_default"] = False


def _mut_text_lock_off(m):
    m["policy"]["text_lock"] = False


def _mut_ocio_block_missing(m):
    del m["colour"]["ocio"]


def _mut_master_exr(m):
    m["colour"]["master"] = "beauty_exr"


def _mut_bad_packet_id(m):
    m["packet_id"] = "pkt_tula-clubhouse"


def _mut_still_range(m):
    m["camera"]["frames"]["end"] = 10


def _mut_up_axis(m):
    m["units"]["up_axis"] = "Y"


def _mut_convention(m):
    m["camera"]["convention"]["camera_forward"] = "+Z"


def _mut_case_collision(m):
    m["files"]["x_display_copy"] = {**copy.deepcopy(m["files"]["id_legend"]), "path": "ID_Legend.json"}


def _mut_denoised_z(m):
    m["render"]["denoiser"]["denoised_elements"] = ["RGBA", "VRayZDepth"]


TIER_A_STILL_MUTATIONS = [
    _mut_z_filtered,
    _mut_z_clamped,
    _mut_z_half,
    _mut_crypto_half,
    _mut_crypto_denoised,
    _mut_normals_world,
    _mut_drop_normals,
    _mut_scaled_camera,
    _mut_mirrored_camera,
    _mut_bad_last_row,
    _mut_fov,
    _mut_fx,
    _mut_skew,
    _mut_safe_frame,
    _mut_ev,
    _mut_dotdot,
    _mut_backslash,
    _mut_absolute,
    _mut_drive_letter,
    _mut_tiff8,
    _mut_missing_beauty,
    _mut_unknown_file_role,
    _mut_still_with_motion,
    _mut_estimated_on_tier_a,
    _mut_lock_and_edit,
    _mut_lint_failed,
    _mut_lint_over,
    _mut_nda_grade_d,
    _mut_lock_by_default_off,
    _mut_text_lock_off,
    _mut_ocio_block_missing,
    _mut_master_exr,
    _mut_bad_packet_id,
    _mut_still_range,
    _mut_up_axis,
    _mut_convention,
    _mut_denoised_z,
    _mut_case_collision,
]


@pytest.mark.parametrize("mutate", TIER_A_STILL_MUTATIONS, ids=lambda f: f.__name__)
def test_tier_a_still_rejects(mutate):
    m = _ex("tierA_still_clubhouse")
    mutate(m)
    assert ps.validate_manifest(m), f"{mutate.__name__} was accepted"


def _mut_motion_hero_not_start(m):
    m["camera"]["frames"]["hero"] = 10


def _mut_motion_no_frames_file(m):
    del m["files"]["camera_frames"]


def _mut_motion_seq_range(m):
    m["files"]["z_seq"]["last"] = 100


def _mut_motion_seq_pattern(m):
    m["files"]["clay_seq"]["pattern"] = "motion/clay.png"


def _mut_motion_frame0(m):
    m["motion"]["frame0_matches_hero"] = False


def _mut_motion_self_hero(m):
    m["motion"]["hero_still_packet_id"] = m["packet_id"]


def _mut_motion_aspect(m):
    m["motion"]["sequence_resolution"] = {"width": 1280, "height": 960}
    for role in ("clay_seq", "id_seq", "z_seq"):
        m["files"][role]["height"] = 960


@pytest.mark.parametrize(
    "mutate",
    [
        _mut_motion_hero_not_start,
        _mut_motion_no_frames_file,
        _mut_motion_seq_range,
        _mut_motion_seq_pattern,
        _mut_motion_frame0,
        _mut_motion_self_hero,
        _mut_motion_aspect,
    ],
    ids=lambda f: f.__name__,
)
def test_tier_a_motion_rejects(mutate):
    m = _ex("tierA_motion_drive_in")
    mutate(m)
    assert ps.validate_manifest(m), f"{mutate.__name__} was accepted"


def _mut_c_scene_hash(m):
    m["scene_hash"] = {"algo": "blake3", "hex": "0" * 64, "digest_version": "1"}


def _mut_c_metric(m):
    m["estimated"]["depth_is_metric"] = True


def _mut_c_units(m):
    m["units"] = {"system_unit": "meters", "meters_per_unit": 1.0, "up_axis": "Z"}


def _mut_c_pose(m):
    m["camera"]["world_from_camera"][0][3] = 5.0


def _mut_c_motion(m):
    m["kind"] = "motion"


def _mut_c_no_estimated(m):
    del m["estimated"]


def _mut_c_host(m):
    m["host"]["app"] = "3dsmax"


@pytest.mark.parametrize(
    "mutate",
    [
        _mut_c_scene_hash,
        _mut_c_metric,
        _mut_c_units,
        _mut_c_pose,
        _mut_c_motion,
        _mut_c_no_estimated,
        _mut_c_host,
    ],
    ids=lambda f: f.__name__,
)
def test_tier_c_rejects(mutate):
    m = _ex("tierC_client_plate")
    mutate(m)
    assert ps.validate_manifest(m), f"{mutate.__name__} was accepted"


# --------------------------------------------------------------------------- legend and policy


def test_architecture_can_never_be_editable():
    m, lg = _ex("tierA_still_clubhouse"), _legend("tierA_still_clubhouse")
    arch = next(e["id"] for e in lg["entries"] if e["class"] == "architecture")
    m["policy"]["locked_ids"].remove(arch)
    m["policy"]["editable_ids"].append(arch)
    assert any("can never be AI-editable" in e for e in ps.validate_legend(lg, m))


def test_signage_can_never_be_editable():
    m, lg = _ex("tierC_client_plate"), _legend("tierC_client_plate")
    sign = next(e["id"] for e in lg["entries"] if e["class"] == "signage")
    m["policy"]["locked_ids"].remove(sign)
    m["policy"]["editable_ids"].append(sign)
    assert any("can never be AI-editable" in e for e in ps.validate_legend(lg, m))


def test_every_legend_id_must_be_locked_or_editable():
    m, lg = _ex("tierA_still_clubhouse"), _legend("tierA_still_clubhouse")
    m["policy"]["locked_ids"].pop()
    assert any("neither locked nor editable" in e for e in ps.validate_legend(lg, m))


def test_policy_cannot_name_ids_outside_the_legend():
    m, lg = _ex("tierA_still_clubhouse"), _legend("tierA_still_clubhouse")
    m["policy"]["locked_ids"].append("o:deadbeef")
    assert any("not in id_legend.json" in e for e in ps.validate_legend(lg, m))


def test_rule_class_must_match_the_rule():
    m, lg = _ex("tierA_still_clubhouse"), _legend("tierA_still_clubhouse")
    veg = next(e for e in lg["entries"] if e["class"] == "vegetation" and e["kind"] == "object")
    veg["class"] = "people"
    assert any("first matching rule" in e for e in ps.validate_legend(lg, m))


def test_coverage_must_sum_from_entries():
    m, lg = _ex("tierA_still_clubhouse"), _legend("tierA_still_clubhouse")
    lg["entries"][0]["pixel_share"] += 0.05
    assert any("labelled_share" in e for e in ps.validate_legend(lg, m))


def test_legend_packet_id_must_match():
    m, lg = _ex("tierA_still_clubhouse"), _legend("tierA_still_clubhouse")
    lg["packet_id"] = "pkt_01JA7R0B2N6T8W4Y1A3C5E7G9J"
    assert any("packet_id does not match" in e for e in ps.validate_legend(lg, m))


def test_segments_not_allowed_in_cryptomatte_legend():
    m, lg = _ex("tierA_still_clubhouse"), _legend("tierA_still_clubhouse")
    lg["entries"][0] = {**lg["entries"][0], "id": "s:0001", "kind": "segment"}
    assert ps.validate_legend(lg, m)


def test_camera_frames_must_be_contiguous_and_start_at_hero():
    name = "tierA_motion_drive_in"
    m = _ex(name)
    frames = json.loads((ps.load_example_dir(name) / "camera_frames.json").read_text(encoding="utf-8"))
    assert ps.validate_camera_frames(frames, m) == []
    gap = copy.deepcopy(frames)
    del gap["frames"][40]
    assert any("contiguous" in e for e in ps.validate_camera_frames(gap, m))
    moved = copy.deepcopy(frames)
    moved["frames"][0]["world_from_camera"][0][3] += 1.0
    assert any("hero frame matrix" in e for e in ps.validate_camera_frames(moved, m))


# --------------------------------------------------------------------------- reference math


def test_murmur3_known_vectors():
    assert ps.murmur3_32(b"") == 0
    assert ps.murmur3_32(b"hello") == 0x248BFA47
    assert ps.murmur3_32(b"The quick brown fox jumps over the lazy dog") == 0x2E4FF723


@given(st.text(min_size=1, max_size=40))
@settings(max_examples=300, deadline=None)
def test_cryptomatte_float_is_always_finite_and_normal(name):
    f = ps.cryptomatte_float(name)
    assert math.isfinite(f)
    assert f == 0.0 or abs(f) >= 1.1754943508222875e-38  # never denormal
    assert ps.cryptomatte_id(name).startswith("o:")


def test_max_identity_rows_give_identity_pose():
    m = ps.world_from_camera_from_max_rows([[1, 0, 0], [0, 1, 0], [0, 0, 1], [0, 0, 0]])
    assert m == [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def test_max_rows_are_transposed_into_columns():
    rows = [[0, 1, 0], [-1, 0, 0], [0, 0, 1], [10, 20, 30]]  # 90 degree yaw, translated
    m = ps.world_from_camera_from_max_rows(rows)
    assert [m[i][0] for i in range(3)] == [0, 1, 0]  # camera +X maps to world +Y
    assert [m[i][3] for i in range(3)] == [10, 20, 30]


def _camera_for(rows, w=1000, h=500, f=25.0, film=36.0):
    fx = f * w / film
    return {
        "world_from_camera": ps.world_from_camera_from_max_rows(rows),
        "intrinsics_px": {"fx": fx, "fy": fx, "principal_point": [w / 2, h / 2]},
    }


def test_point_on_the_view_axis_projects_to_principal_point():
    cam = _camera_for([[1, 0, 0], [0, 0, 1], [0, -1, 0], [0, 0, 0]])  # looking down world +Y, Z up
    u, v, d = ps.project_point(cam, [0, 7.5, 0])
    assert (round(u, 9), round(v, 9), d) == (500.0, 250.0, 7.5)


def test_projection_axes_right_and_up():
    cam = _camera_for([[1, 0, 0], [0, 0, 1], [0, -1, 0], [0, 0, 0]])
    u_r, v_r, _ = ps.project_point(cam, [1, 10, 0])  # world +X is camera right
    u_u, v_u, _ = ps.project_point(cam, [0, 10, 1])  # world +Z is camera up -> smaller v
    assert u_r > 500 and abs(v_r - 250) < 1e-9
    assert v_u < 250 and abs(u_u - 500) < 1e-9


def test_point_behind_camera_raises():
    cam = _camera_for([[1, 0, 0], [0, 0, 1], [0, -1, 0], [0, 0, 0]])
    with pytest.raises(ValueError):
        ps.project_point(cam, [0, -3, 0])


def _rows_from_euler(yaw, pitch, roll, t):
    cy, sy, cp, sp, cr, sr = (
        math.cos(yaw),
        math.sin(yaw),
        math.cos(pitch),
        math.sin(pitch),
        math.cos(roll),
        math.sin(roll),
    )
    rz = [[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]]
    rx = [[1, 0, 0], [0, cp, -sp], [0, sp, cp]]
    ry = [[cr, 0, sr], [0, 1, 0], [-sr, 0, cr]]
    r = [
        [sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for a, b in [(rz, rx)] for i in range(3)
    ]
    r = [[sum(r[i][k] * ry[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    return [[r[0][c], r[1][c], r[2][c]] for c in range(3)] + [list(t)]  # Max rows = columns of R


@given(
    st.floats(-math.pi, math.pi),
    st.floats(-1.4, 1.4),
    st.floats(-0.5, 0.5),
    st.tuples(*[st.floats(-1e5, 1e5)] * 3),
    st.floats(0.5, 500.0),
)
@settings(max_examples=200, deadline=None)
def test_any_rigid_max_pose_projects_its_forward_point_to_centre(yaw, pitch, roll, t, dist):
    rows = _rows_from_euler(yaw, pitch, roll, t)
    cam = _camera_for(rows)
    assert ps._orthonormal_error(cam["world_from_camera"]) is None
    fwd = [-rows[2][i] for i in range(3)]  # camera looks down local -Z
    p = [t[i] + dist * fwd[i] for i in range(3)]
    u, v, d = ps.project_point(cam, p)
    assert abs(u - 500) < 1e-6 and abs(v - 250) < 1e-6 and abs(d - dist) < 1e-6 * max(1, dist)


def test_example_hero_camera_sees_its_focus_point_at_centre():
    m = ps.load_example("tierA_still_clubhouse")
    cam = m["camera"]
    wfc = cam["world_from_camera"]
    fwd = [-wfc[i][2] for i in range(3)]
    p = [wfc[i][3] + cam["focus_distance"] * fwd[i] for i in range(3)]
    u, v, d = ps.project_point(cam, p)
    assert abs(u - 1920) < 1e-2 and abs(v - 1080) < 1e-2  # matrices are stored to 6 decimals
    assert abs(d - cam["focus_distance"]) < 1e-6 * cam["focus_distance"]


# --------------------------------------------------------------------------- Opus review probes (2026-10-17)
# Each probe was accepted by the first draft; every one must now be rejected.


def _probe_lens_shift(m):
    m["camera"]["lens_shift"]["vertical"] = 0.3


def _probe_tilt(m):
    m["camera"]["lens_shift"]["vertical_tilt_correction"] = True


def _probe_distortion(m):
    m["camera"]["distortion"] = "vray_lens_file"


def _probe_dwaa(m):
    m["files"]["beauty_exr"]["compression"] = "dwaa"


def _probe_engine_aspect(m):
    m["camera"]["overscan"]["engine_aspect"] = "1:1"


def _probe_partial_safe_frame_without_overscan(m):
    del m["camera"]["overscan"]


def _probe_coverage_not_one(m):
    m["id_legend"]["coverage"]["uncovered_share"] = 0.3


def _probe_nda_without_grades(m):
    m["policy"]["custody"] = {"nda": True}


@pytest.mark.parametrize(
    "mutate",
    [
        _probe_lens_shift,
        _probe_tilt,
        _probe_distortion,
        _probe_dwaa,
        _probe_engine_aspect,
        _probe_partial_safe_frame_without_overscan,
        _probe_coverage_not_one,
        _probe_nda_without_grades,
    ],
    ids=lambda f: f.__name__,
)
def test_review_probes_still(mutate):
    m = _ex("tierA_still_clubhouse")
    mutate(m)
    assert ps.validate_manifest(m), f"{mutate.__name__} was accepted"


def _probe_z_seq_png8(m):
    m["files"]["z_seq"].update({"format": "png", "dtype": "uint8", "pattern": "motion/z.%04d.png"})


def _probe_id_seq_half(m):
    m["files"]["id_seq"]["dtype"] = "half"


def _probe_aspect_727(m):
    m["motion"]["sequence_resolution"] = {"width": 1280, "height": 727}
    for role in ("clay_seq", "id_seq", "z_seq"):
        m["files"][role]["height"] = 727


def _probe_double_pattern(m):
    m["files"]["clay_seq"]["pattern"] = "motion/clay.%04d.%04d.png"


def _probe_string_pattern(m):
    m["files"]["clay_seq"]["pattern"] = "motion/%s%04d.png"


@pytest.mark.parametrize(
    "mutate",
    [_probe_z_seq_png8, _probe_id_seq_half, _probe_aspect_727, _probe_double_pattern, _probe_string_pattern],
    ids=lambda f: f.__name__,
)
def test_review_probes_motion(mutate):
    m = _ex("tierA_motion_drive_in")
    mutate(m)
    assert ps.validate_manifest(m), f"{mutate.__name__} was accepted"


def _probe_c_depth_png(m):
    m["files"]["depth_estimated"].update({"format": "png", "bit_depth": 8, "path": "depth.png"})


def _probe_c_no_depth_semantics(m):
    del m["estimated"]["depth_semantics"]


def _probe_c_dof(m):
    m["camera"]["dof_enabled"] = True


@pytest.mark.parametrize(
    "mutate", [_probe_c_depth_png, _probe_c_no_depth_semantics, _probe_c_dof], ids=lambda f: f.__name__
)
def test_review_probes_tier_c(mutate):
    m = _ex("tierC_client_plate")
    mutate(m)
    assert ps.validate_manifest(m), f"{mutate.__name__} was accepted"


def test_editable_material_on_locked_object_is_rejected():
    """A living-wall material made editable must not open up the locked facade that also uses it."""
    m, lg = _ex("tierA_still_clubhouse"), _legend("tierA_still_clubhouse")
    facade = next(e for e in lg["entries"] if e["name"] == "ARCH_clubhouse_facade")
    facade["material"] = "MAT_leaf_palm"
    assert any("used by a locked object" in e for e in ps.validate_legend(lg, m))


def test_autotag_cannot_override_a_never_editable_rule():
    m, lg = _ex("tierA_still_clubhouse"), _legend("tierA_still_clubhouse")
    facade = next(e for e in lg["entries"] if e["name"] == "ARCH_clubhouse_facade")
    facade.update({"class": "vegetation", "class_source": "autotag"})
    m["policy"]["locked_ids"].remove(facade["id"])
    m["policy"]["editable_ids"].append(facade["id"])
    errs = ps.validate_legend(lg, m)
    assert any("first matching rule" in e for e in errs)
    assert any("can never be AI-editable" in e for e in errs)


def test_manual_override_of_architecture_rule_still_cannot_be_editable():
    m, lg = _ex("tierA_still_clubhouse"), _legend("tierA_still_clubhouse")
    facade = next(e for e in lg["entries"] if e["name"] == "ARCH_clubhouse_facade")
    facade.update({"class": "vegetation", "class_source": "manual"})
    m["policy"]["locked_ids"].remove(facade["id"])
    m["policy"]["editable_ids"].append(facade["id"])
    assert any("layer and can never be AI-editable" in e for e in ps.validate_legend(lg, m))


def test_id_must_be_the_cryptomatte_hash_of_its_name():
    m, lg = _ex("tierA_still_clubhouse"), _legend("tierA_still_clubhouse")
    veg = next(e for e in lg["entries"] if e["name"] == "VEG_palm_row_A")
    veg["name"] = "VEG_something_else"
    assert any("not the Cryptomatte hash" in e for e in ps.validate_legend(lg, m))


def test_tier_a_entries_need_digests():
    m, lg = _ex("tierA_still_clubhouse"), _legend("tierA_still_clubhouse")
    del lg["entries"][0]["digest"]
    assert any("needs a digest" in e for e in ps.validate_legend(lg, m))


def test_dropping_legend_entries_without_declaring_them_uncovered_fails():
    m, lg = _ex("tierA_still_clubhouse"), _legend("tierA_still_clubhouse")
    dropped = [e for e in lg["entries"] if e["name"] in ("ARCH_clubhouse_facade", "Box042")]
    lg["entries"] = [e for e in lg["entries"] if e not in dropped]
    m["id_legend"]["entry_count"] = len(lg["entries"])
    for e in dropped:
        for key in ("locked_ids", "editable_ids"):
            if e["id"] in m["policy"][key]:
                m["policy"][key].remove(e["id"])
    assert ps.validate_legend(lg, m)


def test_segment_ids_are_five_digit_16_bit_labels():
    m, lg = _ex("tierC_client_plate"), _legend("tierC_client_plate")
    alias = copy.deepcopy(lg)
    alias["entries"][0]["id"] = "s:0001"
    assert ps.validate_legend(alias, m)
    big = copy.deepcopy(lg)
    big["entries"][0]["id"] = "s:99999"
    assert any("16-bit label range" in e for e in ps.validate_legend(big, m))


def test_malformed_hashes_line_is_an_error_not_a_crash(tmp_path):
    root = ps.materialize_example("tierA_motion_drive_in", tmp_path / "p")
    (root / "motion" / "clay.blake3").write_text("deadbeef\n", encoding="utf-8", newline="\n")
    errs = ps.validate_packet_dir(root, check_files=True)
    assert any("hashes file has a line without" in e for e in errs)


def test_legend_with_utf8_bom_is_read(tmp_path):
    """Windows tools often write a UTF-8 BOM; the reader accepts it rather than crashing."""
    root = ps.materialize_example("tierA_still_clubhouse", tmp_path / "p")
    legend = root / "id_legend.json"
    legend.write_bytes(b"\xef\xbb\xbf" + legend.read_bytes())
    errs = ps.validate_packet_dir(root, check_files=False)
    assert errs == []


def test_non_json_legend_is_an_error_not_a_crash(tmp_path):
    root = ps.materialize_example("tierA_still_clubhouse", tmp_path / "p")
    (root / "id_legend.json").write_bytes(b"\x00\x01not json")
    errs = ps.validate_packet_dir(root, check_files=False)
    assert any("not UTF-8 JSON" in e for e in errs)


# --------------------------------------------------------------------------- 0.1.1: fixture packets, EXR parts


def test_examples_are_at_the_current_patch():
    assert ps.PACKET_VERSION == "0.1.1"
    assert {ps.load_example(n)["packet_version"] for n in ps.EXAMPLE_NAMES} == {ps.PACKET_VERSION}


def _fixture(m):
    """The repo fixture shape (router plan decision 10): Blender/Cycles repacked to V-Ray layer names."""
    m["host"] = {
        "app": "none",
        "version": "blender-5.1.1",
        "plugin": {"name": "plate_fixture", "version": "0.1.0"},
    }
    m["renderer"] = {"name": "vray", "version": "fixture-cycles-5.1"}
    m["render"]["session"] = "fixture"
    c = m["colour"]
    c.pop("ocio", None)
    c.update(max_colour_mode="legacy_gamma", legacy_gamma=2.2, working_space="lin_rec709")
    c["vfb_corrections_baked"] = False
    m["files"]["beauty_exr"]["parts"] = "single"
    return m


@pytest.mark.parametrize("parts", ["single", "multi"])
def test_fixture_packet_validates(parts):
    m = _fixture(_ex("tierA_still_clubhouse"))
    m["files"]["beauty_exr"]["parts"] = parts
    assert ps.validate_manifest(m) == []


def test_fixture_motion_packet_validates():
    assert ps.validate_manifest(_fixture(_ex("tierA_motion_drive_in"))) == []


def test_beauty_exr_parts_is_optional():
    m = _ex("tierA_still_clubhouse")
    assert "parts" not in m["files"]["beauty_exr"]
    assert ps.validate_manifest(m) == []


def _mut_parts_unknown(m):
    m["files"]["beauty_exr"]["parts"] = "per_element"


def _mut_host_none_outside_fixture(m):
    m["render"]["session"] = "interactive"


def _mut_fixture_from_max(m):
    m["host"] = _ex("tierA_still_clubhouse")["host"]


@pytest.mark.parametrize(
    "mutate",
    [_mut_parts_unknown, _mut_host_none_outside_fixture, _mut_fixture_from_max],
    ids=lambda f: f.__name__,
)
def test_fixture_packet_rejects(mutate):
    m = _fixture(_ex("tierA_still_clubhouse"))
    mutate(m)
    assert ps.validate_manifest(m), f"{mutate.__name__} was accepted"


def test_fixture_session_is_tier_a_only():
    m = _ex("tierC_client_plate")
    m["render"]["session"] = "fixture"
    assert ps.validate_manifest(m)
