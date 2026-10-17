// Facts a node's header needs that depend on the whole graph: the effective tier (concept is sticky downstream),
// the provenance labels it inherits (`unseen geometry guessed` follows the pixels), the Plate it is anchored to (the
// viewer's A side) and whether a rule breaks on one of its incoming edges. Computed once per graph change in
// PlateCanvas and handed to the node components through a context, so xyflow's node objects stay untouched.

import { createContext, useContext } from "react";
import { effectiveLabels, type EdgeLike, type GraphNode, type RuleViolation } from "../canvasRules";
import type { Tier } from "../nodeTypes";

export interface NodeInfo {
  tier: Tier;
  labels: string[];
  /** The nearest upstream Plate node id (itself for a Plate), or null when the node is not anchored. */
  plateId: string | null;
  /** A rule broken on one of the node's incoming edges (a loaded graph can carry one). */
  violation: RuleViolation | null;
}

export function deriveNodeInfo(nodes: GraphNode[], edges: EdgeLike[], violations: RuleViolation[]): Map<string, NodeInfo> {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const up = new Map<string, string[]>();
  for (const e of edges) up.set(e.target, [...(up.get(e.target) ?? []), e.source]);

  // Breadth-first upstream walk: nearest Plate first, and whether any ancestor is the concept tier.
  const walk = (id: string): { plateId: string | null; concept: boolean } => {
    const seen = new Set<string>([id]);
    let queue = [id];
    let plateId: string | null = null;
    let concept = false;
    while (queue.length) {
      const next: string[] = [];
      for (const cur of queue) {
        const n = byId.get(cur);
        if (!n) continue;
        if (n.data.kind === "Concept" || n.data.outputTier === "concept_unmeasured") concept = true;
        if (plateId === null && n.data.kind === "Plate") plateId = cur;
        for (const src of up.get(cur) ?? []) {
          if (!seen.has(src)) {
            seen.add(src);
            next.push(src);
          }
        }
      }
      queue = next;
    }
    return { plateId, concept };
  };

  const graph = { nodes, edges };
  const out = new Map<string, NodeInfo>();
  for (const n of nodes) {
    const { plateId, concept } = walk(n.id);
    out.set(n.id, {
      tier: concept ? "concept_unmeasured" : "measured",
      labels: effectiveLabels(graph, n.id),
      plateId,
      violation: violations.find((v) => v.edge.target === n.id) ?? null,
    });
  }
  return out;
}

export const NodeInfoContext = createContext<Map<string, NodeInfo>>(new Map());

const FALLBACK: NodeInfo = { tier: "measured", labels: [], plateId: null, violation: null };

export function useNodeInfo(id: string): NodeInfo {
  return useContext(NodeInfoContext).get(id) ?? FALLBACK;
}

/** The nearest upstream Plate of `id` (itself for a Plate), breadth first; null when not anchored. */
export function plateAncestor(nodes: GraphNode[], edges: EdgeLike[], id: string): GraphNode | null {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const up = new Map<string, string[]>();
  for (const e of edges) up.set(e.target, [...(up.get(e.target) ?? []), e.source]);
  const seen = new Set<string>([id]);
  let queue = [id];
  while (queue.length) {
    const next: string[] = [];
    for (const cur of queue) {
      const n = byId.get(cur);
      if (n?.data.kind === "Plate") return n;
      for (const src of up.get(cur) ?? []) {
        if (!seen.has(src)) {
          seen.add(src);
          next.push(src);
        }
      }
    }
    queue = next;
  }
  return null;
}
