// CropPair node editor (T47): pick the push-in centre on the approved still, set the start and end zoom (1.0 to 3.0),
// and see the two crop rectangles live. The rectangles use `cutBoxes`, the TypeScript copy of the daemon's `cut_crops`
// geometry, so what is drawn is what is cut: native pixels from the source view only, no resampling, no new angle.
// Click or drag on the still to move the centre. Node params: `cx`, `cy` (image pixels), `zoomStart`, `zoomEnd`.

import type { ReactElement } from "react";
import { type CropBox, cutBoxes, boxPercent, moveZoom, zoomError } from "./cropBoxes";
import { ImageStage } from "./ImageStage";
import type { Point } from "./maskMath";
import { asNumber, useNodeEditor, useStill } from "./params";
import { C, E } from "./theme";
import { Chip, EditorFrame, Notice, RangeField, Section } from "./widgets";

export const DEFAULT_ZOOMS: readonly [number, number] = [1.0, 1.3];

const boxText = (b: CropBox): string => `${b.x0}, ${b.y0}  ${b.w} x ${b.h}`;

export function CropPairEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const still = useStill(nodeId);
  const size = still?.size ?? null;

  const z0 = asNumber(params.zoomStart, DEFAULT_ZOOMS[0]);
  const z1 = asNumber(params.zoomEnd, DEFAULT_ZOOMS[1]);
  const cx = asNumber(params.cx, size ? Math.floor(size.width / 2) : 0);
  const cy = asNumber(params.cy, size ? Math.floor(size.height / 2) : 0);

  const bad = zoomError(z0, z1);
  const plan = size && !bad ? cutBoxes(size.width, size.height, cx, cy, z0, z1) : null;

  const setZoom = (which: "start" | "end", v: number): void => {
    const [a, b] = moveZoom(z0, z1, which, v);
    set({ zoomStart: a, zoomEnd: b });
  };

  const pickCentre = (p: Point): void => set({ cx: p.x, cy: p.y });

  return (
    <EditorFrame kind="croppair" nodeId={nodeId} title="Crop pair">
      {!size ? (
        <Notice tone="error" testId="croppair-nosize">
          {still?.hash ? "the still's image size is unknown, so the crop rectangles cannot be placed" : "connect this node to an approved still first"}
        </Notice>
      ) : null}

      <ImageStage hash={still?.hash ?? null} size={size} testId="croppair-stage" alt="approved still" onPointerDown={size ? pickCentre : undefined} onPointerMove={size ? pickCentre : undefined}>
        {size && plan ? (
          <>
            <div
              data-testid="croppair-rect-start"
              style={{ position: "absolute", boxSizing: "border-box", border: `2px dashed ${C.advisory}`, ...boxPercent(plan.start, size.width, size.height) }}
            />
            <div
              data-testid="croppair-rect-end"
              style={{ position: "absolute", boxSizing: "border-box", border: `2px solid ${C.accent}`, ...boxPercent(plan.end, size.width, size.height) }}
            />
            <div
              data-testid="croppair-centre"
              style={{
                position: "absolute",
                width: 10,
                height: 10,
                marginLeft: -5,
                marginTop: -5,
                borderRadius: 5,
                background: C.accent,
                left: `${(100 * cx) / size.width}%`,
                top: `${(100 * cy) / size.height}%`,
              }}
            />
          </>
        ) : null}
      </ImageStage>

      <Section title="Zoom" testId="croppair-zoom">
        <RangeField label="start" value={z0} min={1} max={3} step={0.05} onChange={(v) => setZoom("start", v)} testId="croppair-zoom-start" format={(v) => `${v.toFixed(2)}x`} />
        <RangeField label="end" value={z1} min={1} max={3} step={0.05} onChange={(v) => setZoom("end", v)} testId="croppair-zoom-end" format={(v) => `${v.toFixed(2)}x`} />
        <div style={E.row}>
          <Chip tone={bad ? "fail" : "info"} testId="croppair-push" title="end zoom over start zoom">
            push-in {(z1 / z0).toFixed(2)}x
          </Chip>
          <span style={E.note} data-testid="croppair-centre-text">
            centre {Math.round(cx)}, {Math.round(cy)}
          </span>
        </div>
        {bad ? (
          <Notice tone="error" testId="croppair-zoom-error">
            {bad}
          </Notice>
        ) : null}
      </Section>

      {plan ? (
        <Section title="Boxes (x0, y0  w x h, native pixels)" testId="croppair-boxes">
          <table style={E.table}>
            <tbody>
              <tr>
                <th style={E.th}>start</th>
                <td style={E.td} data-testid="croppair-box-start">
                  {boxText(plan.start)}
                </td>
              </tr>
              <tr>
                <th style={E.th}>end</th>
                <td style={E.td} data-testid="croppair-box-end">
                  {boxText(plan.end)}
                </td>
              </tr>
            </tbody>
          </table>
          <span style={E.note}>cut from the source view only: both crops are resampled to the engine size with one scale factor, never extended past the frame</span>
        </Section>
      ) : null}
    </EditorFrame>
  );
}
