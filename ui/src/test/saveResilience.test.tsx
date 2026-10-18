// T147: saving survives the daemon (decision 77). A failed PUT is retried 1, 2, 4, 8, then every 15 s, and at once
// when the daemon comes back; a 422 refusal and a 409 graph_conflict are not. The PUT carries the revision the graph
// was read at and stores the one the daemon answers; a 409 graph_conflict reads `conflict: reload`, and Reload takes
// the daemon's graph while the unsaved Edit prompts stay to copy. Another writer's graph.saved reloads the graph (or
// is a conflict over unsaved changes). Continuous typing saves every 2 s, pagehide flushes with a keepalive PUT,
// beforeunload warns while unsaved, the open project comes back after a reload, the token state is honest and a
// non-JSON error body never reaches the artist. fetch and EventSource are fakes; no daemon runs.

import { ReactFlowProvider } from "@xyflow/react";
import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App, { OPEN_PROJECT_KEY } from "../App";
import { ApiError, type EventSourceLike, fetchJson, nonJsonStatusLine, TOKEN_KEY, tokenFromPaste, tokenState } from "../api/client";
import { useAlertStore, useDaemonStatus } from "../api/useDaemonStatus";
import { followGraphSaves, hub, projectEventsUrl, RunEventHub, unwatchAll } from "../canvas/runEvents";
import { useRunStore } from "../canvas/runStore";
import type { BusRecord } from "../canvas/runTypes";
import { patchNodeParams } from "../editors/params";
import { errorText } from "../makebar/api";
import { useMakeStore } from "../makebar/makeStore";
import type { LoadableGraph } from "../nodeTypes";
import { getProjectGraph } from "../panels/api";
import { initialPanelsState, usePanelsStore } from "../panels/store";
import { StatusBar } from "../PlateCanvas";
import {
  createInitialGraph,
  guardUnload,
  installPageLifecycle,
  SAVE_DEBOUNCE_MS,
  SAVE_MAX_WAIT_MS,
  SAVE_RETRY_MS,
  UNLOAD_WARNING,
  useGraphStore,
} from "../store";

// ---------------------------------------------------------------------------- the daemon stub

interface Call {
  method: string;
  path: string;
  body: unknown;
  headers: Headers;
  keepalive: boolean;
}

/** A reply with a status; `text` sends the body as it is (an HTML error page), not as JSON. */
interface Reply {
  status: number;
  body?: unknown;
  text?: string;
  statusText?: string;
}

type Route = unknown | ((call: Call) => unknown | Reply | Promise<unknown | Reply>);

const reply = (body: unknown, status = 200): Reply => ({ status, body });
const isReply = (v: unknown): v is Reply =>
  typeof v === "object" && v !== null && "status" in v && Object.keys(v).every((k) => ["status", "body", "text", "statusText"].includes(k));

/** Plain-promise responses (no streams), so fake timers never stall a request. Unknown routes answer 404; a route
 * that throws is a network failure. `auth` (when given) answers 401 to any other token on every route but /health. */
function mockApi(routes: Record<string, Route>, auth?: string): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://127.0.0.1");
      const method = (init?.method ?? "GET").toUpperCase();
      const path = url.pathname.replace(/^\/api\/v0/, "");
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      const headers = new Headers(init?.headers);
      const call: Call = { method, path, body, headers, keepalive: init?.keepalive === true };
      calls.push(call);
      const route = routes[`${method} ${path}`];
      let out: unknown;
      if (auth !== undefined && path !== "/health" && headers.get("Authorization") !== `Bearer ${auth}`) {
        out = reply({ error: "unauthorized" }, 401);
      } else if (route === undefined) {
        out = reply({ error: `no stub for ${method} ${path}` }, 404);
      } else {
        out = typeof route === "function" ? await (route as (c: Call) => unknown)(call) : route;
      }
      const r = isReply(out) ? out : reply(out);
      const text = r.text ?? (r.body === undefined ? "" : JSON.stringify(r.body));
      return {
        ok: r.status >= 200 && r.status < 300,
        status: r.status,
        statusText: r.statusText ?? "",
        text: async () => text,
      } as unknown as Response;
    }),
  );
  return calls;
}

