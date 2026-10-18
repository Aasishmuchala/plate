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
//
// T112, several materials in one pass (T90): `Add to the pass` puts the material and colour above into
// `params.materials` (`[{material, target, method?, source_lab?}]`, 1 to 8 distinct `m:` ids; `materialName` is kept
// beside each for display, as at the top level). While the picked material is in the pass, a new colour or source Lab
// updates its entry; clicking a listed material (on the plate or its Edit button) loads it back. With a pass the daemon
// recolours exactly the listed materials, so the pair above is only what is being edited. `method` (auto,
// recomposition, ratio, degraded) is the default for every entry that names none, and each entry can name its own;
// `source_lab` is the source albedo (L a b) when the packet has no diffuse_filter pass and the legend no albedo.

import { type KeyboardEvent, type ReactElement, useEffect, useId, useMemo, useRef, useState } from "react";
import { useGraphStore } from "../store";
import { errorMessage, idPick, parseRecolourExpect, preview, type RecolourExpect, recolourPreview } from "./api";
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

// ------------------------------------------------------------------------------------------------ the pass (T90)

/** The methods `recolour.apply` takes; no method (auto) lets the packet's elements decide. */
export const RECOLOUR_METHODS = ["recomposition", "ratio", "degraded"] as const;
export type RecolourMethod = (typeof RECOLOUR_METHODS)[number];
/** At most this many materials in one pass (`recolour_node.MAX_MATERIALS`). */
export const MAX_MATERIALS = 8;
const MATERIAL_ID = /^m:[0-9a-fA-F]{8}$/;

export type RecolourTarget = string | { lab: [number, number, number]; label: string };

/** One entry of `params.materials`. `materialName` is display only; the daemon reads the other four. */
export interface RecolourEntry {
  material: string;
  materialName?: string;
  target: RecolourTarget;
  method?: RecolourMethod;
  source_lab?: [number, number, number];
}

const isMethod = (v: unknown): v is RecolourMethod => typeof v === "string" && (RECOLOUR_METHODS as readonly string[]).includes(v);
const lab3 = (v: unknown): [number, number, number] | null =>
  Array.isArray(v) && v.length === 3 && v.every((n) => typeof n === "number" && Number.isFinite(n)) ? [v[0] as number, v[1] as number, v[2] as number] : null;

/** A target as the node keeps it: a RAL / hex string or a measured `{lab, label}`; null for anything else. */
export function targetOf(v: unknown): RecolourTarget | null {
  if (typeof v === "string") return v.trim() !== "" ? v : null;
  return labTargetOf(v);
}

/** `params.materials` as the editor shows it: well-formed entries only, first of each material id. */
export function readEntries(v: unknown): RecolourEntry[] {
  if (!Array.isArray(v)) return [];
  const out: RecolourEntry[] = [];
  for (const raw of v) {
    if (typeof raw !== "object" || raw === null) continue;
    const o = raw as Record<string, unknown>;
    const material = typeof o.material === "string" ? o.material : null;
    const target = targetOf(o.target);
    if (!material || target === null || out.some((e) => e.material === material)) continue;
    const entry: RecolourEntry = { material, target };
    if (typeof o.materialName === "string" && o.materialName !== "") entry.materialName = o.materialName;
    if (isMethod(o.method)) entry.method = o.method;
    const src = lab3(o.source_lab);
    if (src) entry.source_lab = src;
    out.push(entry);
  }
  return out;
}

/** What the entry's swatch shows: the RAL or hex colour, or the Lab converted. */
export function targetHex(t: RecolourTarget): string | null {
  if (typeof t !== "string") return labToHex(t.lab);
  const r = parseTarget(t);
  return r.ok ? r.target.hex : null;
}

export const targetLabel = (t: RecolourTarget): string => (typeof t === "string" ? t : `swatch ${t.label}`);

