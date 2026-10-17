// Daemon calls behind the artist-path editors (T69): the Plate packet picker, the Edit recipe list, Export and Review.
// Every call goes through the token-carrying `fetchJson`; every response is normalised here, so the editors see small
// typed shapes and never throw on a field a daemon version omits.
//
//   GET  /api/v0/projects/{pid}/packets[?status=all]      -> [{packet_id, shot_id, tier, kind, status, received_utc,
//                                                              camera_name, plate: {role, relpath, blake3, width, height},
//                                                              supersedes, superseded_by}]                         (T55)
//   GET  /api/v0/packets/{id}/summary                      -> {packet_id, size, plate, camera, legend: {entries}}  (T55)
//   GET  /api/v0/recipes                                   -> [{id, title, kind, engine, shot_class, library, mask, params}] (T61)
//   GET  /api/v0/projects/{pid}/runs[?node_id=]            -> run views, newest first                              (T57)
//   POST /api/v0/runs/{id}/export {out_dir, format, name?, override?, reason?}
//                                                          -> {run_id, path, sidecar, format, shipped}             (T57)
//   POST /api/v0/projects/{pid}/review {run_ids, watermark?, title?}  -> {zip_path, bytes, takes}                  (T74)
//
// Export does not need an accept (decision 39: it is the pre-accept hand-off); a GATE_FAIL take needs an override
// reason, which the daemon ledgers as an `override` record. Review refuses previs and GATE_FAIL-without-override.

import { fetchJson, postJson } from "../api";
import type { ImageSize, PlateTier } from "../nodeTypes";
import { asList } from "../panels/api";

const enc = encodeURIComponent;
const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v: unknown): string | null => (typeof v === "string" && v !== "" ? v : null);
const tierOf = (v: unknown): PlateTier | null => (v === "A" || v === "C" ? v : null);

// ---------------------------------------------------------------------------------------------------- packets

/** The display plate of a packet: `files.display_tiff16` on Tier A, `files.client_plate` on Tier C. */
export interface PacketPlate {
  role: string | null;
  relpath: string | null;
  /** BLAKE3 of the plate file; the daemon serves it by reference at `GET /assets/{blake3}` (decision 32). */
  blake3: string;
  width: number | null;
  height: number | null;
}

export interface PacketItem {
  packetId: string;
  shotId: string;
  tier: PlateTier | null;
  kind: string | null;
  status: string;
  receivedUtc: string | null;
  camera: string | null;
  plate: PacketPlate | null;
  supersedes: string | null;
  /** The packet in the same project that supersedes this one (a re-render of the same camera), else null. */
  supersededBy: string | null;
}

function parsePlate(raw: unknown): PacketPlate | null {
  if (!isRecord(raw)) return null;
  const blake3 = str(raw.blake3) ?? str(raw.hash);
  if (!blake3) return null;
  return { role: str(raw.role), relpath: str(raw.relpath), blake3, width: num(raw.width), height: num(raw.height) };
}

/** One row of `GET /projects/{pid}/packets`; null when it names no packet. */
export function parsePacket(raw: unknown): PacketItem | null {
  if (!isRecord(raw)) return null;
  const packetId = str(raw.packet_id) ?? str(raw.id);
  if (!packetId) return null;
  const camera = isRecord(raw.camera) ? str(raw.camera.name) : null;
  return {
    packetId,
    shotId: str(raw.shot_id) ?? packetId,
    tier: tierOf(raw.tier),
    kind: str(raw.kind),
    status: str(raw.status) ?? "ingested",
    receivedUtc: str(raw.received_utc),
    camera: str(raw.camera_name) ?? camera,
    plate: parsePlate(raw.plate),
    supersedes: str(raw.supersedes),
    supersededBy: str(raw.superseded_by),
  };
}

