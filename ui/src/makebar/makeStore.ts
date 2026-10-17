// Make bar state per node (T35): the draft (engine, preset, N, region crop, params), the live quote, and the
// phase of quote -> approve -> run. Changing anything in the draft drops the quote, because Make must run exactly
// what was quoted. Quote: the graph is saved first (the daemon prices the node it has on disk, decision 31; a failed
// save stops the quote), then `POST /projects/{pid}/nodes/{node}/quote`, then `POST /runs/{id}/fanout {n}` for
// N > 1 (the daemon turns the quoted run into sibling 0 of a group under the same quote). Make: only with a fresh
// quote; at or over the auto cap a dialog asks first; `POST /runs/{id}/approve {by: "human"}` approves the whole
// group, because a click in the browser is always a person (decision 37); then every sibling joins the node's
// candidate strip and is watched on the event bus until it is terminal. A daemon refusal (HUMAN_REFUSALS) opens the
// dialog with its reason; a daily or unattended cap (BLOCKING_REFUSALS) shows it with nothing to approve.

import { create } from "zustand";
import { unwatchRun, watchRun } from "../canvas/runEvents";
import { MAX_CANDIDATES, useRunStore } from "../canvas/runStore";
import { isRunState, isTerminal, type RunWire } from "../canvas/runTypes";
import { useGraphStore } from "../store";
import {
  approveRun,
  BLOCKING_REFUSALS,
  type EngineList,
  errorCode,
  errorDetail,
  errorText,
  fanoutRun,
  HUMAN_REFUSALS,
  listEngines,
  projectCustody,
  QUOTE_NODE_FIELDS,
  type QuoteRequest,
  quoteNode,
} from "./api";
import type { ProjectCustody } from "./engines";
import { autoCapFrom, isFresh, needsHumanApproval, type QuoteState, quoteLifetimeMs, totalPrice } from "./quote";

export interface MakeDraft {
  engineId: string | null;
  preset: string;
  n: number;
  regionCrop: boolean;
  params: Record<string, unknown>;
}

export type MakePhase = "idle" | "quoting" | "quoted" | "making" | "made" | "error";

export interface MakeEntry {
  draft: MakeDraft;
  quote: QuoteState | null;
  phase: MakePhase;
  error: string | null;
  /** The over-the-cap dialog is open (or the daemon asked for a human). */
  approvalOpen: boolean;
  /** Why the dialog is open, when the daemon said. */
  approvalReason: string | null;
  /** The daemon refused outright (daily or unattended cap): the dialog explains and offers no approve. Optional so
   * entries written before it existed still type-check; absent means false. */
  approvalBlocking?: boolean;
}

export const DEFAULT_DRAFT: MakeDraft = { engineId: null, preset: "default", n: 1, regionCrop: false, params: {} };

/** The entry of a node that has none yet; shared and frozen so selectors can return it without re-rendering. */
export const EMPTY_ENTRY: MakeEntry = Object.freeze({
  draft: Object.freeze({ ...DEFAULT_DRAFT, params: Object.freeze({}) }) as MakeDraft,
  quote: null,
  phase: "idle",
  error: null,
  approvalOpen: false,
  approvalReason: null,
  approvalBlocking: false,
}) as MakeEntry;

const freshEntry = (): MakeEntry => ({ ...EMPTY_ENTRY, draft: { ...DEFAULT_DRAFT, params: {} } });

export const clampN = (n: number): number => Math.min(MAX_CANDIDATES, Math.max(1, Math.round(Number.isFinite(n) ? n : 1)));

const closed = { approvalOpen: false, approvalReason: null, approvalBlocking: false } as const;

/** The quote body for a node: the draft, plus `prompt` / `recipe` / `task` / `shot_class` from the node's params when
 * the editors set them. Never `attempt`: the daemon numbers repeats itself (decision 40). */
export function quoteRequest(draft: MakeDraft, nodeParams: Record<string, unknown> | undefined): QuoteRequest {
  const params = { ...draft.params };
  delete params.attempt;
  const req: QuoteRequest = { engine_id: draft.engineId, preset: draft.preset, region_crop: draft.regionCrop, params };
  for (const key of QUOTE_NODE_FIELDS) {
    const v = nodeParams?.[key];
    if (typeof v === "string" && v.trim() !== "") req[key] = v;
  }
  return req;
}

export interface MakeStoreState {
  entries: Record<string, MakeEntry>;
  engines: EngineList | null;
  custody: ProjectCustody | null;
  /** The project `custody` belongs to (null: not loaded). */
  custodyFor: string | null;
  entry: (nodeId: string) => MakeEntry;
  setDraft: (nodeId: string, patch: Partial<MakeDraft>) => void;
  requestQuote: (nodeId: string, projectId: string) => Promise<void>;
  make: (nodeId: string, projectId: string | null, opts?: { confirmed?: boolean }) => Promise<void>;
  closeApproval: (nodeId: string) => void;
  loadEngines: () => Promise<void>;
  loadCustody: (projectId: string) => Promise<void>;
  setCustody: (projectId: string | null, custody: ProjectCustody | null) => void;
  reset: () => void;
}

