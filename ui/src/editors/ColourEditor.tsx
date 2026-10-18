// Colour node editor (T112 for the T93 node, decision 48): a grade in linear light inside a scope. The daemon grades
// the picture wired into `image` (a Plate or nothing: the display master; any other node: its picked take) and keeps
// every pixel outside the scope bit for bit, which its gate holds hard.
//
//   scope       `mask` (the Mask node wired into `mask`), `ids` (legend ids picked here) or `frame`; unset is the
//               daemon's default: mask when a Mask is wired, else the whole frame
//   grade       `ev` -4..4, `kelvin` 2000..12000 (unset keeps the white), `tint` -100..100 (positive towards
//               magenta), `contrast` -50..50, `saturation` -100..100 (-100 is grey), `hue_deg` -180..180
//
// A neutral control is removed from the params rather than written as 0, so an untouched node holds no grade.
// `Preview` asks the numeric preview (`colour_node.preview`): the scope's size, the mean colour inside it before and
// after (Lab), the target white, and the locked ids the scope touches (a grade may move a locked material: that is
// the artist's explicit scope, and the report names them).
// Node params: `scope`, `ids` (string[]), `ev`, `kelvin`, `tint`, `contrast`, `saturation`, `hue_deg`.

import { type ReactElement, useMemo, useState } from "react";
import { postJson } from "../api";
import { useGraphStore } from "../store";
import { errorMessage, idPick } from "./api";
import { labText } from "./colour";
import { wiredInto } from "./CompEditor";
import { ImageStage } from "./ImageStage";
import type { Point } from "./maskMath";
import { asStringList, useNodeEditor, usePlate } from "./params";
import { E } from "./theme";
import { Button, Chip, EditorFrame, Field, Notice, RangeField, Section, Toggle, useAction } from "./widgets";

export const COLOUR_SCOPES = ["mask", "ids", "frame"] as const;
export type ColourScope = (typeof COLOUR_SCOPES)[number];

/** The grade controls: the daemon's param name, its range (`colour_node.RANGES`), the slider step and the label. */
export const GRADE_CONTROLS = [
  { key: "ev", label: "exposure", min: -4, max: 4, step: 0.05, unit: "EV" },
  { key: "tint", label: "tint", min: -100, max: 100, step: 1, unit: "" },
  { key: "contrast", label: "contrast", min: -50, max: 50, step: 1, unit: "" },
  { key: "saturation", label: "saturation", min: -100, max: 100, step: 1, unit: "" },
  { key: "hue_deg", label: "hue", min: -180, max: 180, step: 1, unit: "°" },
] as const;
export const KELVIN_RANGE = { min: 2000, max: 12000, step: 50, neutral: 6500 } as const;
/** Every grade param the editor writes, kelvin included. */
export const GRADE_KEYS = [...GRADE_CONTROLS.map((c) => c.key), "kelvin"] as const;

/** A legend id a scope may name: an object `o:`, a material `m:` or (Tier C) a segment `s:`. */
export const LEGEND_ID = /^[oms]:[0-9a-z_-]+$/i;

/** Ids typed or pasted as text (commas, spaces or new lines), lower-cased like the daemon; the bad ones apart. Pure. */
export function readIds(text: string): { ids: string[]; bad: string[] } {
  const ids: string[] = [];
  const bad: string[] = [];
  for (const raw of text.split(/[\s,;]+/)) {
    const t = raw.trim().toLowerCase();
    if (t === "") continue;
    if (!LEGEND_ID.test(t)) bad.push(raw.trim());
    else if (!ids.includes(t)) ids.push(t);
  }
  return { ids, bad };
}

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const lab3 = (v: unknown): [number, number, number] | null =>
  Array.isArray(v) && v.length === 3 && v.every((x) => typeof x === "number") ? [v[0] as number, v[1] as number, v[2] as number] : null;

interface ColourPreview {
  scope: string | null;
  px: number | null;
  coverage: number | null;
  before: [number, number, number] | null;
  after: [number, number, number] | null;
  locked: string[];
}

function parsePreview(body: unknown): ColourPreview {
  const o = typeof body === "object" && body !== null ? (body as Record<string, unknown>) : {};
  return {
    scope: typeof o.scope === "string" ? o.scope : null,
    px: num(o.px),
    coverage: num(o.coverage),
    before: lab3(o.mean_lab_before),
    after: lab3(o.mean_lab_after),
    locked: Array.isArray(o.locked_ids_in_scope) ? o.locked_ids_in_scope.filter((x): x is string => typeof x === "string") : [],
  };
}

const signed = (v: number, digits = 0): string => `${v > 0 ? "+" : ""}${v.toFixed(digits)}`;

