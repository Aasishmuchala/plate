// T47 node editors: Mask (Cryptomatte pick, paint, SAM click, OCR text-lock), CropPair, Motion, People, Set, LightMix,
// Recolour, Light, Scout, and the video player with its time-map readout. The daemon is a fetch stub that answers by
// "METHOD /path" and records every call, so each test also pins the request the editor sent. jsdom has no canvas
// and no media pipeline: getContext is a recording fake and currentTime / pause are patched on the media element.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { FALLBACK_ENGINES } from "../makebar/engines";
import { EMPTY_ENTRY, useMakeStore } from "../makebar/makeStore";
import { NODE_KINDS, type NodeKind } from "../nodeTypes";
import { createInitialGraph, useGraphStore } from "../store";
import { EDITOR_KINDS, editorFor, PARAM_PANEL_KINDS, PATH_EDITOR_KINDS, pathEditorFor } from "../editors";
import { parseIdPick, parseLightMixSeed, parseRecolourExpect, parseSetSummary } from "../editors/api";
import { hexToLab, labToHex, MOOD_PRESETS, normalizeHex, parseTarget, presetGains, RAL_SRGB } from "../editors/colour";
import { boxFor, boxPercent, cutBoxes, moveZoom, zoomError } from "../editors/cropBoxes";
import { CropPairEditor } from "../editors/CropPairEditor";
import { LightEditor } from "../editors/LightEditor";
import { LightMixEditor } from "../editors/LightMixEditor";
import { clearPaintLayers, MaskEditor } from "../editors/MaskEditor";
import {
  adler32,
  clearMask,
  clientToImage,
  coverage,
  crc32,
  encodeMaskPng,
  newMask,
  NEVER_EDITABLE,
  stampDisc,
  strokeLine,
  toRaster,
  workSize,
  zlibDeflate,
  zlibStored,
} from "../editors/maskMath";
import { MotionEditor } from "../editors/MotionEditor";
import {
  evaluateMove,
  isMoveKind,
  makeBlock,
  MIN_OVERRIDE_CHARS,
  MOVE_KINDS,
  parseRouterCard,
  RISK_LADDER,
  type RouterCard,
} from "../editors/movePolicy";
import { mergeParams, stillRef, upstreamPlates } from "../editors/params";
import { PeopleEditor } from "../editors/PeopleEditor";
import { RecolourEditor } from "../editors/RecolourEditor";
import { ScoutEditor } from "../editors/ScoutEditor";
import { SetEditor } from "../editors/SetEditor";
import { covers, engineFrameFor, frameAt, nearestPacketFrame, packetFrameFor, parseFps, parseTimeMap, timeForFrame } from "../editors/timeMap";
import { chipsFromReport, timeMapFromReport, VideoPlayer } from "../editors/VideoPlayer";

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
function mockApi(routes: Record<string, Route> = {}): Call[] {
  const calls: Call[] = [];
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

/** The amphi-plaza relit frame the CropPair numbers come from. */
const PLATE = { width: 5504, height: 3072 };

type Extra = Record<string, unknown>;

/** Plate -> `kind` node, project p1 open, the node selected. `extra` is merged into the node's data. */
function openGraph(kind: NodeKind, extra: Extra = {}, plate: Extra = {}, id = "n1"): void {
  useGraphStore.getState().loadProject(
    {
      schema: 1,
      nodes: [
        { id: "plate-1", kind: "Plate", label: "Plate", packetId: "room_a", tier: "A", outputHash: "h_plate", outputSize: PLATE, ...plate },
        { id, kind, label: kind, ...extra },
      ],
      edges: [{ id: `plate-1->${id}`, source: "plate-1", target: id, sourceHandle: null, targetHandle: null }],
    } as never,
    "p1",
  );
}

const nodeOf = (id = "n1") => {
  const n = useGraphStore.getState().nodes.find((x) => x.id === id);
  if (!n) throw new Error(`no node ${id}`);
  return n;
};
const paramsOf = (id = "n1"): Record<string, unknown> => nodeOf(id).data.params ?? {};

/** A fake 2D context that records what the editor draws. */
const drawn = { putImageData: vi.fn(), createImageData: vi.fn() };

function mount(ui: ReactElement) {
  return render(ui);
}

/** Pointer down at the centre of a stage (jsdom reports every element as 1280 x 720 at the origin). */
const pointerAt = (stage: HTMLElement, fx: number, fy: number, type: "pointerDown" | "pointerMove" | "pointerUp" = "pointerDown"): void => {
  fireEvent[type](stage, { clientX: fx * 1280, clientY: fy * 720, pointerId: 1 });
};

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  useMakeStore.getState().reset();
  clearPaintLayers();
  window.sessionStorage.clear();
  drawn.putImageData.mockReset();
  drawn.createImageData.mockReset().mockImplementation((w: number, h: number) => ({ data: new Uint8ClampedArray(w * h * 4), width: w, height: h }));
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation((() => drawn) as never);
});

