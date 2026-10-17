// T71 live loop: the event hub's per-project cursor (a Make never replays the project's history), the `onKind`
// routing of alert / packet / plugin records, the last alert in the status bar, the Cancel button on a live
// candidate, and the timer picking up a run the daemon already has. EventSource and fetch are fakes; no daemon runs.

import { ReactFlowProvider } from "@xyflow/react";
import { act, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { type EventSourceLike, TOKEN_KEY } from "../api/client";
import { alertFromRecord, alertLevel, useAlertStore, useDaemonStatus } from "../api/useDaemonStatus";
import { CandidateStrip, isCancellable } from "../canvas/CandidateStrip";
import { hub, projectEventsUrl, RunEventHub, unwatchAll, watchRun } from "../canvas/runEvents";
import { useRunStore } from "../canvas/runStore";
import { type BusRecord, RUN_STATES, type RunWire } from "../canvas/runTypes";
import { getTimerToday } from "../panels/api";
import { TimerPanel } from "../panels/TimerPanel";
import { createInitialGraph, useGraphStore } from "../store";

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
  headers: Headers;
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
      const call: Call = { method, path, body, headers: new Headers(init?.headers) };
      calls.push(call);
      const route = routes[`${method} ${path}`];
      if (route === undefined) return json({ error: `no stub for ${method} ${path}` }, 404);
      const out = typeof route === "function" ? (route as (c: Call) => unknown)(call) : route;
      return out instanceof Response ? out : json(out);
    }),
  );
  return calls;
}

const posts = (calls: Call[], path: string): Call[] => calls.filter((c) => c.method === "POST" && c.path === path);

const stateRec = (seq: number, runId: string, to: string): BusRecord => ({
  seq,
  run_id: runId,
  kind: "run.state",
  data: { run_id: runId, to },
});

const alertRec = (seq: number, level: string, message: string): BusRecord => ({
  seq,
  run_id: null,
  at: "2026-10-18T09:30:00Z",
  kind: "alert",
  data: { kind: "alert", source: "budgets", level, message },
});

const run = (id: string, over: Partial<RunWire> = {}): RunWire => ({ id, state: "QUOTED", node_id: "edit-1", ...over });

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  useRunStore.getState().reset();
  useAlertStore.setState({ lastAlert: null });
  unwatchAll();
  FakeEventSource.all = [];
  hub.setFactory(factory);
});

afterEach(() => {
  unwatchAll();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});

// ------------------------------------------------------------------------------------------------ the cursor

describe("event cursor", () => {
  it("the first open of a project has no cursor, the second carries after=<last seq>", () => {
    window.sessionStorage.setItem(TOKEN_KEY, "tok");
    const h = new RunEventHub(factory);
    const stop1 = h.watch("r1", "p1", () => {});
    expect(FakeEventSource.all[0].url).toBe("/api/v0/events?project_id=p1&token=tok");
    expect(h.cursor("p1")).toBeNull();

    FakeEventSource.all[0].emit(stateRec(3, "r1", "RUNNING"));
    FakeEventSource.all[0].emit(stateRec(7, "r1", "DONE"));
    FakeEventSource.all[0].emit(stateRec(5, "r1", "DONE")); // a late, lower seq never lowers the cursor
    expect(h.cursor("p1")).toBe(7);

    stop1();
    expect(open()).toHaveLength(0);
    h.watch("r2", "p1", () => {});
    expect(FakeEventSource.all[1].url).toBe("/api/v0/events?project_id=p1&token=tok&after=7");
    expect(FakeEventSource.all[1].url).toBe(projectEventsUrl("p1", "tok", 7));
  });

  it("cursors are per project, and closeAll forgets them", () => {
    const h = new RunEventHub(factory);
    h.watch("r1", "p1", () => {});
    FakeEventSource.all[0].emit(stateRec(12, "r1", "RUNNING"));
    h.watch("r9", "p2", () => {});
    expect(FakeEventSource.all[1].url).toBe(projectEventsUrl("p2", null));
    expect(h.cursor("p2")).toBeNull();
    h.closeAll();
    h.watch("r1", "p1", () => {});
    expect(FakeEventSource.all[2].url).toBe(projectEventsUrl("p1", null));
  });

  it("a second Make on the global hub reopens with the cursor, so history is not replayed", async () => {
    mockApi(); // the settle read after a terminal state finds no route and is ignored
    useRunStore.getState().startRuns("edit-1", [run("r1", { state: "RUNNING" })]);
    watchRun("r1", "edit-1", "p1");
    expect(open().map((s) => s.url)).toEqual([projectEventsUrl("p1", null)]);
    act(() => open()[0].emit(stateRec(4, "r1", "DONE")));
    expect(open()).toHaveLength(0); // terminal: unwatched, so the stream closed

    useRunStore.getState().startRuns("edit-1", [run("r2", { state: "QUEUED" })]);
    watchRun("r2", "edit-1", "p1");
    expect(open().map((s) => s.url)).toEqual(["/api/v0/events?project_id=p1&after=4"]);
  });

  it("an unparseable message does not move the cursor", () => {
    const h = new RunEventHub(factory);
    h.watch("r1", "p1", () => {});
    FakeEventSource.all[0].onmessage?.({ data: "not json" } as MessageEvent);
    FakeEventSource.all[0].onmessage?.({ data: JSON.stringify({ seq: 9 }) } as MessageEvent); // no kind: not a record
    expect(h.cursor("p1")).toBeNull();
  });
});

