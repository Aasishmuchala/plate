// The Move policy as the Motion editor shows it: Lensbook's RISK_LADDER (move, risk, repair codes) plus the dials
// that raise a move's risk, and the router verdict that can block a move outright. UX mirror of the daemon's
// `motion/move_policy.py` (T43): the daemon re-checks on quote, and the canvas rules (HIGH_RISK_MOVE) refuse the edge
// of a high-risk move that carries no logged override.
//
//   risk ladder   locked tripod none; push-in, slider drift low; crane rise, light sweep med;
//                 orbit up to 30 deg, aerial drift high; threshold cross highest
//   dials         push-in reveal > 3 m -> med, > 10 m -> high; slider > 10 cm -> med; orbit > 30 deg -> highest
//   override      high and highest need a logged override with a reason (10+ characters here)
//   router        in blocking mode a `render_move` verdict blocks the move and no override lifts it
//   pull-back     a push-in whose start zoom is over its end zoom (z0 > z1, decision 49): shown as `Pull-back`, the
//                 same risk row as the push-in (the daemon plans it as push_in; the reveal dial still applies)
//   single image  a locked tripod may send the start frame alone (`params.single_image`: no end frame, a held frame)

import type { RiskLabel } from "../nodeTypes";

export const MOVE_KINDS = [
  "locked_tripod",
  "push_in",
  "slider_drift",
  "crane_rise",
  "light_sweep",
  "orbit_le_30",
  "aerial_drift",
  "threshold_cross",
] as const;
export type MoveKind = (typeof MOVE_KINDS)[number];

/** `none` is the locked tripod: nothing moves, nothing to repair. */
export type MoveRisk = "none" | RiskLabel;

export interface MoveSpec {
  label: string;
  risk: MoveRisk;
  /** Lensbook repair codes to reach for when the take misbehaves. */
  repair: readonly string[];
}

export const RISK_LADDER: Readonly<Record<MoveKind, MoveSpec>> = {
  locked_tripod: { label: "Locked tripod", risk: "none", repair: [] },
  push_in: { label: "Push-in", risk: "low", repair: ["F6"] },
  slider_drift: { label: "Slider drift", risk: "low", repair: ["F6"] },
  crane_rise: { label: "Crane rise", risk: "med", repair: ["A1"] },
  light_sweep: { label: "Light sweep", risk: "med", repair: ["F12"] },
  orbit_le_30: { label: "Orbit up to 30 deg", risk: "high", repair: ["A1"] },
  aerial_drift: { label: "Aerial drift", risk: "high", repair: ["A1", "A3"] },
  threshold_cross: { label: "Threshold cross", risk: "highest", repair: ["A1", "F7"] },
};

/** What each repair code means (Lensbook A/F codes the ladder cites). */
export const REPAIR_CODES: Readonly<Record<string, string>> = {
  A1: "geometry warp",
  A3: "scale drift",
  F6: "camera improvises",
  F7: "does not match the previous shot",
  F12: "light shifts mid-clip",
};

export const isMoveKind = (v: unknown): v is MoveKind => typeof v === "string" && (MOVE_KINDS as readonly string[]).includes(v);

const ORDER: readonly MoveRisk[] = ["none", "low", "med", "high", "highest"];
const worse = (a: MoveRisk, b: MoveRisk): MoveRisk => (ORDER.indexOf(a) >= ORDER.indexOf(b) ? a : b);

/** The dials a move may carry. A dial that does not apply to the move kind is ignored. */
export interface MoveDials {
  /** Push-in: how far (metres) the end frame reveals beyond what the start frame shows. */
  revealM?: number;
  /** Orbit: swept angle in degrees. */
  orbitDeg?: number;
  /** Slider: travel in centimetres. */
  sliderCm?: number;
}

/** The router verdict card (`plate_gates.router_math.RouterVerdict`, JSON mode). */
export interface RouterCard {
  mode: "advisory" | "blocking";
  verdict: "ai_interpolate" | "rendered_endpoints_plus_clay" | "render_move" | "unknown";
  numbers: {
    D_px_median: number | null;
    disocclusion_fraction: number | null;
    orbit_deg: number | null;
    detail_verdict: string | null;
    crossings: number;
  };
  explanation: string;
}

/** Minimum characters of an override reason: a real sentence, not a dot. */
export const MIN_OVERRIDE_CHARS = 10;

