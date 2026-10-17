// The canvas: xyflow renders the graph from the zustand store (one plain box per node), a static Make bar sits above
// it, a status bar shows the daemon heartbeat and the token state, and a NodeList sidebar lists the nodes and the
// last refusal. App passes the editor, viewer and panels in as children of the sidebar.

import { Background, Controls, ReactFlow } from "@xyflow/react";
import type { ReactNode } from "react";
import { useDaemonStatus } from "./api/useDaemonStatus";
import { useGraphStore } from "./store";

// Placeholder for the quote shown before any run: provider, model, price, custody, ETA (BUILD_PLAN decision on the
// Make bar). Real values arrive with the daemon quote; nothing is priced or promised here.
export function MakeBar() {
  return (
    <div className="make-bar" data-testid="make-bar">
      <span>provider</span> · <span>model</span> · <span>price</span> · <span>custody</span> · <span>ETA</span>
      <em> quote pending</em>
    </div>
  );
}

/** Daemon health from `GET /api/v0/health` and whether this tab holds a bearer token. */
export function StatusBar() {
  const { state, health, tokenPresent } = useDaemonStatus();
  const daemon =
    state === "checking"
      ? "checking"
      : state === "up"
        ? `up${health?.daemon_version ? ` v${health.daemon_version}` : ""}${health?.stub_engine ? " (stub engine)" : ""}`
        : "unreachable";
  return (
    <div className="status-bar" data-testid="status-bar">
      <span data-testid="status-daemon" data-state={state}>
        daemon: {daemon}
      </span>
      <span data-testid="status-token" data-present={tokenPresent}>
        token: {tokenPresent ? "present" : "missing"}
      </span>
    </div>
  );
}

export function NodeList() {
  const nodes = useGraphStore((s) => s.nodes);
  const lastRefusal = useGraphStore((s) => s.lastRefusal);
  const violations = useGraphStore((s) => s.violations);
  return (
    <section className="node-list">
      <h2>Nodes</h2>
      <ul>
        {nodes.map((n) => (
          <li key={n.id} data-testid="node-item">
            {n.data.label}
          </li>
        ))}
      </ul>
      {lastRefusal ? (
        <p className="refusal" role="alert" data-testid="last-refusal">
          {lastRefusal}
        </p>
      ) : null}
      {violations.length > 0 ? (
        <p className="refusal" data-testid="violations">
          {violations.length === 1 ? "1 edge breaks" : `${violations.length} edges break`} the canvas rules
        </p>
      ) : null}
    </section>
  );
}

export default function PlateCanvas({ children }: { children?: ReactNode }) {
  const nodes = useGraphStore((s) => s.nodes);
  const edges = useGraphStore((s) => s.edges);
  const onNodesChange = useGraphStore((s) => s.onNodesChange);
  const onEdgesChange = useGraphStore((s) => s.onEdgesChange);
  const connect = useGraphStore((s) => s.connect);
  return (
    <div className="plate-app">
      <StatusBar />
      <MakeBar />
      <div className="plate-body">
        <div className="plate-canvas">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={connect}
            fitView
          >
            <Background />
            <Controls />
          </ReactFlow>
        </div>
        <aside className="plate-side">
          <NodeList />
          {children}
        </aside>
      </div>
    </div>
  );
}
