// Live run status (T35): one event subscription per run, multiplexed. Each watched run registers a handler; with a
// project open the hub holds ONE EventSource on `/api/v0/events?project_id=…&token=…` and routes records by
// `run_id`, because a browser allows only six HTTP/1.1 connections per host and a best-of-6 with one stream per
// run would starve every other request. Without a project id it falls back to the per-run stream from
// `api/client.sse`. `run.state` moves a candidate's chip; on a terminal state the run is read once
// (`GET /runs/{id}`, plus `/gate` for a GATE_FAIL without checks) and unwatched; `siblings.ranked` orders the strip.

import { API_PREFIX, type EventSourceLike, getToken, type RunEvent, sse } from "../api/client";
import { getGateReport, getRun } from "./runApi";
import { useRunStore } from "./runStore";
import { type BusRecord, isRunState, isTerminal, type RunWire } from "./runTypes";

export type RecordHandler = (rec: BusRecord) => void;
export type EventSourceFactory = (url: string) => EventSourceLike;

const browserFactory: EventSourceFactory = (url) => new EventSource(url) as unknown as EventSourceLike;

/** `/api/v0/events?project_id=…&token=…` (the only route that takes the token in the query). */
export function projectEventsUrl(projectId: string, token: string | null): string {
  const q = new URLSearchParams({ project_id: projectId });
  if (token) q.set("token", token);
  return `${API_PREFIX}/events?${q.toString()}`;
}

/** The bus record inside an SSE message, or null for keep-alives and anything that is not a record. */
export function parseRecord(data: unknown): BusRecord | null {
  let v = data;
  if (typeof v === "string") {
    try {
      v = JSON.parse(v);
    } catch {
      return null;
    }
  }
  if (!v || typeof v !== "object") return null;
  const rec = v as Record<string, unknown>;
  if (typeof rec.kind !== "string") return null;
  return rec as unknown as BusRecord;
}

interface ProjectStream {
  source: EventSourceLike;
  handlers: Map<string, Set<RecordHandler>>;
}

export class RunEventHub {
  private streams = new Map<string, ProjectStream>();
  private perRun = new Map<string, { close: () => void; handlers: Set<RecordHandler> }>();
  private factory: EventSourceFactory;

  constructor(factory: EventSourceFactory = browserFactory) {
    this.factory = factory;
  }

  setFactory(factory: EventSourceFactory): void {
    this.factory = factory;
  }

  /** Watch one run. Returns the unwatch function; the stream closes when its last run is unwatched. */
  watch(runId: string, projectId: string | null, handler: RecordHandler): () => void {
    return projectId ? this.watchProject(runId, projectId, handler) : this.watchRun(runId, handler);
  }

  /** Runs currently watched (both transports). */
  watching(): string[] {
    const ids = new Set<string>(this.perRun.keys());
    for (const s of this.streams.values()) for (const id of s.handlers.keys()) ids.add(id);
    return [...ids];
  }

  /** Open EventSources: one per project with watched runs, plus one per run watched without a project. */
  connections(): number {
    return this.streams.size + this.perRun.size;
  }

  closeAll(): void {
    for (const s of this.streams.values()) s.source.close();
    for (const r of this.perRun.values()) r.close();
    this.streams.clear();
    this.perRun.clear();
  }

  private watchProject(runId: string, projectId: string, handler: RecordHandler): () => void {
    let stream = this.streams.get(projectId);
    if (!stream) {
      const source = this.factory(projectEventsUrl(projectId, getToken()));
      const handlers = new Map<string, Set<RecordHandler>>();
      source.onmessage = (ev: MessageEvent) => {
        const rec = parseRecord(ev.data);
        if (!rec) return;
        // Group records (siblings.ranked) carry no run id: every watcher of the project sees them.
        const targets = rec.run_id ? [handlers.get(rec.run_id)] : [...handlers.values()];
        for (const set of targets) for (const h of set ?? []) h(rec);
      };
      stream = { source, handlers };
      this.streams.set(projectId, stream);
    }
    const set = stream.handlers.get(runId) ?? new Set<RecordHandler>();
    set.add(handler);
    stream.handlers.set(runId, set);
    const s = stream;
    return () => {
      const hs = s.handlers.get(runId);
      hs?.delete(handler);
      if (hs && hs.size === 0) s.handlers.delete(runId);
      if (s.handlers.size === 0 && this.streams.get(projectId) === s) {
        s.source.close();
        this.streams.delete(projectId);
      }
    };
  }

  private watchRun(runId: string, handler: RecordHandler): () => void {
    let entry = this.perRun.get(runId);
    if (!entry) {
      const handlers = new Set<RecordHandler>();
      const close = sse(
        runId,
        (ev: RunEvent) => {
          const rec = parseRecord(ev.data);
          if (rec) for (const h of handlers) h(rec);
        },
        { create: this.factory },
      );
      entry = { close, handlers };
      this.perRun.set(runId, entry);
    }
    entry.handlers.add(handler);
    const e = entry;
    return () => {
      e.handlers.delete(handler);
      if (e.handlers.size === 0 && this.perRun.get(runId) === e) {
        e.close();
        this.perRun.delete(runId);
      }
    };
  }
}

/** The canvas's hub. Tests swap the EventSource factory with `hub.setFactory`. */
export const hub = new RunEventHub();

const unwatchers = new Map<string, () => void>();

/** Read a finished run once and merge it (outputs, gate report, first failing check). */
export async function settleRun(runId: string, nodeId: string): Promise<void> {
  let run: RunWire;
  try {
    run = await getRun(runId);
  } catch {
    return; // the chip already shows the terminal state; the strip still works without outputs
  }
  // A newer Make replaced this node's candidates while the read was in flight: drop it.
  if (!useRunStore.getState().candidates[runId]) return;
  if (run.state === "GATE_FAIL" && !run.gate_report?.checks?.length) {
    try {
      run = { ...run, gate_report: await getGateReport(runId) };
    } catch {
      // no report route yet: the chip says GATE_FAIL without the failing metric
    }
  }
  if (useRunStore.getState().candidates[runId]) useRunStore.getState().upsertRun(run, nodeId);
}

/** Apply one bus record to the run store. Exposed for tests. */
export function applyRecord(rec: BusRecord, runId: string, nodeId: string): void {
  const store = useRunStore.getState();
  const data = rec.data ?? {};
  if (rec.kind === "run.state") {
    const to = data["to"];
    const about = typeof data["run_id"] === "string" ? (data["run_id"] as string) : (rec.run_id ?? runId);
    if (about !== runId || !isRunState(to)) return;
    store.setRunState(runId, to);
    if (isTerminal(to)) {
      unwatchRun(runId);
      void settleRun(runId, nodeId);
    }
  } else if (rec.kind === "siblings.ranked") {
    const group = data["group"];
    const order = data["order"];
    if (typeof group === "string" && Array.isArray(order) && order.every((x) => typeof x === "string")) {
      store.setRanking(group, order as string[]);
    }
  }
}

/** Follow a run until it is terminal. Watching an already watched run is a no-op. */
export function watchRun(runId: string, nodeId: string, projectId: string | null): void {
  if (unwatchers.has(runId)) return;
  unwatchers.set(
    runId,
    hub.watch(runId, projectId, (rec) => applyRecord(rec, runId, nodeId)),
  );
}

export function unwatchRun(runId: string): void {
  const stop = unwatchers.get(runId);
  if (stop) {
    unwatchers.delete(runId);
    stop();
  }
}

export function unwatchAll(): void {
  for (const id of [...unwatchers.keys()]) unwatchRun(id);
  hub.closeAll();
}
