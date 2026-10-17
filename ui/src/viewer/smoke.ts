// Dev-only GPU smoke for the viewer, run in a real browser (README.md): shaders compile, the 64x64 half-float diff of
// a buffer against itself reads back all zeros, the float read-back matches the CPU reference (math.referenceDiff),
// the sRGB EOTF/OETF and row order hold on the canvas, wipe/heatmap/edge colours land where they should, and the
// RGBA8 fallback path draws within 8-bit rounding. Writes a JSON report into #result and sets the title to PASS/FAIL.

import {
  de00Color,
  fromHalfBits,
  halfFloatPack,
  linearToSrgb,
  makeView,
  referenceDiff,
  roundHalf,
  srgbToLinear,
  toHalfBits,
} from "./math";
import { type BitmapPixels, type DrawParams, type HalfPixels, ViewerRenderer } from "./renderer";

interface Check {
  name: string;
  ok: boolean;
  detail: string;
}

const N = 64;
const checks: Check[] = [];
const check = (name: string, ok: boolean, detail = "") => checks.push({ name, ok, detail });

/** Deterministic RGBA values in 0..1 that differ per row (a flipped read-back fails). */
function pattern(seed: number): Float32Array {
  const v = new Float32Array(N * N * 4);
  for (let y = 0; y < N; y++) {
    for (let x = 0; x < N; x++) {
      const i = (y * N + x) * 4;
      v[i] = y / (N - 1);
      v[i + 1] = x / (N - 1);
      v[i + 2] = ((x * 31 + y * 17 + seed * 7) % 64) / 63;
      v[i + 3] = 1;
    }
  }
  return v;
}

const half = (values: Float32Array, linear: boolean): HalfPixels => ({
  kind: "half",
  width: N,
  height: N,
  channels: 4,
  data: halfFloatPack(values),
  linear,
});

function maxAbs(a: Float32Array, b: Float32Array): number {
  let m = 0;
  for (let i = 0; i < a.length; i++) if (i % 4 !== 3) m = Math.max(m, Math.abs(a[i] - b[i]));
  return m;
}

/** Largest distance in half-float ulps between two non-negative RGB read-backs (both are RGBA16F values). */
function maxUlps(a: Float32Array, b: Float32Array): number {
  let m = 0;
  for (let i = 0; i < a.length; i++) if (i % 4 !== 3) m = Math.max(m, Math.abs(toHalfBits(a[i]) - toHalfBits(b[i])));
  return m;
}

// GPU pow() and the CPU's ** can land on either side of a half rounding boundary: one ulp in each stored linear value
// (<= 2^-11 below 1), times the gain, plus the rounding of the result (<= 2^-9 below 4). A flipped row or a missing
// EOTF is off by orders of magnitude more.
const ULP_TOL = 2;
const diffTol = (gain: number) => gain * 2 * 2 ** -11 + 2 ** -9;

/** Canvas pixel (x, y) with y measured from the top, RGBA8. */
function canvasPixel(gl: WebGL2RenderingContext, x: number, y: number): number[] {
  const out = new Uint8Array(4);
  gl.readPixels(x, gl.drawingBufferHeight - 1 - y, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, out);
  return Array.from(out);
}

const near = (got: number[], want: number[], tol: number) => want.every((w, k) => Math.abs(got[k] - w) <= tol);

/** A WebGL2 context that hides EXT_color_buffer_float, to force the RGBA8 path on a capable GPU. */
function withoutFloatTargets(gl: WebGL2RenderingContext): WebGL2RenderingContext {
  return new Proxy(gl, {
    get(target, prop) {
      if (prop === "getExtension") {
        return (name: string) => (name === "EXT_color_buffer_float" ? null : target.getExtension(name));
      }
      const v = Reflect.get(target, prop, target);
      return typeof v === "function" ? v.bind(target) : v;
    },
    set(target, prop, value) {
      return Reflect.set(target, prop, value, target);
    },
  });
}

function draw(r: ViewerRenderer, mode: DrawParams["mode"], extra: Partial<DrawParams> = {}): void {
  r.draw({
    target: { width: N, height: N },
    mode,
    view: makeView(1, { x: N / 2, y: N / 2 }, { width: N, height: N }),
    wipeX: N / 2,
    gain: 1,
    ...extra,
  });
}