afterEach(() => {
  // unmount first: a late effect must not reach for a canvas after the fake is gone
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ================================================================================================ pure: crop boxes

describe("cropBoxes: the cut_crops geometry in TypeScript", () => {
  it("gives the recorded amphi N2b boxes (5504x3072 at 3633,2174, 1.5x to 2.3x)", () => {
    expect(boxFor(5504, 3072, 3633, 2174, 1.5)).toEqual({ x0: 1799, y0: 1024, w: 3668, h: 2048 });
    expect(boxFor(5504, 3072, 3633, 2174, 2.3)).toEqual({ x0: 2437, y0: 1507, w: 2392, h: 1334 });
    const plan = cutBoxes(5504, 3072, 3633, 2174, 1.5, 2.3);
    expect(plan.start).toEqual({ x0: 1799, y0: 1024, w: 3668, h: 2048 });
    expect(plan.end).toEqual({ x0: 2437, y0: 1507, w: 2392, h: 1334 });
    expect(plan.pushRatio).toBeCloseTo(2.3 / 1.5, 12);
  });

  // [W, H, cx, cy, zoom, x0, y0, w, h] produced by plate_gates.croppair.box_for on this checkout
  const PYTHON: [number, number, number, number, number, number, number, number, number][] = [
    [5504, 3072, 3633, 2174, 1.5, 1799, 1024, 3668, 2048],
    [5504, 3072, 3633, 2174, 2.3, 2437, 1507, 2392, 1334],
    [5504, 3072, 0, 0, 1.0, 0, 0, 5504, 3072],
    [5504, 3072, 0, 0, 3.0, 0, 0, 1834, 1024],
    [5504, 3072, 5503, 3071, 2.0, 2752, 1536, 2752, 1536],
    [5504, 3072, 2752, 1536, 1.18, 420, 235, 4664, 2602],
    [3840, 2160, 1920, 1080, 1.5, 640, 360, 2560, 1440],
    [3840, 2160, 100, 2000, 2.7, 0, 1360, 1422, 800],
    [1001, 501, 500, 250, 1.5, 167, 83, 666, 334],
    [1001, 501, 999, 2, 2.05, 513, 0, 488, 244],
    [640, 360, 320, 180, 1.25, 64, 36, 512, 288],
    [641, 361, 640.5, 0.5, 1.9, 305, 0, 336, 190],
  ];
  it.each(PYTHON)("matches the Python box_for on %s x %s at (%s, %s) zoom %s", (W, H, cx, cy, z, x0, y0, w, h) => {
    expect(boxFor(W, H, cx, cy, z)).toEqual({ x0, y0, w, h });
  });

  it("keeps every box even-sized and inside the frame", () => {
    for (const [W, H] of [[1001, 501], [3840, 2160], [5504, 3072]] as const) {
      for (const z of [1, 1.07, 1.5, 2.3, 3]) {
        for (const [cx, cy] of [[0, 0], [W, H], [W / 2, H / 2], [W - 3, 5]] as const) {
          const b = boxFor(W, H, cx, cy, z);
          expect(b.w % 2).toBe(0);
          expect(b.h % 2).toBe(0);
          expect(b.x0).toBeGreaterThanOrEqual(0);
          expect(b.y0).toBeGreaterThanOrEqual(0);
          expect(b.x0 + b.w).toBeLessThanOrEqual(W);
          expect(b.y0 + b.h).toBeLessThanOrEqual(H);
        }
      }
    }
  });

  it("refuses a zoom pair outside 1.0 <= start < end <= 3.0, like the daemon", () => {
    expect(zoomError(1.0, 3.0)).toBeNull();
    expect(zoomError(1.5, 2.3)).toBeNull();
    expect(zoomError(1.0, 1.18)).toBeNull();
    for (const [a, b] of [[1.0, 1.0], [2.0, 1.5], [0.9, 2], [2, 3.01], [1.5, 1.5]] as const) {
      expect(zoomError(a, b)).toMatch(/zoom pair must satisfy/);
      expect(() => cutBoxes(100, 100, 50, 50, a, b)).toThrow(RangeError);
    }
  });

  it("moveZoom keeps the pair valid whichever slider moves", () => {
    expect(moveZoom(1.0, 1.3, "start", 2.0)).toEqual([2.0, 2.05]);
    expect(moveZoom(1.0, 1.3, "start", 3.0)).toEqual([2.95, 3.0]);
    expect(moveZoom(2.0, 2.5, "end", 1.0)).toEqual([1.0, 1.05]);
    expect(moveZoom(2.0, 2.5, "end", 2.02)).toEqual([1.97, 2.02]);
    expect(moveZoom(1.2, 2.0, "start", 0.2)).toEqual([1.0, 2.0]);
    expect(moveZoom(1.2, 2.0, "end", Number.NaN)).toEqual([1.2, 2.0]);
    for (const which of ["start", "end"] as const) {
      for (let v = 0; v <= 4; v += 0.13) {
        const [a, b] = moveZoom(1.4, 1.9, which, v);
        expect(zoomError(a, b)).toBeNull();
      }
    }
  });

  it("boxPercent expresses a box as percentages of the image", () => {
    const p = boxPercent({ x0: 1799, y0: 1024, w: 3668, h: 2048 }, 5504, 3072);
    expect(parseFloat(p.left)).toBeCloseTo((100 * 1799) / 5504, 3);
    expect(parseFloat(p.top)).toBeCloseTo(33.3333, 3);
    expect(parseFloat(p.width)).toBeCloseTo((100 * 3668) / 5504, 3);
    expect(parseFloat(p.height)).toBeCloseTo(66.6667, 3);
  });
});

// ================================================================================================ pure: time map

describe("timeMap: packet frame <-> engine frame", () => {
  const TM = parseTimeMap({ version: "timemap-v0", packet_fps: "25/1", engine_fps: "24/1", packet_frames: [0, 124], engine_frames: 120, duration_s: 5, duration: "5/1", method: "nearest" });

  it("reads frame rates as text or numbers", () => {
    expect(parseFps("25/1")).toBe(25);
    expect(parseFps("24000/1001")).toBeCloseTo(23.976, 3);
    expect(parseFps("23.976")).toBeCloseTo(23.976, 6);
    expect(parseFps(30)).toBe(30);
    for (const bad of ["", "abc", "0/1", "25/0", -1, 0, null, undefined]) expect(parseFps(bad)).toBeNull();
  });

  it("25 to 24 fps over 125 frames is 120 engine frames and packet_frame_for(119) = 123.9583", () => {
    expect(TM).not.toBeNull();
    if (!TM) return;
    expect(TM.engineFrames).toBe(120);
    expect(packetFrameFor(TM, 0)).toBe(0);
    expect(packetFrameFor(TM, 119)).toBeCloseTo(123.9583, 4);
    expect(nearestPacketFrame(TM, 119)).toBe(124);
    expect(covers(TM, 119)).toBe(true);
    // a Kling clip runs past the packet: those frames clamp to the last packet frame and say so
    expect(covers(TM, 125)).toBe(false);
    expect(nearestPacketFrame(TM, 125)).toBe(124);
    expect(engineFrameFor(TM, 124)).toBeCloseTo(119.04, 6);
  });

  it("rejects a time map that is not one", () => {
    expect(parseTimeMap(null)).toBeNull();
    expect(parseTimeMap({ packet_fps: "25/1" })).toBeNull();
    expect(parseTimeMap({ packet_fps: "25/1", engine_fps: "24/1", packet_frames: [10, 5], engine_frames: 10 })).toBeNull();
    expect(parseTimeMap({ packet_fps: "25/1", engine_fps: "24/1", packet_frames: [0, 4], engine_frames: 0 })).toBeNull();
  });

  it("frameAt and timeForFrame round-trip on every frame boundary", () => {
    for (let f = 0; f < 240; f += 1) {
      expect(frameAt(timeForFrame(f, 24), 24)).toBe(f);
      expect(frameAt(f / 24, 24)).toBe(f);
    }
  });
});

// ================================================================================================ pure: move policy

describe("movePolicy: the Lensbook risk ladder, dials and the router block", () => {
  it("risk per move kind", () => {
    const risk = Object.fromEntries(MOVE_KINDS.map((k) => [k, evaluateMove(k).risk]));
    expect(risk).toEqual({
      locked_tripod: "none",
      push_in: "low",
      slider_drift: "low",
      crane_rise: "med",
      light_sweep: "med",
      orbit_le_30: "high",
      aerial_drift: "high",
      threshold_cross: "highest",
    });
    expect(RISK_LADDER.aerial_drift.repair).toEqual(["A1", "A3"]);
    expect(RISK_LADDER.threshold_cross.repair).toEqual(["A1", "F7"]);
    expect(RISK_LADDER.locked_tripod.repair).toEqual([]);
    expect(evaluateMove("locked_tripod").riskLabel).toBeUndefined();
    expect(isMoveKind("push_in")).toBe(true);
    expect(isMoveKind("dolly")).toBe(false);
  });

  it("dials raise the risk: reveal > 3 m is med, > 10 m high; slider > 10 cm med; orbit > 30 deg highest", () => {
    expect(evaluateMove("push_in", { revealM: 2 }).risk).toBe("low");
    expect(evaluateMove("push_in", { revealM: 3 }).risk).toBe("low");
    expect(evaluateMove("push_in", { revealM: 4 }).risk).toBe("med");
    expect(evaluateMove("push_in", { revealM: 11 }).risk).toBe("high");
    expect(evaluateMove("push_in", { revealM: 11 }).raisedBy).toMatch(/invents geometry/);
    expect(evaluateMove("slider_drift", { sliderCm: 10 }).risk).toBe("low");
    expect(evaluateMove("slider_drift", { sliderCm: 12 }).risk).toBe("med");
    expect(evaluateMove("orbit_le_30", { orbitDeg: 30 }).risk).toBe("high");
    expect(evaluateMove("orbit_le_30", { orbitDeg: 35 }).risk).toBe("highest");
    // a dial never lowers a risk and does not apply to another move
    expect(evaluateMove("threshold_cross", { revealM: 0 }).risk).toBe("highest");
    expect(evaluateMove("crane_rise", { revealM: 20, orbitDeg: 90 }).risk).toBe("med");
  });

  it("high and highest need an override reason of 10+ characters until one is logged", () => {
    const high = evaluateMove("orbit_le_30");
    const highest = evaluateMove("threshold_cross");
    expect(makeBlock(evaluateMove("push_in"), "", 0)).toBeNull();
    expect(makeBlock(evaluateMove("crane_rise"), "", 0)).toBeNull();
    for (const ev of [high, highest]) {
      expect(ev.needsOverride).toBe(true);
      expect(makeBlock(ev, "", 0)).toMatch(/override reason of at least 10 characters/);
      expect(makeBlock(ev, "x".repeat(MIN_OVERRIDE_CHARS - 1), 0)).not.toBeNull();
      expect(makeBlock(ev, "  too short ", 0)).not.toBeNull();
      expect(makeBlock(ev, "x".repeat(MIN_OVERRIDE_CHARS), 0)).toBeNull();
      expect(makeBlock(ev, "", 1)).toBeNull();
    }
  });

  it("a blocking render_move verdict blocks regardless of any override; advisory never blocks", () => {
    const card = (mode: "advisory" | "blocking", verdict: RouterCard["verdict"]): RouterCard => ({
      mode,
      verdict,
      numbers: { D_px_median: 8, disocclusion_fraction: 0.2, orbit_deg: 12, detail_verdict: null, crossings: 0 },
      explanation: "",
    });
    const blocked = evaluateMove("push_in", {}, card("blocking", "render_move"));
    expect(blocked.routerBlocks).toBe(true);
    expect(makeBlock(blocked, "a perfectly good long reason", 3)).toMatch(/V-Ray/);
    expect(evaluateMove("push_in", {}, card("advisory", "render_move")).routerBlocks).toBe(false);
    expect(evaluateMove("push_in", {}, card("blocking", "ai_interpolate")).routerBlocks).toBe(false);
    expect(evaluateMove("push_in", {}, card("blocking", "rendered_endpoints_plus_clay")).routerBlocks).toBe(false);
  });

  it("reads the daemon's RouterVerdict JSON", () => {
    const card = parseRouterCard({
      mode: "blocking",
      verdict: "render_move",
      numbers: { disocclusion_fraction: 0.12, orbit_deg: 31.5, D_px_median: 22.4, detail_verdict: "ai_by_region", crossings: [{ kind: "threshold" }] },
      explanation: "12.0 % of end-frame pixels unseen",
      blocks: true,
    });
    expect(card).toEqual({
      mode: "blocking",
      verdict: "render_move",
      numbers: { D_px_median: 22.4, disocclusion_fraction: 0.12, orbit_deg: 31.5, detail_verdict: "ai_by_region", crossings: 1 },
      explanation: "12.0 % of end-frame pixels unseen",
    });
    expect(parseRouterCard({ router: { mode: "advisory", verdict: "ai_interpolate", numbers: {}, explanation: "" } })?.mode).toBe("advisory");
    expect(parseRouterCard({ verdict: "nonsense" })).toBeNull();
    expect(parseRouterCard(null)).toBeNull();
  });
});

// ================================================================================================ pure: mask math

describe("maskMath: pointer mapping, the paint raster and the PNG", () => {
  const rect = { left: 0, top: 0, width: 1280, height: 720 };

  it("maps a client pixel to an image pixel and clamps at the edges", () => {
    expect(clientToImage(rect, PLATE, 640, 360)).toEqual({ x: 2752, y: 1536 });
    expect(clientToImage(rect, PLATE, 0, 0)).toEqual({ x: 0, y: 0 });
    expect(clientToImage(rect, PLATE, 1280, 720)).toEqual({ x: 5503, y: 3071 });
    expect(clientToImage(rect, PLATE, -50, 900)).toEqual({ x: 0, y: 3071 });
    expect(clientToImage({ left: 100, top: 50, width: 640, height: 360 }, { width: 640, height: 360 }, 420, 230)).toEqual({ x: 320, y: 180 });
    expect(clientToImage(rect, null, 1, 1)).toBeNull();
    expect(clientToImage({ ...rect, width: 0 }, PLATE, 1, 1)).toBeNull();
  });

  it("works at a size that keeps the aspect, never upscaled", () => {
    expect(workSize(PLATE)).toEqual({ width: 2048, height: 1143 }); // T82: MAX_RASTER_SIDE 2048
    expect(workSize({ width: 640, height: 360 })).toEqual({ width: 640, height: 360 });
    expect(workSize({ width: 100, height: 4000 }, 1000)).toEqual({ width: 25, height: 1000 });
  });

  it("stamps a disc, strokes without gaps, erases and clears", () => {
    const m = newMask(64, 64);
    const n = stampDisc(m, 32, 32, 8, 255);
    expect(n).toBeGreaterThan(Math.PI * 64 * 0.9);
    expect(n).toBeLessThan(Math.PI * 64 * 1.1);
    expect(m.data[32 * 64 + 32]).toBe(255);
    expect(m.data[0]).toBe(0);
    stampDisc(m, 32, 32, 4, 0);
    expect(m.data[32 * 64 + 32]).toBe(0);
    expect(m.data[32 * 64 + 32 + 6]).toBe(255);

    const s = newMask(100, 20);
    strokeLine(s, 5, 10, 95, 10, 3, 255);
    for (let x = 5; x <= 95; x += 1) expect(s.data[10 * 100 + x]).toBe(255);
    expect(coverage(s)).toBeGreaterThan(0.1);
    clearMask(s);
    expect(coverage(s)).toBe(0);
    // off-frame stamps are clipped, not wrapped
    const c = newMask(10, 10);
    stampDisc(c, -3, -3, 2, 255);
    stampDisc(c, 13, 5, 2, 255);
    expect(coverage(c)).toBe(0);
    const r = toRaster(newMask(100, 50), { width: 1000, height: 500 }, { x: 499, y: 249 });
    expect(r.x).toBeCloseTo(49.95, 9);
    expect(r.y).toBeCloseTo(24.95, 9);
  });

  it("crc32 and adler32 match their reference values", () => {
    expect(crc32(new TextEncoder().encode("123456789"))).toBe(0xcbf43926);
    expect(adler32(new TextEncoder().encode("Wikipedia"))).toBe(0x11e60398);
  });

  async function inflate(zlib: Uint8Array<ArrayBuffer>): Promise<Uint8Array> {
    const stream = new ReadableStream<Uint8Array<ArrayBuffer>>({
      start(c) {
        c.enqueue(zlib);
        c.close();
      },
    }).pipeThrough(new DecompressionStream("deflate"));
    return new Uint8Array(await new Response(stream).arrayBuffer());
  }

  it("both zlib paths inflate back to the input, including blocks past 64 KB", async () => {
    const raw = new Uint8Array(150_000);
    for (let i = 0; i < raw.length; i += 1) raw[i] = (i * 31 + (i >> 8)) & 0xff;
    expect(await inflate(zlibStored(raw))).toEqual(raw);
    expect(await inflate(await zlibDeflate(raw))).toEqual(raw);
    expect(await inflate(zlibStored(new Uint8Array(0)))).toEqual(new Uint8Array(0));
  });

  it("encodes the mask as an 8-bit greyscale PNG whose pixels are the layer", async () => {
    const m = newMask(37, 11);
    stampDisc(m, 10, 5, 4, 255);
    stampDisc(m, 30, 8, 3, 255);
    const png = await encodeMaskPng(m);
    expect(Array.from(png.subarray(0, 8))).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
    const view = new DataView(png.buffer);
    expect(String.fromCharCode(...png.subarray(12, 16))).toBe("IHDR");
    expect([view.getUint32(16), view.getUint32(20), png[24], png[25]]).toEqual([37, 11, 8, 0]);
    // walk the chunks: every CRC verifies, IDAT inflates to the filtered rows
    let o = 8;
    const idat: Uint8Array[] = [];
    const types: string[] = [];
    while (o < png.length) {
      const len = view.getUint32(o);
      const type = String.fromCharCode(...png.subarray(o + 4, o + 8));
      types.push(type);
      expect(view.getUint32(o + 8 + len)).toBe(crc32(png.subarray(o + 4, o + 8 + len)));
      if (type === "IDAT") idat.push(png.subarray(o + 8, o + 8 + len));
      o += 12 + len;
    }
    expect(types).toEqual(["IHDR", "IDAT", "IEND"]);
    const rows = await inflate(idat[0] as Uint8Array<ArrayBuffer>);
    expect(rows.length).toBe(11 * 38);
    for (let y = 0; y < 11; y += 1) {
      expect(rows[y * 38]).toBe(0);
      expect(Array.from(rows.subarray(y * 38 + 1, (y + 1) * 38))).toEqual(Array.from(m.data.subarray(y * 37, (y + 1) * 37)));
    }
  });

  it("policy: architecture, glazing, signage and other are never editable", () => {
    expect([...NEVER_EDITABLE].sort()).toEqual(["architecture", "glazing", "other", "signage"]);
    expect(parseIdPick({ id: "o:1", name: "ARCH_wall", class: "architecture" }).editable).toBe(false);
    expect(parseIdPick({ id: "o:2", name: "VEG_palm", class: "vegetation" }).editable).toBe(true);
    expect(parseIdPick({ id: "o:2", name: "VEG_palm", class: "vegetation", editable: false }).editable).toBe(false);
    expect(parseIdPick({ id: null }).id).toBeNull();
    expect(parseIdPick({ id: "o:3", name: "x", class: "nonsense" }).class).toBe("other");
  });
});

// ================================================================================================ pure: colour, params, parsers

describe("colour helpers", () => {
  it("reads RAL codes and hex colours", () => {
    expect(parseTarget("RAL 7016")).toEqual({ ok: true, target: { kind: "ral", code: "7016", hex: "#383E42", label: "RAL 7016" } });
    expect(parseTarget("ral3020")).toMatchObject({ ok: true, target: { kind: "ral", code: "3020", hex: "#CC0605" } });
    expect(parseTarget("7035")).toMatchObject({ ok: true, target: { kind: "ral", code: "7035" } });
    expect(parseTarget("#abc")).toEqual({ ok: true, target: { kind: "hex", hex: "#AABBCC", label: "#AABBCC" } });
    expect(parseTarget("383e42")).toMatchObject({ ok: true, target: { kind: "hex", hex: "#383E42" } });
    expect(parseTarget("RAL 9999")).toMatchObject({ ok: false, reason: expect.stringMatching(/not in the built-in table/) });
    expect(parseTarget("")).toMatchObject({ ok: false });
    expect(parseTarget("teal")).toMatchObject({ ok: false });
    expect(normalizeHex("#12345")).toBeNull();
    expect(Object.keys(RAL_SRGB)).toHaveLength(15);
  });

  it("converts sRGB to Lab like the daemon (values from plate_gates.recolour.hex_lab) and back", () => {
    const expected: Record<string, [number, number, number]> = {
      "#383E42": [25.801, -1.51, -3.302],
      "#CC0605": [42.738, 67.17, 55.173],
      "#F1ECE1": [93.506, -0.2, 5.921],
      "#0E0E10": [4.018, 0.362, -0.985],
    };
    for (const [hex, lab] of Object.entries(expected)) {
      const got = hexToLab(hex);
      lab.forEach((v, i) => expect(got[i]).toBeCloseTo(v, 1));
      expect(labToHex(got)).toBe(hex);
    }
  });

  it("mood presets expand like lightmix.preset_gains", () => {
    expect(presetGains(MOOD_PRESETS.dusk ?? {}, ["sun", "sky", "interior_1", "Interior_Lamp", "fill"])).toEqual({
      sun: 0.15,
      sky: 0.6,
      interior_1: 3.0,
      Interior_Lamp: 3.0,
      fill: 1.0,
    });
    expect(presetGains({ "*": 2 }, ["a", "b"])).toEqual({ a: 2, b: 2 });
    expect(presetGains({ "lamp?": 4 }, ["lamp1", "lamp12"], 0.5)).toEqual({ lamp1: 4, lamp12: 0.5 });
  });
});

describe("params and parsers", () => {
  it("mergeParams adds, replaces and removes (undefined)", () => {
    expect(mergeParams({ a: 1, b: 2 }, { b: 3, c: 4 })).toEqual({ a: 1, b: 3, c: 4 });
    expect(mergeParams({ a: 1, b: 2 }, { a: undefined })).toEqual({ b: 2 });
    expect(mergeParams(undefined, { a: 1 })).toEqual({ a: 1 });
  });

  it("stillRef finds the nearest upstream image, else the plate; upstreamPlates walks any depth", () => {
    openGraph("Motion");
    useGraphStore.getState().loadProject(
      {
        schema: 1,
        nodes: [
          { id: "plate-1", kind: "Plate", packetId: "room_a", tier: "A", outputHash: "h_plate", outputSize: PLATE },
          { id: "plate-2", kind: "Plate", packetId: "room_b", tier: "A", outputHash: "h_plate2", outputSize: PLATE },
          { id: "edit-1", kind: "Edit", outputHash: "h_edit", outputSize: PLATE },
          { id: "edit-2", kind: "Edit" },
          { id: "set-1", kind: "Set" },
          { id: "cp-1", kind: "CropPair" },
        ],
        edges: [
          { id: "a", source: "plate-1", target: "edit-1", sourceHandle: null, targetHandle: null },
          { id: "b", source: "edit-1", target: "edit-2", sourceHandle: null, targetHandle: null },
          { id: "c", source: "edit-2", target: "cp-1", sourceHandle: null, targetHandle: null },
          { id: "d", source: "plate-1", target: "set-1", sourceHandle: null, targetHandle: null },
          { id: "e", source: "edit-2", target: "set-1", sourceHandle: null, targetHandle: null },
          { id: "f", source: "plate-2", target: "set-1", sourceHandle: null, targetHandle: null },
        ],
      } as never,
      "p1",
    );
    const { nodes, edges } = useGraphStore.getState();
    // edit-2 has no output of its own, so the crop pair's still is edit-1's; the packet is the plate's
    expect(stillRef(nodes, edges, "cp-1")).toEqual({ nodeId: "edit-1", hash: "h_edit", size: PLATE, packetId: "room_a", tier: "A" });
    expect(stillRef(nodes, edges, "edit-1")?.nodeId).toBe("plate-1");
    expect(upstreamPlates(nodes, edges, "set-1").map((n) => n.id).sort()).toEqual(["plate-1", "plate-2"]);
    expect(upstreamPlates(nodes, edges, "cp-1").map((n) => n.id)).toEqual(["plate-1"]);
  });

  it("reads the Set gate's JSON: targets keyed by material, per_view, per_material, cross_view_p90", () => {
    const s = parseSetSummary({
      hero: { packet_id: "room_a", n_materials: 2 },
      targets: { "m:aaaa0001": { name: "MAT_panel", lab: [25.8, -1.5, -3.3] }, "m:aaaa0002": { name: "MAT_leaf", lab: [40, -20, 30] } },
      per_view: [{ index: 0, packet_id: "room_b", status: "ok", per_material: { "m:aaaa0001": { de00_p90: 2.1 }, "m:aaaa0002": { de00_p90: 4.4 } } }],
      per_material: { "m:aaaa0001": { de00_p90: 2.1, hard: true, name: "MAT_panel" }, "m:aaaa0002": { de00_p90: 4.4, hard: false, reason: "editable material: advisory" } },
      cross_view_p90: 2.1,
      verdict: "pass",
    });
    expect(s.hero).toBe("room_a");
    expect(s.views).toEqual(["room_b"]);
    expect(s.crossViewP90).toBe(2.1);
    expect(s.verdict).toBe("pass");
    expect(s.rows).toEqual([
      { id: "m:aaaa0001", name: "MAT_panel", lab: [25.8, -1.5, -3.3], hard: true, p90: 2.1, perView: { room_b: 2.1 }, note: null },
      { id: "m:aaaa0002", name: "MAT_leaf", lab: [40, -20, 30], hard: false, p90: 4.4, perView: { room_b: 4.4 }, note: "editable material: advisory" },
    ]);
    expect(parseSetSummary(null).rows).toEqual([]);
    expect(parseSetSummary({ report: { cross_view_p90: 1, targets: [{ id: "m:1", name: "x", lab: [1, 2, 3] }] } }).rows[0]?.id).toBe("m:1");
  });

  it("reads lightmix.solve and recolour.apply JSON", () => {
    expect(parseLightMixSeed({ gains: { sun: 0.5, sky: 2 }, residual_rms: 0.0004, clipped: ["sky"] })).toEqual({ lights: ["sun", "sky"], gains: { sun: 0.5, sky: 2 }, residualRms: 0.0004, clipped: ["sky"] });
    expect(parseLightMixSeed({ lights: [{ name: "a" }, "b"], gains: {} }).lights).toEqual(["a", "b"]);
    expect(parseRecolourExpect({ method: "recomposition", expected_de00: 0.8 })).toEqual({ method: "recomposition", expectedDe00: 0.8, degraded: false, metal: false });
    expect(parseRecolourExpect({ method: "degraded", expected_de00: 3.9, metal: true })).toMatchObject({ degraded: true, metal: true });
  });
});

// ================================================================================================ registry

describe("editorFor", () => {
  it("has an editor for exactly the nine kinds the task names", () => {
    expect([...EDITOR_KINDS].sort()).toEqual(["CropPair", "Light", "LightMix", "Mask", "Motion", "People", "Recolour", "Scout", "Set"]);
    for (const kind of NODE_KINDS) {
      if ((EDITOR_KINDS as readonly string[]).includes(kind)) expect(editorFor(kind)).not.toBeNull();
      else expect(editorFor(kind)).toBeNull();
    }
  });

  it("T112: the parameter panels live in the path registry, so every kind with parameters has exactly one editor", () => {
    expect([...PARAM_PANEL_KINDS].sort()).toEqual(["Atmosphere", "Colour", "Comp", "Frame", "Sky"]);
    for (const kind of PARAM_PANEL_KINDS) {
      expect(editorFor(kind)).toBeNull();
      expect(pathEditorFor(kind)).not.toBeNull();
    }
    // what is left without an editor: the gate and judge reports, the A/B compare, the unmeasured concept
    const withEditor = new Set<string>([...EDITOR_KINDS, ...PATH_EDITOR_KINDS]);
    expect(NODE_KINDS.filter((k) => !withEditor.has(k)).sort()).toEqual(["Compare", "Concept", "Gate", "Judge"]);
  });

  it.each(EDITOR_KINDS)("the %s editor renders with mock data", async (kind) => {
    mockApi({
      "POST /projects/p1/nodes/n1/preview": {},
    });
    openGraph(kind);
    const Editor = editorFor(kind);
    expect(Editor).not.toBeNull();
    if (!Editor) return;
    mount(<Editor nodeId="n1" />);
    const frame = await screen.findByTestId(`editor-${kind.toLowerCase()}`);
    expect(frame).toHaveAttribute("data-node", "n1");
    expect(frame).toHaveAccessibleName(/editor$/);
  });

  it("hands out a fresh editor per node: typed text does not leak to the next node of the same kind", () => {
    mockApi({});
    useGraphStore.getState().loadProject(
      {
        schema: 1,
        nodes: [
          { id: "plate-1", kind: "Plate", packetId: "room_a", tier: "A", outputHash: "h_plate", outputSize: PLATE },
          { id: "r1", kind: "Recolour" },
          { id: "r2", kind: "Recolour" },
        ],
        edges: [
          { id: "a", source: "plate-1", target: "r1", sourceHandle: null, targetHandle: null },
          { id: "b", source: "plate-1", target: "r2", sourceHandle: null, targetHandle: null },
        ],
      } as never,
      "p1",
    );
    const Editor = editorFor("Recolour");
    if (!Editor) throw new Error("no Recolour editor");
    const view = mount(<Editor nodeId="r1" />);
    fireEvent.change(screen.getByTestId("recolour-target"), { target: { value: "RAL 3020" } });
    expect(screen.getByTestId("recolour-target")).toHaveValue("RAL 3020");
    view.rerender(<Editor nodeId="r2" />);
    expect(screen.getByTestId("editor-recolour")).toHaveAttribute("data-node", "r2");
    expect(screen.getByTestId("recolour-target")).toHaveValue("");
    view.rerender(<Editor nodeId="r1" />);
    expect(screen.getByTestId("recolour-target")).toHaveValue("RAL 3020");
  });

  it("renders an editor for a node that is not in the graph without throwing", () => {
    mockApi();
    for (const kind of EDITOR_KINDS) {
      const Editor = editorFor(kind);
      if (!Editor) continue;
      const { unmount } = mount(<Editor nodeId="missing" />);
      expect(screen.getByTestId(`editor-${kind.toLowerCase()}`)).toBeInTheDocument();
      unmount();
    }
  });
});

describe("App slot", () => {
  const select = (id: string): void => useGraphStore.setState((s) => ({ nodes: s.nodes.map((n) => ({ ...n, selected: n.id === id })) }));

  it("mounts the editor of the selected node and swaps it when the selection moves", async () => {
    mockApi({});
    useGraphStore.getState().loadProject(
      {
        schema: 1,
        nodes: [
          { id: "plate-1", kind: "Plate", packetId: "room_a", tier: "A", outputHash: "h_plate", outputSize: PLATE },
          { id: "mask-1", kind: "Mask" },
          { id: "scout-1", kind: "Scout" },
          { id: "gate-1", kind: "Gate" },
        ],
        edges: [{ id: "a", source: "plate-1", target: "mask-1", sourceHandle: null, targetHandle: null }],
      } as never,
      "p1",
    );
    render(
      <ReactFlowProvider>
        <App />
      </ReactFlowProvider>,
    );
    expect(screen.queryByTestId("editor-mask")).toBeNull();
    select("mask-1");
    expect(await screen.findByTestId("editor-mask")).toHaveAttribute("data-node", "mask-1");
    select("scout-1");
    expect(await screen.findByTestId("editor-scout")).toHaveAttribute("data-node", "scout-1");
    expect(screen.queryByTestId("editor-mask")).toBeNull();
    // a kind without an editor shows none, and the Make bar still follows the selection
    select("gate-1");
    await waitFor(() => expect(screen.queryByTestId("editor-scout")).toBeNull());
    expect(screen.queryByTestId("editor-gate")).toBeNull();
    expect(screen.getByTestId("make-target")).toHaveTextContent("gate-1");
  });
});

// ================================================================================================ CropPair

describe("CropPairEditor", () => {
  beforeEach(() => mockApi());

  it("draws the amphi boxes on the still and prints them in native pixels", () => {
    openGraph("CropPair", { params: { cx: 3633, cy: 2174, zoomStart: 1.5, zoomEnd: 2.3 } });
    mount(<CropPairEditor nodeId="n1" />);
    expect(screen.getByTestId("croppair-box-start")).toHaveTextContent("1799, 1024 3668 x 2048");
    expect(screen.getByTestId("croppair-box-end")).toHaveTextContent("2437, 1507 2392 x 1334");
    const start = screen.getByTestId("croppair-rect-start");
    const end = screen.getByTestId("croppair-rect-end");
    expect(parseFloat(start.style.left)).toBeCloseTo((100 * 1799) / 5504, 2);
    expect(parseFloat(start.style.top)).toBeCloseTo((100 * 1024) / 3072, 2);
    expect(parseFloat(start.style.width)).toBeCloseTo((100 * 3668) / 5504, 2);
    expect(parseFloat(end.style.left)).toBeCloseTo((100 * 2437) / 5504, 2);
    expect(parseFloat(end.style.height)).toBeCloseTo((100 * 1334) / 3072, 2);
    expect(screen.getByTestId("croppair-push")).toHaveTextContent("push-in 1.53x");
    expect(screen.getByTestId("croppair-zoom-start-value")).toHaveTextContent("1.50x");
  });

  it("clicking the still moves the centre to that image pixel", () => {
    openGraph("CropPair", { params: { zoomStart: 1.0, zoomEnd: 1.3 } });
    mount(<CropPairEditor nodeId="n1" />);
    expect(screen.getByTestId("croppair-centre-text")).toHaveTextContent("centre 2752, 1536");
    pointerAt(screen.getByTestId("croppair-stage"), 0.75, 0.25);
    expect(paramsOf()).toMatchObject({ cx: 4128, cy: 768 });
    expect(screen.getByTestId("croppair-centre-text")).toHaveTextContent("centre 4128, 768");
  });

  it("dragging on the still moves the centre with the pointer, and stops when it is released", () => {
    openGraph("CropPair", { params: { zoomStart: 1.0, zoomEnd: 1.3 } });
    mount(<CropPairEditor nodeId="n1" />);
    const stage = screen.getByTestId("croppair-stage");
    pointerAt(stage, 0.25, 0.25);
    pointerAt(stage, 0.5, 0.5, "pointerMove");
    expect(paramsOf()).toMatchObject({ cx: 2752, cy: 1536 });
    pointerAt(stage, 0.5, 0.5, "pointerUp");
    pointerAt(stage, 0.9, 0.9, "pointerMove");
    expect(paramsOf()).toMatchObject({ cx: 2752, cy: 1536 });
  });

  it("the zoom sliders cover 1.0 to 3.0 and keep start below end", () => {
    openGraph("CropPair", { params: { zoomStart: 1.5, zoomEnd: 2.3 } });
    mount(<CropPairEditor nodeId="n1" />);
    const start = screen.getByTestId("croppair-zoom-start") as HTMLInputElement;
    const end = screen.getByTestId("croppair-zoom-end") as HTMLInputElement;
    for (const s of [start, end]) {
      expect(s).toHaveAttribute("min", "1");
      expect(s).toHaveAttribute("max", "3");
    }
    fireEvent.change(start, { target: { value: "2.4" } });
    expect(paramsOf()).toMatchObject({ zoomStart: 2.4, zoomEnd: 2.45 });
    fireEvent.change(end, { target: { value: "1" } });
    expect(paramsOf()).toMatchObject({ zoomStart: 0.95 < 1 ? 1 : 0.95, zoomEnd: 1.05 });
    expect(screen.queryByTestId("croppair-zoom-error")).toBeNull();
  });

  it("an invalid pair stored on the node shows the daemon's rule and draws nothing", () => {
    openGraph("CropPair", { params: { zoomStart: 2.0, zoomEnd: 1.5 } });
    mount(<CropPairEditor nodeId="n1" />);
    expect(screen.getByTestId("croppair-zoom-error")).toHaveTextContent(/zoom pair must satisfy/);
    expect(screen.queryByTestId("croppair-rect-start")).toBeNull();
    expect(screen.queryByTestId("croppair-boxes")).toBeNull();
  });

  it("without a known image size it says so instead of guessing rectangles", () => {
    openGraph("CropPair", {}, { outputSize: undefined });
    mount(<CropPairEditor nodeId="n1" />);
    expect(screen.getByTestId("croppair-nosize")).toBeInTheDocument();
    expect(screen.queryByTestId("croppair-rect-start")).toBeNull();
  });
});

// ================================================================================================ Mask

describe("MaskEditor", () => {
  const OBJ = { id: "o:dc37ac5e", name: "VEG_palm_row_A", class: "vegetation", kind: "object" };

  it("picks a Cryptomatte id under the click and shows a chip in the class colour", async () => {
    const calls = mockApi({ "POST /pixels/idpick": OBJ });
    openGraph("Mask");
    mount(<MaskEditor nodeId="n1" />);
    pointerAt(screen.getByTestId("mask-stage"), 0.5, 0.5);
    const chip = await screen.findByTestId("mask-chip");
    expect(chip).toHaveAttribute("data-id", "o:dc37ac5e");
    expect(chip).toHaveAttribute("data-class", "vegetation");
    expect(chip).toHaveTextContent("VEG_palm_row_A");
    expect(posts(calls, "/pixels/idpick")[0]?.body).toEqual({ packet_id: "room_a", x: 2752, y: 1536 });
    expect(paramsOf()).toMatchObject({ ids: ["o:dc37ac5e"], idInfo: [{ id: "o:dc37ac5e", name: "VEG_palm_row_A", class: "vegetation" }] });
    expect(screen.getByTestId("mask-output-chip")).toHaveTextContent("output: 1 id");
  });

  it("refuses a class policy never opens, a pixel with no id, and a repeat", async () => {
    let next: unknown = { id: "o:11111111", name: "ARCH_wall", class: "architecture" };
    mockApi({ "POST /pixels/idpick": () => next });
    openGraph("Mask");
    mount(<MaskEditor nodeId="n1" />);
    const stage = screen.getByTestId("mask-stage");
    pointerAt(stage, 0.2, 0.2);
    expect(await screen.findByTestId("mask-note")).toHaveTextContent(/ARCH_wall is architecture.*never opens/);
    expect(screen.queryByTestId("mask-chip")).toBeNull();

    next = { id: null };
    pointerAt(stage, 0.3, 0.3);
    await waitFor(() => expect(screen.getByTestId("mask-note")).toHaveTextContent(/no Cryptomatte id/));

    next = OBJ;
    pointerAt(stage, 0.4, 0.4);
    await screen.findByTestId("mask-chip");
    pointerAt(stage, 0.4, 0.4);
    await waitFor(() => expect(screen.getByTestId("mask-note")).toHaveTextContent(/already selected/));
    expect(screen.getAllByTestId("mask-chip")).toHaveLength(1);
  });

  it("removes a chip with its x button and shows a daemon refusal", async () => {
    mockApi({ "POST /pixels/idpick": OBJ });
    openGraph("Mask");
    mount(<MaskEditor nodeId="n1" />);
    pointerAt(screen.getByTestId("mask-stage"), 0.5, 0.5);
    const chip = await screen.findByTestId("mask-chip");
    fireEvent.click(within(chip).getByRole("button", { name: /remove VEG_palm_row_A/ }));
    expect(screen.queryByTestId("mask-chip")).toBeNull();
    expect(paramsOf().ids).toEqual([]);

    mockApi({ "POST /pixels/idpick": () => jsonResponse({ detail: "pixels worker is down" }, 503) });
    pointerAt(screen.getByTestId("mask-stage"), 0.5, 0.5);
    expect(await screen.findByTestId("mask-error")).toHaveTextContent("pixels worker is down");
  });

  it("paints with the brush, draws the layer, erases, and exports a PNG mask to the CAS", async () => {
    const calls = mockApi({ "POST /assets": () => ({ hash: "mask_hash_0123456789abcdef" }) });
    openGraph("Mask");
    mount(<MaskEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("mask-tab-paint"));
    expect(screen.getByTestId("mask-paint-coverage")).toHaveTextContent("painted 0.0% of the frame at 2048x1143");
    expect(screen.getByTestId("mask-paint-save")).toBeDisabled();

    const stage = screen.getByTestId("mask-stage");
    pointerAt(stage, 0.25, 0.5);
    pointerAt(stage, 0.5, 0.5, "pointerMove");
    pointerAt(stage, 0.75, 0.5, "pointerMove");
    pointerAt(stage, 0.75, 0.5, "pointerUp");
    const covText = screen.getByTestId("mask-paint-coverage").textContent ?? "";
    const pct = parseFloat(/painted ([\d.]+)%/.exec(covText)?.[1] ?? "0");
    // a 12 px brush along half the frame width: about 1.1% of a 2048x1143 raster
    expect(pct).toBeGreaterThan(1);
    expect(drawn.putImageData).toHaveBeenCalled();
    const lastImage = drawn.putImageData.mock.calls.at(-1)?.[0] as { data: Uint8ClampedArray };
    expect(lastImage.data.some((v) => v > 0)).toBe(true);

    // the pointer is up: moving now does not paint
    pointerAt(stage, 0.1, 0.1, "pointerMove");
    expect(screen.getByTestId("mask-paint-coverage").textContent).toBe(covText);

    // erase takes part of it back
    fireEvent.click(screen.getByTestId("mask-brush-erase"));
    pointerAt(stage, 0.5, 0.5);
    pointerAt(stage, 0.5, 0.5, "pointerUp");
    const after = parseFloat(/painted ([\d.]+)%/.exec(screen.getByTestId("mask-paint-coverage").textContent ?? "")?.[1] ?? "0");
    expect(after).toBeLessThan(pct);
    expect(after).toBeGreaterThan(0);

    fireEvent.click(screen.getByTestId("mask-paint-save"));
    await waitFor(() => expect(paramsOf().maskHash).toBe("mask_hash_0123456789abcdef"));
    const up = calls.find((c) => c.method === "POST" && c.path === "/assets");
    expect(up?.search).toBe("?kind=mask&project_id=p1&packet_id=room_a"); // T82: the daemon scales it to the plate
    const png = up?.body as Uint8Array;
    expect(Array.from(png.subarray(0, 4))).toEqual([0x89, 0x50, 0x4e, 0x47]);
    const view = new DataView(png.buffer, png.byteOffset, png.byteLength);
    expect([view.getUint32(16), view.getUint32(20)]).toEqual([2048, 1143]);
    expect(screen.getByTestId("mask-output-chip")).toHaveTextContent("mask mask_hash_");
  });

  it("keeps the paint when the editor is closed and reopened, and clears it on request", () => {
    mockApi();
    openGraph("Mask");
    const first = mount(<MaskEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("mask-tab-paint"));
    pointerAt(screen.getByTestId("mask-stage"), 0.5, 0.5);
    pointerAt(screen.getByTestId("mask-stage"), 0.5, 0.5, "pointerUp");
    const text = screen.getByTestId("mask-paint-coverage").textContent;
    first.unmount();
    mount(<MaskEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("mask-tab-paint"));
    expect(screen.getByTestId("mask-paint-coverage").textContent).toBe(text);
    fireEvent.click(screen.getByTestId("mask-paint-clear"));
    expect(screen.getByTestId("mask-paint-coverage")).toHaveTextContent("painted 0.0%");
  });

  it("SAM click sends the points to pixels /segment and keeps them", async () => {
    const calls = mockApi({ "POST /pixels/segment": { mask_hash: "sam_hash_aaaaaaaaaa", segments: 3 } });
    openGraph("Mask");
    mount(<MaskEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("mask-tab-sam"));
    const stage = screen.getByTestId("mask-stage");
    pointerAt(stage, 0.5, 0.5);
    await waitFor(() => expect(paramsOf().samHash).toBe("sam_hash_aaaaaaaaaa"));
    pointerAt(stage, 0.25, 0.75);
    await waitFor(() => expect(posts(calls, "/pixels/segment")).toHaveLength(2));
    expect(posts(calls, "/pixels/segment")[0]?.body).toEqual({ packet_id: "room_a", points: [[2752, 1536]] });
    expect(posts(calls, "/pixels/segment")[1]?.body).toEqual({ packet_id: "room_a", points: [[2752, 1536], [1376, 2304]] });
    expect(screen.getAllByTestId("mask-sam-point")).toHaveLength(2);
    expect(screen.getByTestId("mask-sam-count")).toHaveTextContent("2 points · mask sam_hash_a");
    expect(screen.getByTestId("mask-note")).toHaveTextContent("SAM: 3 regions");
    fireEvent.click(screen.getByTestId("mask-sam-clear"));
    expect(paramsOf().samPoints).toBeUndefined();
    expect(screen.queryAllByTestId("mask-sam-point")).toHaveLength(0);
  });

  it("the text-lock toggle fetches the OCR boxes and shows them", async () => {
    const calls = mockApi({
      "POST /pixels/ocr": {
        shape: [3072, 5504],
        boxes: [
          { bbox: [100, 200, 900, 400], text: "TULA", conf: 0.98 },
          { bbox: { x0: 1000, y0: 1000, x1: 1400, y1: 1200 }, text: "ENTRY", conf: 0.9 },
          { bbox: "bad" },
        ],
      },
    });
    openGraph("Mask");
    mount(<MaskEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("mask-tab-text"));
    expect(screen.queryAllByTestId("mask-ocr-box")).toHaveLength(0);
    fireEvent.click(screen.getByTestId("mask-text-lock"));
    expect(paramsOf().textLock).toBe(true);
    await waitFor(() => expect(screen.getAllByTestId("mask-ocr-box")).toHaveLength(2));
    expect(posts(calls, "/pixels/ocr")[0]?.body).toEqual({ packet_id: "room_a" });
    expect(screen.getByTestId("mask-ocr-count")).toHaveTextContent("2 text boxes: TULA, ENTRY");
    const box = screen.getAllByTestId("mask-ocr-box")[0] as HTMLElement;
    expect(parseFloat(box.style.left)).toBeCloseTo((100 * 100) / 5504, 2);
    expect(parseFloat(box.style.width)).toBeCloseTo((100 * 800) / 5504, 2);
    expect(screen.getByTestId("mask-output-chip")).toHaveTextContent("text lock");
    fireEvent.click(screen.getByTestId("mask-text-lock"));
    expect(screen.queryAllByTestId("mask-ocr-box")).toHaveLength(0);
  });

  it("says why it cannot pick when the node has no plate packet or no image size", () => {
    mockApi();
    openGraph("Mask", {}, { packetId: "unlinked" });
    const { unmount } = mount(<MaskEditor nodeId="n1" />);
    expect(screen.getByTestId("mask-unready")).toHaveTextContent(/connect this node to a plate/);
    unmount();
    openGraph("Mask", {}, { outputSize: undefined });
    mount(<MaskEditor nodeId="n1" />);
    expect(screen.getByTestId("mask-unready")).toHaveTextContent(/image size is unknown/);
  });

  it("an output of mask hash + id list: reflects the node's saved params", () => {
    mockApi();
    openGraph("Mask", {
      params: {
        ids: ["o:1", "o:2"],
        idInfo: [{ id: "o:1", name: "VEG_a", class: "vegetation" }, { id: "o:2", name: "GRD_b", class: "ground" }],
        maskHash: "abcdef0123456789",
        textLock: true,
      },
    });
    mockApi({ "POST /pixels/ocr": { boxes: [] } });
    mount(<MaskEditor nodeId="n1" />);
    expect(screen.getAllByTestId("mask-chip").map((c) => c.getAttribute("data-class"))).toEqual(["vegetation", "ground"]);
    expect(screen.getByTestId("mask-output-chip")).toHaveTextContent("output: 2 ids · mask abcdef0123 · text lock");
  });
});

// ================================================================================================ Motion

const QUOTE = {
  id: "q1",
  provider: "atlas",
  model: "kwaivgi/kling-video-o3-4k/image-to-video",
  custody_grade: "D",
  price_usd: 0.9,
  eta_s: 90,
  created_utc: "2026-10-17T10:00:00Z",
  expires_utc: "2026-10-17T10:15:00Z",
} as const;

/** A fresh quote on the Make store for `nodeId`, as if Quote had just been pressed. */
function freshQuote(nodeId = "n1", engineId = "kling-o3-4k"): void {
  useMakeStore.setState({
    entries: {
      [nodeId]: {
        ...EMPTY_ENTRY,
        draft: { ...EMPTY_ENTRY.draft, engineId, params: { duration: 5 } },
        phase: "quoted",
        quote: {
          quote: { ...QUOTE },
          runs: [{ id: "run_1", state: "QUOTED", quote: { ...QUOTE } }],
          totalUsd: 0.9,
          receivedAt: Date.now(),
          ttlMs: 15 * 60 * 1000,
          autoCapUsd: 2,
        },
      },
    },
  });
}

const ROUTER_ADVISORY = {
  mode: "advisory",
  verdict: "rendered_endpoints_plus_clay",
  numbers: { D_px_median: 22.4, detail_verdict: "ai_by_region", disocclusion_fraction: 0.035, orbit_deg: 12, crossings: [] },
  explanation: "3.5 % of end-frame pixels unseen by the start frame, orbit 12.0 deg",
};

describe("MotionEditor", () => {
  beforeEach(() => {
    useMakeStore.setState({ engines: { engines: [...FALLBACK_ENGINES], source: "fallback" }, custody: null, custodyFor: null });
  });

  const motionRoutes = (router: unknown = ROUTER_ADVISORY): Record<string, Route> => ({
    "POST /projects/p1/nodes/n1/preview": router,
    // the store saves the graph before a quote (decision 31); the daemon answers with the stored graph
    "PUT /projects/p1/graph": (c: Call) => c.body,
    "POST /runs/run_1/approve": [{ id: "run_1", state: "APPROVED" }],
  });

  it("lists the risk ladder with labels and repair codes, and shows the policy for the chosen move", async () => {
    mockApi(motionRoutes());
    openGraph("Motion", { params: { move: "aerial_drift" } });
    mount(<MotionEditor nodeId="n1" />);
    const select = screen.getByTestId("motion-move-select") as HTMLSelectElement;
    const labels = Array.from(select.options).map((o) => o.textContent);
    expect(labels).toContain("Push-in · low · F6");
    expect(labels).toContain("Aerial drift · high · A1 A3");
    expect(labels).toContain("Threshold cross · highest · A1 F7");
    expect(labels).toContain("Locked tripod · none");
    expect(select.options).toHaveLength(MOVE_KINDS.length + 1);
    expect(screen.getByTestId("motion-risk")).toHaveTextContent("risk high");
    expect(screen.getByTestId("motion-repair-A1")).toHaveTextContent("geometry warp");
    expect(screen.getByTestId("motion-repair-A3")).toHaveTextContent("scale drift");
    await waitFor(() => expect(screen.getByTestId("motion-router")).toBeInTheDocument());
  });

  it("changing the move writes it to the node and labels the node's risk for the canvas rules", async () => {
    mockApi(motionRoutes());
    openGraph("Motion");
    mount(<MotionEditor nodeId="n1" />);
    fireEvent.change(screen.getByTestId("motion-move-select"), { target: { value: "threshold_cross" } });
    expect(paramsOf().move).toBe("threshold_cross");
    await waitFor(() => expect(nodeOf().data.riskLabel).toBe("highest"));
    fireEvent.change(screen.getByTestId("motion-move-select"), { target: { value: "push_in" } });
    await waitFor(() => expect(nodeOf().data.riskLabel).toBe("low"));
    fireEvent.change(screen.getByTestId("motion-move-select"), { target: { value: "locked_tripod" } });
    await waitFor(() => expect(nodeOf().data.riskLabel).toBeUndefined());
  });

  it("a dial raises the shown risk", () => {
    mockApi(motionRoutes());
    openGraph("Motion", { params: { move: "push_in", revealM: 4 } });
    mount(<MotionEditor nodeId="n1" />);
    expect(screen.getByTestId("motion-risk")).toHaveTextContent("risk med");
    fireEvent.change(screen.getByTestId("motion-dial-reveal"), { target: { value: "12" } });
    expect(paramsOf().revealM).toBe(12);
    expect(screen.getByTestId("motion-risk")).toHaveTextContent("risk high");
  });

  it("shows the router verdict card with D_px_median, disocclusion, orbit and the advisory chip", async () => {
    const calls = mockApi(motionRoutes());
    openGraph("Motion", { params: { move: "push_in", engine_id: "kling-o3-4k", duration: 6 } });
    mount(<MotionEditor nodeId="n1" />);
    const card = await screen.findByTestId("motion-router");
    expect(card).toHaveAttribute("data-verdict", "rendered_endpoints_plus_clay");
    expect(card).toHaveAttribute("data-mode", "advisory");
    expect(screen.getByTestId("motion-router-mode")).toHaveTextContent("advisory");
    expect(screen.getByTestId("motion-router-verdict")).toHaveTextContent("rendered endpoints plus clay");
    expect(screen.getByTestId("motion-router-d")).toHaveTextContent("22.4 px (ai by region)");
    expect(screen.getByTestId("motion-router-disocc")).toHaveTextContent("3.5%");
    expect(screen.getByTestId("motion-router-orbit")).toHaveTextContent("12.0 deg");
    expect(posts(calls, "/projects/p1/nodes/n1/preview")[0]?.body).toEqual({ what: "router", params: { move: "push_in", duration: 6, engine_id: "kling-o3-4k" } });
  });

  it("shows the blocking chip, and says so when the daemon has no verdict", async () => {
    mockApi(motionRoutes({ ...ROUTER_ADVISORY, mode: "blocking", verdict: "render_move" }));
    openGraph("Motion", { params: { move: "push_in" } });
    mount(<MotionEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("motion-router-mode")).toHaveTextContent("blocking"));
    expect(screen.getByTestId("motion-router-verdict")).toHaveTextContent("render move");
    vi.unstubAllGlobals();

    mockApi({});
    useGraphStore.setState(createInitialGraph());
    openGraph("Motion", { params: { move: "push_in" } });
    mount(<MotionEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getAllByTestId("motion-router-none").at(-1)).toHaveTextContent(/router verdict unavailable/));
  });

  it("disables Make for highest risk until the override text is 10+ characters, then logs it and makes", async () => {
    const calls = mockApi(motionRoutes());
    openGraph("Motion", { params: { move: "threshold_cross", engine_id: "kling-o3-4k" } });
    freshQuote();
    mount(<MotionEditor nodeId="n1" />);
    const make = screen.getByTestId("motion-make");
    expect(make).toBeDisabled();
    expect(screen.getByTestId("motion-block")).toHaveTextContent(/highest risk move needs an override reason of at least 10 characters/);

    const text = screen.getByTestId("motion-override-text");
    fireEvent.change(text, { target: { value: "too short" } });
    expect(screen.getByTestId("motion-override-count")).toHaveTextContent("9 / 10 characters");
    expect(make).toBeDisabled();
    fireEvent.change(text, { target: { value: "client signed off the doorway reveal" } });
    expect(make).toBeEnabled();
    expect(screen.queryByTestId("motion-block")).toBeNull();

    fireEvent.click(make);
    // the override is logged on the node first, with the unseen-geometry label
    expect(nodeOf().data.overrides).toHaveLength(1);
    expect(nodeOf().data.overrides[0]?.reason).toBe("client signed off the doorway reveal");
    expect(nodeOf().data.labels).toContain("unseen geometry guessed");
    await waitFor(() => expect(posts(calls, "/runs/run_1/approve")).toHaveLength(1));
    expect(screen.getAllByTestId("motion-label")[0]).toHaveTextContent("unseen geometry guessed");
  });

  it("an override already logged on the node unblocks Make without typing again", () => {
    mockApi(motionRoutes());
    openGraph("Motion", {
      params: { move: "orbit_le_30", engine_id: "kling-o3-4k" },
      overrides: [{ reason: "orbit approved by the client", by: "human", at: "2026-10-17T10:00:00Z" }],
    });
    freshQuote();
    mount(<MotionEditor nodeId="n1" />);
    expect(screen.getByTestId("motion-make")).toBeEnabled();
    expect(screen.getByTestId("motion-override-count")).toHaveTextContent("1 override logged on this node");
  });

  it("a low risk move needs no override: Make needs only a fresh quote", () => {
    mockApi(motionRoutes());
    openGraph("Motion", { params: { move: "push_in", engine_id: "kling-o3-4k" } });
    mount(<MotionEditor nodeId="n1" />);
    expect(screen.queryByTestId("motion-override")).toBeNull();
    expect(screen.getByTestId("motion-make")).toBeDisabled();
    expect(screen.getByTestId("motion-make")).toHaveAttribute("title", "Make needs a fresh quote");
    freshQuote();
    useMakeStore.setState((s) => ({ entries: { ...s.entries } }));
  });

  it("Make is enabled for a low risk move once the quote is fresh", () => {
    mockApi(motionRoutes());
    openGraph("Motion", { params: { move: "push_in", engine_id: "kling-o3-4k" } });
    freshQuote();
    mount(<MotionEditor nodeId="n1" />);
    expect(screen.getByTestId("motion-make")).toBeEnabled();
    expect(screen.getByTestId("motion-quote-line")).toHaveTextContent("0.90 USD · D");
  });

  it("an expired quote disables Make", () => {
    mockApi(motionRoutes());
    openGraph("Motion", { params: { move: "push_in", engine_id: "kling-o3-4k" } });
    freshQuote();
    useMakeStore.setState((s) => {
      const e = s.entries.n1;
      return e && e.quote ? { entries: { n1: { ...e, quote: { ...e.quote, receivedAt: Date.now() - 20 * 60 * 1000 } } } } : {};
    });
    mount(<MotionEditor nodeId="n1" />);
    expect(screen.getByTestId("motion-make")).toBeDisabled();
    expect(screen.getByTestId("motion-quote-line")).toHaveTextContent("expired");
  });

  it("a blocking render_move verdict disables Make even with an override typed and logged", async () => {
    mockApi(motionRoutes({ ...ROUTER_ADVISORY, mode: "blocking", verdict: "render_move" }));
    openGraph("Motion", {
      params: { move: "threshold_cross", engine_id: "kling-o3-4k" },
      overrides: [{ reason: "client signed off", by: "human", at: "2026-10-17T10:00:00Z" }],
    });
    freshQuote();
    mount(<MotionEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("motion-block")).toHaveTextContent(/render this move in V-Ray/));
    fireEvent.change(screen.getByTestId("motion-override-text"), { target: { value: "a long and sincere reason" } });
    expect(screen.getByTestId("motion-make")).toBeDisabled();
  });

  it("an advisory render_move verdict does not block a low risk move", async () => {
    mockApi(motionRoutes({ ...ROUTER_ADVISORY, mode: "advisory", verdict: "render_move" }));
    openGraph("Motion", { params: { move: "push_in", engine_id: "kling-o3-4k" } });
    freshQuote();
    mount(<MotionEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("motion-router-mode")).toHaveTextContent("advisory"));
    expect(screen.getByTestId("motion-make")).toBeEnabled();
  });

  it("offers Kling and Seedance, keeps the duration inside the engine's range and syncs the Make bar's draft", () => {
    mockApi(motionRoutes());
    openGraph("Motion", { params: { move: "push_in" } });
    mount(<MotionEditor nodeId="n1" />);
    const select = screen.getByTestId("motion-engine-select") as HTMLSelectElement;
    const ids = Array.from(select.options).map((o) => o.value).filter(Boolean);
    expect(ids).toEqual(expect.arrayContaining(["kling-o3-4k", "kling-v3-pro", "seedance-2.5-i2v"]));
    expect(ids.every((id) => id.startsWith("kling") || id.startsWith("seedance"))).toBe(true);

    fireEvent.change(select, { target: { value: "seedance-2.5-i2v" } });
    expect(paramsOf()).toMatchObject({ engine_id: "seedance-2.5-i2v", duration: 5 });
    expect(useMakeStore.getState().entry("n1").draft.engineId).toBe("seedance-2.5-i2v");
    expect(screen.getByTestId("motion-duration")).toHaveAttribute("min", "4");
    expect(screen.getByTestId("motion-duration")).toHaveAttribute("max", "30");

    fireEvent.change(screen.getByTestId("motion-duration"), { target: { value: "12" } });
    expect(paramsOf().duration).toBe(12);
    expect(useMakeStore.getState().entry("n1").draft.params).toEqual({ duration: 12 });

    fireEvent.change(select, { target: { value: "kling-o3-4k" } });
    expect(screen.getByTestId("motion-duration")).toHaveAttribute("max", "15");
    expect(paramsOf().duration).toBe(12);
  });

  it("Quote asks the daemon for a quote through the Make store", async () => {
    const calls = mockApi({
      ...motionRoutes(),
      "POST /projects/p1/nodes/n1/quote": { run: { id: "run_9", state: "QUOTED", quote: { ...QUOTE } }, quote: { ...QUOTE } },
    });
    openGraph("Motion", { params: { move: "push_in", engine_id: "kling-o3-4k" } });
    mount(<MotionEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("motion-quote"));
    await waitFor(() => expect(posts(calls, "/projects/p1/nodes/n1/quote")).toHaveLength(1));
    expect(posts(calls, "/projects/p1/nodes/n1/quote")[0]?.body).toMatchObject({ engine_id: "kling-o3-4k" });
    await waitFor(() => expect(screen.getByTestId("motion-make")).toBeEnabled());
  });
});

