import type { Connection } from "@xyflow/react";
import { beforeEach, describe, expect, it } from "vitest";
import { UNSEEN_GEOMETRY_LABEL, type Graph } from "./nodeTypes";
import { createInitialGraph, graphFromWire, graphToWire, useGraphStore } from "./store";

/** The graph these tests start from (the seed's old placeholder; since T98 the store itself starts empty). */
const SEED: Graph = {
  schema: 1,
  nodes: [
    { kind: "Plate", id: "plate-1", label: "Plate", packetId: "unlinked", tier: "A", position: { x: 0, y: 80 } } as Graph["nodes"][number],
    { kind: "Comp", id: "comp-1", label: "Comp", position: { x: 260, y: 80 } } as Graph["nodes"][number],
    { kind: "Gate", id: "gate-1", label: "Gate", position: { x: 520, y: 80 } } as Graph["nodes"][number],
  ],
  edges: [
    { id: "plate-1->comp-1", source: "plate-1", target: "comp-1", sourceHandle: null, targetHandle: null },
    { id: "comp-1->gate-1", source: "comp-1", target: "gate-1", sourceHandle: null, targetHandle: null },
  ],
};

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  useGraphStore.getState().loadProject(SEED);
});

const conn = (source: string, target: string): Connection => ({ source, target, sourceHandle: null, targetHandle: null });
const state = () => useGraphStore.getState();

describe("graph store", () => {
  it("starts empty with no project (T98); the seed loads as Plate -> Comp -> Gate", () => {
    expect(createInitialGraph()).toMatchObject({ projectId: null, nodes: [], edges: [], saveState: "saved", savedAt: null });
    const { nodes, edges, lastRefusal } = state();
    expect(nodes.map((n) => n.data.kind)).toEqual(["Plate", "Comp", "Gate"]);
    expect(edges).toHaveLength(2);
    expect(lastRefusal).toBeNull();
  });

  it("addNode appends a node with a unique id", () => {
    state().addNode("Edit", { x: 100, y: 200 });
    state().addNode("Edit", { x: 100, y: 300 });
    const { nodes } = state();
    expect(nodes).toHaveLength(5);
    expect(new Set(nodes.map((n) => n.id)).size).toBe(5);
    expect(nodes[3].data.label).toBe("Edit");
  });

  it("marks Concept nodes as the unmeasured tier and everything else as measured", () => {
    state().addNode("Concept", { x: 0, y: 0 });
    state().addNode("Edit", { x: 0, y: 0 });
    const { nodes } = state();
    expect(nodes[3].data.outputTier).toBe("concept_unmeasured");
    expect(nodes[3].className).toContain("concept-unmeasured");
    expect(nodes[4].data.outputTier).toBe("measured");
  });

  it("a refused connect leaves the edges alone and records the reason and code", () => {
    state().addNode("Concept", { x: 0, y: 0 });
    state().addNode("Export", { x: 0, y: 0 });
    state().connect(conn("concept-1", "export-1"));
    const after = state();
    expect(after.edges).toHaveLength(2);
    expect(after.lastRefusal).toBe("concept tier cannot reach Export");
    expect(after.lastRefusalCode).toBe("CONCEPT_TO_DELIVERY");
  });

  it("an allowed connect adds the edge and clears the refusal", () => {
    state().addNode("Export", { x: 0, y: 0 });
    useGraphStore.setState({ lastRefusal: "stale", lastRefusalCode: "CYCLE" });
    state().connect(conn("gate-1", "export-1"));
    const after = state();
    expect(after.edges).toHaveLength(3);
    expect(after.lastRefusal).toBeNull();
    expect(after.lastRefusalCode).toBeNull();
  });

  it("refuses a cycle", () => {
    state().connect(conn("gate-1", "plate-1"));
    expect(state().edges).toHaveLength(2);
    expect(state().lastRefusalCode).toBe("CYCLE");
  });

  it("recomputes generationDepth when edges change and refuses the third generation", () => {
    for (const k of ["Edit", "Edit", "Edit"] as const) state().addNode(k, { x: 0, y: 0 });
    state().connect(conn("plate-1", "edit-1"));
    state().connect(conn("edit-1", "edit-2"));
    expect(state().nodes.find((n) => n.id === "edit-2")?.data.generationDepth).toBe(2);
    state().connect(conn("edit-2", "edit-3"));
    expect(state().lastRefusalCode).toBe("DEPTH_EXCEEDED");
    expect(state().edges.find((e) => e.target === "edit-3")).toBeUndefined();
  });

  it("the video lock opens only after setNodeStatus passes the gate and records an approved full scrub", () => {
    state().addNode("Edit", { x: 0, y: 0 });
    state().addNode("Motion", { x: 0, y: 0 });
    state().connect(conn("plate-1", "edit-1"));
    state().connect(conn("edit-1", "motion-1"));
    expect(state().lastRefusalCode).toBe("VIDEO_LOCKED");

    state().setNodeStatus("edit-1", { gateStatus: "pass" });
    state().connect(conn("edit-1", "motion-1"));
    expect(state().lastRefusalCode).toBe("VIDEO_LOCKED"); // gates passed but nobody approved it

    state().setNodeStatus("edit-1", { approved: true, approvedBy: "aasish" }); // no scrub log
    expect(state().lastRefusalCode).toBe("SCRUB_REQUIRED");
    expect(state().nodes.find((n) => n.id === "edit-1")?.data.approved).toBe(false);

    state().setNodeStatus("edit-1", { approved: true, approvedBy: "aasish" }, { id: "scrub-7", fullScrub: true });
    const edit = state().nodes.find((n) => n.id === "edit-1");
    expect(edit?.data).toMatchObject({ approved: true, approvedBy: "aasish", scrubLogId: "scrub-7" });

    state().connect(conn("edit-1", "motion-1"));
    expect(state().lastRefusal).toBeNull();
    expect(state().edges.some((e) => e.source === "edit-1" && e.target === "motion-1")).toBe(true);
  });

  it("setNodeStatus on a missing node records a refusal", () => {
    state().setNodeStatus("ghost", { gateStatus: "pass" });
    expect(state().lastRefusalCode).toBe("UNKNOWN_NODE");
  });

  it("overrideNode labels an invented angle with 'unseen geometry guessed'", () => {
    state().addNode("Edit", { x: 0, y: 0 }, { params: { cameraOverride: { yaw_deg: 30 } } });
    state().connect(conn("plate-1", "edit-1"));
    expect(state().lastRefusalCode).toBe("INVENTED_ANGLE");
    state().overrideNode("edit-1", "client asked for the east side", "aasish");
    expect(state().nodes.find((n) => n.id === "edit-1")?.data.labels).toContain(UNSEEN_GEOMETRY_LABEL);
    state().connect(conn("plate-1", "edit-1"));
    expect(state().lastRefusal).toBeNull();
  });
});