const puts = (calls: Call[]) => calls.filter((c) => c.method === "PUT" && c.path === "/projects/p/graph");
const gets = (calls: Call[]) => calls.filter((c) => c.method === "GET" && c.path === "/projects/p/graph");

/** The daemon's PUT answer: the graph as sent, at the next revision. */
function storing(start: number) {
  let revision = start;
  return (call: Call) => {
    const { revision: _sent, ...graph } = call.body as Record<string, unknown>;
    revision += 1;
    return { ok: true, project_id: "p", graph: { ...graph, revision } };
  };
}

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
  emit(rec: BusRecord, id?: string): void {
    this.onmessage?.({ data: JSON.stringify(rec), lastEventId: id ?? String(rec.seq ?? "") } as MessageEvent);
  }
}

const openStreams = () => FakeEventSource.all.filter((s) => !s.closed);

// ---------------------------------------------------------------------------- fixtures

/** Plate -> Edit with a prompt, as the daemon stores it at revision 2. */
const GRAPH: LoadableGraph & { revision: number } = {
  schema: 1,
  revision: 2,
  nodes: [
    { id: "plate-1", kind: "Plate", label: "Plate", packetId: "room_a", tier: "A", position: { x: 0, y: 0 } },
    { id: "edit-1", kind: "Edit", label: "Dusk", params: { prompt: "base" }, position: { x: 260, y: 0 } },
  ],
  edges: [{ id: "e0", source: "plate-1", target: "edit-1", sourceHandle: null, targetHandle: null }],
};

/** The same graph after "another tab" added a Review node and saved (revision 3). */
const THEIRS = {
  ...GRAPH,
  revision: 3,
  nodes: [...GRAPH.nodes, { id: "review-1", kind: "Review", label: "Review", position: { x: 520, y: 0 } }],
};

const graph = () => useGraphStore.getState();
const prompt = (id: string) => graph().nodes.find((n) => n.id === id)?.data.params?.prompt;

const mountApp = () =>
  render(
    <ReactFlowProvider>
      <App />
    </ReactFlowProvider>,
  );

const mountStatusBar = () =>
  render(
    <ReactFlowProvider>
      <StatusBar />
    </ReactFlowProvider>,
  );

const chip = () => screen.getByTestId("save-state");

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  useMakeStore.getState().reset();
  useRunStore.getState().reset();
  usePanelsStore.setState(initialPanelsState());
  useAlertStore.setState({ lastAlert: null });
  unwatchAll();
  FakeEventSource.all = [];
  hub.setFactory((url) => new FakeEventSource(url));
  window.sessionStorage.clear();
});

afterEach(() => {
  unwatchAll();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.sessionStorage.clear();
  useGraphStore.setState(createInitialGraph()); // drops any save or retry still scheduled
});

// ---------------------------------------------------------------------------- revision

describe("the graph revision (decision 77)", () => {
  it("the PUT sends the revision the graph was loaded at and the store takes the one the daemon answers", async () => {
    fakeTimers();
    const calls = mockApi({ "PUT /projects/p/graph": storing(2) });
    graph().loadProject(GRAPH, "p");
    expect(graph().revision).toBe(2);
    act(() => patchNodeParams("edit-1", { prompt: "dusk" }));
    advance(SAVE_DEBOUNCE_MS);
    await flush();
    expect(puts(calls)[0].body).toMatchObject({ revision: 2 });
    expect(graph().revision).toBe(3);
    act(() => patchNodeParams("edit-1", { prompt: "dusk, warm" }));
    advance(SAVE_DEBOUNCE_MS);
    await flush();
    expect(puts(calls)[1].body).toMatchObject({ revision: 3 });
    expect(graph().revision).toBe(4);
    expect(graph().saveState).toBe("saved");
  });

  it("the project panel's graph read keeps the revision, so the first save after opening is protected too", async () => {
    mockApi({ "GET /projects/p/graph": { ...GRAPH, revision: 5 } });
    const read = await getProjectGraph("p");
    expect(read).toMatchObject({ revision: 5 });
    graph().loadProject(read, "p");
    expect(graph().revision).toBe(5);
  });
});

