// Atmosphere node editor (T112 for the T93 node, decision 48): Koschmieder depth fog from the packet's own Z. Tier A
// only (a Tier C plate has no Z: the daemon refuses it with `no_z`). The fog fraction of a pixel at depth Z is
// 1 - exp(-3.912 Z / visibility): at the visibility distance contrast has fallen to 2 %.
//
//   visibility_m       20..20000 m (default 800): how far the eye sees; lower is thicker fog
//   colour             the fog's colour as linear [r, g, b] in 0..1; unset takes the mean of the lowest tenth of the
//                      sky (nearest the horizon), else a neutral [0.75, 0.78, 0.82]
//   sky_fog            0..1 (default 0.6): how much fog the sky (and any pixel without usable depth) gets
//   height_falloff_m   metres, unset is off: fog thins above the camera (needs a world_position element; without
//                      one the fog stays uniform and the report says so)
//
// A value equal to the daemon's default is not written. `Preview` asks `atmosphere_node.preview` for the fog fraction
// (median and p90), where the colour came from and the sky pixels.
// Node params: `visibility_m`, `colour`, `sky_fog`, `height_falloff_m`.

import { type ReactElement, useState } from "react";
import { postJson } from "../api";
import { useGraphStore } from "../store";
import { errorMessage } from "./api";
import { hexToLinear, linearToHex } from "./colour";
import { useNodeEditor, usePlate } from "./params";
import { C, E } from "./theme";
import { Button, Chip, EditorFrame, Field, Notice, NumberField, RangeField, Section, Toggle, useAction } from "./widgets";

export const VISIBILITY = { min: 20, max: 20000, default: 800 } as const;
export const SKY_FOG_DEFAULT = 0.6;
export const FALLOFF_DEFAULT_M = 30;
/** The daemon's fog colour when the packet has no sky to take one from (linear). */
export const NEUTRAL_FOG: readonly [number, number, number] = [0.75, 0.78, 0.82];

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const rgb3 = (v: unknown): [number, number, number] | null =>
  Array.isArray(v) && v.length === 3 && v.every((c) => typeof c === "number" && Number.isFinite(c) && c >= 0 && c <= 1) ? [v[0] as number, v[1] as number, v[2] as number] : null;
const round4 = (v: number): number => Math.round(v * 10000) / 10000;

/** The visibility slider runs on log10 metres: 20 m to 20 km in even steps of thickness. */
const toLog = (m: number): number => Math.log10(m);
const fromLog = (l: number): number => {
  const m = 10 ** l;
  return Math.min(VISIBILITY.max, Math.max(VISIBILITY.min, m >= 1000 ? Math.round(m / 50) * 50 : Math.round(m)));
};

interface FogPreview {
  median: number | null;
  p90: number | null;
  colour: [number, number, number] | null;
  colourSource: string | null;
  skyPx: number | null;
  notes: string[];
}

function parsePreview(body: unknown): FogPreview {
  const o = typeof body === "object" && body !== null ? (body as Record<string, unknown>) : {};
  const f = typeof o.f === "object" && o.f !== null ? (o.f as Record<string, unknown>) : {};
  return {
    median: num(f.median),
    p90: num(o.f_p90),
    colour: rgb3(o.colour),
    colourSource: typeof o.colour_source === "string" ? o.colour_source : null,
    skyPx: num(o.sky_px),
    notes: Array.isArray(o.notes) ? o.notes.filter((n): n is string => typeof n === "string") : [],
  };
}

