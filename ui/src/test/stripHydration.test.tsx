// T160 strip hydration (decision 93; stress finding strip-not-hydrated-after-reload): when a project opens, the
// newest Make of every generating node comes back into its candidate strip from `GET /projects/{id}/runs`, each run
// read in full with `GET /runs/{id}` (a list row carries no outputs or gate checks), and the runs still in flight are
// watched. A strip holding a Make of this session is never replaced, and a node whose read fails does not stop the
// others. fetch and EventSource are the fakes of liveLoop.test.tsx; no daemon runs.

import { ReactFlowProvider } from "@xyflow/react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import type { EventSourceLike } from "../api/client";
import { clearThumbCache } from "../canvas/assets";
import { CandidateStrip } from "../canvas/CandidateStrip";
import { hub, projectEventsUrl, unwatchAll, watchRun } from "../canvas/runEvents";
import { MAX_CANDIDATES, useRunStore } from "../canvas/runStore";
import type { BusRecord, QuoteWire, RunWire } from "../canvas/runTypes";
import { useMakeStore } from "../makebar/makeStore";
import type { LoadableGraph } from "../nodeTypes";
import { Panels } from "../panels";
import { initialPanelsState, newestMakes, usePanelsStore } from "../panels/store";
import type { RunRow } from "../panels/types";
import { createInitialGraph, graphFromWire, useGraphStore } from "../store";

// ------------------------------------------------------------------------------------------------ fakes

class FakeEventSource implements EventSourceLike {
  static all: FakeEventSource[] = [];
  url: string;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  closed = false;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.all.push(this);
  }
  addEventListener(): void {}
  close(): void {
    this.closed = true;
  }
  emit(rec: BusRecord): void {
    this.onmessage?.({ data: JSON.stringify(rec), lastEventId: String(rec.seq ?? "") } as MessageEvent);
  }
}

const open = () => FakeEventSource.all.filter((s) => !s.closed);
const factory = (url: string): EventSourceLike => new FakeEventSource(url);

interface Call {
  method: string;
  path: string;
  body: unknown;
}

type Route = unknown | ((call: Call) => unknown | Promise<unknown>);

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
      const out = typeof route === "function" ? await (route as (c: Call) => unknown)(call) : route;
      return out instanceof Response ? out : json(out);
    }),
  );
  return calls;
}

const gets = (calls: Call[], path: string): Call[] => calls.filter((c) => c.method === "GET" && c.path === path);
const runReads = (calls: Call[]): string[] =>
  calls.filter((c) => c.method === "GET" && /^\/runs\/[^/]+$/.test(c.path)).map((c) => c.path.slice("/runs/".length));

const stateRec = (seq: number, runId: string, to: string): BusRecord => ({
  seq,
  run_id: runId,
  kind: "run.state",
  data: { run_id: runId, to },
});

// ------------------------------------------------------------------------------------------------ fixtures

const GRAPH: LoadableGraph = {
  schema: 1,
  nodes: [
    {
      kind: "Plate",
      id: "plate-1",
      label: "Plate",
      packetId: "room_a",
      tier: "A",
      outputHash: "h_plate",
      outputSize: { width: 64, height: 36 },
      position: { x: 0, y: 0 },
    },
    { kind: "Edit", id: "edit-1", label: "Edit", position: { x: 300, y: 0 } },
    { kind: "Edit", id: "edit-2", label: "Edit 2", position: { x: 300, y: 320 } },
    { kind: "Comp", id: "comp-1", label: "Comp", position: { x: 620, y: 0 } },
  ],
  edges: [
    { id: "plate-1->edit-1", source: "plate-1", target: "edit-1", sourceHandle: null, targetHandle: null },
    { id: "plate-1->edit-2", source: "plate-1", target: "edit-2", sourceHandle: null, targetHandle: null },
    { id: "edit-1->comp-1", source: "edit-1", target: "comp-1", sourceHandle: null, targetHandle: null },
  ],
};

