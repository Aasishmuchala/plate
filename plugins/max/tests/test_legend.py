"""The id_legend builder: spec/PACKET.md 4.3 shape, digests, the duplicate-name lint and the planar
class rule (decision 43) with its plate_planar override."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from fake_pymxs import make_node

import plate_spec
from plate_max import autotag, digest, legend, policy
from plate_max.packet_id import PACKET_ID_RE

sha = lambda data: hashlib.sha256(data).hexdigest()  # noqa: E731 (stand-in for BLAKE3: shape only)


def test_entries_follow_the_schema_shape(scene):
    leg = legend.build_id_legend(digest.scene_nodes(), digest_fn=sha)
    assert PACKET_ID_RE.match(leg["packet_id"])
    objects = [e for e in leg["entries"] if e["kind"] == "object"]
    materials = [e for e in leg["entries"] if e["kind"] == "material"]
    assert len(objects) == 6 and len(materials) == 6
    sky = next(e for e in objects if e["name"] == "SKY")
    assert sky == {
        "id": "o:8c06691f",
        "kind": "object",
        "name": "SKY",
        "layer": "SKY",
        "material": "MAT_sky",
        "tags": [],
        "class": "sky",
        "class_source": "rule",
        "pixel_share": 0.0,
        "digest": sky["digest"],
    }
    assert len(sky["digest"]) == 64
    assert plate_spec._schema_errors(leg, "id_legend_file") == []


def test_digest_tracks_the_node_record(scene):
    before = {
        e["id"]: e["digest"] for e in legend.build_id_legend(digest.scene_nodes(), digest_fn=sha)["entries"]
    }
    scene.objects[2].objecttransform.row4 = (5.0, 0.0, 0.0)
    after = {
        e["id"]: e["digest"] for e in legend.build_id_legend(digest.scene_nodes(), digest_fn=sha)["entries"]
    }
    changed = {i for i in before if before[i] != after[i]}
    assert changed == {legend.object_id("VEG_tree_01")}


def test_a_digest_is_mandatory(scene):
    with pytest.raises(legend.LegendError, match="digest"):
        legend.build_id_legend(digest.scene_nodes())
    # per-node digests are not enough: materials have no node record, so they need digest_fn too
    with pytest.raises(legend.LegendError, match="digest"):
        legend.build_id_legend([{**n, "digest": "a" * 64} for n in digest.scene_nodes()])


def test_duplicate_visible_node_names_block_send(scene):
    scene.objects.append(make_node("VEG_tree_01", "VEG_other", "MAT_leaf"))
    with pytest.raises(legend.LegendError, match="duplicate visible node name 'VEG_tree_01'"):
        legend.build_id_legend(digest.scene_nodes(), digest_fn=sha)


def test_the_legend_and_the_lint_use_one_text_for_a_duplicate_name(scene):
    scene.objects.append(make_node("SKY", "ARCH_walls", "MAT_brick"))
    with pytest.raises(legend.LegendError) as caught:
        legend.build_id_legend(digest.scene_nodes(), digest_fn=sha)
    assert str(caught.value) == policy.duplicate_node_text("SKY")
    [problem] = policy.duplicate_node_problems(digest.scene_nodes())
    assert problem["detail"].startswith(str(caught.value))


def test_legend_nodes_refuses_a_duplicate_instead_of_letting_the_last_win(scene):
    from plate_max.actions import send_packet
    from plate_max.exr_header import Coverage

    scene.objects.append(make_node("ARCH_walls", "VEG_tree_01", "MAT_leaf"))  # editable twin, last
    shares = {legend.cryptomatte_hash(n): 0.1 for n in ("ARCH_walls", "SKY")}
    coverage = Coverage(layer="CryptoObject", shares=shares, pixels=100, compression="zip")
    manifest = {n: legend.cryptomatte_hash(n) for n in ("ARCH_walls", "SKY")}
    with pytest.raises(send_packet.SendAbort) as caught:
        send_packet.legend_nodes(manifest, coverage, digest.scene_nodes())
    assert caught.value.stage == "lint" and caught.value.problems[0]["code"] == policy.DUPLICATE_NODE_NAME
    scene.objects.pop()
    nodes, _ = send_packet.legend_nodes(manifest, coverage, digest.scene_nodes())
    assert {n["name"]: n["layer"] for n in nodes} == {"ARCH_walls": "ARCH_walls", "SKY": "SKY"}


def test_two_materials_sharing_a_name_block_send(scene):
    clone = SimpleNamespace(name="MAT_leaf")  # a different material object with the same name
    scene.objects.append(make_node("VEG_tree_02", "VEG_tree_02", clone))
    with pytest.raises(legend.LegendError, match="duplicate visible material name 'MAT_leaf'"):
        legend.build_id_legend(digest.scene_nodes(), digest_fn=sha)


def test_one_material_on_two_nodes_is_one_entry(scene):
    scene.objects.append(make_node("VEG_tree_02", "VEG_tree_02", "MAT_leaf"))
    leg = legend.build_id_legend(digest.scene_nodes(), digest_fn=sha)
    assert [e["name"] for e in leg["entries"]].count("MAT_leaf") == 1


def test_optional_fields_pass_through(scene):
    nodes = [
        {**n, "pixel_share": 0.1, "planar": n["name"] == "ARCH_walls", "detail_size_m": 0.065}
        for n in digest.scene_nodes()
    ]
    leg = legend.build_id_legend(nodes, packet_id="pkt_01JA7Q3M8Z4X6V2C9D5E1F0GHK", digest_fn=sha)
    arch = next(e for e in leg["entries"] if e["name"] == "ARCH_walls")
    assert arch["planar"] is True and arch["detail_size_m"] == 0.065
    assert leg["packet_id"] == "pkt_01JA7Q3M8Z4X6V2C9D5E1F0GHK"
    assert "planar" in next(e for e in leg["entries"] if e["name"] == "SKY")  # False is still recorded


# --------------------------------------------------------------------------- planar (decision 43)


def _nodes(*specs):
    """[(name, layer, share, extra), ...] as legend input nodes on one shared material each."""
    return [
        {"name": n, "layer": layer, "material": f"MAT_{n}", "tags": [], "pixel_share": share, **extra}
        for n, layer, share, extra in specs
    ]


def test_planar_classes_match_the_gate():
    gates = pytest.importorskip("plate_gates.invention")
    assert policy.PLANAR_CLASSES == gates.PLANAR_CLASSES
    assert policy.PLANAR_MIN_SHARE == 0.005


def test_large_architecture_ground_and_glazing_entries_are_planar():
    nodes = _nodes(
        ("ARCH_wall", "ARCH_walls", 0.20, {}),
        ("GRND_terrace", "GRND_terrace", 0.05, {}),
        ("HARD_paving", "HARD_paving", 0.005, {}),  # exactly at the floor counts
        ("GLZ_pane", "GLZ_south", 0.03, {}),
        ("ARCH_bolt", "ARCH_details", 0.0049, {}),  # a sliver: no planar region to scan
        ("VEG_tree", "VEG_trees", 0.30, {}),
        ("SKY_dome", "SKY", 0.25, {}),
        ("Box001", "0", 0.02, {}),
    )
    leg = legend.build_id_legend(nodes, "pkt_01JA7Q3M8Z4X6V2C9D5E1F0GHK", digest_fn=sha)
    objects = {e["name"]: e for e in leg["entries"] if e["kind"] == "object"}
    assert {n for n, e in objects.items() if e.get("planar") is True} == {
        "ARCH_wall",
        "GRND_terrace",
        "HARD_paving",
        "GLZ_pane",
    }
    assert objects["GRND_terrace"]["class"] == "ground" and objects["GRND_terrace"]["class_source"] == "rule"
    for name in ("ARCH_bolt", "VEG_tree", "SKY_dome", "Box001"):
        assert "planar" not in objects[name], name
    assert not any("planar" in e for e in leg["entries"] if e["kind"] == "material")
    assert plate_spec._schema_errors(leg, "id_legend_file") == []


def test_a_node_value_beats_the_rule_both_ways():
    nodes = _nodes(
        ("ARCH_relief", "ARCH_walls", 0.20, {"planar": False}),
        ("ENT_table_top", "ENT_furniture", 0.04, {"planar": True}),
    )
    leg = legend.build_id_legend(nodes, digest_fn=sha)
    objects = {e["name"]: e for e in leg["entries"] if e["kind"] == "object"}
    assert objects["ARCH_relief"]["planar"] is False and objects["ENT_table_top"]["planar"] is True


def test_the_planar_rule_never_changes_a_digest():
    plain = legend.build_id_legend(_nodes(("ARCH_wall", "ARCH_walls", 0.2, {})), "pkt_X", digest_fn=sha)
    forced = legend.build_id_legend(
        _nodes(("ARCH_wall", "ARCH_walls", 0.2, {"planar": False})), "pkt_X", digest_fn=sha
    )
    assert [e["digest"] for e in plain["entries"]] == [e["digest"] for e in forced["entries"]]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("false", False),
        ("False", False),
        (" no ", False),
        ("0", False),
        ("off", False),
        ("true", True),
        ("YES", True),
        ('"true"', True),
        ("1", True),
        (True, True),
        (False, False),
        (0, False),
        (1, True),
        (2, None),
        ("maybe", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_planar(raw, expected):
    assert autotag.parse_planar(raw) is expected


def test_planar_overrides_read_the_user_property(clubhouse):
    clubhouse.getNodeByName("ARCH_wall_north").user_props["plate_planar"] = "false"
    clubhouse.getNodeByName("VEG_tree_01").user_props["plate_planar"] = True
    clubhouse.getNodeByName("GLZ_door_s").user_props["plate_planar"] = "perhaps"  # ignored
    names = ["ARCH_wall_north", "VEG_tree_01", "GLZ_door_s", "ARCH_wall_south", "ForestPack_item_7"]
    assert autotag.planar_overrides(names) == {"ARCH_wall_north": False, "VEG_tree_01": True}
    assert autotag.planar_property(None) is None


def test_scene_with_arch_grnd_glz_layers_and_one_override(clubhouse):
    """The Send path: scene nodes with their render shares, overrides merged by legend_nodes."""
    from fake_pymxs import make_node

    from plate_max.actions import send_packet
    from plate_max.exr_header import Coverage

    clubhouse.objects.append(make_node("GRND_lawn_edge", "GRND_edges", "MAT_stone"))
    clubhouse.getNodeByName("GLZ_curtainwall_s").user_props["plate_planar"] = "false"
    shares = {**clubhouse.pixel_shares, "GRND_lawn_edge": 0.02}
    manifest = {name: legend.cryptomatte_hash(name) for name in shares}
    coverage = Coverage(
        layer="CryptoObject",
        shares={legend.cryptomatte_hash(n): s for n, s in shares.items()},
        pixels=160 * 90,
        compression="zip",
    )
    nodes, _ = send_packet.legend_nodes(manifest, coverage, digest.scene_nodes())
    leg = legend.build_id_legend(nodes, digest_fn=sha)
    objects = {e["name"]: e for e in leg["entries"] if e["kind"] == "object"}
    planar = {n for n, e in objects.items() if e.get("planar") is True}
    expected = {
        n for n, e in objects.items() if e["class"] in policy.PLANAR_CLASSES and e["pixel_share"] >= 0.005
    } - {"GLZ_curtainwall_s"}
    assert planar == expected and "GRND_lawn_edge" in planar and "ARCH_wall_north" in planar
    assert objects["GLZ_curtainwall_s"]["planar"] is False
    assert {objects[n]["class"] for n in planar} == {"architecture", "ground", "glazing"}
