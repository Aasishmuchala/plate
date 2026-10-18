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
// it is out, `saved` (with `savedAt`) once the daemon stored the latest graph, `failed` after a refused or lost PUT,
// `conflict` after another writer moved the graph under this tab. The canvas starts empty: no placeholder nodes; the
// artist opens or creates a project.
//
// T147, decision 77. The graph has the daemon's `revision`: a load that carries one (the daemon's copy) adopts it,
// every PUT sends it and the answer's replaces it. A 409 `graph_conflict` stops saving (`saveState: "conflict"`,
// `conflict.current`) until the artist reloads; `reloadGraph` takes the daemon's graph and keeps the unsaved Edit
// prompts that differ from it in `conflictLocal` (offered as `copy prompt`). Any other failed PUT that is not a 422
// rule refusal is retried 1, 2, 4, 8, then every 15 s (`SAVE_RETRY_MS`) until it lands or the project changes, and
// at once when the daemon comes back (`saveIfPending`). Continuous typing still saves every `SAVE_MAX_WAIT_MS`. A
// `graph.saved` from another writer (`noteRemoteSave`) reads the head: a newer revision is reloaded, or is a
// conflict when this tab has unsaved changes. `installPageLifecycle` flushes a pending save with a keepalive PUT on
// pagehide or when the page is hidden, and asks before unloading while the daemon does not have the latest graph.

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
import { ApiError, getGraph, revisionOf, type SaveOptions, type StoredGraph, saveGraph } from "./api/client";
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

/** The longest a run of changes waits for its PUT: continuous typing still saves this often. */
export const SAVE_MAX_WAIT_MS = 2000;

/** How long it waits after the last node drag before it PUTs the moved positions. */
export const POSITION_SAVE_DEBOUNCE_MS = 1500;

/** The waits before each retry of a failed save; the last one repeats until the save lands. */
export const SAVE_RETRY_MS: readonly number[] = [1000, 2000, 4000, 8000, 15000];

/** What the browser shows (or, today, ignores in favour of its own text) when the tab is left with unsaved work. */
export const UNLOAD_WARNING = "The daemon does not have the latest graph yet; leaving now loses the last changes.";

/** Where the open project's graph stands against the daemon's copy (the status-bar chip). */
export type SaveState = "saved" | "unsaved" | "saving" | "failed" | "conflict";

/** The daemon's answer to a stale save: the revision it holds and the one this tab sent (null: unknown). */
export interface SaveConflict {
  current: number | null;
  sent: number | null;
}

/** An Edit node's prompt as this tab had it. */
export interface LocalPrompt {
  nodeId: string;
  label: string;
  prompt: string;
}

/** The artist's copy at a conflict: the whole graph, and the Edit prompts a Reload would lose. */
export interface ConflictLocal {
  graph: Graph;
  prompts: LocalPrompt[];
}

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
  /** The daemon's revision of the graph this tab holds (decision 77); null when unknown (an older daemon or reader). */
  revision: number | null;
  /** Set while `saveState` is `conflict`. */
  conflict: SaveConflict | null;
  /** Kept from the last conflict; after a Reload, only the Edit prompts that differ from the daemon's graph. */
  conflictLocal: ConflictLocal | null;
  /** The wait before the scheduled retry of a failed save (ms); null when none is scheduled. */
  retryInMs: number | null;
  /** Bumped whenever the graph is replaced from outside the canvas (a load, a Reload, another writer's save), so the
   * editors that hold a draft of a node's params (the Edit prompt box) remount and read the new graph. */
  graphEpoch: number;
  /** Open a graph. One that carries the daemon's `revision` is the stored copy (never an edit); one without it is,
   * for the open project, an edit to save (drop-to-restore). */
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
  /** PUT the graph now (cancels a scheduled save). Resolves once the daemon answered; the outcome is `saveError`.
   * Nothing is sent while `saveState` is `conflict`. */
  saveNow: (opts?: SaveOptions) => Promise<void>;
  /** The conflict chip's Reload: take the daemon's graph; the unsaved Edit prompts it lacks stay in `conflictLocal`. */
  reloadGraph: () => Promise<void>;
  /** Forget `conflictLocal` (the artist copied what they wanted). */
  dismissConflictLocal: () => void;
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
  | "projectId"
  | "nodes"
  | "edges"
  | "lastRefusal"
  | "lastRefusalCode"
  | "violations"
  | "saveError"
  | "saveState"
  | "savedAt"
  | "revision"
  | "conflict"
  | "conflictLocal"
  | "retryInMs"
  | "graphEpoch"
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
    revision: null,
    conflict: null,
    conflictLocal: null,
    retryInMs: null,
    graphEpoch: 0,
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
/** The ceiling on a run of changes (`SAVE_MAX_WAIT_MS`): started by its first change, not restarted by the rest. */
let maxWaitTimer: ReturnType<typeof setTimeout> | null = null;
/** The slower positions save (a drag ended); a structural save that leaves first carries the positions. */
let positionTimer: ReturnType<typeof setTimeout> | null = null;
/** The next retry of a failed save, and how many retries this failure has had. */
let retryTimer: ReturnType<typeof setTimeout> | null = null;
let retryStep = 0;
/** The PUT on the wire, and the save queued behind it. */
let inflight: Promise<void> | null = null;
let followUp: Promise<void> | null = null;
/** Bumped on every change to save; `inflightSeq` is the one the PUT on the wire carries. (Not the daemon's
 * `revision`, which counts the daemon's saves.) */
