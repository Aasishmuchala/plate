// Custody panel: what the project allows to leave the machine, the grade of each engine route the project's runs
// used, and the tail of the outbound log (host, bytes, opaque ids). The log is the evidence behind the custody
// claims, so the panel shows what was recorded and nothing else.

import type { ReactElement } from "react";
import { useGraphStore } from "../store";
import { getOutbound, listProjects } from "./api";
import { formatBytes, formatTime, shortId } from "./format";
import { usePanelsStore } from "./store";
import { C, GRADE_COLOR, S, tint } from "./theme";
import { CUSTODY_GRADES, type CustodyGrade, type OutboundEntry, type RunRow } from "./types";
import { useLoad } from "./useLoad";

export const OUTBOUND_TAIL = 50;

export interface EngineGrade {
  engine: string;
  grades: CustodyGrade[];
  runs: number;
}

/** engine id -> the custody grades its runs were quoted at, in grade order. Runs without a quote add no grade. */
export function engineGrades(runs: readonly RunRow[]): EngineGrade[] {
  const by = new Map<string, { grades: Set<CustodyGrade>; runs: number }>();
  for (const r of runs) {
    const engine = r.route?.engine_id;
    if (!engine) continue;
    const e = by.get(engine) ?? { grades: new Set<CustodyGrade>(), runs: 0 };
    e.runs += 1;
    if (r.quote?.custody_grade) e.grades.add(r.quote.custody_grade);
    by.set(engine, e);
  }
  return [...by.entries()]
    .map(([engine, e]) => ({ engine, grades: CUSTODY_GRADES.filter((g) => e.grades.has(g)), runs: e.runs }))
    .sort((a, b) => a.engine.localeCompare(b.engine));
}

/** Opaque ids as short strings; the log may hold a list or a map of them. */
export function opaqueIds(raw: unknown): string[] {
  if (Array.isArray(raw)) return raw.map(String);
  if (typeof raw === "object" && raw !== null) return Object.values(raw as Record<string, unknown>).map(String);
  return [];
}

function OutboundRow({ e }: { e: OutboundEntry }): ReactElement {
  const ids = opaqueIds(e.opaque_ids);
  const outcome = e.error ? `error: ${e.error}` : e.status !== undefined ? String(e.status) : "in flight";
  return (
    <li style={{ ...S.item, cursor: "default" }} data-testid="outbound-row">
      <span style={S.row}>
        <strong style={S.mono}>{e.host ?? "unknown host"}</strong>
        {e.custody_grade ? <span style={tint(GRADE_COLOR[e.custody_grade])}>{e.custody_grade}</span> : null}
      </span>
      <span style={{ ...S.row, ...S.muted }}>
        <span>{formatTime(e.at)}</span>
        <span>out {formatBytes(e.bytes)}</span>
        <span>in {formatBytes(e.bytes_in)}</span>
        <span style={e.error ? { color: C.fail } : undefined}>{outcome}</span>
      </span>
      <span style={{ ...S.row, ...S.muted }} data-testid="outbound-ids">
        {ids.length === 0 ? "no opaque ids" : `opaque ids: ${ids.slice(0, 3).map(shortId).join(", ")}${ids.length > 3 ? ` +${ids.length - 3}` : ""}`}
      </span>
    </li>
  );
}

export function CustodyPanel(): ReactElement {
  const projectId = useGraphStore((s) => s.projectId);
  const runs = usePanelsStore((s) => s.runs);
  const project = useLoad(projectId ? `project:${projectId}` : null, async () =>
    (await listProjects()).find((p) => p.project_id === projectId) ?? null,
  );
  const outbound = useLoad(projectId ? `outbound:${projectId}` : null, () => getOutbound(projectId as string, OUTBOUND_TAIL));

  if (!projectId) {
    return (
      <section style={S.section} aria-label="Custody" data-testid="custody-panel">
        <h3 style={S.h}>Custody</h3>
        <p style={S.muted}>Open a project to see its custody.</p>
      </section>
    );
  }

  const custody = project.data?.custody;
  const allowed = custody?.allowed_grades ?? [];
  const engines = engineGrades(runs);
  const entries = outbound.data ?? [];

  return (
    <section style={S.section} aria-label="Custody" data-testid="custody-panel">
      <h3 style={S.h}>Custody</h3>
      {project.error ? (
        <p style={S.error} role="alert">
          {project.error}
        </p>
      ) : null}
      {custody ? (
        <div style={S.row} data-testid="project-grade">
          {custody.nda ? <span style={tint(C.advisory)}>NDA</span> : <span style={S.chip}>not NDA</span>}
          <span style={S.muted}>allowed:</span>
          {allowed.map((g) => (
            <span key={g} style={tint(GRADE_COLOR[g])}>
              {g}
            </span>
          ))}
        </div>
      ) : null}
      <h3 style={S.h}>Engine routes</h3>
      {engines.length === 0 ? (
        <p style={S.muted}>No runs yet, so no route has been used.</p>
      ) : (
        <ul style={S.list} data-testid="engine-grades">
          {engines.map((e) => (
            <li key={e.engine} style={S.row} data-testid="engine-grade" data-engine={e.engine}>
              <span style={S.mono}>{e.engine}</span>
              {e.grades.length === 0 ? <span style={S.muted}>no quote</span> : null}
              {e.grades.map((g) => (
                <span key={g} style={tint(GRADE_COLOR[g])}>
                  {g}
                </span>
              ))}
              {custody && e.grades.some((g) => !allowed.includes(g)) ? (
                <span style={{ color: C.fail }}>outside the project's allowed grades</span>
              ) : null}
              <span style={S.muted}>{e.runs} run(s)</span>
            </li>
          ))}
        </ul>
      )}
      <div style={{ ...S.row, justifyContent: "space-between" }}>
        <h3 style={S.h}>Outbound log (last {OUTBOUND_TAIL})</h3>
        <button type="button" style={S.btn} onClick={outbound.reload} disabled={outbound.loading}>
          Refresh
        </button>
      </div>
      {outbound.loading ? (
        <p style={S.muted} role="status">
          Loading outbound log...
        </p>
      ) : null}
      {outbound.error ? (
        <p style={S.error} role="alert">
          {outbound.error}
        </p>
      ) : null}
      {!outbound.loading && !outbound.error && entries.length === 0 ? <p style={S.muted}>No outbound requests logged.</p> : null}
      <ul style={S.list}>
        {entries.map((e) => (
          <OutboundRow key={e.id} e={e} />
        ))}
      </ul>
    </section>
  );
}
