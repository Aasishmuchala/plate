// Pure helpers behind the viewer, kept out of the GL code so vitest can check them without a GPU: the sRGB transfer
// functions (same constants as gates/plate_gates/color.py), IEEE half-float packing for the RGBA16F transport, the
// 100% crop-tile layout (parity with gates/plate_gates/crops.point_tiles), the view transform and the fixed ΔE00 LUT.

import type { Point, Size, Zoom } from "./types";

// ---------------------------------------------------------------------------------------------------------------
// sRGB (IEC 61966-2-1). The shaders use the same piecewise curve; values outside 0..1 follow the linear segment.

export function srgbToLinear(v: number): number {
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

export function linearToSrgb(v: number): number {
  return v <= 0.0031308 ? v * 12.92 : 1.055 * v ** (1 / 2.4) - 0.055;
}

// ---------------------------------------------------------------------------------------------------------------
// Half floats (binary16). Round-to-nearest-even, subnormals kept, overflow to infinity, NaN stays NaN.

const f32 = new Float32Array(1);
const u32 = new Uint32Array(f32.buffer);

export function toHalfBits(v: number): number {
  f32[0] = v;
  const x = u32[0];
  const sign = (x >>> 16) & 0x8000;
  const exp = (x >>> 23) & 0xff;
  let mant = x & 0x7fffff;
  if (exp === 0xff) return sign | 0x7c00 | (mant ? 0x200 : 0);
  const e = exp - 112; // rebias 127 -> 15
  if (e >= 0x1f) return sign | 0x7c00;
  if (e <= 0) {
    if (e < -10) return sign;
    mant |= 0x800000;
    const shift = 14 - e;
    let half = mant >>> shift;
    const rem = mant & ((1 << shift) - 1);
    const halfway = 1 << (shift - 1);
    if (rem > halfway || (rem === halfway && half & 1)) half++;
    return sign | half;
  }
  let half = (e << 10) | (mant >>> 13);
  const rem = mant & 0x1fff;
  if (rem > 0x1000 || (rem === 0x1000 && half & 1)) half++; // a carry into the exponent is the correct result
  return sign | half;
}

export function fromHalfBits(h: number): number {
  const sign = h & 0x8000 ? -1 : 1;
  const e = (h >>> 10) & 0x1f;
  const m = h & 0x3ff;
  if (e === 0) return sign * m * 2 ** -24;
  if (e === 0x1f) return m ? Number.NaN : sign * Number.POSITIVE_INFINITY;
  return sign * (1 + m / 1024) * 2 ** (e - 15);
}

/** Float values -> half-float bit patterns (the RGBA16F upload / transport layout). */
export function halfFloatPack(values: ArrayLike<number>): Uint16Array {
  const out = new Uint16Array(values.length);
  for (let i = 0; i < values.length; i++) out[i] = toHalfBits(values[i]);
  return out;
}

export function halfFloatUnpack(bits: ArrayLike<number>): Float32Array {
  const out = new Float32Array(bits.length);
  for (let i = 0; i < bits.length; i++) out[i] = fromHalfBits(bits[i]);
  return out;
}

/** The value an RGBA16F texel stores for `v`. */
export const roundHalf = (v: number): number => fromHalfBits(toHalfBits(v));

/**
 * CPU reference for the GPU diff path (parity check): what `readAnalysis("diff")` returns for two half-float RGBA
 * buffers. Each input is decoded (sRGB EOTF unless `linear`), stored as half (the linear RGBA16F target), then
 * |a - b| * gain is stored as half again (the RGBA16F read-back target). Alpha is 1.
 */
export function referenceDiff(a: Uint16Array, b: Uint16Array, gain = 1, linear = false): Float32Array {
  if (a.length !== b.length || a.length % 4 !== 0) throw new Error("referenceDiff needs two RGBA buffers of one size");
  const out = new Float32Array(a.length);
  const lin = (h: number) => roundHalf(linear ? fromHalfBits(h) : srgbToLinear(fromHalfBits(h)));
  for (let i = 0; i < a.length; i += 4) {
    for (let c = 0; c < 3; c++) out[i + c] = roundHalf(Math.abs(lin(a[i + c]) - lin(b[i + c])) * gain);
    out[i + 3] = 1;
  }
  return out;
}

/** Half-float RGBA -> 8-bit sRGB-encoded RGBA, for the RGBA8 fallback when float render targets are missing. */
export function halfToSrgb8(data: Uint16Array, linear: boolean): Uint8Array {
  const out = new Uint8Array(data.length);
  const q = (v: number) => Math.min(255, Math.max(0, Math.round(v * 255)));
  for (let i = 0; i < data.length; i += 4) {
    for (let c = 0; c < 3; c++) {
      const v = fromHalfBits(data[i + c]);
      out[i + c] = q(linear ? linearToSrgb(Math.max(v, 0)) : v);
    }
    out[i + 3] = q(fromHalfBits(data[i + 3]));
  }
  return out;
}

// ---------------------------------------------------------------------------------------------------------------
// 100% crop tiles.

/** Python's round(): half to even. */
export function roundHalfEven(v: number): number {
  const r = Math.round(v);
  return Math.abs(v % 1) === 0.5 && r % 2 !== 0 ? r - 1 : r;
}

export interface TileRect {
  point: Point;
  /** Source rect in image pixels. */
  x0: number;
  y0: number;
  w: number;
  h: number;
}

/**
 * One size x size source rect (native pixels) centred on each point and clamped inside the image, smaller only when
 * the image itself is smaller. Same arithmetic as gates/plate_gates/crops.point_tiles, so the viewer's tiles are the
 * gate report's tiles. Non-finite points are skipped.
 */
export function tileLayout(points: readonly Point[], size: number, bounds: Size): TileRect[] {
  if (size < 2) throw new Error("size must be >= 2");
  const w = Math.min(Math.floor(size), bounds.width);
  const h = Math.min(Math.floor(size), bounds.height);
  const out: TileRect[] = [];
  for (const p of points) {
    if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) continue;
    const x0 = Math.min(Math.max(roundHalfEven(p.x) - Math.floor(w / 2), 0), bounds.width - w);
    const y0 = Math.min(Math.max(roundHalfEven(p.y) - Math.floor(h / 2), 0), bounds.height - h);
    out.push({ point: p, x0, y0, w, h });
  }
  return out;
}

