// Wire shapes the panels read. Daemon routes are specified in T25 (runs, gate, projects), T26 (resolve) and T38
// (verdict, scrub, timer, outbound); every field a route may omit is optional here, so a panel renders what it gets
// and never throws on a missing one.

import type { Point } from "../viewer";

export type CustodyGrade = "C0" | "C1" | "C2" | "D";
export const CUSTODY_GRADES: readonly CustodyGrade[] = ["C0", "C1", "C2", "D"];
/** An NDA project runs on C0 and C1 routes only (daemon `projects.NDA_GRADES`). */
export const NDA_GRADES: readonly CustodyGrade[] = ["C0", "C1"];

export interface ProjectCustody {
  nda: boolean;
  allowed_grades: CustodyGrade[];
}

/** One row of `GET /projects`: the head of `project.json`. */
export interface ProjectSummary {
  project_id: string;
  created_utc?: string;
  custody: ProjectCustody;
  engine_pins: Record<string, string>;
}

export interface CreateProjectBody {
  project_id: string;
  custody: ProjectCustody;
  engine_pins: Record<string, string>;
}

/** The slice of a daemon `Run` the panels use (`plate_daemon.models.Run`, JSON mode). */
export interface RunRow {
  id: string;
  project_id?: string;
  node_id?: string | null;
  packet_id?: string;
  state: string;
  route?: { engine_id: string; provider?: string; region?: string } | null;
  quote?: { price_usd?: number; custody_grade?: CustodyGrade } | null;
  cost_usd_billed?: number | null;
  gate_report?: { verdict?: GateStatus } | null;
  created_utc?: string;
  updated_utc?: string;
  approved_by?: string | null;
  result_key?: string | null;
}

/** `GET /runs/{id}`: the run plus, when the daemon kept one, the subgraph that produced it. */
export interface RunDetail extends RunRow {
  subgraph?: { nodes: unknown[]; edges: unknown[] } | null;
}

export type GateStatus = "pass" | "fail" | "advisory";

/** A gate check. `threshold` is optional: the daemon may attach the bound the value was held to. */
export interface GateCheck {
  metric_id: string;
  value: number | null;
  status: GateStatus;
  threshold?: unknown;
  unit?: string;
}

/** `plate_gates.report.GateReport`, JSON mode. */
export interface GateReport {
  gate_version?: string;
  metric_code_hash?: string;
  threshold_set_id?: string;
  engine?: { id: string; version?: string | null } | null;
  tier?: string | null;
  metrics?: Record<string, unknown>;
  checks: GateCheck[];
  verdict: GateStatus;
  backend_notes?: string[];
  created_utc?: string;
  /** Optional: points (image pixels) the gates flagged, for the 100% tiles. */
  flagged_points?: unknown;
  /** Optional: metric_id -> bound, when the daemon sends bounds beside the checks. */
  thresholds?: Record<string, unknown>;
}

export interface GateVersion {
  n: number;
  shipped: boolean;
}

/** What the gate panel shows: one numbered report of a run and the versions that exist. */
export interface GateView {
  report: GateReport;
  n: number;
  shipped: boolean;
  versions: GateVersion[];
}

/** One merged record of `outbound.jsonl` (request + response/error), see `plate_daemon.outbound_log`. */
export interface OutboundEntry {
  id: string;
  at?: string;
  run_id?: string;
  route_id?: string;
  host?: string;
  method?: string;
  path?: string;
  bytes?: number;
  custody_grade?: CustodyGrade;
  opaque_ids?: unknown;
  body_hash?: string;
  status?: number;
  bytes_in?: number;
  error?: string;
}

export type VerdictName = "ship" | "fix" | "render_it";

export interface VerdictBody {
  verdict: VerdictName;
  reasons: string[];
  scrub_log_id: string;
  rater: string;
}

export interface ScrubBody {
  quadrants_visited: number;
  zoom_100: boolean;
}

export interface TimerRow {
  deliverable_id: string;
  minutes: number;
}

/** `POST /assets/resolve`: lineage first, then exact content hash, then the nearest perceptual hash. */
export interface ResolveResult {
  by: "lineage" | "content_hash" | "phash";
  content_hash: string;
  run_id?: string | null;
  distance?: number | null;
}

export type { Point };
