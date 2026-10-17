// Mask node editor (T47, tools T82). Four ways to say what the AI may touch, all on the plate this node is anchored to:
//   Pick       click a pixel, `POST /pixels/idpick` names the Cryptomatte id under it; chips of the selected ids with the
//              class colours. A class that policy never opens (architecture, glazing, signage, other) is refused here.
//   Paint      on a layer at working resolution (at most 2048 on the long side): the brush, Rectangle (drag a filled
//              box), Polygon (click corners; double-click or Enter closes and fills by the even-odd rule, Escape
//              cancels), each adding or erasing; exported as a PNG mask to the CAS. `Load mask…` stores a mask file
//              made elsewhere as the node's mask; `Reset` clears the layer and drops the stored mask.
//   SAM        click points, pixels `/segment` answers the plate-sized mask of the clicked segments (`samHash`) and the
//              segment ids it picked (the stub segments by k-means colour).
//   Text lock  shows the OCR boxes whose pixels are composited back from the plate (a view only).
// Output (node params): `ids` + `idInfo` (the picked ids), `maskHash` (the painted or loaded mask), `samHash`,
// `textLock`. The daemon unions them, intersects with the packet's editable ids and always applies the packet's text
// lock (`textLock` here only toggles the OCR view); this editor never widens what the policy allows. A mask is stored in
// the open project's CAS, so saving needs a project, and the upload names the plate's packet so the daemon scales it
// to that plate's size (a working-size mask would otherwise be refused at the quote).

