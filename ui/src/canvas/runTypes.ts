// Wire shapes the canvas reads from the daemon (T25 routes; the seed's `models.Run` / `models.Quote` /
// `plate_gates.report.GateReport` JSON). Only the fields the canvas uses are typed; everything else passes through.

import type { CustodyGrade, GateStatus } from "../nodeTypes";

/** `plate_daemon.states.RunState`. */
export const RUN_STATES = [
  "DRAFT",
  "QUOTED",
  "APPROVED",
  "QUEUED",
  "STAGING",
  "SUBMITTED",
  "RUNNING",
  "FETCHING",
  "NORMALIZING",
  "GATING",
  "DONE",
  "GATE_FAIL",
  "FAILED",
  "REFUSED",
  "CANCELLED",
  "EXPIRED",
] as const;
export type RunState = (typeof RUN_STATES)[number];

export const TERMINAL_STATES: ReadonlySet<RunState> = new Set<RunState>([
  "DONE",
  "GATE_FAIL",
  "FAILED",
  "REFUSED",
  "CANCELLED",
  "EXPIRED",
]);

export const isRunState = (s: unknown): s is RunState => typeof s === "string" && (RUN_STATES as readonly string[]).includes(s);
export const isTerminal = (s: RunState): boolean => TERMINAL_STATES.has(s);

/** `models.Quote` as JSON: the terms a run is approved against. Times are ISO-8601 UTC. */
export interface QuoteWire {
  id: string;
  run_id?: string | null;
  provider: string;
  model: string;
  model_version?: string;
  region?: string;
  custody_grade: CustodyGrade;
  price_usd: number;
  eta_s: number;
  created_utc: string;
  expires_utc: string;
  price_status?: string;
  sibling_group?: string | null;
  sibling_index?: number | null;
  siblings?: number;
  envelope_id?: string | null;
  approved_by?: string | null;
}

/** One gate check (`plate_gates.report.Check`); `threshold` is optional extra detail. */
export interface GateCheck {
  metric_id: string;
  value: number | null;
  status: "pass" | "fail" | "advisory";
  threshold?: unknown;
}

/** The part of a `GateReport` the canvas reads. */
export interface GateReportWire {
  verdict?: "pass" | "fail" | "advisory";
  checks?: GateCheck[];
  score?: number | null;
  backend_notes?: string[];
}

/** `models.Run` as JSON. */
export interface RunWire {
  id: string;
  run_id?: string;
  project_id?: string;
  node_id?: string | null;
  state: string;
  quote?: QuoteWire | null;
  outputs?: string[];
  result_key?: string | null;
  gate_report?: GateReportWire | null;
  gate_score?: number | null;
  sibling_group?: string | null;
  sibling_index?: number | null;
  approved_by?: string | null;
  reason_code?: string | null;
  error?: string | null;
  /** Set by `POST /runs/{id}/override`: a GATE_FAIL made exportable with a ledgered reason. */
  override?: { reason: string; by: string; at?: string } | null;
}

/** A record from the daemon's event bus (`sse.Bus.read`), the JSON body of every `/api/v0/events` message. */
export interface BusRecord {
  seq?: number;
  project_id?: string;
  run_id?: string | null;
  at?: string;
  kind: string;
  data?: Record<string, unknown>;
}

/** Gate verdict -> the node's `gateStatus`. */
export function gateStatusOf(verdict: GateReportWire["verdict"] | undefined, state: RunState): GateStatus {
  if (verdict === "pass" || verdict === "fail" || verdict === "advisory") return verdict;
  if (state === "GATE_FAIL") return "fail";
  if (state === "DONE") return "pass";
  return "none";
}
