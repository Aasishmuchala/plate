// Panel-side calls to the daemon, all through the token-carrying `fetchJson`. The routes are specified in T25 (runs,
// gate, projects), T26/T38 (resolve, verdict, scrub, timer, outbound); this file codes against those specs and
// tolerates the two common list shapes (a bare array, or an object holding the array) so a route that returns
// `{runs: [...]}` and one that returns `[...]` both work. Nothing here invents a route the plan does not list except
// `POST /runs/{id}/gate/rescore`, which is called out in the T36 report.

import { ApiError, fetchJson, postJson } from "../api";
import type { LoadableGraph } from "../nodeTypes";
import type {
  CreateProjectBody,
  CustodyGrade,
  GateReport,
  GateVersion,
  GateView,
  OutboundEntry,
  ProjectSummary,
  ResolveResult,
  RunDetail,
  RunRow,
  ScrubBody,
  TimerRow,
  VerdictBody,
} from "./types";
import { CUSTODY_GRADES } from "./types";

const enc = encodeURIComponent;

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/** A list from `[...]` or from `{key: [...]}` for the first of `keys` that holds an array; anything else is empty. */
export function asList<T>(body: unknown, ...keys: string[]): T[] {
  if (Array.isArray(body)) return body as T[];
  if (isRecord(body)) {
    for (const k of keys) {
      const v = body[k];
      if (Array.isArray(v)) return v as T[];
    }
  }
  return [];
}

/** The text to show for a failed call: the daemon's `detail`/`error`/`errors` when it sent one, else the status line. */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const b = err.body;
    if (isRecord(b)) {
      if (typeof b.detail === "string") return b.detail;
      if (typeof b.error === "string") return b.error;
      if (Array.isArray(b.errors) && b.errors.length > 0) return b.errors.map(String).join("; ");
    }
    if (typeof b === "string" && b !== "") return b;
    return err.message;
  }
  return err instanceof Error ? err.message : String(err);
}

// ---------------------------------------------------------------------------------------------------- projects

function toGrades(v: unknown): CustodyGrade[] {
  const list = Array.isArray(v) ? v : [];
  return CUSTODY_GRADES.filter((g) => list.includes(g));
}

function toProject(raw: unknown): ProjectSummary | null {
  if (!isRecord(raw) || typeof raw.project_id !== "string") return null;
  const custody = isRecord(raw.custody) ? raw.custody : {};
  const nda = custody.nda === true;
  const grades = toGrades(custody.allowed_grades);
  const pins = isRecord(raw.engine_pins) ? raw.engine_pins : {};
  return {
    project_id: raw.project_id,
    created_utc: typeof raw.created_utc === "string" ? raw.created_utc : undefined,
    custody: { nda, allowed_grades: grades.length > 0 ? grades : nda ? ["C0", "C1"] : [...CUSTODY_GRADES] },
    engine_pins: Object.fromEntries(Object.entries(pins).map(([k, v]) => [k, String(v)])),
  };
}

export async function listProjects(): Promise<ProjectSummary[]> {
  const body = await fetchJson<unknown>("/projects");
  return asList<unknown>(body, "projects").flatMap((p) => toProject(p) ?? []);
}

export async function createProject(body: CreateProjectBody): Promise<ProjectSummary> {
  const res = await postJson<unknown>("/projects", body);
  const made = toProject(isRecord(res) && isRecord(res.project) ? res.project : res);
  return made ?? { project_id: body.project_id, custody: body.custody, engine_pins: body.engine_pins };
}

/** `GET /projects/{id}/graph` returns the graph itself or `{graph}`. */
export async function getProjectGraph(projectId: string): Promise<LoadableGraph> {
  const body = await fetchJson<unknown>(`/projects/${enc(projectId)}/graph`);
  const graph = isRecord(body) && isRecord(body.graph) ? body.graph : body;
  if (!isRecord(graph) || !Array.isArray(graph.nodes) || !Array.isArray(graph.edges)) {
    throw new Error(`project ${projectId}: the daemon returned no graph`);
  }
  return { schema: 1, nodes: graph.nodes, edges: graph.edges } as LoadableGraph;
}

// ---------------------------------------------------------------------------------------------------- runs

export async function listRuns(projectId: string): Promise<RunRow[]> {
  const body = await fetchJson<unknown>(`/projects/${enc(projectId)}/runs`);
  return asList<RunRow>(body, "runs").filter((r) => isRecord(r) && typeof r.id === "string");
}

