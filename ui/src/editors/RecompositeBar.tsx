// Recomposite bar (T99, decision 54): a free re-mask of the stored raw take. It sits under the viewer when the compared
// take kept its raw layer (`normalized.raw_key`): feather 4 to 12 px inside the mask, grow or shrink -32 to 32 px, and
// `Re-mask`, which posts `/runs/{id}/recomposite`. The daemon puts the stored take through the Edit node's Mask as it
// stands now, with the take's own size map, at 0 USD and without calling an engine. The new run joins the node's
// candidate strip and its id is shown here; picking it, comparing it and accepting it are the strip's and the panels'.
//
// App (T101) hands the compared take as props; mounted bare, the bar follows the panels' selected run and the run
// store's candidate for it. A take without a raw layer renders nothing.

import { type ReactElement, useState } from "react";
import {
  checkRecomposite,
  FEATHER_MAX,
  FEATHER_MIN,
  GROW_MAX,
  GROW_MIN,
  recomposite,
  type RecompositeRequest,
} from "../canvas/recompositeApi";
import { type CandidateRun, useRunStore } from "../canvas/runStore";
import type { RunWire } from "../canvas/runTypes";
import { errorMessage } from "../panels/api";
import { usePanelsStore } from "../panels/store";
import { E } from "./theme";
import { Button, Chip, Notice, NumberField, Section, useAction } from "./widgets";

export interface RecompositeBarProps {
  runId?: string;
  nodeId?: string | null;
  /** The take's stored raw layer; without one there is nothing to re-composite. */
  rawKey?: string | null;
}

/** Who the bar re-composites: the props, else the panels' selected run and its candidate's raw layer. */
function useTarget(props: RecompositeBarProps): { runId: string; nodeId: string | null } | null {
  const selected = usePanelsStore((s) => s.selectedRunId);
  const runId = props.runId ?? selected;
  const cand = useRunStore((s) => (runId ? s.candidates[runId] : undefined)) as
    | { rawKey?: string | null; nodeId?: string }
    | undefined;
  const rawKey = props.rawKey ?? cand?.rawKey ?? null;
  if (!runId || !rawKey) return null;
  return { runId, nodeId: props.nodeId ?? cand?.nodeId ?? null };
}

function priceOf(run: RunWire): string {
  const usd = run.quote?.price_usd;
  return typeof usd === "number" ? `${usd.toFixed(2)} USD` : "0.00 USD";
}

export function RecompositeBar(props: RecompositeBarProps = {}): ReactElement | null {
  const target = useTarget(props);
  if (!target) return null;
  return <Bar key={target.runId} runId={target.runId} nodeId={target.nodeId} />;
}

function Bar({ runId, nodeId }: { runId: string; nodeId: string | null }): ReactElement {
  const [feather, setFeather] = useState<number | null>(null); // null: the take's own
  const [grow, setGrow] = useState<number | null>(0);
  const [made, setMade] = useState<RunWire | null>(null);

  const request: RecompositeRequest = {};
  if (feather !== null) request.feather_px = feather;
  if (grow !== null && grow !== 0) request.grow_px = grow;
  const problem = checkRecomposite(request);

  const [go, state] = useAction(async () => {
    const run = await recomposite(runId, request);
    useRunStore.getState().upsertRun(run as CandidateRun, nodeId ?? undefined);
    setMade(run);
  }, errorMessage);

  return (
    <div data-testid="recomposite-bar" data-run={runId} aria-label="Re-mask the stored take" style={E.frame}>
      <Section title="Re-mask (free)">
        <div style={E.row}>
          <NumberField
            label="Feather"
            value={feather}
            onChange={setFeather}
            min={FEATHER_MIN}
            max={FEATHER_MAX}
            step={1}
            testId="recomposite-feather"
            suffix={`px, ${FEATHER_MIN} to ${FEATHER_MAX}; blank keeps the take's`}
          />
          <NumberField
            label="Grow / shrink"
            value={grow}
            onChange={setGrow}
            min={GROW_MIN}
            max={GROW_MAX}
            step={1}
            testId="recomposite-grow"
            suffix={`px, ${GROW_MIN} to ${GROW_MAX}`}
          />
          <Button
            onClick={() => void go()}
            disabled={state.pending || problem !== null}
            testId="recomposite-go"
            title="Put the stored take through the Mask node as it is now; no engine is called"
          >
            {state.pending ? "Re-masking…" : "Re-mask"}
          </Button>
          <Chip tone="pass" title="the stored take is re-composited locally" testId="recomposite-free">
            0.00 USD
          </Chip>
        </div>
        {problem ? <Notice tone="error" testId="recomposite-problem">{problem}</Notice> : null}
        {state.error ? <Notice tone="error" testId="recomposite-error">{state.error}</Notice> : null}
        {made ? (
          <Notice testId="recomposite-made">
            Re-masked as <code data-testid="recomposite-run">{made.id}</code> · {made.state} · {priceOf(made)}
          </Notice>
        ) : null}
      </Section>
    </div>
  );
}
