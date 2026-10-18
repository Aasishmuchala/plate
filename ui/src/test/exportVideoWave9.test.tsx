// T106 delivery and video surfaces: the Export editor's clip delivery, batch export and cutdowns; the player's
// no-download guard and Deliver…; VideoCompare's one clock; the Motion editor's end still, recipe and prompt, elements,
// pull-back zooms, single image, Veo by custody and the engine cards. The daemon is a fetch stub that answers by
// "METHOD /path" and records every call, so each test also pins the request the editor sent. jsdom has no media
// pipeline: currentTime is patched per element, play and pause are spies. No daemon, no GPU.

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useRunStore } from "../canvas/runStore";
import { type CustodyGrade, type LoadableNode } from "../nodeTypes";
import { FALLBACK_ENGINES } from "../makebar/engines";
import { useMakeStore } from "../makebar/makeStore";
import { createInitialGraph, useGraphStore } from "../store";
import { CLIP_FORMATS, isClipRun, parseCutdown, parseExportMany, parseTake } from "../editors/deliverApi";
import { ExportEditor } from "../editors/ExportEditor";
import { clayTimeMap, compareSourceFor, MotionEditor, motionWires } from "../editors/MotionEditor";
import {
  evaluateMove,
  hasToggle,
  motionRecipeLabel,
  parseMotionRecipe,
  presentMove,
  RISK_LADDER,
  zoomDirection,
  zoomPairError,
} from "../editors/movePolicy";
import { parseTimeMap, timeForFrame } from "../editors/timeMap";
import { alignedFrame, alignedRate, alignedTime, type CompareClip, defaultPair, VideoCompare } from "../editors/VideoCompare";
import { VideoPlayer } from "../editors/VideoPlayer";

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
 * The graph PUT echoes, so the autosaving store never sees a failure the test did not ask for. */
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

/** jsdom's media element has no playback: a currentTime per element, and play / pause we can count. */
function patchMedia() {
  const times = new WeakMap<HTMLMediaElement, number>();
  Object.defineProperty(HTMLMediaElement.prototype, "currentTime", {
    configurable: true,
    get(this: HTMLMediaElement) {
      return times.get(this) ?? 0;
    },
    set(this: HTMLMediaElement, v: number) {
      times.set(this, v);
    },
  });
  const pause = vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
  const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(() => Promise.resolve());
  return { pause, play };
}

// ------------------------------------------------------------------------------------------------ fixtures

const TM_TAKE = { version: "timemap-v0", packet_fps: "25/1", engine_fps: "24/1", packet_frames: [0, 124], engine_frames: 120, duration_s: 5, method: "nearest" };
const TM_CLAY = { version: "timemap-v0", packet_fps: "25/1", engine_fps: "25/1", packet_frames: [0, 124], engine_frames: 125, duration_s: 5, method: "identity" };
const REPORT = {
  checks: [
    { metric_id: "video.first_frame", value: 5.42, status: "pass" as const },
    { metric_id: "video.last_frame", value: 7.1, status: "pass" as const },
    { metric_id: "video.camera_lock_median_flow_px", value: 0.31, status: "advisory" as const },
  ],
  metrics: { timemap: TM_TAKE },
};

/** A `run_view` (T57) with the fields the editors read. */
const runView = (id: string, state: string, extra: Record<string, unknown> = {}) => ({
  id,
  run_id: id,
  project_id: "p1",
  node_id: "motion-1",
  state,
  attempt: 1,
  route: { engine_id: "kling-o3-4k", provider: "atlas" },
  outputs: [`h_${id}`],
  created_utc: "2026-10-18T10:00:00Z",
  override: null,
  accepted: null,
  exportable: state === "DONE",
  ...extra,
});

/** A Kling take: a clip by its recorded time map. */
const clipRun = (id: string, state = "DONE", extra: Record<string, unknown> = {}) =>
  runView(id, state, { normalized: { outputs: [`h_${id}`, `h_${id}_start`], clip: `h_${id}`, timemap: TM_TAKE }, ...extra });

const edge = (source: string, target: string, sourceHandle: string | null = null, targetHandle: string | null = null) => ({
  id: `${source}->${target}:${targetHandle ?? "auto"}`,
  source,
  target,
  sourceHandle,
  targetHandle,
});

const PLATE: LoadableNode = { id: "plate-1", kind: "Plate", label: "Plate", packetId: "pk_room_a", tier: "A", outputHash: "h_plate", outputSize: { width: 640, height: 360 } };

/** Plate -> `upstream` (edit-1 or motion-1) -> Export (n1), project p1 open. */
function openExport(upstream: "Edit" | "Motion", exportParams: Record<string, unknown> = {}): void {
  const up = upstream === "Edit" ? "edit-1" : "motion-1";
  useGraphStore.getState().loadProject(
    {
      schema: 1,
      nodes: [PLATE, { id: up, kind: upstream, label: upstream }, { id: "n1", kind: "Export", label: "Export", params: exportParams }],
      edges: [edge("plate-1", up, "image", "image"), edge(up, "n1", upstream === "Edit" ? "image" : "video", "media")],
    },
    "p1",
  );
}

const paramsOf = (id = "n1"): Record<string, unknown> => useGraphStore.getState().nodes.find((n) => n.id === id)?.data.params ?? {};

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  useMakeStore.getState().reset();
  useRunStore.getState().reset();
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete (HTMLMediaElement.prototype as unknown as Record<string, unknown>).currentTime;
});

// ================================================================================================ pure