/** A `GET /projects/{id}/runs` row as the panels type it: no outputs, no gate checks. */
const row = (id: string, created: string, over: Record<string, unknown> = {}): Record<string, unknown> => ({
  id,
  project_id: "p1",
  node_id: "edit-1",
  state: "DONE",
  route: { engine_id: "nano-banana-2.1", provider: "google" },
  created_utc: created,
  ...over,
});

/** The quote a best-of-N sibling shares with its group (`models.Quote` JSON). */
const quoteOf = (group: string, index: number, siblings: number): QuoteWire => ({
  id: `q_${group}_${index}`,
  provider: "google",
  model: "nano-banana-2.1",
  custody_grade: "C2",
  price_usd: 0.04,
  eta_s: 30,
  created_utc: "2026-10-19T09:59:00Z",
  expires_utc: "2026-10-19T10:14:00Z",
  sibling_group: group,
  sibling_index: index,
  siblings,
});

const grouped = (group: string, index: number, siblings: number): Pick<RunWire, "sibling_group" | "sibling_index" | "quote"> => ({
  sibling_group: group,
  sibling_index: index,
  quote: quoteOf(group, index, siblings),
});

/** `GET /runs/{id}`: the run with its outputs and gate report. */
const detail = (id: string, over: Partial<RunWire> & Record<string, unknown> = {}): RunWire => ({
  id,
  project_id: "p1",
  node_id: "edit-1",
  state: "DONE",
  outputs: [`h_${id}`],
  gate_report: { verdict: "pass", checks: [{ metric_id: "registration.residual_px", value: 0.4, status: "pass" }] },
  ...over,
});

const FAIL_REPORT = {
  verdict: "fail" as const,
  checks: [
    { metric_id: "registration.residual_px", value: 0.42, status: "pass" as const },
    { metric_id: "invention_flags", value: 2, status: "fail" as const },
  ],
};

const openProject = (id = "p1") => useGraphStore.getState().loadProject(GRAPH, id);

const nodeCard = (id: string): HTMLElement => {
  const el = document.querySelector<HTMLElement>(`[data-node-id="${id}"]`);
  if (!el) throw new Error(`no node ${id} on the canvas`);
  return el;
};

const stripRuns = (container: HTMLElement): string[] =>
  within(container)
    .queryAllByTestId("candidate")
    .map((c) => c.dataset.run ?? "");

const mountApp = () =>
  render(
    <ReactFlowProvider>
      <App />
    </ReactFlowProvider>,
  );

/** Panels (whose project effect calls `loadRuns`) and the two Edit nodes' strips, without App's project stream. */
const mountPanelsAndStrips = () =>
  render(
    <>
      <Panels />
      <div data-testid="strip-edit-1">
        <CandidateStrip nodeId="edit-1" />
      </div>
      <div data-testid="strip-edit-2">
        <CandidateStrip nodeId="edit-2" />
      </div>
    </>,
  );

/** Wait until `loadRuns` for the open project has finished, hydration included. */
async function runsLoaded(projectId = "p1"): Promise<void> {
  await waitFor(() => {
    const s = usePanelsStore.getState();
    expect(s.runsProjectId).toBe(projectId);
    expect(s.runsLoading).toBe(false);
  });
}

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  useRunStore.getState().reset();
  useMakeStore.getState().reset();
  usePanelsStore.setState(initialPanelsState());
  unwatchAll();
  FakeEventSource.all = [];
  hub.setFactory(factory);
  clearThumbCache();
});

afterEach(() => {
  unwatchAll();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.sessionStorage.clear();
  window.localStorage.clear();
});

// ------------------------------------------------------------------------------------------------ on project open

