"""Canvas rules in Python: the server-side mirror of `ui/src/canvasRules.ts` (doctrine rules 1, 2 and 7).

The UI checks a connection as it is drawn (UX); the daemon is the enforcement point and re-checks every edge
of a graph before `PUT /api/v0/projects/{id}/graph` stores it, and every node before it is quoted. The codes,
the order of the checks and the messages are the TypeScript ones, and `spec/canvas_rules_vectors.json` holds
shared vectors that both suites run (`daemon/tests/test_routes_packets.py` here, vitest's
`canvasRules.vectors.test.ts` there), so a rule that drifts on one side fails a test.

A graph is `project.json.graph`: `{nodes, edges}`. A node is either the wire form (`{"id", "kind", ...data}`,
`ui/src/nodeTypes.ts` `GraphNodeWire`) or `{"id", "data": {...}}`; keys may be camelCase or snake_case (read
shallowly, as the vitest harness does), and missing fields take `makeNodeData`'s defaults. An edge is
`{"id"?, "source", "target"}`; a missing id is `"<source>-><target>"`.

An edge may also carry `sourceHandle` / `targetHandle` (or `source_handle` / `target_handle`); a missing or
null one resolves as the UI's `checkHandles` does (the first output; the first input that takes the edge's
type), through `PORTS`, the mirror of `ui/src/nodes/ports.ts`.

`validate_edge(graph, edge)` checks, in order: UNKNOWN_NODE, CYCLE, ANNOTATION (T81: a Frame is an annotation
and takes no edges), REFERENCE_ONLY (T81: a Reference feeds an engine's `refs`, `elements` or `sky` input only,
never a compositing input), CONCEPT_TO_DELIVERY (the concept tier never reaches Export or Review),
PREVIS_NOT_DELIVERABLE (a previs output, `previs: true` on a node or in its params, never reaches Export,
directly or through other nodes; a Review may show it, it is watermarked PREVIS), NO_PLATE_ANCHOR (a generator
hangs off a Plate: source-view-only; a Reference anchors nothing, so an engine fed one must be anchored through
its other inputs), DEPTH_EXCEEDED (at most two generations from the plate, the target and everything below it),
VIDEO_LOCKED (every still into Motion or CropPair, the end still included, must have passed gates and been
approved; a packet or reference edge is not a still), HIGH_RISK_MOVE (a high or highest risk move needs an
override) and INVENTED_ANGLE (a camera override on an Edit needs an override and the `unseen geometry guessed`
label). `can_accept` refuses a take without a logged full scrub (SCRUB_REQUIRED). `check_nodes` adds what the
TypeScript types make impossible but JSON allows: an unknown kind or a repeated id (UNKNOWN_NODE).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ANNOTATION_KINDS",
    "DELIVERY_KINDS",
    "EXPORT_KINDS",
    "GENERATING_KINDS",
    "MAX_GENERATION_DEPTH",
    "NODE_KINDS",
    "PORTS",
    "REFERENCE_HANDLES",
    "RULE_CODES",
    "UNSEEN_GEOMETRY_LABEL",
    "VIDEO_KINDS",
    "RuleResult",
    "ancestors",
    "anchored_to_plate",
    "as_edge",
    "as_graph",
    "can_accept",
    "check_nodes",
    "effective_labels",
    "generation_depth",
    "is_previs",
    "node_data",
    "upstream_plates",
    "validate_edge",
    "validate_graph",
]

# ui/src/nodeTypes.ts NODE_KINDS, in the same order.
NODE_KINDS = (
    "Plate",
    "Edit",
    "Mask",
    "Comp",
    "Colour",
    "LightMix",
    "Recolour",
    "Gate",
    "Judge",
    "CropPair",
    "Motion",
    "People",
    "Set",
    "Compare",
    "Export",
    "Review",
    "Light",
    "Scout",
    "Concept",
    # Wave 9 (T81): a Reference image for an engine, a Frame annotation, the numeric Atmosphere and Sky.
    "Reference",
    "Frame",
    "Atmosphere",
    "Sky",
)
RULE_CODES = (
    "CONCEPT_TO_DELIVERY",
    "PREVIS_NOT_DELIVERABLE",
    "DEPTH_EXCEEDED",
    "VIDEO_LOCKED",
    "HIGH_RISK_MOVE",
    "NO_PLATE_ANCHOR",
    "INVENTED_ANGLE",
    "CYCLE",
    "UNKNOWN_NODE",
    "SCRUB_REQUIRED",
    "REFERENCE_ONLY",
    "ANNOTATION",
)
# Nodes that make new pixels; each costs a generation of depth.
GENERATING_KINDS = frozenset({"Edit", "Motion", "People", "CropPair"})
MAX_GENERATION_DEPTH = 2
DELIVERY_KINDS = frozenset({"Export", "Review"})
# Where a previs output may not go (T44): delivery proper. A client Review may show a watermarked previs.
EXPORT_KINDS = frozenset({"Export"})
VIDEO_KINDS = frozenset({"Motion", "CropPair"})
UNSEEN_GEOMETRY_LABEL = "unseen geometry guessed"
# Decision 46: the only inputs a Reference may feed. Decision 57: annotation kinds take no edges.
REFERENCE_HANDLES = frozenset({"refs", "elements", "sky"})
ANNOTATION_KINDS = frozenset({"Frame"})
_REFERENCE_ONLY = "a reference feeds an engine's reference input only; it is never composited"
_ANNOTATION = "a Frame is an annotation; it takes no edges"

_Port = tuple[str, str, tuple[str, ...]]  # (handle id, type, accepted types)


def _in(pid: str, ptype: str, *accepts: str) -> _Port:
    return (pid, ptype, accepts or (ptype,))


_IMAGE_OUT = (("image", "image16"),)
# ui/src/nodes/ports.ts PORTS, reduced to what the rules read: per kind, the inputs as (id, type, accepts) and
# the outputs as (id, type), in the TypeScript order. daemon/tests/test_canvas_rules_wave9.py diffs the two.
PORTS: dict[str, dict[str, tuple[Any, ...]]] = {
    "Plate": {"inputs": (), "outputs": (("image", "image16"), ("packet", "packet"))},
    "Edit": {
        "inputs": (_in("image", "image16"), _in("mask", "mask"), _in("refs", "reference")),
        "outputs": _IMAGE_OUT,
    },
    "Mask": {"inputs": (_in("source", "packet", "packet", "image16"),), "outputs": (("mask", "mask"),)},
    "Comp": {"inputs": (_in("base", "image16"), _in("layers", "image16")), "outputs": _IMAGE_OUT},
    "Colour": {"inputs": (_in("image", "image16"), _in("mask", "mask")), "outputs": _IMAGE_OUT},
    "LightMix": {"inputs": (_in("packet", "packet"),), "outputs": _IMAGE_OUT},
    "Recolour": {"inputs": (_in("packet", "packet"), _in("mask", "mask")), "outputs": _IMAGE_OUT},
    "Gate": {
        "inputs": (
            _in("candidate", "image16", "image16", "video"),
            _in("reference", "packet", "packet", "image16"),
        ),
        "outputs": (("report", "report"),),
    },
    "Judge": {
        "inputs": (_in("candidate", "image16", "image16", "video"),),
        "outputs": (("report", "report"),),
    },
    "CropPair": {"inputs": (_in("image", "image16"),), "outputs": (("pair", "image16"),)},
    "Motion": {
        "inputs": (
            _in("image", "image16"),
            _in("end", "image16"),
            _in("packet", "packet"),
            _in("elements", "reference"),
        ),
        "outputs": (("video", "video"),),
    },
    "People": {
        "inputs": (
            _in("image", "image16"),
            _in("packet", "packet"),
            _in("refs", "reference"),
            _in("clip", "video"),
        ),
        "outputs": _IMAGE_OUT,
    },
    "Set": {"inputs": (_in("views", "image16"),), "outputs": (("image", "image16"), ("report", "report"))},
    "Compare": {
        "inputs": (_in("a", "image16", "image16", "video"), _in("b", "image16", "image16", "video")),
        "outputs": (("report", "report"),),
    },
    "Export": {"inputs": (_in("media", "image16", "image16", "video"),), "outputs": ()},
    "Review": {"inputs": (_in("media", "image16", "image16", "video", "report"),), "outputs": ()},
    "Light": {"inputs": (_in("packet", "packet"),), "outputs": (("packet", "packet"),)},
    "Scout": {"inputs": (_in("packet", "packet"),), "outputs": (("packet", "packet"),)},
    "Concept": {"inputs": (_in("image", "image16"),), "outputs": (("image", "image16"),)},
    "Reference": {"inputs": (), "outputs": (("ref", "reference"),)},
    "Frame": {"inputs": (), "outputs": ()},
    "Atmosphere": {"inputs": (_in("packet", "packet"),), "outputs": _IMAGE_OUT},
    "Sky": {"inputs": (_in("packet", "packet"), _in("sky", "reference")), "outputs": _IMAGE_OUT},
}

_CAMEL = re.compile(r"_([a-z])")


@dataclass(frozen=True)
class RuleResult:
    ok: bool
    code: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": True} if self.ok else {"ok": False, "code": self.code, "message": self.message}


def _refuse(code: str, message: str) -> RuleResult:
    return RuleResult(False, code, message)


OK = RuleResult(True)


# ---------------------------------------------------------------------------- the wire form


def _camel(key: str) -> str:
    return _CAMEL.sub(lambda m: m.group(1).upper(), key)


def _camel_keys(obj: Mapping[str, Any]) -> dict[str, Any]:
    return {_camel(str(k)): v for k, v in obj.items()}


def node_data(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A node as `{"id", **PlateNodeData}` with makeNodeData's defaults filled in (pure; no validation)."""
    if not isinstance(raw, Mapping):
        raise ValueError(f"a node must be an object, got {type(raw).__name__}")
    top = _camel_keys(raw)
    fields = _camel_keys(top["data"]) if isinstance(top.get("data"), Mapping) else dict(top)
    kind = fields.get("kind")
    data = {k: v for k, v in fields.items() if k not in ("id", "position", "data")}
    out = {
        "kind": kind,
        "label": kind,
        "generationDepth": 0,
        "outputTier": "concept_unmeasured" if kind == "Concept" else "measured",
        "gateStatus": "none",
        "approved": False,
        "overrides": [],
        "labels": [],
        **data,
    }
    out["id"] = top.get("id")
    return out


