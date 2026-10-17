// Dev-only demo for the manual checks in README.md: mounts the real <Viewer> on synthetic sources (no daemon, no
// client imagery). `?mode=wipe|toggle|diff|edges|de00|tiles` picks the starting mode, `?zoom=1` starts at 100%.

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { halfFloatPack, linearToSrgb, srgbToLinear } from "./math";
import type { ImageSource, Overlays, ViewerMode } from "./types";
import { Viewer } from "./Viewer";

const W = 1280;
const H = 720;

/** A plate-like test card: sky gradient, a building block with windows, a checker floor (sRGB code values). */
function card(shifted: boolean): Float32Array {
  const v = new Float32Array(W * H * 4);
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const i = (y * W + x) * 4;
      let r = 0.55 + 0.3 * (1 - y / H);
      let g = 0.62 + 0.25 * (1 - y / H);
      let b = 0.78 + 0.2 * (1 - y / H);
      if (y > 0.62 * H) {
        const c = (Math.floor(x / 40) + Math.floor(y / 40)) % 2 === 0 ? 0.32 : 0.22;
        r = g = b = c;
      } else if (x > 300 && x < 980 && y > 160) {
        r = 0.74;
        g = 0.7;
        b = 0.64;
        const wx = (x - 300) % 85;
        const wy = (y - 160) % 70;
        if (wx > 20 && wx < 65 && wy > 15 && wy < 50) {
          r = 0.2;
          g = 0.27;
          b = shifted && x > 640 && y < 300 ? 0.5 : 0.33;
        }
      }
      if (shifted && x > 820 && x < 900 && y > 350 && y < 420) {
        r = Math.min(1, r * 1.12);
        g = Math.min(1, g * 1.05);
      }
      v.set([r, g, b, 1], i);
    }
  }
  return v;
}

/** A ΔE00-like map from the two cards (luminance difference scaled to ΔE units; display only). */
function heat(a: Float32Array, b: Float32Array): Float32Array {
  const out = new Float32Array(W * H);
  for (let p = 0; p < W * H; p++) {
    const i = p * 4;
    let d = 0;
    for (let c = 0; c < 3; c++) d += Math.abs(srgbToLinear(a[i + c]) - srgbToLinear(b[i + c]));
    out[p] = d * 60;
  }
  return out;
}

/** Edge mask PNG (white edges) as a blob URL. */
async function edgeUrl(src: Float32Array, threshold: number): Promise<string> {
  const c = document.createElement("canvas");
  c.width = W;
  c.height = H;
  const ctx = c.getContext("2d");
  if (!ctx) throw new Error("no 2d canvas");
  const img = ctx.createImageData(W, H);
  const lum = (x: number, y: number) => {
    const i = (y * W + x) * 4;
    return 0.2126 * src[i] + 0.7152 * src[i + 1] + 0.0722 * src[i + 2];
  };
  for (let y = 1; y < H - 1; y++) {
    for (let x = 1; x < W - 1; x++) {
      const g = Math.abs(lum(x + 1, y) - lum(x - 1, y)) + Math.abs(lum(x, y + 1) - lum(x, y - 1));
      const e = g > threshold ? 255 : 0;
      img.data.set([e, e, e, 255], (y * W + x) * 4);
    }
  }
  ctx.putImageData(img, 0, 0);
  const blob = await new Promise<Blob | null>((resolve) => c.toBlob(resolve, "image/png"));
  if (!blob) throw new Error("toBlob failed");
  return URL.createObjectURL(blob);
}

async function main(): Promise<void> {
  const q = new URLSearchParams(window.location.search);
  const mode = (q.get("mode") ?? "wipe") as ViewerMode;
  const zoom = q.get("zoom") ? Number(q.get("zoom")) : "fit";
  const a = card(false);
  const b = card(true);
  const toSource = (v: Float32Array, label: string): ImageSource => ({
    url: "demo",
    width: W,
    height: H,
    encoding: "png16",
    data: halfFloatPack(v).buffer as ArrayBuffer,
    label,
  });
  const overlays: Overlays = {
    edges: { plate: await edgeUrl(a, 0.06), candidate: await edgeUrl(b, 0.06) },
    heatmap: { url: "demo", width: W, height: H, data: halfFloatPack(heat(a, b)).buffer as ArrayBuffer },
    tiles: [
      { x: 860, y: 385 },
      { x: 700, y: 230 },
      { x: 400, y: 600 },
    ],
  };
  const root = document.getElementById("root");
  if (!root) return;
  createRoot(root).render(
    <StrictMode>
      <p style={{ color: "#a9abb5", letterSpacing: "0.04em" }}>
        PLATE viewer demo · synthetic plate vs candidate · sRGB check {linearToSrgb(srgbToLinear(0.5)).toFixed(3)}
      </p>
      <Viewer a={toSource(a, "Plate")} b={toSource(b, "Candidate")} mode={mode} zoom={zoom} overlays={overlays} />
    </StrictMode>,
  );
}

void main();
