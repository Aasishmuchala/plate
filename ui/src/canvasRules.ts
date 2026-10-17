// Canvas rules enforced in code (CLAUDE.md doctrine rules 1, 2 and 7; BUILD_PLAN section 4). Pure functions over
// nodes and edges so the same checks can run in the store, in tests and later in the daemon (T25 mirrors the codes
// in Python; the server is the enforcement point, these rules are the UX). Every refusal carries a stable `code` and
// a `message` that the UI shows verbatim.

import type { Edge, Node } from "@xyflow/react";
import { type HandleType, type NodeKind, type PlateNodeData, UNSEEN_GEOMETRY_LABEL } from "./nodeTypes";
import { acceptsOf, type KindPorts, PORTS, type PortSpec, portOf } from "./nodes/ports";

export type GraphNode = Node<PlateNodeData>;
/** The part of an edge the rules read. xyflow's `Edge` and the wire `GraphEdge` both satisfy it. The handle ids are
 * optional (T81): REFERENCE_ONLY and VIDEO_LOCKED read them, and a null or missing one resolves as `checkHandles`
 * does (the first output; the first input that takes the edge's type). */
export type EdgeLike = Pick<Edge, "id" | "source" | "target"> & {
  sourceHandle?: string | null;
  targetHandle?: string | null;
};
export interface GraphLike {
  nodes: GraphNode[];
  edges: EdgeLike[];
}

export type RuleCode =
  | "CONCEPT_TO_DELIVERY"
  | "PREVIS_NOT_DELIVERABLE"
  | "DEPTH_EXCEEDED"
  | "VIDEO_LOCKED"
  | "HIGH_RISK_MOVE"
  | "NO_PLATE_ANCHOR"
  | "INVENTED_ANGLE"
  | "CYCLE"
  | "UNKNOWN_NODE"
  | "SCRUB_REQUIRED"
  | "REFERENCE_ONLY"
  | "ANNOTATION";

export type RuleResult = { ok: true } | { ok: false; code: RuleCode; message: string };
export type ConnectResult = { ok: true } | { ok: false; reason: string };

/** Nodes that make new pixels. Each one costs a generation of depth. Mask/Comp/Colour/Gate/Compare, the numeric
 * nodes (LightMix, Recolour, Set, Light, Atmosphere, Sky), Reference and Frame do not. */
export const GENERATING_KINDS: ReadonlySet<NodeKind> = new Set<NodeKind>(["Edit", "Motion", "People", "CropPair"]);

/** Rule 7: generation depth is at most 2 from the original plate. */
export const MAX_GENERATION_DEPTH = 2;

/** Rule 1: the concept tier may not reach any of these. (There is no separate Delivery node today.) */
const DELIVERY_KINDS: ReadonlySet<NodeKind> = new Set<NodeKind>(["Export", "Review"]);

/** T44: a previs output (watermarked PREVIS) never reaches delivery proper. A client Review may show it. */
const EXPORT_KINDS: ReadonlySet<NodeKind> = new Set<NodeKind>(["Export"]);

/** Rule 7: video nodes stay locked until the upstream still passed gates and a human approved it. */
const VIDEO_KINDS: ReadonlySet<NodeKind> = new Set<NodeKind>(["Motion", "CropPair"]);

/** Decision 46: the only inputs a Reference may feed (Edit/People `refs`, Motion `elements`, Sky `sky`). */
export const REFERENCE_HANDLES: ReadonlySet<string> = new Set<string>(["refs", "elements", "sky"]);

/** Decision 57: annotation kinds take no edges and are ignored by every other rule. */
const ANNOTATION_KINDS: ReadonlySet<NodeKind> = new Set<NodeKind>(["Frame"]);

const REFERENCE_ONLY_MESSAGE = "a reference feeds an engine's reference input only; it is never composited";
const ANNOTATION_MESSAGE = "a Frame is an annotation; it takes no edges";

const refuse = (code: RuleCode, message: string): RuleResult => ({ ok: false, code, message });

const portsOf = (kind: NodeKind): KindPorts | undefined => (PORTS as Partial<Record<NodeKind, KindPorts>>)[kind];

/** The input port an edge lands on: the named one, or for a null handle the first input taking `type` (else the first
 * input), the way `checkHandles` resolves it. Null when the kind has no such input. */
function inputFor(kind: NodeKind, handle: string | null | undefined, type: HandleType | null): PortSpec | null {
  const inputs = portsOf(kind)?.inputs ?? [];
  if (handle === null || handle === undefined || handle === "") {
    return (type ? inputs.find((p) => acceptsOf(p).includes(type)) : undefined) ?? inputs[0] ?? null;
  }
  return inputs.find((p) => p.id === handle) ?? null;
}

