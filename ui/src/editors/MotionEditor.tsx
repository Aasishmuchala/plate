// Motion node editor (T47): the move kind with its risk label and repair codes (Lensbook's risk ladder), the dials that
// raise a move's risk, duration, engine (Kling or Seedance), the router verdict card (D_px_median, disocclusion
// fraction, orbit, advisory or blocking chip) and the override reason. Make is disabled while the move is blocked:
//   * a high or highest risk move until the override reason has 10+ characters (logged on the node when Make runs),
//   * a router `render_move` verdict in blocking mode, which no override lifts.
// The quote and Make buttons act on the same per-node entry as the Make bar, so the bar shows the same quote and its
// approval dialog. Node params: `move`, `duration`, `engine_id`, and the dials `revealM`, `orbitDeg`, `sliderCm`.
// The video player below shows the picked (or best) take with its time map and gate chips.

import { type ReactElement, useEffect, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { getGateReport, UI_ACTOR } from "../canvas/runApi";
import { nodeCandidates, useRunStore } from "../canvas/runStore";
import { allowedGrades, bestAllowedGrade, type EngineSummary, engineOptions, FALLBACK_ENGINES } from "../makebar/engines";
import { EMPTY_ENTRY, useMakeStore } from "../makebar/makeStore";
import { isFresh } from "../makebar/quote";
import { useGraphStore } from "../store";
import { errorMessage, routerPreview } from "./api";
import {
  evaluateMove,
  isMoveKind,
  makeBlock,
  MIN_OVERRIDE_CHARS,
  MOVE_KINDS,
  type MoveDials,
  type MoveKind,
  REPAIR_CODES,
  RISK_LADDER,
  type RouterCard,
} from "./movePolicy";
import { asNumber, asString, useNodeEditor } from "./params";
import { C, E, type Tone } from "./theme";
import { timeMapFromReport, VideoPlayer, type PlayerReport } from "./VideoPlayer";
import { Button, Chip, EditorFrame, Field, NumberField, Notice, Section } from "./widgets";

/** The video engines the editor offers and the duration range of each (manifest `duration` param). */
const MOTION_ENGINES: readonly { prefix: string; range: [number, number] }[] = [
  { prefix: "kling", range: [1, 15] },
  { prefix: "seedance", range: [4, 30] },
];

const rangeFor = (engineId: string): [number, number] => MOTION_ENGINES.find((e) => engineId.startsWith(e.prefix))?.range ?? [1, 15];
const isMotionEngine = (e: EngineSummary): boolean => MOTION_ENGINES.some((m) => e.id.startsWith(m.prefix));

const RISK_TONE: Record<string, Tone> = { none: "muted", low: "pass", med: "advisory", high: "fail", highest: "fail" };

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

  const ev = move ? evaluateMove(move, dials, card) : null;
  const logged = node?.data.overrides.length ?? 0;
  const block = ev ? makeBlock(ev, overrideText, logged) : null;

  // engines: the daemon's list or the build's fallback, narrowed to what the project's custody allows
  useEffect(() => {
    if (!engines) void loadEngines();
  }, [engines, loadEngines]);
  const known = custodyFor === projectId ? custody : null;
  const options = engineOptions(engines?.engines ?? FALLBACK_ENGINES, "Motion", known, asString(params.task) || null).filter(isMotionEngine);

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

  return (
    <EditorFrame kind="motion" nodeId={nodeId} title="Motion">
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
        {ev ? (
          <div style={E.row} data-testid="motion-policy">
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

      <Section title="Engine" testId="motion-engine">
        <Field label="engine">
          <select style={E.wide} value={engineId} data-testid="motion-engine-select" onChange={(e) => chooseEngine(e.target.value)}>
            <option value="">{options.length === 0 ? "no engine allowed" : "pick Kling or Seedance"}</option>
            {options.map((o) => (
              <option key={o.id} value={o.id}>
                {o.name} · {bestAllowedGrade(o, known) ?? "-"}
              </option>
            ))}
          </select>
        </Field>
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

      <VideoPlayer hash={clipHash} timeMap={timeMapFromReport(report)} report={report} />
      {take ? (
        <span style={{ ...E.note, color: C.muted }} data-testid="motion-take">
          take: run {take.runId} · {take.state}
        </span>
      ) : null}
    </EditorFrame>
  );
}
