// Which engines the Make bar offers for a node (T35): filtered by what the node makes (its kind maps to engine
// kinds and tasks) and by the project's custody (an NDA project allows C0/C1 routes only). Custody is graded per
// route; a route whose written terms are pending counts as D (docs/custody.md, decision 5a), so today an NDA project
// sees only the local C0 engines. This is UX: the daemon re-checks custody on every quote and refuses with 403.
// There is no text-to-image engine here and no upscaler on a plate (doctrine 1 and 5): Edit and People take image
// edit engines only.

import type { CustodyGrade, NodeKind } from "../nodeTypes";

export interface EngineRouteSummary {
  route_id?: string;
  provider: string;
  custody_grade: CustodyGrade;
  custody_status: "confirmed" | "pending_written_terms";
  /** The router's grade when the daemon sends it; otherwise derived (pending terms = D). */
  effective_grade?: CustodyGrade;
  status?: "active" | "retired";
}

/** One manifest param (`plate_engines.models.Param`), shown in the advanced drawer. */
export interface EngineParamSpec {
  name: string;
  type: string;
  enum?: unknown[];
  range?: [number, number];
  default?: unknown;
  advanced?: boolean;
}

/** What `GET /api/v0/engines` returns per engine: the manifest, or the fields of it the Make bar reads. */
export interface EngineSummary {
  id: string;
  name: string;
  kind: string;
  allowed_tasks: string[];
  routes: EngineRouteSummary[];
  params?: EngineParamSpec[];
  presets?: string[];
  price?: { usd: number; status?: string; model?: string };
}

export interface ProjectCustody {
  nda: boolean;
  allowed_grades: CustodyGrade[];
}

/** How a node kind runs. `engine`: a vendor or local model, chosen in the Make bar. `inprocess`: a numeric node or
 * a Max round trip (T48): still quoted and approved, at 0 USD and C0. `none`: nothing to make (the pipeline runs
 * Comp/Colour/Gate itself; Export, Review and Concept have their own flows; a Concept is never generated). */
export type MakeMode =
  | { mode: "engine"; engineKinds: readonly string[]; tasks: readonly string[] }
  | { mode: "inprocess" }
  | { mode: "none" };

const IMAGE_TASKS = ["relight", "people", "vegetation", "sky", "rephotograph_close"] as const;

export const MAKE_MODES: Readonly<Record<NodeKind, MakeMode>> = {
  Edit: { mode: "engine", engineKinds: ["image_edit", "local_image"], tasks: IMAGE_TASKS },
  People: { mode: "engine", engineKinds: ["image_edit", "local_image"], tasks: ["people"] },
  Motion: {
    mode: "engine",
    engineKinds: ["video_startend", "video_i2v", "video_r2v", "local_video"],
    tasks: ["crop_pair_motion", "previs"],
  },
  CropPair: { mode: "inprocess" },
  LightMix: { mode: "inprocess" },
  Recolour: { mode: "inprocess" },
  Set: { mode: "inprocess" },
  Light: { mode: "inprocess" },
  Scout: { mode: "inprocess" },
  Plate: { mode: "none" },
  Mask: { mode: "none" },
  Comp: { mode: "none" },
  Colour: { mode: "none" },
  Gate: { mode: "none" },
  Judge: { mode: "none" },
  Compare: { mode: "none" },
  Export: { mode: "none" },
  Review: { mode: "none" },
  Concept: { mode: "none" },
};

export const ALL_GRADES: readonly CustodyGrade[] = ["C0", "C1", "C2", "D"];
export const NDA_GRADES: readonly CustodyGrade[] = ["C0", "C1"];

export function effectiveGrade(route: EngineRouteSummary): CustodyGrade {
  if (route.effective_grade) return route.effective_grade;
  return route.custody_status === "confirmed" ? route.custody_grade : "D";
}

/** The grades a project allows; null custody (not loaded) allows everything and the daemon decides. */
export function allowedGrades(custody: ProjectCustody | null): readonly CustodyGrade[] {
  if (!custody) return ALL_GRADES;
  const grades = custody.allowed_grades.length > 0 ? custody.allowed_grades : custody.nda ? NDA_GRADES : ALL_GRADES;
  return custody.nda ? grades.filter((g) => NDA_GRADES.includes(g)) : grades;
}

const RANK: Record<CustodyGrade, number> = { C0: 0, C1: 1, C2: 2, D: 3 };

/** The best (most private) effective grade among an engine's active routes the project allows; null if none. */
export function bestAllowedGrade(engine: EngineSummary, custody: ProjectCustody | null): CustodyGrade | null {
  const ok = allowedGrades(custody);
  const grades = engine.routes
    .filter((r) => (r.status ?? "active") === "active")
    .map(effectiveGrade)
    .filter((g) => ok.includes(g))
    .sort((a, b) => RANK[a] - RANK[b]);
  return grades[0] ?? null;
}