// ================================================================================================ VideoPlayer

describe("VideoPlayer", () => {
  const TM_JSON = { version: "timemap-v0", packet_fps: "25/1", engine_fps: "24/1", packet_frames: [0, 124], engine_frames: 120, duration_s: 5, method: "nearest" };
  const REPORT = {
    checks: [
      { metric_id: "video.first_frame", value: 5.42, status: "pass" as const },
      { metric_id: "video.last_frame", value: 18.3, status: "fail" as const },
      { metric_id: "video.camera_lock_median_flow_px", value: 0.31, status: "advisory" as const },
      { metric_id: "video.reproj_edge_recall", value: 0.874, status: "pass" as const },
    ],
    metrics: { timemap: TM_JSON },
    backend_notes: ["flow backend dis @960"],
  };

  /** jsdom's media element has no playback: give it a currentTime, pause and play we can read back. */
  function patchMedia() {
    let t = 0;
    Object.defineProperty(HTMLMediaElement.prototype, "currentTime", { configurable: true, get: () => t, set: (v: number) => void (t = v) });
    const pause = vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(() => Promise.resolve());
    return { pause, at: () => t, set: (v: number) => void (t = v) };
  }

  afterEach(() => {
    // eslint-disable-next-line @typescript-eslint/no-dynamic-delete
    delete (HTMLMediaElement.prototype as unknown as Record<string, unknown>).currentTime;
  });

  it("shows the time map readout: engine frame -> fractional packet frame", () => {
    const media = patchMedia();
    mount(<VideoPlayer src="blob:clip" timeMap={parseTimeMap(TM_JSON)} />);
    expect(screen.getByTestId("video-readout")).toHaveTextContent("engine frame 0 / 120");
    expect(screen.getByTestId("video-readout")).toHaveTextContent("packet frame 0.00 (nearest 0)");
    expect(screen.getByTestId("video-timemap")).toHaveTextContent("packet 25 fps, frames 0 to 124 -> engine 24 fps, 120 frames (nearest)");

    // jump to engine frame 119 and let the element report it
    media.set(timeForFrame(119, 24));
    fireEvent.timeUpdate(screen.getByTestId("video-element"));
    expect(screen.getByTestId("video-readout")).toHaveTextContent("engine frame 119 / 120");
    expect(screen.getByTestId("video-readout")).toHaveTextContent("packet frame 123.96 (nearest 124)");
    expect(screen.getByTestId("video-readout")).not.toHaveTextContent("clamped");

    // past the packet's range the readout says it clamps
    media.set(timeForFrame(121, 24));
    fireEvent.timeUpdate(screen.getByTestId("video-element"));
    expect(screen.getByTestId("video-readout")).toHaveTextContent("packet frame 126.04 (nearest 124)");
    expect(screen.getByTestId("video-readout")).toHaveTextContent("past the packet's range, clamped");
  });

  it("steps exactly one frame with the buttons and the keys, ten with Shift, and stays inside the clip", () => {
    const media = patchMedia();
    mount(<VideoPlayer src="blob:clip" timeMap={parseTimeMap(TM_JSON)} />);
    const readout = screen.getByTestId("video-readout");
    fireEvent.click(screen.getByTestId("video-fwd1"));
    expect(media.at()).toBeCloseTo(timeForFrame(1, 24), 9);
    expect(readout).toHaveTextContent("engine frame 1 / 120");
    expect(readout).toHaveTextContent("packet frame 1.04 (nearest 1)");
    expect(media.pause).toHaveBeenCalled();

    const player = screen.getByTestId("video-player");
    fireEvent.keyDown(player, { key: "." });
    fireEvent.keyDown(player, { key: "ArrowRight" });
    expect(readout).toHaveTextContent("engine frame 3 / 120");
    fireEvent.keyDown(player, { key: ",", shiftKey: false });
    expect(readout).toHaveTextContent("engine frame 2 / 120");
    fireEvent.keyDown(player, { key: "ArrowRight", shiftKey: true });
    expect(readout).toHaveTextContent("engine frame 12 / 120");
    fireEvent.click(screen.getByTestId("video-back10"));
    expect(readout).toHaveTextContent("engine frame 2 / 120");
    fireEvent.click(screen.getByTestId("video-back10"));
    expect(readout).toHaveTextContent("engine frame 0 / 120");
    fireEvent.click(screen.getByTestId("video-back1"));
    expect(readout).toHaveTextContent("engine frame 0 / 120");
    for (let i = 0; i < 13; i += 1) fireEvent.click(screen.getByTestId("video-fwd10"));
    expect(readout).toHaveTextContent("engine frame 119 / 120");
    // a key that is not the player's is left alone
    expect(fireEvent.keyDown(player, { key: "1" })).toBe(true);
  });

  it("without a time map it still steps, at the given fps, and says no map was recorded", () => {
    const media = patchMedia();
    mount(<VideoPlayer src="blob:clip" fps={30} />);
    fireEvent.click(screen.getByTestId("video-fwd1"));
    expect(media.at()).toBeCloseTo(1.5 / 30, 9);
    expect(screen.getByTestId("video-readout")).toHaveTextContent("engine frame 1");
    expect(screen.getByTestId("video-readout")).toHaveTextContent("no time map recorded for this clip");
    expect(screen.queryByTestId("video-timemap")).toBeNull();
  });

  it("shows landing, lock and drift chips from the report, coloured by status", () => {
    patchMedia();
    mount(<VideoPlayer src="blob:clip" timeMap={timeMapFromReport(REPORT)} report={REPORT} />);
    expect(screen.getByTestId("video-chip-first")).toHaveTextContent("first frame 5.42/255");
    expect(screen.getByTestId("video-chip-first")).toHaveAttribute("data-tone", "pass");
    expect(screen.getByTestId("video-chip-last")).toHaveTextContent("last frame 18.3/255");
    expect(screen.getByTestId("video-chip-last")).toHaveAttribute("data-tone", "fail");
    expect(screen.getByTestId("video-chip-lock")).toHaveTextContent("lock 0.31 px");
    expect(screen.getByTestId("video-chip-lock")).toHaveAttribute("data-tone", "advisory");
    expect(screen.getByTestId("video-chip-drift")).toHaveTextContent("drift 0.874");
    expect(screen.getByTestId("video-notes")).toHaveTextContent("flow backend dis @960");
    expect(timeMapFromReport(REPORT)?.engineFrames).toBe(120);
  });

  it("chips for metrics a report does not hold read 'not scored'", () => {
    const chips = chipsFromReport({ checks: [{ metric_id: "video.first_frame", value: 3, status: "pass" }] });
    expect(chips.map((c) => c.label)).toEqual(["first frame 3/255", "last frame: not scored", "lock: not scored", "drift: not scored"]);
    expect(chips.map((c) => c.tone)).toEqual(["pass", "muted", "muted", "muted"]);
    expect(chipsFromReport(null).every((c) => c.tone === "muted")).toBe(true);
    expect(timeMapFromReport(null)).toBeNull();
  });

  it("a CAS clip loads on request with the bearer token, not on mount", async () => {
    window.sessionStorage.setItem("plate.token", "tok123");
    const calls: { url: string; auth: string | null }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: String(input), auth: new Headers(init?.headers).get("Authorization") });
        return new Response("clip bytes", { status: 200 });
      }),
    );
    // jsdom has no object URLs: give URL the two functions for this test and take them away after
    const had = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
    URL.createObjectURL = vi.fn(() => "blob:made");
    URL.revokeObjectURL = vi.fn();
    try {
      patchMedia();
      mount(<VideoPlayer hash="abc123" />);
      expect(calls).toHaveLength(0);
      expect(screen.queryByTestId("video-element")).toBeNull();
      fireEvent.click(screen.getByTestId("video-load"));
      await waitFor(() => expect(screen.getByTestId("video-element")).toHaveAttribute("src", "blob:made"));
      expect(calls).toEqual([{ url: "/api/v0/assets/abc123", auth: "Bearer tok123" }]);
    } finally {
      URL.createObjectURL = had.create;
      URL.revokeObjectURL = had.revoke;
    }
  });

  it("a failed clip load is shown", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("gone", { status: 404 })));
    mount(<VideoPlayer hash="abc123" />);
    fireEvent.click(screen.getByTestId("video-load"));
    expect(await screen.findByTestId("video-error")).toHaveTextContent("-> 404");
  });

  it("with no clip it says to make the move", () => {
    mount(<VideoPlayer />);
    expect(screen.getByTestId("video-empty")).toBeInTheDocument();
    expect(screen.getByTestId("video-fwd1")).toBeDisabled();
  });
});

