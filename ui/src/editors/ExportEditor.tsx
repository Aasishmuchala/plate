// Export node editor (T69, T106): hand takes to the artist's folder. The candidates are the runs of the node that feeds
// this one (the nearest upstream node that makes something), DONE and overridden first. Export does not need an accept
// (decision 39); a GATE_FAIL take needs an override reason of at least 10 characters, which the daemon ledgers as an
// `override` record.
//   still   `POST /runs/{id}/export {out_dir, format: png16 | tiff16 | exr, name?}`: `<name>.<ext>` plus
//           `<name>.gate.json` (lineage embedded, the gate report marked shipped)
//   clip    a take whose `outputs[0]` ends `.mp4`, whose `normalized.media` is `video` or that records a time map goes
//           out through `POST /runs/{id}/deliver {format: prores4444 | hevc | exr_sequence | fcpxml, out_dir, name?,
//           override_reason?}` (T74)
//   batch   tick takes (or `Select all accepted`) and `POST /projects/{id}/export {run_ids, out_dir, format}` (T86):
//           every take through the single export's guards; a refusal is listed and never stops the others
//   cutdown the `Cutdown` disclosure: a 9:16 | 4:5 | 1:1 | 16:9 crop of the approved master (decision 56), centred at
//           cx / cy (frame fractions), 8-bit png | jpeg for a still or h264 | hevc for a clip,
//           `POST /runs/{id}/cutdown` (T86)
// Node params: `format` (still), `clipFormat`, `outDir`, `name`, `cutdownPreset`, `cutdownCx`, `cutdownCy`,
// `cutdownFormat`, `cutdownCodec`, `exported` (every take file written from here) and `lastPath`. Export makes nothing,
// so the Make bar has nothing to quote here (`MAKE_MODES.Export` is `none`). `ClipDeliver` is the clip half on its own,
// for the video player's `Deliver…`.

import { type ReactElement, useCallback, useEffect, useMemo, useState } from "react";
import type { EdgeLike, GraphNode } from "../canvasRules";
import { MAKE_MODES } from "../makebar/engines";
import { shortId } from "../panels/format";
import { useGraphStore } from "../store";
import { errorMessage } from "./api";
import {
  CLIP_FORMAT_NOTES,
  CLIP_FORMATS,
  type ClipFormat,
  CUTDOWN_CODECS,
  CUTDOWN_FORMATS,
  CUTDOWN_PRESETS,
  type CutdownBody,
  type CutdownCodec,
  type CutdownFormat,
  type CutdownResult,
  cutdownRun,
  deliverRun,
  type ExportManyResult,
  exportMany,
  isClipFormat,
  isCutdownPreset,
  listTakes,
  type TakeItem,
} from "./deliverApi";
import { MIN_OVERRIDE_CHARS } from "./movePolicy";
import { EXPORT_FORMATS, type ExportFormat, exportCandidates, exportRun, type RunItem } from "./nodeApi";
import { asNumber, asString, useNodeEditor } from "./params";
import { E } from "./theme";
import { Button, Chip, EditorFrame, Field, Notice, NumberField, RangeField, Section, useAction } from "./widgets";

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
  /** Run items with their media facts (a `TakeItem` is a `RunItem`). */
  runs: TakeItem[] | null;
  pending: boolean;
  error: string | null;
  reload: () => void;
}

