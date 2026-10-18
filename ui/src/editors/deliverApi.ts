// Daemon calls behind clip delivery, batch export and social cutdowns (T106), plus the media facts of a run the Export
// editor, the player and VideoCompare read. Every call goes through the token-carrying `fetchJson`; every response is
// normalised here, so the editors see small typed shapes and never throw on a field a daemon version omits.
//
//   POST /api/v0/runs/{id}/deliver {format, out_dir, name?, override_reason?}      -> {path, sidecar, format}     (T74)
//        format: prores4444 (.mov) | hevc (.mp4) | exr_sequence (<name>.%04d.exr) | fcpxml (.fcpxml); a still goes
//        out only as exr_sequence (the video formats answer 422 not_a_clip). GATE_FAIL needs a recorded override or
//        an `override_reason` of 10+ characters, which the daemon ledgers before the encode starts.
//   POST /api/v0/projects/{id}/export {run_ids?, accepted?, out_dir, format?, name_pattern?}
//                                                     -> {exported: [{run_id, path, sidecar}], refused: [{run_id,
//                                                        error, detail, code?}]}                                 (T86)
//        each run through `export.export` with the single route's guards; one refusal never stops the others.
//   POST /api/v0/runs/{id}/cutdown {preset, cx?, cy?, out_dir, name?, format?, codec?, long_side?}
//                                                     -> {path, sidecar, box: [x0, y0, w, h]}                     (T86)
//        a downsample-only crop of the approved master (decision 56): 8-bit sRGB png | jpeg for a still, h264 | hevc
//        BT.709 for a clip, inside the packet's safe frame; guarded like deliver.
//   GET  /api/v0/projects/{pid}/runs[?node_id=]       -> run views, newest first, read here as `TakeItem`s        (T57)

import { fetchJson, postJson } from "../api";
import { asList } from "../panels/api";
import { type ExportFormat, parseRunItem, type RunItem } from "./nodeApi";
import { parseTimeMap, type TimeMap } from "./timeMap";

const enc = encodeURIComponent;
const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v: unknown): string | null => (typeof v === "string" && v !== "" ? v : null);

// ---------------------------------------------------------------------------------------------------- takes

/** The four ways the editor delivers a clip (`POST /runs/{id}/deliver`). The route's fifth, `av1_retag` (the lossless
 * colour re-tag of a Vantage 3.3 AV1 export), is a repair, not a delivery, and is left to `plate deliver`. */
export const CLIP_FORMATS = ["prores4444", "hevc", "exr_sequence", "fcpxml"] as const;
export type ClipFormat = (typeof CLIP_FORMATS)[number];

export const CLIP_FORMAT_NOTES: Readonly<Record<ClipFormat, string>> = {
  prores4444: "ProRes 4444 .mov, the grading master",
  hevc: "HEVC .mp4 with BT.709 colour tags",
  exr_sequence: "EXR frame sequence, <name>.0001.exr onwards",
  fcpxml: "FCPXML 1.10 timeline laying the clip on the spine",
};

export const isClipFormat = (v: unknown): v is ClipFormat => typeof v === "string" && (CLIP_FORMATS as readonly string[]).includes(v);

/** A run as the delivery surfaces read it: the T69 run item plus what kind of take it is. */
export interface TakeItem extends RunItem {
  /** `video` when the take is a clip (see `isClipRun`), else `still`. */
  media: "video" | "still";
  /** The frame-rate time map the video gates used (`normalized.timemap`), when the run records one. */
  timeMap: TimeMap | null;
  /** The run carries an `accept` note (a human accepted it after a full scrub). */
  accepted: boolean;
  /** A previs run's clay reference clip (`inputs.reference_videos[0].key`): the motion packet's clay sequence proxy. */
  clayKey: string | null;
  /** A previs take (watermarked PREVIS): Export refuses it, Review may show it. */
  previs: boolean;
}

const CLIP_SUFFIX = /\.(mp4|mov|webm)$/i;

/**
 * A run whose take is a clip: `outputs[0]` ends `.mp4` (or `.mov` / `.webm`), or `normalized.media` is `video`, or a
 * time map is recorded (only motion takes have one). Pure.
 */
export function isClipRun(raw: unknown): boolean {
  if (!isRecord(raw)) return false;
  const norm = isRecord(raw.normalized) ? raw.normalized : {};
  const outputs = Array.isArray(raw.outputs) ? raw.outputs : Array.isArray(norm.outputs) ? norm.outputs : [];
  const first = outputs[0];
  if (typeof first === "string" && CLIP_SUFFIX.test(first)) return true;
  if (norm.media === "video" || raw.media === "video") return true;
  return isRecord(norm.timemap) || isRecord(raw.timemap);
}

function clayKeyOf(raw: Record<string, unknown>): string | null {
  const inputs = isRecord(raw.inputs) ? raw.inputs : {};
  const videos = Array.isArray(inputs.reference_videos) ? inputs.reference_videos : [];
  const first: unknown = videos[0];
  return isRecord(first) ? str(first.key) : str(first);
}

/** One run view as a take; null when it names no run. */
export function parseTake(raw: unknown): TakeItem | null {
  const base = parseRunItem(raw);
  if (!base || !isRecord(raw)) return null;
  const norm = isRecord(raw.normalized) ? raw.normalized : {};
  const inputs = isRecord(raw.inputs) ? raw.inputs : {};
  return {
    ...base,
    media: isClipRun(raw) ? "video" : "still",
    timeMap: parseTimeMap(norm.timemap ?? raw.timemap),
    accepted: isRecord(raw.accepted) || raw.accepted === true,
    clayKey: clayKeyOf(raw),
    previs: norm.previs === true || inputs.previs === true,
  };
}