// ------------------------------------------------------------------------------------------------ onKind and follow

describe("hub.onKind and hub.follow", () => {
  it("routes records by kind to every handler of that kind and stops after unsubscribe", () => {
    const h = new RunEventHub(factory);
    const ingested = vi.fn();
    const plugin = vi.fn();
    const stopIngested = h.onKind("packet.ingested", ingested);
    h.onKind("plugin.job", plugin);
    h.watch("r1", "p1", () => {});
    const es = FakeEventSource.all[0];

    const rec: BusRecord = { seq: 1, run_id: null, kind: "packet.ingested", data: { packet_id: "room_a" } };
    es.emit(rec);
    es.emit({ seq: 2, run_id: null, kind: "plugin.job", data: { job_id: "j1", state: "claimed" } });
    es.emit(stateRec(3, "r1", "RUNNING")); // a run.state is not a kind anyone asked for
    expect(ingested).toHaveBeenCalledTimes(1);
    expect(ingested).toHaveBeenCalledWith(rec);
    expect(plugin).toHaveBeenCalledTimes(1);

    stopIngested();
    es.emit({ seq: 4, run_id: null, kind: "packet.ingested", data: {} });
    expect(ingested).toHaveBeenCalledTimes(1);
  });

  it("delivers run.accepted, graph.saved and packet.superseded; run-scoped records still reach their run's watcher", () => {
    const h = new RunEventHub(factory);
    const seen: string[] = [];
    for (const kind of ["run.accepted", "graph.saved", "packet.superseded"]) h.onKind(kind, (r) => seen.push(r.kind));
    const runHandler = vi.fn();
    h.watch("r1", "p1", runHandler);
    const es = FakeEventSource.all[0];
    es.emit({ seq: 1, run_id: "r1", kind: "run.accepted", data: { run_id: "r1", by: "human" } });
    es.emit({ seq: 2, run_id: "r2", kind: "run.accepted", data: { run_id: "r2", by: "human" } });
    es.emit({ seq: 3, run_id: null, kind: "graph.saved", data: {} });
    es.emit({ seq: 4, run_id: null, kind: "packet.superseded", data: {} });
    expect(seen).toEqual(["run.accepted", "run.accepted", "graph.saved", "packet.superseded"]);
    expect(runHandler.mock.calls.map((c) => c[0].seq)).toEqual([1, 3, 4]); // r1's own record, then the run-less ones
  });

  it("a handler that throws does not stop the others", () => {
    const quiet = vi.spyOn(console, "error").mockImplementation(() => {});
    const h = new RunEventHub(factory);
    const after = vi.fn();
    h.onKind("alert", () => {
      throw new Error("boom");
    });
    h.onKind("alert", after);
    h.follow("p1");
    FakeEventSource.all[0].emit(alertRec(1, "warning", "x"));
    expect(after).toHaveBeenCalledTimes(1);
    expect(quiet).toHaveBeenCalled();
  });

  it("follow keeps the project stream open with no run, shares it with watched runs, and closes on release", () => {
    const h = new RunEventHub(factory);
    const stopFollow = h.follow("p1");
    expect(open()).toHaveLength(1);
    expect(h.watching()).toEqual([]);

    const stopWatch = h.watch("r1", "p1", () => {});
    expect(open()).toHaveLength(1); // the same stream
    stopWatch();
    expect(open()).toHaveLength(1); // still followed
    stopFollow();
    stopFollow(); // releasing twice is harmless
    expect(open()).toHaveLength(0);
  });

  it("closeAll drops streams but keeps onKind handlers registered", () => {
    const h = new RunEventHub(factory);
    const handler = vi.fn();
    h.onKind("alert", handler);
    h.follow("p1");
    h.closeAll();
    expect(open()).toHaveLength(0);
    h.follow("p1");
    FakeEventSource.all[1].emit(alertRec(1, "error", "again"));
    expect(handler).toHaveBeenCalledTimes(1);
  });
});

