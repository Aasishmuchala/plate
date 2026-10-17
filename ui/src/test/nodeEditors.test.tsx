// T69 artist-path editors: the Plate packet picker, the Edit prompt/recipe editor, Export and Review, plus the Judge
// make mode, the palette without Colour / Compare, the Export / Review body hints and the daemon's fake engine in the
// fallback catalogue. The daemon is a fetch stub that answers by "METHOD /path" and records every call, so each test
// also pins the request the editor sent. No daemon, no GPU.

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { clearThumbCache } from "../canvas/assets";
import type { EventSourceLike } from "../api/client";
import { hub } from "../canvas/runEvents";
import type { BusRecord } from "../canvas/runTypes";
import { editorFor, PATH_EDITOR_KINDS, pathEditorFor } from "../editors";
import { EditEditor, promptSource } from "../editors/EditEditor";
import { ExportEditor, producingNodes } from "../editors/ExportEditor";
import { exportCandidates, parsePacket, parsePacketSummary, parseRecipe, parseRunItem } from "../editors/nodeApi";
import { filterPackets, linkFor, PlateEditor } from "../editors/PlateEditor";
import { ReviewEditor } from "../editors/ReviewEditor";
import { engineOptions, FAKE_ENGINE, FALLBACK_ENGINES, fallbackEngines, MAKE_MODES } from "../makebar/engines";
import { useMakeStore } from "../makebar/makeStore";
import { type LoadableNode, makeNodeData, NODE_KINDS } from "../nodeTypes";
import { KIND_SUMMARY } from "../nodes/kinds";
import PlateCanvas, { PALETTE_KINDS } from "../PlateCanvas";
import { createInitialGraph, useGraphStore } from "../store";

// ------------------------------------------------------------------------------------------------ the daemon stub

interface Call {
  method: string;
  path: string;
  search: string;
  body: unknown;
}

type Route = unknown | ((call: Call) => unknown | Promise<unknown>);

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** Answers `METHOD /path` (the /api/v0 prefix and the query string are stripped); anything else is a 404 and recorded.
 * The graph PUT echoes, so a store that autosaves (T68) never sees a failure the test did not ask for. */
function mockApi(routes: Record<string, Route> = {}): Call[] {
  const calls: Call[] = [];
  const all: Record<string, Route> = { "PUT /projects/p1/graph": (c: Call) => c.body, ...routes };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://127.0.0.1");
      const method = (init?.method ?? "GET").toUpperCase();
      const path = url.pathname.replace(/^\/api\/v0/, "");
      const raw = init?.body;
      const body = typeof raw === "string" ? JSON.parse(raw) : (raw ?? null);
      const call: Call = { method, path, search: url.search, body };
      calls.push(call);
      const route = all[`${method} ${path}`];
      if (route === undefined) return jsonResponse({ error: `no stub for ${method} ${path}` }, 404);
      const out = typeof route === "function" ? await (route as (c: Call) => unknown)(call) : route;
      return out instanceof Response ? out : jsonResponse(out);
    }),
  );
  return calls;
}

const called = (calls: Call[], method: string, path: string): Call[] => calls.filter((c) => c.method === method && c.path === path);

class FakeEventSource implements EventSourceLike {
  url: string;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  closed = false;
  constructor(url: string) {
    this.url = url;
  }
  addEventListener(): void {}
  close(): void {
    this.closed = true;
  }
  emit(rec: BusRecord): void {
    this.onmessage?.({ data: JSON.stringify(rec), lastEventId: String(rec.seq ?? "") } as MessageEvent);
  }
}

// ------------------------------------------------------------------------------------------------ fixtures

const ROOM_A_PLATE = "807554a6ed3dcfdb72eacc24d30c95a14d203dc65db98f76e2880eb560efb389";
const ROOM_B_PLATE = "b2".repeat(32);

/** Two packets as T55's `GET /projects/{pid}/packets` lists them (room_a superseded by room_a_v2). */
const PACKETS = [
  {
    packet_id: "pk_room_a",
    project_id: "p1",
    shot_id: "room_a",
    kind: "still",
    tier: "A",
    status: "ingested",
    received_utc: "2026-10-18T09:12:40Z",
    camera_name: "CAM_room_a",
    plate: { role: "display_tiff16", relpath: "display.tif", blake3: ROOM_A_PLATE, width: 640, height: 360 },
    supersedes: null,
    superseded_by: "pk_room_a_v2",
  },
  {
    packet_id: "pk_room_b",
    project_id: "p1",
    shot_id: "room_b",
    kind: "still",
    tier: "A",
    status: "ingested",
    received_utc: "2026-10-18T09:20:03Z",
    camera_name: "CAM_room_b",
    plate: { role: "display_tiff16", relpath: "display.tif", blake3: ROOM_B_PLATE, width: 640, height: 360 },
    supersedes: null,
    superseded_by: null,
  },
];

