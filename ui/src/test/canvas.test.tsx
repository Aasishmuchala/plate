// Canvas tests (T35): every node kind renders with the shared header, typed handles, inline rule refusals that
// never connect and vanish after 4 s, the Make bar (no Make without a fresh quote, expiry disables it, approval
// dialog over the auto cap, custody-filtered engines), the best-of-N strip (N rows, live state over a multiplexed
// event stream, ranking, keys 1..6, GATE_FAIL first check + override with a reason, agent envelopes), the viewer
// A/B wiring, the fallback engine catalogue against the manifests on disk, and the shared rules vectors
// (spec/canvas_rules_vectors.json, T25) through the TS rules. `fetch` and EventSource are fakes; no daemon runs.

import { type Connection, ReactFlowProvider } from "@xyflow/react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { type EventSourceLike, TOKEN_KEY } from "../api/client";
import { assetImageSource, clearThumbCache } from "../canvas/assets";
import { attemptConnect, checkConnection, connectionFromDrop } from "../canvas/connect";
import { hub, parseRecord, projectEventsUrl, unwatchAll, watchRun } from "../canvas/runEvents";
import { type Candidate, rankCandidates, REFUSAL_FLASH_MS, useRunStore } from "../canvas/runStore";
import type { BusRecord, QuoteWire, RunWire } from "../canvas/runTypes";
import { canAccept, generationDepth, validateEdge } from "../canvasRules";
import {
  allowedGrades,
  effectiveGrade,
  engineOptions,
  FALLBACK_ENGINES,
  MAKE_MODES,
  type ProjectCustody,
} from "../makebar/engines";
import { useMakeStore } from "../makebar/makeStore";
import { formatCountdown, needsHumanApproval, QUOTE_TTL_MS, quoteLifetimeMs, totalPrice } from "../makebar/quote";
import { type LoadableNode, NODE_KINDS, type NodeKind, UNSEEN_GEOMETRY_LABEL } from "../nodeTypes";
import { checkHandles, PORTS } from "../nodes/ports";
import { initialPanelsState, usePanelsStore } from "../panels/store";
import PlateCanvas from "../PlateCanvas";
import { createInitialGraph, graphFromWire, useGraphStore } from "../store";

// ---------------------------------------------------------------------------- fakes

interface Call {
  method: string;
  url: string;
  body: unknown;
  headers: Headers;
}

type Reply = { status?: number; body?: unknown } | undefined;
type Route = (method: string, url: string, body: unknown) => Reply;

/** A fetch stub with plain-promise responses (no streams, so fake timers never stall it). */
function mockFetch(route: Route): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const method = init?.method ?? "GET";
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
      calls.push({ method, url, body, headers: new Headers(init?.headers) });
      const out = route(method, url, body) ?? { status: 404, body: { error: "not_found" } };
      const status = out.status ?? 200;
      const text = out.body === undefined ? "" : JSON.stringify(out.body);
      return {
        ok: status >= 200 && status < 300,
        status,
        text: async () => text,
        blob: async () => new Blob([text]),
        arrayBuffer: async () => new ArrayBuffer(0),
      } as unknown as Response;
    }),
  );
  return calls;
}

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

/** Let promise chains (fetch -> text -> JSON -> store) settle inside act. */
async function flush(rounds = 12): Promise<void> {
  await act(async () => {
    for (let i = 0; i < rounds; i++) await Promise.resolve();
  });
}

// ---------------------------------------------------------------------------- fixtures

const T0 = Date.UTC(2026, 9, 17, 12, 0, 0);

function quote(over: Partial<QuoteWire> = {}): QuoteWire {
  const now = Date.now();
  return {
    id: "qt_1",
    run_id: "run_1",
    provider: "atlas",
    model: "nano-banana-2.1",
    model_version: "unpinned",
    region: "unknown",
    custody_grade: "D",
    price_usd: 0.034,
    eta_s: 40,
    created_utc: new Date(now).toISOString(),
    expires_utc: new Date(now + QUOTE_TTL_MS).toISOString(),
    price_status: "unconfirmed",
    siblings: 1,
    ...over,
  };
}

const run = (id: string, over: Partial<RunWire> = {}): RunWire => ({ id, state: "QUOTED", node_id: "edit-1", ...over });

const wireEdge = (source: string, target: string, sourceHandle: string | null = null, targetHandle: string | null = null) => ({
  id: `${source}->${target}`,
  source,
  target,
  sourceHandle,
  targetHandle,
});

/** Plate -> Edit, the Edit selected, project p1 open. */
function loadPlateEdit(edit: Partial<LoadableNode> = {}): void {
  useGraphStore.getState().loadProject(
    {
      schema: 1,
      nodes: [
        { id: "plate-1", kind: "Plate", label: "Plate", packetId: "pk_room_a", tier: "A", outputHash: "h_plate", outputSize: { width: 64, height: 36 }, position: { x: 0, y: 0 } },
        { id: "edit-1", kind: "Edit", label: "Edit", position: { x: 300, y: 0 }, ...edit },
      ],
      edges: [wireEdge("plate-1", "edit-1", "image", "image")],
    },
    "p1",
  );
  select("edit-1");
}

function select(id: string): void {
  useGraphStore.setState((s) => ({ nodes: s.nodes.map((n) => ({ ...n, selected: n.id === id })) }));
}

