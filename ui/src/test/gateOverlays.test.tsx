// T109: the gate overlays reach the viewer. `GET /runs/{id}/gate` lists the G-buffer edge masks and the ΔE00 map the
// daemon stored at gate time (`overlays`); `gateOverlays` maps them onto the viewer's `edges` and `heatmap` inputs, the
// compare slot does so for a candidate against its own plate, and <ConnectedViewer> reads the selected run's when no
// parent passes them. `e` and `h` then draw; a run gated before T109 keeps the viewer's notices. The daemon is a fake;
// jsdom has no WebGL, so the viewer runs its keyboard state machine without a GPU (no texture is fetched).

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CompareViewer } from "../canvas/CompareViewer";
import { type CandidateRun, useRunStore } from "../canvas/runStore";
import { ConnectedViewer, DE00_FORMAT, EDGE_FORMAT, gateOverlays } from "../panels/ConnectedViewer";
import { initialPanelsState, usePanelsStore } from "../panels/store";
import { createInitialGraph, useGraphStore } from "../store";

// ------------------------------------------------------------------------------------------------ fakes

interface Call {
  method: string;
  path: string;
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
      calls.push({ method, path });
      const route = routes[`${method} ${path}`];
      if (route === undefined) return json({ error: `no stub for ${method} ${path}` }, 404);
      const out = typeof route === "function" ? (route as (c: Call) => unknown)({ method, path }) : route;
      return out instanceof Response ? out : json(out);
    }),
  );
  return calls;
}

const gateCalls = (calls: Call[], runId: string) =>
  calls.filter((c) => c.method === "GET" && c.path === `/runs/${runId}/gate`);

// ------------------------------------------------------------------------------------------------ fixtures

const H_PLATE_EDGES = "a".repeat(64);
const H_CAND_EDGES = "b".repeat(64);
const H_DE00 = "c".repeat(64);

/** The block `plate_daemon.gating` writes (schema 1), as `GET /runs/{id}/gate` lifts it to `overlays`. */
const OVERLAYS = {
  schema: 1,
  width: 640,
  height: 360,
  edges_plate: { hash: H_PLATE_EDGES, format: "png8", media_type: "image/png", width: 640, height: 360 },
  edges_candidate: { hash: H_CAND_EDGES, format: "png8", media_type: "image/png", width: 640, height: 360 },
  de00_map: {
    hash: H_DE00,
    format: "r16f",
    media_type: "application/octet-stream",
    width: 640,
    height: 360,
    unit: "dE00",
    scale: 1,
    max: 4.2,
  },
  notes: [],
};

const report = (overlays: unknown) => ({
  gate_version: "1",
  verdict: "pass",
  checks: [{ metric_id: "outside_mask.de00", value: 0, status: "pass" }],
  metrics: overlays ? { overlays } : {},
  n: 1,
  versions: [1],
  shipped: false,
  overlays: overlays ?? null,
});

const PLATE = { width: 640, height: 360 };

const GRAPH = {
  schema: 1 as const,
  nodes: [
    { kind: "Plate" as const, id: "plate-1", label: "Plate", packetId: "room_a", tier: "A" as const, outputHash: "h_plate", outputSize: PLATE },
    { kind: "Edit" as const, id: "edit-1", label: "Edit" },
  ],
  edges: [{ id: "plate-1->edit-1", source: "plate-1", target: "edit-1", sourceHandle: null, targetHandle: null }],
};

const run = (id: string, over: Partial<CandidateRun> = {}): CandidateRun => ({
  id,
  state: "DONE",
  node_id: "edit-1",
  outputs: [`h_${id}`],
  route: { engine_id: "fake" },
  attempt: 1,
  ...over,
});

/** A candidate against its own plate, bound to the panels' selection (what a strip click does). */
function compareWithPlate(runId: string): void {
  act(() => {
    useRunStore.getState().setCompareSide("b", { kind: "run", runId });
    usePanelsStore.getState().selectRun(runId);
  });
}

const viewer = () => screen.getByTestId("viewer");

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  useRunStore.getState().reset();
  usePanelsStore.setState(initialPanelsState());
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ------------------------------------------------------------------------------------------------ the mapping

