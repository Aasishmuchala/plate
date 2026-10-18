// LightMix node editor (T47): one gain slider per light select (0 to the node's max gain, 8 by default), seeded by
// non-negative least squares against a target image, plus mood presets as starting guesses. Light is additive, so a
// mood is a weighted sum of the light selects and a gain can never go negative. Numeric only: no generator is involved.
// Node params: `lights` (names, cached), `gains` {light: gain}, `preset`, `targetHash` (CAS hash of the target image).
//
// T112 adds the rest of what `lightmix_node.prepare` reads: a light's gain may be an `[r, g, b]` vector (the `RGB`
// toggle on its row: a cooler moon or a warmer lamp without a second light; NNLS still seeds one number per light),
// every preset the daemon ships (`MOOD_PRESETS`, pinned to `plate_gates.lightmix.mood_presets`), `max_gain` (where the
// daemon clips a gain, and the sliders' end), `keep_base` (add the part of the beauty the selects do not carry) and
// `seed` (the NNLS pixel sample).

import { type ReactElement, useEffect, useState } from "react";
import { useGraphStore } from "../store";
import { errorMessage, lightmixPreview, parseLightMixSeed, preview } from "./api";
import { type Gain, MOOD_PRESETS, presetGains, presetLabel, readGain } from "./colour";
import { asBool, asNumber, asString, asStringList, useNodeEditor, useStill } from "./params";
import { E } from "./theme";
import { Button, Chip, EditorFrame, Field, Notice, NumberField, RangeField, Section, Toggle, useAction } from "./widgets";

/** The daemon's default `max_gain` (lightmix_node.DEFAULT_MAX_GAIN). */
export const MAX_GAIN = 8;
/** The largest `max_gain` the editor offers; the daemon takes any positive number. */
export const MAX_GAIN_LIMIT = 64;
const CHANNELS = ["r", "g", "b"] as const;

function readGains(v: unknown): Record<string, Gain> {
  const out: Record<string, Gain> = {};
  if (typeof v === "object" && v !== null && !Array.isArray(v)) {
    for (const [k, g] of Object.entries(v)) {
      const gain = readGain(g);
      if (gain !== null) out[k] = gain;
    }
  }
  return out;
}

/** One number for a vector gain (the RGB toggle going back): the mean of its channels, to the slider's 0.01. */
const mean3 = (g: readonly [number, number, number]): number => Math.round(((g[0] + g[1] + g[2]) / 3) * 100) / 100;