/** Engines for a node: right kind, a shared task (the node's own `params.task` when set), an allowed route. */
export function engineOptions(
  engines: readonly EngineSummary[],
  kind: NodeKind,
  custody: ProjectCustody | null,
  task?: string | null,
): EngineSummary[] {
  const m = MAKE_MODES[kind];
  if (m.mode !== "engine") return [];
  const tasks = task ? m.tasks.filter((t) => t === task) : m.tasks;
  return engines.filter(
    (e) =>
      m.engineKinds.includes(e.kind) &&
      e.allowed_tasks.some((t) => tasks.includes(t)) &&
      bestAllowedGrade(e, custody) !== null,
  );
}

/** Params for the drawer: the full manifest list, advanced ones flagged. */
export function drawerParams(engine: EngineSummary | undefined): { basic: EngineParamSpec[]; advanced: EngineParamSpec[] } {
  const params = engine?.params ?? [];
  return { basic: params.filter((p) => !p.advanced), advanced: params.filter((p) => p.advanced) };
}

export const presetsFor = (engine: EngineSummary | undefined): string[] =>
  engine?.presets && engine.presets.length > 0 ? engine.presets : ["default"];

const r = (provider: string, grade: CustodyGrade, status: EngineRouteSummary["custody_status"]): EngineRouteSummary => ({
  route_id: provider,
  provider,
  custody_grade: grade,
  custody_status: status,
});
const P = "pending_written_terms" as const;
const C = "confirmed" as const;

/**
 * Used only when the daemon has no `GET /api/v0/engines`: id, name, kind, tasks and route grades of
 * `engines/plate_engines/manifests/*.yaml` (a vitest checks they still match). No params and no prices: the quote
 * always comes from the daemon, which prices and custody-checks the request itself.
 */
export const FALLBACK_ENGINES: readonly EngineSummary[] = [
  { id: "flux-3-image", name: "FLUX 3 Image", kind: "image_edit", allowed_tasks: ["relight", "vegetation", "sky", "rephotograph_close"], routes: [r("bfl", "D", C)] },
  { id: "gpt-image-2.5", name: "GPT Image 2.5", kind: "image_edit", allowed_tasks: [...IMAGE_TASKS], routes: [r("openai", "C1", P)] },
  { id: "kling-o3-4k", name: "Kling O3 4K", kind: "video_startend", allowed_tasks: ["crop_pair_motion"], routes: [r("atlas", "D", C)] },
  { id: "kling-v3-pro", name: "Kling v3 Pro", kind: "video_startend", allowed_tasks: ["crop_pair_motion"], routes: [r("atlas", "D", C)] },
  { id: "magnific-precision-v2", name: "Magnific Precision v2", kind: "upscale", allowed_tasks: ["upscale_ai_layers"], routes: [r("magnific", "D", P)] },
  { id: "nano-banana-2.1", name: "Nano Banana 2.1", kind: "image_edit", allowed_tasks: [...IMAGE_TASKS], routes: [r("gemini", "C1", P), r("atlas", "D", C)] },
  { id: "nano-banana-pro", name: "Nano Banana Pro", kind: "image_edit", allowed_tasks: [...IMAGE_TASKS], routes: [r("gemini", "C1", P)] },
  { id: "qwen-image-edit-2509", name: "Qwen-Image-Edit-2509 (ControlNet depth and edge)", kind: "local_image", allowed_tasks: [...IMAGE_TASKS], routes: [r("local", "C0", C)] },
  { id: "real-esrgan", name: "Real-ESRGAN", kind: "upscale", allowed_tasks: ["upscale_ai_layers"], routes: [r("local", "C0", C)] },
  { id: "seedance-2.5-i2v", name: "Seedance 2.5 image-to-video", kind: "video_i2v", allowed_tasks: ["crop_pair_motion"], routes: [r("atlas", "D", C), r("byteplus", "D", P), r("wavespeed", "D", P)] },
  { id: "seedance-2.5-r2v", name: "Seedance 2.5 reference-to-video", kind: "video_r2v", allowed_tasks: ["crop_pair_motion", "previs"], routes: [r("atlas", "D", C), r("byteplus", "D", P), r("wavespeed", "D", P)] },
  { id: "seedream-5-pro", name: "Seedream 5 Pro", kind: "image_edit", allowed_tasks: ["relight", "vegetation", "sky", "rephotograph_close"], routes: [r("atlas", "D", C)] },
  { id: "veo-3.1", name: "Veo 3.1", kind: "video_i2v", allowed_tasks: ["crop_pair_motion"], routes: [r("vertex", "C1", P)] },
  { id: "wan-2.2-vace", name: "Wan 2.2 VACE", kind: "local_video", allowed_tasks: ["previs"], routes: [r("local", "C0", C)] },
];
