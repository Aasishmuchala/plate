// Daemon calls behind the node editors. Every call goes through the token-carrying `fetchJson`; every response is
// normalised here so the editors see small typed shapes and never throw on a field a daemon version omits.
//
//   POST /api/v0/pixels/idpick   {packet_id, x, y, kind?}      -> {id, name, class, kind, editable, locked, coverage, depth_m}
//                                                                                                                     (T62 route)
//   POST /api/v0/pixels/segment  {packet_id, points}           -> {mask_hash, labels_hash, segments, selected}     (T82: mask_hash is
//                                                                    the plate-sized 0/255 mask of the clicked segments; null without points)
//   POST /api/v0/pixels/ocr      {packet_id}                   -> {boxes: [{bbox, text, conf}], shape}             (pixels /ocr)
//   POST /api/v0/assets?kind=mask&project_id=<id>[&packet_id=<id>]  body = PNG bytes -> {hash, width, height, scaled_from}
//                                                                    (T56: a mask to the project's CAS; T82: with the packet, scaled to its plate)
//   POST /api/v0/projects/{pid}/nodes/{node}/preview {what, params}  -> router | set | lightmix | recolour numbers
//                                                                       (numeric, no spend, no engine; T48, router: T73)
//   POST /api/v0/plugin/jobs     {action, ...}                 -> {id, state}     (T48 queue; the Max dock polls it)
//   GET  /api/v0/plugin/jobs/{id}                              -> {id, state, result?: {ok, detail?, packet_id?}}
//
// The editors show the daemon's refusal (a 404 included) in a notice and stay usable by hand.

import { fetchJson, postJson } from "../api";
import { errorMessage } from "../panels/api";
import { type IdClass, isIdClass, NEVER_EDITABLE } from "./maskMath";
import { parseRouterCard, type RouterCard } from "./movePolicy";

export { errorMessage };

const enc = encodeURIComponent;
const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v: unknown): string | null => (typeof v === "string" && v !== "" ? v : null);

// ---------------------------------------------------------------------------------------------------- pixels

export interface IdPickResult {
  /** Null when the pixel has no Cryptomatte id (uncovered pixels are locked by construction). */
  id: string | null;
  name: string;
  class: IdClass;
  kind: "object" | "material" | "segment";
  /** False when policy locks this id (the daemon's word) or its class is never editable. */
  editable: boolean;
  /** True when the packet policy locks this id; null when the daemon does not say. */
  locked: boolean | null;
  /** Share of the pixel the id covers, 0..1 (Cryptomatte coverage); null when the daemon does not say. */
  coverage: number | null;
  /** Packet Z at the pixel in metres, when the daemon sends it (the People ground-point picker uses it). */
  depthM: number | null;
}

export function parseIdPick(body: unknown): IdPickResult {
  const r = isRecord(body) ? body : {};
  const cls: IdClass = isIdClass(r.class) ? r.class : "other";
  const kind = r.kind === "material" || r.kind === "segment" ? r.kind : "object";
  const locked = typeof r.locked === "boolean" ? r.locked : null;
  return {
    id: str(r.id),
    name: str(r.name) ?? str(r.id) ?? "",
    class: cls,
    kind,
    editable: r.editable === false || locked === true ? false : !NEVER_EDITABLE.has(cls),
    locked,
    coverage: num(r.coverage),
    depthM: num(r.depth_m),
  };
}

/** `POST /pixels/idpick`: the Cryptomatte id under a plate pixel. `kind` picks object (default) or material ids. */
export async function idPick(body: { packet_id: string; x: number; y: number; kind?: "object" | "material" }): Promise<IdPickResult> {
  return parseIdPick(await postJson<unknown>("/pixels/idpick", body));
}

export interface SegmentResult {
  /** CAS hash of the plate-sized 0/255 mask of the clicked segments, when the daemon stored one. */
  maskHash: string | null;
  /** CAS hash of the 16-bit label map the clicks were looked up in (never a mask). */
  labelsHash: string | null;
  regions: number;
  /** The segment ids under the clicked points, as the daemon picked them. */
  selected: number[];
}

/** `POST /pixels/segment`: SAM-style click segmentation (the stub returns k-means labels). */
export async function segment(body: { packet_id: string; points: [number, number][] }): Promise<SegmentResult> {
  const r = await postJson<unknown>("/pixels/segment", body);
  const o = isRecord(r) ? r : {};
  const segs = o.segments;
  return {
    maskHash: str(o.mask_hash) ?? null,
    labelsHash: str(o.labels_hash) ?? str(o.segments_hash) ?? null,
    regions: Array.isArray(segs) ? segs.length : (num(segs) ?? 0),
    selected: Array.isArray(o.selected) ? o.selected.filter((v): v is number => typeof v === "number" && Number.isInteger(v)) : [],
  };
}

