// Motion node editor (T47): the move kind with its risk label and repair codes (Lensbook's risk ladder), the dials that
// raise a move's risk, duration, engine (Kling or Seedance), the router verdict card (D_px_median, disocclusion
// fraction, orbit, advisory or blocking chip) and the override reason. Make is disabled while the move is blocked:
//   * a high or highest risk move until the override reason has 10+ characters (logged on the node when Make runs),
//   * a router `render_move` verdict in blocking mode, which no override lifts.
// The quote and Make buttons act on the same per-node entry as the Make bar, so the bar shows the same quote and its
// approval dialog. Node params: `move`, `duration`, `engine_id`, and the dials `revealM`, `orbitDeg`, `sliderCm`.
// The video player below shows the picked (or best) take with its time map and gate chips.
//
// T106 (decision 49): the `Stills` row names the start still and the node wired to `end` (or `none: single-still
// move`) with the lock state of each (VIDEO_LOCKED until it passes gates and a human approves it); `elements` lists the
// Reference nodes wired to the elements input. `recipe` picks a `motion_croppair` recipe from
// `GET /recipes?kind=motion_croppair` (title · move · risk) into `params.recipe`; `prompt` (`params.prompt`) overrides
// the recipe's text, which is shown assembled (`GET /recipes/{id}/assemble`). The crop-pair zooms (`zoomStart`,
// `zoomEnd`, each 1.0 to 3.0) take z0 > z1: a push-in played outwards is shown as `Pull-back`, the same risk row. A
// locked tripod may send one frame (`params.single_image`). Veo 3.1 joins Kling and Seedance once the project's custody
// is known to allow it, and each engine shows its Wilson card (lower bound, n, mean cost) for `crop_pair_motion`.
// The player's `Deliver…` and `Compare` act on the take on show.