export const getRun = (runId: string): Promise<RunDetail> => fetchJson<RunDetail>(`/runs/${enc(runId)}`);

// ---------------------------------------------------------------------------------------------------- gate

function toVersions(raw: unknown, n: number, shipped: boolean): GateVersion[] {
  const list = asList<unknown>(raw, "versions");
  const out: GateVersion[] = [];
  for (const v of list) {
    if (typeof v === "number") out.push({ n: v, shipped: false });
    else if (isRecord(v) && typeof v.n === "number") out.push({ n: v.n, shipped: v.shipped === true || typeof v.shipped_utc === "string" });
  }
  if (out.length === 0) {
    for (let i = 1; i <= n; i += 1) out.push({ n: i, shipped: i === n && shipped });
  }
  return out.sort((a, b) => a.n - b.n);
}

/**
 * `GET /runs/{id}/gate[?n=]`: either the report itself, or `{report, n, shipped, versions}`. A 404 (no report yet)
 * returns null.
 */
export async function getGateReport(runId: string, n?: number): Promise<GateView | null> {
  let body: unknown;
  try {
    body = await fetchJson<unknown>(`/runs/${enc(runId)}/gate${n === undefined ? "" : `?n=${n}`}`);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
  if (!isRecord(body)) return null;
  const wrapped = isRecord(body.report);
  const report = (wrapped ? body.report : body) as unknown as GateReport;
  if (!isRecord(report) || !Array.isArray(report.checks)) return null;
  const num = typeof body.n === "number" ? body.n : (n ?? 1);
  const shipped = body.shipped === true || typeof body.shipped_utc === "string";
  return { report, n: num, shipped, versions: toVersions(body, num, shipped) };
}

export const rescoreGate = (runId: string): Promise<unknown> => postJson(`/runs/${enc(runId)}/gate/rescore`, {});

// ---------------------------------------------------------------------------------------------------- custody

export async function getOutbound(projectId: string, tail = 50): Promise<OutboundEntry[]> {
  const body = await fetchJson<unknown>(`/projects/${enc(projectId)}/outbound?tail=${tail}`);
  return asList<OutboundEntry>(body, "entries", "outbound").filter((e) => isRecord(e) && typeof e.id === "string");
}

// ---------------------------------------------------------------------------------------------------- verdict, scrub

export async function postScrub(runId: string, body: ScrubBody): Promise<string> {
  const res = await postJson<unknown>(`/runs/${enc(runId)}/scrub`, body);
  if (isRecord(res) && typeof res.scrub_log_id === "string") return res.scrub_log_id;
  throw new Error("the daemon did not return a scrub_log_id");
}

export const postVerdict = (runId: string, body: VerdictBody): Promise<unknown> => postJson(`/runs/${enc(runId)}/verdict`, body);

// ---------------------------------------------------------------------------------------------------- timer

export const timerAction = (deliverableId: string, action: "start" | "stop"): Promise<unknown> =>
  postJson("/timer", { deliverable_id: deliverableId, action });

/** `GET /timer/today` as rows: an array of rows, `{rows|deliverables|today: [...]}`, or a `{deliverable: minutes}` map. */
export async function getTimerToday(): Promise<TimerRow[]> {
  const body = await fetchJson<unknown>("/timer/today");
  const listed = asList<unknown>(body, "rows", "deliverables", "today");
  const out: TimerRow[] = [];
  for (const r of listed) {
    if (isRecord(r) && typeof r.deliverable_id === "string" && typeof r.minutes === "number") {
      out.push({ deliverable_id: r.deliverable_id, minutes: r.minutes });
    }
  }
  if (out.length > 0 || Array.isArray(body)) return out;
  if (isRecord(body)) {
    const map = isRecord(body.minutes) ? body.minutes : body;
    for (const [k, v] of Object.entries(map)) if (typeof v === "number") out.push({ deliverable_id: k, minutes: v });
  }
  return out;
}

// ---------------------------------------------------------------------------------------------------- resolve

export async function resolveAsset(path: string): Promise<ResolveResult> {
  const res = await postJson<unknown>("/assets/resolve", { path });
  if (!isRecord(res) || typeof res.content_hash !== "string") throw new Error("the daemon returned no match");
  return res as unknown as ResolveResult;
}