export interface OcrBox {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  text: string;
  conf: number | null;
}

export interface OcrResult {
  boxes: OcrBox[];
  size: { width: number; height: number } | null;
}

function toBox(raw: unknown): OcrBox | null {
  if (!isRecord(raw)) return null;
  const b = raw.bbox;
  let c: number[] | null = null;
  if (Array.isArray(b) && b.length === 4 && b.every((v) => typeof v === "number")) c = b as number[];
  else if (isRecord(b) && [b.x0, b.y0, b.x1, b.y1].every((v) => typeof v === "number")) c = [b.x0, b.y0, b.x1, b.y1] as number[];
  if (!c) return null;
  return { x0: c[0] ?? 0, y0: c[1] ?? 0, x1: c[2] ?? 0, y1: c[3] ?? 0, text: typeof raw.text === "string" ? raw.text : "", conf: num(raw.conf) };
}

/** `POST /pixels/ocr`: the OCR boxes the text-lock composites back from the plate. */
export async function ocr(body: { packet_id: string }): Promise<OcrResult> {
  const r = await postJson<unknown>("/pixels/ocr", body);
  const o = isRecord(r) ? r : {};
  const list = Array.isArray(o.boxes) ? o.boxes : [];
  const shape = Array.isArray(o.shape) && o.shape.length === 2 && o.shape.every((v) => typeof v === "number") ? (o.shape as number[]) : null;
  return {
    boxes: list.flatMap((b) => toBox(b) ?? []),
    size: shape ? { width: shape[1] ?? 0, height: shape[0] ?? 0 } : null,
  };
}

export interface StoredMask {
  hash: string;
  /** The stored size (the plate's when the upload named the packet); null when the daemon does not say. */
  width: number | null;
  height: number | null;
  /** The uploaded size when the daemon scaled the mask to the plate, else null. */
  scaledFrom: [number, number] | null;
}

const pair = (v: unknown): [number, number] | null =>
  Array.isArray(v) && v.length === 2 && v.every((x) => typeof x === "number" && Number.isFinite(x)) ? [v[0] as number, v[1] as number] : null;

/**
 * `POST /assets?kind=mask&project_id=<id>[&packet_id=<id>]`: store a mask image in that project's CAS. With the packet
 * the daemon scales it to that packet's plate (nearest neighbour, then 0/255), so a mask painted or made at another
 * size still lands where the quote checks it.
 */
export async function storeMask(bytes: Uint8Array<ArrayBuffer>, projectId: string, packetId?: string | null, contentType = "image/png"): Promise<StoredMask> {
  const packet = packetId ? `&packet_id=${enc(packetId)}` : "";
  const r = await fetchJson<unknown>(`/assets?kind=mask&project_id=${enc(projectId)}${packet}`, {
    method: "POST",
    body: bytes,
    headers: { "Content-Type": contentType },
  });
  const o = isRecord(r) ? r : {};
  const hash = str(o.hash) ?? str(o.content_hash);
  if (!hash) throw new Error("the daemon stored the mask but returned no hash");
  return { hash, width: num(o.width), height: num(o.height), scaledFrom: pair(o.scaled_from) };
}

/** `storeMask` answering the hash only (the T56 call shape; `packetId` adds the T82 scaling). */
export async function uploadMask(png: Uint8Array<ArrayBuffer>, projectId: string, packetId?: string | null): Promise<string> {
  return (await storeMask(png, projectId, packetId)).hash;
}

// ---------------------------------------------------------------------------------------------------- previews

export type PreviewWhat = "router" | "set" | "lightmix" | "recolour";

/** `POST /projects/{pid}/nodes/{node}/preview`: numbers for a node before anything is made. */
export function preview(projectId: string, nodeId: string, what: PreviewWhat, params: Record<string, unknown>): Promise<unknown> {
  return postJson<unknown>(`/projects/${enc(projectId)}/nodes/${enc(nodeId)}/preview`, { what, params });
}

