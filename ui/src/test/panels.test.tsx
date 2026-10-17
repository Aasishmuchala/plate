// T36 panels: history, drop-to-restore, project browser, gate panel, custody panel, verdict keys with the scrub
// gate, and the human-minute timer. The daemon is a fetch stub that answers by "METHOD /path" and records every
// call, so each test also pins the request the panel sent. jsdom has no WebGL: the viewer runs without a GPU and the
// scrub tracker is fed through its reducer-driven `onChange`.

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createInitialGraph, useGraphStore } from "../store";
import { Panels } from "../panels";
import { ConnectedViewer } from "../panels/ConnectedViewer";
import { CustodyPanel, engineGrades, opaqueIds } from "../panels/CustodyPanel";
import { formatBytes, formatElapsed, formatThreshold, formatValue } from "../panels/format";
import { GatePanel, orderChecks } from "../panels/GatePanel";
import { flaggedPoints } from "../panels/gatePoints";
import { sortRuns } from "../panels/HistoryPanel";
import { REASON_CODES } from "../panels/reasonCodes";
import { isImageFile, pathFromDrop, pathFromFileUrl } from "../panels/restore";
import { EMPTY_SCRUB, isFullScrub, observeView, quadrantsVisited } from "../panels/scrub";
import { initialPanelsState, scrubLogFor, usePanelsStore } from "../panels/store";
import { planRestore } from "../panels/subgraph";
import { TimerPanel } from "../panels/TimerPanel";
import { isTextEntry, VerdictPanel, verdictForKey } from "../panels/VerdictPanel";
import { asList, errorMessage } from "../panels/api";
import { ApiError } from "../api";
import type { GateReport, RunDetail, RunRow } from "../panels/types";

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

/** Answers `METHOD /path` (the /api/v0 prefix and the query string are stripped); anything else is a 404 and recorded. */
function mockApi(routes: Record<string, Route>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://127.0.0.1");
      const method = (init?.method ?? "GET").toUpperCase();
      const path = url.pathname.replace(/^\/api\/v0/, "");
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
      const call: Call = { method, path, search: url.search, body };
      calls.push(call);
      const route = routes[`${method} ${path}`];
      if (route === undefined) return jsonResponse({ error: `no stub for ${method} ${path}` }, 404);
      const out = typeof route === "function" ? await (route as (c: Call) => unknown)(call) : route;
      return out instanceof Response ? out : jsonResponse(out);
    }),
  );
  return calls;
}

const posts = (calls: Call[], path: string): Call[] => calls.filter((c) => c.method === "POST" && c.path === path);

// ------------------------------------------------------------------------------------------------ fixtures

const GRAPH = {
  schema: 1 as const,
  nodes: [
    { kind: "Plate" as const, id: "plate-1", label: "Plate", packetId: "room_a", tier: "A" as const },
    { kind: "Edit" as const, id: "edit-1", label: "Edit" },
    { kind: "Comp" as const, id: "comp-1", label: "Comp" },
  ],
  edges: [
    { id: "plate-1->edit-1", source: "plate-1", target: "edit-1", sourceHandle: null, targetHandle: null },
    { id: "edit-1->comp-1", source: "edit-1", target: "comp-1", sourceHandle: null, targetHandle: null },
  ],
};

const run = (id: string, over: Partial<RunDetail> = {}): RunDetail => ({
  id,
  project_id: "p1",
  node_id: "edit-1",
  state: "DONE",
  route: { engine_id: "nano-banana-2.1", provider: "google", region: "us" },
  quote: { price_usd: 0.12, custody_grade: "C2" },
  cost_usd_billed: 0.12,
  gate_report: { verdict: "pass" },
  created_utc: "2026-10-17T10:00:00Z",
  updated_utc: "2026-10-17T10:05:00Z",
  ...over,
});

const REPORT: GateReport = {
  gate_version: "0.1.0",
  threshold_set_id: "ts-2026.10.17-draft",
  engine: { id: "nano-banana-2.1" },
  tier: "A",
  verdict: "fail",
  checks: [
    { metric_id: "registration.residual_px", value: 0.4237, status: "pass", threshold: { op: "le", value: 1, unit: "px" } },
    { metric_id: "texture.fine_ratio", value: 0.47, status: "advisory", threshold: { range: [0.4, 0.6] } },
    { metric_id: "invention_flags", value: 2, status: "fail", threshold: { op: "eq", value: 0 } },
  ],
  metrics: {
    invention: { flags: [{ id: "w1", class: "window", bbox: [100, 60, 140, 100], centroid: [120.5, 80] }] },
    shift: [3, 4],
  },
  backend_notes: ["flow backend: dis (RAFT needs CUDA)"],
  created_utc: "2026-10-17T10:05:00Z",
};

const openProject = (id = "p1") => useGraphStore.getState().loadProject(GRAPH, id);

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  usePanelsStore.setState(initialPanelsState());
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ------------------------------------------------------------------------------------------------ pure helpers

describe("scrub tracker", () => {
  const image = { width: 4000, height: 2000 };
  const viewport = { width: 1000, height: 500 };
  const at = (x: number, y: number) => ({ zoom: 1 as const, pan: { x, y } });

  it("counts a quadrant when its centre is in the viewport at 100% or more", () => {
    let p = observeView(EMPTY_SCRUB, at(1000, 500), image, viewport);
    expect(p.zoom100).toBe(true);
    expect(p.quadrants).toEqual([true, false, false, false]);
    p = observeView(p, at(3000, 1500), image, viewport);
    expect(p.quadrants).toEqual([true, false, false, true]);
    expect(quadrantsVisited(p)).toBe(2);
    expect(isFullScrub(p)).toBe(false);
    p = observeView(observeView(p, at(3000, 500), image, viewport), at(1000, 1500), image, viewport);
    expect(isFullScrub(p)).toBe(true);
  });

  it("ignores views below 100%, including fit of a big image", () => {
    const fit = observeView(EMPTY_SCRUB, { zoom: "fit", pan: null }, image, viewport);
    expect(fit).toBe(EMPTY_SCRUB);
    expect(observeView(EMPTY_SCRUB, { zoom: 0.5, pan: { x: 1000, y: 500 } }, image, viewport)).toBe(EMPTY_SCRUB);
  });

  it("counts fit when fit itself is at 100% or more (a small image in a big viewport)", () => {
    const p = observeView(EMPTY_SCRUB, { zoom: "fit", pan: null }, { width: 640, height: 360 }, { width: 1280, height: 720 });
    expect(isFullScrub(p)).toBe(true);
  });

  it("zoom above 100% sees less, so it needs more panning", () => {
    const p = observeView(EMPTY_SCRUB, { zoom: 4, pan: { x: 2000, y: 1000 } }, image, viewport);
    expect(p.zoom100).toBe(true);
    expect(quadrantsVisited(p)).toBe(0);
  });

  it("returns the same object when nothing changed", () => {
    const p = observeView(EMPTY_SCRUB, at(1000, 500), image, viewport);
    expect(observeView(p, at(1000, 500), image, viewport)).toBe(p);
  });

  it("does nothing with an empty image or viewport", () => {
    expect(observeView(EMPTY_SCRUB, at(0, 0), { width: 0, height: 0 }, viewport)).toBe(EMPTY_SCRUB);
    expect(observeView(EMPTY_SCRUB, at(0, 0), image, { width: 0, height: 0 })).toBe(EMPTY_SCRUB);
  });
});

