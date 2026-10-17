// The frame-rate time map of a motion job (`plate_gates.timemap.TimeMap`, recorded on every motion job and carried in
// the video gate report as `metrics.timemap`): which fractional packet frame an engine frame shows.
//
//   packet_frame_for(j) = start + j * packet_fps / engine_fps        (unclamped)
//   engine_frame_for(k) = (k - start) * engine_fps / packet_fps
//   nearest             = round-half-up of packet_frame_for(j), clamped to [start, end]
//
// 25 -> 24 fps over 125 packet frames is 120 engine frames and packet_frame_for(119) = 123.9583. Frame rates arrive as
// text ("25/1", "24000/1001") or numbers; this module reads both into plain numbers (display math, not gate math).

export interface TimeMap {
  packetFps: number;
  engineFps: number;
  /** First and last packet frame, inclusive. */
  start: number;
  end: number;
  /** Nominal engine frames covering the shot. */
  engineFrames: number;
  durationS: number;
  method: string;
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/** `25`, `"25"`, `"24/1"`, `"24000/1001"`, `"23.976"` -> a positive number; null for anything else. */
export function parseFps(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) && v > 0 ? v : null;
  if (typeof v !== "string") return null;
  const m = /^\s*([0-9.]+)\s*(?:\/\s*([0-9.]+))?\s*$/.exec(v);
  if (!m) return null;
  const num = Number(m[1]);
  const den = m[2] === undefined ? 1 : Number(m[2]);
  const f = num / den;
  return Number.isFinite(f) && f > 0 ? f : null;
}

/** The report's `metrics.timemap` (`TimeMap.to_json()`) as numbers; null when it is missing or unreadable. */
export function parseTimeMap(raw: unknown): TimeMap | null {
  if (!isRecord(raw)) return null;
  const packetFps = parseFps(raw.packet_fps);
  const engineFps = parseFps(raw.engine_fps);
  const frames = raw.packet_frames;
  if (packetFps === null || engineFps === null || !Array.isArray(frames) || frames.length !== 2) return null;
  const start = Number(frames[0]);
  const end = Number(frames[1]);
  const engineFrames = Number(raw.engine_frames);
  if (![start, end, engineFrames].every(Number.isFinite) || end < start || engineFrames < 1) return null;
  const durationS = typeof raw.duration_s === "number" ? raw.duration_s : (end - start + 1) / packetFps;
  return { packetFps, engineFps, start, end, engineFrames, durationS, method: typeof raw.method === "string" ? raw.method : "nearest" };
}

/** Fractional packet frame shown at engine frame `j` (unclamped). */
export const packetFrameFor = (tm: TimeMap, j: number): number => tm.start + (j * tm.packetFps) / tm.engineFps;

/** Fractional engine frame at packet frame `k` (unclamped). */
export const engineFrameFor = (tm: TimeMap, k: number): number => ((k - tm.start) * tm.engineFps) / tm.packetFps;

const roundHalfUp = (x: number): number => (x >= 0 ? Math.floor(x + 0.5) : -Math.floor(-x + 0.5));

/** The packet frame whose Z / ids / clay stand in for engine frame `j` (method 'nearest', clamped to the packet). */
export const nearestPacketFrame = (tm: TimeMap, j: number): number =>
  Math.min(Math.max(roundHalfUp(packetFrameFor(tm, j)), tm.start), tm.end);

/** True when engine frame `j` lies inside the packet's frame range, so no clamping is needed. */
export const covers = (tm: TimeMap, j: number): boolean => {
  const k = packetFrameFor(tm, j);
  return tm.start <= k && k <= tm.end;
};

/** Frame index shown at `timeS` for a clip at `fps`; the epsilon keeps an exact frame boundary on its own frame. */
export const frameAt = (timeS: number, fps: number): number => Math.max(0, Math.floor(timeS * fps + 1e-6));

/** A time inside frame `frame`: browsers land on the frame whose interval holds the time, so aim at its middle. */
export const timeForFrame = (frame: number, fps: number): number => (Math.max(0, frame) + 0.5) / fps;
