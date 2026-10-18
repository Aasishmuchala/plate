"""Canvas rules at scale (router plan T133): `validate_graph` builds the node map, both adjacency maps and every
node's generation depth once, instead of walking the graph for each affected node of each edge.

The change is a refactor, so the oracle is the old behaviour: `_legacy_validate_edge` below is the pre-T133
function (a per-edge walk with a recursive `generation_depth` per affected node), kept verbatim as a reference.
The tests check

- a 300-node Colour chain and a 25 x 20 layered graph validate in under a second, with the same answers as
  `[validate_edge(g, e) for e in edges]` (the no-context path);
- on hundreds of seeded random graphs (DAGs, cyclic graphs, unknown endpoints, repeated edge ids, handles)
  the context path, the no-context path and the legacy reference agree on every edge;
- `generation_depth` matches the old recursive walk and no longer hits the recursion limit on a long chain.
"""

from __future__ import annotations

import random
import time
from collections.abc import Mapping
from typing import Any

import pytest

from plate_daemon import canvas_rules as cr

BUDGET_S = 1.0
APPROVED = {"gateStatus": "pass", "approved": True}


# ---------------------------------------------------------------------------- the pre-T133 reference


def _legacy_depth(nodes, edges, node_id: str) -> int:
    """The recursive `generation_depth` as it was before T133."""
    node_map = cr._by_id(n if "id" in n and "kind" in n else cr.node_data(n) for n in nodes)
    up = cr._upstream(cr.as_edge(e) for e in edges)
    done: dict[str, int] = {}
    active: set[str] = set()

    def walk(nid: str) -> int:
        if nid in done:
            return done[nid]
        node = node_map.get(nid)
        if node is None:
            return -1
        active.add(nid)
        own = 1 if node.get("kind") in cr.GENERATING_KINDS else 0
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


def _legacy_validate_edge(graph: Any, edge: Any) -> cr.RuleResult:
    """`validate_edge` as it was before T133 (same checks in the same order, a graph walk per query)."""
    g = cr.as_graph(graph)
    e = cr.as_edge(edge)
    node_map = cr._by_id(g.nodes)
    source = node_map.get(e["source"])
    target = node_map.get(e["target"])
    if source is None or target is None:
        return cr._refuse("UNKNOWN_NODE", "unknown node")

    others = [x for x in g.edges if x["id"] != e["id"]]
    if e["source"] == e["target"] or e["source"] in cr._reach(e["target"], cr._downstream(others)):
        return cr._refuse("CYCLE", "cycle")

    sk = source.get("kind")
    tk = target.get("kind")

    if sk in cr.ANNOTATION_KINDS or tk in cr.ANNOTATION_KINDS:
        return cr._refuse("ANNOTATION", cr._ANNOTATION)

    if sk == "Reference":
        port = cr._input_for(tk, e.get("targetHandle"), "reference")
        if port is None or port[0] not in cr.REFERENCE_HANDLES or "reference" not in port[2]:
            return cr._refuse("REFERENCE_ONLY", cr._REFERENCE_ONLY)

    nxt = [*others, e]
    next_up = cr._upstream(nxt)

    if tk in cr.DELIVERY_KINDS and cr._concept_tier(node_map, next_up, e["target"]):
        return cr._refuse("CONCEPT_TO_DELIVERY", f"concept tier cannot reach {tk}")

    if tk in cr.EXPORT_KINDS and cr._previs_tier(node_map, next_up, e["target"]):
        return cr._refuse(
            "PREVIS_NOT_DELIVERABLE", f"previs output cannot reach {tk}: PREVIS is not deliverable"
        )

    anchor = e["target"] if sk == "Reference" else e["source"]
    if tk in cr.GENERATING_KINDS and not cr.anchored_to_plate(node_map, next_up, anchor):
        return cr._refuse(
            "NO_PLATE_ANCHOR", "generation must be anchored to a plate camera (source-view-only)"
        )

    affected = [e["target"], *cr._reach(e["target"], cr._downstream(nxt))]
    if any(_legacy_depth(g.nodes, nxt, nid) > cr.MAX_GENERATION_DEPTH for nid in affected):
        return cr._refuse("DEPTH_EXCEEDED", "generation depth > 2 from the plate")

    if tk in cr.VIDEO_KINDS and not (source.get("gateStatus") == "pass" and source.get("approved") is True):
        edge_type, port = cr._edge_ports(sk, tk, e)
        if edge_type is None or port is None or edge_type == "image16" or port[1] == "image16":
            what = "motion" if tk == "Motion" else "crop pair"
            which = "the end still" if port is not None and port[0] == "end" else "the upstream still"
            return cr._refuse("VIDEO_LOCKED", f"{what} is locked until {which} passes gates and is approved")

    if tk == "Motion" and cr._high_risk(target) and cr._count(target.get("overrides")) == 0:
        return cr._refuse(
            "HIGH_RISK_MOVE",
            f"{target.get('riskLabel')} risk move is blocked until it is overridden with a reason",
        )

    if (
        tk == "Edit"
        and cr._camera_override(target)
        and not (
            cr._count(target.get("overrides")) > 0
            and cr.UNSEEN_GEOMETRY_LABEL in (target.get("labels") or [])
        )
    ):
        return cr._refuse(
            "INVENTED_ANGLE",
            "a camera override needs an explicit override and the label 'unseen geometry guessed'",
        )
    return cr.OK


