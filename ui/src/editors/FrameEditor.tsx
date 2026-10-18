// Frame node editor (T112 for the T81 annotation box, decision 57): the title the box shows top-left and its size on
// the canvas. A Frame has no ports and no rule reads it; it only groups nodes for the eye, and it is saved in
// `project.json` like any node. Size presets match the common groupings (one shot, a set, a sequence).
// Node params: `title`, `w`, `h` (canvas pixels; FrameNode draws `w ?? 480` × `h ?? 320`).

import type { ReactElement } from "react";
import { FRAME_DEFAULT_HEIGHT, FRAME_DEFAULT_WIDTH } from "../nodes/FrameNode";
import { asString, useNodeEditor } from "./params";
import { E } from "./theme";
import { Button, EditorFrame, Field, NumberField, Section } from "./widgets";

export const FRAME_MIN = 80;
export const FRAME_MAX = 8000;
export const FRAME_SIZES = [
  { name: "shot", w: FRAME_DEFAULT_WIDTH, h: FRAME_DEFAULT_HEIGHT },
  { name: "set", w: 960, h: 540 },
  { name: "sequence", w: 1600, h: 600 },
] as const;

const size = (v: unknown, fallback: number): number => (typeof v === "number" && Number.isFinite(v) && v > 0 ? Math.round(v) : fallback);

export function FrameEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const title = asString(params.title);
  const w = size(params.w, FRAME_DEFAULT_WIDTH);
  const h = size(params.h, FRAME_DEFAULT_HEIGHT);
  const dim = (key: "w" | "h", v: number | null, fallback: number): void => {
    const n = v === null ? null : Math.round(v);
    set({ [key]: n === null || n === fallback ? undefined : n });
  };

  return (
    <EditorFrame kind="frame" nodeId={nodeId} title="Frame">
      <Section title="Title" testId="frame-title-section">
        <Field label="title">
          <input
            type="text"
            style={E.wide}
            value={title}
            placeholder="frame"
            aria-label="frame title"
            data-testid="frame-title-input"
            maxLength={120}
            onChange={(e) => set({ title: e.target.value.trim() === "" ? undefined : e.target.value })}
          />
        </Field>
      </Section>
      <Section title="Size" testId="frame-size">
        <div style={E.row}>
          <NumberField label="width" value={w} min={FRAME_MIN} max={FRAME_MAX} step={10} onChange={(v) => dim("w", v, FRAME_DEFAULT_WIDTH)} testId="frame-w" />
          <NumberField label="height" value={h} min={FRAME_MIN} max={FRAME_MAX} step={10} onChange={(v) => dim("h", v, FRAME_DEFAULT_HEIGHT)} testId="frame-h" />
        </div>
        <div style={E.row}>
          {FRAME_SIZES.map((s) => (
            <Button
              key={s.name}
              on={w === s.w && h === s.h}
              onClick={() => set({ w: s.w === FRAME_DEFAULT_WIDTH ? undefined : s.w, h: s.h === FRAME_DEFAULT_HEIGHT ? undefined : s.h })}
              testId={`frame-size-${s.name}`}
            >
              {s.name} {s.w} × {s.h}
            </Button>
          ))}
        </div>
        <span style={E.note}>
          canvas pixels, {FRAME_MIN} to {FRAME_MAX}; the box sits behind the nodes and moves on its own (it does not carry them)
        </span>
      </Section>
    </EditorFrame>
  );
}
