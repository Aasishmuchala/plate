// Sky node editor (T112 for the T93 node, decision 48): a reference sky placed only where the render says there is
// sky (the SKY-class Cryptomatte coverage), with its horizon on the packet camera's horizon. Tier A with Cryptomatte
// only. The photo is a node input, never an anchor and never sent to a vendor; it may be resampled, the plate never.
//
//   sky picture    the Reference wired into `sky` (it wins), else `skyHash` (a CAS hash in this project)
//   scale          (0, 8] (default 1): the photo's width as a share of the plate's
//   horizon_frac   0..1 (default 0.5): the row of the photo, from its top, that sits on the horizon
//   dx_px          pixels (default 0): slides the photo sideways
//   tone_match     default on: the photo's mean Lab inside the sky is moved to the plate's
//
// A value equal to the daemon's default is not written; `tone_match` is written only when it is off. `Preview` asks
// `sky_node.preview` for the horizon row, the camera pitch and how much of the frame is sky.
// Node params: `skyHash`, `scale`, `horizon_frac`, `dx_px`, `tone_match`.

import { type ReactElement, useMemo, useState } from "react";
import { postJson } from "../api";
import { useGraphStore } from "../store";
import { errorMessage } from "./api";
import { wiredInto } from "./CompEditor";
import { asBool, asString, useNodeEditor, usePlate } from "./params";
import { E } from "./theme";
import { Button, Chip, EditorFrame, Field, Notice, NumberField, RangeField, Section, Toggle, useAction } from "./widgets";

export const SKY_SCALE = { min: 0.05, max: 8, default: 1 } as const;
export const HORIZON_FRAC_DEFAULT = 0.5;

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const HASH = /^[0-9a-f]{64}$/;

interface SkyPreview {
  horizonY: number | null;
  pitchDeg: number | null;
  coverage: number | null;
  alphaPx: number | null;
  toneShift: number[] | null;
}

function parsePreview(body: unknown): SkyPreview {
  const o = typeof body === "object" && body !== null ? (body as Record<string, unknown>) : {};
  const shift = Array.isArray(o.tone_shift_lab) && o.tone_shift_lab.every((v) => typeof v === "number") ? (o.tone_shift_lab as number[]) : null;
  return { horizonY: num(o.horizon_y), pitchDeg: num(o.pitch_deg), coverage: num(o.coverage), alphaPx: num(o.alpha_px), toneShift: shift };
}

