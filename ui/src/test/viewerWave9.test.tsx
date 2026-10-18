// T101: the judging surface. Set as A / Set as B with engine labels (any pairing is look-only: `scrubEligible`
// false, the verdict panel says what the scrub needs), History rows open the viewer (shift-click sets A), a selected
// node previews its output alone, the W × H chips, Enlarge (the graph hidden, the viewer and strip in its place, Esc
// back), Fullscreen guarded for jsdom, the Max dock chip in the status bar, and the Recomposite slot (T99's bar,
// injected here). The daemon and the event stream are fakes; jsdom has no WebGL, so the viewer runs without a GPU.

import { ReactFlowProvider } from "@xyflow/react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App, {
  formatAgo,
  HOST_POLL_MS,
  hostChip,
  parsePluginStatus,
  type RecompositeBarProps,
  recompositeSlot,
} from "../App";
import type { EventSourceLike } from "../api/client";
import { CompareViewer, SCRUB_HINT } from "../canvas/CompareViewer";
import { hub, unwatchAll } from "../canvas/runEvents";
import { type CandidateRun, isPlateVsCandidate, runLabel, useRunStore } from "../canvas/runStore";
import { EMPTY_SCRUB, quadrantsVisited } from "../panels/scrub";
import { initialPanelsState, usePanelsStore } from "../panels/store";
import { createInitialGraph, useGraphStore } from "../store";
import { toggleFullscreen, Viewer } from "../viewer/Viewer";

// ------------------------------------------------------------------------------------------------ fakes

class FakeEventSource implements EventSourceLike {
  url: string;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(url: string) {
    this.url = url;
  }
  addEventListener(): void {}
  close(): void {}
}

interface Call {
  method: string;
  path: string;
  body: unknown;
}

type Route = unknown | ((call: Call) => unknown);

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** Answers "METHOD /path" (the /api/v0 prefix and query string stripped); anything else is a recorded 404. */
function mockApi(routes: Record<string, Route> = {}): Call[] {
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
      if (route === undefined) return json({ error: `no stub for ${method} ${path}` }, 404);
      const out = typeof route === "function" ? (route as (c: Call) => unknown)(call) : route;
      return out instanceof Response ? out : json(out);
    }),
  );
  return calls;
}

// ------------------------------------------------------------------------------------------------ fixtures

const PLATE = { width: 640, height: 360 };

const GRAPH = {
  schema: 1 as const,
  nodes: [
    { kind: "Plate" as const, id: "plate-1", label: "Plate", packetId: "room_a", tier: "A" as const, outputHash: "h_plate", outputSize: PLATE },
    { kind: "Edit" as const, id: "edit-1", label: "Edit" },
  ],
  edges: [{ id: "plate-1->edit-1", source: "plate-1", target: "edit-1", sourceHandle: null, targetHandle: null }],
};

const run = (id: string, over: Partial<CandidateRun> = {}): CandidateRun => ({
  id,
  state: "DONE",
  node_id: "edit-1",
  outputs: [`h_${id}`],
  route: { engine_id: "nano-banana-2.1" },
  attempt: 1,
  ...over,
});

const R1 = run("r1", { sibling_index: 0, gate_score: 0.9 });
const R2 = run("r2", { sibling_index: 1, gate_score: 0.5, route: { engine_id: "gpt-image-2.5" }, attempt: 2 });

const LABEL_1 = "Edit · nano-banana-2.1 · attempt 1";
const LABEL_2 = "Edit · gpt-image-2.5 · attempt 2";

const openGraph = () => useGraphStore.getState().loadProject(GRAPH, "p1");

const select = (id: string | null) =>
  act(() => {
    const g = useGraphStore.getState();
    g.onNodesChange(g.nodes.map((n) => ({ type: "select" as const, id: n.id, selected: n.id === id })));
  });

const mountApp = () =>
  render(
    <ReactFlowProvider>
      <App />
    </ReactFlowProvider>,
  );

/** The first strip on screen (NodeShell's on the canvas); `within` the enlarged stage for the docked one. */
const rowOf = (runId: string, root: HTMLElement = document.body): HTMLElement => {
  const row = root.querySelector<HTMLElement>(`[data-testid="candidate"][data-run="${runId}"]`);
  if (!row) throw new Error(`no candidate row ${runId}`);
  return row;
};

const caption = () => screen.getByTestId("compare-caption");