/** `GET /projects/{pid}/runs[?node_id=]` as takes, newest first. */
export async function listTakes(projectId: string, nodeId?: string | null): Promise<TakeItem[]> {
  const q = nodeId ? `?node_id=${enc(nodeId)}` : "";
  const body = await fetchJson<unknown>(`/projects/${enc(projectId)}/runs${q}`);
  return asList<unknown>(body, "runs").flatMap((r) => parseTake(r) ?? []);
}

// ---------------------------------------------------------------------------------------------------- deliver

export interface DeliverBody {
  format: ClipFormat;
  /** An absolute folder on the daemon's machine. */
  out_dir: string;
  /** File stem; the daemon defaults to the run id. Letters, digits, `.`, `_` and `-` only. */
  name?: string;
  /** A GATE_FAIL take: deliver anyway, with this reason (10+ characters) ledgered as an `override` record. */
  override_reason?: string;
}

export interface DeliverResult {
  path: string | null;
  sidecar: string | null;
  format: string | null;
}

/** `POST /runs/{id}/deliver`: the take as `format` into `out_dir`, lineage inside the file, a `.gate.json` beside it. */
export async function deliverRun(runId: string, body: DeliverBody): Promise<DeliverResult> {
  const r = await postJson<unknown>(`/runs/${enc(runId)}/deliver`, body);
  const o = isRecord(r) ? r : {};
  return { path: str(o.path), sidecar: str(o.sidecar), format: str(o.format) };
}

// ---------------------------------------------------------------------------------------------------- batch export

export interface ExportManyBody {
  /** The takes to export; with `accepted`, any of them without an `accept` note is refused. */
  run_ids?: string[];
  /** Without `run_ids`: every run of the project with an `accept` note. */
  accepted?: boolean;
  out_dir: string;
  /** `tiff16` when omitted. */
  format?: ExportFormat;
  /** `{node}_{run8}` when omitted; `{node} {run} {run8} {packet} {engine} {n}`. */
  name_pattern?: string;
}

export interface ExportedFile {
  runId: string | null;
  path: string | null;
  sidecar: string | null;
}

export interface RefusedRun {
  runId: string | null;
  error: string;
  detail: string | null;
  code: string | null;
}

export interface ExportManyResult {
  exported: ExportedFile[];
  refused: RefusedRun[];
}

export function parseExportMany(body: unknown): ExportManyResult {
  const o = isRecord(body) ? body : {};
  const list = (v: unknown): Record<string, unknown>[] => (Array.isArray(v) ? v.filter(isRecord) : []);
  return {
    exported: list(o.exported).map((e) => ({ runId: str(e.run_id), path: str(e.path), sidecar: str(e.sidecar) })),
    refused: list(o.refused).map((e) => ({ runId: str(e.run_id), error: str(e.error) ?? "refused", detail: str(e.detail), code: str(e.code) })),
  };
}

/** `POST /projects/{id}/export`: many takes through `export.export`, each guarded like the single export. */
export async function exportMany(projectId: string, body: ExportManyBody): Promise<ExportManyResult> {
  return parseExportMany(await postJson<unknown>(`/projects/${enc(projectId)}/export`, body));
}

// ---------------------------------------------------------------------------------------------------- cutdown

export const CUTDOWN_PRESETS = ["9:16", "4:5", "1:1", "16:9"] as const;
export type CutdownPreset = (typeof CUTDOWN_PRESETS)[number];
export const CUTDOWN_FORMATS = ["png", "jpeg"] as const;
export type CutdownFormat = (typeof CUTDOWN_FORMATS)[number];
export const CUTDOWN_CODECS = ["h264", "hevc"] as const;
export type CutdownCodec = (typeof CUTDOWN_CODECS)[number];

export const isCutdownPreset = (v: unknown): v is CutdownPreset => typeof v === "string" && (CUTDOWN_PRESETS as readonly string[]).includes(v);

export interface CutdownBody {
  preset: CutdownPreset;
  /** Crop centre as fractions of the frame (0.5 when omitted). */
  cx?: number;
  cy?: number;
  out_dir: string;
  /** File stem; the daemon defaults to `<run id>_<preset with x for :>`. */
  name?: string;
  /** Stills only. */
  format?: CutdownFormat;
  /** Clips only. */
  codec?: CutdownCodec;
  /** Shrink the crop's long side to this; a cutdown never upscales (422 `cutdown_error`). */
  long_side?: number;
}

export interface CutdownResult {
  path: string | null;
  sidecar: string | null;
  /** The crop in source pixels, `[x0, y0, w, h]`. */
  box: [number, number, number, number] | null;
}

export function parseCutdown(body: unknown): CutdownResult {
  const o = isRecord(body) ? body : {};
  const b = Array.isArray(o.box) && o.box.length === 4 && o.box.every((v) => num(v) !== null) ? (o.box as [number, number, number, number]) : null;
  return { path: str(o.path), sidecar: str(o.sidecar), box: b };
}

/** `POST /runs/{id}/cutdown`: a social crop of the approved master, lineage and the IPTC AI marker inside. */
export async function cutdownRun(runId: string, body: CutdownBody): Promise<CutdownResult> {
  return parseCutdown(await postJson<unknown>(`/runs/${enc(runId)}/cutdown`, body));
}
