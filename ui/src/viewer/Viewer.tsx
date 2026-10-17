// <Viewer>: the WebGL2 RGBA16F compare viewer (BUILD_PLAN "Viewer": wipe, toggle, difference, red-cyan G-buffer edge
// overlay, ΔE00 heatmap, auto-placed 100% crop tiles at gate-flagged points). The mode, wipe, zoom and pan props seed
// the viewer's own state and re-sync it when they change; user changes come back through onChange.
// Keys while the viewer has focus: ` toggle A/B, [ ] wipe ({ } fine), 1 100%, f fit, d diff, e edges, h heatmap,
// t tiles, arrows pan, + / - zoom, ? key list. Dragging pans (or moves the wipe line when grabbed); the wheel zooms
// about the cursor; double-click goes fit <-> 100%. Without WebGL2 the frame stays and says so: never a blank box.

import {
  type CSSProperties,
  type KeyboardEvent,
  type MouseEvent,
  type PointerEvent,
  type ReactElement,
  type Ref,
  useEffect,
  useImperativeHandle,
  useMemo,
  useReducer,
  useRef,
  useState,
} from "react";
import { loadHeat, loadImage, loadMask } from "./loaders";
import { DE00_MAX, DE00_STOPS, EDGE_COLORS, fitTileSize, type TileCell, tileGrid, tileLayout } from "./math";
import { type AnalysisKind, DEFAULT_GAIN, ViewerRenderer } from "./renderer";
import {
  type Caps,
  type DisplayedMode,
  displayedMode,
  initialViewState,
  keyToAction,
  scaleOf,
  viewerReducer,
  type ViewState,
  viewOf,
} from "./state";
import type { ImageSource, Overlays, Pan, ViewerMode, Zoom } from "./types";

export interface ViewerChange {
  mode: ViewerMode;
  wipe: number;
  zoom: Zoom;
  pan: Pan | null;
  showB: boolean;
}

export type SlotKey = "a" | "b" | "plateEdges" | "candEdges" | "heat";

export type LoadStatus = { state: "loading" } | { state: "ready" } | { state: "error"; message: string };

export interface ViewerHandle {
  /** Linear float read-back at image resolution, rows top-first (null on the 8-bit path). See README.md. */
  readAnalysis(kind: AnalysisKind, gain?: number): Float32Array | null;
  /** True once the slot's pixels are on the GPU. */
  ready(slot?: SlotKey): boolean;
  /** "ok" with float targets, "8-bit", or why there is no GPU path. */
  gpu(): string;
  state(): ViewState;
}

export interface ViewerProps {
  /** The reference (usually the plate). Without it the viewer shows an empty frame. */
  a?: ImageSource;
  /** The candidate. Wipe, toggle and diff need it. */
  b?: ImageSource;
  mode?: ViewerMode;
  /** Wipe line as a fraction of the viewer width, 0..1. */
  wipe?: number;
  zoom?: Zoom;
  pan?: Pan | null;
  overlays?: Overlays;
  /** |A - B| multiplier in diff mode (linear light, before the display OETF). */
  diffGain?: number;
  onChange?: (change: ViewerChange) => void;
  className?: string;
  style?: CSSProperties;
  ref?: Ref<ViewerHandle>;
}

type GpuState =
  | { status: "idle" }
  | { status: "ok"; float: boolean }
  | { status: "unavailable" }
  | { status: "lost" }
  | { status: "error"; message: string };

const MODE_LABEL: Record<DisplayedMode, string> = {
  a: "A",
  b: "B",
  wipe: "Wipe",
  diff: "Diff",
  edges: "Edges",
  de00: "ΔE00",
  tiles: "Tiles",
};

const KEY_LIST: ReadonlyArray<readonly [string, string]> = [
  ["`", "toggle A / B"],
  ["[ ]", "move wipe (shift: fine)"],
  ["1", "100%"],
  ["f", "fit"],
  ["d", "difference"],
  ["e", "edges: red plate, cyan candidate"],
  ["h", "ΔE00 heatmap"],
  ["t", "100% tiles at flagged points"],
  ["arrows", "pan (shift: further)"],
  ["+ -", "zoom"],
];

const C = { bg: "#101114", panel: "rgba(16, 17, 20, 0.82)", line: "#2a2c33", text: "#e7e7ea", muted: "#a9abb5" };
const WARN = "#d9a441";
const HUD_BAND = 30; // CSS pixels kept clear of tiles at the top for the HUD chips
const ERR = "#f0a3a9";