describe("deliverApi: what kind of take a run is", () => {
  it("a clip is an .mp4 output, normalized.media video, or a recorded time map; anything else is a still", () => {
    expect(isClipRun({ outputs: ["take.mp4"] })).toBe(true);
    expect(isClipRun({ outputs: ["TAKE.MOV"] })).toBe(true);
    expect(isClipRun({ outputs: ["ab12"], normalized: { media: "video" } })).toBe(true);
    expect(isClipRun({ outputs: ["ab12"], normalized: { timemap: TM_TAKE } })).toBe(true);
    expect(isClipRun({ outputs: ["ab12"], normalized: { outputs: ["ab12"] } })).toBe(false);
    expect(isClipRun(null)).toBe(false);
  });

  it("reads a take: media, time map, the accept note, a previs run's clay reference", () => {
    const t = parseTake(clipRun("r1", "DONE", { accepted: { scrub_log_id: "s1", by: "human" } }));
    expect(t).toMatchObject({ id: "r1", media: "video", accepted: true, clayKey: null, previs: false, outputs: ["h_r1"] });
    expect(t?.timeMap?.engineFrames).toBe(120);
    const previs = parseTake(runView("r2", "DONE", { inputs: { previs: true, reference_videos: [{ key: "h_clay" }] }, normalized: { previs: true, timemap: TM_CLAY } }));
    expect(previs).toMatchObject({ clayKey: "h_clay", previs: true, media: "video", accepted: false });
    expect(parseTake(runView("r3", "DONE"))?.media).toBe("still");
  });

  it("reads the batch and cutdown answers", () => {
    expect(parseExportMany({ exported: [{ run_id: "r1", path: "/o/a.tif", sidecar: "/o/a.gate.json" }], refused: [{ run_id: "r2", error: "not_exportable", detail: "GATE_FAIL", code: "GATE_FAIL" }] })).toEqual({
      exported: [{ runId: "r1", path: "/o/a.tif", sidecar: "/o/a.gate.json" }],
      refused: [{ runId: "r2", error: "not_exportable", detail: "GATE_FAIL", code: "GATE_FAIL" }],
    });
    expect(parseExportMany(null)).toEqual({ exported: [], refused: [] });
    expect(parseCutdown({ path: "/o/c.png", sidecar: "/o/c.cutdown.json", box: [1312, 0, 1214, 2160] }).box).toEqual([1312, 0, 1214, 2160]);
    expect(parseCutdown({ box: [1, 2] }).box).toBeNull();
  });
});

describe("movePolicy: pull-back, single image, motion recipes", () => {
  it("a push-in with z0 > z1 is presented as Pull-back on the push-in's risk row; equal zooms are locked", () => {
    const back = presentMove("push_in", 1.5, 1.0);
    expect(back).toMatchObject({ key: "pull_back", kind: "push_in", label: "Pull-back", direction: "out" });
    expect(back.risk).toBe(RISK_LADDER.push_in.risk);
    expect(back.repair).toEqual(RISK_LADDER.push_in.repair);
    expect(presentMove("push_in", 1.0, 1.3)).toMatchObject({ key: "push_in", label: "Push-in", direction: "in" });
    expect(presentMove("push_in")).toMatchObject({ key: "push_in", direction: null });
    expect(presentMove("slider_drift", 1.5, 1.0).key).toBe("slider_drift"); // only a push-in turns around
    expect(zoomDirection(1.2, 1.2)).toBe("locked");
    expect(evaluateMove("push_in").risk).toBe("low"); // the ladder itself is unchanged
  });

  it("zooms: each 1.0 to 3.0, z0 > z1 allowed, a push-in needs two different zooms", () => {
    expect(zoomPairError(1.5, 1.0, "push_in")).toBeNull();
    expect(zoomPairError(1.0, 1.3, "push_in")).toBeNull();
    expect(zoomPairError(1.2, 1.2, "push_in")).toMatch(/two different zooms/);
    expect(zoomPairError(1.2, 1.2, "locked_tripod")).toBeNull();
    expect(zoomPairError(3.5, 1.0)).toMatch(/1.0 to 3.0/);
    expect(zoomPairError(0.8, 1.0)).toMatch(/1.0 to 3.0/);
  });

  it("the locked tripod exposes single_image; no other move does", () => {
    expect(hasToggle("locked_tripod", "single_image")).toBe(true);
    expect(hasToggle("push_in", "single_image")).toBe(false);
    expect(hasToggle(null, "single_image")).toBe(false);
  });

  it("a motion recipe reads title · move · risk from its motion block; without one, title · library", () => {
    const back = parseMotionRecipe({ id: "motion_pullback_reveal", title: "Pull-back reveal", kind: "motion_croppair", library: "as02", motion: { move: "push_in" }, croppair: { z0: 1.25, z1: 1.0 } });
    expect(back && motionRecipeLabel(back)).toBe("Pull-back reveal · Pull-back · low");
    const tod = parseMotionRecipe({ id: "motion_time_of_day", title: "Time of day (two stills)", kind: "motion_croppair", motion: { move: "locked_tripod", two_stills: true } });
    expect(tod).toMatchObject({ move: "locked_tripod", twoStills: true });
    expect(tod && motionRecipeLabel(tod)).toBe("Time of day (two stills) · Locked tripod · none");
    const bare = parseMotionRecipe({ id: "motion_light_sweep", title: "Light sweep", kind: "motion_croppair", library: "as08" });
    expect(bare && motionRecipeLabel(bare)).toBe("Light sweep · as08");
    expect(parseMotionRecipe({ id: "night", kind: "still_edit" })).toBeNull();
  });
});