/** `GET /projects/{pid}/packets`: the project's packets, oldest first (the daemon orders by `received_utc`). */
export async function listPackets(projectId: string, status: "ingested" | "all" = "ingested"): Promise<PacketItem[]> {
  const q = status === "ingested" ? "" : `?status=${status}`;
  const body = await fetchJson<unknown>(`/projects/${enc(projectId)}/packets${q}`);
  return asList<unknown>(body, "packets").flatMap((p) => parsePacket(p) ?? []);
}

/** One legend entry of a packet: a Cryptomatte id (`o:<8hex>` object or `m:<8hex>` material) and its policy. */
export interface LegendEntry {
  id: string;
  kind: string | null;
  name: string;
  class: string | null;
  pixelShare: number | null;
  editable: boolean;
  locked: boolean;
  planar: boolean;
}

export interface PacketSummary {
  packetId: string;
  projectId: string | null;
  shotId: string | null;
  tier: PlateTier | null;
  status: string | null;
  size: ImageSize | null;
  plate: PacketPlate | null;
  supersedes: string | null;
  cameraName: string | null;
  legend: LegendEntry[];
}

function parseEntry(raw: unknown): LegendEntry | null {
  if (!isRecord(raw)) return null;
  const id = str(raw.id);
  if (!id) return null;
  return {
    id,
    kind: str(raw.kind),
    name: str(raw.name) ?? id,
    class: str(raw.class),
    pixelShare: num(raw.pixel_share),
    editable: raw.editable === true,
    locked: raw.locked === true,
    planar: raw.planar === true,
  };
}

export function parsePacketSummary(body: unknown): PacketSummary {
  const r = isRecord(body) ? body : {};
  const size = isRecord(r.size) && num(r.size.width) !== null && num(r.size.height) !== null ? { width: r.size.width as number, height: r.size.height as number } : null;
  const legend = isRecord(r.legend) ? r.legend.entries : r.legend;
  return {
    packetId: str(r.packet_id) ?? "",
    projectId: str(r.project_id),
    shotId: str(r.shot_id),
    tier: tierOf(r.tier),
    status: str(r.status),
    size,
    plate: parsePlate(r.plate),
    supersedes: str(r.supersedes),
    cameraName: isRecord(r.camera) ? str(r.camera.name) : null,
    legend: Array.isArray(legend) ? legend.flatMap((e) => parseEntry(e) ?? []) : [],
  };
}

/** `GET /packets/{id}/summary`: size, plate, camera and the legend with editable / locked / planar per id. */
export async function packetSummary(packetId: string): Promise<PacketSummary> {
  return parsePacketSummary(await fetchJson<unknown>(`/packets/${enc(packetId)}/summary`));
}

// ---------------------------------------------------------------------------------------------------- recipes

export interface RecipeItem {
  id: string;
  title: string;
  /** `still_edit`, `still_rephotograph` or `motion_croppair`. */
  kind: string;
  engine: string | null;
  shotClass: string | null;
  library: string | null;
  /** `mask.editable_classes`: what the recipe lets the engine change; everything else is composited back. */
  maskClasses: string[];
  region: string | null;
  params: Record<string, unknown>;
}

export function parseRecipe(raw: unknown): RecipeItem | null {
  if (!isRecord(raw)) return null;
  const id = str(raw.id);
  if (!id) return null;
  const mask = isRecord(raw.mask) ? raw.mask : {};
  const classes = Array.isArray(mask.editable_classes) ? mask.editable_classes.filter((c): c is string => typeof c === "string") : [];
  return {
    id,
    title: str(raw.title) ?? id,
    kind: str(raw.kind) ?? "",
    engine: str(raw.engine),
    shotClass: str(raw.shot_class),
    library: str(raw.library),
    maskClasses: classes,
    region: str(mask.region),
    params: isRecord(raw.params) ? raw.params : {},
  };
}

/** `GET /recipes`: the runnable recipes shipped with the daemon. */
export async function listRecipes(): Promise<RecipeItem[]> {
  return asList<unknown>(await fetchJson<unknown>("/recipes"), "recipes").flatMap((r) => parseRecipe(r) ?? []);
}

// ---------------------------------------------------------------------------------------------------- runs

