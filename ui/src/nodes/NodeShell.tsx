// The frame every node kind shares (T35): a header (kind badge, tier badge, gate badge, approval tick, risk label,
// live run state), a body (thumbnail of the node's output from the CAS, one line of kind-specific facts, inherited
// provenance labels, a broken rule on a loaded edge), typed handles with their port labels, the inline refusal a
// refused connection leaves for 4 s, and the candidate strip of the node's last Make. A Concept node carries a
// permanent "CONCEPT, UNMEASURED" watermark; its output handle is marked as never reaching delivery.

import { Handle, Position } from "@xyflow/react";
import type { ReactNode } from "react";
import { useShallow } from "zustand/react/shallow";
import { CandidateStrip } from "../canvas/CandidateStrip";
import { useNodeInfo } from "../canvas/derive";
import { useThumb } from "../canvas/assets";
import { nodeCandidates, useRunStore } from "../canvas/runStore";
import type { RunState } from "../canvas/runTypes";
import { MAX_GENERATION_DEPTH } from "../canvasRules";
import { type PlateNodeData, UNSEEN_GEOMETRY_LABEL } from "../nodeTypes";
import { ApprovalTick, GateBadge, KindBadge, RiskBadge, StateChip, TierBadge, WatermarkIcon } from "./badges";
import { acceptsOf, PORTS } from "./ports";

export const CONCEPT_WATERMARK = "CONCEPT, UNMEASURED";

function Preview({ hash, kind }: { hash?: string; kind: string }) {
  const thumb = useThumb(hash);
  if (thumb.state === "ready") {
    return <img className="node-thumb" src={thumb.url} alt={`${kind} output`} draggable={false} data-testid="node-thumb" />;
  }
  const text = !hash ? "no output yet" : thumb.state === "loading" ? "loading…" : "preview unavailable";
  return (
    <div className={`node-thumb node-thumb-empty${hash ? "" : " is-none"}`} data-testid="node-thumb-empty">
      {text}
    </div>
  );
}

/** The latest run state for the header: the picked candidate's, else the first candidate's in strip order. */
function useHeaderState(id: string): RunState | null {
  return useRunStore(
    useShallow((s) => {
      const cands = nodeCandidates(s, id);
      if (cands.length === 0) return null;
      const picked = s.byNode[id]?.picked;
      return (cands.find((c) => c.runId === picked) ?? cands[0]).state;
    }),
  );
}

function RefusalBubble({ id }: { id: string }) {
  const flash = useRunStore((s) => (s.flash && s.flash.nodeId === id ? s.flash : null));
  if (!flash) return null;
  return (
    <div className="node-refusal" role="alert" data-testid="node-refusal" data-code={flash.code} key={flash.seq}>
      <strong>{flash.code}</strong> {flash.message}
    </div>
  );
}

function Ports({ data }: { data: PlateNodeData }) {
  const { inputs, outputs } = PORTS[data.kind];
  const rows = Math.max(inputs.length, outputs.length);
  if (rows === 0) return null;
  const concept = data.kind === "Concept";
  return (
    <div className="node-ports">
      {Array.from({ length: rows }, (_, i) => {
        const inp = inputs[i];
        const out = outputs[i];
        return (
          <div className="node-port-row" key={i}>
            {inp ? (
              <>
                <Handle
                  type="target"
                  position={Position.Left}
                  id={inp.id}
                  className={`handle handle-${inp.type}`}
                  title={`${inp.label}: takes ${acceptsOf(inp).join(" or ")}`}
                  data-testid={`handle-in-${inp.id}`}
                  data-type={inp.type}
                />
                <span className="port-label port-in">{inp.label}</span>
              </>
            ) : (
              <span />
            )}
            {out ? (
              <>
                <span className="port-label port-out">{out.label}</span>
                <Handle
                  type="source"
                  position={Position.Right}
                  id={out.id}
                  className={`handle handle-${out.type}${concept ? " handle-concept" : ""}`}
                  title={concept ? `${out.label}: unmeasured, never to Export, Review or Delivery` : `${out.label}: ${out.type}`}
                  data-testid={`handle-out-${out.id}`}
                  data-type={out.type}
                />
              </>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

export interface NodeShellProps {
  id: string;
  data: PlateNodeData;
  selected?: boolean;
  /** One line of kind-specific facts under the preview. */
  summary?: ReactNode;
}

export function NodeShell({ id, data, selected, summary }: NodeShellProps) {
  const info = useNodeInfo(id);
  const runState = useHeaderState(id);
  const concept = data.kind === "Concept" || info.tier === "concept_unmeasured";
  const inherited = info.labels.filter((l) => !data.labels.includes(l));
  const labels = [...data.labels, ...inherited];
  return (
    <div
      className={`plate-node kind-${data.kind.toLowerCase()}${concept ? " is-concept" : ""}${selected ? " is-selected" : ""}`}
      data-testid={`node-${data.kind}`}
      data-node-id={id}
    >
      <header className="node-head">
        <div className="node-head-row">
          <KindBadge kind={data.kind} />
          {data.label !== data.kind ? <span className="node-label">{data.label}</span> : null}
          <span className="node-head-spacer" />
          {runState ? <StateChip state={runState} /> : null}
        </div>
        <div className="node-head-row node-badges">
          <TierBadge tier={info.tier} />
          <GateBadge status={data.gateStatus} />
          <ApprovalTick approved={data.approved} by={data.approvedBy} />
          <RiskBadge risk={data.riskLabel} />
        </div>
      </header>
      <div className="node-body">
        <div className="node-preview">
          <Preview hash={data.outputHash} kind={data.kind} />
          {concept ? (
            <div className="node-watermark" data-testid="concept-watermark" aria-label={CONCEPT_WATERMARK}>
              <WatermarkIcon size={14} />
              <span>{CONCEPT_WATERMARK}</span>
            </div>
          ) : null}
        </div>
        {summary ? <div className="node-summary">{summary}</div> : null}
        {data.generationDepth > 0 ? (
          <div className="node-depth" title="generations from the plate (rule 7: at most 2)">
            gen {data.generationDepth}/{MAX_GENERATION_DEPTH}
          </div>
        ) : null}
        {labels.length > 0 ? (
          <div className="node-labels">
            {labels.map((l) => (
              <span key={l} className={`chip chip-label${l === UNSEEN_GEOMETRY_LABEL ? " is-warn" : ""}`} data-testid="node-label">
                {l}
              </span>
            ))}
          </div>
        ) : null}
        {info.violation ? (
          <div className="node-violation" data-testid="node-violation" data-code={info.violation.code}>
            {info.violation.message}
          </div>
        ) : null}
      </div>
      <Ports data={data} />
      <CandidateStrip nodeId={id} />
      <RefusalBubble id={id} />
    </div>
  );
}