/** `GET /recipes` as T61 serves it: two still recipes and one motion recipe. */
const RECIPES = [
  {
    id: "night_relight_changelist",
    title: "Night relight, change-list edit",
    kind: "still_edit",
    engine: "nano-banana-2.1",
    shot_class: "wide",
    library: "a01",
    mask: { editable_classes: ["sky", "vegetation", "people"], region: "none", lock_rest: true },
    params: { n: 1, size: "native" },
  },
  {
    id: "sunburst_rephotograph_close",
    title: "Sunburst re-photograph, close shot",
    kind: "still_rephotograph",
    engine: "gpt-image-2.5",
    shot_class: "close",
    library: "a08",
    mask: { editable_classes: [], region: "none" },
    params: {},
  },
  { id: "croppair_pushin", title: "CropPair push-in", kind: "motion_croppair", engine: "kling-o3-4k", shot_class: "wide", mask: {}, params: {} },
];

/** A `run_view` (T57) with the fields the editors read. */
const runView = (id: string, state: string, extra: Record<string, unknown> = {}) => ({
  id,
  run_id: id,
  project_id: "p1",
  node_id: "edit-1",
  state,
  attempt: 1,
  route: { engine_id: "fake", provider: "local" },
  outputs: [`h_${id}`],
  created_utc: "2026-10-18T10:00:00Z",
  override: null,
  exportable: state === "DONE",
  ...extra,
});

const edge = (source: string, target: string, sourceHandle: string | null = null, targetHandle: string | null = null) => ({
  id: `${source}->${target}`,
  source,
  target,
  sourceHandle,
  targetHandle,
});

/** Plate -> Edit (edit-1) -> `kind` (n1), project p1 open. */
function openChain(kind: "Export" | "Review" | "Judge", extra: Partial<LoadableNode> = {}, id = "n1"): void {
  useGraphStore.getState().loadProject(
    {
      schema: 1,
      nodes: [
        { id: "plate-1", kind: "Plate", label: "Plate", packetId: "pk_room_a", tier: "A", outputHash: ROOM_A_PLATE, outputSize: { width: 640, height: 360 } },
        { id: "edit-1", kind: "Edit", label: "Edit" },
        { id, kind, label: kind, ...extra },
      ],
      edges: [edge("plate-1", "edit-1", "image", "image"), edge("edit-1", id, "image", kind === "Judge" ? "candidate" : "media")],
    },
    "p1",
  );
}

/** One node of `kind` (n1), alone, project p1 open. */
function openSingle(kind: "Plate" | "Edit", extra: Partial<LoadableNode> = {}): void {
  useGraphStore.getState().loadProject({ schema: 1, nodes: [{ id: "n1", kind, label: kind, ...extra }], edges: [] }, "p1");
}

const nodeOf = (id = "n1") => {
  const n = useGraphStore.getState().nodes.find((x) => x.id === id);
  if (!n) throw new Error(`no node ${id}`);
  return n;
};
const paramsOf = (id = "n1"): Record<string, unknown> => nodeOf(id).data.params ?? {};

const select = (id: string): void => useGraphStore.setState((s) => ({ nodes: s.nodes.map((n) => ({ ...n, selected: n.id === id })) }));

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  useMakeStore.getState().reset();
  clearThumbCache();
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ================================================================================================ pure

