// Export node editor (T69): hand one take to the artist's folder. The candidates are the runs of the node that feeds
// this one (the nearest upstream node that makes something), DONE and overridden first; `Export` posts
// `POST /runs/{id}/export {out_dir, format, name?}` and the daemon writes `<name>.<ext>` plus `<name>.gate.json`
// (lineage embedded, the gate report marked shipped). Export does not need an accept (decision 39); a GATE_FAIL take
// needs an override reason of at least 10 characters, which the daemon ledgers as an `override` record.
// Node params: `format` (png16 | tiff16 | exr), `outDir`, `name`, `exported` (count), `lastPath`. Export makes nothing,
// so the Make bar has nothing to quote here (`MAKE_MODES.Export` is `none`).

import { type ReactElement, useCallback, useEffect, useMemo, useState } from "react";
import type { EdgeLike, GraphNode } from "../canvasRules";
import { MAKE_MODES } from "../makebar/engines";
import { shortId } from "../panels/format";
import { useGraphStore } from "../store";
import { errorMessage } from "./api";
import { MIN_OVERRIDE_CHARS } from "./movePolicy";
import { EXPORT_FORMATS, type ExportFormat, type ExportResult, exportCandidates, exportRun, listRuns, type RunItem } from "./nodeApi";
import { asNumber, asString, useNodeEditor } from "./params";
import { E } from "./theme";
import { Button, Chip, EditorFrame, Field, Notice, Section, useAction } from "./widgets";

/**
 * The nodes whose runs reach `nodeId`: on every upstream branch, the nearest node that makes something (an engine or
 * in-process node); Comp, Gate and Mask pass through. Nearest first, each once. Pure.
 */
export function producingNodes(nodes: readonly GraphNode[], edges: readonly EdgeLike[], nodeId: string): string[] {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const out: string[] = [];
  const seen = new Set<string>([nodeId]);
  let queue = [nodeId];
  while (queue.length) {
    const next: string[] = [];
    for (const id of queue) {
      for (const e of edges) {
        if (e.target !== id || seen.has(e.source)) continue;
        seen.add(e.source);
        const n = byId.get(e.source);
        if (!n) continue;
        if (MAKE_MODES[n.data.kind].mode !== "none") out.push(n.id);
        else next.push(n.id);
      }
    }
    queue = next;
  }
  return out;
}

export interface UpstreamRuns {
  /** The producing node ids (see `producingNodes`). */
  sources: string[];
  runs: RunItem[] | null;
  pending: boolean;
  error: string | null;
  reload: () => void;
}

/** The runs of the nodes feeding `nodeId`, read from `GET /projects/{pid}/runs?node_id=`, newest first per node. */
export function useUpstreamRuns(nodeId: string): UpstreamRuns {
  const projectId = useGraphStore((s) => s.projectId);
  const nodes = useGraphStore((s) => s.nodes);
  const edges = useGraphStore((s) => s.edges);
  const sources = useMemo(() => producingNodes(nodes, edges, nodeId), [nodes, edges, nodeId]);
  const key = sources.join("\n");
  const [runs, setRuns] = useState<RunItem[] | null>(null);
  const [load, state] = useAction(async (pid: string, ids: string[]) => {
    const lists = await Promise.all(ids.map((id) => listRuns(pid, id)));
    setRuns(lists.flat());
  }, errorMessage);
  useEffect(() => {
    setRuns(null);
    if (projectId && key !== "") void load(projectId, key.split("\n"));
  }, [projectId, key, load]);
  const reload = useCallback(() => {
    if (projectId && key !== "") void load(projectId, key.split("\n"));
  }, [projectId, key, load]);
  return { sources, runs, pending: state.pending, error: state.error, reload };
}

export const DEFAULT_EXPORT_FORMAT: ExportFormat = "png16";

const isFormat = (v: unknown): v is ExportFormat => typeof v === "string" && (EXPORT_FORMATS as readonly string[]).includes(v);

const runLabel = (r: RunItem): string =>
  [shortId(r.id), r.attempt !== null ? `attempt ${r.attempt}` : null, r.engineId, r.state === "GATE_FAIL" ? (r.overrideReason ? "GATE_FAIL, overridden" : "GATE_FAIL") : r.state]
    .filter(Boolean)
    .join(" · ");

