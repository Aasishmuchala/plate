// Push-in crop boxes: `plate_gates.croppair` (amphi-plaza/cut_crops.py) in TypeScript, so the CropPair editor draws the
// exact rectangles the daemon will cut. Same geometry byte for byte: w = int(W / z) // 2 * 2 (even), centred on
// (cx, cy), clamped inside the frame, native pixels, no resampling. The zoom pair must satisfy 1.0 <= z0 < z1 <= 3.0.
// Reference case (relit 5504x3072 at (3633, 2174), 1.5x -> 2.3x): boxes (1799, 1024, 3668, 2048) and
// (2437, 1507, 2392, 1334) as (x0, y0, w, h); the vitest pins them.

export const Z_MIN = 1.0;
export const Z_MAX = 3.0;
/** The smallest gap the sliders keep between the two zooms. */
export const Z_STEP = 0.05;

/** A crop in source pixels. */
export interface CropBox {
  x0: number;
  y0: number;
  w: number;
  h: number;
}

/** The centred, clamped, even-sized crop at `zoom` (cut_crops geometry). */
export function boxFor(width: number, height: number, cx: number, cy: number, zoom: number): CropBox {
  const w = Math.floor(Math.trunc(width / zoom) / 2) * 2;
  const h = Math.floor(Math.trunc(height / zoom) / 2) * 2;
  const x0 = Math.trunc(Math.min(Math.max(cx - Math.floor(w / 2), 0), width - w));
  const y0 = Math.trunc(Math.min(Math.max(cy - Math.floor(h / 2), 0), height - h));
  return { x0, y0, w, h };
}

/** Null when 1.0 <= z0 < z1 <= 3.0, else the reason (the daemon raises the same ValueError). */
export function zoomError(z0: number, z1: number): string | null {
  if (!(Z_MIN <= z0 && z0 < z1 && z1 <= Z_MAX)) {
    return `zoom pair must satisfy ${Z_MIN.toFixed(1)} <= start < end <= ${Z_MAX.toFixed(1)}, got ${z0} -> ${z1}`;
  }
  return null;
}

export interface CropPlan {
  start: CropBox;
  end: CropBox;
  /** End zoom over start zoom: how far the move pushes in. */
  pushRatio: number;
}

/** Start and end boxes of a push-in; throws on an invalid zoom pair, like `cut_crops`. */
export function cutBoxes(width: number, height: number, cx: number, cy: number, z0: number, z1: number): CropPlan {
  const bad = zoomError(z0, z1);
  if (bad) throw new RangeError(bad);
  return { start: boxFor(width, height, cx, cy, z0), end: boxFor(width, height, cx, cy, z1), pushRatio: z1 / z0 };
}

const round2 = (v: number): number => Math.round(v * 100) / 100;
const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v));

/**
 * Apply one slider move and keep the pair valid: the moved zoom is clamped into its own range and the other one is
 * pushed along when they would meet (start in [1, 3 - step], end in [1 + step, 3], end >= start + step).
 */
export function moveZoom(z0: number, z1: number, which: "start" | "end", value: number): [number, number] {
  if (!Number.isFinite(value)) return [z0, z1];
  if (which === "start") {
    const s = round2(clamp(value, Z_MIN, Z_MAX - Z_STEP));
    return [s, round2(Math.max(z1, s + Z_STEP))];
  }
  const e = round2(clamp(value, Z_MIN + Z_STEP, Z_MAX));
  return [round2(Math.min(z0, e - Z_STEP)), e];
}

/** A box as CSS percentages of the image, for an absolutely positioned overlay. */
export function boxPercent(box: CropBox, width: number, height: number): { left: string; top: string; width: string; height: string } {
  const pct = (v: number, of: number): string => `${Number(((100 * v) / of).toFixed(4))}%`;
  return { left: pct(box.x0, width), top: pct(box.y0, height), width: pct(box.w, width), height: pct(box.h, height) };
}