describe("nodeApi parsers", () => {
  it("reads a T55 packet row: id, shot, tier, camera, received time, plate by reference, supersession", () => {
    const p = parsePacket(PACKETS[0]);
    expect(p).toEqual({
      packetId: "pk_room_a",
      shotId: "room_a",
      tier: "A",
      kind: "still",
      status: "ingested",
      receivedUtc: "2026-10-18T09:12:40Z",
      camera: "CAM_room_a",
      plate: { role: "display_tiff16", relpath: "display.tif", blake3: ROOM_A_PLATE, width: 640, height: 360 },
      supersedes: null,
      supersededBy: "pk_room_a_v2",
    });
    expect(parsePacket({ shot_id: "no id" })).toBeNull();
    expect(parsePacket({ packet_id: "x", camera: { name: "CAM" } })?.camera).toBe("CAM");
  });

  it("links only a packet whose plate has a hash and a size", () => {
    const ok = parsePacket(PACKETS[1]);
    if (!ok) throw new Error("no packet");
    expect(linkFor(ok)).toEqual({ packetId: "pk_room_b", tier: "A", outputHash: ROOM_B_PLATE, outputSize: { width: 640, height: 360 } });
    const noPlate = parsePacket({ packet_id: "x", tier: "C" });
    if (!noPlate) throw new Error("no packet");
    expect(typeof linkFor(noPlate)).toBe("string");
    const noSize = parsePacket({ packet_id: "y", plate: { blake3: "abc" } });
    if (!noSize) throw new Error("no packet");
    expect(linkFor(noSize)).toBe("the plate file has no size in the manifest");
  });

  it("filters packets by shot, camera, id or tier, ignoring case", () => {
    const list = PACKETS.flatMap((p) => parsePacket(p) ?? []);
    expect(filterPackets(list, "").map((p) => p.packetId)).toEqual(["pk_room_a", "pk_room_b"]);
    expect(filterPackets(list, "ROOM_B").map((p) => p.packetId)).toEqual(["pk_room_b"]);
    expect(filterPackets(list, "cam_room_a").map((p) => p.packetId)).toEqual(["pk_room_a"]);
    expect(filterPackets(list, "tier c")).toEqual([]);
  });

  it("reads a packet summary's legend with editable / locked / planar", () => {
    const s = parsePacketSummary({
      packet_id: "pk_room_a",
      size: { width: 640, height: 360 },
      camera: { name: "CAM_room_a" },
      legend: {
        entries: [
          { id: "o:1a2b3c4d", kind: "object", name: "VEG_shrub_A", class: "vegetation", pixel_share: 0.04, editable: true, locked: false },
          { id: "o:5e6f7a8b", kind: "object", name: "WALL_north", class: "architecture", pixel_share: 0.3, editable: false, locked: true, planar: true },
        ],
      },
    });
    expect(s.size).toEqual({ width: 640, height: 360 });
    expect(s.cameraName).toBe("CAM_room_a");
    expect(s.legend.map((e) => [e.name, e.editable, e.locked, e.planar])).toEqual([
      ["VEG_shrub_A", true, false, false],
      ["WALL_north", false, true, true],
    ]);
  });

  it("reads recipes with their mask classes", () => {
    expect(parseRecipe(RECIPES[0])).toMatchObject({ id: "night_relight_changelist", kind: "still_edit", engine: "nano-banana-2.1", maskClasses: ["sky", "vegetation", "people"], region: "none" });
    expect(parseRecipe({ title: "no id" })).toBeNull();
  });

  it("derives exportable when a daemon omits it, and orders candidates DONE, overridden, GATE_FAIL", () => {
    expect(parseRunItem({ id: "a", state: "GATE_FAIL", override: { reason: "client asked for it", by: "human" } })).toMatchObject({ exportable: true, overrideReason: "client asked for it" });
    expect(parseRunItem({ id: "b", state: "GATE_FAIL" })).toMatchObject({ exportable: false, overrideReason: null });
    expect(parseRunItem({ id: "c", state: "DONE", exportable: false })?.exportable).toBe(false);
    const runs = [
      runView("f1", "GATE_FAIL"),
      runView("o1", "GATE_FAIL", { override: { reason: "accepted look", by: "human" }, exportable: true }),
      runView("x1", "FAILED"),
      runView("d1", "DONE"),
      runView("q1", "RUNNING"),
    ].flatMap((r) => parseRunItem(r) ?? []);
    expect(exportCandidates(runs).map((r) => r.id)).toEqual(["d1", "o1", "f1"]);
  });

  it("finds the producing node upstream, passing through Comp, Gate and Mask", () => {
    const { nodes, edges } = {
      nodes: [
        { id: "p", data: makeNodeData("Plate") },
        { id: "m", data: makeNodeData("Mask") },
        { id: "e", data: makeNodeData("Edit") },
        { id: "c", data: makeNodeData("Comp") },
        { id: "g", data: makeNodeData("Gate") },
        { id: "x", data: makeNodeData("Export") },
      ] as never,
      edges: [edge("p", "m"), edge("m", "e"), edge("p", "e"), edge("e", "c"), edge("c", "g"), edge("g", "x")],
    };
    expect(producingNodes(nodes, edges, "x")).toEqual(["e"]);
    expect(producingNodes(nodes, edges, "e")).toEqual([]);
  });

  it("names the prompt source: explicit beats recipe", () => {
    expect(promptSource({})).toBe("none");
    expect(promptSource({ recipe: "night_relight_changelist" })).toBe("recipe");
    expect(promptSource({ recipe: "night_relight_changelist", prompt: "dusk" })).toBe("explicit");
    expect(promptSource({ prompt: "   " })).toBe("none");
  });
});

// ================================================================================================ Plate

