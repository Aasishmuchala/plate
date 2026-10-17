// The PLATE node vocabulary (BUILD_PLAN section 4) and the typed graph model that mirrors `project.json.graph`.
// There is deliberately no text-to-image node and no Image Creator: the only unmeasured thing on the canvas is
// `Concept`, and canvasRules keeps it away from delivery.

import type { Node } from "@xyflow/react";

export const NODE_KINDS = [
  "Plate",
  "Edit",
  "Mask",
  "Comp",
  "Colour",
  "LightMix",
  "Recolour",
  "Gate",
  "Judge",
  "CropPair",
  "Motion",
  "People",
  "Set",
  "Compare",
  "Export",
  "Review",
  "Light",
  "Scout",
  "Concept",
  // Wave 9 (T81, decisions 46-48 and 57). A Reference holds an image an engine reads (never composited, never a
  // plate anchor); a Frame is an annotation box with no ports; Atmosphere and Sky are numeric nodes. None of the four
  // generates, so each adds 0 to the generation depth. Appended so saved graphs and the Python mirror keep their order.
  "Reference",
  "Frame",
  "Atmosphere",
  "Sky",
] as const;

export type NodeKind = (typeof NODE_KINDS)[number];

// Seed names, kept so earlier imports keep compiling.
export const NODE_TYPES = NODE_KINDS;
export type NodeType = NodeKind;

/** `concept_unmeasured` is the watermarked tier; canvasRules refuses any path from it to Export/Review. */
export type Tier = "measured" | "concept_unmeasured";
/** A Plate is either a Tier A packet (render + G-buffer) or a Tier C client plate (image only). */
export type PlateTier = "A" | "C";
export type GateStatus = "none" | "pass" | "fail" | "advisory";
export type RiskLabel = "low" | "med" | "high" | "highest";

/** One logged override; `at` is an ISO-8601 UTC timestamp. */
export interface Override {
  reason: string;
  by: string;
  at: string;
}

/** What a handle carries (T35). Edges join an output to an input that accepts its type; see `nodes/ports.ts`.
 * `reference` (T81) is a Reference node's image: it feeds an engine's reference input only and is never composited. */
export const HANDLE_TYPES = ["image16", "mask", "packet", "video", "report", "reference"] as const;
export type HandleType = (typeof HANDLE_TYPES)[number];

/** Custody grades of an engine route (docs/custody.md): C0 local, C1/C2 private vendor, D public or unconfirmed. */
export const CUSTODY_GRADES = ["C0", "C1", "C2", "D"] as const;
export type CustodyGrade = (typeof CUSTODY_GRADES)[number];

/** Pixel size of an image output. */
export interface ImageSize {
  width: number;
  height: number;
}

/** The label every downstream node carries once an invented angle or a high-risk move was overridden (doctrine 2). */
export const UNSEEN_GEOMETRY_LABEL = "unseen geometry guessed";

// Index signature so the interface satisfies xyflow's `Node<Record<string, unknown>>` data constraint.
export interface PlateNodeData extends Record<string, unknown> {
  kind: NodeKind;
  label: string;
  /** Derived: generating nodes on the longest path from a Plate. The store recomputes it after every graph change. */
  generationDepth: number;
  outputTier: Tier;
  gateStatus: GateStatus;
  approved: boolean;
  approvedBy?: string;
  /** Id of the logged full scrub that allowed `approved` (doctrine 7). */
  scrubLogId?: string;
  riskLabel?: RiskLabel;
  overrides: Override[];
  /** Provenance labels such as `unseen geometry guessed`; they propagate downstream (see `effectiveLabels`). */
  labels: string[];
  /** Node-specific parameters (engine, region, `cameraOverride`, ...). Node editors own the shapes. */
  params?: Record<string, unknown>;
  /** CAS hash of the node's current output: a Plate's beauty, else the picked candidate. Drives the body thumbnail
   * (`/api/v0/assets/<hash>?fmt=thumb`) and the viewer A/B. */
  outputHash?: string;
  /** Size of `outputHash`; candidates come back registered to the plate, so they share the plate's size. */
  outputSize?: ImageSize;
  /** Plate nodes only. */
  packetId?: string;
  tier?: PlateTier;
}

/** A Plate node: anchored to a packet, depth 0 by definition. */
export type PlateNode = Node<PlateNodeData & { kind: "Plate"; packetId: string; tier: PlateTier; generationDepth: 0 }>;

export function makeNodeData(kind: NodeKind, init: Partial<PlateNodeData> = {}): PlateNodeData {
  return {
    kind,
    label: kind,
    generationDepth: 0,
    outputTier: kind === "Concept" ? "concept_unmeasured" : "measured",
    gateStatus: "none",
    approved: false,
    overrides: [],
    labels: [],
    ...init,
  };
}

export function makePlateNodeData(packetId: string, tier: PlateTier, init: Partial<PlateNodeData> = {}): PlateNodeData {
  return makeNodeData("Plate", { ...init, kind: "Plate", packetId, tier, generationDepth: 0 });
}

/** The wire form of an edge, as stored in `project.json.graph.edges`. */
export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  sourceHandle: string | null;
  targetHandle: string | null;
}

/** The wire form of a node: the node data plus its id and canvas position. */
export type GraphNodeWire = PlateNodeData & { id: string; position?: { x: number; y: number } };

/** `project.json.graph`. */
export interface Graph {
  schema: 1;
  nodes: GraphNodeWire[];
  edges: GraphEdge[];
}

/** What `loadProject` accepts: a hand-written or older graph may omit any field but `id` and `kind`; the store fills
 * the defaults. Every `Graph` is also a `LoadableGraph`. */
export type LoadableNode = Pick<PlateNodeData, "kind"> & Partial<PlateNodeData> & { id: string; position?: { x: number; y: number } };
export interface LoadableGraph {
  schema: 1;
  nodes: LoadableNode[];
  edges: GraphEdge[];
}
