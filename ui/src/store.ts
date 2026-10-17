// The graph store. Every new edge goes through canvasRules.validateEdge; a refusal is recorded in `lastRefusal` /
// `lastRefusalCode` (shown by NodeList) and the edge is not added. Approval goes through canAccept, so a take is
// only ever approved with a logged full scrub. `loadProject` takes `project.json.graph` and `toGraph` returns it.

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
import {
  applyOverride,
  canAccept,
  type GraphNode,
  recomputeDepths,
  type RuleCode,
  type RuleViolation,
  type ScrubLog,
  validateEdge,
  validateGraph,
} from "./canvasRules";
import {
  type Graph,
  type GraphEdge,
  type GraphNodeWire,
  type LoadableGraph,
  makeNodeData,
  makePlateNodeData,
  NODE_KINDS,
  type NodeKind,
  type PlateNodeData,
} from "./nodeTypes";

/** The node fields `setNodeStatus` may change. `approved: true` additionally needs a full-scrub log. */
export type NodeStatusPatch = Partial<
  Pick<PlateNodeData, "gateStatus" | "approved" | "approvedBy" | "scrubLogId" | "riskLabel" | "outputTier">
>;

export interface GraphState {
  projectId: string | null;
  nodes: GraphNode[];
  edges: Edge[];
  lastRefusal: string | null;
  lastRefusalCode: RuleCode | null;
  /** Rule violations found in the current graph (a loaded graph can break a rule; the daemon would refuse it). */
  violations: RuleViolation[];
  loadProject: (graph: LoadableGraph, projectId?: string | null) => void;
  toGraph: () => Graph;
  addNode: (kind: NodeKind, position: XYPosition, init?: Partial<PlateNodeData>) => void;
  onNodesChange: (changes: NodeChange<GraphNode>[]) => void;
  onEdgesChange: (changes: EdgeChange[]) => void;
  connect: (connection: Connection) => void;
  setNodeStatus: (id: string, patch: NodeStatusPatch, scrubLog?: ScrubLog | null) => void;
  overrideNode: (id: string, reason: string, by: string) => void;
}

/** A plain default xyflow box; the class lets the stylesheet mark each kind and the concept tier. */
function flowNode(id: string, position: XYPosition, data: PlateNodeData): GraphNode {
  const tier = data.outputTier === "concept_unmeasured" ? " concept-unmeasured" : "";
  return { id, type: "default", position, className: `kind-${data.kind.toLowerCase()}${tier}`, data };
}

function makeNode(id: string, kind: NodeKind, position: XYPosition, init: Partial<PlateNodeData> = {}): GraphNode {
  const data = kind === "Plate" ? makePlateNodeData("unlinked", "A", init) : makeNodeData(kind, init);
  return flowNode(id, position, data);
}

/** Plate -> Comp -> Gate, left to right: the smallest graph that shows a measured path to a gate. */
export function createInitialGraph(): Pick<
  GraphState,
  "projectId" | "nodes" | "edges" | "lastRefusal" | "lastRefusalCode" | "violations"
> {
  return {
    projectId: null,
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
    lastRefusalCode: null,
    violations: [],
  };
}

function nextId(kind: NodeKind, nodes: GraphNode[]): string {
  const taken = new Set(nodes.map((n) => n.id));
  for (let i = 1; ; i += 1) {
    const id = `${kind.toLowerCase()}-${i}`;
    if (!taken.has(id)) return id;
  }
}

/** `project.json.graph` -> store form. Missing node fields get their defaults; an unknown kind throws. */
export function graphFromWire(graph: LoadableGraph): { nodes: GraphNode[]; edges: Edge[] } {
  const nodes = graph.nodes.map((w): GraphNode => {
    const { id, position, ...rest } = w;
    if (!(NODE_KINDS as readonly string[]).includes(rest.kind)) {
      throw new Error(`unknown node kind '${String(rest.kind)}' (${id})`);
    }
    return flowNode(id, position ?? { x: 0, y: 0 }, makeNodeData(rest.kind, rest));
  });
  const edges: Edge[] = graph.edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    sourceHandle: e.sourceHandle ?? null,
    targetHandle: e.targetHandle ?? null,
  }));
  return { nodes, edges };
}

