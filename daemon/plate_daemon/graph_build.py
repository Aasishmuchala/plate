"""Pure graph builders for templates and batch chains (router plan T95).

A template (`plate_daemon.templates`) is `{id, title, nodes: [{suffix, kind, label?, params?, inputs?}], set}`.
`chain_for_packet(graph, packet_summary, *, template, ...)` lays one packet's chain of it into a project graph
and returns the new graph; the input graph is never touched and nothing here reads a file, the index or a
clock. `apply_template` does that for several packets, column by column, and adds the one Set node a
`set: true` template ends with. The routes (`routes/templates.py`) store the result through
`PUT /projects/{id}/graph`'s own validation, so the canvas rules stay in `canvas_rules`, not here.

- The Plate of a packet is made once: a Plate node already bound to the packet (`packetId`) is reused, wherever
  it sits and whether or not the template lists a Plate, and is not counted as added (as `cli._graph_for` does).
  A new one is `{kind: "Plate", packetId, tier, label: "Plate"}`.
- A node id is `<kind lower>_<camera slug>_<n>` (`edit_cam_a_1`), `n` the first number that is free in the
  graph; the Set node has no camera and is `set_<n>`. The camera slug is the packet camera's name lowered, runs
  of anything but letters and digits turned to `_` (`CAM_a` -> `cam_a`).
- Positions: `x = column * 340`, `y = row * 180`; a chain is one column, its rows the template's order (the
  Plate is row 0). An existing node keeps the place it has.
- `inputs: {port: ref}` becomes one edge per entry that names both ports (`sourceHandle`, `targetHandle`), as
  the UI's `checkHandles` stores them: `"$plate"` is the packet Plate's `image` output, `"$packet"` its
  `packet` output, any other ref the first output of the template node with that suffix.
- A Mask node's `params.ids` are the packet's legend ids whose class is in `params.classes`, minus what the
  packet locks and, when its policy lists any editable id, minus what it does not list (the stricter side wins;
  `cli._mask_ids` reads the same rule). The node keeps `classes` for the editor. A packet with no such id
  leaves `ids` empty: that Mask quotes as `mask_empty` until the artist paints one.
- `engine_id`, `recipe`, `prompt` and `task` prefill the Edit's `params` (`engine`, `recipe`, `prompt`,
  `task`, which is where `Pipeline.quote_node` reads them); anything the template's own Edit params said gives way.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import canvas_rules

__all__ = [
    "COLUMN_W",
    "PLATE_REFS",
    "ROW_H",
    "Applied",
    "GraphBuildError",
    "apply_template",
    "camera_slug",
    "chain_for_packet",
    "mask_ids",
    "next_column",
    "slug",
]

COLUMN_W = 340
ROW_H = 180
# A ref that names the packet's Plate rather than a template node: ref -> the Plate output it means.
PLATE_REFS = {"$plate": "image", "$packet": "packet"}
# The Edit params the caller's arguments prefill: argument -> params key.
EDIT_PREFILL = (("engine_id", "engine"), ("recipe", "recipe"), ("prompt", "prompt"), ("task", "task"))
_SLUG_RUN = re.compile(r"[^a-z0-9]+")


class GraphBuildError(ValueError):
    """A template that cannot be laid into a graph (the routes answer 422)."""


@dataclass
class Chain:
    """What one packet's chain added."""

    plate: str  # the packet's Plate node id (new or reused)
    plate_is_new: bool
    added: list[str] = field(default_factory=list)  # new node ids, build order, a new Plate first
    by_suffix: dict[str, str] = field(default_factory=dict)  # template suffix -> node id
    edits: list[str] = field(default_factory=list)  # new Edit node ids


@dataclass
class Applied:
    """`apply_template`'s answer: the new graph and what it holds that the old one did not."""

    graph: dict[str, Any]
    added: list[str] = field(default_factory=list)
    edits: list[str] = field(default_factory=list)
    plates: dict[str, str] = field(default_factory=dict)  # packet id -> Plate node id
    set_id: str | None = None


def slug(text: Any) -> str:
    """`CAM_a` -> `cam_a`: lowercase, runs of anything but letters and digits become one `_`."""
    return _SLUG_RUN.sub("_", str(text or "").lower()).strip("_")


def camera_slug(summary: Mapping[str, Any]) -> str:
    """The slug of the packet's camera name, else of its shot id, else of its packet id."""
    camera = summary.get("camera")
    name = camera.get("name") if isinstance(camera, Mapping) else None
    return slug(name) or slug(summary.get("shot_id")) or slug(summary.get("packet_id")) or "cam"


