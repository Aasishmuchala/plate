// The canvas: xyflow renders the graph from the zustand store with one component per node kind (nodes/), typed
// handles and the enforced rules (a refused connection never connects and says why on the target node for 4 s);
// the Make bar is docked at the bottom; a status bar shows the daemon heartbeat and the token state; a NodeList
// sidebar lists the nodes and the last refusal. The sidebar mounts the artist-path editor (Plate, Edit, Export,
// Review: `pathEditorFor`) of the selected node; App passes the other editors, the viewer and the panels in as
// children of the sidebar. The add palette leaves out Compare (decision 41: the kind stays in NODE_KINDS so saved
// graphs load); Colour is back in it (T98: a numeric grade node since Wave 9 decision 48). One dark theme (MaxGaffer
// palette) lives in canvas/canvas.css.
//
// T98, the interaction model. Right-click a node or the empty canvas for its menu, and let a wire go on the empty
// canvas to add and wire a node there (canvas/ContextMenu.tsx). Delete or Backspace removes the selection (xyflow,
// outside text fields). With the focus on the canvas, `z` focuses the selection (the whole graph when nothing is
// selected) and Cmd/Ctrl+D duplicates it. A drag that ends saves the positions 1500 ms later (store.onNodeDragStop).
// With no project open the canvas shows an empty-state card (a slim banner over a graph that is not saved); the
// status bar carries the save state, and the tab title is `PLATE · <project>`, with ` *` until the daemon has it.

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
import {
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
  type RefObject,
  useCallback,
  useEffect,
  useMemo,
  useRef,
} from "react";
import { useDaemonStatus } from "./api/useDaemonStatus";
import { attemptConnect, checkConnection, connectionFromDrop } from "./canvas/connect";
import { CanvasMenus, openDropPicker, useCanvasMenu } from "./canvas/ContextMenu";
import { deriveNodeInfo, NodeInfoContext } from "./canvas/derive";
import { FIT_VIEW, focusNodes } from "./canvas/layout";
import { useRunStore } from "./canvas/runStore";
import type { GraphNode } from "./canvasRules";
import { pathEditorFor } from "./editors";
import { FAKE_ENGINE } from "./makebar/engines";
import { MakeBar } from "./makebar/MakeBar";
import { useMakeStore } from "./makebar/makeStore";
import { NODE_KINDS, type NodeKind } from "./nodeTypes";
import { PLATE_NODE_TYPES } from "./nodes/kinds";
import { edgeType } from "./nodes/ports";
import { isTextEntry } from "./panels/VerdictPanel";
import { type PanelTab, usePanelsStore } from "./panels/store";
import { useGraphStore } from "./store";

export { MakeBar };

/** xyflow removes the selection on either key (never while typing in a field). */
const DELETE_KEYS = ["Backspace", "Delete"];

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

const two = (n: number): string => String(n).padStart(2, "0");

/** Local wall-clock time as HH:MM:SS. */
export function clockTime(ms: number): string {
  const d = new Date(ms);
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
}

const SAVE_COLOR: Record<string, string> = {
  saved: "var(--dim, #98989d)",
  unsaved: "var(--text, #f2f2f7)",
  saving: "var(--text, #f2f2f7)",
  failed: "var(--red, #ff453a)",
};

/** The open project's save state: `saved HH:MM:SS`, `unsaved`, `saving…` or `save failed: <message>` (red). */
function SaveChip() {
  const projectId = useGraphStore((s) => s.projectId);
  const saveState = useGraphStore((s) => s.saveState);
  const savedAt = useGraphStore((s) => s.savedAt);
  const error = useGraphStore((s) => s.saveError);
  if (projectId === null) return null;
  const text =
    saveState === "saved"
      ? savedAt === null
        ? "saved"
        : `saved ${clockTime(savedAt)}`
      : saveState === "unsaved"
        ? "unsaved"
        : saveState === "saving"
          ? "saving…"
          : `save failed: ${error?.message || "the daemon did not store the graph"}`;
  return (
    <span
      className="status-save"
      data-testid="save-state"
      data-state={saveState}
      aria-live="polite"
      title={saveState === "failed" && error?.code ? error.code : `project ${projectId}`}
      style={{ color: SAVE_COLOR[saveState] }}
    >
      {text}
    </span>
  );
}

