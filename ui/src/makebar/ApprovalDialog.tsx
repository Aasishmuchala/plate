// The approval dialog (T35): shown when a quote's total is at or over the per-run auto cap, or when the daemon
// refuses an automatic approval. A human reads the terms and approves; Escape or Cancel keeps the quote unspent.

import { useEffect, useRef } from "react";
import { CustodyChip } from "./CustodyChip";
import { formatEta, formatUsd, type QuoteState } from "./quote";

export interface ApprovalDialogProps {
  quote: QuoteState;
  fresh: boolean;
  /** The daemon's reason, when it asked for a human. */
  reason: string | null;
  onApprove: () => void;
  onCancel: () => void;
}

export function ApprovalDialog({ quote, fresh, reason, onApprove, onCancel }: ApprovalDialogProps) {
  const approveRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    approveRef.current?.focus();
  }, []);
  const q = quote.quote;
  const n = quote.runs.length;
  return (
    <div className="dialog-backdrop" onClick={onCancel}>
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="approve-title"
        data-testid="approval-dialog"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.stopPropagation();
            onCancel();
          }
        }}
      >
        <h2 id="approve-title">Approve this spend</h2>
        <p>
          {reason ??
            `${formatUsd(quote.totalUsd)} is at or over the auto-approve cap of ${formatUsd(quote.autoCapUsd)}: approve it yourself.`}
        </p>
        <dl className="dialog-terms">
          <dt>provider</dt>
          <dd>{q.provider}</dd>
          <dt>model</dt>
          <dd>
            {q.model}
            {q.model_version && q.model_version !== "unknown" ? ` ${q.model_version}` : ""}
          </dd>
          <dt>price</dt>
          <dd>
            {formatUsd(quote.totalUsd)}
            {n > 1 ? ` for ${n} candidates` : ""}
          </dd>
          <dt>custody</dt>
          <dd>
            <CustodyChip grade={q.custody_grade} />
          </dd>
          <dt>ETA</dt>
          <dd>{formatEta(q.eta_s)}</dd>
        </dl>
        <div className="dialog-actions">
          <button type="button" className="btn btn-ghost" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="btn btn-primary" ref={approveRef} disabled={!fresh} onClick={onApprove} data-testid="approval-approve">
            Approve and make
          </button>
        </div>
        {!fresh ? <p className="make-error">The quote expired: close this and quote again.</p> : null}
      </div>
    </div>
  );
}
