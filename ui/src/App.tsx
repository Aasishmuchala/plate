// Root component. The ReactFlowProvider lives in main.tsx (and in the tests) so App stays provider-agnostic.
// It mounts the three slots later tasks fill: the node editor for the selected node (T47), the viewer (T24, wired
// to the candidate strip by T35: a clicked candidate is compared with its plate) and the panels (T36).
// T71: it also follows the open project's event stream (so `alert`, `packet.ingested`, `plugin.job` arrive between
// Makes) and shows the last bus alert in the status bar, amber for a warning and red for an error, until dismissed.
// T101: `enlarged` (the viewer's Enlarge button, or `e` outside the viewer) hides the graph with `display: none`, so
// xyflow keeps its state, and puts the viewer in the canvas area's place with the selected node's candidate strip
// docked below; Esc returns, and the sidebar keeps the editors. `f` outside the viewer goes fullscreen. The
// Recomposite bar (T99) sits under the viewer when the compared take has a stored raw layer, and a status-bar chip
// says when the Max dock last polled for jobs (`GET /plugin/status`, every 30 s).
// The canvas theme is imported here (vitest runs with css: false, so tests never load it).

import "./canvas/canvas.css";
import "./canvas/enlarge.css";
import {
  Component,
  type ComponentType,
  type CSSProperties,
  type ReactNode,
  useEffect,
  useLayoutEffect,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { fetchJson } from "./api/client";
import { useLastAlert } from "./api/useDaemonStatus";
import { CandidateStrip } from "./canvas/CandidateStrip";
import { CompareViewer, useBindCompareToPanels } from "./canvas/CompareViewer";
import { hub } from "./canvas/runEvents";
import { useRunStore } from "./canvas/runStore";
import PlateCanvas from "./PlateCanvas";
import { editorFor } from "./editors";
import { Panels } from "./panels";
import { usePanelsStore } from "./panels/store";
import { isTextEntry } from "./panels/VerdictPanel";
import { useGraphStore } from "./store";
import { toggleFullscreen } from "./viewer/Viewer";

const ALERT_COLOR: Record<string, string> = {
  error: "var(--red, #ff453a)",
  warning: "var(--amber, #ff9f0a)",
  info: "var(--dim, #98989d)",
};

const ALERT_STYLE: CSSProperties = { marginLeft: "auto", display: "inline-flex", alignItems: "center", gap: 8 };

/** The status bar PlateCanvas owns, found in the document so a chip can be portalled into it (null: none mounted). */
function useStatusBar(): Element | null {
  const [bar, setBar] = useState<Element | null>(null);
  useLayoutEffect(() => {
    setBar(document.querySelector('[data-testid="status-bar"]'));
  }, []);
  return bar;
}

/**
 * The last alert, drawn inside the status bar. PlateCanvas owns the bar, so it is found in the document and the
 * alert is portalled into it; without a bar (a test that mounts only this) it renders where it is mounted.
 */
function StatusAlert() {
  const { lastAlert, dismissAlert } = useLastAlert();
  const bar = useStatusBar();
  if (!lastAlert) return null;
  const node = (
    <span
      className={`status-alert alert-${lastAlert.level}`}
      data-testid="status-alert"
      data-level={lastAlert.level}
      role={lastAlert.level === "error" ? "alert" : "status"}
      title={lastAlert.at}
      style={{ ...ALERT_STYLE, color: ALERT_COLOR[lastAlert.level] }}
    >
      <span data-testid="status-alert-text">{lastAlert.message}</span>
      <button
        type="button"
        className="btn btn-ghost"
        onClick={dismissAlert}
        data-testid="status-alert-dismiss"
        aria-label="Dismiss alert"
      >
        Dismiss
      </button>
    </span>
  );
  return bar ? createPortal(node, bar) : node;
}

// ------------------------------------------------------------------------------------------------ Max dock chip

/** How often the chip asks the daemon about the dock, and how old a poll may be while jobs wait before it is amber. */
export const HOST_POLL_MS = 30_000;
export const HOST_STALE_MS = 30_000;

/** `GET /plugin/status` (T96): when a dock last polled with `claim=1` (null: never) and the job counts. */
export interface PluginStatus {
  last_poll_utc: string | null;
  queued: number;
  claimed: number;
}

export type HostChipState = "green" | "amber" | "grey";

const count = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);

export function parsePluginStatus(body: unknown): PluginStatus {
  const b = typeof body === "object" && body !== null ? (body as Record<string, unknown>) : {};
  const last = typeof b.last_poll_utc === "string" && b.last_poll_utc !== "" ? b.last_poll_utc : null;
  return { last_poll_utc: last, queued: count(b.queued), claimed: count(b.claimed) };
}

