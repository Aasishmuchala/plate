// <Viewer> wired to the panels: the mode and the gate-flagged tiles come from the panels store ("Open tiles" in the
// gate panel), and every zoom and pan the user makes is folded into the scrub progress of the selected candidate.
// The viewer does not emit a scrub event of its own, so this wrapper derives it from `onChange`, which is the
// "viewer reports the user visited 100% at 4 quadrants" the verdict panel waits for. Mount this instead of a bare
// <Viewer> wherever a candidate is compared with its plate.
//
// T109: the gate overlays. At gate time the daemon stores the G-buffer edge masks edge recall scored and the per-pixel
// ΔE00 map in the project CAS and lists them in `GET /runs/{id}/gate` as `overlays` (`plate_daemon.gating`);
// `gateOverlays` maps that block onto the viewer's `edges` (8-bit PNG URLs, white = edge) and `heatmap` (raw R16F at
// the output's size), so `e` and `h` draw on a real daemon. A parent that knows the pair passes `overlays` (the compare
// slot does); otherwise the selected run's are read here. A run gated before T109 has no block: the viewer keeps its
// "no edge masks for this view" / "no ΔE00 map for this view" notices.

import { type ReactElement, useCallback, useEffect, useRef } from "react";
import { assetUrl } from "../canvas/assets";
import { useRunStore } from "../canvas/runStore";
import { type Overlays, Viewer, type ViewerChange, type ViewerProps } from "../viewer";
import { getGateReport } from "./api";
import { usePanelsStore } from "./store";
import type { ViewSample } from "./scrub";
import { useLoad } from "./useLoad";

/** The gate's share of the viewer overlays: edge masks and the ΔE00 map (the tiles come from the panels store). */
export type GateOverlays = Omit<Overlays, "tiles">;

export type ConnectedViewerProps = Omit<ViewerProps, "mode" | "onChange" | "overlays" | "ref"> & {
  overlays?: GateOverlays;
};

/** Formats of `plate_daemon.gating`'s overlay entries this viewer draws. */
export const EDGE_FORMAT = "png8";
export const DE00_FORMAT = "r16f";

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const isSize = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v) && v > 0;

function entryHash(entry: unknown, format: string): string | null {
  if (!isRecord(entry) || entry.format !== format) return null;
  return typeof entry.hash === "string" && entry.hash.length > 0 ? entry.hash : null;
}

/**
 * `GET /runs/{id}/gate`'s `overlays` block (else the report's `metrics.overlays`) as viewer inputs, every URL on the
 * token-gated `GET /assets/<hash>`. Undefined when the report has none (gated before T109, or the store failed) or
 * names nothing this viewer draws; a ΔE00 map is taken only in dE00 units (`scale` 1) with a positive size.
 */
export function gateOverlays(body: unknown): GateOverlays | undefined {
  if (!isRecord(body)) return undefined;
  const metrics = isRecord(body.metrics) ? body.metrics : null;
  const lifted = isRecord(body.overlays) ? body.overlays : null;
  const block = lifted ?? (metrics && isRecord(metrics.overlays) ? metrics.overlays : null);
  if (!block) return undefined;
  const out: GateOverlays = {};
  const plate = entryHash(block.edges_plate, EDGE_FORMAT);
  const candidate = entryHash(block.edges_candidate, EDGE_FORMAT);
  if (plate || candidate) {
    out.edges = {};
    if (plate) out.edges.plate = assetUrl(plate);
    if (candidate) out.edges.candidate = assetUrl(candidate);
  }
  const de = block.de00_map;
  const heat = entryHash(de, DE00_FORMAT);
  if (heat && isRecord(de) && isSize(de.width) && isSize(de.height) && (de.scale === undefined || de.scale === 1)) {
    out.heatmap = { url: assetUrl(heat), width: de.width, height: de.height };
  }
  return out.edges || out.heatmap ? out : undefined;
}

/**
 * The gate overlays of `runId` (null loads nothing). `refresh` joins the load key: a run picked while it was still
 * going is read again once its state moves, since its report exists only after GATING. An answer for another run is
 * never returned, so a quick switch cannot draw one take's edges over the next.
 */
export function useGateOverlays(runId: string | null, refresh?: string | null): GateOverlays | undefined {
  const { data } = useLoad(runId ? `${runId}#${refresh ?? ""}` : null, async () => {
    const view = await getGateReport(runId as string);
    return { runId, overlays: view ? gateOverlays(view.report) : undefined };
  });
  return data && data.runId === runId ? data.overlays : undefined;
}

export function ConnectedViewer(props: ConnectedViewerProps): ReactElement {
  const { a, zoom, pan } = props;
  const mode = usePanelsStore((s) => s.viewer.mode);
  const tiles = usePanelsStore((s) => s.viewer.tiles);
  const selectedRunId = usePanelsStore((s) => s.selectedRunId);
  const observe = usePanelsStore((s) => s.observeViewer);
  const setViewerMode = usePanelsStore((s) => s.setViewerMode);
  const candidateState = useRunStore((s) => (selectedRunId ? s.candidates[selectedRunId]?.state : undefined));
  const rowState = usePanelsStore((s) => s.runs.find((r) => r.id === selectedRunId)?.state);
  const own = useGateOverlays(props.overlays === undefined ? selectedRunId : null, candidateState ?? rowState);
  const overlays = props.overlays ?? own;
  const box = useRef<HTMLDivElement>(null);

  const sample = useCallback(
    (view: ViewSample) => {
      if (!a) return;
      const el = box.current?.querySelector('[data-testid="viewer"]') ?? box.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      observe(view, { width: a.width, height: a.height }, { width: rect.width * dpr, height: rect.height * dpr });
    },
    [a, observe],
  );

  // The viewer reports only user changes, so the first view of each candidate is sampled here.
  useEffect(() => {
    sample({ zoom: zoom ?? "fit", pan: pan ?? null });
  }, [sample, selectedRunId, a?.url, zoom, pan]);

  const onChange = useCallback(
    (c: ViewerChange) => {
      setViewerMode(c.mode);
      sample({ zoom: c.zoom, pan: c.pan });
    },
    [sample, setViewerMode],
  );

  return (
    <div ref={box} data-testid="connected-viewer">
      <Viewer {...props} mode={mode} overlays={{ ...overlays, tiles }} onChange={onChange} />
    </div>
  );
}
