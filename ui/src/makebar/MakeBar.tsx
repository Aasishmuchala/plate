// The Make bar (T35), docked at the bottom of the canvas, acting on the selected node. Engine (filtered by the
// node's kind and the project's custody; the advanced drawer holds the full manifest params), preset, N (1-6),
// region crop, then Quote: provider · model · price USD · custody grade (C0 green, C1 blue, C2 amber, D red) · ETA,
// with the quote's countdown. Make is enabled only while that quote is fresh; at or over the auto cap a dialog asks
// the human to approve. Before any quote the bar says "quote pending" and never shows a price.

import { useEffect, useState } from "react";
import { useNodeInfo } from "../canvas/derive";
import { GENERATING_KINDS, type GraphNode } from "../canvasRules";
import { useGraphStore } from "../store";
import { ApprovalDialog } from "./ApprovalDialog";
import { CustodyChip } from "./CustodyChip";
import {
  allowedGrades,
  bestAllowedGrade,
  drawerParams,
  type EngineParamSpec,
  type EngineSummary,
  engineOptions,
  MAKE_MODES,
  presetsFor,
} from "./engines";
import { EMPTY_ENTRY, type MakeEntry, useMakeStore } from "./makeStore";
import { formatCountdown, formatEta, formatUsd, isFresh, remainingMs } from "./quote";

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
  const { setDraft, requestQuote, make, closeApproval, loadCustody } = useMakeStore.getState();
  const [drawer, setDrawer] = useState(false);

  useEffect(() => {
    if (projectId && custodyFor !== projectId) void loadCustody(projectId);
  }, [projectId, custodyFor, loadCustody]);

  const known = custodyFor === projectId ? custody : null;
  const mode = MAKE_MODES[node.data.kind];
  const task = typeof node.data.params?.["task"] === "string" ? (node.data.params["task"] as string) : null;
  const options = engineOptions(engines?.engines ?? [], node.data.kind, known, task);
  const engine = options.find((e) => e.id === entry.draft.engineId);
  const blocker = blockerFor(node, info, projectId);
  const needsEngine = mode.mode === "engine";
  const q = entry.quote;
  const now = useNow(q !== null);
  const fresh = isFresh(q, now);
  const canQuote = !blocker && (!needsEngine || !!engine) && entry.phase !== "quoting" && entry.phase !== "making";
  // A failed approval (network, a 5xx) keeps a fresh quote: Make may be retried without re-quoting.
  const canMake = fresh && (entry.phase === "quoted" || entry.phase === "error") && !blocker;

  // Keep the draft's engine valid for this node: drop one the filters no longer allow.
  useEffect(() => {
    if (engines && needsEngine && entry.draft.engineId && !options.some((e) => e.id === entry.draft.engineId)) {
      setDraft(nodeId, { engineId: null });
    }
  }, [engines, needsEngine, entry.draft.engineId, options, nodeId, setDraft]);

  return (
    <>
      <div className="make-row">
        <span className="make-target" data-testid="make-target">
          {node.data.kind} <span className="make-target-id">{nodeId}</span>
        </span>
        {mode.mode === "engine" ? (
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
                <option key={e.id} value={e.id}>
                  {e.name} · {bestAllowedGrade(e, known)}
                </option>
              ))}
            </select>
          </label>
        ) : mode.mode === "inprocess" ? (
          <span className="make-note">runs in-process · C0</span>
        ) : null}
        {mode.mode === "engine" ? (
          <label className="make-field">
            <span>preset</span>
            <select aria-label="preset" data-testid="make-preset" value={entry.draft.preset} onChange={(e) => setDraft(nodeId, { preset: e.target.value })}>
              {presetsFor(engine).map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {mode.mode !== "none" ? (
          <>
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
            {mode.mode === "engine" ? (
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
      <div className="make-row make-row-quote">
        {q ? (
          <span className={`quote-line${fresh ? "" : " is-expired"}`} data-testid="quote-line">
            <span data-testid="quote-provider">{q.quote.provider}</span> · <span data-testid="quote-model">{q.quote.model}</span> ·{" "}
            <span data-testid="quote-price">
              {formatUsd(q.totalUsd)}
              {q.runs.length > 1 ? ` (${q.runs.length} × ${formatUsd(q.quote.price_usd)})` : ""}
            </span>{" "}
            · <CustodyChip grade={q.quote.custody_grade} /> · <span data-testid="quote-eta">ETA {formatEta(q.quote.eta_s)}</span>
            {q.quote.price_status && q.quote.price_status !== "confirmed" ? (
              <span className="chip chip-dim" title="list price, not yet reconciled against a bill">{q.quote.price_status}</span>
            ) : null}
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
                : needsEngine && !engine
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
      {drawer && mode.mode === "engine" ? <AdvancedDrawer engine={engine} entry={entry} nodeId={nodeId} /> : null}
      {entry.approvalOpen && q ? (
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
