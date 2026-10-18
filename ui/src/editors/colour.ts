// Colour helpers for the Recolour and Set editors: the built-in RAL table, target parsing (RAL code or hex), and sRGB
// <-> Lab for swatches. The RAL values are the published sRGB approximations `plate_gates.recolour.RAL_SRGB` carries
// (the official RAL Lab values are licensed, so a job enters the swatch's measured Lab or the hex the material was
// built from). `RAL_SRGB` is those 15 codes; the whole RAL Classic table for the picker is `ralTable.ts`, and
// `parseTarget` reads any of its codes. Display only: the daemon owns the recolour math. T112 adds the sRGB <-> linear
// pair the Atmosphere editor's fog colour needs, vector (`[r, g, b]`) LightMix gains and the full mirror of the
// daemon's mood presets.

import { RAL_BY_CODE } from "./ralTable";

export const RAL_SRGB: Readonly<Record<string, string>> = {
  "1015": "#E6D2B5",
  "1023": "#F7B500",
  "3000": "#A72920",
  "3020": "#CC0605",
  "5002": "#00387B",
  "6005": "#114232",
  "6018": "#57A639",
  "7016": "#383E42",
  "7021": "#2F3234",
  "7024": "#45494E",
  "7035": "#CBD0CC",
  "7037": "#7A7B7A",
  "9005": "#0E0E10",
  "9010": "#F1ECE1",
  "9016": "#F1F0EA",
};

/** `#RGB`, `#RRGGBB`, `RRGGBB` -> `#RRGGBB` upper case; null when it is not a hex colour. */
export function normalizeHex(text: string): string | null {
  const m = /^\s*#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\s*$/.exec(text);
  if (!m) return null;
  let h = (m[1] ?? "").toUpperCase();
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  return `#${h}`;
}

/** What a target field holds. `label` is the normalised text kept on the node (`RAL 7016` or `#383E42`). */
export type Target = { kind: "ral"; code: string; hex: string; label: string } | { kind: "hex"; hex: string; label: string };

export type TargetResult = { ok: true; target: Target } | { ok: false; reason: string };

/** Read `RAL 7016`, `ral7016`, `7016` (a code in the table) or a hex colour. */
export function parseTarget(text: string): TargetResult {
  const raw = text.trim();
  if (raw === "") return { ok: false, reason: "enter a RAL code or a hex colour" };
  const ral = /^(?:ral)?\s*(\d{4})$/i.exec(raw);
  if (ral && ral[1] !== undefined) {
    const code = ral[1];
    const hex = RAL_SRGB[code] ?? RAL_BY_CODE.get(code)?.hex;
    if (hex) return { ok: true, target: { kind: "ral", code, hex, label: `RAL ${code}` } };
    // four hex digits are not a colour, so a four-digit number that is not in the table is an unknown RAL code
    return { ok: false, reason: `RAL ${code} is not in the built-in table: enter its hex or a measured swatch` };
  }
  const hex = normalizeHex(raw);
  if (hex) return { ok: true, target: { kind: "hex", hex, label: hex } };
  return { ok: false, reason: "not a RAL code or a hex colour (#RGB or #RRGGBB)" };
}

const srgbToLinear = (c: number): number => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const linearToSrgb = (c: number): number => (c <= 0.0031308 ? c * 12.92 : 1.055 * c ** (1 / 2.4) - 0.055);

// D65 white
const XN = 0.95047;
const YN = 1.0;
const ZN = 1.08883;

const f = (t: number): number => (t > 216 / 24389 ? Math.cbrt(t) : (24389 / 27 * t + 16) / 116);
const finv = (t: number): number => (t ** 3 > 216 / 24389 ? t ** 3 : (116 * t - 16) / (24389 / 27));

/** `#RRGGBB` (sRGB) -> CIE Lab D65. */
export function hexToLab(hex: string): [number, number, number] {
  const n = normalizeHex(hex) ?? "#000000";
  const r = srgbToLinear(parseInt(n.slice(1, 3), 16) / 255);
  const g = srgbToLinear(parseInt(n.slice(3, 5), 16) / 255);
  const b = srgbToLinear(parseInt(n.slice(5, 7), 16) / 255);
  const x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / XN;
  const y = (0.2126729 * r + 0.7151522 * g + 0.072175 * b) / YN;
  const z = (0.0193339 * r + 0.119192 * g + 0.9503041 * b) / ZN;
  const fx = f(x);
  const fy = f(y);
  const fz = f(z);
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
}

