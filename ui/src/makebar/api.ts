// Daemon calls behind the Make bar (T25 route contract): quote a node, fan the quoted run out to N siblings,
// approve, plus the engine list and the project's custody. Responses are normalised here so the bar only sees
// `RunWire` / `QuoteWire` / `EngineSummary`.
//
//   POST /api/v0/projects/{pid}/nodes/{node}/quote  {engine_id, preset, region_crop, params, prompt?, recipe?,
//                                                    task?, shot_class?}  -> {run, quote} | Run   (never `attempt`:
//                                                    the daemon numbers attempts itself, decision 40)
//   POST /api/v0/runs/{id}/fanout {n}               -> Run[] | {runs: Run[]}   (sibling 0 is the quoted run)
//   POST /api/v0/runs/{id}/approve {by, envelope_id?, reason?}  approves the run's whole sibling group; refusals are
//                                                    409/422 {error, detail} with the codes in HUMAN_REFUSALS
//   GET  /api/v0/engines                             -> EngineSummary[] | {engines: [...]}  (FALLBACK on 404); each
//                                                    engine may carry `capabilities` and per-route `health` (T83)
//   GET  /api/v0/engines/cards?project_id=&task=&shot_class=  -> EngineCard[]  (Wilson-bounded, T83; [] on any error)
//   POST /api/v0/projects/{pid}/nodes/{node}/quote_many {engine_ids, ...QuoteRequest but engine_id}
//                                                    -> {sibling_group, runs: Run[], total_usd}  (T95; one group
//                                                    across engines, approved once through the primary)
//   GET  /api/v0/projects                            -> [{project_id, custody: {nda, allowed_grades}}] | {projects}

import { ApiError, fetchJson, postJson } from "../api/client";
import type { QuoteWire, RunWire } from "../canvas/runTypes";
import {
  allowedGrades,
  effectiveGrade,
  type EngineRouteSummary,
  type EngineSummary,
  FALLBACK_ENGINES,
  type ProjectCustody,
} from "./engines";

const enc = encodeURIComponent;

export interface QuoteRequest {
  engine_id: string | null;
  preset: string;
  region_crop: boolean;
  params: Record<string, unknown>;
  /** From the node's params when set: an explicit prompt wins over a recipe (decision 33). */
  prompt?: string;
  recipe?: string;
  task?: string;
  shot_class?: string;
}

/** The node params a quote carries at the top level of its body. */
export const QUOTE_NODE_FIELDS = ["prompt", "recipe", "task", "shot_class"] as const;

/** The approval refusals that put the approval dialog in front of the artist with the daemon's reason. */
export const HUMAN_REFUSALS: ReadonlySet<string> = new Set([
  "auto_cap",
  "envelope_required",
  "auto_approval_denied",
  "approval_refused",
  "daily_cap",
  "unattended_cap",
]);

/** Of those, the ones no click can approve today: the dialog only says why. */
export const BLOCKING_REFUSALS: ReadonlySet<string> = new Set(["daily_cap", "unattended_cap"]);

export interface QuoteResponse {
  run: RunWire;
  quote: QuoteWire;
  /** The raw body, for fields such as `auto_cap_usd`. */
  body: unknown;
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;

/** `{run, quote}`, or a Run with its quote inline. */
export function normaliseQuote(body: unknown): QuoteResponse {
  if (isObj(body) && isObj(body.run) && isObj(body.quote)) {
    const run = body.run as unknown as RunWire;
    const quote = body.quote as unknown as QuoteWire;
    return { run: run.quote ? run : { ...run, quote }, quote, body };
  }
  if (isObj(body) && typeof body.id === "string" && isObj(body.quote)) {
    const run = body as unknown as RunWire;
    return { run, quote: run.quote as QuoteWire, body };
  }
  throw new Error("the daemon's quote response has no run and quote");
}

export function normaliseRuns(body: unknown): RunWire[] {
  const list = Array.isArray(body) ? body : isObj(body) && Array.isArray(body.runs) ? body.runs : null;
  if (!list) throw new Error("the daemon's fan-out response has no runs");
  return list as RunWire[];
}

/** A daemon refusal as one line: `409 quote_expired: …`. */
export function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    const b = err.body;
    if (isObj(b)) {
      const code = typeof b.error === "string" ? b.error : null;
      const detail =
        typeof b.detail === "string"
          ? b.detail
          : Array.isArray(b.errors)
            ? b.errors.map(String).join("; ")
            : typeof b.message === "string"
              ? b.message
              : null;
      return [String(err.status), [code, detail].filter(Boolean).join(": ")].filter(Boolean).join(" ");
    }
    return typeof b === "string" && b ? `${err.status} ${b}` : err.message;
  }
  return err instanceof Error ? err.message : String(err);
}

