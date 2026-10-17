// A Frame (T81, decision 57): an annotation box drawn behind the nodes it groups. It has no ports and no badges, every
// canvas rule ignores it (an edge touching one is refused with ANNOTATION), and it is saved in `project.json` like any
// node. There is no parentId grouping or collapse: moving a Frame moves only the Frame. `nodrag` is NOT set, so it
// drags like any node; xyflow's wrapper is pushed behind the other nodes by the `.kind-frame` rules in
// canvas/canvas.css.

import type { NodeProps } from "@xyflow/react";
import { memo } from "react";
import type { GraphNode } from "../canvasRules";
import type { PlateNodeData } from "../nodeTypes";

export const FRAME_DEFAULT_WIDTH = 480;
export const FRAME_DEFAULT_HEIGHT = 320;

const size = (v: unknown, fallback: number): number =>
  typeof v === "number" && Number.isFinite(v) && v > 0 ? Math.round(v) : fallback;

/** The title a Frame shows top-left: `params.title`, else "frame". */
export function frameTitle(d: PlateNodeData): string {
  const t = d.params?.["title"];
  return typeof t === "string" && t.trim() !== "" ? t : "frame";
}

export const FrameNode = memo(function FrameNode({ id, data, selected }: NodeProps<GraphNode>) {
  const width = size(data.params?.["w"], FRAME_DEFAULT_WIDTH);
  const height = size(data.params?.["h"], FRAME_DEFAULT_HEIGHT);
  return (
    <div
      className={`plate-frame kind-frame${selected ? " is-selected" : ""}`}
      data-testid="node-Frame"
      data-node-id={id}
      style={{ width, height, zIndex: -1 }}
    >
      <div className="frame-title" data-testid="frame-title">
        {frameTitle(data)}
      </div>
    </div>
  );
});
