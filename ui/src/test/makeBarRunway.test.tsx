// T123: the Make bar shows each video route of an engine as a chip with its grade and per-second price, the preferred
// route (Runway by rule, T63) first; an image engine shows none. The quote line carries the route the daemon
// picked. `fetch` and EventSource are fakes; the harness is makeBarWave9's.

import { ReactFlowProvider } from "@xyflow/react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearThumbCache } from "../canvas/assets";
import { hub, unwatchAll } from "../canvas/runEvents";
import { useRunStore } from "../canvas/runStore";
import type { EventSourceLike } from "../api/client";
import type { QuoteWire } from "../canvas/runTypes";
import type { EngineSummary } from "../makebar/engines";
import { engineChips } from "../makebar/MakeBar";
import { useMakeStore } from "../makebar/makeStore";
import { QUOTE_TTL_MS } from "../makebar/quote";
import { initialPanelsState, usePanelsStore } from "../panels/store";
import PlateCanvas from "../PlateCanvas";
import { createInitialGraph, useGraphStore } from "../store";

// ---------------------------------------------------------------------------- fakes

interface Call {
  method: string;
  path: string;
  body: unknown;
}

type Reply = { status?: number; body?: unknown } | undefined;

function mockFetch(route: (call: Call) => Reply): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://127.0.0.1");
      const call: Call = {
        method: init?.method ?? "GET",
        path: url.pathname.replace(/^\/api\/v0/, ""),
        body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
      };
      calls.push(call);
      const out = route(call) ?? { status: 404, body: { error: "not_found" } };
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
  url: string;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(url: string) {
    this.url = url;
  }
  addEventListener(): void {}
  close(): void {}
}

// ---------------------------------------------------------------------------- fixtures

const route = (provider: string, over: Record<string, unknown> = {}) => ({
  route_id: provider,
  provider,
  custody_grade: "D",
  custody_status: "confirmed",
  effective_grade: "D",
  status: "active",
  health: "ok",
  price: null,
  preferred: false,
  ...over,
});

const caps = (over: Record<string, unknown> = {}) => ({
  largest_size: null,
  image_tail: true,
  reference_images_max: 0,
  elements: true,
  price_unit: "per_second",
  runnable: true,
  unverified: false,
  ...over,
});

const RUNWAY_PRICE = { usd: 0.43, model: "per_second", status: "confirmed", minimum_usd: 0 };

// The daemon lists the Atlas route first (manifest order); the Runway one is the preferred route.
const KLING = {
  id: "kling-o3-4k",
  name: "Kling O3 4K",
  kind: "video_startend",
  allowed_tasks: ["crop_pair_motion"],
  routes: [route("atlas"), route("runway", { price: RUNWAY_PRICE, preferred: true })],
  params: [],
  presets: ["default"],
  capabilities: caps(),
};

const NANO = {
  id: "nano-banana-2.1",
  name: "Nano Banana 2.1",
  kind: "image_edit",
  allowed_tasks: ["relight", "people"],
  routes: [route("atlas", { price: null }), route("gemini", { custody_grade: "C1", effective_grade: "D" })],
  params: [],
  presets: ["default"],
  capabilities: caps({ price_unit: "per_image", image_tail: false, elements: false }),
};

const SEEDANCE = {
  id: "seedance-2.5-i2v",
  name: "Seedance 2.5 image-to-video",
  kind: "video_i2v",
  allowed_tasks: ["crop_pair_motion"],
  routes: [
    route("atlas"),
    route("wavespeed", { status: "retired" }),
    route("runway", { price: { usd: 0.2, model: "per_second", status: "confirmed", minimum_usd: 0.8 }, preferred: true }),
  ],
  params: [],
  presets: ["default"],
  capabilities: caps({ image_tail: false, elements: false }),
};

const WAN = {
  id: "wan-2.2-vace",
  name: "Wan 2.2 VACE",
  kind: "local_video",
  allowed_tasks: ["previs"],
  routes: [route("local", { custody_grade: "C0", effective_grade: "C0" })],
  params: [],
  presets: ["default"],
  capabilities: caps({ image_tail: false, elements: false }),
};

