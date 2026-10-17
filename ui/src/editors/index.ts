// Node editors (T47). `editorFor` returns the editor component for a node kind, or null when the kind has none: App
// mounts it for the selected node. Editors read and write `node.data.params` of the graph store; the Make bar and the
// daemon read the same params. README.md lists the params and the routes each editor uses.

import { type ComponentType, createElement } from "react";
import type { NodeKind } from "../nodeTypes";
import { CropPairEditor } from "./CropPairEditor";
import { LightEditor } from "./LightEditor";
import { LightMixEditor } from "./LightMixEditor";
import { MaskEditor } from "./MaskEditor";
import { MotionEditor } from "./MotionEditor";
import { PeopleEditor } from "./PeopleEditor";
import { RecolourEditor } from "./RecolourEditor";
import { ScoutEditor } from "./ScoutEditor";
import { SetEditor } from "./SetEditor";

export interface EditorProps {
  nodeId: string;
}

export type EditorComponent = ComponentType<EditorProps>;

/** The kinds that have an editor. */
export const EDITOR_KINDS = ["Mask", "CropPair", "Motion", "People", "Set", "LightMix", "Recolour", "Light", "Scout"] as const;

/**
 * App mounts the editor of the selected node without a key, so selecting another node of the same kind would keep the
 * first node's local state (typed text, tab, notes, the router card). The registry hands out editors keyed by node id,
 * so every node gets a fresh one.
 */
function keyedByNode(Editor: EditorComponent, name: string): EditorComponent {
  const ForNode = ({ nodeId }: EditorProps) => createElement(Editor, { key: nodeId, nodeId });
  ForNode.displayName = `${name}ForNode`;
  return ForNode;
}

const EDITORS: Readonly<Partial<Record<NodeKind, EditorComponent>>> = {
  Mask: keyedByNode(MaskEditor, "Mask"),
  CropPair: keyedByNode(CropPairEditor, "CropPair"),
  Motion: keyedByNode(MotionEditor, "Motion"),
  People: keyedByNode(PeopleEditor, "People"),
  Set: keyedByNode(SetEditor, "Set"),
  LightMix: keyedByNode(LightMixEditor, "LightMix"),
  Recolour: keyedByNode(RecolourEditor, "Recolour"),
  Light: keyedByNode(LightEditor, "Light"),
  Scout: keyedByNode(ScoutEditor, "Scout"),
};

export function editorFor(kind: NodeKind): EditorComponent | null {
  return EDITORS[kind] ?? null;
}

export { CropPairEditor, LightEditor, LightMixEditor, MaskEditor, MotionEditor, PeopleEditor, RecolourEditor, ScoutEditor, SetEditor };
export { VideoPlayer, chipsFromReport, timeMapFromReport, type PlayerChip, type PlayerReport, type VideoPlayerProps } from "./VideoPlayer";
export { boxFor, boxPercent, cutBoxes, moveZoom, zoomError, type CropBox, type CropPlan } from "./cropBoxes";
export {
  evaluateMove,
  isMoveKind,
  makeBlock,
  MIN_OVERRIDE_CHARS,
  MOVE_KINDS,
  parseRouterCard,
  REPAIR_CODES,
  RISK_LADDER,
  type MoveDials,
  type MoveEvaluation,
  type MoveKind,
  type RouterCard,
} from "./movePolicy";
export { packetFrameFor, engineFrameFor, nearestPacketFrame, parseTimeMap, type TimeMap } from "./timeMap";
