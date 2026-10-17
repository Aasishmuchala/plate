// The viewer slot (T35 wiring of T24's <Viewer> through T36's <ConnectedViewer>, which turns viewer changes into
// the scrub progress the verdict keys wait for and carries the gate panel's "Open tiles" into the viewer): clicking
// a candidate compares it (B) with the plate its node is anchored to (A). With no plate image known the candidate is
// shown alone. Before any click the viewer shows its empty frame.

import { useShallow } from "zustand/react/shallow";
import { useGraphStore } from "../store";
import { ConnectedViewer } from "../panels/ConnectedViewer";
import type { ImageSource } from "../viewer";
import { assetImageSource } from "./assets";
import { plateAncestor } from "./derive";
import { nodeCandidates, useRunStore } from "./runStore";

interface ComparePlan {
  a?: ImageSource;
  b?: ImageSource;
  caption: string | null;
}

function useComparePlan(): ComparePlan {
  const compare = useRunStore((s) => s.compare);
  const cands = useRunStore(useShallow((s) => (compare ? nodeCandidates(s, compare.nodeId) : [])));
  const nodes = useGraphStore((s) => s.nodes);
  const edges = useGraphStore((s) => s.edges);
  if (!compare) return { caption: null };
  const rank = cands.findIndex((c) => c.runId === compare.runId);
  const cand = cands[rank];
  if (!cand) return { caption: null };
  const plate = plateAncestor(nodes, edges, compare.nodeId);
  const size = plate?.data.outputSize;
  const b = cand.outputHash ? assetImageSource(cand.outputHash, size, `candidate ${rank + 1}`) : undefined;
  const a = plate?.data.outputHash ? assetImageSource(plate.data.outputHash, size, "plate") : undefined;
  const caption = !b
    ? `candidate ${rank + 1} has no output yet (${cand.state})`
    : a
      ? `A plate · B candidate ${rank + 1}`
      : `candidate ${rank + 1} (no plate image on this graph to compare with)`;
  return a ? { a, b, caption } : { a: b, caption };
}

export function CompareViewer() {
  const plan = useComparePlan();
  const close = useRunStore((s) => s.closeCompare);
  return (
    <section className="compare" data-testid="compare">
      {plan.caption ? (
        <div className="compare-head">
          <span data-testid="compare-caption">{plan.caption}</span>
          <button type="button" className="btn btn-ghost" onClick={close}>
            close
          </button>
        </div>
      ) : null}
      <ConnectedViewer a={plan.a} b={plan.b} />
    </section>
  );
}
