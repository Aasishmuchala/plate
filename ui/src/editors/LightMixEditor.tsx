// LightMix node editor (T47): one gain slider per light select (0 to 8, the solver's max_gain), seeded by non-negative
// least squares against a target image, plus mood presets as starting guesses. Light is additive, so a mood is a
// weighted sum of the light selects and a gain can never go negative. Numeric only: no generator is involved.
// Node params: `lights` (names, cached), `gains` {light: gain}, `preset`, `targetHash` (CAS hash of the target image).

import { type ReactElement, useEffect, useState } from "react";
import { useGraphStore } from "../store";
import { errorMessage, lightmixPreview } from "./api";
import { MOOD_PRESETS, presetGains } from "./colour";
import { asString, asStringList, useNodeEditor, useStill } from "./params";
import { E } from "./theme";
import { Button, Chip, EditorFrame, Field, Notice, RangeField, Section, useAction } from "./widgets";

export const MAX_GAIN = 8;

function readGains(v: unknown): Record<string, number> {
  const out: Record<string, number> = {};
  if (typeof v === "object" && v !== null && !Array.isArray(v)) {
    for (const [k, g] of Object.entries(v)) if (typeof g === "number" && Number.isFinite(g)) out[k] = g;
  }
  return out;
}

export function LightMixEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const projectId = useGraphStore((s) => s.projectId);
  const still = useStill(nodeId);
  const lights = asStringList(params.lights);
  const gains = readGains(params.gains);
  const targetHash = asString(params.targetHash);
  const [residual, setResidual] = useState<number | null>(null);
  const [clipped, setClipped] = useState<string[]>([]);

  const gainOf = (name: string): number => gains[name] ?? 1;
  const setGains = (next: Record<string, number>): void => set({ gains: next });

  // learn the light selects of the packet once
  const [loadLights, loadState] = useAction(async () => {
    if (!projectId) return;
    const seed = await lightmixPreview(projectId, nodeId, {});
    if (seed.lights.length > 0) set({ lights: seed.lights, gains: { ...seed.gains, ...gains } });
  }, errorMessage);
  useEffect(() => {
    if (lights.length === 0 && projectId) void loadLights();
    // once per node and project: the list is cached in the params afterwards
  }, [projectId, nodeId]);

  const [seedNnls, seedState] = useAction(async () => {
    if (!projectId) return;
    const seed = await lightmixPreview(projectId, nodeId, { target_hash: targetHash });
    if (seed.lights.length > 0 && lights.length === 0) set({ lights: seed.lights });
    setGains({ ...gains, ...seed.gains });
    setResidual(seed.residualRms);
    setClipped(seed.clipped);
  }, errorMessage);

  const applyPreset = (name: string): void => {
    const preset = MOOD_PRESETS[name];
    if (!preset) return;
    setGains(presetGains(preset, lights));
    set({ preset: name });
    setResidual(null);
    setClipped([]);
  };

  const error = loadState.error ?? seedState.error;

  return (
    <EditorFrame kind="lightmix" nodeId={nodeId} title="Light mix">
      <Section title="Gains" testId="lightmix-gains">
        {lights.length === 0 ? (
          <span style={E.note} data-testid="lightmix-nolights">
            {loadState.pending ? "reading the light selects" : "no light selects known yet: the packet needs x_light_* elements"}
          </span>
        ) : (
          lights.map((name) => (
            <RangeField
              key={name}
              label={name}
              value={gainOf(name)}
              min={0}
              max={MAX_GAIN}
              step={0.05}
              onChange={(v) => setGains({ ...gains, [name]: v })}
              testId={`lightmix-gain-${name}`}
              format={(v) => v.toFixed(2)}
            />
          ))
        )}
        {clipped.length > 0 ? (
          <Chip tone="advisory" testId="lightmix-clipped" title={`clipped to the maximum gain of ${MAX_GAIN}`}>
            clipped: {clipped.join(", ")}
          </Chip>
        ) : null}
        <div style={E.row}>
          <Button onClick={() => setGains(Object.fromEntries(lights.map((n) => [n, 1])))} disabled={lights.length === 0} testId="lightmix-reset">
            Reset to 1.0
          </Button>
        </div>
      </Section>

      <Section title="Mood presets (starting guesses)" testId="lightmix-presets">
        <div style={E.row}>
          {Object.keys(MOOD_PRESETS).map((name) => (
            <Button key={name} on={asString(params.preset) === name} onClick={() => applyPreset(name)} disabled={lights.length === 0} testId={`lightmix-preset-${name}`}>
              {name}
            </Button>
          ))}
        </div>
      </Section>

      <Section title="Seed from a target (NNLS)" testId="lightmix-seed">
        <Field label="target">
          <input
            type="text"
            style={E.wide}
            value={targetHash}
            placeholder="CAS hash of the target image"
            data-testid="lightmix-target"
            onChange={(e) => set({ targetHash: e.target.value.trim() || undefined })}
          />
        </Field>
        <div style={E.row}>
          <Button onClick={() => still?.hash && set({ targetHash: still.hash })} disabled={!still?.hash} testId="lightmix-use-upstream" title="use the nearest upstream image as the target">
            Use upstream image
          </Button>
          <Button onClick={() => void seedNnls()} disabled={!projectId || targetHash === "" || seedState.pending} testId="lightmix-seed-run">
            {seedState.pending ? "Solving" : "Seed gains"}
          </Button>
          {residual !== null ? (
            <Chip tone="info" testId="lightmix-residual" title="RMS of the recomposite against the target, linear light">
              residual {residual.toFixed(4)}
            </Chip>
          ) : null}
        </div>
      </Section>
      {error ? (
        <Notice tone="error" testId="lightmix-error">
          {error}
        </Notice>
      ) : null}
    </EditorFrame>
  );
}