// ------------------------------------------------------------------------------------------------ alerts

describe("alerts", () => {
  it("maps the daemon's level spellings and ignores an alert with no message", () => {
    expect(alertLevel("warning")).toBe("warning");
    expect(alertLevel("WARN")).toBe("warning");
    expect(alertLevel("error")).toBe("error");
    expect(alertLevel("critical")).toBe("error");
    expect(alertLevel("info")).toBe("info");
    expect(alertLevel(undefined)).toBe("info");
    expect(alertFromRecord(alertRec(1, "warning", "  unattended spend  "))).toEqual({
      level: "warning",
      message: "unattended spend",
      at: "2026-10-18T09:30:00Z",
    });
    expect(alertFromRecord({ kind: "alert", data: { level: "error" } })).toBeNull();
  });

  it("an alert record reaches lastAlert, and dismissing clears it", async () => {
    mockApi({ "GET /health": { ok: true, daemon_version: "0.1.0" } });
    const { result } = renderHook(() => useDaemonStatus());
    await waitFor(() => expect(result.current.state).toBe("up"));
    expect(result.current.lastAlert).toBeNull();

    const stop = hub.follow("p1");
    act(() => FakeEventSource.all[0].emit(alertRec(1, "warning", "unattended spend over the cap")));
    expect(result.current.lastAlert).toEqual({
      level: "warning",
      message: "unattended spend over the cap",
      at: "2026-10-18T09:30:00Z",
    });
    act(() => result.current.dismissAlert());
    expect(result.current.lastAlert).toBeNull();
    stop();
  });

  it("the status bar shows the last alert, amber for a warning and red for an error, with a dismiss", async () => {
    mockApi({ "GET /health": { ok: true, daemon_version: "0.1.0" } });
    useGraphStore.getState().loadProject({ schema: 1, nodes: [{ kind: "Plate", id: "plate-1", label: "Plate" }], edges: [] }, "p1");
    render(
      <ReactFlowProvider>
        <App />
      </ReactFlowProvider>,
    );
    // App follows the open project, so the stream is up with no run watched
    expect(open().map((s) => s.url)).toEqual([projectEventsUrl("p1", null)]);
    const bar = screen.getByTestId("status-bar");
    expect(within(bar).queryByTestId("status-alert")).toBeNull();

    act(() => open()[0].emit(alertRec(1, "warning", "unattended spend: 22.00 USD today")));
    const warning = within(bar).getByTestId("status-alert");
    expect(warning).toHaveAttribute("data-level", "warning");
    expect(warning).toHaveStyle({ color: "var(--amber, #ff9f0a)" });
    expect(within(warning).getByTestId("status-alert-text")).toHaveTextContent("unattended spend: 22.00 USD today");

    act(() => open()[0].emit(alertRec(2, "error", "nightly reconcile failed")));
    const error = within(bar).getByTestId("status-alert");
    expect(error).toHaveAttribute("data-level", "error");
    expect(error).toHaveStyle({ color: "var(--red, #ff453a)" });
    expect(error).toHaveTextContent("nightly reconcile failed");

    fireEvent.click(within(bar).getByTestId("status-alert-dismiss"));
    expect(within(bar).queryByTestId("status-alert")).toBeNull();
  });

  it("App holds no stream without a project, and drops it when the project goes away", () => {
    mockApi({ "GET /health": { ok: true } });
    const view = render(
      <ReactFlowProvider>
        <App />
      </ReactFlowProvider>,
    );
    expect(open()).toHaveLength(0);
    act(() => useGraphStore.getState().loadProject({ schema: 1, nodes: [], edges: [] }, "p1"));
    expect(open()).toHaveLength(1);
    view.unmount();
    expect(open()).toHaveLength(0);
  });
});

