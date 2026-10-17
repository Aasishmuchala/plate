// CAS assets on the canvas. Thumbnails come from `GET /api/v0/assets/<hash>?fmt=thumb`; an <img src> cannot send
// the bearer token, so the bytes are fetched with it and shown through an object URL (cached per hash: a hash names
// immutable bytes). Viewer sources point at the same route: `png16` (raw half floats via `fmt=rgba16f`, which the
// viewer appends) when the size is known, else an 8-bit PNG whose size the browser reads from the file.

import { useEffect, useState } from "react";
import { apiUrl, getToken } from "../api/client";
import type { ImageSize } from "../nodeTypes";
import type { ImageSource } from "../viewer";

export const assetUrl = (hash: string, fmt?: "png" | "thumb" | "rgba16f"): string =>
  apiUrl(`/assets/${encodeURIComponent(hash)}${fmt ? `?fmt=${fmt}` : ""}`);

/** A viewer source for a CAS image. Without a size it falls back to `srgb8`: the loader takes the decoded bitmap's
 * size, so the 1x1 placeholder only satisfies its size check. */
export function assetImageSource(hash: string, size: ImageSize | undefined, label?: string): ImageSource {
  const base = size
    ? { url: assetUrl(hash), width: size.width, height: size.height, encoding: "png16" as const }
    : { url: assetUrl(hash, "png"), width: 1, height: 1, encoding: "srgb8" as const };
  return label ? { ...base, label } : base;
}

const THUMB_CACHE_MAX = 256;
const thumbCache = new Map<string, string>();
const inflight = new Map<string, Promise<string | null>>();

function remember(hash: string, url: string): void {
  thumbCache.set(hash, url);
  while (thumbCache.size > THUMB_CACHE_MAX) {
    const oldest = thumbCache.keys().next().value as string;
    const stale = thumbCache.get(oldest);
    thumbCache.delete(oldest);
    if (stale && typeof URL.revokeObjectURL === "function") URL.revokeObjectURL(stale);
  }
}

/** Fetch a thumbnail with the token; resolves to an object URL, or null when it cannot be shown. */
export function loadThumb(hash: string): Promise<string | null> {
  const hit = thumbCache.get(hash);
  if (hit) return Promise.resolve(hit);
  const pending = inflight.get(hash);
  if (pending) return pending;
  const p = (async () => {
    try {
      const headers = new Headers();
      const token = getToken();
      if (token) headers.set("Authorization", `Bearer ${token}`);
      const res = await fetch(assetUrl(hash, "thumb"), { headers });
      if (!res.ok) return null;
      const blob = await res.blob();
      if (typeof URL.createObjectURL !== "function") return null;
      const url = URL.createObjectURL(blob);
      remember(hash, url);
      return url;
    } catch {
      return null;
    } finally {
      inflight.delete(hash);
    }
  })();
  inflight.set(hash, p);
  return p;
}

export type ThumbState = { state: "none" } | { state: "loading" } | { state: "ready"; url: string } | { state: "error" };

export function useThumb(hash: string | null | undefined): ThumbState {
  const [thumb, setThumb] = useState<ThumbState>(() => {
    if (!hash) return { state: "none" };
    const hit = thumbCache.get(hash);
    return hit ? { state: "ready", url: hit } : { state: "loading" };
  });
  useEffect(() => {
    if (!hash) {
      setThumb({ state: "none" });
      return;
    }
    let alive = true;
    const hit = thumbCache.get(hash);
    if (hit) {
      setThumb({ state: "ready", url: hit });
      return;
    }
    setThumb({ state: "loading" });
    loadThumb(hash).then((url) => {
      if (alive) setThumb(url ? { state: "ready", url } : { state: "error" });
    });
    return () => {
      alive = false;
    };
  }, [hash]);
  return thumb;
}

/** Tests only: forget cached thumbnails. */
export function clearThumbCache(): void {
  thumbCache.clear();
  inflight.clear();
}
