// T102 Make bar parity: capability chips from `GET /engines` (largest size, end frame, refs ≤ N, elements, price
// unit, unverified), route health (degraded amber, suspended red and not selectable), `runnable: false` options
// disabled, engine-card numbers in the option label (bold when steering), `Compare engines…` posting one
// `quote_many` and filling the strip with one candidate per engine, the group approval dialog, no preset select on an
// Edit node, and the quote line's price tier and amber `estimated` status. `fetch` and EventSource are fakes.

import { ReactFlowProvider } from "@xyflow/react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearThumbCache } from "../canvas/assets";
import { hub, unwatchAll } from "../canvas/runEvents";
import { useRunStore } from "../canvas/runStore";
import type { EventSourceLike } from "../api/client";
import type { QuoteWire, RunWire } from "../canvas/runTypes";
import { cardFor, engineHealth, type EngineCard, unavailableReason } from "../makebar/api";
import type { EngineSummary } from "../makebar/engines";
import { engineChips, engineLabel, MakeBar, priceUnitText } from "../makebar/MakeBar";
import { compareReady, quoteManyRequest, useMakeStore } from "../makebar/makeStore";
import { formatCard, formatTier, priceStatusTone, QUOTE_TTL_MS, worstGrade } from "../makebar/quote";
import type { LoadableNode } from "../nodeTypes";
import { initialPanelsState, usePanelsStore } from "../panels/store";
import PlateCanvas from "../PlateCanvas";
import { createInitialGraph, useGraphStore } from "../store";

// ---------------------------------------------------------------------------- fakes

interface Call {
  method: string;
  path: string;
  search: URLSearchParams;
  body: unknown;
}

type Reply = { status?: number; body?: unknown } | undefined;
type Route = (call: Call) => Reply;

function mockFetch(route: Route): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://127.0.0.1");
      const call: Call = {
        method: init?.method ?? "GET",
        path: url.pathname.replace(/^\/api\/v0/, ""),
        search: url.searchParams,
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
  ...over,
});

const caps = (over: Record<string, unknown> = {}) => ({
  largest_size: null,
  image_tail: false,
  reference_images_max: 0,
  elements: false,
  price_unit: "per_image",
  runnable: true,
  unverified: false,
  ...over,
});

const NANO = {
  id: "nano-banana-2.1",
  name: "Nano Banana 2.1",
  kind: "image_edit",
  allowed_tasks: ["relight", "people"],
  routes: [route("atlas")],
  params: [],
  presets: ["default", "season_swap"],
  capabilities: caps({ largest_size: "4096x4096", image_tail: true, reference_images_max: 13, elements: true, unverified: true }),
};

const FAKE = {
  id: "fake",
  name: "Fake (echo)",
  kind: "image_edit",
  allowed_tasks: ["relight", "people"],
  routes: [route("local", { custody_grade: "C0", effective_grade: "C0" })],
  params: [],
  presets: ["default"],
  capabilities: caps({ price_unit: "flat" }),
};

const SEEDREAM = {
  id: "seedream-5-pro",
  name: "Seedream 5 Pro",
  kind: "image_edit",
  allowed_tasks: ["relight"],
  routes: [route("atlas")],
  presets: ["default"],
  capabilities: caps({ runnable: false }),
};

const FLUX = {
  id: "flux-3-image",
  name: "FLUX 3 Image",
  kind: "image_edit",
  allowed_tasks: ["relight"],
  routes: [route("bfl", { health: "degraded" })],
  presets: ["default"],
  capabilities: caps(),
};

const GPT = {
  id: "gpt-image-2.5",
  name: "GPT Image 2.5",
  kind: "image_edit",
  allowed_tasks: ["relight"],
  routes: [route("openai", { health: "suspended" })],
  presets: ["default"],
  capabilities: caps(),
};

const ENGINES = [FAKE, FLUX, GPT, NANO, SEEDREAM];

