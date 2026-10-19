// Panel state shared across panels: the project's runs, the selected candidate run, its scrub progress and logged
// scrub, the verdicts given, the takes accepted, and the viewer request ("Open tiles"). The graph itself stays in
// `useGraphStore`; this store holds only what the panels need to talk to each other and to the viewer.
//
// Accept (doctrine 7): `acceptRun` needs the run's logged full scrub, posts `/runs/{id}/accept` with it, then marks
// the producing node approved on the canvas and saves the graph, so the daemon (which owns `approved`) and the
// canvas rules both see the accepted take.
//
// Strips from history (T160, decision 93): once `loadRuns` has the project's runs, each generating node on the
// canvas gets its newest Make back in its candidate strip (the rows of the newest row's best-of-N group, else that
// row alone), read in full with `GET /runs/{id}` because a list row carries no outputs or gate checks; the runs still
// in flight are watched. A node whose read fails is skipped and the rest go on; the pass logs once.

import { create } from "zustand";
import { watchRun } from "../canvas/runEvents";
import { acceptRun as postAccept, getGateReport, getRun, UI_ACTOR } from "../canvas/runApi";
import { type CandidateRun, holdsLiveRun, MAX_CANDIDATES, useRunStore } from "../canvas/runStore";
import { isTerminal } from "../canvas/runTypes";
import { GENERATING_KINDS, type GraphNode } from "../canvasRules";
import { useGraphStore } from "../store";
import type { Point, Size, ViewerMode } from "../viewer/types";
import { errorMessage, listRuns, postScrub, postVerdict } from "./api";
import {
  EMPTY_SCRUB,
  isFullScrub,
  observeView,
  quadrantsVisited,
  type ScrubProgress,
  type ViewSample,
} from "./scrub";
import type { Restore } from "./subgraph";
import type { ResolveResult, RunDetail, RunRow, VerdictName } from "./types";

/** The panel tabs; `spend` and `settings` are T102's. */
export type PanelTab = "project" | "history" | "gate" | "custody" | "timer" | "spend" | "settings";

export const RATER_KEY = "plate.rater";
export const DEFAULT_RATER = "human";

/** What the viewer is asked to show. `ConnectedViewer` feeds user changes back so this always names the live mode. */
export interface ViewerRequest {
  mode: ViewerMode;
  tiles: Point[];
}

/** Drop-to-restore: a dropped image is resolved to the run that made it; the producing subgraph is offered back. */
export type DropState =
  | { kind: "idle" }
  | { kind: "resolving"; name: string }
  | { kind: "needs-path"; name: string }
  | { kind: "match"; result: ResolveResult; run: RunDetail | null; plan: Restore | null; note?: string }
  | { kind: "error"; message: string };

export type Outcome = { ok: true; message: string } | { ok: false; message: string };

function readRater(): string {
  try {
    return window.localStorage.getItem(RATER_KEY) || DEFAULT_RATER;
  } catch {
    return DEFAULT_RATER;
  }
}

export interface PanelsState {
  tab: PanelTab;
  runs: RunRow[];
  runsProjectId: string | null;
  runsLoading: boolean;
  runsError: string | null;
  selectedRunId: string | null;
  scrub: ScrubProgress;
  /** run id -> scrub_log_id returned by the daemon. */
  scrubLogs: Record<string, string>;
  verdicts: Record<string, VerdictName>;
  /** run id -> true once the daemon accepted the take. */
  accepted: Record<string, true>;
  viewer: ViewerRequest;
  rater: string;
  /** The last thing a key or button did, shown under the verdict keys. */
  outcome: Outcome | null;
  drop: DropState;
  setTab: (tab: PanelTab) => void;
  loadRuns: (projectId: string | null) => Promise<void>;
  selectRun: (runId: string | null) => void;
  /** Fold a viewer change into the scrub progress of the selected run. */
  observeViewer: (sample: ViewSample, image: Size, viewport: Size) => void;
  /** Log the full scrub for the selected run with the daemon. Refused until all four quadrants were seen at 100%. */
  logScrub: () => Promise<Outcome>;
  /** Record a verdict for the selected run. Refused without a logged full scrub. */
  recordVerdict: (verdict: VerdictName, reasons: string[]) => Promise<Outcome>;
  /** Accept a take: refused here without its logged full scrub; on success its node is approved and the graph saved. */
  acceptRun: (runId: string) => Promise<Outcome>;
  setDrop: (drop: DropState) => void;
  openTiles: (points: Point[]) => void;
  setViewerMode: (mode: ViewerMode) => void;
  setRater: (rater: string) => void;
  clearOutcome: () => void;
}