describe("PlateEditor", () => {
  it("lists the project's packets and selecting one links the node: packetId, tier, outputHash, outputSize", async () => {
    const calls = mockApi({ "GET /projects/p1/packets": PACKETS });
    openSingle("Plate");
    render(<PlateEditor nodeId="n1" />);
    expect(screen.getByTestId("plate-unlinked")).toHaveTextContent("no packet linked");
    expect(screen.getByTestId("plate-unlinked")).toHaveAttribute("data-tone", "advisory");
    const list = await screen.findByTestId("plate-list");
    expect(within(list).getAllByRole("button")).toHaveLength(2);
    const a = screen.getByTestId("plate-packet-pk_room_a");
    expect(a).toHaveTextContent("room_a");
    expect(a).toHaveTextContent("Tier A");
    expect(a).toHaveTextContent("CAM_room_a");
    expect(a).toHaveTextContent("received 2026-10-18 09:12 UTC");
    expect(a).toHaveAttribute("data-superseded", "true");
    expect(a).toHaveTextContent("superseded");
    expect(screen.getByTestId("plate-packet-pk_room_b")).toHaveAttribute("data-superseded", "false");
    // a thumbnail of each display plate, by reference, with the token
    await waitFor(() => expect(called(calls, "GET", `/assets/${ROOM_B_PLATE}`).map((c) => c.search)).toEqual(["?fmt=thumb"]));

    fireEvent.click(screen.getByTestId("plate-packet-pk_room_b"));
    expect(nodeOf().data).toMatchObject({ packetId: "pk_room_b", tier: "A", outputHash: ROOM_B_PLATE, outputSize: { width: 640, height: 360 } });
    expect(screen.getByTestId("plate-linked")).toHaveTextContent("packet pk_room_b · Tier A");
    expect(screen.queryByTestId("plate-unlinked")).toBeNull();
    expect(screen.getByTestId("plate-packet-pk_room_b")).toHaveAttribute("aria-pressed", "true");
    // the linked packet's summary is read
    await waitFor(() => expect(called(calls, "GET", "/packets/pk_room_b/summary")).toHaveLength(1));
  });

  it("filters with the search box and re-reads on Refresh", async () => {
    const calls = mockApi({ "GET /projects/p1/packets": PACKETS });
    openSingle("Plate");
    render(<PlateEditor nodeId="n1" />);
    await screen.findByTestId("plate-list");
    fireEvent.change(screen.getByTestId("plate-search"), { target: { value: "room_b" } });
    expect(screen.queryByTestId("plate-packet-pk_room_a")).toBeNull();
    expect(screen.getByTestId("plate-packet-pk_room_b")).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("plate-search"), { target: { value: "nothing like it" } });
    expect(screen.getByTestId("plate-empty")).toHaveTextContent("no packet matches the search");
    fireEvent.click(screen.getByTestId("plate-refresh"));
    await waitFor(() => expect(called(calls, "GET", "/projects/p1/packets")).toHaveLength(2));
  });

  it("shows the linked packet's legend counts", async () => {
    mockApi({
      "GET /projects/p1/packets": PACKETS,
      "GET /packets/pk_room_a/summary": {
        packet_id: "pk_room_a",
        size: { width: 640, height: 360 },
        camera: { name: "CAM_room_a" },
        legend: { entries: [{ id: "o:1", name: "VEG", editable: true }, { id: "o:2", name: "WALL", locked: true }, { id: "o:3", name: "FLOOR", locked: true }] },
      },
    });
    openSingle("Plate", { packetId: "pk_room_a", tier: "A" });
    render(<PlateEditor nodeId="n1" />);
    expect(await screen.findByTestId("plate-summary")).toHaveTextContent("3 ids · 1 editable · 2 locked");
    expect(screen.getByTestId("plate-summary")).toHaveTextContent("camera CAM_room_a");
    // room_a was re-rendered: the link says so
    expect(await screen.findByTestId("plate-stale")).toHaveTextContent("superseded by pk_room_a_v2");
  });

  it("re-reads the list when the event hub routes a packet.ingested record for the project", async () => {
    const calls = mockApi({ "GET /projects/p1/packets": PACKETS });
    const sources: FakeEventSource[] = [];
    hub.setFactory((url) => {
      const es = new FakeEventSource(url);
      sources.push(es);
      return es;
    });
    const stop = hub.follow("p1"); // App follows the open project
    try {
      openSingle("Plate");
      const view = render(<PlateEditor nodeId="n1" />);
      await screen.findByTestId("plate-list");
      expect(sources).toHaveLength(1);
      act(() => sources[0].emit({ seq: 1, kind: "packet.ingested", project_id: "other" }));
      act(() => sources[0].emit({ seq: 2, kind: "run.state", project_id: "p1", run_id: "r1", data: { to: "RUNNING" } }));
      expect(called(calls, "GET", "/projects/p1/packets")).toHaveLength(1);
      act(() => sources[0].emit({ seq: 3, kind: "packet.ingested", project_id: "p1", data: { packet_id: "pk_new" } }));
      await waitFor(() => expect(called(calls, "GET", "/projects/p1/packets")).toHaveLength(2));
      act(() => sources[0].emit({ seq: 4, kind: "packet.superseded", project_id: "p1", data: { packet_id: "pk_room_b" } }));
      await waitFor(() => expect(called(calls, "GET", "/projects/p1/packets")).toHaveLength(3));
      view.unmount();
      act(() => sources[0].emit({ seq: 5, kind: "packet.ingested", project_id: "p1" }));
      expect(called(calls, "GET", "/projects/p1/packets")).toHaveLength(3); // unsubscribed on unmount
    } finally {
      stop();
      hub.closeAll();
    }
  });

  it("says so when the daemon has no packets route", async () => {
    mockApi({});
    openSingle("Plate");
    render(<PlateEditor nodeId="n1" />);
    expect(await screen.findByTestId("plate-error")).toHaveTextContent("no stub for GET /projects/p1/packets");
    expect(screen.getByTestId("plate-refresh")).toBeEnabled();
  });
});

// ================================================================================================ Edit

