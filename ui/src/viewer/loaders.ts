// Fetch and decode viewer inputs. Half-float sources come from the daemon as raw little-endian binary16
// (`GET /api/v0/assets/<hash>?fmt=rgba16f`, T25): width * height * 4 halves for RGBA16F, width * height for R16F
// heatmaps, rows top-first, no header. 8-bit images and edge masks are ordinary PNGs the browser decodes, with colour
// management off so the code values arrive untouched. The bearer token goes to this origin only, never elsewhere.
// T159 (decision 93): decoded A/B images are kept in a small LRU keyed by URL, encoding and declared size, so the
// plate is fetched once per compare session while only the B side changes. Viewer URLs name CAS hashes (immutable
// bytes), so a hit never needs revalidating; inline `data` sources skip the cache.

import { getToken } from "../api/client";
import type { BitmapPixels, HalfPixels, Pixels } from "./renderer";
import type { HeatSource, ImageSource } from "./types";

export const RGBA16F_FMT = "rgba16f";

/** Append `fmt=<fmt>` to a URL unless it already names a format. */
export function withFormat(url: string, fmt: string): string {
  const hashAt = url.indexOf("#");
  const base = hashAt >= 0 ? url.slice(0, hashAt) : url;
  const hash = hashAt >= 0 ? url.slice(hashAt) : "";
  if (/[?&]fmt=/.test(base)) return url;
  return `${base}${base.includes("?") ? "&" : "?"}fmt=${encodeURIComponent(fmt)}${hash}`;
}

/** True for relative URLs and URLs on this page's origin: the only ones that get the bearer token. */
export function isSameOrigin(url: string, origin: string = globalThis.location?.origin ?? ""): boolean {
  if (url.startsWith("blob:") || url.startsWith("data:")) return false;
  try {
    return new URL(url, origin || "http://127.0.0.1").origin === (origin || "http://127.0.0.1");
  } catch {
    return false;
  }
}

async function fetchOk(url: string, signal?: AbortSignal): Promise<Response> {
  const headers = new Headers();
  const token = getToken();
  if (token && isSameOrigin(url)) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(url, { headers, signal });
  if (!res.ok) throw new Error(`GET ${url} -> ${res.status}`);
  return res;
}

async function bytesOf(data: Blob | ArrayBuffer): Promise<ArrayBuffer> {
  if (data instanceof ArrayBuffer) return data;
  if (typeof data.arrayBuffer === "function") return data.arrayBuffer();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(data);
  });
}

/** Channel count implied by a raw half-float payload (1 = R16F, 4 = RGBA16F); throws on any other size. */
export function halfChannels(
  byteLength: number,
  width: number,
  height: number,
  allowed: readonly (1 | 4)[] = [1, 4],
): 1 | 4 {
  for (const c of allowed) if (byteLength === width * height * c * 2) return c;
  const want = allowed.map((c) => `${width * height * c * 2} (${c === 1 ? "R16F" : "RGBA16F"})`).join(" or ");
  throw new Error(`expected ${want} bytes of half floats for ${width}x${height}, got ${byteLength}`);
}

const LITTLE_ENDIAN = new Uint8Array(new Uint16Array([1]).buffer)[0] === 1;

/** Little-endian binary16 bytes -> host-order Uint16Array (a copy when the host is big-endian). */
export function decodeHalfBuffer(buf: ArrayBuffer): Uint16Array {
  if (buf.byteLength % 2 !== 0) throw new Error(`half-float payload has an odd byte count (${buf.byteLength})`);
  if (LITTLE_ENDIAN) return new Uint16Array(buf);
  const view = new DataView(buf);
  const out = new Uint16Array(buf.byteLength / 2);
  for (let i = 0; i < out.length; i++) out[i] = view.getUint16(i * 2, true);
  return out;
}

function checkSize(width: number, height: number, what: string): void {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
    throw new Error(`${what} needs a positive integer width and height, got ${width}x${height}`);
  }
}

async function decodeBitmap(blob: Blob): Promise<ImageBitmap> {
  if (typeof createImageBitmap !== "function") throw new Error("this browser cannot decode images (createImageBitmap)");
  return createImageBitmap(blob, { colorSpaceConversion: "none", premultiplyAlpha: "none" });
}

/** Decoded-image cache budget: room for a 4K plate (66 MB of half floats) and three takes. */
export const VIEWER_CACHE_BYTES = 320 * 1024 * 1024;
export const VIEWER_CACHE_ENTRIES = 4;

export interface PixelCacheLimits {
  bytes: number;
  entries: number;
}

export interface PixelCacheStats {
  entries: number;
  bytes: number;
  hits: number;
  misses: number;
}

/** What a decoded image holds in memory: its half-float buffer, or 4 bytes a pixel for a bitmap. */
export function pixelBytes(px: Pixels): number {
  return px.kind === "half" ? px.data.byteLength : px.width * px.height * 4;
}

/** The cache key of a fetched A/B source. */
export function pixelCacheKey(src: ImageSource): string {
  return `${src.url}|${src.encoding}|${src.width}x${src.height}`;
}