// ---------------------------------------------------------------------------- retry and backoff

describe("a failed save is retried", () => {
  it("a failed PUT is retried after 1 s and the chip returns to saved", async () => {
    fakeTimers();
    let fail = true;
    const store = storing(2);
    const calls = mockApi({
      "PUT /projects/p/graph": (call: Call) => (fail ? reply({ error: "io", detail: "disk full" }, 500) : store(call)),
    });
    graph().loadProject(GRAPH, "p");
    mountStatusBar();
    act(() => patchNodeParams("edit-1", { prompt: "dusk" }));
    advance(SAVE_DEBOUNCE_MS);
    await flush();
    expect(puts(calls)).toHaveLength(1);
    expect(chip()).toHaveAttribute("data-state", "failed");
    expect(chip()).toHaveTextContent("save failed: disk full · retry in 1 s");

    fail = false;
    advance(SAVE_RETRY_MS[0] - 1);
    await flush();
    expect(puts(calls)).toHaveLength(1);
    advance(1);
    await flush();
    expect(puts(calls)).toHaveLength(2);
    expect(puts(calls)[1].body).toMatchObject({ revision: 2, nodes: expect.arrayContaining([expect.objectContaining({ params: { prompt: "dusk" } })]) });
    expect(chip()).toHaveAttribute("data-state", "saved");
    expect(chip().textContent).toMatch(/^saved \d\d:\d\d:\d\d$/);
    expect(graph().retryInMs).toBeNull();
  });

  it("backs off 1, 2, 4, 8, 15, 15 s while the daemon stays away (a network failure, 409 graph_busy, 429)", async () => {
    fakeTimers();
    const answers: (() => unknown)[] = [
      () => {
        throw new TypeError("Failed to fetch");
      },
      () => reply({ error: "graph_busy", detail: "another save of this project held it for 5 s; retry" }, 409),
      () => reply({ error: "too_many_saves", retry_after_ms: 500 }, 429),
      () => reply({ error: "internal" }, 503),
      () => {
        throw new TypeError("Failed to fetch");
      },
      () => {
        throw new TypeError("Failed to fetch");
      },
    ];
    let n = 0;
    const calls = mockApi({ "PUT /projects/p/graph": () => answers[Math.min(n++, answers.length - 1)]() });
    graph().loadProject(GRAPH, "p");
    await act(() => graph().saveNow());
    const waits: (number | null)[] = [graph().retryInMs];
    for (const wait of [1000, 2000, 4000, 8000, 15000]) {
      advance(wait - 1);
      await flush();
      const before = puts(calls).length;
      advance(1);
      await flush();
      expect(puts(calls).length).toBe(before + 1);
      waits.push(graph().retryInMs);
    }
    expect(waits).toEqual([1000, 2000, 4000, 8000, 15000, 15000]);
    expect(graph().saveState).toBe("failed");
  });

  it("a 422 rule refusal is not retried", async () => {
    fakeTimers();
    const calls = mockApi({
      "PUT /projects/p/graph": reply({ error: "graph_too_large", errors: ["graph is 5000000 bytes; the limit is 4 MiB"] }, 422),
    });
    graph().loadProject(GRAPH, "p");
    await act(() => graph().saveNow());
    expect(graph().saveState).toBe("failed");
    expect(graph().retryInMs).toBeNull();
    advance(60_000);
    await flush();
    expect(puts(calls)).toHaveLength(1);
  });

  it("stops retrying when another project is opened", async () => {
    fakeTimers();
    const calls = mockApi({ "PUT /projects/p/graph": reply({ error: "internal" }, 500) });
    graph().loadProject(GRAPH, "p");
    await act(() => graph().saveNow());
    expect(graph().retryInMs).toBe(1000);
    graph().loadProject({ ...GRAPH, revision: 0 } as LoadableGraph, "q");
    advance(60_000);
    await flush();
    expect(calls).toHaveLength(1);
  });

  it("the daemon coming back (down -> up) sends the pending save at once", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    let up = false;
    const calls = mockApi({
      "GET /health": () => {
        if (!up) throw new TypeError("Failed to fetch");
        return { ok: true, daemon_version: "0.1.0" };
      },
      "PUT /projects/p/graph": (call: Call) => {
        if (!up) throw new TypeError("Failed to fetch");
        return storing(2)(call);
      },
    });
    graph().loadProject(GRAPH, "p");
    await act(() => graph().saveNow());
    expect(graph().saveState).toBe("failed");
    const { result } = renderHook(() => useDaemonStatus(100));
    await flush();
    expect(result.current.state).toBe("down");
    up = true;
    advance(100); // the next health poll; the 1 s retry is still waiting
    await flush();
    expect(result.current.state).toBe("up");
    expect(puts(calls)).toHaveLength(2);
    expect(graph().saveState).toBe("saved");
  });
});

