// How an editor reads and writes its node. Parameters live in `node.data.params` of the graph store (the store has no
// params action, so `patchNodeParams` sets them through zustand's `setState`; params do not change generation depth or
// the canvas rules, so nothing needs to be re-settled). `plateRef` / `stillRef` find the image an editor works on:
// the Plate the node is anchored to, or the nearest upstream node that has an output.

import { useCallback, useMemo } from "react";
import { type EdgeLike, type GraphNode } from "../canvasRules";
import { plateAncestor } from "../canvas/derive";
import type { ImageSize, PlateTier } from "../nodeTypes";
import { useGraphStore } from "../store";

export type Params = Record<string, unknown>;

/** Merge `patch` over `base`; a key set to `undefined` is removed. Pure. */
export function mergeParams(base: Params | undefined, patch: Params): Params {
  const out: Params = { ...(base ?? {}) };
  for (const [k, v] of Object.entries(patch)) {
    if (v === undefined) delete out[k];
    else out[k] = v;
  }
  return out;
}

export function patchNodeParams(nodeId: string, patch: Params): void {
  useGraphStore.setState((s) => ({
    nodes: s.nodes.map((n) => (n.id === nodeId ? { ...n, data: { ...n.data, params: mergeParams(n.data.params, patch) } } : n)),
  }));
}

export const asNumber = (v: unknown, fallback: number): number => (typeof v === "number" && Number.isFinite(v) ? v : fallback);
export const asString = (v: unknown, fallback = ""): string => (typeof v === "string" ? v : fallback);
export const asBool = (v: unknown, fallback = false): boolean => (typeof v === "boolean" ? v : fallback);
export const asStringList = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

export interface NodeEditorState {
  node: GraphNode | undefined;
  params: Params;
  set: (patch: Params) => void;
}

export function useNodeEditor(nodeId: string): NodeEditorState {
  const node = useGraphStore((s) => s.nodes.find((n) => n.id === nodeId));
  const set = useCallback((patch: Params) => patchNodeParams(nodeId, patch), [nodeId]);
  const params = node?.data.params ?? EMPTY;
  return { node, params, set };
}

const EMPTY: Params = Object.freeze({}) as Params;

/** An image on the canvas: where it lives in the CAS, how big it is and which packet camera it belongs to. */
export interface ImageRef {
  nodeId: string;
  hash: string | null;
  size: ImageSize | null;
  packetId: string | null;
  tier: PlateTier | null;
}

function refOf(n: GraphNode, plate: GraphNode | null): ImageRef {
  const d = n.data;
  const packetId = plate?.data.packetId && plate.data.packetId !== "unlinked" ? plate.data.packetId : null;
  return {
    nodeId: n.id,
    hash: d.outputHash ?? null,
    size: d.outputSize ?? null,
    packetId,
    tier: plate?.data.tier ?? null,
  };
}

/** The Plate this node is anchored to (itself for a Plate), as an image; null when it hangs off no Plate. */
export function plateRef(nodes: GraphNode[], edges: EdgeLike[], nodeId: string): ImageRef | null {
  const plate = plateAncestor(nodes, edges, nodeId);
  return plate ? refOf(plate, plate) : null;
}

/** The nearest upstream node with an output image (breadth first), else the Plate. Its packet is the Plate's. */
export function stillRef(nodes: GraphNode[], edges: EdgeLike[], nodeId: string): ImageRef | null {
  const plate = plateAncestor(nodes, edges, nodeId);
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const seen = new Set<string>([nodeId]);
  let queue = edges.filter((e) => e.target === nodeId).map((e) => e.source);
  while (queue.length) {
    const next: string[] = [];
    for (const id of queue) {
      if (seen.has(id)) continue;
      seen.add(id);
      const n = byId.get(id);
      if (!n) continue;
      // A Reference's hash is its card thumbnail, not a still: it feeds an engine's reference input and is never the
      // image an editor works on (decision 46).
      if (n.data.outputHash && n.data.kind !== "Reference") return refOf(n, plate);
      for (const e of edges) if (e.target === id) next.push(e.source);
    }
    queue = next;
  }
  return plate ? refOf(plate, plate) : null;
}

export function usePlate(nodeId: string): ImageRef | null {
  const nodes = useGraphStore((s) => s.nodes);
  const edges = useGraphStore((s) => s.edges);
  return useMemo(() => plateRef(nodes, edges, nodeId), [nodes, edges, nodeId]);
}

export function useStill(nodeId: string): ImageRef | null {
  const nodes = useGraphStore((s) => s.nodes);
  const edges = useGraphStore((s) => s.edges);
  return useMemo(() => stillRef(nodes, edges, nodeId), [nodes, edges, nodeId]);
}

/** Every Plate upstream of `nodeId` (any depth), nearest first; the Set editor's views. */
export function upstreamPlates(nodes: GraphNode[], edges: EdgeLike[], nodeId: string): GraphNode[] {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const out: GraphNode[] = [];
  const seen = new Set<string>([nodeId]);
  let queue = [nodeId];
  while (queue.length) {
    const next: string[] = [];
    for (const id of queue) {
      for (const e of edges) {
        if (e.target !== id || seen.has(e.source)) continue;
        seen.add(e.source);
        const n = byId.get(e.source);
        if (n?.data.kind === "Plate") out.push(n);
        next.push(e.source);
      }
    }
    queue = next;
  }
  return out;
}
