// Make bar state per node (T35): the draft (engine, preset, N, region crop, params), the live quote, and the
// phase of quote -> approve -> run. Changing anything in the draft drops the quote, because Make must run exactly
// what was quoted. Quote: `POST /projects/{pid}/nodes/{node}/quote`, then `POST /runs/{id}/fanout {n}` for N > 1
// (the daemon turns the quoted run into sibling 0 of a group under the same quote). Make: only with a fresh quote;
// at or over the auto cap a dialog asks the human first; `POST /runs/{id}/approve` approves the whole group; then
// every sibling joins the node's candidate strip and is watched on the event bus until it is terminal.

import { create } from "zustand";
import { unwatchRun, watchRun } from "../canvas/runEvents";
import { MAX_CANDIDATES, useRunStore } from "../canvas/runStore";
import { isRunState, isTerminal, type RunWire } from "../canvas/runTypes";
import { approveRun, type EngineList, errorCode, errorText, fanoutRun, listEngines, projectCustody, quoteNode } from "./api";
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
}) as MakeEntry;

const freshEntry = (): MakeEntry => ({ ...EMPTY_ENTRY, draft: { ...DEFAULT_DRAFT, params: {} } });

export const clampN = (n: number): number => Math.min(MAX_CANDIDATES, Math.max(1, Math.round(Number.isFinite(n) ? n : 1)));

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
      patch(nodeId, { draft, quote: null, phase: "idle", error: null, approvalOpen: false, approvalReason: null });
    },
    requestQuote: async (nodeId, projectId) => {
      const { draft } = get().entry(nodeId);
      patch(nodeId, { phase: "quoting", error: null, quote: null, approvalOpen: false, approvalReason: null });
      try {
        const first = await quoteNode(projectId, nodeId, {
          engine_id: draft.engineId,
          preset: draft.preset,
          region_crop: draft.regionCrop,
          params: draft.params,
        });
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
      if (!q || !isFresh(q, Date.now())) {
        patch(nodeId, { error: "the quote expired: quote again", phase: q ? "error" : cur.phase, approvalOpen: false });
        return;
      }
      if (!opts.confirmed && needsHumanApproval(q.totalUsd, q.autoCapUsd)) {
        patch(nodeId, { approvalOpen: true, approvalReason: null });
        return;
      }
      patch(nodeId, { phase: "making", error: null, approvalOpen: false, approvalReason: null });
      const primary = q.runs[0];
      try {
        const approved = await approveRun(primary.id, { by: opts.confirmed ? "human" : "auto" });
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
        if (code === "auto_cap" || code === "envelope_required" || code === "auto_approval_denied") {
          // The daemon wants a human for this one: ask, keep the quote.
          patch(nodeId, { phase: "quoted", approvalOpen: true, approvalReason: errorText(err) });
          return;
        }
        patch(nodeId, { phase: "error", error: errorText(err), ...(code === "quote_expired" ? { quote: null } : {}) });
      }
    },
    closeApproval: (nodeId) => patch(nodeId, { approvalOpen: false, approvalReason: null }),
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