function quote(over: Partial<QuoteWire> = {}): QuoteWire {
  const now = Date.now();
  return {
    id: "qt_1",
    run_id: "run_m",
    provider: "runway",
    model: "kling-o3-4k",
    model_version: "klingO3_4k",
    region: "us",
    custody_grade: "D",
    price_usd: 2.15,
    eta_s: 90,
    created_utc: new Date(now).toISOString(),
    expires_utc: new Date(now + QUOTE_TTL_MS).toISOString(),
    price_status: "confirmed",
    siblings: 1,
    ...over,
  };
}

/** The routes the Make bar calls, against project p1 and its Motion node. */
function daemon(q: QuoteWire = quote()): Call[] {
  return mockFetch((c) => {
    if (c.path === "/health") return { body: { ok: true, daemon_version: "0.1.0" } };
    if (c.path === "/engines") return { body: [KLING, NANO, SEEDANCE, WAN] };
    if (c.path === "/engines/cards") return { body: [] };
    if (c.path === "/projects" && c.method === "GET") {
      return { body: [{ project_id: "p1", custody: { nda: false, allowed_grades: ["C0", "C1", "C2", "D"] } }] };
    }
    if (c.path === "/projects/p1/graph" && c.method === "PUT") return { body: { ok: true, project_id: "p1", graph: c.body } };
    if (c.path === "/projects/p1/nodes/mo-1/quote" && c.method === "POST") {
      return { body: { run: { id: "run_m", state: "QUOTED", node_id: "mo-1", quote: q }, quote: q } };
    }
    return undefined;
  });
}

/** Plate -> Motion on the packet port, Motion selected, project p1 open. */
function loadGraph(): void {
  useGraphStore.getState().loadProject(
    {
      schema: 1,
      nodes: [
        { id: "plate-1", kind: "Plate", label: "Plate", packetId: "pk_room_a", tier: "A", outputHash: "h_plate", outputSize: { width: 64, height: 36 }, position: { x: 0, y: 0 } },
        { id: "mo-1", kind: "Motion", label: "Motion", position: { x: 300, y: 0 } },
      ],
      edges: [{ id: "plate-1->mo-1", source: "plate-1", target: "mo-1", sourceHandle: "packet", targetHandle: "packet" }],
    },
    "p1",
  );
  useGraphStore.setState((s) => ({ nodes: s.nodes.map((n) => ({ ...n, selected: n.id === "mo-1" })) }));
}

const bar = () => within(screen.getByTestId("make-bar"));
const option = (id: string) => bar().getByRole("option", { name: (_n, el) => (el as HTMLOptionElement).value === id }) as HTMLOptionElement;
const pick = (id: string) => fireEvent.change(bar().getByTestId("make-engine"), { target: { value: id } });
const routeChips = () => bar().queryAllByTestId("make-chip").filter((c) => (c.getAttribute("data-chip") ?? "").startsWith("route-"));

async function enginesLoaded(): Promise<void> {
  await waitFor(() => expect(useMakeStore.getState().engines?.source).toBe("daemon"));
  await waitFor(() => expect(option("kling-o3-4k")).toBeInTheDocument());
}

const mountCanvas = () =>
  render(
    <ReactFlowProvider>
      <PlateCanvas />
    </ReactFlowProvider>,
  );

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  useRunStore.getState().reset();
  useMakeStore.getState().reset();
  usePanelsStore.setState(initialPanelsState());
  unwatchAll();
  hub.setFactory((url) => new FakeEventSource(url));
  clearThumbCache();
});

afterEach(() => {
  unwatchAll();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
});

// ---------------------------------------------------------------------------- route chips