// ------------------------------------------------------------------------------------------------ cancel

describe("Cancel on a live candidate", () => {
  it("is offered from QUEUED to FETCHING and nowhere else", () => {
    const live = ["QUEUED", "STAGING", "SUBMITTED", "RUNNING", "FETCHING"];
    for (const state of RUN_STATES) expect(isCancellable(state)).toBe(live.includes(state));
    mockApi();
    for (const state of RUN_STATES) {
      useRunStore.getState().reset();
      useRunStore.getState().startRuns("edit-1", [run("r1", { state })]);
      const view = render(<CandidateStrip nodeId="edit-1" />);
      expect(screen.queryByTestId("candidate-cancel") !== null).toBe(live.includes(state));
      view.unmount();
    }
  });

  it("posts /runs/r1/cancel with a reason and keeps the chip until the daemon's event arrives", async () => {
    const calls = mockApi({ "POST /runs/r1/cancel": run("r1", { state: "CANCELLED" }) });
    useRunStore.getState().startRuns("edit-1", [run("r1", { state: "RUNNING" })]);
    watchRun("r1", "edit-1", "p1");
    render(<CandidateStrip nodeId="edit-1" />);
    fireEvent.click(screen.getByTestId("candidate-cancel"));
    await waitFor(() => expect(posts(calls, "/runs/r1/cancel")).toHaveLength(1));
    expect(posts(calls, "/runs/r1/cancel")[0].body).toEqual({ reason: "cancelled from the candidate strip" });
    expect(posts(calls, "/runs/r1/cancel")[0].headers.get("Content-Type")).toBe("application/json");
    await waitFor(() => expect(screen.getByTestId("candidate-cancel")).toBeEnabled());
    expect(screen.getByTestId("candidate-state")).toHaveTextContent("RUNNING");

    act(() => open()[0].emit(stateRec(1, "r1", "CANCELLED")));
    expect(screen.getByTestId("candidate-state")).toHaveTextContent("CANCELLED");
    expect(screen.queryByTestId("candidate-cancel")).toBeNull();
  });

  it("shows the daemon's refusal and lets the artist try again", async () => {
    mockApi({ "POST /runs/r1/cancel": json({ error: "illegal_transition", detail: "r1 is already DONE" }, 409) });
    useRunStore.getState().startRuns("edit-1", [run("r1", { state: "FETCHING" })]);
    render(<CandidateStrip nodeId="edit-1" />);
    fireEvent.click(screen.getByTestId("candidate-cancel"));
    expect(await screen.findByTestId("candidate-cancel-error")).toHaveTextContent("r1 is already DONE");
    expect(screen.getByTestId("candidate-cancel")).toBeEnabled();
  });
});

// ------------------------------------------------------------------------------------------------ timer