describe("VideoCompare: the clock and the default pair", () => {
  const take = { timeMap: parseTimeMap(TM_TAKE), fps: 24 };
  const clay = { timeMap: parseTimeMap(TM_CLAY), fps: 25 };

  it("aligns frames through both time maps", () => {
    expect(alignedFrame(124, clay, take)).toBe(119); // packet 124 -> 124 * 24 / 25 = 119.04
    expect(alignedFrame(119, take, clay)).toBe(124); // engine 119 -> packet 123.96
    expect(alignedFrame(0, take, clay)).toBe(0);
    expect(alignedFrame(500, clay, take)).toBe(119); // clamped to B's last frame
    expect(alignedTime(timeForFrame(124, 25), clay, take)).toBeCloseTo(timeForFrame(119, 24), 9);
    // a clip with no time map is on the other's packet clock, one frame per packet frame
    expect(alignedFrame(119, take, { timeMap: null, fps: 25 })).toBe(124);
    expect(alignedRate(clay, take)).toBe(1);
    expect(alignedRate({ timeMap: null, fps: 3 }, take)).toBeCloseTo(3 / 25, 9);
  });

  it("pairs the clay reference with the take on show, else the two latest takes", () => {
    const t = (key: string): CompareClip => ({ key, kind: "take", label: key, hash: `h_${key}` });
    const c: CompareClip = { key: "clay", kind: "clay", label: "clay reference", hash: "h_clay" };
    expect(defaultPair({ clay: c, takes: [t("r2"), t("r1")], current: "r1" })?.map((x) => x.key)).toEqual(["clay", "r1"]);
    expect(defaultPair({ clay: null, takes: [t("r2"), t("r1")] })?.map((x) => x.key)).toEqual(["r2", "r1"]);
    expect(defaultPair({ clay: null, takes: [t("r2")] })).toBeNull();
    expect(defaultPair({ clay: c, takes: [] })).toBeNull();
    expect(defaultPair(null)).toBeNull();
  });

  it("builds the source from the node's runs: clay from a previs run, clips newest first, the take on show with its report", () => {
    const takes = [
      parseTake(clipRun("r3")),
      parseTake(runView("r2", "DONE", { inputs: { previs: true, reference_videos: [{ key: "h_clay" }] }, normalized: { previs: true, timemap: { ...TM_CLAY, packet_fps: "3/1", engine_fps: "24/1", packet_frames: [0, 11], engine_frames: 96 } } })),
      parseTake(clipRun("r1")),
      parseTake(runView("r0", "RUNNING", { normalized: { timemap: TM_TAKE } })),
    ].flatMap((x) => x ?? []);
    const src = compareSourceFor(takes, [], { runId: "r1", hash: "h_r1", report: REPORT });
    expect(src.clay).toMatchObject({ key: "clay", hash: "h_clay" });
    expect(src.clay?.timeMap).toMatchObject({ packetFps: 3, engineFps: 3, engineFrames: 12 });
    expect(src.takes.map((x) => x.key)).toEqual(["r3", "r2", "r1"]);
    expect(src.takes.find((x) => x.key === "r1")?.report).toBe(REPORT);
    expect(defaultPair(src)?.map((x) => x.key)).toEqual(["clay", "r1"]);
    expect(clayTimeMap(null)).toBeNull();
  });
});

// ================================================================================================ Export editor

describe("ExportEditor: clip delivery", () => {
  it("a clip take shows the four delivery formats, and hevc posts /runs/r1/deliver", async () => {
    const calls = mockApi({
      "GET /projects/p1/runs": (c: Call) => (c.search === "?node_id=motion-1" ? [clipRun("r1")] : []),
      "POST /runs/r1/deliver": { path: "/Users/artist/out/walk.mp4", sidecar: "/Users/artist/out/walk.gate.json", format: "hevc" },
    });
    openExport("Motion");
    render(<ExportEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("export-candidate")).toHaveValue("r1"));
    expect(screen.getByTestId("export-media")).toHaveTextContent("clip");
    const formats = within(screen.getByTestId("export-format")).getAllByRole("option").map((o) => (o as HTMLOptionElement).value);
    expect(formats).toEqual([...CLIP_FORMATS]);
    expect(formats).toEqual(["prores4444", "hevc", "exr_sequence", "fcpxml"]);
    expect(screen.getByTestId("export-go")).toHaveTextContent("Deliver");

    fireEvent.change(screen.getByTestId("export-format"), { target: { value: "hevc" } });
    fireEvent.change(screen.getByTestId("export-outdir"), { target: { value: "/Users/artist/out" } });
    fireEvent.change(screen.getByTestId("export-name"), { target: { value: "walk" } });
    fireEvent.click(screen.getByTestId("export-go"));

    expect(await screen.findByTestId("export-path")).toHaveTextContent("/Users/artist/out/walk.mp4");
    expect(screen.getByTestId("export-sidecar")).toHaveTextContent("/Users/artist/out/walk.gate.json");
    const post = called(calls, "POST", "/runs/r1/deliver");
    expect(post).toHaveLength(1);
    expect(post[0]?.body).toEqual({ format: "hevc", out_dir: "/Users/artist/out", name: "walk" });
    expect(called(calls, "POST", "/runs/r1/export")).toHaveLength(0);
    expect(paramsOf()).toMatchObject({ clipFormat: "hevc", exported: 1, lastPath: "/Users/artist/out/walk.mp4" });
  });

  it("a GATE_FAIL clip sends the typed override_reason; a still keeps png16 | tiff16 | exr", async () => {
    const calls = mockApi({
      "GET /projects/p1/runs": [clipRun("r1", "GATE_FAIL", { exportable: false })],
      "POST /runs/r1/deliver": { path: "C:\\Plate\\out\\r1.mov", sidecar: "C:\\Plate\\out\\r1.gate.json", format: "prores4444" },
    });
    openExport("Motion", { outDir: "C:\\Plate\\out" });
    render(<ExportEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("export-candidate")).toHaveValue("r1"));
    expect(screen.getByTestId("export-go")).toBeDisabled();
    fireEvent.change(screen.getByTestId("export-reason"), { target: { value: "client signed the reel" } });
    fireEvent.click(screen.getByTestId("export-go"));
    expect(await screen.findByTestId("export-path")).toHaveTextContent("C:\\Plate\\out\\r1.mov");
    expect(called(calls, "POST", "/runs/r1/deliver")[0]?.body).toEqual({ format: "prores4444", out_dir: "C:\\Plate\\out", override_reason: "client signed the reel" });
  });

  it("a still take keeps the still formats and the single export route", async () => {
    mockApi({ "GET /projects/p1/runs": [runView("r1", "DONE", { node_id: "edit-1" })] });
    openExport("Edit");
    render(<ExportEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("export-candidate")).toHaveValue("r1"));
    expect(screen.getByTestId("export-media")).toHaveTextContent("still");
    expect(within(screen.getByTestId("export-format")).getAllByRole("option").map((o) => (o as HTMLOptionElement).value)).toEqual(["png16", "tiff16", "exr"]);
    expect(screen.getByTestId("export-go")).toHaveTextContent("Export");
  });
});