/** `PLATE · <project>`, with ` *` while the daemon does not have the latest graph. */
function useDocumentTitle(): void {
  const projectId = useGraphStore((s) => s.projectId);
  const saveState = useGraphStore((s) => s.saveState);
  useEffect(() => {
    document.title = projectId === null ? "PLATE" : `PLATE · ${projectId}${saveState === "saved" ? "" : " *"}`;
  }, [projectId, saveState]);
}

/** Daemon health from `GET /api/v0/health`, whether this tab holds a bearer token, and the save state. */
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
      <SaveChip />
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

/** Kinds the add palette leaves out (decision 41): Compare is P5. Colour came back with T98 (decision 48: a numeric
 * grade in linear light inside a scope). */
export const PALETTE_HIDDEN: ReadonlySet<NodeKind> = new Set<NodeKind>(["Compare"]);

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

/** What the canvas keys do: `z` focuses the selection, Cmd/Ctrl+D duplicates it. Null for any other key, and for
 * keys typed into a field. */
export function canvasKeyAction(
  e: Pick<KeyboardEvent, "key" | "metaKey" | "ctrlKey" | "altKey" | "target" | "defaultPrevented">,
): "focus" | "duplicate" | null {
  if (e.defaultPrevented || isTextEntry(e.target instanceof Element ? e.target : null)) return null;
  if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "d") return "duplicate";
  if (!e.metaKey && !e.ctrlKey && !e.altKey && e.key === "z") return "focus";
  return null;
}

/** The keys on the canvas (the `.plate-canvas` element, which takes the focus when the pane is clicked). */
function CanvasKeys({ host }: { host: RefObject<HTMLDivElement | null> }) {
  const flow = useReactFlow();
  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const onKey = (e: KeyboardEvent) => {
      const action = canvasKeyAction(e);
      if (!action) return;
      e.preventDefault(); // Cmd/Ctrl+D would bookmark the page
      const graph = useGraphStore.getState();
      const selected = graph.nodes.filter((n) => n.selected).map((n) => n.id);
      if (action === "duplicate") {
        if (selected.length > 0) graph.duplicateNodes(selected);
      } else if (selected.length > 0) {
        focusNodes(flow, selected);
      } else if (graph.nodes.length > 0) {
        void flow.fitView({ ...FIT_VIEW, duration: 300 });
      }
    };
    el.addEventListener("keydown", onKey);
    return () => el.removeEventListener("keydown", onKey);
  }, [host, flow]);
  return null;
}

/** Open the Projects tab and, a frame later, bring one of its controls to the artist. */
function revealInProjects(target: "open" | "new" | "import"): void {
  const tab: PanelTab = "project";
  usePanelsStore.getState().setTab(tab);
  setTimeout(() => {
    const panel = document.querySelector<HTMLElement>('[data-testid="project-panel"]');
    let el: HTMLElement | null = null;
    if (target === "open") el = panel?.querySelector<HTMLElement>('[data-testid="project-item"] button') ?? panel ?? null;
    if (target === "new") el = document.querySelector<HTMLElement>('[data-testid="create-project"] input');
    if (target === "import") {
      el = document.querySelector<HTMLElement>('[data-testid="import-client"] > button');
      if (el?.getAttribute("aria-expanded") === "false") el.click();
    }
    el?.scrollIntoView?.({ block: "nearest" });
    el?.focus();
  }, 0);
}

const EMPTY_WRAP: CSSProperties = {
  position: "absolute",
  inset: 0,
  display: "flex",
  justifyContent: "center",
  alignItems: "center",
  pointerEvents: "none",
  zIndex: 5,
};

const EMPTY_CARD: CSSProperties = {
  pointerEvents: "auto",
  maxWidth: 380,
  padding: "18px 20px",
  borderRadius: 12,
  background: "var(--panel, #2c2c2e)",
  border: "1px solid var(--hair, rgba(255, 255, 255, 0.1))",
  boxShadow: "0 12px 32px rgba(0, 0, 0, 0.4)",
};

const EMPTY_BANNER: CSSProperties = {
  ...EMPTY_CARD,
  maxWidth: "none",
  padding: "6px 10px",
  borderRadius: 8,
  display: "flex",
  alignItems: "center",
  gap: 10,
};

