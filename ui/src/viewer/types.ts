// Public types of the viewer. Image coordinates are pixels with (0, 0) at the top-left corner of the image; device
// coordinates are canvas backing-store pixels, top-left origin. "100%" means one image pixel per device pixel.

/**
 * How the bytes behind `url` are encoded.
 * - `srgb8`: an ordinary 8-bit PNG/JPEG the browser decodes; values are sRGB code values (EOTF applied on upload).
 * - `png16`: raw little-endian half-float RGBA from `GET /api/v0/assets/<hash>?fmt=rgba16f`, holding the 16-bit
 *   PNG/TIFF's sRGB code values / 65535 (EOTF applied on upload).
 * - `exr-half`: the same raw half-float RGBA transport, values already display-referred linear (no EOTF).
 */
export type ImageEncoding = "srgb8" | "png16" | "exr-half";

export interface ImageSource {
  url: string;
  width: number;
  height: number;
  encoding: ImageEncoding;
  /** Inline payload instead of fetching `url` (tests, and callers that already hold the bytes). */
  data?: Blob | ArrayBuffer;
  /** Shown in the HUD; defaults to "A" / "B". */
  label?: string;
}

/** Edge masks for the red-cyan overlay: 8-bit PNG URLs, white = edge (binary, threshold 0.5). */
export interface EdgeSource {
  /** Plate G-buffer structural edges (red). */
  plate?: string;
  /** Candidate Canny edges from the daemon's gate assets (cyan). */
  candidate?: string;
}

/** Per-pixel CIEDE2000 map the daemon computes: raw little-endian half floats, R16F (1 channel) or RGBA16F (R used). */
export interface HeatSource {
  url: string;
  width: number;
  height: number;
  data?: Blob | ArrayBuffer;
}

export interface Point {
  x: number;
  y: number;
}

export interface Size {
  width: number;
  height: number;
}

export type ViewerMode = "wipe" | "toggle" | "diff" | "edges" | "de00" | "tiles";

/** Device pixels per image pixel, or fit-to-viewport. */
export type Zoom = number | "fit";

/** The image point (image pixels) shown at the centre of the viewport. */
export type Pan = Point;

export interface Overlays {
  edges?: EdgeSource;
  heatmap?: HeatSource;
  /** Gate-flagged points (image pixels) for the 100% crop tiles. */
  tiles?: Point[];
}
