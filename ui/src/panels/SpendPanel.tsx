// Spend (T102): what the open project cost and what it bought, one row per engine and ISO week, from
// `GET /projects/{id}/spend?by=week` (T88: the ledger folded into quoted, billed, accepted and cost per accepted take).
// `All projects` asks `GET /spend?all_projects=1` for the studio's figures instead. Everything here is a read of the
// ledger; a price the vendor has not billed yet shows as quoted only.

import { type ReactElement, useState } from "react";
import { formatUsd } from "../makebar/quote";
import { useGraphStore } from "../store";
import { getAllSpend, getProjectSpend, type SpendReport, type SpendRow } from "./api";
import { S } from "./theme";
import { useLoad } from "./useLoad";

const cell = { padding: "2px 6px", textAlign: "right" as const, whiteSpace: "nowrap" as const };
const head = { ...cell, fontWeight: 500 };
const left = { ...cell, textAlign: "left" as const };

const money = (usd: number | null): string => (usd === null ? "n/a" : formatUsd(usd));

/** Newest week first, then by engine: the latest spend is what the artist is deciding about. */
export function orderSpend(rows: readonly SpendRow[]): SpendRow[] {
  return [...rows].sort((a, b) => (a.bucket === b.bucket ? a.engine_id.localeCompare(b.engine_id) : a.bucket < b.bucket ? 1 : -1));
}

function Totals({ report }: { report: SpendReport }): ReactElement {
  return (
    <span style={S.row} data-testid="spend-totals">
      <span style={S.chip}>quoted {money(report.spent_usd_quoted)}</span>
      <span style={S.chip}>billed {money(report.spent_usd_billed)}</span>
      <span style={S.chip}>{report.generations ?? 0} generations</span>
      <span style={S.chip}>{report.accepted ?? 0} accepted</span>
    </span>
  );
}

function SpendTable({ rows }: { rows: SpendRow[] }): ReactElement {
  if (rows.length === 0) return <p style={{ ...S.muted, margin: 0 }}>Nothing has been spent yet.</p>;
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ borderCollapse: "collapse", width: "100%", fontVariantNumeric: "tabular-nums" }} data-testid="spend-table">
        <thead>
          <tr style={S.muted}>
            <th style={{ ...head, textAlign: "left" }}>Week</th>
            <th style={{ ...head, textAlign: "left" }}>Engine</th>
            <th style={head}>Runs</th>
            <th style={head}>Quoted</th>
            <th style={head}>Billed</th>
            <th style={head}>Accepted</th>
            <th style={head} title="billed (quoted while nothing is billed) divided by the accepted takes">
              Per accepted
            </th>
          </tr>
        </thead>
        <tbody>
          {orderSpend(rows).map((r) => (
            <tr key={`${r.bucket}|${r.engine_id}`} data-testid="spend-row" data-engine={r.engine_id} data-bucket={r.bucket}>
              <td style={{ ...left, ...S.mono }}>{r.bucket}</td>
              <td style={{ ...left, ...S.mono }}>{r.engine_id}</td>
              <td style={cell}>{r.runs}</td>
              <td style={cell}>{formatUsd(r.quoted_usd)}</td>
              <td style={cell}>{r.billed_usd > 0 ? formatUsd(r.billed_usd) : <span style={S.muted}>not billed</span>}</td>
              <td style={cell}>{r.accepted}</td>
              <td style={cell}>{r.cost_per_accepted === null ? <span style={S.muted}>none accepted</span> : formatUsd(r.cost_per_accepted)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function SpendPanel(): ReactElement {
  const projectId = useGraphStore((s) => s.projectId);
  const [all, setAll] = useState(false);
  const key = all ? "spend:all" : projectId ? `spend:${projectId}` : null;
  const { data, loading, error, reload } = useLoad(key, () => (all || !projectId ? getAllSpend("week") : getProjectSpend(projectId, "week")));

  return (
    <section style={S.section} aria-label="Spend" data-testid="spend-panel">
      <div style={{ ...S.row, justifyContent: "space-between" }}>
        <h3 style={S.h}>{all ? "Spend: all projects" : `Spend${projectId ? `: ${projectId}` : ""}`}</h3>
        <span style={S.row}>
          <label style={S.row}>
            <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} aria-label="All projects" />
            All projects
          </label>
          <button type="button" style={S.btn} onClick={reload} disabled={loading || key === null}>
            Refresh
          </button>
        </span>
      </div>
      {key === null ? <p style={{ ...S.muted, margin: 0 }}>Open a project to see its spend, or tick All projects.</p> : null}
      {loading ? (
        <p style={S.muted} role="status">
          Loading spend...
        </p>
      ) : null}
      {error ? (
        <p style={S.error} role="alert">
          {error}
        </p>
      ) : null}
      {data && !loading ? (
        <>
          <Totals report={data} />
          {all && data.projects ? <span style={S.muted}>{data.projects.length} projects</span> : null}
          <SpendTable rows={data.series} />
        </>
      ) : null}
    </section>
  );
}
