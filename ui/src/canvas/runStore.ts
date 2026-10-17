// Run status on the canvas (T35): the candidates each node has made, their live state from the event bus, the
// best-of-N ranking, which candidate a node shows, the A/B pair the viewer compares, and the inline refusal a node
// shows for 4 s after a refused connection. The graph itself stays in `useGraphStore`; this store only adds what
// runs produce. A pick writes the candidate's output hash and gate verdict onto the graph node, so the canvas
// rules (VIDEO_LOCKED reads `gateStatus`) and `project.json` see it.

import { create } from "zustand";
import { useGraphStore } from "../store";
import {
  type GateCheck,
  gateStatusOf,
  isRunState,
  isTerminal,
  type QuoteWire,
  type RunState,
  type RunWire,
} from "./runTypes";

/** How long an inline refusal stays on the canvas. */
export const REFUSAL_FLASH_MS = 4000;

/** At most this many candidates per Make (BUILD_PLAN: N from 1 to 6; the daemon's MAX_FAN_OUT agrees). */
export const MAX_CANDIDATES = 6;

export interface Candidate {
  runId: string;
  nodeId: string;
  /** Sibling index in the best-of-N group (0 for a single run). */
  index: number;
  group: string | null;
  state: RunState;
  outputHash: string | null;
  gateScore: number | null;
  verdict: "pass" | "fail" | "advisory" | null;
  /** The first failing check of a GATE_FAIL, shown on the chip. */
  firstFail: GateCheck | null;
  approvedBy: string | null;
  /** The budget envelope an agent's run spent from. */
  envelopeId: string | null;
  quote: QuoteWire | null;
  override: { reason: string; by: string } | null;
  error: string | null;
}

export interface NodeRuns {
  runIds: string[];
  group: string | null;
  /** The daemon's `siblings.ranked` order, best first, once the group finished. */
  ranked: string[] | null;
  picked: string | null;
}

export interface RefusalFlash {
  seq: number;
  /** The node the message belongs to (the connection's target); null shows it on the canvas. */
  nodeId: string | null;
  code: string;
  message: string;
}

/** The viewer's A/B: a candidate against its node's plate. */
export interface ComparePair {
  nodeId: string;
  runId: string;
}

export interface RunStoreState {
  candidates: Record<string, Candidate>;
  byNode: Record<string, NodeRuns>;
  flash: RefusalFlash | null;
  compare: ComparePair | null;
  /** Replace a node's candidate set with the runs of one Make (one run, or a best-of-N group). */
  startRuns: (nodeId: string, runs: RunWire[]) => void;
  /** Merge a run read from the daemon (GET /runs/{id}); unknown runs are added under their node. */
  upsertRun: (run: RunWire, nodeId?: string) => void;
  setRunState: (runId: string, state: RunState) => void;
  setRanking: (group: string, order: string[]) => void;
  markOverride: (runId: string, reason: string, by: string) => void;
  pick: (nodeId: string, runId: string) => void;
  openCompare: (nodeId: string, runId: string) => void;
  closeCompare: () => void;
  flashRefusal: (nodeId: string | null, code: string, message: string) => void;
  clearFlash: () => void;
  reset: () => void;
}

const firstOutput = (run: RunWire): string | null => run.outputs?.[0] ?? run.result_key ?? null;

const firstFailing = (run: RunWire): GateCheck | null => run.gate_report?.checks?.find((c) => c.status === "fail") ?? null;

const envelopeOf = (run: RunWire): string | null => {
  if (run.quote?.envelope_id) return run.quote.envelope_id;
  const by = run.approved_by ?? run.quote?.approved_by ?? null;
  return by && by.startsWith("agent:") ? by.slice("agent:".length) : null;
};

const scoreOf = (run: RunWire): number | null => {
  const s = run.gate_score ?? run.gate_report?.score;
  return typeof s === "number" && Number.isFinite(s) ? s : null;
};

/** A candidate from a run's JSON, keeping anything the run does not say from `prev`. */
export function candidateFrom(run: RunWire, nodeId: string, prev?: Candidate): Candidate {
  const state: RunState = isRunState(run.state) ? run.state : (prev?.state ?? "DRAFT");
  const verdict = run.gate_report?.verdict ?? prev?.verdict ?? null;
  return {
    runId: run.id,
    nodeId,
    index: run.sibling_index ?? run.quote?.sibling_index ?? prev?.index ?? 0,
    group: run.sibling_group ?? run.quote?.sibling_group ?? prev?.group ?? null,
    state,
    outputHash: firstOutput(run) ?? prev?.outputHash ?? null,
    gateScore: scoreOf(run) ?? prev?.gateScore ?? null,
    verdict,
    firstFail: firstFailing(run) ?? prev?.firstFail ?? null,
    approvedBy: run.approved_by ?? run.quote?.approved_by ?? prev?.approvedBy ?? null,
    envelopeId: envelopeOf(run) ?? prev?.envelopeId ?? null,
    quote: run.quote ?? prev?.quote ?? null,
    override: run.override ? { reason: run.override.reason, by: run.override.by } : (prev?.override ?? null),
    error: run.error ?? run.reason_code ?? prev?.error ?? null,
  };
}

/**
 * The strip order. While any sibling still runs: sibling index. Once all are terminal: the daemon's ranking when
 * it sent one, else the daemon's rank key (DONE before GATE_FAIL before the rest, then gate score high to low,
 * then sibling index), so the best candidate is always first.
 */
