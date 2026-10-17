// Placeholder node-editor registry. T47 replaces it with the real editors (Mask, CropPair, Motion, People, Set,
// LightMix, Recolour, Light, Scout). `editorFor` returns the editor component for a node kind, or null when the kind
// has none yet.

import type { ComponentType } from "react";
import type { NodeKind } from "../nodeTypes";

export interface EditorProps {
  nodeId: string;
}

export type EditorComponent = ComponentType<EditorProps>;

export function editorFor(_kind: NodeKind): EditorComponent | null {
  return null;
}
