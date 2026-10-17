import type { Edge } from "@xyflow/react";
import { describe, expect, it } from "vitest";
import {
  applyOverride,
  canAccept,
  canConnect,
  effectiveLabels,
  generationDepth,
  type GraphNode,
  MAX_GENERATION_DEPTH,
  recomputeDepths,
  validateEdge,
  validateGraph,
} from "./canvasRules";
import {
  makeNodeData,
  makePlateNodeData,
  NODE_KINDS,
  type NodeKind,
  type PlateNodeData,
  UNSEEN_GEOMETRY_LABEL,
} from "./nodeTypes";

const node = (id: string, kind: NodeKind, extra: Partial<PlateNodeData> = {}): GraphNode => ({
  id,
  position: { x: 0, y: 0 },
  data: kind === "Plate" ? makePlateNodeData(`packet-${id}`, "A", extra) : makeNodeData(kind, extra),
});
const edge = (source: string, target: string): Edge => ({ id: `${source}->${target}`, source, target });
const approvedPass: Partial<PlateNodeData> = { gateStatus: "pass", approved: true };

describe("generation depth (doctrine 7)", () => {
  const nodes = [node("p", "Plate"), node("e1", "Edit"), node("e2", "Edit"), node("g", "Gate")];
  const edges = [edge("p", "e1"), edge("e1", "e2"), edge("e2", "g")];

  it("counts generating nodes on the longest path from the plate", () => {
    expect(generationDepth(nodes, edges, "g")).toBe(2);
    expect(generationDepth(nodes, edges, "p")).toBe(0);
  });

  it("refuses a third generation downstream with the depth reason", () => {
    const withThird = [...nodes, node("e3", "Edit")];
    expect(canConnect(withThird, edges, "e2", "e3")).toEqual({
      ok: false,
      reason: "generation depth > 2 from the plate",
    });
    expect(canConnect(withThird, edges, "g", "e3")).toEqual({
      ok: false,
      reason: "generation depth > 2 from the plate",
    });
  });

  it("refuses a connection that pushes an existing downstream generator past depth 2", () => {
    // People (q) is depth 2 via Plate -> Edit -> Mask -> People; feeding the Mask from a depth-2 Edit makes it 3.
    const tail = [
      node("p", "Plate"),
      node("a1", "Edit"),
      node("a2", "Edit"),
      node("b", "Edit"),
      node("m", "Mask"),
      node("q", "People"),
    ];
    const tailEdges = [edge("p", "a1"), edge("a1", "a2"), edge("p", "b"), edge("b", "m"), edge("m", "q")];
    expect(generationDepth(tail, tailEdges, "q")).toBe(2);
    expect(canConnect(tail, tailEdges, "a2", "m")).toEqual({
      ok: false,
      reason: "generation depth > 2 from the plate",
    });
    expect(canConnect(tail, tailEdges, "a1", "m")).toEqual({ ok: true });
  });

  it("allows a second generation and leaves non-generators free", () => {
    const more = [...nodes, node("c", "Comp")];
    expect(canConnect(more, edges, "g", "c")).toEqual({ ok: true });
    expect(canConnect([node("p", "Plate"), node("e1", "Edit")], [], "p", "e1")).toEqual({ ok: true });
  });

  it("increments on Edit, Motion, People and CropPair and not on the other kinds", () => {
    const incrementing: NodeKind[] = ["Edit", "Motion", "People", "CropPair"];
    for (const kind of NODE_KINDS) {
      const g = [node("p", "Plate"), node("x", kind)];
      const expected = incrementing.includes(kind) ? 1 : 0;
      expect(generationDepth(g, [edge("p", "x")], "x"), kind).toBe(expected);
    }
  });

  it("Mask, Comp, Colour, Gate and Compare do not add depth between two generations", () => {
    const chain = ["Mask", "Comp", "Colour", "Gate", "Compare"] as const;
    const ns = [node("p", "Plate"), node("e1", "Edit"), ...chain.map((k) => node(k, k)), node("e2", "Edit")];
    const es = [edge("p", "e1"), edge("e1", "Mask"), edge("Mask", "Comp"), edge("Comp", "Colour"), edge("Colour", "Gate"), edge("Gate", "Compare")];
    expect(canConnect(ns, es, "Compare", "e2")).toEqual({ ok: true });
    expect(generationDepth(ns, [...es, edge("Compare", "e2")], "e2")).toBe(MAX_GENERATION_DEPTH);
  });

  it.each(["Edit", "Motion", "People", "CropPair"] as const)("a third %s on the path is DEPTH_EXCEEDED", (kind) => {
    const ns = [node("p", "Plate"), node("a", "Edit"), node("b", "People"), node("c", kind)];
    const result = validateEdge({ nodes: ns, edges: [edge("p", "a"), edge("a", "b")] }, edge("b", "c"));
    expect(result).toMatchObject({ ok: false, code: "DEPTH_EXCEEDED" });
  });

  it("recomputeDepths refreshes data.generationDepth and keeps untouched nodes by identity", () => {
    const fresh = recomputeDepths(nodes, edges);
    expect(fresh.map((n) => n.data.generationDepth)).toEqual([0, 1, 2, 2]);
    expect(fresh[0]).toBe(nodes[0]);
    expect(recomputeDepths(fresh, edges)[2]).toBe(fresh[2]);
  });
});