export function rankCandidates(cands: Candidate[], ranked: string[] | null = null): Candidate[] {
  const byIndex = [...cands].sort((a, b) => a.index - b.index);
  if (!cands.every((c) => isTerminal(c.state))) return byIndex;
  if (ranked && ranked.length > 0) {
    const pos = new Map(ranked.map((id, i) => [id, i]));
    return [...byIndex].sort((a, b) => (pos.get(a.runId) ?? Infinity) - (pos.get(b.runId) ?? Infinity));
  }
  const klass = (c: Candidate) => (c.state === "DONE" ? 0 : c.state === "GATE_FAIL" ? 1 : 2);
  return [...byIndex].sort(
    (a, b) =>
      klass(a) - klass(b) ||
      (b.gateScore ?? -Infinity) - (a.gateScore ?? -Infinity) ||
      a.index - b.index,
  );
}

/** The candidates of a node in strip order. */
export function nodeCandidates(state: Pick<RunStoreState, "candidates" | "byNode">, nodeId: string): Candidate[] {
  const entry = state.byNode[nodeId];
  if (!entry) return [];
  const cands = entry.runIds.map((id) => state.candidates[id]).filter((c): c is Candidate => c !== undefined);
  return rankCandidates(cands, entry.ranked);
}

/** Write a picked candidate onto its graph node: output hash (thumbnail, viewer) and gate verdict (canvas rules). */
function writePickToGraph(c: Candidate): void {
  const graph = useGraphStore.getState();
  const node = graph.nodes.find((n) => n.id === c.nodeId);
  if (!node) return;
  const gateStatus = gateStatusOf(c.verdict ?? undefined, c.state);
  if (node.data.gateStatus !== gateStatus) graph.setNodeStatus(c.nodeId, { gateStatus });
  const outputHash = c.outputHash ?? undefined;
  useGraphStore.setState((s) => ({
    nodes: s.nodes.map((n) =>
      n.id === c.nodeId && n.data.outputHash !== outputHash ? { ...n, data: { ...n.data, outputHash } } : n,
    ),
  }));
}

let flashTimer: ReturnType<typeof setTimeout> | null = null;
let flashSeq = 0;

const empty = (): Pick<RunStoreState, "candidates" | "byNode" | "flash" | "compare"> => ({
  candidates: {},
  byNode: {},
  flash: null,
  compare: null,
});

export const useRunStore = create<RunStoreState>()((set, get) => ({
  ...empty(),
  startRuns: (nodeId, runs) =>
    set((s) => {
      const candidates = { ...s.candidates };
      for (const id of s.byNode[nodeId]?.runIds ?? []) delete candidates[id];
      for (const run of runs) candidates[run.id] = candidateFrom(run, nodeId);
      const group = runs.map((r) => r.sibling_group ?? r.quote?.sibling_group ?? null).find((g) => g) ?? null;
      const byNode = { ...s.byNode, [nodeId]: { runIds: runs.map((r) => r.id), group, ranked: null, picked: null } };
      return { candidates, byNode };
    }),
  upsertRun: (run, nodeId) =>
    set((s) => {
      const prev = s.candidates[run.id];
      const owner = nodeId ?? prev?.nodeId ?? run.node_id ?? null;
      if (!owner) return {};
      const cand = candidateFrom(run, owner, prev);
      const entry = s.byNode[owner] ?? { runIds: [], group: null, ranked: null, picked: null };
      const runIds = entry.runIds.includes(run.id) ? entry.runIds : [...entry.runIds, run.id];
      return {
        candidates: { ...s.candidates, [run.id]: cand },
        byNode: { ...s.byNode, [owner]: { ...entry, runIds, group: entry.group ?? cand.group } },
      };
    }),
  setRunState: (runId, state) =>
    set((s) => {
      const prev = s.candidates[runId];
      if (!prev || prev.state === state) return {};
      return { candidates: { ...s.candidates, [runId]: { ...prev, state } } };
    }),
  setRanking: (group, order) =>
    set((s) => {
      const byNode = { ...s.byNode };
      let changed = false;
      for (const [nodeId, entry] of Object.entries(s.byNode)) {
        if (entry.group === group) {
          byNode[nodeId] = { ...entry, ranked: order };
          changed = true;
        }
      }
      return changed ? { byNode } : {};
    }),
  markOverride: (runId, reason, by) =>
    set((s) => {
      const prev = s.candidates[runId];
      return prev ? { candidates: { ...s.candidates, [runId]: { ...prev, override: { reason, by } } } } : {};
    }),
  pick: (nodeId, runId) => {
    const cand = get().candidates[runId];
    if (!cand || cand.nodeId !== nodeId) return;
    set((s) => ({ byNode: { ...s.byNode, [nodeId]: { ...s.byNode[nodeId], picked: runId } } }));
    writePickToGraph(cand);
  },
  openCompare: (nodeId, runId) => set({ compare: { nodeId, runId } }),
  closeCompare: () => set({ compare: null }),
  flashRefusal: (nodeId, code, message) => {
    flashSeq += 1;
    const seq = flashSeq;
    if (flashTimer !== null) clearTimeout(flashTimer);
    flashTimer = setTimeout(() => {
      flashTimer = null;
      if (get().flash?.seq === seq) set({ flash: null });
    }, REFUSAL_FLASH_MS);
    set({ flash: { seq, nodeId, code, message } });
  },
  clearFlash: () => {
    if (flashTimer !== null) clearTimeout(flashTimer);
    flashTimer = null;
    set({ flash: null });
  },
  reset: () => {
    if (flashTimer !== null) clearTimeout(flashTimer);
    flashTimer = null;
    set(empty());
  },
}));