let editSeq = 0;
let inflightSeq = -1;
/** The highest revision another writer's `graph.saved` named while it could not be checked (a PUT or a check was
 * out); `Infinity` when it named none. */
let remoteHint: number | null = null;
let remoteCheck: Promise<void> | null = null;
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

function clear(timer: ReturnType<typeof setTimeout> | null): null {
  if (timer !== null) clearTimeout(timer);
  return null;
}

/** Drop every scheduled save: the debounce, its ceiling, the positions save and a retry. */
function cancelScheduledSave(): void {
  saveTimer = clear(saveTimer);
  maxWaitTimer = clear(maxWaitTimer);
  positionTimer = clear(positionTimer);
  retryTimer = clear(retryTimer);
}

/** The chip's state; the store's own bookkeeping, never an edit. */
function setSaveState(saveState: SaveState, savedAt?: number): void {
  const s = useGraphStore.getState();
  if (s.saveState === saveState && savedAt === undefined) return;
  quietly(() => useGraphStore.setState(savedAt === undefined ? { saveState } : { saveState, savedAt }));
}

const inConflict = (): boolean => useGraphStore.getState().saveState === "conflict";

/** A structural change: remember it and (re)start the debounce; the first change of a run starts its ceiling.
 * Whichever fires first saves, and the save cancels the other. */
function markChanged(): void {
  editSeq += 1;
  if (inConflict()) return; // nothing is saved over another writer's graph until the artist reloads
  saveTimer = clear(saveTimer);
  positionTimer = clear(positionTimer);
  saveTimer = setTimeout(() => {
    saveTimer = null;
    void useGraphStore.getState().saveNow();
  }, SAVE_DEBOUNCE_MS);
  maxWaitTimer ??= setTimeout(() => {
    maxWaitTimer = null;
    void useGraphStore.getState().saveNow();
  }, SAVE_MAX_WAIT_MS);
  setSaveState("unsaved");
}

/** Positions changed (nothing else): the PUT on the wire, if any, no longer carries the graph. */
function markMoved(): void {
  editSeq += 1;
  if (!inConflict()) setSaveState("unsaved");
}