/** `12 s ago`, `3 min ago`, `2 h ago`. */
export function formatAgo(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 5) return "just now";
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  return `${Math.floor(s / 3600)} h ago`;
}

/** The chip for a status read at `now`: green once a dock polled, amber while jobs wait on a stale poll, grey never. */
export function hostChip(status: PluginStatus | null, now: number): { state: HostChipState; text: string } {
  if (!status) return { state: "grey", text: "Max dock · status unavailable" };
  const queued = status.queued > 0 ? ` · ${status.queued} queued` : "";
  const at = status.last_poll_utc ? Date.parse(status.last_poll_utc) : Number.NaN;
  if (!Number.isFinite(at)) return { state: "grey", text: `Max dock · never polled${queued}` };
  const age = now - at;
  const state: HostChipState = status.queued > 0 && age > HOST_STALE_MS ? "amber" : "green";
  return { state, text: `Max dock · polled ${formatAgo(age)}${queued}` };
}

const HOST_COLOR: Record<HostChipState, string> = {
  green: "var(--green, #30d158)",
  amber: "var(--amber, #ff9f0a)",
  grey: "var(--dim, #98989d)",
};

/** The Max dock chip, portalled into the status bar like the alert. */
function HostChip() {
  const bar = useStatusBar();
  const [read, setRead] = useState<{ status: PluginStatus | null; at: number } | null>(null);
  useEffect(() => {
    let alive = true;
    const poll = () => {
      fetchJson<unknown>("/plugin/status").then(
        (body) => {
          if (alive) setRead({ status: parsePluginStatus(body), at: Date.now() });
        },
        () => {
          if (alive) setRead({ status: null, at: Date.now() });
        },
      );
    };
    poll();
    const timer = window.setInterval(poll, HOST_POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);
  if (!read) return null;
  const chip = hostChip(read.status, read.at);
  const node = (
    <span
      className={`host-chip host-${chip.state}`}
      data-testid="host-chip"
      data-state={chip.state}
      title={read.status?.last_poll_utc ? `last poll ${read.status.last_poll_utc}` : "no Max dock has polled yet"}
      style={{ color: HOST_COLOR[chip.state] }}
    >
      {chip.text}
    </span>
  );
  return bar ? createPortal(node, bar) : node;
}

// ------------------------------------------------------------------------------------------------ Recomposite slot

/** What the slot hands T99's bar: the compared take and the raw layer it keeps. */
export interface RecompositeBarProps {
  runId: string;
  nodeId: string | null;
  rawKey: string;
}

type RecompositeModule = { RecompositeBar?: ComponentType<RecompositeBarProps> };

// A glob, not an import: until T99's file lands the glob is empty and the slot renders nothing (and still builds).
const RECOMPOSITE_MODULES = import.meta.glob<RecompositeModule>("./editors/RecompositeBar.tsx");

/** Where the slot gets the bar; tests swap `load`. */
export const recompositeSlot = {
  load: async (): Promise<ComponentType<RecompositeBarProps> | null> => {
    const loader = Object.values(RECOMPOSITE_MODULES)[0];
    if (!loader) return null;
    try {
      return (await loader()).RecompositeBar ?? null;
    } catch {
      return null;
    }
  },
};

/** A bar that throws renders nothing; the viewer above it stays. */
class SlotBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }
  render(): ReactNode {
    return this.state.failed ? null : this.props.children;
  }
}

/** The compared take (side B when it is a run, else the panels' selected run) and its stored raw layer. */
function useRecompositeTarget(): RecompositeBarProps | null {
  const sideB = useRunStore((s) => (s.compare?.b?.kind === "run" ? s.compare.b : null));
  const selectedRunId = usePanelsStore((s) => s.selectedRunId);
  const runId = sideB?.runId ?? selectedRunId;
  const cand = useRunStore((s) => (runId ? s.candidates[runId] : undefined));
  const row = usePanelsStore((s) => (runId ? s.runs.find((r) => r.id === runId) : undefined));
  if (!runId) return null;
  const fromRow = row as (typeof row & { raw_key?: unknown; normalized?: { raw_key?: unknown } | null }) | undefined;
  const rowKey = fromRow?.normalized?.raw_key ?? fromRow?.raw_key;
  const rawKey = cand?.rawKey ?? (typeof rowKey === "string" ? rowKey : null);
  if (!rawKey) return null;
  return { runId, nodeId: cand?.nodeId ?? sideB?.nodeId ?? row?.node_id ?? null, rawKey };
}

