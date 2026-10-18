// Live run status (T35): one event subscription per run, multiplexed. Each watched run registers a handler; with a
// project open the hub holds ONE EventSource on `/api/v0/events?project_id=…&token=…` and routes records by
// `run_id`, because a browser allows only six HTTP/1.1 connections per host and a best-of-6 with one stream per
// run would starve every other request. Without a project id it falls back to the per-run stream from
// `api/client.sse`. `run.state` moves a candidate's chip; on a terminal state the run is read once
// (`GET /runs/{id}`, plus `/gate` for a GATE_FAIL without checks) and unwatched; `siblings.ranked` orders the strip.
// T71: the hub remembers the highest `seq` it saw per project, so a stream reopened later (a second Make) asks for
// `&after=<seq>` and never replays the project's history; `hub.onKind(kind, handler)` routes the records that are
// not about one run's state (`alert`, `packet.ingested`, `plugin.job`, `run.accepted`, `graph.saved`,
// `packet.superseded`); `hub.follow(projectId)` keeps the open project's stream up so those arrive between Makes.
// T147: the cursor is the raw SSE id of the newest record, `<epoch>.<seq>` since T144, passed back as `after`
// untouched (never parsed into a number, so a reindex's new epoch is never mistaken for an old seq; a bare integer
// id from an older daemon is kept as its seq). `followGraphSaves(projectId)` follows the project and hands every
// `graph.saved` (with the revision it names) to the graph store, which reloads or flags a conflict.

import { API_PREFIX, type EventSourceLike, getToken, type RunEvent, sse } from "../api/client";
import { noteRemoteSave } from "../store";
import { getGateReport, getRun } from "./runApi";
import { useRunStore } from "./runStore";
import { type BusRecord, isRunState, isTerminal, type RunWire } from "./runTypes";

export type RecordHandler = (rec: BusRecord) => void;
export type EventSourceFactory = (url: string) => EventSourceLike;

/** A source that never delivers: a page with no EventSource (a test, an old webview) just gets no live updates. */
const inertSource = (): EventSourceLike => ({ onmessage: null, onerror: null, addEventListener: () => {}, close: () => {} });

const browserFactory: EventSourceFactory = (url) => {
  if (typeof EventSource === "undefined") return inertSource();
  try {
    return new EventSource(url) as unknown as EventSourceLike;
  } catch {
    return inertSource();
  }
};

/** A project stream's cursor: the last SSE id as sent (`<epoch>.<seq>`), or the seq of an older daemon's bare id. */
export type EventCursor = string | number;

/**
 * `/api/v0/events?project_id=…&token=…[&after=<cursor>]` (the only route that takes the token in the query). `after`
 * is the hub's cursor for a project it has seen before; the first open of a project has none.
 */
export function projectEventsUrl(projectId: string, token: string | null, after?: EventCursor | null): string {
  const q = new URLSearchParams({ project_id: projectId });
  if (token) q.set("token", token);
  if (typeof after === "string" && after !== "") q.set("after", after);
  else if (typeof after === "number" && Number.isFinite(after)) q.set("after", String(after));
  return `${API_PREFIX}/events?${q.toString()}`;
}

interface CursorAt {
  /** What goes back as `after`. */
  cursor: EventCursor;
  /** The id's epoch (null: a bare id), so a new epoch replaces the cursor whatever its seq. */
  epoch: string | null;
  seq: number | null;
}

/** Where a record moves a cursor: its raw SSE id, else its seq. Null when it carries neither. */
function cursorOf(id: string | null, rec: BusRecord): CursorAt | null {
  const seq = typeof rec.seq === "number" && Number.isFinite(rec.seq) ? rec.seq : null;
  const dot = id === null ? -1 : id.lastIndexOf(".");
  if (id !== null && dot > 0) return { cursor: id, epoch: id.slice(0, dot), seq };
  // a bare id (an older daemon) is the record's seq; with no seq the id string goes back as it came
  if (seq !== null) return { cursor: seq, epoch: null, seq };
  return id !== null ? { cursor: id, epoch: null, seq: null } : null;
}

/** A later record never lowers the cursor within one epoch; a record from another epoch (a rebuild) replaces it. */
function advances(cur: CursorAt | undefined, next: CursorAt): boolean {
  if (!cur || cur.epoch !== next.epoch) return true;
  return cur.seq === null || next.seq === null || next.seq > cur.seq;
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
  /** Callers of `hub.follow(project)`: they hold the stream open with no run watched. */
  follows: number;
}

export class RunEventHub {
  private streams = new Map<string, ProjectStream>();
  private perRun = new Map<string, { close: () => void; handlers: Set<RecordHandler> }>();
  private cursors = new Map<string, CursorAt>();
  private kindHandlers = new Map<string, Set<RecordHandler>>();
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