/** A daemon with the routes the Make bar calls. `price` per run, `n` siblings on fan-out. */
function daemon(opts: { price?: number; approve?: Reply; runs?: Record<string, RunWire>; engines?: Reply } = {}): Call[] {
  const price = opts.price ?? 0.034;
  return mockFetch((method, url, body) => {
    if (url.endsWith("/api/v0/health")) return { body: { ok: true, daemon_version: "0.1.0" } };
    if (url.endsWith("/api/v0/engines")) return opts.engines ?? { status: 404, body: { error: "not_found" } };
    if (url.endsWith("/api/v0/projects") && method === "GET") {
      return { body: [{ project_id: "p1", custody: { nda: false, allowed_grades: ["C0", "C1", "C2", "D"] } }] };
    }
    if (url.endsWith("/api/v0/projects/p1/nodes/edit-1/quote") && method === "POST") {
      const q = quote({ price_usd: price });
      return { body: { run: run("run_1", { quote: q }), quote: q } };
    }
    const fan = /\/api\/v0\/runs\/run_1\/fanout$/.exec(url);
    if (fan && method === "POST") {
      const n = (body as { n: number }).n;
      return {
        body: Array.from({ length: n }, (_, i) =>
          run(i === 0 ? "run_1" : `run_${i + 1}`, { sibling_group: "grp_1", sibling_index: i, quote: quote({ price_usd: price, sibling_group: "grp_1", sibling_index: i, siblings: n }) }),
        ),
      };
    }
    if (url.endsWith("/api/v0/runs/run_1/approve") && method === "POST") return opts.approve ?? { body: run("run_1", { state: "APPROVED" }) };
    const get = /\/api\/v0\/runs\/([^/?]+)$/.exec(url);
    if (get && method === "GET" && opts.runs?.[get[1]]) return { body: opts.runs[get[1]] };
    const ov = /\/api\/v0\/runs\/([^/?]+)\/override$/.exec(url);
    if (ov && method === "POST") return { body: run(ov[1], { state: "GATE_FAIL", override: { reason: (body as { reason: string }).reason, by: "human" } }) };
    return undefined;
  });
}

const mountCanvas = () =>
  render(
    <ReactFlowProvider>
      <PlateCanvas />
    </ReactFlowProvider>,
  );

const makeBar = () => within(screen.getByTestId("make-bar"));

const engineValues = () =>
  within(makeBar().getByTestId("make-engine"))
    .getAllByRole("option")
    .map((o) => (o as HTMLOptionElement).value)
    .filter(Boolean);

/** Wait until the engine list (daemon or fallback) is in the select. */
const enginesLoaded = () => waitFor(() => expect(engineValues().length).toBeGreaterThan(0));

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  useRunStore.getState().reset();
  useMakeStore.getState().reset();
  usePanelsStore.setState(initialPanelsState());
  unwatchAll();
  FakeEventSource.all = [];
  hub.setFactory((url) => new FakeEventSource(url));
  clearThumbCache();
});

afterEach(() => {
  unwatchAll();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
});

// ---------------------------------------------------------------------------- node kinds

describe("node components", () => {
  it("renders every node kind with the shared header and its typed handles", () => {
    mockFetch(() => undefined);
    const nodes: LoadableNode[] = NODE_KINDS.map((kind, i) => ({ id: `n-${kind}`, kind, label: kind, position: { x: (i % 5) * 260, y: Math.floor(i / 5) * 260 } }));
    useGraphStore.getState().loadProject({ schema: 1, nodes, edges: [] });
    mountCanvas();
    for (const kind of NODE_KINDS) {
      const node = screen.getByTestId(`node-${kind}`);
      expect(within(node).getByTestId("badge-kind")).toHaveTextContent(kind);
      expect(within(node).getByTestId("badge-tier")).toHaveAttribute(
        "data-tier",
        kind === "Concept" ? "concept_unmeasured" : "measured",
      );
      for (const p of PORTS[kind].inputs) expect(within(node).getByTestId(`handle-in-${p.id}`)).toHaveAttribute("data-type", p.type);
      for (const p of PORTS[kind].outputs) expect(within(node).getByTestId(`handle-out-${p.id}`)).toHaveAttribute("data-type", p.type);
    }
    expect(NODE_KINDS).toHaveLength(19);
  });

  it("shows gate, approval and risk badges and the inherited 'unseen geometry guessed' label", () => {
    mockFetch(() => undefined);
    useGraphStore.getState().loadProject({
      schema: 1,
      nodes: [
        { id: "p", kind: "Plate", label: "Plate", packetId: "pk", tier: "A", gateStatus: "pass", approved: true, approvedBy: "aasish" },
        { id: "e", kind: "Edit", label: "Edit", gateStatus: "fail", labels: [UNSEEN_GEOMETRY_LABEL] },
        { id: "m", kind: "Mask", label: "Mask", gateStatus: "advisory", riskLabel: "highest" },
        { id: "c", kind: "Comp", label: "Comp" },
      ],
      edges: [wireEdge("p", "e", "image", "image"), wireEdge("e", "c", "image", "base")],
    });
    mountCanvas();
    const plate = screen.getByTestId("node-Plate");
    expect(within(plate).getByTestId("badge-gate")).toHaveAttribute("data-status", "pass");
    expect(within(plate).getByTestId("badge-approved")).toHaveAttribute("title", "approved by aasish");
    expect(within(plate).getByText("pk · Tier A")).toBeInTheDocument();
    expect(within(screen.getByTestId("node-Edit")).getByTestId("badge-gate")).toHaveAttribute("data-status", "fail");
    expect(within(screen.getByTestId("node-Mask")).getByTestId("badge-risk")).toHaveAttribute("data-risk", "highest");
    expect(within(screen.getByTestId("node-Comp")).queryByTestId("badge-gate")).toBeNull();
    // the label follows the pixels downstream
    expect(within(screen.getByTestId("node-Comp")).getByTestId("node-label")).toHaveTextContent(UNSEEN_GEOMETRY_LABEL);
  });

  it("marks a Concept node and everything downstream of it as concept_unmeasured with a permanent watermark", () => {
    mockFetch(() => undefined);
    useGraphStore.getState().loadProject({
      schema: 1,
      nodes: [
        { id: "c", kind: "Concept", label: "Concept" },
        { id: "cmp", kind: "Compare", label: "Compare" },
      ],
      edges: [wireEdge("c", "cmp", "image", "a")],
    });
    mountCanvas();
    const concept = screen.getByTestId("node-Concept");
    expect(within(concept).getByTestId("concept-watermark")).toHaveTextContent("CONCEPT, UNMEASURED");
    expect(within(concept).getByTestId("handle-out-image").className).toContain("handle-concept");
    expect(within(screen.getByTestId("node-Compare")).getByTestId("badge-tier")).toHaveAttribute("data-tier", "concept_unmeasured");
  });

  it("Export and Review have no output handle; the body thumbnail is fetched with the token", async () => {
    window.sessionStorage.setItem(TOKEN_KEY, "tok");
    const calls = mockFetch((_m, url) => (url.includes("/assets/") ? { body: "png" } : undefined));
    const created = vi.fn(() => "blob:thumb-1");
    vi.stubGlobal("URL", Object.assign(globalThis.URL, { createObjectURL: created, revokeObjectURL: vi.fn() }));
    useGraphStore.getState().loadProject({
      schema: 1,
      nodes: [
        { id: "x", kind: "Export", label: "Export", outputHash: "h_abc" },
        { id: "r", kind: "Review", label: "Review" },
      ],
      edges: [],
    });
    mountCanvas();
    expect(PORTS.Export.outputs).toHaveLength(0);
    expect(within(screen.getByTestId("node-Export")).queryByTestId(/handle-out-/)).toBeNull();
    expect(within(screen.getByTestId("node-Review")).queryByTestId(/handle-out-/)).toBeNull();
    const img = await screen.findByTestId("node-thumb");
    expect(img).toHaveAttribute("src", "blob:thumb-1");
    const thumbCall = calls.find((c) => c.url.includes("/assets/"));
    expect(thumbCall?.url).toBe("/api/v0/assets/h_abc?fmt=thumb");
    expect(thumbCall?.headers.get("Authorization")).toBe("Bearer tok");
  });
});

