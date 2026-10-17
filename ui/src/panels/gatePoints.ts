// The gate-flagged points (image pixels) behind "Open tiles". The daemon may send them beside the report as
// `flagged_points` (bare [x, y] pairs or {x, y} objects are fine there), or they ride inside the metrics the way
// `plate_gates.invention.Flag.to_dict` writes them: objects carrying `centroid: [x, y]` or `bbox: [x0, y0, x1, y1]`.
// Inside the metrics only those two shapes count, because a bare number pair there can be a shift vector, not a flag.

import type { GateReport, Point } from "./types";

/** The viewer lays out at most this many tiles legibly; more points than this are cut to the first ones. */
export const MAX_TILE_POINTS = 12;

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

function flagShape(item: unknown): Point | null {
  if (!isRecord(item)) return null;
  const c = item.centroid;
  if (Array.isArray(c) && c.length === 2 && isNum(c[0]) && isNum(c[1])) return { x: c[0], y: c[1] };
  const b = item.bbox;
  if (Array.isArray(b) && b.length === 4 && b.every(isNum)) return { x: (b[0] + b[2]) / 2, y: (b[1] + b[3]) / 2 };
  return null;
}

function explicitShape(item: unknown): Point | null {
  if (Array.isArray(item) && item.length === 2 && isNum(item[0]) && isNum(item[1])) return { x: item[0], y: item[1] };
  if (isRecord(item) && isNum(item.x) && isNum(item.y)) return { x: item.x, y: item.y };
  return flagShape(item);
}

function scanMetrics(value: unknown, depth: number, out: Point[]): void {
  if (Array.isArray(value)) {
    for (const item of value) {
      const p = flagShape(item);
      if (p) out.push(p);
      else if (depth > 0) scanMetrics(item, depth - 1, out);
    }
  } else if (isRecord(value)) {
    if (Array.isArray(value.flagged_points)) {
      for (const item of value.flagged_points) {
        const p = explicitShape(item);
        if (p) out.push(p);
      }
    }
    if (depth > 0) for (const v of Object.values(value)) scanMetrics(v, depth - 1, out);
  }
}

export function flaggedPoints(report: GateReport): Point[] {
  const out: Point[] = [];
  if (Array.isArray(report.flagged_points)) {
    for (const item of report.flagged_points) {
      const p = explicitShape(item);
      if (p) out.push(p);
    }
  }
  if (out.length === 0) scanMetrics(report.metrics, 3, out);
  const seen = new Set<string>();
  const unique: Point[] = [];
  for (const p of out) {
    const key = `${Math.round(p.x)},${Math.round(p.y)}`;
    if (!seen.has(key)) {
      seen.add(key);
      unique.push(p);
    }
  }
  return unique.slice(0, MAX_TILE_POINTS);
}
