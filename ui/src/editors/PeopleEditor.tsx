// People node editor (T47): who stands in the plate, doing what, where and under which light. The ground-point picker
// puts the person's feet on the plate; the packet's Z at that pixel gives the person's depth in metres (the occlusion
// plane: anything the packet puts nearer than that hides the person), which stays editable. Node params: `cast`,
// `behavior`, `placement` {x, y} in plate pixels, `light`, `personDepthM`, `depthSource` ("z" | "manual"), `mode`.
// The run goes through STAFFAGE (custody D unless it runs local-only), so an NDA project refuses it.

import { type ReactElement, useState } from "react";
import { errorMessage, idPick } from "./api";
import { ImageStage } from "./ImageStage";
import { type Point, pointPercent } from "./maskMath";
import { asString, useNodeEditor, usePlate } from "./params";
import { C, E } from "./theme";
import { Chip, EditorFrame, Field, Notice, NumberField, Section, useAction } from "./widgets";

export const PEOPLE_MODES = [
  { id: "i2v", label: "i2v: image to video" },
  { id: "retake", label: "retake" },
  { id: "wan-edit", label: "wan-edit" },
] as const;

const readPlacement = (v: unknown): Point | null =>
  typeof v === "object" && v !== null && typeof (v as Point).x === "number" && typeof (v as Point).y === "number" ? { x: (v as Point).x, y: (v as Point).y } : null;

export function PeopleEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const plate = usePlate(nodeId);
  const [note, setNote] = useState<string | null>(null);
  const size = plate?.size ?? null;
  const packetId = plate?.packetId ?? null;
  const placement = readPlacement(params.placement);
  const depth = typeof params.personDepthM === "number" ? params.personDepthM : null;
  const source = asString(params.depthSource) === "z" ? "z" : "manual";
  const mode = asString(params.mode, "i2v");

  const [place, placeState] = useAction(async (p: Point) => {
    set({ placement: { x: p.x, y: p.y } });
    if (!packetId) return;
    const r = await idPick({ packet_id: packetId, x: p.x, y: p.y });
    if (r.depthM !== null && r.depthM > 0) {
      set({ personDepthM: Math.round(r.depthM * 100) / 100, depthSource: "z" });
      setNote(`ground point (${p.x}, ${p.y}): packet Z ${r.depthM.toFixed(2)} m${r.id ? `, standing on ${r.name}` : ""}`);
    } else {
      setNote(`ground point (${p.x}, ${p.y}) set; the daemon sent no depth there, so enter the person's depth by hand`);
    }
  }, errorMessage);

  return (
    <EditorFrame kind="people" nodeId={nodeId} title="People">
      <Notice testId="people-custody">People runs through STAFFAGE, a custody D route unless STAFFAGE is local-only: an NDA project refuses it.</Notice>

      <ImageStage hash={plate?.hash ?? null} size={size} testId="people-stage" alt="plate" onPointerDown={size ? (p) => void place(p) : undefined}>
        {size && placement ? (
          <span
            data-testid="people-ground-point"
            style={{ position: "absolute", width: 12, height: 12, marginLeft: -6, marginTop: -6, borderRadius: 6, border: `2px solid ${C.pass}`, background: "rgba(48,209,88,0.3)", ...pointPercent(placement, size) }}
          />
        ) : null}
      </ImageStage>
      <span style={E.note} data-testid="people-placement">
        {placement ? `feet at ${placement.x}, ${placement.y}` : "click the plate where the person's feet touch the ground"}
      </span>

      <Section title="Person" testId="people-fields">
        <Field label="cast">
          <input type="text" style={E.wide} value={asString(params.cast)} placeholder="cast member id or name" data-testid="people-cast" onChange={(e) => set({ cast: e.target.value || undefined })} />
        </Field>
        <Field label="behaviour">
          <input type="text" style={E.wide} value={asString(params.behavior)} placeholder="e.g. walks toward the entrance, unhurried" data-testid="people-behavior" onChange={(e) => set({ behavior: e.target.value || undefined })} />
        </Field>
        <Field label="light">
          <input type="text" style={E.wide} value={asString(params.light)} placeholder="match the plate (default), or describe the light" data-testid="people-light" onChange={(e) => set({ light: e.target.value || undefined })} />
        </Field>
        <Field label="mode">
          <select style={E.wide} value={mode} data-testid="people-mode" onChange={(e) => set({ mode: e.target.value })}>
            {PEOPLE_MODES.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </select>
        </Field>
      </Section>

      <Section title="Depth" testId="people-depth">
        <NumberField
          label="depth"
          value={depth}
          onChange={(v) => set({ personDepthM: v ?? undefined, depthSource: "manual" })}
          min={0.1}
          step={0.1}
          suffix="m from the camera"
          testId="people-depth-input"
        />
        <div style={E.row}>
          <Chip tone={depth === null ? "advisory" : source === "z" ? "pass" : "info"} testId="people-depth-source" title="the occlusion plane: packet geometry nearer than this hides the person">
            {depth === null ? "depth not set" : source === "z" ? "from packet Z" : "set by hand"}
          </Chip>
          {placeState.pending ? <span style={E.note}>reading Z</span> : null}
        </div>
      </Section>

      {note ? <Notice testId="people-note">{note}</Notice> : null}
      {placeState.error ? (
        <Notice tone="error" testId="people-error">
          {placeState.error}
        </Notice>
      ) : null}
    </EditorFrame>
  );
}
