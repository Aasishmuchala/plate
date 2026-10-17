// The PLATE node vocabulary (BUILD_PLAN section 4). There is deliberately no text-to-image node and no
// Image Creator: the only unmeasured thing on the canvas is `Concept`, and canvasRules keeps it away from delivery.

export const NODE_TYPES = [
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
  "Concept",
] as const;

export type NodeType = (typeof NODE_TYPES)[number];

// Index signature so the interface satisfies xyflow's `Node<Record<string, unknown>>` data constraint.
export interface PlateNodeData extends Record<string, unknown> {
  type: NodeType;
  label: string;
  gatePassed?: boolean;
  humanApproved?: boolean;
  tier?: "measured" | "concept";
}