export interface RunItem {
  id: string;
  nodeId: string | null;
  state: string;
  /** DONE, or GATE_FAIL with a ledgered override: what Export and Review take. */
  exportable: boolean;
  overrideReason: string | null;
  attempt: number | null;
  createdUtc: string | null;
  engineId: string | null;
  outputs: string[];
}

export function parseRunItem(raw: unknown): RunItem | null {
  if (!isRecord(raw)) return null;
  const id = str(raw.id) ?? str(raw.run_id);
  if (!id) return null;
  const state = str(raw.state) ?? "DRAFT";
  const override = isRecord(raw.override) ? raw.override : null;
  const overrideReason = override ? (str(override.reason) ?? "override logged") : null;
  const exportable = typeof raw.exportable === "boolean" ? raw.exportable : state === "DONE" || (state === "GATE_FAIL" && override !== null);
  const route = isRecord(raw.route) ? raw.route : {};
  return {
    id,
    nodeId: str(raw.node_id),
    state,
    exportable,
    overrideReason,
    attempt: num(raw.attempt),
    createdUtc: str(raw.created_utc),
    engineId: str(route.engine_id),
    outputs: Array.isArray(raw.outputs) ? raw.outputs.filter((o): o is string => typeof o === "string") : [],
  };
}

/** `GET /projects/{pid}/runs[?node_id=]`: run views, newest first. */
export async function listRuns(projectId: string, nodeId?: string | null): Promise<RunItem[]> {
  const q = nodeId ? `?node_id=${enc(nodeId)}` : "";
  const body = await fetchJson<unknown>(`/projects/${enc(projectId)}/runs${q}`);
  return asList<unknown>(body, "runs").flatMap((r) => parseRunItem(r) ?? []);
}

/** Takes that left the engine and have pixels: DONE first, then GATE_FAIL with an override, then GATE_FAIL. */
export function exportCandidates(runs: readonly RunItem[]): RunItem[] {
  const rank = (r: RunItem): number => (r.state === "DONE" ? 0 : r.exportable ? 1 : 2);
  return runs
    .filter((r) => r.state === "DONE" || r.state === "GATE_FAIL")
    .map((r, i) => ({ r, i }))
    .sort((a, b) => rank(a.r) - rank(b.r) || a.i - b.i)
    .map(({ r }) => r);
}

// ---------------------------------------------------------------------------------------------------- export, review

export const EXPORT_FORMATS = ["png16", "tiff16", "exr"] as const;
export type ExportFormat = (typeof EXPORT_FORMATS)[number];

export interface ExportBody {
  /** A folder on the daemon's machine; the daemon writes `<name>.<ext>` and `<name>.gate.json` into it. */
  out_dir: string;
  format: ExportFormat;
  name?: string;
  /** A GATE_FAIL take: export anyway, with `reason` ledgered as an `override` record. */
  override?: boolean;
  reason?: string;
}

export interface ExportResult {
  runId: string | null;
  path: string | null;
  sidecar: string | null;
  format: string | null;
  shipped: boolean;
}

export async function exportRun(runId: string, body: ExportBody): Promise<ExportResult> {
  const r = await postJson<unknown>(`/runs/${enc(runId)}/export`, body);
  const o = isRecord(r) ? r : {};
  return { runId: str(o.run_id), path: str(o.path), sidecar: str(o.sidecar), format: str(o.format), shipped: o.shipped === true };
}

export interface ReviewBody {
  run_ids: string[];
  watermark?: string;
  title?: string;
}

export interface ReviewResult {
  zipPath: string | null;
  bytes: number | null;
  takes: number | null;
}

export async function buildReview(projectId: string, body: ReviewBody): Promise<ReviewResult> {
  const r = await postJson<unknown>(`/projects/${enc(projectId)}/review`, body);
  const o = isRecord(r) ? r : {};
  return { zipPath: str(o.zip_path) ?? str(o.path), bytes: num(o.bytes), takes: num(o.takes) };
}