describe("gateOverlays", () => {
  it("maps the gate's overlay block onto the viewer's edges and heatmap, on the token-gated assets route", () => {
    expect(EDGE_FORMAT).toBe("png8");
    expect(DE00_FORMAT).toBe("r16f");
    expect(gateOverlays(report(OVERLAYS))).toEqual({
      edges: { plate: `/api/v0/assets/${H_PLATE_EDGES}`, candidate: `/api/v0/assets/${H_CAND_EDGES}` },
      heatmap: { url: `/api/v0/assets/${H_DE00}`, width: 640, height: 360 },
    });
  });

  it("reads the stored report's metrics.overlays when the answer has no top-level block", () => {
    const { overlays: _lifted, ...stored } = report(OVERLAYS);
    expect(gateOverlays(stored)?.heatmap).toEqual({ url: `/api/v0/assets/${H_DE00}`, width: 640, height: 360 });
  });

  it("gives nothing for a report gated before T109, a failed store, or a GET that found no report", () => {
    expect(gateOverlays(report(null))).toBeUndefined();
    expect(gateOverlays({ ...report(null), metrics: { gbuffer: { n_ref: 10 } } })).toBeUndefined();
    expect(gateOverlays(report({ schema: 1, error: "OSError: disk full" }))).toBeUndefined();
    expect(gateOverlays(null)).toBeUndefined();
  });

  it("a tier C report has the heatmap only; formats and scales this viewer cannot draw are left out", () => {
    const tierC = { ...OVERLAYS, edges_plate: null, edges_candidate: null };
    expect(gateOverlays(report(tierC))).toEqual({
      heatmap: { url: `/api/v0/assets/${H_DE00}`, width: 640, height: 360 },
    });
    const png16 = { ...OVERLAYS, de00_map: { ...OVERLAYS.de00_map, format: "png16", scale: 0.01 } };
    expect(gateOverlays(report(png16))?.heatmap).toBeUndefined();
    const scaled = { ...OVERLAYS, de00_map: { ...OVERLAYS.de00_map, scale: 0.01 } };
    expect(gateOverlays(report(scaled))?.heatmap).toBeUndefined();
    const noSize = { ...OVERLAYS, de00_map: { ...OVERLAYS.de00_map, width: 0 } };
    expect(gateOverlays(report(noSize))?.heatmap).toBeUndefined();
    const plateOnly = { ...OVERLAYS, edges_candidate: { ...OVERLAYS.edges_candidate, format: "jpeg" } };
    expect(gateOverlays(report(plateOnly))?.edges).toEqual({ plate: `/api/v0/assets/${H_PLATE_EDGES}` });
  });
});

// ------------------------------------------------------------------------------------------------ the compare slot

describe("the compare slot draws the gate's overlays", () => {
  function openCandidate(runOver: Partial<CandidateRun> = {}) {
    useGraphStore.getState().loadProject(GRAPH, "p1");
    useRunStore.getState().startRuns("edit-1", [run("r1", runOver)]);
    render(<CompareViewer />);
    compareWithPlate("r1");
    expect(screen.getByTestId("compare")).toHaveAttribute("data-bound", "true");
  }

  it("`e` and `h` draw once the candidate's gate lists its edge masks and ΔE00 map", async () => {
    const calls = mockApi({ "GET /runs/r1/gate": report(OVERLAYS) });
    openCandidate();
    await waitFor(() => expect(gateCalls(calls, "r1")).toHaveLength(1));
    await waitFor(() => {
      fireEvent.keyDown(viewer(), { key: "e" });
      expect(viewer()).toHaveAttribute("data-mode", "edges");
    });
    expect(screen.queryByTestId("viewer-notice")).toBeNull();
    expect(usePanelsStore.getState().viewer.mode).toBe("edges");
    fireEvent.keyDown(viewer(), { key: "h" });
    expect(viewer()).toHaveAttribute("data-mode", "de00");
    expect(usePanelsStore.getState().viewer.mode).toBe("de00");
    // the slot read the gate once; <ConnectedViewer> did not read it again
    expect(gateCalls(calls, "r1")).toHaveLength(1);
  });

  it("a run gated before T109 keeps the 'no edge masks for this view' notice", async () => {
    const calls = mockApi({ "GET /runs/r1/gate": report(null) });
    openCandidate();
    await waitFor(() => expect(gateCalls(calls, "r1")).toHaveLength(1));
    await act(async () => {});
    fireEvent.keyDown(viewer(), { key: "e" });
    expect(screen.getByTestId("viewer-notice")).toHaveTextContent("no edge masks for this view");
    expect(viewer()).toHaveAttribute("data-mode", "wipe");
    fireEvent.keyDown(viewer(), { key: "h" });
    expect(screen.getByTestId("viewer-notice")).toHaveTextContent("no ΔE00 map for this view");
    expect(viewer()).toHaveAttribute("data-mode", "wipe");
  });

  it("a take still running is read again when it finishes", async () => {
    let gated = false;
    const calls = mockApi({
      "GET /runs/r1/gate": () => (gated ? report(OVERLAYS) : json({ error: "no_report" }, 404)),
    });
    openCandidate({ state: "RUNNING" });
    await waitFor(() => expect(gateCalls(calls, "r1")).toHaveLength(1));
    await act(async () => {});
    fireEvent.keyDown(viewer(), { key: "e" });
    expect(screen.getByTestId("viewer-notice")).toHaveTextContent("no edge masks for this view");
    gated = true;
    act(() => useRunStore.getState().startRuns("edit-1", [run("r1", { state: "DONE" })]));
    await waitFor(() => expect(gateCalls(calls, "r1")).toHaveLength(2));
    await waitFor(() => {
      fireEvent.keyDown(viewer(), { key: "e" });
      expect(viewer()).toHaveAttribute("data-mode", "edges");
    });
  });

  it("a look-only pair (two takes) has no overlays and never reads a gate for them", async () => {
    const calls = mockApi({ "GET /runs/r1/gate": report(OVERLAYS), "GET /runs/r2/gate": report(OVERLAYS) });
    useGraphStore.getState().loadProject(GRAPH, "p1");
    useRunStore.getState().startRuns("edit-1", [run("r1"), run("r2", { attempt: 2 })]);
    render(<CompareViewer />);
    act(() => {
      useRunStore.getState().setCompareSide("a", { kind: "run", runId: "r1" });
      useRunStore.getState().setCompareSide("b", { kind: "run", runId: "r2" });
    });
    expect(useRunStore.getState().scrubEligible).toBe(false);
    await act(async () => {});
    fireEvent.keyDown(viewer(), { key: "e" });
    expect(screen.getByTestId("viewer-notice")).toHaveTextContent("no edge masks for this view");
    expect(gateCalls(calls, "r1")).toHaveLength(0);
    expect(gateCalls(calls, "r2")).toHaveLength(0);
  });
});

