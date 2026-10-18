// T102 panels: the Project panel's Project Sheet (14 fields in the daemon's order, defaults as placeholders, one PUT
// with auto carry-over) and `Apply template…` (template, packets defaulting to those with no Plate on the canvas,
// engine, recipe, quote and auto-approve; the stored graph is loaded back), the Spend tab (a row per engine and week,
// All projects) and the Settings tab (session and health facts, key env var names only, the Help link). The daemon
// is a fetch stub answering by "METHOD /path" that records every call.

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useMakeStore } from "../makebar/makeStore";
import { Panels } from "../panels";
import { keyNames } from "../panels/api";
import { applySummary, ProjectPanel, sheetBody, sheetFormValues } from "../panels/ProjectPanel";
import { describe as describeValue } from "../panels/SettingsPanel";
import { orderSpend } from "../panels/SpendPanel";
import { initialPanelsState, usePanelsStore } from "../panels/store";
import { createInitialGraph, useGraphStore } from "../store";

// ------------------------------------------------------------------------------------------------ the daemon stub

interface Call {
  method: string;
  path: string;
  search: URLSearchParams;
  body: unknown;
}

type Route = unknown | ((call: Call) => unknown);

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function mockApi(routes: Record<string, Route>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://127.0.0.1");
      const method = (init?.method ?? "GET").toUpperCase();
      const path = url.pathname.replace(/^\/api\/v0/, "");
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      const call: Call = { method, path, search: url.searchParams, body };
      calls.push(call);
      const route = routes[`${method} ${path}`];
      if (route === undefined) return jsonResponse({ error: `no stub for ${method} ${path}` }, 404);
      const out = typeof route === "function" ? (route as (c: Call) => unknown)(call) : route;
      return out instanceof Response ? out : jsonResponse(out);
    }),
  );
  return calls;
}

const sent = (calls: Call[], method: string, path: string): Call[] => calls.filter((c) => c.method === method && c.path === path);

// ------------------------------------------------------------------------------------------------ fixtures

const PROJECT = { project_id: "p1", created_utc: "2026-10-18T09:00:00Z", custody: { nda: false, allowed_grades: ["C0", "C1", "C2", "D"] }, engine_pins: {} };

const FIELDS = [
  ["name", ""],
  ["location", ""],
  ["format", "4K architectural film, 16:9"],
  ["light_logic", ""],
  ["palette_dominant", ""],
  ["palette_secondary", ""],
  ["palette_accent", ""],
  ["lens_character", ""],
  ["depth_register", "deep legible for exteriors"],
  ["species", ""],
  ["materials", ""],
  ["climate", ""],
  ["audio_bed", ""],
  ["scale_note", "door height 2.1m"],
] as const;

function settings(sheet: Record<string, string> = {}, carry = true) {
  return {
    project_sheet: Object.fromEntries(FIELDS.map(([k, d]) => [k, sheet[k] ?? d])),
    sheet_fields: FIELDS.map(([key, d]) => ({ key, lensbook_key: key, default: d })),
    default_engine: {},
    auto_carry_over: carry,
    policy: {},
  };
}

const PLATE_GRAPH = {
  schema: 1 as const,
  nodes: [{ kind: "Plate" as const, id: "plate_room_a_1", label: "Plate", packetId: "room_a", tier: "A" as const, position: { x: 0, y: 0 } }],
  edges: [],
};

const echoGraph = (c: Call) => ({ ok: true, project_id: "p1", graph: c.body });

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  usePanelsStore.setState(initialPanelsState());
  useMakeStore.getState().reset();
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// ------------------------------------------------------------------------------------------------ Project Sheet

