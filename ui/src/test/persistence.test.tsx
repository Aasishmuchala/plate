// T68: the graph is saved by the UI and its status is the daemon's (decision 31). Autosave 400 ms after a structural
// change (one PUT per burst, a move is not a change), a save before every quote, the daemon's status fields written
// back without another save, rule refusals drawn on the edge, human approvals (decision 37) and the refusal dialog,
// accept after a logged full scrub (doctrine 7), cancel. The daemon is a fetch stub that answers by "METHOD /path"
// (the /api/v0 prefix and the query string stripped) and records every call in order, as panels.test.tsx does.

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type EventSourceLike, fetchJson, saveGraph } from "../api/client";
import { deriveNodeInfo } from "../canvas/derive";
import { cancelRun } from "../canvas/runApi";
import { hub, unwatchAll } from "../canvas/runEvents";
import { useRunStore } from "../canvas/runStore";
import type { QuoteWire } from "../canvas/runTypes";
import { patchNodeParams } from "../editors/params";
import { MakeBar } from "../makebar/MakeBar";
import { quoteRequest, useMakeStore } from "../makebar/makeStore";
import { QUOTE_TTL_MS } from "../makebar/quote";
import type { LoadableGraph } from "../nodeTypes";
import { initialPanelsState, usePanelsStore } from "../panels/store";
import { VerdictPanel } from "../panels/VerdictPanel";
import { createInitialGraph, SAVE_DEBOUNCE_MS, useGraphStore } from "../store";

// ---------------------------------------------------------------------------- the daemon stub

interface Call {
  method: string;
  path: string;
  body: unknown;
  headers: Headers;
}

interface Reply {
  status: number;
  body: unknown;
}

type Route = unknown | ((call: Call) => unknown | Reply | Promise<unknown | Reply>);

const reply = (body: unknown, status = 200): Reply => ({ status, body });
const isReply = (v: unknown): v is Reply =>
  typeof v === "object" && v !== null && "status" in v && "body" in v && Object.keys(v).length === 2;

/** Plain-promise responses (no streams), so fake timers never stall a request. Unknown routes answer 404. */
function mockApi(routes: Record<string, Route>): Call[] {
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
      const out =
        route === undefined
          ? reply({ error: `no stub for ${method} ${path}` }, 404)
          : typeof route === "function"
            ? await (route as (c: Call) => unknown)(call)
            : route;
      const { status, body: payload } = isReply(out) ? out : reply(out);
      const text = payload === undefined ? "" : JSON.stringify(payload);
      return { ok: status >= 200 && status < 300, status, text: async () => text } as unknown as Response;
    }),
  );
  return calls;
}

const echoGraph = (call: Call) => ({ ok: true, project_id: "p", graph: call.body });
const puts = (calls: Call[]) => calls.filter((c) => c.method === "PUT" && c.path === "/projects/p/graph");
const order = (calls: Call[]) => calls.map((c) => `${c.method} ${c.path}`);

/** Let promise chains (fetch -> text -> JSON -> store) settle. */
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

class FakeEventSource implements EventSourceLike {
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  addEventListener(): void {}
  close(): void {}
}

// ---------------------------------------------------------------------------- fixtures

/** Plate -> Edit -> Motion; the Edit passed its gates and is approved, so the canvas allows the Motion edge. */
const GRAPH: LoadableGraph = {
  schema: 1,
  nodes: [
    { id: "plate-1", kind: "Plate", label: "Plate", packetId: "room_a", tier: "A", position: { x: 0, y: 0 } },
    { id: "edit-1", kind: "Edit", label: "Edit", gateStatus: "pass", approved: true, approvedBy: "human", scrubLogId: "s0", position: { x: 260, y: 0 } },
    { id: "motion-1", kind: "Motion", label: "Motion", position: { x: 520, y: 0 } },
  ],
  edges: [
    { id: "e0", source: "plate-1", target: "edit-1", sourceHandle: null, targetHandle: null },
    { id: "e1", source: "edit-1", target: "motion-1", sourceHandle: null, targetHandle: null },
  ],
};