/**
 * The daemon's router preview as the Motion editor's `RouterCard`. `route_move` answers `{mode, verdict, numbers:
 * {...}, explanation}`; the T73 preview route answers the same numbers flat (`D_px_median`, `disocclusion_fraction`,
 * `orbit_deg` next to `verdict`). Both shapes (and a `{router: ...}` wrapper) are folded into the nested one that
 * `parseRouterCard` reads. Null when the body is not a router verdict.
 */
export function parseRouterPreview(body: unknown): RouterCard | null {
  const top = isRecord(body) && isRecord(body.router) ? body.router : body;
  if (!isRecord(top)) return null;
  if (isRecord(top.numbers)) return parseRouterCard(top);
  const numbers: Record<string, unknown> = {};
  for (const k of ["D_px_median", "disocclusion_fraction", "orbit_deg", "detail_verdict", "crossings"]) {
    if (k in top) numbers[k] = top[k];
  }
  return parseRouterCard({ ...top, numbers });
}

export async function routerPreview(projectId: string, nodeId: string, params: Record<string, unknown>): Promise<RouterCard | null> {
  return parseRouterPreview(await preview(projectId, nodeId, "router", params));
}

/** One Set row: a material, its Lab on the hero, and its cross-view ΔE00 p90 on each other view. */
export interface SetRow {
  id: string;
  name: string;
  lab: [number, number, number] | null;
  /** Editable materials (vegetation, people, sky, water) are scored but only advisory. */
  hard: boolean;
  p90: number | null;
  perView: Record<string, number | null>;
  note: string | null;
}

export interface SetSummary {
  hero: string | null;
  views: string[];
  rows: SetRow[];
  crossViewP90: number | null;
  verdict: "pass" | "fail" | "advisory" | null;
  notes: string[];
}

/** Hard limit of the Set gate: cross-view p90 ΔE00 <= 3 (BUILD_PLAN P4 exit). */
export const SET_P90_LIMIT = 3;

const lab3 = (v: unknown): [number, number, number] | null =>
  Array.isArray(v) && v.length >= 3 && v.slice(0, 3).every((x) => typeof x === "number") ? [v[0] as number, v[1] as number, v[2] as number] : null;

/** A Set summary from `setgate.run` JSON (targets keyed by material id, `per_view` list, `per_material`). */
export function parseSetSummary(body: unknown): SetSummary {
  const top = isRecord(body) && isRecord(body.report) ? body.report : body;
  const r = isRecord(top) ? top : {};
  const targetsRaw = r.targets;
  const targets: Record<string, Record<string, unknown>> = {};
  if (isRecord(targetsRaw)) {
    for (const [id, t] of Object.entries(targetsRaw)) if (isRecord(t)) targets[id] = t;
  } else if (Array.isArray(targetsRaw)) {
    for (const t of targetsRaw) if (isRecord(t) && typeof t.id === "string") targets[t.id] = t;
  }
  const perMaterial = isRecord(r.per_material) ? r.per_material : {};
  const perView = Array.isArray(r.per_view) ? r.per_view.filter(isRecord) : [];
  const views = perView.map((v) => str(v.packet_id)).flatMap((v) => (v ? [v] : []));
  const ids = Array.from(new Set([...Object.keys(targets), ...Object.keys(perMaterial)]));
  const rows = ids.map((id): SetRow => {
    const t = targets[id] ?? {};
    const m = isRecord(perMaterial[id]) ? (perMaterial[id] as Record<string, unknown>) : {};
    const byView: Record<string, number | null> = {};
    for (const v of perView) {
      const pid = str(v.packet_id);
      const pm = isRecord(v.per_material) && isRecord(v.per_material[id]) ? (v.per_material[id] as Record<string, unknown>) : null;
      if (pid) byView[pid] = pm ? (num(pm.de00_p90) ?? num(pm.p90)) : null;
    }
    return {
      id,
      name: str(t.name) ?? str(m.name) ?? id,
      lab: lab3(t.lab),
      hard: m.hard === true,
      p90: num(m.de00_p90),
      perView: byView,
      note: str(m.reason),
    };
  });
  const hero = isRecord(r.hero) ? str(r.hero.packet_id) : null;
  const verdict = r.verdict === "pass" || r.verdict === "fail" || r.verdict === "advisory" ? r.verdict : null;
  return {
    hero,
    views,
    rows,
    crossViewP90: num(r.cross_view_p90),
    verdict,
    notes: Array.isArray(r.notes) ? r.notes.filter((n): n is string => typeof n === "string") : [],
  };
}

export async function setPreview(projectId: string, nodeId: string, params: { hero: string; views: string[] }): Promise<SetSummary> {
  return parseSetSummary(await preview(projectId, nodeId, "set", params));
}

