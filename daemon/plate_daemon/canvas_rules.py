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

`validate_edge(graph, edge)` checks, in order: UNKNOWN_NODE, CYCLE, CONCEPT_TO_DELIVERY (the concept tier
never reaches Export or Review), PREVIS_NOT_DELIVERABLE (a previs output, `previs: true` on a node or in its
params, never reaches Export, directly or through other nodes; a Review may show it, it is watermarked PREVIS),
NO_PLATE_ANCHOR (a generator hangs off a Plate: source-view-only),
DEPTH_EXCEEDED (at most two generations from the plate, the target and everything below it), VIDEO_LOCKED
(Motion and CropPair wait for a still that passed gates and was approved), HIGH_RISK_MOVE (a high or highest
risk move needs an override) and INVENTED_ANGLE (a camera override on an Edit needs an override and the
`unseen geometry guessed` label). `can_accept` refuses a take without a logged full scrub (SCRUB_REQUIRED).
`check_nodes` adds what the TypeScript types make impossible but JSON allows: an unknown kind or a repeated
id (UNKNOWN_NODE).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DELIVERY_KINDS",
    "EXPORT_KINDS",
    "GENERATING_KINDS",
    "MAX_GENERATION_DEPTH",
    "NODE_KINDS",
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
)
# Nodes that make new pixels; each costs a generation of depth.
GENERATING_KINDS = frozenset({"Edit", "Motion", "People", "CropPair"})
MAX_GENERATION_DEPTH = 2
DELIVERY_KINDS = frozenset({"Export", "Review"})
# Where a previs output may not go (T44): delivery proper. A client Review may show a watermarked previs.
EXPORT_KINDS = frozenset({"Export"})
VIDEO_KINDS = frozenset({"Motion", "CropPair"})
UNSEEN_GEOMETRY_LABEL = "unseen geometry guessed"

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
        return {"id": eid, "source": raw["source"], "target": raw["target"]}
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

    nxt = [*others, e]
    next_up = _upstream(nxt)
    tk = target.get("kind")

    if tk in DELIVERY_KINDS and _concept_tier(node_map, next_up, e["target"]):
        return _refuse("CONCEPT_TO_DELIVERY", f"concept tier cannot reach {tk}")

    if tk in EXPORT_KINDS and _previs_tier(node_map, next_up, e["target"]):
        return _refuse(
            "PREVIS_NOT_DELIVERABLE", f"previs output cannot reach {tk}: PREVIS is not deliverable"
        )

    if tk in GENERATING_KINDS and not anchored_to_plate(node_map, next_up, e["source"]):
        return _refuse("NO_PLATE_ANCHOR", "generation must be anchored to a plate camera (source-view-only)")

    affected = [e["target"], *_reach(e["target"], _downstream(nxt))]
    if any(generation_depth(g.nodes, nxt, nid) > MAX_GENERATION_DEPTH for nid in affected):
        return _refuse("DEPTH_EXCEEDED", "generation depth > 2 from the plate")

    if tk in VIDEO_KINDS and not (source.get("gateStatus") == "pass" and source.get("approved") is True):
        what = "motion" if tk == "Motion" else "crop pair"
        return _refuse(
            "VIDEO_LOCKED", f"{what} is locked until the upstream still passes gates and is approved"
        )

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
