// Recolour node editor (T47): pick the material on the plate, type a RAL code or a hex colour, and see the expected
// ΔE00 of the recomposition before anything runs. Recolour is element recomposition (math on the render's own passes),
// not an AI edit, so it needs no editable id. Node params: `material` (m: id), `materialName`, `target` (`RAL 7016` or
// `#383E42`, or `{lab, label}` for a measured Lab swatch), `targetHex`.
//
// T91: a `RAL…` button opens a searchable grid of the whole RAL Classic table (`ralTable.ts`), and a `Project swatches`
// list keeps the colours this project measured (hex or Lab, with a note) in `localStorage["plate.swatches.<projectId>"]`.
// Picking from either writes `params.target`; the value feeds Recolour, never a prompt.
//
// Expected ΔE comes from the daemon's numeric preview (`recolour.apply`): <= 1 for recomposition, about 2.5 for the
// ratio fallback, wider when the passes are missing (`degraded`), which is expected to fail on dark semi-gloss panels.

import { type KeyboardEvent, type ReactElement, useEffect, useId, useMemo, useRef, useState } from "react";
import { useGraphStore } from "../store";
import { errorMessage, idPick, type RecolourExpect, recolourPreview } from "./api";
import {
  labTargetOf,
  labText,
  labToHex,
  loadSwatches,
  parseTarget,
  readSwatchInput,
  saveSwatches,
  type Swatch,
  swatchHex,
  swatchTarget,
} from "./colour";
import { ImageStage } from "./ImageStage";
import type { Point } from "./maskMath";
import { asString, useNodeEditor, usePlate } from "./params";
import { RAL_CLASSIC, type RalColour, searchRal } from "./ralTable";
import { C, E } from "./theme";
import { Button, Chip, EditorFrame, Field, Notice, Section, useAction } from "./widgets";

const RAL_COLUMNS = 6;

