// The viewer slot (T35 wiring of T24's <Viewer> through T36's <ConnectedViewer>, which turns viewer changes into
// the scrub progress the verdict keys wait for and carries the gate panel's "Open tiles" into the viewer): clicking
// a candidate compares it (B) with the plate its node is anchored to (A). With no plate image known the candidate is
// shown alone. Before any click the viewer shows its empty frame.
//
// T101: either side can be any plate, run or node output (Set as A / Set as B in the strip, shift-click in History,
// T98's context menu). Only a candidate against its own plate, while it is the panels' selected run, goes through
// <ConnectedViewer>; any other pairing is for looking only, so a plain <Viewer> shows it and the verdict panel says
// what the scrub needs. With nothing compared, a selected node's output (a Plate's beauty) is shown alone. The HUD's
// Enlarge and Fullscreen buttons drive `enlarged` in the run store; App moves this slot over the canvas.
//
// T109: a candidate against its own plate also gets the gate's overlays (`GET /runs/{id}/gate` -> `overlays`: the
// G-buffer edge masks and the ΔE00 map the daemon stored at gate time), mapped by `gateOverlays` onto the viewer's
// `edges` and `heatmap`, so `e` and `h` draw. They describe that pair only: any other pairing has none, and neither has
// a run gated before T109, where the viewer says "no edge masks for this view".

import { type CSSProperties, useCallback, useEffect, useLayoutEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useShallow } from "zustand/react/shallow";
import type { EdgeLike, GraphNode } from "../canvasRules";
import { ConnectedViewer, type GateOverlays, useGateOverlays } from "../panels/ConnectedViewer";
import { usePanelsStore } from "../panels/store";
import type { RunRow } from "../panels/types";
import { useGraphStore } from "../store";
import type { ImageSource } from "../viewer";
import { toggleFullscreen, Viewer, type ViewerChange, type ViewerProps } from "../viewer/Viewer";
import { assetImageSource } from "./assets";
import { plateAncestor } from "./derive";
import { type Candidate, nodeCandidates, type Side, useRunStore } from "./runStore";

/** What the verdict panel says while a pair other than a candidate and its plate is open. */
export const SCRUB_HINT = "compare a candidate with its plate to scrub";

/** The viewer fills the enlarged stage (a column flex box, enlarge.css) instead of keeping the image's aspect ratio. */
const FILL: CSSProperties = { flex: "1 1 0", aspectRatio: "auto", minHeight: 0 };

/** Handed to <ConnectedViewer> while a pair's overlays are absent, so it does not read the gate a second time. */
const NO_OVERLAYS: GateOverlays = {};

interface ComparePlan {
  a?: ImageSource;
  b?: ImageSource;
  caption: string | null;
  /** The view feeds the scrub of the panels' selected run: a candidate against its own plate. */
  bound: boolean;
  /** Why the scrub does not count on this view (a pair is open that is not a candidate and its plate). */
  hint: string | null;
  /** A pair is open (the head offers close). */
  open: boolean;
  /** The run whose gate overlays fit this view (a candidate against its own plate, both images known), and its state
   * (the overlays are read again when it moves: the report exists only after GATING). */
  overlayRun: { runId: string; state: string | null } | null;
}

interface Lookup {
  nodes: GraphNode[];
  edges: EdgeLike[];
  candidates: Record<string, Candidate>;
  runs: RunRow[];
}

/** A History row's first output (`outputs[0]`, else the raw result) when the strip never held the run. */
function rowOutput(runs: RunRow[], runId: string | undefined): string | null {
  const row = runs.find((r) => r.id === runId) as (RunRow & { outputs?: unknown }) | undefined;
  if (!row) return null;
  const outs = Array.isArray(row.outputs) ? row.outputs.filter((o): o is string => typeof o === "string") : [];
  return outs[0] ?? row.result_key ?? null;
}

/** The image a side names and the size it is registered at (candidates share their plate's size). */
function sideImage(side: Side, look: Lookup): ImageSource | undefined {
  const plate = side.nodeId ? plateAncestor(look.nodes, look.edges, side.nodeId) : null;
  let hash: string | null = null;
  let size = plate?.data.outputSize;
  if (side.kind === "plate") {
    hash = plate?.data.outputHash ?? null;
  } else if (side.kind === "run") {
    const cand = side.runId ? look.candidates[side.runId] : undefined;
    hash = cand?.outputHash ?? side.outputHash ?? rowOutput(look.runs, side.runId);
  } else {
    const node = look.nodes.find((n) => n.id === side.nodeId);
    hash = node?.data.outputHash ?? side.outputHash ?? null;
    size = node?.data.outputSize ?? size;
  }
  return hash ? assetImageSource(hash, size, side.label) : undefined;
}

