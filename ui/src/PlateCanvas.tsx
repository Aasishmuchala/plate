// Placeholder canvas: xyflow renders the graph from the zustand store, a static Make bar sits above it and a
// NodeList sidebar lists the nodes and the last refusal. Nothing here talks to the daemon yet.

import { Background, Controls, ReactFlow } from "@xyflow/react";
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

export function NodeList() {
  const nodes = useGraphStore((s) => s.nodes);
  const lastRefusal = useGraphStore((s) => s.lastRefusal);
  return (
    <aside className="node-list">
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
    </aside>
  );
}

export default function PlateCanvas() {
  const nodes = useGraphStore((s) => s.nodes);
  const edges = useGraphStore((s) => s.edges);
  const onNodesChange = useGraphStore((s) => s.onNodesChange);
  const onEdgesChange = useGraphStore((s) => s.onEdgesChange);
  const connect = useGraphStore((s) => s.connect);
  return (
    <div className="plate-app">
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
        <NodeList />
      </div>
    </div>
  );
}
