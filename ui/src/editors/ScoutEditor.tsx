// Scout node editor (T47): how many scout poses to place from the scene bounds, and whether to keep the temporary
// cameras. Make queues the scout in Max: the dock creates `__PLATE_scout_N` cameras, renders low-res previews
// (1280 px wide) and, on request, full packets; the cameras are deleted afterwards unless kept.
// Node params: `count` (1 to 24), `keep`.

import type { ReactElement } from "react";
import { asBool, asNumber, useNodeEditor } from "./params";
import { E } from "./theme";
import { Chip, EditorFrame, NumberField, Section, Toggle } from "./widgets";

export const SCOUT_MIN = 1;
export const SCOUT_MAX = 24;
export const SCOUT_DEFAULT = 6;

export function ScoutEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const count = asNumber(params.count, SCOUT_DEFAULT);
  const keep = asBool(params.keep);
  return (
    <EditorFrame kind="scout" nodeId={nodeId} title="Scout">
      <Section title="Poses" testId="scout-fields">
        <NumberField
          label="count"
          value={count}
          onChange={(v) => v !== null && set({ count: Math.round(v) })}
          min={SCOUT_MIN}
          max={SCOUT_MAX}
          step={1}
          suffix={`poses (${SCOUT_MIN} to ${SCOUT_MAX})`}
          testId="scout-count"
        />
        <Toggle label="keep the scout cameras in the scene" checked={keep} onChange={(v) => set({ keep: v })} testId="scout-keep" />
        <div style={E.row}>
          <Chip tone={keep ? "advisory" : "muted"} testId="scout-cleanup">
            {keep ? "cameras stay in the scene" : "temporary cameras are deleted afterwards"}
          </Chip>
        </div>
        <span style={E.note}>Make asks the Max dock to place {count} scout pose{count === 1 ? "" : "s"} from the scene bounds and render previews.</span>
      </Section>
    </EditorFrame>
  );
}
