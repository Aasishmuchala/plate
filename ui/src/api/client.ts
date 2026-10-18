// Daemon API client. The per-install bearer token arrives once in the URL fragment (`#token=...`, printed by the
// daemon on start), is moved into sessionStorage and sent as `Authorization: Bearer`. EventSource cannot set
// headers, so the SSE helper alone puts the token in the query string; the daemon accepts `?token=` on
// `GET /api/v0/events` and nowhere else (T25 route note). Every request also says `X-Plate-Client: ui`: that header
// is how the daemon tells a person at the canvas from MCP, curl or the Max plugin (decision 37).
//
// T147. The token state is honest: `tokenState()` is `none` without a token, `rejected` once the daemon answered 401
// to the token this tab holds (until an authenticated call with it succeeds or another token is set), else
// `present`; `setToken` stores a pasted one. A non-JSON error body (a proxy's HTML page) never reaches the artist:
// its `ApiError` carries no body and the message `"<status> <statusText> (non-JSON response)"`. The graph carries
// the daemon's `revision` (decision 77): `getGraph` keeps it, `saveGraph` sends the one the graph was read at.

import type { Graph, LoadableGraph } from "../nodeTypes";

export const API_PREFIX = "/api/v0";
export const TOKEN_KEY = "plate.token";

/** Sent on every UI request; only these requests (and approvals `by: "human"`) count as a human at the canvas. */
export const CLIENT_HEADER = "X-Plate-Client";
export const CLIENT_ID = "ui";

export interface TokenStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface TokenSource {
  location: { hash: string; pathname: string; search: string };
  storage: TokenStorage;
  /** Used to drop the token from the visible URL once it is stored. */
  replaceState: (url: string) => void;
}

function browserSource(): TokenSource {
  return {
    location: window.location,
    storage: window.sessionStorage,
    replaceState: (url) => window.history.replaceState(null, "", url),
  };
}

/** The value of `token` in a URL fragment such as `#token=abc` or `#a=1&token=abc`; null when absent or empty. */
export function parseTokenFromHash(hash: string): string | null {
  const raw = hash.startsWith("#") ? hash.slice(1) : hash;
  const token = new URLSearchParams(raw).get("token");
  return token ? token : null;
}

/**
 * Call once at startup. A token in the fragment wins: it is stored in sessionStorage and removed from the address
 * bar. Otherwise the stored token (a reload) is returned. Null means this tab has no token.
 */
export function captureToken(source: TokenSource = browserSource()): string | null {
  const fromHash = parseTokenFromHash(source.location.hash);
  if (fromHash) {
    source.storage.setItem(TOKEN_KEY, fromHash);
    source.replaceState(source.location.pathname + source.location.search);
    return fromHash;
  }
  return source.storage.getItem(TOKEN_KEY);
}

export function getToken(storage: TokenStorage = window.sessionStorage): string | null {
  return storage.getItem(TOKEN_KEY);
}

export function clearToken(storage: TokenStorage = window.sessionStorage): void {
  storage.removeItem(TOKEN_KEY);
  notifyToken();
}

// ---------------------------------------------------------------------------- token state (T147)

/** What the status bar says about this tab's token. */
export type TokenState = "present" | "rejected" | "none";

/** The token the daemon last answered 401 to (null: none was refused, or a later call with it succeeded). */
let rejectedToken: string | null = null;
/** Bumped by `setToken`, so the shell can re-read everything with the new token. */
let generation = 0;
const tokenListeners = new Set<() => void>();

function notifyToken(): void {
  for (const listener of [...tokenListeners]) listener();
}

export function tokenState(storage: TokenStorage = window.sessionStorage): TokenState {
  const token = getToken(storage);
  if (!token) return "none";
  return token === rejectedToken ? "rejected" : "present";
}

/** How many times a token was set in this tab (a paste). */
export const tokenGeneration = (): number => generation;

/** Called whenever the token or its state changes; returns the unsubscribe function. */
export function onTokenChange(listener: () => void): () => void {
  tokenListeners.add(listener);
  return () => {
    tokenListeners.delete(listener);
  };
}

/** Store a token typed or pasted by the artist, as `captureToken` stores the fragment's. */
export function setToken(token: string, storage: TokenStorage = window.sessionStorage): void {
  storage.setItem(TOKEN_KEY, token);
  if (rejectedToken === token) rejectedToken = null;
  generation += 1;
  notifyToken();
}

/**
 * The token in what the artist pasted: the daemon's whole start line or URL (`...#token=abc`), the fragment
 * (`#token=abc`, `token=abc`) or the bare token. Null when there is none (empty, or whitespace inside).
 */
