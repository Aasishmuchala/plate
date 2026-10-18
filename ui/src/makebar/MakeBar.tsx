// The Make bar (T35), docked at the bottom of the canvas, acting on the selected node. Engine (filtered by the
// node's kind and the project's custody; the advanced drawer holds the full manifest params), preset, N (1-6),
// region crop, then Quote: provider · model · price USD · custody grade (C0 green, C1 blue, C2 amber, D red) · ETA,
// with the quote's countdown. Make is enabled only while that quote is fresh; at or over the auto cap a dialog asks
// the human to approve. Before any quote the bar says "quote pending" and never shows a price.
//
// T102 (decisions 50, 51): an engine option reads `name · grade`, plus ` · ≥ lower bound % (n) · $mean` from its
// engine card for the node's task and shot class (bold once the card steers the router). Under the select a chip row
// says what the manifest declares (largest size, end frame, refs ≤ N, elements, price unit, unverified) and the route
// health (degraded amber; suspended red, and the option is disabled; an engine with no vendor model id is disabled
// too). `Compare engines…` quotes 2 to 5 engines as one group (`quote_many`), approved once against the group total.
// An Edit node has no preset select (its editor owns the recipe; `preset: "default"` stays in the body); other kinds
// keep one when their engine lists presets. The quote line names the price tier and flags an `estimated` price amber.

import { type CSSProperties, useEffect, useRef, useState } from "react";
import { useNodeInfo } from "../canvas/derive";
import { GENERATING_KINDS, type GraphNode } from "../canvasRules";
import type { CustodyGrade } from "../nodeTypes";
import { useGraphStore } from "../store";
import {
  capabilitiesOf,
  cardFor,
  COMPARE_MAX,
  COMPARE_MIN,
  type EngineCard,
  engineHealth,
  unavailableReason,
} from "./api";
import { ApprovalDialog } from "./ApprovalDialog";
import { CustodyChip } from "./CustodyChip";
import {
  allowedGrades,
  bestAllowedGrade,
  drawerParams,
  effectiveGrade,
  type EngineParamSpec,
  type EngineRouteSummary,
  type EngineSummary,
  engineOptions,
  MAKE_MODES,
  presetsFor,
  type ProjectCustody,
} from "./engines";
import { cardsKey, compareIdsOf, compareReady, EMPTY_ENTRY, type MakeEntry, useMakeStore } from "./makeStore";
import {
  CUSTODY_TONE,
  formatCard,
  formatCountdown,
  formatEta,
  formatTier,
  formatUsd,
  type GroupLine,
  isFresh,
  priceStatusTone,
  type QuoteState,
  remainingMs,
  tierOf,
  worstGrade,
} from "./quote";

/** Re-render every `ms` while `active`, returning the current time. */
function useNow(active: boolean, ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [active, ms]);
  return active ? now : Date.now();
}

/** "provider · model · price · custody · ETA — quote pending": what the bar shows before a quote exists. */
function PendingQuote({ note }: { note?: string }) {
  return (
    <span className="quote-line is-pending" data-testid="quote-line">
      <span>provider</span> · <span>model</span> · <span>price</span> · <span>custody</span> · <span>ETA</span>
      <em> quote pending</em>
      {note ? <span className="make-note"> · {note}</span> : null}
    </span>
  );
}

function ParamInput({ spec, value, onChange }: { spec: EngineParamSpec; value: unknown; onChange: (v: unknown) => void }) {
  const id = `param-${spec.name}`;
  const shown = value ?? spec.default;
  if (spec.type === "enum" && spec.enum) {
    return (
      <select id={id} value={shown === undefined ? "" : String(shown)} onChange={(e) => onChange(e.target.value === "" ? undefined : e.target.value)}>
        <option value="">(engine default)</option>
        {spec.enum.map((v) => (
          <option key={String(v)} value={String(v)}>
            {String(v)}
          </option>
        ))}
      </select>
    );
  }
  if (spec.type === "bool") {
    return <input id={id} type="checkbox" checked={shown === true} onChange={(e) => onChange(e.target.checked)} />;
  }
  if (spec.type === "int" || spec.type === "float") {
    const [lo, hi] = spec.range ?? [undefined, undefined];
    return (
      <input
        id={id}
        type="number"
        min={lo}
        max={hi}
        step={spec.type === "int" ? 1 : "any"}
        value={shown === undefined ? "" : String(shown)}
        onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))}
      />
    );
  }
  return <input id={id} type="text" value={shown === undefined ? "" : String(shown)} onChange={(e) => onChange(e.target.value || undefined)} />;
}