import { type ReactElement, useEffect, useMemo, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { fetchJson } from "../api";
import type { EdgeLike, GraphNode } from "../canvasRules";
import { getGateReport, UI_ACTOR } from "../canvas/runApi";
import { type Candidate, nodeCandidates, useRunStore } from "../canvas/runStore";
import { cardFor } from "../makebar/api";
import { allowedGrades, bestAllowedGrade, type EngineSummary, engineOptions, FALLBACK_ENGINES } from "../makebar/engines";
import { cardsKey, EMPTY_ENTRY, useMakeStore } from "../makebar/makeStore";
import { formatCard, isFresh } from "../makebar/quote";
import { checkHandles } from "../nodes/ports";
import { asList } from "../panels/api";
import { useGraphStore } from "../store";
import { errorMessage, routerPreview } from "./api";
import { listTakes, type TakeItem } from "./deliverApi";
import {
  evaluateMove,
  hasToggle,
  isMoveKind,
  makeBlock,
  MIN_OVERRIDE_CHARS,
  MOVE_KINDS,
  type MotionRecipe,
  motionRecipeLabel,
  type MoveDials,
  type MoveKind,
  parseMotionRecipe,
  presentMove,
  REPAIR_CODES,
  RISK_LADDER,
  type RouterCard,
  ZOOM_MAX,
  ZOOM_MIN,
  zoomDirection,
  zoomPairError,
} from "./movePolicy";
import { type AssembledRecipe, assembleRecipe } from "./nodeApi";
import { asNumber, asString, useNodeEditor } from "./params";
import { C, E, type Tone } from "./theme";
import type { TimeMap } from "./timeMap";
import type { CompareClip, CompareSource } from "./VideoCompare";
import { timeMapFromReport, VideoPlayer, type PlayerReport } from "./VideoPlayer";
import { Button, Chip, EditorFrame, Field, NumberField, Notice, Section, Toggle } from "./widgets";

/** The video engines the editor offers and the duration range of each (manifest `duration` param). */
const MOTION_ENGINES: readonly { prefix: string; range: [number, number] }[] = [
  { prefix: "kling", range: [1, 15] },
  { prefix: "seedance", range: [4, 30] },
  { prefix: "veo", range: [4, 8] },
];

/** The engine-card task of a crop-pair or two-still move (`GET /engines/cards?task=`). */
export const MOTION_CARD_TASK = "crop_pair_motion";

/** Veo stays custody D until its written terms are filed (decision 49): offered only when the project's custody is
 * known and allows it, so an NDA project, or one whose custody has not loaded, never sees it. */
const needsKnownCustody = (e: EngineSummary): boolean => e.id.startsWith("veo");

const rangeFor = (engineId: string): [number, number] => MOTION_ENGINES.find((e) => engineId.startsWith(e.prefix))?.range ?? [1, 15];
const isMotionEngine = (e: EngineSummary): boolean => MOTION_ENGINES.some((m) => e.id.startsWith(m.prefix));

const RISK_TONE: Record<string, Tone> = { none: "muted", low: "pass", med: "advisory", high: "fail", highest: "fail" };

// ---------------------------------------------------------------------------------------------------- wires

/** What feeds a Motion node, by the input each edge resolves to (a null handle resolves as `checkHandles` does). */
export interface MotionWires {
  /** The still on `image` (the start frame). */
  start: GraphNode | null;
  /** The still on `end`, or null for a single-still move. */
  end: GraphNode | null;
  /** The Reference nodes on `elements`. */
  elements: GraphNode[];
}

/** The start still, end still and elements of `nodeId`. Pure. */
export function motionWires(nodes: readonly GraphNode[], edges: readonly EdgeLike[], nodeId: string): MotionWires {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const out: MotionWires = { start: null, end: null, elements: [] };
  for (const e of edges) {
    if (e.target !== nodeId) continue;
    const src = byId.get(e.source);
    if (!src) continue;
    const v = checkHandles(src.data.kind, e.sourceHandle, "Motion", e.targetHandle);
    const port = v.ok ? v.targetHandle : (e.targetHandle ?? null);
    if (port === "end") out.end ??= src;
    else if (port === "elements") out.elements.push(src);
    else if (port === "image") out.start ??= src;
  }
  return out;
}

/** A still into Motion is unlocked once it passed its gates and a human approved it (VIDEO_LOCKED otherwise). */
export const stillUnlocked = (n: GraphNode): boolean => n.data.gateStatus === "pass" && n.data.approved === true;

// ---------------------------------------------------------------------------------------------------- recipes

/** `GET /recipes?kind=motion_croppair[&project_id=]`: shipped, studio and (with the project) the project's own. */
export async function listMotionRecipes(projectId: string | null): Promise<MotionRecipe[]> {
  const q = projectId ? `&project_id=${encodeURIComponent(projectId)}` : "";
  const body = await fetchJson<unknown>(`/recipes?kind=motion_croppair${q}`);
  return asList<unknown>(body, "recipes").flatMap((r) => parseMotionRecipe(r) ?? []);
}

const tokensOf = (v: unknown): Record<string, string> =>
  typeof v === "object" && v !== null && !Array.isArray(v)
    ? Object.fromEntries(Object.entries(v).filter((e): e is [string, string] => typeof e[1] === "string"))
    : {};

// ---------------------------------------------------------------------------------------------------- compare

/** The clay reference on the packet clock: one frame per packet frame, at the rate the previs played the path. */
export function clayTimeMap(previs: TimeMap | null): TimeMap | null {
  if (!previs) return null;
  const frames = previs.end - previs.start + 1;
  return { ...previs, engineFps: previs.packetFps, engineFrames: frames, durationS: frames / previs.packetFps, method: "identity" };
}

const takeLabel = (id: string, engineId: string | null | undefined, attempt: number | null | undefined): string =>
  [`take ${id.slice(-8)}`, engineId ?? null, typeof attempt === "number" ? `attempt ${attempt}` : null].filter(Boolean).join(" · ");

/**
 * What the player's Compare can pair: the clay reference (a previs run's `inputs.reference_videos[0]`) and the node's
 * clips newest first, from `GET /projects/{pid}/runs?node_id=` when it answered, else from the candidate strip. The
 * take on show carries its loaded report and time map. Pure.
 */
export function compareSourceFor(
  takes: readonly TakeItem[] | null,
  cands: readonly Candidate[],
  current: { runId: string; hash: string | null; report: PlayerReport | null } | null,
): CompareSource {
  const withClay = takes?.find((t) => t.clayKey !== null) ?? null;
  const clay: CompareClip | null = withClay?.clayKey
    ? { key: "clay", kind: "clay", label: "clay reference", hash: withClay.clayKey, timeMap: clayTimeMap(withClay.timeMap) }
    : null;
  const clips: CompareClip[] = takes
    ? takes
        .filter((t) => (t.media === "video" || t.id === current?.runId) && t.outputs.length > 0 && (t.state === "DONE" || t.state === "GATE_FAIL"))
        .map((t) => ({ key: t.id, kind: "take", label: takeLabel(t.id, t.engineId, t.attempt), runId: t.id, hash: t.outputs[0] ?? null, timeMap: t.timeMap }))
    : cands
        .filter((c) => c.outputHash)
        .map((c) => ({ key: c.runId, kind: "take", label: takeLabel(c.runId, c.engineId, c.attempt), runId: c.runId, hash: c.outputHash }));
  const shown = current
    ? clips.map((c) =>
        c.key === current.runId
          ? { ...c, hash: current.hash ?? c.hash, report: current.report, timeMap: timeMapFromReport(current.report) ?? c.timeMap ?? null }
          : c,
      )
    : clips;
  return { clay, takes: shown, current: current?.runId ?? null };
}

const zoomText = (z0: number, z1: number): string => {
  const d = zoomDirection(z0, z1);
  if (d === "locked") return `locked ${z0.toFixed(2)}x`;
  return `${d === "in" ? "push-in" : "pull-back"} ${(z1 / z0).toFixed(2)}x`;
};

/** Re-render once a second while `active`, returning the current time (the quote countdown). */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [active]);
  return active ? now : Date.now();
}

