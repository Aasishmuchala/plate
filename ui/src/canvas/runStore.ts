// Run status on the canvas (T35): the candidates each node has made, their live state from the event bus, the
// best-of-N ranking, which candidate a node shows, the A/B pair the viewer compares, and the inline refusal a node
// shows for 4 s after a refused connection. The graph itself stays in `useGraphStore`; this store only adds what
// runs produce. A pick writes the candidate's output hash and gate verdict onto the graph node, so the canvas
// rules (VIDEO_LOCKED reads `gateStatus`) and `project.json` see it.
//
// T101: the A/B pair is two sides, each a plate, a run or a node's output, and `setCompareSide` sets either one.
// Only a candidate against its own plate is `scrubEligible`: the scrub (and so the verdict keys and accept) stays
// bound to that pairing, and any other pairing is for looking only. `enlarged` is the viewer filling the canvas.

import { create } from "zustand";
import { useGraphStore } from "../store";
import { plateAncestor } from "./derive";
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
  /** The engine the run was routed to (`run.route.engine_id`). */
  engineId?: string | null;
  /** The daemon's attempt number for this node and packet (decision 40). */
  attempt?: number | null;
  /** The stored raw take a Recomposite can re-mask (`normalized.raw_key`). */
  rawKey?: string | null;
}

/** A run as the strip reads it: the typed wire fields plus the few extra ones the daemon's run view carries. */
export type CandidateRun = RunWire & {
  route?: { engine_id?: string | null } | null;
  attempt?: number | null;
  raw_key?: string | null;
  normalized?: { raw_key?: string | null } | null;
};

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

/** What one side of the viewer shows: the plate a node is anchored to, a run's output, or a node's output. */
export type SideKind = "plate" | "run" | "node";

export interface Side {
  kind: SideKind;
  runId?: string;
  nodeId?: string;
  label: string;
  /** The image when the caller knows it (a History run the strip never held); the stores are read first. */
  outputHash?: string;
}

/** What a caller names when it sets a side; the label is filled from the graph and the strip when left out. */
export type SideRef = Omit<Side, "label"> & { label?: string };

/** The viewer's A/B. A candidate click sets A = the node's plate and B = the run; B may be empty. */
export interface ComparePair {
  a: Side;
  b: Side | null;
}

export interface RunStoreState {
  candidates: Record<string, Candidate>;
  byNode: Record<string, NodeRuns>;
  flash: RefusalFlash | null;
  compare: ComparePair | null;
  /** True only while the pair is a candidate against its own plate: the scrub, verdict and accept follow it. */
  scrubEligible: boolean;
  /** The viewer fills the canvas area (the graph is hidden, not unmounted). */
  enlarged: boolean;
  /** Replace a node's candidate set with the runs of one Make (one run, or a best-of-N group). */
  startRuns: (nodeId: string, runs: CandidateRun[]) => void;
  /** Merge a run read from the daemon (GET /runs/{id}); unknown runs are added under their node. */
  upsertRun: (run: CandidateRun, nodeId?: string) => void;
  setRunState: (runId: string, state: RunState) => void;
  setRanking: (group: string, order: string[]) => void;
  markOverride: (runId: string, reason: string, by: string) => void;
  pick: (nodeId: string, runId: string) => void;
  openCompare: (nodeId: string, runId: string) => void;
  /** Set side A or B (T98's context menu and the strip's Set as A / Set as B). */
  setCompareSide: (side: "a" | "b", ref: SideRef) => void;
  closeCompare: () => void;
  setEnlarged: (on: boolean) => void;
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

const engineOf = (run: CandidateRun): string | null => run.route?.engine_id ?? null;

const attemptOf = (run: CandidateRun): number | null =>
  typeof run.attempt === "number" && Number.isFinite(run.attempt) ? run.attempt : null;

const rawKeyOf = (run: CandidateRun): string | null => run.normalized?.raw_key ?? run.raw_key ?? null;

/** A candidate from a run's JSON, keeping anything the run does not say from `prev`. */
export function candidateFrom(run: CandidateRun, nodeId: string, prev?: Candidate): Candidate {
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
    engineId: engineOf(run) ?? prev?.engineId ?? null,
    attempt: attemptOf(run) ?? prev?.attempt ?? null,
    rawKey: rawKeyOf(run) ?? prev?.rawKey ?? null,
  };
}

