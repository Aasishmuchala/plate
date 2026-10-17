// Node editors (T47). `editorFor` returns the editor component for a node kind, or null when the kind has none: App
// mounts it for the selected node. Editors read and write `node.data.params` of the graph store; the Make bar and the
// daemon read the same params. README.md lists the params and the routes each editor uses.
// The artist-path editors (T69: Plate packet picker, Edit prompt/recipe, Export, Review) have their own registry,
// `pathEditorFor`, which PlateCanvas mounts in the sidebar for the selected node. They are kept out of `editorFor`
// because the T47 suites pin `editorFor` to its nine kinds; a kind has an editor in one registry or the other.

import { type ComponentType, createElement } from "react";
import type { NodeKind } from "../nodeTypes";
import { CropPairEditor } from "./CropPairEditor";
import { EditEditor } from "./EditEditor";
import { ExportEditor } from "./ExportEditor";
import { LightEditor } from "./LightEditor";
import { LightMixEditor } from "./LightMixEditor";
import { MaskEditor } from "./MaskEditor";
import { MotionEditor } from "./MotionEditor";
import { PeopleEditor } from "./PeopleEditor";
import { PlateEditor } from "./PlateEditor";
import { RecolourEditor } from "./RecolourEditor";
import { ReviewEditor } from "./ReviewEditor";
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

/** The kinds of the artist path (T69): link a packet, say what changes, hand the take out. */
export const PATH_EDITOR_KINDS = ["Plate", "Edit", "Export", "Review"] as const;

const PATH_EDITORS: Readonly<Partial<Record<NodeKind, EditorComponent>>> = {
  Plate: keyedByNode(PlateEditor, "Plate"),
  Edit: keyedByNode(EditEditor, "Edit"),
  Export: keyedByNode(ExportEditor, "Export"),
  Review: keyedByNode(ReviewEditor, "Review"),
};

export function pathEditorFor(kind: NodeKind): EditorComponent | null {
  return PATH_EDITORS[kind] ?? null;
}

export { CropPairEditor, LightEditor, LightMixEditor, MaskEditor, MotionEditor, PeopleEditor, RecolourEditor, ScoutEditor, SetEditor };
export { EditEditor, ExportEditor, PlateEditor, ReviewEditor };
export { EDIT_RECIPE_KINDS, EDIT_TASKS, promptSource, SHOT_CLASSES, type PromptSource } from "./EditEditor";
export { DEFAULT_EXPORT_FORMAT, producingNodes, useUpstreamRuns, type UpstreamRuns } from "./ExportEditor";
export { filterPackets, linkFor, PACKET_EVENT_KINDS } from "./PlateEditor";
export { DEFAULT_WATERMARK } from "./ReviewEditor";
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
