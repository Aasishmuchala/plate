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
//   GET  /api/v0/engines                             -> EngineSummary[] | {engines: [...]}  (FALLBACK on 404)
//   GET  /api/v0/projects                            -> [{project_id, custody: {nda, allowed_grades}}] | {projects}

import { ApiError, fetchJson, postJson } from "../api/client";
import type { QuoteWire, RunWire } from "../canvas/runTypes";
import { type EngineSummary, FALLBACK_ENGINES, type ProjectCustody } from "./engines";

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
