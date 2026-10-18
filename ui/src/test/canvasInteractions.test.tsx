// T98: the canvas interaction model. Duplicate (params and incoming edges copied, status and take reset), the slower
// positions save after a drag, Delete/Backspace on the selection, `z` and Cmd/Ctrl+D on the canvas, the node and
// pane context menus, the connect-drop picker, Tidy by rank, Clear (asked again, then saved empty), the empty state
// with no placeholder nodes, the save-state chip and the tab title, Colour back in the palette, and the node card's
// tooltip and double-click. The daemon is a fetch stub answering by "METHOD /path"; xyflow's `fitView` is wrapped in
// a spy (jsdom never measures nodes, so the viewport itself cannot be read).

import { type FinalConnectionState, Position, ReactFlowProvider } from "@xyflow/react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { dropKinds, dropVerdict, openDropPicker, useCanvasMenu } from "../canvas/ContextMenu";
import { FOCUS_OPTIONS, TIDY_COLUMN_PX, TIDY_ROW_PX, tidyPositions, tidyRanks } from "../canvas/layout";
import { useRunStore } from "../canvas/runStore";
import type { LoadableGraph, NodeKind } from "../nodeTypes";
import { nodeTooltip } from "../nodes/NodeShell";
import { initialPanelsState, usePanelsStore } from "../panels/store";
import PlateCanvas, { canvasKeyAction, clockTime, PALETTE_HIDDEN, PALETTE_KINDS } from "../PlateCanvas";
import { createInitialGraph, DUPLICATE_OFFSET_PX, POSITION_SAVE_DEBOUNCE_MS, SAVE_DEBOUNCE_MS, useGraphStore } from "../store";

// ---------------------------------------------------------------------------- xyflow's fitView, spied

const { fitSpy } = vi.hoisted(() => ({ fitSpy: vi.fn() }));

vi.mock("@xyflow/react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@xyflow/react")>();
  const wrapped = new WeakMap<object, unknown>();
  return {
    ...actual,
    useReactFlow: () => {
      const rf = actual.useReactFlow();
      let out = wrapped.get(rf);
      if (!out) {
        out = {
          ...rf,
          fitView: (options?: unknown) => {
            fitSpy(options);
            return Promise.resolve(true);
          },
        };
        wrapped.set(rf, out);
      }
      return out;
    },
  };
});

// ---------------------------------------------------------------------------- the daemon stub

interface Call {
  method: string;
  path: string;
  body: unknown;
}

type Reply = { status: number; body: unknown };
type Route = unknown | ((call: Call) => unknown);

const reply = (body: unknown, status = 200): Reply => ({ status, body });
const isReply = (v: unknown): v is Reply =>
  typeof v === "object" && v !== null && "status" in v && "body" in v && Object.keys(v).length === 2;

function mockApi(routes: Record<string, Route>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://127.0.0.1");
      const method = (init?.method ?? "GET").toUpperCase();
      const path = url.pathname.replace(/^\/api\/v0/, "");
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      const call: Call = { method, path, body };
      calls.push(call);
      const route = routes[`${method} ${path}`];
      const out = route === undefined ? reply({ error: "not_found" }, 404) : typeof route === "function" ? (route as (c: Call) => unknown)(call) : route;
      const { status, body: payload } = isReply(out) ? out : reply(out);
      const text = payload === undefined ? "" : JSON.stringify(payload);
      return { ok: status >= 200 && status < 300, status, text: async () => text } as unknown as Response;
    }),
  );
  return calls;
}

const echoGraph = (call: Call) => ({ ok: true, project_id: "p", graph: call.body });
const puts = (calls: Call[]) => calls.filter((c) => c.method === "PUT" && c.path === "/projects/p/graph");
type WireBody = { nodes: { id: string; position: { x: number; y: number } }[]; edges: { id: string; source: string; target: string }[] };
const sent = (call: Call) => call.body as WireBody;

async function flush(rounds = 30): Promise<void> {
  await act(async () => {
    for (let i = 0; i < rounds; i += 1) await Promise.resolve();
  });
}

function advance(ms: number): void {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

const fakeTimers = () => vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });

// ---------------------------------------------------------------------------- fixtures

/** Plate -> Edit (gated, approved, with a prompt and a take). */
const GRAPH: LoadableGraph = {
  schema: 1,
  nodes: [
    { id: "plate-1", kind: "Plate", label: "Room A", packetId: "room_a", tier: "A", outputHash: "h_plate_0001", outputSize: { width: 640, height: 360 }, position: { x: 0, y: 0 } },
    {
      id: "edit-1",
      kind: "Edit",
      label: "Dusk",
      gateStatus: "pass",
      approved: true,
      approvedBy: "human",
      scrubLogId: "s1",
      outputHash: "abcdef1234567890",
      labels: ["night relight"],
      params: { prompt: "dusk relight", tokens: { "[SKY]": "late blue hour" } },
      position: { x: 300, y: 20 },
    },
  ],
  edges: [{ id: "e0", source: "plate-1", target: "edit-1", sourceHandle: "image", targetHandle: "image" }],
};