export function tokenFromPaste(text: string): string | null {
  const raw = text.trim();
  if (raw === "") return null;
  const at = raw.indexOf("#");
  if (at >= 0 || raw.includes("token=")) {
    const fragment = at >= 0 ? raw.slice(at) : raw.slice(raw.indexOf("token="));
    return parseTokenFromHash(fragment.split(/\s/)[0]);
  }
  return /\s/.test(raw) ? null : raw;
}

/** `/health` answers without a token, so its 2xx says nothing about the token; every other route checks it. */
const HEALTH_PATH = "/api/v0/health";

function noteAuth(status: number, sent: string | null, url: string): void {
  if (sent === null) return;
  if (status === 401) {
    if (rejectedToken !== sent) {
      rejectedToken = sent;
      notifyToken();
    }
  } else if (status >= 200 && status < 300 && url !== HEALTH_PATH && rejectedToken === sent) {
    rejectedToken = null;
    notifyToken();
  }
}

/** `/health` -> `/api/v0/health`; a path that already starts with the prefix is left alone. */
export function apiUrl(path: string): string {
  if (path === API_PREFIX || path.startsWith(`${API_PREFIX}/`)) return path;
  return `${API_PREFIX}${path.startsWith("/") ? "" : "/"}${path}`;
}

/** One canvas-rule refusal in a daemon error body: an edge rule (`edge`) or a node problem (`node`). */
export interface ApiViolation {
  edge?: { id: string; source?: string; target?: string };
  node?: string | null;
  code: string;
  message: string;
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;

function violationsOf(body: unknown): ApiViolation[] | null {
  if (!isRecord(body) || !Array.isArray(body.violations)) return null;
  return body.violations.filter(
    (v): v is ApiViolation => isRecord(v) && typeof v.code === "string" && typeof v.message === "string",
  );
}

export class ApiError extends Error {
  status: number;
  body: unknown;
  /** `body.violations` of a canvas-rule refusal (`PUT /projects/{id}/graph` answers 422 with them); else null. */
  violations: ApiViolation[] | null;
  constructor(status: number, body: unknown, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.violations = violationsOf(body);
  }
}

/** `502 Bad Gateway (non-JSON response)`: what a failed call whose body is not JSON says, never the body itself. */
export function nonJsonStatusLine(status: number, statusText?: string | null): string {
  return `${[String(status), statusText ?? ""].filter((part) => part !== "").join(" ")} (non-JSON response)`;
}

/**
 * JSON request with the bearer token. Non-2xx throws `ApiError` carrying the parsed body; a body that is not JSON
 * (a proxy's HTML error page) is dropped and the message is the status line. A 2xx text body is returned as text.
 */
export async function fetchJson<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  headers.set(CLIENT_HEADER, CLIENT_ID);
  const token = getToken();
  if (token && !headers.has("Authorization")) headers.set("Authorization", `Bearer ${token}`);
  if (init.body !== undefined && init.body !== null && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const url = apiUrl(path);
  const res = await fetch(url, { ...init, headers });
  const sent = headers.get("Authorization")?.replace(/^Bearer /, "") ?? null;
  noteAuth(res.status, sent, url);
  const text = await res.text();
  let body: unknown = null;
  let json = true;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
      json = false;
    }
  }
  if (!res.ok) {
    if (!json) throw new ApiError(res.status, null, nonJsonStatusLine(res.status, res.statusText));
    throw new ApiError(res.status, body, `${init.method ?? "GET"} ${url} -> ${res.status}`);
  }
  return body as T;
}

export const postJson = <T = unknown>(path: string, body: unknown): Promise<T> =>
  fetchJson<T>(path, { method: "POST", body: JSON.stringify(body) });

export const putJson = <T = unknown>(path: string, body: unknown): Promise<T> =>
  fetchJson<T>(path, { method: "PUT", body: JSON.stringify(body) });

/** A graph as the daemon stores it: `revision` (decision 77) counts its saves; absent from an older daemon. */
export type StoredGraph = LoadableGraph & { revision?: number };

const isRevision = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v) && v >= 0;

/** The `revision` a stored graph carries, or null when it carries none. */
export const revisionOf = (graph: unknown): number | null =>
  isRecord(graph) && isRevision(graph.revision) ? graph.revision : null;

export interface SaveOptions {
  /** `fetch(..., {keepalive: true})`: the request outlives the page (a flush on pagehide). */
  keepalive?: boolean;
}