export function ExportEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const upstream = useUpstreamRuns(nodeId);
  const candidates = useMemo(() => exportCandidates(upstream.runs ?? []), [upstream.runs]);
  const [picked, setPicked] = useState<string>("");
  const [reason, setReason] = useState("");
  const [result, setResult] = useState<ExportResult | null>(null);

  const format = isFormat(params.format) ? params.format : DEFAULT_EXPORT_FORMAT;
  const outDir = asString(params.outDir);
  const name = asString(params.name);
  const exported = asNumber(params.exported, 0);

  // the first candidate (DONE or overridden first) until the artist picks one
  const run = candidates.find((r) => r.id === picked) ?? candidates[0] ?? null;
  const needsReason = run !== null && !run.exportable;
  const reasonOk = reason.trim().length >= MIN_OVERRIDE_CHARS;

  const [doExport, exportState] = useAction(async (r: RunItem) => {
    const res = await exportRun(r.id, {
      out_dir: outDir.trim(),
      format,
      ...(name.trim() ? { name: name.trim() } : {}),
      ...(r.exportable ? {} : { override: true, reason: reason.trim() }),
    });
    setResult(res);
    setReason("");
    set({ exported: exported + 1, lastPath: res.path ?? undefined });
    upstream.reload();
  }, errorMessage);

  const blocker =
    run === null
      ? "no take to export yet"
      : outDir.trim() === ""
        ? "type the folder to export into"
        : needsReason && !reasonOk
          ? `a GATE_FAIL take needs an override reason of at least ${MIN_OVERRIDE_CHARS} characters`
          : null;

  return (
    <EditorFrame kind="export" nodeId={nodeId} title="Export">
      <Section title="Take" testId="export-take">
        {upstream.sources.length === 0 ? (
          <span style={E.note} data-testid="export-nosource">
            connect the node whose take you want to export
          </span>
        ) : (
          <div style={E.row}>
            <Field label="candidate">
              <select
                style={E.wide}
                value={run?.id ?? ""}
                data-testid="export-candidate"
                aria-label="candidate"
                onChange={(e) => {
                  setPicked(e.target.value);
                  setReason("");
                }}
              >
                {candidates.length === 0 ? <option value="">{upstream.pending ? "loading the runs" : "no finished take"}</option> : null}
                {candidates.map((r) => (
                  <option key={r.id} value={r.id}>
                    {runLabel(r)}
                  </option>
                ))}
              </select>
            </Field>
            <Button onClick={upstream.reload} disabled={upstream.pending} testId="export-refresh" title="read the runs again">
              Refresh
            </Button>
          </div>
        )}
        {run ? (
          <div style={E.row}>
            <Chip tone={run.state === "DONE" ? "pass" : run.exportable ? "advisory" : "fail"} testId="export-state">
              {run.state}
            </Chip>
            {run.overrideReason ? <span style={E.note}>override: {run.overrideReason}</span> : null}
          </div>
        ) : null}
        {needsReason ? (
          <Field label="override">
            <input
              type="text"
              style={E.wide}
              value={reason}
              placeholder={`why ship a take that failed its gate (at least ${MIN_OVERRIDE_CHARS} characters)`}
              data-testid="export-reason"
              aria-label="override reason"
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>
        ) : null}
      </Section>

      <Section title="Output" testId="export-output">
        <div style={E.row}>
          <Field label="format">
            <select value={format} data-testid="export-format" aria-label="format" style={E.input} onChange={(e) => set({ format: e.target.value })}>
              {EXPORT_FORMATS.map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </select>
          </Field>
          <Field label="name">
            <input type="text" style={E.input} value={name} placeholder="default: the run id" data-testid="export-name" onChange={(e) => set({ name: e.target.value || undefined })} />
          </Field>
        </div>
        <Field label="out dir">
          <input
            type="text"
            style={E.wide}
            value={outDir}
            placeholder="absolute folder on the daemon's machine"
            data-testid="export-outdir"
            onChange={(e) => set({ outDir: e.target.value || undefined })}
          />
        </Field>
        <div style={E.row}>
          <Button onClick={() => run && void doExport(run)} disabled={blocker !== null || exportState.pending} on={blocker === null} testId="export-go" title={blocker ?? "write the take and its gate sidecar"}>
            {exportState.pending ? "Exporting" : "Export"}
          </Button>
          {blocker ? <span style={E.note}>{blocker}</span> : null}
        </div>
      </Section>

      {result ? (
        <Section title="Written" testId="export-result">
          <span style={E.note} data-testid="export-path">
            {result.path ?? "the daemon did not say where"}
          </span>
          {result.sidecar ? (
            <span style={E.note} data-testid="export-sidecar">
              {result.sidecar}
            </span>
          ) : null}
          {result.shipped ? <Chip tone="pass">gate report marked shipped</Chip> : null}
        </Section>
      ) : null}

      {(exportState.error ?? upstream.error) ? (
        <Notice tone="error" testId="export-error">
          {exportState.error ?? upstream.error}
        </Notice>
      ) : null}
    </EditorFrame>
  );
}