export interface MoveEvaluation {
  kind: MoveKind;
  label: string;
  risk: MoveRisk;
  /** The `riskLabel` to put on the node; undefined for a locked tripod. */
  riskLabel: RiskLabel | undefined;
  repair: readonly string[];
  /** The risk went up because of a dial, not the base move. */
  raisedBy: string | null;
  /** High and highest moves are blocked until an override with a reason is logged. */
  needsOverride: boolean;
  /** The router says render it and is in blocking mode: no override lifts this. */
  routerBlocks: boolean;
}

export function evaluateMove(kind: MoveKind, dials: MoveDials = {}, router: RouterCard | null = null): MoveEvaluation {
  const spec = RISK_LADDER[kind];
  let risk = spec.risk;
  let raisedBy: string | null = null;
  const raise = (to: MoveRisk, why: string): void => {
    const next = worse(risk, to);
    if (next !== risk) {
      risk = next;
      raisedBy = why;
    }
  };
  if (kind === "push_in" && dials.revealM !== undefined) {
    if (dials.revealM > 10) raise("high", `reveal ${dials.revealM} m > 10 m invents geometry`);
    else if (dials.revealM > 3) raise("med", `reveal ${dials.revealM} m > 3 m`);
  }
  if (kind === "slider_drift" && dials.sliderCm !== undefined && dials.sliderCm > 10) {
    raise("med", `slider ${dials.sliderCm} cm > 10 cm`);
  }
  if (kind === "orbit_le_30" && dials.orbitDeg !== undefined && dials.orbitDeg > 30) {
    raise("highest", `orbit ${dials.orbitDeg} deg > 30 deg`);
  }
  return {
    kind,
    label: spec.label,
    risk,
    riskLabel: risk === "none" ? undefined : risk,
    repair: spec.repair,
    raisedBy,
    needsOverride: risk === "high" || risk === "highest",
    routerBlocks: router !== null && router.mode === "blocking" && router.verdict === "render_move",
  };
}

/** Why Make is blocked right now, or null when it may run. `loggedOverrides` counts overrides already on the node. */
export function makeBlock(ev: MoveEvaluation, overrideText: string, loggedOverrides: number): string | null {
  if (ev.routerBlocks) return "the router says render this move in V-Ray; no override lifts a blocking router verdict";
  if (ev.needsOverride && loggedOverrides === 0 && overrideText.trim().length < MIN_OVERRIDE_CHARS) {
    return `a ${ev.risk} risk move needs an override reason of at least ${MIN_OVERRIDE_CHARS} characters`;
  }
  return null;
}

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** A router card from the daemon's `RouterVerdict` JSON (or `{router: ...}`); null when it does not look like one. */
export function parseRouterCard(body: unknown): RouterCard | null {
  const raw = typeof body === "object" && body !== null && "router" in body ? (body as { router: unknown }).router : body;
  if (typeof raw !== "object" || raw === null) return null;
  const r = raw as Record<string, unknown>;
  const n = (typeof r.numbers === "object" && r.numbers !== null ? r.numbers : {}) as Record<string, unknown>;
  const verdict = r.verdict;
  if (verdict !== "ai_interpolate" && verdict !== "rendered_endpoints_plus_clay" && verdict !== "render_move" && verdict !== "unknown") return null;
  return {
    mode: r.mode === "blocking" ? "blocking" : "advisory",
    verdict,
    numbers: {
      D_px_median: num(n.D_px_median),
      disocclusion_fraction: num(n.disocclusion_fraction),
      orbit_deg: num(n.orbit_deg),
      detail_verdict: typeof n.detail_verdict === "string" ? n.detail_verdict : null,
      crossings: Array.isArray(n.crossings) ? n.crossings.length : (num(n.crossings) ?? 0),
    },
    explanation: typeof r.explanation === "string" ? r.explanation : "",
  };
}

// ---------------------------------------------------------------------------------------------------- zooms, pull-back

/** Each crop-pair zoom lies in [1.0, 3.0] (`plate_gates.croppair.validate_zooms`). */
export const ZOOM_MIN = 1.0;
export const ZOOM_MAX = 3.0;

/** `in` (z1 > z0, a push-in), `out` (z0 > z1, a pull-back) or `locked` (z0 = z1, the frame holds). */
export type ZoomDirection = "in" | "out" | "locked";

export const zoomDirection = (z0: number, z1: number): ZoomDirection => (z1 > z0 ? "in" : z1 < z0 ? "out" : "locked");

/**
 * Null when the zoom pair suits the move, else the reason (the daemon re-checks): each zoom in [1.0, 3.0]; a push-in
 * or pull-back needs two different zooms. A locked tripod holds one zoom whatever is typed, so any pair passes here.
 */