describe("EditEditor", () => {
  it("offers the still recipes, and picking one writes params.recipe, shows its mask classes and fills the Make bar engine", async () => {
    mockApi({ "GET /recipes": RECIPES });
    openSingle("Edit");
    render(<EditEditor nodeId="n1" />);
    const select = screen.getByTestId("edit-recipe");
    await waitFor(() => expect(within(select).getAllByRole("option").map((o) => (o as HTMLOptionElement).value)).toEqual(["", "night_relight_changelist", "sunburst_rephotograph_close"]));
    expect(within(select).getByText("Night relight, change-list edit · nano-banana-2.1")).toBeInTheDocument();
    expect(screen.getByTestId("edit-prompt-source")).toHaveTextContent("prompt source: none");

    fireEvent.change(select, { target: { value: "night_relight_changelist" } });
    expect(paramsOf()).toEqual({ recipe: "night_relight_changelist" });
    expect(screen.getByTestId("edit-recipe-classes")).toHaveTextContent("sky");
    expect(screen.getByTestId("edit-recipe-classes")).toHaveTextContent("vegetation");
    expect(screen.getByTestId("edit-recipe-classes")).toHaveTextContent("people");
    expect(screen.getByTestId("edit-prompt-source")).toHaveTextContent("prompt source: recipe");
    expect(useMakeStore.getState().entry("n1").draft.engineId).toBe("nano-banana-2.1");

    fireEvent.change(screen.getByTestId("edit-prompt"), { target: { value: "late blue hour, fixtures on" } });
    expect(paramsOf()).toEqual({ recipe: "night_relight_changelist", prompt: "late blue hour, fixtures on" });
    expect(screen.getByTestId("edit-prompt-source")).toHaveTextContent("prompt source: explicit");
    expect(screen.getByTestId("edit-prompt-source")).toHaveAttribute("data-source", "explicit");

    // clearing the prompt falls back to the recipe; whitespace alone is no prompt
    fireEvent.change(screen.getByTestId("edit-prompt"), { target: { value: "  " } });
    expect(paramsOf()).toEqual({ recipe: "night_relight_changelist" });
    expect(screen.getByTestId("edit-prompt-source")).toHaveTextContent("prompt source: recipe");
  });

  it("keeps an engine the artist already picked in the Make bar", async () => {
    mockApi({ "GET /recipes": RECIPES });
    openSingle("Edit");
    useMakeStore.getState().setDraft("n1", { engineId: "qwen-image-edit-2509" });
    render(<EditEditor nodeId="n1" />);
    await waitFor(() => expect(within(screen.getByTestId("edit-recipe")).getAllByRole("option")).toHaveLength(3));
    fireEvent.change(screen.getByTestId("edit-recipe"), { target: { value: "sunburst_rephotograph_close" } });
    expect(paramsOf().recipe).toBe("sunburst_rephotograph_close");
    expect(useMakeStore.getState().entry("n1").draft.engineId).toBe("qwen-image-edit-2509");
    expect(screen.getByTestId("edit-recipe-classes")).toHaveTextContent("whole frame");
  });

  it("writes task and shot_class, and unsets them with the empty choice", async () => {
    mockApi({ "GET /recipes": RECIPES });
    openSingle("Edit");
    render(<EditEditor nodeId="n1" />);
    fireEvent.change(screen.getByTestId("edit-task"), { target: { value: "relight" } });
    fireEvent.change(screen.getByTestId("edit-shot-class"), { target: { value: "close" } });
    expect(paramsOf()).toEqual({ task: "relight", shot_class: "close" });
    expect(within(screen.getByTestId("edit-task")).getAllByRole("option").map((o) => (o as HTMLOptionElement).value)).toEqual(["", "relight", "people", "vegetation", "sky", "rephotograph_close"]);
    expect(within(screen.getByTestId("edit-shot-class")).getAllByRole("option").map((o) => (o as HTMLOptionElement).value)).toEqual(["", "wide", "medium", "close"]);
    fireEvent.change(screen.getByTestId("edit-task"), { target: { value: "" } });
    expect(paramsOf()).toEqual({ shot_class: "close" });
    await waitFor(() => expect(within(screen.getByTestId("edit-recipe")).getAllByRole("option")).toHaveLength(3));
  });

  it("shows a recipe the daemon no longer has instead of dropping it", async () => {
    mockApi({ "GET /recipes": RECIPES });
    openSingle("Edit", { params: { recipe: "retired_recipe" } });
    render(<EditEditor nodeId="n1" />);
    await waitFor(() => expect(within(screen.getByTestId("edit-recipe")).getByText("retired_recipe (not on this daemon)")).toBeInTheDocument());
    expect(screen.getByTestId("edit-recipe")).toHaveValue("retired_recipe");
  });
});

// ================================================================================================ Export