describe("concept tier (doctrine 1)", () => {
  it("refuses Concept -> Export", () => {
    const nodes = [node("c", "Concept"), node("x", "Export")];
    expect(canConnect(nodes, [], "c", "x")).toEqual({ ok: false, reason: "concept tier cannot reach Export" });
    expect(validateEdge({ nodes, edges: [] }, edge("c", "x"))).toMatchObject({ code: "CONCEPT_TO_DELIVERY" });
  });

  it("refuses the concept tier reaching Export through intermediate nodes", () => {
    const nodes = [node("p", "Plate"), node("c", "Concept"), node("m", "Comp"), node("x", "Export")];
    const edges = [edge("p", "m"), edge("c", "m")];
    expect(canConnect(nodes, edges, "m", "x")).toEqual({ ok: false, reason: "concept tier cannot reach Export" });
  });

  it("refuses Concept -> Review and allows a measured path into Export", () => {
    const nodes = [node("p", "Plate"), node("c", "Concept"), node("r", "Review"), node("x", "Export")];
    expect(canConnect(nodes, [], "c", "r").ok).toBe(false);
    expect(canConnect(nodes, [], "p", "x")).toEqual({ ok: true });
  });

  it("treats any node whose outputTier is concept_unmeasured as the concept tier", () => {
    const nodes = [node("p", "Plate"), node("m", "Comp", { outputTier: "concept_unmeasured" }), node("x", "Export")];
    expect(validateEdge({ nodes, edges: [edge("p", "m")] }, edge("m", "x"))).toMatchObject({
      ok: false,
      code: "CONCEPT_TO_DELIVERY",
    });
  });

  it("Concept nodes default to the concept_unmeasured tier and everything else to measured", () => {
    for (const kind of NODE_KINDS) {
      expect(makeNodeData(kind).outputTier).toBe(kind === "Concept" ? "concept_unmeasured" : "measured");
    }
  });

  it("has no text-to-image or image-creator node kind", () => {
    expect(NODE_KINDS).toHaveLength(19);
    for (const kind of NODE_KINDS) expect(kind).not.toMatch(/text|creator|prompt|imagine|txt2img/i);
  });
});

describe("source-view-only (doctrine 2)", () => {
  it("refuses to feed a generator from a node that has no plate upstream", () => {
    const nodes = [node("m", "Mask"), node("e", "Edit"), node("c", "Concept")];
    expect(canConnect(nodes, [], "m", "e").ok).toBe(false);
    expect(canConnect(nodes, [], "c", "e")).toEqual({
      ok: false,
      reason: "generation must be anchored to a plate camera (source-view-only)",
    });
    expect(validateEdge({ nodes, edges: [] }, edge("m", "e"))).toMatchObject({ code: "NO_PLATE_ANCHOR" });
  });

  it("NO_PLATE_ANCHOR applies to every generating kind", () => {
    for (const kind of ["Edit", "Motion", "People", "CropPair"] as const) {
      const nodes = [node("m", "Mask", approvedPass), node("t", kind)];
      expect(validateEdge({ nodes, edges: [] }, edge("m", "t")), kind).toMatchObject({ code: "NO_PLATE_ANCHOR" });
    }
  });

  it("an Edit with params.cameraOverride and no override+label is INVENTED_ANGLE", () => {
    const params = { cameraOverride: { yaw_deg: 40 } };
    const bare = node("e", "Edit", { params });
    const overriddenNoLabel = node("e", "Edit", { params, overrides: [{ reason: "client asked", by: "a", at: "t" }] });
    for (const e of [bare, overriddenNoLabel]) {
      expect(validateEdge({ nodes: [node("p", "Plate"), e], edges: [] }, edge("p", "e"))).toMatchObject({
        ok: false,
        code: "INVENTED_ANGLE",
      });
    }
  });

  it("an invented angle connects once applyOverride gave it the override and the label", () => {
    const e = applyOverride(node("e", "Edit", { params: { cameraOverride: { yaw_deg: 40 } } }), "client asked for the east side", "aasish");
    expect(e.data.labels).toContain(UNSEEN_GEOMETRY_LABEL);
    expect(validateEdge({ nodes: [node("p", "Plate"), e], edges: [] }, edge("p", "e"))).toEqual({ ok: true });
  });

  it("an Edit without cameraOverride is never INVENTED_ANGLE", () => {
    expect(canConnect([node("p", "Plate"), node("e", "Edit", { params: { cameraOverride: null } })], [], "p", "e")).toEqual({ ok: true });
  });
});