export function zoomPairError(z0: number, z1: number, kind: MoveKind | null = null): string | null {
  const inRange = (z: number): boolean => Number.isFinite(z) && ZOOM_MIN <= z && z <= ZOOM_MAX;
  if (!inRange(z0) || !inRange(z1)) {
    return `each zoom must lie in ${ZOOM_MIN.toFixed(1)} to ${ZOOM_MAX.toFixed(1)}, got ${z0} -> ${z1}`;
  }
  if (kind === "push_in" && z0 === z1) return "a push-in or pull-back needs two different zooms; a held frame is a locked tripod";
  return null;
}

/** The ladder row a pull-back shows: a push-in played outwards, so the same risk and repair codes. */
export const PULL_BACK: MoveSpec = { label: "Pull-back", risk: RISK_LADDER.push_in.risk, repair: RISK_LADDER.push_in.repair };

/** What a move is shown as: a ladder key, or `pull_back` (a presentation of `push_in`, not a move of its own). */
export type MovePresentation = MoveKind | "pull_back";

export interface PresentedMove extends MoveSpec {
  key: MovePresentation;
  /** The ladder move the daemon plans. */
  kind: MoveKind;
  /** From the zoom pair when both zooms are known, else null. */
  direction: ZoomDirection | null;
}

/** The move as the editor shows it: `push_in` with z0 > z1 is `Pull-back` (same risk row); everything else as laddered. */
export function presentMove(kind: MoveKind, z0: number | null = null, z1: number | null = null): PresentedMove {
  const direction = z0 !== null && z1 !== null ? zoomDirection(z0, z1) : null;
  if (kind === "push_in" && direction === "out") return { ...PULL_BACK, key: "pull_back", kind, direction };
  return { ...RISK_LADDER[kind], key: kind, kind, direction };
}

/** A switch a move exposes on top of its dials. */
export type MoveToggle = "single_image";

/** The locked tripod may send its start frame alone (`single_image`: no `image_tail`, a held frame only). */
export const MOVE_TOGGLES: Readonly<Partial<Record<MoveKind, readonly MoveToggle[]>>> = { locked_tripod: ["single_image"] };

export const hasToggle = (kind: MoveKind | null, toggle: MoveToggle): boolean => kind !== null && (MOVE_TOGGLES[kind] ?? []).includes(toggle);

// ---------------------------------------------------------------------------------------------------- motion recipes

/** A `motion_croppair` recipe as the Motion editor's select shows it (title · move · risk). */
export interface MotionRecipe {
  id: string;
  title: string;
  library: string | null;
  engine: string | null;
  /** `motion.move` when the daemon sends the recipe's motion block, else null. */
  move: MoveKind | null;
  /** `croppair.z0`, `croppair.z1` when the daemon sends the recipe's croppair block. */
  zooms: [number, number] | null;
  /** `motion.two_stills`: the recipe moves between two approved stills, so the end input must be wired. */
  twoStills: boolean;
  /** The move as shown (a pull-back for a push-in recipe with z0 > z1); null when the move is not known. */
  presented: PresentedMove | null;
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/** One row of `GET /recipes?kind=motion_croppair`; null for a row that is not a motion recipe. */
export function parseMotionRecipe(raw: unknown): MotionRecipe | null {
  if (!isObj(raw) || typeof raw.id !== "string" || raw.id === "") return null;
  if (raw.kind !== undefined && raw.kind !== "motion_croppair") return null;
  const motion = isObj(raw.motion) ? raw.motion : {};
  const params = isObj(raw.params) ? raw.params : {};
  const cp = isObj(raw.croppair) ? raw.croppair : {};
  const moveRaw = motion.move ?? params.move;
  const move = isMoveKind(moveRaw) ? moveRaw : null;
  const z0 = num(cp.z0);
  const z1 = num(cp.z1);
  const zooms: [number, number] | null = z0 !== null && z1 !== null ? [z0, z1] : null;
  return {
    id: raw.id,
    title: typeof raw.title === "string" && raw.title !== "" ? raw.title : raw.id,
    library: typeof raw.library === "string" ? raw.library : null,
    engine: typeof raw.engine === "string" ? raw.engine : null,
    move,
    zooms,
    twoStills: motion.two_stills === true,
    presented: move ? presentMove(move, zooms?.[0] ?? null, zooms?.[1] ?? null) : null,
  };
}

/** `title · move · risk`; a recipe whose motion block the daemon did not send reads `title · <library>`. */
export function motionRecipeLabel(r: MotionRecipe): string {
  if (r.presented) return `${r.title} · ${r.presented.label} · ${r.presented.risk}`;
  return [r.title, r.library].filter(Boolean).join(" · ");
}