describe("flagged points", () => {
  it("reads invention flags by centroid and bbox, and never a bare number pair inside the metrics", () => {
    expect(flaggedPoints(REPORT)).toEqual([{ x: 120.5, y: 80 }]);
  });

  it("reads explicit flagged_points in every shape and drops duplicates", () => {
    const r = { ...REPORT, metrics: {}, flagged_points: [[10, 20], { x: 30, y: 40 }, { centroid: [50, 60] }, { bbox: [0, 0, 10, 10] }, [10, 20]] };
    expect(flaggedPoints(r)).toEqual([
      { x: 10, y: 20 },
      { x: 30, y: 40 },
      { x: 50, y: 60 },
      { x: 5, y: 5 },
    ]);
  });

  it("is empty when the gates flagged nothing", () => {
    expect(flaggedPoints({ ...REPORT, metrics: { a: 1 } })).toEqual([]);
  });
});

describe("formatting", () => {
  it("formats values, bounds, bytes and the timer", () => {
    expect(formatValue(0.42371)).toBe("0.4237");
    expect(formatValue(412.04)).toBe("412");
    expect(formatValue(0)).toBe("0");
    expect(formatValue(null)).toBe("n/a");
    expect(formatThreshold({ op: "le", value: 1, unit: "px" })).toBe("<= 1 px");
    expect(formatThreshold({ op: "ge", value: 0.9 })).toBe(">= 0.9");
    expect(formatThreshold({ op: "eq", value: 0 })).toBe("= 0");
    expect(formatThreshold({ range: [0.4, 0.6] })).toBe("0.4 to 0.6");
    expect(formatThreshold(undefined)).toBe("\u2014");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatElapsed(65_000)).toBe("01:05");
  });

  it("orders failures before advisories before passes", () => {
    expect(orderChecks(REPORT.checks).map((c) => c.status)).toEqual(["fail", "advisory", "pass"]);
  });

  it("sorts runs newest first", () => {
    const ids = sortRuns([run("a", { updated_utc: "2026-10-17T09:00:00Z" }), run("b", { updated_utc: "2026-10-17T11:00:00Z" })]).map((r) => r.id);
    expect(ids).toEqual(["b", "a"]);
  });

  it("reads lists from an array or an object and daemon errors from their body", () => {
    expect(asList([1, 2], "x")).toEqual([1, 2]);
    expect(asList({ runs: [3] }, "runs")).toEqual([3]);
    expect(asList({ ok: true }, "runs")).toEqual([]);
    expect(errorMessage(new ApiError(409, { detail: "expired" }, "x"))).toBe("expired");
    expect(errorMessage(new ApiError(422, { errors: ["a", "b"] }, "x"))).toBe("a; b");
    expect(errorMessage(new ApiError(500, "boom", "x"))).toBe("boom");
  });
});

describe("dropped file paths", () => {
  const file = (name: string, type = "") => new File(["x"], name, { type });
  const dt = (flavours: Record<string, string>) => ({ getData: (f: string) => flavours[f] ?? "" });

  it("takes the path the browser exposes", () => {
    const f = file("a.png", "image/png");
    Object.defineProperty(f, "path", { value: "C:\\renders\\a.png" });
    expect(pathFromDrop(f, dt({}))).toBe("C:\\renders\\a.png");
  });

  it("reads a file URL, with Windows drive letters and percent escapes", () => {
    expect(pathFromFileUrl("file:///C:/renders/shot%201.png")).toBe("C:/renders/shot 1.png");
    expect(pathFromFileUrl("file:///srv/plate/a.png")).toBe("/srv/plate/a.png");
    expect(pathFromFileUrl("https://example.com/a.png")).toBeNull();
    expect(pathFromDrop(file("a.png"), dt({ "text/uri-list": "file:///srv/a.png" }))).toBe("/srv/a.png");
  });

  it("accepts an absolute path in the text flavour and nothing else", () => {
    expect(pathFromDrop(file("a.png"), dt({ "text/plain": "D:\\x\\a.png" }))).toBe("D:\\x\\a.png");
    expect(pathFromDrop(file("a.png"), dt({ "text/plain": "a.png" }))).toBeNull();
    expect(pathFromDrop(file("a.png"), null)).toBeNull();
  });

  it("knows image files by type or extension (an EXR has no MIME type)", () => {
    expect(isImageFile({ name: "a.exr", type: "" })).toBe(true);
    expect(isImageFile({ name: "a.bin", type: "image/png" })).toBe(true);
    expect(isImageFile({ name: "notes.txt", type: "text/plain" })).toBe(false);
  });
});

describe("restore plan", () => {
  const current = () => {
    openProject();
    const g = useGraphStore.getState();
    return { nodes: g.nodes, edges: g.edges, wire: g.toGraph() };
  };

  it("is the run's node and everything upstream of it on the current graph", () => {
    const plan = planRestore(run("r1", { node_id: "edit-1" }), current());
    expect(plan?.nodeIds.sort()).toEqual(["edit-1", "plate-1"]);
    expect(plan?.added).toBe(0);
  });

  it("uses the run's snapshot and adds the nodes the canvas lacks", () => {
    const snap = {
      nodes: [
        { id: "plate-1", kind: "Plate", packetId: "room_a", tier: "A" },
        { id: "edit-9", kind: "Edit", label: "Edit (old)" },
      ],
      edges: [{ id: "plate-1->edit-9", source: "plate-1", target: "edit-9" }],
    };
    const plan = planRestore(run("r9", { node_id: "edit-9", subgraph: snap }), current());
    expect(plan?.nodeIds).toEqual(["plate-1", "edit-9"]);
    expect(plan?.added).toBe(1);
    expect(plan?.graph.nodes.map((n) => n.id)).toEqual(["plate-1", "edit-1", "comp-1", "edit-9"]);
    expect(plan?.graph.edges.map((e) => e.id)).toContain("plate-1->edit-9");
  });

  it("is null when the run's node is not on the canvas and there is no snapshot", () => {
    expect(planRestore(run("r1", { node_id: "gone" }), current())).toBeNull();
    expect(planRestore(run("r1", { node_id: null }), current())).toBeNull();
  });
});