describe("video lock (doctrine 7)", () => {
  const base = (data: Partial<PlateNodeData>): [GraphNode[], Edge[]] => [
    [node("p", "Plate"), node("e", "Edit", data), node("m", "Motion")],
    [edge("p", "e")],
  ];

  it("refuses Edit -> Motion until the still has passed gates and been approved", () => {
    const reason = "motion is locked until the upstream still passes gates and is approved";
    for (const data of [
      { gateStatus: "fail" },
      { gateStatus: "pass" },
      { approved: true },
      { gateStatus: "advisory", approved: true },
      { gateStatus: "fail", approved: true },
    ] as Partial<PlateNodeData>[]) {
      const [nodes, edges] = base(data);
      expect(canConnect(nodes, edges, "e", "m"), JSON.stringify(data)).toEqual({ ok: false, reason });
    }
  });

  it("allows Edit -> Motion once gateStatus is pass and approved", () => {
    const [nodes, edges] = base(approvedPass);
    expect(canConnect(nodes, edges, "e", "m")).toEqual({ ok: true });
  });

  it("locks CropPair the same way", () => {
    const ns = [node("p", "Plate"), node("e", "Edit"), node("c", "CropPair")];
    expect(validateEdge({ nodes: ns, edges: [edge("p", "e")] }, edge("e", "c"))).toMatchObject({
      ok: false,
      code: "VIDEO_LOCKED",
    });
    const ok = [node("p", "Plate"), node("e", "Edit", approvedPass), node("c", "CropPair")];
    expect(validateEdge({ nodes: ok, edges: [edge("p", "e")] }, edge("e", "c"))).toEqual({ ok: true });
  });

  it("a Plate that is not approved cannot feed a Motion directly", () => {
    const ns = [node("p", "Plate"), node("m", "Motion")];
    expect(validateEdge({ nodes: ns, edges: [] }, edge("p", "m"))).toMatchObject({ code: "VIDEO_LOCKED" });
  });
});

describe("high-risk moves (doctrine 7)", () => {
  const motion = (extra: Partial<PlateNodeData>) => [node("p", "Plate", approvedPass), node("m", "Motion", extra)];

  it.each(["high", "highest"] as const)("blocks a %s-risk Motion without an override", (riskLabel) => {
    expect(validateEdge({ nodes: motion({ riskLabel }), edges: [] }, edge("p", "m"))).toMatchObject({
      ok: false,
      code: "HIGH_RISK_MOVE",
    });
  });

  it.each(["low", "med"] as const)("lets a %s-risk Motion through", (riskLabel) => {
    expect(validateEdge({ nodes: motion({ riskLabel }), edges: [] }, edge("p", "m"))).toEqual({ ok: true });
  });

  it("lets a high-risk Motion through once overridden, and the override carries the unseen-geometry label", () => {
    const m = applyOverride(node("m", "Motion", { riskLabel: "high" }), "director wants the reveal", "aasish");
    expect(m.data.labels).toEqual([UNSEEN_GEOMETRY_LABEL]);
    expect(validateEdge({ nodes: [node("p", "Plate", approvedPass), m], edges: [] }, edge("p", "m"))).toEqual({ ok: true });
  });
});

