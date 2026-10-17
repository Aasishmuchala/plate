// Panel state shared across panels: the project's runs, the selected candidate run, its scrub progress and logged
// scrub, the verdicts given, and the viewer request ("Open tiles"). The graph itself stays in `useGraphStore`; this
// store holds only what the panels need to talk to each other and to the viewer.

import { create } from "zustand";
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

export type PanelTab = "project" | "history" | "gate" | "custody" | "timer";

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
    viewer: { mode: "wipe", tiles: [] },
    rater: readRater(),
    outcome: null,
    drop: { kind: "idle" },
  };
}

const fail = (message: string): Outcome => ({ ok: false, message });
const done = (message: string): Outcome => ({ ok: true, message });

export const usePanelsStore = create<PanelsState>()((set, get) => ({
  ...initialPanelsState(),
  setTab: (tab) => set({ tab }),
  loadRuns: async (projectId) => {
    if (!projectId) {
      set({ runs: [], runsProjectId: null, runsLoading: false, runsError: null });
      return;
    }
    set({ runsLoading: true, runsError: null, runsProjectId: projectId });
    try {
      const runs = await listRuns(projectId);
      if (get().runsProjectId !== projectId) return; // a newer project was opened meanwhile
      set({ runs, runsLoading: false });
    } catch (err) {
      if (get().runsProjectId !== projectId) return;
      set({ runs: [], runsLoading: false, runsError: errorMessage(err) });
    }
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