function quote(over: Partial<QuoteWire> & { price_tier?: string | null } = {}): QuoteWire {
  const now = Date.now();
  return {
    id: "qt_1",
    run_id: "run_a",
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

const groupRun = (id: string, engine: string, i: number, q: QuoteWire): RunWire & { route: { engine_id: string } } => ({
  id,
  state: "QUOTED",
  node_id: "edit-1",
  sibling_group: "grp_x",
  sibling_index: i,
  route: { engine_id: engine },
  quote: { ...q, run_id: id, sibling_group: "grp_x", sibling_index: i, siblings: 2 },
});

const CARD: EngineCard = {
  engine_id: "nano-banana-2.1",
  task: "relight",
  shot_class: null,
  n: 20,
  passes: 18,
  pass_rate: 0.9,
  wilson_lower_95: 0.6989,
  determinism_var: null,
  mean_cost: 0.034,
  custody: "D",
  steering: true,
};

interface DaemonOpts {
  engines?: unknown[];
  cards?: EngineCard[];
  quote?: QuoteWire;
  groupPrices?: [number, number];
}

/** The routes the Make bar calls, against project p1. */
function daemon(opts: DaemonOpts = {}): Call[] {
  return mockFetch((c) => {
    if (c.path === "/health") return { body: { ok: true, daemon_version: "0.1.0" } };
    if (c.path === "/engines") return { body: opts.engines ?? ENGINES };
    if (c.path === "/engines/cards") return { body: opts.cards ?? [] };
    if (c.path === "/projects" && c.method === "GET") {
      return { body: [{ project_id: "p1", custody: { nda: false, allowed_grades: ["C0", "C1", "C2", "D"] } }] };
    }
    if (c.path === "/projects/p1/graph" && c.method === "PUT") return { body: { ok: true, project_id: "p1", graph: c.body } };
    if (c.path === "/projects/p1/nodes/edit-1/quote" && c.method === "POST") {
      const q = opts.quote ?? quote();
      return { body: { run: { id: "run_a", state: "QUOTED", node_id: "edit-1", quote: q }, quote: q } };
    }
    if (c.path === "/projects/p1/nodes/edit-1/quote_many" && c.method === "POST") {
      const ids = (c.body as { engine_ids: string[] }).engine_ids;
      const [p0, p1] = opts.groupPrices ?? [0, 0.034];
      const runs = ids.map((engine, i) =>
        groupRun(`run_${String.fromCharCode(97 + i)}`, engine, i, quote({ provider: engine === "fake" ? "local" : "atlas", model: engine, price_usd: i === 0 ? p0 : p1, custody_grade: engine === "fake" ? "C0" : "D" })),
      );
      return { body: { sibling_group: "grp_x", runs, total_usd: p0 + p1 } };
    }
    if (c.path === "/runs/run_a/approve" && c.method === "POST") {
      return { body: [{ id: "run_a", state: "APPROVED" }, { id: "run_b", state: "APPROVED" }] };
    }
    return undefined;
  });
}

const wireEdge = (source: string, target: string, sourceHandle: string | null = null, targetHandle: string | null = null) => ({
  id: `${source}->${target}`,
  source,
  target,
  sourceHandle,
  targetHandle,
});

/** Plate -> Edit and a People node on the same plate; `selected` is selected, project p1 open. */
function loadGraph(selected = "edit-1", edit: Partial<LoadableNode> = {}): void {
  useGraphStore.getState().loadProject(
    {
      schema: 1,
      nodes: [
        { id: "plate-1", kind: "Plate", label: "Plate", packetId: "pk_room_a", tier: "A", outputHash: "h_plate", outputSize: { width: 64, height: 36 }, position: { x: 0, y: 0 } },
        { id: "edit-1", kind: "Edit", label: "Edit", position: { x: 300, y: 0 }, ...edit },
        { id: "people-1", kind: "People", label: "People", position: { x: 300, y: 200 } },
      ],
      edges: [wireEdge("plate-1", "edit-1", "image", "image"), wireEdge("plate-1", "people-1", "image", "image")],
    },
    "p1",
  );
  useGraphStore.setState((s) => ({ nodes: s.nodes.map((n) => ({ ...n, selected: n.id === selected })) }));
}

const bar = () => within(screen.getByTestId("make-bar"));

const option = (id: string) => bar().getByRole("option", { name: (_n, el) => (el as HTMLOptionElement).value === id }) as HTMLOptionElement;

const chip = (key: string) => bar().getAllByTestId("make-chip").find((c) => c.getAttribute("data-chip") === key);

async function enginesLoaded(): Promise<void> {
  await waitFor(() => expect(useMakeStore.getState().engines?.source).toBe("daemon"));
  await waitFor(() => expect(option("nano-banana-2.1")).toBeInTheDocument());
}

const pick = (id: string) => fireEvent.change(bar().getByTestId("make-engine"), { target: { value: id } });

/** The Make bar inside the canvas, which provides the node info (plate anchoring) a quote needs. */
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

// ---------------------------------------------------------------------------- chips and options

describe("capability chips and route health", () => {
  it("renders the chips of an engine with image_tail and reference_images_max 13", async () => {
    daemon();
    loadGraph();
    render(<MakeBar />);
    await enginesLoaded();
    expect(bar().queryByTestId("make-chips")).toBeNull(); // nothing picked: nothing to describe
    pick("nano-banana-2.1");
    const chips = bar().getByTestId("make-chips");
    expect(within(chips).getAllByTestId("make-chip").map((c) => c.getAttribute("data-chip"))).toEqual([
      "size",
      "end_frame",
      "refs",
      "elements",
      "price_unit",
      "unverified",
    ]);
    expect(chip("size")).toHaveTextContent("max 4096×4096");
    expect(chip("end_frame")).toHaveTextContent("end frame");
    expect(chip("refs")).toHaveTextContent("refs ≤ 13");
    expect(chip("price_unit")).toHaveTextContent("per image");
    expect(chip("unverified")).toHaveAttribute("data-tone", "amber");
  });

  it("a degraded route shows an amber chip and stays selectable", async () => {
    daemon();
    loadGraph();
    render(<MakeBar />);
    await enginesLoaded();
    expect(option("flux-3-image").disabled).toBe(false);
    pick("flux-3-image");
    const health = chip("health");
    expect(health).toHaveTextContent("degraded");
    expect(health).toHaveAttribute("data-tone", "amber");
    expect(health?.className).toContain("tone-amber");
    expect(health?.getAttribute("title")).toContain("bfl");
  });

  it("a runnable false option is disabled with the P0 title; a suspended one is disabled too", async () => {
    daemon();
    loadGraph();
    render(<MakeBar />);
    await enginesLoaded();
    const seedream = option("seedream-5-pro");
    expect(seedream.disabled).toBe(true);
    expect(seedream.title).toBe("model id unknown until the P0 smoke test");
    const gpt = option("gpt-image-2.5");
    expect(gpt.disabled).toBe(true);
    expect(gpt.title).toContain("suspended");
    expect(option("nano-banana-2.1").disabled).toBe(false);
    // a draft holding an engine that can no longer run is cleared
    useMakeStore.getState().setDraft("edit-1", { engineId: "seedream-5-pro" });
    await waitFor(() => expect(useMakeStore.getState().entry("edit-1").draft.engineId).toBeNull());
  });

  it("an engine card puts ≥ lower bound (n) · $mean in the label, bold while it steers", async () => {
    const calls = daemon({ cards: [CARD] });
    loadGraph("edit-1", { params: { task: "relight", shot_class: "wide" } });
    render(<MakeBar />);
    await enginesLoaded();
    await waitFor(() => expect(option("nano-banana-2.1").textContent).toBe("Nano Banana 2.1 · D · ≥ 70% (20) · $0.034"));
    expect(option("nano-banana-2.1")).toHaveAttribute("data-steering", "true");
    expect(option("nano-banana-2.1").style.fontWeight).toBe("700");
    expect(option("fake").textContent).toBe("Fake (echo) · C0");
    const asked = calls.find((c) => c.path === "/engines/cards");
    expect(asked?.search.get("project_id")).toBe("p1");
    expect(asked?.search.get("task")).toBe("relight");
    expect(asked?.search.get("shot_class")).toBe("wide");
    pick("nano-banana-2.1");
    expect(chip("card")?.querySelector("strong")).toHaveTextContent("≥ 70% (20) · $0.034");
    // one request per project, task and shot class
    expect(calls.filter((c) => c.path === "/engines/cards")).toHaveLength(1);
  });

  it("helpers: health over allowed routes, card choice, labels and formats", () => {
    const e = (routes: unknown[], c: Record<string, unknown> = {}) => ({ ...NANO, routes, capabilities: caps(c) }) as unknown as EngineSummary;
    expect(engineHealth(e([route("a", { health: "suspended" }), route("b", { health: "suspended" })]), null).status).toBe("suspended");
    expect(engineHealth(e([route("a", { health: "suspended" }), route("b")]), null).status).toBe("ok");
    expect(engineHealth(e([route("a", { health: "degraded" }), route("b")]), null)).toEqual({ status: "degraded", routes: ["a"] });
    // a route the NDA project may not use does not count
    const nda = { nda: true, allowed_grades: ["C0", "C1"] as ("C0" | "C1")[] };
    const mixed = e([route("cloud", { health: "suspended" }), route("local", { custody_grade: "C0", effective_grade: "C0" })]);
    expect(engineHealth(mixed, nda).status).toBe("ok");
    expect(unavailableReason(e([route("a")], { runnable: false }), null)).toBe("model id unknown until the P0 smoke test");
    expect(unavailableReason(e([route("a")]), null)).toBeNull();
    expect(cardFor([{ ...CARD, n: 5, steering: false }, { ...CARD, n: 3, steering: true }], "nano-banana-2.1")?.n).toBe(3);
    expect(cardFor([CARD], "fake")).toBeNull();
    expect(formatCard({ wilson_lower_95: 0.6989, n: 20, mean_cost: 1.2 })).toBe("≥ 70% (20) · $1.20");
    expect(formatCard({ wilson_lower_95: 0.2, n: 3, mean_cost: null })).toBe("≥ 20% (3)");
    expect(engineLabel(NANO as unknown as EngineSummary, "C1", null)).toBe("Nano Banana 2.1 · C1");
    expect(priceUnitText("per_second")).toBe("per second");
    expect(priceUnitText("flat")).toBe("flat price");
    expect(engineChips(FAKE as unknown as EngineSummary, null, null).map((c) => c.key)).toEqual(["price_unit"]);
    expect(formatTier("quality=high")).toBe("quality high");
    expect(formatTier("image_size=4K,audio=true")).toBe("image_size 4K · audio true");
    expect(formatTier(null)).toBeNull();
    expect(priceStatusTone("estimated")).toBe("amber");
    expect(priceStatusTone("unconfirmed")).toBe("dim");
    expect(worstGrade(["C0", "D", "C1"])).toBe("D");
  });
});

// ---------------------------------------------------------------------------- preset, tier

describe("preset select and the quote line", () => {
  it("an Edit node has no preset select and still sends preset default; a People node keeps it", async () => {
    const calls = daemon();
    loadGraph();
    const { unmount } = mountCanvas();
    await enginesLoaded();
    pick("nano-banana-2.1");
    expect(bar().queryByTestId("make-preset")).toBeNull();
    fireEvent.click(bar().getByTestId("make-quote"));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/quote"))).toBe(true));
    expect((calls.find((c) => c.path.endsWith("/quote"))?.body as { preset: string }).preset).toBe("default");
    unmount();
    useGraphStore.setState((s) => ({ nodes: s.nodes.map((n) => ({ ...n, selected: n.id === "people-1" })) }));
    render(<MakeBar />);
    await enginesLoaded();
    pick("nano-banana-2.1");
    const preset = bar().getByTestId("make-preset");
    expect(within(preset).getAllByRole("option").map((o) => (o as HTMLOptionElement).value)).toEqual(["default", "season_swap"]);
    // an engine that lists no preset but the default needs no select
    pick("fake");
    expect(bar().queryByTestId("make-preset")).toBeNull();
  });

  it("the quote line names the price tier and flags an estimated price amber", async () => {
    daemon({ quote: quote({ price_usd: 0.136, price_status: "estimated", price_tier: "image_size=4K" }) });
    loadGraph();
    mountCanvas();
    await enginesLoaded();
    pick("nano-banana-2.1");
    fireEvent.click(bar().getByTestId("make-quote"));
    await waitFor(() => expect(bar().getByTestId("quote-price")).toHaveTextContent("0.14 USD"));
    expect(bar().getByTestId("quote-tier")).toHaveTextContent("tier image_size 4K");
    const status = bar().getByTestId("quote-price-status");
    expect(status).toHaveTextContent("estimated");
    expect(status).toHaveAttribute("data-tone", "amber");
    expect(status.className).toContain("tone-amber");
  });
});

// ---------------------------------------------------------------------------- compare engines

const compareBox = (id: string) =>
  bar()
    .getAllByTestId("make-compare-engine")
    .find((b) => b.getAttribute("data-engine") === id) as HTMLInputElement;

describe("Compare engines…", () => {
  it("two engines post one quote_many with both ids; the strip shows both candidates with their engines", async () => {
    const calls = daemon();
    loadGraph();
    mountCanvas();
    await enginesLoaded();
    pick("nano-banana-2.1");
    fireEvent.click(bar().getByTestId("make-compare"));
    expect(bar().getByTestId("make-compare")).toHaveAttribute("aria-pressed", "true");
    expect(bar().queryByTestId("make-engine")).toBeNull();
    expect(bar().queryByTestId("make-n")).toBeNull(); // one take per engine
    // the engine picked before is the first compared; unavailable engines cannot be ticked
    expect(compareBox("nano-banana-2.1").checked).toBe(true);
    expect(compareBox("seedream-5-pro").disabled).toBe(true);
    expect(compareBox("gpt-image-2.5").disabled).toBe(true);
    expect(bar().getByTestId("make-quote")).toBeDisabled(); // one engine is not a comparison
    fireEvent.click(compareBox("fake"));
    expect(bar().getByTestId("make-quote")).toBeEnabled();
    fireEvent.click(bar().getByTestId("make-quote"));
    await waitFor(() => expect(bar().getByTestId("quote-line")).toHaveAttribute("data-group", "true"));
    const posted = calls.filter((c) => c.path === "/projects/p1/nodes/edit-1/quote_many");
    expect(posted).toHaveLength(1);
    expect(posted[0].body).toEqual({ engine_ids: ["nano-banana-2.1", "fake"], preset: "default", region_crop: false, params: {} });
    expect(calls.some((c) => c.path.endsWith("/quote"))).toBe(false);
    expect(bar().getByTestId("quote-price")).toHaveTextContent("0.034 USD (2 engines)");
    expect(bar().getAllByTestId("quote-group-engine").map((e) => e.getAttribute("data-engine"))).toEqual(["nano-banana-2.1", "fake"]);
    expect(bar().getByTestId("quote-custody")).toHaveAttribute("data-grade", "D"); // the least private of the group
    fireEvent.click(bar().getByTestId("make-make"));
    await waitFor(() => expect(screen.getAllByTestId("candidate")).toHaveLength(2));
    expect(calls.filter((c) => c.path === "/runs/run_a/approve")).toHaveLength(1); // one approval for the group
    expect(calls.find((c) => c.path === "/runs/run_a/approve")?.body).toEqual({ by: "human" });
    const rows = screen.getAllByTestId("candidate");
    expect(rows.map((r) => r.getAttribute("data-run"))).toEqual(["run_a", "run_b"]);
    expect(rows[0]).toHaveTextContent("nano-banana-2.1");
    expect(rows[1]).toHaveTextContent("fake");
  });

  it("at or over the cap the group dialog lists every engine and the group total", async () => {
    const calls = daemon({ groupPrices: [1.2, 1.3] });
    loadGraph();
    mountCanvas();
    await enginesLoaded();
    fireEvent.click(bar().getByTestId("make-compare"));
    fireEvent.click(compareBox("nano-banana-2.1"));
    fireEvent.click(compareBox("flux-3-image"));
    fireEvent.click(bar().getByTestId("make-quote"));
    await waitFor(() => expect(bar().getByTestId("make-make")).toBeEnabled());
    fireEvent.click(bar().getByTestId("make-make"));
    const dialog = await screen.findByTestId("approval-dialog");
    expect(dialog).toHaveAttribute("data-group", "true");
    expect(dialog).toHaveTextContent("Approve 2 engines");
    expect(within(dialog).getAllByTestId("approval-engine").map((r) => r.getAttribute("data-engine"))).toEqual([
      "nano-banana-2.1",
      "flux-3-image",
    ]);
    expect(within(dialog).getByTestId("approval-total")).toHaveTextContent("2.50 USD");
    expect(calls.some((c) => c.path.endsWith("/approve"))).toBe(false);
    fireEvent.click(within(dialog).getByTestId("approval-approve"));
    await waitFor(() => expect(calls.filter((c) => c.path === "/runs/run_a/approve")).toHaveLength(1));
  });

  it("caps the comparison at five engines and drops it when switched back to one engine", async () => {
    const many = ["a", "b", "c", "d", "e", "f"].map((x) => ({ ...FAKE, id: `eng-${x}`, name: `Engine ${x}` }));
    daemon({ engines: [NANO, ...many] });
    loadGraph();
    render(<MakeBar />);
    await enginesLoaded();
    fireEvent.click(bar().getByTestId("make-compare"));
    for (const x of ["a", "b", "c", "d", "e"]) fireEvent.click(compareBox(`eng-${x}`));
    expect(compareBox("eng-f").disabled).toBe(true);
    expect(bar().getByTestId("make-compare-count")).toHaveTextContent("5 picked");
    expect(compareReady(useMakeStore.getState().entry("edit-1").draft)).toBe(true);
    fireEvent.click(bar().getByTestId("make-compare"));
    expect(useMakeStore.getState().entry("edit-1").draft.compare).toBe(false);
    expect(bar().getByTestId("make-engine")).toBeInTheDocument();
  });

  it("quoteManyRequest carries the node's prompt fields and no engine_id or drawer params", () => {
    const body = quoteManyRequest(
      { engineId: "nano-banana-2.1", preset: "default", n: 3, regionCrop: true, params: { image_size: "4K" }, compare: true, compareIds: ["fake", "nano-banana-2.1", "fake"] },
      { recipe: "season_swap", task: "relight" },
    );
    expect(body).toEqual({ engine_ids: ["fake", "nano-banana-2.1"], preset: "default", region_crop: true, params: {}, recipe: "season_swap", task: "relight" });
    expect("engine_id" in body).toBe(false);
  });
});