function AdvancedDrawer({ engine, entry, nodeId }: { engine: EngineSummary | undefined; entry: MakeEntry; nodeId: string }) {
  const setDraft = useMakeStore((s) => s.setDraft);
  const { basic, advanced } = drawerParams(engine);
  const all = [...basic, ...advanced];
  const set = (name: string, v: unknown) => {
    const params = { ...entry.draft.params };
    if (v === undefined) delete params[name];
    else params[name] = v;
    setDraft(nodeId, { params });
  };
  return (
    <div className="make-drawer" data-testid="make-drawer">
      {!engine ? (
        <p className="make-note">Pick an engine to see its parameters.</p>
      ) : all.length === 0 ? (
        <p className="make-note">The daemon sent no parameters for {engine.name}; the engine defaults apply.</p>
      ) : (
        <table>
          <tbody>
            {all.map((p) => (
              <tr key={p.name} data-testid="make-param">
                <th>
                  <label htmlFor={`param-${p.name}`}>{p.name}</label>
                  {p.advanced ? <span className="chip chip-dim">advanced</span> : null}
                </th>
                <td className="make-param-type">{p.type}</td>
                <td>
                  <ParamInput spec={p} value={entry.draft.params[p.name]} onChange={(v) => set(p.name, v)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/** `Nano Banana 2.1 · C1`, plus ` · ≥ 70% (20) · $0.034` when the engine has a card for this task and shot class. */
export function engineLabel(engine: EngineSummary, grade: CustodyGrade | null, card: EngineCard | null): string {
  const base = `${engine.name} · ${grade ?? "D"}`;
  return card ? `${base} · ${formatCard(card)}` : base;
}

const PRICE_UNITS: Readonly<Record<string, string>> = {
  per_image: "per image",
  per_second: "per second",
  per_megapixel: "per megapixel",
  flat: "flat price",
};

/** `per_image` -> `per image`; an unknown unit is shown as the daemon spells it. */
export const priceUnitText = (unit: string): string => PRICE_UNITS[unit] ?? `per ${unit.replace(/^per_/, "").replace(/_/g, " ")}`;

interface Chip {
  key: string;
  text: string;
  tone?: "green" | "blue" | "amber" | "red";
  title?: string;
  strong?: boolean;
}

const isVideoEngine = (engine: EngineSummary): boolean => engine.kind.startsWith("video") || engine.kind === "local_video";

/** One chip per active route of a video engine, the preferred route first: `runway · D · 0.43 USD per second`
 * (no price: `atlas · D`), toned by the route's effective grade. T123: the artist sees which route a quote tries
 * first and what it costs per second before asking for one. */
function routeChips(engine: EngineSummary): Chip[] {
  const active = engine.routes.filter((r: EngineRouteSummary) => (r.status ?? "active") === "active");
  const ordered = [...active.filter((r) => r.preferred === true), ...active.filter((r) => r.preferred !== true)];
  return ordered.map((r) => {
    const grade = effectiveGrade(r);
    const price = r.price ? ` · ${formatUsd(r.price.usd)} ${priceUnitText(r.price.model)}` : "";
    return {
      key: `route-${r.route_id ?? r.provider}`,
      text: `${r.provider} · ${grade}${price}`,
      tone: CUSTODY_TONE[grade],
      title: r.preferred === true ? "preferred route for video" : "fallback route",
    };
  });
}

/** The chips under the engine select: manifest capabilities, video routes, route health and the engine card. */
export function engineChips(engine: EngineSummary, custody: ProjectCustody | null, card: EngineCard | null): Chip[] {
  const caps = capabilitiesOf(engine);
  const chips: Chip[] = [];
  if (caps?.largest_size) {
    chips.push({
      key: "size",
      text: `max ${caps.largest_size.replace("x", "×")}`,
      title: "the largest size this engine answers at; the plate is never upscaled to fit it",
    });
  }
  if (caps?.image_tail) chips.push({ key: "end_frame", text: "end frame", title: "takes an end still: a start/end move" });
  if (typeof caps?.reference_images_max === "number" && caps.reference_images_max > 0) {
    chips.push({ key: "refs", text: `refs ≤ ${caps.reference_images_max}`, title: "reference images it takes beside the plate" });
  }
  if (caps?.elements) chips.push({ key: "elements", text: "elements", title: "takes element references for a move" });
  if (caps?.price_unit) chips.push({ key: "price_unit", text: priceUnitText(caps.price_unit), title: "how the vendor bills it" });
  if (caps?.unverified) {
    chips.push({ key: "unverified", text: "unverified", tone: "amber", title: "nothing about this manifest was checked against the vendor yet (P0 smoke test)" });
  }
  if (isVideoEngine(engine)) chips.push(...routeChips(engine));
  const health = engineHealth(engine, custody);
  if (health.status === "degraded") {
    chips.push({
      key: "health",
      text: "degraded",
      tone: "amber",
      title: `repeated provider failures on ${health.routes.join(", ")} in the last hour: the router skips it`,
    });
  } else if (health.status === "suspended" || health.status === "down") {
    chips.push({ key: "health", text: health.status, tone: "red", title: `${health.status} on ${health.routes.join(", ")}` });
  }
  if (card) {
    chips.push({
      key: "card",
      text: formatCard(card),
      strong: card.steering,
      title: `${card.passes} of ${card.n} gate passes for ${card.task}${card.shot_class ? ` / ${card.shot_class}` : ""} (Wilson 95% lower bound)${card.steering ? "; steers the router" : "; too few runs to steer the router yet"}`,
    });
  }
  return chips;
}

function EngineChips({ chips }: { chips: Chip[] }) {
  if (chips.length === 0) return null;
  return (
    <div className="make-row make-chips" data-testid="make-chips">
      {chips.map((c) => (
        <span
          key={c.key}
          className={`chip${c.tone ? ` tone-${c.tone}` : ""}`}
          data-testid="make-chip"
          data-chip={c.key}
          data-tone={c.tone ?? "plain"}
          title={c.title}
        >
          {c.strong ? <strong>{c.text}</strong> : c.text}
        </span>
      ))}
    </div>
  );
}

const STEERING_STYLE: CSSProperties = { fontWeight: 700 };

/** One option per engine: disabled (with the reason as its title) when it has no model id or is suspended. */
function EngineOption({ engine, custody, card }: { engine: EngineSummary; custody: ProjectCustody | null; card: EngineCard | null }) {
  const reason = unavailableReason(engine, custody);
  return (
    <option
      value={engine.id}
      disabled={reason !== null}
      title={reason ?? undefined}
      data-steering={card?.steering ? "true" : undefined}
      style={card?.steering ? STEERING_STYLE : undefined}
    >
      {engineLabel(engine, bestAllowedGrade(engine, custody), card)}
    </option>
  );
}

/** The engines to compare: 2 to 5 of the node's engines, each a checkbox; unavailable ones cannot be ticked. */
function CompareList({
  options,
  custody,
  cards,
  picked,
  onChange,
}: {
  options: EngineSummary[];
  custody: ProjectCustody | null;
  cards: EngineCard[] | undefined;
  picked: string[];
  onChange: (ids: string[]) => void;
}) {
  const full = picked.length >= COMPARE_MAX;
  return (
    <div className="make-row make-compare" role="group" aria-label="engines to compare" data-testid="make-compare-engines">
      {options.length === 0 ? <span className="make-note">no engine to compare</span> : null}
      {options.map((e) => {
        const reason = unavailableReason(e, custody);
        const on = picked.includes(e.id);
        const card = cardFor(cards, e.id);
        return (
          <label key={e.id} className="make-field make-check" title={reason ?? undefined}>
            <input
              type="checkbox"
              aria-label={`compare ${e.id}`}
              data-testid="make-compare-engine"
              data-engine={e.id}
              checked={on}
              disabled={reason !== null || (!on && full)}
              onChange={(ev) => onChange(ev.target.checked ? [...picked, e.id] : picked.filter((id) => id !== e.id))}
            />
            <span style={card?.steering ? STEERING_STYLE : undefined}>{engineLabel(e, bestAllowedGrade(e, custody), card)}</span>
          </label>
        );
      })}
      <span className="make-note" data-testid="make-compare-count">
        {picked.length} picked ({COMPARE_MIN} to {COMPARE_MAX}) · one take per engine, each at its own defaults
      </span>
    </div>
  );
}

/** The price status next to a price: `estimated` amber (derived, not billed yet), any other unconfirmed status dim. */
function PriceStatus({ status }: { status: string | undefined }) {
  if (!status || status === "confirmed") return null;
  const tone = priceStatusTone(status);
  return (
    <span
      className={`chip ${tone === "amber" ? "tone-amber" : "chip-dim"}`}
      data-testid="quote-price-status"
      data-tone={tone}
      title={status === "estimated" ? "a derived figure, not a vendor's: the first bill confirms or corrects it" : "list price, not yet reconciled against a bill"}
    >
      {status}
    </span>
  );
}

function TierChip({ tier }: { tier: string | null }) {
  const text = formatTier(tier);
  if (!text) return null;
  return (
    <span className="chip chip-dim" data-testid="quote-tier" title={`priced at the manifest tier ${tier}`}>
      tier {text}
    </span>
  );
}

/** A compared group's quote: each engine at its price, then the total, the least private grade and the longest ETA. */
function GroupQuoteLine({ q, fresh, now }: { q: QuoteState; fresh: boolean; now: number }) {
  const lines = q.group ?? [];
  return (
    <span className={`quote-line${fresh ? "" : " is-expired"}`} data-testid="quote-line" data-group="true">
      <span data-testid="quote-group">
        {lines.map((l, i) => (
          <span key={l.runId} data-testid="quote-group-engine" data-engine={l.engineId}>
            {i > 0 ? " · " : null}
            {l.engineId} {formatUsd(l.priceUsd)}
            <PriceStatus status={l.priceStatus} />
          </span>
        ))}
      </span>{" "}
      · <span data-testid="quote-price">{formatUsd(q.totalUsd)} ({lines.length} engines)</span> ·{" "}
      <CustodyChip grade={worstGrade(lines.map((l) => l.custody))} /> ·{" "}
      <span data-testid="quote-eta">ETA {formatEta(Math.max(0, ...lines.map((l) => l.etaS)))}</span>
      <span className="quote-countdown" data-testid="quote-countdown" data-fresh={fresh}>
        {fresh ? `expires in ${formatCountdown(remainingMs(q, now))}` : "quote expired: quote again"}
      </span>
    </span>
  );
}

/** The approval for a compared group: every engine with its terms, and the one total a click approves. */
function GroupApprovalDialog({
  quote,
  fresh,
  reason,
  blocking,
  onApprove,
  onCancel,
}: {
  quote: QuoteState;
  fresh: boolean;
  reason: string | null;
  blocking: boolean;
  onApprove: () => void;
  onCancel: () => void;
}) {
  const approveRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    (approveRef.current ?? closeRef.current)?.focus();
  }, []);
  const lines: GroupLine[] = quote.group ?? [];
  return (
    <div className="dialog-backdrop" onClick={onCancel}>
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="approve-group-title"
        data-testid="approval-dialog"
        data-blocking={blocking}
        data-group="true"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.stopPropagation();
            onCancel();
          }
        }}
      >
        <h2 id="approve-group-title">{blocking ? "This spend is blocked" : `Approve ${lines.length} engines`}</h2>
        <p>
          {reason ??
            `${formatUsd(quote.totalUsd)} for the group is at or over the auto-approve cap of ${formatUsd(quote.autoCapUsd)}: approve it yourself.`}
        </p>
        <table className="dialog-terms" data-testid="approval-engines">
          <thead>
            <tr>
              <th>engine</th>
              <th>provider · model</th>
              <th>price</th>
              <th>custody</th>
              <th>ETA</th>
            </tr>
          </thead>
          <tbody>
            {lines.map((l) => (
              <tr key={l.runId} data-testid="approval-engine" data-engine={l.engineId}>
                <td>{l.engineId}</td>
                <td>
                  {l.provider} · {l.model}
                </td>
                <td>
                  {formatUsd(l.priceUsd)}
                  {l.priceStatus === "estimated" ? " (estimated)" : ""}
                </td>
                <td>
                  <CustodyChip grade={l.custody} />
                </td>
                <td>{formatEta(l.etaS)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th colSpan={2}>group total</th>
              <td data-testid="approval-total">{formatUsd(quote.totalUsd)}</td>
              <td colSpan={2} />
            </tr>
          </tfoot>
        </table>
        <div className="dialog-actions">
          {blocking ? (
            <button type="button" className="btn" ref={closeRef} onClick={onCancel} data-testid="approval-close">
              Close
            </button>
          ) : (
            <>
              <button type="button" className="btn btn-ghost" onClick={onCancel}>
                Cancel
              </button>
              <button type="button" className="btn btn-primary" ref={approveRef} disabled={!fresh} onClick={onApprove} data-testid="approval-approve">
                Approve and make
              </button>
            </>
          )}
        </div>
        {!fresh && !blocking ? <p className="make-error">The quote expired: close this and quote again.</p> : null}
      </div>
    </div>
  );
}

function blockerFor(node: GraphNode, info: ReturnType<typeof useNodeInfo>, projectId: string | null): string | null {
  const mode = MAKE_MODES[node.data.kind];
  if (mode.mode === "none") return `nothing to make on a ${node.data.kind} node`;
  if (GENERATING_KINDS.has(node.data.kind) && info.plateId === null) {
    return "connect it to a plate first (generation is anchored to a plate camera)";
  }
  if (info.violation) return info.violation.message;
  if (!projectId) return "open a project to quote";
  return null;
}

function NodeMakeBar({ node }: { node: GraphNode }) {
  const nodeId = node.id;
  const projectId = useGraphStore((s) => s.projectId);
  const info = useNodeInfo(nodeId);
  const entry = useMakeStore((s) => s.entries[nodeId] ?? EMPTY_ENTRY);
  const engines = useMakeStore((s) => s.engines);
  const custody = useMakeStore((s) => s.custody);
  const custodyFor = useMakeStore((s) => s.custodyFor);
  const { setDraft, requestQuote, make, closeApproval, loadCustody, loadCards } = useMakeStore.getState();
  const [drawer, setDrawer] = useState(false);

  useEffect(() => {
    if (projectId && custodyFor !== projectId) void loadCustody(projectId);
  }, [projectId, custodyFor, loadCustody]);

  const known = custodyFor === projectId ? custody : null;
  const mode = MAKE_MODES[node.data.kind];
  const task = typeof node.data.params?.["task"] === "string" ? (node.data.params["task"] as string) : null;
  const shotClass = typeof node.data.params?.["shot_class"] === "string" ? (node.data.params["shot_class"] as string) : null;
  const needsEngine = mode.mode === "engine";
  const cards = useMakeStore((s) => (projectId ? s.cards[cardsKey(projectId, task, shotClass)] : undefined));
  useEffect(() => {
    if (projectId && needsEngine) void loadCards(projectId, task, shotClass);
  }, [projectId, needsEngine, task, shotClass, loadCards]);

  const options = engineOptions(engines?.engines ?? [], node.data.kind, known, task);
  const engine = options.find((e) => e.id === entry.draft.engineId);
  const comparing = needsEngine && entry.draft.compare === true;
  const compareIds = compareIdsOf(entry.draft);
  const blocker = blockerFor(node, info, projectId);
  const q = entry.quote;
  const now = useNow(q !== null);
  const fresh = isFresh(q, now);
  const idle = entry.phase !== "quoting" && entry.phase !== "making";
  const canQuote = !blocker && (comparing ? compareReady(entry.draft) : !needsEngine || !!engine) && idle;
  // A failed approval (network, a 5xx) keeps a fresh quote: Make may be retried without re-quoting.
  const canMake = fresh && (entry.phase === "quoted" || entry.phase === "error") && !blocker;
  const card = engine ? cardFor(cards, engine.id) : null;
  // An Edit's recipe is its editor's (decision 50): no preset select; other kinds keep one when the engine lists any.
  const presets = presetsFor(engine);
  const showPreset = needsEngine && !comparing && node.data.kind !== "Edit" && !!engine && presets.some((p) => p !== "default");

  // Keep the draft's engine valid for this node: drop one the filters no longer allow, or that can no longer run.
  useEffect(() => {
    if (!engines || !needsEngine || !entry.draft.engineId) return;
    const still = options.find((e) => e.id === entry.draft.engineId);
    if (!still || unavailableReason(still, known) !== null) setDraft(nodeId, { engineId: null });
  }, [engines, needsEngine, entry.draft.engineId, options, known, nodeId, setDraft]);

  // The same for the engines being compared.
  useEffect(() => {
    if (!engines || !comparing) return;
    const kept = compareIds.filter((id) => {
      const e = options.find((o) => o.id === id);
      return e !== undefined && unavailableReason(e, known) === null;
    });
    if (kept.length !== compareIds.length) setDraft(nodeId, { compareIds: kept });
  }, [engines, comparing, compareIds, options, known, nodeId, setDraft]);

  const toggleCompare = () => {
    if (comparing) {
      setDraft(nodeId, { compare: false, compareIds: [] });
      return;
    }
    const seed = engine && unavailableReason(engine, known) === null ? [engine.id] : [];
    setDrawer(false);
    setDraft(nodeId, { compare: true, compareIds: seed });
  };

  return (
    <>
      <div className="make-row">
        <span className="make-target" data-testid="make-target">
          {node.data.kind} <span className="make-target-id">{nodeId}</span>
        </span>
        {mode.mode === "engine" && !comparing ? (
          <label className="make-field">
            <span>engine</span>
            <select
              aria-label="engine"
              data-testid="make-engine"
              value={entry.draft.engineId ?? ""}
              onChange={(e) => setDraft(nodeId, { engineId: e.target.value || null, preset: "default", params: {} })}
            >
              <option value="">{options.length === 0 ? "no engine allowed" : "pick an engine"}</option>
              {options.map((e) => (
                <EngineOption key={e.id} engine={e} custody={known} card={cardFor(cards, e.id)} />
              ))}
            </select>
          </label>
        ) : mode.mode === "inprocess" ? (
          <span className="make-note">runs in-process · C0</span>
        ) : null}
        {mode.mode === "engine" ? (
          <button
            type="button"
            className="btn btn-ghost"
            aria-pressed={comparing}
            onClick={toggleCompare}
            data-testid="make-compare"
            title={`quote ${COMPARE_MIN} to ${COMPARE_MAX} engines side by side as one group, approved once`}
          >
            {comparing ? "One engine" : "Compare engines…"}
          </button>
        ) : null}
        {showPreset ? (
          <label className="make-field">
            <span>preset</span>
            <select aria-label="preset" data-testid="make-preset" value={entry.draft.preset} onChange={(e) => setDraft(nodeId, { preset: e.target.value })}>
              {presets.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {mode.mode !== "none" ? (
          <>
            {comparing ? null : (
              <label className="make-field">
                <span>N</span>
                <input
                  aria-label="candidates"
                  data-testid="make-n"
                  type="number"
                  min={1}
                  max={6}
                  step={1}
                  value={entry.draft.n}
                  onChange={(e) => setDraft(nodeId, { n: Number(e.target.value) })}
                />
              </label>
            )}
            {mode.mode === "engine" ? (
              <label className="make-field make-check">
                <input
                  type="checkbox"
                  data-testid="make-region"
                  checked={entry.draft.regionCrop}
                  onChange={(e) => setDraft(nodeId, { regionCrop: e.target.checked })}
                />
                <span title="mask bounding box + 25% context at the engine's best size; the plate is never upscaled">region crop</span>
              </label>
            ) : null}
            {mode.mode === "engine" && !comparing ? (
              <button type="button" className="btn btn-ghost" aria-expanded={drawer} onClick={() => setDrawer((d) => !d)} data-testid="make-advanced">
                advanced {drawer ? "▴" : "▾"}
              </button>
            ) : null}
            <button
              type="button"
              className="btn"
              disabled={!canQuote}
              onClick={() => projectId && void requestQuote(nodeId, projectId)}
              data-testid="make-quote"
            >
              {entry.phase === "quoting" ? "Quoting…" : q ? "Re-quote" : "Quote"}
            </button>
          </>
        ) : null}
      </div>
      {comparing ? (
        <CompareList options={options} custody={known} cards={cards} picked={compareIds} onChange={(ids) => setDraft(nodeId, { compareIds: ids })} />
      ) : engine ? (
        <EngineChips chips={engineChips(engine, known, card)} />
      ) : null}
      <div className="make-row make-row-quote">
        {q?.group ? (
          <GroupQuoteLine q={q} fresh={fresh} now={now} />
        ) : q ? (
          <span className={`quote-line${fresh ? "" : " is-expired"}`} data-testid="quote-line">
            <span data-testid="quote-provider">{q.quote.provider}</span> · <span data-testid="quote-model">{q.quote.model}</span> ·{" "}
            <span data-testid="quote-price">
              {formatUsd(q.totalUsd)}
              {q.runs.length > 1 ? ` (${q.runs.length} × ${formatUsd(q.quote.price_usd)})` : ""}
            </span>{" "}
            · <CustodyChip grade={q.quote.custody_grade} /> · <span data-testid="quote-eta">ETA {formatEta(q.quote.eta_s)}</span>
            <TierChip tier={tierOf(q.quote)} />
            <PriceStatus status={q.quote.price_status} />
            {q.quote.envelope_id ? (
              <span className="chip chip-envelope" data-testid="quote-envelope">
                envelope {q.quote.envelope_id}
              </span>
            ) : null}
            <span className="quote-countdown" data-testid="quote-countdown" data-fresh={fresh}>
              {fresh ? `expires in ${formatCountdown(remainingMs(q, now))}` : "quote expired: quote again"}
            </span>
          </span>
        ) : (
          <PendingQuote
            note={
              blocker ??
              (entry.phase === "made"
                ? "made: the candidates run in the strip under the node"
                : comparing && !compareReady(entry.draft)
                  ? `pick ${COMPARE_MIN} to ${COMPARE_MAX} engines to compare`
                  : needsEngine && !comparing && !engine
                    ? "pick an engine"
                    : undefined)
            }
          />
        )}
        <span className="make-spacer" />
        {entry.error ? (
          <span className="make-error" role="alert" data-testid="make-error">
            {entry.error}
          </span>
        ) : null}
        {mode.mode !== "none" ? (
          <button
            type="button"
            className="btn btn-primary"
            disabled={!canMake}
            onClick={() => void make(nodeId, projectId)}
            data-testid="make-make"
            title={canMake ? "approve and run" : "Make needs a fresh quote"}
          >
            {entry.phase === "making" ? "Making…" : "Make"}
          </button>
        ) : null}
      </div>
      {drawer && mode.mode === "engine" && !comparing ? <AdvancedDrawer engine={engine} entry={entry} nodeId={nodeId} /> : null}
      {entry.approvalOpen && q?.group ? (
        <GroupApprovalDialog
          quote={q}
          fresh={fresh}
          reason={entry.approvalReason}
          blocking={entry.approvalBlocking === true}
          onApprove={() => void make(nodeId, projectId, { confirmed: true })}
          onCancel={() => closeApproval(nodeId)}
        />
      ) : entry.approvalOpen && q ? (
        <ApprovalDialog
          quote={q}
          fresh={fresh}
          reason={entry.approvalReason}
          blocking={entry.approvalBlocking === true}
          onApprove={() => void make(nodeId, projectId, { confirmed: true })}
          onCancel={() => closeApproval(nodeId)}
        />
      ) : null}
      {engines?.source === "fallback" && mode.mode === "engine" ? (
        <div className="make-note make-row" data-testid="make-engines-fallback">
          engine list from this build (the daemon has no /engines yet); price and custody come from the quote
        </div>
      ) : null}
      {known === null && projectId && mode.mode === "engine" ? (
        <div className="make-note make-row">project custody unknown here ({allowedGrades(null).join(", ")} shown); the daemon refuses what the project does not allow</div>
      ) : null}
    </>
  );
}

export function MakeBar() {
  const selected = useGraphStore((s) => s.nodes.find((n) => n.selected));
  const engines = useMakeStore((s) => s.engines);
  const loadEngines = useMakeStore((s) => s.loadEngines);
  useEffect(() => {
    if (!engines) void loadEngines();
  }, [engines, loadEngines]);
  return (
    <footer className="make-bar" data-testid="make-bar">
      {selected ? (
        <NodeMakeBar key={selected.id} node={selected} />
      ) : (
        <div className="make-row">
          <PendingQuote note="select a node to make" />
        </div>
      )}
    </footer>
  );
}