// ---------------------------------------------------------------------------- conflict

describe("a revision conflict", () => {
  it("a 409 graph_conflict shows conflict: reload; nothing more is saved; Reload fetches and the prompt is offered to copy", async () => {
    window.sessionStorage.setItem(TOKEN_KEY, "tok");
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    const calls = mockApi({
      "PUT /projects/p/graph": reply({ error: "graph_conflict", current: 3, sent: 2 }, 409),
      "GET /projects/p/graph": THEIRS,
    });
    graph().loadProject(GRAPH, "p");
    mountApp();
    act(() => patchNodeParams("edit-1", { prompt: "TAB A prompt" }));
    await act(() => graph().saveNow());
    expect(graph().saveState).toBe("conflict");
    expect(graph().conflict).toEqual({ current: 3, sent: 2 });
    expect(chip()).toHaveTextContent(/^conflict: reload$/);
    expect(chip()).toHaveAttribute("data-state", "conflict");
    expect(graph().saveError?.code).toBe("graph_conflict");

    act(() => patchNodeParams("edit-1", { prompt: "TAB A prompt, more" })); // still not saved over the other writer
    await act(() => graph().saveNow());
    expect(puts(calls)).toHaveLength(1);

    fireEvent.click(screen.getByTestId("save-reload"));
    await waitFor(() => expect(graph().saveState).toBe("saved"));
    expect(gets(calls)).toHaveLength(1);
    expect(graph().revision).toBe(3);
    expect(graph().nodes.map((n) => n.id)).toEqual(["plate-1", "edit-1", "review-1"]);
    expect(prompt("edit-1")).toBe("base");
    expect(screen.queryByTestId("save-reload")).toBeNull();

    // the Edit prompt the reload dropped is offered to copy, nothing else
    expect(graph().conflictLocal?.prompts).toEqual([{ nodeId: "edit-1", label: "Dusk", prompt: "TAB A prompt" }]);
    fireEvent.click(screen.getByTestId("copy-prompt-edit-1"));
    await waitFor(() => expect(screen.getByTestId("copy-prompt-edit-1")).toHaveTextContent("copied: Dusk"));
    expect(writeText).toHaveBeenCalledWith("TAB A prompt");
    fireEvent.click(screen.getByTestId("conflict-dismiss"));
    expect(screen.queryByTestId("conflict-bar")).toBeNull();
    expect(graph().conflictLocal).toBeNull();

    // the next edit saves on the daemon's revision
    act(() => patchNodeParams("edit-1", { prompt: "base, colder" }));
    await act(() => graph().saveNow());
    expect(puts(calls)[1].body).toMatchObject({ revision: 3 });
  });

  it("without a clipboard the prompt is shown to copy by hand", async () => {
    vi.stubGlobal("navigator", { ...navigator, clipboard: undefined });
    mockApi({ "PUT /projects/p/graph": reply({ error: "graph_conflict", current: 3, sent: 2 }, 409), "GET /projects/p/graph": THEIRS });
    graph().loadProject(GRAPH, "p");
    mountApp();
    act(() => patchNodeParams("edit-1", { prompt: "mine" }));
    await act(() => graph().saveNow());
    await act(() => graph().reloadGraph());
    fireEvent.click(screen.getByTestId("copy-prompt-edit-1"));
    expect(await screen.findByTestId("conflict-prompt-text-edit-1")).toHaveValue("mine");
  });
});

