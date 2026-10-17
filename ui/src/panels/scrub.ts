// The full-scrub record behind "a take is accepted only after a logged full scrub" (doctrine 7). The viewer reports
// every user change of zoom and pan (`ViewerChange`); the tracker turns those into "which quadrants of the image
// were inspected at 100% or more". A quadrant counts when its centre point lay inside the viewport while the scale
// was at least one device pixel per image pixel. "fit" counts only when fit itself is >= 100% (a small image in a
// big viewport). A full scrub is all four quadrants.

import { scaleFor } from "../viewer/math";
import type { Pan, Size, Zoom } from "../viewer/types";

/** Top-left, top-right, bottom-left, bottom-right. */
export type Quadrants = readonly [boolean, boolean, boolean, boolean];

export interface ScrubProgress {
  quadrants: Quadrants;
  /** True once the view has been at >= 100% at all. */
  zoom100: boolean;
}

export const EMPTY_SCRUB: ScrubProgress = { quadrants: [false, false, false, false], zoom100: false };

export const QUADRANT_NAMES = ["top-left", "top-right", "bottom-left", "bottom-right"] as const;

const SCALE_EPS = 1e-6;

/** What `observeView` reads from a viewer change. */
export interface ViewSample {
  zoom: Zoom;
  pan: Pan | null;
}

/** The quadrant centres of an image, in image pixels, in `Quadrants` order. */
export function quadrantCentres(image: Size): { x: number; y: number }[] {
  const { width: w, height: h } = image;
  return [
    { x: w / 4, y: h / 4 },
    { x: (3 * w) / 4, y: h / 4 },
    { x: w / 4, y: (3 * h) / 4 },
    { x: (3 * w) / 4, y: (3 * h) / 4 },
  ];
}

/** Fold one view of the image into the progress. `viewport` is in device pixels (the viewer's backing store). */
export function observeView(prev: ScrubProgress, view: ViewSample, image: Size, viewport: Size): ScrubProgress {
  if (image.width <= 0 || image.height <= 0 || viewport.width <= 0 || viewport.height <= 0) return prev;
  const scale = scaleFor(view.zoom, image, viewport);
  if (scale < 1 - SCALE_EPS) return prev;
  const centre = view.pan ?? { x: image.width / 2, y: image.height / 2 };
  const halfW = viewport.width / (2 * scale);
  const halfH = viewport.height / (2 * scale);
  const seen = quadrantCentres(image).map(
    (q) => Math.abs(q.x - centre.x) <= halfW && Math.abs(q.y - centre.y) <= halfH,
  );
  const quadrants = prev.quadrants.map((v, i) => v || seen[i]) as unknown as Quadrants;
  const changed = quadrants.some((v, i) => v !== prev.quadrants[i]);
  if (!changed && prev.zoom100) return prev;
  return { quadrants, zoom100: true };
}

export const quadrantsVisited = (p: ScrubProgress): number => p.quadrants.filter(Boolean).length;

/** All four quadrants inspected at 100% or more. */
export const isFullScrub = (p: ScrubProgress): boolean => p.zoom100 && quadrantsVisited(p) === 4;
