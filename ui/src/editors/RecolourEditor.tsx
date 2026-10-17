// Recolour node editor (T47): pick the material on the plate, type a RAL code or a hex colour, and see the expected
// ΔE00 of the recomposition before anything runs. Recolour is element recomposition (math on the render's own passes),
// not an AI edit, so it needs no editable id. Node params: `material` (m: id), `materialName`, `target` (`RAL 7016` or
// `#383E42`), `targetHex`.
//
// Expected ΔE comes from the daemon's numeric preview (`recolour.apply`): <= 1 for recomposition, about 2.5 for the
// ratio fallback, wider when the passes are missing (`degraded`), which is expected to fail on dark semi-gloss panels.

import { type ReactElement, useEffect, useState } from "react";
import { useGraphStore } from "../store";
import { errorMessage, idPick, type RecolourExpect, recolourPreview } from "./api";
import { parseTarget, RAL_SRGB } from "./colour";
import { ImageStage } from "./ImageStage";
import type { Point } from "./maskMath";
import { asString, useNodeEditor, usePlate } from "./params";
import { E } from "./theme";
import { Chip, EditorFrame, Field, Notice, Section, useAction } from "./widgets";

export function RecolourEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const plate = usePlate(nodeId);
  const projectId = useGraphStore((s) => s.projectId);
  const [text, setText] = useState(asString(params.target));
  const [expect, setExpect] = useState<RecolourExpect | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const size = plate?.size ?? null;
  const packetId = plate?.packetId ?? null;
  const material = asString(params.material);
  const parsed = text.trim() === "" ? null : parseTarget(text);
  const target = parsed && parsed.ok ? parsed.target : null;

  const [pick, pickState] = useAction(async (p: Point) => {
    if (!packetId) return;
    const r = await idPick({ packet_id: packetId, x: p.x, y: p.y, kind: "material" });
    if (r.id === null) {
      setNote(`no material id at (${p.x}, ${p.y})`);
      return;
    }
    set({ material: r.id, materialName: r.name });
    setNote(null);
  }, errorMessage);

  const [estimate, estimateState] = useAction(async (m: string, label: string) => {
    if (!projectId) return;
    const t = parseTarget(label);
    if (!t.ok) return;
    setExpect(await recolourPreview(projectId, nodeId, { material_id: m, ...(t.target.kind === "ral" ? { ral: t.target.code } : { hex: t.target.hex }) }));
  }, errorMessage);

  // a material and a valid target: ask the daemon what to expect
  const label = target?.label ?? "";
  useEffect(() => {
    setExpect(null);
    if (projectId && material && label) void estimate(material, label);
    // the preview follows the material and the target, nothing else
  }, [projectId, nodeId, material, label]);

  const onText = (v: string): void => {
    setText(v);
    const r = v.trim() === "" ? null : parseTarget(v);
    if (r && r.ok) set({ target: r.target.label, targetHex: r.target.hex });
    else set({ target: undefined, targetHex: undefined });
  };

  const de = expect?.expectedDe00 ?? null;
  const deTone = de === null ? "muted" : de <= 1 ? "pass" : de <= 2.5 ? "advisory" : "fail";

  return (
    <EditorFrame kind="recolour" nodeId={nodeId} title="Recolour">
      <ImageStage hash={plate?.hash ?? null} size={size} testId="recolour-stage" alt="plate" onPointerDown={size && packetId ? (p) => void pick(p) : undefined} />
      <span style={E.note} data-testid="recolour-material">
        {material ? `material ${asString(params.materialName, material)} (${material})` : "click the plate on the material to recolour"}
      </span>

      <Section title="Target" testId="recolour-target-section">
        <Field label="colour">
          <input
            type="text"
            style={E.wide}
            value={text}
            placeholder="RAL 7016 or #383E42"
            list="recolour-ral-list"
            data-testid="recolour-target"
            onChange={(e) => onText(e.target.value)}
          />
          <datalist id="recolour-ral-list">
            {Object.keys(RAL_SRGB).map((code) => (
              <option key={code} value={`RAL ${code}`} />
            ))}
          </datalist>
        </Field>
        {target ? (
          <div style={E.row}>
            <span
              aria-hidden
              data-testid="recolour-swatch"
              style={{ display: "inline-block", width: 28, height: 16, borderRadius: 3, border: "1px solid #48484a", background: target.hex }}
            />
            <span style={E.note} data-testid="recolour-hex">
              {target.label} · {target.hex}
            </span>
          </div>
        ) : parsed && !parsed.ok ? (
          <Notice tone="error" testId="recolour-target-error">
            {parsed.reason}
          </Notice>
        ) : null}
      </Section>

      <Section title="Expected result" testId="recolour-expect">
        {expect ? (
          <div style={E.row}>
            <Chip tone={deTone} testId="recolour-de" title="expected ΔE00 of the recomposite against a re-render">
              expected ΔE00 {de === null ? "n/a" : de.toFixed(2)}
            </Chip>
            {expect.method ? (
              <Chip tone={expect.degraded ? "fail" : "info"} testId="recolour-method">
                {expect.method}
              </Chip>
            ) : null}
            {expect.metal ? (
              <Chip tone="advisory" testId="recolour-metal" title="specular energy over 0.3 of the beauty inside the mask">
                metal: reflections stay untinted
              </Chip>
            ) : null}
          </div>
        ) : (
          <span style={E.note} data-testid="recolour-noexpect">
            {estimateState.pending ? "estimating" : material && target ? "no estimate yet" : "pick a material and a colour"}
          </span>
        )}
        {expect?.degraded ? <span style={E.note}>the render has no light passes for this material: whole-beauty scaling, which fails on dark semi-gloss panels</span> : null}
      </Section>

      {note ? <Notice testId="recolour-note">{note}</Notice> : null}
      {(pickState.error ?? estimateState.error) ? (
        <Notice tone="error" testId="recolour-error">
          {pickState.error ?? estimateState.error}
        </Notice>
      ) : null}
    </EditorFrame>
  );
}