/** A drag ended: save the positions 1500 ms after the last one, unless a structural save is due first anyway. */
function schedulePositionSave(): void {
  markMoved();
  if (inConflict() || saveTimer !== null) return;
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

/** A good save: take the daemon's node status and revision, drop the refusals of an earlier failed save. */
function applyStored(stored: StoredGraph): void {
  const s = useGraphStore.getState();
  const wire = new Map<string, Record<string, unknown>>();
  for (const n of stored.nodes) if (n && typeof n.id === "string") wire.set(n.id, n as unknown as Record<string, unknown>);
  const nodes = s.nodes.map((n) => {
    const w = wire.get(n.id);
    const patch = w ? serverStatus(n.data, w) : null;
    return patch ? { ...n, data: { ...n.data, ...patch } } : n;
  });
  const revision = revisionOf(stored) ?? s.revision;
  quietly(() => useGraphStore.setState({ ...settle(nodes, s.edges), saveError: null, revision, retryInMs: null }));
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

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;
const asRevision = (v: unknown): number | null => (typeof v === "number" && Number.isInteger(v) && v >= 0 ? v : null);

/** The daemon's 409 `graph_conflict` (the graph moved since this tab read it), or null for any other failure. */
function conflictOf(err: unknown): SaveConflict | null {
  if (!(err instanceof ApiError) || err.status !== 409 || !isRecord(err.body) || err.body.error !== "graph_conflict") {
    return null;
  }
  return { current: asRevision(err.body.current), sent: asRevision(err.body.sent) };
}

/** Everything but a rule refusal (422) and a conflict heals by itself: the network, a 5xx, `graph_busy`, a 429. */
const retryable = (err: unknown): boolean => !(err instanceof ApiError) || (err.status !== 422 && conflictOf(err) === null);

/** Try a failed save again after the next wait of `SAVE_RETRY_MS`. */
function scheduleRetry(): void {
  retryTimer = clear(retryTimer);
  const wait = SAVE_RETRY_MS[Math.min(retryStep, SAVE_RETRY_MS.length - 1)];
  retryStep += 1;
  retryTimer = setTimeout(() => {
    retryTimer = null;
    void useGraphStore.getState().saveNow();
  }, wait);
  quietly(() => useGraphStore.setState({ retryInMs: wait }));
}

/** The Edit prompts of a graph that are not blank. */
function editPrompts(nodes: GraphNode[]): LocalPrompt[] {
  const out: LocalPrompt[] = [];
  for (const n of nodes) {
    const prompt = n.data.kind === "Edit" ? n.data.params?.prompt : undefined;
    if (typeof prompt === "string" && prompt.trim() !== "") out.push({ nodeId: n.id, label: n.data.label, prompt });
  }
  return out;
}

/** Another writer moved the graph: stop saving over it, keep the artist's copy, and say so on the chip. */
function enterConflict(conflict: SaveConflict): void {
  cancelScheduledSave();
  const s = useGraphStore.getState();
  const local = s.conflictLocal ?? { graph: s.toGraph(), prompts: editPrompts(s.nodes) };
  const at = conflict.current === null ? "" : ` (now revision ${conflict.current})`;
  const saveError: SaveError = {
    code: "graph_conflict",
    message: `the graph was changed elsewhere${at}; reload it to go on`,
  };
  quietly(() =>
    useGraphStore.setState({ saveState: "conflict", conflict, conflictLocal: local, saveError, retryInMs: null }),
  );
}

/** Replace the open graph with the daemon's copy (nothing unsaved is lost: the caller checked). */
function applyRemote(head: StoredGraph): void {
  const s = useGraphStore.getState();
  const { nodes, edges } = graphFromWire(head);
  const selected = new Set(s.nodes.filter((n) => n.selected).map((n) => n.id));
  const kept = nodes.map((n) => (selected.has(n.id) ? { ...n, selected: true } : n));
  cancelScheduledSave();
  retryStep = 0;
  editSeq += 1; // a PUT still out does not carry this graph
  quietly(() =>
    useGraphStore.setState({
      edges,
      ...settle(kept, edges),
      ...noRefusal,
      revision: revisionOf(head),
      conflict: null,
      saveError: null,
      saveState: "saved",
      retryInMs: null,
      graphEpoch: s.graphEpoch + 1,
    }),
  );
}

/** Read the head after another writer's `graph.saved` and reload it, or flag a conflict over unsaved changes. */
async function checkHead(projectId: string): Promise<void> {
  let head: StoredGraph;
  try {
    head = await getGraph(projectId);
  } catch {
    return; // the next graph.saved, or this tab's next save (a 409 when stale), tells again
  }
  const s = useGraphStore.getState();
  if (s.projectId !== projectId || s.saveState === "conflict") return;
  const theirs = revisionOf(head);
  if (theirs === null || (s.revision !== null && theirs <= s.revision)) return; // nothing newer than ours
  if (inflight) {
    // a PUT left while the head was read: its answer (stored, or a 409) decides; look again after it
    remoteHint = Math.max(remoteHint ?? theirs, theirs);
    return;
  }
  try {
    if (s.saveState === "saved") applyRemote(head);
    // Unsaved here and a revision of ours to compare with: a conflict. With no revision of ours this tab cannot
    // tell another writer from its own history; its next save (sent with none) is stored and adopts the revision.
    else if (s.revision !== null) enterConflict({ current: theirs, sent: s.revision });
  } catch (err) {
    console.error("the daemon's graph could not be loaded", err);
  }
}

/** Run the deferred head check once nothing is out; a hint this tab's own save has since caught up with is dropped. */
function runRemoteCheck(projectId: string): void {
  if (remoteHint === null || inflight || remoteCheck) return;
  const hint = remoteHint;
  remoteHint = null;
  const ours = useGraphStore.getState().revision;
  if (ours !== null && hint <= ours) return; // it was the echo of the PUT that just came back
  remoteCheck = checkHead(projectId).finally(() => {
    remoteCheck = null;
    if (useGraphStore.getState().projectId === projectId) runRemoteCheck(projectId);
  });
}

/**
 * A `graph.saved` for `projectId` naming `revision` (null: it named none). This tab's own save, or anything not
 * newer than the revision it holds, is ignored; otherwise the head is read once nothing is out (runEvents).
 */
export function noteRemoteSave(projectId: string, revision: number | null): void {
  const s = useGraphStore.getState();
  if (s.projectId !== projectId) return;
  if (revision !== null && s.revision !== null && revision <= s.revision) return;
  const hint = revision ?? Number.POSITIVE_INFINITY;
  remoteHint = remoteHint === null ? hint : Math.max(remoteHint, hint);
  runRemoteCheck(projectId);
}

/** Send the pending save now, if there is one (the daemon came back, a token was pasted). */
export function saveIfPending(): void {
  const s = useGraphStore.getState();
  if (s.projectId !== null && s.saveState !== "saved" && s.saveState !== "conflict") void s.saveNow();
}

/** The page is going away (pagehide) or hidden (mobile browsers may never fire pagehide): send what is pending
 * with a keepalive PUT. A PUT already out is left alone: the unload warning covered what came after it. */
export function flushPendingSave(): void {
  const s = useGraphStore.getState();
  if (s.projectId === null || s.saveState === "saved" || s.saveState === "conflict" || inflight) return;
  void s.saveNow({ keepalive: true });
}

/** `beforeunload`: ask before leaving while the daemon does not have the latest graph. */
export function guardUnload(event: Event): void {
  const s = useGraphStore.getState();
  if (s.projectId === null || s.saveState === "saved") return;
  event.preventDefault();
  (event as BeforeUnloadEvent).returnValue = UNLOAD_WARNING;
}

/** Flush on pagehide and on hidden, warn on unload. Returns the uninstall function (App mounts it). */
export function installPageLifecycle(win: Window = window): () => void {
  const onHidden = () => {
    if (win.document.visibilityState === "hidden") flushPendingSave();
  };
  win.addEventListener("pagehide", flushPendingSave);
  win.document.addEventListener("visibilitychange", onHidden);
  win.addEventListener("beforeunload", guardUnload);
  return () => {
    win.removeEventListener("pagehide", flushPendingSave);
    win.document.removeEventListener("visibilitychange", onHidden);
    win.removeEventListener("beforeunload", guardUnload);
  };
}

async function putGraph(
  projectId: string,
  graph: Graph,
  carried: number,
  revision: number | null,
  opts: SaveOptions,
): Promise<void> {
  let stored: StoredGraph;
  try {
    stored = await saveGraph(projectId, graph, revision, opts);
  } catch (err) {
    if (useGraphStore.getState().projectId !== projectId) return;
    const conflict = conflictOf(err);
    if (conflict) {
      enterConflict(conflict);
      return;
    }
    refuseSave(err);
    setSaveState("failed");
    if (retryable(err)) scheduleRetry();
    return;
  }
  // another project was opened while the PUT was out: its answer is not about this graph
  if (useGraphStore.getState().projectId !== projectId) return;
  retryStep = 0;
  applyStored(stored);
  // a change made while the PUT was out is still to be saved (its own timer or the queued follow-up sends it)
  if (editSeq === carried) setSaveState("saved", Date.now());
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
    // The daemon's copy (it carries a revision) replaces what this tab holds and is never an edit.
    const stored = revisionOf(graph);
    // Opening a project is not an edit; re-loading the open one with a different graph is (drop-to-restore adds the
    // producing subgraph this way), and is saved like any other change.
    const edited = sameProject && stored === null && signature(prev.nodes, prev.edges) !== signature(settled.nodes, edges);
    if (!sameProject || stored !== null) {
      cancelScheduledSave();
      retryStep = 0;
      editSeq += 1; // a PUT still out (for the old project, or of the old graph) does not carry this graph
    }
    const status = { saveError: null, saveState: "saved" as const, conflict: null, retryInMs: null, revision: stored };
    const fresh = !sameProject
      ? { ...status, savedAt: null, conflictLocal: null }
      : stored !== null
        ? status
        : {};
    quietly(() => set({ projectId, edges, ...settled, ...noRefusal, ...fresh, graphEpoch: prev.graphEpoch + 1 }));
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
  saveNow: (opts = {}) => {
    cancelScheduledSave();
    const projectId = get().projectId;
    if (projectId === null || get().saveState === "conflict") return Promise.resolve();
    if (inflight) {
      // The PUT on the wire already carries this graph; otherwise send the current one as soon as it is back.
      if (inflightSeq === editSeq) return inflight;
      followUp ??= inflight.then(() => {
        followUp = null;
        return get().projectId === projectId ? get().saveNow(opts) : undefined;
      });
      return followUp;
    }
    inflightSeq = editSeq;
    setSaveState("saving");
    if (get().retryInMs !== null) quietly(() => set({ retryInMs: null }));
    inflight = putGraph(projectId, get().toGraph(), editSeq, get().revision, opts).finally(() => {
      inflight = null;
      // a graph.saved that came while the PUT was out is checked now (after the follow-up, if one is queued)
      if (followUp === null) runRemoteCheck(projectId);
    });
    return inflight;
  },
  reloadGraph: async () => {
    const projectId = get().projectId;
    if (projectId === null) return;
    const local = get().conflictLocal;
    let head: StoredGraph;
    try {
      head = await getGraph(projectId);
    } catch (err) {
      if (get().projectId === projectId) {
        quietly(() => set({ saveError: { code: "reload_failed", message: `the reload failed: ${describeFailure(err).message}` } }));
      }
      return;
    }
    if (get().projectId !== projectId) return;
    try {
      applyRemote(head);
    } catch (err) {
      quietly(() => set({ saveError: { code: "reload_failed", message: `the reload failed: ${describeFailure(err).message}` } }));
      return;
    }
    // what a Reload would lose: the artist's Edit prompts the daemon's graph does not have
    const theirs = new Map(head.nodes.map((n): [string, unknown] => [n.id, n.kind === "Edit" ? n.params?.prompt : undefined]));
    const prompts = (local?.prompts ?? []).filter((p) => theirs.get(p.nodeId) !== p.prompt);
    quietly(() => set({ conflictLocal: local && prompts.length > 0 ? { ...local, prompts } : null }));
  },
  dismissConflictLocal: () => quietly(() => set({ conflictLocal: null })),
}));

// Every store update is checked here, whichever path made it (the actions above, the editors' `setState` for
// params, the candidate strip writing a pick): a structural change on an open project schedules a save. Switching
// projects drops a save scheduled for the old one.
useGraphStore.subscribe((state, prev) => {
  if (state.projectId !== prev.projectId) {
    cancelScheduledSave();
    retryStep = 0;
    remoteHint = null;
    return;
  }
  if (quiet > 0 || state.projectId === null) return;
  if (structurallyChanged(prev, state)) markChanged();
});