/** CIE Lab D65 -> `#RRGGBB` (clipped to the sRGB gamut). */
export function labToHex(lab: readonly [number, number, number]): string {
  const fy = (lab[0] + 16) / 116;
  const x = XN * finv(fy + lab[1] / 500);
  const y = YN * finv(fy);
  const z = ZN * finv(fy - lab[2] / 200);
  const lin = [3.2404542 * x - 1.5371385 * y - 0.4985314 * z, -0.969266 * x + 1.8760108 * y + 0.041556 * z, 0.0556434 * x - 0.2040259 * y + 1.0572252 * z];
  const byte = (c: number): string =>
    Math.round(255 * Math.min(1, Math.max(0, linearToSrgb(Math.min(1, Math.max(0, c))))))
      .toString(16)
      .padStart(2, "0");
  return `#${lin.map(byte).join("").toUpperCase()}`;
}

/** `L 54.2  a 3.1  b -2.0`. */
export const labText = (lab: readonly [number, number, number]): string => `L ${lab[0].toFixed(1)}  a ${lab[1].toFixed(1)}  b ${lab[2].toFixed(1)}`;

// ------------------------------------------------------------------------------------------------ project swatches

/** A colour the project measured or was given, kept in this browser: a hex or a Lab (or both) plus where it came from. */
export interface Swatch {
  name: string;
  hex?: string;
  lab?: [number, number, number];
  /** How it was measured (instrument, date, light), free text. */
  note?: string;
}

/** Where a project's swatches live: `localStorage["plate.swatches.<projectId>"]`, a JSON list of `Swatch`. */
export const swatchStorageKey = (projectId: string): string => `plate.swatches.${projectId}`;

/** The colour to show for a swatch: its hex, else its Lab converted. */
export const swatchHex = (s: Swatch): string => (s.hex ? s.hex : s.lab ? labToHex(s.lab) : "#000000");

const isLab = (v: unknown): v is [number, number, number] => Array.isArray(v) && v.length === 3 && v.every((n) => typeof n === "number" && Number.isFinite(n));

function toSwatch(v: unknown): Swatch | null {
  if (typeof v !== "object" || v === null) return null;
  const o = v as Record<string, unknown>;
  if (typeof o.name !== "string" || o.name.trim() === "") return null;
  const hex = typeof o.hex === "string" ? normalizeHex(o.hex) : null;
  const lab = isLab(o.lab) ? ([o.lab[0], o.lab[1], o.lab[2]] as [number, number, number]) : null;
  if (!hex && !lab) return null;
  const out: Swatch = { name: o.name.trim() };
  if (hex) out.hex = hex;
  if (lab) out.lab = lab;
  if (typeof o.note === "string" && o.note.trim() !== "") out.note = o.note.trim();
  return out;
}

/** The project's swatches; an unreadable or blocked store is an empty list, and an entry that is not a swatch is dropped. */
export function loadSwatches(projectId: string | null): Swatch[] {
  if (!projectId) return [];
  try {
    const raw = window.localStorage.getItem(swatchStorageKey(projectId));
    const data: unknown = raw === null ? [] : JSON.parse(raw);
    if (!Array.isArray(data)) return [];
    return data.map(toSwatch).filter((s): s is Swatch => s !== null);
  } catch {
    return [];
  }
}

/** Write the list; false when the browser refuses (storage blocked or full). */
export function saveSwatches(projectId: string, swatches: readonly Swatch[]): boolean {
  try {
    window.localStorage.setItem(swatchStorageKey(projectId), JSON.stringify(swatches));
    return true;
  } catch {
    return false;
  }
}

export type SwatchResult = { ok: true; swatch: Swatch } | { ok: false; reason: string };

/**
 * A swatch from the add form. `value` is a hex (`#383E42`) or a Lab: three numbers, with or without the letters
 * (`54.2 3.1 -2.0`, `L 54.2 a 3.1 b -2.0`, `54.2, 3.1, -2.0`). A Lab keeps its exact numbers; its hex is only for display.
 */
export function readSwatchInput(name: string, value: string, note: string): SwatchResult {
  const label = name.trim();
  if (label === "") return { ok: false, reason: "name the swatch" };
  const out: Swatch = { name: label };
  const hex = normalizeHex(value);
  if (hex) out.hex = hex;
  else {
    const nums = value.match(/[-+]?\d+(?:\.\d+)?/g)?.map(Number) ?? [];
    const [L, a, b] = nums;
    if (nums.length !== 3 || L === undefined || a === undefined || b === undefined || !nums.every(Number.isFinite)) {
      return { ok: false, reason: "enter a hex (#383E42) or a Lab as three numbers (L a b)" };
    }
    if (L < 0 || L > 100) return { ok: false, reason: "L is between 0 and 100" };
    if (Math.abs(a) > 160 || Math.abs(b) > 160) return { ok: false, reason: "a and b are between -160 and 160" };
    out.lab = [L, a, b];
  }
  const n = note.trim();
  if (n !== "") out.note = n;
  return { ok: true, swatch: out };
}