import { type ChangeEvent, type ReactElement, useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { useThumb } from "../canvas/assets";
import { errorMessage, idPick, type IdPickResult, ocr, type OcrBox, segment, type StoredMask, storeMask } from "./api";
import { ImageStage } from "./ImageStage";
import {
  CLASS_COLOURS,
  clearMask,
  coverage,
  dedupeVertices,
  encodeMaskPng,
  fillPolygon,
  fillRect,
  isIdClass,
  type MaskLayer,
  maskToRgba,
  newMask,
  type PickedId,
  plateBoxToRaster,
  type Point,
  pointPercent,
  stampDisc,
  strokeLine,
  toRaster,
  workSize,
} from "./maskMath";
import { useGraphStore } from "../store";
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

type PaintTool = "brush" | "rect" | "polygon";
const TOOLS: readonly { id: PaintTool; label: string; title: string }[] = [
  { id: "brush", label: "Brush", title: "paint or erase with a round brush" },
  { id: "rect", label: "Rectangle", title: "drag a box: it is filled (or erased) when you let go" },
  { id: "polygon", label: "Polygon", title: "click the corners; double-click or Enter closes and fills, Escape cancels" },
];

/** Mask files the Load button offers: what the daemon decodes as an 8-bit grey, RGB or RGBA image. */
const MASK_FILES = "image/png,image/jpeg,image/tiff,.png,.jpg,.jpeg,.tif,.tiff";

const isTextEntry = (el: EventTarget | null): boolean =>
  el instanceof HTMLElement && (el.isContentEditable || el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement);

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

/** ` (scaled to WxH)` when the daemon scaled an upload to the plate, else nothing. */
const scaledNote = (m: StoredMask): string => (m.scaledFrom && m.width !== null && m.height !== null ? ` (scaled to ${m.width}x${m.height})` : "");

async function fileBytes(file: File): Promise<Uint8Array<ArrayBuffer>> {
  const buf = typeof file.arrayBuffer === "function" ? await file.arrayBuffer() : await new Response(file).arrayBuffer();
  return new Uint8Array(buf);
}

export function MaskEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const plate = usePlate(nodeId);
  const [tab, setTab] = useState<Tab>("pick");
  const [note, setNote] = useState<string | null>(null);
  const [brush, setBrush] = useState(12);
  const [erase, setErase] = useState(false);
  const [tool, setTool] = useState<PaintTool>("brush");
  const [rectDrag, setRectDrag] = useState<{ a: Point; b: Point } | null>(null);
  const [poly, setPoly] = useState<Point[]>([]);
  const [samSelected, setSamSelected] = useState<number[]>([]);
  const [boxes, setBoxes] = useState<OcrBox[]>([]);
  const [lastPick, setLastPick] = useState<IdPickResult | null>(null);
  const projectId = useGraphStore((s) => s.projectId);
  const [paintVersion, redraw] = useReducer((n: number) => n + 1, 0); // bumped after every change to the layer
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const last = useRef<Point | null>(null);
  const rect = useRef<{ a: Point; b: Point } | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);

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
  // the layer is mutated in place: paintVersion is what changes when it does
  const painted = useMemo(() => (layer ? coverage(layer) : 0), [layer, paintVersion]);
  const savedThumb = useThumb(tab === "paint" ? maskHash : null);

  // draw the paint layer over the plate: once per change to it (a rectangle drag re-renders on every move)
  useEffect(() => {
    const cv = canvas.current;
    if (!cv || !layer) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    const img = ctx.createImageData(layer.width, layer.height);
    img.data.set(maskToRgba(layer, [10, 132, 255]));
    ctx.putImageData(img, 0, 0);
  }, [layer, paintVersion]);

  const [pick, pickState] = useAction(async (p: Point) => {
    if (!packetId) return;
    const r = await idPick({ packet_id: packetId, x: p.x, y: p.y });
    if (r.id === null) {
      setLastPick(null);
      setNote(`no Cryptomatte id at (${p.x}, ${p.y}): uncovered pixels stay locked`);
      return;
    }
    setLastPick(r);
    if (!r.editable) {
      setNote(
        r.locked === true
          ? `${r.name} is locked by the packet policy, so it cannot join the mask`
          : `${r.name} is ${r.class}: policy never opens that class, so it cannot join the mask`,
      );
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
    setSamSelected(r.selected);
    setNote(`SAM: ${r.regions} region${r.regions === 1 ? "" : "s"}${r.maskHash ? "" : " (the daemon stored no mask)"}`);
  }, errorMessage);

  const [loadOcr, ocrState] = useAction(async () => {
    if (!packetId) return;
    const r = await ocr({ packet_id: packetId });
    setBoxes(r.boxes);
  }, errorMessage);

  const [saveMask, saveState] = useAction(async () => {
    if (!layer) return;
    if (!projectId) throw new Error("open a project to save the mask: it is stored in the project's CAS");
    const png = await encodeMaskPng(layer);
    const stored = await storeMask(png, projectId, packetId);
    set({ maskHash: stored.hash });
    setNote(`mask saved${scaledNote(stored)}: ${(coverage(layer) * 100).toFixed(1)}% of the frame, ${short(stored.hash)}`);
  }, errorMessage);

  const [loadMask, loadState] = useAction(async (file: File) => {
    if (!projectId) throw new Error("open a project to load a mask: it is stored in the project's CAS");
    const stored = await storeMask(await fileBytes(file), projectId, packetId, file.type || "application/octet-stream");
    set({ maskHash: stored.hash });
    setNote(`mask loaded${scaledNote(stored)} from ${file.name}: ${short(stored.hash)}`);
  }, errorMessage);

  const onFile = (e: ChangeEvent<HTMLInputElement>): void => {
    const file = e.currentTarget.files?.[0];
    e.currentTarget.value = ""; // the same file can be chosen again
    if (file) void loadMask(file);
  };

  const resetMask = (): void => {
    if (layer) clearMask(layer);
    rect.current = null;
    setRectDrag(null);
    setPoly([]);
    set({ maskHash: undefined });
    setNote("mask reset: the paint layer is clear and the node has no stored mask");
    redraw();
  };

  const value: 0 | 255 = erase ? 0 : 255;

  const closePolygon = useCallback(() => {
    if (!layer || !size) return;
    const corners = dedupeVertices(poly);
    if (corners.length < 3) {
      setNote("a polygon needs at least three corners");
      return;
    }
    fillPolygon(
      layer,
      corners.map((c) => toRaster(layer, size, c)),
      value,
    );
    setPoly([]);
    setNote(null);
    redraw();
  }, [layer, size, poly, value]);

  // a polygon in progress: Enter closes it, Escape drops it (not while typing in a field)
  useEffect(() => {
    if (tab !== "paint" || tool !== "polygon" || poly.length === 0) return;
    const onKey = (e: KeyboardEvent): void => {
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || isTextEntry(e.target)) return;
      if (e.key === "Enter") {
        e.preventDefault();
        closePolygon();
      } else if (e.key === "Escape") {
        e.preventDefault();
        setPoly([]);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [tab, tool, poly.length, closePolygon]);

  // text lock on: fetch the OCR boxes once per packet
  useEffect(() => {
    if (textLock && packetId && boxes.length === 0) void loadOcr();
  }, [textLock, packetId]);

  const onDown = useCallback(
    (p: Point) => {
      if (tab === "pick") {
        void pick(p);
      } else if (tab === "paint" && layer && size) {
        if (tool === "rect") {
          rect.current = { a: p, b: p };
          setRectDrag(rect.current);
        } else if (tool === "polygon") {
          setPoly((prev) => [...prev, p]);
        } else {
          const r = toRaster(layer, size, p);
          stampDisc(layer, r.x, r.y, brush, value);
          last.current = r;
          redraw();
        }
      } else if (tab === "sam") {
        void runSam([...samPoints, [p.x, p.y]]);
      }
    },
    [tab, tool, pick, layer, size, brush, value, runSam, samPoints],
  );

  const onMove = useCallback(
    (p: Point) => {
      if (tab !== "paint" || !layer || !size) return;
      if (tool === "rect") {
        if (!rect.current) return;
        rect.current = { a: rect.current.a, b: p };
        setRectDrag(rect.current);
        return;
      }
      if (tool !== "brush" || !last.current) return;
      const r = toRaster(layer, size, p);
      strokeLine(layer, last.current.x, last.current.y, r.x, r.y, brush, value);
      last.current = r;
      redraw();
    },
    [tab, tool, layer, size, brush, value],
  );

  const onUp = useCallback(() => {
    last.current = null;
    const box = rect.current;
    rect.current = null;
    if (!box) return;
    setRectDrag(null);
    if (tab !== "paint" || tool !== "rect" || !layer || !size) return;
    if (box.a.x === box.b.x && box.a.y === box.b.y) return; // a click is not a drag: nothing to fill
    const r = plateBoxToRaster(layer, size, box.a, box.b);
    fillRect(layer, r.x0, r.y0, r.x1, r.y1, value);
    redraw();
  }, [tab, tool, layer, size, value]);

  const removeId = (id: string): void => {
    const next = picked.filter((x) => x.id !== id);
    set({ ids: next.map((x) => x.id), idInfo: next });
  };

  const error = pickState.error ?? samState.error ?? ocrState.error ?? saveState.error ?? loadState.error;
  const paintCursor = tool === "brush" ? "cell" : "crosshair";

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

      <div
        data-testid="mask-stage-wrap"
        onDoubleClick={() => {
          if (tab === "paint" && tool === "polygon") closePolygon();
        }}
      >
        <ImageStage
          hash={plate?.hash ?? null}
          size={size}
          testId="mask-stage"
          alt="plate"
          cursor={tab === "paint" ? paintCursor : "crosshair"}
          onPointerDown={ready ? onDown : undefined}
          onPointerMove={onMove}
          onPointerUp={onUp}
        >
          {tab === "paint" && savedThumb.state === "ready" ? (
            <img
              src={savedThumb.url}
              alt=""
              data-testid="mask-saved-overlay"
              title="the node's stored mask"
              style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "fill", opacity: 0.35, mixBlendMode: "screen" }}
            />
          ) : null}
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
          {size && rectDrag ? (
            <span
              data-testid="mask-rect-preview"
              style={{
                position: "absolute",
                border: `1px dashed ${erase ? C.fail : C.accent}`,
                left: `${(100 * Math.min(rectDrag.a.x, rectDrag.b.x)) / size.width}%`,
                top: `${(100 * Math.min(rectDrag.a.y, rectDrag.b.y)) / size.height}%`,
                width: `${(100 * (Math.abs(rectDrag.b.x - rectDrag.a.x) + 1)) / size.width}%`,
                height: `${(100 * (Math.abs(rectDrag.b.y - rectDrag.a.y) + 1)) / size.height}%`,
              }}
            />
          ) : null}
          {size && poly.length > 0 ? (
            <svg
              data-testid="mask-poly-outline"
              viewBox={`0 0 ${size.width} ${size.height}`}
              preserveAspectRatio="none"
              style={{ position: "absolute", inset: 0, width: "100%", height: "100%", overflow: "visible" }}
            >
              <polyline
                points={poly.map((v) => `${v.x + 0.5},${v.y + 0.5}`).join(" ")}
                fill="none"
                stroke={erase ? C.fail : C.accent}
                strokeWidth={1.5}
                strokeDasharray="4 3"
                vectorEffect="non-scaling-stroke"
              />
            </svg>
          ) : null}
          {size
            ? poly.map((v, i) => (
                <span
                  key={`${v.x}:${v.y}:${i}`}
                  data-testid="mask-poly-vertex"
                  style={{ position: "absolute", width: 6, height: 6, marginLeft: -3, marginTop: -3, borderRadius: 3, background: erase ? C.fail : C.accent, ...pointPercent(v, size) }}
                />
              ))
            : null}
        </ImageStage>
      </div>

      {tab === "paint" ? (
        <Section title="Brush" testId="mask-brush">
          <div style={E.row} role="group" aria-label="paint tools">
            {TOOLS.map((t) => (
              <Button
                key={t.id}
                on={tool === t.id}
                title={t.title}
                onClick={() => {
                  setTool(t.id);
                  setPoly([]);
                  rect.current = null;
                  setRectDrag(null);
                }}
                testId={`mask-tool-${t.id}`}
              >
                {t.label}
              </Button>
            ))}
          </div>
          {tool === "brush" ? (
            <RangeField label="size" value={brush} min={1} max={128} step={1} onChange={setBrush} testId="mask-brush-size" format={(v) => `${v} px`} />
          ) : null}
          {tool === "polygon" ? (
            <div style={E.row}>
              <span style={E.note} data-testid="mask-poly-count">
                {poly.length === 0 ? "click the corners on the plate" : `${poly.length} corner${poly.length === 1 ? "" : "s"}: double-click or Enter to fill, Escape to cancel`}
              </span>
              <Button onClick={closePolygon} disabled={poly.length < 3} testId="mask-poly-close">
                Close polygon
              </Button>
              <Button onClick={() => setPoly([])} disabled={poly.length === 0} testId="mask-poly-cancel">
                Cancel
              </Button>
            </div>
          ) : null}
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
            <Button onClick={() => void saveMask()} disabled={!layer || !projectId || painted === 0 || saveState.pending} testId="mask-paint-save">
              {saveState.pending ? "Saving" : "Save mask"}
            </Button>
          </div>
          <div style={E.row}>
            <input ref={fileInput} type="file" accept={MASK_FILES} hidden onChange={onFile} data-testid="mask-load-input" />
            <Button
              onClick={() => fileInput.current?.click()}
              disabled={!projectId || loadState.pending}
              title="store a mask image made elsewhere (white = editable) as this node's mask; it is scaled to the plate"
              testId="mask-load"
            >
              {loadState.pending ? "Loading" : "Load mask…"}
            </Button>
            <Button
              onClick={resetMask}
              disabled={painted === 0 && !maskHash && poly.length === 0}
              title="clear the paint layer and remove the node's stored mask"
              testId="mask-reset"
            >
              Reset
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
            {samSelected.length > 0 ? (
              <Chip tone="info" testId="mask-sam-selected" title="the segment ids under the clicked points">
                segment{samSelected.length === 1 ? "" : "s"} {samSelected.join(", ")}
              </Chip>
            ) : null}
            <Button
              onClick={() => {
                set({ samPoints: undefined, samHash: undefined });
                setSamSelected([]);
              }}
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
            label="show OCR text-lock boxes"
            title="signage is always locked by the packet policy"
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

      {lastPick ? (
        <Section title="Last pick" testId="mask-pick">
          <div style={E.row}>
            <span data-testid="mask-pick-name">
              {lastPick.name} <span style={E.note}>{lastPick.class}</span>
            </span>
            {lastPick.locked !== null ? (
              <Chip tone={lastPick.locked ? "fail" : "pass"} testId="mask-pick-locked" title="the packet policy's word on this id">
                {lastPick.locked ? "locked" : "not locked"}
              </Chip>
            ) : null}
            {lastPick.coverage !== null ? (
              <Chip tone="muted" testId="mask-pick-coverage" title="how much of the pixel this id covers">
                coverage {Math.round(lastPick.coverage * 100)}%
              </Chip>
            ) : null}
            {lastPick.depthM !== null ? (
              <Chip tone="muted" testId="mask-pick-depth" title="packet Z at the pixel">
                depth {lastPick.depthM.toFixed(2)} m
              </Chip>
            ) : null}
          </div>
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
