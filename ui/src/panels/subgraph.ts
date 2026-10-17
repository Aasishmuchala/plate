// Drop-to-restore, graph side: which nodes produced a run, and how to bring them back onto the canvas.

import type { Edge } from "@xyflow/react";
import type { GraphNode } from "../canvasRules";
import type { Graph, GraphEdge, LoadableGraph, LoadableNode } from "../nodeTypes";
import { NODE_KINDS } from "../nodeTypes";
import type { RunDetail } from "./types";

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/** `nodeId` and every node upstream of it, in no particular order. */
export function ancestorsOf(nodeId: string, edges: readonly { source: string; target: string }[]): Set<string> {
  const up = new Map<string, string[]>();
  for (const e of edges) up.set(e.target, [...(up.get(e.target) ?? []), e.source]);
  const seen = new Set<string>([nodeId]);
  const stack = [nodeId];
  while (stack.length) {
    const id = stack.pop() as string;
    for (const src of up.get(id) ?? []) {
      if (!seen.has(src)) {
        seen.add(src);
        stack.push(src);
      }
    }
  }
  return seen;
}

function snapshotNodes(raw: unknown[]): LoadableNode[] {
  return raw.filter(
    (n): n is LoadableNode =>
      isRecord(n) && typeof n.id === "string" && typeof n.kind === "string" && (NODE_KINDS as readonly string[]).includes(n.kind),
  );
}

function snapshotEdges(raw: unknown[]): GraphEdge[] {
  const out: GraphEdge[] = [];
  for (const e of raw) {
    if (!isRecord(e) || typeof e.source !== "string" || typeof e.target !== "string") continue;
    out.push({
      id: typeof e.id === "string" ? e.id : `${e.source}->${e.target}`,
      source: e.source,
      target: e.target,
      sourceHandle: typeof e.sourceHandle === "string" ? e.sourceHandle : null,
      targetHandle: typeof e.targetHandle === "string" ? e.targetHandle : null,
    });
  }
  return out;
}

export interface Restore {
  /** Ids of the producing subgraph, all of which end up on the canvas and selected. */
  nodeIds: string[];
  /** The graph to load: the current one plus whatever the run's snapshot has that the canvas lacks. */
  graph: LoadableGraph;
  /** Nodes the canvas did not have and the load adds. */
  added: number;
}

/**
 * The producing subgraph of `run` against the current canvas. When the daemon kept a snapshot (`run.subgraph`) its
 * nodes and edges are used and any the canvas lacks are added; otherwise the subgraph is the run's node and its
 * ancestors on the current graph. Null when neither gives a node (no `node_id`, or an unknown one with no snapshot).
 */
export function planRestore(
  run: RunDetail,
  current: { nodes: GraphNode[]; edges: Edge[]; wire: Graph },
): Restore | null {
  const snap = run.subgraph;
  if (snap && snap.nodes.length > 0) {
    const nodes = snapshotNodes(snap.nodes);
    if (nodes.length === 0) return null;
    const have = new Set(current.wire.nodes.map((n) => n.id));
    const missing = nodes.filter((n) => !have.has(n.id));
    const haveEdge = new Set(current.wire.edges.map((e) => e.id));
    const newEdges = snapshotEdges(snap.edges).filter((e) => !haveEdge.has(e.id));
    const merged: LoadableGraph = {
      schema: 1,
      nodes: [...current.wire.nodes, ...missing],
      edges: [...current.wire.edges, ...newEdges],
    };
    return { nodeIds: nodes.map((n) => n.id), graph: merged, added: missing.length };
  }
  const nodeId = run.node_id;
  if (!nodeId || !current.nodes.some((n) => n.id === nodeId)) return null;
  const ids = ancestorsOf(nodeId, current.edges);
  return { nodeIds: [...ids].filter((id) => current.nodes.some((n) => n.id === id)), graph: current.wire, added: 0 };
}
