// Connecting two nodes on the canvas (T35). Two checks, in order: the handle types (nodes/ports.ts: a mask never
// feeds an image input) and then the doctrine rules (canvasRules.validateEdge through the graph store). A refusal
// never connects; its message shows inline on the target node for 4 s (runStore.flashRefusal) and stays in the
// side list as the store's `lastRefusal`. `checkConnection` is the same verdict without side effects, used by
// xyflow's `isValidConnection` to colour handles while dragging.

import type { Connection, FinalConnectionState } from "@xyflow/react";
import { type RuleCode, validateEdge } from "../canvasRules";
import { checkHandles } from "../nodes/ports";
import { useGraphStore } from "../store";
import { useRunStore } from "./runStore";

export type ConnectVerdict =
  | { ok: true; connection: Connection }
  | { ok: false; code: RuleCode | "HANDLE_TYPE"; message: string };

/** Would `c` be accepted? On success the connection comes back with both handle ids resolved. */
export function checkConnection(c: Connection): ConnectVerdict {
  const { nodes, edges } = useGraphStore.getState();
  const source = nodes.find((n) => n.id === c.source);
  const target = nodes.find((n) => n.id === c.target);
  if (!source || !target) return { ok: false, code: "UNKNOWN_NODE", message: "unknown node" };
  const typed = checkHandles(source.data.kind, c.sourceHandle, target.data.kind, c.targetHandle);
  if (!typed.ok) return { ok: false, code: typed.code, message: typed.message };
  // Pass the resolved ports: VIDEO_LOCKED and the reference rules decide by handle (T81).
  const rules = validateEdge(
    { nodes, edges },
    {
      id: `${c.source}->${c.target}`,
      source: c.source,
      target: c.target,
      sourceHandle: typed.sourceHandle,
      targetHandle: typed.targetHandle,
    },
  );
  if (!rules.ok) return { ok: false, code: rules.code, message: rules.message };
  return { ok: true, connection: { ...c, sourceHandle: typed.sourceHandle, targetHandle: typed.targetHandle } };
}

/** Connect `c` or refuse it with an inline message. Returns true when the edge was added. */
export function attemptConnect(c: Connection): boolean {
  const verdict = checkConnection(c);
  const graph = useGraphStore.getState();
  if (!verdict.ok) {
    const lastRefusalCode = verdict.code === "HANDLE_TYPE" ? null : verdict.code;
    useGraphStore.setState({ lastRefusal: verdict.message, lastRefusalCode });
    useRunStore.getState().flashRefusal(c.target, verdict.code, verdict.message);
    return false;
  }
  const before = graph.edges.length;
  graph.connect(verdict.connection);
  const after = useGraphStore.getState();
  if (after.edges.length > before) return true;
  // The store refused it (a rule it checks that we did not) or the edge already existed.
  if (after.lastRefusal) useRunStore.getState().flashRefusal(c.target, after.lastRefusalCode ?? "REFUSED", after.lastRefusal);
  return false;
}

/**
 * xyflow does not call onConnect for a drop that `isValidConnection` refused; this turns such a drop (released on a
 * handle) into the same refusal message, so a refused drag never fails silently.
 */
export function connectionFromDrop(state: FinalConnectionState): Connection | null {
  if (state.isValid !== false || !state.fromHandle || !state.toHandle || !state.fromNode || !state.toNode) return null;
  const fromIsSource = state.fromHandle.type === "source";
  const src = fromIsSource ? state.fromHandle : state.toHandle;
  const dst = fromIsSource ? state.toHandle : state.fromHandle;
  return {
    source: src.nodeId,
    target: dst.nodeId,
    sourceHandle: src.id ?? null,
    targetHandle: dst.id ?? null,
  };
}