/** What an edge carries (its source port's type; null when the port is unknown) and the input it lands on. */
function edgePorts(sourceKind: NodeKind, targetKind: NodeKind, edge: EdgeLike): { type: HandleType | null; input: PortSpec | null } {
  const type = portsOf(sourceKind) ? (portOf(sourceKind, "outputs", edge.sourceHandle)?.type ?? null) : null;
  return { type, input: inputFor(targetKind, edge.targetHandle, type) };
}

const byId = (nodes: GraphNode[]): Map<string, GraphNode> => new Map(nodes.map((n) => [n.id, n]));

function upstreamMap(edges: EdgeLike[]): Map<string, string[]> {
  const m = new Map<string, string[]>();
  for (const e of edges) m.set(e.target, [...(m.get(e.target) ?? []), e.source]);
  return m;
}

function downstreamMap(edges: EdgeLike[]): Map<string, string[]> {
  const m = new Map<string, string[]>();
  for (const e of edges) m.set(e.source, [...(m.get(e.source) ?? []), e.target]);
  return m;
}

/** Every node reachable from `start` along `next` (excluding `start` unless a cycle returns to it). */
function reach(start: string, next: Map<string, string[]>): Set<string> {
  const seen = new Set<string>();
  const stack = [...(next.get(start) ?? [])];
  while (stack.length) {
    const id = stack.pop() as string;
    if (seen.has(id)) continue;
    seen.add(id);
    stack.push(...(next.get(id) ?? []));
  }
  return seen;
}

/**
 * Number of generating nodes (Edit, Motion, People, CropPair) on the longest upstream path from a Plate node to
 * `nodeId`, counting `nodeId` itself when it generates. A node with no Plate upstream has depth 0 (it is not
 * anchored to a packet camera; `validateEdge` refuses to feed such a node into a generator). Back edges of a cycle
 * are ignored so this always terminates.
 */
export function generationDepth(nodes: GraphNode[], edges: EdgeLike[], nodeId: string): number {
  const nodeMap = byId(nodes);
  const up = upstreamMap(edges);
  const done = new Map<string, number>(); // -1 = no Plate upstream
  const active = new Set<string>(); // nodes on the current DFS path: an edge back into one is a cycle
  const walk = (id: string): number => {
    const known = done.get(id);
    if (known !== undefined) return known;
    const node = nodeMap.get(id);
    if (!node) return -1;
    active.add(id);
    const own = GENERATING_KINDS.has(node.data.kind) ? 1 : 0;
    let best = node.data.kind === "Plate" ? 0 : -1;
    for (const src of up.get(id) ?? []) {
      if (active.has(src)) continue;
      const d = walk(src);
      if (d >= 0) best = Math.max(best, d + own);
    }
    active.delete(id);
    done.set(id, best);
    return best;
  };
  return Math.max(walk(nodeId), 0);
}

/** The same nodes with `data.generationDepth` refreshed from the edges; unchanged nodes keep their identity. */
export function recomputeDepths(nodes: GraphNode[], edges: EdgeLike[]): GraphNode[] {
  return nodes.map((n) => {
    const depth = generationDepth(nodes, edges, n.id);
    return n.data.generationDepth === depth ? n : { ...n, data: { ...n.data, generationDepth: depth } };
  });
}

/** True when `id` has a Plate node among its ancestors (or is one): the generation is anchored to a packet camera. */
function anchoredToPlate(nodeMap: Map<string, GraphNode>, up: Map<string, string[]>, id: string): boolean {
  if (nodeMap.get(id)?.data.kind === "Plate") return true;
  return [...reach(id, up)].some((a) => nodeMap.get(a)?.data.kind === "Plate");
}

/** Concept tier is sticky: a node is unmeasured if it, or anything feeding it, is a Concept node. */
function isConceptTier(nodeMap: Map<string, GraphNode>, up: Map<string, string[]>, id: string): boolean {
  const isConcept = (n?: GraphNode) => n?.data.kind === "Concept" || n?.data.outputTier === "concept_unmeasured";
  if (isConcept(nodeMap.get(id))) return true;
  return [...reach(id, up)].some((a) => isConcept(nodeMap.get(a)));
}

/** A previs output: `previs: true` on the node (the previs run sets it) or in its params (the editor sets it). */
export const isPrevis = (data: PlateNodeData | undefined): boolean =>
  data !== undefined && (data.previs === true || data.params?.["previs"] === true);

