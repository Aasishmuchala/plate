import { beforeEach, describe, expect, it } from "vitest";
import { createInitialGraph, useGraphStore } from "./store";

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
});

describe("graph store", () => {
  it("starts as Plate -> Comp -> Gate", () => {
    const { nodes, edges, lastRefusal } = useGraphStore.getState();
    expect(nodes.map((n) => n.data.type)).toEqual(["Plate", "Comp", "Gate"]);
    expect(edges).toHaveLength(2);
    expect(lastRefusal).toBeNull();
  });

  it("addNode appends a node with a unique id", () => {
    useGraphStore.getState().addNode("Edit", { x: 100, y: 200 });
    useGraphStore.getState().addNode("Edit", { x: 100, y: 300 });
    const { nodes } = useGraphStore.getState();
    expect(nodes).toHaveLength(5);
    expect(new Set(nodes.map((n) => n.id)).size).toBe(5);
    expect(nodes[3].data.label).toBe("Edit");
  });

  it("marks Concept nodes as the concept tier", () => {
    useGraphStore.getState().addNode("Concept", { x: 0, y: 0 });
    expect(useGraphStore.getState().nodes[3].data.tier).toBe("concept");
  });

  it("a refused connect leaves the edges alone and records the reason", () => {
    const s = useGraphStore.getState();
    s.addNode("Concept", { x: 0, y: 0 });
    s.addNode("Export", { x: 0, y: 0 });
    s.connect({ source: "concept-1", target: "export-1", sourceHandle: null, targetHandle: null });
    const after = useGraphStore.getState();
    expect(after.edges).toHaveLength(2);
    expect(after.lastRefusal).toBe("concept tier cannot reach Export");
  });

  it("an allowed connect adds the edge and clears the refusal", () => {
    const s = useGraphStore.getState();
    s.addNode("Export", { x: 0, y: 0 });
    useGraphStore.setState({ lastRefusal: "stale" });
    s.connect({ source: "gate-1", target: "export-1", sourceHandle: null, targetHandle: null });
    const after = useGraphStore.getState();
    expect(after.edges).toHaveLength(3);
    expect(after.lastRefusal).toBeNull();
  });
});
