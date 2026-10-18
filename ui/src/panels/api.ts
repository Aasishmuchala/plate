// Panel-side calls to the daemon, all through the token-carrying `fetchJson`. The routes are specified in T25 (runs,
// gate, projects), T26/T38 (resolve, verdict, scrub, timer, outbound); this file codes against those specs and
// tolerates the two common list shapes (a bare array, or an object holding the array) so a route that returns
// `{runs: [...]}` and one that returns `[...]` both work. Nothing here invents a route the plan does not list except
// `POST /runs/{id}/gate/rescore`, which is called out in the T36 report.

import { ApiError, apiUrl, CLIENT_HEADER, CLIENT_ID, fetchJson, getToken, postJson, putJson } from "../api";
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

/** The timer the daemon is running right now; `since` is when it started, in epoch milliseconds. */
export interface RunningTimer {
  deliverable_id: string;
  since: number;
}

/** `GET /timer/today`: today's minutes per deliverable and the running timer, if any. */
export interface TimerToday {
  rows: TimerRow[];
  running: RunningTimer | null;
}

/** `running` from the daemon is `{deliverable_id, started_at: ISO}`; `since` (epoch ms or ISO) is taken as well. */
function toRunning(raw: unknown): RunningTimer | null {
  if (!isRecord(raw) || typeof raw.deliverable_id !== "string" || raw.deliverable_id === "") return null;
  const at = raw.since ?? raw.started_at;
  const ms = typeof at === "number" ? at : typeof at === "string" ? Date.parse(at) : Number.NaN;
  return { deliverable_id: raw.deliverable_id, since: Number.isFinite(ms) ? ms : Date.now() };
}

/**
 * `GET /timer/today`: the rows from an array, `{rows|deliverables|today: [...]}`, or a `{deliverable: minutes}` map,
 * and `running` from `{running: {deliverable_id, started_at}}` (null when nothing runs).
 */
export async function getTimerToday(): Promise<TimerToday> {
  const body = await fetchJson<unknown>("/timer/today");
  const running = isRecord(body) ? toRunning(body.running) : null;
  const listed = asList<unknown>(body, "rows", "deliverables", "today");
  const rows: TimerRow[] = [];
  for (const r of listed) {
    if (isRecord(r) && typeof r.deliverable_id === "string" && typeof r.minutes === "number") {
      rows.push({ deliverable_id: r.deliverable_id, minutes: r.minutes });
    }
  }
  if (rows.length > 0 || Array.isArray(body)) return { rows, running };
  if (isRecord(body)) {
    const map = isRecord(body.minutes) ? body.minutes : body;
    for (const [k, v] of Object.entries(map)) if (typeof v === "number") rows.push({ deliverable_id: k, minutes: v });
  }
  return { rows, running };
}

/**
 * The deliverable named by a `409 timer_running` refusal (`{error: "timer_running", detail, running: {deliverable_id}}`):
 * another timer is already running and has to be stopped first. Null for any other error.
 */
export function timerConflict(err: unknown): string | null {
  if (!(err instanceof ApiError) || err.status !== 409 || !isRecord(err.body)) return null;
  if (err.body.error !== "timer_running") return null;
  const id = isRecord(err.body.running) ? err.body.running.deliverable_id : null;
  return typeof id === "string" && id !== "" ? id : null;
}

/** True for `409 timer_not_running`: the daemon has no timer, whatever this panel believed. */
export const timerNotRunning = (err: unknown): boolean =>
  err instanceof ApiError && err.status === 409 && isRecord(err.body) && err.body.error === "timer_not_running";

// ---------------------------------------------------------------------------------------------------- resolve

export async function resolveAsset(path: string): Promise<ResolveResult> {
  const res = await postJson<unknown>("/assets/resolve", { path });
  if (!isRecord(res) || typeof res.content_hash !== "string") throw new Error("the daemon returned no match");
  return res as unknown as ResolveResult;
}

// ---------------------------------------------------------------------------------------------------- T102: settings

/** One Project Sheet field in form order (`recipes/project_sheet.yaml`): the default is the input's placeholder. */
export interface SheetField {
  key: string;
  lensbook_key?: string;
  default: string;
}