/** The daemon's own sentence for a refusal (`detail`), else the one-line `errorText`. */
export function errorDetail(err: unknown): string {
  if (err instanceof ApiError && isObj(err.body) && typeof err.body.detail === "string" && err.body.detail) return err.body.detail;
  return errorText(err);
}

/** The refusal code of a daemon error body (`auto_cap`, `envelope_required`, `quote_expired`, …). */
export function errorCode(err: unknown): string | null {
  if (err instanceof ApiError && isObj(err.body)) {
    const b = err.body;
    if (typeof b.error === "string") return b.error;
    if (typeof b.code === "string") return b.code;
  }
  return null;
}

export async function quoteNode(projectId: string, nodeId: string, req: QuoteRequest): Promise<QuoteResponse> {
  return normaliseQuote(await postJson(`/projects/${enc(projectId)}/nodes/${enc(nodeId)}/quote`, req));
}

export async function fanoutRun(runId: string, n: number): Promise<RunWire[]> {
  return normaliseRuns(await postJson(`/runs/${enc(runId)}/fanout`, { n }));
}

export interface ApproveRequest {
  by: "human" | "auto";
  envelope_id?: string;
  reason?: string;
}

export async function approveRun(runId: string, req: ApproveRequest): Promise<RunWire[] | null> {
  const body = await postJson<unknown>(`/runs/${enc(runId)}/approve`, req);
  if (Array.isArray(body)) return body as RunWire[];
  if (isObj(body) && Array.isArray(body.runs)) return body.runs as RunWire[];
  if (isObj(body) && typeof body.id === "string") return [body as unknown as RunWire];
  return null;
}

export interface EngineList {
  engines: EngineSummary[];
  /** "daemon" when `/engines` answered, "fallback" when the build's catalogue is used. */
  source: "daemon" | "fallback";
}

export async function listEngines(): Promise<EngineList> {
  try {
    const body = await fetchJson<unknown>("/engines");
    const list = Array.isArray(body) ? body : isObj(body) && Array.isArray(body.engines) ? body.engines : null;
    if (list) return { engines: list as EngineSummary[], source: "daemon" };
  } catch {
    // no /engines route yet (404), or the daemon is down: the build's catalogue keeps the bar usable
  }
  return { engines: [...FALLBACK_ENGINES], source: "fallback" };
}

/** The open project's custody from `GET /projects`; null when the daemon does not say. */
export async function projectCustody(projectId: string): Promise<ProjectCustody | null> {
  try {
    const body = await fetchJson<unknown>("/projects");
    const list = Array.isArray(body) ? body : isObj(body) && Array.isArray(body.projects) ? body.projects : [];
    for (const p of list) {
      if (!isObj(p)) continue;
      const id = p.project_id ?? p.id;
      if (id !== projectId || !isObj(p.custody)) continue;
      const c = p.custody;
      if (typeof c.nda !== "boolean") return null;
      const grades = Array.isArray(c.allowed_grades) ? (c.allowed_grades as ProjectCustody["allowed_grades"]) : [];
      return { nda: c.nda, allowed_grades: grades };
    }
  } catch {
    // unknown custody: the bar shows every engine and says the daemon decides
  }
  return null;
}