// ---------------------------------------------------------------------------- connections

describe("typed handles and rule refusals", () => {
  const conn = (source: string, target: string, sourceHandle: string | null = null, targetHandle: string | null = null): Connection => ({
    source,
    target,
    sourceHandle,
    targetHandle,
  });

  it("checkHandles refuses a type mismatch and resolves a null target handle by type", () => {
    expect(checkHandles("Mask", "mask", "Comp", "base")).toEqual({
      ok: false,
      code: "HANDLE_TYPE",
      message: "Comp base takes image16, not mask",
    });
    expect(checkHandles("Mask", null, "Edit", null)).toMatchObject({ ok: true, type: "mask", targetHandle: "mask" });
    expect(checkHandles("Plate", "packet", "Export", null)).toMatchObject({ ok: false, code: "HANDLE_TYPE" });
    expect(checkHandles("Motion", "video", "Export", "media")).toMatchObject({ ok: true, type: "video" });
    expect(checkHandles("Gate", "report", "Export", null)).toMatchObject({ ok: false });
  });

  it("an invalid connection shows the rule message inline for 4 s and does not connect", () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    mockFetch(() => undefined);
    useGraphStore.getState().loadProject({
      schema: 1,
      nodes: [
        { id: "c", kind: "Concept", label: "Concept", position: { x: 0, y: 0 } },
        { id: "x", kind: "Export", label: "Export", position: { x: 300, y: 0 } },
      ],
      edges: [],
    });
    mountCanvas();
    let added = true;
    act(() => {
      added = attemptConnect(conn("c", "x", "image", "media"));
    });
    expect(added).toBe(false);
    expect(useGraphStore.getState().edges).toHaveLength(0);
    const bubble = within(screen.getByTestId("node-Export")).getByTestId("node-refusal");
    expect(bubble).toHaveAttribute("data-code", "CONCEPT_TO_DELIVERY");
    expect(bubble).toHaveTextContent("concept tier cannot reach Export");
    expect(screen.getByTestId("last-refusal")).toHaveTextContent("concept tier cannot reach Export");
    act(() => {
      vi.advanceTimersByTime(REFUSAL_FLASH_MS - 1);
    });
    expect(screen.queryByTestId("node-refusal")).not.toBeNull();
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(screen.queryByTestId("node-refusal")).toBeNull();
  });

  it("a handle type mismatch is refused the same way, and a valid connection stores both handle ids", () => {
    mockFetch(() => undefined);
    useGraphStore.getState().loadProject({
      schema: 1,
      nodes: [
        { id: "p", kind: "Plate", label: "Plate", packetId: "pk", tier: "A" },
        { id: "m", kind: "Mask", label: "Mask" },
        { id: "e", kind: "Edit", label: "Edit" },
        { id: "c", kind: "Comp", label: "Comp" },
      ],
      edges: [],
    });
    mountCanvas();
    act(() => {
      expect(attemptConnect(conn("m", "c", "mask", "base"))).toBe(false);
    });
    expect(within(screen.getByTestId("node-Comp")).getByTestId("node-refusal")).toHaveAttribute("data-code", "HANDLE_TYPE");
    act(() => {
      expect(attemptConnect(conn("p", "m"))).toBe(true);
      expect(attemptConnect(conn("p", "e"))).toBe(true);
      expect(attemptConnect(conn("m", "e"))).toBe(true);
    });
    const edges = useGraphStore.getState().edges.map((e) => [e.source, e.sourceHandle, e.target, e.targetHandle]);
    expect(edges).toEqual([
      ["p", "image", "m", "source"],
      ["p", "image", "e", "image"],
      ["m", "mask", "e", "mask"],
    ]);
  });

  it("motion stays locked until the upstream still passed gates and is approved", () => {
    mockFetch(() => undefined);
    useGraphStore.getState().loadProject({
      schema: 1,
      nodes: [
        { id: "p", kind: "Plate", label: "Plate", packetId: "pk", tier: "A" },
        { id: "e", kind: "Edit", label: "Edit" },
        { id: "mo", kind: "Motion", label: "Motion" },
      ],
      edges: [wireEdge("p", "e", "image", "image")],
    });
    mountCanvas();
    expect(checkConnection(conn("e", "mo", "image", "image"))).toMatchObject({ ok: false, code: "VIDEO_LOCKED" });
    act(() => {
      attemptConnect(conn("e", "mo", "image", "image"));
    });
    expect(within(screen.getByTestId("node-Motion")).getByTestId("node-refusal")).toHaveTextContent("motion is locked");
  });

  it("turns a drop that isValidConnection refused into the refusal message", () => {
    const state = {
      isValid: false,
      fromHandle: { id: "image", nodeId: "c", type: "source" },
      toHandle: { id: "media", nodeId: "x", type: "target" },
      fromNode: { id: "c" },
      toNode: { id: "x" },
    } as unknown as Parameters<typeof connectionFromDrop>[0];
    expect(connectionFromDrop(state)).toEqual({ source: "c", target: "x", sourceHandle: "image", targetHandle: "media" });
    // dragged backwards, from the input
    const back = { ...state, fromHandle: state.toHandle, toHandle: state.fromHandle } as typeof state;
    expect(connectionFromDrop(back)).toEqual({ source: "c", target: "x", sourceHandle: "image", targetHandle: "media" });
    expect(connectionFromDrop({ ...state, isValid: true } as typeof state)).toBeNull();
  });
});