describe("loadProject / toGraph", () => {
  const wire: Graph = {
    schema: 1,
    nodes: [
      { kind: "Plate", id: "p", label: "Plate A", packetId: "pk_1", tier: "A", position: { x: 0, y: 0 } } as Graph["nodes"][number],
      { kind: "Edit", id: "e", label: "Edit", position: { x: 200, y: 0 } } as Graph["nodes"][number],
      { kind: "Gate", id: "g", label: "Gate", gateStatus: "pass", position: { x: 400, y: 0 } } as Graph["nodes"][number],
    ],
    edges: [
      { id: "p->e", source: "p", target: "e", sourceHandle: null, targetHandle: null },
      { id: "e->g", source: "e", target: "g", sourceHandle: "out", targetHandle: "in" },
    ],
  };

  it("loads a project.json graph, fills defaults and derives the depth", () => {
    state().loadProject(wire, "proj-1");
    const s = state();
    expect(s.projectId).toBe("proj-1");
    expect(s.nodes.map((n) => n.id)).toEqual(["p", "e", "g"]);
    expect(s.nodes[0].data).toMatchObject({ kind: "Plate", packetId: "pk_1", tier: "A", generationDepth: 0 });
    expect(s.nodes[1].data).toMatchObject({ gateStatus: "none", approved: false, overrides: [], outputTier: "measured" });
    expect(s.nodes.map((n) => n.data.generationDepth)).toEqual([0, 1, 1]);
    expect(s.violations).toEqual([]);
    expect(s.lastRefusal).toBeNull();
  });

  it("toGraph round-trips the wire form", () => {
    state().loadProject(wire);
    const out = state().toGraph();
    expect(out.schema).toBe(1);
    expect(out.edges).toEqual(wire.edges);
    expect(out.nodes.map((n) => [n.id, n.kind, n.position])).toEqual(wire.nodes.map((n) => [n.id, n.kind, n.position]));
    state().loadProject(out);
    expect(state().toGraph()).toEqual(out);
  });

  it("reports the edges of a loaded graph that break a canvas rule", () => {
    state().loadProject({
      schema: 1,
      nodes: [
        { kind: "Concept", id: "c", label: "Concept" } as Graph["nodes"][number],
        { kind: "Export", id: "x", label: "Export" } as Graph["nodes"][number],
      ],
      edges: [{ id: "c->x", source: "c", target: "x", sourceHandle: null, targetHandle: null }],
    });
    expect(state().violations.map((v) => v.code)).toEqual(["CONCEPT_TO_DELIVERY"]);
  });

  it("rejects an unknown node kind, so there is no way to load a text-to-image node", () => {
    const bad = { schema: 1, nodes: [{ kind: "ImageCreator", id: "z", label: "x" }], edges: [] } as unknown as Graph;
    expect(() => graphFromWire(bad)).toThrow(/unknown node kind/);
  });

  it("graphToWire keeps handles as null when absent", () => {
    const { nodes, edges } = graphFromWire(wire);
    expect(graphToWire(nodes, edges).edges[0].sourceHandle).toBeNull();
  });
});