export const TILE_GAP = 6; // device pixels between tiles, as in the gate crop sheets
export const TILE_PAD = 8;
export const TILE_MAX = 512; // point_tiles default
export const TILE_MIN = 32;

interface GridShape {
  cols: number;
  groupW: number;
}

function gridShape(size: number, viewportW: number, pair: boolean): GridShape {
  const groupW = pair ? 2 * size + TILE_GAP : size;
  const cols = Math.max(1, Math.floor((viewportW - 2 * TILE_PAD + TILE_GAP) / (groupW + TILE_GAP)));
  return { cols, groupW };
}

/** Largest tile size (device = image pixels) in [TILE_MIN, max] that fits every point's tile (or A|B pair). */
export function fitTileSize(count: number, viewport: Size, pair: boolean, max = TILE_MAX): number {
  if (count <= 0) return Math.max(TILE_MIN, Math.min(max, TILE_MAX));
  for (let s = Math.max(TILE_MIN, Math.floor(max)); s > TILE_MIN; s--) {
    const { cols, groupW } = gridShape(s, viewport.width, pair);
    const rows = Math.ceil(count / cols);
    const needW = Math.min(count, cols) * (groupW + TILE_GAP) - TILE_GAP + 2 * TILE_PAD;
    const needH = rows * (s + TILE_GAP) - TILE_GAP + 2 * TILE_PAD;
    if (needW <= viewport.width && needH <= viewport.height) return s;
  }
  return TILE_MIN;
}

export interface TileCell {
  /** Index into the tileLayout result. */
  index: number;
  slot: "a" | "b";
  src: TileRect;
  /** Destination rect in device pixels, top-left origin. */
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Place the tiles in rows: each point gets its A tile, and the B tile right of it when `pair`. */
export function tileGrid(tiles: readonly TileRect[], viewport: Size, pair: boolean): TileCell[] {
  if (tiles.length === 0) return [];
  const size = Math.max(tiles[0].w, tiles[0].h);
  const { cols, groupW } = gridShape(size, viewport.width, pair);
  const cells: TileCell[] = [];
  tiles.forEach((t, index) => {
    const x = TILE_PAD + (index % cols) * (groupW + TILE_GAP);
    const y = TILE_PAD + Math.floor(index / cols) * (size + TILE_GAP);
    cells.push({ index, slot: "a", src: t, x, y, w: t.w, h: t.h });
    if (pair) cells.push({ index, slot: "b", src: t, x: x + size + TILE_GAP, y, w: t.w, h: t.h });
  });
  return cells;
}

// ---------------------------------------------------------------------------------------------------------------
// View transform.

export const ZOOM_MIN = 1 / 64;
export const ZOOM_MAX = 64;

export const clampZoom = (z: number): number => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z));