export function SkyEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const nodes = useGraphStore((s) => s.nodes);
  const edges = useGraphStore((s) => s.edges);
  const projectId = useGraphStore((s) => s.projectId);
  const plate = usePlate(nodeId);
  const [result, setResult] = useState<SkyPreview | null>(null);

  const reference = useMemo(() => wiredInto(nodes, edges, nodeId, "sky").find((n) => n.data.kind === "Reference") ?? null, [nodes, edges, nodeId]);
  const refHash = reference ? asString(reference.data.params?.referenceHash) || (reference.data.outputHash ?? "") : "";
  const skyHash = asString(params.skyHash);
  const scale = num(params.scale) ?? SKY_SCALE.default;
  const horizonFrac = num(params.horizon_frac) ?? HORIZON_FRAC_DEFAULT;
  const dx = num(params.dx_px) ?? 0;
  const toneMatch = asBool(params.tone_match, true);
  const width = plate?.size?.width ?? null;

  const write = (patch: Record<string, unknown>): void => {
    set(patch);
    setResult(null);
  };

  const [runPreview, state] = useAction(async () => {
    if (!projectId) throw new Error("open a project to preview");
    const body: Record<string, unknown> = {};
    for (const k of ["skyHash", "scale", "horizon_frac", "dx_px", "tone_match"]) if (params[k] !== undefined && params[k] !== null) body[k] = params[k];
    setResult(parsePreview(await postJson<unknown>(`/projects/${encodeURIComponent(projectId)}/nodes/${encodeURIComponent(nodeId)}/preview`, { what: "sky", params: body })));
  }, errorMessage);

  const hasPicture = reference !== null ? refHash !== "" : skyHash !== "";
  const hashProblem = reference === null && skyHash !== "" && !HASH.test(skyHash) ? "a CAS hash is 64 lower-case hex characters" : null;

  return (
    <EditorFrame kind="sky" nodeId={nodeId} title="Sky">
      {plate?.tier === "C" ? (
        <Notice tone="error" testId="sky-tier">
          a Tier C plate has no Cryptomatte: Sky places a sky only where a Tier A render says there is one
        </Notice>
      ) : null}

      <Section title="Sky picture" testId="sky-picture">
        {reference ? (
          <span style={E.note} data-testid="sky-reference">
            from {reference.data.label} ({reference.id}){refHash ? ` · ${refHash.slice(0, 8)}` : ": it has no image yet"}; the wired reference wins over a hash below
          </span>
        ) : (
          <span style={E.note} data-testid="sky-reference">
            no Reference is wired into the sky input: wire one, or name a picture already in this project
          </span>
        )}
        <Field label="hash">
          <input
            type="text"
            style={E.wide}
            value={skyHash}
            placeholder="CAS hash of the sky photo (PNG)"
            aria-label="sky picture hash"
            data-testid="sky-hash"
            onChange={(e) => write({ skyHash: e.target.value.trim() || undefined })}
          />
        </Field>
        {hashProblem ? (
          <Notice tone="error" testId="sky-hash-problem">
            {hashProblem}
          </Notice>
        ) : null}
      </Section>

      <Section title="Placement" testId="sky-placement">
        <RangeField
          label="scale"
          value={scale}
          min={SKY_SCALE.min}
          max={SKY_SCALE.max}
          step={0.05}
          onChange={(v) => write({ scale: v === SKY_SCALE.default ? undefined : v })}
          testId="sky-scale"
          format={(v) => `${v.toFixed(2)} ×`}
        />
        <span style={E.note}>the photo's width as a share of the plate's (default {SKY_SCALE.default})</span>
        <RangeField
          label="horizon"
          value={horizonFrac}
          min={0}
          max={1}
          step={0.01}
          onChange={(v) => write({ horizon_frac: v === HORIZON_FRAC_DEFAULT ? undefined : v })}
          testId="sky-horizon-frac"
          format={(v) => v.toFixed(2)}
        />
        <span style={E.note}>the photo row, from its top (0) to its bottom (1), that sits on the camera's horizon</span>
        <NumberField
          label="slide"
          value={dx}
          min={width ? -SKY_SCALE.max * width : undefined}
          max={width ? SKY_SCALE.max * width : undefined}
          step={1}
          suffix="px sideways (positive is right)"
          onChange={(v) => write({ dx_px: v === null || v === 0 ? undefined : Math.round(v) })}
          testId="sky-dx"
        />
        <Toggle
          label="match the photo's tone to the plate's sky"
          checked={toneMatch}
          onChange={(v) => write({ tone_match: v ? undefined : false })}
          testId="sky-tone-match"
          title="moves the photo's mean Lab inside the sky to the plate's mean there"
        />
      </Section>

      <Section title="Preview" testId="sky-preview-section">
        <div style={E.row}>
          <Button onClick={() => void runPreview()} disabled={!projectId || !hasPicture || plate?.tier === "C" || state.pending} testId="sky-preview">
            {state.pending ? "Measuring" : "Preview"}
          </Button>
          {result ? (
            <Chip tone="info" testId="sky-horizon" title="the row where the camera's level view meets the frame">
              horizon y {result.horizonY === null ? "n/a" : result.horizonY.toFixed(1)} · pitch {result.pitchDeg === null ? "n/a" : `${result.pitchDeg.toFixed(1)}°`}
            </Chip>
          ) : null}
          {result ? (
            <Chip tone={result.alphaPx ? "info" : "advisory"} testId="sky-coverage" title="share of the frame the SKY Cryptomatte covers">
              sky {result.coverage === null ? "n/a" : `${(result.coverage * 100).toFixed(1)} %`}
            </Chip>
          ) : null}
        </div>
        {result?.toneShift ? (
          <span style={E.note} data-testid="sky-tone-shift">
            tone match moved the photo by L {result.toneShift[0]?.toFixed(1)} a {result.toneShift[1]?.toFixed(1)} b {result.toneShift[2]?.toFixed(1)}
          </span>
        ) : null}
      </Section>
      {state.error ? (
        <Notice tone="error" testId="sky-error">
          {state.error}
        </Notice>
      ) : null}
    </EditorFrame>
  );
}