/** The runs of the nodes feeding `nodeId`, read from `GET /projects/{pid}/runs?node_id=` as takes, newest first per node. */
export function useUpstreamRuns(nodeId: string): UpstreamRuns {
  const projectId = useGraphStore((s) => s.projectId);
  const nodes = useGraphStore((s) => s.nodes);
  const edges = useGraphStore((s) => s.edges);
  const sources = useMemo(() => producingNodes(nodes, edges, nodeId), [nodes, edges, nodeId]);
  const key = sources.join("\n");
  const [runs, setRuns] = useState<TakeItem[] | null>(null);
  const [load, state] = useAction(async (pid: string, ids: string[]) => {
    const lists = await Promise.all(ids.map((id) => listTakes(pid, id)));
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
export const DEFAULT_CLIP_FORMAT: ClipFormat = "prores4444";
export const DEFAULT_CUTDOWN = { preset: "9:16", cx: 0.5, cy: 0.5, format: "png", codec: "h264" } as const;

const isFormat = (v: unknown): v is ExportFormat => typeof v === "string" && (EXPORT_FORMATS as readonly string[]).includes(v);
const isCutdownFormat = (v: unknown): v is CutdownFormat => typeof v === "string" && (CUTDOWN_FORMATS as readonly string[]).includes(v);
const isCutdownCodec = (v: unknown): v is CutdownCodec => typeof v === "string" && (CUTDOWN_CODECS as readonly string[]).includes(v);

/** `exportCandidates` keeps the objects it is given, so the media facts of a take survive the ranking. */
const candidatesOf = <T extends RunItem>(runs: readonly T[]): T[] => exportCandidates(runs) as T[];

const runLabel = (r: RunItem): string =>
  [shortId(r.id), r.attempt !== null ? `attempt ${r.attempt}` : null, r.engineId, r.state === "GATE_FAIL" ? (r.overrideReason ? "GATE_FAIL, overridden" : "GATE_FAIL") : r.state]
    .filter(Boolean)
    .join(" · ");

const boxText = (b: CutdownResult["box"]): string => (b ? `${b[0]}, ${b[1]}  ${b[2]} x ${b[3]}` : "box not reported");

/** What was written by the last single export or delivery. */
interface Written {
  path: string | null;
  sidecar: string | null;
  /** `shipped` for an export that marked the report shipped, else the delivery format. */
  note: string | null;
}

/** Why the clip cannot go yet, or null: a folder, and a 10-character reason for a GATE_FAIL without an override. */
function deliverBlocker(outDir: string, needsReason: boolean, reason: string): string | null {
  if (outDir.trim() === "") return "type the folder to deliver into";
  if (needsReason && reason.trim().length < MIN_OVERRIDE_CHARS) {
    return `a GATE_FAIL take needs an override reason of at least ${MIN_OVERRIDE_CHARS} characters`;
  }
  return null;
}

/**
 * The clip formats on their own (the video player's `Deliver…`): `POST /runs/{id}/deliver` with the format, folder,
 * name and, for a GATE_FAIL take without a recorded override, the reason. Local state: it writes no node.
 */
export function ClipDeliver({ runId, needsReason = false, testId = "clip-deliver" }: { runId: string | null; needsReason?: boolean; testId?: string }): ReactElement {
  const [format, setFormat] = useState<ClipFormat>(DEFAULT_CLIP_FORMAT);
  const [outDir, setOutDir] = useState("");
  const [name, setName] = useState("");
  const [reason, setReason] = useState("");
  const [done, setDone] = useState<Written | null>(null);
  const [go, state] = useAction(async (id: string) => {
    const res = await deliverRun(id, {
      format,
      out_dir: outDir.trim(),
      ...(name.trim() ? { name: name.trim() } : {}),
      ...(needsReason ? { override_reason: reason.trim() } : {}),
    });
    setDone({ path: res.path, sidecar: res.sidecar, note: res.format ?? format });
  }, errorMessage);
  const blocker = runId === null ? "no take to deliver yet" : deliverBlocker(outDir, needsReason, reason);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }} data-testid={testId}>
      <div style={E.row}>
        <Field label="format">
          <select value={format} style={E.input} data-testid={`${testId}-format`} aria-label="clip format" onChange={(e) => isClipFormat(e.target.value) && setFormat(e.target.value)}>
            {CLIP_FORMATS.map((f) => (
              <option key={f} value={f} title={CLIP_FORMAT_NOTES[f]}>
                {f}
              </option>
            ))}
          </select>
        </Field>
        <Field label="name">
          <input type="text" style={E.input} value={name} placeholder="default: the run id" data-testid={`${testId}-name`} onChange={(e) => setName(e.target.value)} />
        </Field>
      </div>
      <Field label="out dir">
        <input type="text" style={E.wide} value={outDir} placeholder="absolute folder on the daemon's machine" data-testid={`${testId}-outdir`} onChange={(e) => setOutDir(e.target.value)} />
      </Field>
      {needsReason ? (
        <Field label="override">
          <input
            type="text"
            style={E.wide}
            value={reason}
            placeholder={`why ship a take that failed its gate (at least ${MIN_OVERRIDE_CHARS} characters)`}
            data-testid={`${testId}-reason`}
            aria-label="override reason"
            onChange={(e) => setReason(e.target.value)}
          />
        </Field>
      ) : null}
      <div style={E.row}>
        <Button onClick={() => runId && void go(runId)} disabled={blocker !== null || state.pending} on={blocker === null} testId={`${testId}-go`} title={blocker ?? CLIP_FORMAT_NOTES[format]}>
          {state.pending ? "Delivering" : "Deliver"}
        </Button>
        <span style={E.note}>{blocker ?? CLIP_FORMAT_NOTES[format]}</span>
      </div>
      {done ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <span style={E.note} data-testid={`${testId}-path`}>
            {done.path ?? "the daemon did not say where"}
          </span>
          {done.sidecar ? (
            <span style={E.note} data-testid={`${testId}-sidecar`}>
              {done.sidecar}
            </span>
          ) : null}
        </div>
      ) : null}
      {state.error ? (
        <Notice tone="error" testId={`${testId}-error`}>
          {state.error}
        </Notice>
      ) : null}
    </div>
  );
}

