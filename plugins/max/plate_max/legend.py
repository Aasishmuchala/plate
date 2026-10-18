"""Cryptomatte ids and the id_legend.json builder.

murmur3_32 is MurmurHash3_x86_32 (seed 0), the hash Cryptomatte stores per object or material
name; cryptomatte_hash adds the Cryptomatte exponent fix so the float32 id is never denormal, inf
or NaN. The legend format is spec/PACKET.md 4.3 (schema #/$defs/id_legend_file). Pure stdlib, so it
runs in Max's Python; plate_spec.cryptomatte_id is the reference it is tested against.

Each entry's id is the hash of its name and nothing else, so two visible nodes (or two different
materials) with one name would merge into one ID with one class: that is a LegendError, not a
warning. Send refuses such a scene before it renders (policy.lint_scene, DUPLICATE_NODE_NAME, whose
detail starts with the same words, policy.duplicate_node_text) and again when it builds the legend
inputs (actions.send_packet.legend_nodes), so this check is the last net. Entries also carry a digest, the BLAKE3 of the node's or material's record; BLAKE3 is not
in the standard library, so the caller passes the hash function.

planar (decision 43) is a sender-side class rule: an object entry of class architecture, ground or
glazing (policy.PLANAR_CLASSES) whose pixel share is at least policy.PLANAR_MIN_SHARE gets
planar: true. A node that carries "planar" (the plate_planar user property, read by
autotag.planar_overrides) keeps its own value instead, true or false. Materials are never planar.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from typing import Any

from . import policy as policy_mod
from .fsutil import canonical_json_bytes
from .packet_id import new_packet_id


class LegendError(ValueError):
    """The scene cannot be turned into a legend (duplicate names, a missing digest): blocks Send."""


def murmur3_32(data: bytes, seed: int = 0) -> int:
    c1, c2 = 0xCC9E2D51, 0x1B873593
    h = seed & 0xFFFFFFFF
    nblocks = len(data) // 4
    for i in range(nblocks):
        (k,) = struct.unpack_from("<I", data, i * 4)
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
        h = ((h << 13) | (h >> 19)) & 0xFFFFFFFF
        h = (h * 5 + 0xE6546B64) & 0xFFFFFFFF
    tail = data[nblocks * 4 :]
    k = 0
    if len(tail) >= 3:
        k ^= tail[2] << 16
    if len(tail) >= 2:
        k ^= tail[1] << 8
    if len(tail) >= 1:
        k ^= tail[0]
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
    h ^= len(data)
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return h


def cryptomatte_hash(name: str) -> str:
    """8 lowercase hex: murmur3_32 of the UTF-8 name, bit 23 flipped when the float exponent is 0 or 255."""
    h = murmur3_32(name.encode("utf-8"))
    exponent = (h >> 23) & 0xFF
    if exponent in (0, 255):
        h ^= 1 << 23
    return f"{h:08x}"


def object_id(name: str) -> str:
    return f"o:{cryptomatte_hash(name)}"


def material_id(name: str) -> str:
    return f"m:{cryptomatte_hash(name)}"


def _restrictive_class(classes: list[str]) -> str:
    for cls in policy_mod.NEVER_EDITABLE_CLASSES:
        if cls in classes:
            return cls
    return classes[0]


def planar_by_rule(entry: dict[str, Any]) -> bool:
    """The planar class rule for one object entry (its class and recorded pixel_share)."""
    return (
        entry.get("kind") == "object"
        and entry.get("class") in policy_mod.PLANAR_CLASSES
        and float(entry.get("pixel_share") or 0.0) >= policy_mod.PLANAR_MIN_SHARE
    )


def node_record(node: dict[str, Any], layer: str | None, tags: list[str]) -> dict[str, Any]:
    """The interim (digest_version "0") record a node's digest is taken over: its name, layer,
    material, tags and world transform. Mesh topology and shader parameters join in version 1."""
    return {
        "kind": "object",
        "name": str(node["name"]),
        "layer": layer,
        "material": node.get("material"),
        "tags": tags,
        "transform": node.get("transform"),
    }


def _node_layer(node: dict[str, Any]) -> str | None:
    layer = node.get("layer")
    return str(layer) if layer is not None else None


def _node_tags(node: dict[str, Any]) -> list[str]:
    return sorted({str(t).lower() for t in node.get("tags") or ()})


def record_bytes(node: dict[str, Any]) -> bytes:
    """The canonical bytes an object entry's digest is taken over (what digest_fn is handed for this
    node in build_id_legend): Send checks their size before it renders."""
    return canonical_json_bytes(node_record(node, _node_layer(node), _node_tags(node)))


def build_id_legend(
    nodes: list[dict[str, Any]],
    packet_id: str | None = None,
    digest_fn: Callable[[bytes], str] | None = None,
) -> dict[str, Any]:
    """id_legend.json content for the VISIBLE nodes [{name, material, layer, tags, pixel_share?,
    planar?, detail_size_m?, transform?, material_uid?, digest?}].

    One object entry per node and one material entry per distinct material. pixel_share is the
    fraction of the frame the id covers at rank 0; it comes from the render (the Cryptomatte EXR),
    not from Max, so the caller passes it per node (default 0.0). A material's share is the sum over
    the objects that use it and its class is the most restrictive among them. digest_fn(bytes) -> 64
    hex BLAKE3 is applied to each canonical record unless the node already carries a "digest".
    planar: the node's own value when it has one (True or False is recorded), else true by
    planar_by_rule, else absent. Raises LegendError for duplicate names, two materials sharing a
    name, or a missing digest.
    """
    entries: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    materials: dict[str, list[dict[str, Any]]] = {}
    material_uids: dict[str, set[Any]] = {}

    def claim(entry_id: str, name: str, what: str) -> None:
        if entry_id in seen:
            if seen[entry_id] != name:
                raise LegendError(
                    f"cryptomatte hash collision: {name!r} and {seen[entry_id]!r} both give {entry_id}"
                )
            raise LegendError(
                policy_mod.duplicate_node_text(name)
                if what == "node"
                else f"duplicate visible {what} name {name!r}"
            )
        seen[entry_id] = name

    def digest_of(record: dict[str, Any], given: str | None) -> str:
        if given:
            return given
        if digest_fn is None:
            raise LegendError("legend entries need a digest: pass digest_fn (BLAKE3) or per-node digests")
        return digest_fn(canonical_json_bytes(record))

    for node in nodes:
        name = str(node["name"])
        layer = _node_layer(node)
        tags = _node_tags(node)
        cls, source = policy_mod.classify_object(layer, tags)
        entry: dict[str, Any] = {"id": object_id(name), "kind": "object", "name": name}
        if layer is not None:
            entry["layer"] = layer
        if node.get("material"):
            entry["material"] = str(node["material"])
        entry.update(
            {
                "tags": tags,
                "class": cls,
                "class_source": source,
                "pixel_share": round(float(node.get("pixel_share", 0.0)), 6),
                "digest": digest_of(node_record(node, entry.get("layer"), tags), node.get("digest")),
            }
        )
        if node.get("planar") is not None:
            entry["planar"] = bool(node["planar"])
        elif planar_by_rule(entry):
            entry["planar"] = True
        if node.get("detail_size_m"):
            entry["detail_size_m"] = float(node["detail_size_m"])
        claim(entry["id"], name, "node")
        entries.append(entry)
        if entry.get("material"):
            materials.setdefault(entry["material"], []).append(entry)
            if node.get("material_uid") is not None:
                material_uids.setdefault(entry["material"], set()).add(node["material_uid"])

    for mat_name, users in materials.items():
        if len(material_uids.get(mat_name, ())) > 1:
            raise LegendError(
                f"duplicate visible material name {mat_name!r}: two different materials share it"
            )
        mat_entry = {
            "id": material_id(mat_name),
            "kind": "material",
            "name": mat_name,
            "tags": [],
            "class": _restrictive_class([u["class"] for u in users]),
            "class_source": "autotag",
            "pixel_share": round(min(1.0, sum(u["pixel_share"] for u in users)), 6),
            "digest": digest_of({"kind": "material", "name": mat_name}, None),
        }
        claim(mat_entry["id"], mat_name, "material")
        entries.append(mat_entry)

    return {"packet_id": packet_id or new_packet_id(), "entries": entries}