describe("Project Sheet", () => {
  it("shows the 14 fields in the daemon's order with defaults as placeholders and PUTs project_sheet.species", async () => {
    useGraphStore.getState().loadProject(PLATE_GRAPH, "p1");
    const calls = mockApi({
      "GET /projects": [PROJECT],
      "GET /projects/p1/settings": settings({ location: "Bengaluru" }),
      "PUT /projects/p1/settings": (c: Call) => {
        const b = c.body as { project_sheet: Record<string, string>; auto_carry_over: boolean };
        return settings(b.project_sheet, b.auto_carry_over);
      },
    });
    render(<ProjectPanel />);
    await screen.findByTestId("custody-row");
    // closed: nothing is fetched until the disclosure opens
    expect(sent(calls, "GET", "/projects/p1/settings")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Project Sheet" }));
    await waitFor(() => expect(screen.getAllByTestId("sheet-field")).toHaveLength(14));
    const inputs = screen.getAllByTestId("sheet-field") as HTMLInputElement[];
    expect(inputs.map((i) => i.dataset.key)).toEqual(FIELDS.map(([k]) => k));
    expect(screen.getByLabelText("Sheet format")).toHaveAttribute("placeholder", "4K architectural film, 16:9");
    expect((screen.getByLabelText("Sheet format") as HTMLInputElement).value).toBe(""); // at its default: blank
    expect((screen.getByLabelText("Sheet location") as HTMLInputElement).value).toBe("Bengaluru");
    expect(screen.getByLabelText("Auto carry-over")).toBeChecked();
    fireEvent.change(screen.getByLabelText("Sheet species"), { target: { value: "rain trees, bamboo" } });
    fireEvent.click(screen.getByLabelText("Auto carry-over"));
    fireEvent.click(screen.getByRole("button", { name: "Save sheet" }));
    await screen.findByTestId("sheet-saved");
    const put = sent(calls, "PUT", "/projects/p1/settings");
    expect(put).toHaveLength(1);
    expect(put[0].body).toEqual({ project_sheet: { location: "Bengaluru", species: "rain trees, bamboo" }, auto_carry_over: false });
    expect((screen.getByLabelText("Sheet species") as HTMLInputElement).value).toBe("rain trees, bamboo");
    expect(screen.getByLabelText("Auto carry-over")).not.toBeChecked();
  });

  it("shows the daemon's refusal and keeps what was typed", async () => {
    useGraphStore.getState().loadProject(PLATE_GRAPH, "p1");
    mockApi({
      "GET /projects": [PROJECT],
      "GET /projects/p1/settings": settings(),
      "PUT /projects/p1/settings": jsonResponse({ errors: ["project sheet field 'species' must be text"] }, 422),
    });
    render(<ProjectPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Project Sheet" }));
    fireEvent.change(await screen.findByLabelText("Sheet species"), { target: { value: "neem" } });
    fireEvent.click(screen.getByRole("button", { name: "Save sheet" }));
    expect(await screen.findByText("project sheet field 'species' must be text")).toBeInTheDocument();
    expect((screen.getByLabelText("Sheet species") as HTMLInputElement).value).toBe("neem");
  });

  it("sheet helpers: defaults read blank, blanks are left out", () => {
    const s = { ...settings({ species: "neem" }), sheet_fields: settings().sheet_fields };
    const values = sheetFormValues(s);
    expect(values.format).toBe("");
    expect(values.species).toBe("neem");
    expect(sheetBody({ ...values, climate: "  " })).toEqual({ species: "neem" });
  });
});

// ------------------------------------------------------------------------------------------------ Apply template

const PACKETS = [
  { packet_id: "room_a", shot_id: "s01", tier: "A", camera_name: "Cam_A", status: "ingested" },
  { packet_id: "room_a_v2", shot_id: "s01", tier: "A", camera_name: "Cam_A", status: "ingested" },
  { packet_id: "room_b", shot_id: "s02", tier: "A", camera_name: "Cam_B", status: "ingested" },
];

const TEMPLATES = [
  { id: "edit_chain", title: "Edit chain (sky, planting, people)", nodes: 5, set: false, origin: "shipped" },
  { id: "set_chain", title: "Set chain", nodes: 3, set: true, origin: "shipped" },
];

const RECIPES = [
  { id: "season_swap", title: "Season swap", kind: "still_edit", engine: "nano-banana-2.1" },
  { id: "drive_in", title: "Drive in", kind: "motion_croppair", engine: "kling-o3-4k" },
];

const STORED = {
  schema: 1,
  nodes: [
    ...PLATE_GRAPH.nodes,
    { kind: "Plate", id: "plate_cam_b_1", label: "Plate", packetId: "room_b", tier: "A", position: { x: 340, y: 0 } },
    { kind: "Edit", id: "edit_cam_b_1", label: "Edit", position: { x: 340, y: 360 } },
  ],
  edges: [{ id: "plate_cam_b_1->edit_cam_b_1", source: "plate_cam_b_1", target: "edit_cam_b_1", sourceHandle: "image", targetHandle: "image" }],
};

function templateDaemon(apply: Route = { nodes_added: 0, edits: [], runs: [], total_usd: 0, refused: [], awaiting_approval: [] }) {
  return mockApi({
    "GET /projects": [PROJECT],
    "GET /templates": TEMPLATES,
    "GET /projects/p1/packets": PACKETS,
    "GET /recipes": RECIPES,
    "GET /engines": [
      { id: "nano-banana-2.1", name: "Nano Banana 2.1", kind: "image_edit", allowed_tasks: ["relight"], routes: [{ provider: "atlas", custody_grade: "D", custody_status: "confirmed", effective_grade: "D", status: "active" }] },
    ],
    "GET /projects/p1/runs": [],
    "PUT /projects/p1/graph": echoGraph,
    "POST /projects/p1/templates/apply": apply,
  });
}

const packetBox = (id: string) => screen.getByLabelText(`Packet ${id}`) as HTMLInputElement;

describe("Apply template", () => {
  it("defaults to the packets with no Plate, posts packet_ids and reloads the stored graph", async () => {
    useGraphStore.getState().loadProject(PLATE_GRAPH, "p1");
    const calls = templateDaemon({
      nodes_added: 2,
      edits: ["edit_cam_b_1", "edit_cam_a_2"],
      runs: [
        { id: "run_1", state: "QUOTED", node_id: "edit_cam_b_1" },
        { id: "run_2", state: "QUOTED", node_id: "edit_cam_a_2" },
      ],
      total_usd: 0.068,
      refused: [],
      awaiting_approval: [],
      graph: STORED,
    });
    render(<ProjectPanel />);
    await screen.findByTestId("custody-row");
    expect(sent(calls, "GET", "/templates")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Apply template…" }));
    await waitFor(() => expect(screen.getAllByTestId("template-packet")).toHaveLength(3));
    expect(sent(calls, "GET", "/templates")[0].search.get("project_id")).toBe("p1");
    // room_a already has a Plate on the canvas: not picked by default
    expect(packetBox("room_a").checked).toBe(false);
    expect(packetBox("room_a_v2").checked).toBe(true);
    expect(packetBox("room_b").checked).toBe(true);
    fireEvent.click(packetBox("room_a_v2"));
    await waitFor(() => expect(screen.getByRole("option", { name: "Nano Banana 2.1" })).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Template engine"), { target: { value: "nano-banana-2.1" } });
    // Edit recipes only: a motion recipe has no place on an Edit
    const recipes = within(screen.getByLabelText("Template recipe")).getAllByRole("option").map((o) => (o as HTMLOptionElement).value);
    expect(recipes).toEqual(["", "season_swap"]);
    fireEvent.change(screen.getByLabelText("Template recipe"), { target: { value: "season_swap" } });
    expect(screen.getByLabelText("Auto-approve under cap")).not.toBeChecked();
    fireEvent.click(screen.getByLabelText("Auto-approve under cap"));
    fireEvent.click(screen.getByRole("button", { name: "Apply template" }));
    expect(await screen.findByTestId("template-result")).toHaveTextContent("1 chain · 2 quoted · 0.068 USD");
    // the canvas is stored first, then the template is laid over it
    const order = calls.filter((c) => c.method !== "GET").map((c) => `${c.method} ${c.path}`);
    expect(order.slice(0, 2)).toEqual(["PUT /projects/p1/graph", "POST /projects/p1/templates/apply"]);
    expect(sent(calls, "POST", "/projects/p1/templates/apply")[0].body).toEqual({
      template_id: "edit_chain",
      packet_ids: ["room_b"],
      engine_id: "nano-banana-2.1",
      recipe: "season_swap",
      quote: true,
      auto_approve: true,
    });
    await waitFor(() => expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(["plate_room_a_1", "plate_cam_b_1", "edit_cam_b_1"]));
    expect(useGraphStore.getState().projectId).toBe("p1");
    await waitFor(() => expect(sent(calls, "GET", "/projects/p1/runs").length).toBeGreaterThan(0));
  });

  it("auto-approve needs quote; a refused Edit and a waiting approval are listed", async () => {
    useGraphStore.getState().loadProject(PLATE_GRAPH, "p1");
    const calls = templateDaemon({
      nodes_added: 4,
      edits: ["edit_a", "edit_b"],
      runs: [{ id: "run_1", state: "QUOTED", node_id: "edit_b" }],
      total_usd: 2.4,
      refused: [{ node: "edit_a", status: 422, error: "no_prompt", detail: "edit_a has no prompt or recipe" }],
      awaiting_approval: [{ node: "edit_b", error: "auto_cap", run_id: "run_1", usd: 2.4 }],
      graph: STORED,
    });
    render(<ProjectPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Apply template…" }));
    await waitFor(() => expect(screen.getAllByTestId("template-packet")).toHaveLength(3));
    fireEvent.click(screen.getByLabelText("Auto-approve under cap"));
    fireEvent.click(screen.getByLabelText("Quote each Edit"));
    expect(screen.getByLabelText("Auto-approve under cap")).toBeDisabled();
    expect(screen.getByLabelText("Auto-approve under cap")).not.toBeChecked();
    fireEvent.click(screen.getByLabelText("Quote each Edit"));
    fireEvent.click(screen.getByRole("button", { name: "Apply template" }));
    const result = await screen.findByTestId("template-result");
    expect(result).toHaveTextContent("2 chains · 1 quoted · 2.40 USD");
    expect(result).toHaveTextContent("1 waiting for your approval");
    expect(result).toHaveTextContent("edit_a: edit_a has no prompt or recipe");
    expect((sent(calls, "POST", "/projects/p1/templates/apply")[0].body as { auto_approve: boolean }).auto_approve).toBe(false);
  });

  it("refuses to apply with no packet picked, and over a graph that did not save", async () => {
    useGraphStore.getState().loadProject(PLATE_GRAPH, "p1");
    const calls = mockApi({
      "GET /projects": [PROJECT],
      "GET /templates": TEMPLATES,
      "GET /projects/p1/packets": PACKETS,
      "GET /recipes": [],
      "GET /engines": [],
      "PUT /projects/p1/graph": jsonResponse({ error: "project_locked", detail: "another writer holds the lock" }, 409),
    });
    render(<ProjectPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Apply template…" }));
    await waitFor(() => expect(screen.getAllByTestId("template-packet")).toHaveLength(3));
    fireEvent.click(packetBox("room_a_v2"));
    fireEvent.click(packetBox("room_b"));
    fireEvent.click(screen.getByRole("button", { name: "Apply template" }));
    expect(await screen.findByText("Pick at least one packet.")).toBeInTheDocument();
    fireEvent.click(packetBox("room_b"));
    fireEvent.click(screen.getByRole("button", { name: "Apply template" }));
    expect(await screen.findByText(/the graph is not saved/)).toBeInTheDocument();
    expect(sent(calls, "POST", "/projects/p1/templates/apply")).toHaveLength(0);
  });

  it("applySummary", () => {
    expect(applySummary(3, { runs: [], total_usd: 0 })).toBe("3 chains · 0 quoted · 0.00 USD");
  });
});

// ------------------------------------------------------------------------------------------------ Spend

const SPEND = {
  project_id: "p1",
  by: "week",
  since: null,
  spent_usd_quoted: 0.104,
  spent_usd_billed: 0.068,
  generations: 3,
  accepted: 1,
  rerolled: 0,
  by_engine: {},
  by_custody: {},
  series: [
    { bucket: "2026-W41", engine_id: "nano-banana-2.1", runs: 1, quoted_usd: 0.034, billed_usd: 0.034, accepted: 0, cost_per_accepted: null },
    { bucket: "2026-W42", engine_id: "fake", runs: 1, quoted_usd: 0, billed_usd: 0, accepted: 1, cost_per_accepted: 0 },
    { bucket: "2026-W42", engine_id: "nano-banana-2.1", runs: 1, quoted_usd: 0.07, billed_usd: 0.034, accepted: 0, cost_per_accepted: null },
  ],
};

const tab = (name: string) => fireEvent.click(screen.getByRole("tab", { name }));

describe("Spend tab", () => {
  it("renders a row per engine and week from /projects/{id}/spend?by=week; All projects asks /spend", async () => {
    useGraphStore.getState().loadProject(PLATE_GRAPH, "p1");
    const calls = mockApi({
      "GET /projects": [PROJECT],
      "GET /projects/p1/runs": [],
      "GET /projects/p1/spend": SPEND,
      "GET /spend": {
        ...SPEND,
        scope: "all_projects",
        projects: ["p1", "p2"],
        series: [{ bucket: "2026-W42", engine_id: "seedance-2.5-i2v", runs: 2, quoted_usd: 1.8, billed_usd: 0, accepted: 1, cost_per_accepted: 1.8 }],
      },
    });
    render(<Panels />);
    tab("Spend");
    await waitFor(() => expect(screen.getAllByTestId("spend-row")).toHaveLength(3));
    expect(sent(calls, "GET", "/projects/p1/spend")[0].search.get("by")).toBe("week");
    const rows = screen.getAllByTestId("spend-row");
    // newest week first, then by engine
    expect(rows.map((r) => `${r.dataset.bucket} ${r.dataset.engine}`)).toEqual([
      "2026-W42 fake",
      "2026-W42 nano-banana-2.1",
      "2026-W41 nano-banana-2.1",
    ]);
    expect(rows[1]).toHaveTextContent("0.070 USD");
    expect(rows[1]).toHaveTextContent("none accepted");
    expect(rows[0]).toHaveTextContent("not billed");
    expect(screen.getByTestId("spend-totals")).toHaveTextContent("billed 0.068 USD");
    fireEvent.click(screen.getByLabelText("All projects"));
    await waitFor(() => expect(screen.getAllByTestId("spend-row")).toHaveLength(1));
    const all = sent(calls, "GET", "/spend")[0];
    expect(all.search.get("all_projects")).toBe("1");
    expect(all.search.get("by")).toBe("week");
    expect(screen.getByTestId("spend-row")).toHaveAttribute("data-engine", "seedance-2.5-i2v");
    expect(screen.getByText("2 projects")).toBeInTheDocument();
  });

  it("asks for a project (or All projects) when none is open", async () => {
    mockApi({ "GET /projects": [] });
    render(<Panels />);
    tab("Spend");
    expect(screen.getByTestId("spend-panel")).toHaveTextContent("Open a project to see its spend");
  });

  it("orderSpend sorts newest week first", () => {
    expect(orderSpend(SPEND.series).map((r) => r.bucket)).toEqual(["2026-W42", "2026-W42", "2026-W41"]);
  });
});

// ------------------------------------------------------------------------------------------------ Settings

describe("Settings tab", () => {
  it("lists packets_dirs and the daemon facts read-only, key env var names only", async () => {
    mockApi({
      "GET /projects": [],
      "GET /session": {
        ok: true,
        daemon_version: "0.1.0",
        packet_versions: ["0.1"],
        packets_dirs: ["/Volumes/nas/packets", "/Users/aasish/packets"],
        projects_dir: "/Users/aasish/Library/Application Support/Plate/projects",
        dev: false,
        cas_copy: false,
      },
      "GET /health": {
        ok: true,
        daemon_version: "0.1.0",
        stub_engine: true,
        pixels: "inproc",
        router_mode: "advisory",
        nightly: { running: true, next_utc: "2026-10-19T02:00:00Z" },
        keys_set: { ATLAS_API_KEY: true, GEMINI_API_KEY: false, OPENAI_API_KEY: true },
      },
    });
    render(<Panels />);
    tab("Settings");
    const dirs = await screen.findByTestId("settings-packets-dirs");
    expect(within(dirs).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["/Volumes/nas/packets", "/Users/aasish/packets"]);
    expect(screen.getByTestId("settings-version")).toHaveTextContent("0.1.0");
    expect(screen.getByTestId("settings-projects-dir")).toHaveTextContent("Application Support/Plate/projects");
    expect(screen.getByTestId("settings-dev")).toHaveTextContent("off");
    expect(screen.getByTestId("settings-router-mode")).toHaveTextContent("advisory");
    expect(screen.getByTestId("settings-pixels")).toHaveTextContent("inproc");
    expect(screen.getByTestId("settings-nightly")).toHaveTextContent("running on · next_utc 2026-10-19T02:00:00Z");
    expect(screen.getByTestId("settings-keys")).toHaveTextContent("ATLAS_API_KEY");
    expect(screen.getByTestId("settings-keys")).toHaveTextContent("OPENAI_API_KEY");
    expect(screen.getByTestId("settings-keys")).not.toHaveTextContent("GEMINI_API_KEY");
    expect(screen.getByTestId("settings-panel").querySelector("input, select, textarea")).toBeNull(); // read-only
    const help = screen.getByTestId("settings-help");
    expect(help).toHaveAttribute("href", "/api/v0/docs/artist-manual.md");
    expect(help).toHaveAttribute("target", "_blank");
    expect(help.getAttribute("rel")).toContain("noopener");
  });

  it("says what /health does not report, and still shows the session when /health fails", async () => {
    mockApi({
      "GET /projects": [],
      "GET /session": { ok: true, daemon_version: "0.1.0", packets_dirs: [], projects_dir: null, dev: true },
      "GET /health": jsonResponse({ error: "boom" }, 500),
    });
    render(<Panels />);
    tab("Settings");
    expect(await screen.findByText(/health: boom/)).toBeInTheDocument();
    expect(screen.getByTestId("settings-dev")).toHaveTextContent("on (PLATE_DEV=1)");
    expect(screen.getByTestId("settings-router-mode")).toHaveTextContent("not reported");
    expect(screen.getByTestId("settings-keys")).toHaveTextContent("not reported");
    expect(screen.getByTestId("settings-packets-dirs")).toHaveTextContent("none");
  });

  it("Help opens a new tab and fills it with the manual fetched with the token", async () => {
    window.sessionStorage.setItem("plate.token", "tok123");
    const fakeTab = { document: document.implementation.createHTMLDocument(""), close: vi.fn() };
    const opened = vi.spyOn(window, "open").mockImplementation(() => fakeTab as unknown as Window);
    let auth: string | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(String(input), "http://127.0.0.1").pathname;
        if (path === "/api/v0/docs/artist-manual.md") {
          auth = new Headers(init?.headers).get("Authorization");
          return new Response("# Artist manual\n<script>x</script>", { status: 200, headers: { "Content-Type": "text/markdown" } });
        }
        if (path === "/api/v0/session") return jsonResponse({ packets_dirs: [] });
        if (path === "/api/v0/health") return jsonResponse({ ok: true });
        return jsonResponse([]);
      }),
    );
    render(<Panels />);
    tab("Settings");
    fireEvent.click(await screen.findByTestId("settings-help"));
    expect(opened).toHaveBeenCalledWith("", "_blank");
    await waitFor(() => expect(fakeTab.document.body.querySelector("pre")).not.toBeNull());
    expect(auth).toBe("Bearer tok123");
    const pre = fakeTab.document.body.querySelector("pre") as HTMLPreElement;
    expect(pre.textContent).toBe("# Artist manual\n<script>x</script>");
    expect(fakeTab.document.body.querySelector("script")).toBeNull(); // shown as text, never parsed
    expect(fakeTab.document.title).toBe("PLATE artist manual");
    opened.mockRestore();
  });

  it("helpers: key names from a list or a map; plain descriptions", () => {
    expect(keyNames(["A", "B"])).toEqual(["A", "B"]);
    expect(keyNames({ A: true, B: false })).toEqual(["A"]);
    expect(keyNames(undefined)).toBeNull();
    expect(describeValue(true)).toBe("on");
    expect(describeValue(undefined)).toBe("not reported");
    expect(describeValue({ running: false, jobs: 3 })).toBe("running off · jobs 3");
  });
});

// ------------------------------------------------------------------------------------------------ tabs

describe("panel tabs", () => {
  it("has Spend and Settings tabs beside the T36 ones", async () => {
    mockApi({ "GET /projects": [], "GET /session": { packets_dirs: [] }, "GET /health": { ok: true } });
    render(<Panels />);
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual([
      "Projects",
      "History",
      "Gate",
      "Custody",
      "Timer",
      "Spend",
      "Settings",
    ]);
    act(() => usePanelsStore.getState().setTab("settings"));
    expect(await screen.findByTestId("settings-panel")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Settings" })).toHaveAttribute("aria-selected", "true");
  });
});
