// Viewer tests without a GPU (jsdom has no WebGL): the colour and half-float math, the 100% tile layout (expected
// values produced by gates/plate_gates/crops.point_tiles), the mode state machine and keyboard map, the loaders, the
// renderer's GL calls against a recording fake context (RGBA16F/HALF_FLOAT, NEAREST at >= 100%, the RGBA8 fallback),
// and the component's DOM behaviour. Real pixels on a real GPU are the browser smoke in src/viewer/README.md.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TOKEN_KEY } from "../api/client";
import {
  LINEARIZE_FRAG,
  LINEARIZE_UNIFORMS,
  VERT,
  VIEW_FRAG,
  VIEW_MODE,
  VIEW_UNIFORMS,
} from "../shaders/viewer";
import { Viewer, type ViewerHandle } from "../viewer";
import {
  decodeHalfBuffer,
  halfChannels,
  isSameOrigin,
  loadHeat,
  loadImage,
  withFormat,
} from "../viewer/loaders";
import {
  clampZoom,
  DE00_LUT_SIZE,
  DE00_STOPS,
  de00Color,
  de00Lut,
  fitTileSize,
  fitZoom,
  fromHalfBits,
  halfFloatPack,
  halfFloatUnpack,
  halfToSrgb8,
  imageToScreen,
  linearToSrgb,
  makeView,
  referenceDiff,
  roundHalfEven,
  screenToImage,
  srgbToLinear,
  TILE_MIN,
  tileGrid,
  tileLayout,
  toHalfBits,
} from "../viewer/math";
import { type HalfPixels, ViewerRenderer } from "../viewer/renderer";
import {
  displayedMode,
  initialViewState,
  keyToAction,
  NO_CAPS,
  PAN_STEP,
  scaleOf,
  type ViewAction,
  viewerReducer,
  type ViewState,
  viewOf,
} from "../viewer/state";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});

// ---------------------------------------------------------------------------------------------------------------
describe("sRGB transfer functions", () => {
  it("hits the endpoints and the knee of IEC 61966-2-1", () => {
    expect(srgbToLinear(0)).toBe(0);
    expect(srgbToLinear(1)).toBeCloseTo(1, 12);
    expect(linearToSrgb(1)).toBeCloseTo(1, 12);
    expect(srgbToLinear(0.04045)).toBeCloseTo(0.0031308, 6);
    expect(linearToSrgb(0.0031308)).toBeCloseTo(0.04045, 5);
  });

  it("matches gates/plate_gates/color.py on 8-bit code values", () => {
    // np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4) for 128/255, 10/255, 255/255
    expect(srgbToLinear(128 / 255)).toBeCloseTo(0.2158605, 6);
    expect(srgbToLinear(10 / 255)).toBeCloseTo(0.0030353, 6);
    expect(srgbToLinear(0.5)).toBeCloseTo(0.2140411, 6);
  });

  it("round-trips across 0..1", () => {
    for (let i = 0; i <= 1000; i++) {
      const v = i / 1000;
      expect(linearToSrgb(srgbToLinear(v))).toBeCloseTo(v, 9);
    }
  });
});

// ---------------------------------------------------------------------------------------------------------------
describe("half floats", () => {
  it("packs known values bit-exactly", () => {
    const cases: [number, number][] = [
      [0, 0x0000],
      [-0, 0x8000],
      [1, 0x3c00],
      [-2, 0xc000],
      [0.5, 0x3800],
      [0.1, 0x2e66],
      [1 / 3, 0x3555],
      [65504, 0x7bff],
      [65520, 0x7c00], // rounds up past the largest finite half
      [1e9, 0x7c00],
      [-Infinity, 0xfc00],
      [2 ** -24, 0x0001],
      [2 ** -14, 0x0400],
      [2 ** -25, 0x0000], // a tie between 0 and the smallest subnormal goes to even
      [1e-9, 0x0000],
    ];
    for (const [v, bits] of cases) expect(toHalfBits(v), `toHalfBits(${v})`).toBe(bits);
    expect(toHalfBits(Number.NaN) & 0x7c00).toBe(0x7c00);
    expect(toHalfBits(Number.NaN) & 0x3ff).not.toBe(0);
  });

  it("rounds ties to even", () => {
    expect(toHalfBits(1 + 2 ** -11)).toBe(0x3c00); // halfway between 0x3c00 and 0x3c01 -> even
    expect(toHalfBits(1 + 3 * 2 ** -11)).toBe(0x3c02); // halfway between 0x3c01 and 0x3c02 -> even
    expect(toHalfBits(1 + 2 ** -11 + 2 ** -20)).toBe(0x3c01); // just above halfway
  });

  it("round-trips every finite and infinite half bit pattern", () => {
    for (let h = 0; h < 0x10000; h++) {
      const e = (h >>> 10) & 0x1f;
      if (e === 0x1f && (h & 0x3ff) !== 0) continue; // NaN payloads
      expect(toHalfBits(fromHalfBits(h))).toBe(h);
    }
  });

  it("packs and unpacks a 64x64 RGBA buffer, and the CPU diff of a buffer against itself is all zeros", () => {
    const n = 64 * 64 * 4;
    const values = new Float32Array(n);
    for (let i = 0; i < n; i++) values[i] = (i % 4 === 3 ? 1 : ((i * 7919) % 1000) / 1000) as number;
    const bits = halfFloatPack(values);
    expect(bits).toHaveLength(n);
    const back = halfFloatUnpack(bits);
    for (let i = 0; i < n; i++) expect(Math.abs(back[i] - values[i])).toBeLessThanOrEqual(2 ** -11);
    for (const linear of [false, true]) {
      const diff = referenceDiff(bits, bits, 8, linear);
      for (let i = 0; i < n; i += 4) {
        expect([diff[i], diff[i + 1], diff[i + 2], diff[i + 3]]).toEqual([0, 0, 0, 1]);
      }
    }
  });

  it("referenceDiff decodes sRGB code values before differencing", () => {
    const a = halfFloatPack([0.5, 0.5, 0.5, 1]);
    const b = halfFloatPack([0, 0, 0, 1]);
    const d = referenceDiff(a, b, 1);
    expect(d[0]).toBeCloseTo(srgbToLinear(0.5), 3);
    expect(referenceDiff(a, b, 1, true)[0]).toBe(0.5);
    expect(() => referenceDiff(a, halfFloatPack([0, 0, 0]))).toThrow();
  });

  it("converts half RGBA to 8-bit sRGB for the RGBA8 fallback", () => {
    const linear = halfFloatPack([srgbToLinear(128 / 255), 0, 1, 1]);
    expect(Array.from(halfToSrgb8(linear, true))).toEqual([128, 0, 255, 255]);
    const codes = halfFloatPack([0.5, -1, 2, 0.5]);
    expect(Array.from(halfToSrgb8(codes, false))).toEqual([128, 0, 255, 128]);
  });
});

