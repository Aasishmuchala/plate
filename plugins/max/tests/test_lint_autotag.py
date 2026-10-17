"""policy.lint (policy checks plus the scene checks) and the auto-tagger."""

from __future__ import annotations

import hashlib

import pytest
from fake_pymxs import FakeCamera, FakeLayer, make_node

import plate_spec
from plate_max import autotag, digest, legend, packet, policy

_digest = lambda data: hashlib.sha256(data).hexdigest()  # noqa: E731 (stand-in for BLAKE3: shape only)


def _clubhouse_legend(runtime, unlabelled_share=None):
    """The clubhouse legend with each node's render-side share; the three unlabelled nodes' shares
    are rescaled to sum to unlabelled_share when given."""
    nodes = digest.scene_nodes()
    shares = dict(runtime.pixel_shares)
    if unlabelled_share is not None:
        bare = [n["name"] for n in nodes if n["layer"] == "0"]
        for name in bare:
            shares[name] = unlabelled_share / len(bare)
    return legend.build_id_legend([{**n, "pixel_share": shares[n["name"]]} for n in nodes], digest_fn=_digest)


def _coverage(leg):
    return {e["id"]: e["pixel_share"] for e in leg["entries"]}


# ---- unlabelled share


def test_clubhouse_unlabelled_share_of_four_percent_passes(clubhouse):
    leg = _clubhouse_legend(clubhouse)
    pol = policy.derive_policy(leg)
    report = policy.lint(pol, _coverage(leg))
    assert report.passed and report == [] and report.problems == []
    assert report.unlabelled_share == pytest.approx(0.04, abs=1e-5)


def test_clubhouse_unlabelled_share_of_eight_percent_fails(clubhouse):
    leg = _clubhouse_legend(clubhouse, unlabelled_share=0.08)
    pol = policy.derive_policy(leg)
    report = policy.lint(pol, _coverage(leg))
    assert not report.passed
    assert report.codes == ["UNLABELLED_SHARE"]
    assert report.unlabelled_share == pytest.approx(0.08, abs=1e-5)
    assert "8.0%" in report[0] and "5.0%" in report[0]
    assert policy.lint(pol, _coverage(leg), max_unlabelled_share=0.10).passed


def test_the_report_keeps_the_old_list_shape_and_adds_the_fields(clubhouse):
    leg = _clubhouse_legend(clubhouse, unlabelled_share=0.08)
    report = policy.lint(policy.derive_policy(leg), _coverage(leg))
    assert isinstance(report, list) and len(report) == 1 and isinstance(report[0], str)
    assert "; ".join(report).startswith("unlabelled IDs cover")  # what packet.build_manifest joins
    assert report["passed"] is False and report["unlabelled_share"] == pytest.approx(0.08, abs=1e-5)
    (problem,) = report["problems"]
    assert set(problem) == {"code", "node", "detail"} and problem["detail"] == report[0]
    assert report.as_dict() == {
        "passed": False,
        "unlabelled_share": pytest.approx(0.08, abs=1e-5),
        "problems": [problem],
    }
    assert [p["code"] for p in report.problems] == report.codes


def test_the_policy_overlap_checks_have_codes(clubhouse):
    leg = _clubhouse_legend(clubhouse)
    pol = policy.derive_policy(leg)
    pol["locked_ids"].append(pol["editable_ids"][0])
    assert policy.lint(pol, {}).codes == ["LOCKED_AND_EDITABLE"]
    pol = policy.derive_policy(leg)
    pol["editable_ids"].append(pol["unlabelled_ids"][0])
    pol["locked_ids"].remove(pol["unlabelled_ids"][0])
    assert policy.lint(pol, {}).codes == ["UNLABELLED_EDITABLE"]


def test_existing_callers_without_the_new_keywords_still_work():
    pol = {"locked_ids": ["o:00000001"], "editable_ids": [], "unlabelled_ids": ["o:00000001"]}
    assert policy.lint(pol, {"o:00000001": 0.01}) == []
    assert policy.lint(pol, {"o:00000001": 0.2}).codes == ["UNLABELLED_SHARE"]
    assert policy.lint(pol, None).passed  # no coverage at all counts as none


# ---- scene checks