/** `GET /projects/{id}/settings` (T88). */
export interface ProjectSettings {
  project_sheet: Record<string, string>;
  sheet_fields: SheetField[];
  default_engine: Record<string, string>;
  auto_carry_over: boolean;
  policy: Record<string, unknown>;
}

/** `PUT /projects/{id}/settings`: each key it carries replaces the stored one (a sheet field left out is its default). */
export interface SettingsPut {
  project_sheet?: Record<string, string>;
  default_engine?: Record<string, string>;
  auto_carry_over?: boolean;
}

const textMap = (v: unknown): Record<string, string> =>
  isRecord(v) ? Object.fromEntries(Object.entries(v).filter(([, x]) => typeof x === "string") as [string, string][]) : {};

function toSettings(raw: unknown): ProjectSettings {
  if (!isRecord(raw)) throw new Error("the daemon returned no project settings");
  const fields = asList<unknown>(raw.sheet_fields).flatMap((f): SheetField[] =>
    isRecord(f) && typeof f.key === "string"
      ? [{ key: f.key, lensbook_key: typeof f.lensbook_key === "string" ? f.lensbook_key : undefined, default: typeof f.default === "string" ? f.default : "" }]
      : [],
  );
  return {
    project_sheet: textMap(raw.project_sheet),
    sheet_fields: fields,
    default_engine: textMap(raw.default_engine),
    auto_carry_over: raw.auto_carry_over !== false,
    policy: isRecord(raw.policy) ? raw.policy : {},
  };
}

export async function getProjectSettings(projectId: string): Promise<ProjectSettings> {
  return toSettings(await fetchJson<unknown>(`/projects/${enc(projectId)}/settings`));
}

export async function putProjectSettings(projectId: string, body: SettingsPut): Promise<ProjectSettings> {
  return toSettings(await putJson<unknown>(`/projects/${enc(projectId)}/settings`, body));
}

// ---------------------------------------------------------------------------------------------------- T102: templates

/** One row of `GET /templates` (T95): `nodes` per packet's chain, `set` when it ends in one Set over every Edit. */
export interface TemplateSummary {
  id: string;
  title: string;
  nodes: number;
  set: boolean;
  origin?: string;
}

export async function listTemplates(projectId: string | null): Promise<TemplateSummary[]> {
  const body = await fetchJson<unknown>(`/templates${projectId ? `?project_id=${enc(projectId)}` : ""}`);
  return asList<unknown>(body, "templates").flatMap((t): TemplateSummary[] =>
    isRecord(t) && typeof t.id === "string"
      ? [
          {
            id: t.id,
            title: typeof t.title === "string" && t.title !== "" ? t.title : t.id,
            nodes: typeof t.nodes === "number" ? t.nodes : 0,
            set: t.set === true,
            origin: typeof t.origin === "string" ? t.origin : undefined,
          },
        ]
      : [],
  );
}

/** The fields of a `GET /projects/{id}/packets` row the template form shows. */
export interface PacketChoice {
  packet_id: string;
  shot_id?: string;
  camera_name?: string;
  tier?: string;
  received_utc?: string;
}

/** The project's ingested packets, oldest first (as the daemon orders them). */
export async function listProjectPackets(projectId: string): Promise<PacketChoice[]> {
  const body = await fetchJson<unknown>(`/projects/${enc(projectId)}/packets`);
  const str = (v: unknown): string | undefined => (typeof v === "string" && v !== "" ? v : undefined);
  return asList<unknown>(body, "packets").flatMap((p): PacketChoice[] =>
    isRecord(p) && typeof p.packet_id === "string"
      ? [{ packet_id: p.packet_id, shot_id: str(p.shot_id), camera_name: str(p.camera_name), tier: str(p.tier), received_utc: str(p.received_utc) }]
      : [],
  );
}

/** The fields of a `GET /recipes` row the template form offers. */
export interface RecipeChoice {
  id: string;
  title: string;
  kind?: string;
  engine?: string;
}