export function ExportEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const projectId = useGraphStore((s) => s.projectId);
  const upstream = useUpstreamRuns(nodeId);
  const candidates = useMemo(() => candidatesOf(upstream.runs ?? []), [upstream.runs]);
  const [picked, setPicked] = useState<string>("");
  const [reason, setReason] = useState("");
  const [result, setResult] = useState<Written | null>(null);
  const [batch, setBatch] = useState<string[]>([]);
  const [batchResult, setBatchResult] = useState<ExportManyResult | null>(null);
  const [cutOpen, setCutOpen] = useState(false);
  const [cutName, setCutName] = useState("");
  const [cutLong, setCutLong] = useState<number | null>(null);
  const [cutResult, setCutResult] = useState<CutdownResult | null>(null);

  const format = isFormat(params.format) ? params.format : DEFAULT_EXPORT_FORMAT;
  const clipFormat = isClipFormat(params.clipFormat) ? params.clipFormat : DEFAULT_CLIP_FORMAT;
  const outDir = asString(params.outDir);
  const name = asString(params.name);
  const exported = asNumber(params.exported, 0);
  const preset = isCutdownPreset(params.cutdownPreset) ? params.cutdownPreset : DEFAULT_CUTDOWN.preset;
  const cx = asNumber(params.cutdownCx, DEFAULT_CUTDOWN.cx);
  const cy = asNumber(params.cutdownCy, DEFAULT_CUTDOWN.cy);
  const cutFormat = isCutdownFormat(params.cutdownFormat) ? params.cutdownFormat : DEFAULT_CUTDOWN.format;
  const cutCodec = isCutdownCodec(params.cutdownCodec) ? params.cutdownCodec : DEFAULT_CUTDOWN.codec;

  // the first candidate (DONE or overridden first) until the artist picks one
  const run = candidates.find((r) => r.id === picked) ?? candidates[0] ?? null;
  const clip = run?.media === "video";
  const needsReason = run !== null && !run.exportable;
  const reasonOk = reason.trim().length >= MIN_OVERRIDE_CHARS;
  const accepted = candidates.filter((r) => r.accepted);
  const selected = batch.filter((id) => candidates.some((r) => r.id === id));

  // every take file written from this node counts once (the .gate.json sidecars do not)
  const count = (n: number): void => set({ exported: asNumber(paramNow(nodeId, "exported"), exported) + n });

  const [doExport, exportState] = useAction(async (r: TakeItem) => {
    if (r.media === "video") {
      const res = await deliverRun(r.id, {
        format: clipFormat,
        out_dir: outDir.trim(),
        ...(name.trim() ? { name: name.trim() } : {}),
        ...(r.exportable ? {} : { override_reason: reason.trim() }),
      });
      setResult({ path: res.path, sidecar: res.sidecar, note: `delivered as ${res.format ?? clipFormat}` });
      setReason("");
      count(1);
      set({ lastPath: res.path ?? undefined });
    } else {
      const res = await exportRun(r.id, {
        out_dir: outDir.trim(),
        format,
        ...(name.trim() ? { name: name.trim() } : {}),
        ...(r.exportable ? {} : { override: true, reason: reason.trim() }),
      });
      setResult({ path: res.path, sidecar: res.sidecar, note: res.shipped ? "gate report marked shipped" : null });
      setReason("");
      count(1);
      set({ lastPath: res.path ?? undefined });
    }
    upstream.reload();
  }, errorMessage);

  const [doBatch, batchState] = useAction(async (pid: string, ids: string[]) => {
    const res = await exportMany(pid, { run_ids: ids, out_dir: outDir.trim(), format });
    setBatchResult(res);
    if (res.exported.length > 0) {
      count(res.exported.length);
      set({ lastPath: res.exported[res.exported.length - 1]?.path ?? undefined });
    }
    upstream.reload();
  }, errorMessage);

  const [doCutdown, cutState] = useAction(async (r: TakeItem) => {
    const body: CutdownBody = {
      preset,
      cx,
      cy,
      out_dir: outDir.trim(),
      ...(cutName.trim() ? { name: cutName.trim() } : {}),
      ...(r.media === "video" ? { codec: cutCodec } : { format: cutFormat }),
      ...(cutLong !== null ? { long_side: cutLong } : {}),
    };
    const res = await cutdownRun(r.id, body);
    setCutResult(res);
    count(1);
    set({ lastPath: res.path ?? undefined });
  }, errorMessage);

  const verb = clip ? "deliver" : "export";
  const blocker =
    run === null
      ? `no take to ${verb} yet`
      : outDir.trim() === ""
        ? `type the folder to ${verb} into`
        : needsReason && !reasonOk
          ? `a GATE_FAIL take needs an override reason of at least ${MIN_OVERRIDE_CHARS} characters`
          : null;
  const batchBlocker =
    projectId === null ? "open a project first" : selected.length === 0 ? "tick the takes to export" : outDir.trim() === "" ? "type the folder to export into" : null;
  const cutBlocker =
    run === null
      ? "no take to cut down yet"
      : outDir.trim() === ""
        ? "type the folder to write into"
        : needsReason
          ? "a GATE_FAIL take needs an override recorded first (Export or Deliver it with a reason)"
          : null;

  const toggle = (id: string, on: boolean): void => setBatch((b) => (on ? [...b.filter((x) => x !== id), id] : b.filter((x) => x !== id)));

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
                  setCutResult(null);
                }}
              >
                {candidates.length === 0 ? <option value="">{upstream.pending ? "loading the runs" : "no finished take"}</option> : null}
                {candidates.map((r) => (
                  <option key={r.id} value={r.id}>
                    {runLabel(r)}
                    {r.media === "video" ? " · clip" : ""}
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
            <Chip tone="info" testId="export-media" title={clip ? "a clip: it is delivered (ProRes, HEVC, EXR sequence or FCPXML)" : "a still: it is exported (16-bit PNG, TIFF or EXR)"}>
              {clip ? "clip" : "still"}
            </Chip>
            {run.accepted ? (
              <Chip tone="pass" testId="export-accepted">
                accepted
              </Chip>
            ) : null}
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
            {clip ? (
              <select value={clipFormat} data-testid="export-format" aria-label="format" style={E.input} onChange={(e) => set({ clipFormat: e.target.value })}>
                {CLIP_FORMATS.map((f) => (
                  <option key={f} value={f} title={CLIP_FORMAT_NOTES[f]}>
                    {f}
                  </option>
                ))}
              </select>
            ) : (
              <select value={format} data-testid="export-format" aria-label="format" style={E.input} onChange={(e) => set({ format: e.target.value })}>
                {EXPORT_FORMATS.map((f) => (
                  <option key={f} value={f}>
                    {f}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field label="name">
            <input type="text" style={E.input} value={name} placeholder="default: the run id" data-testid="export-name" onChange={(e) => set({ name: e.target.value || undefined })} />
          </Field>
        </div>
        {clip ? (
          <span style={E.note} data-testid="export-format-note">
            {CLIP_FORMAT_NOTES[clipFormat]}
          </span>
        ) : null}
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
          <Button
            onClick={() => run && void doExport(run)}
            disabled={blocker !== null || exportState.pending}
            on={blocker === null}
            testId="export-go"
            title={blocker ?? (clip ? "deliver the clip with its lineage and gate sidecar" : "write the take and its gate sidecar")}
          >
            {exportState.pending ? (clip ? "Delivering" : "Exporting") : clip ? "Deliver" : "Export"}
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
          {result.note ? <Chip tone="pass">{result.note}</Chip> : null}
        </Section>
      ) : null}

      {(exportState.error ?? upstream.error) ? (
        <Notice tone="error" testId="export-error">
          {exportState.error ?? upstream.error}
        </Notice>
      ) : null}

      {candidates.length > 0 ? (
        <Section title="Batch export" testId="export-batch">
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }} role="group" aria-label="takes to export">
            {candidates.map((r) => (
              <label key={r.id} style={E.field}>
                <input type="checkbox" checked={selected.includes(r.id)} data-testid={`export-batch-${r.id}`} onChange={(e) => toggle(r.id, e.target.checked)} />
                <span>
                  {runLabel(r)}
                  {r.accepted ? " · accepted" : ""}
                  {r.media === "video" ? " · clip" : ""}
                </span>
              </label>
            ))}
          </div>
          <div style={E.row}>
            <Button
              onClick={() => setBatch(accepted.map((r) => r.id))}
              disabled={accepted.length === 0}
              testId="export-select-accepted"
              title={accepted.length === 0 ? "no take upstream has been accepted yet" : `tick the ${accepted.length} accepted take${accepted.length === 1 ? "" : "s"}`}
            >
              Select all accepted
            </Button>
            <Button
              onClick={() => projectId && void doBatch(projectId, selected)}
              disabled={batchBlocker !== null || batchState.pending}
              on={batchBlocker === null}
              testId="export-batch-go"
              title={batchBlocker ?? `export ${selected.length} take${selected.length === 1 ? "" : "s"} as ${format}`}
            >
              {batchState.pending ? "Exporting" : `Export ${selected.length} selected`}
            </Button>
            <span style={E.note}>{batchBlocker ?? `as ${format}, named {node}_{run8}; clips go through Deliver`}</span>
          </div>
          {batchResult ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }} data-testid="export-batch-result">
              {batchResult.exported.map((x, i) => (
                <span key={`${x.runId ?? i}`} style={E.note} data-testid="export-batch-path">
                  {x.path ?? "written"}
                  {x.sidecar ? ` + ${x.sidecar}` : ""}
                </span>
              ))}
              {batchResult.refused.map((x, i) => (
                <span key={`${x.runId ?? i}`} style={E.error} data-testid="export-batch-refused">
                  {x.runId ? `${shortId(x.runId)}: ` : ""}
                  {x.detail ?? x.error}
                  {x.code ? ` (${x.code})` : ""}
                </span>
              ))}
              {batchResult.exported.length === 0 && batchResult.refused.length === 0 ? <span style={E.note}>the daemon wrote nothing</span> : null}
            </div>
          ) : null}
          {batchState.error ? (
            <Notice tone="error" testId="export-batch-error">
              {batchState.error}
            </Notice>
          ) : null}
        </Section>
      ) : null}

      <Section title="Cutdown" testId="export-cutdown">
        <div style={E.row}>
          <Button onClick={() => setCutOpen((o) => !o)} on={cutOpen} testId="export-cutdown-toggle" title="a social crop of the approved master: downsample only, inside the safe frame">
            {cutOpen ? "Cutdown ▾" : "Cutdown ▸"}
          </Button>
          {!cutOpen ? <span style={E.note}>9:16, 4:5, 1:1 or 16:9 from the approved master, never upscaled</span> : null}
        </div>
        {cutOpen ? (
          <>
            <div style={E.row}>
              <Field label="preset">
                <select value={preset} style={E.input} data-testid="export-cutdown-preset" aria-label="cutdown preset" onChange={(e) => set({ cutdownPreset: e.target.value })}>
                  {CUTDOWN_PRESETS.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              </Field>
              {clip ? (
                <Field label="codec">
                  <select value={cutCodec} style={E.input} data-testid="export-cutdown-codec" aria-label="cutdown codec" onChange={(e) => set({ cutdownCodec: e.target.value })}>
                    {CUTDOWN_CODECS.map((c) => (
                      <option key={c} value={c}>
                        {c}
                      </option>
                    ))}
                  </select>
                </Field>
              ) : (
                <Field label="format">
                  <select value={cutFormat} style={E.input} data-testid="export-cutdown-format" aria-label="cutdown format" onChange={(e) => set({ cutdownFormat: e.target.value })}>
                    {CUTDOWN_FORMATS.map((f) => (
                      <option key={f} value={f}>
                        {f}
                      </option>
                    ))}
                  </select>
                </Field>
              )}
            </div>
            <RangeField label="centre x" value={cx} min={0} max={1} step={0.01} onChange={(v) => set({ cutdownCx: v })} testId="export-cutdown-cx" format={(v) => v.toFixed(2)} />
            <RangeField label="centre y" value={cy} min={0} max={1} step={0.01} onChange={(v) => set({ cutdownCy: v })} testId="export-cutdown-cy" format={(v) => v.toFixed(2)} />
            <div style={E.row}>
              <NumberField label="long side" value={cutLong} onChange={setCutLong} min={2} step={2} suffix="px (empty: the crop's own size; never larger)" testId="export-cutdown-long" />
              <Field label="name">
                <input type="text" style={E.input} value={cutName} placeholder={`default: <run>_${preset.replace(":", "x")}`} data-testid="export-cutdown-name" onChange={(e) => setCutName(e.target.value)} />
              </Field>
            </div>
            <div style={E.row}>
              <Button onClick={() => run && void doCutdown(run)} disabled={cutBlocker !== null || cutState.pending} on={cutBlocker === null} testId="export-cutdown-go" title={cutBlocker ?? `cut ${preset} into the out dir`}>
                {cutState.pending ? "Cutting" : `Cut down ${preset}`}
              </Button>
              <span style={E.note}>{cutBlocker ?? (clip ? `${cutCodec} BT.709, lineage in the container` : `8-bit sRGB ${cutFormat}, lineage and the IPTC AI marker inside`)}</span>
            </div>
            {cutResult ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 2 }} data-testid="export-cutdown-result">
                <span style={E.note} data-testid="export-cutdown-path">
                  {cutResult.path ?? "the daemon did not say where"}
                </span>
                {cutResult.sidecar ? (
                  <span style={E.note} data-testid="export-cutdown-sidecar">
                    {cutResult.sidecar}
                  </span>
                ) : null}
                <span style={E.note} data-testid="export-cutdown-box">
                  crop {boxText(cutResult.box)}
                </span>
              </div>
            ) : null}
            {cutState.error ? (
              <Notice tone="error" testId="export-cutdown-error">
                {cutState.error}
              </Notice>
            ) : null}
          </>
        ) : null}
      </Section>
    </EditorFrame>
  );
}

/** A node param read from the store at call time: two actions finishing together must not count from one stale value. */
function paramNow(nodeId: string, key: string): unknown {
  return useGraphStore.getState().nodes.find((n) => n.id === nodeId)?.data.params?.[key];
}