describe("ExportEditor", () => {
  it("lists the upstream node's runs, posts /runs/r1/export with format png16 and shows the path", async () => {
    const calls = mockApi({
      "GET /projects/p1/runs": (c: Call) => (c.search === "?node_id=edit-1" ? [runView("r9", "RUNNING"), runView("r1", "DONE")] : []),
      "POST /runs/r1/export": { run_id: "r1", path: "/Users/artist/out/room_a_night.png", sidecar: "/Users/artist/out/room_a_night.gate.json", format: "png16", shipped: true },
    });
    openChain("Export");
    render(<ExportEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("export-candidate")).toHaveValue("r1"));
    expect(within(screen.getByTestId("export-candidate")).getAllByRole("option")).toHaveLength(1); // a RUNNING take is not a candidate
    expect(screen.getByTestId("export-state")).toHaveTextContent("DONE");
    expect(screen.queryByTestId("export-reason")).toBeNull();
    expect(screen.getByTestId("export-outdir")).toHaveAttribute("placeholder", "absolute folder on the daemon's machine");
    expect(screen.getByTestId("export-go")).toBeDisabled(); // no folder yet

    fireEvent.change(screen.getByTestId("export-format"), { target: { value: "tiff16" } });
    fireEvent.change(screen.getByTestId("export-format"), { target: { value: "png16" } });
    fireEvent.change(screen.getByTestId("export-outdir"), { target: { value: "/Users/artist/out" } });
    fireEvent.change(screen.getByTestId("export-name"), { target: { value: "room_a_night" } });
    expect(screen.getByTestId("export-go")).toBeEnabled();
    fireEvent.click(screen.getByTestId("export-go"));

    expect(await screen.findByTestId("export-path")).toHaveTextContent("/Users/artist/out/room_a_night.png");
    expect(screen.getByTestId("export-sidecar")).toHaveTextContent("/Users/artist/out/room_a_night.gate.json");
    const post = called(calls, "POST", "/runs/r1/export");
    expect(post).toHaveLength(1);
    expect(post[0].body).toEqual({ out_dir: "/Users/artist/out", format: "png16", name: "room_a_night" });
    expect(paramsOf()).toMatchObject({ format: "png16", outDir: "/Users/artist/out", name: "room_a_night", exported: 1, lastPath: "/Users/artist/out/room_a_night.png" });
    expect(called(calls, "GET", "/projects/p1/runs").length).toBeGreaterThanOrEqual(2); // re-read after the export
  });

  it("a GATE_FAIL candidate needs an override reason of 10 characters before Export enables; the reason is posted", async () => {
    const calls = mockApi({
      "GET /projects/p1/runs": [runView("r1", "GATE_FAIL", { exportable: false })],
      "POST /runs/r1/export": { run_id: "r1", path: "C:\\Plate\\out\\r1.tif", sidecar: "C:\\Plate\\out\\r1.gate.json", format: "tiff16", shipped: true },
    });
    openChain("Export", { params: { outDir: "C:\\Plate\\out", format: "tiff16" } });
    render(<ExportEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("export-candidate")).toHaveValue("r1"));
    expect(screen.getByTestId("export-state")).toHaveTextContent("GATE_FAIL");
    const reason = screen.getByTestId("export-reason");
    expect(screen.getByTestId("export-go")).toBeDisabled();
    fireEvent.change(reason, { target: { value: "too short" } }); // 9 characters
    expect(screen.getByTestId("export-go")).toBeDisabled();
    fireEvent.change(reason, { target: { value: "client approved the look" } });
    expect(screen.getByTestId("export-go")).toBeEnabled();
    fireEvent.click(screen.getByTestId("export-go"));
    expect(await screen.findByTestId("export-path")).toHaveTextContent("C:\\Plate\\out\\r1.tif");
    expect(called(calls, "POST", "/runs/r1/export")[0].body).toEqual({ out_dir: "C:\\Plate\\out", format: "tiff16", override: true, reason: "client approved the look" });
  });

  it("an overridden GATE_FAIL exports without a new reason; a daemon refusal shows", async () => {
    const calls = mockApi({
      "GET /projects/p1/runs": [runView("r2", "GATE_FAIL", { exportable: true, override: { reason: "accepted look", by: "human" } })],
      "POST /runs/r2/export": () => jsonResponse({ error: "not_exportable", detail: "run r2 has no output" }, 409),
    });
    openChain("Export", { params: { outDir: "/out" } });
    render(<ExportEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("export-candidate")).toHaveValue("r2"));
    expect(screen.queryByTestId("export-reason")).toBeNull();
    fireEvent.click(screen.getByTestId("export-go"));
    expect(await screen.findByTestId("export-error")).toHaveTextContent("run r2 has no output");
    expect(called(calls, "POST", "/runs/r2/export")[0].body).toEqual({ out_dir: "/out", format: "png16" });
    expect(paramsOf().exported).toBeUndefined();
  });

  it("without an upstream maker it asks for one", () => {
    mockApi({});
    useGraphStore.getState().loadProject({ schema: 1, nodes: [{ id: "n1", kind: "Export", label: "Export" }], edges: [] }, "p1");
    render(<ExportEditor nodeId="n1" />);
    expect(screen.getByTestId("export-nosource")).toBeInTheDocument();
    expect(screen.getByTestId("export-go")).toBeDisabled();
  });
});

// ================================================================================================ Review