def mask_ids(summary: Mapping[str, Any], classes: Sequence[str]) -> list[str]:
    """Legend ids of `classes` that the packet lets an edit touch (see the module docstring)."""
    entries = [e for e in ((summary.get("legend") or {}).get("entries") or []) if isinstance(e, Mapping)]
    any_editable = any(e.get("editable") for e in entries)
    return [
        str(e["id"])
        for e in entries
        if e.get("class") in classes
        and not e.get("locked")
        and (e.get("editable") or not any_editable)
        and e.get("id") is not None
    ]


def _position(node: Mapping[str, Any]) -> tuple[float, float] | None:
    top = node.get("position")
    if not isinstance(top, Mapping) and isinstance(node.get("data"), Mapping):
        top = node["data"].get("position")
    if not isinstance(top, Mapping):
        return None
    x, y = top.get("x"), top.get("y")
    if isinstance(x, (int, float)) and isinstance(y, (int, float)) and not isinstance(x, bool):
        return float(x), float(y)
    return None


def next_column(graph: Mapping[str, Any]) -> int:
    """The first column to the right of every positioned node (0 for an empty or unpositioned graph)."""
    columns = [round(p[0] / COLUMN_W) for n in graph.get("nodes") or [] if (p := _position(n)) is not None]
    return max(columns) + 1 if columns else 0


def _new_id(kind: str, cam: str | None, taken: set[str]) -> str:
    base = f"{kind.lower()}_{cam}" if cam else kind.lower()
    n = 1
    while f"{base}_{n}" in taken:
        n += 1
    nid = f"{base}_{n}"
    taken.add(nid)
    return nid


def _plate_node(graph: Mapping[str, Any], packet_id: str) -> str | None:
    for n in graph.get("nodes") or []:
        d = canvas_rules.node_data(n)
        if d.get("kind") == "Plate" and d.get("packetId") == packet_id:
            return str(d["id"])
    return None


def _edge(source: str, source_handle: str, target: str, target_handle: str) -> dict[str, Any]:
    return {
        "id": f"{source}.{source_handle}->{target}.{target_handle}",
        "source": source,
        "sourceHandle": source_handle,
        "target": target,
        "targetHandle": target_handle,
    }


def _first_output(kind: str) -> str:
    outputs = (canvas_rules.PORTS.get(kind) or {}).get("outputs") or ()
    if not outputs:
        raise GraphBuildError(f"a {kind} node has no output to take an input from")
    return str(outputs[0][0])