describe("ExportEditor: batch export", () => {
  it("Select all accepted ticks the accepted takes and posts their run_ids; exported and refused are listed", async () => {
    const calls = mockApi({
      "GET /projects/p1/runs": [
        runView("r1", "DONE", { node_id: "edit-1", accepted: { scrub_log_id: "s1", by: "human" } }),
        runView("r2", "DONE", { node_id: "edit-1" }),
        runView("r3", "GATE_FAIL", { node_id: "edit-1", exportable: true, override: { reason: "accepted look", by: "human" }, accepted: { scrub_log_id: "s2", by: "human" } }),
      ],
      "POST /projects/p1/export": {
        exported: [{ run_id: "r1", path: "/out/edit-1_r1.png", sidecar: "/out/edit-1_r1.gate.json" }],
        refused: [{ run_id: "r3", error: "not_exportable", detail: "run r3 has no output", code: "RUN_NOT_DELIVERABLE" }],
      },
    });
    openExport("Edit", { outDir: "/out" });
    render(<ExportEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("export-batch")).toBeInTheDocument());
    expect(screen.getByTestId("export-batch-go")).toBeDisabled(); // nothing ticked
    fireEvent.click(screen.getByTestId("export-select-accepted"));
    expect(screen.getByTestId("export-batch-r1")).toBeChecked();
    expect(screen.getByTestId("export-batch-r2")).not.toBeChecked();
    expect(screen.getByTestId("export-batch-r3")).toBeChecked();
    expect(screen.getByTestId("export-batch-go")).toHaveTextContent("Export 2 selected");
    fireEvent.click(screen.getByTestId("export-batch-go"));

    await waitFor(() => expect(called(calls, "POST", "/projects/p1/export")).toHaveLength(1));
    expect(called(calls, "POST", "/projects/p1/export")[0]?.body).toEqual({ run_ids: ["r1", "r3"], out_dir: "/out", format: "png16" });
    expect(await screen.findByTestId("export-batch-path")).toHaveTextContent("/out/edit-1_r1.png + /out/edit-1_r1.gate.json");
    expect(screen.getByTestId("export-batch-refused")).toHaveTextContent("run r3 has no output (RUN_NOT_DELIVERABLE)");
    expect(paramsOf().exported).toBe(1);
  });

  it("a hand-ticked take goes alone, and Select all accepted is disabled when nothing upstream is accepted", async () => {
    const calls = mockApi({
      "GET /projects/p1/runs": [runView("r1", "DONE", { node_id: "edit-1" }), runView("r2", "DONE", { node_id: "edit-1" })],
      "POST /projects/p1/export": { exported: [{ run_id: "r2", path: "/out/b.tif", sidecar: "/out/b.gate.json" }], refused: [] },
    });
    openExport("Edit", { outDir: "/out", format: "tiff16" });
    render(<ExportEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("export-batch")).toBeInTheDocument());
    expect(screen.getByTestId("export-select-accepted")).toBeDisabled();
    fireEvent.click(screen.getByTestId("export-batch-r2"));
    fireEvent.click(screen.getByTestId("export-batch-go"));
    await waitFor(() => expect(called(calls, "POST", "/projects/p1/export")).toHaveLength(1));
    expect(called(calls, "POST", "/projects/p1/export")[0]?.body).toEqual({ run_ids: ["r2"], out_dir: "/out", format: "tiff16" });
  });
});

describe("ExportEditor: cutdowns", () => {
  it("the Cutdown disclosure posts preset 9:16 with the centre and format, and shows the path, sidecar and box", async () => {
    const calls = mockApi({
      "GET /projects/p1/runs": [runView("r1", "DONE", { node_id: "edit-1" })],
      "POST /runs/r1/cutdown": { path: "/out/r1_9x16.png", sidecar: "/out/r1_9x16.cutdown.json", box: [1312, 0, 1214, 2160] },
    });
    openExport("Edit", { outDir: "/out" });
    render(<ExportEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("export-candidate")).toHaveValue("r1"));
    expect(screen.queryByTestId("export-cutdown-preset")).toBeNull(); // closed
    fireEvent.click(screen.getByTestId("export-cutdown-toggle"));
    const preset = screen.getByTestId("export-cutdown-preset");
    expect(within(preset).getAllByRole("option").map((o) => (o as HTMLOptionElement).value)).toEqual(["9:16", "4:5", "1:1", "16:9"]);
    fireEvent.change(preset, { target: { value: "4:5" } });
    fireEvent.change(preset, { target: { value: "9:16" } });
    fireEvent.change(screen.getByTestId("export-cutdown-cx"), { target: { value: "0.3" } });
    expect(screen.queryByTestId("export-cutdown-codec")).toBeNull(); // a still takes a format, not a codec
    fireEvent.change(screen.getByTestId("export-cutdown-format"), { target: { value: "jpeg" } });
    fireEvent.click(screen.getByTestId("export-cutdown-go"));

    expect(await screen.findByTestId("export-cutdown-path")).toHaveTextContent("/out/r1_9x16.png");
    expect(screen.getByTestId("export-cutdown-sidecar")).toHaveTextContent("/out/r1_9x16.cutdown.json");
    expect(screen.getByTestId("export-cutdown-box")).toHaveTextContent("1312, 0 1214 x 2160");
    expect(called(calls, "POST", "/runs/r1/cutdown")[0]?.body).toEqual({ preset: "9:16", cx: 0.3, cy: 0.5, out_dir: "/out", format: "jpeg" });
    expect(paramsOf()).toMatchObject({ cutdownPreset: "9:16", cutdownCx: 0.3, cutdownFormat: "jpeg", exported: 1 });
  });

  it("a clip cutdown sends a codec; a GATE_FAIL take without a recorded override cannot be cut", async () => {
    const calls = mockApi({
      "GET /projects/p1/runs": [clipRun("r1"), clipRun("r2", "GATE_FAIL", { exportable: false })],
      "POST /runs/r1/cutdown": { path: "/out/r1_1x1.mp4", sidecar: "/out/r1_1x1.cutdown.json", box: [140, 0, 360, 360] },
    });
    openExport("Motion", { outDir: "/out", cutdownPreset: "1:1" });
    render(<ExportEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("export-candidate")).toHaveValue("r1"));
    fireEvent.click(screen.getByTestId("export-cutdown-toggle"));
    fireEvent.change(screen.getByTestId("export-cutdown-codec"), { target: { value: "hevc" } });
    fireEvent.click(screen.getByTestId("export-cutdown-go"));
    expect(await screen.findByTestId("export-cutdown-path")).toHaveTextContent("/out/r1_1x1.mp4");
    expect(called(calls, "POST", "/runs/r1/cutdown")[0]?.body).toEqual({ preset: "1:1", cx: 0.5, cy: 0.5, out_dir: "/out", codec: "hevc" });

    fireEvent.change(screen.getByTestId("export-candidate"), { target: { value: "r2" } });
    expect(screen.getByTestId("export-cutdown-go")).toBeDisabled();
  });
});