/** What a picked swatch writes to `params.target`: its hex, or for a Lab the `{lab, label}` form the daemon reads. */
export const swatchTarget = (s: Swatch): string | { lab: [number, number, number]; label: string } =>
  s.lab ? { lab: s.lab, label: s.name } : swatchHex(s);

/** `{lab, label}` as `swatchTarget` wrote it, else null. */
export function labTargetOf(v: unknown): { lab: [number, number, number]; label: string } | null {
  if (typeof v !== "object" || v === null || Array.isArray(v)) return null;
  const o = v as Record<string, unknown>;
  return isLab(o.lab) ? { lab: [o.lab[0], o.lab[1], o.lab[2]], label: typeof o.label === "string" ? o.label : "lab" } : null;
}

// ------------------------------------------------------------------------------------------------ linear light

/** `#RRGGBB` (sRGB display) -> linear Rec.709 `[r, g, b]` in 0..1 (the sRGB EOTF; Atmosphere's fog colour). */
export function hexToLinear(hex: string): [number, number, number] {
  const n = normalizeHex(hex) ?? "#000000";
  return [1, 3, 5].map((i) => srgbToLinear(parseInt(n.slice(i, i + 2), 16) / 255)) as [number, number, number];
}

/** Linear Rec.709 `[r, g, b]` -> `#RRGGBB` (clipped to 0..1 first; for showing a linear colour as a swatch). */
export function linearToHex(rgb: readonly number[]): string {
  const byte = (c: number): string =>
    Math.round(255 * linearToSrgb(Math.min(1, Math.max(0, Number.isFinite(c) ? c : 0))))
      .toString(16)
      .padStart(2, "0");
  return `#${[0, 1, 2].map((i) => byte(rgb[i] ?? 0)).join("").toUpperCase()}`;
}

// ------------------------------------------------------------------------------------------------ LightMix presets

/** One light's gain, as `plate_gates.lightmix` takes it: a number, or an `[r, g, b]` vector (each >= 0). */
export type Gain = number | readonly [number, number, number];

/** A gain read from params: a finite number >= 0 or three of them; null for anything else. */
export function readGain(v: unknown): Gain | null {
  if (typeof v === "number") return Number.isFinite(v) && v >= 0 ? v : null;
  if (Array.isArray(v) && v.length === 3 && v.every((c) => typeof c === "number" && Number.isFinite(c) && c >= 0)) {
    return [v[0] as number, v[1] as number, v[2] as number];
  }
  return null;
}

/**
 * Mood presets for the LightMix gain sliders: a mirror of `plate_gates.lightmix.mood_presets` (the names the daemon's
 * `params.preset` accepts, in its order, with its patterns and gains). Initial guesses, never a solution. The daemon
 * has no preset route, so `nodeParamPanels.test.tsx` pins this table to `gates/plate_gates/lightmix.py`.
 */
export const MOOD_PRESETS: Readonly<Record<string, Readonly<Record<string, Gain>>>> = {
  dusk: { sun: 0.15, sky: 0.6, "interior_*": 3.0 },
  moonlight: { sun: 0.0, sky: 0.08, "interior_*": 0.6, "facade_*": 0.3 },
  luxury_exterior: { sun: 0.0, sky: 0.15, "interior_*": 0.0, "facade_*": 2.5, "landscape_*": 1.5 },
  artificial_light: { sun: 0.0, sky: 0.05, "interior_*": 3.0, "facade_*": 1.0, "landscape_*": 1.0 },
};

/** How a preset name reads on a button: `luxury_exterior` -> `luxury exterior`. */
export const presetLabel = (name: string): string => name.replace(/_/g, " ");

const globToRegExp = (glob: string): RegExp =>
  new RegExp(`^${glob.toLowerCase().replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".")}$`);

/**
 * `lightmix.preset_gains`: each light takes the gain of the first pattern that matches it (case insensitive), else
 * `fallback`. A vector gain is passed through as a new `[r, g, b]`, never collapsed to one number.
 */
export function presetGains(preset: Readonly<Record<string, Gain>>, names: readonly string[], fallback: Gain = 1): Record<string, Gain> {
  const copy = (g: Gain): Gain => (typeof g === "number" ? g : [g[0], g[1], g[2]]);
  const out: Record<string, Gain> = {};
  const entries = Object.entries(preset).map(([pat, gain]) => [globToRegExp(pat), gain] as const);
  for (const name of names) {
    out[name] = copy(fallback);
    for (const [re, gain] of entries) {
      if (re.test(name.toLowerCase())) {
        out[name] = copy(gain);
        break;
      }
    }
  }
  return out;
}