// ---------------------------------------------------------------------------- another writer's graph.saved

describe("another writer's graph.saved", () => {
  const saved = (revision: number, seq = 10): BusRecord => ({
    seq,
    project_id: "p",
    run_id: null,
    kind: "graph.saved",
    data: { kind: "graph.saved", nodes: 3, edges: 1, revision },
  });

  it("a graph.saved with a higher revision reloads the graph: the node another tab added appears", async () => {
    // the other tab also rewrote the prompt of the Edit this tab has open in its editor
    const theirs = {
      ...THEIRS,
      nodes: THEIRS.nodes.map((n) => (n.id === "edit-1" ? { ...n, params: { prompt: "the other tab's prompt" } } : n)),
    };
    const calls = mockApi({ "GET /projects/p/graph": theirs });
    graph().loadProject(GRAPH, "p");
    act(() => useGraphStore.setState((st) => ({ nodes: st.nodes.map((n) => ({ ...n, selected: n.id === "edit-1" })) })));
    mountApp();
    expect(screen.getByTestId("edit-prompt")).toHaveValue("base");
    const stream = openStreams().find((s) => s.url.includes("project_id=p"));
    expect(stream).toBeDefined();

    act(() => stream?.emit(saved(2, 9))); // this tab's own revision: nothing to read
    await flush();
    expect(gets(calls)).toHaveLength(0);

    act(() => stream?.emit(saved(3, 10)));
    await waitFor(() => expect(graph().revision).toBe(3));
    expect(gets(calls)).toHaveLength(1);
    expect(graph().nodes.map((n) => n.id)).toContain("review-1");
    await waitFor(() => expect(screen.getAllByTestId("node-item").map((li) => li.textContent)).toContain("Review"));
    // the open editor re-read the reloaded node, so typing on goes on from the other tab's text, not over it
    expect(graph().nodes.find((n) => n.id === "edit-1")?.selected).toBe(true);
    expect(screen.getByTestId("edit-prompt")).toHaveValue("the other tab's prompt");
    expect(graph().saveState).toBe("saved");
    expect(puts(calls)).toHaveLength(0); // a reload is not an edit
  });

  it("a graph.saved with a higher revision over unsaved changes is a conflict, not a silent reload", async () => {
    fakeTimers();
    const calls = mockApi({ "GET /projects/p/graph": THEIRS });
    graph().loadProject(GRAPH, "p");
    const h = new RunEventHub((url) => new FakeEventSource(url));
    const stop = followGraphSaves("p", h);
    act(() => patchNodeParams("edit-1", { prompt: "mine" })); // unsaved: the debounce has not fired
    act(() => openStreams()[0].emit(saved(3)));
    await flush();
    expect(gets(calls)).toHaveLength(1);
    expect(graph().saveState).toBe("conflict");
    expect(graph().conflict).toEqual({ current: 3, sent: 2 });
    expect(prompt("edit-1")).toBe("mine"); // kept on the canvas until the artist reloads
    advance(SAVE_DEBOUNCE_MS + SAVE_MAX_WAIT_MS);
    await flush();
    expect(puts(calls)).toHaveLength(0);
    stop();
  });

  it("a graph.saved that arrives while this tab's PUT is out is checked after the answer (its own echo reads nothing)", async () => {
    let release: (() => void) | null = null;
    const store = storing(2);
    const calls = mockApi({
      "PUT /projects/p/graph": (call: Call) =>
        new Promise((resolve) => {
          release = () => resolve(store(call));
        }),
      "GET /projects/p/graph": THEIRS,
    });
    graph().loadProject(GRAPH, "p");
    const h = new RunEventHub((url) => new FakeEventSource(url));
    const stop = followGraphSaves("p", h);
    act(() => patchNodeParams("edit-1", { prompt: "mine" }));
    let out!: Promise<void>;
    act(() => {
      out = graph().saveNow();
    });
    await flush();
    act(() => openStreams()[0].emit(saved(3))); // the echo of this very PUT, before its answer
    await flush();
    expect(gets(calls)).toHaveLength(0);
    act(() => release?.());
    await act(() => out);
    await flush();
    expect(graph().revision).toBe(3);
    expect(gets(calls)).toHaveLength(0);
    expect(graph().saveState).toBe("saved");
    stop();
  });
});