def test_a_scaled_camera_is_SCALED_CAMERA(install_scene):
    install_scene("scaled_camera")
    report = policy.lint_scene("CAM_scaled")
    assert report.codes == ["SCALED_CAMERA"]
    assert report.problems[0]["node"] == "CAM_scaled"
    assert "not orthonormal" in report[0]


def test_a_shifted_camera_is_SHIFTED_CAMERA(install_scene):
    install_scene("shifted_camera")
    report = policy.lint_scene("CAM_shifted")
    assert report.codes == ["SHIFTED_CAMERA"]
    assert "lens shift" in report[0]


def test_lint_agrees_with_the_writer_about_shifted_cameras(clubhouse):
    cam = clubhouse.cameras["CAM_front"]
    for attr, value, word in (
        ("horizontal_shift", 0.1, "lens shift"),
        ("vertical_tilt", 2.0, "tilt"),
        ("distortion", 0.03, "distortion"),
    ):
        setattr(cam, attr, value)
        report = policy.lint_scene("CAM_front")
        assert report.codes == ["SHIFTED_CAMERA"] and word in report[0]
        with pytest.raises(packet.ManifestError, match=word):
            packet.build_camera_block(digest.camera_digest("CAM_front"))
        setattr(cam, attr, 0.0)
    assert policy.lint_scene("CAM_front").passed


def test_a_clean_clubhouse_camera_passes_before_any_render(clubhouse):
    report = policy.lint_scene("CAM_front")
    assert report.passed and report == []
    assert report.unlabelled_share is None  # no coverage exists before the render


def test_lint_scene_adds_the_policy_checks_when_coverage_is_known(clubhouse):
    leg = _clubhouse_legend(clubhouse, unlabelled_share=0.08)
    report = policy.lint_scene("CAM_front", policy.derive_policy(leg), _coverage(leg))
    assert report.codes == ["UNLABELLED_SHARE"]


def test_a_hidden_camera_is_HIDDEN_CAMERA(clubhouse):
    clubhouse.cameras["CAM_front"].isHidden = True
    report = policy.lint_scene("CAM_front")
    assert report.codes == ["HIDDEN_CAMERA"] and "hidden" in report[0]
    clubhouse.cameras["CAM_front"].isHidden = False
    clubhouse.cameras["CAM_front"].layer = FakeLayer("CAMERAS", ishidden=True)
    assert policy.lint_scene("CAM_front").codes == ["HIDDEN_CAMERA"]


@pytest.mark.parametrize("renderer", ["Default_Scanline_Renderer", "Arnold", "Corona_10"])
def test_a_non_vray_renderer_is_NO_VRAY(clubhouse, renderer):
    clubhouse.renderers.current.classname = renderer
    report = policy.lint_scene("CAM_front")
    assert report.codes == ["NO_VRAY"] and renderer in report[0]


@pytest.mark.parametrize("renderer", ["V_Ray_7", "V_Ray_7_hotfix_3", "V_Ray_GPU_7", "VRay_6"])
def test_vray_class_names_pass(clubhouse, renderer):
    clubhouse.renderers.current.classname = renderer
    assert policy.lint_scene("CAM_front").passed


def test_an_unreadable_renderer_is_NO_VRAY(clubhouse):
    clubhouse.renderers = None
    report = policy.lint_scene("CAM_front")
    assert report.codes == ["NO_VRAY"] and "unreadable" in report[0]


@pytest.mark.parametrize(
    ("name", "bad"),
    [("ARCH:wall", ":"), ("VEG/tree", "/"), ("HARD\\paving", "\\"), ("a:b/c", ": /")],
)
def test_node_names_that_break_file_names_are_BAD_NAME_CHARS(clubhouse, name, bad):
    clubhouse.objects.append(make_node(name, "ARCH_walls", "MAT_brick"))
    report = policy.lint_scene("CAM_front")
    assert report.codes == ["BAD_NAME_CHARS"]
    assert report.problems[0]["node"] == name
    assert all(repr(c) in report[0] for c in bad.split())


def test_a_bad_camera_name_is_caught_too(clubhouse):
    clubhouse.add_camera(FakeCamera("CAM:front2"))
    assert policy.lint_scene("CAM:front2").codes == ["BAD_NAME_CHARS"]