export const useMakeStore = create<MakeStoreState>()((set, get) => {
  const patch = (nodeId: string, p: Partial<MakeEntry>) =>
    set((s) => ({ entries: { ...s.entries, [nodeId]: { ...(s.entries[nodeId] ?? freshEntry()), ...p } } }));

  return {
    entries: {},
    engines: null,
    custody: null,
    custodyFor: null,
    entry: (nodeId) => get().entries[nodeId] ?? freshEntry(),
    setDraft: (nodeId, p) => {
      const cur = get().entry(nodeId);
      const draft = { ...cur.draft, ...p, ...(p.n !== undefined ? { n: clampN(p.n) } : {}) };
      // A changed draft is a different request: the old quote no longer prices it.
      patch(nodeId, { draft, quote: null, phase: "idle", error: null, ...closed });
    },
    requestQuote: async (nodeId, projectId) => {
      patch(nodeId, { phase: "quoting", error: null, quote: null, ...closed });
      // The daemon quotes the node as it is stored: save first, and quote nothing the daemon does not have.
      await useGraphStore.getState().saveNow();
      if (get().entry(nodeId).phase !== "quoting") return; // the draft changed while the graph was saved
      const saveError = useGraphStore.getState().saveError;
      if (saveError) {
        patch(nodeId, { phase: "error", error: `the graph is not saved: ${saveError.message}` });
        return;
      }
      const { draft } = get().entry(nodeId);
      const node = useGraphStore.getState().nodes.find((n) => n.id === nodeId);
      try {
        const first = await quoteNode(projectId, nodeId, quoteRequest(draft, node?.data.params));
        let runs: RunWire[] = [first.run];
        if (draft.n > 1) {
          const group = await fanoutRun(first.run.id, draft.n);
          runs = group.map((r) => (r.quote ? r : { ...r, quote: first.quote }));
        }
        const quote = runs[0]?.quote ?? first.quote;
        const state: QuoteState = {
          quote,
          runs,
          totalUsd: totalPrice(runs, quote),
          receivedAt: Date.now(),
          ttlMs: quoteLifetimeMs(quote),
          autoCapUsd: autoCapFrom(first.body),
        };
        // Only if the draft did not change while the quote was in flight.
        if (get().entry(nodeId).phase === "quoting") patch(nodeId, { quote: state, phase: "quoted" });
      } catch (err) {
        patch(nodeId, { phase: "error", error: errorText(err) });
      }
    },
    make: async (nodeId, projectId, opts = {}) => {
      const cur = get().entry(nodeId);
      const q = cur.quote;
      if (opts.confirmed && cur.approvalBlocking) return; // a blocked spend has nothing to confirm
      if (!q || !isFresh(q, Date.now())) {
        patch(nodeId, { error: "the quote expired: quote again", phase: q ? "error" : cur.phase, ...closed });
        return;
      }
      if (!opts.confirmed && needsHumanApproval(q.totalUsd, q.autoCapUsd)) {
        patch(nodeId, { approvalOpen: true, approvalReason: null, approvalBlocking: false });
        return;
      }
      patch(nodeId, { phase: "making", error: null, ...closed });
      const primary = q.runs[0];
      try {
        // Under the cap or confirmed in the dialog, the click was a person's: the cap only decides whether to ask.
        const approved = await approveRun(primary.id, { by: "human" });
        const byId = new Map((approved ?? []).map((r) => [r.id, r]));
        const runs = q.runs.map((r) => {
          const fresh = byId.get(r.id);
          return fresh ? { ...r, ...fresh, quote: fresh.quote ?? r.quote } : { ...r, state: "APPROVED" };
        });
        // The new Make replaces the node's strip: stop following the runs it replaces.
        for (const id of useRunStore.getState().byNode[nodeId]?.runIds ?? []) unwatchRun(id);
        useRunStore.getState().startRuns(nodeId, runs);
        for (const r of runs) {
          if (!(isRunState(r.state) && isTerminal(r.state))) watchRun(r.id, nodeId, projectId);
        }
        patch(nodeId, { phase: "made", quote: null });
      } catch (err) {
        const code = errorCode(err);
        if (code !== null && HUMAN_REFUSALS.has(code)) {
          // The daemon wants a person to read this one: show its reason in the dialog and keep the quote.
          const blocking = BLOCKING_REFUSALS.has(code);
          patch(nodeId, { phase: "quoted", approvalOpen: true, approvalReason: errorDetail(err), approvalBlocking: blocking });
          return;
        }
        patch(nodeId, { phase: "error", error: errorText(err), ...(code === "quote_expired" ? { quote: null } : {}) });
      }
    },
    closeApproval: (nodeId) => patch(nodeId, closed),
    loadEngines: async () => {
      const list = await listEngines();
      set({ engines: list });
    },
    loadCustody: async (projectId) => {
      const custody = await projectCustody(projectId);
      set({ custody, custodyFor: projectId });
    },
    setCustody: (projectId, custody) => set({ custody, custodyFor: projectId }),
    reset: () => set({ entries: {}, engines: null, custody: null, custodyFor: null }),
  };
});
