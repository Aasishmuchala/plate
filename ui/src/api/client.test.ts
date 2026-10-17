import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  apiUrl,
  captureToken,
  clearToken,
  fetchJson,
  getHealth,
  parseTokenFromHash,
  sse,
  sseUrl,
  TOKEN_KEY,
  type EventSourceLike,
  type TokenStorage,
} from "./client";

function memoryStorage(initial: Record<string, string> = {}): TokenStorage & { map: Map<string, string> } {
  const map = new Map(Object.entries(initial));
  return {
    map,
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => void map.set(k, v),
    removeItem: (k) => void map.delete(k),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
});

describe("token from the URL fragment", () => {
  it("parses #token=...", () => {
    expect(parseTokenFromHash("#token=abc123")).toBe("abc123");
    expect(parseTokenFromHash("token=abc123")).toBe("abc123");
    expect(parseTokenFromHash("#a=1&token=xyz&b=2")).toBe("xyz");
  });

  it("returns null for a missing or empty token", () => {
    expect(parseTokenFromHash("")).toBeNull();
    expect(parseTokenFromHash("#")).toBeNull();
    expect(parseTokenFromHash("#other=1")).toBeNull();
    expect(parseTokenFromHash("#token=")).toBeNull();
  });

  it("decodes percent-escapes", () => {
    expect(parseTokenFromHash("#token=a%2Bb%3D")).toBe("a+b=");
  });

  it("captureToken stores the fragment token in sessionStorage and strips it from the address bar", () => {
    const storage = memoryStorage();
    const replaceState = vi.fn();
    const got = captureToken({
      location: { hash: "#token=secret", pathname: "/", search: "?x=1" },
      storage,
      replaceState,
    });
    expect(got).toBe("secret");
    expect(storage.map.get(TOKEN_KEY)).toBe("secret");
    expect(replaceState).toHaveBeenCalledWith("/?x=1");
  });

  it("captureToken falls back to the stored token on reload, and a new fragment token wins", () => {
    const storage = memoryStorage({ [TOKEN_KEY]: "old" });
    const src = (hash: string) => ({ location: { hash, pathname: "/", search: "" }, storage, replaceState: vi.fn() });
    expect(captureToken(src(""))).toBe("old");
    expect(captureToken(src("#token=new"))).toBe("new");
    expect(storage.map.get(TOKEN_KEY)).toBe("new");
  });

  it("captureToken returns null when the tab has no token", () => {
    const storage = memoryStorage();
    expect(captureToken({ location: { hash: "", pathname: "/", search: "" }, storage, replaceState: vi.fn() })).toBeNull();
  });
});

describe("fetchJson", () => {
  it("prefixes /api/v0 once and sends the bearer token from sessionStorage", async () => {
    window.sessionStorage.setItem(TOKEN_KEY, "tok");
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchJson("/projects")).resolves.toEqual({ ok: true });
    await fetchJson("/api/v0/projects");
    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(calls[0][0]).toBe("/api/v0/projects");
    expect(calls[1][0]).toBe("/api/v0/projects");
    expect(new Headers(calls[0][1].headers).get("Authorization")).toBe("Bearer tok");
  });

  it("sends no Authorization header without a token and never puts the token in the URL", async () => {
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await fetchJson("/health");
    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(new Headers(calls[0][1].headers).has("Authorization")).toBe(false);
    expect(calls[0][0]).not.toContain("token");
  });

  it("throws ApiError with the status and parsed body on a non-2xx response", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ code: "NO_TOKEN" }), { status: 401 })));
    const err = await fetchJson("/projects").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(401);
    expect((err as ApiError).body).toEqual({ code: "NO_TOKEN" });
  });

  it("sets a JSON content type on a request with a body", async () => {
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await fetchJson("/runs", { method: "POST", body: "{}" });
    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(new Headers(calls[0][1].headers).get("Content-Type")).toBe("application/json");
  });

  it("getHealth reads /api/v0/health", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true, daemon_version: "0.1.0" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(getHealth()).resolves.toMatchObject({ ok: true, daemon_version: "0.1.0" });
    expect((fetchMock.mock.calls as unknown as [string][])[0][0]).toBe("/api/v0/health");
  });

  it("clearToken forgets the token", () => {
    window.sessionStorage.setItem(TOKEN_KEY, "tok");
    clearToken();
    expect(window.sessionStorage.getItem(TOKEN_KEY)).toBeNull();
  });
});

describe("apiUrl", () => {
  it("adds the prefix to relative paths and leaves prefixed ones alone", () => {
    expect(apiUrl("health")).toBe("/api/v0/health");
    expect(apiUrl("/health")).toBe("/api/v0/health");
    expect(apiUrl("/api/v0/health")).toBe("/api/v0/health");
  });
});

describe("sse", () => {
  class FakeEventSource implements EventSourceLike {
    onmessage: ((ev: MessageEvent) => void) | null = null;
    onerror: ((ev: Event) => void) | null = null;
    listeners = new Map<string, (ev: MessageEvent) => void>();
    closed = false;
    url: string;
    constructor(url: string) {
      this.url = url;
    }
    addEventListener(type: string, listener: (ev: MessageEvent) => void) {
      this.listeners.set(type, listener);
    }
    close() {
      this.closed = true;
    }
  }

  it("is the only call that puts the token in the query string", () => {
    expect(sseUrl("run_1", "tok")).toBe("/api/v0/events?run_id=run_1&token=tok");
    expect(sseUrl("run_1", null)).toBe("/api/v0/events?run_id=run_1");
    expect(sseUrl(null, null)).toBe("/api/v0/events");
  });

  it("opens an EventSource with ?token=, delivers parsed events and unsubscribes", () => {
    window.sessionStorage.setItem(TOKEN_KEY, "tok");
    let es!: FakeEventSource;
    const events: unknown[] = [];
    const stop = sse("run_9", (ev) => events.push(ev), {
      events: ["state"],
      create: (url) => (es = new FakeEventSource(url)),
    });
    expect(es.url).toBe("/api/v0/events?run_id=run_9&token=tok");
    es.onmessage?.(new MessageEvent("message", { data: '{"n":1}' }));
    es.listeners.get("state")?.(new MessageEvent("state", { data: '{"state":"QUOTED"}', lastEventId: "7" }));
    es.onmessage?.(new MessageEvent("message", { data: "plain text" }));
    expect(events).toEqual([
      { event: "message", id: undefined, data: { n: 1 } },
      { event: "state", id: "7", data: { state: "QUOTED" } },
      { event: "message", id: undefined, data: "plain text" },
    ]);
    stop();
    expect(es.closed).toBe(true);
  });

  it("forwards connection errors", () => {
    let es!: FakeEventSource;
    const onError = vi.fn();
    sse("r", () => {}, { token: null, onError, create: (url) => (es = new FakeEventSource(url)) });
    expect(es.url).toBe("/api/v0/events?run_id=r");
    es.onerror?.(new Event("error"));
    expect(onError).toHaveBeenCalledOnce();
  });
});
