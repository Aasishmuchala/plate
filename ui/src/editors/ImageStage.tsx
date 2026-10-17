// The picture an editor works on: a CAS image (thumbnail through the token-carrying fetch) in a box with the image's
// aspect, pointer events mapped to IMAGE pixels, and an overlay layer that children draw into with percentage
// positions. The stage maps pointer positions from the image's size, not from the thumbnail, so a pick lands on the
// plate pixel whatever resolution the thumbnail has. Without a known size the stage still shows the picture but
// reports no points.

import { type PointerEvent as ReactPointerEvent, type ReactElement, type ReactNode, useRef } from "react";
import { useThumb } from "../canvas/assets";
import type { ImageSize } from "../nodeTypes";
import { clientToImage, type Point } from "./maskMath";
import { C, E } from "./theme";

export interface ImageStageProps {
  hash: string | null;
  size: ImageSize | null;
  testId: string;
  alt: string;
  /** Pointer pressed on the image (a click, or the start of a stroke). */
  onPointerDown?: (p: Point) => void;
  /** Pointer moved while pressed (a stroke). */
  onPointerMove?: (p: Point) => void;
  onPointerUp?: () => void;
  cursor?: string;
  children?: ReactNode;
}

const FALLBACK: ImageSize = { width: 16, height: 9 };

export function ImageStage({ hash, size, testId, alt, onPointerDown, onPointerMove, onPointerUp, cursor = "crosshair", children }: ImageStageProps): ReactElement {
  const thumb = useThumb(hash);
  const box = size ?? FALLBACK;
  const pressed = useRef(false);

  const point = (e: ReactPointerEvent<HTMLDivElement>): Point | null =>
    clientToImage(e.currentTarget.getBoundingClientRect(), size, e.clientX, e.clientY);

  return (
    <div
      data-testid={testId}
      data-image-size={size ? `${size.width}x${size.height}` : "unknown"}
      style={{ ...E.stage, aspectRatio: `${box.width} / ${box.height}`, cursor: size ? cursor : "default" }}
      onPointerDown={(e) => {
        const p = point(e);
        if (!p) return;
        pressed.current = true;
        // jsdom has no pointer capture; a browser keeps the stroke alive when the pointer leaves the stage
        e.currentTarget.setPointerCapture?.(e.pointerId);
        onPointerDown?.(p);
      }}
      onPointerMove={(e) => {
        if (!pressed.current) return;
        const p = point(e);
        if (p) onPointerMove?.(p);
      }}
      onPointerUp={() => {
        if (!pressed.current) return;
        pressed.current = false;
        onPointerUp?.();
      }}
      onPointerCancel={() => {
        if (!pressed.current) return;
        pressed.current = false;
        onPointerUp?.();
      }}
    >
      {thumb.state === "ready" ? (
        <img src={thumb.url} alt={alt} draggable={false} style={{ width: "100%", height: "100%", objectFit: "fill", display: "block" }} />
      ) : (
        <div style={{ ...E.note, position: "absolute", inset: 0, display: "grid", placeItems: "center", color: C.muted }}>
          {hash === null ? "no image on this node yet" : thumb.state === "loading" ? "loading image" : "image unavailable"}
        </div>
      )}
      <div style={E.overlay}>{children}</div>
    </div>
  );
}
