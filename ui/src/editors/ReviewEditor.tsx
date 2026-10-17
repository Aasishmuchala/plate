// Review node editor (T69): build a static review bundle for the client (A/B wipe against the plate, gate badges,
// watermarked proxies, a comment form). The takes are the DONE and overridden runs of the node that feeds this one;
// `Build bundle` posts `POST /projects/{pid}/review {run_ids, watermark, title}` and shows the zip the daemon wrote
// under the project's `review/` folder. The daemon refuses previs takes and GATE_FAIL takes without an override.
// Node params: `watermark` (default REVIEW), `title`, `bundleTakes` and `bundlePath` (the last bundle).

import { type ReactElement, useMemo, useState } from "react";
import { formatBytes, shortId } from "../panels/format";
import { useGraphStore } from "../store";
import { errorMessage } from "./api";
import { useUpstreamRuns } from "./ExportEditor";
import { buildReview, type ReviewResult } from "./nodeApi";
import { asString, useNodeEditor } from "./params";
import { E } from "./theme";
import { Button, Chip, EditorFrame, Field, Notice, Section, Toggle, useAction } from "./widgets";

export const DEFAULT_WATERMARK = "REVIEW";

export function ReviewEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const projectId = useGraphStore((s) => s.projectId);
  const upstream = useUpstreamRuns(nodeId);
  const takes = useMemo(() => (upstream.runs ?? []).filter((r) => r.exportable), [upstream.runs]);
  const failing = (upstream.runs ?? []).filter((r) => r.state === "GATE_FAIL" && !r.exportable).length;
  const [chosen, setChosen] = useState<string[]>([]);
  const [result, setResult] = useState<ReviewResult | null>(null);

  const watermark = typeof params.watermark === "string" ? params.watermark : DEFAULT_WATERMARK;
  const title = asString(params.title);
  const selected = chosen.filter((id) => takes.some((r) => r.id === id));

  const toggle = (id: string, on: boolean): void =>
    setChosen((cur) => (on ? Array.from(new Set([...cur, id])) : cur.filter((x) => x !== id)));

  const [build, buildState] = useAction(async (pid: string, runIds: string[]) => {
    const res = await buildReview(pid, {
      run_ids: runIds,
      watermark: watermark.trim() || DEFAULT_WATERMARK,
      ...(title.trim() ? { title: title.trim() } : {}),
    });
    setResult(res);
    set({ bundleTakes: res.takes ?? runIds.length, bundlePath: res.zipPath ?? undefined });
  }, errorMessage);

  const blocker = !projectId ? "open a project first" : selected.length === 0 ? "pick at least one take" : null;

  return (
    <EditorFrame kind="review" nodeId={nodeId} title="Review">
      <Section title="Takes" testId="review-takes">
        {upstream.sources.length === 0 ? (
          <span style={E.note}>connect the node whose takes the client should see</span>
        ) : takes.length === 0 ? (
          <span style={E.note} data-testid="review-notakes">
            {upstream.pending ? "loading the runs" : "no DONE or overridden take yet"}
          </span>
        ) : (
          takes.map((r) => (
            <Toggle
              key={r.id}
              label={[shortId(r.id), r.attempt !== null ? `attempt ${r.attempt}` : null, r.engineId, r.state === "DONE" ? "DONE" : "overridden"].filter(Boolean).join(" · ")}
              checked={selected.includes(r.id)}
              onChange={(v) => toggle(r.id, v)}
              testId={`review-run-${r.id}`}
            />
          ))
        )}
        {failing > 0 ? (
          <span style={E.note} data-testid="review-failing">
            {failing} GATE_FAIL take{failing === 1 ? "" : "s"} need an override before review
          </span>
        ) : null}
        <div style={E.row}>
          <Button onClick={upstream.reload} disabled={upstream.pending || upstream.sources.length === 0} testId="review-refresh">
            Refresh
          </Button>
        </div>
      </Section>

      <Section title="Bundle" testId="review-bundle">
        <Field label="watermark">
          <input type="text" style={E.wide} value={watermark} data-testid="review-watermark" onChange={(e) => set({ watermark: e.target.value })} />
        </Field>
        <Field label="title">
          <input type="text" style={E.wide} value={title} placeholder="shown at the top of the bundle" data-testid="review-title" onChange={(e) => set({ title: e.target.value || undefined })} />
        </Field>
        <div style={E.row}>
          <Button
            onClick={() => projectId && void build(projectId, selected)}
            disabled={blocker !== null || buildState.pending}
            on={blocker === null}
            testId="review-build"
            title={blocker ?? "write the static HTML bundle with watermarked proxies"}
          >
            {buildState.pending ? "Building" : "Build bundle"}
          </Button>
          {blocker ? <span style={E.note}>{blocker}</span> : <span style={E.note}>{selected.length} take{selected.length === 1 ? "" : "s"}</span>}
        </div>
      </Section>

      {result ? (
        <Section title="Bundle written" testId="review-result">
          <span style={E.note} data-testid="review-zip">
            {result.zipPath ?? "the daemon did not say where"}
          </span>
          <div style={E.row}>
            <Chip tone="info" testId="review-size">
              {formatBytes(result.bytes)}
            </Chip>
            {result.takes !== null ? <Chip tone="muted">{result.takes} takes</Chip> : null}
          </div>
        </Section>
      ) : null}

      {(buildState.error ?? upstream.error) ? (
        <Notice tone="error" testId="review-error">
          {buildState.error ?? upstream.error}
        </Notice>
      ) : null}
    </EditorFrame>
  );
}