/** `L a b` (with or without letters or commas) -> three numbers; null when it is not one. Empty text is `undefined`. */
export function parseSourceLab(text: string): [number, number, number] | null | undefined {
  if (text.trim() === "") return undefined;
  const nums = text.match(/[-+]?\d+(?:\.\d+)?/g)?.map(Number) ?? [];
  const lab = lab3(nums);
  if (!lab || lab[0] < 0 || lab[0] > 100 || Math.abs(lab[1]) > 160 || Math.abs(lab[2]) > 160) return null;
  return lab;
}

const labWords = (lab: readonly number[]): string => lab.map((n) => String(n)).join(" ");

/** The entry with `key` set to `value`, or without `key` when `value` is undefined (no undefined keys in the graph). */
function withField(e: RecolourEntry, key: "method" | "source_lab", value: RecolourMethod | [number, number, number] | undefined): RecolourEntry {
  const out: RecolourEntry = { ...e };
  if (key === "method") {
    if (isMethod(value)) out.method = value;
    else delete out.method;
  } else {
    const lab = lab3(value);
    if (lab) out.source_lab = lab;
    else delete out.source_lab;
  }
  return out;
}

/** The listed materials, one row each: its colour, its method, the pass estimate, Edit and Remove. */
function PassSection({
  entries,
  current,
  estimates,
  onEdit,
  onRemove,
  onMethod,
}: {
  entries: RecolourEntry[];
  current: string;
  estimates: Record<string, RecolourExpect>;
  onEdit: (e: RecolourEntry) => void;
  onRemove: (material: string) => void;
  onMethod: (material: string, m: RecolourMethod | undefined) => void;
}): ReactElement {
  return (
    <table style={E.table} data-testid="recolour-pass-table">
      <thead>
        <tr>
          <th style={E.th}>material</th>
          <th style={E.th}>colour</th>
          <th style={E.th}>method</th>
          <th style={E.th}>expected</th>
          <th style={E.th} aria-label="actions" />
        </tr>
      </thead>
      <tbody>
        {entries.map((e, i) => {
          const hex = targetHex(e.target);
          const est = estimates[e.material];
          const de = est?.expectedDe00 ?? null;
          return (
            <tr key={e.material} data-testid="recolour-pass-row" data-material={e.material} aria-current={e.material === current ? "true" : undefined}>
              <td style={E.td}>
                {e.materialName ?? e.material}
                {e.materialName ? <span style={E.note}> ({e.material})</span> : null}
              </td>
              <td style={E.td}>
                <span aria-hidden style={{ display: "inline-block", width: 14, height: 10, marginRight: 4, borderRadius: 2, border: `1px solid ${C.line}`, background: hex ?? "transparent" }} />
                <span data-testid={`recolour-pass-target-${i}`}>{targetLabel(e.target)}</span>
                {e.source_lab ? <span style={E.note}> · source L a b {labWords(e.source_lab)}</span> : null}
              </td>
              <td style={E.td}>
                <select
                  style={E.input}
                  value={e.method ?? ""}
                  aria-label={`method for ${e.materialName ?? e.material}`}
                  data-testid={`recolour-pass-method-${i}`}
                  onChange={(ev) => onMethod(e.material, isMethod(ev.target.value) ? ev.target.value : undefined)}
                >
                  <option value="">default</option>
                  {RECOLOUR_METHODS.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              </td>
              <td style={E.td}>
                {est ? (
                  <Chip tone={de === null ? "muted" : de <= 1 ? "pass" : de <= 2.5 ? "advisory" : "fail"} testId={`recolour-pass-de-${i}`} title={est.method ?? undefined}>
                    ΔE00 {de === null ? "n/a" : de.toFixed(2)}
                  </Chip>
                ) : (
                  <span style={E.note}>not estimated</span>
                )}
              </td>
              <td style={E.td}>
                <div style={E.row}>
                  <Button onClick={() => onEdit(e)} testId={`recolour-pass-edit-${i}`} title="load this material and colour above">
                    Edit
                  </Button>
                  <Button onClick={() => onRemove(e.material)} testId={`recolour-pass-remove-${i}`} title="take this material out of the pass">
                    Remove
                  </Button>
                </div>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function RecolourEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const plate = usePlate(nodeId);
  const projectId = useGraphStore((s) => s.projectId);
  const [text, setText] = useState(asString(params.target));
  const [expect, setExpect] = useState<RecolourExpect | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [sourceLabText, setSourceLabText] = useState(() => {
    const src = lab3(params.source_lab);
    return src ? labWords(src) : "";
  });
  const [passEstimates, setPassEstimates] = useState<Record<string, RecolourExpect>>({});

  const size = plate?.size ?? null;
  const packetId = plate?.packetId ?? null;
  const material = asString(params.material);
  const entries = readEntries(params.materials);
  const inPass = entries.some((e) => e.material === material);
  const sourceLab = lab3(params.source_lab);
  const parsedLab = parseSourceLab(sourceLabText);
  const defaultMethod = isMethod(params.method) ? params.method : "";
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
    setNote(null);
    const listed = readEntries(useGraphStore.getState().nodes.find((n) => n.id === nodeId)?.data.params?.materials).find((e) => e.material === r.id);
    if (listed) loadEntry({ ...listed, materialName: listed.materialName ?? r.name });
    else set({ material: r.id, materialName: r.name });
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

  // While the picked material is in the pass, its entry follows the colour and the source Lab typed above.
  const withEntry = (edit: (e: RecolourEntry) => RecolourEntry): { materials?: RecolourEntry[] } =>
    inPass ? { materials: entries.map((e) => (e.material === material ? edit(e) : e)) } : {};

  const onText = (v: string): void => {
    setText(v);
    const r = v.trim() === "" ? null : parseTarget(v);
    if (r && r.ok) set({ target: r.target.label, targetHex: r.target.hex, ...withEntry((e) => ({ ...e, target: r.target.label })) });
    else set({ target: undefined, targetHex: undefined });
  };

  // a picked swatch: a hex goes into the field like typed text; a Lab keeps its exact numbers in `params.target`
  const applySwatch = (s: Swatch): void => {
    if (s.lab) {
      setText("");
      const t = swatchTarget(s);
      set({ target: t, targetHex: swatchHex(s), ...withEntry((e) => ({ ...e, target: t })) });
    } else {
      onText(swatchHex(s));
    }
  };

  const onSourceLab = (v: string): void => {
    setSourceLabText(v);
    const lab = parseSourceLab(v);
    if (lab === null) return; // shown as an error; the node keeps its last good value
    set({ source_lab: lab, ...withEntry((e) => withField(e, "source_lab", lab)) });
  };

  // an entry back into the fields above: its material, its colour (a Lab swatch stays in params.target) and its Lab
  function loadEntry(e: RecolourEntry): void {
    setText(typeof e.target === "string" ? e.target : "");
    setSourceLabText(e.source_lab ? labWords(e.source_lab) : "");
    set({ material: e.material, materialName: e.materialName, target: e.target, targetHex: targetHex(e.target) ?? undefined, source_lab: e.source_lab });
  }

  const currentTarget = targetOf(params.target);
  const canAdd = MATERIAL_ID.test(material) && currentTarget !== null && (inPass || entries.length < MAX_MATERIALS);
  const addToPass = (): void => {
    if (!canAdd || currentTarget === null) return;
    const entry: RecolourEntry = { material, target: currentTarget };
    const name = asString(params.materialName);
    if (name) entry.materialName = name;
    const old = entries.find((e) => e.material === material);
    if (old?.method) entry.method = old.method;
    if (sourceLab) entry.source_lab = sourceLab;
    set({ materials: inPass ? entries.map((e) => (e.material === material ? entry : e)) : [...entries, entry] });
  };
  const writeEntries = (next: RecolourEntry[]): void => {
    set({ materials: next.length > 0 ? next : undefined });
    setPassEstimates({});
  };

  const [estimatePass, passState] = useAction(async (list: RecolourEntry[]) => {
    if (!projectId) return;
    const body = await preview(projectId, nodeId, "recolour", {
      materials: list.map(({ materialName: _name, ...rest }) => rest),
      ...(defaultMethod ? { method: defaultMethod } : {}),
    });
    const rows = typeof body === "object" && body !== null && Array.isArray((body as { materials?: unknown }).materials) ? (body as { materials: unknown[] }).materials : [];
    const next: Record<string, RecolourExpect> = {};
    for (const row of rows) {
      const id = typeof row === "object" && row !== null ? (row as { material?: unknown }).material : null;
      if (typeof id === "string") next[id] = parseRecolourExpect(row);
    }
    setPassEstimates(next);
  }, errorMessage);

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

      <Section title="Method and source" testId="recolour-method-section">
        <Field label="method">
          <select style={E.input} value={defaultMethod} data-testid="recolour-method-default" aria-label="method" onChange={(e) => set({ method: isMethod(e.target.value) ? e.target.value : undefined })}>
            <option value="">auto (the packet's passes decide)</option>
            {RECOLOUR_METHODS.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </Field>
        <Field label="source Lab">
          <input
            type="text"
            style={{ ...E.input, width: 150 }}
            value={sourceLabText}
            placeholder="L a b (optional)"
            aria-label="source albedo Lab"
            data-testid="recolour-source-lab"
            onChange={(e) => onSourceLab(e.target.value)}
          />
          <span style={E.note}>the material's own colour, when the packet has no diffuse_filter pass and the legend no albedo</span>
        </Field>
        {parsedLab === null ? (
          <Notice tone="error" testId="recolour-source-lab-error">
            source Lab is three numbers: L 0 to 100, a and b -160 to 160
          </Notice>
        ) : null}
        <span style={E.note}>method is the default for every material of the pass that names none</span>
      </Section>

      <Section title={`Pass (${entries.length} of ${MAX_MATERIALS} materials)`} testId="recolour-pass">
        {entries.length === 0 ? (
          <span style={E.note} data-testid="recolour-pass-empty">
            one material: the pair above. Add it to recolour up to {MAX_MATERIALS} materials in one run.
          </span>
        ) : (
          <>
            <PassSection
              entries={entries}
              current={material}
              estimates={passEstimates}
              onEdit={loadEntry}
              onRemove={(m) => writeEntries(entries.filter((e) => e.material !== m))}
              onMethod={(m, method) => writeEntries(entries.map((e) => (e.material === m ? withField(e, "method", method) : e)))}
            />
            <span style={E.note} data-testid="recolour-pass-note">
              the run recolours the {entries.length} listed material{entries.length === 1 ? "" : "s"}
              {material && !inPass ? `; ${asString(params.materialName, material)} above is not in the pass until you add it` : ""}
            </span>
          </>
        )}
        <div style={E.row}>
          <Button onClick={addToPass} disabled={!canAdd} testId="recolour-pass-add" title={canAdd ? undefined : !MATERIAL_ID.test(material) ? "pick a material on the plate first" : currentTarget === null ? "give the material a colour first" : `a pass holds at most ${MAX_MATERIALS} materials`}>
            {inPass ? "Update in the pass" : "Add to the pass"}
          </Button>
          <Button onClick={() => void estimatePass(entries)} disabled={!projectId || entries.length === 0 || passState.pending} testId="recolour-pass-estimate">
            {passState.pending ? "Estimating" : "Estimate the pass"}
          </Button>
          <Button onClick={() => writeEntries([])} disabled={entries.length === 0} testId="recolour-pass-clear">
            Clear the pass
          </Button>
        </div>
        {passState.error ? (
          <Notice tone="error" testId="recolour-pass-error">
            {passState.error}
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