/** The `RAL…` button and its popover: search by code or name, a six column grid, arrow keys, Enter or click to pick. */
function RalPicker({ current, onPick }: { current: string | null; onPick: (c: RalColour) => void }): ReactElement {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const opener = useRef<HTMLButtonElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const cells = useRef<(HTMLButtonElement | null)[]>([]);
  const popId = useId();
  const results = useMemo(() => searchRal(query), [query]);

  const close = (refocus: boolean): void => {
    setOpen(false);
    setQuery("");
    setActive(0);
    if (refocus) opener.current?.focus();
  };

  useEffect(() => {
    if (open) search.current?.focus();
  }, [open]);

  // a press outside the picker closes it
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent): void => {
      if (root.current && e.target instanceof Node && !root.current.contains(e.target)) close(false);
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  const focusCell = (i: number): void => {
    const j = Math.max(0, Math.min(results.length - 1, i));
    setActive(j);
    cells.current[j]?.focus();
  };

  const pick = (c: RalColour): void => {
    onPick(c);
    close(true);
  };

  const onGridKey = (e: KeyboardEvent<HTMLDivElement>): void => {
    const lastRow = Math.floor((results.length - 1) / RAL_COLUMNS);
    switch (e.key) {
      case "ArrowRight":
        focusCell(active + 1);
        break;
      case "ArrowLeft":
        focusCell(active - 1);
        break;
      case "ArrowDown":
        if (Math.floor(active / RAL_COLUMNS) < lastRow) focusCell(active + RAL_COLUMNS);
        break;
      case "ArrowUp":
        if (active < RAL_COLUMNS) search.current?.focus();
        else focusCell(active - RAL_COLUMNS);
        break;
      case "Home":
        focusCell(0);
        break;
      case "End":
        focusCell(results.length - 1);
        break;
      default:
        return;
    }
    e.preventDefault();
  };

  const onSearchKey = (e: KeyboardEvent<HTMLInputElement>): void => {
    if (e.key === "ArrowDown" && results.length > 0) {
      e.preventDefault();
      focusCell(0);
    } else if (e.key === "Enter" && results[0]) {
      e.preventDefault();
      pick(results[0]);
    }
  };

  const rows: RalColour[][] = [];
  for (let i = 0; i < results.length; i += RAL_COLUMNS) rows.push(results.slice(i, i + RAL_COLUMNS));

  return (
    <div ref={root} style={{ position: "relative", display: "inline-block" }}>
      <button
        ref={opener}
        type="button"
        style={{ ...E.button, ...(open ? E.buttonOn : null) }}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? popId : undefined}
        data-testid="recolour-ral-button"
        title="pick from the RAL Classic table"
        onClick={() => (open ? close(false) : setOpen(true))}
      >
        RAL…
      </button>
      {open ? (
        <div
          id={popId}
          role="dialog"
          aria-label="RAL Classic colours"
          data-testid="recolour-ral-popover"
          style={{
            position: "absolute",
            top: "100%",
            left: 0,
            zIndex: 30,
            marginTop: 4,
            width: 480,
            maxWidth: "calc(100vw - 32px)",
            display: "flex",
            flexDirection: "column",
            gap: 6,
            padding: 8,
            background: C.panel,
            border: `1px solid ${C.line}`,
            borderRadius: 6,
            boxShadow: "0 8px 24px rgba(0, 0, 0, 0.5)",
          }}
          onKeyDown={(e) => {
            if (e.key !== "Escape") return;
            e.stopPropagation();
            close(true);
          }}
        >
          <input
            ref={search}
            type="search"
            style={E.wide}
            value={query}
            placeholder="search code or name (7016, anthracite)"
            aria-label="Search RAL code or name"
            data-testid="recolour-ral-search"
            onChange={(e) => {
              setQuery(e.target.value);
              setActive(0);
            }}
            onKeyDown={onSearchKey}
          />
          <span style={E.note} aria-live="polite" data-testid="recolour-ral-count">
            {results.length === RAL_CLASSIC.length ? `${results.length} colours` : `${results.length} of ${RAL_CLASSIC.length}`}
          </span>
          {results.length === 0 ? (
            <span style={E.note} data-testid="recolour-ral-none">
              no RAL code or name matches
            </span>
          ) : (
            <div
              role="grid"
              aria-label="RAL Classic"
              aria-colcount={RAL_COLUMNS}
              aria-rowcount={rows.length}
              data-testid="recolour-ral-grid"
              style={{ maxHeight: 320, overflowY: "auto", display: "flex", flexDirection: "column", gap: 4 }}
              onKeyDown={onGridKey}
            >
              {rows.map((row, r) => (
                <div key={row[0]?.code ?? r} role="row" style={{ display: "grid", gridTemplateColumns: `repeat(${RAL_COLUMNS}, minmax(0, 1fr))`, gap: 4 }}>
                  {row.map((c, k) => {
                    const i = r * RAL_COLUMNS + k;
                    const here = current === c.code;
                    return (
                      <div key={c.code} role="gridcell" style={{ minWidth: 0 }}>
                        <button
                          ref={(el) => {
                            cells.current[i] = el;
                          }}
                          type="button"
                          tabIndex={i === active ? 0 : -1}
                          aria-label={`RAL ${c.code} ${c.name}`}
                          aria-current={here ? "true" : undefined}
                          title={`RAL ${c.code} ${c.name} ${c.hex}`}
                          data-testid={`recolour-ral-${c.code}`}
                          style={{
                            display: "flex",
                            flexDirection: "column",
                            gap: 2,
                            width: "100%",
                            padding: 3,
                            font: "inherit",
                            fontSize: 10,
                            textAlign: "left",
                            color: C.text,
                            background: C.bg,
                            border: `1px solid ${here ? C.accent : C.line}`,
                            borderRadius: 4,
                            cursor: "pointer",
                            minWidth: 0,
                          }}
                          onFocus={() => setActive(i)}
                          onClick={() => pick(c)}
                        >
                          <span aria-hidden style={{ display: "block", height: 18, borderRadius: 2, border: `1px solid ${C.line}`, background: c.hex }} />
                          <span style={{ fontWeight: 600 }}>{c.code}</span>
                          <span style={{ color: C.muted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.name}</span>
                        </button>
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}

/** The project's measured colours: name, hex or Lab, a note on how it was measured. Kept in this browser per project. */
function ProjectSwatches({ projectId, onPick }: { projectId: string | null; onPick: (s: Swatch) => void }): ReactElement {
  const [list, setList] = useState<Swatch[]>(() => loadSwatches(projectId));
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [note, setNote] = useState("");
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    setList(loadSwatches(projectId));
  }, [projectId]);

  const commit = (next: Swatch[]): void => {
    setList(next);
    if (projectId && !saveSwatches(projectId, next)) setProblem("this browser would not keep the swatches (storage is blocked or full)");
  };

  const add = (): void => {
    const r = readSwatchInput(name, value, note);
    if (!r.ok) {
      setProblem(r.reason);
      return;
    }
    setProblem(null);
    // a swatch with the same name is replaced: re-measuring a colour updates it
    const key = r.swatch.name.toLowerCase();
    commit([...list.filter((s) => s.name.toLowerCase() !== key), r.swatch]);
    setName("");
    setValue("");
    setNote("");
  };

  const onEnter = (e: KeyboardEvent<HTMLInputElement>): void => {
    if (e.key === "Enter") {
      e.preventDefault();
      add();
    }
  };

  return (
    <Section title="Project swatches" testId="recolour-swatches">
      {!projectId ? (
        <span style={E.note} data-testid="recolour-swatches-noproject">
          open a project to keep swatches
        </span>
      ) : list.length === 0 ? (
        <span style={E.note} data-testid="recolour-swatches-empty">
          none yet: add a colour measured on the sample
        </span>
      ) : (
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 4 }} data-testid="recolour-swatch-list">
          {list.map((s, i) => (
            <li key={`${i}:${s.name}`} style={E.row} data-testid="recolour-swatch-item" data-name={s.name}>
              <span aria-hidden style={{ display: "inline-block", width: 20, height: 14, borderRadius: 3, border: `1px solid ${C.line}`, background: swatchHex(s) }} />
              <button type="button" style={E.button} data-testid={`recolour-swatch-use-${i}`} title={`use ${s.name} as the target`} onClick={() => onPick(s)}>
                {s.name}
              </button>
              <span style={E.note}>{s.lab ? `${labText(s.lab)} · ${swatchHex(s)}` : swatchHex(s)}</span>
              {s.note ? (
                <span style={E.note} data-testid={`recolour-swatch-note-${i}`}>
                  {s.note}
                </span>
              ) : null}
              <button
                type="button"
                style={E.button}
                aria-label={`remove swatch ${s.name}`}
                data-testid={`recolour-swatch-remove-${i}`}
                onClick={() => commit(list.filter((x) => x !== s))}
              >
                remove
              </button>
            </li>
          ))}
        </ul>
      )}
      <div style={E.row}>
        <input
          type="text"
          style={{ ...E.input, width: 110 }}
          value={name}
          placeholder="name"
          aria-label="swatch name"
          disabled={!projectId}
          data-testid="recolour-swatch-name"
          onChange={(e) => setName(e.target.value)}
          onKeyDown={onEnter}
        />
        <input
          type="text"
          style={{ ...E.input, width: 150 }}
          value={value}
          placeholder="#383E42 or L a b"
          aria-label="swatch hex or Lab"
          disabled={!projectId}
          data-testid="recolour-swatch-value"
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onEnter}
        />
        <input
          type="text"
          style={E.wide}
          value={note}
          placeholder="measured note (instrument, date, light)"
          aria-label="swatch note"
          disabled={!projectId}
          data-testid="recolour-swatch-note"
          onChange={(e) => setNote(e.target.value)}
          onKeyDown={onEnter}
        />
        <Button onClick={add} disabled={!projectId} testId="recolour-swatch-add">
          Add
        </Button>
      </div>
      {problem ? (
        <Notice tone="error" testId="recolour-swatch-error">
          {problem}
        </Notice>
      ) : null}
    </Section>
  );
}

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
  // a measured Lab swatch is the node's target while the field is empty (typing replaces it)
  const labTarget = target === null && text.trim() === "" ? labTargetOf(params.target) : null;
  const labHex = labTarget ? labToHex(labTarget.lab) : null;
  // what the preview is asked about: a RAL code, else the hex (a Lab swatch goes as its 8-bit hex, for the estimate only)
  const request: { ral?: string; hex?: string } | null = target ? (target.kind === "ral" ? { ral: target.code } : { hex: target.hex }) : labHex ? { hex: labHex } : null;
  const requestKey = request ? (request.ral ? `ral:${request.ral}` : `hex:${request.hex ?? ""}`) : "";

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

  const [estimate, estimateState] = useAction(async (m: string, req: { ral?: string; hex?: string }) => {
    if (!projectId) return;
    setExpect(await recolourPreview(projectId, nodeId, { material_id: m, ...req }));
  }, errorMessage);

  // a material and a valid target: ask the daemon what to expect
  useEffect(() => {
    setExpect(null);
    if (projectId && material && request) void estimate(material, request);
    // the preview follows the material and the target, nothing else
  }, [projectId, nodeId, material, requestKey]);

  const onText = (v: string): void => {
    setText(v);
    const r = v.trim() === "" ? null : parseTarget(v);
    if (r && r.ok) set({ target: r.target.label, targetHex: r.target.hex });
    else set({ target: undefined, targetHex: undefined });
  };

  // a picked swatch: a hex goes into the field like typed text; a Lab keeps its exact numbers in `params.target`
  const applySwatch = (s: Swatch): void => {
    if (s.lab) {
      setText("");
      set({ target: swatchTarget(s), targetHex: swatchHex(s) });
    } else {
      onText(swatchHex(s));
    }
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
            {RAL_CLASSIC.map((c) => (
              <option key={c.code} value={`RAL ${c.code}`} label={c.name} />
            ))}
          </datalist>
        </Field>
        <div style={E.row}>
          <RalPicker current={target?.kind === "ral" ? target.code : null} onPick={(c) => onText(`RAL ${c.code}`)} />
          <span style={E.note}>the whole RAL Classic table; a measured swatch below wins over a RAL approximation</span>
        </div>
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
        ) : labTarget && labHex ? (
          <div style={E.row}>
            <span
              aria-hidden
              data-testid="recolour-swatch"
              style={{ display: "inline-block", width: 28, height: 16, borderRadius: 3, border: "1px solid #48484a", background: labHex }}
            />
            <span style={E.note} data-testid="recolour-hex">
              swatch {labTarget.label} · {labText(labTarget.lab)} · {labHex}
            </span>
          </div>
        ) : parsed && !parsed.ok ? (
          <Notice tone="error" testId="recolour-target-error">
            {parsed.reason}
          </Notice>
        ) : null}
      </Section>

      <ProjectSwatches projectId={projectId} onPick={applySwatch} />

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
            {estimateState.pending ? "estimating" : material && request ? "no estimate yet" : "pick a material and a colour"}
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
