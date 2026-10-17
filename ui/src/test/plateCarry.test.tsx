// T96: the Plate editor's carry-over button. A linked packet whose summary names a `supersedes` offers
// "Carry over approved layers from <old id>"; the click posts `POST /packets/{id}/carry-over {from_packet_id}` and the
// editor lists one `carried | dropped · reason` row per layer, then reads the project's packets again. The daemon is a
// fetch stub that answers by "METHOD /path" and records every call. No daemon, no GPU.

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearThumbCache } from "../canvas/assets";
import { carryOver, parseCarryResult, parseCarryResults, PlateEditor } from "../editors/PlateEditor";
import { createInitialGraph, useGraphStore } from "../store";

interface Call {
  method: string;
  path: string;
  body: unknown;
}

type Route = unknown | ((call: Call) => unknown | Promise<unknown>);

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** Answers `METHOD /path` (the /api/v0 prefix and the query string are stripped); anything else is a 404. */
function mockApi(routes: Record<string, Route>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://127.0.0.1");
      const method = (init?.method ?? "GET").toUpperCase();
      const path = url.pathname.replace(/^\/api\/v0/, "");
      const raw = init?.body;
      calls.push({ method, path, body: typeof raw === "string" ? JSON.parse(raw) : (raw ?? null) });
      const route = routes[`${method} ${path}`];
      if (route === undefined) return jsonResponse({ error: `no stub for ${method} ${path}` }, 404);
      const out = typeof route === "function" ? await (route as (c: Call) => unknown)(calls[calls.length - 1]) : route;
      return out instanceof Response ? out : jsonResponse(out);
    }),
  );
  return calls;
}

const called = (calls: Call[], method: string, path: string): Call[] => calls.filter((c) => c.method === method && c.path === path);

const HASH = (c: string): string => c.repeat(64);

const PACKET = (id: string, extra: Record<string, unknown> = {}) => ({
  packet_id: id,
  project_id: "p1",
  shot_id: id,
  kind: "still",
  tier: "A",
  status: "ingested",
  received_utc: "2026-10-18T09:12:40Z",
  camera_name: "CAM_room_a",
  plate: { role: "display_tiff16", relpath: "display.tif", blake3: HASH("a"), width: 640, height: 360 },
  supersedes: null,
  superseded_by: null,
  ...extra,
});

/** room_a, re-rendered as room_a_v2 after the client changed a wall. */
const PACKETS = [PACKET("pk_room_a", { superseded_by: "pk_room_a_v2" }), PACKET("pk_room_a_v2", { supersedes: "pk_room_a" })];

const summary = (id: string, supersedes: string | null) => ({
  packet_id: id,
  project_id: "p1",
  size: { width: 640, height: 360 },
  camera: { name: "CAM_room_a" },
  supersedes,
  legend: { entries: [{ id: "o:1", name: "VEG", editable: true }] },
});

const RESULTS = [
  {
    run_id: "run_people",
    ids: ["o:1", "o:2"],
    status: "carried",
    code: "carried",
    reason: "ids unchanged, id mask IoU 1.000, quick gates pass",
    iou: 1,
    new_run_id: "run_carried",
    output: HASH("c"),
  },
  { run_id: "run_wall", ids: ["o:9"], status: "dropped", code: "material_changed", reason: "material changed", new_run_id: null },
];

/** One Plate node (n1) on project p1, linked to `packetId`. */
function openPlate(packetId: string): void {
  useGraphStore
    .getState()
    .loadProject({ schema: 1, nodes: [{ id: "n1", kind: "Plate", label: "Plate", packetId, tier: "A" }], edges: [] }, "p1");
}

const baseRoutes = (extra: Record<string, Route> = {}): Record<string, Route> => ({
  "GET /projects/p1/packets": PACKETS,
  "GET /packets/pk_room_a/summary": summary("pk_room_a", null),
  "GET /packets/pk_room_a_v2/summary": summary("pk_room_a_v2", "pk_room_a"),
  "PUT /projects/p1/graph": (c: Call) => c.body,
  ...extra,
});

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  clearThumbCache();
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ------------------------------------------------------------------------------------------------ pure

