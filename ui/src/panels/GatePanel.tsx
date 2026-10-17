// Gate panel: the latest GateReport of the selected run as a check list (metric, value, threshold, status), the
// backend notes (which flow backend, what ran as a stand-in), report version chips (a lock marks a shipped report,
// which a rescore never touches), "Open tiles" to inspect the flagged points at 100% and "Rescore".

import { type ReactElement, useState } from "react";
import { errorMessage, getGateReport, rescoreGate } from "./api";
import { formatThreshold, formatTime, formatValue } from "./format";
import { flaggedPoints } from "./gatePoints";
import { usePanelsStore } from "./store";
import { C, S, STATUS_COLOR, tint } from "./theme";
import type { GateCheck, GateStatus } from "./types";
import { useLoad } from "./useLoad";

const SEVERITY: Record<GateStatus, number> = { fail: 0, advisory: 1, pass: 2 };

/** Failures first, then advisories, then passes; the daemon's order inside each group. */
export function orderChecks(checks: readonly GateCheck[]): GateCheck[] {
  return checks
    .map((c, i) => ({ c, i }))
    .sort((a, b) => SEVERITY[a.c.status] - SEVERITY[b.c.status] || a.i - b.i)
    .map((x) => x.c);
}

function Lock(): ReactElement {
  return (
    <svg width="10" height="12" viewBox="0 0 10 12" role="img" aria-label="shipped" data-testid="shipped-lock">
      <title>shipped: this report is frozen</title>
      <rect x="1" y="5" width="8" height="6" rx="1" fill="currentColor" />
      <path d="M3 5V3.5a2 2 0 0 1 4 0V5" fill="none" stroke="currentColor" strokeWidth="1.2" />
    </svg>
  );
}

export function GatePanel(): ReactElement {
  const runId = usePanelsStore((s) => s.selectedRunId);
  const openTiles = usePanelsStore((s) => s.openTiles);
  const [pick, setPick] = useState<{ runId: string | null; n: number } | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const n = pick && pick.runId === runId ? pick.n : undefined;
  const { data, loading, error, reload } = useLoad(runId ? `${runId}#${n ?? "latest"}` : null, () =>
    getGateReport(runId as string, n),
  );

  const rescore = async () => {
    if (!runId) return;
    setBusy(true);
    setActionError(null);
    try {
      await rescoreGate(runId);
      setPick(null); // the new version is the latest
      reload();
    } catch (err) {
      setActionError(`Rescore failed: ${errorMessage(err)}`);
    } finally {
      setBusy(false);
    }
  };

  if (!runId) {
    return (
      <section style={S.section} aria-label="Gate" data-testid="gate-panel">
        <h3 style={S.h}>Gate</h3>
        <p style={S.muted}>Select a run in History to see its gate report.</p>
      </section>
    );
  }

  const report = data?.report;
  const points = report ? flaggedPoints(report) : [];

  return (
    <section style={S.section} aria-label="Gate" data-testid="gate-panel">
      <div style={{ ...S.row, justifyContent: "space-between" }}>
        <h3 style={S.h}>Gate</h3>
        {report ? (
          <span style={tint(STATUS_COLOR[report.verdict])} data-testid="gate-verdict">
            {report.verdict}
          </span>
        ) : null}
      </div>
      {loading ? (
        <p style={S.muted} role="status">
          Loading gate report...
        </p>
      ) : null}
      {error ? (
        <p style={S.error} role="alert">
          {error}
        </p>
      ) : null}
      {!loading && !error && !data ? <p style={S.muted}>No gate report for this run yet.</p> : null}
      {data && report ? (
        <>
          <div style={S.row} role="group" aria-label="Report versions">
            {data.versions.map((v) => (
              <button
                key={v.n}
                type="button"
                style={{ ...S.btn, ...(v.n === data.n ? { borderColor: C.accent, color: C.accent } : {}), display: "inline-flex", gap: 4, alignItems: "center" }}
                aria-pressed={v.n === data.n}
                data-testid="gate-version"
                data-shipped={v.shipped}
                onClick={() => setPick({ runId, n: v.n })}
              >
                v{v.n}
                {v.shipped ? <Lock /> : null}
              </button>
            ))}
          </div>
          <table style={{ borderCollapse: "collapse", width: "100%" }} data-testid="gate-checks">
            <thead>
              <tr style={S.muted}>
                <th style={{ textAlign: "left", fontWeight: 500 }}>Metric</th>
                <th style={{ textAlign: "right", fontWeight: 500 }}>Value</th>
                <th style={{ textAlign: "right", fontWeight: 500 }}>Threshold</th>
                <th style={{ textAlign: "left", fontWeight: 500, paddingLeft: 6 }}>Status</th>
              </tr>
            </thead>
            <tbody>
              {orderChecks(report.checks).map((c) => (
                <tr key={c.metric_id} data-testid="gate-check" data-status={c.status} data-metric={c.metric_id}>
                  <td style={{ ...S.mono, wordBreak: "break-all" }}>{c.metric_id}</td>
                  <td style={{ textAlign: "right" }}>{formatValue(c.value)}</td>
                  <td style={{ textAlign: "right", ...S.muted }}>
                    {formatThreshold(c.threshold ?? report.thresholds?.[c.metric_id])}
                  </td>
                  <td style={{ paddingLeft: 6 }}>
                    <span style={tint(STATUS_COLOR[c.status])}>{c.status}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {report.checks.length === 0 ? <p style={S.muted}>The report holds no checks.</p> : null}
          {report.backend_notes && report.backend_notes.length > 0 ? (
            <ul style={{ ...S.list, ...S.muted }} aria-label="Backend notes" data-testid="backend-notes">
              {report.backend_notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          ) : null}
          <p style={{ ...S.muted, margin: 0 }}>
            {[
              report.threshold_set_id,
              report.gate_version ? `gates ${report.gate_version}` : null,
              report.engine ? report.engine.id : null,
              formatTime(report.created_utc),
            ]
              .filter(Boolean)
              .join(" | ")}
          </p>
          {data.shipped ? (
            <p style={{ ...S.muted, margin: 0 }}>Shipped: a rescore writes a new version and leaves this one as it is.</p>
          ) : null}
          <div style={S.row}>
            <button
              type="button"
              style={S.btn}
              disabled={points.length === 0}
              title={points.length === 0 ? "The gates flagged no points" : `Inspect ${points.length} flagged point(s) at 100%`}
              onClick={() => openTiles(points)}
            >
              Open tiles{points.length > 0 ? ` (${points.length})` : ""}
            </button>
            <button type="button" style={S.btn} disabled={busy} onClick={() => void rescore()}>
              Rescore
            </button>
          </div>
        </>
      ) : null}
      {actionError ? (
        <p style={S.error} role="alert">
          {actionError}
        </p>
      ) : null}
    </section>
  );
}
