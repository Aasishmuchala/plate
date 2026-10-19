// The viewer slot App mounts (T24). <Viewer> renders an empty frame until it gets an A image, so App can mount it
// with no props; panels and editors pass sources, mode and overlays once they exist (T35/T36/T47). The decoded-image
// cache (T159) is exported for hosts and tests that need to size or clear it.

export {
  PixelCache,
  type PixelCacheLimits,
  type PixelCacheStats,
  pixelCache,
  setPixelCacheLimits,
  VIEWER_CACHE_BYTES,
  VIEWER_CACHE_ENTRIES,
} from "./loaders";
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