describe("video route chips", () => {
  it("lists the runway route first with its grade and per-second price, then the atlas fallback", async () => {
    daemon();
    loadGraph();
    mountCanvas();
    await enginesLoaded();
    pick("kling-o3-4k");
    const chips = routeChips();
    expect(chips.map((c) => c.getAttribute("data-chip"))).toEqual(["route-runway", "route-atlas"]);
    expect(chips[0]).toHaveTextContent("runway · D · 0.43 USD per second");
    expect(chips[0]).toHaveAttribute("data-tone", "red");
    expect(chips[0].className).toContain("tone-red");
    expect(chips[0]).toHaveAttribute("title", "preferred route for video");
    expect(chips[1].textContent).toBe("atlas · D"); // no price of its own: no figure to show
    expect(chips[1]).toHaveAttribute("title", "fallback route");
    expect(chips[1]).toHaveAttribute("data-tone", "red");
  });

  it("the route chips sit after the capability chips and before the health chip", () => {
    const degraded = { ...KLING, routes: [route("atlas", { health: "degraded" }), route("runway", { price: RUNWAY_PRICE, preferred: true })] };
    const keys = engineChips(degraded as unknown as EngineSummary, null, null).map((c) => c.key);
    expect(keys).toEqual(["end_frame", "elements", "price_unit", "route-runway", "route-atlas", "health"]);
  });

  it("an image engine renders no route chips, with or without prices on its routes", async () => {
    daemon();
    loadGraph();
    mountCanvas();
    await enginesLoaded();
    expect(engineChips(NANO as unknown as EngineSummary, null, null).some((c) => c.key.startsWith("route-"))).toBe(false);
    const priced = { ...NANO, routes: [route("runway", { price: RUNWAY_PRICE, preferred: true })] };
    expect(engineChips(priced as unknown as EngineSummary, null, null).some((c) => c.key.startsWith("route-"))).toBe(false);
  });

  it("a retired route gets no chip; the minimum does not change the per-second figure", async () => {
    daemon();
    loadGraph();
    mountCanvas();
    await enginesLoaded();
    pick("seedance-2.5-i2v");
    expect(routeChips().map((c) => c.getAttribute("data-chip"))).toEqual(["route-runway", "route-atlas"]);
    expect(routeChips()[0]).toHaveTextContent("runway · D · 0.20 USD per second");
  });

  it("a local video engine shows its one route, not preferred, graded C0 in green", async () => {
    daemon();
    loadGraph();
    mountCanvas();
    await enginesLoaded();
    const [only] = engineChips(WAN as unknown as EngineSummary, null, null).filter((c) => c.key.startsWith("route-"));
    expect(only).toMatchObject({ key: "route-local", text: "local · C0", tone: "green", title: "fallback route" });
  });

  it("without prices or preference from the daemon (the fallback catalogue) the chips still name provider and grade", () => {
    const old = { ...KLING, routes: [{ provider: "atlas", custody_grade: "D", custody_status: "confirmed" }] };
    const chips = engineChips(old as unknown as EngineSummary, null, null).filter((c) => c.key.startsWith("route-"));
    expect(chips).toHaveLength(1);
    expect(chips[0]).toMatchObject({ key: "route-atlas", text: "atlas · D", title: "fallback route" });
  });
});

// ---------------------------------------------------------------------------- the quote line

describe("the quote line names the route the daemon chose", () => {
  it("shows runway, the price and the D chip for a Runway quote", async () => {
    const calls = daemon();
    loadGraph();
    mountCanvas();
    await enginesLoaded();
    pick("kling-o3-4k");
    fireEvent.click(bar().getByTestId("make-quote"));
    await waitFor(() => expect(bar().getByTestId("quote-provider")).toHaveTextContent("runway"));
    expect(calls.filter((c) => c.path === "/projects/p1/nodes/mo-1/quote")).toHaveLength(1);
    expect(bar().getByTestId("quote-price")).toHaveTextContent("2.15 USD");
    expect(bar().getByTestId("quote-custody")).toHaveAttribute("data-grade", "D");
    expect(bar().getByTestId("quote-custody")).toHaveTextContent("D");
  });
});
