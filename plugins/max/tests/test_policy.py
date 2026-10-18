from __future__ import annotations

import hashlib

import pytest
from fake_pymxs import make_node

import plate_spec
from plate_max import digest, legend, policy

_digest = lambda data: hashlib.sha256(data).hexdigest()  # noqa: E731 (stand-in for BLAKE3: shape only)
EDITABLE_NAMES = ("VEG_tree_01", "SKY", "PEOPLE_01", "WATER_pool")
LOCKED_NAMES = ("ARCH_walls", "GLAZING_south")


def _legend(scene, extra=()):
    nodes = digest.scene_nodes()
    nodes += list(extra)
    return legend.build_id_legend(nodes, digest_fn=_digest)


def test_editable_ids_are_exactly_the_four_labelled_layers(scene):
    leg = _legend(scene)
    pol = policy.derive_policy(leg)
    objects = {i for i in pol["editable_ids"] if i.startswith("o:")}
    assert objects == {legend.object_id(n) for n in EDITABLE_NAMES}
    # their materials ride along, nothing else does
    assert {i for i in pol["editable_ids"] if i.startswith("m:")} == {
        legend.material_id(m) for m in ("MAT_leaf", "MAT_sky", "MAT_people", "MAT_water")
    }
    assert {legend.object_id(n) for n in LOCKED_NAMES} <= set(pol["locked_ids"])
    assert pol["unlabelled_ids"] == []


def test_locked_and_editable_are_disjoint_and_cover_the_legend(scene):
    leg = _legend(scene)
    pol = policy.derive_policy(leg)
    assert not set(pol["locked_ids"]) & set(pol["editable_ids"])
    assert set(pol["locked_ids"]) | set(pol["editable_ids"]) == {e["id"] for e in leg["entries"]}


def test_a_veg_layer_tagged_signage_is_locked(scene):
    scene.objects.append(make_node("VEG_logo_planter", "VEG_logo", "MAT_paint", tags=("signage",)))
    leg = _legend(scene)
    pol = policy.derive_policy(leg)
    entry = next(e for e in leg["entries"] if e["name"] == "VEG_logo_planter")
    assert (entry["class"], entry["class_source"]) == ("signage", "manual")
    assert entry["id"] in pol["locked_ids"]
    assert entry["id"] not in pol["editable_ids"]


def test_a_material_shared_with_the_facade_stays_locked(scene):
    scene.objects.append(make_node("VEG_on_brick", "VEG_wall", "MAT_brick"))
    leg = _legend(scene)
    pol = policy.derive_policy(leg)
    assert legend.object_id("VEG_on_brick") in pol["editable_ids"]
    assert legend.material_id("MAT_brick") in pol["locked_ids"]


def test_nodes_without_a_usable_layer_are_unlabelled_and_locked(scene):
    scene.objects.append(make_node("Box042", "0", "MAT_default"))
    scene.objects.append(make_node("Teapot", "Furniture_misc", "MAT_default"))
    leg = _legend(scene)
    pol = policy.derive_policy(leg)
    for name in ("Box042", "Teapot"):
        assert legend.object_id(name) in pol["unlabelled_ids"]
        assert legend.object_id(name) in pol["locked_ids"]
    assert {e["class"] for e in leg["entries"] if e["name"] == "Box042"} == {"other"}


def test_every_editable_prefix_maps_to_an_editable_class():
    for prefix in policy.EDITABLE_PREFIXES:
        cls, source = policy.classify_object(prefix + "x", [])
        assert source == "rule"
        assert cls not in policy.NEVER_EDITABLE_CLASSES, prefix


def test_never_editable_classes_match_the_spec():
    assert set(policy.NEVER_EDITABLE_CLASSES) == set(plate_spec.NEVER_EDITABLE)


def test_lint_blocks_over_the_share_and_passes_under_it(scene):
    scene.objects.append(make_node("Box042", "0", "MAT_default"))
    pol = policy.derive_policy(_legend(scene))
    box = legend.object_id("Box042")
    blocked = policy.lint(pol, {box: 0.08})
    assert len(blocked) == 1 and "8.0%" in blocked[0]
    assert policy.lint(pol, {box: 0.02}) == []
    assert policy.lint(pol, {box: 0.08}, max_unlabelled_share=0.10) == []