export function LightMixEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const projectId = useGraphStore((s) => s.projectId);
  const still = useStill(nodeId);
  const lights = asStringList(params.lights);
  const gains = readGains(params.gains);
  const targetHash = asString(params.targetHash);
  const maxGainSet = typeof params.max_gain === "number" && Number.isFinite(params.max_gain) && params.max_gain > 0;
  const maxGain = maxGainSet ? (params.max_gain as number) : MAX_GAIN;
  const keepBase = asBool(params.keep_base);
  const seedSet = typeof params.seed === "number" && Number.isInteger(params.seed);
  const [residual, setResidual] = useState<number | null>(null);
  const [clipped, setClipped] = useState<string[]>([]);

  const gainOf = (name: string): Gain => gains[name] ?? 1;
  const setGains = (next: Record<string, Gain>): void => set({ gains: next });
  const setGain = (name: string, g: Gain): void => setGains({ ...gains, [name]: g });
  // what the daemon would clip: any channel over the maximum
  const overMax = lights.filter((n) => {
    const g = gainOf(n);
    return typeof g === "number" ? g > maxGain : g.some((c) => c > maxGain);
  });

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
    // the solve also reads the node's max gain, base and sample seed: send the ones the artist set
    const body: Record<string, unknown> = { target_hash: targetHash };
    if (maxGainSet) body.max_gain = maxGain;
    if (keepBase) body.keep_base = true;
    if (seedSet) body.seed = params.seed;
    const seed = parseLightMixSeed(await preview(projectId, nodeId, "lightmix", body));
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
  const fmt = (v: number): string => v.toFixed(2);

  return (
    <EditorFrame kind="lightmix" nodeId={nodeId} title="Light mix">
      <Section title="Gains" testId="lightmix-gains">
        {lights.length === 0 ? (
          <span style={E.note} data-testid="lightmix-nolights">
            {loadState.pending ? "reading the light selects" : "no light selects known yet: the packet needs x_light_* elements"}
          </span>
        ) : (
          lights.map((name) => {
            const g = gainOf(name);
            const rgb = typeof g !== "number";
            return (
              <div key={name} style={{ display: "flex", flexDirection: "column", gap: 2 }} data-testid={`lightmix-light-${name}`} data-channels={rgb ? "rgb" : "scalar"}>
                <div style={E.row}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    {rgb ? (
                      <span style={E.label}>{name}</span>
                    ) : (
                      <RangeField label={name} value={g} min={0} max={maxGain} step={0.05} onChange={(v) => setGain(name, v)} testId={`lightmix-gain-${name}`} format={fmt} />
                    )}
                  </div>
                  <Button
                    on={rgb}
                    onClick={() => setGain(name, rgb ? mean3(g) : [g, g, g])}
                    testId={`lightmix-rgb-${name}`}
                    title={rgb ? "one gain for the whole light (the mean of its three)" : "a gain per colour channel: warms or cools this light without a second one"}
                  >
                    RGB
                  </Button>
                </div>
                {rgb
                  ? CHANNELS.map((ch, i) => (
                      <RangeField
                        key={ch}
                        label={ch}
                        value={g[i] ?? 1}
                        min={0}
                        max={maxGain}
                        step={0.05}
                        onChange={(v) => setGain(name, [i === 0 ? v : g[0], i === 1 ? v : g[1], i === 2 ? v : g[2]])}
                        testId={`lightmix-gain-${name}-${ch}`}
                        format={fmt}
                      />
                    ))
                  : null}
              </div>
            );
          })
        )}
        {clipped.length > 0 ? (
          <Chip tone="advisory" testId="lightmix-clipped" title={`clipped to the maximum gain of ${maxGain}`}>
            clipped: {clipped.join(", ")}
          </Chip>
        ) : null}
        {overMax.length > 0 ? (
          <Chip tone="advisory" testId="lightmix-over-max" title="the daemon clips each channel to the maximum gain">
            over max gain {maxGain}: {overMax.join(", ")}
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
              {presetLabel(name)}
            </Button>
          ))}
        </div>
        <span style={E.note}>a preset sets the sliders by light name (sun, sky, interior_*, facade_*, landscape_*); a light it does not name stays at 1.0</span>
      </Section>

      <Section title="Mix" testId="lightmix-mix">
        <NumberField
          label="max gain"
          value={maxGain}
          min={0.05}
          max={MAX_GAIN_LIMIT}
          step={0.5}
          suffix={`each channel is clipped here (default ${MAX_GAIN})`}
          onChange={(v) => set({ max_gain: v === null ? undefined : v })}
          testId="lightmix-max-gain"
        />
        <Toggle
          label="keep the base (what the light selects do not carry: emissives, environment reflections)"
          checked={keepBase}
          onChange={(v) => set({ keep_base: v ? true : undefined })}
          testId="lightmix-keep-base"
        />
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
        <NumberField
          label="sample seed"
          value={seedSet ? asNumber(params.seed, 0) : null}
          min={0}
          step={1}
          suffix="the pixel sample the solver fits (default 0)"
          onChange={(v) => set({ seed: v === null ? undefined : Math.round(v) })}
          testId="lightmix-seed-value"
        />
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
        <span style={E.note}>the solver fits one number per light; switch a light to RGB afterwards to tint it</span>
      </Section>
      {error ? (
        <Notice tone="error" testId="lightmix-error">
          {error}
        </Notice>
      ) : null}
    </EditorFrame>
  );
}
