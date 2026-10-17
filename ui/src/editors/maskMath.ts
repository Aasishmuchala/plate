// Pure pieces of the Mask editor: the ID classes with their colours and lock state, pointer-to-pixel mapping, the
// paint raster (brush add / erase, filled rectangles and even-odd polygons on an 8-bit layer at a working size) and
// the PNG it is exported as. No DOM here, so the vitest drives all of it directly.

import type { ImageSize } from "../nodeTypes";

// ---------------------------------------------------------------------------------------------------- classes

/** The legend classes (PACKET.md section 4.3). */
export const ID_CLASSES = [
  "architecture",
  "glazing",
  "signage",
  "ground",
  "vegetation",
  "sky",
  "people",
  "water",
  "vehicle",
  "furniture",
  "other",
] as const;
export type IdClass = (typeof ID_CLASSES)[number];

export const isIdClass = (v: unknown): v is IdClass => typeof v === "string" && (ID_CLASSES as readonly string[]).includes(v);

/** Never editable by policy (PACKET.md section 7): picking one of these cannot open it up. */
export const NEVER_EDITABLE: ReadonlySet<IdClass> = new Set<IdClass>(["architecture", "glazing", "signage", "other"]);

export const CLASS_COLOURS: Readonly<Record<IdClass, string>> = {
  architecture: "#8e8e93",
  glazing: "#64d2ff",
  signage: "#ff9f0a",
  ground: "#a2845e",
  vegetation: "#30d158",
  sky: "#0a84ff",
  people: "#ff6482",
  water: "#5ac8fa",
  vehicle: "#bf5af2",
  furniture: "#ffd60a",
  other: "#636366",
};

/** A picked id as the chips and `params.idInfo` carry it. */
export interface PickedId {
  id: string;
  name: string;
  class: IdClass;
}

// ---------------------------------------------------------------------------------------------------- pointer

export interface Point {
  x: number;
  y: number;
}

