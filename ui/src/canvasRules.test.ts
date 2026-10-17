import type { Edge } from "@xyflow/react";
import { describe, expect, it } from "vitest";
import { canConnect, generationDepth, type GraphNode } from "./canvasRules";
import type { NodeType, PlateNodeData } from "./nodeTypes";

const node = (id: string, type: NodeType, extra: Partial<PlateNodeData> = {}): GraphNode => ({
  id,
  position: { x: 0, y: 0 },
  data: { type, label: type, ...extra },
});
const edge = (source: string, target: string): Edge => ({ id: `${source}->${target}`, source, target });

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
});

describe("concept tier (doctrine 1)", () => {
  it("refuses Concept -> Export", () => {
    const nodes = [node("c", "Concept", { tier: "concept" }), node("x", "Export")];
    expect(canConnect(nodes, [], "c", "x")).toEqual({ ok: false, reason: "concept tier cannot reach Export" });
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
});

describe("source-view-only (doctrine 2)", () => {
  it("refuses to feed a generator from a node that has no plate upstream", () => {
    const nodes = [node("m", "Mask"), node("e", "Edit"), node("c", "Concept")];
    expect(canConnect(nodes, [], "m", "e").ok).toBe(false);
    expect(canConnect(nodes, [], "c", "e")).toEqual({
      ok: false,
      reason: "generation must be anchored to a plate camera (source-view-only)",
    });
  });
});

describe("video lock (doctrine 7)", () => {
  const base = (data: Partial<PlateNodeData>): [GraphNode[], Edge[]] => [
    [node("p", "Plate"), node("e", "Edit", data), node("m", "Motion")],
    [edge("p", "e")],
  ];

  it("refuses Edit -> Motion until the still has passed gates and been approved", () => {
    const reason = "motion is locked until the upstream still passes gates and is approved";
    for (const data of [{ gatePassed: false }, { gatePassed: true }, { humanApproved: true }]) {
      const [nodes, edges] = base(data);
      expect(canConnect(nodes, edges, "e", "m")).toEqual({ ok: false, reason });
    }
  });

  it("allows Edit -> Motion once gatePassed and humanApproved", () => {
    const [nodes, edges] = base({ gatePassed: true, humanApproved: true });
    expect(canConnect(nodes, edges, "e", "m")).toEqual({ ok: true });
  });
});

describe("cycles", () => {
  it("refuses a back edge and a self loop", () => {
    const nodes = [node("p", "Plate"), node("a", "Comp"), node("b", "Comp")];
    const edges = [edge("p", "a"), edge("a", "b")];
    expect(canConnect(nodes, edges, "b", "a")).toEqual({ ok: false, reason: "cycle" });
    expect(canConnect(nodes, edges, "a", "a")).toEqual({ ok: false, reason: "cycle" });
  });

  it("generationDepth terminates on a cyclic graph", () => {
    const nodes = [node("p", "Plate"), node("a", "Edit"), node("b", "Edit")];
    const edges = [edge("p", "a"), edge("a", "b"), edge("b", "a")];
    expect(generationDepth(nodes, edges, "b")).toBeGreaterThanOrEqual(1);
  });
});
