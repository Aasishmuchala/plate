// Canvas rules enforced in code (CLAUDE.md doctrine rules 1, 2 and 7; BUILD_PLAN section 4). Pure functions over
// nodes and edges so the same checks can run in the store, in tests and later in the daemon. Every refusal returns a
// reason string that the UI shows verbatim.

import type { Edge, Node } from "@xyflow/react";
import type { NodeType, PlateNodeData } from "./nodeTypes";

export type GraphNode = Node<PlateNodeData>;
export type ConnectResult = { ok: true } | { ok: false; reason: string };

/** Nodes that make new pixels. Each one costs a generation of depth. */
export const GENERATING_TYPES: ReadonlySet<NodeType> = new Set<NodeType>([
  "Edit",
  "People",
  "Motion",
  "CropPair",
  "Recolour",
]);

/** Rule 7: generation depth is at most 2 from the original plate. */
export const MAX_GENERATION_DEPTH = 2;

/** Rule 1: the concept tier may not reach any of these. (There is no separate Delivery node yet.) */
const DELIVERY_TYPES: ReadonlySet<NodeType> = new Set<NodeType>(["Export", "Review"]);

const byId = (nodes: GraphNode[]): Map<string, GraphNode> => new Map(nodes.map((n) => [n.id, n]));

function upstreamMap(edges: Edge[]): Map<string, string[]> {
  const m = new Map<string, string[]>();
  for (const e of edges) m.set(e.target, [...(m.get(e.target) ?? []), e.source]);
  return m;
}

function downstreamMap(edges: Edge[]): Map<string, string[]> {
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
 * Number of generating nodes (Edit, People, Motion, CropPair, Recolour) on the longest upstream path from a Plate
 * node to `nodeId`, counting `nodeId` itself when it generates. A node with no Plate upstream has depth 0 (it is not
 * anchored to a packet camera; `canConnect` refuses to feed such a node into a generator). Back edges of a cycle are
 * ignored so this always terminates.
 */
export function generationDepth(nodes: GraphNode[], edges: Edge[], nodeId: string): number {
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
    const own = GENERATING_TYPES.has(node.data.type) ? 1 : 0;
    let best = node.data.type === "Plate" ? 0 : -1;
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

/** True when `id` has a Plate node among its ancestors (or is one): the generation is anchored to a packet camera. */
function anchoredToPlate(nodeMap: Map<string, GraphNode>, up: Map<string, string[]>, id: string): boolean {
  if (nodeMap.get(id)?.data.type === "Plate") return true;
  return [...reach(id, up)].some((a) => nodeMap.get(a)?.data.type === "Plate");
}

/** Concept tier is sticky: a node is unmeasured if it, or anything feeding it, is a Concept node. */
function isConceptTier(nodeMap: Map<string, GraphNode>, up: Map<string, string[]>, id: string): boolean {
  const isConcept = (n?: GraphNode) => n?.data.type === "Concept" || n?.data.tier === "concept";
  if (isConcept(nodeMap.get(id))) return true;
  return [...reach(id, up)].some((a) => isConcept(nodeMap.get(a)));
}

/**
 * Would adding `sourceId -> targetId` be allowed? Checks, in order: cycle, concept tier into delivery
 * (doctrine 1), generation anchored to a plate camera (doctrine 2, source-view-only), generation depth
 * (doctrine 7), and the video lock (doctrine 7).
 */
export function canConnect(nodes: GraphNode[], edges: Edge[], sourceId: string, targetId: string): ConnectResult {
  const nodeMap = byId(nodes);
  const source = nodeMap.get(sourceId);
  const target = nodeMap.get(targetId);
  if (!source || !target) return { ok: false, reason: "unknown node" };

  const down = downstreamMap(edges);

  // (4) no cycles, self-loops included: the target must not already lead back to the source.
  if (sourceId === targetId || reach(targetId, down).has(sourceId)) return { ok: false, reason: "cycle" };

  const next: Edge[] = [...edges, { id: `${sourceId}->${targetId}`, source: sourceId, target: targetId }];
  const nextUp = upstreamMap(next);

  // (2) concept tier never reaches Export (or Review), directly or through other nodes.
  if (DELIVERY_TYPES.has(target.data.type) && isConceptTier(nodeMap, nextUp, targetId)) {
    return { ok: false, reason: `concept tier cannot reach ${target.data.type}` };
  }

  // source-view-only: a generator must hang off a Plate, so every generation is anchored to a packet camera.
  if (GENERATING_TYPES.has(target.data.type) && !anchoredToPlate(nodeMap, nextUp, sourceId)) {
    return { ok: false, reason: "generation must be anchored to a plate camera (source-view-only)" };
  }

  // (1) depth: the target and everything downstream of it must stay within MAX_GENERATION_DEPTH.
  const affected = [targetId, ...reach(targetId, downstreamMap(next))];
  if (affected.some((id) => generationDepth(nodes, next, id) > MAX_GENERATION_DEPTH)) {
    return { ok: false, reason: "generation depth > 2 from the plate" };
  }

  // (3) video stays locked until the upstream still passed gates and a human approved it.
  if (target.data.type === "Motion" && !(source.data.gatePassed === true && source.data.humanApproved === true)) {
    return { ok: false, reason: "motion is locked until the upstream still passes gates and is approved" };
  }

  return { ok: true };
}