describe("a valid chain", () => {
  it("Plate -> Edit -> Comp -> Gate -> Export connects edge by edge", () => {
    const nodes = [node("p", "Plate"), node("e", "Edit"), node("c", "Comp"), node("g", "Gate"), node("x", "Export")];
    const edges: Edge[] = [];
    for (const [s, t] of [["p", "e"], ["e", "c"], ["c", "g"], ["g", "x"]] as const) {
      expect(canConnect(nodes, edges, s, t), `${s}->${t}`).toEqual({ ok: true });
      edges.push(edge(s, t));
    }
    expect(validateGraph({ nodes, edges })).toEqual([]);
    expect(recomputeDepths(nodes, edges).map((n) => n.data.generationDepth)).toEqual([0, 1, 1, 1, 1]);
  });
});

describe("cycles and unknown nodes", () => {
  it("refuses a back edge and a self loop", () => {
    const nodes = [node("p", "Plate"), node("a", "Comp"), node("b", "Comp")];
    const edges = [edge("p", "a"), edge("a", "b")];
    expect(canConnect(nodes, edges, "b", "a")).toEqual({ ok: false, reason: "cycle" });
    expect(canConnect(nodes, edges, "a", "a")).toEqual({ ok: false, reason: "cycle" });
    expect(validateEdge({ nodes, edges }, edge("b", "a"))).toMatchObject({ code: "CYCLE" });
  });

  it("generationDepth terminates on a cyclic graph", () => {
    const nodes = [node("p", "Plate"), node("a", "Edit"), node("b", "Edit")];
    const edges = [edge("p", "a"), edge("a", "b"), edge("b", "a")];
    expect(generationDepth(nodes, edges, "b")).toBeGreaterThanOrEqual(1);
  });

  it("refuses an edge to a node that does not exist", () => {
    expect(canConnect([node("p", "Plate")], [], "p", "ghost")).toEqual({ ok: false, reason: "unknown node" });
  });
});

describe("accepting a take (doctrine 7)", () => {
  const n = node("e", "Edit");
  it("is ok only when the scrub log says fullScrub true", () => {
    expect(canAccept(n, { id: "scrub-1", fullScrub: true })).toEqual({ ok: true });
  });

  it("is refused without a log, with a partial scrub, or with a missing flag", () => {
    for (const log of [null, undefined, { fullScrub: false }, { id: "s", fullScrub: false }]) {
      expect(canAccept(n, log)).toMatchObject({ ok: false, code: "SCRUB_REQUIRED" });
    }
  });
});

describe("overrides and labels", () => {
  it("appends the override without mutating the input", () => {
    const n = node("e", "Edit");
    const out = applyOverride(n, "because", "aasish", "2026-10-17T09:41:07Z");
    expect(out.data.overrides).toEqual([{ reason: "because", by: "aasish", at: "2026-10-17T09:41:07Z" }]);
    expect(n.data.overrides).toEqual([]);
    expect(out.data.labels).toEqual([]); // a plain Edit guesses nothing
  });

  it("does not duplicate the unseen-geometry label on a second override", () => {
    const first = applyOverride(node("e", "Edit", { params: { cameraOverride: { yaw_deg: 10 } } }), "r1", "a");
    const second = applyOverride(first, "r2", "b");
    expect(second.data.overrides).toHaveLength(2);
    expect(second.data.labels).toEqual([UNSEEN_GEOMETRY_LABEL]);
  });

  it("refuses a blank reason", () => {
    expect(() => applyOverride(node("e", "Edit"), "   ", "a")).toThrow(/reason/);
  });

  it("the unseen-geometry label follows the pixels downstream", () => {
    const e = applyOverride(node("e", "Edit", { params: { cameraOverride: true } }), "r", "a");
    const nodes = [node("p", "Plate"), e, node("c", "Comp"), node("g", "Gate"), node("other", "Comp")];
    const edges = [edge("p", "e"), edge("e", "c"), edge("c", "g")];
    expect(effectiveLabels({ nodes, edges }, "g")).toEqual([UNSEEN_GEOMETRY_LABEL]);
    expect(effectiveLabels({ nodes, edges }, "other")).toEqual([]);
    expect(effectiveLabels({ nodes, edges }, "p")).toEqual([]);
  });
});

describe("validateGraph", () => {
  it("lists every edge of a loaded graph that breaks a rule", () => {
    const nodes = [node("p", "Plate"), node("e", "Edit"), node("m", "Motion"), node("c", "Concept"), node("x", "Export")];
    const edges = [edge("p", "e"), edge("e", "m"), edge("c", "x")];
    const found = validateGraph({ nodes, edges });
    expect(found.map((v) => v.code).sort()).toEqual(["CONCEPT_TO_DELIVERY", "VIDEO_LOCKED"]);
  });
});
