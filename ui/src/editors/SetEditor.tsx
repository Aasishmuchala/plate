// Set node editor (T47): one look across the cameras of a set. The hero camera anchors the look; the table lists every
// material's Lab on the hero and its cross-view ΔE00 p90 on each other view, and the chip holds the worst locked
// material to the gate: p90 <= 3, hard. Editable materials (vegetation, people, sky, water) are scored but advisory.
// Numbers come from the daemon's numeric preview of the Set node (`setgate.run`); nothing here is estimated.
// Node params: `hero` (the hero's packet id).

import { type ReactElement, useMemo, useState } from "react";
import { useGraphStore } from "../store";
import { errorMessage, SET_P90_LIMIT, type SetSummary, setPreview } from "./api";
import { labText, labToHex } from "./colour";
import { asString, upstreamPlates, useNodeEditor } from "./params";
import { E } from "./theme";
import { Button, Chip, EditorFrame, Field, Notice, Section, useAction } from "./widgets";

const f1 = (v: number | null): string => (v === null ? "n/a" : v.toFixed(1));

export function SetEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const nodes = useGraphStore((s) => s.nodes);
  const edges = useGraphStore((s) => s.edges);
  const projectId = useGraphStore((s) => s.projectId);
  const [summary, setSummary] = useState<SetSummary | null>(null);

  // the views are the Plates upstream of the Set; with none connected, every Plate of the graph
  const connected = useMemo(() => upstreamPlates(nodes, edges, nodeId), [nodes, edges, nodeId]);
  const plates = useMemo(() => (connected.length > 0 ? connected : nodes.filter((n) => n.data.kind === "Plate")), [connected, nodes]);
  const options = plates.flatMap((p) => (p.data.packetId && p.data.packetId !== "unlinked" ? [{ id: p.data.packetId, label: `${p.data.label} · ${p.data.packetId}` }] : []));
  const hero = asString(params.hero);
  const heroKnown = options.some((o) => o.id === hero);
  const views = options.map((o) => o.id).filter((id) => id !== hero);

  const [measure, state] = useAction(async () => {
    if (!projectId) throw new Error("open a project to measure");
    setSummary(await setPreview(projectId, nodeId, { hero, views }));
  }, errorMessage);

  const canMeasure = projectId !== null && heroKnown && views.length > 0 && !state.pending;
  const p90 = summary?.crossViewP90 ?? null;
  const tone = p90 === null ? "muted" : p90 <= SET_P90_LIMIT ? "pass" : summary?.verdict === "advisory" ? "advisory" : "fail";
  const columns = summary && summary.views.length > 0 ? summary.views : views;

  return (
    <EditorFrame kind="set" nodeId={nodeId} title="Set">
      <Section title="Hero camera" testId="set-hero">
        <Field label="hero">
          <select style={E.wide} value={heroKnown ? hero : ""} data-testid="set-hero-select" onChange={(e) => set({ hero: e.target.value || undefined })}>
            <option value="">{options.length === 0 ? "no plate with a packet" : "pick the hero camera"}</option>
            {options.map((o) => (
              <option key={o.id} value={o.id}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
        <span style={E.note} data-testid="set-views">
          {connected.length === 0 && plates.length > 0 ? "no plate is connected to this node; using every plate in the graph. " : ""}
          {views.length} other view{views.length === 1 ? "" : "s"}: {views.join(", ") || "none"}
        </span>
        <div style={E.row}>
          <Button onClick={() => void measure()} disabled={!canMeasure} testId="set-measure">
            {state.pending ? "Measuring" : "Measure"}
          </Button>
          <Chip tone={tone} testId="set-p90" title={`cross-view ΔE00 p90 of the worst locked material, held to <= ${SET_P90_LIMIT}`}>
            cross-view p90 {f1(p90)} (limit {SET_P90_LIMIT})
          </Chip>
          {summary?.verdict ? (
            <Chip tone={summary.verdict === "pass" ? "pass" : summary.verdict === "fail" ? "fail" : "advisory"} testId="set-verdict">
              {summary.verdict}
            </Chip>
          ) : null}
        </div>
      </Section>

      <Section title="Material targets (from the hero)" testId="set-targets">
        {summary && summary.rows.length > 0 ? (
          <table style={E.table}>
            <thead>
              <tr>
                <th style={E.th}>material</th>
                <th style={E.th}>Lab on hero</th>
                <th style={E.th}>p90</th>
                {columns.map((v) => (
                  <th key={v} style={E.th} data-testid="set-view-col">
                    {v}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {summary.rows.map((r) => (
                <tr key={r.id} data-testid="set-row" data-hard={r.hard}>
                  <td style={E.td}>
                    {r.lab ? <span aria-hidden style={{ display: "inline-block", width: 10, height: 10, marginRight: 4, background: labToHex(r.lab), border: "1px solid #48484a" }} /> : null}
                    {r.name}
                    {!r.hard ? <span style={E.note}> (advisory)</span> : null}
                  </td>
                  <td style={E.td} data-testid="set-lab">
                    {r.lab ? labText(r.lab) : "n/a"}
                  </td>
                  <td style={E.td} data-testid="set-row-p90">
                    {f1(r.p90)}
                  </td>
                  {columns.map((v) => (
                    <td key={v} style={E.td} data-testid="set-cell">
                      {f1(r.perView[v] ?? null)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <span style={E.note} data-testid="set-empty">
            not measured yet: pick the hero and press Measure
          </span>
        )}
        {summary?.notes.map((n) => (
          <span key={n} style={E.note}>
            {n}
          </span>
        ))}
      </Section>
      {state.error ? (
        <Notice tone="error" testId="set-error">
          {state.error}
        </Notice>
      ) : null}
    </EditorFrame>
  );
}