describe("MotionEditor player wiring", () => {
  it("shows the picked take's clip, report chips and time map", async () => {
    useMakeStore.setState({ engines: { engines: [...FALLBACK_ENGINES], source: "fallback" } });
    const report = {
      verdict: "pass",
      checks: [{ metric_id: "video.first_frame", value: 4.1, status: "pass" }],
      metrics: { timemap: { packet_fps: "25/1", engine_fps: "24/1", packet_frames: [0, 124], engine_frames: 120, duration_s: 5, method: "nearest" } },
    };
    mockApi({ "POST /projects/p1/nodes/n1/preview": ROUTER_ADVISORY, "GET /runs/run_7/gate": { report, n: 1 } });
    openGraph("Motion", { params: { move: "push_in" }, outputHash: "clip_hash_1" });
    const { useRunStore } = await import("../canvas/runStore");
    useRunStore.getState().reset();
    useRunStore.getState().startRuns("n1", [{ id: "run_7", state: "DONE", outputs: ["clip_hash_1"], gate_report: { verdict: "pass" } }]);
    useRunStore.getState().pick("n1", "run_7");
    mount(<MotionEditor nodeId="n1" />);
    expect(await screen.findByTestId("video-load")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("video-chip-first")).toHaveTextContent("first frame 4.1/255"));
    expect(screen.getByTestId("video-timemap")).toHaveTextContent("120 frames");
    expect(screen.getByTestId("motion-take")).toHaveTextContent("run run_7 · DONE");
    useRunStore.getState().reset();
  });
});