describe("timer running state", () => {
  it("GET /timer/today with running renders Running on mount", async () => {
    const startedAt = new Date(Date.now() - 95_000).toISOString();
    mockApi({
      "GET /timer/today": {
        day: "2026-10-18",
        rows: [{ deliverable_id: "hero-still", minutes: 12.5 }],
        totals: { minutes: 12.5 },
        running: { deliverable_id: "hero-still", started_at: startedAt, project_id: null },
      },
    });
    render(<TimerPanel />);
    await waitFor(() => expect(screen.getByTestId("timer-state")).toHaveTextContent(/Running 01:3\d/));
    expect(screen.getByLabelText("Deliverable id")).toHaveValue("hero-still");
    expect(screen.getByLabelText("Deliverable id")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Stop" })).toBeEnabled();
    expect(screen.getByText("12.5 min")).toBeInTheDocument();
  });

  it("stopping a timer the panel adopted posts that deliverable's stop and re-reads today", async () => {
    let running: unknown = { deliverable_id: "walk-02", since: Date.now() - 5000 };
    const calls = mockApi({
      "GET /timer/today": () => ({ rows: [], running }),
      "POST /timer": () => {
        running = null;
        return { ok: true };
      },
    });
    render(<TimerPanel />);
    await waitFor(() => expect(screen.getByTestId("timer-state")).toHaveTextContent(/Running/));
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(screen.getByTestId("timer-state")).toHaveTextContent("Stopped"));
    expect(posts(calls, "/timer")[0].body).toEqual({ deliverable_id: "walk-02", action: "stop" });
    await waitFor(() => expect(calls.filter((c) => c.path === "/timer/today")).toHaveLength(2));
    expect(screen.getByTestId("timer-state")).toHaveTextContent("Stopped");
  });

  it("a 409 timer_running names the deliverable to stop first, and its Stop button stops that one", async () => {
    let stopped = false;
    const calls = mockApi({
      "GET /timer/today": { rows: [] },
      "POST /timer": (c: Call) => {
        const body = c.body as { deliverable_id: string; action: string };
        if (body.action === "start") {
          return json(
            {
              error: "timer_running",
              detail: "another deliverable is running: hero-still; stop it first",
              running: { deliverable_id: "hero-still", started_at: "2026-10-18T09:00:00Z" },
            },
            409,
          );
        }
        stopped = body.deliverable_id === "hero-still";
        return { ok: true };
      },
    });
    render(<TimerPanel />);
    fireEvent.change(screen.getByLabelText("Deliverable id"), { target: { value: "walk-02" } });
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    const conflict = await screen.findByTestId("timer-conflict");
    expect(conflict).toHaveTextContent("stop hero-still first");
    expect(screen.getByTestId("timer-state")).toHaveTextContent("Stopped");

    fireEvent.click(screen.getByTestId("timer-conflict-stop"));
    await waitFor(() => expect(screen.queryByTestId("timer-conflict")).toBeNull());
    expect(stopped).toBe(true);
    expect(posts(calls, "/timer").map((c) => c.body)).toEqual([
      { deliverable_id: "walk-02", action: "start" },
      { deliverable_id: "hero-still", action: "stop" },
    ]);
  });

  it("a Stop the daemon answers with timer_not_running ends the panel's timer anyway", async () => {
    mockApi({
      "GET /timer/today": { rows: [], running: { deliverable_id: "walk-02", since: Date.now() - 1000 } },
      "POST /timer": json({ error: "timer_not_running", detail: "no timer is running" }, 409),
    });
    render(<TimerPanel />);
    await waitFor(() => expect(screen.getByTestId("timer-state")).toHaveTextContent(/Running/));
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("no timer is running");
    expect(screen.getByTestId("timer-state")).toHaveTextContent("Stopped");
  });

  it("getTimerToday reads running from started_at, from since, and returns null for the older shapes", async () => {
    mockApi({ "GET /timer/today": { rows: [], running: { deliverable_id: "a", started_at: "2026-10-18T09:00:00Z" } } });
    expect(await getTimerToday()).toEqual({ rows: [], running: { deliverable_id: "a", since: Date.UTC(2026, 9, 18, 9, 0, 0) } });
    mockApi({ "GET /timer/today": { rows: [], running: { deliverable_id: "b", since: 1234567 } } });
    expect((await getTimerToday()).running).toEqual({ deliverable_id: "b", since: 1234567 });
    mockApi({ "GET /timer/today": { rows: [], running: null } });
    expect((await getTimerToday()).running).toBeNull();
    mockApi({ "GET /timer/today": [{ deliverable_id: "x", minutes: 3 }] });
    expect(await getTimerToday()).toEqual({ rows: [{ deliverable_id: "x", minutes: 3 }], running: null });
    mockApi({ "GET /timer/today": { "hero-still": 3 } });
    expect(await getTimerToday()).toEqual({ rows: [{ deliverable_id: "hero-still", minutes: 3 }], running: null });
  });
});