const graph = () => useGraphStore.getState();
const nodeOf = (id: string) => {
  const n = graph().nodes.find((x) => x.id === id);
  if (!n) throw new Error(`no node ${id}`);
  return n;
};

function select(...ids: string[]): void {
  useGraphStore.setState((s) => ({ nodes: s.nodes.map((n) => ({ ...n, selected: ids.includes(n.id) })) }));
}

const mountCanvas = () =>
  render(
    <ReactFlowProvider>
      <PlateCanvas />
    </ReactFlowProvider>,
  );

const canvasEl = () => screen.getByTestId("plate-canvas");
const paneEl = () => document.querySelector(".react-flow__pane") as HTMLElement;
const card = (id: string) => document.querySelector(`[data-node-id="${id}"]`) as HTMLElement;

/** What xyflow hands `onConnectEnd` for a wire from `nodeId`'s `handle` output let go away from any handle. */
function dropState(nodeId: string, handle: string): FinalConnectionState {
  const fromHandle = { id: handle, nodeId, type: "source", position: Position.Right, x: 0, y: 0, width: 8, height: 8 };
  return {
    isValid: null,
    from: { x: 0, y: 0 },
    fromHandle,
    fromPosition: Position.Right,
    fromNode: { id: nodeId },
    to: { x: 500, y: 300 },
    toHandle: null,
    toPosition: null,
    toNode: null,
    pointer: { x: 500, y: 300 },
  } as unknown as FinalConnectionState;
}

const ORIGINAL_SET_SIDE = useRunStore.getState().setCompareSide;

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  useRunStore.getState().reset();
  usePanelsStore.setState(initialPanelsState());
  useCanvasMenu.setState({ menu: null });
  fitSpy.mockClear();
  window.sessionStorage.clear();
  document.title = "PLATE";
});