describe("carry-over parsers", () => {
  it("reads a result row: run, ids, verdict, machine code, reason, new take", () => {
    expect(parseCarryResult(RESULTS[0])).toEqual({
      runId: "run_people",
      ids: ["o:1", "o:2"],
      status: "carried",
      code: "carried",
      reason: "ids unchanged, id mask IoU 1.000, quick gates pass",
      newRunId: "run_carried",
    });
    expect(parseCarryResult(RESULTS[1])).toMatchObject({ status: "dropped", code: "material_changed", reason: "material changed", newRunId: null });
  });

  it("reads anything that is not `carried` as dropped, and never throws on a row it does not know", () => {
    expect(parseCarryResult({ run_id: "r", status: "maybe" })).toMatchObject({ status: "dropped", reason: "no reason given" });
    expect(parseCarryResult({ status: "carried" })).toMatchObject({ runId: null, ids: [], reason: "carried" });
    expect(parseCarryResult("junk")).toBeNull();
    expect(parseCarryResult(null)).toBeNull();
  });

  it("takes the route's list or the packet.carried record's {results}", () => {
    expect(parseCarryResults(RESULTS).map((r) => r.status)).toEqual(["carried", "dropped"]);
    expect(parseCarryResults({ results: RESULTS }).map((r) => r.runId)).toEqual(["run_people", "run_wall"]);
    expect(parseCarryResults([RESULTS[0], 7, null])).toHaveLength(1);
    expect(parseCarryResults({ error: "nope" })).toEqual([]);
  });

  it("carryOver posts the predecessor, or an empty body for the manifest's default", async () => {
    const calls = mockApi({ "POST /packets/pk_a%2Fb/carry-over": RESULTS, "POST /packets/pk_x/carry-over": [] });
    expect((await carryOver("pk_a/b", "pk_old")).map((r) => r.status)).toEqual(["carried", "dropped"]);
    expect(await carryOver("pk_x")).toEqual([]);
    expect(calls.map((c) => c.body)).toEqual([{ from_packet_id: "pk_old" }, {}]);
  });
});

// ------------------------------------------------------------------------------------------------ the editor