def _template_nodes(template: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    nodes = template.get("nodes")
    if not isinstance(nodes, list) or not nodes or not all(isinstance(n, Mapping) for n in nodes):
        raise GraphBuildError(f"template {template.get('id')!r} needs a list of nodes")
    return nodes


def _empty(graph: Mapping[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(dict(graph)) if isinstance(graph, Mapping) else {}
    if not isinstance(out.get("nodes"), list):
        out["nodes"] = []
    if not isinstance(out.get("edges"), list):
        out["edges"] = []
    return out


def build_chain(
    graph: Mapping[str, Any],
    packet_summary: Mapping[str, Any],
    *,
    template: Mapping[str, Any],
    engine_id: str | None = None,
    recipe: str | None = None,
    prompt: str | None = None,
    task: str | None = None,
    column: int = 0,
) -> tuple[dict[str, Any], Chain]:
    """`chain_for_packet`, with what it added as well."""
    packet_id = packet_summary.get("packet_id")
    if not isinstance(packet_id, str) or not packet_id:
        raise GraphBuildError("the packet summary names no packet_id")
    entries = _template_nodes(template)
    out = _empty(graph)
    nodes, edges = out["nodes"], out["edges"]
    taken = {str(n.get("id")) for n in nodes if isinstance(n, Mapping)}
    cam = camera_slug(packet_summary)
    x = column * COLUMN_W
    # the Plate takes row 0; a template that lists no Plate starts its own nodes one row down
    offset = 0 if any(e.get("kind") == "Plate" for e in entries) else 1

    plate_id = _plate_node(out, packet_id)
    chain = Chain(plate=plate_id or "", plate_is_new=plate_id is None)
    if plate_id is None:
        plate_id = chain.plate = _new_id("Plate", cam, taken)
        nodes.append(
            {
                "id": plate_id,
                "kind": "Plate",
                "label": "Plate",
                "packetId": packet_id,
                "tier": packet_summary.get("tier"),
                "position": {"x": x, "y": 0},
            }
        )
        chain.added.append(plate_id)

    def source_of(ref: str, where: str) -> tuple[str, str]:
        if ref in PLATE_REFS:
            return plate_id, PLATE_REFS[ref]
        if ref not in chain.by_suffix:
            raise GraphBuildError(f"{where}: input {ref!r} names no earlier node of the template")
        nid = chain.by_suffix[ref]
        kind = next(e["kind"] for e in entries if e.get("suffix") == ref)
        return nid, _first_output(str(kind))

    for row, entry in enumerate(entries, start=offset):
        kind, suffix = entry.get("kind"), entry.get("suffix")
        if not isinstance(kind, str) or not isinstance(suffix, str) or not suffix:
            raise GraphBuildError(f"template {template.get('id')!r}: every node needs a kind and a suffix")
        if kind == "Plate":
            chain.by_suffix[suffix] = plate_id
            continue
        nid = _new_id(kind, cam, taken)
        node: dict[str, Any] = {
            "id": nid,
            "kind": kind,
            "label": entry.get("label") or kind,
            "position": {"x": x, "y": row * ROW_H},
        }
        params = copy.deepcopy(dict(entry.get("params") or {}))
        if kind == "Mask":
            classes = [str(c) for c in params.get("classes") or []]
            if classes:
                params["ids"] = mask_ids(packet_summary, classes)
        elif kind == "Edit":
            given = {"engine_id": engine_id, "recipe": recipe, "prompt": prompt, "task": task}
            for arg, key in EDIT_PREFILL:
                if given[arg] is not None:
                    params[key] = given[arg]
            chain.edits.append(nid)
        if params:
            node["params"] = params
        nodes.append(node)
        chain.by_suffix[suffix] = nid
        chain.added.append(nid)
        for port, ref in (entry.get("inputs") or {}).items():
            src, handle = source_of(str(ref), f"{kind} {suffix}")
            edges.append(_edge(src, handle, nid, str(port)))
    return out, chain


def chain_for_packet(
    graph: Mapping[str, Any],
    packet_summary: Mapping[str, Any],
    *,
    template: Mapping[str, Any],
    engine_id: str | None = None,
    recipe: str | None = None,
    prompt: str | None = None,
    task: str | None = None,
    column: int = 0,
) -> dict[str, Any]:
    """`graph` plus the chain `template` builds for the packet `packet_summary` (a `GET /packets/{id}/summary`
    answer) in column `column`. Pure: the argument is not modified."""
    return build_chain(
        graph,
        packet_summary,
        template=template,
        engine_id=engine_id,
        recipe=recipe,
        prompt=prompt,
        task=task,
        column=column,
    )[0]


def apply_template(
    graph: Mapping[str, Any],
    packet_summaries: Sequence[Mapping[str, Any]],
    *,
    template: Mapping[str, Any],
    engine_id: str | None = None,
    recipe: str | None = None,
    prompt: str | None = None,
    task: str | None = None,
    column: int | None = None,
) -> Applied:
    """One chain per packet, one column each from `column` (default: right of everything in `graph`); a
    `set: true` template then gets one Set node, a column further right, fed by every new Edit."""
    first = next_column(graph) if column is None else column
    current: dict[str, Any] = _empty(graph)
    applied = Applied(graph=current)
    rows = {e.get("kind"): i for i, e in enumerate(_template_nodes(template))}
    for i, summary in enumerate(packet_summaries):
        current, chain = build_chain(
            current,
            summary,
            template=template,
            engine_id=engine_id,
            recipe=recipe,
            prompt=prompt,
            task=task,
            column=first + i,
        )
        applied.plates[str(summary["packet_id"])] = chain.plate
        applied.added.extend(chain.added)
        applied.edits.extend(chain.edits)
    if template.get("set") and applied.edits:
        taken = {str(n.get("id")) for n in current["nodes"] if isinstance(n, Mapping)}
        set_id = _new_id("Set", None, taken)
        has_plate = any(e.get("kind") == "Plate" for e in _template_nodes(template))
        row = rows.get("Edit", 0) + (0 if has_plate else 1)
        current["nodes"].append(
            {
                "id": set_id,
                "kind": "Set",
                "label": "Set",
                "position": {"x": (first + len(packet_summaries)) * COLUMN_W, "y": row * ROW_H},
            }
        )
        for edit_id in applied.edits:
            current["edges"].append(_edge(edit_id, _first_output("Edit"), set_id, "views"))
        applied.added.append(set_id)
        applied.set_id = set_id
    applied.graph = current
    return applied