const S: Record<string, CSSProperties> = {
  canvas: { position: "absolute", inset: 0, width: "100%", height: "100%", display: "block" },
  wipeLine: { position: "absolute", top: 0, bottom: 0, left: 8, width: 1, background: "rgba(231, 231, 234, 0.8)" },
  tick: { position: "absolute", transform: "translateX(-50%)" },
  hud: {
    position: "absolute",
    top: 6,
    left: 6,
    right: 6,
    display: "flex",
    flexWrap: "wrap",
    gap: 6,
    alignItems: "center",
    pointerEvents: "none",
    fontSize: 11,
    letterSpacing: "0.04em",
  },
  chip: { padding: "2px 6px", background: C.panel, border: `1px solid ${C.line}`, color: C.text, borderRadius: 3 },
  centre: {
    position: "absolute",
    inset: 0,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    textAlign: "center",
    padding: 12,
    color: C.muted,
    fontSize: 12,
    pointerEvents: "none",
  },
  notice: {
    position: "absolute",
    bottom: 8,
    left: "50%",
    transform: "translateX(-50%)",
    padding: "3px 8px",
    background: C.panel,
    border: `1px solid ${C.line}`,
    color: C.text,
    fontSize: 11,
    whiteSpace: "nowrap",
    pointerEvents: "none",
  },
  legend: {
    position: "absolute",
    bottom: 8,
    right: 8,
    padding: "4px 6px",
    background: C.panel,
    border: `1px solid ${C.line}`,
    color: C.muted,
    fontSize: 10,
    pointerEvents: "none",
  },
  keys: {
    position: "absolute",
    top: 30,
    right: 6,
    margin: 0,
    padding: "6px 8px",
    background: C.panel,
    border: `1px solid ${C.line}`,
    color: C.text,
    fontSize: 11,
    lineHeight: 1.5,
    listStyle: "none",
    pointerEvents: "none",
  },
};

const ALL_SLOTS: SlotKey[] = ["a", "b", "plateEdges", "candEdges", "heat"];
const message = (e: unknown) => (e instanceof Error ? e.message : String(e));
const devicePixelRatioNow = () => (typeof window !== "undefined" && window.devicePixelRatio) || 1;

function deGradient(): string {
  return `linear-gradient(to right, ${DE00_STOPS.map(([v, c]) => `${c} ${(v / DE00_MAX) * 100}%`).join(", ")})`;
}

/** Which modes the given sources allow. */
function capsOf(p: ViewerProps): Caps {
  return {
    hasB: p.b !== undefined,
    hasEdges: Boolean(p.overlays?.edges?.plate || p.overlays?.edges?.candidate),
    hasHeat: p.overlays?.heatmap !== undefined,
    hasTiles: (p.overlays?.tiles?.length ?? 0) > 0,
  };
}

/** Load one source onto the GPU whenever it (or the renderer) changes; aborts stale loads. */
function useGpuSource<T>(
  renderer: ViewerRenderer | null,
  key: SlotKey,
  load: ((signal: AbortSignal) => Promise<T>) | null,
  apply: (r: ViewerRenderer, value: T | null) => void,
  setStatus: (key: SlotKey, s: LoadStatus | null) => void,
  deps: readonly unknown[],
): void {
  useEffect(() => {
    if (!renderer) return;
    if (!load) {
      apply(renderer, null);
      setStatus(key, null);
      return;
    }
    const ctl = new AbortController();
    setStatus(key, { state: "loading" });
    load(ctl.signal).then(
      (value) => {
        if (ctl.signal.aborted) return;
        try {
          apply(renderer, value);
          setStatus(key, { state: "ready" });
        } catch (e) {
          setStatus(key, { state: "error", message: message(e) });
        }
      },
      (e) => {
        if (!ctl.signal.aborted) setStatus(key, { state: "error", message: message(e) });
      },
    );
    return () => ctl.abort();
  }, [renderer, ...deps]); // deps name the source identity; the closures are rebuilt every render
}