/** `<node label> · <engine_id> · attempt n`, leaving out the parts that are not known. */
export function runLabel(nodeLabel: string, engineId?: string | null, attempt?: number | null): string {
  const parts = [nodeLabel, engineId ?? null, typeof attempt === "number" ? `attempt ${attempt}` : null];
  return parts.filter(Boolean).join(" · ");
}

const nodeLabelOf = (nodeId: string | undefined): string | null => {
  if (!nodeId) return null;
  const node = useGraphStore.getState().nodes.find((n) => n.id === nodeId);
  return node?.data.label ?? null;
};

/** The strip's label of a candidate (and the viewer's caption for it outside the plate pairing). */
export function candidateLabel(c: Candidate): string {
  return runLabel(nodeLabelOf(c.nodeId) ?? c.nodeId, c.engineId, c.attempt);
}

/** The Plate node a side's node is anchored to, or null. */
function plateIdOf(nodeId: string | undefined): string | null {
  if (!nodeId) return null;
  const g = useGraphStore.getState();
  return plateAncestor(g.nodes, g.edges, nodeId)?.id ?? null;
}

/** A side with its node and label filled in from the strip and the graph. */
function resolveSide(ref: SideRef, candidates: Record<string, Candidate>): Side {
  const cand = ref.runId ? candidates[ref.runId] : undefined;
  const nodeId = ref.nodeId ?? cand?.nodeId;
  const side: Side = { ...ref, ...(nodeId ? { nodeId } : {}), label: ref.label ?? "" };
  if (ref.label) return side;
  if (ref.kind === "run") {
    side.label = cand ? candidateLabel(cand) : `run ${(ref.runId ?? "").slice(0, 8)}`;
  } else if (ref.kind === "plate") {
    side.label = "plate"; // as a candidate click names it (`A plate · B candidate 1`)
  } else {
    side.label = nodeLabelOf(nodeId) ?? nodeId ?? "node";
  }
  return side;
}

/** True when the pair is a run against the plate its own node is anchored to: the one pairing a scrub counts on. */
export function isPlateVsCandidate(pair: ComparePair | null): boolean {
  if (!pair?.b) return false;
  const { a, b } = pair;
  if (a.kind !== "plate" || b.kind !== "run" || !b.runId || !a.nodeId || !b.nodeId) return false;
  if (a.nodeId === b.nodeId) return true;
  const plate = plateIdOf(a.nodeId);
  return plate !== null && plate === plateIdOf(b.nodeId);
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

const empty = (): Pick<RunStoreState, "candidates" | "byNode" | "flash" | "compare" | "scrubEligible" | "enlarged"> => ({
  candidates: {},
  byNode: {},
  flash: null,
  compare: null,
  scrubEligible: false,
  enlarged: false,
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
  openCompare: (nodeId, runId) =>
    set((s) => {
      const b = resolveSide({ kind: "run", runId, nodeId }, s.candidates);
      return { compare: { a: { kind: "plate", nodeId, label: "plate" }, b }, scrubEligible: true };
    }),
  setCompareSide: (which, ref) =>
    set((s) => {
      const side = resolveSide(ref, s.candidates);
      let compare: ComparePair;
      if (which === "a") {
        compare = { a: side, b: s.compare?.b ?? null };
      } else if (s.compare) {
        compare = { a: s.compare.a, b: side };
      } else if (side.nodeId && side.kind !== "plate") {
        // nothing open yet: B is compared with the plate of its node, as a candidate click would
        compare = { a: resolveSide({ kind: "plate", nodeId: side.nodeId }, s.candidates), b: side };
      } else {
        compare = { a: side, b: null };
      }
      return { compare, scrubEligible: isPlateVsCandidate(compare) };
    }),
  closeCompare: () => set({ compare: null, scrubEligible: false }),
  setEnlarged: (on) => set((s) => (s.enlarged === on ? {} : { enlarged: on })),
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