export function ColourEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const nodes = useGraphStore((s) => s.nodes);
  const edges = useGraphStore((s) => s.edges);
  const projectId = useGraphStore((s) => s.projectId);
  const plate = usePlate(nodeId);
  const [idText, setIdText] = useState("");
  const [idProblem, setIdProblem] = useState<string | null>(null);
  const [pickKind, setPickKind] = useState<"object" | "material">("object");
  const [result, setResult] = useState<ColourPreview | null>(null);

  const mask = useMemo(() => wiredInto(nodes, edges, nodeId, "mask").find((n) => n.data.kind === "Mask") ?? null, [nodes, edges, nodeId]);
  const source = useMemo(() => wiredInto(nodes, edges, nodeId, "image")[0] ?? null, [nodes, edges, nodeId]);
  const scopeSet = typeof params.scope === "string" && (COLOUR_SCOPES as readonly string[]).includes(params.scope) ? (params.scope as ColourScope) : null;
  const scope: ColourScope = scopeSet ?? (mask ? "mask" : "frame");
  const ids = asStringList(params.ids);
  const kelvin = num(params.kelvin);
  const valueOf = (key: string): number => num(params[key]) ?? 0;
  const graded = GRADE_KEYS.some((k) => num(params[k]) !== null && (k === "kelvin" || params[k] !== 0));

  const setGrade = (key: string, v: number): void => {
    set({ [key]: v === 0 ? undefined : v });
    setResult(null);
  };
  const setIds = (next: string[]): void => {
    set({ ids: next.length > 0 ? next : undefined });
    setResult(null);
  };
  const addIds = (): void => {
    const r = readIds(idText);
    if (r.bad.length > 0) {
      setIdProblem(`not a legend id: ${r.bad.join(", ")} (o:…, m:… or s:…)`);
      return;
    }
    setIdProblem(null);
    setIds([...ids, ...r.ids.filter((id) => !ids.includes(id))]);
    setIdText("");
  };

  const [pick, pickState] = useAction(async (p: Point) => {
    if (!plate?.packetId) return;
    const r = await idPick({ packet_id: plate.packetId, x: p.x, y: p.y, kind: pickKind });
    if (r.id === null) {
      setIdProblem(`no ${pickKind} id at (${p.x}, ${p.y})`);
      return;
    }
    setIdProblem(null);
    const id = r.id.toLowerCase();
    if (!ids.includes(id)) setIds([...ids, id]);
  }, errorMessage);

  const [runPreview, previewState] = useAction(async () => {
    if (!projectId) throw new Error("open a project to preview");
    const body: Record<string, unknown> = {};
    if (scopeSet) body.scope = scopeSet;
    if (ids.length > 0) body.ids = ids;
    for (const k of GRADE_KEYS) if (num(params[k]) !== null) body[k] = params[k];
    setResult(parsePreview(await postJson<unknown>(`/projects/${encodeURIComponent(projectId)}/nodes/${encodeURIComponent(nodeId)}/preview`, { what: "colour", params: body })));
  }, errorMessage);

  const scopeProblem = scope === "mask" && !mask ? "scope mask needs a Mask node wired into the mask input" : scope === "ids" && ids.length === 0 ? "scope ids needs at least one legend id" : null;

  return (
    <EditorFrame kind="colour" nodeId={nodeId} title="Colour">
      <Section title="Source" testId="colour-source">
        <span style={E.note} data-testid="colour-source-node">
          {source === null || source.data.kind === "Plate" ? "grades the plate (the packet's display master)" : `grades ${source.data.label} (${source.id}): its picked take`}
        </span>
      </Section>

      <Section title="Scope" testId="colour-scope-section">
        <Field label="scope">
          <select style={E.wide} value={scopeSet ?? ""} data-testid="colour-scope" aria-label="scope" onChange={(e) => set({ scope: e.target.value === "" ? undefined : e.target.value })}>
            <option value="">auto ({mask ? "the wired mask" : "the whole frame: no Mask is wired"})</option>
            <option value="mask">mask: the Mask node wired in</option>
            <option value="ids">ids: legend ids picked here</option>
            <option value="frame">frame: every pixel</option>
          </select>
        </Field>
        {scope === "mask" ? (
          <span style={E.note} data-testid="colour-scope-mask">
            {mask ? `inside ${mask.data.label} (${mask.id})` : "no Mask node is wired into the mask input"}
          </span>
        ) : null}
        {scope === "ids" ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }} data-testid="colour-ids">
            <ImageStage hash={plate?.hash ?? null} size={plate?.size ?? null} testId="colour-stage" alt="plate" onPointerDown={plate?.size && plate.packetId ? (p) => void pick(p) : undefined} />
            <div style={E.row}>
              <span style={E.label}>pick</span>
              <Button on={pickKind === "object"} onClick={() => setPickKind("object")} testId="colour-pick-object">
                object
              </Button>
              <Button on={pickKind === "material"} onClick={() => setPickKind("material")} testId="colour-pick-material">
                material
              </Button>
              <span style={E.note}>click the plate to add the id under the pointer</span>
            </div>
            <div style={E.row}>
              <input
                type="text"
                style={E.wide}
                value={idText}
                placeholder="o:1a2b3c4d, m:… (comma or space separated)"
                aria-label="legend ids"
                data-testid="colour-ids-input"
                onChange={(e) => setIdText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    addIds();
                  }
                }}
              />
              <Button onClick={addIds} disabled={idText.trim() === ""} testId="colour-ids-add">
                Add
              </Button>
            </div>
            <div style={E.row} data-testid="colour-id-list">
              {ids.length === 0 ? <span style={E.note}>no ids yet</span> : null}
              {ids.map((id) => (
                <span key={id} style={E.row}>
                  <Chip tone="info">{id}</Chip>
                  <Button onClick={() => setIds(ids.filter((x) => x !== id))} testId={`colour-id-remove-${id}`} title={`take ${id} out of the scope`}>
                    ×
                  </Button>
                </span>
              ))}
            </div>
          </div>
        ) : null}
        {scopeProblem ? (
          <Notice tone="error" testId="colour-scope-problem">
            {scopeProblem}
          </Notice>
        ) : null}
        <span style={E.note}>outside the scope every pixel stays as it was (no feather); the gate holds that hard</span>
      </Section>

      <Section title="Grade (linear light)" testId="colour-grade">
        {GRADE_CONTROLS.map((c) => (
          <RangeField
            key={c.key}
            label={c.label}
            value={valueOf(c.key)}
            min={c.min}
            max={c.max}
            step={c.step}
            onChange={(v) => setGrade(c.key, v)}
            testId={`colour-${c.key}`}
            format={(v) => `${signed(v, c.key === "ev" ? 2 : 0)}${c.unit ? ` ${c.unit}` : ""}`}
          />
        ))}
        <Toggle
          label="change the white balance"
          checked={kelvin !== null}
          onChange={(v) => {
            set({ kelvin: v ? KELVIN_RANGE.neutral : undefined });
            setResult(null);
          }}
          testId="colour-kelvin-on"
          title="off keeps the picture's white; on adapts it to the white of a colour temperature (6500 K changes nothing)"
        />
        {kelvin !== null ? (
          <RangeField
            label="white"
            value={kelvin}
            min={KELVIN_RANGE.min}
            max={KELVIN_RANGE.max}
            step={KELVIN_RANGE.step}
            onChange={(v) => {
              set({ kelvin: v });
              setResult(null);
            }}
            testId="colour-kelvin"
            format={(v) => `${Math.round(v)} K`}
          />
        ) : null}
        <div style={E.row}>
          <Button
            onClick={() => {
              set(Object.fromEntries(GRADE_KEYS.map((k) => [k, undefined])));
              setResult(null);
            }}
            disabled={!graded}
            testId="colour-reset"
          >
            Reset the grade
          </Button>
          <span style={E.note}>tint: positive is towards magenta; saturation -100 is grey</span>
        </div>
      </Section>

      <Section title="Preview" testId="colour-preview-section">
        <div style={E.row}>
          <Button onClick={() => void runPreview()} disabled={!projectId || scopeProblem !== null || previewState.pending} testId="colour-preview">
            {previewState.pending ? "Measuring" : "Preview"}
          </Button>
          {result ? (
            <Chip tone="info" testId="colour-coverage" title="pixels inside the scope">
              {result.scope ?? scope}: {result.px ?? "n/a"} px ({result.coverage === null ? "n/a" : `${(result.coverage * 100).toFixed(1)} %`})
            </Chip>
          ) : null}
          {result && result.locked.length > 0 ? (
            <Chip tone="advisory" testId="colour-locked" title="locked ids inside the scope: the grade moves them on the artist's say-so">
              grades locked: {result.locked.join(", ")}
            </Chip>
          ) : null}
        </div>
        {result?.before && result.after ? (
          <span style={E.note} data-testid="colour-means">
            mean inside: {labText(result.before)} → {labText(result.after)}
          </span>
        ) : null}
      </Section>
      {(pickState.error ?? previewState.error ?? idProblem) ? (
        <Notice tone="error" testId="colour-error">
          {pickState.error ?? previewState.error ?? idProblem}
        </Notice>
      ) : null}
    </EditorFrame>
  );
}
