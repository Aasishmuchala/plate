// Typed handles (T35). Every node kind declares its input and output ports and the data each one carries
// (`image16`, `mask`, `packet`, `video`, `report`); an edge may only join an output to an input that accepts its
// type. This is the plumbing check that runs before the doctrine rules in canvasRules: a mask never feeds an image
// input, a report never reaches Export. Handle ids are unique per side of a node, so `image` can be both an input
// and an output id. An edge with a null handle id uses the first port on that side (xyflow does the same).

import type { HandleType, NodeKind } from "../nodeTypes";

export interface PortSpec {
  /** The xyflow handle id. */
  id: string;
  /** What flows through the port. For an input it is the preferred type; `accepts` may widen it. */
  type: HandleType;
  label: string;
  /** Input ports only: every type the port takes (defaults to `[type]`). */
  accepts?: readonly HandleType[];
}

export interface KindPorts {
  inputs: readonly PortSpec[];
  outputs: readonly PortSpec[];
}

const port = (id: string, type: HandleType, label: string = id, accepts?: readonly HandleType[]): PortSpec =>
  accepts ? { id, type, label, accepts } : { id, type, label };

const IMAGE_OUT = [port("image", "image16", "image")] as const;

export const PORTS: Readonly<Record<NodeKind, KindPorts>> = {
  // The packet's beauty and the packet itself (Z, normals, Cryptomatte, camera) for nodes that read elements.
  Plate: { inputs: [], outputs: [port("image", "image16", "beauty"), port("packet", "packet")] },
  Edit: { inputs: [port("image", "image16"), port("mask", "mask")], outputs: IMAGE_OUT },
  // Cryptomatte pick from a packet, or SAM/paint on a Tier C image.
  Mask: { inputs: [port("source", "packet", "source", ["packet", "image16"])], outputs: [port("mask", "mask")] },
  Comp: {
    inputs: [port("base", "image16", "base"), port("layer", "image16", "layer"), port("mask", "mask")],
    outputs: IMAGE_OUT,
  },
  Colour: { inputs: [port("image", "image16")], outputs: IMAGE_OUT },
  LightMix: { inputs: [port("packet", "packet", "light selects")], outputs: IMAGE_OUT },
  Recolour: { inputs: [port("packet", "packet", "elements"), port("mask", "mask")], outputs: IMAGE_OUT },
  Gate: {
    inputs: [
      port("candidate", "image16", "candidate", ["image16", "video"]),
      port("reference", "packet", "reference", ["packet", "image16"]),
    ],
    outputs: [port("report", "report")],
  },
  Judge: { inputs: [port("candidate", "image16", "candidate", ["image16", "video"])], outputs: [port("report", "report")] },
  // Start and end crops of the approved still, the input of a push-in.
  CropPair: { inputs: [port("image", "image16", "still")], outputs: [port("pair", "image16", "start/end")] },
  Motion: { inputs: [port("image", "image16", "start"), port("packet", "packet")], outputs: [port("video", "video")] },
  People: { inputs: [port("image", "image16"), port("packet", "packet", "packet Z")], outputs: IMAGE_OUT },
  Set: { inputs: [port("views", "image16", "views")], outputs: [port("image", "image16"), port("report", "report")] },
  Compare: {
    inputs: [port("a", "image16", "A", ["image16", "video"]), port("b", "image16", "B", ["image16", "video"])],
    outputs: [port("report", "report")],
  },
  Export: { inputs: [port("media", "image16", "media", ["image16", "video"])], outputs: [] },
  Review: { inputs: [port("media", "image16", "media", ["image16", "video", "report"])], outputs: [] },
  // Max round trips: a new packet comes back (MaxGaffer light match, MaxDirector scout poses).
  Light: { inputs: [port("packet", "packet")], outputs: [port("packet", "packet")] },
  Scout: { inputs: [port("packet", "packet")], outputs: [port("packet", "packet")] },
  // Unmeasured: its output exists for looking and comparing; canvasRules keeps it from Export/Review.
  Concept: { inputs: [port("image", "image16", "reference")], outputs: [port("image", "image16", "concept")] },
};

/** The port a handle id names on one side of a kind; a null id means the first port. */
export function portOf(kind: NodeKind, side: "inputs" | "outputs", handleId: string | null | undefined): PortSpec | null {
  const ports = PORTS[kind][side];
  if (handleId === null || handleId === undefined || handleId === "") return ports[0] ?? null;
  return ports.find((p) => p.id === handleId) ?? null;
}

export const acceptsOf = (p: PortSpec): readonly HandleType[] => p.accepts ?? [p.type];

export type HandleVerdict =
  | { ok: true; type: HandleType; sourceHandle: string; targetHandle: string }
  | { ok: false; code: "HANDLE_TYPE"; message: string };

/**
 * Does output `sourceHandle` of a `sourceKind` node fit input `targetHandle` of a `targetKind` node? A null source
 * handle is the first output; a null target handle is the first input that takes the source's type (so a
 * programmatic Mask -> Edit lands on Edit's mask input). On success the resolved handle ids come back, so the stored
 * edge always names both ports.
 */
export function checkHandles(
  sourceKind: NodeKind,
  sourceHandle: string | null | undefined,
  targetKind: NodeKind,
  targetHandle: string | null | undefined,
): HandleVerdict {
  const out = portOf(sourceKind, "outputs", sourceHandle);
  if (!out) {
    const what = sourceHandle ? `an output '${sourceHandle}'` : "outputs";
    return { ok: false, code: "HANDLE_TYPE", message: `${sourceKind} has no ${what}` };
  }
  const inputs = PORTS[targetKind].inputs;
  const inp =
    targetHandle === null || targetHandle === undefined || targetHandle === ""
      ? (inputs.find((p) => acceptsOf(p).includes(out.type)) ?? inputs[0] ?? null)
      : (inputs.find((p) => p.id === targetHandle) ?? null);
  if (!inp) {
    const what = targetHandle ? `an input '${targetHandle}'` : "inputs";
    return { ok: false, code: "HANDLE_TYPE", message: `${targetKind} has no ${what}` };
  }
  const accepts = acceptsOf(inp);
  if (!accepts.includes(out.type)) {
    return {
      ok: false,
      code: "HANDLE_TYPE",
      message: `${targetKind} ${inp.label} takes ${accepts.join(" or ")}, not ${out.type}`,
    };
  }
  return { ok: true, type: out.type, sourceHandle: out.id, targetHandle: inp.id };
}

/** The data type an edge carries (from its source port), for edge colouring. Null when the port is unknown. */
export function edgeType(sourceKind: NodeKind | undefined, sourceHandle: string | null | undefined): HandleType | null {
  if (!sourceKind) return null;
  return portOf(sourceKind, "outputs", sourceHandle)?.type ?? null;
}