// ---------------------------------------------------------------------------- Make bar

describe("Make bar", () => {
  it("before any node is selected it shows a pending quote and no price", () => {
    mockFetch(() => undefined);
    mountCanvas();
    const bar = screen.getByTestId("make-bar");
    expect(bar).toHaveTextContent("quote pending");
    expect(bar).toHaveTextContent("select a node to make");
    expect(within(bar).queryByTestId("quote-price")).toBeNull();
  });

  it("Make is disabled without a quote; Quote needs an engine", async () => {
    daemon();
    loadPlateEdit();
    mountCanvas();
    await waitFor(() => expect(makeBar().getByTestId("make-engines-fallback")).toBeInTheDocument());
    expect(makeBar().getByTestId("make-make")).toBeDisabled();
    expect(makeBar().getByTestId("make-quote")).toBeDisabled();
    fireEvent.change(makeBar().getByTestId("make-engine"), { target: { value: "nano-banana-2.1" } });
    expect(makeBar().getByTestId("make-quote")).toBeEnabled();
    expect(makeBar().getByTestId("make-make")).toBeDisabled();
    expect(makeBar().getByTestId("quote-line")).toHaveTextContent("quote pending");
  });

  it("Quote shows provider · model · price USD · custody grade · ETA and a countdown; Make then enables", async () => {
    const calls = daemon();
    loadPlateEdit();
    mountCanvas();
    await enginesLoaded();
    fireEvent.change(makeBar().getByTestId("make-engine"), { target: { value: "nano-banana-2.1" } });
    fireEvent.click(makeBar().getByTestId("make-region"));
    fireEvent.click(makeBar().getByTestId("make-quote"));
    await waitFor(() => expect(makeBar().getByTestId("quote-provider")).toHaveTextContent("atlas"));
    expect(makeBar().getByTestId("quote-model")).toHaveTextContent("nano-banana-2.1");
    expect(makeBar().getByTestId("quote-price")).toHaveTextContent("0.034 USD");
    const grade = makeBar().getByTestId("quote-custody");
    expect(grade).toHaveAttribute("data-grade", "D");
    expect(grade.className).toContain("tone-red");
    expect(makeBar().getByTestId("quote-eta")).toHaveTextContent("ETA ~40 s");
    expect(makeBar().getByTestId("quote-countdown")).toHaveTextContent(/expires in 1[45]:\d\d/);
    expect(makeBar().getByTestId("make-make")).toBeEnabled();
    const q = calls.find((c) => c.method === "POST" && c.url.endsWith("/quote"));
    expect(q?.url).toBe("/api/v0/projects/p1/nodes/edit-1/quote");
    expect(q?.body).toEqual({ engine_id: "nano-banana-2.1", preset: "default", region_crop: true, params: {} });
    // N = 1: no fan-out call
    expect(calls.some((c) => c.url.endsWith("/fanout"))).toBe(false);
  });

  it("quote expiry disables Make and says to quote again", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
    vi.setSystemTime(T0);
    daemon();
    loadPlateEdit();
    mountCanvas();
    await flush();
    fireEvent.change(makeBar().getByTestId("make-engine"), { target: { value: "nano-banana-2.1" } });
    fireEvent.click(makeBar().getByTestId("make-quote"));
    await flush();
    expect(makeBar().getByTestId("make-make")).toBeEnabled();
    expect(makeBar().getByTestId("quote-countdown")).toHaveTextContent("expires in 15:00");
    act(() => {
      vi.advanceTimersByTime(14 * 60 * 1000 + 59 * 1000);
    });
    expect(makeBar().getByTestId("quote-countdown")).toHaveTextContent("expires in 0:01");
    expect(makeBar().getByTestId("make-make")).toBeEnabled();
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(makeBar().getByTestId("make-make")).toBeDisabled();
    expect(makeBar().getByTestId("quote-countdown")).toHaveTextContent("quote expired");
    expect(makeBar().getByTestId("quote-countdown")).toHaveAttribute("data-fresh", "false");
  });

  it("changing the draft after a quote drops the quote (Make runs exactly what was quoted)", async () => {
    daemon();
    loadPlateEdit();
    mountCanvas();
    await enginesLoaded();
    fireEvent.change(makeBar().getByTestId("make-engine"), { target: { value: "nano-banana-2.1" } });
    fireEvent.click(makeBar().getByTestId("make-quote"));
    await waitFor(() => expect(makeBar().getByTestId("make-make")).toBeEnabled());
    fireEvent.change(makeBar().getByTestId("make-n"), { target: { value: "3" } });
    expect(makeBar().getByTestId("make-make")).toBeDisabled();
    expect(makeBar().getByTestId("quote-line")).toHaveTextContent("quote pending");
  });

  it("at or over the auto cap Make opens the approval dialog; approving sends a human approval", async () => {
    const calls = daemon({ price: 2.5 });
    loadPlateEdit();
    mountCanvas();
    await enginesLoaded();
    fireEvent.change(makeBar().getByTestId("make-engine"), { target: { value: "nano-banana-2.1" } });
    fireEvent.click(makeBar().getByTestId("make-quote"));
    await waitFor(() => expect(makeBar().getByTestId("make-make")).toBeEnabled());
    fireEvent.click(makeBar().getByTestId("make-make"));
    const dialog = await screen.findByTestId("approval-dialog");
    expect(dialog).toHaveTextContent("2.50 USD is at or over the auto-approve cap of 2.00 USD");
    expect(calls.some((c) => c.url.endsWith("/approve"))).toBe(false);
    fireEvent.click(within(dialog).getByTestId("approval-approve"));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/approve"))).toBe(true));
    expect(calls.find((c) => c.url.endsWith("/approve"))?.body).toEqual({ by: "human" });
    await waitFor(() => expect(screen.queryByTestId("approval-dialog")).toBeNull());
  });

  it("under the cap Make approves as auto; a daemon refusal of auto asks the human instead", async () => {
    const calls = daemon({ approve: { status: 422, body: { error: "auto_cap", detail: "a human approves this one" } } });
    loadPlateEdit();
    mountCanvas();
    await enginesLoaded();
    fireEvent.change(makeBar().getByTestId("make-engine"), { target: { value: "nano-banana-2.1" } });
    fireEvent.click(makeBar().getByTestId("make-quote"));
    await waitFor(() => expect(makeBar().getByTestId("make-make")).toBeEnabled());
    fireEvent.click(makeBar().getByTestId("make-make"));
    const dialog = await screen.findByTestId("approval-dialog");
    expect(calls.find((c) => c.url.endsWith("/approve"))?.body).toEqual({ by: "auto" });
    expect(dialog).toHaveTextContent("a human approves this one");
  });

  it("offers engines by node kind and project custody: NDA leaves only the local C0 engines", () => {
    const open: ProjectCustody = { nda: false, allowed_grades: ["C0", "C1", "C2", "D"] };
    const nda: ProjectCustody = { nda: true, allowed_grades: ["C0", "C1"] };
    const ids = (kind: NodeKind, c: ProjectCustody | null, task?: string) => engineOptions(FALLBACK_ENGINES, kind, c, task).map((e) => e.id);
    expect(ids("Edit", open)).toContain("nano-banana-2.1");
    expect(ids("Edit", open)).not.toContain("real-esrgan"); // the plate is never upscaled
    expect(ids("Edit", open)).not.toContain("kling-o3-4k");
    expect(ids("Edit", nda)).toEqual(["qwen-image-edit-2509"]); // pending terms count as D (decision 5a)
    expect(ids("Motion", nda)).toEqual(["wan-2.2-vace"]);
    expect(ids("Motion", open)).toEqual(expect.arrayContaining(["kling-o3-4k", "seedance-2.5-i2v", "veo-3.1"]));
    expect(ids("People", open)).not.toContain("flux-3-image"); // flux lists no people task
    expect(ids("Edit", open, "people")).not.toContain("seedream-5-pro");
    expect(ids("Concept", open)).toEqual([]); // there is no text-to-image path
    expect(MAKE_MODES.Concept.mode).toBe("none");
    expect(allowedGrades({ nda: true, allowed_grades: ["C0", "C1", "D"] })).toEqual(["C0", "C1"]);
    expect(effectiveGrade({ provider: "gemini", custody_grade: "C1", custody_status: "pending_written_terms" })).toBe("D");
  });

  it("the NDA project's engine select lists only C0", async () => {
    daemon();
    loadPlateEdit();
    mountCanvas();
    await enginesLoaded();
    act(() => useMakeStore.getState().setCustody("p1", { nda: true, allowed_grades: ["C0", "C1"] }));
    expect(engineValues()).toEqual(["qwen-image-edit-2509"]);
  });

  it("the advanced drawer shows the full manifest params when the daemon sends them", async () => {
    daemon({
      engines: {
        body: [
          {
            id: "nano-banana-2.1",
            name: "Nano Banana 2.1",
            kind: "image_edit",
            allowed_tasks: ["relight"],
            routes: [{ provider: "atlas", custody_grade: "D", custody_status: "confirmed" }],
            params: [
              { name: "image_size", type: "enum", enum: ["1K", "2K", "4K"] },
              { name: "seed", type: "int", range: [0, 10], advanced: true },
            ],
          },
        ],
      },
    });
    loadPlateEdit();
    mountCanvas();
    await enginesLoaded();
    expect(makeBar().queryByTestId("make-engines-fallback")).toBeNull();
    fireEvent.change(makeBar().getByTestId("make-engine"), { target: { value: "nano-banana-2.1" } });
    fireEvent.click(makeBar().getByTestId("make-advanced"));
    const rows = makeBar().getAllByTestId("make-param");
    expect(rows.map((r) => r.querySelector("label")?.textContent)).toEqual(["image_size", "seed"]);
    fireEvent.change(makeBar().getByLabelText("image_size"), { target: { value: "2K" } });
    expect(useMakeStore.getState().entry("edit-1").draft.params).toEqual({ image_size: "2K" });
  });

  it("blocks Make on a node that is not anchored to a plate, and on kinds that make nothing", () => {
    mockFetch(() => undefined);
    useGraphStore.getState().loadProject(
      { schema: 1, nodes: [{ id: "edit-1", kind: "Edit", label: "Edit" }, { id: "g", kind: "Gate", label: "Gate" }], edges: [] },
      "p1",
    );
    select("edit-1");
    const { unmount } = mountCanvas();
    expect(makeBar().getByTestId("quote-line")).toHaveTextContent("connect it to a plate first");
    expect(makeBar().getByTestId("make-quote")).toBeDisabled();
    unmount();
    select("g");
    mountCanvas();
    expect(makeBar().getByTestId("quote-line")).toHaveTextContent("nothing to make on a Gate node");
    expect(makeBar().queryByTestId("make-make")).toBeNull();
  });

  it("quote helpers: lifetime from the daemon's timestamps, countdown text, cap test, group total", () => {
    expect(quoteLifetimeMs({ created_utc: "2026-10-17T12:00:00Z", expires_utc: "2026-10-17T12:15:00Z" })).toBe(QUOTE_TTL_MS);
    expect(quoteLifetimeMs({ created_utc: "2026-10-17T12:00:00Z", expires_utc: "2026-10-17T12:05:00Z" })).toBe(5 * 60 * 1000);
    expect(quoteLifetimeMs({ created_utc: "x", expires_utc: "y" })).toBe(QUOTE_TTL_MS);
    expect(formatCountdown(61_000)).toBe("1:01");
    expect(formatCountdown(0)).toBe("0:00");
    expect(needsHumanApproval(2.0, 2.0)).toBe(true);
    expect(needsHumanApproval(1.99, 2.0)).toBe(false);
    const q = quote({ price_usd: 0.5 });
    expect(totalPrice([run("a", { quote: q }), run("b", { quote: q }), run("c", { quote: q })], q)).toBeCloseTo(1.5);
  });
});

