"""The id_legend builder: spec/PACKET.md 4.3 shape, digests, and the duplicate-name lint."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from fake_pymxs import make_node

import plate_spec
from plate_max import digest, legend
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
