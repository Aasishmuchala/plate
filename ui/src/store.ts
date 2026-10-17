// The graph store. Every new edge goes through canvasRules.validateEdge; a refusal is recorded in `lastRefusal` /
// `lastRefusalCode` (shown by NodeList) and the edge is not added. Approval goes through canAccept, so a take is
// only ever approved with a logged full scrub. `loadProject` takes `project.json.graph` and `toGraph` returns it.
//
// Autosave (decision 31). With a project open, every structural change schedules `saveNow()` 400 ms later, so a
// burst of edits is one PUT: a node added or removed, an edge, or any node's data (the editors' params, a status, a
// linked packet). A move, a selection or xyflow measuring a node is not a change. The Make bar calls `saveNow()`
// itself before every quote. One PUT is on the wire at a time; a change made while it is out goes right after it.
// The daemon owns the node status: the stored graph it answers with carries the `gateStatus`, `approved`,
// `approvedBy` and `scrubLogId` it derived, and those are written back onto the nodes without scheduling another
// save. A 422 rule refusal sets `saveError` and adds the named edge to `violations`, which the canvas draws refused.

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
import { ApiError, saveGraph } from "./api/client";
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
  type GateStatus,
  type Graph,
  type GraphEdge,
  type GraphNodeWire,
  type ImageSize,
  type LoadableGraph,
  makeNodeData,
  makePlateNodeData,
  NODE_KINDS,
  type NodeKind,
  type PlateNodeData,
  type PlateTier,
} from "./nodeTypes";

/** The node fields `setNodeStatus` may change. `approved: true` additionally needs a full-scrub log. */
export type NodeStatusPatch = Partial<
  Pick<PlateNodeData, "gateStatus" | "approved" | "approvedBy" | "scrubLogId" | "riskLabel" | "outputTier">
>;

/** How long the store waits after the last structural change before it PUTs the graph. */
export const SAVE_DEBOUNCE_MS = 400;

/** Why the last save failed. A rule refusal names its code (`VIDEO_LOCKED`, ...) and, for an edge rule, the edge. */
export interface SaveError {
  /** The canvas-rule code, else the daemon's error code; null when it gave none (a network failure). */
  code: string | null;
  message: string;
  edgeId?: string;
}

/** What the packet picker links a Plate node to: the packet, its tier and the display plate (CAS hash, size). */
export interface PlatePacketLink {
  packetId: string;
  tier: PlateTier;
  outputHash: string;
  outputSize: ImageSize;
}