async function run(): Promise<void> {
  const canvas = document.getElementById("c") as HTMLCanvasElement;
  const r = ViewerRenderer.create(canvas);
  if (!r) {
    check("webgl2", false, "no WebGL2 context");
    return;
  }
  const gl = r.gl;
  const info = gl.getExtension("WEBGL_debug_renderer_info");
  const gpuName = info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
  check("webgl2", true, `${gl.getParameter(gl.VERSION)} on ${gpuName}`);
  check("float targets (EXT_color_buffer_float)", r.caps.floatTargets);

  const a = pattern(1);
  const b = pattern(2);
  const pa = half(a, false); // png16: sRGB code values
  const pb = half(b, false);

  // 1. the spec's parity test: diff of a buffer against itself is exactly zero
  r.setImage("a", pa);
  r.setImage("b", half(a, false));
  const self = r.readAnalysis("diff", 8);
  check("diff(A, A) == 0", self !== null && maxAbs(self, new Float32Array(self.length)) === 0);

  // 2. float read-back vs the CPU reference, png16 and exr-half
  r.setImage("b", pb);
  const gpu = r.readAnalysis("diff", 4);
  const cpu = referenceDiff(pa.data, pb.data, 4, false);
  const e1 = gpu ? maxAbs(gpu, cpu) : Number.POSITIVE_INFINITY;
  check("diff(A, B) png16 matches referenceDiff", e1 <= diffTol(4), `max |gpu - cpu| ${e1} (tol ${diffTol(4)})`);
  r.setImage("a", half(a, true));
  r.setImage("b", half(b, true));
  const gpuLin = r.readAnalysis("diff", 1);
  const cpuLin = referenceDiff(halfFloatPack(a), halfFloatPack(b), 1, true);
  const e2 = gpuLin ? maxAbs(gpuLin, cpuLin) : Number.POSITIVE_INFINITY;
  check("diff(A, B) exr-half matches referenceDiff", e2 <= diffTol(1), `max |gpu - cpu| ${e2} (tol ${diffTol(1)})`);

  // 3. EOTF on upload + row order: read-back of A equals half(srgbToLinear(v)) with row 0 at the top
  r.setImage("a", pa);
  const lin = r.readAnalysis("a");
  let e3 = Number.POSITIVE_INFINITY;
  if (lin) {
    const want = new Float32Array(lin.length);
    for (let i = 0; i < want.length; i++) {
      want[i] = i % 4 === 3 ? 1 : roundHalf(srgbToLinear(fromHalfBits(pa.data[i])));
    }
    e3 = maxUlps(lin, want);
  }
  check("linearize: EOTF and top-first rows", e3 <= ULP_TOL, `max ${e3} half ulps`);

  // 4. canvas: OETF at 100%, top-left orientation, wipe split
  r.setImage("b", pb);
  draw(r, "a");
  const px = (x: number, y: number, src: Float32Array) => {
    const i = (y * N + x) * 4;
    return [0, 1, 2].map((c) => Math.round(linearToSrgb(srgbToLinear(roundHalf(src[i + c]))) * 255));
  };
  const topLeft = canvasPixel(gl, 3, 5);
  check("canvas A at 100% (OETF, orientation)", near(topLeft, px(3, 5, a), 1), `${topLeft} vs ${px(3, 5, a)}`);
  draw(r, "wipe");
  const left = canvasPixel(gl, 10, 40);
  const right = canvasPixel(gl, 50, 40);
  check(
    "wipe: A left of the line, B right",
    near(left, px(10, 40, a), 1) && near(right, px(50, 40, b), 1),
    `${left} / ${right}`,
  );

  // 5. heatmap colours
  const de = new Float32Array(N * N).fill(0);
  for (let i = 0; i < N * N; i++) de[i] = ((i % N) / (N - 1)) * 12;
  r.setHeat({ kind: "half", width: N, height: N, channels: 1, data: halfFloatPack(de), linear: true });
  draw(r, "de00");
  const heatAt = (x: number) => de00Color(roundHalf(((x % N) / (N - 1)) * 12));
  const h1 = canvasPixel(gl, 16, 8);
  const h2 = canvasPixel(gl, 63, 8);
  check("ΔE00 LUT on canvas", near(h1, heatAt(16), 3) && near(h2, de00Color(10), 1), `${h1} / ${h2}`);

  // 6. edge overlay colours: plate edges on column 10, candidate on column 20, both on column 30
  const mask = (cols: number[]): BitmapPixels => {
    const img = new ImageData(N, N);
    for (let y = 0; y < N; y++) for (const x of cols) img.data.set([255, 255, 255, 255], (y * N + x) * 4);
    for (let i = 3; i < img.data.length; i += 4) img.data[i] = 255;
    return { kind: "bitmap", width: N, height: N, source: img };
  };
  r.setMask("plateEdges", mask([10, 30]));
  r.setMask("candEdges", mask([20, 30]));
  draw(r, "edges");
  const red = canvasPixel(gl, 10, 30);
  const cyan = canvasPixel(gl, 20, 30);
  const white = canvasPixel(gl, 30, 30);
  check(
    "edges: red plate, cyan candidate, white both",
    near(red, [255, 0, 0], 0) && near(cyan, [0, 255, 255], 0) && near(white, [255, 255, 255], 0),
    `${red} / ${cyan} / ${white}`,
  );

  // 7. tiles: a 100% crop pinned at the cell corner
  r.draw({
    target: { width: N, height: N },
    mode: "tiles",
    view: makeView(0.25, { x: 32, y: 32 }, { width: N, height: N }),
    wipeX: 0,
    gain: 1,
    tiles: [
      { index: 0, slot: "a", src: { point: { x: 40, y: 40 }, x0: 30, y0: 31, w: 16, h: 16 }, x: 2, y: 3, w: 16, h: 16 },
    ],
  });
  const t0 = canvasPixel(gl, 2, 3);
  check("tile shows source pixel (30, 31) at its corner", near(t0, px(30, 31, a), 1), `${t0} vs ${px(30, 31, a)}`);
  r.dispose();

  // 8. RGBA8 fallback on the same GPU
  const fb = new ViewerRenderer(withoutFloatTargets(gl));
  check("fallback reports no float targets", !fb.caps.floatTargets);
  fb.setImage("a", pa);
  draw(fb, "a");
  const f0 = canvasPixel(gl, 3, 5);
  check("fallback A within 8-bit rounding", near(f0, px(3, 5, a), 2), `${f0} vs ${px(3, 5, a)}`);
  check("fallback has no float read-back", fb.readAnalysis("a") === null);
  fb.dispose();
}

run()
  .catch((e: unknown) => check("no exceptions", false, e instanceof Error ? e.message : String(e)))
  .finally(() => {
    const ok = checks.length > 0 && checks.every((c) => c.ok);
    document.title = ok ? "PASS" : "FAIL";
    const out = document.getElementById("result");
    if (out) out.textContent = JSON.stringify({ ok, checks }, null, 2);
  });
