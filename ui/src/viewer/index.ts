// The viewer slot App mounts (T24). <Viewer> renders an empty frame until it gets an A image, so App can mount it
// with no props; panels and editors pass sources, mode and overlays once they exist (T35/T36/T47).

export { DEFAULT_GAIN, type AnalysisKind } from "./renderer";
export type { Caps, DisplayedMode, ViewState } from "./state";
export type {
  EdgeSource,
  HeatSource,
  ImageEncoding,
  ImageSource,
  Overlays,
  Pan,
  Point,
  ViewerMode,
  Zoom,
} from "./types";
export {
  type LoadStatus,
  type SlotKey,
  Viewer,
  type ViewerChange,
  type ViewerHandle,
  type ViewerProps,
} from "./Viewer";