/** A closed ImageBitmap reports 0 x 0 and can no longer be uploaded. */
function isClosed(px: Pixels): boolean {
  return px.kind === "bitmap" && (px.source as { width?: unknown }).width === 0;
}

/**
 * LRU of decoded viewer images under a byte and an entry budget. `put` evicts the least recently used entries until
 * both limits hold; an image larger than the whole byte budget is not cached. Cached bitmaps are not closed here:
 * closing is the renderer's business, and a bitmap found closed is dropped as a miss.
 */
export class PixelCache {
  private readonly items = new Map<string, { px: Pixels; bytes: number }>();
  private limits: PixelCacheLimits;
  private total = 0;
  private hits = 0;
  private misses = 0;

  constructor(limits: Partial<PixelCacheLimits> = {}) {
    this.limits = { bytes: VIEWER_CACHE_BYTES, entries: VIEWER_CACHE_ENTRIES, ...limits };
  }

  get(key: string): Pixels | undefined {
    const item = this.items.get(key);
    if (!item || isClosed(item.px)) {
      if (item) this.drop(key);
      this.misses++;
      return undefined;
    }
    this.items.delete(key); // re-insert: Map order is the recency order, oldest first
    this.items.set(key, item);
    this.hits++;
    return item.px;
  }

  put(key: string, px: Pixels): void {
    this.drop(key);
    const bytes = pixelBytes(px);
    if (bytes > this.limits.bytes || this.limits.entries < 1) return;
    this.items.set(key, { px, bytes });
    this.total += bytes;
    this.evict();
  }

  setLimits(limits: Partial<PixelCacheLimits>): void {
    this.limits = { ...this.limits, ...limits };
    this.evict();
  }

  clear(): void {
    this.items.clear();
    this.total = 0;
    this.hits = 0;
    this.misses = 0;
  }

  stats(): PixelCacheStats {
    return { entries: this.items.size, bytes: this.total, hits: this.hits, misses: this.misses };
  }

  private drop(key: string): void {
    const item = this.items.get(key);
    if (!item) return;
    this.items.delete(key);
    this.total -= item.bytes;
  }

  private evict(): void {
    while (this.items.size > 0 && (this.items.size > this.limits.entries || this.total > this.limits.bytes)) {
      this.drop(this.items.keys().next().value as string);
    }
  }
}

/** The viewer's shared cache: every <Viewer> slot reads through it. */
export const pixelCache = new PixelCache();

/** Tests: resize the shared cache (an omitted limit goes back to its default). */
export function setPixelCacheLimits(limits: Partial<PixelCacheLimits> = {}): void {
  pixelCache.setLimits({ bytes: VIEWER_CACHE_BYTES, entries: VIEWER_CACHE_ENTRIES, ...limits });
}

async function decodeImage(src: ImageSource, signal?: AbortSignal): Promise<Pixels> {
  if (src.encoding === "srgb8") {
    const blob = src.data
      ? src.data instanceof Blob
        ? src.data
        : new Blob([src.data])
      : await (await fetchOk(src.url, signal)).blob();
    const bmp = await decodeBitmap(blob);
    return { kind: "bitmap", width: bmp.width, height: bmp.height, source: bmp } satisfies BitmapPixels;
  }
  const buf = src.data
    ? await bytesOf(src.data)
    : await (await fetchOk(withFormat(src.url, RGBA16F_FMT), signal)).arrayBuffer();
  halfChannels(buf.byteLength, src.width, src.height, [4]);
  return {
    kind: "half",
    width: src.width,
    height: src.height,
    channels: 4,
    data: decodeHalfBuffer(buf),
    linear: src.encoding === "exr-half",
  } satisfies HalfPixels;
}

/** A or B: half floats for png16 / exr-half, a decoded bitmap for srgb8. A fetched source reads through `cache`; a
 * load aborted before it is stored leaves the cache as it was. */
export async function loadImage(
  src: ImageSource,
  signal?: AbortSignal,
  cache: PixelCache = pixelCache,
): Promise<Pixels> {
  checkSize(src.width, src.height, "image");
  if (src.data) return decodeImage(src, signal);
  const key = pixelCacheKey(src);
  const hit = cache.get(key);
  if (hit) return hit;
  const px = await decodeImage(src, signal);
  if (!signal?.aborted) cache.put(key, px);
  return px;
}

/** An edge-mask PNG. */
export async function loadMask(url: string, signal?: AbortSignal): Promise<BitmapPixels> {
  const bmp = await decodeBitmap(await (await fetchOk(url, signal)).blob());
  return { kind: "bitmap", width: bmp.width, height: bmp.height, source: bmp };
}

/** The daemon's ΔE00 map (R16F or RGBA16F, ΔE in R). The URL is used as given: it names its own format. */
export async function loadHeat(src: HeatSource, signal?: AbortSignal): Promise<HalfPixels> {
  checkSize(src.width, src.height, "heatmap");
  const buf = src.data ? await bytesOf(src.data) : await (await fetchOk(src.url, signal)).arrayBuffer();
  const channels = halfChannels(buf.byteLength, src.width, src.height);
  return { kind: "half", width: src.width, height: src.height, channels, data: decodeHalfBuffer(buf), linear: true };
}