export interface RectLike {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** Client pixel -> image pixel of an image shown filling `rect`; null for an empty rect or an unknown size. */
export function clientToImage(rect: RectLike, size: ImageSize | null, clientX: number, clientY: number): Point | null {
  if (!size || rect.width <= 0 || rect.height <= 0 || size.width <= 0 || size.height <= 0) return null;
  const fx = (clientX - rect.left) / rect.width;
  const fy = (clientY - rect.top) / rect.height;
  return {
    x: Math.min(size.width - 1, Math.max(0, Math.floor(fx * size.width))),
    y: Math.min(size.height - 1, Math.max(0, Math.floor(fy * size.height))),
  };
}

/** Image pixel -> CSS percentages of the stage, for a marker. */
export function pointPercent(p: Point, size: ImageSize): { left: string; top: string } {
  return { left: `${Number(((100 * (p.x + 0.5)) / size.width).toFixed(4))}%`, top: `${Number(((100 * (p.y + 0.5)) / size.height).toFixed(4))}%` };
}

// ---------------------------------------------------------------------------------------------------- raster

/** An 8-bit mask: 255 = editable, 0 = locked. `data` is row-major, `width * height` bytes. */
export interface MaskLayer {
  width: number;
  height: number;
  data: Uint8Array<ArrayBuffer>;
}

/**
 * Longest side of the painting raster: painting at plate size (5504 x 3072) would be 17 MB per layer. 2048 keeps an
 * edge within about 3 plate pixels on a 5.5K plate; the daemon scales the saved mask to the packet's plate size
 * (nearest neighbour, then 0/255) when the upload names the packet (T82).
 */
export const MAX_RASTER_SIDE = 2048;

/** The raster size for a plate: aspect kept, longest side at most `maxSide`, never upscaled. */
export function workSize(size: ImageSize, maxSide: number = MAX_RASTER_SIDE): ImageSize {
  const k = Math.min(1, maxSide / Math.max(size.width, size.height));
  return { width: Math.max(1, Math.round(size.width * k)), height: Math.max(1, Math.round(size.height * k)) };
}

export function newMask(width: number, height: number): MaskLayer {
  return { width, height, data: new Uint8Array(width * height) };
}

/** Plate pixel -> raster pixel (floats). */
export function toRaster(layer: MaskLayer, plate: ImageSize, p: Point): Point {
  return { x: (p.x + 0.5) * (layer.width / plate.width), y: (p.y + 0.5) * (layer.height / plate.height) };
}

/** Fill a disc of radius `r` (raster pixels) at (cx, cy) with `value`. Returns the number of pixels written. */
export function stampDisc(layer: MaskLayer, cx: number, cy: number, r: number, value: 0 | 255): number {
  const rr = Math.max(0.5, r);
  const y0 = Math.max(0, Math.floor(cy - rr));
  const y1 = Math.min(layer.height - 1, Math.ceil(cy + rr));
  let n = 0;
  for (let y = y0; y <= y1; y += 1) {
    const dy = y + 0.5 - cy;
    const span = rr * rr - dy * dy;
    if (span < 0) continue;
    const half = Math.sqrt(span);
    const xa = Math.max(0, Math.ceil(cx - half - 0.5));
    const xb = Math.min(layer.width - 1, Math.floor(cx + half - 0.5));
    for (let x = xa; x <= xb; x += 1) {
      layer.data[y * layer.width + x] = value;
      n += 1;
    }
  }
  return n;
}

/** A stroke segment: discs every half radius from (x0, y0) to (x1, y1), so a fast drag leaves no gaps. */
export function strokeLine(layer: MaskLayer, x0: number, y0: number, x1: number, y1: number, r: number, value: 0 | 255): void {
  const dist = Math.hypot(x1 - x0, y1 - y0);
  const step = Math.max(0.5, r / 2);
  const n = Math.max(1, Math.ceil(dist / step));
  for (let i = 0; i <= n; i += 1) {
    const t = i / n;
    stampDisc(layer, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, r, value);
  }
}

/**
 * Fill the box between raster edges (x0, y0) and (x1, y1) (either corner order) with `value`: every pixel whose centre
 * lies inside it. Returns the number of pixels written.
 */
export function fillRect(layer: MaskLayer, x0: number, y0: number, x1: number, y1: number, value: 0 | 255): number {
  const xa = Math.max(0, Math.ceil(Math.min(x0, x1) - 0.5));
  const xb = Math.min(layer.width - 1, Math.ceil(Math.max(x0, x1) - 0.5) - 1);
  const ya = Math.max(0, Math.ceil(Math.min(y0, y1) - 0.5));
  const yb = Math.min(layer.height - 1, Math.ceil(Math.max(y0, y1) - 0.5) - 1);
  if (xb < xa || yb < ya) return 0;
  for (let y = ya; y <= yb; y += 1) layer.data.fill(value, y * layer.width + xa, y * layer.width + xb + 1);
  return (xb - xa + 1) * (yb - ya + 1);
}

/**
 * The raster box of a drag between two plate pixels: both end pixels are inside it (the box runs from the smaller
 * pixel's left/top edge to the larger one's right/bottom edge), mapped to the layer's working size.
 */
export function plateBoxToRaster(layer: MaskLayer, plate: ImageSize, a: Point, b: Point): { x0: number; y0: number; x1: number; y1: number } {
  const sx = layer.width / plate.width;
  const sy = layer.height / plate.height;
  return {
    x0: Math.min(a.x, b.x) * sx,
    y0: Math.min(a.y, b.y) * sy,
    x1: (Math.max(a.x, b.x) + 1) * sx,
    y1: (Math.max(a.y, b.y) + 1) * sy,
  };
}

/** Consecutive vertices closer than `eps` merged (a double-click lands two clicks on one corner), and the closing one. */
export function dedupeVertices(points: readonly Point[], eps = 0.5): Point[] {
  const out: Point[] = [];
  for (const p of points) {
    const prev = out[out.length - 1];
    if (!prev || Math.hypot(p.x - prev.x, p.y - prev.y) >= eps) out.push(p);
  }
  while (out.length > 1) {
    const first = out[0] as Point;
    const lastP = out[out.length - 1] as Point;
    if (Math.hypot(first.x - lastP.x, first.y - lastP.y) >= eps) break;
    out.pop();
  }
  return out;
}

/**
 * Fill a closed polygon (raster coordinates, implicitly closed) with `value` by even-odd scanlines through pixel
 * centres: a self-crossing outline leaves its doubly covered parts open, as a vector editor's even-odd rule does.
 * Fewer than three vertices fill nothing. Returns the number of pixels written.
 */
export function fillPolygon(layer: MaskLayer, points: readonly Point[], value: 0 | 255): number {
  const n = points.length;
  if (n < 3) return 0;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const p of points) {
    minY = Math.min(minY, p.y);
    maxY = Math.max(maxY, p.y);
  }
  const y0 = Math.max(0, Math.ceil(minY - 0.5));
  const y1 = Math.min(layer.height - 1, Math.floor(maxY - 0.5));
  const xs: number[] = [];
  let written = 0;
  for (let y = y0; y <= y1; y += 1) {
    const cy = y + 0.5;
    xs.length = 0;
    for (let i = 0; i < n; i += 1) {
      const a = points[i] as Point;
      const b = points[(i + 1) % n] as Point;
      // half-open in y, so a vertex on the scanline is counted once and horizontal edges never
      if ((a.y <= cy) !== (b.y <= cy)) xs.push(a.x + ((cy - a.y) / (b.y - a.y)) * (b.x - a.x));
    }
    xs.sort((p, q) => p - q);
    for (let k = 0; k + 1 < xs.length; k += 2) {
      const xa = Math.max(0, Math.ceil((xs[k] as number) - 0.5));
      const xb = Math.min(layer.width - 1, Math.ceil((xs[k + 1] as number) - 0.5) - 1);
      if (xb < xa) continue;
      layer.data.fill(value, y * layer.width + xa, y * layer.width + xb + 1);
      written += xb - xa + 1;
    }
  }
  return written;
}

