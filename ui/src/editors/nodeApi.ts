// Daemon calls behind the artist-path editors (T69): the Plate packet picker, the Edit recipe list, Export and Review.
// Every call goes through the token-carrying `fetchJson`; every response is normalised here, so the editors see small
// typed shapes and never throw on a field a daemon version omits.
//
//   GET  /api/v0/projects/{pid}/packets[?status=all]      -> [{packet_id, shot_id, tier, kind, status, received_utc,
//                                                              camera_name, plate: {role, relpath, blake3, width, height},
//                                                              supersedes, superseded_by}]                         (T55)
//   GET  /api/v0/packets/{id}/summary                      -> {packet_id, size, plate, camera, legend: {entries}}  (T55)
//   GET  /api/v0/recipes[?project_id=]                    -> [{id, title, kind, engine, shot_class, library, mask, params,
//                                                              origin, group, thumbnail, task, tokens, overrides}]   (T61, T83)
//   GET  /api/v0/recipes/{id}/assemble[?project_id=&tokens=<urlencoded JSON>]
//                                                          -> {prompt, negative_prompt, unfilled, unfilled_tokens,
//                                                              prompt_source}                                        (T83)
//   POST /api/v0/recipes {scope, project_id?, overwrite?, recipe}  -> 201 {id, path, origin}; 409 recipe_exists      (T83)
//   POST /api/v0/assets?kind=reference&project_id=<id>     body = PNG/JPEG bytes -> {hash, width, height, scaled_from} (T82)
//   GET  /api/v0/projects/{pid}/references                 -> [{hash, width, height, bytes, uploaded_utc, note?}]     (T82)
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
  /** The folder the recipe came from: `shipped`, `studio` or `project` (T83); null on a daemon that does not say. */
  origin: string | null;
  /** Heads the recipe in the grouped select (`lighting`, `weather`, ...); null = ungrouped. */
  group: string | null;
  /** A CAS hash of an image shown beside the recipe, when it has one. */
  thumbnail: string | null;
  /** The engine-card task the recipe's runs belong to, when it names one. */
  task: string | null;
  /** The `[TOKEN]` texts still open when the recipe is assembled against an empty Project Sheet with its own defaults. */
  tokens: string[];
  /** The recipe's own token defaults, keyed by the literal `[TOKEN]` text. */
  overrides: Record<string, string>;
}

const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

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
    origin: str(raw.origin),
    group: str(raw.group),
    thumbnail: str(raw.thumbnail),
    task: str(raw.task),
    tokens: strings(raw.tokens),
    overrides: isRecord(raw.overrides) ? Object.fromEntries(Object.entries(raw.overrides).filter((e): e is [string, string] => typeof e[1] === "string")) : {},
  };
}

/** `GET /recipes[?project_id=]`: the runnable recipes (shipped and studio, plus the project's own with `projectId`). */
export async function listRecipes(projectId?: string | null): Promise<RecipeItem[]> {
  const q = projectId ? `?project_id=${enc(projectId)}` : "";
  return asList<unknown>(await fetchJson<unknown>(`/recipes${q}`), "recipes").flatMap((r) => parseRecipe(r) ?? []);
}

/** A recipe as the daemon would send it: tokens filled, and what is still open. */
export interface AssembledRecipe {
  prompt: string;
  negativePrompt: string;
  /** How many `[TOKEN]`s are still open in `prompt`. */
  unfilled: number;
  unfilledTokens: string[];
  /** `recipe` today; the daemon names where the text came from. */
  promptSource: string;
}

export function parseAssembled(body: unknown): AssembledRecipe {
  const r = isRecord(body) ? body : {};
  const open = strings(r.unfilled_tokens);
  return {
    prompt: typeof r.prompt === "string" ? r.prompt : "",
    negativePrompt: typeof r.negative_prompt === "string" ? r.negative_prompt : "",
    unfilled: num(r.unfilled) ?? open.length,
    unfilledTokens: open,
    promptSource: str(r.prompt_source) ?? "recipe",
  };
}

/**
 * `GET /recipes/{id}/assemble`: the text the recipe would send, filled from its own defaults, then `tokens` (the node's
 * `params.tokens`, keyed by the literal `[TOKEN]` text), then the project's Project Sheet.
 */
