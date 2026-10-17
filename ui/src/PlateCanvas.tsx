// The canvas: xyflow renders the graph from the zustand store with one component per node kind (nodes/), typed
// handles and the enforced rules (a refused connection never connects and says why on the target node for 4 s);
// the Make bar is docked at the bottom; a status bar shows the daemon heartbeat and the token state; a NodeList
// sidebar lists the nodes and the last refusal. The sidebar mounts the artist-path editor (Plate, Edit, Export,
// Review: `pathEditorFor`) of the selected node; App passes the other editors, the viewer and the panels in as
// children of the sidebar. The add palette leaves out Colour and Compare (decision 41: the kinds stay in NODE_KINDS so
// saved graphs load). One dark theme (MaxGaffer palette) lives in canvas/canvas.css.

import {
  Background,
  type Connection,
  Controls,
  type Edge,
  type FinalConnectionState,
  Panel,
  ReactFlow,
  useReactFlow,
} from "@xyflow/react";
import { type ReactNode, useCallback, useEffect, useMemo } from "react";
import { useDaemonStatus } from "./api/useDaemonStatus";
import { attemptConnect, checkConnection, connectionFromDrop } from "./canvas/connect";
import { deriveNodeInfo, NodeInfoContext } from "./canvas/derive";
import { useRunStore } from "./canvas/runStore";
import { pathEditorFor } from "./editors";
import { FAKE_ENGINE } from "./makebar/engines";
import { MakeBar } from "./makebar/MakeBar";
import { useMakeStore } from "./makebar/makeStore";
import { NODE_KINDS, type NodeKind } from "./nodeTypes";
import { PLATE_NODE_TYPES } from "./nodes/kinds";
import { edgeType } from "./nodes/ports";
import { useGraphStore } from "./store";

export { MakeBar };

/** Fit the graph on load, but never blow a small graph up past 100%. */
const FIT_VIEW = { maxZoom: 1, padding: 0.2 } as const;

/**
 * A stub daemon (`/health` says `stub_engine`) that has no `GET /engines` still runs its fake engine: add it to the
 * build's catalogue the Make bar fell back to. A daemon that lists its engines lists the fake itself (T61).
 */
function useStubEngineFallback(stubEngine: boolean): void {
  const engines = useMakeStore((s) => s.engines);
  useEffect(() => {
    if (!stubEngine || engines?.source !== "fallback" || engines.engines.some((e) => e.id === FAKE_ENGINE.id)) return;
    useMakeStore.setState({ engines: { ...engines, engines: [...engines.engines, FAKE_ENGINE] } });
  }, [stubEngine, engines]);
}