const pct = (v: number | null): string => (v === null ? "n/a" : `${(100 * v).toFixed(1)}%`);
const fix = (v: number | null, d = 1): string => (v === null ? "n/a" : v.toFixed(d));

function RouterCardView({ card, error }: { card: RouterCard | null; error: string | null }): ReactElement {
  if (!card) {
    return (
      <Notice testId="motion-router-none">
        {error ? `router verdict unavailable: ${error}` : "no router verdict yet: it needs a motion packet and the project's router mode"}
      </Notice>
    );
  }
  const n = card.numbers;
  const tone: Tone = card.verdict === "render_move" ? "fail" : card.verdict === "rendered_endpoints_plus_clay" ? "advisory" : card.verdict === "ai_interpolate" ? "pass" : "muted";
  return (
    <div data-testid="motion-router" data-verdict={card.verdict} data-mode={card.mode} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={E.row}>
        <Chip tone={tone} testId="motion-router-verdict">
          {card.verdict.replace(/_/g, " ")}
        </Chip>
        <Chip tone={card.mode === "blocking" ? "fail" : "advisory"} testId="motion-router-mode" title={card.mode === "blocking" ? "a render_move verdict blocks Make" : "the verdict informs; it does not block"}>
          {card.mode}
        </Chip>
      </div>
      <table style={E.table}>
        <tbody>
          <tr>
            <th style={E.th}>D_px_median</th>
            <td style={E.td} data-testid="motion-router-d">
              {fix(n.D_px_median)} px{n.detail_verdict ? ` (${n.detail_verdict.replace(/_/g, " ")})` : ""}
            </td>
          </tr>
          <tr>
            <th style={E.th}>disocclusion</th>
            <td style={E.td} data-testid="motion-router-disocc">
              {pct(n.disocclusion_fraction)}
            </td>
          </tr>
          <tr>
            <th style={E.th}>orbit</th>
            <td style={E.td} data-testid="motion-router-orbit">
              {fix(n.orbit_deg)} deg{n.crossings > 0 ? ` · ${n.crossings} crossing${n.crossings === 1 ? "" : "s"}` : ""}
            </td>
          </tr>
        </tbody>
      </table>
      {card.explanation ? <span style={E.note}>{card.explanation}</span> : null}
    </div>
  );
}