describe("strips hydrate from run history on project open", () => {
  it("a GATE_FAIL take newer than a DONE take in another group: one candidate with its failing check, compare and pick work", async () => {
    const calls = mockApi({
      "GET /projects/p1/runs": [
        row("r-fail", "2026-10-19T10:00:00Z", { state: "GATE_FAIL", ...grouped("g2", 0, 1), gate_report: { verdict: "fail" } }),
        row("r-done", "2026-10-19T09:00:00Z", { state: "DONE", ...grouped("g1", 0, 1), gate_report: { verdict: "pass" } }),
      ],
      "GET /runs/r-fail": detail("r-fail", { state: "GATE_FAIL", outputs: ["h_fail"], gate_report: FAIL_REPORT, ...grouped("g2", 0, 1) }),
      "GET /runs/r-done": detail("r-done", grouped("g1", 0, 1)),
    });
    openProject();
    mountApp();

    const card = nodeCard("edit-1");
    await waitFor(() => expect(stripRuns(card)).toEqual(["r-fail"]));
    const cand = within(card).getByTestId("candidate");
    expect(cand).toHaveAttribute("data-state", "GATE_FAIL");
    expect(within(cand).getByTestId("candidate-fail")).toHaveTextContent("invention_flags 2");
    expect(runReads(calls)).toEqual(["r-fail"]); // the older Make is history only, never read into the strip
    const entry = useRunStore.getState().byNode["edit-1"];
    expect(entry).toMatchObject({ runIds: ["r-fail"], group: "g2", ranked: null, picked: null });
    expect(useRunStore.getState().candidates["r-fail"].outputHash).toBe("h_fail");

    // compare: the candidate against its plate, with its output (not "candidate 1 has no output yet")
    fireEvent.click(within(cand).getByTitle("compare with the plate in the viewer"));
    const caption = screen.getByTestId("compare-caption");
    expect(caption).toHaveTextContent("A plate · B candidate 1");
    expect(caption).not.toHaveTextContent("no output yet");
    expect(usePanelsStore.getState().selectedRunId).toBe("r-fail");

    // pick: key 1 in the strip makes the take the node's output, so the card no longer says "no output yet"
    fireEvent.keyDown(within(card).getByTestId("candidate-strip"), { key: "1" });
    expect(useRunStore.getState().byNode["edit-1"].picked).toBe("r-fail");
    const node = useGraphStore.getState().nodes.find((n) => n.id === "edit-1");
    expect(node?.data.outputHash).toBe("h_fail");
    expect(node?.data.gateStatus).toBe("fail");
    await waitFor(() => expect(within(card).queryByText("no output yet")).toBeNull());
  });

  it("a best-of-3 group fills the strip with its three candidates; an ungrouped newest run comes back alone", async () => {
    const calls = mockApi({
      "GET /projects/p1/runs": [
        // edit-2 lists its older run first: the newest by created_utc wins, not the list position
        row("e2-old", "2026-10-19T08:00:00Z", { node_id: "edit-2" }),
        row("r3c", "2026-10-19T10:00:02Z", grouped("g3", 2, 3)),
        row("r3b", "2026-10-19T10:00:01Z", grouped("g3", 1, 3)),
        row("r3a", "2026-10-19T10:00:00Z", grouped("g3", 0, 3)),
        row("r-old", "2026-10-19T07:00:00Z"),
        row("e2-new", "2026-10-19T09:00:00Z", { node_id: "edit-2" }),
        row("c-1", "2026-10-19T11:00:00Z", { node_id: "comp-1" }), // a Comp makes no candidates
        row("x-1", "2026-10-19T11:00:00Z", { node_id: "edit-9" }), // a node no longer on the canvas
      ],
      "GET /runs/r3a": detail("r3a", { ...grouped("g3", 0, 3), gate_score: 0.71 }),
      "GET /runs/r3b": detail("r3b", { ...grouped("g3", 1, 3), gate_score: 0.93 }),
      "GET /runs/r3c": detail("r3c", { ...grouped("g3", 2, 3), gate_score: 0.82 }),
      "GET /runs/e2-new": detail("e2-new", { node_id: "edit-2" }),
    });
    openProject();
    mountApp();

    const card = nodeCard("edit-1");
    // all three are terminal, so the strip ranks by the daemon's rank key (gate score, high to low)
    await waitFor(() => expect(stripRuns(card)).toEqual(["r3b", "r3c", "r3a"]));
    await waitFor(() => expect(stripRuns(nodeCard("edit-2"))).toEqual(["e2-new"]));
    expect(within(card).getByTestId("candidate-strip")).toHaveAttribute("aria-label", "candidates: keys 1 to 3 pick");
    expect(useRunStore.getState().byNode["edit-1"]).toMatchObject({ group: "g3", ranked: null, picked: null });
    expect(runReads(calls).sort()).toEqual(["e2-new", "r3a", "r3b", "r3c"]);
    expect(useRunStore.getState().byNode["comp-1"]).toBeUndefined();
    expect(useRunStore.getState().byNode["edit-9"]).toBeUndefined();
  });

  it("a RUNNING take is shown and watched: an EventSource opens for it and its next state moves the chip", async () => {
    let reads = 0;
    const calls = mockApi({
      "GET /projects/p1/runs": [row("r-live", "2026-10-19T10:00:00Z", { state: "RUNNING" })],
      "GET /runs/r-live": () => {
        reads += 1;
        return reads === 1 ? detail("r-live", { state: "RUNNING", outputs: [] }) : detail("r-live", { state: "DONE" });
      },
    });
    openProject();
    expect(open()).toHaveLength(0);
    mountPanelsAndStrips();

    const strip = screen.getByTestId("strip-edit-1");
    await waitFor(() => expect(stripRuns(strip)).toEqual(["r-live"]));
    expect(within(strip).getByTestId("candidate-state")).toHaveTextContent("RUNNING");
    expect(within(strip).getByTestId("candidate-cancel")).toBeInTheDocument();
    expect(open().map((s) => s.url)).toEqual([projectEventsUrl("p1", null)]);
    expect(hub.watching()).toEqual(["r-live"]);

    act(() => open()[0].emit(stateRec(1, "r-live", "DONE")));
    expect(within(strip).getByTestId("candidate-state")).toHaveTextContent("DONE");
    await waitFor(() => expect(gets(calls, "/runs/r-live")).toHaveLength(2)); // the settle read
    await waitFor(() => expect(useRunStore.getState().candidates["r-live"].outputHash).toBe("h_r-live"));
    expect(open()).toHaveLength(0); // terminal: unwatched, and nothing else held the stream
  });

  it("a node with a run in flight from this session is not replaced; the other nodes still hydrate", async () => {
    const calls = mockApi({
      "GET /projects/p1/runs": [
        row("h-1", "2026-10-19T09:00:00Z"),
        row("e2", "2026-10-19T09:30:00Z", { node_id: "edit-2" }),
      ],
      "GET /runs/h-1": detail("h-1"),
      "GET /runs/e2": detail("e2", { node_id: "edit-2" }),
    });
    openProject();
    // this session's Make, still running when the runs are read
    useRunStore.getState().startRuns("edit-1", [{ id: "s-1", node_id: "edit-1", state: "RUNNING" }]);
    watchRun("s-1", "edit-1", "p1");
    mountPanelsAndStrips();

    await waitFor(() => expect(stripRuns(screen.getByTestId("strip-edit-2"))).toEqual(["e2"]));
    await runsLoaded();
    const strip = screen.getByTestId("strip-edit-1");
    expect(stripRuns(strip)).toEqual(["s-1"]);
    expect(within(strip).getByTestId("candidate-state")).toHaveTextContent("RUNNING");
    expect(runReads(calls)).toEqual(["e2"]); // the in-flight node's history is not even read
    expect(useRunStore.getState().candidates["h-1"]).toBeUndefined();
  });

  it("a project with no runs leaves every strip empty and reads nothing more", async () => {
    const calls = mockApi({ "GET /projects/p1/runs": [] });
    openProject();
    mountPanelsAndStrips();
    await runsLoaded();
    expect(screen.queryAllByTestId("candidate-strip")).toHaveLength(0);
    expect(useRunStore.getState().byNode).toEqual({});
    expect(runReads(calls)).toEqual([]);
    expect(open()).toHaveLength(0);
  });

  it("a node whose read fails does not stop the others, and the pass logs once", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    mockApi({
      "GET /projects/p1/runs": [
        row("r1", "2026-10-19T10:00:00Z", grouped("g1", 0, 2)),
        row("r2", "2026-10-19T10:00:00Z", grouped("g1", 1, 2)),
        row("e2", "2026-10-19T09:00:00Z", { node_id: "edit-2" }),
      ],
      "GET /runs/r1": detail("r1", grouped("g1", 0, 2)),
      "GET /runs/r2": json({ error: "index_busy", detail: "the index is rebuilding" }, 503),
      "GET /runs/e2": detail("e2", { node_id: "edit-2" }),
    });
    openProject();
    mountPanelsAndStrips();

    await waitFor(() => expect(stripRuns(screen.getByTestId("strip-edit-2"))).toEqual(["e2"]));
    await runsLoaded();
    expect(stripRuns(screen.getByTestId("strip-edit-1"))).toEqual([]); // never half a best-of-2
    expect(warn).toHaveBeenCalledTimes(1);
    expect(String(warn.mock.calls[0][0])).toMatch(/1 node\(s\) not restored.*edit-1: the index is rebuilding/);
    expect(usePanelsStore.getState().runs.map((r) => r.id)).toEqual(["r1", "r2", "e2"]); // History is unaffected
    expect(usePanelsStore.getState().runsError).toBeNull();
  });

  it("a project left while its runs are read is not hydrated onto the next one", async () => {
    let release: (v: unknown) => void = () => {};
    const held = new Promise((resolve) => {
      release = resolve;
    });
    mockApi({
      "GET /projects/p1/runs": [row("r1", "2026-10-19T10:00:00Z")],
      "GET /runs/r1": async () => {
        await held;
        return detail("r1");
      },
      "GET /projects/p2/runs": [],
    });
    openProject("p1");
    mountPanelsAndStrips();
    await waitFor(() => expect(usePanelsStore.getState().runs.map((r) => r.id)).toEqual(["r1"]));
    act(() => openProject("p2"));
    await runsLoaded("p2");
    await act(async () => {
      release(null);
      await held;
    });
    expect(useRunStore.getState().byNode["edit-1"]).toBeUndefined();
    expect(stripRuns(screen.getByTestId("strip-edit-1"))).toEqual([]);
  });

  it("the History selection flow is unchanged: selecting a run does not touch the strips", async () => {
    mockApi({
      "GET /projects/p1/runs": [row("r1", "2026-10-19T10:00:00Z")],
      "GET /runs/r1": detail("r1"),
    });
    openProject();
    mountPanelsAndStrips();
    await waitFor(() => expect(stripRuns(screen.getByTestId("strip-edit-1"))).toEqual(["r1"]));
    act(() => usePanelsStore.getState().selectRun("r1"));
    expect(usePanelsStore.getState().selectedRunId).toBe("r1");
    expect(useRunStore.getState().byNode["edit-1"]).toMatchObject({ runIds: ["r1"], picked: null });
  });
});

