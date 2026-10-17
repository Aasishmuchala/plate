// The graph store for the placeholder canvas. Every new edge goes through canvasRules.canConnect; a refusal is
// recorded in `lastRefusal` (shown by NodeList) and the edge is not added.

import {
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  type Connection,
  type Edge,
  type EdgeChange,
  type NodeChange,
  type XYPosition,
} from "@xyflow/react";
import { create } from "zustand";
import { canConnect, type GraphNode } from "./canvasRules";
import type { NodeType, PlateNodeData } from "./nodeTypes";

export interface GraphState {
  nodes: GraphNode[];
  edges: Edge[];
  lastRefusal: string | null;
  addNode: (type: NodeType, position: XYPosition) => void;
  onNodesChange: (changes: NodeChange<GraphNode>[]) => void;
  onEdgesChange: (changes: EdgeChange[]) => void;
  connect: (connection: Connection) => void;
}

function makeNode(id: string, type: NodeType, position: XYPosition): GraphNode {
  const data: PlateNodeData = { type, label: type, tier: type === "Concept" ? "concept" : "measured" };
  return { id, type: "default", position, data };
}

/** Plate -> Comp -> Gate, left to right: the smallest graph that shows a measured path to a gate. */
export function createInitialGraph(): Pick<GraphState, "nodes" | "edges" | "lastRefusal"> {
  return {
    nodes: [
      makeNode("plate-1", "Plate", { x: 0, y: 80 }),
      makeNode("comp-1", "Comp", { x: 260, y: 80 }),
      makeNode("gate-1", "Gate", { x: 520, y: 80 }),
    ],
    edges: [
      { id: "plate-1->comp-1", source: "plate-1", target: "comp-1" },
      { id: "comp-1->gate-1", source: "comp-1", target: "gate-1" },
    ],
    lastRefusal: null,
  };
}

function nextId(type: NodeType, nodes: GraphNode[]): string {
  const taken = new Set(nodes.map((n) => n.id));
  for (let i = 1; ; i += 1) {
    const id = `${type.toLowerCase()}-${i}`;
    if (!taken.has(id)) return id;
  }
}

export const useGraphStore = create<GraphState>()((set, get) => ({
  ...createInitialGraph(),
  addNode: (type, position) =>
    set((s) => ({ nodes: [...s.nodes, makeNode(nextId(type, s.nodes), type, position)] })),
  onNodesChange: (changes) => set((s) => ({ nodes: applyNodeChanges(changes, s.nodes) })),
  onEdgesChange: (changes) => set((s) => ({ edges: applyEdgeChanges(changes, s.edges) })),
  connect: (connection) => {
    const { nodes, edges } = get();
    const { source, target } = connection;
    const verdict = canConnect(nodes, edges, source, target);
    if (!verdict.ok) {
      set({ lastRefusal: verdict.reason });
      return;
    }
    set({ edges: addEdge(connection, edges), lastRefusal: null });
  },
}));
