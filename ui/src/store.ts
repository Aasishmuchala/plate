// The graph store. Every new edge goes through canvasRules.validateEdge; a refusal is recorded in `lastRefusal` /
// `lastRefusalCode` (shown by NodeList) and the edge is not added. Approval goes through canAccept, so a take is
// only ever approved with a logged full scrub. `loadProject` takes `project.json.graph` and `toGraph` returns it.
//
// Autosave (decision 31). With a project open, every structural change schedules `saveNow()` 400 ms later, so a
// burst of edits is one PUT: a node added or removed, an edge, or any node's data (the editors' params, a status, a
// linked packet). A move, a selection or xyflow measuring a node is not a structural change. The Make bar calls `saveNow()`
// itself before every quote. One PUT is on the wire at a time; a change made while it is out goes right after it.
// The daemon owns the node status: the stored graph it answers with carries the `gateStatus`, `approved`,
// `approvedBy` and `scrubLogId` it derived, and those are written back onto the nodes without scheduling another
// save. A 422 rule refusal sets `saveError` and adds the named edge to `violations`, which the canvas draws refused.
//
// T98. A move is saved too, but on its own slower clock: a node drag ending (`onNodeDragStop`) schedules a
// positions save 1500 ms later, so a run of drags is one PUT (a structural save due sooner carries the positions with
// it). Tidy, Clear and Duplicate are artist commands: Tidy and Clear save at once, Duplicate is a structural change.
// `saveState` is what the status-bar chip shows: `unsaved` from the first change until its PUT leaves, `saving` while
// it is out, `saved` (with `savedAt`) once the daemon stored the latest graph, `failed` after a refused or lost PUT.
// The canvas starts empty: no placeholder nodes; the artist opens or creates a project.

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
import { tidyPositions } from "./canvas/layout";
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

/** How long it waits after the last node drag before it PUTs the moved positions. */
export const POSITION_SAVE_DEBOUNCE_MS = 1500;

/** Where the open project's graph stands against the daemon's copy (the status-bar chip). */
export type SaveState = "saved" | "unsaved" | "saving" | "failed";

/** A duplicate sits this far right of and below its original. */
export const DUPLICATE_OFFSET_PX = 40;

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
  saveState: SaveState;
  /** When the daemon last stored this project's graph from this tab (epoch ms); null since the project was opened. */
  savedAt: number | null;
  loadProject: (graph: LoadableGraph, projectId?: string | null) => void;
  toGraph: () => Graph;
  /** Add a node; returns its id. */
  addNode: (kind: NodeKind, position: XYPosition, init?: Partial<PlateNodeData>) => string;
  /** Remove a node and every edge that touches it. */
  removeNode: (id: string) => void;
  /**
   * Copy nodes 40 px down and right, selected in place of the originals: the label, params and provenance labels
   * come along; the status (gate verdict, approval, scrub log, overrides) and the take (`outputHash`) do not, except
   * that a Plate keeps its packet link. Every edge into a copied node is re-made into its copy (from the copy of its
   * source when that was copied too) through the canvas rules, so a copy fed by an unapproved still stays unwired.
   * Returns the new ids.
   */
  duplicateNodes: (ids: readonly string[]) => string[];
  /** Empty the graph and save it at once. The caller asks the artist first. */
  clearGraph: () => Promise<void>;
  /** Lay the graph out by rank (canvas/layout.ts) and save the positions at once. */
  tidy: () => Promise<void>;
  /** xyflow's `onNodeDragStop`: schedule the positions save. */
  onNodeDragStop: () => void;
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

/** No project, no nodes (T98: the canvas shows its empty state until a project is opened). */
export function createInitialGraph(): Pick<
  GraphState,
  "projectId" | "nodes" | "edges" | "lastRefusal" | "lastRefusalCode" | "violations" | "saveError" | "saveState" | "savedAt"
> {
  return {
    projectId: null,
    nodes: [],
    edges: [],
    lastRefusal: null,
    lastRefusalCode: null,
    violations: [],
    saveError: null,
    saveState: "saved",
    savedAt: null,
  };
}

function nextId(kind: NodeKind, nodes: GraphNode[]): string {
  const taken = new Set(nodes.map((n) => n.id));
  for (let i = 1; ; i += 1) {
    const id = `${kind.toLowerCase()}-${i}`;
    if (!taken.has(id)) return id;
  }
}

/** A copy's data: what the artist set (label, params, provenance labels), none of what a run or a human decided. A
 * Plate's `outputHash`/`outputSize` are its packet's beauty, not a take, so a copied Plate keeps them. */
function duplicateData(d: PlateNodeData): PlateNodeData {
  const {
    approved: _approved,
    approvedBy: _approvedBy,
    scrubLogId: _scrubLogId,
    gateStatus: _gateStatus,
    overrides: _overrides,
    outputHash,
    outputSize,
    params,
    ...rest
  } = d;
  return makeNodeData(d.kind, {
    ...rest,
    labels: [...d.labels],
    ...(params ? { params: structuredClone(params) } : {}),
    ...(d.kind === "Plate" && outputHash ? { outputHash, ...(outputSize ? { outputSize: { ...outputSize } } : {}) } : {}),
  });
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
/** The slower positions save (a drag ended); a structural save that leaves first carries the positions. */
let positionTimer: ReturnType<typeof setTimeout> | null = null;
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
  if (positionTimer !== null) {
    clearTimeout(positionTimer);
    positionTimer = null;
  }
}

