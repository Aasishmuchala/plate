// The viewer's mode state machine and keyboard map, as a pure reducer so it is tested without a DOM or a GPU.
// `mode` is what was asked for; `displayedMode` is what can be drawn with the sources present (no B image means no
// wipe/toggle/diff, no edge masks means no edge overlay, and so on), falling back to the last compare mode, then A.

import { clampZoom, fitZoom, makeView, scaleFor, screenToImage, type View } from "./math";
import type { Pan, Point, Size, ViewerMode, Zoom } from "./types";

export interface Caps {
  hasB: boolean;
  hasEdges: boolean;
  hasHeat: boolean;
  hasTiles: boolean;
}

export const NO_CAPS: Caps = { hasB: false, hasEdges: false, hasHeat: false, hasTiles: false };

export type BaseMode = "wipe" | "toggle";

export interface ViewState {
  mode: ViewerMode;
  /** The compare mode d/e/h/t return to when pressed again. */
  base: BaseMode;
  /** Wipe line as a fraction of the viewport width; A left of it, B right. */
  wipe: number;
  /** Toggle mode: B is showing. */
  showB: boolean;
  zoom: Zoom;
  /** Image point at the viewport centre; null = the image centre. */
  pan: Pan | null;
  caps: Caps;
  image: Size;
  viewport: Size;
  /** Why the last key did nothing (shown briefly in the HUD); null after any action that took effect. */
  notice: string | null;
}

/** What the renderer draws. */
export type DisplayedMode = "a" | "b" | "wipe" | "diff" | "edges" | "de00" | "tiles";

export const WIPE_STEP = 0.05;
export const WIPE_FINE = 0.01;
export const PAN_STEP = 64; // device pixels per arrow press
export const PAN_BIG = 256; // with shift
export const ZOOM_SNAP = 0.03; // zooming within 3% of 100% lands on exactly 100%

export type OverlayMode = "diff" | "edges" | "de00" | "tiles";

export type ViewAction =
  | { type: "flip" }
  | { type: "wipeStep"; delta: number }
  | { type: "setWipe"; wipe: number }
  | { type: "toggleMode"; mode: OverlayMode }
  | { type: "zoom100" }
  | { type: "fit" }
  | { type: "zoomBy"; factor: number; at?: Point }
  | { type: "zoomStep"; dir: 1 | -1 }
  | { type: "pan"; dx: number; dy: number }
  | { type: "caps"; caps: Caps }
  | { type: "clearNotice" }
  | { type: "layout"; image?: Size; viewport?: Size }
  | { type: "sync"; patch: { mode?: ViewerMode; wipe?: number; zoom?: Zoom; pan?: Pan | null } };

export interface ViewInit {
  mode?: ViewerMode;
  wipe?: number;
  zoom?: Zoom;
  pan?: Pan | null;
  image?: Size;
  viewport?: Size;
  caps?: Caps;
}

const clamp01 = (v: number) => Math.min(1, Math.max(0, v));
const tidy = (v: number) => Math.round(v * 1e4) / 1e4;

export function initialViewState(init: ViewInit = {}): ViewState {
  const mode = init.mode ?? "wipe";
  return {
    mode,
    base: mode === "toggle" ? "toggle" : "wipe",
    wipe: clamp01(init.wipe ?? 0.5),
    showB: false,
    zoom: init.zoom === undefined || init.zoom === "fit" ? "fit" : clampZoom(init.zoom),
    pan: init.pan ?? null,
    caps: init.caps ?? NO_CAPS,
    image: init.image ?? { width: 0, height: 0 },
    viewport: init.viewport ?? { width: 0, height: 0 },
    notice: null,
  };
}

const NEEDS: Record<ViewerMode, keyof Caps> = {
  wipe: "hasB",
  toggle: "hasB",
  diff: "hasB",
  edges: "hasEdges",
  de00: "hasHeat",
  tiles: "hasTiles",
};

const MISSING: Record<keyof Caps, string> = {
  hasB: "no B image to compare",
  hasEdges: "no edge masks for this view",
  hasHeat: "no ΔE00 map for this view",
  hasTiles: "no gate-flagged points for tiles",
};

export const modeAvailable = (mode: ViewerMode, caps: Caps): boolean => caps[NEEDS[mode]];

export function displayedMode(s: ViewState): DisplayedMode {
  let m: ViewerMode = s.mode;
  if (!modeAvailable(m, s.caps)) m = s.base;
  if (!modeAvailable(m, s.caps)) return "a";
  if (m === "toggle") return s.showB ? "b" : "a";
  return m;
}

export const scaleOf = (s: ViewState): number => scaleFor(s.zoom, s.image, s.viewport);

export const centreOf = (s: ViewState): Pan => s.pan ?? { x: s.image.width / 2, y: s.image.height / 2 };

export const viewOf = (s: ViewState): View => makeView(scaleOf(s), centreOf(s), s.viewport);

function clampPan(p: Pan, image: Size): Pan {
  return { x: Math.min(image.width, Math.max(0, p.x)), y: Math.min(image.height, Math.max(0, p.y)) };
}

const refuse = (s: ViewState, notice: string): ViewState => (s.notice === notice ? s : { ...s, notice });