  /** Close every stream and forget the cursors (a logout, or a test reset). `onKind` handlers stay registered. */
  closeAll(): void {
    for (const s of this.streams.values()) s.source.close();
    for (const r of this.perRun.values()) r.close();
    this.streams.clear();
    this.perRun.clear();
    this.cursors.clear();
  }

  /** The newest record's SSE id on a project's stream (`<epoch>.<seq>`; an older daemon's bare id as its seq), or
   * null before its first record (the next open has no cursor). */
  cursor(projectId: string): EventCursor | null {
    return this.cursors.get(projectId)?.cursor ?? null;
  }

  /**
   * Handle records of one `kind` from any project stream: `alert`, `packet.ingested`, `plugin.job`, `run.accepted`,
   * `graph.saved`, `packet.superseded` (the run-state kinds already reach `watch` handlers by run id). Returns the
   * unsubscribe function. Registering does not open a stream: `follow` or `watch` does.
   */
  onKind(kind: string, handler: RecordHandler): () => void {
    const set = this.kindHandlers.get(kind) ?? new Set<RecordHandler>();
    set.add(handler);
    this.kindHandlers.set(kind, set);
    return () => {
      set.delete(handler);
      if (set.size === 0 && this.kindHandlers.get(kind) === set) this.kindHandlers.delete(kind);
    };
  }

  /** Keep the project's stream open with no run watched, so `onKind` records arrive between Makes. */
  follow(projectId: string): () => void {
    const stream = this.openProject(projectId);
    stream.follows += 1;
    let released = false;
    return () => {
      if (released) return;
      released = true;
      stream.follows = Math.max(0, stream.follows - 1);
      this.releaseProject(projectId, stream);
    };
  }

  private emitKind(rec: BusRecord): void {
    for (const h of [...(this.kindHandlers.get(rec.kind) ?? [])]) {
      try {
        h(rec);
      } catch (err) {
        console.error(`event handler for ${rec.kind} threw`, err);
      }
    }
  }

  private openProject(projectId: string): ProjectStream {
    const open = this.streams.get(projectId);
    if (open) return open;
    const source = this.factory(projectEventsUrl(projectId, getToken(), this.cursor(projectId)));
    const handlers = new Map<string, Set<RecordHandler>>();
    source.onmessage = (ev: MessageEvent) => {
      const rec = parseRecord(ev.data);
      if (!rec) return;
      const id = typeof ev.lastEventId === "string" && ev.lastEventId !== "" ? ev.lastEventId : null;
      const next = cursorOf(id, rec);
      if (next && advances(this.cursors.get(projectId), next)) this.cursors.set(projectId, next);
      // Group records (siblings.ranked) carry no run id: every watcher of the project sees them.
      const targets = rec.run_id ? [handlers.get(rec.run_id)] : [...handlers.values()];
      for (const set of targets) for (const h of set ?? []) h(rec);
      this.emitKind(rec);
    };
    const stream: ProjectStream = { source, handlers, follows: 0 };
    this.streams.set(projectId, stream);
    return stream;
  }

  /** Close a project's stream once no run is watched and nobody follows it. */
  private releaseProject(projectId: string, stream: ProjectStream): void {
    if (stream.handlers.size === 0 && stream.follows === 0 && this.streams.get(projectId) === stream) {
      stream.source.close();
      this.streams.delete(projectId);
    }
  }

  private watchProject(runId: string, projectId: string, handler: RecordHandler): () => void {
    const stream = this.openProject(projectId);
    const set = stream.handlers.get(runId) ?? new Set<RecordHandler>();
    set.add(handler);
    stream.handlers.set(runId, set);
    return () => {
      const hs = stream.handlers.get(runId);
      hs?.delete(handler);
      if (hs && hs.size === 0) stream.handlers.delete(runId);
      this.releaseProject(projectId, stream);
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
          if (!rec) return;
          for (const h of handlers) h(rec);
          this.emitKind(rec);
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

/** The revision a `graph.saved` record names (`data.revision`), or null when it names none. */
export function savedRevision(rec: BusRecord): number | null {
  const v = rec.data?.["revision"];
  return typeof v === "number" && Number.isInteger(v) && v >= 0 ? v : null;
}

/**
 * Follow the open project (as `hub.follow`) and give the graph store every `graph.saved` of it: another writer's
 * save is reloaded, or is a conflict over unsaved changes; this tab's own is ignored. Returns the stop function.
 */
export function followGraphSaves(projectId: string, on: RunEventHub = hub): () => void {
  const stopKind = on.onKind("graph.saved", (rec) => {
    if (typeof rec.project_id === "string" && rec.project_id !== projectId) return;
    noteRemoteSave(projectId, savedRevision(rec));
  });
  const stopFollow = on.follow(projectId);
  return () => {
    stopKind();
    stopFollow();
  };
}

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