/** The chip's state; the store's own bookkeeping, never an edit. */
function setSaveState(saveState: SaveState, savedAt?: number): void {
  const s = useGraphStore.getState();
  if (s.saveState === saveState && savedAt === undefined) return;
  quietly(() => useGraphStore.setState(savedAt === undefined ? { saveState } : { saveState, savedAt }));
}

/** A structural change: remember it and (re)start the debounce. */
function markChanged(): void {
  revision += 1;
  cancelScheduledSave();
  saveTimer = setTimeout(() => {
    saveTimer = null;
    void useGraphStore.getState().saveNow();
  }, SAVE_DEBOUNCE_MS);
  setSaveState("unsaved");
}

/** Positions changed (nothing else): the PUT on the wire, if any, no longer carries the graph. */
function markMoved(): void {
  revision += 1;
  setSaveState("unsaved");
}

/** A drag ended: save the positions 1500 ms after the last one, unless a structural save is due first anyway. */
function schedulePositionSave(): void {
  markMoved();
  if (saveTimer !== null) return;
  if (positionTimer !== null) clearTimeout(positionTimer);
  positionTimer = setTimeout(() => {
    positionTimer = null;
    void useGraphStore.getState().saveNow();
  }, POSITION_SAVE_DEBOUNCE_MS);
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

async function putGraph(projectId: string, graph: Graph, carried: number): Promise<void> {
  let stored: LoadableGraph;
  try {
    stored = await saveGraph(projectId, graph);
  } catch (err) {
    if (useGraphStore.getState().projectId === projectId) {
      refuseSave(err);
      setSaveState("failed");
    }
    return;
  }
  // another project was opened while the PUT was out: its answer is not about this graph
  if (useGraphStore.getState().projectId !== projectId) return;
  applyStored(stored);
  // a change made while the PUT was out is still to be saved (its own timer or the queued follow-up sends it)
  if (revision === carried) setSaveState("saved", Date.now());
  else setSaveState("unsaved");
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
    const fresh = sameProject ? {} : { saveError: null, saveState: "saved" as const, savedAt: null };
    quietly(() => set({ projectId, edges, ...settled, ...noRefusal, ...fresh }));
    if (edited) markChanged();
  },
  toGraph: () => graphToWire(get().nodes, get().edges),
  addNode: (kind, position, init) => {
    const id = nextId(kind, get().nodes);
    set((s) => settle([...s.nodes, makeNode(id, kind, position, init)], s.edges));
    return id;
  },
  duplicateNodes: (ids) => {
    const { nodes, edges } = get();
    const wanted = new Set(ids);
    const originals = nodes.filter((n) => wanted.has(n.id));
    if (originals.length === 0) return [];
    const copyOf = new Map<string, string>();
    const copies: GraphNode[] = [];
    let taken = nodes;
    for (const n of originals) {
      const id = nextId(n.data.kind, taken);
      const at = { x: n.position.x + DUPLICATE_OFFSET_PX, y: n.position.y + DUPLICATE_OFFSET_PX };
      const copy: GraphNode = { ...flowNode(id, at, duplicateData(n.data)), selected: true };
      copyOf.set(n.id, id);
      copies.push(copy);
      taken = [...taken, copy];
    }
    const next = [...nodes.map((n) => (n.selected ? { ...n, selected: false } : n)), ...copies];
    // Re-make the incoming edges through the rules. A pass may unlock the next (a copied Plate -> Edit anchors the
    // copied Edit before its own copied downstream edge is checked), so repeat until a pass adds nothing.
    let pending = edges
      .filter((e) => copyOf.has(e.target))
      .map(
        (e): Connection => ({
          source: copyOf.get(e.source) ?? e.source,
          target: copyOf.get(e.target) as string,
          sourceHandle: e.sourceHandle ?? null,
          targetHandle: e.targetHandle ?? null,
        }),
      );
    let wired = edges;
    let refused: { message: string; code: RuleCode } | null = null;
    for (let progress = true; progress && pending.length > 0; ) {
      progress = false;
      const left: Connection[] = [];
      for (const c of pending) {
        const verdict = validateEdge({ nodes: next, edges: wired }, { id: `${c.source}->${c.target}`, ...c });
        if (verdict.ok) {
          wired = addEdge(c, wired);
          progress = true;
        } else {
          refused = { message: verdict.message, code: verdict.code };
          left.push(c);
        }
      }
      pending = left;
    }
    const why =
      pending.length > 0 && refused
        ? refusal(`${pending.length === 1 ? "1 edge" : `${pending.length} edges`} not copied: ${refused.message}`, refused.code)
        : noRefusal;
    set({ edges: wired, ...settle(next, wired), ...why });
    return copies.map((c) => c.id);
  },
  clearGraph: () => {
    set({ nodes: [], edges: [], violations: [], ...noRefusal });
    return get().saveNow();
  },
  tidy: () => {
    const { nodes, edges, projectId } = get();
    const placed = tidyPositions(nodes, edges);
    let moved = false;
    const next = nodes.map((n) => {
      const p = placed.get(n.id);
      if (!p || (p.x === n.position.x && p.y === n.position.y)) return n;
      moved = true;
      return { ...n, position: p };
    });
    if (!moved) return Promise.resolve();
    set({ nodes: next });
    if (projectId === null) return Promise.resolve();
    markMoved();
    return get().saveNow();
  },
  onNodeDragStop: () => {
    if (get().projectId !== null) schedulePositionSave();
  },
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
    const { source, target, sourceHandle, targetHandle } = connection;
    const verdict = validateEdge(
      { nodes, edges },
      { id: `${source}->${target}`, source, target, sourceHandle, targetHandle },
    );
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
    setSaveState("saving");
    inflight = putGraph(projectId, get().toGraph(), revision).finally(() => {
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