def test_every_problem_is_reported_not_just_the_first(clubhouse):
    clubhouse.renderers.current.classname = "Arnold"
    clubhouse.cameras["CAM_front"].isHidden = True
    clubhouse.cameras["CAM_front"].vertical_shift = 0.2
    clubhouse.objects.append(make_node("bad:name", "ARCH_walls", "MAT_brick"))
    assert sorted(policy.lint_scene("CAM_front").codes) == [
        "BAD_NAME_CHARS",
        "HIDDEN_CAMERA",
        "NO_VRAY",
        "SHIFTED_CAMERA",
    ]


def test_an_unknown_camera_raises(clubhouse):
    with pytest.raises(digest.CameraNotFound):
        policy.lint_scene("CAM_missing")


def test_pure_lint_runs_only_the_checks_it_is_given():
    assert policy.lint(None, None).passed
    assert policy.lint(None, None, renderer="Arnold").codes == ["NO_VRAY"]
    assert policy.lint(None, None, node_names=["fine", "a:b"]).codes == ["BAD_NAME_CHARS"]
    cam = {"name": "C", "world_from_camera": None, "lens_shift": {"horizontal": 0.0, "vertical": 0.5}}
    assert policy.lint(None, None, camera=cam).codes == ["SHIFTED_CAMERA"]
    assert policy.lint(None, None, camera=cam, camera_hidden=True).codes == [
        "SHIFTED_CAMERA",
        "HIDDEN_CAMERA",
    ]


# ---- the auto-tagger


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Glass_panel", "glazing"),
        ("GLZ_door", "glazing"),
        ("WindowFrame", "glazing"),
        ("tree_oak", "vegetation"),
        ("Palm04", "vegetation"),
        ("shrub_box", "vegetation"),
        ("Grass_patch", "vegetation"),
        ("hedge_long", "vegetation"),
        ("Leaf_cluster", "vegetation"),
        ("SkyDome", "sky"),
        ("dome_light", "sky"),
        ("HDRI_sunset", "sky"),
        ("Person_01", "people"),
        ("people_group", "people"),
        ("human_male", "people"),
        ("Figure_standing", "people"),
        ("Water_surface", "water"),
        ("pool_edge", "water"),
        ("Pond01", "water"),
        ("car_suv", "vehicle"),
        ("Vehicle_parked", "vehicle"),
        ("bike_rack", "vehicle"),
        ("sofa_3seat", "furniture"),
        ("Chair_dining", "furniture"),
        ("table_oak", "furniture"),
        ("Bed_king", "furniture"),
        ("road_main", "ground"),
        ("Paving_stone", "ground"),
        ("ground_plane", "ground"),
        ("floor_lobby", "ground"),
        ("sett_granite", "ground"),
        ("lawn_front", "ground"),
        ("sign_entry", "signage"),
        ("Logo_brand", "signage"),
        ("text_overlay", "signage"),
    ],
)
def test_keywords_map_to_their_class(name, expected):
    cls, confidence = autotag.autotag({"name": name})
    assert cls == expected
    assert 0.0 < confidence <= autotag.MAX_CONFIDENCE


@pytest.mark.parametrize(
    "name", ["Box042", "Teapot01", "carpet_wool", "skylight_unit", "stable", "MAT_default"]
)
def test_no_match_is_other_with_zero_confidence(name):
    assert autotag.autotag({"name": name}) == ("other", 0.0)


def test_words_are_whole_words_not_fragments():
    # carpet is not car, skylight is not sky, the "bed" in "embedded" is not a bed
    for name in ("carpet", "skylight", "embedded", "tablet", "texture", "signature"):
        assert autotag.autotag({"name": name})[0] == "other", name


def test_camel_case_digits_and_plurals_split_into_words():
    assert autotag.tokens("PalmTree3") == ["palm", "tree", "3"]
    assert autotag.tokens("VEG_tree_01") == ["veg", "tree", "01"]
    assert autotag.tokens("Glass001") == ["glass", "001"]
    assert autotag.tokens(None) == []
    assert autotag.autotag({"name": "trees_row"})[0] == "vegetation"
    assert autotag.autotag({"name": "Windows_n"})[0] == "glazing"
    assert autotag.autotag({"name": "glasses"})[0] == "glazing"