describe("ReviewEditor", () => {
  it("offers DONE and overridden takes, posts run_ids with the watermark and shows the zip and its size", async () => {
    const calls = mockApi({
      "GET /projects/p1/runs": [
        runView("r1", "DONE"),
        runView("r2", "GATE_FAIL", { exportable: true, override: { reason: "accepted look", by: "human" } }),
        runView("r3", "GATE_FAIL", { exportable: false }),
      ],
      "POST /projects/p1/review": { zip_path: "/plate/projects/p1/review/review_20261018.zip", bytes: 3_250_585, takes: 2 },
    });
    openChain("Review");
    render(<ReviewEditor nodeId="n1" />);
    await screen.findByTestId("review-run-r1");
    expect(screen.getByTestId("review-run-r2")).toBeInTheDocument();
    expect(screen.queryByTestId("review-run-r3")).toBeNull();
    expect(screen.getByTestId("review-failing")).toHaveTextContent("1 GATE_FAIL take need an override before review");
    expect(screen.getByTestId("review-watermark")).toHaveValue("REVIEW");
    expect(screen.getByTestId("review-build")).toBeDisabled();

    fireEvent.click(screen.getByTestId("review-run-r1"));
    fireEvent.click(screen.getByTestId("review-run-r2"));
    fireEvent.change(screen.getByTestId("review-title"), { target: { value: "Room A, night" } });
    fireEvent.click(screen.getByTestId("review-build"));

    expect(await screen.findByTestId("review-zip")).toHaveTextContent("/plate/projects/p1/review/review_20261018.zip");
    expect(screen.getByTestId("review-size")).toHaveTextContent("3.1 MB");
    const post = called(calls, "POST", "/projects/p1/review");
    expect(post).toHaveLength(1);
    expect(post[0].body).toEqual({ run_ids: ["r1", "r2"], watermark: "REVIEW", title: "Room A, night" });
    expect(paramsOf()).toMatchObject({ bundleTakes: 2, bundlePath: "/plate/projects/p1/review/review_20261018.zip", title: "Room A, night" });
  });

  it("unticking a take drops it from the bundle; a refusal shows", async () => {
    const calls = mockApi({
      "GET /projects/p1/runs": [runView("r1", "DONE"), runView("r4", "DONE")],
      "POST /projects/p1/review": () => jsonResponse({ error: "previs", detail: "previs takes never reach review" }, 409),
    });
    openChain("Review", { params: { watermark: "CLIENT DRAFT" } });
    render(<ReviewEditor nodeId="n1" />);
    await screen.findByTestId("review-run-r4");
    fireEvent.click(screen.getByTestId("review-run-r1"));
    fireEvent.click(screen.getByTestId("review-run-r4"));
    fireEvent.click(screen.getByTestId("review-run-r1"));
    fireEvent.click(screen.getByTestId("review-build"));
    expect(await screen.findByTestId("review-error")).toHaveTextContent("previs takes never reach review");
    expect(called(calls, "POST", "/projects/p1/review")[0].body).toEqual({ run_ids: ["r4"], watermark: "CLIENT DRAFT" });
  });
});

// ================================================================================================ canvas

