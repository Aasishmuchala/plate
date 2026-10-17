// History: the project's runs, newest first. A click selects the run as the candidate and its producing node on the
// canvas. State, engine, price, gate verdict and time come straight from the daemon's Run records.

import type { ReactElement } from "react";
import { PathResolver } from "./DropRestore";
import { formatTime, formatUsd, shortId } from "./format";
import { focusRun } from "./restore";
import { usePanelsStore, VERDICT_LABEL } from "./store";
import { C, S, STATUS_COLOR, tint } from "./theme";
import type { RunRow } from "./types";

const BAD = new Set(["GATE_FAIL", "FAILED", "REFUSED"]);
const INERT = new Set(["CANCELLED", "EXPIRED", "DRAFT"]);

function stateColor(state: string): string {
  if (state === "DONE") return C.pass;
  if (BAD.has(state)) return C.fail;
  if (INERT.has(state)) return C.muted;
  return C.accent;
}

/** Newest first by `updated_utc` (then `created_utc`); ISO strings sort as text. */
export function sortRuns(runs: readonly RunRow[]): RunRow[] {
  const key = (r: RunRow) => r.updated_utc ?? r.created_utc ?? "";
  return [...runs].sort((a, b) => key(b).localeCompare(key(a)));
}

export function HistoryPanel(): ReactElement {
  const runs = usePanelsStore((s) => s.runs);
  const loading = usePanelsStore((s) => s.runsLoading);
  const error = usePanelsStore((s) => s.runsError);
  const selected = usePanelsStore((s) => s.selectedRunId);
  const verdicts = usePanelsStore((s) => s.verdicts);
  const projectId = usePanelsStore((s) => s.runsProjectId);
  const loadRuns = usePanelsStore((s) => s.loadRuns);
  const dropKind = usePanelsStore((s) => s.drop.kind);

  return (
    <section style={S.section} aria-label="History" data-testid="history-panel">
      <div style={{ ...S.row, justifyContent: "space-between" }}>
        <h3 style={S.h}>History</h3>
        <button type="button" style={S.btn} disabled={!projectId || loading} onClick={() => void loadRuns(projectId)}>
          Refresh
        </button>
      </div>
      {!projectId ? <p style={S.muted}>Open a project to see its runs.</p> : null}
      {loading ? (
        <p style={S.muted} role="status">
          Loading runs...
        </p>
      ) : null}
      {error ? (
        <p style={S.error} role="alert">
          {error}
        </p>
      ) : null}
      {projectId && !loading && !error && runs.length === 0 ? <p style={S.muted}>No runs yet.</p> : null}
      <ul style={S.list}>
        {sortRuns(runs).map((run) => {
          const verdict = run.gate_report?.verdict;
          const given = verdicts[run.id];
          const price = run.cost_usd_billed ?? run.quote?.price_usd;
          return (
            <li key={run.id}>
              <button
                type="button"
                style={{ ...S.item, width: "100%", ...(selected === run.id ? { borderColor: C.accent } : {}) }}
                aria-pressed={selected === run.id}
                data-testid="history-run"
                data-run-id={run.id}
                onClick={() => focusRun(run)}
              >
                <span style={S.row}>
                  <span style={tint(stateColor(run.state))} data-testid="run-state">
                    {run.state}
                  </span>
                  <span style={S.mono}>{run.route?.engine_id ?? "no engine"}</span>
                </span>
                <span style={{ ...S.row, ...S.muted }}>
                  <span title={run.id}>{shortId(run.id)}</span>
                  <span data-testid="run-price">{formatUsd(price)}</span>
                  {verdict ? (
                    <span style={tint(STATUS_COLOR[verdict])} data-testid="run-gate">
                      gate {verdict}
                    </span>
                  ) : null}
                  {given ? <span style={tint(C.accent)}>{VERDICT_LABEL[given]}</span> : null}
                </span>
                <span style={S.muted}>{formatTime(run.updated_utc ?? run.created_utc)}</span>
              </button>
            </li>
          );
        })}
      </ul>
      <p style={{ ...S.muted, margin: 0 }}>Drop an image anywhere to find the run that made it, or</p>
      <PathResolver autoFocus={dropKind === "needs-path"} />
    </section>
  );
}