/** Previs is sticky like the concept tier: a node is previs if it, or anything feeding it, is previs. */
function isPrevisTier(nodeMap: Map<string, GraphNode>, up: Map<string, string[]>, id: string): boolean {
  if (isPrevis(nodeMap.get(id)?.data)) return true;
  return [...reach(id, up)].some((a) => isPrevis(nodeMap.get(a)?.data));
}

const hasCameraOverride = (data: PlateNodeData): boolean => {
  const v = data.params?.["cameraOverride"];
  return v !== undefined && v !== null && v !== false;
};

const isHighRisk = (data: PlateNodeData): boolean => data.riskLabel === "high" || data.riskLabel === "highest";

/** Where an override must also carry the `unseen geometry guessed` label (doctrine 2): an invented camera angle on
 * an Edit, or a Motion whose risk label says it guesses unseen sides. */
const needsUnseenLabel = (data: PlateNodeData): boolean =>
  (data.kind === "Edit" && hasCameraOverride(data)) || (data.kind === "Motion" && isHighRisk(data));

/**
 * Would `edge` be allowed in `graph`? The edge may or may not already be in `graph.edges` (it is ignored there), so
 * the same call vets a new connection and re-checks a loaded one. Checks, in order: unknown node, cycle,
 * ANNOTATION (T81: an edge touching a Frame), REFERENCE_ONLY (T81: a Reference feeds `refs`, `elements` or `sky`
 * only; a null target handle resolves to the first input taking a reference), CONCEPT_TO_DELIVERY (doctrine 1),
 * PREVIS_NOT_DELIVERABLE (T44), NO_PLATE_ANCHOR (doctrine 2, source-view-only; a reference never anchors, so an
 * engine fed one must hang off a Plate through its other inputs), DEPTH_EXCEEDED (doctrine 7), VIDEO_LOCKED
 * (doctrine 7: every still into Motion or CropPair, the end still included, must be gateStatus pass and approved; a
 * packet or reference edge is not a still), HIGH_RISK_MOVE, INVENTED_ANGLE.
 */
export function validateEdge(graph: GraphLike, edge: EdgeLike): RuleResult {
  const nodeMap = byId(graph.nodes);
  const source = nodeMap.get(edge.source);
  const target = nodeMap.get(edge.target);
  if (!source || !target) return refuse("UNKNOWN_NODE", "unknown node");

  const others = graph.edges.filter((e) => e.id !== edge.id);

  // No cycles, self-loops included: the target must not already lead back to the source.
  if (edge.source === edge.target || reach(edge.target, downstreamMap(others)).has(edge.source)) {
    return refuse("CYCLE", "cycle");
  }

  const sk = source.data.kind;
  const tk = target.data.kind;

  // A Frame is an annotation: it takes no edges, in or out.
  if (ANNOTATION_KINDS.has(sk) || ANNOTATION_KINDS.has(tk)) return refuse("ANNOTATION", ANNOTATION_MESSAGE);

  // A reference is an engine input, never pixels of the output: it may land on a reference input only.
  if (sk === "Reference") {
    const input = inputFor(tk, edge.targetHandle, "reference");
    if (!input || !REFERENCE_HANDLES.has(input.id) || !acceptsOf(input).includes("reference")) {
      return refuse("REFERENCE_ONLY", REFERENCE_ONLY_MESSAGE);
    }
  }

  const next: EdgeLike[] = [...others, edge];
  const nextUp = upstreamMap(next);

  // The concept tier never reaches Export (or Review), directly or through other nodes.
  if (DELIVERY_KINDS.has(tk) && isConceptTier(nodeMap, nextUp, edge.target)) {
    return refuse("CONCEPT_TO_DELIVERY", `concept tier cannot reach ${tk}`);
  }

  // Previs (Seedance clay-motion, watermarked PREVIS) never reaches Export, directly or through other nodes.
  if (EXPORT_KINDS.has(tk) && isPrevisTier(nodeMap, nextUp, edge.target)) {
    return refuse("PREVIS_NOT_DELIVERABLE", `previs output cannot reach ${tk}: PREVIS is not deliverable`);
  }

  // Source-view-only: a generator must hang off a Plate, so every generation is anchored to a packet camera. A
  // Reference anchors nothing: an edge from one needs the generator itself anchored through its other inputs.
  const anchor = sk === "Reference" ? edge.target : edge.source;
  if (GENERATING_KINDS.has(tk) && !anchoredToPlate(nodeMap, nextUp, anchor)) {
    return refuse("NO_PLATE_ANCHOR", "generation must be anchored to a plate camera (source-view-only)");
  }

  // Depth: the target and everything downstream of it must stay within MAX_GENERATION_DEPTH.
  const affected = [edge.target, ...reach(edge.target, downstreamMap(next))];
  if (affected.some((id) => generationDepth(graph.nodes, next, id) > MAX_GENERATION_DEPTH)) {
    return refuse("DEPTH_EXCEEDED", "generation depth > 2 from the plate");
  }

  // Video stays locked until every still feeding it (start or end) passed gates and a human approved it. A packet or
  // a reference edge is not a still; an edge whose ports cannot be resolved is treated as one (fail closed).
  if (VIDEO_KINDS.has(tk) && !(source.data.gateStatus === "pass" && source.data.approved === true)) {
    const { type, input } = edgePorts(sk, tk, edge);
    if (type === null || input === null || type === "image16" || input.type === "image16") {
      const which = input?.id === "end" ? `the ${input.label}` : "the upstream still";
      return refuse("VIDEO_LOCKED", `${tk === "Motion" ? "motion" : "crop pair"} is locked until ${which} passes gates and is approved`);
    }
  }

  // High-risk moves are blocked unless overridden (and then labelled, see applyOverride).
  if (tk === "Motion" && isHighRisk(target.data) && target.data.overrides.length === 0) {
    return refuse("HIGH_RISK_MOVE", `${target.data.riskLabel} risk move is blocked until it is overridden with a reason`);
  }

  // An invented camera angle needs an explicit override and the "unseen geometry guessed" label.
  if (
    tk === "Edit" &&
    hasCameraOverride(target.data) &&
    !(target.data.overrides.length > 0 && target.data.labels.includes(UNSEEN_GEOMETRY_LABEL))
  ) {
    return refuse("INVENTED_ANGLE", "a camera override needs an explicit override and the label 'unseen geometry guessed'");
  }

  return { ok: true };
}