// ---------------------------------------------------------------------------- debounce ceiling and page lifecycle

describe("debounce ceiling and leaving the page", () => {
  it("typing for 5 s sends at least two PUTs (the 400 ms debounce alone would send none until the end)", async () => {
    fakeTimers();
    const calls = mockApi({ "PUT /projects/p/graph": storing(2) });
    graph().loadProject(GRAPH, "p");
    let text = "";
    for (let t = 0; t < 5000; t += 100) {
      text += "x";
      act(() => patchNodeParams("edit-1", { prompt: text }));
      advance(100);
      await flush(5);
    }
    expect(puts(calls).length).toBeGreaterThanOrEqual(2);
    const first = puts(calls)[0].body as { nodes: { id: string; params?: { prompt?: string } }[] };
    expect(first.nodes.find((n) => n.id === "edit-1")?.params?.prompt?.length).toBe(SAVE_MAX_WAIT_MS / 100);
  });

  it("pagehide sends the pending save as a keepalive PUT at once", async () => {
    fakeTimers();
    const calls = mockApi({ "PUT /projects/p/graph": storing(2) });
    graph().loadProject(GRAPH, "p");
    const uninstall = installPageLifecycle(window);
    window.dispatchEvent(new Event("pagehide"));
    await flush();
    expect(calls).toHaveLength(0); // nothing pending: nothing sent

    act(() => patchNodeParams("edit-1", { prompt: "typed 150 ms before the reload" }));
    window.dispatchEvent(new Event("pagehide"));
    await flush();
    expect(puts(calls)).toHaveLength(1);
    expect(puts(calls)[0].keepalive).toBe(true);
    expect(puts(calls)[0].body).toMatchObject({ revision: 2 });
    expect(graph().saveState).toBe("saved");
    advance(SAVE_MAX_WAIT_MS);
    await flush();
    expect(puts(calls)).toHaveLength(1); // the debounce it replaced is gone
    uninstall();
  });

  it("a page going hidden flushes too", async () => {
    fakeTimers();
    const calls = mockApi({ "PUT /projects/p/graph": storing(2) });
    graph().loadProject(GRAPH, "p");
    const uninstall = installPageLifecycle(window);
    act(() => graph().addNode("Gate", { x: 600, y: 0 }));
    const had = Object.getOwnPropertyDescriptor(document, "visibilityState");
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    try {
      document.dispatchEvent(new Event("visibilitychange"));
      await flush();
    } finally {
      if (had) Object.defineProperty(document, "visibilityState", had);
      else delete (document as unknown as Record<string, unknown>).visibilityState;
    }
    expect(puts(calls)).toHaveLength(1);
    expect(puts(calls)[0].keepalive).toBe(true);
    uninstall();
  });

  it("beforeunload sets returnValue while unsaved, and not once the daemon has the graph", async () => {
    fakeTimers();
    mockApi({ "PUT /projects/p/graph": storing(2) });
    const unload = () => {
      const ev = new Event("beforeunload", { cancelable: true });
      Object.defineProperty(ev, "returnValue", { value: "", writable: true }); // BeforeUnloadEvent's string field
      window.dispatchEvent(ev);
      return ev;
    };
    const uninstall = installPageLifecycle(window);
    graph().loadProject(GRAPH, "p");
    expect(unload().returnValue).toBe("");

    act(() => patchNodeParams("edit-1", { prompt: "unsaved" }));
    const ev = unload();
    expect(ev.returnValue).toBe(UNLOAD_WARNING);
    expect(ev.defaultPrevented).toBe(true);

    advance(SAVE_DEBOUNCE_MS);
    await flush();
    expect(graph().saveState).toBe("saved");
    expect(unload().returnValue).toBe("");
    uninstall();

    // the handler alone, as the browser calls it
    act(() => patchNodeParams("edit-1", { prompt: "again" }));
    const direct = { preventDefault: vi.fn(), returnValue: "" } as unknown as Event;
    guardUnload(direct);
    expect((direct as unknown as { returnValue: string }).returnValue).toBe(UNLOAD_WARNING);
  });
});