// ================================================================================================ People

describe("PeopleEditor", () => {
  it("places the feet on a click and takes the depth from the packet Z there", async () => {
    const calls = mockApi({ "POST /pixels/idpick": { id: "o:g1", name: "HARD_paving", class: "ground", depth_m: 6.254 } });
    openGraph("People");
    mount(<PeopleEditor nodeId="n1" />);
    pointerAt(screen.getByTestId("people-stage"), 0.5, 0.75);
    await waitFor(() => expect(paramsOf().personDepthM).toBe(6.25));
    expect(posts(calls, "/pixels/idpick")[0]?.body).toEqual({ packet_id: "room_a", x: 2752, y: 2304 });
    expect(paramsOf()).toMatchObject({ placement: { x: 2752, y: 2304 }, depthSource: "z" });
    expect(screen.getByTestId("people-placement")).toHaveTextContent("feet at 2752, 2304");
    expect(screen.getByTestId("people-ground-point")).toBeInTheDocument();
    expect(screen.getByTestId("people-depth-input")).toHaveValue(6.25);
    expect(screen.getByTestId("people-depth-source")).toHaveTextContent("from packet Z");
    expect(screen.getByTestId("people-note")).toHaveTextContent("standing on HARD_paving");
  });

  it("a hand-typed depth wins and is marked as set by hand", async () => {
    mockApi({ "POST /pixels/idpick": { id: "o:g1", name: "HARD_paving", class: "ground", depth_m: 6.254 } });
    openGraph("People");
    mount(<PeopleEditor nodeId="n1" />);
    pointerAt(screen.getByTestId("people-stage"), 0.5, 0.75);
    await waitFor(() => expect(paramsOf().depthSource).toBe("z"));
    fireEvent.change(screen.getByTestId("people-depth-input"), { target: { value: "4.5" } });
    expect(paramsOf()).toMatchObject({ personDepthM: 4.5, depthSource: "manual" });
    expect(screen.getByTestId("people-depth-source")).toHaveTextContent("set by hand");
  });

  it("when the daemon sends no depth the point is kept and the user is asked for one", async () => {
    mockApi({ "POST /pixels/idpick": { id: null } });
    openGraph("People");
    mount(<PeopleEditor nodeId="n1" />);
    pointerAt(screen.getByTestId("people-stage"), 0.25, 0.5);
    await waitFor(() => expect(screen.getByTestId("people-note")).toHaveTextContent(/enter the person's depth by hand/));
    expect(paramsOf().placement).toEqual({ x: 1376, y: 1536 });
    expect(paramsOf().personDepthM).toBeUndefined();
    expect(screen.getByTestId("people-depth-source")).toHaveTextContent("depth not set");
  });

  it("writes cast, behaviour, light and mode to the node, and offers the three STAFFAGE modes", () => {
    mockApi();
    openGraph("People");
    mount(<PeopleEditor nodeId="n1" />);
    fireEvent.change(screen.getByTestId("people-cast"), { target: { value: "cast_aparna" } });
    fireEvent.change(screen.getByTestId("people-behavior"), { target: { value: "walks toward the entrance" } });
    fireEvent.change(screen.getByTestId("people-light"), { target: { value: "low sun from the left" } });
    fireEvent.change(screen.getByTestId("people-mode"), { target: { value: "retake" } });
    expect(paramsOf()).toMatchObject({ cast: "cast_aparna", behavior: "walks toward the entrance", light: "low sun from the left", mode: "retake" });
    expect(Array.from((screen.getByTestId("people-mode") as HTMLSelectElement).options).map((o) => o.value)).toEqual(["i2v", "retake", "wan-edit"]);
    expect(screen.getByTestId("people-custody")).toHaveTextContent(/NDA project refuses it/);
    fireEvent.change(screen.getByTestId("people-cast"), { target: { value: "" } });
    expect(paramsOf().cast).toBeUndefined();
  });
});

// ================================================================================================ Set

describe("SetEditor", () => {
  const REPORT = {
    hero: { packet_id: "room_a", n_materials: 2 },
    targets: { "m:aaaa0001": { name: "MAT_panel", lab: [25.8, -1.5, -3.3] }, "m:aaaa0002": { name: "MAT_leaf", lab: [40, -20, 30] } },
    per_view: [{ index: 0, packet_id: "room_b", status: "ok", per_material: { "m:aaaa0001": { de00_p90: 2.1 }, "m:aaaa0002": { de00_p90: 4.4 } } }],
    per_material: { "m:aaaa0001": { de00_p90: 2.1, hard: true, name: "MAT_panel" }, "m:aaaa0002": { de00_p90: 4.4, hard: false, name: "MAT_leaf", reason: "editable material: advisory" } },
    cross_view_p90: 2.1,
    verdict: "pass",
  };

  function twoCameras(): void {
    useGraphStore.getState().loadProject(
      {
        schema: 1,
        nodes: [
          { id: "plate-1", kind: "Plate", label: "Room A", packetId: "room_a", tier: "A", outputHash: "h_a", outputSize: { width: 640, height: 360 } },
          { id: "plate-2", kind: "Plate", label: "Room B", packetId: "room_b", tier: "A", outputHash: "h_b", outputSize: { width: 640, height: 360 } },
          { id: "n1", kind: "Set", label: "Set" },
        ],
        edges: [
          { id: "a", source: "plate-1", target: "n1", sourceHandle: null, targetHandle: null },
          { id: "b", source: "plate-2", target: "n1", sourceHandle: null, targetHandle: null },
        ],
      } as never,
      "p1",
    );
  }

  it("lists the cameras, measures against the hero and fills the targets table with ΔE per view", async () => {
    const calls = mockApi({ "POST /projects/p1/nodes/n1/preview": REPORT });
    twoCameras();
    mount(<SetEditor nodeId="n1" />);
    const select = screen.getByTestId("set-hero-select") as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.textContent)).toEqual(["pick the hero camera", "Room A · room_a", "Room B · room_b"]);
    expect(screen.getByTestId("set-measure")).toBeDisabled();
    expect(screen.getByTestId("set-empty")).toBeInTheDocument();

    fireEvent.change(select, { target: { value: "room_a" } });
    expect(paramsOf().hero).toBe("room_a");
    expect(screen.getByTestId("set-views")).toHaveTextContent("1 other view: room_b");
    fireEvent.click(screen.getByTestId("set-measure"));
    await screen.findAllByTestId("set-row");
    expect(posts(calls, "/projects/p1/nodes/n1/preview")[0]?.body).toEqual({ what: "set", params: { hero: "room_a", views: ["room_b"] } });

    const rows = screen.getAllByTestId("set-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("MAT_panel");
    expect(within(rows[0] as HTMLElement).getByTestId("set-lab")).toHaveTextContent("L 25.8 a -1.5 b -3.3");
    expect(within(rows[0] as HTMLElement).getByTestId("set-row-p90")).toHaveTextContent("2.1");
    expect(within(rows[0] as HTMLElement).getByTestId("set-cell")).toHaveTextContent("2.1");
    expect(rows[1]).toHaveAttribute("data-hard", "false");
    expect(rows[1]).toHaveTextContent("(advisory)");
    expect(within(rows[1] as HTMLElement).getByTestId("set-cell")).toHaveTextContent("4.4");
    expect(screen.getByTestId("set-view-col")).toHaveTextContent("room_b");
    expect(screen.getByTestId("set-p90")).toHaveTextContent("cross-view p90 2.1 (limit 3)");
    expect(screen.getByTestId("set-p90")).toHaveAttribute("data-tone", "pass");
    expect(screen.getByTestId("set-verdict")).toHaveTextContent("pass");
  });

  it("a p90 over 3 is a failing chip", async () => {
    mockApi({ "POST /projects/p1/nodes/n1/preview": { ...REPORT, cross_view_p90: 3.6, verdict: "fail" } });
    twoCameras();
    openHero();
    mount(<SetEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("set-measure"));
    await waitFor(() => expect(screen.getByTestId("set-p90")).toHaveAttribute("data-tone", "fail"));
    expect(screen.getByTestId("set-p90")).toHaveTextContent("3.6");
    expect(screen.getByTestId("set-verdict")).toHaveTextContent("fail");

    function openHero() {
      useGraphStore.setState((s) => ({ nodes: s.nodes.map((n) => (n.id === "n1" ? { ...n, data: { ...n.data, params: { hero: "room_a" } } } : n)) }));
    }
  });

  it("with one camera there is nothing to compare, and a daemon refusal is shown", async () => {
    mockApi({});
    openGraph("Set", { params: { hero: "room_a" } });
    mount(<SetEditor nodeId="n1" />);
    expect(screen.getByTestId("set-views")).toHaveTextContent("0 other views: none");
    expect(screen.getByTestId("set-measure")).toBeDisabled();
    vi.unstubAllGlobals();

    mockApi({ "POST /projects/p1/nodes/n1/preview": () => jsonResponse({ detail: "no world positions" }, 422) });
    twoCameras();
    useGraphStore.setState((s) => ({ nodes: s.nodes.map((n) => (n.id === "n1" ? { ...n, data: { ...n.data, params: { hero: "room_a" } } } : n)) }));
    useGraphStore.setState({ projectId: "p1" });
    const second = render(<SetEditor nodeId="n1" />);
    const buttons = within(second.container).getAllByTestId("set-measure");
    fireEvent.click(buttons[buttons.length - 1] as HTMLElement);
    expect(await within(second.container).findByTestId("set-error")).toHaveTextContent("no world positions");
  });
});

// ================================================================================================ LightMix

describe("LightMixEditor", () => {
  const SEED = { lights: ["sun", "sky", "interior_1"], gains: { sun: 1, sky: 1, interior_1: 1 } };

  it("learns the light selects from the packet and shows a slider per light from 0 to 8", async () => {
    const calls = mockApi({ "POST /projects/p1/nodes/n1/preview": SEED });
    openGraph("LightMix");
    mount(<LightMixEditor nodeId="n1" />);
    await screen.findByTestId("lightmix-gain-sun");
    expect(posts(calls, "/projects/p1/nodes/n1/preview")[0]?.body).toEqual({ what: "lightmix", params: {} });
    expect(paramsOf().lights).toEqual(["sun", "sky", "interior_1"]);
    for (const name of SEED.lights) {
      const slider = screen.getByTestId(`lightmix-gain-${name}`);
      expect(slider).toHaveAttribute("min", "0");
      expect(slider).toHaveAttribute("max", "8");
    }
  });

  it("a slider writes that light's gain; the others keep theirs", async () => {
    mockApi({ "POST /projects/p1/nodes/n1/preview": SEED });
    openGraph("LightMix", { params: { lights: SEED.lights, gains: { sun: 0.5, sky: 1, interior_1: 1 } } });
    mount(<LightMixEditor nodeId="n1" />);
    fireEvent.change(screen.getByTestId("lightmix-gain-sky"), { target: { value: "2.5" } });
    expect(paramsOf().gains).toEqual({ sun: 0.5, sky: 2.5, interior_1: 1 });
    expect(screen.getByTestId("lightmix-gain-sky-value")).toHaveTextContent("2.50");
    expect(screen.getByTestId("lightmix-gain-sun-value")).toHaveTextContent("0.50");
    fireEvent.click(screen.getByTestId("lightmix-reset"));
    expect(paramsOf().gains).toEqual({ sun: 1, sky: 1, interior_1: 1 });
  });

  it("the dusk mood preset sets starting gains by light name", () => {
    mockApi({});
    openGraph("LightMix", { params: { lights: ["sun", "sky", "interior_1", "fill"] } });
    mount(<LightMixEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("lightmix-preset-dusk"));
    expect(paramsOf().gains).toEqual({ sun: 0.15, sky: 0.6, interior_1: 3, fill: 1 });
    expect(paramsOf().preset).toBe("dusk");
    expect(screen.getByTestId("lightmix-gain-interior_1-value")).toHaveTextContent("3.00");
  });

  it("seeds the gains by NNLS against a target and shows the residual and the clipped lights", async () => {
    const calls = mockApi({
      "POST /projects/p1/nodes/n1/preview": (c: Call) =>
        (c.body as { params: { target_hash?: string } }).params.target_hash
          ? { gains: { sun: 0.4, sky: 2.2, interior_1: 8 }, residual_rms: 0.00123, clipped: ["interior_1"] }
          : SEED,
    });
    openGraph("LightMix", { params: { lights: SEED.lights } });
    mount(<LightMixEditor nodeId="n1" />);
    expect(screen.getByTestId("lightmix-seed-run")).toBeDisabled();
    fireEvent.change(screen.getByTestId("lightmix-target"), { target: { value: "dusk_reference_hash" } });
    expect(paramsOf().targetHash).toBe("dusk_reference_hash");
    fireEvent.click(screen.getByTestId("lightmix-seed-run"));
    await screen.findByTestId("lightmix-residual");
    expect(posts(calls, "/projects/p1/nodes/n1/preview").at(-1)?.body).toEqual({ what: "lightmix", params: { target_hash: "dusk_reference_hash" } });
    expect(paramsOf().gains).toEqual({ sun: 0.4, sky: 2.2, interior_1: 8 });
    expect(screen.getByTestId("lightmix-residual")).toHaveTextContent("residual 0.0012");
    expect(screen.getByTestId("lightmix-clipped")).toHaveTextContent("clipped: interior_1");
  });

  it("the upstream image can be the target, and a missing light list says what the packet needs", async () => {
    mockApi({});
    openGraph("LightMix");
    mount(<LightMixEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("lightmix-use-upstream"));
    expect(paramsOf().targetHash).toBe("h_plate");
    await waitFor(() => expect(screen.getByTestId("lightmix-nolights")).toHaveTextContent(/x_light_\*/));
  });
});

// ================================================================================================ Recolour

describe("RecolourEditor", () => {
  it("picks a material from the plate and turns a RAL code into a swatch", async () => {
    const calls = mockApi({ "POST /pixels/idpick": { id: "m:7016abcd", name: "MAT_panel", class: "architecture", kind: "material" } });
    openGraph("Recolour");
    mount(<RecolourEditor nodeId="n1" />);
    pointerAt(screen.getByTestId("recolour-stage"), 0.5, 0.5);
    await waitFor(() => expect(paramsOf().material).toBe("m:7016abcd"));
    expect(posts(calls, "/pixels/idpick")[0]?.body).toEqual({ packet_id: "room_a", x: 2752, y: 1536, kind: "material" });
    expect(screen.getByTestId("recolour-material")).toHaveTextContent("material MAT_panel (m:7016abcd)");

    fireEvent.change(screen.getByTestId("recolour-target"), { target: { value: "ral 3020" } });
    expect(paramsOf()).toMatchObject({ target: "RAL 3020", targetHex: "#CC0605" });
    expect(screen.getByTestId("recolour-hex")).toHaveTextContent("RAL 3020 · #CC0605");
    expect(screen.getByTestId("recolour-swatch").style.background).toMatch(/rgb\(204, 6, 5\)|#cc0605/i);
  });

  it("shows the expected ΔE00 and method from the numeric preview, coloured by how close it is", async () => {
    const calls = mockApi({ "POST /projects/p1/nodes/n1/preview": { method: "recomposition", expected_de00: 0.8 } });
    openGraph("Recolour", { params: { material: "m:7016abcd", materialName: "MAT_panel", target: "RAL 3020" } });
    mount(<RecolourEditor nodeId="n1" />);
    const chip = await screen.findByTestId("recolour-de");
    expect(chip).toHaveTextContent("expected ΔE00 0.80");
    expect(chip).toHaveAttribute("data-tone", "pass");
    expect(screen.getByTestId("recolour-method")).toHaveTextContent("recomposition");
    expect(posts(calls, "/projects/p1/nodes/n1/preview")[0]?.body).toEqual({ what: "recolour", params: { material_id: "m:7016abcd", ral: "3020" } });
  });

  it("a hex target goes as hex; ratio is advisory; a degraded estimate is a failing chip with its reason", async () => {
    const calls = mockApi({ "POST /projects/p1/nodes/n1/preview": { method: "degraded", expected_de00: 3.9, degraded: true, metal: true } });
    openGraph("Recolour", { params: { material: "m:1", target: "#383e42" } });
    mount(<RecolourEditor nodeId="n1" />);
    const chip = await screen.findByTestId("recolour-de");
    expect(chip).toHaveAttribute("data-tone", "fail");
    expect(screen.getByTestId("recolour-method")).toHaveAttribute("data-tone", "fail");
    expect(screen.getByTestId("recolour-metal")).toBeInTheDocument();
    expect(screen.getByTestId("recolour-expect")).toHaveTextContent(/fails on dark semi-gloss panels/);
    expect(posts(calls, "/projects/p1/nodes/n1/preview")[0]?.body).toEqual({ what: "recolour", params: { material_id: "m:1", hex: "#383E42" } });
  });

  it("rejects a RAL code outside the table and an unreadable colour, and clears the node's target", () => {
    mockApi({});
    openGraph("Recolour", { params: { target: "RAL 7016", targetHex: "#383E42" } });
    mount(<RecolourEditor nodeId="n1" />);
    fireEvent.change(screen.getByTestId("recolour-target"), { target: { value: "RAL 9999" } });
    expect(screen.getByTestId("recolour-target-error")).toHaveTextContent(/not in the built-in table/);
    expect(paramsOf().target).toBeUndefined();
    expect(paramsOf().targetHex).toBeUndefined();
    fireEvent.change(screen.getByTestId("recolour-target"), { target: { value: "teal" } });
    expect(screen.getByTestId("recolour-target-error")).toHaveTextContent(/not a RAL code or a hex colour/);
    fireEvent.change(screen.getByTestId("recolour-target"), { target: { value: "#FFF" } });
    expect(screen.queryByTestId("recolour-target-error")).toBeNull();
    expect(paramsOf()).toMatchObject({ target: "#FFFFFF", targetHex: "#FFFFFF" });
    expect(screen.getByTestId("recolour-noexpect")).toHaveTextContent("pick a material and a colour");
  });
});

// ================================================================================================ Light

describe("LightEditor", () => {
  it("Match in Max queues a light.match job with the reference and the locks", async () => {
    const calls = mockApi({ "POST /plugin/jobs": { id: "job_5", state: "queued" } });
    openGraph("Light", { params: { camera: "Cam_Lobby" } });
    mount(<LightEditor nodeId="n1" />);
    expect(screen.getByTestId("light-match")).toBeDisabled();
    fireEvent.change(screen.getByTestId("light-reference-hash"), { target: { value: "ref_hash_1" } });
    fireEvent.click(screen.getByTestId("light-lock-exposure.ev"));
    fireEvent.click(screen.getByTestId("light-lock-sun.azimuth_deg"));
    fireEvent.click(screen.getByTestId("light-deep"));
    expect(paramsOf()).toMatchObject({ referenceHash: "ref_hash_1", locks: ["exposure.ev", "sun.azimuth_deg"], deep: true });
    fireEvent.click(screen.getByTestId("light-match"));
    await screen.findByTestId("light-job");
    expect(posts(calls, "/plugin/jobs")[0]?.body).toEqual({
      action: "light.match",
      packet_id: "room_a",
      camera: "Cam_Lobby",
      reference_hash: "ref_hash_1",
      locks: ["exposure.ev", "sun.azimuth_deg"],
      deep: true,
    });
    expect(screen.getByTestId("light-job")).toHaveTextContent("match queued: job job_5 (queued)");
  });

  it("an unlock removes the key; the lock list stays sorted and empty clears the param", () => {
    mockApi({});
    openGraph("Light", { params: { locks: ["sun.size"] } });
    mount(<LightEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("light-lock-dome.intensity"));
    expect(paramsOf().locks).toEqual(["dome.intensity", "sun.size"]);
    fireEvent.click(screen.getByTestId("light-lock-sun.size"));
    fireEvent.click(screen.getByTestId("light-lock-dome.intensity"));
    expect(paramsOf().locks).toBeUndefined();
    // all eleven MaxGaffer parameters can be locked
    expect(screen.getAllByTestId(/^light-lock-/)).toHaveLength(11);
  });

  it("queues the Vantage preview without a reference", async () => {
    const calls = mockApi({ "POST /plugin/jobs": { job_id: "job_6" } });
    openGraph("Light");
    mount(<LightEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("light-vantage"));
    await screen.findByTestId("light-job");
    expect(posts(calls, "/plugin/jobs")[0]?.body).toEqual({ action: "light.vantage_preview", packet_id: "room_a" });
    expect(screen.getByTestId("light-job")).toHaveTextContent("vantage preview queued: job job_6");
  });

  it("the upstream image can be the reference; a refusal from the daemon is shown", async () => {
    mockApi({ "POST /plugin/jobs": () => jsonResponse({ error: "no_dock", detail: "no Max dock is connected" }, 409) });
    openGraph("Light");
    mount(<LightEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("light-use-upstream"));
    expect(paramsOf().referenceHash).toBe("h_plate");
    fireEvent.click(screen.getByTestId("light-match"));
    expect(await screen.findByTestId("light-error")).toHaveTextContent("no Max dock is connected");
  });

  it("without a plate packet the buttons are disabled and it says why", () => {
    mockApi({});
    openGraph("Light", { params: { referenceHash: "x" } }, { packetId: "unlinked" });
    mount(<LightEditor nodeId="n1" />);
    expect(screen.getByTestId("light-match")).toBeDisabled();
    expect(screen.getByTestId("light-vantage")).toBeDisabled();
    expect(screen.getByTestId("light-nopacket")).toBeInTheDocument();
  });
});

// ================================================================================================ Scout

describe("ScoutEditor", () => {
  it("count and keep go to the node; the summary line on the canvas reads them", () => {
    mockApi({});
    openGraph("Scout");
    mount(<ScoutEditor nodeId="n1" />);
    expect(screen.getByTestId("scout-count")).toHaveValue(6);
    fireEvent.change(screen.getByTestId("scout-count"), { target: { value: "9" } });
    expect(paramsOf().count).toBe(9);
    // out of range is not written
    fireEvent.change(screen.getByTestId("scout-count"), { target: { value: "99" } });
    expect(paramsOf().count).toBe(9);
    fireEvent.change(screen.getByTestId("scout-count"), { target: { value: "0" } });
    expect(paramsOf().count).toBe(9);
    expect(screen.getByTestId("scout-cleanup")).toHaveTextContent("temporary cameras are deleted afterwards");
    // clearing the box to retype leaves it cleared; the next number goes through
    fireEvent.change(screen.getByTestId("scout-count"), { target: { value: "" } });
    expect(screen.getByTestId("scout-count")).toHaveValue(null);
    expect(paramsOf().count).toBe(9);
    fireEvent.change(screen.getByTestId("scout-count"), { target: { value: "7" } });
    expect(paramsOf().count).toBe(7);
    expect(screen.getByTestId("scout-count")).toHaveValue(7);
    fireEvent.click(screen.getByTestId("scout-keep"));
    expect(paramsOf().keep).toBe(true);
    expect(screen.getByTestId("scout-cleanup")).toHaveTextContent("cameras stay in the scene");
  });
});