// ------------------------------------------------------------------------------------------------ the store

describe("runStore.hydrate", () => {
  const cand = (id: string, state: string, over: Partial<RunWire> = {}): RunWire => ({ id, node_id: "edit-1", state, ...over });

  it("fills an empty strip like startRuns, ranks and picks empty", () => {
    useRunStore.getState().hydrate("edit-1", [cand("a", "DONE", { outputs: ["h_a"], ...grouped("g", 0, 2) }), cand("b", "GATE_FAIL", grouped("g", 1, 2))]);
    const s = useRunStore.getState();
    expect(s.byNode["edit-1"]).toEqual({ runIds: ["a", "b"], group: "g", ranked: null, picked: null });
    expect(s.candidates["a"]).toMatchObject({ nodeId: "edit-1", state: "DONE", outputHash: "h_a", index: 0 });
    expect(s.candidates["b"]).toMatchObject({ state: "GATE_FAIL", index: 1 });
  });

  it("leaves a strip with a non-terminal run alone, and replaces it once every run is terminal", () => {
    const store = useRunStore.getState();
    store.startRuns("edit-1", [cand("live", "SUBMITTED")]);
    store.hydrate("edit-1", [cand("old", "DONE")]);
    expect(useRunStore.getState().byNode["edit-1"].runIds).toEqual(["live"]);
    expect(useRunStore.getState().candidates["old"]).toBeUndefined();

    store.setRunState("live", "FAILED");
    store.hydrate("edit-1", [cand("newer", "DONE")]);
    expect(useRunStore.getState().byNode["edit-1"].runIds).toEqual(["newer"]);
    expect(useRunStore.getState().candidates["live"]).toBeUndefined();
  });

  it("an empty list changes nothing", () => {
    useRunStore.getState().hydrate("edit-1", []);
    expect(useRunStore.getState().byNode).toEqual({});
    useRunStore.getState().startRuns("edit-1", [cand("a", "DONE")]);
    useRunStore.getState().hydrate("edit-1", []);
    expect(useRunStore.getState().byNode["edit-1"].runIds).toEqual(["a"]);
  });

  it("the same runs read again keep the strip's ranking and pick", () => {
    useGraphStore.getState().loadProject(GRAPH, "p1");
    const store = useRunStore.getState();
    store.hydrate("edit-1", [cand("a", "DONE", grouped("g", 0, 2)), cand("b", "DONE", grouped("g", 1, 2))]);
    store.setRanking("g", ["b", "a"]);
    store.pick("edit-1", "b");
    store.hydrate("edit-1", [cand("b", "DONE", { ...grouped("g", 1, 2), outputs: ["h_b"] }), cand("a", "DONE", grouped("g", 0, 2))]);
    const s = useRunStore.getState();
    expect(s.byNode["edit-1"]).toMatchObject({ ranked: ["b", "a"], picked: "b", group: "g" });
    expect(s.candidates["b"].outputHash).toBe("h_b");
  });
});