// ---------------------------------------------------------------------------------------------------- Wave 9 (T102)

/** `capabilities` of a `GET /engines` entry (T83): what the manifest declares, for the Make bar's chips. */
export interface EngineCapabilities {
  /** "WxH" of the biggest output size the manifest declares; null for an auto-only or size-less engine. */
  largest_size: string | null;
  /** Takes an end frame (`image_tail` / `end_image`): a start/end motion engine. */
  image_tail: boolean;
  /** How many reference images it takes (0: none). */
  reference_images_max: number;
  elements: boolean;
  /** The manifest's price model: `per_image`, `per_second`, `per_megapixel` or `flat`. */
  price_unit: string;
  /** False while the manifest names no vendor model id: the daemon refuses to submit it. */
  runnable: boolean;
  /** The manifest carries `unverified_p0`: nothing about it was checked against the vendor yet. */
  unverified: boolean;
}

/** A route's health on the daemon's board (T83/T85): degraded routes are shown and never routed to. */
export type RouteHealth = "ok" | "degraded" | "down" | "suspended";

/** A route as the Wave 9 daemon lists it: the T35 summary plus its health on the board. */
export type HealthRoute = EngineRouteSummary & { health?: RouteHealth };

/** An engine as the Wave 9 daemon lists it: the T35 summary plus capabilities and route health, all optional. */
export type EngineEntry = Omit<EngineSummary, "routes"> & {
  capabilities?: Partial<EngineCapabilities>;
  routes: HealthRoute[];
};

/** The engine's capabilities, or null when the daemon sent none (the build's fallback catalogue). */
export function capabilitiesOf(engine: EngineSummary | undefined): Partial<EngineCapabilities> | null {
  const caps = (engine as unknown as EngineEntry | undefined)?.capabilities;
  return caps && typeof caps === "object" ? caps : null;
}

/** False only when the daemon says so: an engine without capabilities is assumed runnable (the daemon decides). */
export const isRunnable = (engine: EngineSummary | undefined): boolean => capabilitiesOf(engine)?.runnable !== false;

export const NOT_RUNNABLE_TITLE = "model id unknown until the P0 smoke test";

export interface EngineHealth {
  /** `ok`, or the reading worth showing: `degraded` (amber), `suspended` (red, not selectable) or `down` (red). */
  status: RouteHealth;
  /** The routes the reading came from (`atlas`, `gemini`, ...). */
  routes: string[];
}

/** Route health over the routes this project may use (active, allowed grade): suspended only when every one is,
 * degraded when any one is (the router skips it, another route may still serve), down when none has a signal. */
export function engineHealth(engine: EngineSummary, custody: ProjectCustody | null): EngineHealth {
  const ok = allowedGrades(custody);
  const routes = (engine.routes as HealthRoute[]).filter(
    (r) => (r.status ?? "active") === "active" && ok.includes(effectiveGrade(r)),
  );
  const named = (rs: HealthRoute[]) => rs.map((r) => r.route_id ?? r.provider);
  const health = (r: HealthRoute): RouteHealth => r.health ?? "ok";
  if (routes.length === 0) return { status: "ok", routes: [] };
  if (routes.every((r) => health(r) === "suspended")) return { status: "suspended", routes: named(routes) };
  const degraded = routes.filter((r) => health(r) === "degraded");
  if (degraded.length > 0) return { status: "degraded", routes: named(degraded) };
  if (routes.every((r) => health(r) === "down" || health(r) === "suspended")) return { status: "down", routes: named(routes) };
  return { status: "ok", routes: [] };
}

/** Why an engine cannot be picked right now, or null when it can. */
export function unavailableReason(engine: EngineSummary, custody: ProjectCustody | null): string | null {
  if (!isRunnable(engine)) return NOT_RUNNABLE_TITLE;
  const h = engineHealth(engine, custody);
  if (h.status === "suspended") return `suspended on ${h.routes.join(", ")}: the daemon will not route to it`;
  return null;
}