export function initialPanelsState(): Pick<
  PanelsState,
  | "tab"
  | "runs"
  | "runsProjectId"
  | "runsLoading"
  | "runsError"
  | "selectedRunId"
  | "scrub"
  | "scrubLogs"
  | "verdicts"
  | "accepted"
  | "viewer"
  | "rater"
  | "outcome"
  | "drop"
> {
  return {
    tab: "project",
    runs: [],
    runsProjectId: null,
    runsLoading: false,
    runsError: null,
    selectedRunId: null,
    scrub: EMPTY_SCRUB,
    scrubLogs: {},
    verdicts: {},
    accepted: {},
    viewer: { mode: "wipe", tiles: [] },
    rater: readRater(),
    outcome: null,
    drop: { kind: "idle" },
  };
}

const fail = (message: string): Outcome => ({ ok: false, message });
const done = (message: string): Outcome => ({ ok: true, message });

/** The canvas node that made a run: the daemon's answer, else the runs list, else the candidate strip. */
function producingNode(answer: { node_id?: string | null } | null, runId: string, runs: RunRow[]): string | null {
  return answer?.node_id ?? runs.find((r) => r.id === runId)?.node_id ?? useRunStore.getState().candidates[runId]?.nodeId ?? null;
}

// ---------------------------------------------------------------------------- strips from history (T160)

/** The best-of-N group a listed run belongs to: `quote.sibling_group`, else the run's own `sibling_group`. */
function groupOfRow(row: RunRow): string | null {
  const r = row as RunRow & { sibling_group?: unknown; quote?: { sibling_group?: unknown } | null };
  const g = r.quote?.sibling_group ?? r.sibling_group;
  return typeof g === "string" && g !== "" ? g : null;
}

const createdAt = (row: RunRow): number => {
  const t = typeof row.created_utc === "string" ? Date.parse(row.created_utc) : Number.NaN;
  return Number.isFinite(t) ? t : Number.NEGATIVE_INFINITY;
};

/**
 * Per generating node of `nodes`, the listed rows of its newest Make: the rows sharing the newest row's
 * `quote.sibling_group`, else the newest row alone, at most `MAX_CANDIDATES`, newest first. Rows for other nodes
 * (a Plate, a Comp, a node no longer on the canvas) are left out.
 */
export function newestMakes(rows: readonly RunRow[], nodes: readonly GraphNode[]): Map<string, RunRow[]> {
  const generating = new Set(nodes.filter((n) => GENERATING_KINDS.has(n.data.kind)).map((n) => n.id));
  // newest first by created_utc; the daemon's own order (newest first) breaks ties
  const ordered = rows
    .map((row, i) => ({ row, i }))
    .sort((a, b) => createdAt(b.row) - createdAt(a.row) || a.i - b.i)
    .map((x) => x.row);
  const byNode = new Map<string, RunRow[]>();
  for (const row of ordered) {
    const nodeId = row.node_id;
    if (!nodeId || !generating.has(nodeId)) continue;
    const list = byNode.get(nodeId) ?? [];
    list.push(row);
    byNode.set(nodeId, list);
  }
  const out = new Map<string, RunRow[]>();
  for (const [nodeId, list] of byNode) {
    const group = groupOfRow(list[0]);
    const make = group === null ? [list[0]] : list.filter((r) => groupOfRow(r) === group);
    out.set(nodeId, make.slice(0, MAX_CANDIDATES));
  }
  return out;
}

