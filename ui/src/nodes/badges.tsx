// The header badges every node shares: kind, tier (concept in amber with the watermark icon), gate verdict,
// approval tick, risk label and the live run state. Small and stateless; the colours live in canvas/canvas.css.

import type { GateStatus, NodeKind, RiskLabel, Tier } from "../nodeTypes";
import type { RunState } from "../canvas/runTypes";

/** A droplet: the watermark glyph on everything unmeasured. */
export function WatermarkIcon({ size = 11 }: { size?: number }) {
  return (
    <svg className="icon-watermark" width={size} height={size} viewBox="0 0 12 12" aria-hidden="true" focusable="false">
      <path d="M6 1 C6 1 2.2 5.4 2.2 7.6 A3.8 3.8 0 0 0 9.8 7.6 C9.8 5.4 6 1 6 1 Z" fill="currentColor" />
    </svg>
  );
}

export function KindBadge({ kind }: { kind: NodeKind }) {
  return (
    <span className={`badge badge-kind kind-${kind.toLowerCase()}`} data-testid="badge-kind">
      {kind}
    </span>
  );
}

export function TierBadge({ tier }: { tier: Tier }) {
  if (tier === "concept_unmeasured") {
    return (
      <span className="badge badge-tier tier-concept" data-testid="badge-tier" data-tier={tier} title="concept tier: never measured, never delivered">
        <WatermarkIcon /> concept_unmeasured
      </span>
    );
  }
  return (
    <span className="badge badge-tier tier-measured" data-testid="badge-tier" data-tier={tier}>
      measured
    </span>
  );
}

const GATE_TEXT: Record<Exclude<GateStatus, "none">, string> = { pass: "gate pass", fail: "gate fail", advisory: "advisory" };

export function GateBadge({ status }: { status: GateStatus }) {
  if (status === "none") return null;
  return (
    <span className={`badge badge-gate gate-${status}`} data-testid="badge-gate" data-status={status}>
      {GATE_TEXT[status]}
    </span>
  );
}

export function ApprovalTick({ approved, by }: { approved: boolean; by?: string }) {
  if (!approved) return null;
  return (
    <span className="badge badge-approved" data-testid="badge-approved" title={by ? `approved by ${by}` : "approved"}>
      ✓ approved
    </span>
  );
}

export function RiskBadge({ risk }: { risk?: RiskLabel }) {
  if (!risk) return null;
  return (
    <span className={`badge badge-risk risk-${risk}`} data-testid="badge-risk" data-risk={risk}>
      risk {risk}
    </span>
  );
}

const STATE_TONE: Partial<Record<RunState, string>> = {
  DONE: "ok",
  GATE_FAIL: "bad",
  FAILED: "bad",
  REFUSED: "bad",
  CANCELLED: "dim",
  EXPIRED: "dim",
  QUOTED: "dim",
  DRAFT: "dim",
};

/** A run state as a chip: green done, red failures, grey idle, blue (accent) while it moves. */
export function StateChip({ state, testId = "state-chip" }: { state: RunState; testId?: string }) {
  const tone = STATE_TONE[state] ?? "live";
  return (
    <span className={`chip chip-state tone-${tone}`} data-testid={testId} data-state={state}>
      {state}
    </span>
  );
}