export async function listRecipeChoices(projectId: string | null): Promise<RecipeChoice[]> {
  const body = await fetchJson<unknown>(`/recipes${projectId ? `?project_id=${enc(projectId)}` : ""}`);
  return asList<unknown>(body, "recipes").flatMap((r): RecipeChoice[] =>
    isRecord(r) && typeof r.id === "string"
      ? [
          {
            id: r.id,
            title: typeof r.title === "string" && r.title !== "" ? r.title : r.id,
            kind: typeof r.kind === "string" ? r.kind : undefined,
            engine: typeof r.engine === "string" ? r.engine : undefined,
          },
        ]
      : [],
  );
}

/** `POST /projects/{id}/templates/apply`. */
export interface ApplyTemplateBody {
  template_id: string;
  packet_ids: string[];
  engine_id?: string;
  recipe?: string;
  quote: boolean;
  auto_approve: boolean;
}

export interface ApplyRefusal {
  node: string;
  error: string;
  detail?: string;
}

export interface ApplyResult {
  nodes_added: number;
  edits: string[];
  runs: RunRow[];
  total_usd: number;
  refused: ApplyRefusal[];
  awaiting_approval: ApplyRefusal[];
  /** The graph as the daemon stored it, when it sent it. */
  graph: LoadableGraph | null;
}

const refusals = (v: unknown): ApplyRefusal[] =>
  asList<unknown>(v).flatMap((r): ApplyRefusal[] =>
    isRecord(r) && typeof r.node === "string"
      ? [{ node: r.node, error: typeof r.error === "string" ? r.error : "refused", detail: typeof r.detail === "string" ? r.detail : undefined }]
      : [],
  );

export async function applyTemplate(projectId: string, body: ApplyTemplateBody): Promise<ApplyResult> {
  const res = await postJson<unknown>(`/projects/${enc(projectId)}/templates/apply`, body);
  if (!isRecord(res)) throw new Error("the daemon returned nothing for the template");
  const g = res.graph;
  const graph =
    isRecord(g) && Array.isArray(g.nodes) && Array.isArray(g.edges) ? ({ schema: 1, nodes: g.nodes, edges: g.edges } as LoadableGraph) : null;
  return {
    nodes_added: typeof res.nodes_added === "number" ? res.nodes_added : 0,
    edits: asList<unknown>(res.edits).filter((e): e is string => typeof e === "string"),
    runs: asList<RunRow>(res.runs).filter((r) => isRecord(r) && typeof r.id === "string"),
    total_usd: typeof res.total_usd === "number" ? res.total_usd : 0,
    refused: refusals(res.refused),
    awaiting_approval: refusals(res.awaiting_approval),
    graph,
  };
}

// ---------------------------------------------------------------------------------------------------- T102: spend

export type SpendBy = "engine" | "day" | "week";

/** One `series` row of `GET /projects/{id}/spend` (T88): one bucket (a week here) of one engine. */
export interface SpendRow {
  bucket: string;
  engine_id: string;
  runs: number;
  quoted_usd: number;
  billed_usd: number;
  accepted: number;
  /** Billed (quoted while nothing is billed) over accepted; null when nothing was accepted. */
  cost_per_accepted: number | null;
}

export interface SpendReport {
  by: string;
  spent_usd_quoted: number | null;
  spent_usd_billed: number | null;
  generations: number | null;
  accepted: number | null;
  series: SpendRow[];
  /** `GET /spend?all_projects=1`: the projects folded in. */
  projects?: string[];
}

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

function toSpend(raw: unknown): SpendReport {
  if (!isRecord(raw)) throw new Error("the daemon returned no spend report");
  const series = asList<unknown>(raw.series).flatMap((r): SpendRow[] =>
    isRecord(r) && typeof r.engine_id === "string"
      ? [
          {
            bucket: typeof r.bucket === "string" ? r.bucket : "unknown",
            engine_id: r.engine_id,
            runs: num(r.runs) ?? 0,
            quoted_usd: num(r.quoted_usd) ?? 0,
            billed_usd: num(r.billed_usd) ?? 0,
            accepted: num(r.accepted) ?? 0,
            cost_per_accepted: num(r.cost_per_accepted),
          },
        ]
      : [],
  );
  const projects = asList<unknown>(raw.projects, "project_ids").filter((p): p is string => typeof p === "string");
  return {
    by: typeof raw.by === "string" ? raw.by : "week",
    spent_usd_quoted: num(raw.spent_usd_quoted),
    spent_usd_billed: num(raw.spent_usd_billed),
    generations: num(raw.generations),
    accepted: num(raw.accepted),
    series,
    ...(projects.length > 0 ? { projects } : {}),
  };
}