// ================================================================================================ the player

describe("VideoPlayer: no download, Deliver…", () => {
  it("the <video> has controlslist=nodownload, no picture-in-picture and no context menu", () => {
    patchMedia();
    render(<VideoPlayer src="blob:clip" />);
    const video = screen.getByTestId("video-element");
    expect(video).toHaveAttribute("controlslist", "nodownload");
    expect((video as HTMLVideoElement & { disablePictureInPicture?: boolean }).disablePictureInPicture ?? video.hasAttribute("disablepictureinpicture")).toBe(true);
    expect(fireEvent.contextMenu(video)).toBe(false); // the default (the browser's Save video as…) is prevented
    expect(screen.getByTestId("video-deliver")).toBeDisabled(); // no run named
    expect(screen.getByTestId("video-compare-toggle")).toBeDisabled(); // nothing to pair
  });

  it("Deliver… opens the clip formats inline and posts the take's deliver, with the reason for a GATE_FAIL", async () => {
    patchMedia();
    const calls = mockApi({ "POST /runs/r7/deliver": { path: "/out/r7.fcpxml", sidecar: "/out/r7.gate.json", format: "fcpxml" } });
    render(<VideoPlayer src="blob:clip" runId="r7" needsReason />);
    expect(screen.queryByTestId("video-deliver-form")).toBeNull();
    fireEvent.click(screen.getByTestId("video-deliver"));
    const form = screen.getByTestId("video-deliver-form");
    expect(within(form).getAllByRole("option").map((o) => (o as HTMLOptionElement).value)).toEqual(["prores4444", "hevc", "exr_sequence", "fcpxml"]);
    fireEvent.change(screen.getByTestId("video-deliver-form-format"), { target: { value: "fcpxml" } });
    fireEvent.change(screen.getByTestId("video-deliver-form-outdir"), { target: { value: "/out" } });
    expect(screen.getByTestId("video-deliver-form-go")).toBeDisabled(); // the GATE_FAIL reason is missing
    fireEvent.change(screen.getByTestId("video-deliver-form-reason"), { target: { value: "client asked for the timeline" } });
    fireEvent.click(screen.getByTestId("video-deliver-form-go"));
    expect(await screen.findByTestId("video-deliver-form-path")).toHaveTextContent("/out/r7.fcpxml");
    expect(called(calls, "POST", "/runs/r7/deliver")[0]?.body).toEqual({ format: "fcpxml", out_dir: "/out", override_reason: "client asked for the timeline" });
  });

  it("Compare mounts VideoCompare on the default pair", () => {
    patchMedia();
    const compare = {
      clay: { key: "clay", kind: "clay" as const, label: "clay reference", src: "blob:clay", timeMap: parseTimeMap(TM_CLAY) },
      takes: [{ key: "r1", kind: "take" as const, label: "take r1", src: "blob:take", timeMap: parseTimeMap(TM_TAKE), report: REPORT }],
      current: "r1",
    };
    render(<VideoPlayer src="blob:take" runId="r1" compare={compare} />);
    fireEvent.click(screen.getByTestId("video-compare-toggle"));
    expect(screen.getByTestId("video-compare")).toBeInTheDocument();
    expect(screen.getByTestId("compare-label-a")).toHaveTextContent("A clay reference");
    expect(screen.getByTestId("compare-label-b")).toHaveTextContent("B take r1");
    for (const side of ["a", "b"]) expect(screen.getByTestId(`compare-video-${side}`)).toHaveAttribute("controlslist", "nodownload");
  });
});

