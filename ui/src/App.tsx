// Root component. The ReactFlowProvider lives in main.tsx (and in the tests) so App stays provider-agnostic.
// It mounts the three slots later tasks fill: the node editor for the selected node (T47), the viewer (T24, wired
// to the candidate strip by T35: a clicked candidate is compared with its plate) and the panels (T36).
// T71: it also follows the open project's event stream (so `alert`, `packet.ingested`, `plugin.job` arrive between
// Makes) and shows the last bus alert in the status bar, amber for a warning and red for an error, until dismissed.
// The canvas theme is imported here (vitest runs with css: false, so tests never load it).

import "./canvas/canvas.css";
import { type CSSProperties, useEffect, useLayoutEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useLastAlert } from "./api/useDaemonStatus";
import { CompareViewer } from "./canvas/CompareViewer";
import { hub } from "./canvas/runEvents";
import PlateCanvas from "./PlateCanvas";
import { editorFor } from "./editors";
import { Panels } from "./panels";
import { useGraphStore } from "./store";

const ALERT_COLOR: Record<string, string> = {
  error: "var(--red, #ff453a)",
  warning: "var(--amber, #ff9f0a)",
  info: "var(--dim, #98989d)",
};

const ALERT_STYLE: CSSProperties = { marginLeft: "auto", display: "inline-flex", alignItems: "center", gap: 8 };

/**
 * The last alert, drawn inside the status bar. PlateCanvas owns the bar, so it is found in the document and the
 * alert is portalled into it; without a bar (a test that mounts only this) it renders where it is mounted.
 */
function StatusAlert() {
  const { lastAlert, dismissAlert } = useLastAlert();
  const [bar, setBar] = useState<Element | null>(null);
  useLayoutEffect(() => {
    setBar(document.querySelector('[data-testid="status-bar"]'));
  }, []);
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

export default function App() {
  const selected = useGraphStore((s) => s.nodes.find((n) => n.selected));
  const projectId = useGraphStore((s) => s.projectId);
  const Editor = selected ? editorFor(selected.data.kind) : null;

  // Hold the open project's event stream so alerts and packet/plugin records arrive even when no run is live.
  useEffect(() => (projectId ? hub.follow(projectId) : undefined), [projectId]);

  return (
    <PlateCanvas>
      {Editor && selected ? <Editor nodeId={selected.id} /> : null}
      <CompareViewer />
      <Panels />
      <StatusAlert />
    </PlateCanvas>
  );
}