def as_edge(raw: Any) -> dict[str, Any]:
    """`{id, source, target}` from an edge object, a `"a->b"` string or an `[a, b]` pair."""
    if isinstance(raw, str):
        source, _, target = (s.strip() for s in raw.partition("->"))
        if source and target:
            return {"id": f"{source}->{target}", "source": source, "target": target}
    elif isinstance(raw, (list, tuple)) and len(raw) == 2:
        return {"id": f"{raw[0]}->{raw[1]}", "source": str(raw[0]), "target": str(raw[1])}
    elif (
        isinstance(raw, Mapping) and isinstance(raw.get("source"), str) and isinstance(raw.get("target"), str)
    ):
        eid = raw.get("id") if isinstance(raw.get("id"), str) else f"{raw['source']}->{raw['target']}"
        out = {"id": eid, "source": raw["source"], "target": raw["target"]}
        # The handle ids ride along when the edge names them (a null or empty one resolves like a missing one).
        for key, alias in (("sourceHandle", "source_handle"), ("targetHandle", "target_handle")):
            handle = raw.get(key, raw.get(alias))
            if isinstance(handle, str) and handle:
                out[key] = handle
        return out
    raise ValueError(f"an edge needs a source and a target: {raw!r}")


@dataclass(frozen=True)
class Graph:
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]