/** Seed entry point: `validateEdge` for the edge `sourceId -> targetId`, with the failure as a `reason` string. */
export function canConnect(nodes: GraphNode[], edges: EdgeLike[], sourceId: string, targetId: string): ConnectResult {
  const verdict = validateEdge({ nodes, edges }, { id: `${sourceId}->${targetId}`, source: sourceId, target: targetId });
  return verdict.ok ? { ok: true } : { ok: false, reason: verdict.message };
}

export interface RuleViolation {
  edge: EdgeLike;
  code: RuleCode;
  message: string;
}

/** Re-check every edge of a loaded graph; an empty list means the graph obeys the canvas rules. */
export function validateGraph(graph: GraphLike): RuleViolation[] {
  const out: RuleViolation[] = [];
  for (const edge of graph.edges) {
    const verdict = validateEdge(graph, edge);
    if (!verdict.ok) out.push({ edge, code: verdict.code, message: verdict.message });
  }
  return out;
}

/** The logged scrub that allows a take to be accepted (doctrine 7). */
export interface ScrubLog {
  id?: string;
  fullScrub: boolean;
}

/** A take is accepted only after a logged full scrub. */
export function canAccept(node: GraphNode, scrubLog: ScrubLog | null | undefined): RuleResult {
  if (scrubLog?.fullScrub === true) return { ok: true };
  return refuse("SCRUB_REQUIRED", `'${node.data.label}' cannot be accepted without a logged full scrub`);
}

/** Record an override (reason required) and add the `unseen geometry guessed` label where it applies. Pure. */
export function applyOverride(node: GraphNode, reason: string, by: string, at: string = new Date().toISOString()): GraphNode {
  if (reason.trim() === "") throw new Error("an override needs a reason");
  const data = node.data;
  const labels =
    needsUnseenLabel(data) && !data.labels.includes(UNSEEN_GEOMETRY_LABEL)
      ? [...data.labels, UNSEEN_GEOMETRY_LABEL]
      : data.labels;
  return { ...node, data: { ...data, overrides: [...data.overrides, { reason, by, at }], labels } };
}

/** Labels a node carries: its own plus those of every ancestor, so `unseen geometry guessed` follows the pixels. */
export function effectiveLabels(graph: GraphLike, nodeId: string): string[] {
  const nodeMap = byId(graph.nodes);
  const ids = [nodeId, ...reach(nodeId, upstreamMap(graph.edges))];
  const out: string[] = [];
  for (const id of ids) {
    for (const label of nodeMap.get(id)?.data.labels ?? []) if (!out.includes(label)) out.push(label);
  }
  return out;
}
