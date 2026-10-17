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
