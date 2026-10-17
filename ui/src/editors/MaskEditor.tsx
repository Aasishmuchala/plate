// Mask node editor (T47). Four ways to say what the AI may touch, all on the plate this node is anchored to:
//   Pick       click a pixel, `POST /pixels/idpick` names the Cryptomatte id under it; chips of the selected ids with the
//              class colours. A class that policy never opens (architecture, glazing, signage, other) is refused here.
//   Paint      brush add / erase on a layer at working resolution, exported as a PNG mask to the CAS.
//   SAM        click points, pixels `/segment` returns the region (the stub returns k-means labels).
//   Text lock  shows the OCR boxes whose pixels are composited back from the plate.
// Output (node params): `ids` + `idInfo` (the picked ids), `maskHash` (the painted mask), `samHash`, `textLock`. The
// daemon unions them, intersects with the packet's editable ids and re-applies the text lock; this editor never widens
// what the policy allows.

import { type ReactElement, useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { idPick, errorMessage, ocr, type OcrBox, segment, uploadMask } from "./api";
import { ImageStage } from "./ImageStage";
import {
  CLASS_COLOURS,
  clearMask,
  coverage,
  encodeMaskPng,
  isIdClass,
  type MaskLayer,
  maskToRgba,
  newMask,
  type PickedId,
  type Point,
  pointPercent,
  stampDisc,
  strokeLine,
  toRaster,
  workSize,
} from "./maskMath";
import { asBool, asString, useNodeEditor, usePlate } from "./params";
import { C, E } from "./theme";
import { Button, Chip, EditorFrame, Notice, RangeField, Section, Toggle, useAction } from "./widgets";

type Tab = "pick" | "paint" | "sam" | "text";
const TABS: readonly { id: Tab; label: string }[] = [
  { id: "pick", label: "Pick id" },
  { id: "paint", label: "Paint" },
  { id: "sam", label: "SAM click" },
  { id: "text", label: "Text lock" },
];

/** Paint layers live outside React so a stroke survives selecting another node; export makes it permanent. */
const paintLayers = new Map<string, MaskLayer>();
export const clearPaintLayers = (): void => paintLayers.clear();

function readPicked(v: unknown): PickedId[] {
  if (!Array.isArray(v)) return [];
  return v.flatMap((x): PickedId[] => {
    if (typeof x !== "object" || x === null) return [];
    const o = x as Record<string, unknown>;
    return typeof o.id === "string" ? [{ id: o.id, name: typeof o.name === "string" ? o.name : o.id, class: isIdClass(o.class) ? o.class : "other" }] : [];
  });
}

function readPoints(v: unknown): [number, number][] {
  if (!Array.isArray(v)) return [];
  return v.flatMap((p): [number, number][] =>
    Array.isArray(p) && p.length === 2 && typeof p[0] === "number" && typeof p[1] === "number" ? [[p[0], p[1]]] : [],
  );
}

const short = (h: string): string => (h.length > 10 ? h.slice(0, 10) : h);

export function MaskEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const plate = usePlate(nodeId);
  const [tab, setTab] = useState<Tab>("pick");
  const [note, setNote] = useState<string | null>(null);
  const [brush, setBrush] = useState(12);
  const [erase, setErase] = useState(false);
  const [boxes, setBoxes] = useState<OcrBox[]>([]);
  const [, redraw] = useReducer((n: number) => n + 1, 0);
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const last = useRef<Point | null>(null);

  const picked = readPicked(params.idInfo);
  const samPoints = readPoints(params.samPoints);
  const textLock = asBool(params.textLock);
  const maskHash = asString(params.maskHash) || null;
  const samHash = asString(params.samHash) || null;
  const packetId = plate?.packetId ?? null;
  const size = plate?.size ?? null;
  const ready = packetId !== null && size !== null;

  // the paint layer for this node, created at working resolution on first use
  const layer = useMemo(() => {
    if (!size) return null;
    const have = paintLayers.get(nodeId);
    const want = workSize(size);
    if (have && have.width === want.width && have.height === want.height) return have;
    const fresh = newMask(want.width, want.height);
    paintLayers.set(nodeId, fresh);
    return fresh;
  }, [nodeId, size]);
  const painted = layer ? coverage(layer) : 0;

  // draw the paint layer over the plate
  useEffect(() => {
    const cv = canvas.current;
    if (!cv || !layer) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    const img = ctx.createImageData(layer.width, layer.height);
    img.data.set(maskToRgba(layer, [10, 132, 255]));
    ctx.putImageData(img, 0, 0);
  });

  const [pick, pickState] = useAction(async (p: Point) => {
    if (!packetId) return;
    const r = await idPick({ packet_id: packetId, x: p.x, y: p.y });
    if (r.id === null) {
      setNote(`no Cryptomatte id at (${p.x}, ${p.y}): uncovered pixels stay locked`);
      return;
    }
    if (!r.editable) {
      setNote(`${r.name} is ${r.class}: policy never opens that class, so it cannot join the mask`);
      return;
    }
    if (picked.some((x) => x.id === r.id)) {
      setNote(`${r.name} is already selected`);
      return;
    }
    const next: PickedId[] = [...picked, { id: r.id, name: r.name, class: r.class }];
    set({ ids: next.map((x) => x.id), idInfo: next });
    setNote(null);
  }, errorMessage);

  const [runSam, samState] = useAction(async (points: [number, number][]) => {
    if (!packetId) return;
    const r = await segment({ packet_id: packetId, points });
    set({ samPoints: points, samHash: r.maskHash ?? undefined });
    setNote(`SAM: ${r.regions} region${r.regions === 1 ? "" : "s"}${r.maskHash ? "" : " (the daemon stored no mask)"}`);
  }, errorMessage);

  const [loadOcr, ocrState] = useAction(async () => {
    if (!packetId) return;
    const r = await ocr({ packet_id: packetId });
    setBoxes(r.boxes);
  }, errorMessage);

  const [saveMask, saveState] = useAction(async () => {
    if (!layer) return;
    const png = await encodeMaskPng(layer);
    const hash = await uploadMask(png);
    set({ maskHash: hash });
    setNote(`mask saved: ${(coverage(layer) * 100).toFixed(1)}% of the frame, ${short(hash)}`);
  }, errorMessage);

  // text lock on: fetch the OCR boxes once per packet
  useEffect(() => {
    if (textLock && packetId && boxes.length === 0) void loadOcr();
  }, [textLock, packetId]);

  const onDown = useCallback(
    (p: Point) => {
      if (tab === "pick") {
        void pick(p);
      } else if (tab === "paint" && layer && size) {
        const r = toRaster(layer, size, p);
        stampDisc(layer, r.x, r.y, brush, erase ? 0 : 255);
        last.current = r;
        redraw();
      } else if (tab === "sam") {
        void runSam([...samPoints, [p.x, p.y]]);
      }
    },
    [tab, pick, layer, size, brush, erase, runSam, samPoints],
  );

  const onMove = useCallback(
    (p: Point) => {
      if (tab !== "paint" || !layer || !size || !last.current) return;
      const r = toRaster(layer, size, p);
      strokeLine(layer, last.current.x, last.current.y, r.x, r.y, brush, erase ? 0 : 255);
      last.current = r;
      redraw();
    },
    [tab, layer, size, brush, erase],
  );

  const removeId = (id: string): void => {
    const next = picked.filter((x) => x.id !== id);
    set({ ids: next.map((x) => x.id), idInfo: next });
  };

  const error = pickState.error ?? samState.error ?? ocrState.error ?? saveState.error;

  return (
    <EditorFrame kind="mask" nodeId={nodeId} title="Mask">
      {!ready ? (
        <Notice tone="error" testId="mask-unready">
          {packetId === null ? "connect this node to a plate with a packet first" : "the plate's image size is unknown, so pixels cannot be mapped"}
        </Notice>
      ) : null}
      <div style={E.row} role="group" aria-label="mask tools">
        {TABS.map((t) => (
          <Button key={t.id} on={tab === t.id} onClick={() => setTab(t.id)} testId={`mask-tab-${t.id}`}>
            {t.label}
          </Button>
        ))}
      </div>

      <ImageStage
        hash={plate?.hash ?? null}
        size={size}
        testId="mask-stage"
        alt="plate"
        cursor={tab === "paint" ? "cell" : "crosshair"}
        onPointerDown={ready ? onDown : undefined}
        onPointerMove={onMove}
        onPointerUp={() => {
          last.current = null;
        }}
      >
        {layer ? (
          <canvas
            ref={canvas}
            width={layer.width}
            height={layer.height}
            data-testid="mask-paint-canvas"
            style={{ position: "absolute", inset: 0, width: "100%", height: "100%", imageRendering: "pixelated" }}
          />
        ) : null}
        {size
          ? samPoints.map((p, i) => (
              <span
                key={`${p[0]}:${p[1]}:${i}`}
                data-testid="mask-sam-point"
                style={{ position: "absolute", width: 8, height: 8, marginLeft: -4, marginTop: -4, borderRadius: 4, background: C.advisory, ...pointPercent({ x: p[0], y: p[1] }, size) }}
              />
            ))
          : null}
        {size && textLock
          ? boxes.map((b, i) => (
              <span
                key={i}
                data-testid="mask-ocr-box"
                title={b.text}
                style={{
                  position: "absolute",
                  border: `1px solid ${C.advisory}`,
                  left: `${(100 * b.x0) / size.width}%`,
                  top: `${(100 * b.y0) / size.height}%`,
                  width: `${(100 * (b.x1 - b.x0)) / size.width}%`,
                  height: `${(100 * (b.y1 - b.y0)) / size.height}%`,
                }}
              />
            ))
          : null}
      </ImageStage>

      {tab === "paint" ? (
        <Section title="Brush" testId="mask-brush">
          <RangeField label="size" value={brush} min={1} max={64} step={1} onChange={setBrush} testId="mask-brush-size" format={(v) => `${v} px`} />
          <div style={E.row}>
            <Button on={!erase} onClick={() => setErase(false)} testId="mask-brush-add">
              Add
            </Button>
            <Button on={erase} onClick={() => setErase(true)} testId="mask-brush-erase">
              Erase
            </Button>
            <Button
              onClick={() => {
                if (layer) clearMask(layer);
                redraw();
              }}
              testId="mask-paint-clear"
            >
              Clear paint
            </Button>
            <Button onClick={() => void saveMask()} disabled={!layer || painted === 0 || saveState.pending} testId="mask-paint-save">
              {saveState.pending ? "Saving" : "Save mask"}
            </Button>
          </div>
          <span style={E.note} data-testid="mask-paint-coverage">
            painted {(painted * 100).toFixed(1)}% of the frame{layer ? ` at ${layer.width}x${layer.height}` : ""}
          </span>
        </Section>
      ) : null}

      {tab === "sam" ? (
        <Section title="SAM points" testId="mask-sam">
          <div style={E.row}>
            <span style={E.note} data-testid="mask-sam-count">
              {samPoints.length} point{samPoints.length === 1 ? "" : "s"}
              {samHash ? ` · mask ${short(samHash)}` : ""}
            </span>
            <Button
              onClick={() => set({ samPoints: undefined, samHash: undefined })}
              disabled={samPoints.length === 0 && !samHash}
              testId="mask-sam-clear"
            >
              Clear points
            </Button>
            {samState.pending ? <span style={E.note}>segmenting</span> : null}
          </div>
        </Section>
      ) : null}

      {tab === "text" || textLock ? (
        <Section title="Text lock" testId="mask-text">
          <Toggle
            label="lock text (OCR boxes are composited back from the plate)"
            checked={textLock}
            onChange={(v) => {
              set({ textLock: v });
              if (!v) setBoxes([]);
            }}
            testId="mask-text-lock"
          />
          {textLock ? (
            <span style={E.note} data-testid="mask-ocr-count">
              {ocrState.pending ? "reading text" : `${boxes.length} text box${boxes.length === 1 ? "" : "es"}`}
              {boxes.length > 0 ? `: ${boxes.slice(0, 4).map((b) => b.text || "(unreadable)").join(", ")}` : ""}
            </span>
          ) : null}
        </Section>
      ) : null}

      <Section title="Selected ids" testId="mask-ids">
        {picked.length === 0 ? (
          <span style={E.note}>none yet: pick ids on the plate</span>
        ) : (
          <div style={E.row}>
            {picked.map((p) => (
              <span
                key={p.id}
                data-testid="mask-chip"
                data-id={p.id}
                data-class={p.class}
                style={{ display: "inline-flex", alignItems: "center", gap: 4, padding: "1px 6px", borderRadius: 10, border: `1px solid ${CLASS_COLOURS[p.class]}`, fontSize: 11 }}
              >
                <span aria-hidden style={{ width: 8, height: 8, borderRadius: 4, background: CLASS_COLOURS[p.class] }} />
                {p.name}
                <span style={E.note}>{p.class}</span>
                <button type="button" aria-label={`remove ${p.name}`} onClick={() => removeId(p.id)} style={{ ...E.button, padding: "0 4px", border: "none", background: "none" }}>
                  x
                </button>
              </span>
            ))}
          </div>
        )}
      </Section>

      <div style={E.row} data-testid="mask-output">
        <Chip tone={picked.length > 0 || maskHash || samHash ? "info" : "muted"} testId="mask-output-chip">
          output: {picked.length} id{picked.length === 1 ? "" : "s"}
          {maskHash ? ` · mask ${short(maskHash)}` : ""}
          {samHash ? ` · sam ${short(samHash)}` : ""}
          {textLock ? " · text lock" : ""}
        </Chip>
      </div>
      {note ? <Notice testId="mask-note">{note}</Notice> : null}
      {error ? (
        <Notice tone="error" testId="mask-error">
          {error}
        </Notice>
      ) : null}
    </EditorFrame>
  );
}