export function AtmosphereEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const projectId = useGraphStore((s) => s.projectId);
  const plate = usePlate(nodeId);
  const [result, setResult] = useState<FogPreview | null>(null);

  const visibility = num(params.visibility_m) ?? VISIBILITY.default;
  const colour = rgb3(params.colour);
  const skyFog = num(params.sky_fog) ?? SKY_FOG_DEFAULT;
  const falloff = num(params.height_falloff_m);

  const write = (patch: Record<string, unknown>): void => {
    set(patch);
    setResult(null);
  };
  const setVisibility = (m: number | null): void => write({ visibility_m: m === null || m === VISIBILITY.default ? undefined : m });

  const [runPreview, state] = useAction(async () => {
    if (!projectId) throw new Error("open a project to preview");
    const body: Record<string, unknown> = {};
    for (const k of ["visibility_m", "colour", "sky_fog", "height_falloff_m"]) if (params[k] !== undefined && params[k] !== null) body[k] = params[k];
    setResult(parsePreview(await postJson<unknown>(`/projects/${encodeURIComponent(projectId)}/nodes/${encodeURIComponent(nodeId)}/preview`, { what: "atmosphere", params: body })));
  }, errorMessage);

  return (
    <EditorFrame kind="atmosphere" nodeId={nodeId} title="Atmosphere">
      {plate?.tier === "C" ? (
        <Notice tone="error" testId="atmosphere-tier">
          a Tier C plate carries no Z: Atmosphere needs a Tier A packet's depth
        </Notice>
      ) : null}

      <Section title="Visibility" testId="atmosphere-visibility-section">
        <RangeField
          label="visibility"
          value={toLog(visibility)}
          min={toLog(VISIBILITY.min)}
          max={toLog(VISIBILITY.max)}
          step={0.01}
          onChange={(l) => setVisibility(fromLog(l))}
          testId="atmosphere-visibility-slider"
          format={() => (visibility >= 1000 ? `${(visibility / 1000).toFixed(visibility % 1000 === 0 ? 0 : 2)} km` : `${visibility} m`)}
        />
        <NumberField
          label="metres"
          value={visibility}
          min={VISIBILITY.min}
          max={VISIBILITY.max}
          step={10}
          suffix={`${VISIBILITY.min} to ${VISIBILITY.max} (default ${VISIBILITY.default}); lower is thicker`}
          onChange={setVisibility}
          testId="atmosphere-visibility"
        />
      </Section>

      <Section title="Fog colour" testId="atmosphere-colour-section">
        <Toggle
          label="take the colour from the sky near the horizon"
          checked={colour === null}
          onChange={(auto) => write({ colour: auto ? undefined : [...NEUTRAL_FOG] })}
          testId="atmosphere-colour-auto"
          title="the mean linear colour of the lowest tenth of the sky's pixels; a neutral grey when the packet has no sky"
        />
        {colour ? (
          <div style={E.row}>
            <Field label="colour">
              <input
                type="color"
                value={linearToHex(colour).toLowerCase()}
                aria-label="fog colour"
                data-testid="atmosphere-colour"
                style={{ width: 44, height: 22, padding: 0, border: `1px solid ${C.line}`, background: C.bg }}
                onChange={(e) => write({ colour: hexToLinear(e.target.value).map(round4) })}
              />
            </Field>
            <span style={E.note} data-testid="atmosphere-colour-linear">
              linear {colour.map((c) => c.toFixed(3)).join(" ")}
            </span>
          </div>
        ) : null}
      </Section>

      <Section title="Sky and height" testId="atmosphere-sky-section">
        <RangeField
          label="sky fog"
          value={skyFog}
          min={0}
          max={1}
          step={0.01}
          onChange={(v) => write({ sky_fog: v === SKY_FOG_DEFAULT ? undefined : v })}
          testId="atmosphere-sky-fog"
          format={(v) => v.toFixed(2)}
        />
        <span style={E.note}>how much fog the sky, and any pixel without usable depth, takes (default {SKY_FOG_DEFAULT})</span>
        <Toggle
          label="thin the fog with height"
          checked={falloff !== null}
          onChange={(v) => write({ height_falloff_m: v ? FALLOFF_DEFAULT_M : undefined })}
          testId="atmosphere-falloff-on"
          title="needs the packet's world_position element; without it the fog stays uniform"
        />
        {falloff !== null ? (
          <NumberField
            label="falloff"
            value={falloff}
            min={0.1}
            max={100000}
            step={1}
            suffix="m above the camera for the fog to thin by e"
            onChange={(v) => v !== null && v > 0 && write({ height_falloff_m: v })}
            testId="atmosphere-falloff"
          />
        ) : null}
      </Section>

      <Section title="Preview" testId="atmosphere-preview-section">
        <div style={E.row}>
          <Button onClick={() => void runPreview()} disabled={!projectId || plate?.tier === "C" || state.pending} testId="atmosphere-preview">
            {state.pending ? "Measuring" : "Preview"}
          </Button>
          {result ? (
            <Chip tone="info" testId="atmosphere-fog" title="fog fraction per pixel: 0 is clear, 1 is the fog colour">
              fog median {result.median === null ? "n/a" : result.median.toFixed(3)} · p90 {result.p90 === null ? "n/a" : result.p90.toFixed(3)}
            </Chip>
          ) : null}
          {result?.colour ? (
            <Chip tone="muted" testId="atmosphere-colour-source" title="where the fog colour came from">
              <span aria-hidden style={{ display: "inline-block", width: 10, height: 10, borderRadius: 2, background: linearToHex(result.colour) }} />
              {result.colourSource === "params" ? "colour set here" : result.colourSource === "sky_band" ? "colour from the sky" : "neutral colour (no sky)"}
            </Chip>
          ) : null}
        </div>
        {result?.notes.map((n) => (
          <span key={n} style={E.note}>
            {n}
          </span>
        ))}
      </Section>
      {state.error ? (
        <Notice tone="error" testId="atmosphere-error">
          {state.error}
        </Notice>
      ) : null}
    </EditorFrame>
  );
}
