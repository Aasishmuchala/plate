// T82 mask tools: the Rectangle and Polygon fills (maskMath, then through the Mask editor's stage), `Load mask…` and
// `Reset`, the upload that names the plate's packet so the daemon scales it to the plate, and SAM storing the
// plate-sized `mask_hash` (never the label map) with the picked segment ids. The daemon is a fetch stub that answers
// by "METHOD /path" and records every call, as in editors.test.tsx; jsdom reports every element as 1280 x 720.

import { cleanup, configure, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { segment, storeMask, uploadMask } from "../editors/api";
import { clearPaintLayers, MaskEditor } from "../editors/MaskEditor";
import {
  coverage,
  dedupeVertices,
  fillPolygon,
  fillRect,
  MAX_RASTER_SIDE,
  newMask,
  plateBoxToRaster,
  workSize,
} from "../editors/maskMath";
import type { NodeKind } from "../nodeTypes";
import { createInitialGraph, useGraphStore } from "../store";

configure({ asyncUtilTimeout: 5000 });
vi.setConfig({ testTimeout: 20_000 });

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

const PLATE = { width: 5504, height: 3072 };

function openGraph(kind: NodeKind, extra: Record<string, unknown> = {}, projectId: string | null = "p1"): void {
  useGraphStore.getState().loadProject(
    {
      schema: 1,
      nodes: [
        { id: "plate-1", kind: "Plate", label: "Plate", packetId: "room_a", tier: "A", outputHash: "h_plate", outputSize: PLATE },
        { id: "n1", kind, label: kind, ...extra },
      ],
      edges: [{ id: "plate-1->n1", source: "plate-1", target: "n1", sourceHandle: null, targetHandle: null }],
    } as never,
    projectId,
  );
}

const paramsOf = (): Record<string, unknown> => useGraphStore.getState().nodes.find((n) => n.id === "n1")?.data.params ?? {};

const drawn = { putImageData: vi.fn(), createImageData: vi.fn() };

const pointerAt = (stage: HTMLElement, fx: number, fy: number, type: "pointerDown" | "pointerMove" | "pointerUp" = "pointerDown"): void => {
  fireEvent[type](stage, { clientX: fx * 1280, clientY: fy * 720, pointerId: 1 });
};

/** The painted share of the frame the editor reports, in percent. */
const paintedPct = (): number => parseFloat(/painted ([\d.]+)%/.exec(screen.getByTestId("mask-paint-coverage").textContent ?? "")?.[1] ?? "NaN");

function openPaint(extra: Record<string, unknown> = {}, projectId: string | null = "p1"): HTMLElement {
  openGraph("Mask", extra, projectId);
  render(<MaskEditor nodeId="n1" />);
  fireEvent.click(screen.getByTestId("mask-tab-paint"));
  return screen.getByTestId("mask-stage");
}

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  clearPaintLayers();
  window.sessionStorage.clear();
  drawn.putImageData.mockReset();
  drawn.createImageData.mockReset().mockImplementation((w: number, h: number) => ({ data: new Uint8ClampedArray(w * h * 4), width: w, height: h }));
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation((() => drawn) as never);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ================================================================================================ maskMath

describe("rectangle and polygon fills (maskMath)", () => {
  it("paints at up to 2048 on the long side, never upscaled", () => {
    expect(MAX_RASTER_SIDE).toBe(2048);
    expect(workSize(PLATE)).toEqual({ width: 2048, height: 1143 });
    expect(workSize({ width: 640, height: 360 })).toEqual({ width: 640, height: 360 });
  });

  it("a filled box covers its area fraction of the layer within 1 %", () => {
    const m = newMask(2048, 1143);
    for (const [x0, y0, x1, y1] of [
      [512, 285.75, 1536, 857.25],
      [100.3, 40.6, 1900.8, 300.2],
      [1700, 1000, 200, 120], // corners in either order
    ] as const) {
      m.data.fill(0);
      const n = fillRect(m, x0, y0, x1, y1, 255);
      const want = (Math.abs(x1 - x0) * Math.abs(y1 - y0)) / (2048 * 1143);
      expect(Math.abs(coverage(m) - want)).toBeLessThan(0.01 * want);
      expect(n).toBe(Math.round(coverage(m) * 2048 * 1143));
    }
    // clamped to the layer, and erasing takes it back
    const small = newMask(10, 10);
    expect(fillRect(small, -5, -5, 50, 50, 255)).toBe(100);
    expect(fillRect(small, 2, 2, 8, 8, 0)).toBe(36);
    expect(coverage(small)).toBeCloseTo(0.64, 6);
    expect(fillRect(small, 3.2, 3.2, 3.4, 3.4, 255)).toBe(0); // no pixel centre inside
  });

  it("a drag box between two plate pixels includes both end pixels", () => {
    const m = newMask(2048, 1143);
    const r = plateBoxToRaster(m, PLATE, { x: 4128, y: 2304 }, { x: 1376, y: 768 });
    expect(r.x0).toBeCloseTo(512, 6);
    expect(r.x1).toBeCloseTo((4129 * 2048) / 5504, 6);
    expect(r.y0).toBeCloseTo((768 * 1143) / 3072, 6);
    expect(r.y1).toBeCloseTo((2305 * 1143) / 3072, 6);
  });

  it("a triangle covers half of its bounding box within 2 %", () => {
    const m = newMask(1000, 800);
    fillPolygon(
      m,
      [
        { x: 100, y: 100 },
        { x: 900, y: 100 },
        { x: 100, y: 700 },
      ],
      255,
    );
    const ofBox = (coverage(m) * 1000 * 800) / (800 * 600);
    expect(Math.abs(ofBox - 0.5)).toBeLessThan(0.02 * 0.5);
    expect(m.data[150 * 1000 + 150]).toBe(255); // inside, near the right angle
    expect(m.data[650 * 1000 + 850]).toBe(0); // across the hypotenuse
  });

  it("fills by the even-odd rule, needs three corners, and erases", () => {
    // a pentagram: its five points are inside, the pentagon at its centre is covered twice and stays open
    const star = [0, 2, 4, 1, 3].map((k) => {
      const a = -Math.PI / 2 + (k * 2 * Math.PI) / 5;
      return { x: 200 + 180 * Math.cos(a), y: 200 + 180 * Math.sin(a) };
    });
    const m = newMask(400, 400);
    fillPolygon(m, star, 255);
    expect(m.data[200 * 400 + 200]).toBe(0);
    expect(m.data[60 * 400 + 200]).toBe(255); // the top point
    expect(fillPolygon(m, star.slice(0, 2), 255)).toBe(0);
    const sq = newMask(20, 20);
    fillRect(sq, 0, 0, 20, 20, 255);
    const written = fillPolygon(
      sq,
      [
        { x: 5, y: 5 },
        { x: 15, y: 5 },
        { x: 15, y: 15 },
        { x: 5, y: 15 },
      ],
      0,
    );
    expect(written).toBe(100);
    expect(coverage(sq)).toBeCloseTo(0.75, 6);
  });

  it("merges the double-click corner and the closing corner", () => {
    const pts = [
      { x: 1, y: 1 },
      { x: 10, y: 1 },
      { x: 10, y: 1 },
      { x: 10, y: 10 },
      { x: 1, y: 1 },
    ];
    expect(dedupeVertices(pts)).toEqual([
      { x: 1, y: 1 },
      { x: 10, y: 1 },
      { x: 10, y: 10 },
    ]);
    expect(dedupeVertices([{ x: 3, y: 3 }])).toEqual([{ x: 3, y: 3 }]);
  });
});

// ================================================================================================ api

describe("mask upload names the packet (T82)", () => {
  it("uploadMask adds packet_id when given and keeps the T56 URL without it", async () => {
    const calls = mockApi({ "POST /assets": { hash: "h1", width: 640, height: 360, scaled_from: [1024, 576] } });
    expect(await uploadMask(new Uint8Array([0x89]), "p1", "pkt 7/a")).toBe("h1");
    expect(posts(calls, "/assets")[0]?.search).toBe("?kind=mask&project_id=p1&packet_id=pkt%207%2Fa");
    expect(posts(calls, "/assets")[0]?.search).toContain("packet_id=");
    await uploadMask(new Uint8Array([0x89]), "p1");
    expect(posts(calls, "/assets")[1]?.search).toBe("?kind=mask&project_id=p1");
    expect(await storeMask(new Uint8Array([0x89]), "p1", "room_a")).toEqual({ hash: "h1", width: 640, height: 360, scaledFrom: [1024, 576] });
  });

  it("segment reads the plate-sized mask and the picked ids, never the label map as a mask", async () => {
    mockApi({ "POST /pixels/segment": { mask_hash: null, labels_hash: "labels_0123456789", segments: [{ id: 1 }, { id: 2 }], selected: [] } });
    expect(await segment({ packet_id: "room_a", points: [] })).toEqual({ maskHash: null, labelsHash: "labels_0123456789", regions: 2, selected: [] });
  });
});

// ================================================================================================ the editor

describe("Mask editor paint tools (T82)", () => {
  it("Rectangle: a drag previews a box and fills it on release; a click fills nothing", () => {
    mockApi();
    const stage = openPaint();
    fireEvent.click(screen.getByTestId("mask-tool-rect"));
    expect(screen.getByTestId("mask-tool-rect")).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByTestId("mask-brush-size")).toBeNull();

    pointerAt(stage, 0.4, 0.4);
    pointerAt(stage, 0.4, 0.4, "pointerUp");
    expect(paintedPct()).toBe(0);

    pointerAt(stage, 0.25, 0.25);
    pointerAt(stage, 0.6, 0.5, "pointerMove");
    pointerAt(stage, 0.75, 0.75, "pointerMove");
    expect(screen.getByTestId("mask-rect-preview")).toBeInTheDocument();
    expect(paintedPct()).toBe(0); // nothing is written until the pointer is up
    pointerAt(stage, 0.75, 0.75, "pointerUp");
    expect(screen.queryByTestId("mask-rect-preview")).toBeNull();
    expect(Math.abs(paintedPct() - 25)).toBeLessThan(0.25); // the box is a quarter of the frame, within 1 %

    // Erase takes a box back out
    fireEvent.click(screen.getByTestId("mask-brush-erase"));
    pointerAt(stage, 0.25, 0.25);
    pointerAt(stage, 0.5, 0.75, "pointerMove");
    pointerAt(stage, 0.5, 0.75, "pointerUp");
    expect(Math.abs(paintedPct() - 12.5)).toBeLessThan(0.25);
  });

  it("Polygon: click corners, Enter fills a triangle covering half its bounding box; Escape cancels", () => {
    mockApi();
    const stage = openPaint();
    fireEvent.click(screen.getByTestId("mask-tool-polygon"));
    expect(screen.getByTestId("mask-poly-close")).toBeDisabled();
    pointerAt(stage, 0.1, 0.1);
    pointerAt(stage, 0.1, 0.1, "pointerUp");
    pointerAt(stage, 0.9, 0.1);
    pointerAt(stage, 0.9, 0.1, "pointerUp");
    expect(screen.getAllByTestId("mask-poly-vertex")).toHaveLength(2);
    // two corners: Enter refuses, nothing is filled
    fireEvent.keyDown(document.body, { key: "Enter" });
    expect(screen.getByTestId("mask-note")).toHaveTextContent("at least three corners");
    expect(paintedPct()).toBe(0);
    pointerAt(stage, 0.1, 0.9);
    pointerAt(stage, 0.1, 0.9, "pointerUp");
    expect(screen.getByTestId("mask-poly-count")).toHaveTextContent("3 corners");
    expect(screen.getByTestId("mask-poly-outline")).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "Enter" });
    expect(screen.queryAllByTestId("mask-poly-vertex")).toHaveLength(0);
    const ofBox = paintedPct() / (100 * 0.8 * 0.8);
    expect(Math.abs(ofBox - 0.5)).toBeLessThan(0.02 * 0.5);

    // Escape drops a polygon in progress without filling it
    const before = paintedPct();
    pointerAt(stage, 0.6, 0.6);
    pointerAt(stage, 0.9, 0.6);
    pointerAt(stage, 0.9, 0.9);
    expect(screen.getAllByTestId("mask-poly-vertex")).toHaveLength(3);
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryAllByTestId("mask-poly-vertex")).toHaveLength(0);
    expect(paintedPct()).toBe(before);
  });

  it("Polygon: a double-click on the last corner closes it", () => {
    mockApi();
    const stage = openPaint();
    fireEvent.click(screen.getByTestId("mask-tool-polygon"));
    pointerAt(stage, 0.5, 0.2);
    pointerAt(stage, 0.8, 0.8);
    // a double-click: two presses on one spot, then the dblclick
    pointerAt(stage, 0.2, 0.8);
    pointerAt(stage, 0.2, 0.8, "pointerUp");
    pointerAt(stage, 0.2, 0.8);
    pointerAt(stage, 0.2, 0.8, "pointerUp");
    fireEvent.doubleClick(stage);
    expect(screen.queryAllByTestId("mask-poly-vertex")).toHaveLength(0);
    // base 0.6 x height 0.6 of the frame: 18 %
    expect(Math.abs(paintedPct() - 18)).toBeLessThan(0.5);
  });

  it("Save mask names the plate's packet and says when the daemon scaled it", async () => {
    const calls = mockApi({ "POST /assets": { hash: "mask_hash_0123456789", width: 5504, height: 3072, scaled_from: [2048, 1143] } });
    const stage = openPaint();
    fireEvent.click(screen.getByTestId("mask-tool-rect"));
    pointerAt(stage, 0.1, 0.1);
    pointerAt(stage, 0.3, 0.3, "pointerMove");
    pointerAt(stage, 0.3, 0.3, "pointerUp");
    fireEvent.click(screen.getByTestId("mask-paint-save"));
    await waitFor(() => expect(paramsOf().maskHash).toBe("mask_hash_0123456789"));
    expect(posts(calls, "/assets")[0]?.search).toBe("?kind=mask&project_id=p1&packet_id=room_a");
    expect(screen.getByTestId("mask-note")).toHaveTextContent("mask saved (scaled to 5504x3072)");
  });

  it("Load mask… stores the chosen file as the node's mask, scaled to the plate", async () => {
    const calls = mockApi({ "POST /assets": { hash: "loaded_hash_abcdef", width: 5504, height: 3072, scaled_from: [1920, 1080] } });
    openPaint();
    const bytes = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 1, 2, 3]);
    const file = new File([bytes], "facade_mask.png", { type: "image/png" });
    fireEvent.change(screen.getByTestId("mask-load-input"), { target: { files: [file] } });
    await waitFor(() => expect(paramsOf().maskHash).toBe("loaded_hash_abcdef"));
    const up = posts(calls, "/assets")[0];
    expect(up?.search).toBe("?kind=mask&project_id=p1&packet_id=room_a");
    expect(Array.from(up?.body as Uint8Array)).toEqual(Array.from(bytes));
    expect(screen.getByTestId("mask-note")).toHaveTextContent("mask loaded (scaled to 5504x3072) from facade_mask.png");
    expect(screen.getByTestId("mask-output-chip")).toHaveTextContent("mask loaded_has");
  });

  it("Load mask… needs an open project", () => {
    mockApi();
    openPaint({}, null);
    expect(screen.getByTestId("mask-load")).toBeDisabled();
  });

  it("Reset clears the layer and removes maskHash", () => {
    mockApi();
    const stage = openPaint({ params: { maskHash: "abcdef0123456789", ids: ["o:1"] } });
    fireEvent.click(screen.getByTestId("mask-tool-rect"));
    pointerAt(stage, 0.2, 0.2);
    pointerAt(stage, 0.6, 0.6, "pointerMove");
    pointerAt(stage, 0.6, 0.6, "pointerUp");
    expect(paintedPct()).toBeGreaterThan(10);
    fireEvent.click(screen.getByTestId("mask-reset"));
    expect(paintedPct()).toBe(0);
    expect("maskHash" in paramsOf()).toBe(false);
    expect(paramsOf().ids).toEqual(["o:1"]); // the picked ids are not the stored mask
    expect(screen.getByTestId("mask-reset")).toBeDisabled();
  });

  it("SAM keeps the plate-sized mask_hash as samHash and shows the selected segment ids", async () => {
    const calls = mockApi({
      "POST /pixels/segment": { mask_hash: "sam_mask_0123456789", labels_hash: "sam_labels_999999", segments: [{ id: 3 }, { id: 7 }, { id: 9 }], selected: [3, 7] },
    });
    openGraph("Mask");
    render(<MaskEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("mask-tab-sam"));
    pointerAt(screen.getByTestId("mask-stage"), 0.5, 0.5);
    await waitFor(() => expect(paramsOf().samHash).toBe("sam_mask_0123456789"));
    expect(posts(calls, "/pixels/segment")[0]?.body).toEqual({ packet_id: "room_a", points: [[2752, 1536]] });
    expect(screen.getByTestId("mask-sam-selected")).toHaveTextContent("segments 3, 7");
    fireEvent.click(screen.getByTestId("mask-sam-clear"));
    expect(paramsOf().samHash).toBeUndefined();
    expect(screen.queryByTestId("mask-sam-selected")).toBeNull();
  });

  it("SAM with nothing picked stores no samHash (the label map is never a mask)", async () => {
    mockApi({ "POST /pixels/segment": { mask_hash: null, labels_hash: "sam_labels_999999", segments: [{ id: 1 }], selected: [] } });
    openGraph("Mask");
    render(<MaskEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("mask-tab-sam"));
    pointerAt(screen.getByTestId("mask-stage"), 0.5, 0.5);
    await waitFor(() => expect(screen.getByTestId("mask-note")).toHaveTextContent("the daemon stored no mask"));
    expect(paramsOf().samHash).toBeUndefined();
    expect(paramsOf().samPoints).toEqual([[2752, 1536]]);
  });
});