describe("verdict key mapping", () => {
  it("plain digits give verdicts outside the viewer, Shift+digit also inside it", () => {
    expect(verdictForKey({ key: "1", code: "Digit1", shiftKey: false }, false)).toBe("ship");
    expect(verdictForKey({ key: "2", code: "Digit2", shiftKey: false }, false)).toBe("fix");
    expect(verdictForKey({ key: "3", code: "Digit3", shiftKey: false }, false)).toBe("render_it");
    expect(verdictForKey({ key: "1", code: "Digit1", shiftKey: false }, true)).toBeNull();
    expect(verdictForKey({ key: "#", code: "Digit3", shiftKey: true }, true)).toBe("render_it");
    expect(verdictForKey({ key: "4", code: "Digit4", shiftKey: false }, false)).toBeNull();
  });

  it("treats text boxes as text and checkboxes as buttons", () => {
    const text = document.createElement("input");
    const box = document.createElement("input");
    box.type = "checkbox";
    expect(isTextEntry(text)).toBe(true);
    expect(isTextEntry(box)).toBe(false);
    expect(isTextEntry(document.createElement("textarea"))).toBe(true);
    expect(isTextEntry(document.createElement("button"))).toBe(false);
    expect(isTextEntry(null)).toBe(false);
  });
});

describe("reason codes", () => {
  const RAW = Object.values(
    import.meta.glob("../../../calib/reason_codes.yaml", { query: "?raw", import: "default", eager: true }) as Record<string, string>,
  )[0];

  it.skipIf(RAW === undefined)("match calib/reason_codes.yaml exactly", () => {
    const yaml: Record<string, string> = {};
    let inCodes = false;
    for (const line of RAW.split(/\r?\n/)) {
      if (/^codes:\s*$/.test(line)) inCodes = true;
      else if (/^\S/.test(line)) inCodes = false;
      else if (inCodes) {
        const m = /^\s+([A-Z]\d+):\s*(.+?)\s*$/.exec(line);
        if (m) yaml[m[1]] = m[2];
      }
    }
    expect(REASON_CODES).toEqual(yaml);
  });
});

describe("custody helpers", () => {
  it("groups the grades each engine was quoted at", () => {
    const rows = engineGrades([
      run("1"),
      run("2"),
      run("3", { route: { engine_id: "veo-3.1" }, quote: { price_usd: 1, custody_grade: "D" } }),
      run("4", { route: { engine_id: "veo-3.1" }, quote: { price_usd: 1, custody_grade: "C2" } }),
      run("5", { quote: null }),
    ]);
    expect(rows).toEqual([
      { engine: "nano-banana-2.1", grades: ["C2"], runs: 3 },
      { engine: "veo-3.1", grades: ["C2", "D"], runs: 2 },
    ]);
  });

  it("reads opaque ids from a list or a map", () => {
    expect(opaqueIds(["a", "b"])).toEqual(["a", "b"]);
    expect(opaqueIds({ x: "a1", y: "b2" })).toEqual(["a1", "b2"]);
    expect(opaqueIds(undefined)).toEqual([]);
  });
});

// ------------------------------------------------------------------------------------------------ project browser