def as_graph(graph: Any) -> Graph:
    if isinstance(graph, Graph):
        return graph
    if not isinstance(graph, Mapping):
        raise ValueError("a graph must be an object {nodes, edges}")
    g = graph["graph"] if isinstance(graph.get("graph"), Mapping) else graph
    nodes = [node_data(n) for n in (g.get("nodes") or [])]
    edges = [as_edge(e) for e in (g.get("edges") or [])]
    return Graph(nodes, edges)


# ---------------------------------------------------------------------------- traversal


def _by_id(nodes: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for n in nodes:
        out[n["id"]] = n  # the last one wins, as a JS Map built from the list does
    return out


def _upstream(edges: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    m: dict[str, list[str]] = {}
    for e in edges:
        m.setdefault(e["target"], []).append(e["source"])
    return m


def _downstream(edges: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    m: dict[str, list[str]] = {}
    for e in edges:
        m.setdefault(e["source"], []).append(e["target"])
    return m


def _reach(start: str, nxt: Mapping[str, list[str]]) -> set[str]:
    """Every node reachable from `start` along `nxt` (excluding `start` unless a cycle returns to it)."""
    seen: set[str] = set()
    stack = list(nxt.get(start, ()))
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        stack.extend(nxt.get(nid, ()))
    return seen


def generation_depth(nodes: Iterable[Mapping[str, Any]], edges: Iterable[Any], node_id: str) -> int:
    """Generating nodes on the longest upstream path from a Plate to `node_id`, counting `node_id` when it
    generates; 0 for a node with no Plate upstream. Back edges of a cycle are ignored."""
    node_map = _by_id(n if "id" in n and "kind" in n else node_data(n) for n in nodes)
    up = _upstream(as_edge(e) for e in edges)
    done: dict[str, int] = {}
    active: set[str] = set()

    def walk(nid: str) -> int:
        if nid in done:
            return done[nid]
        node = node_map.get(nid)
        if node is None:
            return -1
        active.add(nid)
        own = 1 if node.get("kind") in GENERATING_KINDS else 0
        best = 0 if node.get("kind") == "Plate" else -1
        for src in up.get(nid, ()):
            if src in active:
                continue
            d = walk(src)
            if d >= 0:
                best = max(best, d + own)
        active.discard(nid)
        done[nid] = best
        return best

    return max(walk(node_id), 0)


def ancestors(graph: Any, node_id: str) -> set[str]:
    """Every node upstream of `node_id` (excluding it unless a cycle returns to it)."""
    g = as_graph(graph)
    return _reach(node_id, _upstream(g.edges))


def anchored_to_plate(
    node_map: Mapping[str, Mapping[str, Any]], up: Mapping[str, list[str]], nid: str
) -> bool:
    if (node_map.get(nid) or {}).get("kind") == "Plate":
        return True
    return any((node_map.get(a) or {}).get("kind") == "Plate" for a in _reach(nid, up))


def upstream_plates(graph: Any, node_id: str) -> list[dict[str, Any]]:
    """The Plate nodes among `node_id`'s ancestors (or the node itself), nearest first."""
    g = as_graph(graph)
    node_map = _by_id(g.nodes)
    up = _upstream(g.edges)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    frontier = [node_id]
    while frontier:
        nxt: list[str] = []
        for nid in frontier:
            if nid in seen:
                continue
            seen.add(nid)
            node = node_map.get(nid)
            if node is not None and node.get("kind") == "Plate":
                out.append(node)
            nxt.extend(up.get(nid, ()))
        frontier = nxt
    return out


def _is_concept(node: Mapping[str, Any] | None) -> bool:
    return node is not None and (
        node.get("kind") == "Concept" or node.get("outputTier") == "concept_unmeasured"
    )


def _concept_tier(node_map: Mapping[str, Mapping[str, Any]], up: Mapping[str, list[str]], nid: str) -> bool:
    if _is_concept(node_map.get(nid)):
        return True
    return any(_is_concept(node_map.get(a)) for a in _reach(nid, up))


def is_previs(node: Mapping[str, Any] | None) -> bool:
    """A previs output: `previs: true` on the node (set by the previs run) or in its params (set by the editor)."""
    if node is None:
        return False
    params = node.get("params")
    return node.get("previs") is True or (isinstance(params, Mapping) and params.get("previs") is True)


def _previs_tier(node_map: Mapping[str, Mapping[str, Any]], up: Mapping[str, list[str]], nid: str) -> bool:
    if is_previs(node_map.get(nid)):
        return True
    return any(is_previs(node_map.get(a)) for a in _reach(nid, up))


def _camera_override(node: Mapping[str, Any]) -> bool:
    params = node.get("params")
    v = params.get("cameraOverride") if isinstance(params, Mapping) else None
    return v is not None and v is not False


def _high_risk(node: Mapping[str, Any]) -> bool:
    return node.get("riskLabel") in ("high", "highest")


def _count(v: Any) -> int:
    return len(v) if isinstance(v, (list, tuple)) else 0


def _input_for(kind: Any, handle: Any, edge_type: str | None) -> _Port | None:
    """The input port an edge lands on: the named one, or for a missing handle the first input taking
    `edge_type` (else the first input), as `checkHandles` resolves it. None when the kind has no such input."""
    inputs = (PORTS.get(kind) or {}).get("inputs", ()) if isinstance(kind, str) else ()
    if not isinstance(handle, str) or not handle:
        if edge_type is not None:
            for p in inputs:
                if edge_type in p[2]:
                    return p
        return inputs[0] if inputs else None
    return next((p for p in inputs if p[0] == handle), None)


def _edge_ports(source_kind: Any, target_kind: Any, e: Mapping[str, Any]) -> tuple[str | None, _Port | None]:
    """What an edge carries (its source port's type; None when the port is unknown) and the input it lands on."""
    outputs = (PORTS.get(source_kind) or {}).get("outputs", ()) if isinstance(source_kind, str) else ()
    handle = e.get("sourceHandle")
    if not isinstance(handle, str) or not handle:
        out = outputs[0] if outputs else None
    else:
        out = next((p for p in outputs if p[0] == handle), None)
    edge_type = out[1] if out is not None else None
    return edge_type, _input_for(target_kind, e.get("targetHandle"), edge_type)


# ---------------------------------------------------------------------------- the rules


def validate_edge(graph: Any, edge: Any) -> RuleResult:
    """Would `edge` be allowed in `graph`? The edge may already be in `graph.edges` (it is ignored there)."""
    g = as_graph(graph)
    e = as_edge(edge)
    node_map = _by_id(g.nodes)
    source = node_map.get(e["source"])
    target = node_map.get(e["target"])
    if source is None or target is None:
        return _refuse("UNKNOWN_NODE", "unknown node")

    others = [x for x in g.edges if x["id"] != e["id"]]
    if e["source"] == e["target"] or e["source"] in _reach(e["target"], _downstream(others)):
        return _refuse("CYCLE", "cycle")

    sk = source.get("kind")
    tk = target.get("kind")

    if sk in ANNOTATION_KINDS or tk in ANNOTATION_KINDS:
        return _refuse("ANNOTATION", _ANNOTATION)

    if sk == "Reference":
        port = _input_for(tk, e.get("targetHandle"), "reference")
        if port is None or port[0] not in REFERENCE_HANDLES or "reference" not in port[2]:
            return _refuse("REFERENCE_ONLY", _REFERENCE_ONLY)

    nxt = [*others, e]
    next_up = _upstream(nxt)

    if tk in DELIVERY_KINDS and _concept_tier(node_map, next_up, e["target"]):
        return _refuse("CONCEPT_TO_DELIVERY", f"concept tier cannot reach {tk}")

    if tk in EXPORT_KINDS and _previs_tier(node_map, next_up, e["target"]):
        return _refuse(
            "PREVIS_NOT_DELIVERABLE", f"previs output cannot reach {tk}: PREVIS is not deliverable"
        )

    # A Reference anchors nothing: an edge from one needs the generator itself anchored through its other inputs.
    anchor = e["target"] if sk == "Reference" else e["source"]
    if tk in GENERATING_KINDS and not anchored_to_plate(node_map, next_up, anchor):
        return _refuse("NO_PLATE_ANCHOR", "generation must be anchored to a plate camera (source-view-only)")

    affected = [e["target"], *_reach(e["target"], _downstream(nxt))]
    if any(generation_depth(g.nodes, nxt, nid) > MAX_GENERATION_DEPTH for nid in affected):
        return _refuse("DEPTH_EXCEEDED", "generation depth > 2 from the plate")

    if tk in VIDEO_KINDS and not (source.get("gateStatus") == "pass" and source.get("approved") is True):
        # Only a still is locked; an edge whose ports cannot be resolved is treated as one (fail closed).
        edge_type, port = _edge_ports(sk, tk, e)
        if edge_type is None or port is None or edge_type == "image16" or port[1] == "image16":
            what = "motion" if tk == "Motion" else "crop pair"
            which = "the end still" if port is not None and port[0] == "end" else "the upstream still"
            return _refuse("VIDEO_LOCKED", f"{what} is locked until {which} passes gates and is approved")

    if tk == "Motion" and _high_risk(target) and _count(target.get("overrides")) == 0:
        return _refuse(
            "HIGH_RISK_MOVE",
            f"{target.get('riskLabel')} risk move is blocked until it is overridden with a reason",
        )

    if (
        tk == "Edit"
        and _camera_override(target)
        and not (
            _count(target.get("overrides")) > 0 and UNSEEN_GEOMETRY_LABEL in (target.get("labels") or [])
        )
    ):
        return _refuse(
            "INVENTED_ANGLE",
            "a camera override needs an explicit override and the label 'unseen geometry guessed'",
        )
    return OK


def validate_graph(graph: Any) -> list[dict[str, Any]]:
    """Every edge re-checked: `[{edge, code, message}]`, empty when the graph obeys the canvas rules."""
    g = as_graph(graph)
    out: list[dict[str, Any]] = []
    for e in g.edges:
        res = validate_edge(g, e)
        if not res.ok:
            out.append({"edge": e, "code": res.code, "message": res.message})
    return out


def check_nodes(graph: Any) -> list[dict[str, Any]]:
    """Node-level problems JSON allows and the TypeScript types do not: unknown kinds, missing or repeated ids."""
    g = as_graph(graph)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for n in g.nodes:
        nid = n.get("id")
        if not isinstance(nid, str) or not nid:
            out.append({"node": nid, "code": "UNKNOWN_NODE", "message": "a node needs a string id"})
            continue
        if nid in seen:
            out.append({"node": nid, "code": "UNKNOWN_NODE", "message": f"node id {nid!r} appears twice"})
        seen.add(nid)
        if n.get("kind") not in NODE_KINDS:
            out.append(
                {"node": nid, "code": "UNKNOWN_NODE", "message": f"unknown node kind {n.get('kind')!r}"}
            )
    return out


def can_accept(node: Mapping[str, Any], scrub_log: Mapping[str, Any] | None) -> RuleResult:
    """A take is accepted only after a logged full scrub (doctrine 7)."""
    log = _camel_keys(scrub_log) if isinstance(scrub_log, Mapping) else None
    if log is not None and log.get("fullScrub") is True:
        return OK
    n = node_data(node)
    return _refuse("SCRUB_REQUIRED", f"'{n.get('label')}' cannot be accepted without a logged full scrub")


def effective_labels(graph: Any, node_id: str) -> list[str]:
    """Labels a node carries: its own plus every ancestor's (`unseen geometry guessed` follows the pixels)."""
    g = as_graph(graph)
    node_map = _by_id(g.nodes)
    out: list[str] = []
    for nid in [node_id, *_reach(node_id, _upstream(g.edges))]:
        for label in (node_map.get(nid) or {}).get("labels") or []:
            if label not in out:
                out.append(label)
    return out