export interface GraphState {
  projectId: string | null;
  nodes: GraphNode[];
  edges: Edge[];
  lastRefusal: string | null;
  lastRefusalCode: RuleCode | null;
  /** Rule violations found in the current graph (a loaded graph can break a rule; the daemon would refuse it), plus
   * the edges the daemon refused on the last save. */
  violations: RuleViolation[];
  /** The last save's failure; null after a good save. */
  saveError: SaveError | null;
  loadProject: (graph: LoadableGraph, projectId?: string | null) => void;
  toGraph: () => Graph;
  addNode: (kind: NodeKind, position: XYPosition, init?: Partial<PlateNodeData>) => void;
  /** Remove a node and every edge that touches it. */
  removeNode: (id: string) => void;
  onNodesChange: (changes: NodeChange<GraphNode>[]) => void;
  onEdgesChange: (changes: EdgeChange[]) => void;
  connect: (connection: Connection) => void;
  setNodeStatus: (id: string, patch: NodeStatusPatch, scrubLog?: ScrubLog | null) => void;
  overrideNode: (id: string, reason: string, by: string) => void;
  /** Link a Plate node to a packet (the Plate editor's picker). */
  setPlatePacket: (nodeId: string, link: PlatePacketLink) => void;
  /** PUT the graph now (cancels a scheduled save). Resolves once the daemon answered; the outcome is `saveError`. */
  saveNow: () => Promise<void>;
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
  "projectId" | "nodes" | "edges" | "lastRefusal" | "lastRefusalCode" | "violations" | "saveError"
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
    saveError: null,
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

// ---------------------------------------------------------------------------- autosave bookkeeping

let saveTimer: ReturnType<typeof setTimeout> | null = null;
/** The PUT on the wire, and the save queued behind it. */
let inflight: Promise<void> | null = null;
let followUp: Promise<void> | null = null;
/** Bumped on every structural change; `inflightRevision` is the revision the PUT on the wire carries. */
let revision = 0;
let inflightRevision = -1;
/** While above zero, store updates are the store's own bookkeeping (a load, the daemon's status), not edits. */
let quiet = 0;

function quietly(fn: () => void): void {
  quiet += 1;
  try {
    fn();
  } finally {
    quiet -= 1;
  }
}

function cancelScheduledSave(): void {
  if (saveTimer !== null) {
    clearTimeout(saveTimer);
    saveTimer = null;
  }
}

/** A structural change: remember it and (re)start the debounce. */
function markChanged(): void {
  revision += 1;
  cancelScheduledSave();
  saveTimer = setTimeout(() => {
    saveTimer = null;
    void useGraphStore.getState().saveNow();
  }, SAVE_DEBOUNCE_MS);
}

/** True when `b` differs from `a` in anything that is saved, other than node positions. Data objects are replaced on
 * every change (never mutated), so a reference test per node is enough; edges are compared by their endpoints
 * because a selection replaces the edge object. */
function structurallyChanged(a: Pick<GraphState, "nodes" | "edges">, b: Pick<GraphState, "nodes" | "edges">): boolean {
  if (a.nodes !== b.nodes) {
    if (a.nodes.length !== b.nodes.length) return true;
    for (let i = 0; i < b.nodes.length; i += 1) {
      if (a.nodes[i].id !== b.nodes[i].id || a.nodes[i].data !== b.nodes[i].data) return true;
    }
  }
  if (a.edges !== b.edges) {
    if (a.edges.length !== b.edges.length) return true;
    for (let i = 0; i < b.edges.length; i += 1) {
      const x = a.edges[i];
      const y = b.edges[i];
      if (
        x.id !== y.id ||
        x.source !== y.source ||
        x.target !== y.target ||
        (x.sourceHandle ?? null) !== (y.sourceHandle ?? null) ||
        (x.targetHandle ?? null) !== (y.targetHandle ?? null)
      ) {
        return true;
      }
    }
  }
  return false;
}

/** JSON with object keys sorted, so two graphs that differ only in key order compare equal. */
function stableJson(value: unknown): string {
  return JSON.stringify(value, (_key, v: unknown) =>
    v && typeof v === "object" && !Array.isArray(v)
      ? Object.fromEntries(Object.entries(v as Record<string, unknown>).sort(([x], [y]) => (x < y ? -1 : x > y ? 1 : 0)))
      : v,
  );
}

/** What a save carries apart from positions. */
function signature(nodes: GraphNode[], edges: Edge[]): string {
  return stableJson({
    nodes: nodes.map((n) => ({ id: n.id, data: n.data })),
    edges: edges.map((e) => [e.id, e.source, e.target, e.sourceHandle ?? null, e.targetHandle ?? null]),
  });
}

const GATE_STATUSES: ReadonlySet<string> = new Set<GateStatus>(["none", "pass", "fail", "advisory"]);

/** The daemon's status fields for one node, as a patch over `cur`; null when they already agree. */
function serverStatus(cur: PlateNodeData, wire: Record<string, unknown>): Partial<PlateNodeData> | null {
  const out: Partial<PlateNodeData> = {};
  const gate = wire.gateStatus;
  if (typeof gate === "string" && GATE_STATUSES.has(gate) && gate !== cur.gateStatus) out.gateStatus = gate as GateStatus;
  if (typeof wire.approved === "boolean") {
    if (wire.approved !== cur.approved) out.approved = wire.approved;
    // Not approved: the daemon drops both fields, so they go here too.
    const by = wire.approved && typeof wire.approvedBy === "string" ? wire.approvedBy : undefined;
    const log = wire.approved && typeof wire.scrubLogId === "string" ? wire.scrubLogId : undefined;
    if (by !== cur.approvedBy) out.approvedBy = by;
    if (log !== cur.scrubLogId) out.scrubLogId = log;
  }
  return Object.keys(out).length > 0 ? out : null;
}

/** A good save: take the daemon's node status, drop the refusals of an earlier failed save. */
function applyStored(stored: LoadableGraph): void {
  const s = useGraphStore.getState();
  const wire = new Map<string, Record<string, unknown>>();
  for (const n of stored.nodes) if (n && typeof n.id === "string") wire.set(n.id, n as unknown as Record<string, unknown>);
  const nodes = s.nodes.map((n) => {
    const w = wire.get(n.id);
    const patch = w ? serverStatus(n.data, w) : null;
    return patch ? { ...n, data: { ...n.data, ...patch } } : n;
  });
  quietly(() => useGraphStore.setState({ ...settle(nodes, s.edges), saveError: null }));
}

function describeFailure(err: unknown): { code: string | null; message: string } {
  if (err instanceof ApiError && err.body && typeof err.body === "object") {
    const b = err.body as Record<string, unknown>;
    const code = typeof b.error === "string" ? b.error : typeof b.code === "string" ? b.code : null;
    const message =
      typeof b.detail === "string"
        ? b.detail
        : Array.isArray(b.errors) && b.errors.length > 0
          ? b.errors.map(String).join("; ")
          : typeof b.message === "string"
            ? b.message
            : err.message;
    return { code, message };
  }
  return { code: null, message: err instanceof Error ? err.message : String(err) };
}

/** A failed save. A rule refusal marks the edges it names refused (`violations`, drawn by the canvas). */
function refuseSave(err: unknown): void {
  const s = useGraphStore.getState();
  const found = err instanceof ApiError ? (err.violations ?? []) : [];
  if (found.length === 0) {
    quietly(() => useGraphStore.setState({ saveError: describeFailure(err) }));
    return;
  }
  const first = found[0];
  const edgeId = first.edge?.id;
  const edges = new Map(s.edges.map((e) => [e.id, e]));
  const violations = [...s.violations];
  for (const v of found) {
    const edge = v.edge?.id ? edges.get(v.edge.id) : undefined;
    if (!edge || violations.some((x) => x.edge.id === edge.id && x.code === v.code)) continue;
    violations.push({ edge, code: v.code as RuleCode, message: v.message });
  }
  const saveError: SaveError = { code: first.code, message: first.message, ...(edgeId ? { edgeId } : {}) };
  quietly(() => useGraphStore.setState({ saveError, violations }));
}

async function putGraph(projectId: string, graph: Graph): Promise<void> {
  let stored: LoadableGraph;
  try {
    stored = await saveGraph(projectId, graph);
  } catch (err) {
    if (useGraphStore.getState().projectId === projectId) refuseSave(err);
    return;
  }
  // another project was opened while the PUT was out: its answer is not about this graph
  if (useGraphStore.getState().projectId === projectId) applyStored(stored);
}

// ---------------------------------------------------------------------------- the store

export const useGraphStore = create<GraphState>()((set, get) => ({
  ...createInitialGraph(),
  loadProject: (graph, projectId = null) => {
    const prev = get();
    const { nodes, edges } = graphFromWire(graph);
    const settled = settle(nodes, edges);
    const sameProject = projectId !== null && projectId === prev.projectId;
    // Opening a project is not an edit; re-loading the open one with a different graph is (drop-to-restore adds the
    // producing subgraph this way), and is saved like any other change.
    const edited = sameProject && signature(prev.nodes, prev.edges) !== signature(settled.nodes, edges);
    if (!sameProject) {
      cancelScheduledSave();
      revision += 1; // a PUT still out for the old project does not carry this graph
    }
    quietly(() => set({ projectId, edges, ...settled, ...noRefusal, ...(sameProject ? {} : { saveError: null }) }));
    if (edited) markChanged();
  },
  toGraph: () => graphToWire(get().nodes, get().edges),
  addNode: (kind, position, init) =>
    set((s) => {
      const nodes = [...s.nodes, makeNode(nextId(kind, s.nodes), kind, position, init)];
      return settle(nodes, s.edges);
    }),
  removeNode: (id) =>
    set((s) => {
      if (!s.nodes.some((n) => n.id === id)) return s;
      const nodes = s.nodes.filter((n) => n.id !== id);
      const edges = s.edges.filter((e) => e.source !== id && e.target !== id);
      return { edges, ...settle(nodes, edges) };
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
  setPlatePacket: (nodeId, link) => {
    const { nodes, edges } = get();
    const node = nodes.find((n) => n.id === nodeId);
    if (!node) {
      set(refusal("unknown node", "UNKNOWN_NODE"));
      return;
    }
    if (node.data.kind !== "Plate") {
      set(refusal(`'${node.data.label}' is not a Plate node`));
      return;
    }
    const data: PlateNodeData = {
      ...node.data,
      packetId: link.packetId,
      tier: link.tier,
      outputHash: link.outputHash,
      outputSize: { width: link.outputSize.width, height: link.outputSize.height },
    };
    set({ ...settle(nodes.map((n) => (n.id === nodeId ? { ...n, data } : n)), edges), ...noRefusal });
  },
  saveNow: () => {
    cancelScheduledSave();
    const projectId = get().projectId;
    if (projectId === null) return Promise.resolve();
    if (inflight) {
      // The PUT on the wire already carries this graph; otherwise send the current one as soon as it is back.
      if (inflightRevision === revision) return inflight;
      followUp ??= inflight.then(() => {
        followUp = null;
        return get().projectId === projectId ? get().saveNow() : undefined;
      });
      return followUp;
    }
    inflightRevision = revision;
    inflight = putGraph(projectId, get().toGraph()).finally(() => {
      inflight = null;
    });
    return inflight;
  },
}));

// Every store update is checked here, whichever path made it (the actions above, the editors' `setState` for
// params, the candidate strip writing a pick): a structural change on an open project schedules a save. Switching
// projects drops a save scheduled for the old one.
useGraphStore.subscribe((state, prev) => {
  if (state.projectId !== prev.projectId) {
    cancelScheduledSave();
    return;
  }
  if (quiet > 0 || state.projectId === null) return;
  if (structurallyChanged(prev, state)) markChanged();
});