export function Viewer(props: ViewerProps): ReactElement {
  const { a, b, overlays, diffGain = DEFAULT_GAIN, onChange, ref } = props;
  const rootRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [renderer, setRenderer] = useState<ViewerRenderer | null>(null);
  const [gpu, setGpu] = useState<GpuState>({ status: "idle" });
  const [status, setStatusMap] = useState<Partial<Record<SlotKey, LoadStatus>>>({});
  const [focused, setFocused] = useState(false);
  const [showKeys, setShowKeys] = useState(false);
  const [grabbing, setGrabbing] = useState(false);
  const [state, dispatch] = useReducer(viewerReducer, props, (p) =>
    initialViewState({
      mode: p.mode,
      wipe: p.wipe,
      zoom: p.zoom,
      pan: p.pan,
      image: p.a ? { width: p.a.width, height: p.a.height } : undefined,
      caps: capsOf(p),
    }),
  );
  const drag = useRef<{ kind: "pan" | "wipe"; x: number; y: number } | null>(null);
  const rendererRef = useRef(renderer);
  rendererRef.current = renderer;
  const stateRef = useRef(state);
  stateRef.current = state;
  const statusRef = useRef(status);
  statusRef.current = status;
  const gpuRef = useRef(gpu);
  gpuRef.current = gpu;
  const hasA = a !== undefined;

  const setStatus = (key: SlotKey, s: LoadStatus | null) =>
    setStatusMap((m) => {
      if (s === null) {
        if (!(key in m)) return m;
        const next = { ...m };
        delete next[key];
        return next;
      }
      return { ...m, [key]: s };
    });

  // GPU context: created only while there is an A image (browsers cap live WebGL contexts), rebuilt on restore.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!hasA || !canvas) return;
    let r: ViewerRenderer | null = null;
    const start = () => {
      try {
        r = ViewerRenderer.create(canvas);
        setGpu(r ? { status: "ok", float: r.caps.floatTargets } : { status: "unavailable" });
      } catch (e) {
        r = null;
        setGpu({ status: "error", message: message(e) });
      }
      setRenderer(r);
    };
    const onLost = (e: Event) => {
      e.preventDefault(); // lets the browser restore the context
      r = null;
      setRenderer(null);
      setGpu({ status: "lost" });
      setStatusMap({}); // everything is re-uploaded on restore
    };
    canvas.addEventListener("webglcontextlost", onLost);
    canvas.addEventListener("webglcontextrestored", start);
    start();
    return () => {
      canvas.removeEventListener("webglcontextlost", onLost);
      canvas.removeEventListener("webglcontextrestored", start);
      r?.dispose();
      setRenderer(null);
      setGpu({ status: "idle" });
      setStatusMap({});
    };
  }, [hasA]);

  useGpuSource(
    renderer,
    "a",
    a ? (signal) => loadImage(a, signal) : null,
    (r, px) => {
      r.setImage("a", px);
      if (px) dispatch({ type: "layout", image: { width: px.width, height: px.height } });
    },
    setStatus,
    [a?.url, a?.encoding, a?.width, a?.height, a?.data],
  );
  useGpuSource(
    renderer,
    "b",
    b ? (signal) => loadImage(b, signal) : null,
    (r, px) => r.setImage("b", px),
    setStatus,
    [b?.url, b?.encoding, b?.width, b?.height, b?.data],
  );
  const plateUrl = overlays?.edges?.plate;
  const candUrl = overlays?.edges?.candidate;
  useGpuSource(
    renderer,
    "plateEdges",
    plateUrl ? (signal) => loadMask(plateUrl, signal) : null,
    (r, px) => r.setMask("plateEdges", px),
    setStatus,
    [plateUrl],
  );
  useGpuSource(
    renderer,
    "candEdges",
    candUrl ? (signal) => loadMask(candUrl, signal) : null,
    (r, px) => r.setMask("candEdges", px),
    setStatus,
    [candUrl],
  );
  const heat = overlays?.heatmap;
  useGpuSource(
    renderer,
    "heat",
    heat ? (signal) => loadHeat(heat, signal) : null,
    (r, px) => r.setHeat(px),
    setStatus,
    [heat?.url, heat?.width, heat?.height, heat?.data],
  );

  // Declared size until the pixels arrive (an 8-bit image reports its decoded size on load).
  useEffect(() => {
    if (a) dispatch({ type: "layout", image: { width: a.width, height: a.height } });
  }, [a?.width, a?.height]);

  const tilePoints = overlays?.tiles;
  const caps = capsOf(props);
  useEffect(() => {
    dispatch({ type: "caps", caps });
  }, [caps.hasB, caps.hasEdges, caps.hasHeat, caps.hasTiles]); // caps is rebuilt each render from these flags

  // Props re-sync the state when they change (on the first run they repeat the initial values: the reducer no-ops).
  useEffect(() => {
    if (props.mode !== undefined) dispatch({ type: "sync", patch: { mode: props.mode } });
  }, [props.mode]);
  useEffect(() => {
    if (props.wipe !== undefined) dispatch({ type: "sync", patch: { wipe: props.wipe } });
  }, [props.wipe]);
  useEffect(() => {
    if (props.zoom !== undefined) dispatch({ type: "sync", patch: { zoom: props.zoom } });
  }, [props.zoom]);
  const panX = props.pan?.x;
  const panY = props.pan?.y;
  const panNull = props.pan === null;
  useEffect(() => {
    if (panNull) dispatch({ type: "sync", patch: { pan: null } });
    else if (panX !== undefined && panY !== undefined) dispatch({ type: "sync", patch: { pan: { x: panX, y: panY } } });
  }, [panX, panY, panNull]);

  // Report user changes once each.
  const reported = useRef<string | null>(null);
  useEffect(() => {
    const change: ViewerChange = {
      mode: state.mode,
      wipe: state.wipe,
      zoom: state.zoom,
      pan: state.pan,
      showB: state.showB,
    };
    const key = JSON.stringify(change);
    if (reported.current === null) {
      reported.current = key;
      return;
    }
    if (key !== reported.current) {
      reported.current = key;
      onChange?.(change);
    }
  }, [state.mode, state.wipe, state.zoom, state.pan, state.showB, onChange]);

  // Viewport size in device pixels; the canvas backing store follows it.
  useEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const measure = () => {
      const rect = el.getBoundingClientRect();
      const dpr = devicePixelRatioNow();
      const width = Math.max(1, Math.round(rect.width * dpr));
      const height = Math.max(1, Math.round(rect.height * dpr));
      const canvas = canvasRef.current;
      if (canvas && (canvas.width !== width || canvas.height !== height)) {
        canvas.width = width;
        canvas.height = height;
      }
      const vp = stateRef.current.viewport;
      if (vp.width !== width || vp.height !== height) dispatch({ type: "layout", viewport: { width, height } });
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [hasA]);

  // The wheel listener must be non-passive to keep the page from scrolling while zooming.
  useEffect(() => {
    const el = rootRef.current;
    if (!el || !hasA) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      const dpr = devicePixelRatioNow();
      const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaMode === 2 ? e.deltaY * rect.height : e.deltaY;
      const at = { x: (e.clientX - rect.left) * dpr, y: (e.clientY - rect.top) * dpr };
      dispatch({ type: "zoomBy", factor: Math.exp(-dy * 0.002), at });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [hasA]);

  useEffect(() => {
    if (state.notice === null) return;
    const t = setTimeout(() => dispatch({ type: "clearNotice" }), 2500);
    return () => clearTimeout(t);
  }, [state.notice]);

  const mode = displayedMode(state);
  const view = viewOf(state);
  const scale = scaleOf(state);

  // Tiles fill the viewport below the HUD band, so the chips never sit on a tile.
  const tiles: TileCell[] = useMemo(() => {
    if (!tilePoints?.length || state.viewport.width <= 1 || state.image.width <= 0) return [];
    const pair = state.caps.hasB;
    const band = Math.round(HUD_BAND * devicePixelRatioNow());
    const area = { width: state.viewport.width, height: Math.max(1, state.viewport.height - band) };
    const size = fitTileSize(tilePoints.length, area, pair);
    return tileGrid(tileLayout(tilePoints, size, state.image), area, pair).map((c) => ({ ...c, y: c.y + band }));
  }, [tilePoints, state.viewport, state.image, state.caps.hasB]);

  // Draw after every render: state, uploads and size all re-render.
  useEffect(() => {
    if (!renderer) return;
    const id = requestAnimationFrame(() => {
      renderer.draw({
        target: state.viewport,
        mode,
        view,
        wipeX: state.wipe * state.viewport.width,
        gain: diffGain,
        tiles,
      });
    });
    return () => cancelAnimationFrame(id);
  });

  const handle = useMemo<ViewerHandle>(
    () => ({
      readAnalysis: (kind, gain = 1) => rendererRef.current?.readAnalysis(kind, gain) ?? null,
      ready: (slot = "a") => statusRef.current[slot]?.state === "ready",
      gpu: () => {
        const g = gpuRef.current;
        return g.status === "ok" ? (g.float ? "ok" : "8-bit") : g.status;
      },
      state: () => stateRef.current,
    }),
    [],
  );
  useImperativeHandle(ref, () => handle, [handle]);
  // The browser smoke (README.md) reaches the handle through the element: no globals.
  useEffect(() => {
    const el = rootRef.current as (HTMLDivElement & { plateViewer?: ViewerHandle }) | null;
    if (!el) return;
    el.plateViewer = handle;
    return () => {
      delete el.plateViewer;
    };
  }, [handle]);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "?") {
      setShowKeys((v) => !v);
      e.preventDefault();
      return;
    }
    if (!hasA) return;
    const act = keyToAction(e);
    if (!act) return;
    e.preventDefault();
    dispatch(act);
  };

  const localX = (e: PointerEvent<HTMLDivElement>) => e.clientX - e.currentTarget.getBoundingClientRect().left;

  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || !hasA) return;
    e.currentTarget.focus();
    const width = e.currentTarget.getBoundingClientRect().width;
    const onLine = mode === "wipe" && Math.abs(localX(e) - state.wipe * width) <= 8;
    drag.current = { kind: onLine ? "wipe" : "pan", x: e.clientX, y: e.clientY };
    setGrabbing(!onLine);
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };

  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d) return;
    if (d.kind === "wipe") {
      const width = e.currentTarget.getBoundingClientRect().width;
      if (width > 0) dispatch({ type: "setWipe", wipe: localX(e) / width });
      return;
    }
    const dpr = devicePixelRatioNow();
    dispatch({ type: "pan", dx: -(e.clientX - d.x) * dpr, dy: -(e.clientY - d.y) * dpr });
    d.x = e.clientX;
    d.y = e.clientY;
  };

  const endDrag = (e: PointerEvent<HTMLDivElement>) => {
    drag.current = null;
    setGrabbing(false);
    if (e.currentTarget.hasPointerCapture?.(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
  };

  const onDoubleClick = (e: MouseEvent<HTMLDivElement>) => {
    if (!hasA || mode === "tiles") return;
    if (state.zoom !== "fit") {
      dispatch({ type: "fit" });
      return;
    }
    const rect = e.currentTarget.getBoundingClientRect();
    const dpr = devicePixelRatioNow();
    const at = { x: (e.clientX - rect.left) * dpr, y: (e.clientY - rect.top) * dpr };
    dispatch({ type: "zoomBy", factor: 1 / scale, at });
  };

  const labelA = a?.label ?? "A";
  const labelB = b?.label ?? "B";
  const modeText =
    mode === "a"
      ? labelA
      : mode === "b"
        ? labelB
        : mode === "diff"
          ? `Diff ×${diffGain}`
          : mode === "wipe"
            ? `${labelA} | ${labelB}`
            : MODE_LABEL[mode];
  const zoomText = mode === "tiles" ? "100%" : `${Math.round(scale * 1000) / 10}%${state.zoom === "fit" ? " fit" : ""}`;
  const sizeMismatch = a && b && (a.width !== b.width || a.height !== b.height);
  const errors = ALL_SLOTS.flatMap((k) => {
    const s = status[k];
    return s?.state === "error" ? [`${k}: ${s.message}`] : [];
  });
  const loading = ALL_SLOTS.filter((k) => status[k]?.state === "loading");
  const dpr = devicePixelRatioNow();

  let centreText: string | null = null;
  if (!a) centreText = "No image selected";
  else if (gpu.status === "unavailable") centreText = "WebGL2 unavailable: the viewer needs a WebGL2 browser";
  else if (gpu.status === "lost") centreText = "GPU context lost; waiting for the browser to restore it";
  else if (gpu.status === "error") centreText = `Viewer failed to start: ${gpu.message}`;
  else if (status.a?.state === "loading") centreText = `Loading ${labelA}…`;

  const rootStyle: CSSProperties = {
    position: "relative",
    width: "100%",
    aspectRatio: a ? `${a.width} / ${a.height}` : "16 / 9",
    minHeight: 120,
    overflow: "hidden",
    background: C.bg,
    color: C.text,
    outline: "none",
    border: `1px solid ${focused ? "#6b6f7d" : C.line}`,
    boxSizing: "border-box",
    cursor: !hasA ? "default" : grabbing ? "grabbing" : "crosshair",
    touchAction: "none",
    userSelect: "none",
    ...props.style,
  };

  return (
    <div
      ref={rootRef}
      className={props.className ? `plate-viewer ${props.className}` : "plate-viewer"}
      data-testid="viewer"
      data-mode={hasA ? mode : "empty"}
      data-gpu={gpu.status === "ok" ? (gpu.float ? "float" : "8-bit") : gpu.status}
      data-ready={ALL_SLOTS.filter((k) => status[k]?.state === "ready").join(" ")}
      role="group"
      aria-label={a ? `Image viewer, ${modeText}` : "Image viewer, no image"}
      aria-keyshortcuts="` [ ] 1 F D E H T ArrowLeft ArrowRight ArrowUp ArrowDown"
      tabIndex={0}
      style={rootStyle}
      onKeyDown={onKeyDown}
      onFocus={() => setFocused(true)}
      onBlur={() => setFocused(false)}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onDoubleClick={onDoubleClick}
    >
      {hasA ? <canvas ref={canvasRef} style={S.canvas} data-testid="viewer-canvas" /> : null}

      {mode === "wipe" && renderer ? (
        <div
          data-testid="viewer-wipe"
          style={{
            position: "absolute",
            top: 0,
            bottom: 0,
            left: `calc(${state.wipe * 100}% - 8px)`,
            width: 17,
            cursor: "ew-resize",
          }}
        >
          <div style={S.wipeLine} />
          <span style={{ ...S.chip, position: "absolute", bottom: 6, right: 14, fontSize: 10 }}>{labelA}</span>
          <span style={{ ...S.chip, position: "absolute", bottom: 6, left: 14, fontSize: 10 }}>{labelB}</span>
        </div>
      ) : null}

      {mode === "tiles" && renderer
        ? tiles.map((c) => (
            <span
              key={`${c.index}-${c.slot}`}
              style={{ ...S.chip, position: "absolute", left: c.x / dpr + 2, top: c.y / dpr + 2, fontSize: 10 }}
            >
              {c.index + 1} {c.slot === "a" ? labelA : labelB}
            </span>
          ))
        : null}

      {hasA ? (
        <div style={S.hud}>
          <span style={S.chip} data-testid="viewer-mode">
            {modeText}
          </span>
          <span style={S.chip} data-testid="viewer-zoom">
            {zoomText}
          </span>
          {gpu.status === "ok" && !gpu.float ? (
            <span
              style={{ ...S.chip, color: WARN, borderColor: WARN }}
              data-testid="badge-8bit"
              title="No float render targets (EXT_color_buffer_float): images are shown through RGBA8"
            >
              8-bit preview
            </span>
          ) : null}
          {sizeMismatch ? (
            <span style={{ ...S.chip, color: WARN, borderColor: WARN }} data-testid="badge-size">
              {labelB} {b.width}×{b.height} shown on {labelA} {a.width}×{a.height}
            </span>
          ) : null}
          {loading.length > 0 && status.a?.state !== "loading" ? (
            <span style={{ ...S.chip, color: C.muted }}>loading {loading.join(", ")}…</span>
          ) : null}
          <span style={{ flex: 1 }} />
          <span style={{ ...S.chip, color: C.muted }} aria-hidden="true">
            ?
          </span>
        </div>
      ) : null}

      {centreText ? (
        <div style={S.centre} data-testid="viewer-message">
          {centreText}
        </div>
      ) : null}

      {errors.length > 0 ? (
        <div style={{ ...S.centre, alignItems: "flex-end", color: ERR }} role="alert" data-testid="viewer-error">
          {errors.join(" · ")}
        </div>
      ) : null}

      {mode === "de00" && renderer ? (
        <div style={S.legend} data-testid="viewer-legend">
          <div style={{ width: 160, height: 8, background: deGradient() }} />
          <div style={{ position: "relative", height: 12, width: 160 }}>
            {[0, 1, 2, 5, 10].map((v) => (
              <span key={v} style={{ ...S.tick, left: `${(v / DE00_MAX) * 100}%` }}>
                {v}
              </span>
            ))}
          </div>
          <div>ΔE00 (≥ 10 saturates)</div>
        </div>
      ) : null}

      {mode === "edges" && renderer ? (
        <div style={S.legend} data-testid="viewer-legend">
          <span style={{ color: EDGE_COLORS.plate }}>■</span> plate G-buffer{" "}
          <span style={{ color: EDGE_COLORS.candidate }}>■</span> candidate{" "}
          <span style={{ color: EDGE_COLORS.both }}>■</span> both
        </div>
      ) : null}

      {state.notice ? (
        <div style={S.notice} role="status" data-testid="viewer-notice">
          {state.notice}
        </div>
      ) : null}

      {showKeys ? (
        <ul style={S.keys} data-testid="viewer-keys">
          {KEY_LIST.map(([k, what]) => (
            <li key={k}>
              <b style={{ display: "inline-block", minWidth: 48 }}>{k}</b>
              {what}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