/** No project open: open one, start one, or bring in a client plate. Over a graph (made before any project was
 * open, so not saved) it is a slim banner along the bottom instead of a card in the middle. */
function EmptyState() {
  const projectId = useGraphStore((s) => s.projectId);
  const hasNodes = useGraphStore((s) => s.nodes.length > 0);
  if (projectId !== null) return null;
  const buttons = (
    <>
      <button type="button" className="btn btn-primary" data-testid="empty-open" onClick={() => revealInProjects("open")}>
        Open a project
      </button>
      <button type="button" className="btn" data-testid="empty-new" onClick={() => revealInProjects("new")}>
        New project
      </button>
      <button type="button" className="btn" data-testid="empty-import" onClick={() => revealInProjects("import")}>
        Import client plate
      </button>
    </>
  );
  if (hasNodes) {
    return (
      <div style={{ ...EMPTY_WRAP, alignItems: "flex-end", paddingBottom: 16 }}>
        <div style={EMPTY_BANNER} data-testid="empty-state" data-variant="banner" role="region" aria-label="No project open">
          <span style={{ color: "var(--dim, #98989d)" }}>No project open: this graph is not saved.</span>
          {buttons}
        </div>
      </div>
    );
  }
  return (
    <div style={EMPTY_WRAP}>
      <div style={EMPTY_CARD} data-testid="empty-state" data-variant="card" role="region" aria-label="No project open">
        <h2 style={{ margin: "0 0 6px", fontSize: 15, fontWeight: 600 }}>No project open</h2>
        <p style={{ margin: "0 0 14px", color: "var(--dim, #98989d)", lineHeight: 1.45 }}>
          A graph lives in its project. Open one from the Projects panel, start a new one, or import a client plate
          into an open project.
        </p>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>{buttons}</div>
      </div>
    </div>
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
  const onNodeDragStop = useGraphStore((s) => s.onNodeDragStop);
  const canvasRef = useRef<HTMLDivElement>(null);
  useDocumentTitle();

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

  // xyflow skips onConnect for a drop isValidConnection refused; turn that drop into the refusal message. A wire let
  // go on the empty canvas opens the add-and-connect picker there.
  const onConnectEnd = useCallback((e: MouseEvent | TouchEvent, state: FinalConnectionState) => {
    const refused = connectionFromDrop(state);
    if (refused) attemptConnect(refused);
    else openDropPicker(e, state);
  }, []);

  const onNodeContextMenu = useCallback((e: ReactMouseEvent, node: GraphNode) => {
    e.preventDefault();
    useCanvasMenu.getState().openMenu({ kind: "node", nodeId: node.id, x: e.clientX, y: e.clientY });
  }, []);

  const onPaneContextMenu = useCallback((e: ReactMouseEvent | MouseEvent) => {
    e.preventDefault();
    useCanvasMenu.getState().openMenu({ kind: "pane", x: e.clientX, y: e.clientY });
  }, []);

  // a menu belongs to the spot it was opened on: panning or zooming the view closes it
  const onMoveStart = useCallback(() => useCanvasMenu.getState().closeMenu(), []);

  return (
    <NodeInfoContext.Provider value={info}>
      <div className="plate-app">
        <StatusBar />
        <div className="plate-body">
          <div className="plate-canvas" ref={canvasRef} tabIndex={-1} style={{ outline: "none" }} data-testid="plate-canvas">
            <ReactFlow
              nodes={nodes}
              edges={shownEdges}
              nodeTypes={PLATE_NODE_TYPES}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onNodeDragStop={onNodeDragStop}
              onConnect={onConnect}
              onConnectEnd={onConnectEnd}
              onNodeContextMenu={onNodeContextMenu}
              onPaneContextMenu={onPaneContextMenu}
              onMoveStart={onMoveStart}
              isValidConnection={isValidConnection}
              deleteKeyCode={DELETE_KEYS}
              colorMode="dark"
              minZoom={0.2}
              fitView
              fitViewOptions={FIT_VIEW}
            >
              <Background gap={24} size={1} />
              <Controls showInteractive={false} />
              <AddPalette />
              <CanvasKeys host={canvasRef} />
              <CanvasMenus kinds={PALETTE_KINDS} />
            </ReactFlow>
            <EmptyState />
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