def test_lint_blocks_overlap_between_locked_and_editable(scene):
    pol = policy.derive_policy(_legend(scene))
    pol["locked_ids"].append(pol["editable_ids"][0])
    problems = policy.lint(pol, {})
    assert len(problems) == 1 and "both locked and editable" in problems[0]


@pytest.mark.parametrize("tag", policy.NEVER_EDITABLE_TAGS)
def test_each_never_editable_tag_locks_an_editable_layer(tag):
    cls, source = policy.classify_object("SKY", [tag.upper()])
    assert cls == tag and source in ("rule", "manual")


# --------------------------------------------------------------------------- decision 81 checks


def test_duplicate_node_names_list_every_layer(scene):
    scene.objects.append(make_node("VEG_tree_01", "ARCH_walls", "MAT_brick"))
    scene.objects.append(make_node("VEG_tree_01", None, "MAT_leaf"))
    [problem] = policy.duplicate_node_problems(digest.scene_nodes())
    assert problem["code"] == policy.DUPLICATE_NODE_NAME and problem["node"] == "VEG_tree_01"
    assert problem["detail"].startswith(policy.duplicate_node_text("VEG_tree_01") + ": 3 geometry nodes")
    assert "'VEG_tree_01', 'ARCH_walls', no layer" in problem["detail"]


def test_unique_names_are_no_problem(scene):
    assert policy.duplicate_node_problems(digest.scene_nodes()) == []
    assert policy.duplicate_camera_problems(["CAM_a", "CAM_b"]) == []
    assert [p["node"] for p in policy.duplicate_camera_problems(["CAM_a", "CAM_b", "CAM_a"])] == ["CAM_a"]


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("CAM_front", False),
        ("Cam 1", False),
        ("CAM_\U0001f3e0", False),
        ("CAM:1", False),  # BAD_NAME_CHARS' business, not the slug's
        ("\u5ba2\u6237A", True),
        ("CAM_caf\u00e9", True),
        ("   ", True),
        ("", True),
        ("\U0001f3a5", True),
    ],
)
def test_shot_slug_problems(name, bad):
    problems = policy.shot_slug_problems(name)
    assert bool(problems) is bad
    assert all(p["code"] == policy.BAD_SHOT_SLUG and p["node"] == name for p in problems)


def test_lens_value_problems_name_the_property_and_skip_unread_values():
    cam = {
        "name": "C",
        "focal_mm": 24.0,
        "film_gate_mm": None,
        "zoom_factor": 1.0,
        "f_number": float("inf"),
        "exposure": {"iso": -100.0, "shutter_s": None},
        "read_from": {"f_number": "fnumber", "exposure.iso": "ISO"},
    }
    problems = policy.lens_value_problems(cam)
    assert [p["code"] for p in problems] == [policy.BAD_LENS, policy.BAD_LENS]
    assert "f-number fnumber is inf, not a finite number" in problems[0]["detail"]
    assert "ISO ISO is -100, not positive" in problems[1]["detail"]
    assert policy.lens_value_problems({"name": "C"}) == []


def test_a_non_finite_pose_is_bad_pose_and_only_that():
    m = [[1.0, 0.0, 0.0, float("nan")], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0, 0, 0, 1]]
    problems, _ = policy.camera_checks({"name": "C", "world_from_camera": m})
    assert [p["code"] for p in problems] == [policy.BAD_POSE]
    assert "position (row4) x" in problems[0]["detail"]
    scaled = [[2.0, 0.0, 0.0, 0.0], [0.0, 2.0, 0.0, 0.0], [0.0, 0.0, 2.0, 0.0], [0, 0, 0, 1]]
    assert [p["code"] for p in policy.camera_checks({"name": "C", "world_from_camera": scaled})[0]] == [
        policy.SCALED_CAMERA
    ]