/** Daemon health from `GET /api/v0/health` and whether this tab holds a bearer token. */
export function StatusBar() {
  const { state, health, tokenPresent } = useDaemonStatus();
  useStubEngineFallback(state === "up" && health?.stub_engine === true);
  const daemon =
    state === "checking"
      ? "checking"
      : state === "up"
        ? `up${health?.daemon_version ? ` v${health.daemon_version}` : ""}${health?.stub_engine ? " (stub engine)" : ""}`
        : "unreachable";
  return (
    <div className="status-bar" data-testid="status-bar">
      <span className="status-brand">PLATE</span>
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

/** Kinds the add palette leaves out (decision 41): Recolour, LightMix and Set cover photometry; Compare is P5. */
export const PALETTE_HIDDEN: ReadonlySet<NodeKind> = new Set<NodeKind>(["Colour", "Compare"]);

/** The kinds the add palette offers, in NODE_KINDS order. */
export const PALETTE_KINDS: readonly NodeKind[] = NODE_KINDS.filter((k) => !PALETTE_HIDDEN.has(k));

/** The artist-path editor (Plate, Edit, Export, Review) of the selected node; nothing for any other kind. */
function SelectedPathEditor() {
  const selected = useGraphStore((s) => s.nodes.find((n) => n.selected));
  const Editor = selected ? pathEditorFor(selected.data.kind) : null;
  return Editor && selected ? <Editor nodeId={selected.id} /> : null;
}

/** Add a node near the middle of the visible canvas, nudged so repeated adds do not stack exactly. */
function AddPalette() {
  const addNode = useGraphStore((s) => s.addNode);
  const count = useGraphStore((s) => s.nodes.length);
  const flow = useReactFlow();
  const add = (kind: NodeKind) => {
    const nudge = (count % 6) * 24;
    let pos = { x: 80 + nudge, y: 260 + nudge };
    const r = document.querySelector(".plate-canvas")?.getBoundingClientRect();
    if (r && r.width > 0) {
      const c = flow.screenToFlowPosition({ x: r.left + r.width / 2, y: r.top + r.height / 2 });
      pos = { x: c.x - 110 + nudge, y: c.y - 60 + nudge };
    }
    addNode(kind, pos);
  };
  return (
    <Panel position="top-left" className="add-palette" data-testid="add-palette">
      <details>
        <summary>+ node</summary>
        <div className="add-palette-grid">
          {PALETTE_KINDS.map((k) => (
            <button
              key={k}
              type="button"
              className={`btn btn-chip kind-${k.toLowerCase()}`}
              onClick={() => add(k)}
              data-testid={`add-${k}`}
              title={k === "Concept" ? "unmeasured: never reaches Export, Review or Delivery" : `add a ${k} node`}
            >
              {k}
            </button>
          ))}
        </div>
      </details>
    </Panel>
  );
}

/** A refusal with no node to sit on (or one whose node is gone) shows over the canvas instead. */
function CanvasToast() {
  const flash = useRunStore((s) => s.flash);
  const onNode = useGraphStore((s) => (flash?.nodeId ? s.nodes.some((n) => n.id === flash.nodeId) : false));
  if (!flash || onNode) return null;
  return (
    <div className="canvas-toast" role="alert" data-testid="canvas-refusal" key={flash.seq}>
      <strong>{flash.code}</strong> {flash.message}
    </div>
  );
}

export default function PlateCanvas({ children }: { children?: ReactNode }) {
  const nodes = useGraphStore((s) => s.nodes);
  const edges = useGraphStore((s) => s.edges);
  const violations = useGraphStore((s) => s.violations);
  const onNodesChange = useGraphStore((s) => s.onNodesChange);
  const onEdgesChange = useGraphStore((s) => s.onEdgesChange);

  const info = useMemo(() => deriveNodeInfo(nodes, edges, violations), [nodes, edges, violations]);

  // Colour each edge by the data it carries; an edge that breaks a rule (a loaded graph) is drawn as refused.
  const shownEdges = useMemo((): Edge[] => {
    const kinds = new Map(nodes.map((n) => [n.id, n.data.kind]));
    const broken = new Set(violations.map((v) => v.edge.id));
    return edges.map((e) => {
      const cls = `edge-${edgeType(kinds.get(e.source), e.sourceHandle) ?? "unknown"}${broken.has(e.id) ? " edge-violation" : ""}`;
      return e.className === cls ? e : { ...e, className: cls };
    });
  }, [nodes, edges, violations]);

  // While dragging: handles go green/red by the same verdict a drop would get.
  const isValidConnection = useCallback(
    (c: Connection | Edge) =>
      checkConnection({
        source: c.source,
        target: c.target,
        sourceHandle: c.sourceHandle ?? null,
        targetHandle: c.targetHandle ?? null,
      }).ok,
    [],
  );

  const onConnect = useCallback((c: Connection) => {
    attemptConnect(c);
  }, []);

  // xyflow skips onConnect for a drop isValidConnection refused; turn that drop into the refusal message.
  const onConnectEnd = useCallback((_e: MouseEvent | TouchEvent, state: FinalConnectionState) => {
    const refused = connectionFromDrop(state);
    if (refused) attemptConnect(refused);
  }, []);

  return (
    <NodeInfoContext.Provider value={info}>
      <div className="plate-app">
        <StatusBar />
        <div className="plate-body">
          <div className="plate-canvas">
            <ReactFlow
              nodes={nodes}
              edges={shownEdges}
              nodeTypes={PLATE_NODE_TYPES}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              onConnectEnd={onConnectEnd}
              isValidConnection={isValidConnection}
              colorMode="dark"
              minZoom={0.2}
              fitView
              fitViewOptions={FIT_VIEW}
            >
              <Background gap={24} size={1} />
              <Controls showInteractive={false} />
              <AddPalette />
            </ReactFlow>
            <CanvasToast />
          </div>
          <aside className="plate-side">
            <NodeList />
            <SelectedPathEditor />
            {children}
          </aside>
        </div>
        <MakeBar />
      </div>
    </NodeInfoContext.Provider>
  );
}