describe("VideoCompare: two clips on one clock", () => {
  const A: CompareClip = { key: "clay", kind: "clay", label: "clay reference", src: "blob:clay", timeMap: parseTimeMap(TM_CLAY) };
  const B: CompareClip = { key: "r1", kind: "take", label: "take r1", src: "blob:take", timeMap: parseTimeMap(TM_TAKE), report: REPORT };

  it("the second video follows the first's currentTime: on timeupdate, every animation frame while playing, keys and scrubber", () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "requestAnimationFrame", "cancelAnimationFrame", "Date"] });
    const media = patchMedia();
    render(<VideoCompare a={A} b={B} />);
    const va = screen.getByTestId("compare-video-a") as HTMLVideoElement;
    const vb = screen.getByTestId("compare-video-b") as HTMLVideoElement;

    // the clay shows packet frame 124: the take must show its engine frame 119 (124 * 24 / 25 = 119.04)
    va.currentTime = timeForFrame(124, 25);
    fireEvent.timeUpdate(va);
    expect(vb.currentTime).toBeCloseTo(timeForFrame(119, 24), 9);
    expect(screen.getByTestId("compare-readout")).toHaveTextContent("A frame 124 / 125 -> packet frame 124.00 · B frame 119 / 120");

    // playing: B plays at the aligned rate and is re-seated on every animation frame, with no timeupdate at all
    fireEvent.play(va);
    expect(media.play).toHaveBeenCalled();
    expect(vb.playbackRate).toBe(1);
    va.currentTime = timeForFrame(50, 25);
    act(() => void vi.advanceTimersByTime(40));
    expect(vb.currentTime).toBeCloseTo(timeForFrame(48, 24), 9);
    // within half a frame B is left alone (no seek storm)
    vb.currentTime = timeForFrame(48, 24) + 0.01;
    act(() => void vi.advanceTimersByTime(40));
    expect(vb.currentTime).toBeCloseTo(timeForFrame(48, 24) + 0.01, 9);
    fireEvent.pause(va);

    // `.` steps A one frame and B follows; `,` back
    const box = screen.getByTestId("video-compare");
    va.currentTime = timeForFrame(10, 25);
    fireEvent.timeUpdate(va);
    fireEvent.keyDown(box, { key: "." });
    expect(va.currentTime).toBeCloseTo(timeForFrame(11, 25), 9);
    expect(vb.currentTime).toBeCloseTo(timeForFrame(11, 24), 9); // 11 * 24 / 25 = 10.56 -> 11
    fireEvent.keyDown(box, { key: ",", shiftKey: true });
    expect(va.currentTime).toBeCloseTo(timeForFrame(1, 25), 9);
    expect(vb.currentTime).toBeCloseTo(timeForFrame(1, 24), 9);
    expect(media.pause).toHaveBeenCalled();

    // the shared scrubber seeks A by frame, B follows
    fireEvent.change(screen.getByTestId("compare-scrubber"), { target: { value: "100" } });
    expect(va.currentTime).toBeCloseTo(timeForFrame(100, 25), 9);
    expect(vb.currentTime).toBeCloseTo(timeForFrame(96, 24), 9);
  });

  it("side by side or stacked, a label and the landing / lock chips per side, both pictures guarded", () => {
    patchMedia();
    render(<VideoCompare a={A} b={B} />);
    const box = screen.getByTestId("video-compare");
    expect(box).toHaveAttribute("data-layout", "side");
    fireEvent.click(screen.getByTestId("compare-layout-stacked"));
    expect(box).toHaveAttribute("data-layout", "stacked");
    expect(screen.getByTestId("compare-chip-b-first")).toHaveTextContent("first frame 5.42/255");
    expect(screen.getByTestId("compare-chip-b-lock")).toHaveAttribute("data-tone", "advisory");
    expect(screen.getByTestId("compare-chip-a-first")).toHaveTextContent("first frame: not scored");
    const va = screen.getByTestId("compare-video-a");
    expect(va).toHaveAttribute("controlslist", "nodownload");
    expect(fireEvent.contextMenu(va)).toBe(false);
  });

  it("CAS clips load on request with the token, and a side's gate report is read from its run", async () => {
    window.sessionStorage.setItem("plate.token", "tok9");
    const urls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        urls.push(String(input));
        if (String(input).endsWith("/runs/r1/gate")) return jsonResponse({ report: REPORT, n: 1 });
        return new Response("clip bytes", { status: 200 });
      }),
    );
    const had = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
    URL.createObjectURL = vi.fn(() => "blob:made");
    URL.revokeObjectURL = vi.fn();
    try {
      patchMedia();
      render(<VideoCompare a={{ ...A, src: null, hash: "h_clay" }} b={{ ...B, src: null, hash: "h_r1", report: null, runId: "r1" }} />);
      await waitFor(() => expect(screen.getByTestId("compare-chip-b-first")).toHaveTextContent("first frame 5.42/255"));
      expect(urls).toEqual(["/api/v0/runs/r1/gate"]); // no clip fetched before Load
      expect(screen.getByTestId("compare-empty-a")).toHaveTextContent("not loaded");
      fireEvent.click(screen.getByTestId("compare-load"));
      await waitFor(() => expect(screen.getByTestId("compare-video-b")).toHaveAttribute("src", "blob:made"));
      expect(urls).toEqual(expect.arrayContaining(["/api/v0/assets/h_clay", "/api/v0/assets/h_r1"]));
    } finally {
      URL.createObjectURL = had.create;
      URL.revokeObjectURL = had.revoke;
    }
  });
});

// ================================================================================================ the Motion editor

const MOTION_RECIPES = [
  { id: "motion_pullback_reveal", title: "Pull-back reveal", kind: "motion_croppair", engine: "kling-o3-4k", library: "as02", mask: {}, params: { n: 1 }, motion: { move: "push_in" }, croppair: { z0: 1.25, z1: 1.0 } },
  { id: "motion_time_of_day", title: "Time of day (two stills)", kind: "motion_croppair", engine: "kling-o3-4k", library: "as08", mask: {}, params: {}, motion: { move: "locked_tripod", two_stills: true } },
  { id: "motion_light_sweep", title: "Light sweep", kind: "motion_croppair", engine: "kling-o3-4k", library: "as08", mask: {}, params: {} },
];

interface MotionGraph {
  end?: { approved: boolean } | null;
  elements?: boolean;
  params?: Record<string, unknown>;
}

/** Plate -> still-1 (approved Edit) -> Motion (n1) image; optionally still-2 -> end and a Reference -> elements. */
function openMotion({ end = null, elements = false, params = {} }: MotionGraph = {}): void {
  const nodes: LoadableNode[] = [
    PLATE,
    { id: "still-1", kind: "Edit", label: "Dusk relight", gateStatus: "pass", approved: true, outputHash: "h_still1", outputSize: { width: 640, height: 360 } },
    { id: "n1", kind: "Motion", label: "Motion", params },
  ];
  const edges = [edge("plate-1", "still-1", "image", "image"), edge("still-1", "n1", "image", "image")];
  if (end) {
    nodes.push({ id: "still-2", kind: "Edit", label: "Night relight", gateStatus: end.approved ? "pass" : "fail", approved: end.approved, outputHash: "h_still2" });
    edges.push(edge("plate-1", "still-2", "image", "image"), edge("still-2", "n1", "image", "end"));
  }
  if (elements) {
    nodes.push({ id: "ref-1", kind: "Reference", label: "Brass sample", params: { referenceHash: "abcdef0123456789", role: "object" }, outputHash: "abcdef0123456789" });
    edges.push(edge("ref-1", "n1", "ref", "elements"));
  }
  useGraphStore.getState().loadProject({ schema: 1, nodes, edges }, "p1");
}

