"""Wave 9 canvas contract in Python (T81): the Reference, Frame, Atmosphere and Sky kinds, the handle-aware
REFERENCE_ONLY / ANNOTATION / VIDEO_LOCKED rules, and the parity of `canvas_rules` with the TypeScript files it
mirrors (`ui/src/nodeTypes.ts` NODE_KINDS, `ui/src/nodes/ports.ts` PORTS, `ui/src/canvasRules.ts` RuleCode).
The shared vectors themselves run in `test_routes_packets.py`; this file checks the messages, the handle
resolution and the drift guards the vectors cannot."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from plate_daemon import canvas_rules as cr

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui" / "src"
VECTORS = ROOT / "spec" / "canvas_rules_vectors.json"

REFERENCE_ONLY = "a reference feeds an engine's reference input only; it is never composited"
ANNOTATION = "a Frame is an annotation; it takes no edges"
APPROVED = {"gateStatus": "pass", "approved": True}


def _node(nid: str, kind: str, **data):
    out = {"id": nid, "kind": kind, **data}
    if kind == "Plate":
        out.setdefault("packetId", "pkt_a")
        out.setdefault("tier", "A")
    return out


def _edge(source: str, target: str, sh: str | None = None, th: str | None = None) -> dict:
    out: dict = {"id": f"{source}->{target}", "source": source, "target": target}
    if sh is not None:
        out["sourceHandle"] = sh
    if th is not None:
        out["targetHandle"] = th
    return out


def _check(nodes: list, edges: list, edge: dict) -> dict:
    return cr.validate_edge({"nodes": nodes, "edges": edges}, edge).to_dict()


# ---------------------------------------------------------------------------- vocabulary


def test_the_four_kinds_are_appended_and_the_two_codes_too():
    assert cr.NODE_KINDS[-4:] == ("Reference", "Frame", "Atmosphere", "Sky")
    assert len(cr.NODE_KINDS) == 23
    assert cr.RULE_CODES[-2:] == ("REFERENCE_ONLY", "ANNOTATION")
    assert cr.GENERATING_KINDS == frozenset({"Edit", "Motion", "People", "CropPair"})
    assert cr.REFERENCE_HANDLES == frozenset({"refs", "elements", "sky"})
    assert cr.ANNOTATION_KINDS == frozenset({"Frame"})
    assert set(cr.PORTS) == set(cr.NODE_KINDS)


def test_check_nodes_accepts_the_four_kinds():
    graph = {
        "nodes": [_node(f"n{i}", k) for i, k in enumerate(("Reference", "Frame", "Atmosphere", "Sky"))],
        "edges": [],
    }
    assert cr.check_nodes(graph) == []
    bad = {"nodes": [_node("x", "Sticker")], "edges": []}
    assert [p["code"] for p in cr.check_nodes(bad)] == ["UNKNOWN_NODE"]


def test_as_edge_carries_handle_ids_only_when_named():
    assert cr.as_edge({"source": "a", "target": "b"}) == {"id": "a->b", "source": "a", "target": "b"}
    assert cr.as_edge({"source": "a", "target": "b", "sourceHandle": None, "targetHandle": ""}) == {
        "id": "a->b",
        "source": "a",
        "target": "b",
    }
    assert cr.as_edge({"source": "a", "target": "b", "source_handle": "ref", "target_handle": "refs"}) == {
        "id": "a->b",
        "source": "a",
        "target": "b",
        "sourceHandle": "ref",
        "targetHandle": "refs",
    }
    assert cr.as_edge("a->b") == {"id": "a->b", "source": "a", "target": "b"}


# ---------------------------------------------------------------------------- REFERENCE_ONLY


def _ref_graph():
    nodes = [
        _node("p", "Plate"),
        _node("r", "Reference", params={"referenceHash": "b3_ref", "role": "style"}),
        _node("e", "Edit"),
        _node("c", "Comp"),
        _node("x", "Export"),
        _node("m", "Mask"),
        _node("cl", "Colour"),
    ]
    return nodes, [_edge("p", "e", "image", "image")]


def test_a_reference_lands_on_refs_by_name_or_by_type():
    nodes, edges = _ref_graph()
    assert _check(nodes, edges, _edge("r", "e", "ref", "refs")) == {"ok": True}
    assert _check(nodes, edges, _edge("r", "e")) == {"ok": True}  # a null target resolves to refs


@pytest.mark.parametrize(
    "edge",
    [
        _edge("r", "e", "ref", "image"),
        _edge("r", "e", "ref", "mask"),
        _edge("r", "e", "ref", "bogus"),
        _edge("r", "c", "ref", "base"),
        _edge("r", "c", "ref", "layers"),
        _edge("r", "c"),
        _edge("r", "x"),
        _edge("r", "x", "ref", "media"),
        _edge("r", "m"),
        _edge("r", "cl"),
    ],
    ids=lambda e: f"{e['target']}:{e.get('targetHandle')}",
)
def test_a_reference_is_never_composited(edge):
    nodes, edges = _ref_graph()
    assert _check(nodes, edges, edge) == {"ok": False, "code": "REFERENCE_ONLY", "message": REFERENCE_ONLY}


def test_a_reference_never_anchors_the_engine_it_feeds():
    nodes, _ = _ref_graph()
    got = _check(nodes, [], _edge("r", "e", "ref", "refs"))
    assert got["code"] == "NO_PLATE_ANCHOR"
    motion = [_node("r", "Reference"), _node("m", "Motion", riskLabel="low")]
    assert _check(motion, [], _edge("r", "m", None, "elements"))["code"] == "NO_PLATE_ANCHOR"
    edges = [_edge("p", "e", "image", "image"), _edge("r", "e", "ref", "refs")]
    assert cr.validate_graph({"nodes": nodes, "edges": edges}) == []
    assert cr.generation_depth(nodes, edges, "e") == 1
    assert cr.generation_depth(nodes, edges, "r") == 0


def test_references_feed_motion_elements_people_refs_and_the_sky():
    nodes = [
        _node("p", "Plate"),
        _node("r", "Reference"),
        _node("e", "Edit", **APPROVED),
        _node("m", "Motion", riskLabel="low"),
        _node("pp", "People"),
        _node("s", "Sky"),
    ]
    edges = [
        _edge("p", "e"),
        _edge("e", "m", "image", "image"),
        _edge("p", "pp"),
        _edge("p", "s", "packet", "packet"),
    ]
    for edge in (
        _edge("r", "m", "ref", "elements"),
        _edge("r", "pp", "ref", "refs"),
        _edge("r", "s", "ref", "sky"),
    ):
        assert _check(nodes, edges, edge) == {"ok": True}, edge
    # Sky is numeric: no anchor needed
    assert _check([_node("r", "Reference"), _node("s", "Sky")], [], _edge("r", "s")) == {"ok": True}


# ---------------------------------------------------------------------------- ANNOTATION


@pytest.mark.parametrize("pair", [("p", "f"), ("f", "x"), ("r", "f"), ("k", "f"), ("f", "e")])
def test_a_frame_takes_no_edges(pair):
    nodes = [
        _node("p", "Plate"),
        _node("f", "Frame", params={"title": "terrace set"}),
        _node("r", "Reference"),
        _node("x", "Export"),
        _node("k", "Concept"),
        _node("e", "Edit"),
    ]
    assert _check(nodes, [], _edge(*pair)) == {"ok": False, "code": "ANNOTATION", "message": ANNOTATION}


def test_a_frame_self_loop_is_a_cycle_first_and_a_frame_changes_nothing_else():
    nodes = [_node("p", "Plate"), _node("f", "Frame"), _node("e", "Edit")]
    assert _check(nodes, [], _edge("f", "f"))["code"] == "CYCLE"
    assert _check(nodes, [], _edge("p", "e")) == {"ok": True}
    assert cr.generation_depth(nodes, [], "f") == 0


# ---------------------------------------------------------------------------- VIDEO_LOCKED and the end still


def _motion_graph(end_still: dict):
    nodes = [
        _node("p", "Plate"),
        _node("s", "Edit", **APPROVED),
        _node("t", "Edit", **end_still),
        _node("m", "Motion", riskLabel="low"),
    ]
    return nodes, [_edge("p", "s"), _edge("p", "t"), _edge("s", "m", "image", "image")]


def test_an_unapproved_end_still_is_locked_and_named():
    nodes, edges = _motion_graph({"gateStatus": "pass", "approved": False})
    assert _check(nodes, edges, _edge("t", "m", "image", "end")) == {
        "ok": False,
        "code": "VIDEO_LOCKED",
        "message": "motion is locked until the end still passes gates and is approved",
    }
    nodes, edges = _motion_graph({"gateStatus": "fail", "approved": True})
    assert (
        "end still" in _check(nodes, edges, {"source": "t", "target": "m", "target_handle": "end"})["message"]
    )


def test_an_approved_end_still_is_allowed_and_the_start_keeps_its_message():
    nodes, edges = _motion_graph(APPROVED)
    assert _check(nodes, edges, _edge("t", "m", "image", "end")) == {"ok": True}
    nodes, edges = _motion_graph({})
    assert _check(nodes, edges, _edge("t", "m", "image", "image")) == {
        "ok": False,
        "code": "VIDEO_LOCKED",
        "message": "motion is locked until the upstream still passes gates and is approved",
    }
    assert _check(nodes, edges, _edge("t", "m"))["code"] == "VIDEO_LOCKED"  # null lands on the start


def test_a_packet_edge_is_not_a_still_and_unresolved_ports_fail_closed():
    nodes, edges = _motion_graph(APPROVED)
    assert _check(nodes, edges, _edge("p", "m", "packet", "packet")) == {"ok": True}
    # a null target handle takes the packet port
    assert _check(nodes, edges, _edge("p", "m", "packet")) == {"ok": True}
    assert _check(nodes, edges, _edge("p", "m", "image", "image"))["code"] == "VIDEO_LOCKED"
    assert _check(nodes, edges, _edge("p", "m"))["code"] == "VIDEO_LOCKED"  # the beauty, the first output
    assert _check(nodes, edges, _edge("p", "m", "bogus", "packet"))["code"] == "VIDEO_LOCKED"
    assert _check(nodes, edges, _edge("p", "m", "packet", "bogus"))["code"] == "VIDEO_LOCKED"
    cp = [_node("p", "Plate"), _node("e", "Edit"), _node("cp", "CropPair")]
    assert _check(cp, [_edge("p", "e")], _edge("e", "cp", "image", "image")) == {
        "ok": False,
        "code": "VIDEO_LOCKED",
        "message": "crop pair is locked until the upstream still passes gates and is approved",
    }


def test_comp_colour_atmosphere_and_sky_add_no_depth():
    nodes = [
        _node("p", "Plate"),
        _node("e1", "Edit"),
        _node("e2", "Edit"),
        _node("c", "Comp"),
        _node("cl", "Colour"),
        _node("a", "Atmosphere"),
        _node("s", "Sky"),
    ]
    edges = [
        _edge("p", "e1"),
        _edge("e1", "e2"),
        _edge("e2", "c", "image", "layers"),
        _edge("c", "cl"),
        _edge("p", "a", "packet", "packet"),
        _edge("p", "s", "packet", "packet"),
    ]
    assert cr.generation_depth(nodes, edges, "cl") == 2
    assert cr.generation_depth(nodes, edges, "a") == 0
    assert cr.generation_depth(nodes, edges, "s") == 0
    assert cr.validate_graph({"nodes": nodes, "edges": edges}) == []


def test_an_unknown_kind_does_not_crash_the_handle_resolution():
    nodes = [_node("p", "Plate"), _node("z", "Sticker"), _node("m", "Motion", riskLabel="low")]
    assert _check(nodes, [], _edge("z", "m"))["code"] == "NO_PLATE_ANCHOR"
    nodes = [_node("p", "Plate"), _node("z", "Sticker")]
    assert _check(nodes, [], _edge("p", "z", "image", "whatever")) == {"ok": True}


# ---------------------------------------------------------------------------- the shared vector file


def test_the_vector_file_has_the_wave9_vectors():
    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]
    assert len(vectors) >= 46
    names = {v["name"] for v in vectors}
    for name in (
        "reference to edit refs is allowed",
        "reference to export is REFERENCE_ONLY",
        "reference to comp base is REFERENCE_ONLY",
        "reference alone does not anchor an edit",
        "frame takes no edges",
        "motion end from an unapproved still is VIDEO_LOCKED",
        "motion end from an approved still is allowed",
        "comp with two approved layers at depth 2 is allowed",
        "edit after edit after edit is DEPTH_EXCEEDED",
        "sky node needs a packet",
    ):
        assert name in names, name


def test_every_vector_also_holds_with_the_handles_dropped():
    """The vitest harness drops handle ids; a vector that only holds with them would split the two suites."""
    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]
    strip = ("sourceHandle", "targetHandle", "source_handle", "target_handle")
    for v in vectors:
        if "edge" not in v:
            continue
        graph = json.loads(json.dumps(v["graph"]))
        for e in graph.get("edges") or []:
            for k in strip:
                e.pop(k, None)
        edge = {k: val for k, val in v["edge"].items() if k not in strip}
        got = cr.validate_edge(graph, edge)
        assert got.ok == v["expect"]["ok"], v["name"]
        if not got.ok:
            assert got.code == v["expect"]["code"], v["name"]


# ---------------------------------------------------------------------------- parity with the TypeScript


def _ts(path: Path) -> str:
    if not path.is_file():
        pytest.skip(f"{path} is not in this checkout")
    return re.sub(r"//[^\n]*", "", path.read_text(encoding="utf-8"))


def test_node_kinds_match_nodetypes_ts():
    src = _ts(UI / "nodeTypes.ts")
    body = re.search(r"export const NODE_KINDS = \[(.*?)\] as const;", src, re.S)
    assert body, "NODE_KINDS not found in nodeTypes.ts"
    assert tuple(re.findall(r'"(\w+)"', body.group(1))) == cr.NODE_KINDS


def test_rule_codes_match_canvasrules_ts():
    src = _ts(UI / "canvasRules.ts")
    body = re.search(r"export type RuleCode =(.*?);", src, re.S)
    assert body, "RuleCode not found in canvasRules.ts"
    assert tuple(re.findall(r'"(\w+)"', body.group(1))) == cr.RULE_CODES


_PORT = re.compile(r'port\("([^"]+)", "([^"]+)"(?:, "[^"]*")?(?:, \[([^\]]*)\])?\)')


def _ports_of(segment: str) -> list[tuple]:
    return [
        (m.group(1), m.group(2), tuple(re.findall(r'"(\w+)"', m.group(3) or "")))
        for m in _PORT.finditer(segment)
    ]


def test_ports_match_ports_ts():
    src = _ts(UI / "nodes" / "ports.ts")
    body = re.search(r"export const PORTS[^=]*= \{\n(.*?)\n\};", src, re.S)
    assert body, "PORTS not found in ports.ts"
    keys = list(re.finditer(r"^  (\w+): \{", body.group(1), re.M))
    ts: dict[str, dict] = {}
    for i, k in enumerate(keys):
        end = keys[i + 1].start() if i + 1 < len(keys) else len(body.group(1))
        chunk = body.group(1)[k.end() : end]
        ins, _, outs = chunk.partition("outputs:")
        inputs = [(pid, ptype, acc or (ptype,)) for pid, ptype, acc in _ports_of(ins)]
        outputs = (
            [("image", "image16")] if "IMAGE_OUT" in outs else [(pid, t) for pid, t, _ in _ports_of(outs)]
        )
        ts[k.group(1)] = {"inputs": inputs, "outputs": outputs}
    assert list(ts) == list(cr.NODE_KINDS)
    for kind, ports in ts.items():
        assert [tuple(p) for p in cr.PORTS[kind]["inputs"]] == ports["inputs"], kind
        assert [tuple(p) for p in cr.PORTS[kind]["outputs"]] == ports["outputs"], kind
