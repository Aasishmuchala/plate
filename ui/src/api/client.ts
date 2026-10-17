// Daemon API client. The per-install bearer token arrives once in the URL fragment (`#token=...`, printed by the
// daemon on start), is moved into sessionStorage and sent as `Authorization: Bearer`. EventSource cannot set
// headers, so the SSE helper alone puts the token in the query string; the daemon accepts `?token=` on
// `GET /api/v0/events` and nowhere else (T25 route note).

export const API_PREFIX = "/api/v0";
export const TOKEN_KEY = "plate.token";

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
}

/** `/health` -> `/api/v0/health`; a path that already starts with the prefix is left alone. */
export function apiUrl(path: string): string {
  if (path === API_PREFIX || path.startsWith(`${API_PREFIX}/`)) return path;
  return `${API_PREFIX}${path.startsWith("/") ? "" : "/"}${path}`;
}

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, body: unknown, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

/** JSON request with the bearer token. Non-2xx throws `ApiError` carrying the parsed body. */
export async function fetchJson<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  const token = getToken();
  if (token && !headers.has("Authorization")) headers.set("Authorization", `Bearer ${token}`);
  if (init.body !== undefined && init.body !== null && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(apiUrl(path), { ...init, headers });
  const text = await res.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!res.ok) throw new ApiError(res.status, body, `${init.method ?? "GET"} ${apiUrl(path)} -> ${res.status}`);
  return body as T;
}

export const postJson = <T = unknown>(path: string, body: unknown): Promise<T> =>
  fetchJson<T>(path, { method: "POST", body: JSON.stringify(body) });

export const putJson = <T = unknown>(path: string, body: unknown): Promise<T> =>
  fetchJson<T>(path, { method: "PUT", body: JSON.stringify(body) });

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
