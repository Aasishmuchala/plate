// T159 (decision 93): the viewer's decoded-image cache. A counting `fetch` fake answers `width * height * 8` bytes for
// `?fmt=rgba16f` (one shared buffer per size, so a 4K strip costs one 66 MB allocation, not one per response) and a
// small PNG blob otherwise. The loader tests drive `loadImage` directly; the <Viewer> tests run the component against
// a recording WebGL2 fake (jsdom has none) to show the A/B slots read through the shared cache and still upload on
// every source change. Regression for the stress low viewer-4k-refetch (the plate's 66 MB buffer re-downloaded on
// most candidate switches).

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  type ImageSource,
  PixelCache,
  pixelCache,
  setPixelCacheLimits,
  VIEWER_CACHE_BYTES,
  VIEWER_CACHE_ENTRIES,
  Viewer,
} from "../viewer";
import { loadImage, pixelBytes, pixelCacheKey } from "../viewer/loaders";
import type { HalfPixels } from "../viewer/renderer";

// ------------------------------------------------------------------------------------------------ fakes

const SIZES = new Map<string, { width: number; height: number }>();
const BUFFERS = new Map<number, ArrayBuffer>();

const shared = (bytes: number): ArrayBuffer => {
  let buf = BUFFERS.get(bytes);
  if (!buf) {
    buf = new ArrayBuffer(bytes);
    BUFFERS.set(bytes, buf);
  }
  return buf;
};

/** A png16 asset of the given size, registered with the fetch fake. */
function asset(name: string, width: number, height: number): ImageSource {
  const url = `/api/v0/assets/${name}`;
  SIZES.set(url, { width, height });
  return { url, width, height, encoding: "png16" };
}

const abortError = () => new DOMException("The operation was aborted.", "AbortError");

interface CountingFetch {
  urls: string[];
  /** Fetches of one source, whatever format the loader appended. */
  count(src: ImageSource | string): number;
  /** While held, responses wait for `release()` (or the caller's abort). */
  hold(): void;
  release(): void;
}

function countingFetch(): CountingFetch {
  const urls: string[] = [];
  let held = false;
  let waiting: (() => void)[] = [];
  const answer = (url: string): Response => {
    const u = new URL(url, "http://127.0.0.1");
    const size = SIZES.get(u.pathname);
    if (!size) return { ok: false, status: 404 } as Response;
    if (u.searchParams.get("fmt") === "rgba16f") {
      const buf = shared(size.width * size.height * 8);
      return { ok: true, status: 200, arrayBuffer: async () => buf } as unknown as Response;
    }
    const blob = new Blob([new Uint8Array(8)], { type: "image/png" });
    return { ok: true, status: 200, blob: async () => blob } as unknown as Response;
  };
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      urls.push(url);
      const signal = init?.signal ?? undefined;
      if (signal?.aborted) return Promise.reject(abortError());
      if (!held) return Promise.resolve(answer(url));
      return new Promise<Response>((resolve, reject) => {
        signal?.addEventListener("abort", () => reject(abortError()), { once: true });
        waiting.push(() => resolve(answer(url)));
      });
    }),
  );
  return {
    urls,
    count: (src) => {
      const want = typeof src === "string" ? src : src.url;
      return urls.filter((u) => u.split("?")[0] === want).length;
    },
    hold: () => {
      held = true;
    },
    release: () => {
      held = false;
      const go = waiting;
      waiting = [];
      for (const fn of go) fn();
    },
  };
}