const originalLoad = recompositeSlot.load;

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  useRunStore.getState().reset();
  usePanelsStore.setState(initialPanelsState());
  unwatchAll();
  hub.setFactory((url) => new FakeEventSource(url));
  window.sessionStorage.clear();
});

afterEach(() => {
  recompositeSlot.load = originalLoad;
  unwatchAll();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ------------------------------------------------------------------------------------------------ A/B picker

describe("A/B picker with engine labels", () => {
  it("labels each strip row `<node label> · <engine_id> · attempt n`", () => {
    mockApi();
    openGraph();
    useRunStore.getState().startRuns("edit-1", [R1, R2]);
    mountApp();
    expect(within(rowOf("r1")).getByTestId("candidate-label")).toHaveTextContent(LABEL_1);
    expect(within(rowOf("r2")).getByTestId("candidate-label")).toHaveTextContent(LABEL_2);
    expect(useRunStore.getState().candidates["r2"]).toMatchObject({ engineId: "gpt-image-2.5", attempt: 2 });
    expect(runLabel("Edit", null, null)).toBe("Edit");
  });

  it("Set as A / Set as B pairs two takes: the caption names both, and the scrub is not eligible", () => {
    mockApi();
    openGraph();
    useRunStore.getState().startRuns("edit-1", [R1, R2]);
    mountApp();
    fireEvent.click(within(rowOf("r1")).getByTestId("cand-set-a"));
    fireEvent.click(within(rowOf("r2")).getByTestId("cand-set-b"));
    expect(caption()).toHaveTextContent(`A ${LABEL_1} · B ${LABEL_2}`);
    const s = useRunStore.getState();
    expect(s.scrubEligible).toBe(false);
    expect(s.compare?.a).toMatchObject({ kind: "run", runId: "r1", nodeId: "edit-1", label: LABEL_1 });
    expect(s.compare?.b).toMatchObject({ kind: "run", runId: "r2", nodeId: "edit-1", label: LABEL_2 });
    expect(rowOf("r1")).toHaveAttribute("data-side", "a");
    expect(rowOf("r2")).toHaveAttribute("data-side", "b");
    expect(within(rowOf("r1")).getByTestId("cand-set-a")).toHaveAttribute("aria-pressed", "true");
    // the verdict panel says what the scrub needs, and the view is not bound to it
    const verdict = screen.getByTestId("verdict-panel");
    expect(within(verdict).getByTestId("scrub-hint")).toHaveTextContent(SCRUB_HINT);
    expect(screen.getByTestId("compare")).toHaveAttribute("data-bound", "false");
    expect(screen.queryByTestId("connected-viewer")).toBeNull();
  });

  it("the candidate click is still a plate-vs-candidate compare, eligible and bound to the selected run", () => {
    mockApi();
    openGraph();
    useRunStore.getState().startRuns("edit-1", [R1, R2]);
    mountApp();
    fireEvent.click(within(rowOf("r1")).getByTitle("compare with the plate in the viewer"));
    expect(caption()).toHaveTextContent("A plate · B candidate 1");
    expect(useRunStore.getState().scrubEligible).toBe(true);
    expect(usePanelsStore.getState().selectedRunId).toBe("r1");
    expect(screen.getByTestId("compare")).toHaveAttribute("data-bound", "true");
    expect(screen.getByTestId("connected-viewer")).toBeInTheDocument();
    expect(screen.queryByTestId("scrub-hint")).toBeNull();
    // Set as A on the other take turns it into a look-only pair
    fireEvent.click(within(rowOf("r2")).getByTestId("cand-set-a"));
    expect(useRunStore.getState().scrubEligible).toBe(false);
    expect(caption()).toHaveTextContent(`A ${LABEL_2} · B ${LABEL_1}`);
  });

  it("a look-only pair never feeds the scrub; the plate pairing does", () => {
    mockApi();
    openGraph();
    useRunStore.getState().startRuns("edit-1", [R1, R2]);
    mountApp();
    act(() => usePanelsStore.getState().selectRun("r1"));
    fireEvent.click(within(rowOf("r2")).getByTestId("cand-set-a"));
    fireEvent.click(within(rowOf("r1")).getByTestId("cand-set-b"));
    fireEvent.keyDown(screen.getByTestId("viewer"), { key: "1" }); // 100%: would count quadrants if it were bound
    expect(usePanelsStore.getState().scrub).toEqual(EMPTY_SCRUB);
    // a 640×360 plate at fit in the 1280×720 test viewport is above 100%: the bound view counts it at once
    fireEvent.click(within(rowOf("r1")).getByTitle("compare with the plate in the viewer"));
    expect(quadrantsVisited(usePanelsStore.getState().scrub)).toBe(4);
  });

  it("setCompareSide takes T98's refs without labels and fills them from the graph", () => {
    mockApi();
    openGraph();
    useRunStore.getState().startRuns("edit-1", [R1]);
    act(() => useRunStore.getState().setCompareSide("b", { kind: "run", runId: "r1" }));
    let s = useRunStore.getState();
    // nothing was open: A becomes the plate of the run's node, which is the eligible pairing
    expect(s.compare?.a).toMatchObject({ kind: "plate", nodeId: "edit-1", label: "plate" });
    expect(s.compare?.b).toMatchObject({ kind: "run", runId: "r1", nodeId: "edit-1", label: LABEL_1 });
    expect(s.scrubEligible).toBe(true);
    act(() => useRunStore.getState().setCompareSide("a", { kind: "node", nodeId: "plate-1" }));
    s = useRunStore.getState();
    expect(s.compare?.a).toMatchObject({ kind: "node", nodeId: "plate-1", label: "Plate" });
    expect(s.scrubEligible).toBe(false);
    expect(isPlateVsCandidate({ a: { kind: "plate", nodeId: "plate-1", label: "p" }, b: s.compare!.b })).toBe(true);
    act(() => useRunStore.getState().closeCompare());
    expect(useRunStore.getState()).toMatchObject({ compare: null, scrubEligible: false });
  });
});

// ------------------------------------------------------------------------------------------------ History

describe("History opens the viewer", () => {
  const ROWS = [
    { ...run("r-old"), updated_utc: "2026-10-18T09:00:00Z", route: { engine_id: "nano-banana-2.1" }, attempt: 1 },
    { ...run("r-new"), updated_utc: "2026-10-18T11:00:00Z", route: { engine_id: "gpt-image-2.5" }, attempt: 2 },
  ];

  async function showHistory() {
    mockApi({ "GET /projects": [], "GET /projects/p1/runs": { runs: ROWS } });
    openGraph();
    mountApp();
    fireEvent.click(screen.getByRole("tab", { name: "History" }));
    return screen.findAllByTestId("history-run");
  }

  const historyRow = (rows: HTMLElement[], id: string) => rows.find((r) => r.dataset.runId === id) as HTMLElement;

  it("a row click compares the run (B) with its plate (A) and selects it", async () => {
    const rows = await showHistory();
    fireEvent.click(historyRow(rows, "r-new"));
    const s = useRunStore.getState();
    expect(s.compare?.a).toMatchObject({ kind: "plate", nodeId: "edit-1" });
    expect(s.compare?.b).toMatchObject({ kind: "run", runId: "r-new", label: LABEL_2, outputHash: "h_r-new" });
    expect(s.scrubEligible).toBe(true);
    expect(usePanelsStore.getState().selectedRunId).toBe("r-new");
    expect(caption()).toHaveTextContent(`A plate · B ${LABEL_2}`);
    expect(screen.getByTestId("viewer-mode")).toHaveTextContent(`plate | ${LABEL_2}`);
    expect(historyRow(screen.getAllByTestId("history-run"), "r-new")).toHaveAttribute("data-side", "B");
  });

  it("shift-click sets A only", async () => {
    const rows = await showHistory();
    fireEvent.click(historyRow(rows, "r-new"));
    fireEvent.click(historyRow(screen.getAllByTestId("history-run"), "r-old"), { shiftKey: true });
    const s = useRunStore.getState();
    expect(s.compare?.a).toMatchObject({ kind: "run", runId: "r-old", label: LABEL_1 });
    expect(s.compare?.b).toMatchObject({ kind: "run", runId: "r-new" });
    expect(s.scrubEligible).toBe(false);
    expect(usePanelsStore.getState().selectedRunId).toBe("r-new"); // shift-click is not a selection
    expect(caption()).toHaveTextContent(`A ${LABEL_1} · B ${LABEL_2}`);
    const after = screen.getAllByTestId("history-run");
    expect(historyRow(after, "r-old")).toHaveAttribute("data-side", "A");
    expect(within(historyRow(after, "r-old")).getByTestId("run-side")).toHaveTextContent("A");
  });

  it("shift-click with nothing open shows the run alone on A", async () => {
    const rows = await showHistory();
    fireEvent.click(historyRow(rows, "r-old"), { shiftKey: true });
    expect(useRunStore.getState().compare).toMatchObject({ a: { runId: "r-old" }, b: null });
    expect(caption()).toHaveTextContent(`A ${LABEL_1} · set B to compare`);
    expect(screen.getByTestId("viewer")).not.toHaveAttribute("data-mode", "empty");
  });
});

// ------------------------------------------------------------------------------------------------ node preview

describe("node-select preview", () => {
  it("a selected node with no compare open shows its output alone, captioned `<label> (no comparison)`", () => {
    mockApi();
    openGraph();
    mountApp();
    expect(screen.queryByTestId("compare-caption")).toBeNull();
    expect(screen.getByTestId("viewer")).toHaveAttribute("data-mode", "empty");
    select("plate-1");
    expect(caption()).toHaveTextContent("Plate (no comparison)");
    expect(screen.getByTestId("viewer-size")).toHaveTextContent("640 × 360");
    expect(screen.getByTestId("viewer")).toHaveAttribute("data-mode", "a");
    expect(screen.queryByTestId("scrub-hint")).toBeNull();
    // a node with no output yet shows nothing
    select("edit-1");
    expect(screen.queryByTestId("compare-caption")).toBeNull();
    expect(screen.getByTestId("viewer")).toHaveAttribute("data-mode", "empty");
  });

  it("an open compare wins over the selection", () => {
    mockApi();
    openGraph();
    useRunStore.getState().startRuns("edit-1", [R1]);
    mountApp();
    fireEvent.click(within(rowOf("r1")).getByTitle("compare with the plate in the viewer"));
    select("plate-1");
    expect(caption()).toHaveTextContent("A plate · B candidate 1");
  });
});

// ------------------------------------------------------------------------------------------------ size chips

describe("W × H chips", () => {
  const png = (w: number, h: number, url = "/api/v0/assets/a") => ({ url, width: w, height: h, encoding: "png16" as const });

  it("renders one always-on chip per side, `640 × 360`, and keeps badge-size for a mismatch", () => {
    const { rerender } = render(<Viewer a={png(640, 360)} />);
    expect(screen.getByTestId("viewer-size")).toHaveTextContent("640 × 360");
    expect(screen.getByTestId("viewer-size")).toHaveAttribute("data-side", "a");
    rerender(<Viewer a={png(640, 360)} b={png(320, 180, "/api/v0/assets/b")} />);
    const chips = screen.getAllByTestId("viewer-size");
    expect(chips.map((c) => c.textContent)).toEqual(["A 640 × 360", "B 320 × 180"]);
    expect(screen.getByTestId("badge-size")).toBeInTheDocument();
  });

  it("an 8-bit source with no known size has no chip until it loads", () => {
    render(<Viewer a={{ url: "/x.png", width: 1, height: 1, encoding: "srgb8" }} />);
    expect(screen.queryByTestId("viewer-size")).toBeNull();
  });
});

// ------------------------------------------------------------------------------------------------ enlarge

describe("Enlarge and Fullscreen", () => {
  it("Enlarge hides the graph, puts the viewer and the strip in the canvas area's place, and Esc returns", () => {
    mockApi();
    openGraph();
    useRunStore.getState().startRuns("edit-1", [R1, R2]);
    mountApp();
    const shell = screen.getByTestId("plate-shell");
    expect(shell).not.toHaveClass("is-enlarged");
    fireEvent.click(within(rowOf("r1")).getByTitle("compare with the plate in the viewer"));
    fireEvent.click(screen.getByTestId("viewer-enlarge"));
    expect(shell).toHaveClass("is-enlarged");
    expect(useRunStore.getState().enlarged).toBe(true);
    const stage = screen.getByTestId("enlarged-stage");
    // the stage sits in the row that holds the canvas, not in the sidebar; xyflow stays mounted
    expect(stage.parentElement).toBe(document.querySelector(".plate-canvas")?.parentElement);
    expect(document.querySelector(".plate-canvas .react-flow")).not.toBeNull();
    expect(within(stage).getByTestId("viewer")).toBeInTheDocument();
    expect(within(stage).getByTestId("compare-caption")).toHaveTextContent("A plate · B candidate 1");
    expect(within(within(stage).getByTestId("enlarged-strip")).getAllByTestId("candidate")).toHaveLength(2);
    expect(screen.getAllByTestId("viewer")).toHaveLength(1); // moved, not copied
    // the sidebar keeps the panels and the editors
    expect(document.querySelector(".plate-side [data-testid='panels']")).not.toBeNull();
    expect(within(stage).getByTestId("viewer-enlarge")).toHaveTextContent("Restore");
    expect(document.activeElement).toBe(within(stage).getByTestId("viewer")); // keys go to the moved viewer
    fireEvent.keyDown(within(stage).getByTestId("viewer"), { key: "Escape" });
    expect(shell).not.toHaveClass("is-enlarged");
    expect(screen.queryByTestId("enlarged-stage")).toBeNull();
    expect(screen.getByTestId("viewer")).toBeInTheDocument();
  });

  it("`e` outside the viewer toggles enlarge; inside the viewer (edges) and in text boxes it does not", () => {
    mockApi();
    openGraph();
    useRunStore.getState().startRuns("edit-1", [R1]);
    mountApp();
    fireEvent.click(within(rowOf("r1")).getByTitle("compare with the plate in the viewer"));
    fireEvent.keyDown(screen.getByTestId("viewer"), { key: "e" });
    expect(useRunStore.getState().enlarged).toBe(false);
    fireEvent.keyDown(screen.getByTestId("candidate-strip"), { key: "e" }); // data-plate-keys keeps its keys
    expect(useRunStore.getState().enlarged).toBe(false);
    const input = document.createElement("input");
    document.body.appendChild(input);
    fireEvent.keyDown(input, { key: "e" });
    expect(useRunStore.getState().enlarged).toBe(false);
    input.remove();
    fireEvent.keyDown(document.body, { key: "e" });
    expect(useRunStore.getState().enlarged).toBe(true);
    expect(screen.getByTestId("plate-shell")).toHaveClass("is-enlarged");
    fireEvent.keyDown(document.body, { key: "e" });
    expect(useRunStore.getState().enlarged).toBe(false);
    fireEvent.keyDown(document.body, { key: "e", metaKey: true });
    expect(useRunStore.getState().enlarged).toBe(false);
  });

  it("Fullscreen asks the page for fullscreen when it can and enlarges; without the API nothing throws", () => {
    mockApi();
    openGraph();
    mountApp();
    select("plate-1");
    expect(toggleFullscreen()).toBe(false); // jsdom has no Fullscreen API
    fireEvent.click(screen.getByTestId("viewer-fullscreen"));
    expect(useRunStore.getState().enlarged).toBe(true);
    act(() => useRunStore.getState().setEnlarged(false));
    const request = vi.fn(async () => {});
    Object.defineProperty(document.documentElement, "requestFullscreen", { value: request, configurable: true });
    try {
      fireEvent.click(screen.getByTestId("viewer-fullscreen"));
      expect(request).toHaveBeenCalledTimes(1);
      fireEvent.keyDown(document.body, { key: "f" });
      expect(request).toHaveBeenCalledTimes(2);
    } finally {
      delete (document.documentElement as { requestFullscreen?: unknown }).requestFullscreen;
    }
  });
});

// ------------------------------------------------------------------------------------------------ Max dock chip

describe("Max dock chip", () => {
  const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

  it("is amber in the status bar when jobs wait and the last poll is older than 30 s", async () => {
    mockApi({ "GET /plugin/status": { last_poll_utc: ago(120_000), queued: 2, claimed: 0 } });
    mountApp();
    const chip = await within(screen.getByTestId("status-bar")).findByTestId("host-chip");
    expect(chip).toHaveAttribute("data-state", "amber");
    expect(chip).toHaveTextContent("Max dock · polled 2 min ago · 2 queued");
  });

  it("is green after a recent poll and grey when no dock ever polled", async () => {
    mockApi({ "GET /plugin/status": { last_poll_utc: ago(8_000), queued: 1, claimed: 0 } });
    const view = mountApp();
    const chip = await screen.findByTestId("host-chip");
    expect(chip).toHaveAttribute("data-state", "green");
    expect(chip).toHaveTextContent(/^Max dock · polled \d+ s ago · 1 queued$/);
    view.unmount();
    mockApi({ "GET /plugin/status": { last_poll_utc: null, queued: 0, claimed: 0 } });
    mountApp();
    const grey = await screen.findByTestId("host-chip");
    expect(grey).toHaveAttribute("data-state", "grey");
    expect(grey).toHaveTextContent("Max dock · never polled");
  });

  it("polls GET /plugin/status every 30 s", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const calls = mockApi({ "GET /plugin/status": { last_poll_utc: null, queued: 0, claimed: 0 } });
    mountApp();
    const polls = () => calls.filter((c) => c.path === "/plugin/status").length;
    expect(polls()).toBe(1);
    act(() => vi.advanceTimersByTime(HOST_POLL_MS - 1));
    expect(polls()).toBe(1);
    act(() => vi.advanceTimersByTime(1));
    expect(polls()).toBe(2);
  });

  it("reads the status and the age", () => {
    const now = Date.parse("2026-10-18T10:00:00Z");
    expect(parsePluginStatus({ last_poll_utc: "", queued: "x" })).toEqual({ last_poll_utc: null, queued: 0, claimed: 0 });
    expect(hostChip({ last_poll_utc: "2026-10-18T09:59:00Z", queued: 3, claimed: 0 }, now).state).toBe("amber");
    expect(hostChip({ last_poll_utc: "2026-10-18T09:59:00Z", queued: 0, claimed: 1 }, now).state).toBe("green");
    expect(hostChip({ last_poll_utc: "2026-10-18T09:59:40Z", queued: 3, claimed: 0 }, now).state).toBe("green");
    expect(hostChip({ last_poll_utc: null, queued: 3, claimed: 0 }, now)).toEqual({ state: "grey", text: "Max dock · never polled · 3 queued" });
    expect(hostChip(null, now)).toEqual({ state: "grey", text: "Max dock · status unavailable" });
    expect([formatAgo(2_000), formatAgo(12_000), formatAgo(125_000), formatAgo(7_300_000)]).toEqual([
      "just now",
      "12 s ago",
      "2 min ago",
      "2 h ago",
    ]);
  });
});

// ------------------------------------------------------------------------------------------------ Recomposite slot

describe("Recomposite slot", () => {
  function FakeBar({ runId, rawKey, nodeId }: RecompositeBarProps) {
    return (
      <div data-testid="fake-recomposite">
        {runId} {rawKey} {nodeId}
      </div>
    );
  }

  it("renders T99's bar under the viewer when the compared candidate has raw_key, and not otherwise", async () => {
    recompositeSlot.load = async () => FakeBar;
    mockApi();
    openGraph();
    useRunStore.getState().startRuns("edit-1", [run("r1", { normalized: { raw_key: "raw_1" } }), run("r2")]);
    mountApp();
    expect(screen.queryByTestId("fake-recomposite")).toBeNull();
    fireEvent.click(within(rowOf("r1")).getByTitle("compare with the plate in the viewer"));
    expect(await screen.findByTestId("fake-recomposite")).toHaveTextContent("r1 raw_1 edit-1");
    expect(screen.getByTestId("recomposite-slot")).toHaveAttribute("data-run", "r1");
    fireEvent.click(within(rowOf("r2")).getByTitle("compare with the plate in the viewer"));
    await waitFor(() => expect(screen.queryByTestId("fake-recomposite")).toBeNull());
    // it follows the viewer into the enlarged stage
    fireEvent.click(within(rowOf("r1")).getByTitle("compare with the plate in the viewer"));
    fireEvent.click(screen.getByTestId("viewer-enlarge"));
    expect(await within(screen.getByTestId("enlarged-stage")).findByTestId("fake-recomposite")).toBeInTheDocument();
  });

  it("a bar that throws renders nothing and leaves the viewer", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    recompositeSlot.load = async () =>
      function Broken(): never {
        throw new Error("boom");
      };
    mockApi();
    openGraph();
    useRunStore.getState().startRuns("edit-1", [run("r1", { raw_key: "raw_top" })]);
    mountApp();
    fireEvent.click(within(rowOf("r1")).getByTitle("compare with the plate in the viewer"));
    await waitFor(() => expect(screen.getByTestId("recomposite-slot")).toBeEmptyDOMElement());
    expect(screen.getByTestId("viewer")).toBeInTheDocument();
  });

  it("the shipped loader finds T99's RecompositeBar or nothing, never an error", async () => {
    const bar = await originalLoad();
    expect(bar === null || typeof bar === "function" || typeof bar === "object").toBe(true);
  });
});

// ------------------------------------------------------------------------------------------------ standalone slot

describe("CompareViewer on its own", () => {
  it("shows the scrub hint in place when no verdict panel is mounted", () => {
    mockApi();
    openGraph();
    useRunStore.getState().startRuns("edit-1", [R1, R2]);
    act(() => {
      useRunStore.getState().setCompareSide("a", { kind: "run", runId: "r1" });
      useRunStore.getState().setCompareSide("b", { kind: "run", runId: "r2" });
    });
    render(<CompareViewer />);
    expect(within(screen.getByTestId("compare")).getByTestId("scrub-hint")).toHaveTextContent(SCRUB_HINT);
  });
});