describe("project browser", () => {
  const PROJECTS = [
    {
      project_id: "tula",
      created_utc: "2026-10-01T09:00:00Z",
      custody: { nda: true, allowed_grades: ["C0", "C1"] },
      engine_pins: { "nano-banana-2.1": "2.1.3" },
    },
    { project_id: "demo", custody: { nda: false, allowed_grades: ["C0", "C1", "C2", "D"] }, engine_pins: {} },
  ];

  it("lists projects with their NDA badge and allowed grades", async () => {
    mockApi({ "GET /projects": PROJECTS });
    render(<Panels />);
    const items = await screen.findAllByTestId("project-item");
    expect(items.map((i) => i.dataset.projectId)).toEqual(["tula", "demo"]);
    expect(within(items[0]).getByText("NDA")).toBeInTheDocument();
    expect(within(items[0]).getByText("C1")).toBeInTheDocument();
    expect(within(items[0]).queryByText("D")).toBeNull();
    expect(within(items[1]).queryByText("NDA")).toBeNull();
    expect(within(items[1]).getByText("D")).toBeInTheDocument();
  });

  it("opens a project onto the canvas, loads its runs and shows its engine pins", async () => {
    const calls = mockApi({
      "GET /projects": { projects: PROJECTS },
      "GET /projects/tula/graph": { graph: GRAPH },
      "GET /projects/tula/runs": { runs: [run("r1")] },
    });
    render(<Panels />);
    fireEvent.click(await screen.findByRole("button", { name: "Open tula" }));
    await waitFor(() => expect(useGraphStore.getState().projectId).toBe("tula"));
    expect(useGraphStore.getState().nodes.map((n) => n.id)).toEqual(["plate-1", "edit-1", "comp-1"]);
    await waitFor(() => expect(usePanelsStore.getState().runs.map((r) => r.id)).toEqual(["r1"]));
    expect(calls.some((c) => c.path === "/projects/tula/runs")).toBe(true);
    const pins = await screen.findByTestId("pins-table");
    expect(pins).toHaveTextContent("nano-banana-2.1");
    expect(pins).toHaveTextContent("2.1.3");
  });

  it("says so when a project cannot be opened", async () => {
    mockApi({ "GET /projects": PROJECTS, "GET /projects/tula/graph": jsonResponse({ detail: "locked by another writer" }, 409) });
    render(<Panels />);
    fireEvent.click(await screen.findByRole("button", { name: "Open tula" }));
    expect(await screen.findByText(/Could not open tula: locked by another writer/)).toBeInTheDocument();
    expect(useGraphStore.getState().projectId).toBeNull();
  });

  it("creates an NDA project with C0 and C1 only and the engine pins given", async () => {
    const calls = mockApi({
      "GET /projects": [],
      "POST /projects": { project_id: "new-job", custody: { nda: true, allowed_grades: ["C0", "C1"] }, engine_pins: { "veo-3.1": "3.1" } },
      "GET /projects/new-job/graph": { schema: 1, nodes: [], edges: [] },
      "GET /projects/new-job/runs": [],
    });
    render(<Panels />);
    await screen.findByText("No projects yet. Create one below.");
    fireEvent.change(screen.getByLabelText("Project id"), { target: { value: "new-job" } });
    fireEvent.click(screen.getByLabelText(/NDA project/));
    expect(screen.getByLabelText("Allow C2")).toBeDisabled();
    expect(screen.getByLabelText("Allow C2")).not.toBeChecked();
    expect(screen.getByLabelText("Allow D")).toBeDisabled();
    expect(screen.getByLabelText("Allow C0")).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Add pin" }));
    fireEvent.change(screen.getByLabelText("Pinned engine id"), { target: { value: "veo-3.1" } });
    fireEvent.change(screen.getByLabelText("Pinned version"), { target: { value: "3.1" } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));
    await waitFor(() => expect(posts(calls, "/projects")).toHaveLength(1));
    expect(posts(calls, "/projects")[0].body).toEqual({
      project_id: "new-job",
      custody: { nda: true, allowed_grades: ["C0", "C1"] },
      engine_pins: { "veo-3.1": "3.1" },
    });
    await waitFor(() => expect(useGraphStore.getState().projectId).toBe("new-job"));
  });

  it("refuses a bad project id before calling the daemon", async () => {
    const calls = mockApi({ "GET /projects": [] });
    render(<Panels />);
    await screen.findByText("No projects yet. Create one below.");
    fireEvent.change(screen.getByLabelText("Project id"), { target: { value: "Bad ID" } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/lower-case/);
    expect(posts(calls, "/projects")).toHaveLength(0);
  });

  it("refuses a half-filled pin", async () => {
    const calls = mockApi({ "GET /projects": [] });
    render(<Panels />);
    await screen.findByText("No projects yet. Create one below.");
    fireEvent.change(screen.getByLabelText("Project id"), { target: { value: "ok-id" } });
    fireEvent.click(screen.getByRole("button", { name: "Add pin" }));
    fireEvent.change(screen.getByLabelText("Pinned engine id"), { target: { value: "veo-3.1" } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/both an engine id and a version/);
    expect(posts(calls, "/projects")).toHaveLength(0);
  });
});

// ------------------------------------------------------------------------------------------------ history

describe("history", () => {
  const RUNS: RunRow[] = [
    run("r-old", { updated_utc: "2026-10-17T09:00:00Z", state: "GATE_FAIL", gate_report: { verdict: "fail" }, cost_usd_billed: null, quote: { price_usd: 0.04, custody_grade: "C2" } }),
    run("r-new", { updated_utc: "2026-10-17T11:30:00Z" }),
  ];

  async function showHistory() {
    openProject();
    mockApi({ "GET /projects": [], "GET /projects/p1/runs": { runs: RUNS } });
    render(<Panels />);
    fireEvent.click(screen.getByRole("tab", { name: "History" }));
    return screen.findAllByTestId("history-run");
  }

  it("lists runs newest first with state, engine, price and gate verdict", async () => {
    const rows = await showHistory();
    expect(rows.map((r) => r.dataset.runId)).toEqual(["r-new", "r-old"]);
    expect(within(rows[0]).getByTestId("run-state")).toHaveTextContent("DONE");
    expect(within(rows[0]).getByText("nano-banana-2.1")).toBeInTheDocument();
    expect(within(rows[0]).getByTestId("run-price")).toHaveTextContent("$0.12");
    expect(within(rows[0]).getByTestId("run-gate")).toHaveTextContent("gate pass");
    expect(within(rows[1]).getByTestId("run-state")).toHaveTextContent("GATE_FAIL");
    expect(within(rows[1]).getByTestId("run-price")).toHaveTextContent("$0.04");
    expect(within(rows[1]).getByTestId("run-gate")).toHaveTextContent("gate fail");
    expect(rows[0]).toHaveTextContent("2026-10-17 11:30 UTC");
  });

  it("a click selects the run and its producing node on the canvas", async () => {
    const rows = await showHistory();
    fireEvent.click(rows[1]);
    expect(usePanelsStore.getState().selectedRunId).toBe("r-old");
    expect(useGraphStore.getState().nodes.filter((n) => n.selected).map((n) => n.id)).toEqual(["edit-1"]);
    expect(rows[1]).toHaveAttribute("aria-pressed", "true");
    expect(rows[0]).toHaveAttribute("aria-pressed", "false");
  });

  it("shows the daemon's error when the runs cannot be read", async () => {
    openProject();
    mockApi({ "GET /projects": [], "GET /projects/p1/runs": jsonResponse({ detail: "index is rebuilding" }, 503) });
    render(<Panels />);
    fireEvent.click(screen.getByRole("tab", { name: "History" }));
    expect(await screen.findByText("index is rebuilding")).toBeInTheDocument();
  });

  it("asks for a project first when none is open", () => {
    mockApi({ "GET /projects": [] });
    render(<Panels />);
    fireEvent.click(screen.getByRole("tab", { name: "History" }));
    expect(screen.getByText("Open a project to see its runs.")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------------------------------------ drop to restore

describe("drop-to-restore", () => {
  const dropFile = (file: File, extra: Record<string, string> = {}) =>
    fireEvent.drop(document.body, {
      dataTransfer: { types: ["Files"], files: [file], getData: (f: string) => extra[f] ?? "" },
    });
  const imageWithPath = (path: string) => {
    const f = new File(["x"], "shot.png", { type: "image/png" });
    Object.defineProperty(f, "path", { value: path });
    return f;
  };

  it("resolves a dropped image by lineage and restores the producing subgraph on 'Load N nodes'", async () => {
    openProject();
    const calls = mockApi({
      "GET /projects": [],
      "GET /projects/p1/runs": [],
      "POST /assets/resolve": { by: "lineage", content_hash: "abcdef0123456789", run_id: "r1" },
      "GET /runs/r1": run("r1", { node_id: "edit-1" }),
    });
    render(<Panels />);
    await waitFor(() => expect(calls.some((c) => c.path === "/projects/p1/runs")).toBe(true));
    act(() => {
      dropFile(imageWithPath("C:\\renders\\shot.png"));
    });
    expect(await screen.findByTestId("drop-match")).toHaveTextContent("lineage");
    expect(posts(calls, "/assets/resolve")[0].body).toEqual({ path: "C:\\renders\\shot.png" });
    fireEvent.click(screen.getByRole("button", { name: "Load 2 nodes" }));
    expect(useGraphStore.getState().nodes.filter((n) => n.selected).map((n) => n.id).sort()).toEqual(["edit-1", "plate-1"]);
    expect(usePanelsStore.getState().selectedRunId).toBe("r1");
    expect(usePanelsStore.getState().runs.map((r) => r.id)).toContain("r1");
    expect(usePanelsStore.getState().tab).toBe("history");
    expect(screen.queryByTestId("drop-prompt")).toBeNull();
  });

  it("brings back nodes the canvas no longer has from the run's snapshot", async () => {
    openProject();
    mockApi({
      "GET /projects": [],
      "GET /projects/p1/runs": [],
      "POST /assets/resolve": { by: "lineage", content_hash: "ff00ff00ff00", run_id: "r9" },
      "GET /runs/r9": run("r9", {
        node_id: "edit-9",
        subgraph: {
          nodes: [{ id: "plate-1", kind: "Plate", packetId: "room_a", tier: "A" }, { id: "edit-9", kind: "Edit", label: "Edit (old)" }],
          edges: [{ id: "plate-1->edit-9", source: "plate-1", target: "edit-9" }],
        },
      }),
    });
    render(<Panels />);
    act(() => {
      dropFile(imageWithPath("/srv/shot.png"));
    });
    fireEvent.click(await screen.findByRole("button", { name: "Load 2 nodes" }));
    const g = useGraphStore.getState();
    expect(g.nodes.map((n) => n.id)).toContain("edit-9");
    expect(g.nodes.filter((n) => n.selected).map((n) => n.id).sort()).toEqual(["edit-9", "plate-1"]);
    expect(g.projectId).toBe("p1");
  });

  it("says when an image matched an asset no run produced", async () => {
    openProject();
    mockApi({
      "GET /projects": [],
      "GET /projects/p1/runs": [],
      "POST /assets/resolve": { by: "phash", content_hash: "1234567890ab", run_id: null, distance: 7 },
    });
    render(<Panels />);
    act(() => {
      dropFile(imageWithPath("/srv/jpeg-copy.jpg"));
    });
    const match = await screen.findByTestId("drop-match");
    expect(match).toHaveTextContent("perceptual hash (distance 7)");
    expect(match).toHaveTextContent("no run produced it");
    expect(screen.queryByRole("button", { name: /^Load/ })).toBeNull();
  });

  it("asks for the path when the browser gives none, then resolves what is pasted", async () => {
    openProject();
    const calls = mockApi({
      "GET /projects": [],
      "GET /projects/p1/runs": [],
      "POST /assets/resolve": { by: "content_hash", content_hash: "99aa99aa99aa", run_id: null },
    });
    render(<Panels />);
    act(() => {
      dropFile(new File(["x"], "shot.png", { type: "image/png" }));
    });
    expect(await screen.findByText(/did not say where shot.png lives/)).toBeInTheDocument();
    expect(posts(calls, "/assets/resolve")).toHaveLength(0);
    expect(screen.getByRole("tab", { name: "History" })).toHaveAttribute("aria-selected", "true");
    fireEvent.change(screen.getByTestId("resolve-path"), { target: { value: "D:\\plates\\shot.png" } });
    fireEvent.click(screen.getByRole("button", { name: "Resolve" }));
    expect(await screen.findByTestId("drop-match")).toHaveTextContent("exact content hash");
    expect(posts(calls, "/assets/resolve")[0].body).toEqual({ path: "D:\\plates\\shot.png" });
  });

  it("shows a daemon error and leaves non-image drops alone", async () => {
    openProject();
    const calls = mockApi({
      "GET /projects": [],
      "GET /projects/p1/runs": [],
      "POST /assets/resolve": jsonResponse({ detail: "path outside the allowed roots" }, 403),
    });
    render(<Panels />);
    act(() => {
      dropFile(new File(["x"], "notes.txt", { type: "text/plain" }));
    });
    expect(posts(calls, "/assets/resolve")).toHaveLength(0);
    act(() => {
      dropFile(imageWithPath("/etc/shot.png"));
    });
    expect(await screen.findByText("path outside the allowed roots")).toBeInTheDocument();
  });

  it("shows the drop overlay while a file is dragged over the window", () => {
    mockApi({ "GET /projects": [] });
    render(<Panels />);
    fireEvent.dragEnter(document.body, { dataTransfer: { types: ["Files"], files: [] } });
    expect(screen.getByTestId("drop-overlay")).toBeInTheDocument();
    fireEvent.dragLeave(document.body, { dataTransfer: { types: ["Files"], files: [] } });
    expect(screen.queryByTestId("drop-overlay")).toBeNull();
  });
});

// ------------------------------------------------------------------------------------------------ gate panel

describe("gate panel", () => {
  const gateRoute = (call: Call) =>
    call.search === "?n=1"
      ? { report: { ...REPORT, verdict: "pass", checks: [REPORT.checks[0]] }, n: 1, shipped: true, versions: [{ n: 1, shipped: true }, { n: 2, shipped: false }] }
      : { report: REPORT, n: 2, shipped: false, versions: [{ n: 1, shipped: true }, { n: 2, shipped: false }] };

  it("asks for a run when none is selected", () => {
    mockApi({});
    render(<GatePanel />);
    expect(screen.getByText("Select a run in History to see its gate report.")).toBeInTheDocument();
  });

  it("lists checks failures first with value, threshold and a status that names itself", async () => {
    usePanelsStore.setState({ selectedRunId: "r1" });
    mockApi({ "GET /runs/r1/gate": gateRoute });
    render(<GatePanel />);
    const rows = await screen.findAllByTestId("gate-check");
    expect(rows.map((r) => r.dataset.metric)).toEqual(["invention_flags", "texture.fine_ratio", "registration.residual_px"]);
    expect(rows.map((r) => r.dataset.status)).toEqual(["fail", "advisory", "pass"]);
    expect(rows[0]).toHaveTextContent("= 0");
    expect(rows[0]).toHaveTextContent("fail");
    expect(rows[1]).toHaveTextContent("0.4 to 0.6");
    expect(rows[2]).toHaveTextContent("0.4237");
    expect(rows[2]).toHaveTextContent("<= 1 px");
    expect(screen.getByTestId("gate-verdict")).toHaveTextContent("fail");
    expect(screen.getByTestId("backend-notes")).toHaveTextContent("flow backend: dis (RAFT needs CUDA)");
  });

  it("marks shipped versions with a lock and switches version on a chip click", async () => {
    usePanelsStore.setState({ selectedRunId: "r1" });
    const calls = mockApi({ "GET /runs/r1/gate": gateRoute });
    render(<GatePanel />);
    const chips = await screen.findAllByTestId("gate-version");
    expect(chips.map((c) => c.textContent)).toEqual(["v1", "v2"]);
    expect(within(chips[0]).getByTestId("shipped-lock")).toBeInTheDocument();
    expect(within(chips[1]).queryByTestId("shipped-lock")).toBeNull();
    expect(chips[1]).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(chips[0]);
    await waitFor(() => expect(screen.getAllByTestId("gate-check")).toHaveLength(1));
    expect(calls.filter((c) => c.path === "/runs/r1/gate").map((c) => c.search)).toEqual(["", "?n=1"]);
    expect(screen.getByText(/Shipped: a rescore writes a new version/)).toBeInTheDocument();
  });

  it("'Open tiles' hands the flagged points to the viewer", async () => {
    usePanelsStore.setState({ selectedRunId: "r1" });
    mockApi({ "GET /runs/r1/gate": gateRoute });
    render(<GatePanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open tiles/ }));
    expect(usePanelsStore.getState().viewer).toEqual({ mode: "tiles", tiles: [{ x: 120.5, y: 80 }] });
  });

  it("disables 'Open tiles' when nothing was flagged", async () => {
    usePanelsStore.setState({ selectedRunId: "r1" });
    mockApi({ "GET /runs/r1/gate": { report: { ...REPORT, metrics: {} }, n: 1, versions: [1] } });
    render(<GatePanel />);
    expect(await screen.findByRole("button", { name: /Open tiles/ })).toBeDisabled();
  });

  it("'Rescore' posts, then shows the newest version", async () => {
    usePanelsStore.setState({ selectedRunId: "r1" });
    let version = 1;
    const calls = mockApi({
      "GET /runs/r1/gate": () => ({ report: REPORT, n: version, versions: Array.from({ length: version }, (_, i) => ({ n: i + 1, shipped: false })) }),
      "POST /runs/r1/gate/rescore": () => {
        version = 2;
        return { ok: true };
      },
    });
    render(<GatePanel />);
    await screen.findAllByTestId("gate-version");
    fireEvent.click(screen.getByRole("button", { name: "Rescore" }));
    await waitFor(() => expect(screen.getAllByTestId("gate-version")).toHaveLength(2));
    expect(posts(calls, "/runs/r1/gate/rescore")).toHaveLength(1);
  });

  it("reports a failed rescore and a run with no report yet", async () => {
    usePanelsStore.setState({ selectedRunId: "r1" });
    mockApi({
      "GET /runs/r1/gate": { report: REPORT, n: 1, versions: [1] },
      "POST /runs/r1/gate/rescore": jsonResponse({ detail: "gates are busy" }, 409),
    });
    const { unmount } = render(<GatePanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Rescore" }));
    expect(await screen.findByText("Rescore failed: gates are busy")).toBeInTheDocument();
    unmount();

    mockApi({ "GET /runs/r1/gate": jsonResponse({ error: "not found" }, 404) });
    render(<GatePanel />);
    expect(await screen.findByText("No gate report for this run yet.")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------------------------------------ custody panel

describe("custody panel", () => {
  it("shows the project grade, each engine route's grade and the outbound tail", async () => {
    openProject("p1");
    usePanelsStore.setState({
      runs: [run("r1"), run("r2"), run("r3", { route: { engine_id: "veo-3.1" }, quote: { price_usd: 1, custody_grade: "D" } })],
    });
    const calls = mockApi({
      "GET /projects": [{ project_id: "p1", custody: { nda: true, allowed_grades: ["C0", "C1"] }, engine_pins: {} }],
      "GET /projects/p1/outbound": {
        entries: [
          {
            id: "01J1",
            at: "2026-10-17T10:01:00Z",
            run_id: "r1",
            host: "generativelanguage.googleapis.com",
            method: "POST",
            bytes: 1536,
            bytes_in: 2_097_152,
            custody_grade: "C2",
            opaque_ids: ["aaaa1111bbbb2222", "cccc3333dddd4444"],
            status: 200,
          },
          { id: "01J2", host: "api.atlascloud.ai", custody_grade: "C2", bytes: 10, error: "timeout", opaque_ids: [] },
        ],
      },
    });
    render(<CustodyPanel />);
    const grade = await screen.findByTestId("project-grade");
    expect(grade).toHaveTextContent("NDA");
    expect(grade).toHaveTextContent("C0");
    expect(grade).not.toHaveTextContent("C2");
    const engines = screen.getAllByTestId("engine-grade");
    expect(engines.map((e) => e.dataset.engine)).toEqual(["nano-banana-2.1", "veo-3.1"]);
    expect(engines[0]).toHaveTextContent("C2");
    expect(engines[0]).toHaveTextContent("outside the project's allowed grades");
    expect(engines[1]).toHaveTextContent("D");
    const rows = await screen.findAllByTestId("outbound-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("generativelanguage.googleapis.com");
    expect(rows[0]).toHaveTextContent("out 1.5 KB");
    expect(rows[0]).toHaveTextContent("in 2.0 MB");
    expect(within(rows[0]).getByTestId("outbound-ids")).toHaveTextContent("aaaa1111, cccc3333");
    expect(rows[1]).toHaveTextContent("error: timeout");
    expect(within(rows[1]).getByTestId("outbound-ids")).toHaveTextContent("no opaque ids");
    expect(calls.find((c) => c.path === "/projects/p1/outbound")?.search).toBe("?tail=50");
  });

  it("says nothing left the machine when the log is empty, and asks for a project first", async () => {
    mockApi({});
    const { unmount } = render(<CustodyPanel />);
    expect(screen.getByText("Open a project to see its custody.")).toBeInTheDocument();
    unmount();

    openProject("p1");
    mockApi({ "GET /projects": [], "GET /projects/p1/outbound": [] });
    render(<CustodyPanel />);
    expect(await screen.findByText("No outbound requests logged.")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------------------------------------ verdict keys

describe("verdict keys and the scrub gate", () => {
  const IMAGE = { width: 4000, height: 2000 };
  const VIEWPORT = { width: 1000, height: 500 };
  const view = (x: number, y: number) =>
    act(() => usePanelsStore.getState().observeViewer({ zoom: 1, pan: { x, y } }, IMAGE, VIEWPORT));
  const seeAllQuadrants = () => {
    view(1000, 500);
    view(3000, 500);
    view(1000, 1500);
    view(3000, 1500);
  };
  const key = (k: string, init: KeyboardEventInit = {}, target: Element = document.body) =>
    fireEvent.keyDown(target, { key: k, ...init });

  const routes = () => ({
    "POST /runs/r1/scrub": { scrub_log_id: "scrub-9" },
    "POST /runs/r1/verdict": { ok: true },
  });

  beforeEach(() => {
    usePanelsStore.setState({ selectedRunId: "r1" });
  });

  it("refuses a verdict until a full scrub is logged, and enables the box only after four quadrants at 100%", async () => {
    const calls = mockApi(routes());
    render(<VerdictPanel />);
    const box = screen.getByTestId("full-scrub");
    expect(box).toBeDisabled();
    key("1");
    expect(await screen.findByRole("alert")).toHaveTextContent("a full scrub must be logged before a verdict");
    expect(posts(calls, "/runs/r1/verdict")).toHaveLength(0);

    view(1000, 500);
    view(3000, 500);
    view(1000, 1500);
    expect(screen.getByTestId("scrub-count")).toHaveTextContent("3/4 quadrants at 100%");
    expect(box).toBeDisabled();
    view(3000, 1500);
    expect(screen.getByTestId("scrub-count")).toHaveTextContent("4/4 quadrants at 100%");
    expect(box).toBeEnabled();
    expect(posts(calls, "/runs/r1/scrub")).toHaveLength(0);
  });

  it("ticking the box logs the scrub, and 1 then records Ship with that scrub", async () => {
    const calls = mockApi(routes());
    render(<VerdictPanel />);
    seeAllQuadrants();
    fireEvent.click(screen.getByTestId("full-scrub"));
    await waitFor(() => expect(screen.getByTestId("full-scrub")).toBeChecked());
    expect(posts(calls, "/runs/r1/scrub")[0].body).toEqual({ quadrants_visited: 4, zoom_100: true });
    expect(scrubLogFor("r1")).toEqual({ id: "scrub-9", fullScrub: true });
    expect(screen.getByTestId("full-scrub")).toBeDisabled();

    key("1");
    await waitFor(() => expect(posts(calls, "/runs/r1/verdict")).toHaveLength(1));
    expect(posts(calls, "/runs/r1/verdict")[0].body).toEqual({ verdict: "ship", reasons: [], scrub_log_id: "scrub-9", rater: "human" });
    expect(await screen.findByTestId("verdict-outcome")).toHaveTextContent("Ship recorded");
    expect(usePanelsStore.getState().verdicts).toEqual({ r1: "ship" });
  });

  it("2 and 3 ask for reason codes; Enter submits them and Esc cancels", async () => {
    const calls = mockApi(routes());
    usePanelsStore.setState({ scrubLogs: { r1: "scrub-9" } });
    render(<VerdictPanel />);
    key("2");
    const picker = await screen.findByTestId("reason-picker");
    expect(posts(calls, "/runs/r1/verdict")).toHaveLength(0);
    fireEvent.click(within(picker).getByLabelText("A2 texture crawl"));
    fireEvent.click(within(picker).getByLabelText("P4 texture band"));
    key("Enter");
    await waitFor(() => expect(posts(calls, "/runs/r1/verdict")).toHaveLength(1));
    expect(posts(calls, "/runs/r1/verdict")[0].body).toEqual({
      verdict: "fix",
      reasons: ["A2", "P4"],
      scrub_log_id: "scrub-9",
      rater: "human",
    });
    await waitFor(() => expect(screen.queryByTestId("reason-picker")).toBeNull());

    key("3");
    expect(await screen.findByTestId("reason-picker")).toHaveTextContent("Render-it");
    key("Escape");
    expect(screen.queryByTestId("reason-picker")).toBeNull();
    expect(posts(calls, "/runs/r1/verdict")).toHaveLength(1);
  });

  it("the buttons do what the keys do", async () => {
    const calls = mockApi(routes());
    usePanelsStore.setState({ scrubLogs: { r1: "scrub-9" } });
    render(<VerdictPanel />);
    fireEvent.click(screen.getByTestId("verdict-render_it"));
    fireEvent.click(await screen.findByLabelText("P2 invented edge"));
    fireEvent.click(screen.getByRole("button", { name: "Submit Render-it" }));
    await waitFor(() => expect(posts(calls, "/runs/r1/verdict")).toHaveLength(1));
    expect(posts(calls, "/runs/r1/verdict")[0].body).toMatchObject({ verdict: "render_it", reasons: ["P2"] });
  });

  it("ignores digits typed in a text box, with a modifier, and plain digits inside the viewer", async () => {
    const calls = mockApi(routes());
    usePanelsStore.setState({ scrubLogs: { r1: "scrub-9" } });
    render(
      <>
        <VerdictPanel />
        <div data-testid="viewer" tabIndex={0} />
      </>,
    );
    key("1", {}, screen.getByLabelText("Rater"));
    key("1", { ctrlKey: true });
    key("1", { metaKey: true });
    key("1", {}, screen.getByTestId("viewer"));
    key("4");
    await act(async () => {});
    expect(posts(calls, "/runs/r1/verdict")).toHaveLength(0);
  });

  it("Shift+digit gives a verdict even from the viewer, where plain 1 means 100% zoom", async () => {
    const calls = mockApi(routes());
    usePanelsStore.setState({ scrubLogs: { r1: "scrub-9" } });
    render(
      <>
        <VerdictPanel />
        <div data-testid="viewer" tabIndex={0} />
      </>,
    );
    key("!", { code: "Digit1", shiftKey: true }, screen.getByTestId("viewer"));
    await waitFor(() => expect(posts(calls, "/runs/r1/verdict")).toHaveLength(1));
    expect(posts(calls, "/runs/r1/verdict")[0].body).toMatchObject({ verdict: "ship" });
  });

  it("needs a selected candidate, and shows the daemon's refusal", async () => {
    usePanelsStore.setState({ selectedRunId: null });
    const calls = mockApi(routes());
    const { unmount } = render(<VerdictPanel />);
    expect(screen.getByTestId("verdict-run")).toHaveTextContent("no candidate selected");
    expect(screen.getByTestId("verdict-ship")).toBeDisabled();
    key("1");
    expect(await screen.findByRole("alert")).toHaveTextContent("select a candidate first");
    expect(calls).toHaveLength(0);
    unmount();

    usePanelsStore.setState({ selectedRunId: "r1", scrubLogs: { r1: "scrub-9" }, outcome: null });
    mockApi({ "POST /runs/r1/verdict": jsonResponse({ detail: "scrub log not found" }, 409) });
    render(<VerdictPanel />);
    key("1");
    expect(await screen.findByRole("alert")).toHaveTextContent("verdict not recorded: scrub log not found");
    expect(usePanelsStore.getState().verdicts).toEqual({});
  });

  it("a new candidate starts with an empty scrub and a closed picker", async () => {
    mockApi(routes());
    usePanelsStore.setState({ scrubLogs: { r1: "scrub-9" } });
    render(<VerdictPanel />);
    seeAllQuadrants();
    key("2");
    await screen.findByTestId("reason-picker");
    act(() => usePanelsStore.getState().selectRun("r2"));
    expect(screen.queryByTestId("reason-picker")).toBeNull();
    expect(screen.getByTestId("scrub-count")).toHaveTextContent("0/4 quadrants at 100%");
    expect(screen.getByTestId("full-scrub")).toBeDisabled();
  });

  it("keeps the rater between sessions", () => {
    mockApi({});
    render(<VerdictPanel />);
    fireEvent.change(screen.getByLabelText("Rater"), { target: { value: "aasish" } });
    expect(window.localStorage.getItem("plate.rater")).toBe("aasish");
    expect(usePanelsStore.getState().rater).toBe("aasish");
  });
});

// ------------------------------------------------------------------------------------------------ viewer wiring

describe("connected viewer", () => {
  const BIG = { url: "/api/v0/assets/aa?fmt=rgba16f", width: 4000, height: 2000, encoding: "srgb8" as const };

  it("feeds the viewer's zoom and pan into the scrub progress of the selected run", () => {
    mockApi({});
    usePanelsStore.setState({ selectedRunId: "r1" });
    render(<ConnectedViewer a={BIG} />);
    expect(usePanelsStore.getState().scrub).toEqual(EMPTY_SCRUB); // fit of a big image is below 100%
    const viewer = screen.getByTestId("viewer");
    fireEvent.keyDown(viewer, { key: "1" }); // 100% at the image centre: no quadrant centre in view
    expect(usePanelsStore.getState().scrub.zoom100).toBe(true);
    expect(quadrantsVisited(usePanelsStore.getState().scrub)).toBe(0);
    for (let i = 0; i < 3; i += 1) fireEvent.keyDown(viewer, { key: "ArrowLeft", shiftKey: true });
    for (let i = 0; i < 3; i += 1) fireEvent.keyDown(viewer, { key: "ArrowUp", shiftKey: true });
    expect(usePanelsStore.getState().scrub.quadrants).toEqual([true, false, false, false]);
  });

  it("counts a small image as inspected when fit already shows it above 100%", () => {
    mockApi({});
    usePanelsStore.setState({ selectedRunId: "r1" });
    render(<ConnectedViewer a={{ ...BIG, width: 640, height: 360 }} />);
    expect(isFullScrub(usePanelsStore.getState().scrub)).toBe(true);
  });

  it("takes the tiles mode and points from the store and reports the viewer's mode back", () => {
    mockApi({});
    usePanelsStore.setState({ selectedRunId: "r1" });
    render(<ConnectedViewer a={BIG} />);
    act(() => usePanelsStore.getState().openTiles([{ x: 10, y: 20 }]));
    expect(usePanelsStore.getState().viewer.mode).toBe("tiles");
    fireEvent.keyDown(screen.getByTestId("viewer"), { key: "1" });
    expect(usePanelsStore.getState().scrub.zoom100).toBe(true);
  });
});

// ------------------------------------------------------------------------------------------------ timer

describe("timer", () => {
  const tick = () => act(async () => {});

  it("Space on the focused panel starts and stops, and today's minutes come from the daemon", async () => {
    let today: unknown = { rows: [{ deliverable_id: "hero-still", minutes: 12.5 }] };
    const calls = mockApi({
      "GET /timer/today": () => today,
      "POST /timer": (c: Call) => {
        if ((c.body as { action: string }).action === "stop") today = [{ deliverable_id: "hero-still", minutes: 14 }];
        return { ok: true };
      },
    });
    render(<TimerPanel />);
    expect(await screen.findByText("12.5 min")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Deliverable id"), { target: { value: "hero-still" } });
    const panel = screen.getByTestId("timer-panel");
    panel.focus();
    fireEvent.keyDown(panel, { key: " " });
    await waitFor(() => expect(screen.getByTestId("timer-state")).toHaveTextContent("Running 00:00"));
    expect(posts(calls, "/timer")[0].body).toEqual({ deliverable_id: "hero-still", action: "start" });
    expect(window.localStorage.getItem("plate.timer.deliverable")).toBe("hero-still");
    expect(screen.getByLabelText("Deliverable id")).toBeDisabled();

    fireEvent.keyDown(panel, { key: " " });
    await waitFor(() => expect(screen.getByTestId("timer-state")).toHaveTextContent("Stopped"));
    expect(posts(calls, "/timer")[1].body).toEqual({ deliverable_id: "hero-still", action: "stop" });
    expect(await screen.findByText("14.0 min")).toBeInTheDocument();
  });

  it("does not start without a deliverable, and leaves Space alone inside the text box", async () => {
    const calls = mockApi({ "GET /timer/today": [], "POST /timer": { ok: true } });
    render(<TimerPanel />);
    await tick();
    const panel = screen.getByTestId("timer-panel");
    const input = screen.getByLabelText("Deliverable id");
    fireEvent.change(input, { target: { value: "hero-still" } });
    fireEvent.keyDown(input, { key: " " }); // typing a space in the box must not start the timer
    await tick();
    expect(posts(calls, "/timer")).toHaveLength(0);
    expect(screen.getByTestId("timer-state")).toHaveTextContent("Stopped");
    fireEvent.change(input, { target: { value: "  " } });
    fireEvent.keyDown(panel, { key: " " });
    expect(await screen.findByRole("alert")).toHaveTextContent("Name the deliverable first.");
    expect(posts(calls, "/timer")).toHaveLength(0);
  });

  it("reads today's minutes from a plain map too, and shows the daemon's refusal", async () => {
    mockApi({
      "GET /timer/today": { "hero-still": 3, "walkthrough-02": 41.25 },
      "POST /timer": jsonResponse({ detail: "another deliverable is running" }, 409),
    });
    render(<TimerPanel />);
    expect(await screen.findByText("41.3 min")).toBeInTheDocument();
    expect(screen.getAllByTestId("timer-row").map((r) => r.firstChild?.textContent)).toEqual(["hero-still", "walkthrough-02"]);
    fireEvent.change(screen.getByLabelText("Deliverable id"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("another deliverable is running");
    expect(screen.getByTestId("timer-state")).toHaveTextContent("Stopped");
  });
});

// ------------------------------------------------------------------------------------------------ the whole area

describe("panels area", () => {
  it("shows one panel per tab and keeps the verdict keys on every tab", async () => {
    mockApi({ "GET /projects": [] });
    render(<Panels />);
    expect(screen.getByTestId("project-panel")).toBeInTheDocument();
    expect(screen.getByTestId("verdict-panel")).toBeInTheDocument();
    for (const [name, id] of [
      ["History", "history-panel"],
      ["Gate", "gate-panel"],
      ["Custody", "custody-panel"],
      ["Timer", "timer-panel"],
    ] as const) {
      fireEvent.click(screen.getByRole("tab", { name }));
      expect(screen.getByRole("tab", { name })).toHaveAttribute("aria-selected", "true");
      expect(screen.getByTestId(id)).toBeInTheDocument();
      expect(screen.getByTestId("verdict-panel")).toBeInTheDocument();
    }
  });

  it("drops the selected candidate when a different project is opened", async () => {
    mockApi({ "GET /projects": [], "GET /projects/p1/runs": [run("r1")], "GET /projects/p2/runs": [] });
    openProject("p1");
    render(<Panels />);
    act(() => usePanelsStore.getState().selectRun("r1"));
    expect(usePanelsStore.getState().selectedRunId).toBe("r1");
    act(() => useGraphStore.getState().loadProject(GRAPH, "p2"));
    await waitFor(() => expect(usePanelsStore.getState().selectedRunId).toBeNull());
    await waitFor(() => expect(usePanelsStore.getState().runsProjectId).toBe("p2"));
  });
});