/** Fraction of the layer that is set. */
export function coverage(layer: MaskLayer): number {
  let on = 0;
  for (let i = 0; i < layer.data.length; i += 1) if (layer.data[i] !== 0) on += 1;
  return layer.data.length === 0 ? 0 : on / layer.data.length;
}

export function clearMask(layer: MaskLayer): void {
  layer.data.fill(0);
}

/** RGBA bytes for drawing the layer over the plate: `rgb` at `alpha` where set, transparent elsewhere. */
export function maskToRgba(layer: MaskLayer, rgb: readonly [number, number, number], alpha = 120): Uint8ClampedArray<ArrayBuffer> {
  const out = new Uint8ClampedArray(layer.width * layer.height * 4);
  for (let i = 0; i < layer.data.length; i += 1) {
    if (layer.data[i] === 0) continue;
    out[i * 4] = rgb[0];
    out[i * 4 + 1] = rgb[1];
    out[i * 4 + 2] = rgb[2];
    out[i * 4 + 3] = alpha;
  }
  return out;
}

// ---------------------------------------------------------------------------------------------------- PNG

let crcTable: Uint32Array | null = null;

export function crc32(bytes: Uint8Array): number {
  if (!crcTable) {
    crcTable = new Uint32Array(256);
    for (let n = 0; n < 256; n += 1) {
      let c = n;
      for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      crcTable[n] = c >>> 0;
    }
  }
  let crc = 0xffffffff;
  for (let i = 0; i < bytes.length; i += 1) crc = (crcTable[(crc ^ (bytes[i] ?? 0)) & 0xff] ?? 0) ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

export function adler32(bytes: Uint8Array): number {
  let a = 1;
  let b = 0;
  for (let i = 0; i < bytes.length; i += 1) {
    a = (a + (bytes[i] ?? 0)) % 65521;
    b = (b + a) % 65521;
  }
  return ((b << 16) | a) >>> 0;
}

/** A zlib stream of stored (uncompressed) deflate blocks: valid anywhere, used when CompressionStream is missing. */
export function zlibStored(raw: Uint8Array): Uint8Array<ArrayBuffer> {
  const BLOCK = 65535;
  const blocks = Math.max(1, Math.ceil(raw.length / BLOCK));
  const out = new Uint8Array(2 + raw.length + blocks * 5 + 4);
  const view = new DataView(out.buffer);
  out[0] = 0x78;
  out[1] = 0x01;
  let o = 2;
  for (let b = 0; b < blocks; b += 1) {
    const chunk = raw.subarray(b * BLOCK, Math.min(raw.length, (b + 1) * BLOCK));
    out[o] = b === blocks - 1 ? 1 : 0;
    view.setUint16(o + 1, chunk.length, true);
    view.setUint16(o + 3, ~chunk.length & 0xffff, true);
    out.set(chunk, o + 5);
    o += 5 + chunk.length;
  }
  view.setUint32(o, adler32(raw), false);
  return out;
}

/** zlib (RFC 1950) with the platform's CompressionStream when there is one, else stored blocks. */
export async function zlibDeflate(raw: Uint8Array<ArrayBuffer>): Promise<Uint8Array<ArrayBuffer>> {
  if (typeof CompressionStream === "function" && typeof ReadableStream === "function" && typeof Response === "function") {
    try {
      const source = new ReadableStream<Uint8Array<ArrayBuffer>>({
        start(controller) {
          controller.enqueue(raw);
          controller.close();
        },
      });
      const packed = await new Response(source.pipeThrough(new CompressionStream("deflate"))).arrayBuffer();
      return new Uint8Array(packed);
    } catch {
      // fall through to stored blocks
    }
  }
  return zlibStored(raw);
}

const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

function chunk(type: string, data: Uint8Array): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(12 + data.length);
  const view = new DataView(out.buffer);
  view.setUint32(0, data.length, false);
  for (let i = 0; i < 4; i += 1) out[4 + i] = type.charCodeAt(i);
  out.set(data, 8);
  view.setUint32(8 + data.length, crc32(out.subarray(4, 8 + data.length)), false);
  return out;
}

/** The mask as an 8-bit greyscale PNG (255 = editable). */
export async function encodeMaskPng(layer: MaskLayer): Promise<Uint8Array<ArrayBuffer>> {
  const { width, height } = layer;
  const rows = new Uint8Array(height * (width + 1));
  for (let y = 0; y < height; y += 1) {
    rows[y * (width + 1)] = 0; // filter: none
    rows.set(layer.data.subarray(y * width, (y + 1) * width), y * (width + 1) + 1);
  }
  const ihdr = new Uint8Array(13);
  const hv = new DataView(ihdr.buffer);
  hv.setUint32(0, width, false);
  hv.setUint32(4, height, false);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 0; // greyscale
  const parts = [new Uint8Array(PNG_SIGNATURE), chunk("IHDR", ihdr), chunk("IDAT", await zlibDeflate(rows)), chunk("IEND", new Uint8Array(0))];
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let o = 0;
  for (const p of parts) {
    out.set(p, o);
    o += p.length;
  }
  return out;
}