/** One Wilson-bounded engine card (`plate_engines.cards.Card.to_dict()` plus `steering`). */
export interface EngineCard {
  engine_id: string;
  task: string;
  shot_class: string | null;
  n: number;
  passes: number;
  pass_rate: number;
  wilson_lower_95: number;
  determinism_var?: number | null;
  mean_cost: number | null;
  custody?: string;
  /** n has reached the router's threshold: this card ranks engines. */
  steering: boolean;
}

function toCard(raw: unknown): EngineCard | null {
  if (!isObj(raw) || typeof raw.engine_id !== "string" || typeof raw.n !== "number") return null;
  const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
  return {
    engine_id: raw.engine_id,
    task: typeof raw.task === "string" ? raw.task : "",
    shot_class: typeof raw.shot_class === "string" ? raw.shot_class : null,
    n: raw.n,
    passes: num(raw.passes) ?? 0,
    pass_rate: num(raw.pass_rate) ?? 0,
    wilson_lower_95: num(raw.wilson_lower_95) ?? 0,
    determinism_var: num(raw.determinism_var),
    mean_cost: num(raw.mean_cost),
    custody: typeof raw.custody === "string" ? raw.custody : undefined,
    steering: raw.steering === true,
  };
}

/** `GET /engines/cards` for the node's task and shot class. A daemon without the route, or any failure, is no
 * cards: the bar then shows the manifest facts alone. */
export async function listCards(projectId: string, task?: string | null, shotClass?: string | null): Promise<EngineCard[]> {
  const q = new URLSearchParams({ project_id: projectId });
  if (task) q.set("task", task);
  if (shotClass) q.set("shot_class", shotClass);
  try {
    const body = await fetchJson<unknown>(`/engines/cards?${q.toString()}`);
    const list = Array.isArray(body) ? body : isObj(body) && Array.isArray(body.cards) ? body.cards : [];
    return list.flatMap((c) => toCard(c) ?? []);
  } catch {
    return [];
  }
}

/** The card the bar shows for an engine: a steering one first, then the one with the most runs. */
export function cardFor(cards: readonly EngineCard[] | undefined, engineId: string): EngineCard | null {
  const mine = (cards ?? []).filter((c) => c.engine_id === engineId && c.n > 0);
  mine.sort((a, b) => Number(b.steering) - Number(a.steering) || b.n - a.n);
  return mine[0] ?? null;
}

/** `quote_many`'s body: the quote body with `engine_ids` (2 to 5, distinct) instead of `engine_id`. */
export type QuoteManyRequest = Omit<QuoteRequest, "engine_id"> & { engine_ids: string[] };

export interface QuoteManyResponse {
  group: string | null;
  runs: RunWire[];
  /** The daemon's sum of the quotes, when it sent one. */
  totalUsd: number | null;
}

export const COMPARE_MIN = 2;
export const COMPARE_MAX = 5;

export async function quoteMany(projectId: string, nodeId: string, body: QuoteManyRequest): Promise<QuoteManyResponse> {
  const res = await postJson<unknown>(`/projects/${enc(projectId)}/nodes/${enc(nodeId)}/quote_many`, body);
  const runs = normaliseRuns(res);
  const group = isObj(res) && typeof res.sibling_group === "string" ? res.sibling_group : null;
  const total = isObj(res) && typeof res.total_usd === "number" && Number.isFinite(res.total_usd) ? res.total_usd : null;
  return { group, runs, totalUsd: total };
}

/** The engine a run of a group was quoted on (`run.route.engine_id`), else its quote's model. */
export function runEngineId(run: RunWire): string {
  const route = (run as RunWire & { route?: { engine_id?: string | null } | null }).route;
  return route?.engine_id ?? run.quote?.model ?? run.id;
}

/** The engine id a refused `quote_many` names (the daemon adds `engine_id` to the refusal body). */
export function refusedEngine(err: unknown): string | null {
  return err instanceof ApiError && isObj(err.body) && typeof err.body.engine_id === "string" ? err.body.engine_id : null;
}