// ---------------------------------------------------------------------------- reopen, token, non-JSON errors

describe("reopen after a reload", () => {
  it("the open project id is kept in sessionStorage and reopened (with its revision) after a reload", async () => {
    window.sessionStorage.setItem(TOKEN_KEY, "tok");
    const calls = mockApi({ "GET /projects/p/graph": { ...GRAPH, revision: 7 } });
    graph().loadProject(GRAPH, "p");
    const first = mountApp();
    await waitFor(() => expect(window.sessionStorage.getItem(OPEN_PROJECT_KEY)).toBe("p"));
    first.unmount();

    useGraphStore.setState(createInitialGraph()); // the reload: a fresh store, the same tab's sessionStorage
    mountApp();
    await waitFor(() => expect(graph().projectId).toBe("p"));
    expect(gets(calls)).toHaveLength(1);
    expect(graph().revision).toBe(7);
    expect(graph().nodes.map((n) => n.id)).toEqual(["plate-1", "edit-1"]);
    expect(graph().saveState).toBe("saved");
  });

  it("a remembered project the daemon no longer has is forgotten", async () => {
    window.sessionStorage.setItem(TOKEN_KEY, "tok");
    window.sessionStorage.setItem(OPEN_PROJECT_KEY, "p");
    const calls = mockApi({ "GET /projects/p/graph": reply({ error: "not_found", project_id: "p" }, 404) });
    mountApp();
    await waitFor(() => expect(gets(calls)).toHaveLength(1));
    await waitFor(() => expect(window.sessionStorage.getItem(OPEN_PROJECT_KEY)).toBeNull());
    expect(graph().projectId).toBeNull();
  });
});

describe("the token state", () => {
  it("a 401 shows token: rejected; pasting the daemon's line stores the token and the project reopens", async () => {
    window.sessionStorage.setItem(TOKEN_KEY, "tok-stale");
    window.sessionStorage.setItem(OPEN_PROJECT_KEY, "p");
    const calls = mockApi({ "GET /health": { ok: true }, "GET /projects/p/graph": GRAPH, "GET /projects": [] }, "tok-good");
    mountApp();
    await waitFor(() => expect(screen.getByTestId("status-token")).toHaveTextContent("token: rejected"));
    expect(screen.getByTestId("status-token")).toHaveAttribute("data-state", "rejected");
    expect(graph().projectId).toBeNull();

    fireEvent.change(screen.getByTestId("token-input"), {
      target: { value: "PLATE daemon http://127.0.0.1:4610/#token=tok-good" },
    });
    fireEvent.click(screen.getByTestId("token-submit"));
    expect(window.sessionStorage.getItem(TOKEN_KEY)).toBe("tok-good");
    await waitFor(() => expect(screen.getByTestId("status-token")).toHaveTextContent("token: present"));
    await waitFor(() => expect(graph().projectId).toBe("p"));
    expect(screen.queryByTestId("token-entry")).toBeNull();
    const reads = gets(calls).map((c) => c.headers.get("Authorization"));
    expect(reads).toEqual(["Bearer tok-stale", "Bearer tok-good"]);
  });

  it("a tokenless tab says token: missing and offers the paste field; nonsense is refused", async () => {
    mockApi({ "GET /health": { ok: true } });
    mountApp();
    expect(screen.getByTestId("status-token")).toHaveTextContent("token: missing");
    expect(screen.getByTestId("token-input")).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("token-input"), { target: { value: "two words" } });
    fireEvent.click(screen.getByTestId("token-submit"));
    expect(screen.getByTestId("token-problem")).toHaveTextContent("no token in that text");
    expect(window.sessionStorage.getItem(TOKEN_KEY)).toBeNull();
  });

  it("tokenState: rejected only for the token that got the 401, cleared by a good authenticated call", async () => {
    window.sessionStorage.setItem(TOKEN_KEY, "tok-a");
    let good = false;
    mockApi({ "GET /runs/r1": () => (good ? { id: "r1" } : reply({ error: "unauthorized" }, 401)), "GET /health": { ok: true } });
    expect(tokenState()).toBe("present");
    await fetchJson("/runs/r1").catch(() => undefined);
    expect(tokenState()).toBe("rejected");
    await fetchJson("/health"); // health takes no token: its 200 proves nothing
    expect(tokenState()).toBe("rejected");
    good = true;
    await fetchJson("/runs/r1");
    expect(tokenState()).toBe("present");
    window.sessionStorage.removeItem(TOKEN_KEY);
    expect(tokenState()).toBe("none");
  });

  it("tokenFromPaste reads the start line, a URL, a fragment or the bare token", () => {
    expect(tokenFromPaste("PLATE daemon http://127.0.0.1:4610/#token=abc")).toBe("abc");
    expect(tokenFromPaste("  #token=a%2Bb  ")).toBe("a+b");
    expect(tokenFromPaste("token=xyz")).toBe("xyz");
    expect(tokenFromPaste("raw-token_123")).toBe("raw-token_123");
    expect(tokenFromPaste("")).toBeNull();
    expect(tokenFromPaste("#nothing=here")).toBeNull();
  });
});