export function MotionEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { node, params, set } = useNodeEditor(nodeId);
  const projectId = useGraphStore((s) => s.projectId);
  const setNodeStatus = useGraphStore((s) => s.setNodeStatus);
  const overrideNode = useGraphStore((s) => s.overrideNode);

  const entry = useMakeStore((s) => s.entries[nodeId] ?? EMPTY_ENTRY);
  const engines = useMakeStore((s) => s.engines);
  const custody = useMakeStore((s) => s.custody);
  const custodyFor = useMakeStore((s) => s.custodyFor);
  const { setDraft, requestQuote, make, loadEngines } = useMakeStore.getState();

  const [overrideText, setOverrideText] = useState("");
  const [card, setCard] = useState<RouterCard | null>(null);
  const [cardError, setCardError] = useState<string | null>(null);
  const [report, setReport] = useState<PlayerReport | null>(null);
  const [recipes, setRecipes] = useState<MotionRecipe[] | null>(null);
  const [recipesError, setRecipesError] = useState<string | null>(null);
  const [assembled, setAssembled] = useState<AssembledRecipe | null>(null);
  const [assembleError, setAssembleError] = useState<string | null>(null);
  const [takes, setTakes] = useState<TakeItem[] | null>(null);

  const nodes = useGraphStore((s) => s.nodes);
  const edges = useGraphStore((s) => s.edges);
  const wires = useMemo(() => motionWires(nodes, edges, nodeId), [nodes, edges, nodeId]);

  const moveParam = params.move;
  const move: MoveKind | null = isMoveKind(moveParam) ? moveParam : null;
  const dials: MoveDials = {
    ...(typeof params.revealM === "number" ? { revealM: params.revealM } : {}),
    ...(typeof params.orbitDeg === "number" ? { orbitDeg: params.orbitDeg } : {}),
    ...(typeof params.sliderCm === "number" ? { sliderCm: params.sliderCm } : {}),
  };
  const engineId = asString(params.engine_id);
  const [lo, hi] = rangeFor(engineId);
  const duration = asNumber(params.duration, Math.max(lo, 5));
  const recipeId = asString(params.recipe);
  const prompt = asString(params.prompt);
  const tokens = tokensOf(params.tokens);
  const tokensKey = JSON.stringify(tokens);
  const recipe = recipes?.find((r) => r.id === recipeId) ?? null;
  const single = params.single_image === true;

  // the zoom pair: the node's own when both are typed, else the recipe's (when the daemon sends its croppair block)
  const zStart = typeof params.zoomStart === "number" ? params.zoomStart : null;
  const zEnd = typeof params.zoomEnd === "number" ? params.zoomEnd : null;
  const ownZooms: [number, number] | null = zStart !== null && zEnd !== null ? [zStart, zEnd] : null;
  const zooms = ownZooms ?? recipe?.zooms ?? null;
  const presented = move ? presentMove(move, zooms?.[0] ?? null, zooms?.[1] ?? null) : null;
  const zoomErr = ownZooms ? zoomPairError(ownZooms[0], ownZooms[1], move) : null;

  const ev = move ? evaluateMove(move, dials, card) : null;
  const logged = node?.data.overrides.length ?? 0;
  const block = ev ? makeBlock(ev, overrideText, logged) : null;

  // engines: the daemon's list or the build's fallback, narrowed to what the project's custody allows
  useEffect(() => {
    if (!engines) void loadEngines();
  }, [engines, loadEngines]);
  const known = custodyFor === projectId ? custody : null;
  const options = engineOptions(engines?.engines ?? FALLBACK_ENGINES, "Motion", known, asString(params.task) || null).filter(
    (e) => isMotionEngine(e) && (known !== null || !needsKnownCustody(e)),
  );

  // the Wilson cards of the video engines for crop-pair motion (a daemon without them is no cards)
  const cards = useMakeStore((s) => (projectId ? s.cards[cardsKey(projectId, MOTION_CARD_TASK, null)] : undefined));
  useEffect(() => {
    if (projectId) void useMakeStore.getState().loadCards(projectId, MOTION_CARD_TASK, null);
  }, [projectId]);

  // the motion recipes, once per project
  useEffect(() => {
    let alive = true;
    setRecipesError(null);
    listMotionRecipes(projectId)
      .then((list) => alive && setRecipes(list))
      .catch((err: unknown) => {
        if (!alive) return;
        setRecipes([]);
        setRecipesError(errorMessage(err));
      });
    return () => {
      alive = false;
    };
  }, [projectId]);

  // the recipe's text as the daemon would send it (the node's prompt, when typed, replaces it)
  useEffect(() => {
    setAssembled(null);
    setAssembleError(null);
    if (!recipeId) return;
    let alive = true;
    assembleRecipe(recipeId, projectId, JSON.parse(tokensKey) as Record<string, string>)
      .then((a) => alive && setAssembled(a))
      .catch((err: unknown) => alive && setAssembleError(errorMessage(err)));
    return () => {
      alive = false;
    };
  }, [recipeId, projectId, tokensKey]);

  // the node's riskLabel follows the move, so the canvas rule HIGH_RISK_MOVE sees it
  useEffect(() => {
    if (!node || !ev) return;
    if (node.data.riskLabel !== ev.riskLabel) setNodeStatus(nodeId, { riskLabel: ev.riskLabel });
  }, [node, ev, nodeId, setNodeStatus]);

  // the router verdict for this move, from the daemon's numeric preview
  const dialKey = JSON.stringify(dials);
  useEffect(() => {
    if (!projectId || !move) {
      setCard(null);
      setCardError(null);
      return;
    }
    let alive = true;
    routerPreview(projectId, nodeId, { move, duration, engine_id: engineId || undefined, ...dials })
      .then((c) => {
        if (!alive) return;
        setCard(c);
        setCardError(null);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setCard(null);
        setCardError(errorMessage(err));
      });
    return () => {
      alive = false;
    };
    // the dials are keyed by their JSON, so a new object on each render does not refetch
  }, [projectId, nodeId, move, dialKey, duration, engineId]);

  // keep the Make bar's draft on the engine and duration chosen here
  useEffect(() => {
    if (engineId && entry.draft.engineId !== engineId) setDraft(nodeId, { engineId, params: { ...entry.draft.params, duration } });
  }, [engineId, entry.draft.engineId, entry.draft.params, duration, nodeId, setDraft]);

  // the take shown in the player: the picked candidate, else the best one
  const cands = useRunStore(useShallow((s) => nodeCandidates(s, nodeId)));
  const picked = useRunStore((s) => s.byNode[nodeId]?.picked ?? null);
  const take = cands.find((c) => c.runId === picked) ?? cands[0] ?? null;
  const clipHash = take?.outputHash ?? node?.data.outputHash ?? null;
  const takeRun = take?.runId ?? null;

  // the node's runs with their media facts, for Compare (the clay reference and the sibling clips)
  const candCount = cands.length;
  useEffect(() => {
    if (!projectId) {
      setTakes(null);
      return;
    }
    let alive = true;
    listTakes(projectId, nodeId)
      .then((t) => alive && setTakes(t))
      .catch(() => alive && setTakes(null));
    return () => {
      alive = false;
    };
  }, [projectId, nodeId, candCount, take?.state]);
  const compare = compareSourceFor(takes, cands, takeRun ? { runId: takeRun, hash: clipHash, report } : null);
  useEffect(() => {
    if (!takeRun) {
      setReport(null);
      return;
    }
    let alive = true;
    getGateReport(takeRun)
      .then((r) => alive && setReport(r as unknown as PlayerReport))
      .catch(() => alive && setReport(null));
    return () => {
      alive = false;
    };
  }, [takeRun, take?.state]);

  const now = useNow(entry.quote !== null);
  const fresh = isFresh(entry.quote, now);
  const canQuote = projectId !== null && engineId !== "" && entry.phase !== "quoting" && entry.phase !== "making";
  const canMake = fresh && (entry.phase === "quoted" || entry.phase === "error") && block === null;

  const chooseEngine = (id: string): void => {
    const [a, b] = rangeFor(id);
    const d = Math.min(Math.max(duration, a), b);
    set({ engine_id: id || undefined, duration: id ? d : undefined });
    setDraft(nodeId, { engineId: id || null, preset: "default", params: id ? { duration: d } : {} });
  };

  const setZoom = (key: "zoomStart" | "zoomEnd", v: number | null): void => {
    // the pair is written whole, so the daemon never mixes one typed zoom with one of the recipe's
    const other = key === "zoomStart" ? (zEnd ?? recipe?.zooms?.[1] ?? null) : (zStart ?? recipe?.zooms?.[0] ?? null);
    const otherKey = key === "zoomStart" ? "zoomEnd" : "zoomStart";
    if (v === null) set({ [key]: undefined });
    else set({ [key]: v, ...(other !== null && params[otherKey] === undefined ? { [otherKey]: other } : {}) });
  };

  const chooseDuration = (v: number | null): void => {
    if (v === null) return;
    set({ duration: v });
    if (engineId) setDraft(nodeId, { params: { ...entry.draft.params, duration: v } });
  };

  const doMake = (): void => {
    if (!ev || block !== null) return;
    // the override is logged on the node first: it is what the canvas rule and the daemon look for
    if (ev.needsOverride && logged === 0) overrideNode(nodeId, overrideText.trim(), UI_ACTOR);
    void make(nodeId, projectId);
  };

  const stillRow = (which: "start" | "end", n: GraphNode | null): ReactElement => {
    const testId = which === "start" ? "motion-start" : "motion-end";
    if (!n) {
      return (
        <div style={E.row} data-testid={testId}>
          <span style={E.label}>{which === "start" ? "start" : "end still"}</span>
          <span style={E.note}>{which === "start" ? "no start still wired" : "none: single-still move"}</span>
        </div>
      );
    }
    const open = stillUnlocked(n);
    return (
      <div style={E.row} data-testid={testId} data-node={n.id}>
        <span style={E.label}>{which === "start" ? "start" : "end still"}</span>
        <span>
          {n.data.label} <span style={E.note}>({n.id})</span>
        </span>
        <Chip
          tone={open ? "pass" : "fail"}
          testId={`${testId}-lock`}
          title={open ? "passed its gates and approved: the move may use it" : "VIDEO_LOCKED: the still must pass its gates and be approved by a human first"}
        >
          {open ? "approved" : "locked"}
        </Chip>
      </div>
    );
  };

  return (
    <EditorFrame kind="motion" nodeId={nodeId} title="Motion">
      <Section title="Stills" testId="motion-stills">
        {stillRow("start", wires.start)}
        {stillRow("end", wires.end)}
        {wires.end && single ? (
          <Notice tone="error" testId="motion-single-end">
            single image sends the start frame alone: unwire the end still or turn single image off
          </Notice>
        ) : null}
      </Section>

      <Section title="Move" testId="motion-move">
        <Field label="move">
          <select
            style={E.wide}
            value={move ?? ""}
            data-testid="motion-move-select"
            onChange={(e) => set({ move: e.target.value || undefined })}
          >
            <option value="">pick a move</option>
            {MOVE_KINDS.map((k) => (
              <option key={k} value={k}>
                {RISK_LADDER[k].label} · {RISK_LADDER[k].risk}
                {RISK_LADDER[k].repair.length > 0 ? ` · ${RISK_LADDER[k].repair.join(" ")}` : ""}
              </option>
            ))}
          </select>
        </Field>
        {move === "push_in" ? (
          <NumberField label="reveal" value={typeof params.revealM === "number" ? params.revealM : null} onChange={(v) => set({ revealM: v ?? undefined })} min={0} step={0.5} suffix="m beyond the start frame" testId="motion-dial-reveal" />
        ) : null}
        {move === "slider_drift" ? (
          <NumberField label="travel" value={typeof params.sliderCm === "number" ? params.sliderCm : null} onChange={(v) => set({ sliderCm: v ?? undefined })} min={0} step={1} suffix="cm" testId="motion-dial-slider" />
        ) : null}
        {move === "orbit_le_30" ? (
          <NumberField label="orbit" value={typeof params.orbitDeg === "number" ? params.orbitDeg : null} onChange={(v) => set({ orbitDeg: v ?? undefined })} min={0} max={360} step={1} suffix="deg" testId="motion-dial-orbit" />
        ) : null}
        {hasToggle(move, "single_image") ? (
          <Toggle
            label="single image (no end frame: the start frame held)"
            checked={single}
            onChange={(v) => set({ single_image: v || undefined })}
            testId="motion-single-image"
            title="sends no image_tail; only a frame that holds may do this"
          />
        ) : null}
        {ev ? (
          <div style={E.row} data-testid="motion-policy">
            {presented ? (
              <Chip tone="info" testId="motion-presented" title={presented.key === "pull_back" ? "a push-in played outwards (start zoom over end zoom): the push-in's risk row" : "the move as planned"}>
                {presented.label}
              </Chip>
            ) : null}
            <Chip tone={RISK_TONE[ev.risk] ?? "muted"} testId="motion-risk" title={ev.raisedBy ?? "base risk of this move"}>
              risk {ev.risk}
            </Chip>
            {ev.repair.map((code) => (
              <Chip key={code} tone="info" testId={`motion-repair-${code}`} title={REPAIR_CODES[code] ?? code}>
                {code} {REPAIR_CODES[code] ?? ""}
              </Chip>
            ))}
            {ev.raisedBy ? <span style={E.note}>raised: {ev.raisedBy}</span> : null}
          </div>
        ) : (
          <span style={E.note}>pick a move to see its risk and repair codes</span>
        )}
      </Section>

      {move && move !== "locked_tripod" ? (
        <Section title="Crop pair" testId="motion-zoom">
          <div style={E.row}>
            <NumberField label="start zoom" value={zStart} onChange={(v) => setZoom("zoomStart", v)} min={ZOOM_MIN} max={ZOOM_MAX} step={0.05} suffix="x" testId="motion-zoom-start" />
            <NumberField label="end zoom" value={zEnd} onChange={(v) => setZoom("zoomEnd", v)} min={ZOOM_MIN} max={ZOOM_MAX} step={0.05} suffix="x" testId="motion-zoom-end" />
          </div>
          <div style={E.row}>
            <Chip tone={zoomErr ? "fail" : "info"} testId="motion-direction" title="end zoom over start zoom: above 1 pushes in, below 1 pulls back">
              {zooms ? zoomText(zooms[0], zooms[1]) : "the recipe's zooms"}
            </Chip>
            {ownZooms ? (
              <Button onClick={() => set({ zoomStart: undefined, zoomEnd: undefined })} testId="motion-zoom-clear" title="drop the typed zooms and use the recipe's">
                Recipe zooms
              </Button>
            ) : (
              <span style={E.note}>{recipe?.zooms ? "from the recipe" : "empty: the recipe's crop pair"}</span>
            )}
          </div>
          {zoomErr ? (
            <Notice tone="error" testId="motion-zoom-error">
              {zoomErr}
            </Notice>
          ) : null}
        </Section>
      ) : null}

      <Section title="Recipe and prompt" testId="motion-recipe">
        <Field label="recipe">
          <select style={E.wide} value={recipeId} data-testid="motion-recipe-select" aria-label="motion recipe" onChange={(e) => set({ recipe: e.target.value || undefined })}>
            <option value="">{recipes === null ? "loading the recipes" : `no recipe: the daemon's default (${wires.end ? "two stills" : "push-in"})`}</option>
            {(recipes ?? []).map((r) => (
              <option key={r.id} value={r.id}>
                {motionRecipeLabel(r)}
              </option>
            ))}
            {recipeId && recipes !== null && !recipe ? <option value={recipeId}>{recipeId} (not on this daemon)</option> : null}
          </select>
        </Field>
        {recipesError ? <span style={E.note}>recipes unavailable: {recipesError}</span> : null}
        {recipe?.twoStills && !wires.end ? (
          <Notice tone="error" testId="motion-recipe-needs-end">
            {recipe.title} moves between two approved stills: wire the end still to the end input
          </Notice>
        ) : null}
        {recipe?.move && move && recipe.move !== move ? (
          <span style={E.note} data-testid="motion-recipe-move">
            the node's move ({RISK_LADDER[move].label}) is planned, not the recipe's ({RISK_LADDER[recipe.move].label})
          </span>
        ) : null}
        <Field label="prompt">
          <textarea
            style={{ ...E.wide, minHeight: 56, resize: "vertical" }}
            value={prompt}
            placeholder="empty: the recipe's text below; typed text replaces it (motion only, no pace words)"
            data-testid="motion-prompt"
            aria-label="motion prompt"
            onChange={(e) => set({ prompt: e.target.value || undefined })}
          />
        </Field>
        {assembled ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <span style={E.note}>{prompt.trim() ? "the recipe's text (your prompt replaces it)" : "the text the recipe sends"}</span>
            <div
              data-testid="motion-assembled"
              data-overridden={prompt.trim() ? "true" : "false"}
              style={{ ...E.note, whiteSpace: "pre-wrap", color: prompt.trim() ? C.muted : C.text, textDecoration: prompt.trim() ? "line-through" : "none" }}
            >
              {assembled.prompt}
            </div>
            {assembled.unfilledTokens.length > 0 ? <span style={E.note}>open tokens: {assembled.unfilledTokens.join(", ")}</span> : null}
          </div>
        ) : null}
        {assembleError ? <span style={E.note}>assembled text unavailable: {assembleError}</span> : null}
      </Section>

      <Section title="Elements" testId="motion-elements">
        {wires.elements.length === 0 ? (
          <span style={E.note} data-testid="motion-elements-none">
            none: wire Reference nodes to the elements input (an engine input, never composited)
          </span>
        ) : (
          <div style={E.row}>
            {wires.elements.map((n) => {
              const hash = asString(n.data.params?.referenceHash) || n.data.outputHash || "";
              const role = asString(n.data.params?.role) || "reference";
              return (
                <Chip key={n.id} tone="info" testId={`motion-element-${n.id}`} title={hash || "no image yet"}>
                  {n.data.label} · {role} · {hash ? hash.slice(0, 8) : "no image"}
                </Chip>
              );
            })}
          </div>
        )}
      </Section>

      <Section title="Engine" testId="motion-engine">
        <Field label="engine">
          <select style={E.wide} value={engineId} data-testid="motion-engine-select" onChange={(e) => chooseEngine(e.target.value)}>
            <option value="">{options.length === 0 ? "no engine allowed" : "pick a video engine"}</option>
            {options.map((o) => {
              const c = cardFor(cards, o.id);
              return (
                <option key={o.id} value={o.id}>
                  {o.name} · {bestAllowedGrade(o, known) ?? "-"}
                  {c ? ` · ${formatCard(c)}` : ""}
                </option>
              );
            })}
          </select>
        </Field>
        {options.length > 0 ? (
          <table style={E.table} data-testid="motion-cards">
            <tbody>
              {options.map((o) => {
                const c = cardFor(cards, o.id);
                return (
                  <tr key={o.id} data-testid={`motion-card-${o.id}`}>
                    <th style={E.th}>{o.name}</th>
                    <td style={E.td}>{c ? formatCard(c) : "no gated runs yet"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : null}
        <NumberField label="duration" value={duration} onChange={chooseDuration} min={lo} max={hi} step={1} suffix={`s (${lo} to ${hi})`} testId="motion-duration" />
        {known === null && projectId ? <span style={E.note}>project custody unknown here ({allowedGrades(null).join(", ")} shown); the daemon refuses what the project does not allow</span> : null}
      </Section>

      <Section title="Router verdict" testId="motion-router-section">
        <RouterCardView card={card} error={cardError} />
      </Section>

      {ev?.needsOverride ? (
        <Section title="Override" testId="motion-override">
          <Field label="reason">
            <input
              type="text"
              style={E.wide}
              value={overrideText}
              placeholder={`why this ${ev.risk} risk move is worth it (${MIN_OVERRIDE_CHARS}+ characters)`}
              data-testid="motion-override-text"
              onChange={(e) => setOverrideText(e.target.value)}
            />
          </Field>
          <span style={E.note} data-testid="motion-override-count">
            {logged > 0 ? `${logged} override${logged === 1 ? "" : "s"} logged on this node` : `${overrideText.trim().length} / ${MIN_OVERRIDE_CHARS} characters`}
          </span>
          {node?.data.labels.length ? (
            <div style={E.row}>
              {node.data.labels.map((l) => (
                <Chip key={l} tone="advisory" testId="motion-label">
                  {l}
                </Chip>
              ))}
            </div>
          ) : null}
        </Section>
      ) : null}

      <div style={E.row} data-testid="motion-actions">
        <Button onClick={() => projectId && void requestQuote(nodeId, projectId)} disabled={!canQuote} testId="motion-quote">
          {entry.phase === "quoting" ? "Quoting" : entry.quote ? "Re-quote" : "Quote"}
        </Button>
        <Button onClick={doMake} disabled={!canMake} testId="motion-make" title={block ?? (fresh ? "approve and run" : "Make needs a fresh quote")}>
          {entry.phase === "making" ? "Making" : "Make"}
        </Button>
        {entry.quote ? (
          <span style={E.note} data-testid="motion-quote-line">
            {entry.quote.quote.model} · {entry.quote.totalUsd.toFixed(2)} USD · {entry.quote.quote.custody_grade}
            {fresh ? "" : " · expired"}
          </span>
        ) : (
          <span style={E.note}>quote pending</span>
        )}
      </div>
      {block ? (
        <Notice tone="error" testId="motion-block">
          Make is blocked: {block}
        </Notice>
      ) : null}
      {entry.error ? (
        <Notice tone="error" testId="motion-error">
          {entry.error}
        </Notice>
      ) : null}

      <VideoPlayer
        hash={clipHash}
        timeMap={timeMapFromReport(report)}
        report={report}
        runId={takeRun}
        needsReason={take !== null && take.state === "GATE_FAIL" && take.override === null}
        compare={compare}
      />
      {take ? (
        <span style={{ ...E.note, color: C.muted }} data-testid="motion-take">
          take: run {take.runId} · {take.state}
        </span>
      ) : null}
    </EditorFrame>
  );
}