def test_name_material_and_layer_all_count():
    assert autotag.autotag({"name": "Obj01", "material": "MAT_glass"})[0] == "glazing"
    assert autotag.autotag({"name": "Obj01", "layer": "Water_features"})[0] == "water"
    assert autotag.autotag({"name": "Obj01", "material": None, "layer": None}) == ("other", 0.0)


def test_agreeing_fields_raise_confidence_and_conflicts_lower_it():
    name_only = autotag.autotag({"name": "tree_01"})[1]
    both = autotag.autotag({"name": "tree_01", "material": "MAT_leaf"})[1]
    assert both > name_only
    conflicted = autotag.autotag({"name": "tree_01", "material": "MAT_glass"})
    assert conflicted[0] == "vegetation" and conflicted[1] < name_only
    # a layer hit outweighs a material hit
    assert autotag.autotag({"name": "x", "layer": "Water", "material": "MAT_glass"})[0] == "water"


def test_ties_break_in_the_priority_order():
    # one name field naming two classes: glazing comes first in CLASS_KEYWORDS
    assert autotag.autotag({"name": "glass_tree"})[0] == "glazing"
    assert autotag.autotag({"name": "pool_chair"})[0] == "water"


def test_autotag_returns_known_schema_classes():
    schema_classes = set(plate_spec.load_schema()["$defs"]["id_class"]["enum"])
    assert {cls for cls, _ in autotag.CLASS_KEYWORDS} <= schema_classes


# ---- applying tags to a legend


def _bare_entry(**kw):
    return {
        "id": "o:00000001",
        "kind": "object",
        "name": "Obj",
        "class": "other",
        "class_source": "unlabelled",
        **kw,
    }


def test_apply_fills_only_unlabelled_entries_and_marks_them_autotag():
    out = autotag.apply_autotag(_bare_entry(name="sofa_big", layer="0"))
    assert (out["class"], out["class_source"]) == ("furniture", "autotag")
    ruled = _bare_entry(name="sofa_big", **{"class": "vegetation", "class_source": "rule"})
    assert autotag.apply_autotag(ruled) == ruled
    manual = _bare_entry(name="sofa_big", **{"class": "signage", "class_source": "manual"})
    assert autotag.apply_autotag(manual) == manual


def test_apply_leaves_weak_and_missing_guesses_unlabelled():
    material_only = _bare_entry(name="Obj", material="MAT_glass")  # 0.6, under the default 0.7
    assert autotag.apply_autotag(material_only) == material_only
    assert autotag.apply_autotag(material_only, min_confidence=0.5)["class"] == "glazing"
    nothing = _bare_entry(name="Box042")
    assert autotag.apply_autotag(nothing) == nothing


def test_apply_returns_a_copy():
    entry = _bare_entry(name="car_01")
    out = autotag.apply_autotag(entry)
    assert out is not entry and entry["class_source"] == "unlabelled"


def test_an_autotag_never_makes_anything_editable(scene):
    scene.objects.append(make_node("tree_ghost", "0", "MAT_default"))
    scene.objects.append(make_node("window_stray", "0", "MAT_default"))
    leg = legend.build_id_legend(digest.scene_nodes(), digest_fn=_digest)
    tagged = autotag.apply_autotag_to_legend(leg)
    by_name = {e["name"]: e for e in tagged["entries"] if e["kind"] == "object"}
    assert (by_name["tree_ghost"]["class"], by_name["tree_ghost"]["class_source"]) == (
        "vegetation",
        "autotag",
    )
    assert by_name["window_stray"]["class"] == "glazing"
    pol = policy.derive_policy(tagged)
    for name in ("tree_ghost", "window_stray"):
        assert by_name[name]["id"] in pol["locked_ids"] and by_name[name]["id"] not in pol["editable_ids"]
    assert policy.lint(pol, {}).passed


def test_a_tagged_legend_still_validates_against_the_schema(clubhouse):
    leg = autotag.apply_autotag_to_legend(_clubhouse_legend(clubhouse))
    assert plate_spec._schema_errors(leg, "id_legend_file") == []


def test_the_clubhouse_unlabelled_names_have_no_keywords(clubhouse):
    leg = autotag.apply_autotag_to_legend(_clubhouse_legend(clubhouse))
    still = [e["name"] for e in leg["entries"] if e["class_source"] == "unlabelled"]
    assert sorted(still) == ["Box042", "Sphere007", "Teapot01"]