/** A run as the strip needs it: `GET /runs/{id}`, plus `/gate` for a GATE_FAIL whose report carries no checks. */
async function readCandidate(runId: string): Promise<CandidateRun> {
  const run: CandidateRun = await getRun(runId);
  if (run.state === "GATE_FAIL" && !run.gate_report?.checks?.length) {
    try {
      return { ...run, gate_report: await getGateReport(runId) };
    } catch {
      // no report route: the chip says GATE_FAIL without the failing metric
    }
  }
  return run;
}

/**
 * Fill the strips of the open project's generating nodes from `rows` (T160). `current` says whether the project is
 * still the one open; once it is not, nothing more is written. Never throws.
 */
async function hydrateStrips(projectId: string, rows: readonly RunRow[], current: () => boolean): Promise<void> {
  if (!current()) return;
  const makes = newestMakes(rows, useGraphStore.getState().nodes);
  const failed: string[] = [];
  await Promise.all(
    [...makes].map(async ([nodeId, make]) => {
      try {
        if (holdsLiveRun(useRunStore.getState(), nodeId)) return; // this session's Make is still in flight
        const runs = await Promise.all(make.map((row) => readCandidate(row.id)));
        if (!current()) return;
        useRunStore.getState().hydrate(nodeId, runs);
        const held = useRunStore.getState().byNode[nodeId]?.runIds ?? [];
        for (const run of runs) {
          const cand = useRunStore.getState().candidates[run.id];
          if (held.includes(run.id) && cand && !isTerminal(cand.state)) watchRun(run.id, nodeId, projectId);
        }
      } catch (err) {
        failed.push(`${nodeId}: ${errorMessage(err)}`);
      }
    }),
  );
  if (failed.length > 0) {
    console.warn(`candidate strips: ${failed.length} node(s) not restored from history (${failed.join("; ")})`);
  }
}

/** After the daemon accepted: approve the producing node with the scrub that allowed it, then save the graph. */
async function approveOnCanvas(nodeId: string, scrubLogId: string): Promise<Outcome> {
  useGraphStore.getState().setNodeStatus(nodeId, { approved: true, approvedBy: UI_ACTOR, scrubLogId }, { id: scrubLogId, fullScrub: true });
  const graph = useGraphStore.getState();
  if (graph.nodes.find((n) => n.id === nodeId)?.data.approved !== true) {
    return fail(`take accepted, but the canvas did not mark it: ${graph.lastRefusal ?? `no node ${nodeId}`}`);
  }
  await graph.saveNow();
  const saveError = useGraphStore.getState().saveError;
  return saveError ? fail(`take accepted, but the graph was not saved: ${saveError.message}`) : done("take accepted");
}