export interface LightMixSeed {
  /** Light-select names in the packet's `x_light_*` roles. */
  lights: string[];
  gains: Record<string, number>;
  residualRms: number | null;
  /** Lights whose NNLS gain was clipped to max_gain. */
  clipped: string[];
}

/** `lightmix.solve` JSON (`gains`, `residual_rms`, `clipped`) plus the light names (`lights`, or the gain keys). */
export function parseLightMixSeed(body: unknown): LightMixSeed {
  const top = isRecord(body) && isRecord(body.result) ? body.result : body;
  const r = isRecord(top) ? top : {};
  const gains: Record<string, number> = {};
  if (isRecord(r.gains)) for (const [k, v] of Object.entries(r.gains)) if (typeof v === "number" && Number.isFinite(v)) gains[k] = v;
  const named = Array.isArray(r.lights) ? r.lights.flatMap((l) => (typeof l === "string" ? [l] : isRecord(l) && typeof l.name === "string" ? [l.name] : [])) : [];
  const lights = named.length > 0 ? named : Object.keys(gains);
  return {
    lights,
    gains,
    residualRms: num(r.residual_rms),
    clipped: Array.isArray(r.clipped) ? r.clipped.filter((n): n is string => typeof n === "string") : [],
  };
}

export async function lightmixPreview(projectId: string, nodeId: string, params: { target_hash?: string }): Promise<LightMixSeed> {
  return parseLightMixSeed(await preview(projectId, nodeId, "lightmix", params));
}

export interface RecolourExpect {
  method: "recomposition" | "ratio" | "degraded" | null;
  expectedDe00: number | null;
  degraded: boolean;
  /** Spec energy over 0.3 of the beauty in the mask: tinted metal reflections are out of scope. */
  metal: boolean;
}

export function parseRecolourExpect(body: unknown): RecolourExpect {
  const top = isRecord(body) && isRecord(body.result) ? body.result : body;
  const r = isRecord(top) ? top : {};
  const method = r.method === "recomposition" || r.method === "ratio" || r.method === "degraded" ? r.method : null;
  return { method, expectedDe00: num(r.expected_de00), degraded: r.degraded === true || method === "degraded", metal: r.metal === true };
}

export async function recolourPreview(
  projectId: string,
  nodeId: string,
  params: { material_id: string; ral?: string; hex?: string },
): Promise<RecolourExpect> {
  return parseRecolourExpect(await preview(projectId, nodeId, "recolour", params));
}

// ---------------------------------------------------------------------------------------------------- plugin queue

/** A job on the plugin queue as the editors see it. `detail` / `resultPacketId` come from the dock's answer. */
export interface PluginJob {
  id: string;
  /** `queued | claimed | done | failed | cancelled` (the daemon's word; an unknown state is kept as sent). */
  state: string;
  /** The dock's `detail` text (why a job failed, or what it did); null until the job has an answer. */
  detail: string | null;
  /** The packet the dock sent for this job (`result.packet_id`), e.g. the re-render of a Light match. */
  resultPacketId: string | null;
}

/** States after which a job never changes again; the Light editor stops polling there. */
export const PLUGIN_JOB_FINAL: ReadonlySet<string> = new Set(["done", "failed", "cancelled"]);

function toPluginJob(body: unknown, fallbackId?: string): PluginJob {
  const o = isRecord(body) ? body : {};
  const id = str(o.id) ?? str(o.job_id) ?? fallbackId;
  if (!id) throw new Error("the daemon returned a job with no id");
  const result = isRecord(o.result) ? o.result : {};
  return { id, state: str(o.state) ?? "queued", detail: str(result.detail) ?? str(result.reason), resultPacketId: str(result.packet_id) };
}

/** `POST /plugin/jobs`: the daemon queues a job for the Max dock to pick up (T48's `plugin_queue`). */
export async function queuePluginJob(job: { action: string } & Record<string, unknown>): Promise<PluginJob> {
  const r = await postJson<unknown>("/plugin/jobs", job);
  const o = isRecord(r) ? r : {};
  if (!str(o.id) && !str(o.job_id)) throw new Error("the daemon queued the job but returned no id");
  return toPluginJob(r);
}

/** `GET /plugin/jobs/{id}`: one job's state and, once the dock has answered, its result. */
export async function getPluginJob(id: string): Promise<PluginJob> {
  return toPluginJob(await fetchJson<unknown>(`/plugin/jobs/${enc(id)}`), id);
}