function useComparePlan(): ComparePlan {
  const compare = useRunStore((s) => s.compare);
  const eligible = useRunStore((s) => s.scrubEligible);
  const candidates = useRunStore((s) => s.candidates);
  const nodeId = compare?.b?.nodeId ?? null;
  const cands = useRunStore(useShallow((s) => (nodeId ? nodeCandidates(s, nodeId) : [])));
  const nodes = useGraphStore((s) => s.nodes);
  const edges = useGraphStore((s) => s.edges);
  const runs = usePanelsStore((s) => s.runs);
  const selectedRunId = usePanelsStore((s) => s.selectedRunId);
  const look: Lookup = { nodes, edges, candidates, runs };

  if (!compare) {
    // nothing compared: the selected node's own output, alone
    const node = nodes.find((n) => n.selected);
    if (!node?.data.outputHash) return { caption: null, bound: false, hint: null, open: false, overlayRun: null };
    const label = node.data.label;
    const a = sideImage({ kind: "node", nodeId: node.id, label }, look);
    return { a, caption: `${label} (no comparison)`, bound: false, hint: null, open: false, overlayRun: null };
  }

  const { a: sideA, b: sideB } = compare;
  if (eligible && sideB) {
    // a candidate against its plate: the T35 captions, by the candidate's place in the strip
    const rank = cands.findIndex((c) => c.runId === sideB.runId);
    const cand = cands[rank];
    const name = cand ? `candidate ${rank + 1}` : sideB.label;
    const b = sideImage({ ...sideB, label: name }, look);
    const a = sideImage(sideA, look);
    const bound = selectedRunId === sideB.runId;
    const caption = !b
      ? `${name} has no output yet${cand ? ` (${cand.state})` : ""}`
      : a
        ? `A ${sideA.label} · B ${name}`
        : `${name} (no plate image on this graph to compare with)`;
    const runId = sideB.runId;
    const state = (runId ? (candidates[runId]?.state ?? runs.find((r) => r.id === runId)?.state) : null) ?? null;
    const overlayRun = a && b && runId ? { runId, state } : null;
    return a
      ? { a, b, caption, bound, hint: null, open: true, overlayRun }
      : { a: b, caption, bound, hint: null, open: true, overlayRun: null };
  }

  const a = sideImage(sideA, look);
  const b = sideB ? sideImage(sideB, look) : undefined;
  const missing = [!a ? "A" : null, sideB && !b ? "B" : null].filter(Boolean);
  const pair = sideB ? `A ${sideA.label} · B ${sideB.label}` : `A ${sideA.label} · set B to compare`;
  const caption = missing.length > 0 ? `${pair} (${missing.join(" and ")}: no image yet)` : pair;
  const lookOnly = { caption, bound: false, hint: SCRUB_HINT, open: true, overlayRun: null };
  return a ? { a, b, ...lookOnly } : { a: b, ...lookOnly };
}

/**
 * Keep the panels' selected run on the candidate of a plate-vs-candidate pair whenever the pair changes (a strip
 * row, a History row or T98's menu), so the scrub it feeds is that candidate's. App calls it once: it watches the
 * pair, not the selection, so selecting another run elsewhere is never undone.
 */
export function useBindCompareToPanels(): void {
  const compare = useRunStore((s) => s.compare);
  const eligible = useRunStore((s) => s.scrubEligible);
  useEffect(() => {
    const runId = compare?.b?.runId;
    if (eligible && runId) usePanelsStore.getState().selectRun(runId);
  }, [compare, eligible]);
}

/** The hint, inside the verdict panel when one is mounted (portalled like the status-bar alert), else in place. */
function ScrubHint({ hint }: { hint: string | null }) {
  const [panel, setPanel] = useState<Element | null>(null);
  useLayoutEffect(() => {
    const el = document.querySelector('[data-testid="verdict-panel"]');
    setPanel((cur) => (cur === el ? cur : el));
  });
  if (!hint) return null;
  const node = (
    <p className="scrub-hint" data-testid="scrub-hint" role="status">
      {hint}
    </p>
  );
  return panel ? createPortal(node, panel) : node;
}

/** A pair that is not a candidate and its plate: the panels' mode, never the scrub or the gate's tiles. */
function LookViewer(props: Omit<ViewerProps, "mode" | "onChange">) {
  const mode = usePanelsStore((s) => s.viewer.mode);
  const setViewerMode = usePanelsStore((s) => s.setViewerMode);
  const onChange = useCallback((c: ViewerChange) => setViewerMode(c.mode), [setViewerMode]);
  return <Viewer {...props} mode={mode} onChange={onChange} />;
}

export function CompareViewer({ fill = false }: { fill?: boolean }) {
  const plan = useComparePlan();
  const overlays = useGateOverlays(plan.overlayRun?.runId ?? null, plan.overlayRun?.state);
  const close = useRunStore((s) => s.closeCompare);
  const enlarged = useRunStore((s) => s.enlarged);
  const setEnlarged = useRunStore((s) => s.setEnlarged);
  const onEnlarge = useCallback(() => setEnlarged(!useRunStore.getState().enlarged), [setEnlarged]);
  const onFullscreen = useCallback(() => {
    setEnlarged(true);
    toggleFullscreen();
  }, [setEnlarged]);
  const shared = { a: plan.a, b: plan.b, enlarged, onEnlarge, onFullscreen, style: fill ? FILL : undefined };
  const own = plan.overlayRun ? overlays : undefined;
  return (
    <section className={fill ? "compare compare-fill" : "compare"} data-testid="compare" data-bound={plan.bound}>
      {plan.caption ? (
        <div className="compare-head">
          <span data-testid="compare-caption">{plan.caption}</span>
          {plan.open ? (
            <button type="button" className="btn btn-ghost" onClick={close}>
              close
            </button>
          ) : null}
        </div>
      ) : null}
      {plan.bound ? (
        <ConnectedViewer {...shared} overlays={own ?? NO_OVERLAYS} />
      ) : (
        <LookViewer {...shared} overlays={own} />
      )}
      <ScrubHint hint={plan.hint} />
    </section>
  );
}