afterEach(() => {
  useRunStore.setState({ setCompareSide: ORIGINAL_SET_SIDE } as never);
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------- duplicate

describe("duplicate", () => {
  it("copies the label, params and labels, resets the status and the take, and re-makes the incoming edge", () => {
    graph().loadProject(GRAPH);
    const ids = graph().duplicateNodes(["edit-1"]);
    expect(ids).toEqual(["edit-2"]);
    const orig = nodeOf("edit-1");
    const copy = nodeOf("edit-2");
    expect(copy.data).toMatchObject({ kind: "Edit", label: "Dusk", labels: ["night relight"], gateStatus: "none", approved: false, overrides: [] });
    expect(copy.data.params).toEqual({ prompt: "dusk relight", tokens: { "[SKY]": "late blue hour" } });
    expect(copy.data.params).not.toBe(orig.data.params); // a deep copy: editing one never edits the other
    expect(copy.data.approvedBy).toBeUndefined();
    expect(copy.data.scrubLogId).toBeUndefined();
    expect(copy.data.outputHash).toBeUndefined();
    expect(copy.position).toEqual({ x: 300 + DUPLICATE_OFFSET_PX, y: 20 + DUPLICATE_OFFSET_PX });
    expect(copy.selected).toBe(true);
    expect(orig.selected).toBeFalsy();
    const into = graph().edges.filter((e) => e.target === "edit-2");
    expect(into).toHaveLength(1);
    expect(into[0]).toMatchObject({ source: "plate-1", sourceHandle: "image", targetHandle: "image" });
    expect(graph().edges).toHaveLength(2);
    expect(graph().violations).toEqual([]);
  });

  it("a copied Plate keeps its packet link; edges between copied nodes join the copies; Frames are copied too", () => {
    graph().loadProject({
      ...GRAPH,
      nodes: [...GRAPH.nodes, { id: "frame-1", kind: "Frame", label: "Frame", params: { title: "Lobby", w: 600, h: 300 }, position: { x: -20, y: -20 } }],
    });
    graph().duplicateNodes(["plate-1", "edit-1", "frame-1"]);
    expect(nodeOf("plate-2").data).toMatchObject({ packetId: "room_a", tier: "A", outputHash: "h_plate_0001", outputSize: { width: 640, height: 360 } });
    expect(nodeOf("frame-2").data.params).toEqual({ title: "Lobby", w: 600, h: 300 });
    expect(nodeOf("frame-2").position).toEqual({ x: 20, y: 20 });
    expect(graph().edges.filter((e) => e.target === "edit-2").map((e) => e.source)).toEqual(["plate-2"]);
  });

  it("an incoming edge the rules refuse for the copy is left out and said so (an unapproved copy cannot feed Motion)", () => {
    graph().loadProject({
      ...GRAPH,
      nodes: [...GRAPH.nodes, { id: "motion-1", kind: "Motion", label: "Motion", position: { x: 600, y: 0 } }],
      edges: [...GRAPH.edges, { id: "e1", source: "edit-1", target: "motion-1", sourceHandle: "image", targetHandle: "image" }],
    });
    graph().duplicateNodes(["edit-1", "motion-1"]);
    expect(graph().edges.some((e) => e.source === "plate-1" && e.target === "edit-2")).toBe(true);
    expect(graph().edges.some((e) => e.target === "motion-2")).toBe(false);
    expect(graph().lastRefusalCode).toBe("VIDEO_LOCKED");
    expect(graph().lastRefusal).toMatch(/^1 edge not copied: /);
  });

  it("Cmd/Ctrl+D on the canvas duplicates the selection and keeps the browser from bookmarking", () => {
    mockApi({});
    graph().loadProject(GRAPH);
    mountCanvas();
    act(() => select("edit-1"));
    let notPrevented = true;
    act(() => {
      notPrevented = fireEvent.keyDown(canvasEl(), { key: "d", metaKey: true });
    });
    expect(notPrevented).toBe(false);
    expect(graph().nodes.map((n) => n.id)).toEqual(["plate-1", "edit-1", "edit-2"]);
    act(() => {
      fireEvent.keyDown(canvasEl(), { key: "d", ctrlKey: true });
    });
    expect(graph().nodes.map((n) => n.id)).toEqual(["plate-1", "edit-1", "edit-2", "edit-3"]); // the copy was selected
  });
});

// ---------------------------------------------------------------------------- positions save

describe("positions save", () => {
  it("a drag stop issues one PUT after 1500 ms and none before; the chip goes unsaved → saving… → saved", async () => {
    fakeTimers();
    const calls = mockApi({ "PUT /projects/p/graph": echoGraph });
    graph().loadProject(GRAPH, "p");
    act(() => graph().onNodesChange([{ type: "position", id: "edit-1", position: { x: 420, y: 60 }, dragging: true }]));
    act(() => graph().onNodesChange([{ type: "position", id: "edit-1", position: { x: 440, y: 80 }, dragging: false }]));
    advance(SAVE_DEBOUNCE_MS * 3); // a move alone is never a structural save
    expect(puts(calls)).toHaveLength(0);
    expect(graph().saveState).toBe("saved");

    act(() => graph().onNodeDragStop());
    expect(graph().saveState).toBe("unsaved");
    advance(POSITION_SAVE_DEBOUNCE_MS - 1);
    await flush();
    expect(puts(calls)).toHaveLength(0);
    advance(1);
    expect(graph().saveState).toBe("saving");
    await flush();
    expect(puts(calls)).toHaveLength(1);
    expect(sent(puts(calls)[0]).nodes.find((n) => n.id === "edit-1")?.position).toEqual({ x: 440, y: 80 });
    expect(graph().saveState).toBe("saved");
    expect(graph().savedAt).toEqual(expect.any(Number));
    advance(5000);
    await flush();
    expect(puts(calls)).toHaveLength(1);
  });

  it("drags in a row are one PUT; a structural save due sooner carries the positions", async () => {
    fakeTimers();
    const calls = mockApi({ "PUT /projects/p/graph": echoGraph });
    graph().loadProject(GRAPH, "p");
    act(() => graph().onNodesChange([{ type: "position", id: "edit-1", position: { x: 1, y: 2 } }]));
    act(() => graph().onNodeDragStop());
    advance(1000);
    act(() => graph().onNodesChange([{ type: "position", id: "plate-1", position: { x: 3, y: 4 } }]));
    act(() => graph().onNodeDragStop());
    advance(POSITION_SAVE_DEBOUNCE_MS);
    await flush();
    expect(puts(calls)).toHaveLength(1);

    act(() => graph().onNodesChange([{ type: "position", id: "edit-1", position: { x: 9, y: 9 } }]));
    act(() => graph().onNodeDragStop());
    act(() => graph().addNode("Gate", { x: 0, y: 400 }));
    advance(SAVE_DEBOUNCE_MS);
    await flush();
    expect(puts(calls)).toHaveLength(2);
    expect(sent(puts(calls)[1]).nodes.find((n) => n.id === "edit-1")?.position).toEqual({ x: 9, y: 9 });
    advance(POSITION_SAVE_DEBOUNCE_MS * 2);
    await flush();
    expect(puts(calls)).toHaveLength(2);
  });

  it("a drag stop with no project open saves nothing", () => {
    fakeTimers();
    const calls = mockApi({});
    graph().loadProject(GRAPH);
    act(() => graph().onNodeDragStop());
    advance(POSITION_SAVE_DEBOUNCE_MS * 2);
    expect(calls).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------- keys

describe("keys", () => {
  it("Delete removes the selection and its edges; Backspace does too; neither does while typing", async () => {
    mockApi({});
    graph().loadProject({
      ...GRAPH,
      nodes: [...GRAPH.nodes, { id: "gate-1", kind: "Gate", label: "Gate", position: { x: 600, y: 0 } }],
      edges: [...GRAPH.edges, { id: "e1", source: "edit-1", target: "gate-1", sourceHandle: "image", targetHandle: "candidate" }],
    });
    mountCanvas();
    act(() => select("edit-1"));
    fireEvent.keyDown(document.body, { key: "Delete", code: "Delete" });
    fireEvent.keyUp(document.body, { key: "Delete", code: "Delete" });
    await waitFor(() => expect(graph().nodes.map((n) => n.id)).toEqual(["plate-1", "gate-1"]));
    expect(graph().edges).toEqual([]);

    const field = document.createElement("input");
    document.body.appendChild(field);
    act(() => select("gate-1"));
    fireEvent.keyDown(field, { key: "Backspace", code: "Backspace" });
    fireEvent.keyUp(field, { key: "Backspace", code: "Backspace" });
    await flush();
    expect(graph().nodes.map((n) => n.id)).toEqual(["plate-1", "gate-1"]);
    field.remove();

    fireEvent.keyDown(document.body, { key: "Backspace", code: "Backspace" });
    fireEvent.keyUp(document.body, { key: "Backspace", code: "Backspace" });
    await waitFor(() => expect(graph().nodes.map((n) => n.id)).toEqual(["plate-1"]));
  });

  it("z focuses the selected node (fitView with that node, 300 ms, at most 150%); with nothing selected it fits", () => {
    mockApi({});
    graph().loadProject(GRAPH);
    mountCanvas();
    act(() => select("edit-1"));
    fitSpy.mockClear();
    fireEvent.keyDown(canvasEl(), { key: "z" });
    expect(fitSpy).toHaveBeenCalledTimes(1);
    expect(fitSpy).toHaveBeenCalledWith({ nodes: [{ id: "edit-1" }], ...FOCUS_OPTIONS });
    expect(FOCUS_OPTIONS).toEqual({ duration: 300, maxZoom: 1.5 });

    act(() => select());
    fitSpy.mockClear();
    fireEvent.keyDown(canvasEl(), { key: "z" });
    expect(fitSpy).toHaveBeenCalledWith(expect.objectContaining({ duration: 300 }));
    expect((fitSpy.mock.calls[0][0] as { nodes?: unknown }).nodes).toBeUndefined();
  });

  it("canvasKeyAction: z and Cmd/Ctrl+D only, never inside a text field", () => {
    const field = document.createElement("textarea");
    const base = { metaKey: false, ctrlKey: false, altKey: false, defaultPrevented: false, target: document.body };
    expect(canvasKeyAction({ ...base, key: "z" })).toBe("focus");
    expect(canvasKeyAction({ ...base, key: "d", metaKey: true })).toBe("duplicate");
    expect(canvasKeyAction({ ...base, key: "D", ctrlKey: true })).toBe("duplicate");
    expect(canvasKeyAction({ ...base, key: "z", metaKey: true })).toBeNull(); // the browser's undo
    expect(canvasKeyAction({ ...base, key: "d" })).toBeNull();
    expect(canvasKeyAction({ ...base, key: "z", target: field })).toBeNull();
  });
});

// ---------------------------------------------------------------------------- node menu

describe("node context menu", () => {
  it("lists Duplicate, Focus, Copy prompt, Copy output hash and Delete; Set as A / B only with the viewer's A/B", async () => {
    mockApi({});
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    graph().loadProject(GRAPH);
    mountCanvas();

    useRunStore.setState({ setCompareSide: undefined } as never);
    fireEvent.contextMenu(card("edit-1"), { clientX: 200, clientY: 120 });
    let menu = within(screen.getByTestId("canvas-menu-node"));
    for (const id of ["duplicate", "focus", "copy-prompt", "copy-hash", "delete"]) expect(menu.getByTestId(`menu-${id}`)).toBeInTheDocument();
    expect(menu.queryByTestId("menu-set-a")).toBeNull();
    expect(menu.queryByTestId("menu-set-b")).toBeNull();
    expect(menu.queryByTestId("menu-open")).toBeNull(); // no run and no A/B yet: nothing to open

    fireEvent.click(menu.getByTestId("menu-copy-prompt"));
    expect(screen.queryByTestId("canvas-menu-node")).toBeNull(); // a pick closes the menu
    await flush();
    expect(writeText).toHaveBeenCalledWith("dusk relight");

    const setSide = vi.fn();
    useRunStore.setState({ setCompareSide: setSide } as never);
    fireEvent.contextMenu(card("edit-1"), { clientX: 200, clientY: 120 });
    menu = within(screen.getByTestId("canvas-menu-node"));
    fireEvent.click(menu.getByTestId("menu-copy-hash"));
    await flush();
    expect(writeText).toHaveBeenLastCalledWith("abcdef1234567890");
    fireEvent.contextMenu(card("edit-1"), { clientX: 200, clientY: 120 });
    fireEvent.click(within(screen.getByTestId("canvas-menu-node")).getByTestId("menu-set-b"));
    expect(setSide).toHaveBeenCalledWith("b", { kind: "node", nodeId: "edit-1" });
    fireEvent.contextMenu(card("plate-1"), { clientX: 20, clientY: 20 });
    fireEvent.click(within(screen.getByTestId("canvas-menu-node")).getByTestId("menu-set-a"));
    expect(setSide).toHaveBeenLastCalledWith("a", { kind: "plate", nodeId: "plate-1" });
  });

  it("Open in viewer compares the node's run with its plate; Focus fits it; Duplicate and Delete act on it", () => {
    mockApi({});
    graph().loadProject(GRAPH);
    useRunStore.getState().startRuns("edit-1", [{ id: "run_9", state: "DONE", node_id: "edit-1", outputs: ["h_run_9"] }]);
    mountCanvas();
    fireEvent.contextMenu(card("edit-1"), { clientX: 200, clientY: 120 });
    fireEvent.click(within(screen.getByTestId("canvas-menu-node")).getByTestId("menu-open"));
    expect(useRunStore.getState().compare).not.toBeNull();
    expect(JSON.stringify(useRunStore.getState().compare)).toContain("run_9");

    fireEvent.contextMenu(card("edit-1"), { clientX: 200, clientY: 120 });
    fitSpy.mockClear();
    fireEvent.click(within(screen.getByTestId("canvas-menu-node")).getByTestId("menu-focus"));
    expect(fitSpy).toHaveBeenCalledWith({ nodes: [{ id: "edit-1" }], ...FOCUS_OPTIONS });

    fireEvent.contextMenu(card("edit-1"), { clientX: 200, clientY: 120 });
    fireEvent.click(within(screen.getByTestId("canvas-menu-node")).getByTestId("menu-duplicate"));
    expect(graph().nodes.map((n) => n.id)).toEqual(["plate-1", "edit-1", "edit-2"]);

    fireEvent.contextMenu(card("edit-1"), { clientX: 200, clientY: 120 });
    fireEvent.click(within(screen.getByTestId("canvas-menu-node")).getByTestId("menu-delete"));
    expect(graph().nodes.map((n) => n.id)).toEqual(["plate-1", "edit-2"]);
    expect(graph().edges.map((e) => e.target)).toEqual(["edit-2"]);
  });

  it("on a selected node the menu acts on the whole selection; Escape and a press outside close it", () => {
    mockApi({});
    graph().loadProject(GRAPH);
    mountCanvas();
    act(() => select("plate-1", "edit-1"));
    fireEvent.contextMenu(card("edit-1"), { clientX: 200, clientY: 120 });
    const menu = within(screen.getByTestId("canvas-menu-node"));
    expect(menu.getByTestId("menu-duplicate")).toHaveTextContent("Duplicate 2 nodes");
    expect(menu.getByTestId("menu-delete")).toHaveTextContent("Delete 2 nodes");
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByTestId("canvas-menu-node")).toBeNull();

    fireEvent.contextMenu(card("edit-1"), { clientX: 200, clientY: 120 });
    fireEvent.keyDown(screen.getByTestId("menu-focus"), { key: "Escape" }); // focus inside the menu
    expect(screen.queryByTestId("canvas-menu-node")).toBeNull();

    fireEvent.contextMenu(card("edit-1"), { clientX: 200, clientY: 120 });
    fireEvent.pointerDown(paneEl());
    expect(screen.queryByTestId("canvas-menu-node")).toBeNull();
    expect(graph().nodes).toHaveLength(2);
  });
});

// ---------------------------------------------------------------------------- pane menu, Tidy, Clear

describe("pane context menu", () => {
  it("Add node here adds the picked kind; Fit fits the graph", () => {
    mockApi({});
    graph().loadProject(GRAPH);
    mountCanvas();
    fireEvent.contextMenu(paneEl(), { clientX: 640, clientY: 360 });
    const menu = within(screen.getByTestId("canvas-menu-pane"));
    fireEvent.click(menu.getByTestId("menu-add"));
    const kinds = within(screen.getByTestId("submenu-add"))
      .getAllByRole("menuitem")
      .map((b) => b.textContent);
    expect(kinds).toEqual([...PALETTE_KINDS]);
    fireEvent.click(screen.getByTestId("menu-add-Colour"));
    expect(graph().nodes.map((n) => n.data.kind)).toEqual(["Plate", "Edit", "Colour"]);
    expect(screen.queryByTestId("canvas-menu-pane")).toBeNull();

    fireEvent.contextMenu(paneEl(), { clientX: 640, clientY: 360 });
    fitSpy.mockClear();
    fireEvent.click(screen.getByTestId("menu-fit"));
    expect(fitSpy).toHaveBeenCalledWith({ maxZoom: 1, padding: 0.2, duration: 300 });
  });

  it("Tidy places a Plate at x 0 and its Edit at x 340, by longest path; unanchored nodes after; Frames stay", async () => {
    const calls = mockApi({ "PUT /projects/p/graph": echoGraph });
    graph().loadProject(
      {
        schema: 1,
        nodes: [
          { id: "plate-1", kind: "Plate", label: "Plate", packetId: "room_a", tier: "A", position: { x: 500, y: 500 } },
          { id: "edit-1", kind: "Edit", label: "Edit", position: { x: 13, y: 77 } },
          { id: "comp-1", kind: "Comp", label: "Comp", position: { x: -40, y: 900 } },
          { id: "ref-1", kind: "Reference", label: "Reference", position: { x: 9, y: 9 } },
          { id: "frame-1", kind: "Frame", label: "Frame", position: { x: 777, y: 888 } },
        ],
        edges: [
          { id: "a", source: "plate-1", target: "edit-1", sourceHandle: "image", targetHandle: "image" },
          { id: "b", source: "edit-1", target: "comp-1", sourceHandle: "image", targetHandle: "layers" },
          { id: "c", source: "plate-1", target: "comp-1", sourceHandle: "image", targetHandle: "base" },
          { id: "d", source: "ref-1", target: "edit-1", sourceHandle: "ref", targetHandle: "refs" },
        ],
      },
      "p",
    );
    mountCanvas();
    fireEvent.contextMenu(paneEl(), { clientX: 640, clientY: 360 });
    fireEvent.click(screen.getByTestId("menu-tidy"));
    await flush();
    expect(nodeOf("plate-1").position).toEqual({ x: 0, y: 0 });
    expect(nodeOf("edit-1").position).toEqual({ x: TIDY_COLUMN_PX, y: 0 });
    expect(nodeOf("comp-1").position).toEqual({ x: 2 * TIDY_COLUMN_PX, y: 0 }); // longest path, not the shortest
    expect(nodeOf("ref-1").position).toEqual({ x: 3 * TIDY_COLUMN_PX, y: 0 });
    expect(nodeOf("frame-1").position).toEqual({ x: 777, y: 888 });
    expect(puts(calls)).toHaveLength(1);
    expect(sent(puts(calls)[0]).nodes.find((n) => n.id === "edit-1")?.position).toEqual({ x: 340, y: 0 });
  });

  it("tidyRanks stacks a rank by id (numeric-aware) and survives a cycle in a loaded graph", () => {
    graph().loadProject({
      schema: 1,
      nodes: [
        { id: "plate-1", kind: "Plate", label: "Plate", packetId: "room_a", tier: "A" },
        { id: "edit-10", kind: "Edit", label: "Edit" },
        { id: "edit-2", kind: "Edit", label: "Edit" },
        { id: "x", kind: "Gate", label: "Gate" },
        { id: "y", kind: "Gate", label: "Gate" },
      ],
      edges: [
        { id: "a", source: "plate-1", target: "edit-10", sourceHandle: null, targetHandle: null },
        { id: "b", source: "plate-1", target: "edit-2", sourceHandle: null, targetHandle: null },
        { id: "c", source: "x", target: "y", sourceHandle: null, targetHandle: null },
        { id: "d", source: "y", target: "x", sourceHandle: null, targetHandle: null },
      ],
    });
    const { nodes, edges } = graph();
    expect(Object.fromEntries(tidyRanks(nodes, edges))).toEqual({ "plate-1": 0, "edit-10": 1, "edit-2": 1, x: 2, y: 2 });
    const at = tidyPositions(nodes, edges);
    expect(at.get("edit-2")).toEqual({ x: TIDY_COLUMN_PX, y: 0 });
    expect(at.get("edit-10")).toEqual({ x: TIDY_COLUMN_PX, y: TIDY_ROW_PX });
  });

  it("Clear asks once more, then PUTs an empty graph", async () => {
    const calls = mockApi({ "PUT /projects/p/graph": echoGraph });
    graph().loadProject(GRAPH, "p");
    mountCanvas();
    fireEvent.contextMenu(paneEl(), { clientX: 640, clientY: 360 });
    fireEvent.click(screen.getByTestId("menu-clear"));
    expect(screen.getByTestId("menu-confirm-box")).toHaveTextContent("Remove all 2 nodes and 1 edge?");
    fireEvent.click(screen.getByTestId("menu-cancel"));
    expect(graph().nodes).toHaveLength(2);
    fireEvent.click(screen.getByTestId("menu-clear"));
    fireEvent.click(screen.getByTestId("menu-confirm"));
    await flush();
    expect(graph().nodes).toEqual([]);
    expect(graph().edges).toEqual([]);
    expect(puts(calls)).toHaveLength(1);
    expect(sent(puts(calls)[0])).toMatchObject({ schema: 1, nodes: [], edges: [] });
    fireEvent.contextMenu(paneEl(), { clientX: 640, clientY: 360 });
    expect(screen.getByTestId("menu-clear")).toBeDisabled();
    expect(screen.getByTestId("menu-tidy")).toBeDisabled();
  });
});

// ---------------------------------------------------------------------------- connect-drop

describe("connect-drop on the empty canvas", () => {
  it("an image16 output offers Edit/Comp/Colour/Motion…/Export; adding one places it and wires it", () => {
    mockApi({});
    graph().loadProject({ schema: 1, nodes: [GRAPH.nodes[0]], edges: [] });
    mountCanvas();
    let opened = false;
    act(() => {
      opened = openDropPicker({ target: paneEl(), clientX: 500, clientY: 300 } as unknown as MouseEvent, dropState("plate-1", "image"));
    });
    expect(opened).toBe(true);
    const picker = within(screen.getByTestId("canvas-menu-drop"));
    expect(screen.getByTestId("canvas-menu-drop")).toHaveTextContent("Add and connect · image16");
    const offered = picker.getAllByRole("menuitem").map((b) => b.textContent);
    for (const k of ["Edit", "Comp", "Colour", "Motion", "Export"]) expect(offered).toContain(k);
    for (const k of ["LightMix", "Recolour", "Light", "Scout", "Atmosphere", "Sky", "Reference", "Frame", "Compare"]) expect(offered).not.toContain(k);
    // the rules still apply: a plate is not an approved still, so Motion is offered but refused, with the reason
    expect(picker.getByTestId("menu-drop-Motion")).toBeDisabled();
    expect(picker.getByTestId("menu-drop-Motion")).toHaveAttribute("title", expect.stringMatching(/locked/));

    fireEvent.click(picker.getByTestId("menu-drop-Edit"));
    expect(screen.queryByTestId("canvas-menu-drop")).toBeNull();
    expect(graph().nodes.map((n) => n.id)).toEqual(["plate-1", "edit-1"]);
    expect(Number.isFinite(nodeOf("edit-1").position.x)).toBe(true);
    expect(graph().edges).toHaveLength(1);
    expect(graph().edges[0]).toMatchObject({ source: "plate-1", target: "edit-1", sourceHandle: "image", targetHandle: "image" });
  });

  it("opens only for a wire from an output let go on the pane", () => {
    mockApi({});
    graph().loadProject(GRAPH);
    mountCanvas();
    const pane = { target: paneEl(), clientX: 1, clientY: 1 } as unknown as MouseEvent;
    const onNode = { target: card("edit-1"), clientX: 1, clientY: 1 } as unknown as MouseEvent;
    const onHandle = { ...dropState("plate-1", "image"), toHandle: { id: "image", nodeId: "edit-1", type: "target" } } as unknown as FinalConnectionState;
    const fromInput = { ...dropState("edit-1", "image"), fromHandle: { id: "image", nodeId: "edit-1", type: "target" } } as unknown as FinalConnectionState;
    expect(openDropPicker(onNode, dropState("plate-1", "image"))).toBe(false);
    expect(openDropPicker(pane, onHandle)).toBe(false);
    expect(openDropPicker(pane, fromInput)).toBe(false);
    expect(useCanvasMenu.getState().menu).toBeNull();
  });

  it("dropKinds: first inputs that take the type, else any input; dropVerdict runs the rules on a probe node", () => {
    const kinds: readonly NodeKind[] = PALETTE_KINDS;
    expect(dropKinds("packet", kinds)).toEqual(["Mask", "LightMix", "Recolour", "Light", "Scout", "Atmosphere", "Sky"]);
    expect(dropKinds("mask", kinds)).toEqual(["Edit", "Colour", "Recolour"]);
    expect(dropKinds("reference", kinds)).toEqual(["Edit", "Motion", "People", "Sky"]);
    expect(dropKinds("report", kinds)).toEqual(["Review"]);
    graph().loadProject(GRAPH);
    expect(dropVerdict("plate-1", "image", "Edit")).toEqual({ ok: true });
    expect(dropVerdict("plate-1", "image", "LightMix")).toMatchObject({ ok: false });
    expect(dropVerdict("edit-1", "image", "Motion")).toEqual({ ok: true }); // an approved still opens the lock
  });
});

// ---------------------------------------------------------------------------- empty state, chip, title, palette

describe("empty state, save chip, title, palette", () => {
  it("the empty state renders without a project and the placeholder nodes are gone", () => {
    mockApi({});
    expect(createInitialGraph().nodes).toEqual([]);
    mountCanvas();
    expect(screen.getByTestId("empty-state")).toHaveAttribute("data-variant", "card");
    expect(document.querySelectorAll(".react-flow__node")).toHaveLength(0);
    expect(screen.queryAllByTestId("node-item")).toHaveLength(0);
    expect(screen.queryByTestId("save-state")).toBeNull();
    usePanelsStore.setState({ tab: "gate" });
    fireEvent.click(screen.getByTestId("empty-new"));
    expect(usePanelsStore.getState().tab).toBe("project");
    for (const id of ["empty-open", "empty-import"]) expect(screen.getByTestId(id)).toBeInTheDocument();

    act(() => graph().addNode("Edit", { x: 0, y: 0 })); // a graph made before any project: not saved, says so
    expect(screen.getByTestId("empty-state")).toHaveAttribute("data-variant", "banner");
    act(() => graph().loadProject(GRAPH, "p"));
    expect(screen.queryByTestId("empty-state")).toBeNull();
  });

  it("the save chip shows save failed on a 500 (red), and saved HH:MM:SS after a good save; the title marks unsaved", async () => {
    fakeTimers();
    let fail = true;
    mockApi({
      "PUT /projects/p/graph": (call: Call) => (fail ? reply({ error: "io", detail: "disk full" }, 500) : echoGraph(call)),
    });
    graph().loadProject(GRAPH, "p");
    mountCanvas();
    expect(screen.getByTestId("save-state")).toHaveTextContent(/^saved$/);
    expect(document.title).toBe("PLATE · p");

    act(() => graph().addNode("Gate", { x: 600, y: 0 }));
    expect(screen.getByTestId("save-state")).toHaveTextContent(/^unsaved$/);
    expect(document.title).toBe("PLATE · p *");
    advance(SAVE_DEBOUNCE_MS);
    expect(screen.getByTestId("save-state")).toHaveTextContent("saving…");
    await flush();
    const chip = screen.getByTestId("save-state");
    expect(chip).toHaveAttribute("data-state", "failed");
    expect(chip).toHaveTextContent("save failed: disk full");
    expect(chip).toHaveStyle({ color: "var(--red, #ff453a)" });
    expect(document.title).toBe("PLATE · p *");

    fail = false;
    act(() => graph().addNode("Gate", { x: 600, y: 200 }));
    advance(SAVE_DEBOUNCE_MS);
    await flush();
    expect(screen.getByTestId("save-state")).toHaveAttribute("data-state", "saved");
    expect(screen.getByTestId("save-state").textContent).toMatch(/^saved \d\d:\d\d:\d\d$/);
    expect(document.title).toBe("PLATE · p");
    expect(clockTime(new Date(2026, 9, 18, 7, 5, 9).getTime())).toBe("07:05:09");
  });

  it("the palette has a Colour button; only Compare stays hidden", () => {
    mockApi({});
    mountCanvas();
    const palette = within(screen.getByTestId("add-palette"));
    expect(palette.getByTestId("add-Colour")).toBeInTheDocument();
    expect(palette.queryByTestId("add-Compare")).toBeNull();
    expect([...PALETTE_HIDDEN]).toEqual(["Compare"]);
    expect(PALETTE_KINDS).toContain("Colour");
  });
});

// ---------------------------------------------------------------------------- node card

describe("node card", () => {
  it("the tooltip names the prompt (else the recipe) and the take", () => {
    mockApi({});
    graph().loadProject({
      ...GRAPH,
      nodes: [...GRAPH.nodes, { id: "edit-2", kind: "Edit", label: "Edit", params: { recipe: "a03_dusk" }, position: { x: 0, y: 300 } }],
    });
    mountCanvas();
    expect(card("edit-1")).toHaveAttribute("title", "prompt: dusk relight · run abcdef12");
    expect(card("edit-2")).toHaveAttribute("title", "recipe: a03_dusk");
    expect(card("plate-1")).toHaveAttribute("title", "plate h_plate_");
    expect(nodeTooltip({ ...nodeOf("edit-2").data, params: {} })).toBeUndefined();
  });

  it("a double-click focuses the node and opens it in the viewer; not from its candidate strip", () => {
    mockApi({});
    graph().loadProject(GRAPH);
    useRunStore.getState().startRuns("edit-1", [{ id: "run_9", state: "DONE", node_id: "edit-1", outputs: ["h_run_9"] }]);
    mountCanvas();
    fitSpy.mockClear();
    fireEvent.doubleClick(card("edit-1").querySelector(".node-body") as HTMLElement);
    expect(fitSpy).toHaveBeenCalledWith({ nodes: [{ id: "edit-1" }], ...FOCUS_OPTIONS });
    expect(JSON.stringify(useRunStore.getState().compare)).toContain("run_9");

    act(() => useRunStore.getState().closeCompare());
    fitSpy.mockClear();
    const strip = card("edit-1").querySelector(".cand-strip");
    expect(strip).not.toBeNull();
    fireEvent.doubleClick(strip as HTMLElement);
    expect(fitSpy).not.toHaveBeenCalled();
    expect(useRunStore.getState().compare).toBeNull();
  });
});
