// The best-of-N strip under a node (T35): one row per sibling run with its live state chip, ranked by gate score
// once every sibling is done. Click a row to compare it with the plate in the viewer; with the strip focused, keys
// 1..6 pick that row as the node's output (the keys stay inside the strip, so they never reach the global verdict
// keys or the viewer; the strip is marked `data-plate-keys` for the panels' key scope). Comparing or picking a row
// also selects that run in the panels (T36), so the gate panel, the scrub and the verdict keys follow the strip.
// A GATE_FAIL row shows its first failing check and an "Override (reason required)" button; the override is
// ledgered by the daemon and only then shown here. Agents' runs show the envelope they spent from. A run that is
// queued or in flight (QUEUED..FETCHING) has a Cancel button (T71); the chip moves when the daemon's event arrives.
// T101: each row is labelled `<node label> · <engine_id> · attempt n` and has Set as A / Set as B, which put the run
// on either side of the viewer for any pairing (two takes, two engines); only the row click compares with the plate
// and keeps the scrub bound to it.

import { type FormEvent, type KeyboardEvent, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { StateChip } from "../nodes/badges";
import { errorMessage } from "../panels/api";
import { usePanelsStore } from "../panels/store";
import { useGraphStore } from "../store";
import { useThumb } from "./assets";
import { cancelRun, overrideRun, UI_ACTOR } from "./runApi";
import { type Candidate, candidateLabel, MAX_CANDIDATES, nodeCandidates, runLabel, useRunStore } from "./runStore";

/** Show a candidate against its plate in the viewer and make it the panels' selected run. */
export function compareCandidate(nodeId: string, runId: string): void {
  useRunStore.getState().openCompare(nodeId, runId);
  usePanelsStore.getState().selectRun(runId);
}

/** Put a candidate on side A or B of the viewer, labelled as the strip labels it. */
export function setCandidateSide(side: "a" | "b", cand: Candidate): void {
  const ref = { kind: "run" as const, runId: cand.runId, nodeId: cand.nodeId, label: candidateLabel(cand) };
  useRunStore.getState().setCompareSide(side, ref);
}

/** Make a candidate the node's output (and compare it). */
export function pickCandidate(nodeId: string, runId: string): void {
  useRunStore.getState().pick(nodeId, runId);
  compareCandidate(nodeId, runId);
}

/** States a run can still be cancelled from: queued, or with a vendor (or on its way to / from one). */
const CANCELLABLE: ReadonlySet<string> = new Set(["QUEUED", "STAGING", "SUBMITTED", "RUNNING", "FETCHING"]);

export const isCancellable = (state: string): boolean => CANCELLABLE.has(state);

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

function Row({
  cand,
  rank,
  picked,
  comparing,
  side,
}: {
  cand: Candidate;
  rank: number;
  picked: boolean;
  comparing: boolean;
  side: "a" | "b" | null;
}) {
  const nodeLabel = useGraphStore((s) => s.nodes.find((n) => n.id === cand.nodeId)?.data.label);
  const [overriding, setOverriding] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const open = () => compareCandidate(cand.nodeId, cand.runId);
  const fail = cand.state === "GATE_FAIL";
  const cancel = () => {
    if (cancelling) return;
    setCancelling(true);
    setCancelError(null);
    cancelRun(cand.runId, "cancelled from the candidate strip").then(
      () => setCancelling(false),
      (err: unknown) => {
        setCancelling(false);
        setCancelError(errorMessage(err));
      },
    );
  };
  return (
    <li
      className={`cand${picked ? " is-picked" : ""}${comparing ? " is-comparing" : ""}`}
      data-testid="candidate"
      data-run={cand.runId}
      data-state={cand.state}
      data-side={side ?? undefined}
    >
      <button type="button" className="cand-main nodrag" onClick={open} title="compare with the plate in the viewer">
        <span className="cand-rank">{rank}</span>
        <Thumb hash={cand.outputHash} />
        <StateChip state={cand.state} testId="candidate-state" />
        {cand.gateScore !== null ? <span className="cand-score">{cand.gateScore.toFixed(2)}</span> : null}
        {picked ? <span className="cand-picked">picked</span> : null}
      </button>
      <span className="cand-label" data-testid="candidate-label">
        {runLabel(nodeLabel ?? cand.nodeId, cand.engineId, cand.attempt)}
      </span>
      <span className="cand-sides">
        <button
          type="button"
          className="btn btn-ghost nodrag"
          onClick={() => setCandidateSide("a", cand)}
          aria-pressed={side === "a"}
          title="show this take on side A of the viewer"
          data-testid="cand-set-a"
        >
          Set as A
        </button>
        <button
          type="button"
          className="btn btn-ghost nodrag"
          onClick={() => setCandidateSide("b", cand)}
          aria-pressed={side === "b"}
          title="show this take on side B of the viewer"
          data-testid="cand-set-b"
        >
          Set as B
        </button>
      </span>
      {cand.envelopeId ? (
        <span className="chip chip-envelope" data-testid="candidate-envelope" title="an agent's run: spent from this envelope">
          envelope {cand.envelopeId}
        </span>
      ) : null}
      {isCancellable(cand.state) ? (
        <button
          type="button"
          className="btn btn-ghost nodrag"
          onClick={cancel}
          disabled={cancelling}
          title="cancel this run"
          data-testid="candidate-cancel"
        >
          Cancel
        </button>
      ) : null}
      {cancelError ? (
        <span className="cand-error" role="alert" data-testid="candidate-cancel-error">
          {cancelError}
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
    if (cand) pickCandidate(nodeId, cand.runId);
  };
  return (
    <section
      className="cand-strip nodrag nowheel"
      tabIndex={0}
      onKeyDown={onKeyDown}
      aria-label={`candidates: keys 1 to ${cands.length} pick`}
      data-plate-keys=""
      data-testid="candidate-strip"
    >
      <ol>
        {cands.map((c, i) => (
          <Row
            key={c.runId}
            cand={c}
            rank={i + 1}
            picked={picked === c.runId}
            comparing={compare?.b?.runId === c.runId || compare?.a.runId === c.runId}
            side={compare?.a.runId === c.runId ? "a" : compare?.b?.runId === c.runId ? "b" : null}
          />
        ))}
      </ol>
    </section>
  );
}
