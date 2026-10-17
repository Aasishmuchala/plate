// Colour helpers for the Recolour and Set editors: the built-in RAL table, target parsing (RAL code or hex), and sRGB
// <-> Lab for swatches. The RAL values are the published sRGB approximations `plate_gates.recolour.RAL_SRGB` carries
// (the official RAL Lab values are licensed, so a job enters the swatch's measured Lab or the hex the material was
// built from). Display only: the daemon owns the recolour math.

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
    const hex = RAL_SRGB[code];
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

/** Mood presets for the LightMix gain sliders (`plate_gates.lightmix.mood_presets`): initial guesses, never a solution. */
export const MOOD_PRESETS: Readonly<Record<string, Readonly<Record<string, number>>>> = {
  dusk: { sun: 0.15, sky: 0.6, "interior_*": 3.0 },
};

const globToRegExp = (glob: string): RegExp =>
  new RegExp(`^${glob.toLowerCase().replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".")}$`);

/** `lightmix.preset_gains`: each light takes the gain of the first pattern that matches it (case insensitive), else `fallback`. */
export function presetGains(preset: Readonly<Record<string, number>>, names: readonly string[], fallback = 1): Record<string, number> {
  const out: Record<string, number> = {};
  const entries = Object.entries(preset).map(([pat, gain]) => [globToRegExp(pat), gain] as const);
  for (const name of names) {
    out[name] = fallback;
    for (const [re, gain] of entries) {
      if (re.test(name.toLowerCase())) {
        out[name] = gain;
        break;
      }
    }
  }
  return out;
}