// ---------------------------------------------------------------------------------------------------------------
describe("tileLayout: parity with gates point_tiles", () => {
  const rects = (pts: [number, number][], size: number, w: number, h: number) =>
    tileLayout(
      pts.map(([x, y]) => ({ x, y })),
      size,
      { width: w, height: h },
    ).map((t) => [t.x0, t.y0, t.w, t.h]);

  // Each expected tuple is (x0, y0, w, h) read back from point_tiles crops on a coordinate-encoded image.
  it("centres, clamps and keeps native size", () => {
    expect(
      rects(
        [
          [320, 180],
          [0, 0],
          [639, 359],
          [2.5, 100],
          [3.5, 100],
          [300.5, 200.5],
          [10, 350],
        ],
        64,
        640,
        360,
      ),
    ).toEqual([
      [288, 148, 64, 64],
      [0, 0, 64, 64],
      [576, 296, 64, 64],
      [0, 68, 64, 64],
      [0, 68, 64, 64],
      [268, 168, 64, 64],
      [0, 296, 64, 64],
    ]);
  });

  it("shrinks only to the frame when the frame is smaller than the tile", () => {
    expect(
      rects(
        [
          [320, 180],
          [100.4, 50.6],
        ],
        512,
        640,
        360,
      ),
    ).toEqual([
      [64, 0, 512, 360],
      [0, 0, 512, 360],
    ]);
  });

  it("uses floor(w / 2) for odd sizes and Python's half-to-even rounding", () => {
    expect(
      rects(
        [
          [320, 180],
          [33, 21],
        ],
        65,
        640,
        360,
      ),
    ).toEqual([
      [288, 148, 65, 65],
      [1, 0, 65, 65],
    ]);
    expect(
      rects(
        [
          [1920, 1080],
          [3839, 10],
          [1000.5, 999.5],
        ],
        512,
        3840,
        2160,
      ),
    ).toEqual([
      [1664, 824, 512, 512],
      [3328, 0, 512, 512],
      [744, 744, 512, 512],
    ]);
  });

  it("rejects tiny sizes and skips non-finite points", () => {
    expect(() => tileLayout([{ x: 1, y: 1 }], 1, { width: 10, height: 10 })).toThrow("size must be >= 2");
    expect(tileLayout([{ x: Number.NaN, y: 1 }], 8, { width: 10, height: 10 })).toEqual([]);
  });

  it("rounds half to even like Python", () => {
    expect([0.5, 1.5, 2.5, 3.5, -0.5, -1.5, -2.5, 2.4, 2.6].map(roundHalfEven)).toEqual([
      0, 2, 2, 4, -0, -2, -2, 2, 3,
    ]);
  });
});