/** Store form -> `project.json.graph`. */
export function graphToWire(nodes: GraphNode[], edges: Edge[]): Graph {
  return {
    schema: 1,
    nodes: nodes.map((n): GraphNodeWire => ({ ...n.data, id: n.id, position: { x: n.position.x, y: n.position.y } })),
    edges: edges.map(
      (e): GraphEdge => ({
        id: e.id,
        source: e.source,
        target: e.target,
        sourceHandle: e.sourceHandle ?? null,
        targetHandle: e.targetHandle ?? null,
      }),
    ),
  };
}

/** Refresh the derived `generationDepth` and the violation list after a structural change. */
function settle(nodes: GraphNode[], edges: Edge[]): { nodes: GraphNode[]; violations: RuleViolation[] } {
  const fresh = recomputeDepths(nodes, edges);
  return { nodes: fresh, violations: validateGraph({ nodes: fresh, edges }) };
}

const refusal = (message: string, code: RuleCode | null = null) => ({ lastRefusal: message, lastRefusalCode: code });
const noRefusal = { lastRefusal: null, lastRefusalCode: null } as const;

export const useGraphStore = create<GraphState>()((set, get) => ({
  ...createInitialGraph(),
  loadProject: (graph, projectId = null) => {
    const { nodes, edges } = graphFromWire(graph);
    set({ projectId, edges, ...settle(nodes, edges), ...noRefusal });
  },
  toGraph: () => graphToWire(get().nodes, get().edges),
  addNode: (kind, position, init) =>
    set((s) => {
      const nodes = [...s.nodes, makeNode(nextId(kind, s.nodes), kind, position, init)];
      return settle(nodes, s.edges);
    }),
  onNodesChange: (changes) =>
    set((s) => {
      const nodes = applyNodeChanges(changes, s.nodes);
      return changes.some((c) => c.type === "remove") ? settle(nodes, s.edges) : { nodes };
    }),
  onEdgesChange: (changes) =>
    set((s) => {
      const edges = applyEdgeChanges(changes, s.edges);
      return changes.some((c) => c.type !== "select") ? { edges, ...settle(s.nodes, edges) } : { edges };
    }),
  connect: (connection) => {
    const { nodes, edges } = get();
    const { source, target } = connection;
    const verdict = validateEdge({ nodes, edges }, { id: `${source}->${target}`, source, target });
    if (!verdict.ok) {
      set(refusal(verdict.message, verdict.code));
      return;
    }
    const next = addEdge(connection, edges);
    set({ edges: next, ...settle(nodes, next), ...noRefusal });
  },
  setNodeStatus: (id, patch, scrubLog) => {
    const { nodes, edges } = get();
    const node = nodes.find((n) => n.id === id);
    if (!node) {
      set(refusal("unknown node", "UNKNOWN_NODE"));
      return;
    }
    const next: Partial<PlateNodeData> = { ...patch };
    if (patch.approved === true && !node.data.approved) {
      const verdict = canAccept(node, scrubLog);
      if (!verdict.ok) {
        set(refusal(verdict.message, verdict.code));
        return;
      }
      next.scrubLogId = patch.scrubLogId ?? scrubLog?.id;
    } else if (patch.approved === false) {
      next.approvedBy = undefined;
      next.scrubLogId = undefined;
    }
    const updated = nodes.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...next } } : n));
    set({ ...settle(updated, edges), ...noRefusal });
  },
  overrideNode: (id, reason, by) => {
    const { nodes, edges } = get();
    const node = nodes.find((n) => n.id === id);
    if (!node) {
      set(refusal("unknown node", "UNKNOWN_NODE"));
      return;
    }
    try {
      const overridden = applyOverride(node, reason, by);
      set({ ...settle(nodes.map((n) => (n.id === id ? overridden : n)), edges), ...noRefusal });
    } catch (err) {
      set(refusal(err instanceof Error ? err.message : "override failed"));
    }
  },
}));