/**
 * `PUT /projects/{id}/graph`: store the canvas graph, built on `revision` (the one the graph was read at; null sends
 * none, which the daemon accepts as an old client). The daemon answers `{ok, project_id, graph}` with the graph as
 * stored, whose node status fields (`gateStatus`, `approved`, `approvedBy`, `scrubLogId`) it derived itself
 * (decision 31) and whose `revision` is the new one; a bare graph is accepted too, and a body with no graph falls
 * back to what was sent. A rule refusal throws an `ApiError` (422) whose `violations` name the edge; a stale
 * revision is a 409 `graph_conflict` with the daemon's `current`.
 */
export async function saveGraph(
  projectId: string,
  graph: Graph,
  revision: number | null = null,
  opts: SaveOptions = {},
): Promise<StoredGraph> {
  const path = `/projects/${encodeURIComponent(projectId)}/graph`;
  const payload = JSON.stringify(revision === null ? graph : { ...graph, revision });
  const init: RequestInit = { method: "PUT", body: payload };
  let body: unknown;
  if (opts.keepalive) {
    try {
      body = await fetchJson<unknown>(path, { ...init, keepalive: true });
    } catch (err) {
      // A keepalive body over the browser's 64 KiB quota is refused before it leaves: send it plainly instead.
      if (err instanceof ApiError) throw err;
      body = await fetchJson<unknown>(path, init);
    }
  } else {
    body = await fetchJson<unknown>(path, init);
  }
  const stored = isRecord(body) && isRecord(body.graph) ? body.graph : body;
  if (isRecord(stored) && Array.isArray(stored.nodes) && Array.isArray(stored.edges)) return stored as unknown as StoredGraph;
  return graph;
}

/** `GET /projects/{id}/graph` with its `revision`: the graph itself or `{graph}`. */
export async function getGraph(projectId: string): Promise<StoredGraph> {
  const body = await fetchJson<unknown>(`/projects/${encodeURIComponent(projectId)}/graph`);
  const graph = isRecord(body) && isRecord(body.graph) ? body.graph : body;
  if (!isRecord(graph) || !Array.isArray(graph.nodes) || !Array.isArray(graph.edges)) {
    throw new Error(`project ${projectId}: the daemon returned no graph`);
  }
  const revision = revisionOf(graph) ?? (isRecord(body) ? revisionOf(body) : null);
  return {
    schema: 1,
    nodes: graph.nodes,
    edges: graph.edges,
    ...(revision !== null ? { revision } : {}),
  } as StoredGraph;
}

/** `GET /api/v0/health` (no token needed): the plugin's version handshake, also the status bar's heartbeat. */
export interface Health {
  ok: boolean;
  daemon_version?: string;
  packet_versions?: string[];
  stub_engine?: boolean;
}

export const getHealth = (): Promise<Health> => fetchJson<Health>("/health");

/** One server-sent event: `event` is the SSE event name ("message" when the server sends none). */
export interface RunEvent {
  event: string;
  id?: string;
  data: unknown;
}

/** The slice of EventSource the helper uses, so tests can inject a fake. */
export interface EventSourceLike {
  onmessage: ((ev: MessageEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
  addEventListener(type: string, listener: (ev: MessageEvent) => void): void;
  close(): void;
}

export interface SseOptions {
  /** Named SSE events to listen for besides unnamed "message" events. */
  events?: string[];
  onError?: (ev: Event) => void;
  token?: string | null;
  create?: (url: string) => EventSourceLike;
}

/** `/api/v0/events?run_id=...&token=...`: the only URL that carries the token in its query string. */
export function sseUrl(runId: string | null, token: string | null): string {
  const q = new URLSearchParams();
  if (runId) q.set("run_id", runId);
  if (token) q.set("token", token);
  const qs = q.toString();
  return `${API_PREFIX}/events${qs ? `?${qs}` : ""}`;
}

/** Subscribe to a run's events. Returns the unsubscribe function. */
export function sse(runId: string, onEvent: (ev: RunEvent) => void, opts: SseOptions = {}): () => void {
  const token = opts.token === undefined ? getToken() : opts.token;
  const url = sseUrl(runId, token);
  const source = opts.create ? opts.create(url) : (new EventSource(url) as unknown as EventSourceLike);
  const deliver = (ev: MessageEvent, name: string) => {
    let data: unknown = ev.data;
    if (typeof ev.data === "string") {
      try {
        data = JSON.parse(ev.data);
      } catch {
        data = ev.data;
      }
    }
    const id = typeof ev.lastEventId === "string" && ev.lastEventId !== "" ? ev.lastEventId : undefined;
    onEvent({ event: name, id, data });
  };
  source.onmessage = (ev) => deliver(ev, "message");
  for (const name of opts.events ?? []) source.addEventListener(name, (ev) => deliver(ev, name));
  if (opts.onError) source.onerror = opts.onError;
  return () => source.close();
}