/** A WebGL2 context that records calls (the shape of viewer.test.ts's fake, trimmed to what the viewer needs). */
function fakeGl() {
  const calls: unknown[][] = [];
  const consts = new Map<string, number>();
  let next = 0x9000;
  let gl: Record<string, unknown> = {};
  const special: Record<string, (...args: unknown[]) => unknown> = {
    getExtension: (name) => (name === "EXT_color_buffer_float" ? {} : null),
    getShaderParameter: () => true,
    getProgramParameter: () => true,
    checkFramebufferStatus: () => gl.FRAMEBUFFER_COMPLETE,
    getUniformLocation: (_p, name) => ({ uniform: name }),
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
  /** texImage2D calls that uploaded a source's half floats (not the linear targets, which pass null). */
  const halfUploads = () =>
    calls.filter((c) => c[0] === "texImage2D" && c[8] === gl.HALF_FLOAT && c[9] instanceof Uint16Array).length;
  vi.stubGlobal("WebGL2RenderingContext", class {});
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(((kind: string) =>
    kind === "webgl2" ? gl : null) as unknown as typeof HTMLCanvasElement.prototype.getContext);
  return { halfUploads };
}

beforeEach(() => {
  pixelCache.clear();
  setPixelCacheLimits();
});

afterEach(() => {
  cleanup();
  pixelCache.clear();
  setPixelCacheLimits();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  SIZES.clear();
});

const PLATE_4K = 3840 * 2160 * 8; // 66,355,200 bytes of RGBA16F

// ------------------------------------------------------------------------------------------------ the cache

describe("PixelCache", () => {
  const half = (bytes: number): HalfPixels => ({
    kind: "half",
    width: bytes / 8,
    height: 1,
    channels: 4,
    data: new Uint16Array(bytes / 2),
    linear: false,
  });

  it("ships the decision-93 budget and keys by url, encoding and declared size", () => {
    expect(VIEWER_CACHE_BYTES).toBe(320 * 1024 * 1024);
    expect(VIEWER_CACHE_ENTRIES).toBe(4);
    const plate = asset("h_plate", 3840, 2160);
    expect(pixelCacheKey(plate)).toBe("/api/v0/assets/h_plate|png16|3840x2160");
    expect(pixelCacheKey({ ...plate, encoding: "exr-half" })).not.toBe(pixelCacheKey(plate));
    expect(pixelCacheKey({ ...plate, width: 1920, height: 1080 })).not.toBe(pixelCacheKey(plate));
    expect(pixelBytes(half(64))).toBe(64);
    expect(pixelBytes({ kind: "bitmap", width: 10, height: 5, source: {} as ImageBitmap })).toBe(200);
  });

  it("evicts the least recently used entry; a get refreshes recency", () => {
    const cache = new PixelCache({ entries: 3 });
    cache.put("k1", half(8));
    cache.put("k2", half(8));
    cache.put("k3", half(8));
    expect(cache.get("k1")).toBeDefined();
    cache.put("k4", half(8));
    expect(cache.get("k2")).toBeUndefined();
    expect(cache.get("k1")).toBeDefined();
    expect(cache.get("k3")).toBeDefined();
    expect(cache.get("k4")).toBeDefined();
    expect(cache.stats()).toEqual({ entries: 3, bytes: 24, hits: 4, misses: 1 });
  });

  it("holds the byte budget, refuses an entry larger than it, and replaces a key in place", () => {
    const cache = new PixelCache({ bytes: 100, entries: 10 });
    cache.put("a", half(48));
    cache.put("b", half(48));
    cache.put("c", half(48)); // 144 > 100: "a" goes
    expect(cache.stats()).toMatchObject({ entries: 2, bytes: 96 });
    expect(cache.get("a")).toBeUndefined();
    cache.put("huge", half(104));
    expect(cache.get("huge")).toBeUndefined();
    expect(cache.stats()).toMatchObject({ entries: 2, bytes: 96 });
    cache.put("b", half(16));
    expect(cache.stats()).toMatchObject({ entries: 2, bytes: 64 });
    cache.setLimits({ bytes: 40 });
    expect(cache.stats()).toMatchObject({ entries: 1, bytes: 16 });
    cache.clear();
    expect(cache.stats()).toEqual({ entries: 0, bytes: 0, hits: 0, misses: 0 });
  });
});

// ------------------------------------------------------------------------------------------------ loadImage

describe("loadImage through the cache", () => {
  it("A (plate) + B1, then B2 and B3 with the same A: one fetch for A, one per B", async () => {
    const f = countingFetch();
    const plate = asset("h_plate", 64, 36);
    const takes = [asset("h_r1", 64, 36), asset("h_r2", 64, 36), asset("h_r3", 64, 36)];
    const first = await loadImage(plate);
    for (const b of takes) {
      expect(await loadImage(plate)).toBe(first);
      await loadImage(b);
    }
    expect(f.count(plate)).toBe(1);
    for (const b of takes) expect(f.count(b)).toBe(1);
    expect(f.urls[0]).toBe("/api/v0/assets/h_plate?fmt=rgba16f");
    expect(pixelCache.stats()).toEqual({ entries: 4, bytes: 4 * 64 * 36 * 8, hits: 3, misses: 4 });
  });

  it("the strip's typical pattern keeps a 4K plate resident under the default limits", async () => {
    const f = countingFetch();
    const plate = asset("h_plate", 3840, 2160);
    const takes = [1, 2, 3, 4].map((i) => asset(`h_r${i}`, 3840, 2160));
    // 4 takes x 3 passes, as the stress repro cycled them; every switch asks for A again (the viewer remounts)
    for (let pass = 0; pass < 3; pass++) {
      for (const b of takes) {
        const a = (await loadImage(plate)) as HalfPixels;
        expect(a.data.byteLength).toBe(PLATE_4K);
        await loadImage(b);
        const s = pixelCache.stats();
        expect(s.bytes).toBeLessThanOrEqual(VIEWER_CACHE_BYTES);
        expect(s.entries).toBeLessThanOrEqual(VIEWER_CACHE_ENTRIES);
      }
    }
    expect(f.count(plate)).toBe(1);
    expect(pixelCache.stats()).toMatchObject({ entries: 4, bytes: 4 * PLATE_4K, hits: 11 });
    expect(pixelCache.get(pixelCacheKey(plate))).toBeDefined();
  });

  it("with room for three images the fourth distinct one evicts the first, which then fetches again", async () => {
    const f = countingFetch();
    const imgs = [1, 2, 3, 4].map((i) => asset(`h_${i}`, 32, 18));
    const entryBytes = 32 * 18 * 8;
    setPixelCacheLimits({ bytes: 3 * entryBytes, entries: 4 });
    for (const img of imgs) await loadImage(img);
    expect(pixelCache.stats()).toMatchObject({ entries: 3, bytes: 3 * entryBytes });
    await loadImage(imgs[0]);
    expect(f.count(imgs[0])).toBe(2);
    await loadImage(imgs[3]); // still resident
    expect(f.count(imgs[3])).toBe(1);
    expect(pixelCache.stats().hits).toBe(1);
  });

  it("does not cache a single image larger than the byte budget", async () => {
    const f = countingFetch();
    const img = asset("h_big", 32, 18);
    setPixelCacheLimits({ bytes: 32 * 18 * 8 - 1 });
    await loadImage(img);
    await loadImage(img);
    expect(f.count(img)).toBe(2);
    expect(pixelCache.stats()).toMatchObject({ entries: 0, bytes: 0, hits: 0, misses: 2 });
  });

  it("an aborted load does not populate the cache", async () => {
    const f = countingFetch();
    const img = asset("h_r1", 16, 9);
    // aborted in flight: the fetch rejects and nothing is stored
    f.hold();
    const ctl = new AbortController();
    const pending = loadImage(img, ctl.signal);
    ctl.abort();
    await expect(pending).rejects.toThrow(/abort/i);
    f.release();
    expect(pixelCache.stats().entries).toBe(0);
    // aborted after the bytes arrived but before the store: still not cached
    const late = new AbortController();
    const done = loadImage(img, late.signal);
    late.abort();
    await done.catch(() => undefined);
    expect(pixelCache.stats().entries).toBe(0);
    // the next load fetches again and is the one that is kept
    await loadImage(img);
    expect(f.count(img)).toBe(3);
    expect(pixelCache.stats().entries).toBe(1);
    await loadImage(img);
    expect(f.count(img)).toBe(3);
  });

  it("a failed fetch is not cached", async () => {
    const f = countingFetch();
    const missing: ImageSource = { url: "/api/v0/assets/nope", width: 4, height: 4, encoding: "png16" };
    await expect(loadImage(missing)).rejects.toThrow(/404/);
    await expect(loadImage(missing)).rejects.toThrow(/404/);
    expect(f.count(missing)).toBe(2);
    expect(pixelCache.stats().entries).toBe(0);
  });

  it("inline data bypasses the cache: no fetch, no entry, no hit or miss", async () => {
    const f = countingFetch();
    const img = { ...asset("h_inline", 2, 1), data: new ArrayBuffer(2 * 1 * 8) };
    await loadImage(img);
    await loadImage(img);
    expect(f.urls).toHaveLength(0);
    expect(pixelCache.stats()).toEqual({ entries: 0, bytes: 0, hits: 0, misses: 0 });
  });

  it("keeps a decoded bitmap (not closed) and drops one that was closed since", async () => {
    const f = countingFetch();
    const bmp = { width: 10, height: 5 };
    vi.stubGlobal("createImageBitmap", vi.fn(async () => bmp));
    const png: ImageSource = { ...asset("h_png", 1, 1), encoding: "srgb8" }; // size unknown: the decoded one wins
    const first = await loadImage(png);
    expect(first).toMatchObject({ kind: "bitmap", width: 10, height: 5 });
    expect(await loadImage(png)).toBe(first);
    expect(f.count(png)).toBe(1);
    expect(pixelCache.stats()).toMatchObject({ entries: 1, bytes: 10 * 5 * 4 });
    bmp.width = 0; // what ImageBitmap.close() leaves behind
    await loadImage(png);
    expect(f.count(png)).toBe(2);
  });

  it("a caller's own cache is used instead of the shared one", async () => {
    const f = countingFetch();
    const img = asset("h_own", 8, 8);
    const own = new PixelCache({ entries: 1 });
    await loadImage(img, undefined, own);
    await loadImage(img, undefined, own);
    expect(f.count(img)).toBe(1);
    expect(own.stats()).toMatchObject({ entries: 1, hits: 1, misses: 1 });
    expect(pixelCache.stats()).toEqual({ entries: 0, bytes: 0, hits: 0, misses: 0 });
  });
});

// ------------------------------------------------------------------------------------------------ <Viewer>

describe("<Viewer> reads A and B through the shared cache", () => {
  const ready = () => screen.getByTestId("viewer").getAttribute("data-ready");

  it("fetches the plate once across take switches and a remount, and uploads on every source change", async () => {
    const f = countingFetch();
    const gl = fakeGl();
    const plate = asset("h_plate", 64, 36);
    const [b1, b2, b3] = [1, 2, 3].map((i) => asset(`h_r${i}`, 64, 36));
    const { rerender, unmount } = render(createElement(Viewer, { a: plate, b: b1 }));
    await waitFor(() => expect(ready()).toBe("a b"));
    for (const b of [b2, b3]) {
      rerender(createElement(Viewer, { a: plate, b }));
      await waitFor(() => expect(f.count(b)).toBe(1));
      await waitFor(() => expect(ready()).toBe("a b"));
    }
    const uploads = gl.halfUploads();
    expect(uploads).toBe(4); // A once, then B1, B2, B3
    // the compare slot swaps viewers (a new GPU context): A and B come from the cache and are uploaded again
    unmount();
    render(createElement(Viewer, { a: plate, b: b2 }));
    await waitFor(() => expect(ready()).toBe("a b"));
    expect(gl.halfUploads()).toBe(uploads + 2);
    expect(f.count(plate)).toBe(1);
    for (const b of [b1, b2, b3]) expect(f.count(b)).toBe(1);
    expect(pixelCache.stats()).toMatchObject({ entries: 4, hits: 2, misses: 4 });
  });

  it("a load aborted by an unmount is not cached: the next viewer fetches it again", async () => {
    const f = countingFetch();
    fakeGl();
    const plate = asset("h_plate", 32, 18);
    const b1 = asset("h_r1", 32, 18);
    const first = render(createElement(Viewer, { a: plate }));
    await waitFor(() => expect(ready()).toBe("a"));
    f.hold();
    first.rerender(createElement(Viewer, { a: plate, b: b1 }));
    await waitFor(() => expect(f.count(b1)).toBe(1));
    first.unmount(); // aborts B's fetch
    f.release();
    expect(pixelCache.stats().entries).toBe(1);
    render(createElement(Viewer, { a: plate, b: b1 }));
    await waitFor(() => expect(ready()).toBe("a b"));
    expect(f.count(b1)).toBe(2);
    expect(f.count(plate)).toBe(1);
  });
});