/** Just a linked Plate. */
const PLATE_ONLY: LoadableGraph = { schema: 1, nodes: [GRAPH.nodes[0]], edges: [] };

const graph = () => useGraphStore.getState();
const nodeData = (id: string) => {
  const n = graph().nodes.find((x) => x.id === id);
  if (!n) throw new Error(`no node ${id}`);
  return n.data;
};
const wireNode = (call: Call, id: string) =>
  (call.body as { nodes: { id: string }[] }).nodes.find((n) => n.id === id) as Record<string, unknown> | undefined;

function quote(over: Partial<QuoteWire> = {}): QuoteWire {
  const now = Date.now();
  return {
    id: "qt_1",
    run_id: "run_1",
    provider: "atlas",
    model: "nano-banana-2.1",
    custody_grade: "D",
    price_usd: 0.034,
    eta_s: 40,
    created_utc: new Date(now).toISOString(),
    expires_utc: new Date(now + QUOTE_TTL_MS).toISOString(),
    ...over,
  };
}

const quoteAnswer = (price = 0.034) => {
  const q = quote({ price_usd: price });
  return { run: { id: "run_1", state: "QUOTED", node_id: "edit-1", quote: q }, quote: q, auto_cap_usd: 2 };
};

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  useMakeStore.getState().reset();
  useRunStore.getState().reset();
  usePanelsStore.setState(initialPanelsState());
  unwatchAll();
  hub.setFactory(() => new FakeEventSource());
  window.sessionStorage.clear();
});

afterEach(() => {
  unwatchAll();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const fakeTimers = () => vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });

// ---------------------------------------------------------------------------- autosave