describe("a non-JSON error body", () => {
  const HTML = "<html><body><h1>Bad Gateway</h1><script>x</script></body></html>";

  it("an HTML error body renders the status line only (the save chip and the Make bar's errorText)", async () => {
    fakeTimers();
    mockApi({ "PUT /projects/p/graph": { status: 502, statusText: "Bad Gateway", text: HTML } });
    graph().loadProject(GRAPH, "p");
    mountStatusBar();
    await act(() => graph().saveNow());
    expect(chip()).toHaveTextContent("save failed: 502 Bad Gateway (non-JSON response)");
    expect(document.body.innerHTML).not.toContain("<h1>");
    expect(document.body.textContent).not.toContain("Bad Gateway</h1>");

    const err = await fetchJson("/projects/p/graph", { method: "PUT", body: "{}" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).body).toBeNull();
    expect(errorText(err)).toBe("502 Bad Gateway (non-JSON response)");
    expect(nonJsonStatusLine(504, "")).toBe("504 (non-JSON response)");
  });

  it("a JSON error body still reaches errorText, and a 2xx text body is still returned as text", async () => {
    mockApi({
      "POST /projects/p/nodes/edit-1/quote": reply({ error: "quote_expired", detail: "the quote is 6 minutes old" }, 409),
      "GET /docs/manual": { status: 200, text: "# Artist manual" },
    });
    const err = await fetchJson("/projects/p/nodes/edit-1/quote", { method: "POST", body: "{}" }).catch((e: unknown) => e);
    expect(errorText(err)).toBe("409 quote_expired: the quote is 6 minutes old");
    expect(await fetchJson("/docs/manual")).toBe("# Artist manual");
  });
});

// ---------------------------------------------------------------------------- the SSE cursor

describe("the event cursor is the raw SSE id", () => {
  const rec = (seq: number): BusRecord => ({ seq, run_id: "r1", kind: "run.state", data: { run_id: "r1", to: "RUNNING" } });

  it("an <epoch>.<seq> id goes back as after= untouched; a lower seq of the same epoch never lowers it; a new epoch replaces it", () => {
    const h = new RunEventHub((url) => new FakeEventSource(url));
    const stop = h.watch("r1", "p1", () => {});
    const es = FakeEventSource.all[0];
    es.emit(rec(7), "01JEPOCHA.7");
    es.emit(rec(5), "01JEPOCHA.5");
    expect(h.cursor("p1")).toBe("01JEPOCHA.7");
    es.emit(rec(2), "01JEPOCHB.2"); // a reindex restarted seq under a new epoch
    expect(h.cursor("p1")).toBe("01JEPOCHB.2");
    stop();
    h.watch("r2", "p1", () => {});
    expect(FakeEventSource.all[1].url).toBe("/api/v0/events?project_id=p1&after=01JEPOCHB.2");
    expect(FakeEventSource.all[1].url).toBe(projectEventsUrl("p1", null, "01JEPOCHB.2"));
  });
});
