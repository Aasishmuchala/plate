// Canvas geometry (T98): Tidy and Focus. Tidy lays the graph out left to right by rank, the longest path from any
// Plate (a Plate is rank 0, a node it feeds is at least rank 1, and so on). Nodes no Plate reaches (a Reference, a
// node whose chain is broken, a cycle in a loaded graph) go in one column after the last rank. Within a rank the
// nodes stack by id (numeric-aware, so edit-2 sits above edit-10). Frames are annotations: Tidy never moves one
// and they take no part in the ranks. Focus zooms the view onto some nodes, never past 150%.

import type { FitViewOptions, XYPosition } from "@xyflow/react";
import type { EdgeLike, GraphNode } from "../canvasRules";

/** Horizontal distance between two ranks. */
export const TIDY_COLUMN_PX = 340;
/** Vertical distance between two nodes of one rank. */
export const TIDY_ROW_PX = 180;

/** How Focus (the `z` key, the node menu, a double-click) moves the view. */
export const FOCUS_OPTIONS = { duration: 300, maxZoom: 1.5 } as const;

/** Fit the graph on load (and from the pane menu), but never blow a small graph up past 100%. */
export const FIT_VIEW = { maxZoom: 1, padding: 0.2 } as const;

const byIdNumeric = (a: string, b: string): number => a.localeCompare(b, "en", { numeric: true });

/**
 * The rank of every node but the Frames: the longest path (in edges) from any Plate; nodes no Plate reaches get the
 * rank after the last one. A cycle (a loaded graph can carry one) never loops: its nodes stay unreached.
 */
export function tidyRanks(nodes: GraphNode[], edges: EdgeLike[]): Map<string, number> {
  const ids = new Set(nodes.filter((n) => n.data.kind !== "Frame").map((n) => n.id));
  const out = new Map<string, string[]>();
  const indegree = new Map<string, number>([...ids].map((id) => [id, 0]));
  for (const e of edges) {
    if (!ids.has(e.source) || !ids.has(e.target) || e.source === e.target) continue;
    out.set(e.source, [...(out.get(e.source) ?? []), e.target]);
    indegree.set(e.target, (indegree.get(e.target) ?? 0) + 1);
  }
  // Kahn's order: every node comes after all of its upstream nodes, so its longest path is final when it is reached.
  const order: string[] = [];
  const ready = [...ids].filter((id) => indegree.get(id) === 0).sort(byIdNumeric);
  while (ready.length > 0) {
    const id = ready.shift() as string;
    order.push(id);
    for (const next of out.get(id) ?? []) {
      const left = (indegree.get(next) ?? 0) - 1;
      indegree.set(next, left);
      if (left === 0) ready.push(next);
    }
  }
  const kinds = new Map(nodes.map((n) => [n.id, n.data.kind]));
  const rank = new Map<string, number>();
  for (const id of order) {
    if (kinds.get(id) === "Plate") rank.set(id, 0);
    const r = rank.get(id);
    if (r === undefined) continue;
    for (const next of out.get(id) ?? []) rank.set(next, Math.max(rank.get(next) ?? 0, r + 1));
  }
  const after = rank.size === 0 ? 0 : Math.max(...rank.values()) + 1;
  for (const id of ids) if (!rank.has(id)) rank.set(id, after);
  return rank;
}

/** Where Tidy puts each node: `x = rank × 340`, `y = row × 180`. Frames are left out (they stay where they are). */
export function tidyPositions(nodes: GraphNode[], edges: EdgeLike[]): Map<string, XYPosition> {
  const ranks = tidyRanks(nodes, edges);
  const columns = new Map<number, string[]>();
  for (const [id, r] of ranks) columns.set(r, [...(columns.get(r) ?? []), id]);
  const out = new Map<string, XYPosition>();
  for (const [r, column] of columns) {
    column.sort(byIdNumeric).forEach((id, row) => out.set(id, { x: r * TIDY_COLUMN_PX, y: row * TIDY_ROW_PX }));
  }
  return out;
}

/** Zoom the view onto `ids` (300 ms, at most 150%). Nothing to focus does nothing. */
export function focusNodes(flow: { fitView: (options?: FitViewOptions) => Promise<boolean> }, ids: readonly string[]): void {
  if (ids.length === 0) return;
  void flow.fitView({ nodes: ids.map((id) => ({ id })), ...FOCUS_OPTIONS });
}
