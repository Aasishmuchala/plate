// Node components and typed handles for the canvas (T35).

export { ApprovalTick, GateBadge, KindBadge, RiskBadge, StateChip, TierBadge, WatermarkIcon } from "./badges";
export { KIND_SUMMARY, NODE_COMPONENTS, PLATE_NODE_TYPES, type PlateNodeProps } from "./kinds";
export { CONCEPT_WATERMARK, NodeShell, type NodeShellProps } from "./NodeShell";
export {
  acceptsOf,
  checkHandles,
  edgeType,
  type HandleVerdict,
  type KindPorts,
  PORTS,
  portOf,
  type PortSpec,
} from "./ports";