export function viewerReducer(s: ViewState, a: ViewAction): ViewState {
  switch (a.type) {
    case "flip":
      if (!s.caps.hasB) return refuse(s, MISSING.hasB);
      return { ...s, mode: "toggle", base: "toggle", showB: !s.showB, notice: null };
    case "wipeStep":
      if (!s.caps.hasB) return refuse(s, MISSING.hasB);
      if (s.mode !== "wipe") return { ...s, mode: "wipe", base: "wipe", notice: null };
      return { ...s, wipe: tidy(clamp01(s.wipe + a.delta)), notice: null };
    case "setWipe":
      return { ...s, mode: "wipe", base: "wipe", wipe: tidy(clamp01(a.wipe)), notice: null };
    case "toggleMode":
      if (s.mode === a.mode) return { ...s, mode: s.base, notice: null };
      if (!modeAvailable(a.mode, s.caps)) return refuse(s, MISSING[NEEDS[a.mode]]);
      return { ...s, mode: a.mode, notice: null };
    case "zoom100":
      return { ...s, zoom: 1, pan: centreOf(s), notice: null };
    case "fit":
      return { ...s, zoom: "fit", pan: null, notice: null };
    case "zoomBy": {
      const from = scaleOf(s);
      let to = clampZoom(from * a.factor);
      if (Math.abs(to - 1) < ZOOM_SNAP) to = 1;
      let centre = centreOf(s);
      if (a.at) {
        // keep the image point under the cursor where it is
        const p = screenToImage(a.at, makeView(from, centre, s.viewport));
        centre = { x: p.x - (a.at.x - s.viewport.width / 2) / to, y: p.y - (a.at.y - s.viewport.height / 2) / to };
      }
      return { ...s, zoom: to, pan: clampPan(centre, s.image), notice: null };
    }
    case "zoomStep": {
      // keyboard zoom walks the powers of two around 100%: fit 27% -> 50% -> 100% -> 200%
      const lg = Math.log2(scaleOf(s));
      const to = clampZoom(2 ** (a.dir > 0 ? Math.floor(lg + 1e-6) + 1 : Math.ceil(lg - 1e-6) - 1));
      return { ...s, zoom: to, pan: centreOf(s), notice: null };
    }
    case "pan": {
      const scale = scaleOf(s);
      const c = centreOf(s);
      const pan = clampPan({ x: c.x + a.dx / scale, y: c.y + a.dy / scale }, s.image);
      return { ...s, zoom: s.zoom === "fit" ? fitZoom(s.image, s.viewport) : s.zoom, pan, notice: null };
    }
    case "caps": {
      const c = s.caps;
      const same = (Object.keys(c) as (keyof Caps)[]).every((k) => c[k] === a.caps[k]);
      return same ? s : { ...s, caps: a.caps };
    }
    case "clearNotice":
      return s.notice === null ? s : { ...s, notice: null };
    case "layout": {
      const image = a.image ?? s.image;
      const viewport = a.viewport ?? s.viewport;
      const pan = s.pan ? clampPan(s.pan, image) : null;
      return { ...s, image, viewport, pan };
    }
    case "sync": {
      const p = a.patch;
      const next = { ...s };
      if (p.mode !== undefined) {
        next.mode = p.mode;
        if (p.mode === "wipe" || p.mode === "toggle") next.base = p.mode;
      }
      if (p.wipe !== undefined) next.wipe = clamp01(p.wipe);
      if (p.zoom !== undefined) next.zoom = p.zoom === "fit" ? "fit" : clampZoom(p.zoom);
      if (p.pan !== undefined) next.pan = p.pan ? clampPan(p.pan, s.image) : null;
      const samePan = next.pan === s.pan || (next.pan && s.pan && next.pan.x === s.pan.x && next.pan.y === s.pan.y);
      const same = next.mode === s.mode && next.base === s.base && next.wipe === s.wipe && next.zoom === s.zoom;
      return same && samePan ? s : next;
    }
  }
}

export interface KeyLike {
  key: string;
  shiftKey?: boolean;
  ctrlKey?: boolean;
  metaKey?: boolean;
  altKey?: boolean;
}

/**
 * ` flips A/B (toggle mode), [ ] move the wipe (shift: { } fine), 1 = 100%, f = fit, d diff, e edges, h ΔE00
 * heatmap, t tiles (each pressed again returns to wipe/toggle), arrows pan (shift: further), + / - zoom.
 * Browser shortcuts (ctrl, cmd, alt) are left alone.
 */
export function keyToAction(e: KeyLike): ViewAction | null {
  if (e.ctrlKey || e.metaKey || e.altKey) return null;
  const step = e.shiftKey ? PAN_BIG : PAN_STEP;
  switch (e.key) {
    case "`":
      return { type: "flip" };
    case "[":
      return { type: "wipeStep", delta: -WIPE_STEP };
    case "]":
      return { type: "wipeStep", delta: WIPE_STEP };
    case "{":
      return { type: "wipeStep", delta: -WIPE_FINE };
    case "}":
      return { type: "wipeStep", delta: WIPE_FINE };
    case "1":
      return { type: "zoom100" };
    case "f":
    case "F":
      return { type: "fit" };
    case "d":
    case "D":
      return { type: "toggleMode", mode: "diff" };
    case "e":
    case "E":
      return { type: "toggleMode", mode: "edges" };
    case "h":
    case "H":
      return { type: "toggleMode", mode: "de00" };
    case "t":
    case "T":
      return { type: "toggleMode", mode: "tiles" };
    case "ArrowLeft":
      return { type: "pan", dx: -step, dy: 0 };
    case "ArrowRight":
      return { type: "pan", dx: step, dy: 0 };
    case "ArrowUp":
      return { type: "pan", dx: 0, dy: -step };
    case "ArrowDown":
      return { type: "pan", dx: 0, dy: step };
    case "+":
    case "=":
      return { type: "zoomStep", dir: 1 };
    case "-":
    case "_":
      return { type: "zoomStep", dir: -1 };
    default:
      return null;
  }
}