def _legacy_validate_graph(graph: Any) -> list[dict[str, Any]]:
    g = cr.as_graph(graph)
    out = []
    for e in g.edges:
        res = _legacy_validate_edge(g, e)
        if not res.ok:
            out.append({"edge": e, "code": res.code, "message": res.message})
    return out


# ---------------------------------------------------------------------------- graphs


def _plate(nid: str = "plate") -> dict:
    return {"id": nid, "kind": "Plate", "packetId": "pkt_a", "tier": "A"}


def _edge(source: str, target: str, **extra: Any) -> dict:
    return {"id": f"{source}->{target}", "source": source, "target": target, **extra}


def _chain_graph(n: int = 300) -> dict:
    """Plate -> c0 -> ... -> c(n-1), Colour nodes, with three Edit nodes in the line (the third is a third
    generation: DEPTH_EXCEEDED on every edge at or above it) and a few side nodes that break other rules."""
    nodes: list[dict] = [_plate()]
    edges: list[dict] = []
    prev = "plate"
    generators = {n // 3: "Edit", n // 2: "Edit", (4 * n) // 5: "Edit"}
    for i in range(n):
        nid = f"c{i}"
        nodes.append({"id": nid, "kind": generators.get(i, "Colour"), **APPROVED})
        edges.append(_edge(prev, nid))
        prev = nid
    nodes += [
        {"id": "export", "kind": "Export"},
        {"id": "concept", "kind": "Concept"},
        {"id": "motion", "kind": "Motion"},  # fed by an unapproved still
        {"id": "orphan_edit", "kind": "Edit"},  # no plate behind it
        {"id": "unapproved", "kind": "Colour"},
    ]
    edges += [
        _edge(prev, "export"),
        _edge("concept", "export"),
        _edge("plate", "unapproved"),
        _edge("unapproved", "motion"),
        _edge("concept", "orphan_edit"),
    ]
    return {"nodes": nodes, "edges": edges}


def _layered_graph(layers: int = 25, width: int = 20) -> dict:
    """25 layers of 20 nodes under one Plate; each node takes two nodes of the layer above (so about 1 000
    edges), with an Edit here and there so generation depths differ across the graph."""
    nodes: list[dict] = [_plate()]
    edges: list[dict] = []
    above = ["plate"]
    for layer in range(layers):
        row = []
        for col in range(width):
            nid = f"l{layer}_{col}"
            kind = "Edit" if (layer in (4, 11, 18) and col % 7 == 0) else ("Comp" if layer % 2 else "Colour")
            nodes.append({"id": nid, "kind": kind, **APPROVED})
            row.append(nid)
            sources = above if len(above) <= 2 else [above[col % len(above)], above[(col + 3) % len(above)]]
            for src in sources:
                edges.append(_edge(src, nid))
        above = row
    nodes += [{"id": "export", "kind": "Export"}, {"id": "review", "kind": "Review"}]
    edges += [_edge(above[0], "export"), _edge(above[1], "review")]
    return {"nodes": nodes, "edges": edges}


def _timed(fn):
    t0 = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t0


def _brute_force(g: dict) -> list[dict]:
    graph = cr.as_graph(g)
    out = []
    for e in graph.edges:
        res = cr.validate_edge(graph, e)
        if not res.ok:
            out.append({"edge": e, "code": res.code, "message": res.message})
    return out


# ---------------------------------------------------------------------------- the scale targets


@pytest.mark.parametrize(
    "name,builder",
    [("a 300-node Colour chain", _chain_graph), ("a 25 x 20 layered graph", _layered_graph)],
    ids=["chain300", "layered25x20"],
)
def test_validate_graph_is_fast_and_equals_the_per_edge_answers(name, builder):
    g = builder()
    graph = cr.as_graph(g)  # normalised once, as the route's first call does; the timing is the rules
    result, elapsed = _timed(lambda: cr.validate_graph(graph))
    assert elapsed < BUDGET_S, f"{name}: validate_graph took {elapsed:.2f}s"

    assert result == _brute_force(g)
    # The graphs are built to hold violations, so the equality above compares something.
    codes = {v["code"] for v in result}
    assert "DEPTH_EXCEEDED" in codes
    assert len(result) > 10


def test_the_fixture_graphs_violate_the_rules_they_are_built_to():
    chain = {v["code"] for v in cr.validate_graph(_chain_graph())}
    assert {"DEPTH_EXCEEDED", "CONCEPT_TO_DELIVERY", "VIDEO_LOCKED", "NO_PLATE_ANCHOR"} <= chain
    layered = cr.validate_graph(_layered_graph())
    assert {v["code"] for v in layered} == {"DEPTH_EXCEEDED"}


def test_validate_graph_accepts_a_raw_graph_dict_in_under_the_budget():
    g = _chain_graph()
    _, elapsed = _timed(lambda: cr.validate_graph(g))
    assert elapsed < BUDGET_S


def test_a_clean_300_node_chain_has_no_violations():
    nodes = [_plate(), *({"id": f"c{i}", "kind": "Colour"} for i in range(300))]
    edges = [_edge("plate" if i == 0 else f"c{i - 1}", f"c{i}") for i in range(300)]
    assert cr.validate_graph({"nodes": nodes, "edges": edges}) == []


def test_a_long_chain_does_not_hit_the_recursion_limit():
    n = 3000
    nodes = [_plate(), *({"id": f"c{i}", "kind": "Edit" if i == 5 else "Colour"} for i in range(n))]
    edges = [_edge("plate" if i == 0 else f"c{i - 1}", f"c{i}") for i in range(n)]
    assert cr.generation_depth(nodes, edges, f"c{n - 1}") == 1
    graph = cr.as_graph({"nodes": nodes, "edges": edges})
    result, elapsed = _timed(lambda: cr.validate_graph(graph))
    assert result == []
    assert elapsed < BUDGET_S


def test_validate_edge_alone_still_works_and_is_cheap_on_the_chain():
    graph = cr.as_graph(_chain_graph())
    ok, elapsed = _timed(lambda: cr.validate_edge(graph, _edge("c10", "c11")))
    assert ok.code == "DEPTH_EXCEEDED"  # c11 is upstream of the third Edit
    assert elapsed < BUDGET_S / 10
    fresh = cr.validate_edge(graph, _edge("c299", "export"))
    assert fresh.ok is False and fresh.code == "CONCEPT_TO_DELIVERY"


# ---------------------------------------------------------------------------- the context API


def test_build_context_exposes_the_tables_of_an_acyclic_graph():
    g = cr.as_graph(
        {
            "nodes": [
                _plate(),
                {"id": "e1", "kind": "Edit"},
                {"id": "e2", "kind": "Edit"},
                {"id": "c", "kind": "Colour"},
                {"id": "x", "kind": "Concept"},
                {"id": "stray", "kind": "Colour"},
            ],
            "edges": [_edge("plate", "e1"), _edge("e1", "e2"), _edge("e2", "c"), _edge("x", "stray")],
        }
    )
    ctx = cr.build_context(g)
    assert ctx.acyclic is True
    assert ctx.anchored["c"] and ctx.anchored["plate"] and not ctx.anchored["stray"]
    assert ctx.concept["stray"] and not ctx.concept["c"]
    assert ctx.max_depth_below["plate"] == 2 and ctx.max_depth_below["stray"] == 0
    assert all(ctx.covers(e) for e in g.edges)
    assert not ctx.covers(_edge("plate", "c"))


def test_a_cycle_anywhere_turns_the_tables_off_and_the_answers_stay_the_same():
    g = {
        "nodes": [
            _plate(),
            {"id": "a", "kind": "Edit"},
            {"id": "b", "kind": "Edit"},
            {"id": "c", "kind": "Edit"},
        ],
        "edges": [_edge("plate", "a"), _edge("a", "b"), _edge("b", "a"), _edge("b", "c")],
    }
    graph = cr.as_graph(g)
    assert cr.build_context(graph).acyclic is False
    assert cr.validate_graph(graph) == _legacy_validate_graph(graph)
    codes = [v["code"] for v in cr.validate_graph(graph)]
    assert codes.count("CYCLE") == 2  # a->b and b->a


def test_an_edge_the_context_was_not_built_from_is_checked_on_its_own():
    g = cr.as_graph(_chain_graph(30))
    ctx = cr.build_context(g)
    stray = _edge("c3", "export")  # not in the graph
    assert not ctx.covers(stray)
    assert cr.validate_edge(g, stray, ctx) == cr.validate_edge(g, stray) == _legacy_validate_edge(g, stray)
    # An edge that rewires a stored id (same id, other ends) is not covered either.
    moved = {"id": "plate->c0", "source": "c5", "target": "c0"}
    assert not ctx.covers(moved)
    assert cr.validate_edge(g, moved, ctx) == _legacy_validate_edge(g, moved)
    assert cr.validate_edge(g, moved, ctx).code == "CYCLE"


def test_repeated_edge_ids_fall_back_to_the_per_edge_path():
    g = {
        "nodes": [
            _plate(),
            {"id": "a", "kind": "Edit"},
            {"id": "b", "kind": "Colour"},
            {"id": "c", "kind": "Edit"},
        ],
        "edges": [
            {"id": "same", "source": "plate", "target": "a"},
            {"id": "same", "source": "a", "target": "b"},
            _edge("b", "c"),
        ],
    }
    graph = cr.as_graph(g)
    ctx = cr.build_context(graph)
    assert [cr.validate_edge(graph, e, ctx) for e in graph.edges] == [
        _legacy_validate_edge(graph, e) for e in graph.edges
    ]


# ---------------------------------------------------------------------------- generation_depth


def _random_graph(rng: random.Random, *, cyclic: bool, messy: bool) -> dict:
    kinds = [
        "Plate", "Edit", "Edit", "Edit", "Colour", "Colour", "Comp", "Mask", "Motion", "CropPair", "People",
        "Export", "Review", "Concept", "Reference", "Frame", "Gate", "Set", "Light",
    ]  # fmt: skip
    n = rng.randint(2, 14)
    nodes = []
    for i in range(n):
        node: dict[str, Any] = {
            "id": f"n{i}",
            "kind": "Plate" if i == 0 and rng.random() < 0.85 else rng.choice(kinds),
        }
        if rng.random() < 0.4:
            node.update(APPROVED)
        if rng.random() < 0.1:
            node["previs"] = True
        if rng.random() < 0.1:
            node["riskLabel"] = rng.choice(["low", "high", "highest"])
        if rng.random() < 0.1:
            node["params"] = {"cameraOverride": {"yaw": 5}}
        if rng.random() < 0.1:
            node["overrides"] = [{"reason": "ok"}]
        nodes.append(node)
    edges: list[dict] = []
    for i in range(n - 1):  # a spine, so generations stack up and depth is exercised
        if rng.random() < 0.7:
            edges.append(_edge(f"n{i}", f"n{i + 1}"))
    for _ in range(rng.randint(0, n)):
        a, b = rng.randrange(n), rng.randrange(n)
        if not cyclic:
            if a == b:
                continue
            a, b = min(a, b), max(a, b)
        edge = _edge(f"n{a}", f"n{b}")
        if messy:
            roll = rng.random()
            if roll < 0.05:
                edge["target"] = "ghost"  # an unknown node
            elif roll < 0.1:
                edge["id"] = f"dup{rng.randrange(3)}"  # ids that repeat across different ends
            if rng.random() < 0.3:
                kind = next((x["kind"] for x in nodes if x["id"] == edge["target"]), None)
                inputs = (cr.PORTS.get(kind) or {}).get("inputs", ())
                if inputs:
                    edge["targetHandle"] = rng.choice(inputs)[0]
        edges.append(edge)
    return {"nodes": nodes, "edges": edges}


def _all_agree(g: dict) -> None:
    graph = cr.as_graph(g)
    ctx = cr.build_context(graph)
    expected = [_legacy_validate_edge(graph, e) for e in graph.edges]
    assert [cr.validate_edge(graph, e) for e in graph.edges] == expected
    assert [cr.validate_edge(graph, e, ctx) for e in graph.edges] == expected
    assert cr.validate_graph(graph) == _legacy_validate_graph(graph)


@pytest.mark.parametrize("seed", range(150))
def test_random_dags_agree_with_the_pre_t133_rules(seed):
    _all_agree(_random_graph(random.Random(seed), cyclic=False, messy=seed % 2 == 0))


@pytest.mark.parametrize("seed", range(150))
def test_random_graphs_with_cycles_agree_with_the_pre_t133_rules(seed):
    _all_agree(_random_graph(random.Random(1000 + seed), cyclic=True, messy=seed % 2 == 0))


@pytest.mark.parametrize("seed", range(60))
def test_generation_depth_matches_the_old_recursive_walk(seed):
    g = _random_graph(random.Random(5000 + seed), cyclic=seed % 2 == 1, messy=False)
    graph = cr.as_graph(g)
    for node in graph.nodes:
        assert cr.generation_depth(graph.nodes, graph.edges, node["id"]) == _legacy_depth(
            graph.nodes, graph.edges, node["id"]
        )
    assert cr.generation_depth(graph.nodes, graph.edges, "ghost") == 0


def test_the_tables_equal_the_walks_for_every_node_of_a_dag():
    for seed in range(40):
        graph = cr.as_graph(_random_graph(random.Random(9000 + seed), cyclic=False, messy=False))
        ctx = cr.build_context(graph)
        assert ctx.acyclic
        up = cr._upstream(graph.edges)
        down = cr._downstream(graph.edges)
        for node in graph.nodes:
            nid = node["id"]
            assert ctx.anchored[nid] == cr.anchored_to_plate(ctx.node_map, up, nid)
            assert ctx.concept[nid] == cr._concept_tier(ctx.node_map, up, nid)
            assert ctx.previs[nid] == cr._previs_tier(ctx.node_map, up, nid)
            affected = [nid, *cr._reach(nid, down)]
            assert ctx.max_depth_below[nid] == max(
                _legacy_depth(graph.nodes, graph.edges, a) for a in affected
            )


def test_node_maps_are_shared_not_rebuilt_per_edge(monkeypatch):
    """validate_graph builds the node map and the adjacency once; a regression to a per-edge rebuild shows here."""
    graph = cr.as_graph(_chain_graph(60))
    calls = {"by_id": 0, "upstream": 0}
    by_id, upstream = cr._by_id, cr._upstream

    def counting_by_id(nodes):
        calls["by_id"] += 1
        return by_id(nodes)

    def counting_upstream(edges):
        calls["upstream"] += 1
        return upstream(edges)

    monkeypatch.setattr(cr, "_by_id", counting_by_id)
    monkeypatch.setattr(cr, "_upstream", counting_upstream)
    result = cr.validate_graph(graph)
    assert result  # the graph has violations
    assert calls == {"by_id": 1, "upstream": 1}


def test_mapping_inputs_are_not_mutated():
    g = _chain_graph(20)
    snapshot = repr(g)
    cr.validate_graph(g)
    cr.validate_edge(g, _edge("c1", "c2"))
    assert repr(g) == snapshot
    assert isinstance(g, Mapping)