export function fitZoom(image: Size, viewport: Size): number {
  if (image.width <= 0 || image.height <= 0 || viewport.width <= 0 || viewport.height <= 0) return 1;
  return clampZoom(Math.min(viewport.width / image.width, viewport.height / image.height));
}

export function scaleFor(zoom: Zoom, image: Size, viewport: Size): number {
  return zoom === "fit" ? fitZoom(image, viewport) : clampZoom(zoom);
}

export interface View {
  /** Device pixels per image pixel. */
  scale: number;
  /** Device position of image pixel (0, 0). Whole pixels at >= 100% so texels land on device pixels. */
  tx: number;
  ty: number;
}

export function makeView(scale: number, centre: Point, viewport: Size): View {
  let tx = viewport.width / 2 - centre.x * scale;
  let ty = viewport.height / 2 - centre.y * scale;
  if (scale >= 1) {
    tx = Math.round(tx);
    ty = Math.round(ty);
  }
  return { scale, tx, ty };
}

export const imageToScreen = (p: Point, v: View): Point => ({ x: p.x * v.scale + v.tx, y: p.y * v.scale + v.ty });
export const screenToImage = (s: Point, v: View): Point => ({ x: (s.x - v.tx) / v.scale, y: (s.y - v.ty) / v.scale });

// ---------------------------------------------------------------------------------------------------------------
// ΔE00 heatmap LUT. Fixed, 0..10, stops on the gate thresholds (thresholds.yaml): 1 = feather p99 / RAL limit,
// 2 = per-material median limit, 5 = per-material p90 limit. Linear between stops in sRGB 8-bit, display-referred.

export const DE00_MAX = 10;
export const DE00_LUT_SIZE = 256;

export const DE00_STOPS: ReadonlyArray<readonly [number, string]> = [
  [0, "#0b0d12"],
  [1, "#2b4fb8"],
  [2, "#29b6c8"],
  [3.5, "#f2d43d"],
  [5, "#f08a24"],
  [10, "#e0245e"],
];

const hexRgb = (hex: string): [number, number, number] => [
  Number.parseInt(hex.slice(1, 3), 16),
  Number.parseInt(hex.slice(3, 5), 16),
  Number.parseInt(hex.slice(5, 7), 16),
];

/** sRGB 8-bit colour for one ΔE00 value; below 0 clamps to the first stop, above 10 (or NaN) to the last. */
export function de00Color(de: number): [number, number, number] {
  const stops = DE00_STOPS;
  if (Number.isNaN(de) || de >= stops[stops.length - 1][0]) return hexRgb(stops[stops.length - 1][1]);
  if (de <= stops[0][0]) return hexRgb(stops[0][1]);
  for (let i = 1; i < stops.length; i++) {
    const [v1, c1] = stops[i];
    if (de <= v1) {
      const [v0, c0] = stops[i - 1];
      const t = (de - v0) / (v1 - v0);
      const a = hexRgb(c0);
      const b = hexRgb(c1);
      return [0, 1, 2].map((k) => Math.round(a[k] + (b[k] - a[k]) * t)) as [number, number, number];
    }
  }
  return hexRgb(stops[stops.length - 1][1]);
}

/** The LUT texture: DE00_LUT_SIZE RGBA8 entries, entry i = de00Color(i / (size - 1) * DE00_MAX). */
export function de00Lut(size = DE00_LUT_SIZE): Uint8Array {
  const out = new Uint8Array(size * 4);
  for (let i = 0; i < size; i++) {
    const [r, g, b] = de00Color((i / (size - 1)) * DE00_MAX);
    out.set([r, g, b, 255], i * 4);
  }
  return out;
}

/** Overlay colours, as in gates/plate_gates/align.edge_agreement: red plate, cyan candidate, white both. */
export const EDGE_COLORS = { plate: "#ff0000", candidate: "#00ffff", both: "#ffffff" } as const;