describe("autosave", () => {
  it("adding a node, then 450 ms later, issues one PUT of the graph with X-Plate-Client: ui", async () => {
    fakeTimers();
    window.sessionStorage.setItem("plate.token", "tok");
    const calls = mockApi({ "PUT /projects/p/graph": echoGraph });
    graph().loadProject(PLATE_ONLY, "p");
    expect(calls).toHaveLength(0); // opening a project is not an edit
    act(() => graph().addNode("Edit", { x: 260, y: 0 }));
    advance(SAVE_DEBOUNCE_MS - 1);
    expect(puts(calls)).toHaveLength(0);
    advance(450 - (SAVE_DEBOUNCE_MS - 1));
    await flush();
    expect(order(calls)).toEqual(["PUT /projects/p/graph"]);
    expect(calls[0].headers.get("X-Plate-Client")).toBe("ui");
    expect(calls[0].headers.get("Authorization")).toBe("Bearer tok");
    expect((calls[0].body as { nodes: { id: string }[] }).nodes.map((n) => n.id)).toEqual(["plate-1", "edit-1"]);
    expect(graph().saveError).toBeNull();
  });

  it("two rapid edits are one PUT carrying both; a move or a selection is not an edit", async () => {
    fakeTimers();
    const calls = mockApi({ "PUT /projects/p/graph": echoGraph });
    graph().loadProject(GRAPH, "p");
    act(() => graph().onNodesChange([{ type: "position", id: "edit-1", position: { x: 300, y: 40 } }]));
    act(() => graph().onNodesChange([{ type: "select", id: "edit-1", selected: true }]));
    act(() => graph().onEdgesChange([{ type: "select", id: "e0", selected: true }]));
    advance(1000);
    await flush();
    expect(puts(calls)).toHaveLength(0);

    act(() => patchNodeParams("edit-1", { prompt: "dusk relight" })); // the editors' params setter
    advance(200);
    act(() => graph().removeNode("motion-1"));
    advance(SAVE_DEBOUNCE_MS + 50);
    await flush();
    expect(puts(calls)).toHaveLength(1);
    const sent = puts(calls)[0];
    expect(wireNode(sent, "edit-1")?.params).toEqual({ prompt: "dusk relight" });
    expect(wireNode(sent, "motion-1")).toBeUndefined();
    expect((sent.body as { edges: { id: string }[] }).edges.map((e) => e.id)).toEqual(["e0"]);
    advance(2000);
    await flush();
    expect(puts(calls)).toHaveLength(1);
  });

  it("connect, setNodeStatus and setPlatePacket each schedule a save", async () => {
    fakeTimers();
    const calls = mockApi({ "PUT /projects/p/graph": echoGraph });
    graph().loadProject(PLATE_ONLY, "p");
    act(() => graph().addNode("Edit", { x: 260, y: 0 }));
    advance(SAVE_DEBOUNCE_MS);
    await flush();
    act(() => graph().connect({ source: "plate-1", target: "edit-1", sourceHandle: null, targetHandle: null }));
    advance(SAVE_DEBOUNCE_MS);
    await flush();
    act(() => graph().setNodeStatus("edit-1", { gateStatus: "pass" }));
    advance(SAVE_DEBOUNCE_MS);
    await flush();
    act(() =>
      graph().setPlatePacket("plate-1", { packetId: "room_b", tier: "A", outputHash: "h_room_b", outputSize: { width: 640, height: 360 } }),
    );
    advance(SAVE_DEBOUNCE_MS);
    await flush();
    expect(puts(calls)).toHaveLength(4);
    expect(wireNode(puts(calls)[3], "plate-1")).toMatchObject({
      packetId: "room_b",
      tier: "A",
      outputHash: "h_room_b",
      outputSize: { width: 640, height: 360 },
    });
    expect(nodeData("plate-1").packetId).toBe("room_b");
  });

  it("writes the daemon's status fields back onto the nodes without saving again", async () => {
    fakeTimers();
    // The daemon owns the status (decision 31): the Edit was never accepted, so it is not approved.
    const calls = mockApi({
      "PUT /projects/p/graph": (call: Call) => {
        const g = call.body as { nodes: Record<string, unknown>[] };
        const nodes = g.nodes.map((n) => {
          if (n.id !== "edit-1") return n;
          const { approvedBy: _by, scrubLogId: _log, ...rest } = n;
          return { ...rest, gateStatus: "fail", approved: false };
        });
        return { ok: true, project_id: "p", graph: { ...g, nodes } };
      },
    });
    graph().loadProject(PLATE_ONLY, "p");
    act(() => graph().addNode("Edit", { x: 260, y: 0 }, { gateStatus: "pass", approved: true, approvedBy: "me", scrubLogId: "made-up" }));
    advance(SAVE_DEBOUNCE_MS);
    await flush();
    expect(nodeData("edit-1")).toMatchObject({ gateStatus: "fail", approved: false });
    expect(nodeData("edit-1").approvedBy).toBeUndefined();
    expect(nodeData("edit-1").scrubLogId).toBeUndefined();
    advance(2000);
    await flush();
    expect(puts(calls)).toHaveLength(1);
  });

  it("a change made while a PUT is out goes right after it; saving again unchanged reuses the PUT", async () => {
    fakeTimers();
    const pending: (() => void)[] = [];
    const calls = mockApi({
      "PUT /projects/p/graph": (call: Call) =>
        new Promise((resolve) => {
          pending.push(() => resolve(echoGraph(call)));
        }),
    });
    graph().loadProject(PLATE_ONLY, "p");
    act(() => graph().addNode("Edit", { x: 260, y: 0 }));
    let first!: Promise<void>;
    act(() => {
      first = graph().saveNow();
    });
    await flush();
    expect(puts(calls)).toHaveLength(1);
    expect(graph().saveNow()).toBe(first); // nothing changed since that PUT left
    act(() => patchNodeParams("edit-1", { task: "sky" }));
    let second!: Promise<void>;
    act(() => {
      second = graph().saveNow();
    });
    await flush();
    expect(puts(calls)).toHaveLength(1); // one PUT on the wire at a time
    pending[0]();
    await flush();
    expect(puts(calls)).toHaveLength(2);
    expect(wireNode(puts(calls)[1], "edit-1")?.params).toEqual({ task: "sky" });
    pending[1]();
    await act(async () => {
      await second;
    });
    advance(2000);
    await flush();
    expect(puts(calls)).toHaveLength(2);
  });

  it("opening another project drops the save scheduled for the old one; re-loading the open one with a change saves", async () => {
    fakeTimers();
    const calls = mockApi({ "PUT /projects/p/graph": echoGraph });
    graph().loadProject(PLATE_ONLY, "p");
    act(() => graph().addNode("Edit", { x: 260, y: 0 }));
    graph().loadProject(PLATE_ONLY, "q");
    advance(2000);
    await flush();
    expect(calls).toHaveLength(0);

    graph().loadProject(GRAPH, "p");
    graph().loadProject(GRAPH, "p"); // the same graph again: nothing to save
    advance(2000);
    await flush();
    expect(calls).toHaveLength(0);
    graph().loadProject({ ...GRAPH, nodes: [...GRAPH.nodes, { id: "comp-1", kind: "Comp", label: "Comp" }] }, "p"); // drop-to-restore
    advance(SAVE_DEBOUNCE_MS);
    await flush();
    expect(puts(calls)).toHaveLength(1);
    expect(wireNode(puts(calls)[0], "comp-1")).toBeDefined();
  });

  it("a 422 with violations sets saveError and draws the edge refused; the next good save clears both", async () => {
    fakeTimers();
    let refuse = true;
    mockApi({
      "PUT /projects/p/graph": (call: Call) =>
        refuse
          ? reply(
              {
                errors: ["VIDEO_LOCKED: 'Motion' stays locked until 'Edit' passed gates and is approved"],
                violations: [
                  {
                    edge: { id: "e1", source: "edit-1", target: "motion-1" },
                    code: "VIDEO_LOCKED",
                    message: "'Motion' stays locked until 'Edit' passed gates and is approved",
                  },
                ],
              },
              422,
            )
          : echoGraph(call),
    });
    graph().loadProject(GRAPH, "p");
    expect(graph().violations).toEqual([]); // the canvas rules alone allow this graph
    act(() => patchNodeParams("motion-1", { duration_s: 5 }));
    advance(SAVE_DEBOUNCE_MS);
    await flush();
    expect(graph().saveError).toEqual({
      code: "VIDEO_LOCKED",
      message: "'Motion' stays locked until 'Edit' passed gates and is approved",
      edgeId: "e1",
    });
    expect(graph().violations.map((v) => [v.edge.id, v.code])).toEqual([["e1", "VIDEO_LOCKED"]]);
    // what the canvas draws: the refused edge (PlateCanvas `edge-violation`) and the message on its target node
    const info = deriveNodeInfo(graph().nodes, graph().edges, graph().violations);
    expect(info.get("motion-1")?.violation?.code).toBe("VIDEO_LOCKED");

    refuse = false;
    await act(() => graph().saveNow());
    expect(graph().saveError).toBeNull();
    expect(graph().violations).toEqual([]);
  });

  it("any other failure sets saveError.message", async () => {
    mockApi({ "PUT /projects/p/graph": reply({ error: "locked", detail: "project.lock is held by another writer" }, 409) });
    graph().loadProject(PLATE_ONLY, "p");
    await act(() => graph().saveNow());
    expect(graph().saveError).toEqual({ code: "locked", message: "project.lock is held by another writer" });
  });

  it("does nothing without an open project", async () => {
    fakeTimers();
    const calls = mockApi({});
    act(() => graph().addNode("Edit", { x: 0, y: 0 }));
    advance(2000);
    await act(() => graph().saveNow());
    expect(calls).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------- client

describe("api client", () => {
  it("sends X-Plate-Client: ui on every request; saveGraph returns the stored graph; ApiError carries violations", async () => {
    const stored = { schema: 1, nodes: [{ id: "plate-1", kind: "Plate", gateStatus: "none" }], edges: [] };
    const calls = mockApi({
      "GET /health": { ok: true },
      "PUT /projects/p/graph": { ok: true, project_id: "p", graph: stored },
      "PUT /projects/bare/graph": stored,
      "PUT /projects/bad/graph": reply({ errors: ["x"], violations: [{ edge: { id: "e9" }, code: "CYCLE", message: "a cycle" }] }, 422),
    });
    await fetchJson("/health");
    const g = useGraphStore.getState().toGraph();
    expect(await saveGraph("p", g)).toEqual(stored);
    expect(await saveGraph("bare", g)).toEqual(stored);
    const err = await saveGraph("bad", g).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).violations).toEqual([{ edge: { id: "e9" }, code: "CYCLE", message: "a cycle" }]);
    expect(new ApiError(500, { error: "boom" }, "x").violations).toBeNull();
    expect(calls.map((c) => c.headers.get("X-Plate-Client"))).toEqual(["ui", "ui", "ui", "ui"]);
  });

  it("cancelRun posts the reason to /runs/{id}/cancel", async () => {
    const calls = mockApi({ "POST /runs/r1/cancel": { id: "r1", state: "CANCELLED" } });
    await cancelRun("r1", "wrong engine");
    await cancelRun("r1");
    expect(calls.map((c) => [c.path, c.body])).toEqual([
      ["/runs/r1/cancel", { reason: "wrong engine" }],
      ["/runs/r1/cancel", {}],
    ]);
  });
});

// ---------------------------------------------------------------------------- quote and approval

describe("quote and approval", () => {
  it("Quote on a fresh node PUTs the graph before the POST quote, once", async () => {
    fakeTimers();
    const calls = mockApi({ "PUT /projects/p/graph": echoGraph, "POST /projects/p/nodes/edit-1/quote": quoteAnswer() });
    graph().loadProject(PLATE_ONLY, "p");
    act(() => graph().addNode("Edit", { x: 260, y: 0 }));
    act(() => graph().connect({ source: "plate-1", target: "edit-1", sourceHandle: null, targetHandle: null }));
    useMakeStore.getState().setDraft("edit-1", { engineId: "nano-banana-2.1" });
    await act(() => useMakeStore.getState().requestQuote("edit-1", "p"));
    expect(order(calls)).toEqual(["PUT /projects/p/graph", "POST /projects/p/nodes/edit-1/quote"]);
    expect(wireNode(calls[0], "edit-1")).toBeDefined();
    expect(useMakeStore.getState().entry("edit-1").phase).toBe("quoted");
    advance(2000); // the scheduled save was folded into the quote's
    await flush();
    expect(puts(calls)).toHaveLength(1);
  });

  it("the quote body carries prompt, recipe, task and shot_class from the node params, never attempt", async () => {
    const calls = mockApi({ "PUT /projects/p/graph": echoGraph, "POST /projects/p/nodes/edit-1/quote": quoteAnswer() });
    graph().loadProject(GRAPH, "p");
    patchNodeParams("edit-1", {
      prompt: "relight to late blue hour",
      recipe: "night_relight_changelist",
      task: "relight",
      shot_class: "wide",
      attempt: 3,
    });
    useMakeStore.getState().setDraft("edit-1", { engineId: "nano-banana-2.1", params: { seed: 7, attempt: 2 } });
    await act(() => useMakeStore.getState().requestQuote("edit-1", "p"));
    const q = calls.find((c) => c.method === "POST" && c.path.endsWith("/quote"));
    expect(q?.body).toEqual({
      engine_id: "nano-banana-2.1",
      preset: "default",
      region_crop: false,
      params: { seed: 7 },
      prompt: "relight to late blue hour",
      recipe: "night_relight_changelist",
      task: "relight",
      shot_class: "wide",
    });
    expect(JSON.stringify(q?.body)).not.toContain("attempt");
    // fields that are absent or blank stay out of the body
    expect(quoteRequest({ engineId: "x", preset: "default", n: 1, regionCrop: false, params: {} }, { prompt: "  " })).toEqual({
      engine_id: "x",
      preset: "default",
      region_crop: false,
      params: {},
    });
  });

  it("a failed save stops the quote with the save error", async () => {
    const calls = mockApi({ "PUT /projects/p/graph": reply({ detail: "disk full" }, 500) });
    graph().loadProject(GRAPH, "p");
    useMakeStore.getState().setDraft("edit-1", { engineId: "nano-banana-2.1" });
    await act(() => useMakeStore.getState().requestQuote("edit-1", "p"));
    expect(order(calls)).toEqual(["PUT /projects/p/graph"]);
    const entry = useMakeStore.getState().entry("edit-1");
    expect(entry.phase).toBe("error");
    expect(entry.error).toBe("the graph is not saved: disk full");
    expect(entry.quote).toBeNull();
  });

  it("Make under the cap approves by: human", async () => {
    const calls = mockApi({
      "PUT /projects/p/graph": echoGraph,
      "POST /projects/p/nodes/edit-1/quote": quoteAnswer(),
      "POST /runs/run_1/approve": { id: "run_1", state: "APPROVED", node_id: "edit-1" },
    });
    graph().loadProject(GRAPH, "p");
    useMakeStore.getState().setDraft("edit-1", { engineId: "nano-banana-2.1" });
    await act(() => useMakeStore.getState().requestQuote("edit-1", "p"));
    await act(() => useMakeStore.getState().make("edit-1", "p"));
    expect(calls.find((c) => c.path === "/runs/run_1/approve")?.body).toEqual({ by: "human" });
    expect(useMakeStore.getState().entry("edit-1").phase).toBe("made");
  });

  async function refusedMake(code: string, detail: string): Promise<{ calls: Call[]; unmount: () => void }> {
    const calls = mockApi({
      "PUT /projects/p/graph": echoGraph,
      "POST /projects/p/nodes/edit-1/quote": quoteAnswer(),
      "POST /runs/run_1/approve": reply({ error: code, detail }, 409),
    });
    graph().loadProject(GRAPH, "p");
    act(() => graph().onNodesChange([{ type: "select", id: "edit-1", selected: true }]));
    const { unmount } = render(<MakeBar />);
    await act(async () => {
      useMakeStore.getState().setDraft("edit-1", { engineId: "nano-banana-2.1" });
      await useMakeStore.getState().requestQuote("edit-1", "p");
      await useMakeStore.getState().make("edit-1", "p");
    });
    return { calls, unmount };
  }

  it("unattended_cap shows the daemon's detail as a blocking message with no approve", async () => {
    const detail = "nobody has used the canvas for 31 min: unattended spend stops at 5.00 USD today";
    const { calls } = await refusedMake("unattended_cap", detail);
    const dialog = await screen.findByTestId("approval-dialog");
    expect(dialog).toHaveTextContent(detail);
    expect(dialog).toHaveAttribute("data-blocking", "true");
    expect(screen.queryByTestId("approval-approve")).toBeNull();
    // nothing to confirm: a confirmed make on a blocked entry sends nothing
    await act(() => useMakeStore.getState().make("edit-1", "p", { confirmed: true }));
    expect(calls.filter((c) => c.path === "/runs/run_1/approve")).toHaveLength(1);
    fireEvent.click(screen.getByTestId("approval-close"));
    expect(screen.queryByTestId("approval-dialog")).toBeNull();
    expect(useMakeStore.getState().entry("edit-1").approvalBlocking).toBe(false);
  });

  it("daily_cap blocks the same way", async () => {
    await refusedMake("daily_cap", "the daily cap of 25.00 USD is spent");
    const dialog = await screen.findByTestId("approval-dialog");
    expect(dialog).toHaveTextContent("the daily cap of 25.00 USD is spent");
    expect(screen.queryByTestId("approval-approve")).toBeNull();
  });

  it("auto_cap, envelope_required and approval_refused open the dialog with the detail and an approve", async () => {
    for (const code of ["auto_cap", "envelope_required", "approval_refused"]) {
      const { unmount } = await refusedMake(code, `refused: ${code}`);
      const dialog = await screen.findByTestId("approval-dialog");
      expect(dialog).toHaveTextContent(`refused: ${code}`);
      expect(dialog).toHaveAttribute("data-blocking", "false");
      expect(screen.getByTestId("approval-approve")).toBeEnabled();
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      expect(screen.queryByTestId("approval-dialog")).toBeNull();
      unmount();
      useMakeStore.getState().reset();
    }
  });
});

// ---------------------------------------------------------------------------- accept

describe("accept after the full scrub", () => {
  const ACCEPTED = { id: "r1", node_id: "motion-1", state: "DONE", accepted: { scrub_log_id: "scrub-9", decision: "accepted", by: "human" } };

  it("acceptRun without a logged scrub is refused here and sends nothing", async () => {
    const calls = mockApi({});
    graph().loadProject(GRAPH, "p");
    const out = await usePanelsStore.getState().acceptRun("r1");
    expect(out.ok).toBe(false);
    expect(out.message).toContain("full scrub");
    expect(calls).toHaveLength(0);
    expect(nodeData("motion-1").approved).toBe(false);
  });

  it("with the scrub it posts /accept, then PUTs the graph with the node approved", async () => {
    fakeTimers();
    const calls = mockApi({ "POST /runs/r1/accept": ACCEPTED, "PUT /projects/p/graph": echoGraph });
    graph().loadProject(GRAPH, "p");
    usePanelsStore.setState({ scrubLogs: { r1: "scrub-9" } });
    let out!: Awaited<ReturnType<ReturnType<typeof usePanelsStore.getState>["acceptRun"]>>;
    await act(async () => {
      out = await usePanelsStore.getState().acceptRun("r1");
    });
    expect(out).toEqual({ ok: true, message: "take accepted" });
    expect(order(calls)).toEqual(["POST /runs/r1/accept", "PUT /projects/p/graph"]);
    expect(calls[0].body).toEqual({ scrub_log_id: "scrub-9", by: "human" });
    expect(wireNode(calls[1], "motion-1")).toMatchObject({ approved: true, approvedBy: "human", scrubLogId: "scrub-9" });
    expect(nodeData("motion-1")).toMatchObject({ approved: true, approvedBy: "human", scrubLogId: "scrub-9" });
    expect(usePanelsStore.getState().accepted).toEqual({ r1: true });
    advance(2000);
    await flush();
    expect(puts(calls)).toHaveLength(1);
  });

  it("the daemon's refusal is the outcome, and the node stays unapproved", async () => {
    mockApi({ "POST /runs/r1/accept": reply({ error: "scrub_required", detail: "no logged full scrub 'scrub-9' of run r1" }, 409) });
    graph().loadProject(GRAPH, "p");
    usePanelsStore.setState({ scrubLogs: { r1: "scrub-9" } });
    const out = await usePanelsStore.getState().acceptRun("r1");
    expect(out.ok).toBe(false);
    expect(out.message).toContain("take not accepted");
    expect(nodeData("motion-1").approved).toBe(false);
  });

  it("VerdictPanel: Accept take enables only after the full scrub, is offered after Ship, and accepts", async () => {
    const calls = mockApi({
      "POST /runs/r1/verdict": { ok: true },
      "POST /runs/r1/accept": ACCEPTED,
      "PUT /projects/p/graph": echoGraph,
    });
    graph().loadProject(GRAPH, "p");
    usePanelsStore.setState({ selectedRunId: "r1" });
    render(<VerdictPanel />);
    const button = screen.getByTestId("accept-take");
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("Accept take");

    act(() => usePanelsStore.setState({ scrubLogs: { r1: "scrub-9" } }));
    expect(button).toBeEnabled();
    expect(button).toHaveAttribute("data-offered", "false");
    fireEvent.keyDown(document.body, { key: "1" }); // Ship
    await waitFor(() => expect(button).toHaveAttribute("data-offered", "true"));

    fireEvent.click(button);
    await waitFor(() => expect(button).toHaveTextContent("Accepted"));
    expect(button).toBeDisabled();
    expect(order(calls)).toEqual(["POST /runs/r1/verdict", "POST /runs/r1/accept", "PUT /projects/p/graph"]);
    expect(screen.getByTestId("verdict-outcome")).toHaveTextContent("take accepted");
    expect(nodeData("motion-1").approved).toBe(true);
  });
});