function RecompositeSlot() {
  const target = useRecompositeTarget();
  const [Bar, setBar] = useState<ComponentType<RecompositeBarProps> | null>(null);
  useEffect(() => {
    let alive = true;
    void recompositeSlot.load().then((c) => {
      if (alive) setBar(() => c);
    });
    return () => {
      alive = false;
    };
  }, []);
  if (!target || !Bar) return null;
  return (
    <div className="recomposite-slot" data-testid="recomposite-slot" data-run={target.runId}>
      <SlotBoundary key={target.runId}>
        <Bar {...target} />
      </SlotBoundary>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ enlarge

/** Where `e` and `f` belong to someone else: the viewer (edges, fit) and anything marked `data-plate-keys`. */
const KEY_SCOPE = '[data-testid="viewer"], [data-plate-keys]';

/** `e` enlarge, `f` fullscreen (outside the viewer and text boxes), Esc back to the canvas. */
function useEnlargeKeys(): void {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
      const target = e.target instanceof Element ? e.target : null;
      if (isTextEntry(target)) return;
      const runs = useRunStore.getState();
      if (e.key === "Escape") {
        if (runs.enlarged) runs.setEnlarged(false);
        return;
      }
      if (target?.closest(KEY_SCOPE)) return;
      if (e.key === "e" || e.key === "E") {
        e.preventDefault();
        runs.setEnlarged(!runs.enlarged);
      } else if (e.key === "f" || e.key === "F") {
        e.preventDefault();
        runs.setEnlarged(true);
        toggleFullscreen();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);
}

/**
 * Where the enlarged stage goes: the row that holds PlateCanvas's `.plate-canvas` (hidden while enlarged), so the
 * stage takes the canvas area's place beside the sidebar (enlarge.css orders it first). Not inside `.plate-canvas`:
 * the canvas's own keys (T98: z, Cmd/Ctrl+D) must not fire from the viewer. Found on mount and on every toggle.
 */
function useCanvasArea(enlarged: boolean): Element | null {
  const [area, setArea] = useState<Element | null>(null);
  useLayoutEffect(() => {
    const el = document.querySelector(".plate-canvas")?.parentElement ?? null;
    setArea((cur) => (cur === el ? cur : el));
  }, [enlarged]);
  return enlarged && area?.isConnected ? area : null;
}

export default function App() {
  const selected = useGraphStore((s) => s.nodes.find((n) => n.selected));
  const projectId = useGraphStore((s) => s.projectId);
  const enlarged = useRunStore((s) => s.enlarged);
  // The strip docked under the enlarged viewer: the selected node's, unless it made nothing and a pair is open.
  const stripNode = useRunStore((s) => {
    const own = selected && (s.byNode[selected.id]?.runIds.length ?? 0) > 0 ? selected.id : null;
    return own ?? s.compare?.b?.nodeId ?? s.compare?.a.nodeId ?? selected?.id ?? null;
  });
  const Editor = selected ? editorFor(selected.data.kind) : null;
  const area = useCanvasArea(enlarged);

  // Hold the open project's event stream so alerts and packet/plugin records arrive even when no run is live.
  useEffect(() => (projectId ? hub.follow(projectId) : undefined), [projectId]);
  useBindCompareToPanels();
  useEnlargeKeys();
  // Leaving the enlarged view also leaves the page's fullscreen.
  useEffect(() => {
    if (!enlarged && typeof document !== "undefined" && document.fullscreenElement) toggleFullscreen();
  }, [enlarged]);
  // The moved viewer takes the focus, so its own keys (zoom, pan, wipe) work at once; Esc still returns.
  useLayoutEffect(() => {
    if (area) area.querySelector<HTMLElement>('[data-testid="enlarged-stage"] [data-testid="viewer"]')?.focus();
  }, [area]);

  const stage = (
    <div className="enlarged-stage" data-testid="enlarged-stage">
      <CompareViewer fill />
      <RecompositeSlot />
      {stripNode ? (
        <div className="enlarged-strip" data-testid="enlarged-strip">
          <CandidateStrip nodeId={stripNode} />
        </div>
      ) : null}
    </div>
  );

  return (
    <div className={enlarged ? "plate-shell is-enlarged" : "plate-shell"} data-testid="plate-shell">
      <PlateCanvas>
        {Editor && selected ? <Editor nodeId={selected.id} /> : null}
        {!enlarged ? (
          <>
            <CompareViewer />
            <RecompositeSlot />
          </>
        ) : area ? null : (
          stage
        )}
        <Panels />
        <StatusAlert />
        <HostChip />
      </PlateCanvas>
      {area ? createPortal(stage, area) : null}
    </div>
  );
}