// ------------------------------------------------------------------------------------------------ ConnectedViewer

describe("ConnectedViewer on its own", () => {
  const A = { url: "/api/v0/assets/h_plate", width: 640, height: 360, encoding: "png16" as const };
  const B = { url: "/api/v0/assets/h_r1", width: 640, height: 360, encoding: "png16" as const };

  it("reads the selected run's gate overlays when no parent passes them", async () => {
    const calls = mockApi({ "GET /runs/r1/gate": report(OVERLAYS) });
    usePanelsStore.setState({ selectedRunId: "r1" });
    render(<ConnectedViewer a={A} b={B} />);
    await waitFor(() => expect(gateCalls(calls, "r1")).toHaveLength(1));
    await waitFor(() => {
      fireEvent.keyDown(viewer(), { key: "h" });
      expect(viewer()).toHaveAttribute("data-mode", "de00");
    });
  });

  it("takes a parent's overlays as given and reads nothing", async () => {
    const calls = mockApi({ "GET /runs/r1/gate": report(null) });
    usePanelsStore.setState({ selectedRunId: "r1" });
    render(<ConnectedViewer a={A} b={B} overlays={gateOverlays(report(OVERLAYS))} />);
    fireEvent.keyDown(viewer(), { key: "e" });
    expect(viewer()).toHaveAttribute("data-mode", "edges");
    await act(async () => {});
    expect(gateCalls(calls, "r1")).toHaveLength(0);
  });

  it("never shows one run's overlays on the next: switching to an old run drops them", async () => {
    const calls = mockApi({ "GET /runs/r1/gate": report(OVERLAYS), "GET /runs/r2/gate": report(null) });
    usePanelsStore.setState({ selectedRunId: "r1" });
    render(<ConnectedViewer a={A} b={B} />);
    await waitFor(() => {
      fireEvent.keyDown(viewer(), { key: "e" });
      expect(viewer()).toHaveAttribute("data-mode", "edges");
    });
    fireEvent.keyDown(viewer(), { key: "e" }); // back to wipe
    act(() => usePanelsStore.getState().selectRun("r2"));
    fireEvent.keyDown(viewer(), { key: "e" });
    expect(viewer()).not.toHaveAttribute("data-mode", "edges");
    expect(screen.getByTestId("viewer-notice")).toHaveTextContent("no edge masks for this view");
    await waitFor(() => expect(gateCalls(calls, "r2")).toHaveLength(1));
  });
});