describe("newestMakes", () => {
  const nodes = graphFromWire(GRAPH).nodes;
  const rows = (list: Record<string, unknown>[]) => list as unknown as RunRow[];

  it("caps a group at MAX_CANDIDATES and keeps only generating nodes on the canvas", () => {
    const group = Array.from({ length: MAX_CANDIDATES + 1 }, (_, i) =>
      row(`g${i}`, `2026-10-19T10:00:${String(10 - i).padStart(2, "0")}Z`, grouped("big", i, MAX_CANDIDATES + 1)),
    );
    const makes = newestMakes(rows([...group, row("p", "2026-10-19T12:00:00Z", { node_id: "plate-1" })]), nodes);
    expect([...makes.keys()]).toEqual(["edit-1"]);
    expect(makes.get("edit-1")?.map((r) => r.id)).toEqual(group.slice(0, MAX_CANDIDATES).map((r) => r.id));
  });

  it("reads the group from quote.sibling_group, else the run's own field; no group means the newest run alone", () => {
    const makes = newestMakes(
      rows([
        row("q1", "2026-10-19T10:00:00Z", { quote: { sibling_group: "gq" } }),
        row("q2", "2026-10-19T10:00:00Z", { sibling_group: "gq" }),
        row("solo-new", "2026-10-19T10:00:00Z", { node_id: "edit-2" }),
        row("solo-old", "2026-10-19T09:00:00Z", { node_id: "edit-2" }),
      ]),
      nodes,
    );
    expect(makes.get("edit-1")?.map((r) => r.id)).toEqual(["q1", "q2"]);
    expect(makes.get("edit-2")?.map((r) => r.id)).toEqual(["solo-new"]);
  });
});