describe("tile grid", () => {
  const vp = { width: 1200, height: 700 };

  it("fits every tile pair inside the viewport without overlap", () => {
    const pts = Array.from({ length: 7 }, (_, i) => ({ x: 100 + i * 300, y: 200 + i * 100 }));
    const size = fitTileSize(pts.length, vp, true);
    expect(size).toBeGreaterThanOrEqual(TILE_MIN);
    const cells = tileGrid(tileLayout(pts, size, { width: 3840, height: 2160 }), vp, true);
    expect(cells).toHaveLength(14);
    for (const c of cells) {
      expect(c.x).toBeGreaterThanOrEqual(0);
      expect(c.y).toBeGreaterThanOrEqual(0);
      expect(c.x + c.w).toBeLessThanOrEqual(vp.width);
      expect(c.y + c.h).toBeLessThanOrEqual(vp.height);
    }
    for (let i = 0; i < cells.length; i++) {
      for (let j = i + 1; j < cells.length; j++) {
        const p = cells[i];
        const q = cells[j];
        const overlap = p.x < q.x + q.w && q.x < p.x + p.w && p.y < q.y + q.h && q.y < p.y + p.h;
        expect(overlap, `cells ${i} and ${j} overlap`).toBe(false);
      }
    }
    const [a0, b0] = cells;
    expect([a0.slot, b0.slot]).toEqual(["a", "b"]);
    expect(b0.x).toBeGreaterThan(a0.x);
    expect(b0.y).toBe(a0.y);
    expect(b0.src).toBe(a0.src);
  });

  it("uses the largest tile that still fits and never exceeds 512", () => {
    expect(fitTileSize(1, { width: 4000, height: 4000 }, false)).toBe(512);
    const s = fitTileSize(4, vp, false);
    expect(fitTileSize(4, vp, false, s + 1)).toBe(s);
    expect(tileGrid([], vp, true)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------------------------------------------
describe("view transform", () => {
  it("fits the image and centres it", () => {
    const image = { width: 3840, height: 2160 };
    const viewport = { width: 1280, height: 720 };
    expect(fitZoom(image, viewport)).toBeCloseTo(1 / 3, 12);
    const v = makeView(fitZoom(image, viewport), { x: 1920, y: 1080 }, viewport);
    expect(imageToScreen({ x: 0, y: 0 }, v)).toEqual({ x: 0, y: 0 });
    const br = imageToScreen({ x: 3840, y: 2160 }, v);
    expect(br.x).toBeCloseTo(1280, 9);
    expect(br.y).toBeCloseTo(720, 9);
  });

  it("snaps the translation to whole device pixels at >= 100% and inverts exactly", () => {
    const v = makeView(1, { x: 100.3, y: 50.7 }, { width: 101, height: 61 });
    expect(Number.isInteger(v.tx) && Number.isInteger(v.ty)).toBe(true);
    const p = { x: 12.25, y: 7.5 };
    expect(screenToImage(imageToScreen(p, v), v)).toEqual(p);
    const z = makeView(0.37, { x: 10, y: 10 }, { width: 100, height: 100 });
    expect(Number.isInteger(z.tx)).toBe(false);
  });

  it("clamps zoom to 1/64..64", () => {
    expect(clampZoom(1000)).toBe(64);
    expect(clampZoom(0)).toBe(1 / 64);
    expect(fitZoom({ width: 0, height: 0 }, { width: 10, height: 10 })).toBe(1);
  });
});

// ---------------------------------------------------------------------------------------------------------------
describe("ΔE00 LUT", () => {
  it("is fixed: exact colours at the stops, clamped outside 0..10", () => {
    const hex = (c: [number, number, number]) => `#${c.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
    for (const [v, c] of DE00_STOPS) expect(hex(de00Color(v))).toBe(c);
    expect(de00Color(-3)).toEqual(de00Color(0));
    expect(de00Color(25)).toEqual(de00Color(10));
    expect(de00Color(Number.NaN)).toEqual(de00Color(10));
    expect(de00Color(1.5)).toEqual([42, 131, 192]); // midway between #2b4fb8 and #29b6c8
  });

  it("builds a 256-entry RGBA8 texture whose ends are the first and last stops", () => {
    const lut = de00Lut();
    expect(lut).toHaveLength(DE00_LUT_SIZE * 4);
    expect(Array.from(lut.slice(0, 4))).toEqual([...de00Color(0), 255]);
    expect(Array.from(lut.slice(-4))).toEqual([...de00Color(10), 255]);
  });
});

// ---------------------------------------------------------------------------------------------------------------
describe("mode state machine and keys", () => {
  const ALL = { hasB: true, hasEdges: true, hasHeat: true, hasTiles: true };
  const base = (over: Partial<ViewState> = {}): ViewState => ({
    ...initialViewState({ image: { width: 3840, height: 2160 }, viewport: { width: 1280, height: 720 }, caps: ALL }),
    ...over,
  });
  const press = (s: ViewState, ...keys: string[]) =>
    keys.reduce((acc, key) => {
      const a = keyToAction({ key });
      return a ? viewerReducer(acc, a) : acc;
    }, s);

  it("maps the spec keys and leaves browser shortcuts alone", () => {
    const want: Record<string, ViewAction["type"]> = {
      "`": "flip",
      "[": "wipeStep",
      "]": "wipeStep",
      "1": "zoom100",
      f: "fit",
      d: "toggleMode",
      e: "toggleMode",
      h: "toggleMode",
      t: "toggleMode",
      ArrowLeft: "pan",
      ArrowRight: "pan",
      ArrowUp: "pan",
      ArrowDown: "pan",
    };
    for (const [key, type] of Object.entries(want)) expect(keyToAction({ key })?.type, key).toBe(type);
    expect(keyToAction({ key: "d", metaKey: true })).toBeNull();
    expect(keyToAction({ key: "f", ctrlKey: true })).toBeNull();
    expect(keyToAction({ key: "x" })).toBeNull();
    expect(keyToAction({ key: "h" })).toEqual({ type: "toggleMode", mode: "de00" });
    expect(keyToAction({ key: "ArrowLeft", shiftKey: true })).toEqual({ type: "pan", dx: -256, dy: 0 });
  });

  it("d, e, h and t enter their mode and return to the compare mode when pressed again", () => {
    let s = base();
    expect(displayedMode(s)).toBe("wipe");
    for (const [key, mode] of [
      ["d", "diff"],
      ["e", "edges"],
      ["h", "de00"],
      ["t", "tiles"],
    ] as const) {
      s = press(s, key);
      expect(displayedMode(s)).toBe(mode);
      s = press(s, key);
      expect(displayedMode(s)).toBe("wipe");
    }
    s = press(s, "d", "e");
    expect(displayedMode(s)).toBe("edges"); // one overlay mode replaces another
  });

  it("` flips A/B in toggle mode and makes toggle the mode d returns to", () => {
    let s = press(base(), "`");
    expect(displayedMode(s)).toBe("b");
    s = press(s, "`");
    expect(displayedMode(s)).toBe("a");
    s = press(s, "d", "d");
    expect(displayedMode(s)).toBe("a");
    expect(s.base).toBe("toggle");
  });

  it("[ and ] enter wipe first, then step by 0.05 and clamp to 0..1", () => {
    let s = press(base(), "d", "]");
    expect(displayedMode(s)).toBe("wipe");
    expect(s.wipe).toBe(0.5);
    s = press(s, "]", "]");
    expect(s.wipe).toBe(0.6);
    s = press(s, ...Array(20).fill("]"));
    expect(s.wipe).toBe(1);
    s = press(s, ...Array(30).fill("["));
    expect(s.wipe).toBe(0);
    s = press(s, "}", "}", "}");
    expect(s.wipe).toBe(0.03);
  });

  it("refuses modes whose sources are missing, with a notice, and falls back to what can be drawn", () => {
    const s = base({ caps: NO_CAPS });
    expect(displayedMode(s)).toBe("a");
    for (const key of ["d", "e", "h", "t", "`", "]"]) {
      const next = press(s, key);
      expect(next.mode, key).toBe(s.mode);
      expect(next.notice, key).toMatch(/^no /);
    }
    expect(press(s, "e").notice).toBe("no edge masks for this view");
    const onlyB = base({ caps: { ...NO_CAPS, hasB: true }, mode: "edges" });
    expect(displayedMode(onlyB)).toBe("wipe"); // edges requested by a prop, masks absent
    expect(press(onlyB, "1").notice).toBeNull();
  });

  it("1 is exact 100% around the current centre, f fits, arrows pan in device pixels", () => {
    let s = base();
    expect(scaleOf(s)).toBeCloseTo(1 / 3, 9);
    s = press(s, "1");
    expect(s.zoom).toBe(1);
    expect(s.pan).toEqual({ x: 1920, y: 1080 });
    s = press(s, "ArrowRight", "ArrowDown");
    expect(s.pan).toEqual({ x: 1920 + PAN_STEP, y: 1080 + PAN_STEP });
    s = press(s, "f");
    expect(s.zoom).toBe("fit");
    expect(s.pan).toBeNull();
    s = press(s, "ArrowLeft"); // panning at fit freezes the fit zoom as a number
    expect(s.zoom).toBeCloseTo(1 / 3, 9);
    expect(s.pan?.x).toBeCloseTo(1920 - PAN_STEP * 3, 9);
  });

  it("keeps the pan inside the image", () => {
    const s = press(base({ zoom: 1, pan: { x: 10, y: 10 } }), ...Array(5).fill("ArrowLeft"), "ArrowUp");
    expect(s.pan).toEqual({ x: 0, y: 0 });
  });

  it("+ and - walk powers of two through 100%, the wheel snaps near 100% and zooms about the cursor", () => {
    let s = press(base(), "+");
    expect(s.zoom).toBe(0.5);
    s = press(s, "+");
    expect(s.zoom).toBe(1);
    s = press(s, "+", "-", "-");
    expect(s.zoom).toBe(0.5);
    const near = viewerReducer(base({ zoom: 0.98, pan: { x: 500, y: 500 } }), { type: "zoomBy", factor: 1.01 });
    expect(near.zoom).toBe(1);
    const before = base({ zoom: 1, pan: { x: 1000, y: 800 } });
    const at = { x: 200, y: 150 };
    const p = screenToImage(at, viewOf(before));
    const after = viewerReducer(before, { type: "zoomBy", factor: 4, at });
    const q = screenToImage(at, viewOf(after));
    expect(Math.abs(q.x - p.x)).toBeLessThanOrEqual(0.5); // whole-pixel snap at >= 100%
    expect(Math.abs(q.y - p.y)).toBeLessThanOrEqual(0.5);
  });

  it("sync applies prop changes and clearNotice clears the notice", () => {
    let s = viewerReducer(base(), { type: "sync", patch: { mode: "toggle", wipe: 2, zoom: 4, pan: { x: -5, y: 9 } } });
    expect([s.mode, s.base, s.wipe, s.zoom]).toEqual(["toggle", "toggle", 1, 4]);
    expect(s.pan).toEqual({ x: 0, y: 9 });
    s = viewerReducer({ ...s, notice: "x" }, { type: "clearNotice" });
    expect(s.notice).toBeNull();
    // re-sending the current values (every first effect run) and unchanged caps leave the state object alone
    expect(viewerReducer(s, { type: "sync", patch: { mode: s.mode, wipe: s.wipe, zoom: s.zoom, pan: s.pan } })).toBe(s);
    expect(viewerReducer(s, { type: "caps", caps: { ...s.caps } })).toBe(s);
  });
});

// ---------------------------------------------------------------------------------------------------------------
describe("loaders", () => {
  it("adds fmt=rgba16f unless the URL names a format, keeping any fragment", () => {
    expect(withFormat("/api/v0/assets/ab12", "rgba16f")).toBe("/api/v0/assets/ab12?fmt=rgba16f");
    expect(withFormat("/api/v0/assets/ab12?p=1#x", "rgba16f")).toBe("/api/v0/assets/ab12?p=1&fmt=rgba16f#x");
    expect(withFormat("/api/v0/assets/ab12?fmt=png", "rgba16f")).toBe("/api/v0/assets/ab12?fmt=png");
  });

  it("sends the bearer token to this origin only", () => {
    const origin = "http://127.0.0.1:4610";
    expect(isSameOrigin("/api/v0/assets/x", origin)).toBe(true);
    expect(isSameOrigin("http://127.0.0.1:4610/api/v0/assets/x", origin)).toBe(true);
    expect(isSameOrigin("https://cdn.example.com/x.png", origin)).toBe(false);
    expect(isSameOrigin("blob:http://127.0.0.1:4610/1234", origin)).toBe(false);
  });

  it("infers R16F or RGBA16F from the byte count and rejects anything else", () => {
    expect(halfChannels(2 * 2 * 4 * 2, 2, 2)).toBe(4);
    expect(halfChannels(2 * 2 * 2, 2, 2)).toBe(1);
    expect(() => halfChannels(31, 2, 2)).toThrow(/expected 8 \(R16F\) or 32 \(RGBA16F\) bytes .* for 2x2, got 31/);
    expect(() => halfChannels(8, 2, 2, [4])).toThrow(/expected 32 \(RGBA16F\)/);
  });

  it("reads little-endian binary16", () => {
    const bytes = new Uint8Array([0x00, 0x3c, 0x00, 0xc0]);
    expect(Array.from(decodeHalfBuffer(bytes.buffer))).toEqual([0x3c00, 0xc000]);
    expect(() => decodeHalfBuffer(new ArrayBuffer(3))).toThrow(/odd byte count/);
  });

  it("decodes an inline half-float payload (ArrayBuffer or Blob) without fetching", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const data = halfFloatPack([0.25, 0.5, 0.75, 1, 0, 0, 0, 1]).buffer as ArrayBuffer;
    const exr = (await loadImage({ url: "unused", width: 2, height: 1, encoding: "exr-half", data })) as HalfPixels;
    expect([exr.kind, exr.channels, exr.linear, exr.width, exr.height]).toEqual(["half", 4, true, 2, 1]);
    expect(fromHalfBits(exr.data[1])).toBe(0.5);
    const png = (await loadImage({
      url: "unused",
      width: 2,
      height: 1,
      encoding: "png16",
      data: new Blob([data]),
    })) as HalfPixels;
    expect(png.linear).toBe(false);
    expect(fetchSpy).not.toHaveBeenCalled();
    await expect(loadImage({ url: "u", width: 3, height: 1, encoding: "exr-half", data })).rejects.toThrow(
      /expected 24 \(RGBA16F\) bytes/,
    );
    await expect(loadImage({ url: "u", width: 0, height: 1, encoding: "exr-half", data })).rejects.toThrow(
      /positive integer/,
    );
  });

  it("fetches assets with ?fmt=rgba16f and the bearer token", async () => {
    window.sessionStorage.setItem(TOKEN_KEY, "tok123");
    const body = halfFloatPack([0, 0, 0, 1]).buffer as ArrayBuffer;
    const fetchSpy = vi.fn(async (_url: string, _init?: RequestInit) => new Response(body, { status: 200 }));
    vi.stubGlobal("fetch", fetchSpy);
    await loadImage({ url: "/api/v0/assets/abcd", width: 1, height: 1, encoding: "exr-half" });
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe("/api/v0/assets/abcd?fmt=rgba16f");
    expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer tok123");
    await loadImage({ url: "https://cdn.example.com/x", width: 1, height: 1, encoding: "png16" });
    expect(new Headers(fetchSpy.mock.calls[1][1]?.headers).get("Authorization")).toBeNull();
    fetchSpy.mockResolvedValueOnce(new Response("gone", { status: 404 }));
    await expect(loadImage({ url: "/api/v0/assets/zz", width: 1, height: 1, encoding: "png16" })).rejects.toThrow(
      "-> 404",
    );
  });

  it("loads a heatmap as R16F from its URL as given", async () => {
    const body = halfFloatPack([0, 2.5, 10, 0.5]).buffer as ArrayBuffer;
    const fetchSpy = vi.fn(async () => new Response(body, { status: 200 }));
    vi.stubGlobal("fetch", fetchSpy);
    const heat = await loadHeat({ url: "/api/v0/gates/r1/de00", width: 2, height: 2 });
    expect([heat.channels, heat.linear]).toEqual([1, true]);
    expect(fetchSpy.mock.calls[0]).toEqual(["/api/v0/gates/r1/de00", expect.anything()]);
  });

  it("reports when the browser cannot decode 8-bit images", async () => {
    vi.stubGlobal("createImageBitmap", undefined);
    await expect(
      loadImage({ url: "u", width: 1, height: 1, encoding: "srgb8", data: new Blob([new Uint8Array(4)]) }),
    ).rejects.toThrow(/createImageBitmap/);
  });
});

// ---------------------------------------------------------------------------------------------------------------
// A recording WebGL2 stand-in: constants are stable numbers, every method call is logged.

type Call = [string, ...unknown[]];

function fakeGl(opts: { floatExt: boolean; complete?: boolean }) {
  const calls: Call[] = [];
  const consts = new Map<string, number>();
  const uniforms: string[] = [];
  let next = 0x9000;
  let gl: Record<string, unknown> = {};
  const special: Record<string, (...args: unknown[]) => unknown> = {
    getExtension: (name) => (name === "EXT_color_buffer_float" && opts.floatExt ? {} : null),
    getShaderParameter: () => true,
    getProgramParameter: () => true,
    checkFramebufferStatus: () => (opts.complete === false ? 0 : gl.FRAMEBUFFER_COMPLETE),
    getUniformLocation: (_p, name) => {
      uniforms.push(name as string);
      return { uniform: name };
    },
  };
  gl = new Proxy(
    {},
    {
      get(_t, prop) {
        if (typeof prop !== "string") return undefined;
        if (/^[A-Z][A-Z0-9_]*$/.test(prop)) {
          if (!consts.has(prop)) consts.set(prop, (next += 16));
          return consts.get(prop);
        }
        return (...args: unknown[]) => {
          calls.push([prop, ...args]);
          if (special[prop]) return special[prop](...args);
          if (prop.startsWith("create")) return { kind: prop.slice(6), id: next++ };
          return undefined;
        };
      },
      set: () => true,
      has: () => false,
    },
  ) as Record<string, unknown>;
  const C = (name: string) => gl[name] as number;
  const named = (name: string) => calls.filter((c) => c[0] === name);
  const uniform = (fn: string, name: string) =>
    named(fn)
      .filter((c) => (c[1] as { uniform?: string } | null)?.uniform === name)
      .map((c) => c.slice(2));
  return { gl: gl as unknown as WebGL2RenderingContext, calls, uniforms, C, named, uniform };
}

const halfImage = (width: number, height: number, linear = true): HalfPixels => ({
  kind: "half",
  width,
  height,
  channels: 4,
  data: halfFloatPack(new Array(width * height * 4).fill(0.5)),
  linear,
});

describe("renderer (recording GL)", () => {
  it("requests EXT_color_buffer_float, uploads half floats as RGBA16F / HALF_FLOAT, linearizes into RGBA16F", () => {
    const f = fakeGl({ floatExt: true });
    const r = new ViewerRenderer(f.gl);
    expect(r.caps.floatTargets).toBe(true);
    expect(f.named("getExtension").map((c) => c[1])).toContain("EXT_color_buffer_float");
    const px = halfImage(4, 2);
    r.setImage("a", px);
    const uploads = f.named("texImage2D");
    const src = uploads.find((c) => c[3] === f.C("RGBA16F") && c[9] === px.data);
    const { C } = f;
    expect(src?.slice(1)).toEqual([C("TEXTURE_2D"), 0, C("RGBA16F"), 4, 2, 0, C("RGBA"), C("HALF_FLOAT"), px.data]);
    const target = uploads.find((c) => c[3] === f.C("RGBA16F") && c[4] === 4 && c[9] === null);
    expect(target).toBeDefined();
    expect(f.uniform("uniform1i", "u_decode").at(-1)).toEqual([0]); // exr-half is already linear
    expect(f.named("generateMipmap")).not.toHaveLength(0);
    r.setImage("b", halfImage(4, 2, false));
    expect(f.uniform("uniform1i", "u_decode").at(-1)).toEqual([1]); // png16 code values get the EOTF
  });

  it("samples NEAREST at >= 100% and trilinear below", () => {
    const f = fakeGl({ floatExt: true });
    const r = new ViewerRenderer(f.gl);
    r.setImage("a", halfImage(8, 8));
    const filters = () =>
      f
        .named("texParameteri")
        .filter((c) => c[2] === f.C("TEXTURE_MAG_FILTER") || c[2] === f.C("TEXTURE_MIN_FILTER"))
        .map((c) => [c[2], c[3]]);
    const target = { width: 16, height: 16 };
    const n0 = filters().length;
    r.draw({ target, mode: "a", view: { scale: 2, tx: 0, ty: 0 }, wipeX: 0, gain: 1 });
    expect(filters().slice(n0)).toEqual([
      [f.C("TEXTURE_MIN_FILTER"), f.C("NEAREST")],
      [f.C("TEXTURE_MAG_FILTER"), f.C("NEAREST")],
    ]);
    const n1 = filters().length;
    r.draw({ target, mode: "a", view: { scale: 0.5, tx: 0, ty: 0 }, wipeX: 0, gain: 1 });
    expect(filters().slice(n1)).toEqual([
      [f.C("TEXTURE_MIN_FILTER"), f.C("LINEAR_MIPMAP_LINEAR")],
      [f.C("TEXTURE_MAG_FILTER"), f.C("LINEAR")],
    ]);
    expect(f.uniform("uniform1i", "u_nearest").at(-1)).toEqual([0]);
  });

  it("falls back to RGBA8 without EXT_color_buffer_float, and reads back nothing", () => {
    const f = fakeGl({ floatExt: false });
    const r = new ViewerRenderer(f.gl);
    expect(r.caps.floatTargets).toBe(false);
    r.setImage("a", halfImage(2, 2));
    const uploads = f.named("texImage2D");
    expect(uploads.some((c) => c[3] === f.C("RGBA16F"))).toBe(false);
    const a = uploads.at(-1);
    expect(a?.slice(3, 9)).toEqual([f.C("RGBA8"), 2, 2, 0, f.C("RGBA"), f.C("UNSIGNED_BYTE")]);
    expect(a?.[9]).toBeInstanceOf(Uint8Array);
    r.draw({ target: { width: 4, height: 4 }, mode: "a", view: { scale: 1, tx: 0, ty: 0 }, wipeX: 0, gain: 1 });
    expect(f.uniform("uniform1i", "u_decode").at(-1)).toEqual([1]);
    expect(r.readAnalysis("a")).toBeNull();
  });

  it("treats an incomplete RGBA16F framebuffer like a missing extension", () => {
    const f = fakeGl({ floatExt: true, complete: false });
    expect(new ViewerRenderer(f.gl).caps.floatTargets).toBe(false);
  });

  it("only asks for uniforms the shaders declare", () => {
    const f = fakeGl({ floatExt: true });
    const r = new ViewerRenderer(f.gl);
    r.setImage("a", halfImage(4, 4));
    r.setImage("b", halfImage(4, 4));
    r.setHeat({ ...halfImage(4, 4), channels: 1, data: halfFloatPack(new Array(16).fill(3)) });
    for (const mode of ["a", "b", "wipe", "diff", "edges", "de00"] as const) {
      r.draw({ target: { width: 8, height: 8 }, mode, view: { scale: 1, tx: 0, ty: 0 }, wipeX: 4, gain: 4 });
    }
    r.readAnalysis("diff", 8);
    const declared = (src: string) => new Set([...src.matchAll(/uniform\s+\w+\s+(\w+)\s*;/g)].map((m) => m[1]));
    const all = new Set([...declared(VIEW_FRAG), ...declared(LINEARIZE_FRAG)]);
    expect(f.uniforms.length).toBeGreaterThan(0);
    for (const name of f.uniforms) expect(all.has(name), name).toBe(true);
    for (const name of VIEW_UNIFORMS) expect(declared(VIEW_FRAG).has(name), name).toBe(true);
    for (const name of LINEARIZE_UNIFORMS) expect(declared(LINEARIZE_FRAG).has(name), name).toBe(true);
  });

  it("uploads the ΔE00 map as R16F (one channel) and the LUT as a 256x1 RGBA8 texture", () => {
    const f = fakeGl({ floatExt: true });
    const r = new ViewerRenderer(f.gl);
    const data = halfFloatPack([0, 1, 2, 10]);
    r.setHeat({ kind: "half", width: 2, height: 2, channels: 1, data, linear: true });
    const heat = f.named("texImage2D").find((c) => c[9] === data);
    expect(heat?.slice(3, 9)).toEqual([f.C("R16F"), 2, 2, 0, f.C("RED"), f.C("HALF_FLOAT")]);
    const lut = f.named("texImage2D").find((c) => c[4] === DE00_LUT_SIZE && c[5] === 1);
    expect(lut?.[3]).toBe(f.C("RGBA8"));
  });

  it("draws tiles as separate scissored quads at 100%, B beside A", () => {
    const f = fakeGl({ floatExt: true });
    const r = new ViewerRenderer(f.gl);
    r.setImage("a", halfImage(640, 360));
    r.setImage("b", halfImage(640, 360));
    const viewport = { width: 800, height: 400 };
    const cells = tileGrid(
      tileLayout(
        [
          { x: 320, y: 180 },
          { x: 10, y: 10 },
        ],
        64,
        { width: 640, height: 360 },
      ),
      viewport,
      true,
    );
    const before = f.named("drawArrays").length;
    r.draw({ target: viewport, mode: "tiles", view: { scale: 0.3, tx: 0, ty: 0 }, wipeX: 0, gain: 1, tiles: cells });
    expect(f.named("drawArrays").length - before).toBe(4);
    const scissors = f.named("scissor").map((c) => c.slice(1));
    expect(scissors).toEqual(cells.map((c) => [c.x, viewport.height - c.y - c.h, c.w, c.h]));
    const translates = f.uniform("uniform2f", "u_translate").slice(-4);
    expect(translates).toEqual(cells.map((c) => [c.x - c.src.x0, c.y - c.src.y0]));
    const { A, B } = VIEW_MODE;
    expect(f.uniform("uniform1i", "u_mode").slice(-4)).toEqual([[A], [B], [A], [B]]);
    expect(f.uniform("uniform1f", "u_scale").at(-1)).toEqual([1]);
  });

  it("draws A alone while B is still loading, and reads back linear floats at image size", () => {
    const f = fakeGl({ floatExt: true });
    const r = new ViewerRenderer(f.gl);
    r.setImage("a", halfImage(3, 2));
    r.draw({ target: { width: 8, height: 8 }, mode: "diff", view: { scale: 1, tx: 0, ty: 0 }, wipeX: 0, gain: 4 });
    expect(f.uniform("uniform1i", "u_mode").at(-1)).toEqual([VIEW_MODE.A]);
    expect(r.readAnalysis("diff")).toBeNull();
    r.setImage("b", halfImage(3, 2));
    const out = r.readAnalysis("diff", 8);
    expect(out).toBeInstanceOf(Float32Array);
    expect(out).toHaveLength(3 * 2 * 4);
    const read = f.named("readPixels").at(-1);
    expect(read?.slice(1, 7)).toEqual([0, 0, 3, 2, f.C("RGBA"), f.C("FLOAT")]);
    expect(f.uniform("uniform1i", "u_linearOut").at(-1)).toEqual([1]);
    expect(f.uniform("uniform1i", "u_flipY").at(-1)).toEqual([0]);
    r.dispose();
  });
});

describe("shader sources", () => {
  it("are GLSL ES 3.00 in ASCII and carry the TS mode numbers", () => {
    for (const src of [VERT, LINEARIZE_FRAG, VIEW_FRAG]) {
      expect(src.startsWith("#version 300 es\n")).toBe(true);
      expect(/^[\x09\x0a\x0d\x20-\x7e]*$/.test(src)).toBe(true);
    }
    for (const [name, id] of Object.entries(VIEW_MODE)) expect(VIEW_FRAG).toContain(`const int MODE_${name} = ${id};`);
    expect(VIEW_FRAG).toContain(`const float LUT_SIZE = ${DE00_LUT_SIZE}.0;`);
    expect(VIEW_FRAG).toContain("vec3(pe, ce, ce)"); // red plate, cyan candidate, white both
  });
});

// ---------------------------------------------------------------------------------------------------------------
describe("<Viewer>", () => {
  const exr = (w = 4, h = 2) => ({
    url: "/api/v0/assets/a",
    width: w,
    height: h,
    encoding: "exr-half" as const,
    data: halfFloatPack(new Array(w * h * 4).fill(0.5)).buffer as ArrayBuffer,
  });

  afterEach(() => cleanup());

  it("shows an empty frame with no image (the App slot)", () => {
    render(createElement(Viewer));
    const root = screen.getByTestId("viewer");
    expect(root).toHaveAttribute("data-mode", "empty");
    expect(screen.getByTestId("viewer-message")).toHaveTextContent("No image selected");
    expect(screen.queryByTestId("viewer-canvas")).toBeNull();
  });

  it("says so when there is no WebGL2, and still runs the keyboard state machine", () => {
    const onChange = vi.fn();
    render(createElement(Viewer, { a: exr(), b: { ...exr(), url: "/api/v0/assets/b" }, onChange }));
    const root = screen.getByTestId("viewer");
    expect(screen.getByTestId("viewer-message")).toHaveTextContent("WebGL2 unavailable");
    expect(root).toHaveAttribute("data-gpu", "unavailable");
    expect(root).toHaveAttribute("data-mode", "wipe");
    fireEvent.keyDown(root, { key: "d" });
    expect(root).toHaveAttribute("data-mode", "diff");
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ mode: "diff" }));
    fireEvent.keyDown(root, { key: "d" });
    expect(root).toHaveAttribute("data-mode", "wipe");
    fireEvent.keyDown(root, { key: "`" });
    expect(root).toHaveAttribute("data-mode", "b");
    fireEvent.keyDown(root, { key: "e" });
    expect(screen.getByTestId("viewer-notice")).toHaveTextContent("no edge masks for this view");
    fireEvent.keyDown(root, { key: "1" });
    expect(screen.getByTestId("viewer-zoom")).toHaveTextContent("100%");
    fireEvent.keyDown(root, { key: "?" });
    expect(screen.getByTestId("viewer-keys")).toHaveTextContent("ΔE00 heatmap");
  });

  it("follows the mode prop and shows the size-mismatch badge", () => {
    const { rerender } = render(createElement(Viewer, { a: exr(4, 2), b: exr(2, 2), mode: "toggle" }));
    const root = screen.getByTestId("viewer");
    expect(root).toHaveAttribute("data-mode", "a");
    expect(screen.getByTestId("badge-size")).toHaveTextContent("B 2×2 shown on A 4×2");
    rerender(createElement(Viewer, { a: exr(4, 2), b: exr(2, 2), mode: "diff" }));
    expect(root).toHaveAttribute("data-mode", "diff");
  });

  it("exposes a handle on the ref and on the element for the browser smoke", () => {
    let handle: ViewerHandle | null = null;
    render(createElement(Viewer, { a: exr(), ref: (h: ViewerHandle | null) => void (handle = h) }));
    const el = screen.getByTestId("viewer") as HTMLElement & { plateViewer?: ViewerHandle };
    expect(el.plateViewer).toBeDefined();
    expect(handle).toBe(el.plateViewer);
    expect(el.plateViewer?.gpu()).toBe("unavailable");
    expect(el.plateViewer?.ready()).toBe(false);
    expect(el.plateViewer?.readAnalysis("a")).toBeNull();
    expect(el.plateViewer?.state().image).toEqual({ width: 4, height: 2 });
  });

  describe("with a (recording) WebGL2 context", () => {
    const withGl = (floatExt: boolean) => {
      const f = fakeGl({ floatExt });
      vi.stubGlobal("WebGL2RenderingContext", class {});
      vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(((kind: string) =>
        kind === "webgl2" ? f.gl : null) as unknown as typeof HTMLCanvasElement.prototype.getContext);
      return f;
    };

    it("uploads A as RGBA16F and reports it ready", async () => {
      const f = withGl(true);
      render(createElement(Viewer, { a: exr() }));
      const root = screen.getByTestId("viewer");
      await waitFor(() => expect(root).toHaveAttribute("data-ready", "a"));
      expect(root).toHaveAttribute("data-gpu", "float");
      expect(screen.queryByTestId("badge-8bit")).toBeNull();
      expect(f.named("texImage2D").some((c) => c[3] === f.C("RGBA16F") && c[8] === f.C("HALF_FLOAT"))).toBe(true);
      expect((root as HTMLElement & { plateViewer?: ViewerHandle }).plateViewer?.ready("a")).toBe(true);
    });

    it("shows the 8-bit preview badge when float targets are missing", async () => {
      withGl(false);
      render(createElement(Viewer, { a: exr() }));
      const root = screen.getByTestId("viewer");
      await waitFor(() => expect(root).toHaveAttribute("data-ready", "a"));
      expect(root).toHaveAttribute("data-gpu", "8-bit");
      expect(screen.getByTestId("badge-8bit")).toHaveTextContent("8-bit preview");
    });

    it("shows a load error in place instead of a blank frame", async () => {
      withGl(true);
      const bad = { ...exr(), data: new ArrayBuffer(6) };
      render(createElement(Viewer, { a: bad }));
      await waitFor(() => expect(screen.getByTestId("viewer-error")).toHaveTextContent(/a: expected 64 \(RGBA16F\)/));
    });

    it("draws through requestAnimationFrame after a key press", async () => {
      const f = withGl(true);
      const frames: FrameRequestCallback[] = [];
      vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => frames.push(cb));
      vi.stubGlobal("cancelAnimationFrame", () => {});
      render(createElement(Viewer, { a: exr(), b: exr() }));
      const root = screen.getByTestId("viewer");
      await waitFor(() => expect(root.getAttribute("data-ready")).toBe("a b"));
      fireEvent.keyDown(root, { key: "d" });
      act(() => frames.at(-1)?.(0));
      expect(f.uniform("uniform1i", "u_mode").at(-1)).toEqual([VIEW_MODE.DIFF]);
      expect(f.uniform("uniform1f", "u_gain").at(-1)).toEqual([4]);
    });
  });
});
