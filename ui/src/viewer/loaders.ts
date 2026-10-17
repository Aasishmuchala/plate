// Fetch and decode viewer inputs. Half-float sources come from the daemon as raw little-endian binary16
// (`GET /api/v0/assets/<hash>?fmt=rgba16f`, T25): width * height * 4 halves for RGBA16F, width * height for R16F
// heatmaps, rows top-first, no header. 8-bit images and edge masks are ordinary PNGs the browser decodes, with colour
// management off so the code values arrive untouched. The bearer token goes to this origin only, never elsewhere.

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

/** A or B: half floats for png16 / exr-half, a decoded bitmap for srgb8. */
export async function loadImage(src: ImageSource, signal?: AbortSignal): Promise<Pixels> {
  checkSize(src.width, src.height, "image");
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