export async function assembleRecipe(recipeId: string, projectId?: string | null, tokens: Record<string, string> = {}): Promise<AssembledRecipe> {
  const query: string[] = [];
  if (projectId) query.push(`project_id=${enc(projectId)}`);
  if (Object.keys(tokens).length > 0) query.push(`tokens=${enc(JSON.stringify(tokens))}`);
  const q = query.length > 0 ? `?${query.join("&")}` : "";
  return parseAssembled(await fetchJson<unknown>(`/recipes/${enc(recipeId)}/assemble${q}`));
}

export const RECIPE_SCOPES = ["studio", "project"] as const;
export type RecipeScope = (typeof RECIPE_SCOPES)[number];

/** A schema-1 recipe document as `POST /recipes` takes it (`recipes/README.md`). */
export interface RecipeDoc {
  schema: 1;
  id: string;
  title: string;
  kind: "still_edit";
  engine: string;
  prompt: string;
  negative_prompt?: string;
  group?: string;
  task?: string;
  shot_class?: string;
  mask: { editable_classes: string[] };
}

export interface SavedRecipe {
  id: string;
  path: string | null;
  origin: string;
}

/**
 * `POST /recipes`: save a recipe to the studio folder or the project's. 409 `recipe_exists` (an id that exists in any
 * folder) throws unless `overwrite`; 422 carries the schema error text.
 */
export async function saveRecipe(scope: RecipeScope, projectId: string | null | undefined, doc: RecipeDoc, overwrite = false): Promise<SavedRecipe> {
  const body: Record<string, unknown> = { scope, recipe: doc };
  if (projectId) body.project_id = projectId;
  if (overwrite) body.overwrite = true;
  const r = await postJson<unknown>("/recipes", body);
  const o = isRecord(r) ? r : {};
  return { id: str(o.id) ?? doc.id, path: str(o.path), origin: str(o.origin) ?? scope };
}

// ---------------------------------------------------------------------------------------------------- references

export interface StoredReference {
  hash: string;
  width: number | null;
  height: number | null;
  /** The uploaded size when the daemon shrank it (a side over 4096), else null. */
  scaledFrom: [number, number] | null;
}

const pair = (v: unknown): [number, number] | null =>
  Array.isArray(v) && v.length === 2 && v.every((x) => typeof x === "number" && Number.isFinite(x)) ? [v[0] as number, v[1] as number] : null;

/**
 * `POST /assets?kind=reference&project_id=<id>`: store a PNG or JPEG in the project's CAS as a reference image (decision
 * 46: an engine input, never composited). The daemon re-encodes it as an 8-bit RGB PNG, which drops all metadata.
 */
export async function uploadReference(bytes: Uint8Array<ArrayBuffer>, projectId: string, contentType = "image/png"): Promise<StoredReference> {
  const r = await fetchJson<unknown>(`/assets?kind=reference&project_id=${enc(projectId)}`, {
    method: "POST",
    body: bytes,
    headers: { "Content-Type": contentType },
  });
  const o = isRecord(r) ? r : {};
  const hash = str(o.hash) ?? str(o.content_hash);
  if (!hash) throw new Error("the daemon stored the reference but returned no hash");
  return { hash, width: num(o.width), height: num(o.height), scaledFrom: pair(o.scaled_from) };
}

export interface ReferenceItem {
  hash: string;
  width: number | null;
  height: number | null;
  bytes: number | null;
  uploadedUtc: string | null;
  note: string | null;
}

export function parseReference(raw: unknown): ReferenceItem | null {
  if (!isRecord(raw)) return null;
  const hash = str(raw.hash);
  if (!hash) return null;
  return { hash, width: num(raw.width), height: num(raw.height), bytes: num(raw.bytes), uploadedUtc: str(raw.uploaded_utc), note: str(raw.note) };
}

/** `GET /projects/{pid}/references`: every reference uploaded to the project, in upload order. */
export async function listReferences(projectId: string): Promise<ReferenceItem[]> {
  return asList<unknown>(await fetchJson<unknown>(`/projects/${enc(projectId)}/references`), "references").flatMap((r) => parseReference(r) ?? []);
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
  /** Keep the whole render (overscan included) instead of the packet's safe frame. */
  full_frame?: boolean;
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