export async function getProjectSpend(projectId: string, by: SpendBy = "week"): Promise<SpendReport> {
  return toSpend(await fetchJson<unknown>(`/projects/${enc(projectId)}/spend?by=${by}`));
}

/** Every project's spend; the daemon refuses this without the explicit `all_projects=1`. */
export async function getAllSpend(by: SpendBy = "week"): Promise<SpendReport> {
  return toSpend(await fetchJson<unknown>(`/spend?all_projects=1&by=${by}`));
}

// ---------------------------------------------------------------------------------------------------- T102: session, health, docs

/** `GET /session`: what this daemon was started with. */
export interface SessionInfo {
  daemon_version?: string;
  packet_versions?: string[];
  packets_dirs: string[];
  projects_dir: string | null;
  dev?: boolean;
  cas_copy?: boolean;
  router_mode?: string;
}

/** `GET /health` plus the optional fields a later daemon adds (router mode, nightly, which key env vars are set). */
export interface HealthInfo {
  ok?: boolean;
  daemon_version?: string;
  stub_engine?: boolean;
  pixels?: unknown;
  router_mode?: string;
  nightly?: unknown;
  /** The names of the vendor key env vars that are set; never their values. Null when /health does not say. */
  keys_set: string[] | null;
}

const strList = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

export async function getSession(): Promise<SessionInfo> {
  const raw = await fetchJson<unknown>("/session");
  if (!isRecord(raw)) throw new Error("the daemon returned no session");
  return {
    daemon_version: typeof raw.daemon_version === "string" ? raw.daemon_version : undefined,
    packet_versions: strList(raw.packet_versions),
    packets_dirs: strList(raw.packets_dirs),
    projects_dir: typeof raw.projects_dir === "string" ? raw.projects_dir : null,
    dev: typeof raw.dev === "boolean" ? raw.dev : undefined,
    cas_copy: typeof raw.cas_copy === "boolean" ? raw.cas_copy : undefined,
    router_mode: typeof raw.router_mode === "string" ? raw.router_mode : undefined,
  };
}

/** Key env var names from `/health`: a list of names, or a `{NAME: set?}` map (only the set ones are kept). */
export function keyNames(raw: unknown): string[] | null {
  if (Array.isArray(raw)) return strList(raw);
  if (isRecord(raw)) return Object.entries(raw).filter(([, v]) => v === true).map(([k]) => k);
  return null;
}

export async function getHealthInfo(): Promise<HealthInfo> {
  const raw = await fetchJson<unknown>("/health");
  if (!isRecord(raw)) throw new Error("the daemon returned no health");
  const keys = raw.keys_set ?? raw.env_keys ?? raw.keys ?? raw.api_keys;
  return {
    ok: typeof raw.ok === "boolean" ? raw.ok : undefined,
    daemon_version: typeof raw.daemon_version === "string" ? raw.daemon_version : undefined,
    stub_engine: typeof raw.stub_engine === "boolean" ? raw.stub_engine : undefined,
    pixels: raw.pixels,
    router_mode: typeof raw.router_mode === "string" ? raw.router_mode : undefined,
    nightly: raw.nightly,
    keys_set: keyNames(keys),
  };
}

/** The docs route a manual is served at (`GET /api/v0/docs/{name}`, token-gated). */
export const docUrl = (name: string): string => apiUrl(`/docs/${name.split("/").map(enc).join("/")}`);

/** A doc's raw Markdown, fetched with the token (a new tab cannot send the bearer header itself). */
export async function fetchDocText(name: string): Promise<string> {
  const headers = new Headers({ Accept: "text/markdown, text/plain" });
  headers.set(CLIENT_HEADER, CLIENT_ID);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(docUrl(name), { headers });
  const text = await res.text();
  if (!res.ok) throw new ApiError(res.status, text, `GET ${docUrl(name)} -> ${res.status}`);
  return text;
}