export const usePanelsStore = create<PanelsState>()((set, get) => ({
  ...initialPanelsState(),
  setTab: (tab) => set({ tab }),
  loadRuns: async (projectId) => {
    if (!projectId) {
      set({ runs: [], runsProjectId: null, runsLoading: false, runsError: null });
      return;
    }
    set({ runsLoading: true, runsError: null, runsProjectId: projectId });
    let runs: RunRow[];
    try {
      runs = await listRuns(projectId);
    } catch (err) {
      if (get().runsProjectId !== projectId) return;
      set({ runs: [], runsLoading: false, runsError: errorMessage(err) });
      return;
    }
    if (get().runsProjectId !== projectId) return; // a newer project was opened meanwhile
    set({ runs, runsLoading: false });
    // the strips of the canvas the runs belong to, from their newest Makes (T160)
    await hydrateStrips(
      projectId,
      runs,
      () => get().runsProjectId === projectId && useGraphStore.getState().projectId === projectId,
    );
  },
  selectRun: (runId) =>
    set((s) => (s.selectedRunId === runId ? s : { selectedRunId: runId, scrub: EMPTY_SCRUB, outcome: null })),
  observeViewer: (sample, image, viewport) =>
    set((s) => {
      if (!s.selectedRunId) return s;
      const next = observeView(s.scrub, sample, image, viewport);
      return next === s.scrub ? s : { scrub: next };
    }),
  logScrub: async () => {
    const { selectedRunId: runId, scrub, scrubLogs } = get();
    if (!runId) return fail("select a candidate first");
    if (scrubLogs[runId]) return done("scrub already logged");
    if (!isFullScrub(scrub)) return fail("inspect all four quadrants at 100% first");
    try {
      const id = await postScrub(runId, { quadrants_visited: quadrantsVisited(scrub), zoom_100: true });
      set((s) => ({ scrubLogs: { ...s.scrubLogs, [runId]: id } }));
      const out = done("full scrub logged");
      set({ outcome: out });
      return out;
    } catch (err) {
      const out = fail(`scrub not logged: ${errorMessage(err)}`);
      set({ outcome: out });
      return out;
    }
  },
  recordVerdict: async (verdict, reasons) => {
    const { selectedRunId: runId, scrubLogs, rater } = get();
    let out: Outcome;
    const scrubLogId = runId ? scrubLogs[runId] : undefined;
    if (!runId) {
      out = fail("select a candidate first");
    } else if (!scrubLogId) {
      out = fail("a full scrub must be logged before a verdict");
    } else {
      try {
        await postVerdict(runId, { verdict, reasons, scrub_log_id: scrubLogId, rater });
        set((s) => ({ verdicts: { ...s.verdicts, [runId]: verdict } }));
        out = done(`${VERDICT_LABEL[verdict]} recorded`);
      } catch (err) {
        out = fail(`verdict not recorded: ${errorMessage(err)}`);
      }
    }
    set({ outcome: out });
    return out;
  },
  acceptRun: async (runId) => {
    const scrubLogId = get().scrubLogs[runId];
    let out: Outcome;
    if (!scrubLogId) {
      out = fail("a full scrub must be logged before the take is accepted");
    } else {
      try {
        const answer = await postAccept(runId, scrubLogId, UI_ACTOR);
        set((s) => ({ accepted: { ...s.accepted, [runId]: true } }));
        const nodeId = producingNode(answer, runId, get().runs);
        // Since G2 (T111) the daemon derives a node's approval from the run behind its picked take, so the
        // accepted take must also become the picked one, or the node stays unapproved after the save.
        if (nodeId) useRunStore.getState().pick(nodeId, runId);
        out = nodeId ? await approveOnCanvas(nodeId, scrubLogId) : done("take accepted (its node is not on this canvas)");
      } catch (err) {
        out = fail(`take not accepted: ${errorMessage(err)}`);
      }
    }
    set({ outcome: out });
    return out;
  },
  setDrop: (drop) => set({ drop }),
  openTiles: (points) => set({ viewer: { mode: "tiles", tiles: points } }),
  setViewerMode: (mode) => set((s) => (s.viewer.mode === mode ? s : { viewer: { ...s.viewer, mode } })),
  setRater: (rater) => {
    try {
      window.localStorage.setItem(RATER_KEY, rater);
    } catch {
      // private mode: the rater lasts for this tab only
    }
    set({ rater });
  },
  clearOutcome: () => set({ outcome: null }),
}));

export const VERDICT_LABEL: Record<VerdictName, string> = { ship: "Ship", fix: "Fix", render_it: "Render-it" };

/** The scrub the graph store's `canAccept` wants: `{id, fullScrub: true}` once the daemon logged it, else null. */
export function scrubLogFor(runId: string | null): { id: string; fullScrub: true } | null {
  if (!runId) return null;
  const id = usePanelsStore.getState().scrubLogs[runId];
  return id ? { id, fullScrub: true } : null;
}