const motionRoutes = (extra: Record<string, Route> = {}): Record<string, Route> => ({
  "POST /projects/p1/nodes/n1/preview": { mode: "advisory", verdict: "ai_interpolate", numbers: { D_px_median: 4, disocclusion_fraction: 0.01, orbit_deg: 0, crossings: [] }, explanation: "" },
  "GET /recipes": MOTION_RECIPES,
  ...extra,
});

const setCustody = (nda: boolean, grades: CustodyGrade[]): void => useMakeStore.getState().setCustody("p1", { nda, allowed_grades: grades });

describe("MotionEditor: stills, elements, recipe and prompt", () => {
  beforeEach(() => {
    useMakeStore.setState({ engines: { engines: [...FALLBACK_ENGINES], source: "fallback" } });
  });

  it("renders the end-still row with the lock state of both stills, and the elements wired to it", () => {
    mockApi(motionRoutes());
    openMotion({ end: { approved: false }, elements: true });
    render(<MotionEditor nodeId="n1" />);
    expect(screen.getByTestId("motion-start")).toHaveTextContent("Dusk relight");
    expect(screen.getByTestId("motion-start-lock")).toHaveTextContent("approved");
    expect(screen.getByTestId("motion-end")).toHaveTextContent("end still");
    expect(screen.getByTestId("motion-end")).toHaveTextContent("Night relight");
    expect(screen.getByTestId("motion-end")).toHaveAttribute("data-node", "still-2");
    expect(screen.getByTestId("motion-end-lock")).toHaveTextContent("locked");
    expect(screen.getByTestId("motion-end-lock")).toHaveAttribute("data-tone", "fail");
    expect(screen.getByTestId("motion-element-ref-1")).toHaveTextContent("Brass sample · object · abcdef01");
  });

  it("without an end still the row says single-still move; an approved end still is unlocked", () => {
    mockApi(motionRoutes());
    openMotion();
    const { unmount } = render(<MotionEditor nodeId="n1" />);
    expect(screen.getByTestId("motion-end")).toHaveTextContent("none: single-still move");
    expect(screen.getByTestId("motion-elements-none")).toBeInTheDocument();
    unmount();
    openMotion({ end: { approved: true } });
    render(<MotionEditor nodeId="n1" />);
    expect(screen.getByTestId("motion-end-lock")).toHaveTextContent("approved");
    const g = useGraphStore.getState();
    expect(motionWires(g.nodes, g.edges, "n1")).toMatchObject({ start: { id: "still-1" }, end: { id: "still-2" }, elements: [] });
  });

  it("offers the motion recipes as title · move · risk, writes params.recipe and params.prompt, and shows the assembled text", async () => {
    const calls = mockApi(
      motionRoutes({
        "GET /recipes/motion_pullback_reveal/assemble": { prompt: "Shot 1: wide shot, steady pull-back at constant speed.", negative_prompt: "morphing", unfilled: 0, unfilled_tokens: [] },
      }),
    );
    openMotion({ params: { move: "push_in" } });
    render(<MotionEditor nodeId="n1" />);
    const select = screen.getByTestId("motion-recipe-select") as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBe(MOTION_RECIPES.length + 1));
    const labels = Array.from(select.options).map((o) => o.textContent);
    expect(labels).toContain("Pull-back reveal · Pull-back · low");
    expect(labels).toContain("Time of day (two stills) · Locked tripod · none");
    expect(labels).toContain("Light sweep · as08");
    const listCall = called(calls, "GET", "/recipes")[0];
    expect(new URLSearchParams(listCall?.search).get("kind")).toBe("motion_croppair");
    expect(new URLSearchParams(listCall?.search).get("project_id")).toBe("p1");

    fireEvent.change(select, { target: { value: "motion_pullback_reveal" } });
    expect(paramsOf().recipe).toBe("motion_pullback_reveal");
    expect(await screen.findByTestId("motion-assembled")).toHaveTextContent("steady pull-back at constant speed");
    expect(screen.getByTestId("motion-assembled")).toHaveAttribute("data-overridden", "false");
    // the recipe's crop pair is a pull-back, shown as such until zooms are typed
    expect(screen.getByTestId("motion-direction")).toHaveTextContent("pull-back 0.80x");
    expect(screen.getByTestId("motion-presented")).toHaveTextContent("Pull-back");

    fireEvent.change(screen.getByTestId("motion-prompt"), { target: { value: "The camera pulls back from the door to the whole facade." } });
    expect(paramsOf().prompt).toBe("The camera pulls back from the door to the whole facade.");
    expect(screen.getByTestId("motion-assembled")).toHaveAttribute("data-overridden", "true");
    fireEvent.change(screen.getByTestId("motion-prompt"), { target: { value: "" } });
    expect(paramsOf().prompt).toBeUndefined();

    // a two-stills recipe asks for the end still
    fireEvent.change(select, { target: { value: "motion_time_of_day" } });
    expect(screen.getByTestId("motion-recipe-needs-end")).toHaveTextContent("wire the end still");
    fireEvent.change(select, { target: { value: "" } });
    expect(paramsOf().recipe).toBeUndefined();
  });

  it("z0 1.5 / z1 1.0 is accepted and labelled pull-back on the push-in's risk; equal zooms on a push-in are refused", () => {
    mockApi(motionRoutes());
    openMotion({ params: { move: "push_in" } });
    render(<MotionEditor nodeId="n1" />);
    fireEvent.change(screen.getByTestId("motion-zoom-start"), { target: { value: "1.5" } });
    fireEvent.change(screen.getByTestId("motion-zoom-end"), { target: { value: "1.0" } });
    expect(paramsOf()).toMatchObject({ zoomStart: 1.5, zoomEnd: 1 });
    expect(screen.getByTestId("motion-direction")).toHaveTextContent("pull-back 0.67x");
    expect(screen.getByTestId("motion-presented")).toHaveTextContent("Pull-back");
    expect(screen.getByTestId("motion-risk")).toHaveTextContent("risk low");
    expect(screen.queryByTestId("motion-zoom-error")).toBeNull();

    fireEvent.change(screen.getByTestId("motion-zoom-end"), { target: { value: "1.5" } });
    expect(screen.getByTestId("motion-zoom-error")).toHaveTextContent("two different zooms");
    fireEvent.click(screen.getByTestId("motion-zoom-clear"));
    expect(paramsOf().zoomStart).toBeUndefined();
    expect(paramsOf().zoomEnd).toBeUndefined();
  });

  it("a locked tripod has no crop pair and exposes single image; with an end still wired it says why that cannot go", () => {
    mockApi(motionRoutes());
    openMotion({ params: { move: "locked_tripod" }, end: { approved: true } });
    render(<MotionEditor nodeId="n1" />);
    expect(screen.queryByTestId("motion-zoom")).toBeNull();
    fireEvent.click(screen.getByTestId("motion-single-image"));
    expect(paramsOf().single_image).toBe(true);
    expect(screen.getByTestId("motion-single-end")).toHaveTextContent("unwire the end still");
    fireEvent.click(screen.getByTestId("motion-single-image"));
    expect(paramsOf().single_image).toBeUndefined();
  });
});

