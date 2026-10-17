// One xyflow node component per NodeKind (T35). Every kind but Frame renders the shared NodeShell; what differs is
// the one line of facts under the preview. A Frame (T81) is an annotation box with no ports (FrameNode). The store
// creates nodes with xyflow type "default", so `nodeTypes.default` dispatches on `data.kind`; a node whose type names
// a kind directly ("Edit") gets that kind's component.

import type { NodeProps, NodeTypes } from "@xyflow/react";
import { type ComponentType, memo } from "react";
import type { GraphNode } from "../canvasRules";
import { NODE_KINDS, type NodeKind, type PlateNodeData } from "../nodeTypes";
import { FrameNode, frameTitle } from "./FrameNode";
import { NodeShell } from "./NodeShell";

const param = (d: PlateNodeData, key: string): string | null => {
  const v = d.params?.[key];
  if (v === undefined || v === null || v === "") return null;
  return typeof v === "string" || typeof v === "number" || typeof v === "boolean" ? String(v) : null;
};

const count = (d: PlateNodeData, key: string): number | null => {
  const v = d.params?.[key];
  return Array.isArray(v) ? v.length : null;
};

const whole = (d: PlateNodeData, key: string): number | null => {
  const v = d.params?.[key];
  return typeof v === "number" && Number.isFinite(v) ? Math.max(0, Math.round(v)) : null;
};

/** First 8 characters of a CAS hash param, for a card. */
const short = (d: PlateNodeData, key: string): string | null => {
  const v = param(d, key);
  return v === null ? null : v.slice(0, 8);
};

/** The prompt caption an Edit card carries (T81): at most 48 characters, an ellipsis when cut. */
export const PROMPT_CAPTION_CHARS = 48;
const caption = (text: string): string => {
  const t = text.trim().replace(/\s+/g, " ");
  return t.length > PROMPT_CAPTION_CHARS ? `${t.slice(0, PROMPT_CAPTION_CHARS)}…` : t;
};

/** The facts line per kind. Null shows nothing. Node editors (T47) own the params; these only read them. */
export const KIND_SUMMARY: Readonly<Record<NodeKind, (d: PlateNodeData) => string | null>> = {
  Plate: (d) => `${d.packetId && d.packetId !== "unlinked" ? d.packetId : "no packet linked"} · Tier ${d.tier ?? "?"}`,
  Edit: (d) => {
    const engine = param(d, "engine_id") ?? param(d, "engine");
    const task = param(d, "task");
    const recipe = param(d, "recipe");
    const prompt = param(d, "prompt");
    const parts = [
      engine ?? "engine: pick in the Make bar",
      task,
      recipe ? `recipe ${recipe}` : prompt && prompt.trim() !== "" ? caption(prompt) : null,
    ];
    if (param(d, "cameraOverride")) parts.push("camera override");
    return parts.filter(Boolean).join(" · ");
  },
  Mask: (d) => {
    const n = count(d, "ids");
    return n === null ? "no ids picked" : `${n} id${n === 1 ? "" : "s"}${param(d, "textLock") === "true" ? " · text-lock" : ""}`;
  },
  Comp: () => "feather inside the mask, outside untouched",
  Colour: () => "OCIO display transform",
  LightMix: () => "light-select gains (NNLS)",
  Recolour: (d) => param(d, "target") ?? "pick a material and a RAL/hex",
  Gate: (d) => (d.gateStatus === "none" ? "not scored yet" : `verdict ${d.gateStatus}`),
  Judge: () => "two-sample consensus",
  CropPair: (d) => {
    const z0 = param(d, "zoomStart");
    const z1 = param(d, "zoomEnd");
    return z0 && z1 ? `zoom ${z0} → ${z1}` : "start/end crops of the still";
  },
  Motion: (d) => {
    const move = param(d, "move");
    const engine = param(d, "engine_id");
    return [move ? `move ${move}` : "move: not set", engine].filter(Boolean).join(" · ");
  },
  People: (d) => param(d, "cast") ?? "cast, placement, light",
  Set: () => "cross-view consistency",
  Compare: () => "A/B in the viewer",
  // the Export editor's default format is png16; `exported` counts the takes it handed out
  Export: (d) => `${param(d, "format") ?? "png16"} · ${whole(d, "exported") ?? 0} exported`,
  Review: (d) => {
    const n = whole(d, "bundleTakes");
    return n === null ? "static review bundle" : `${n} in last bundle`;
  },
  Light: () => "match a reference in Max",
  Scout: (d) => (param(d, "count") ? `${param(d, "count")} scout poses` : "scout poses from Max"),
  Concept: () => "unmeasured: look, never deliver",
  Reference: (d) => {
    const hash = short(d, "referenceHash");
    return hash ? `${param(d, "role") ?? "style"} · ${hash}` : "no image";
  },
  Frame: (d) => frameTitle(d),
  Atmosphere: (d) => `visibility ${param(d, "visibility_m") ?? 800} m`,
  Sky: (d) => {
    const toneMatch = d.params?.["tone_match"] !== false;
    return `sky: ${short(d, "skyHash") ?? "from the sky input"} · tone match ${toneMatch ? "on" : "off"}`;
  },
};

export type PlateNodeProps = NodeProps<GraphNode>;

function makeKindNode(kind: NodeKind): ComponentType<PlateNodeProps> {
  const summary = KIND_SUMMARY[kind];
  const C = ({ id, data, selected }: PlateNodeProps) => (
    <NodeShell id={id} data={data} selected={selected} summary={summary(data)} />
  );
  C.displayName = `${kind}Node`;
  return memo(C);
}

/** The component for each kind: the shared shell, except the Frame annotation box. */
export const NODE_COMPONENTS: Readonly<Record<NodeKind, ComponentType<PlateNodeProps>>> = Object.fromEntries(
  NODE_KINDS.map((k) => [k, k === "Frame" ? FrameNode : makeKindNode(k)]),
) as Record<NodeKind, ComponentType<PlateNodeProps>>;

/** xyflow type "default" (what the store creates): pick the component by `data.kind`. */
const KindDispatch = memo(function KindDispatch(props: PlateNodeProps) {
  const C = NODE_COMPONENTS[props.data.kind] ?? NODE_COMPONENTS.Edit;
  return <C {...props} />;
});

/** Pass to `<ReactFlow nodeTypes>`; defined once at module level so xyflow never sees a new object. */
export const PLATE_NODE_TYPES: NodeTypes = { default: KindDispatch, ...NODE_COMPONENTS } as unknown as NodeTypes;