// ---------------------------------------------------------------------------- fan-out, live status

async function quoteAndMake(n: number, opts: Parameters<typeof daemon>[0] = {}): Promise<Call[]> {
  const calls = daemon(opts);
  loadPlateEdit();
  mountCanvas();
  await enginesLoaded();
  fireEvent.change(makeBar().getByTestId("make-engine"), { target: { value: "nano-banana-2.1" } });
  fireEvent.change(makeBar().getByTestId("make-n"), { target: { value: String(n) } });
  fireEvent.click(makeBar().getByTestId("make-quote"));
  await waitFor(() => expect(makeBar().getByTestId("make-make")).toBeEnabled());
  fireEvent.click(makeBar().getByTestId("make-make"));
  await waitFor(() => expect(screen.getAllByTestId("candidate")).toHaveLength(n));
  return calls;
}

const strip = () => within(screen.getByTestId("candidate-strip"));

describe("fan-out and live status", () => {
  it("N = 3 fans the quoted run out, prices the group, and the strip renders 3 candidates", async () => {
    const calls = await quoteAndMake(3);
    expect(calls.find((c) => c.url.endsWith("/fanout"))?.body).toEqual({ n: 3 });
    expect(calls.filter((c) => c.url.endsWith("/approve"))).toHaveLength(1); // the daemon approves the whole group
    const rows = strip().getAllByTestId("candidate");
    expect(rows.map((r) => r.getAttribute("data-run"))).toEqual(["run_1", "run_2", "run_3"]);
    expect(rows.map((r) => r.getAttribute("data-state"))).toEqual(["APPROVED", "APPROVED", "APPROVED"]);
  });

  it("the group price is the sum of the siblings and shown with the per-run price", async () => {
    daemon({ price: 0.5 });
    loadPlateEdit();
    mountCanvas();
    await enginesLoaded();
    fireEvent.change(makeBar().getByTestId("make-engine"), { target: { value: "nano-banana-2.1" } });
    fireEvent.change(makeBar().getByTestId("make-n"), { target: { value: "6" } });
    fireEvent.click(makeBar().getByTestId("make-quote"));
    await waitFor(() => expect(makeBar().getByTestId("quote-price")).toHaveTextContent("3.00 USD (6 × 0.50 USD)"));
    // 3.00 >= 2.00: a human approves
    fireEvent.click(makeBar().getByTestId("make-make"));
    expect(await screen.findByTestId("approval-dialog")).toBeInTheDocument();
  });

  it("N is clamped to 1..6", () => {
    useMakeStore.getState().setDraft("edit-1", { n: 9 });
    expect(useMakeStore.getState().entry("edit-1").draft.n).toBe(6);
    useMakeStore.getState().setDraft("edit-1", { n: 0 });
    expect(useMakeStore.getState().entry("edit-1").draft.n).toBe(1);
  });

  it("one event stream per project carries every sibling's state; terminal runs are read once and ranked", async () => {
    const done = (id: string, score: number, out: string): RunWire =>
      run(id, { state: "DONE", outputs: [out], gate_score: score, gate_report: { verdict: "pass", checks: [] }, sibling_group: "grp_1" });
    await quoteAndMake(3, {
      runs: {
        run_1: done("run_1", 0.71, "h_1"),
        run_2: done("run_2", 0.93, "h_2"),
        run_3: run("run_3", {
          state: "GATE_FAIL",
          outputs: ["h_3"],
          gate_score: 0.99,
          sibling_group: "grp_1",
          gate_report: {
            verdict: "fail",
            checks: [
              { metric_id: "registration.residual_px", value: 0.4, status: "pass" },
              { metric_id: "outside_mask.de00_max", value: 3.2, status: "fail" },
              { metric_id: "texture.lapvar", value: 120, status: "fail" },
            ],
          },
        }),
      },
    });
    expect(open()).toHaveLength(1);
    expect(open()[0].url).toBe(projectEventsUrl("p1", null));
    const es = open()[0];
    act(() => {
      es.emit({ seq: 1, run_id: "run_2", kind: "run.state", data: { run_id: "run_2", from: "QUEUED", to: "RUNNING" } });
    });
    expect(strip().getAllByTestId("candidate-state").map((c) => c.textContent)).toEqual(["APPROVED", "RUNNING", "APPROVED"]);
    // the node header follows the first candidate
    expect(within(screen.getByTestId("node-Edit")).getAllByTestId("state-chip")[0]).toHaveTextContent("APPROVED");
    act(() => {
      es.emit({ seq: 2, run_id: "run_1", kind: "run.state", data: { run_id: "run_1", from: "GATING", to: "DONE" } });
      es.emit({ seq: 3, run_id: "run_2", kind: "run.state", data: { run_id: "run_2", from: "GATING", to: "DONE" } });
      es.emit({ seq: 4, run_id: "run_3", kind: "run.state", data: { run_id: "run_3", from: "GATING", to: "GATE_FAIL" } });
    });
    // all terminal: the stream closes, each run was read once, and the strip ranks DONE by score, then GATE_FAIL
    expect(open()).toHaveLength(0);
    await waitFor(() => expect(strip().getAllByTestId("candidate").map((r) => r.getAttribute("data-run"))).toEqual(["run_2", "run_1", "run_3"]));
    const fail = within(strip().getAllByTestId("candidate")[2]);
    expect(fail.getByTestId("candidate-fail")).toHaveTextContent("outside_mask.de00_max 3.200");
    expect(fail.getByTestId("override-open")).toHaveTextContent("Override (reason required)");
  });

  it("a GATE_FAIL override needs a reason and is posted to the daemon before it shows", async () => {
    const calls = await quoteAndMake(1, {
      runs: { run_1: run("run_1", { state: "GATE_FAIL", gate_report: { verdict: "fail", checks: [{ metric_id: "gbuffer.recall", value: 0.81, status: "fail" }] } }) },
    });
    act(() => {
      open()[0].emit({ seq: 1, run_id: "run_1", kind: "run.state", data: { run_id: "run_1", to: "GATE_FAIL" } });
    });
    await waitFor(() => expect(strip().getByTestId("candidate-fail")).toHaveTextContent("gbuffer.recall 0.810"));
    fireEvent.click(strip().getByTestId("override-open"));
    const submit = strip().getByTestId("override-submit");
    expect(submit).toBeDisabled();
    fireEvent.change(strip().getByLabelText("override reason"), { target: { value: "   " } });
    expect(submit).toBeDisabled();
    fireEvent.change(strip().getByLabelText("override reason"), { target: { value: "client signed off the hedge" } });
    expect(submit).toBeEnabled();
    fireEvent.click(submit);
    await waitFor(() => expect(strip().getByTestId("candidate-overridden")).toBeInTheDocument());
    expect(calls.find((c) => c.url.endsWith("/runs/run_1/override"))?.body).toEqual({ reason: "client signed off the hedge", by: "human" });
  });

  it("a GATE_FAIL read without checks falls back to the run's gate report for the first failing check", async () => {
    await quoteAndMake(1, { runs: { run_1: run("run_1", { state: "GATE_FAIL", gate_report: null }) } });
    const route = vi.mocked(fetch).getMockImplementation();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).endsWith("/runs/run_1/gate")) {
          const text = JSON.stringify({ n: 1, report: { verdict: "fail", checks: [{ metric_id: "invention.flags", value: 2, status: "fail" }] } });
          return { ok: true, status: 200, text: async () => text } as unknown as Response;
        }
        return (route as typeof fetch)(input, init);
      }),
    );
    act(() => {
      open()[0].emit({ run_id: "run_1", kind: "run.state", data: { run_id: "run_1", to: "GATE_FAIL" } });
    });
    await waitFor(() => expect(strip().getByTestId("candidate-fail")).toHaveTextContent("invention.flags 2"));
  });

  it("keys 1..6 in the strip pick a candidate: its output and verdict go onto the node, the panels select it", async () => {
    await quoteAndMake(2, {
      runs: {
        run_1: run("run_1", { state: "DONE", outputs: ["h_1"], gate_score: 0.5, gate_report: { verdict: "pass" } }),
        run_2: run("run_2", { state: "DONE", outputs: ["h_2"], gate_score: 0.9, gate_report: { verdict: "advisory" } }),
      },
    });
    act(() => {
      open()[0].emit({ run_id: "run_1", kind: "run.state", data: { run_id: "run_1", to: "DONE" } });
      open()[0].emit({ run_id: "run_2", kind: "run.state", data: { run_id: "run_2", to: "DONE" } });
    });
    await waitFor(() => expect(strip().getAllByTestId("candidate")[0]).toHaveAttribute("data-run", "run_2"));
    const s = screen.getByTestId("candidate-strip");
    expect(s).toHaveAttribute("data-plate-keys");
    const outer = vi.fn();
    document.addEventListener("keydown", outer);
    fireEvent.keyDown(s, { key: "2" });
    document.removeEventListener("keydown", outer);
    expect(outer).not.toHaveBeenCalled(); // the strip keeps its digits from the global verdict keys
    expect(useRunStore.getState().byNode["edit-1"].picked).toBe("run_1");
    const node = useGraphStore.getState().nodes.find((n) => n.id === "edit-1");
    expect(node?.data.outputHash).toBe("h_1");
    expect(node?.data.gateStatus).toBe("pass");
    expect(usePanelsStore.getState().selectedRunId).toBe("run_1");
    fireEvent.keyDown(s, { key: "1" });
    expect(useGraphStore.getState().nodes.find((n) => n.id === "edit-1")?.data.gateStatus).toBe("advisory");
    fireEvent.keyDown(s, { key: "6" }); // no sixth candidate: nothing changes
    expect(useRunStore.getState().byNode["edit-1"].picked).toBe("run_2");
  });

  it("an agent's run shows the envelope it spent from", () => {
    mockFetch(() => undefined);
    loadPlateEdit();
    useRunStore.getState().startRuns("edit-1", [run("run_a", { state: "RUNNING", approved_by: "agent:env_42" })]);
    mountCanvas();
    expect(strip().getByTestId("candidate-envelope")).toHaveTextContent("envelope env_42");
  });

  it("without a project the hub falls back to one stream per run, with the token in the query", () => {
    window.sessionStorage.setItem(TOKEN_KEY, "tok");
    useRunStore.getState().startRuns("edit-1", [run("r1", { state: "QUEUED" })]);
    watchRun("r1", "edit-1", null);
    expect(open().map((s) => s.url)).toEqual(["/api/v0/events?run_id=r1&token=tok"]);
    open()[0].emit({ run_id: "r1", kind: "run.state", data: { run_id: "r1", to: "STAGING" } });
    expect(useRunStore.getState().candidates["r1"].state).toBe("STAGING");
    expect(parseRecord("not json")).toBeNull();
    expect(parseRecord({ seq: 1 })).toBeNull();
  });

  it("rankCandidates keeps sibling order while any run is live and the daemon's order once it sent one", () => {
    const c = (runId: string, index: number, state: Candidate["state"], gateScore: number | null): Candidate => ({
      runId, nodeId: "n", index, group: "g", state, outputHash: null, gateScore, verdict: null, firstFail: null,
      approvedBy: null, envelopeId: null, quote: null, override: null, error: null,
    });
    const live = [c("a", 0, "DONE", 0.1), c("b", 1, "RUNNING", null), c("d", 2, "DONE", 0.9)];
    expect(rankCandidates(live).map((x) => x.runId)).toEqual(["a", "b", "d"]);
    const finished = [c("a", 0, "DONE", 0.1), c("b", 1, "FAILED", null), c("d", 2, "DONE", 0.9), c("e", 3, "GATE_FAIL", 0.95)];
    expect(rankCandidates(finished).map((x) => x.runId)).toEqual(["d", "a", "e", "b"]);
    expect(rankCandidates(finished, ["e", "b", "a", "d"]).map((x) => x.runId)).toEqual(["e", "b", "a", "d"]);
  });
});