describe("palette, Judge, body hints, registry", () => {
  const mountCanvas = () =>
    render(
      <ReactFlowProvider>
        <PlateCanvas />
      </ReactFlowProvider>,
    );

  it("the palette has no Colour and no Compare button; the kinds stay loadable", () => {
    mockApi({});
    mountCanvas();
    const palette = screen.getByTestId("add-palette");
    expect(within(palette).queryByTestId("add-Colour")).toBeNull();
    expect(within(palette).queryByTestId("add-Compare")).toBeNull();
    for (const k of ["Plate", "Edit", "Mask", "Recolour", "LightMix", "Set", "Judge", "Export", "Review", "Concept"]) {
      expect(within(palette).getByTestId(`add-${k}`)).toBeInTheDocument();
    }
    expect(PALETTE_KINDS).toHaveLength(NODE_KINDS.length - 2);
    expect(NODE_KINDS).toContain("Colour");
    expect(NODE_KINDS).toContain("Compare");
    // a saved graph with the hidden kinds still loads and renders
    act(() =>
      useGraphStore.getState().loadProject({
        schema: 1,
        nodes: [
          { id: "c", kind: "Colour", label: "Colour" },
          { id: "k", kind: "Compare", label: "Compare" },
        ],
        edges: [],
      }),
    );
    expect(screen.getByTestId("node-Colour")).toBeInTheDocument();
    expect(screen.getByTestId("node-Compare")).toBeInTheDocument();
  });

  it("a Judge node is quoted in-process: the Make bar shows Quote and posts the node's quote", async () => {
    const quote = {
      id: "qt_j",
      run_id: "rj",
      provider: "anthropic",
      model: "claude-opus-5-5",
      custody_grade: "D",
      price_usd: 0.42,
      eta_s: 30,
      created_utc: new Date().toISOString(),
      expires_utc: new Date(Date.now() + 600_000).toISOString(),
    };
    const calls = mockApi({
      "GET /projects": [{ project_id: "p1", custody: { nda: false, allowed_grades: ["C0", "C1", "C2", "D"] } }],
      "POST /projects/p1/nodes/judge-1/quote": { run: { id: "rj", state: "QUOTED", node_id: "judge-1", quote }, quote },
    });
    expect(MAKE_MODES.Judge).toEqual({ mode: "inprocess" });
    openChain("Judge", {}, "judge-1");
    select("judge-1");
    mountCanvas();
    const bar = within(screen.getByTestId("make-bar"));
    expect(bar.getByTestId("make-target")).toHaveTextContent("Judge");
    const q = bar.getByTestId("make-quote");
    expect(q).toHaveTextContent("Quote");
    expect(q).toBeEnabled();
    expect(bar.queryByTestId("make-engine")).toBeNull(); // no engine to pick: the judge's route is the daemon's
    fireEvent.click(q);
    await waitFor(() => expect(called(calls, "POST", "/projects/p1/nodes/judge-1/quote")).toHaveLength(1));
    expect(called(calls, "POST", "/projects/p1/nodes/judge-1/quote")[0].body).toMatchObject({ engine_id: null });
    expect(await bar.findByTestId("quote-provider")).toHaveTextContent("anthropic");
  });

  it("Export and Review bodies read their params", () => {
    expect(KIND_SUMMARY.Export(makeNodeData("Export"))).toBe("png16 · 0 exported");
    expect(KIND_SUMMARY.Export(makeNodeData("Export", { params: { format: "tiff16", exported: 2 } }))).toBe("tiff16 · 2 exported");
    expect(KIND_SUMMARY.Review(makeNodeData("Review"))).toBe("static review bundle");
    expect(KIND_SUMMARY.Review(makeNodeData("Review", { params: { bundleTakes: 3 } }))).toBe("3 in last bundle");
  });

  it("the path editors are their own registry; the T47 registry is unchanged", () => {
    expect([...PATH_EDITOR_KINDS].sort()).toEqual(["Edit", "Export", "Plate", "Review"]);
    for (const kind of NODE_KINDS) {
      const inPath = (PATH_EDITOR_KINDS as readonly string[]).includes(kind);
      expect(pathEditorFor(kind) !== null).toBe(inPath);
      if (inPath) expect(editorFor(kind)).toBeNull();
    }
  });

  it("each path editor renders for a node that is not in the graph without throwing", () => {
    mockApi({});
    for (const kind of PATH_EDITOR_KINDS) {
      const Editor = pathEditorFor(kind);
      if (!Editor) throw new Error(`no ${kind} editor`);
      const { unmount } = render(<Editor nodeId="missing" />);
      expect(screen.getByTestId(`editor-${kind.toLowerCase()}`)).toHaveAttribute("data-node", "missing");
      unmount();
    }
  });

  it("App's sidebar mounts the path editor of the selected node and swaps it with the selection", async () => {
    mockApi({ "GET /projects/p1/packets": PACKETS, "GET /recipes": RECIPES });
    openChain("Export");
    render(
      <ReactFlowProvider>
        <App />
      </ReactFlowProvider>,
    );
    expect(screen.queryByTestId("editor-plate")).toBeNull();
    act(() => select("plate-1"));
    expect(await screen.findByTestId("editor-plate")).toHaveAttribute("data-node", "plate-1");
    act(() => select("edit-1"));
    expect(await screen.findByTestId("editor-edit")).toHaveAttribute("data-node", "edit-1");
    expect(screen.queryByTestId("editor-plate")).toBeNull();
    act(() => select("n1"));
    expect(await screen.findByTestId("editor-export")).toHaveAttribute("data-node", "n1");
    expect(screen.getByTestId("make-target")).toHaveTextContent("n1");
  });
});

// ================================================================================================ fake engine

describe("the daemon's fake engine", () => {
  it("is listed exactly as T61 serves it, and only next to the build catalogue of a stub daemon", () => {
    expect(FAKE_ENGINE).toEqual({
      id: "fake",
      name: "Fake (echo)",
      kind: "image_edit",
      allowed_tasks: ["relight", "people", "vegetation", "sky", "rephotograph_close"],
      routes: [{ route_id: "local", provider: "local", custody_grade: "C0", custody_status: "confirmed", effective_grade: "C0", status: "active", upload_visibility: "local" }],
      params: [],
      presets: ["default"],
      price: { usd: 0, model: "flat", status: "confirmed" },
    });
    expect(FALLBACK_ENGINES.some((e) => e.id === "fake")).toBe(false); // the catalogue mirrors the manifests one to one
    expect(fallbackEngines(false).map((e) => e.id)).toEqual(FALLBACK_ENGINES.map((e) => e.id));
    expect(fallbackEngines(true).map((e) => e.id)).toContain("fake");
    const nda = { nda: true, allowed_grades: ["C0", "C1"] } as const;
    expect(engineOptions(fallbackEngines(true), "Edit", { ...nda, allowed_grades: [...nda.allowed_grades] }).map((e) => e.id)).toEqual(["qwen-image-edit-2509", "fake"]);
    expect(engineOptions(fallbackEngines(true), "Motion", null).map((e) => e.id)).not.toContain("fake");
  });

  it("a stub daemon without /engines offers the fake engine in the Make bar", async () => {
    mockApi({
      "GET /health": { ok: true, daemon_version: "0.1.0", stub_engine: true },
      "GET /projects": [{ project_id: "p1", custody: { nda: false, allowed_grades: ["C0", "C1", "C2", "D"] } }],
    });
    openChain("Export");
    select("edit-1");
    render(
      <ReactFlowProvider>
        <PlateCanvas />
      </ReactFlowProvider>,
    );
    const values = () =>
      within(screen.getByTestId("make-engine"))
        .getAllByRole("option")
        .map((o) => (o as HTMLOptionElement).value);
    await waitFor(() => expect(values()).toContain("fake"));
    expect(values()).toContain("nano-banana-2.1");
    expect(useMakeStore.getState().engines?.source).toBe("fallback");
  });
});