describe("MotionEditor: engines by custody, engine cards, the take's Deliver", () => {
  beforeEach(() => {
    useMakeStore.setState({ engines: { engines: [...FALLBACK_ENGINES], source: "fallback" } });
  });

  const engineIds = (): string[] => Array.from((screen.getByTestId("motion-engine-select") as HTMLSelectElement).options).map((o) => o.value).filter(Boolean);

  it("lists veo-3.1 when the project's custody allows it, never on an NDA project or with custody unknown", () => {
    mockApi(motionRoutes());
    openMotion({ params: { move: "push_in" } });
    const { unmount } = render(<MotionEditor nodeId="n1" />);
    expect(engineIds()).not.toContain("veo-3.1"); // custody not loaded
    unmount();

    setCustody(false, ["C0", "C1", "C2", "D"]);
    const second = render(<MotionEditor nodeId="n1" />);
    expect(engineIds()).toEqual(expect.arrayContaining(["kling-o3-4k", "kling-v3-pro", "seedance-2.5-i2v", "veo-3.1"]));
    fireEvent.change(screen.getByTestId("motion-engine-select"), { target: { value: "veo-3.1" } });
    expect(paramsOf()).toMatchObject({ engine_id: "veo-3.1", duration: 5 });
    expect(screen.getByTestId("motion-duration")).toHaveAttribute("max", "8");
    second.unmount();

    setCustody(true, ["C0", "C1"]);
    render(<MotionEditor nodeId="n1" />);
    expect(engineIds()).not.toContain("veo-3.1");
  });

  it("shows each video engine's Wilson card for crop_pair_motion beside it", async () => {
    const calls = mockApi(
      motionRoutes({
        "GET /engines/cards": [
          { engine_id: "kling-o3-4k", task: "crop_pair_motion", shot_class: null, n: 14, passes: 11, pass_rate: 0.786, wilson_lower_95: 0.524, mean_cost: 0.9, steering: true },
        ],
      }),
    );
    openMotion({ params: { move: "push_in" } });
    render(<MotionEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("motion-card-kling-o3-4k")).toHaveTextContent("≥ 52% (14)"));
    expect(screen.getByTestId("motion-card-kling-v3-pro")).toHaveTextContent("no gated runs yet");
    const option = Array.from((screen.getByTestId("motion-engine-select") as HTMLSelectElement).options).find((o) => o.value === "kling-o3-4k");
    expect(option?.textContent).toContain("≥ 52% (14)");
    const cardCall = called(calls, "GET", "/engines/cards")[0];
    expect(new URLSearchParams(cardCall?.search).get("task")).toBe("crop_pair_motion");
  });

  it("the player's Deliver… posts the take on show; Compare pairs the node's two latest clips", async () => {
    patchMedia();
    const calls = mockApi(
      motionRoutes({
        "GET /runs/run_7/gate": { report: REPORT, n: 1 },
        "GET /projects/p1/runs": [clipRun("run_8"), clipRun("run_7")],
        "POST /runs/run_7/deliver": { path: "/out/run_7.mov", sidecar: "/out/run_7.gate.json", format: "prores4444" },
      }),
    );
    openMotion({ params: { move: "push_in" } });
    useRunStore.getState().startRuns("n1", [
      { id: "run_7", state: "DONE", outputs: ["h_run_7"], gate_report: { verdict: "pass" } },
      { id: "run_8", state: "DONE", outputs: ["h_run_8"], gate_report: { verdict: "pass" } },
    ]);
    useRunStore.getState().pick("n1", "run_7");
    render(<MotionEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("video-chip-first")).toHaveTextContent("first frame 5.42/255"));

    fireEvent.click(screen.getByTestId("video-deliver"));
    fireEvent.change(screen.getByTestId("video-deliver-form-outdir"), { target: { value: "/out" } });
    expect(screen.queryByTestId("video-deliver-form-reason")).toBeNull(); // a DONE take needs no reason
    fireEvent.click(screen.getByTestId("video-deliver-form-go"));
    expect(await screen.findByTestId("video-deliver-form-path")).toHaveTextContent("/out/run_7.mov");
    expect(called(calls, "POST", "/runs/run_7/deliver")[0]?.body).toEqual({ format: "prores4444", out_dir: "/out" });

    await waitFor(() => expect(screen.getByTestId("video-compare-toggle")).toBeEnabled());
    fireEvent.click(screen.getByTestId("video-compare-toggle"));
    expect(screen.getByTestId("compare-label-a")).toHaveTextContent("take run_8");
    expect(screen.getByTestId("compare-label-b")).toHaveTextContent("take run_7");
  });
});