describe("PlateEditor carry-over", () => {
  it("offers the button only for a packet that supersedes another", async () => {
    mockApi(baseRoutes());
    openPlate("pk_room_a");
    const view = render(<PlateEditor nodeId="n1" />);
    await screen.findByTestId("plate-list");
    await waitFor(() => expect(screen.getByTestId("plate-summary")).toBeInTheDocument());
    expect(screen.queryByTestId("plate-carry")).toBeNull();
    view.unmount();

    openPlate("pk_room_a_v2");
    render(<PlateEditor nodeId="n1" />);
    const button = await screen.findByTestId("plate-carry-button");
    expect(button).toHaveTextContent("Carry over approved layers from pk_room_a");
    expect(button).toBeEnabled();
    expect(screen.queryByTestId("plate-carry-list")).toBeNull();
  });

  it("posts the predecessor, lists carried | dropped · reason per layer and reads the packets again", async () => {
    const calls = mockApi(baseRoutes({ "POST /packets/pk_room_a_v2/carry-over": RESULTS }));
    openPlate("pk_room_a_v2");
    render(<PlateEditor nodeId="n1" />);
    fireEvent.click(await screen.findByTestId("plate-carry-button"));

    const list = await screen.findByTestId("plate-carry-list");
    expect(called(calls, "POST", "/packets/pk_room_a_v2/carry-over").map((c) => c.body)).toEqual([{ from_packet_id: "pk_room_a" }]);
    const ok = screen.getByTestId("plate-carry-run_people");
    expect(ok).toHaveAttribute("data-status", "carried");
    expect(ok).toHaveTextContent("carried · ids unchanged, id mask IoU 1.000, quick gates pass");
    expect(ok).toHaveTextContent("2 ids · run run_people");
    expect(ok).toHaveAttribute("title", expect.stringContaining("run_carried"));
    const dropped = screen.getByTestId("plate-carry-run_wall");
    expect(dropped).toHaveAttribute("data-status", "dropped");
    expect(dropped).toHaveTextContent("dropped · material changed");
    expect(dropped).toHaveTextContent("1 id · run run_wall");
    expect(list.querySelectorAll("li")).toHaveLength(2);
    expect(screen.getByTestId("plate-carry-count")).toHaveTextContent("1 carried · 1 dropped");

    // the picker was refreshed: the list was read once on mount and once after the carry
    await waitFor(() => expect(called(calls, "GET", "/projects/p1/packets")).toHaveLength(2));
    expect(screen.getByTestId("plate-carry-button")).toBeEnabled(); // pressing it again is safe: the daemon is idempotent
  });

  it("says so when the old plate has no accepted layers", async () => {
    mockApi(baseRoutes({ "POST /packets/pk_room_a_v2/carry-over": [] }));
    openPlate("pk_room_a_v2");
    render(<PlateEditor nodeId="n1" />);
    fireEvent.click(await screen.findByTestId("plate-carry-button"));
    expect(await screen.findByTestId("plate-carry-empty")).toHaveTextContent("nothing to carry: pk_room_a has no accepted layers");
    expect(screen.queryByTestId("plate-carry-list")).toBeNull();
  });

  it("shows the daemon's refusal and keeps the button usable", async () => {
    mockApi(
      baseRoutes({
        "POST /packets/pk_room_a_v2/carry-over": () =>
          jsonResponse({ error: "not_ingested", packet_id: "pk_room_a", status: "ingesting", detail: "pk_room_a is still ingesting" }, 409),
      }),
    );
    openPlate("pk_room_a_v2");
    render(<PlateEditor nodeId="n1" />);
    fireEvent.click(await screen.findByTestId("plate-carry-button"));
    expect(await screen.findByTestId("plate-carry-error")).toHaveTextContent("pk_room_a is still ingesting");
    expect(screen.queryByTestId("plate-carry-list")).toBeNull();
    expect(screen.getByTestId("plate-carry-button")).toBeEnabled();
  });

  it("holds the button while the daemon works and drops the list when the node is linked elsewhere", async () => {
    let release: (v: unknown) => void = () => undefined;
    const slow = new Promise((resolve) => {
      release = resolve;
    });
    mockApi(baseRoutes({ "POST /packets/pk_room_a_v2/carry-over": () => slow }));
    openPlate("pk_room_a_v2");
    render(<PlateEditor nodeId="n1" />);
    fireEvent.click(await screen.findByTestId("plate-carry-button"));
    await waitFor(() => expect(screen.getByTestId("plate-carry-button")).toBeDisabled());
    expect(screen.getByTestId("plate-carry-button")).toHaveTextContent("Carrying");
    release(RESULTS);
    await screen.findByTestId("plate-carry-list");

    // pick the old packet: the section goes (it supersedes nothing) and so do the results
    fireEvent.click(screen.getByTestId("plate-packet-pk_room_a"));
    await waitFor(() => expect(screen.queryByTestId("plate-carry")).toBeNull());
    expect(screen.queryByTestId("plate-carry-list")).toBeNull();
  });

  it("never offers the button to an unlinked Plate", async () => {
    mockApi(baseRoutes());
    useGraphStore.getState().loadProject({ schema: 1, nodes: [{ id: "n1", kind: "Plate", label: "Plate" }], edges: [] }, "p1");
    render(<PlateEditor nodeId="n1" />);
    await screen.findByTestId("plate-list");
    expect(screen.getByTestId("plate-unlinked")).toBeInTheDocument();
    expect(screen.queryByTestId("plate-carry")).toBeNull();
  });
});