// ---------------------------------------------------------------------------- viewer wiring

describe("viewer A/B", () => {
  it("clicking a candidate compares it with its plate", async () => {
    mockFetch(() => undefined);
    loadPlateEdit();
    useRunStore.getState().startRuns("edit-1", [run("run_1", { state: "DONE", outputs: ["h_out"] })]);
    render(
      <ReactFlowProvider>
        <App />
      </ReactFlowProvider>,
    );
    fireEvent.click(within(screen.getByTestId("candidate")).getByTitle("compare with the plate in the viewer"));
    expect(screen.getByTestId("compare-caption")).toHaveTextContent("A plate · B candidate 1");
    expect(screen.getByTestId("viewer")).toBeInTheDocument();
    expect(usePanelsStore.getState().selectedRunId).toBe("run_1");
    expect(assetImageSource("h_out", { width: 64, height: 36 })).toEqual({ url: "/api/v0/assets/h_out", width: 64, height: 36, encoding: "png16" });
    expect(assetImageSource("h_out", undefined).encoding).toBe("srgb8");
  });
});

// ---------------------------------------------------------------------------- data on disk

const MANIFESTS = import.meta.glob("../../../engines/plate_engines/manifests/*.yaml", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** The few manifest fields the fallback catalogue mirrors, read with line patterns (the files are flat YAML). */
function manifestFacts(text: string) {
  const one = (re: RegExp) => re.exec(text)?.[1]?.trim().replace(/^"|"$/g, "") ?? null;
  const tasks = one(/^allowed_tasks:\s*\[(.*)\]\s*$/m);
  const routes: { provider: string; grade: string; status: string }[] = [];
  const re = /^ {2}- provider:\s*(\S+)[\s\S]*?^ {4}custody_grade:\s*(\S+)\s*^ {4}custody_status:\s*(\S+)/gm;
  for (let m = re.exec(text); m; m = re.exec(text)) routes.push({ provider: m[1], grade: m[2], status: m[3] });
  return {
    id: one(/^id:\s*(.+)$/m),
    name: one(/^name:\s*(.+)$/m),
    kind: one(/^kind:\s*(.+)$/m),
    allowed_tasks: tasks ? tasks.split(",").map((t) => t.trim()) : [],
    routes,
  };
}

describe("fallback engine catalogue", () => {
  it("matches engines/plate_engines/manifests/*.yaml", () => {
    const facts = Object.values(MANIFESTS).map(manifestFacts).sort((a, b) => String(a.id).localeCompare(String(b.id)));
    expect(facts.length).toBeGreaterThanOrEqual(14);
    const ours = [...FALLBACK_ENGINES]
      .sort((a, b) => a.id.localeCompare(b.id))
      .map((e) => ({
        id: e.id,
        name: e.name,
        kind: e.kind,
        allowed_tasks: e.allowed_tasks,
        routes: e.routes.map((r) => ({ provider: r.provider, grade: r.custody_grade, status: r.custody_status })),
      }));
    expect(ours).toEqual(facts);
  });
});

const VECTORS = import.meta.glob("../../../spec/canvas_rules_vectors.json", { import: "default", eager: true }) as Record<string, unknown>;
const vectorsDoc = Object.values(VECTORS)[0];

type Wire = { nodes: LoadableNode[]; edges: { id?: string; source: string; target: string }[] };
interface Expect {
  ok: boolean;
  code?: string;
}
/** The three vector shapes of spec/canvas_rules_vectors.json (T25): edge, accept and depth. */
type Vector =
  | { name: string; graph: Wire; edge: { id?: string; source: string; target: string }; expect: Expect }
  | { name: string; node: LoadableNode; scrub_log: { id?: string; fullScrub: boolean } | null; expect: Expect }
  | { name: string; graph: Wire; node: string; depth: number };

/** Wire graph -> store nodes/edges; edges without an id get `source->target`. */
function storeGraph(g: Wire) {
  const edges = g.edges.map((e) => ({
    id: e.id ?? `${e.source}->${e.target}`,
    source: e.source,
    target: e.target,
    sourceHandle: null,
    targetHandle: null,
  }));
  return graphFromWire({ schema: 1, nodes: g.nodes, edges });
}

const verdictOf = (r: { ok: boolean; code?: string }) => (r.ok ? { ok: true } : { ok: false, code: r.code });

describe("shared canvas rules vectors (spec/canvas_rules_vectors.json)", () => {
  const vectors = ((vectorsDoc as { vectors?: Vector[] } | undefined)?.vectors ?? []) as Vector[];
  it.skipIf(vectorsDoc === undefined)("the TS rules agree with every vector", () => {
    expect(vectors.length).toBeGreaterThan(0);
    for (const v of vectors) {
      if ("edge" in v) {
        const { nodes, edges } = storeGraph(v.graph);
        const edge = { id: v.edge.id ?? `${v.edge.source}->${v.edge.target}`, source: v.edge.source, target: v.edge.target };
        const got = validateEdge({ nodes, edges }, edge);
        expect({ name: v.name, ...verdictOf(got) }).toEqual({ name: v.name, ...verdictOf(v.expect) });
      } else if ("scrub_log" in v) {
        const [node] = graphFromWire({ schema: 1, nodes: [v.node], edges: [] }).nodes;
        const got = canAccept(node, v.scrub_log);
        expect({ name: v.name, ...verdictOf(got) }).toEqual({ name: v.name, ...verdictOf(v.expect) });
      } else {
        const { nodes, edges } = storeGraph(v.graph);
        expect({ name: v.name, depth: generationDepth(nodes, edges, v.node) }).toEqual({ name: v.name, depth: v.depth });
      }
    }
  });
});
