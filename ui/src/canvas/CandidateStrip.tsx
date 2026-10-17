// The best-of-N strip under a node (T35): one row per sibling run with its live state chip, ranked by gate score
// once every sibling is done. Click a row to compare it with the plate in the viewer; with the strip focused, keys
// 1..6 pick that row as the node's output (the keys stay inside the strip, so they never reach the global verdict
// keys or the viewer). A GATE_FAIL row shows its first failing check and an "Override (reason required)" button;
// the override is ledgered by the daemon and only then shown here. Agents' runs show the envelope they spent from.

import { type FormEvent, type KeyboardEvent, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { StateChip } from "../nodes/badges";
import { useThumb } from "./assets";
import { overrideRun, UI_ACTOR } from "./runApi";
import { type Candidate, MAX_CANDIDATES, nodeCandidates, useRunStore } from "./runStore";

const fmtValue = (v: number | null): string => (v === null ? "n/a" : Number.isInteger(v) ? String(v) : v.toFixed(3));

function Thumb({ hash }: { hash: string | null }) {
  const thumb = useThumb(hash);
  if (thumb.state === "ready") return <img className="cand-thumb" src={thumb.url} alt="" draggable={false} />;
  return <span className={`cand-thumb cand-thumb-${thumb.state}`} aria-hidden="true" />;
}

function OverrideForm({ cand, onDone }: { cand: Candidate; onDone: () => void }) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ok = reason.trim().length > 0;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!ok || busy) return;
    setBusy(true);
    setError(null);
    overrideRun(cand.runId, reason).then(
      () => {
        useRunStore.getState().markOverride(cand.runId, reason.trim(), UI_ACTOR);
        onDone();
      },
      (err: unknown) => {
        setBusy(false);
        setError(err instanceof Error ? err.message : "override failed");
      },
    );
  };
  return (
    <form className="cand-override nodrag" onSubmit={submit} data-testid="override-form">
      <input
        className="nodrag"
        aria-label="override reason"
        placeholder="why ship it anyway (ledgered)"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        onKeyDown={(e) => e.stopPropagation()}
        autoFocus
      />
      <button type="submit" className="btn btn-primary" disabled={!ok || busy} data-testid="override-submit">
        Override
      </button>
      <button type="button" className="btn btn-ghost" onClick={onDone}>
        Cancel
      </button>
      {error ? (
        <span className="cand-error" role="alert">
          {error}
        </span>
      ) : null}
    </form>
  );
}

function Row({ cand, rank, picked, comparing }: { cand: Candidate; rank: number; picked: boolean; comparing: boolean }) {
  const [overriding, setOverriding] = useState(false);
  const open = () => useRunStore.getState().openCompare(cand.nodeId, cand.runId);
  const fail = cand.state === "GATE_FAIL";
  return (
    <li
      className={`cand${picked ? " is-picked" : ""}${comparing ? " is-comparing" : ""}`}
      data-testid="candidate"
      data-run={cand.runId}
      data-state={cand.state}
    >
      <button type="button" className="cand-main nodrag" onClick={open} title="compare with the plate in the viewer">
        <span className="cand-rank">{rank}</span>
        <Thumb hash={cand.outputHash} />
        <StateChip state={cand.state} testId="candidate-state" />
        {cand.gateScore !== null ? <span className="cand-score">{cand.gateScore.toFixed(2)}</span> : null}
        {picked ? <span className="cand-picked">picked</span> : null}
      </button>
      {cand.envelopeId ? (
        <span className="chip chip-envelope" data-testid="candidate-envelope" title="an agent's run: spent from this envelope">
          envelope {cand.envelopeId}
        </span>
      ) : null}
      {fail ? (
        <div className="cand-fail" data-testid="candidate-fail">
          <span className="cand-check">
            {cand.firstFail ? `${cand.firstFail.metric_id} ${fmtValue(cand.firstFail.value)}` : "gate failed"}
          </span>
          {cand.override ? (
            <span className="chip chip-override" data-testid="candidate-overridden" title={cand.override.reason}>
              overridden
            </span>
          ) : overriding ? null : (
            <button type="button" className="btn btn-ghost nodrag" onClick={() => setOverriding(true)} data-testid="override-open">
              Override (reason required)
            </button>
          )}
        </div>
      ) : null}
      {fail && overriding && !cand.override ? <OverrideForm cand={cand} onDone={() => setOverriding(false)} /> : null}
      {cand.error && (cand.state === "FAILED" || cand.state === "REFUSED") ? (
        <div className="cand-error">{cand.error}</div>
      ) : null}
    </li>
  );
}

export function CandidateStrip({ nodeId }: { nodeId: string }) {
  const cands = useRunStore(useShallow((s) => nodeCandidates(s, nodeId)));
  const picked = useRunStore((s) => s.byNode[nodeId]?.picked ?? null);
  const compare = useRunStore((s) => s.compare);
  if (cands.length === 0) return null;
  const onKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    const n = Number(e.key);
    if (!Number.isInteger(n) || n < 1 || n > MAX_CANDIDATES) return;
    e.preventDefault();
    e.stopPropagation();
    const cand = cands[n - 1];
    if (!cand) return;
    useRunStore.getState().pick(nodeId, cand.runId);
    useRunStore.getState().openCompare(nodeId, cand.runId);
  };
  return (
    <section
      className="cand-strip nodrag nowheel"
      tabIndex={0}
      onKeyDown={onKeyDown}
      aria-label={`candidates: keys 1 to ${cands.length} pick`}
      data-testid="candidate-strip"
    >
      <ol>
        {cands.map((c, i) => (
          <Row
            key={c.runId}
            cand={c}
            rank={i + 1}
            picked={picked === c.runId}
            comparing={compare?.runId === c.runId}
          />
        ))}
      </ol>
    </section>
  );
}
