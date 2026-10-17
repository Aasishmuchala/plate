// WebGL2 renderer behind <Viewer>. Each A/B image is uploaded once (RGBA16F + HALF_FLOAT for the half-float
// transport, RGBA8 for 8-bit images), run through the LINEARIZE pass into a linear RGBA16F texture, and then drawn
// by the VIEW pass under one view transform. EXT_color_buffer_float is what makes RGBA16F a render target; without it
// every image goes to RGBA8 holding sRGB code values (decoded per sample) and the UI shows "8-bit preview".
// The renderer never resamples for export: it only displays, and `readAnalysis` reads the float values back for the
// parity check against the Python compositor (see README.md).

import {
  BG_DISPLAY,
  LINEARIZE_FRAG,
  type LinearizeUniform,
  VERT,
  VIEW_FRAG,
  VIEW_MODE,
  type ViewUniform,
} from "../shaders/viewer";
import { DE00_LUT_SIZE, DE00_MAX, de00Lut, halfToSrgb8, type TileCell, type View } from "./math";
import type { DisplayedMode } from "./state";
import type { Size } from "./types";

export interface HalfPixels {
  kind: "half";
  width: number;
  height: number;
  channels: 1 | 4;
  /** Half-float bit patterns, host byte order, rows top-first. */
  data: Uint16Array;
  /** True when the values are already linear (exr-half); false for sRGB code values (png16). */
  linear: boolean;
}

export interface BitmapPixels {
  kind: "bitmap";
  width: number;
  height: number;
  /** An 8-bit image holding sRGB code values. */
  source: TexImageSource;
}

export type Pixels = HalfPixels | BitmapPixels;
export type ImageSlot = "a" | "b";
export type MaskSlot = "plateEdges" | "candEdges";
export type AnalysisKind = "a" | "b" | "diff";

export interface RendererCaps {
  /** Linear RGBA16F targets and float read-back (EXT_color_buffer_float). False = the RGBA8 "8-bit preview". */
  floatTargets: boolean;
}

export interface DrawParams {
  /** Canvas backing-store size, device pixels. */
  target: Size;
  mode: DisplayedMode;
  view: View;
  /** Wipe line, device pixels from the left. */
  wipeX: number;
  gain: number;
  tiles?: readonly TileCell[];
}

export const DEFAULT_GAIN = 4;
export const EDGE_BASE = 0.35;

interface Tex {
  tex: WebGLTexture;
  width: number;
  height: number;
  mips: boolean;
  /** Filter currently set: true = NEAREST, false = LINEAR, null = not set yet. */
  nearest: boolean | null;
}

const MODE_ID: Record<Exclude<DisplayedMode, "tiles">, number> = {
  a: VIEW_MODE.A,
  b: VIEW_MODE.B,
  wipe: VIEW_MODE.WIPE,
  diff: VIEW_MODE.DIFF,
  edges: VIEW_MODE.EDGES,
  de00: VIEW_MODE.DE00,
};

function compile(gl: WebGL2RenderingContext, type: number, src: string): WebGLShader {
  const s = gl.createShader(type);
  if (!s) throw new Error("createShader failed");
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(s);
    gl.deleteShader(s);
    throw new Error(`shader compile: ${log}`);
  }
  return s;
}

function link(gl: WebGL2RenderingContext, frag: string): WebGLProgram {
  const p = gl.createProgram();
  if (!p) throw new Error("createProgram failed");
  const vs = compile(gl, gl.VERTEX_SHADER, VERT);
  const fs = compile(gl, gl.FRAGMENT_SHADER, frag);
  gl.attachShader(p, vs);
  gl.attachShader(p, fs);
  gl.linkProgram(p);
  gl.deleteShader(vs);
  gl.deleteShader(fs);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(`program link: ${gl.getProgramInfoLog(p)}`);
  return p;
}

export class ViewerRenderer {
  readonly gl: WebGL2RenderingContext;
  private readonly floatOk: boolean;
  private readonly linearize: WebGLProgram;
  private readonly view: WebGLProgram;
  private readonly locs = new Map<WebGLProgram, Map<string, WebGLUniformLocation | null>>();
  private readonly vao: WebGLVertexArrayObject;
  private readonly quad: WebGLBuffer;
  private readonly fbo: WebGLFramebuffer;
  private readonly blank: WebGLTexture;
  private readonly lut: WebGLTexture;
  private images: Partial<Record<ImageSlot, Tex>> = {};
  private masks: Partial<Record<MaskSlot, Tex>> = {};
  private heat: Tex | null = null;

  /** Null when the browser has no WebGL2 (jsdom, very old GPUs); the component shows a message instead. */
  static create(canvas: HTMLCanvasElement): ViewerRenderer | null {
    if (typeof WebGL2RenderingContext === "undefined") return null;
    const gl = canvas.getContext("webgl2", {
      alpha: false,
      antialias: false,
      depth: false,
      stencil: false,
      premultipliedAlpha: false,
      preserveDrawingBuffer: false,
    });
    return gl ? new ViewerRenderer(gl) : null;
  }

  constructor(gl: WebGL2RenderingContext) {
    this.gl = gl;
    if ("drawingBufferColorSpace" in gl) gl.drawingBufferColorSpace = "srgb";
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    gl.pixelStorei(gl.UNPACK_COLORSPACE_CONVERSION_WEBGL, gl.NONE);
    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.BLEND);
    this.linearize = link(gl, LINEARIZE_FRAG);
    this.view = link(gl, VIEW_FRAG);
    const vao = gl.createVertexArray();
    const quad = gl.createBuffer();
    const fbo = gl.createFramebuffer();
    if (!vao || !quad || !fbo) throw new Error("WebGL2 resource allocation failed");
    this.vao = vao;
    this.quad = quad;
    this.fbo = fbo;
    gl.bindVertexArray(vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, quad);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    this.blank = this.newTex();
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([0, 0, 0, 255]));
    this.lut = this.newTex();
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, DE00_LUT_SIZE, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, de00Lut());
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    this.floatOk = this.probeFloatTarget();
  }

  get caps(): RendererCaps {
    return { floatTargets: this.floatOk };
  }

  get hasImage(): boolean {
    return this.images.a !== undefined;
  }

  /** The extension alone is not enough on every driver: an RGBA16F attachment must also be framebuffer-complete. */
  private probeFloatTarget(): boolean {
    const gl = this.gl;
    if (!gl.getExtension("EXT_color_buffer_float")) return false;
    const t = this.newTex();
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA16F, 1, 1, 0, gl.RGBA, gl.HALF_FLOAT, null);
    const ok = this.attach(t);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.deleteTexture(t);
    return ok;
  }

  private attach(t: WebGLTexture): boolean {
    const gl = this.gl;
    gl.bindFramebuffer(gl.FRAMEBUFFER, this.fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, t, 0);
    return gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE;
  }

  private newTex(): WebGLTexture {
    const gl = this.gl;
    const t = gl.createTexture();
    if (!t) throw new Error("createTexture failed");
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    return t;
  }

  private drop(t: Tex | null | undefined): void {
    if (t) this.gl.deleteTexture(t.tex);
  }

  private loc(p: WebGLProgram, name: string): WebGLUniformLocation | null {
    let m = this.locs.get(p);
    if (!m) this.locs.set(p, (m = new Map()));
    if (!m.has(name)) m.set(name, this.gl.getUniformLocation(p, name));
    return m.get(name) ?? null;
  }

  private int(name: ViewUniform, v: number): void {
    this.gl.uniform1i(this.loc(this.view, name), v);
  }

  private float(name: ViewUniform, ...v: number[]): void {
    const l = this.loc(this.view, name);
    if (v.length === 1) this.gl.uniform1f(l, v[0]);
    else if (v.length === 2) this.gl.uniform2f(l, v[0], v[1]);
    else this.gl.uniform3f(l, v[0], v[1], v[2]);
  }

  private linInt(name: LinearizeUniform, v: number): void {
    this.gl.uniform1i(this.loc(this.linearize, name), v);
  }

  /** Upload A or B; null clears the slot. */
  setImage(slot: ImageSlot, px: Pixels | null): void {
    this.drop(this.images[slot]);
    delete this.images[slot];
    if (!px) return;
    if (px.kind === "half" && px.channels !== 4) throw new Error(`image ${slot} must be RGBA, got ${px.channels}`);
    this.images[slot] = this.floatOk ? this.uploadLinear(px) : this.uploadFallback(px);
  }

  private uploadLinear(px: Pixels): Tex {
    const gl = this.gl;
    const src = this.newTex();
    if (px.kind === "half") {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA16F, px.width, px.height, 0, gl.RGBA, gl.HALF_FLOAT, px.data);
    } else {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, px.source);
    }
    const out = this.newTex();
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA16F, px.width, px.height, 0, gl.RGBA, gl.HALF_FLOAT, null);
    if (!this.attach(out)) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.deleteTexture(src);
      gl.deleteTexture(out);
      throw new Error(`RGBA16F target ${px.width}x${px.height} is not framebuffer-complete`);
    }
    gl.viewport(0, 0, px.width, px.height);
    gl.disable(gl.SCISSOR_TEST);
    gl.useProgram(this.linearize);
    gl.bindVertexArray(this.vao);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, src);
    this.linInt("u_src", 0);
    this.linInt("u_decode", px.kind === "bitmap" || !px.linear ? 1 : 0);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.deleteTexture(src);
    gl.bindTexture(gl.TEXTURE_2D, out);
    gl.generateMipmap(gl.TEXTURE_2D);
    return { tex: out, width: px.width, height: px.height, mips: true, nearest: null };
  }

  private uploadFallback(px: Pixels): Tex {
    const gl = this.gl;
    const t = this.newTex();
    if (px.kind === "half") {
      const bytes = px.channels === 4 ? halfToSrgb8(px.data, px.linear) : null;
      if (!bytes) throw new Error("fallback upload needs RGBA");
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, px.width, px.height, 0, gl.RGBA, gl.UNSIGNED_BYTE, bytes);
    } else {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, px.source);
    }
    gl.generateMipmap(gl.TEXTURE_2D);
    return { tex: t, width: px.width, height: px.height, mips: true, nearest: null };
  }

  /** Edge mask (8-bit, white = edge); null clears it. */
  setMask(slot: MaskSlot, px: BitmapPixels | null): void {
    const gl = this.gl;
    this.drop(this.masks[slot]);
    delete this.masks[slot];
    if (!px) return;
    const t = this.newTex();
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, px.source);
    gl.generateMipmap(gl.TEXTURE_2D);
    this.masks[slot] = { tex: t, width: px.width, height: px.height, mips: true, nearest: null };
  }

  /** ΔE00 map as half floats (R16F, or RGBA16F with ΔE in R); null clears it. Sampling float textures needs no
   * extension, so the heatmap keeps full precision even on the 8-bit path. */
  setHeat(px: HalfPixels | null): void {
    const gl = this.gl;
    this.drop(this.heat);
    this.heat = null;
    if (!px) return;
    const t = this.newTex();
    if (px.channels === 1) {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R16F, px.width, px.height, 0, gl.RED, gl.HALF_FLOAT, px.data);
    } else {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA16F, px.width, px.height, 0, gl.RGBA, gl.HALF_FLOAT, px.data);
    }
    this.heat = { tex: t, width: px.width, height: px.height, mips: false, nearest: null };
  }

  /** NEAREST at >= 100% (no smoothing of the pixels being judged), trilinear below. */
  private filter(t: Tex, nearest: boolean): void {
    const gl = this.gl;
    gl.bindTexture(gl.TEXTURE_2D, t.tex);
    if (t.nearest === nearest) return;
    const min = nearest ? gl.NEAREST : t.mips ? gl.LINEAR_MIPMAP_LINEAR : gl.LINEAR;
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, min);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, nearest ? gl.NEAREST : gl.LINEAR);
    t.nearest = nearest;
  }

  private bindInputs(nearest: boolean): void {
    const gl = this.gl;
    const slots: [ViewUniform, Tex | null | undefined][] = [
      ["u_a", this.images.a],
      ["u_b", this.images.b],
      ["u_plateEdges", this.masks.plateEdges],
      ["u_candEdges", this.masks.candEdges],
      ["u_heat", this.heat],
    ];
    slots.forEach(([name, t], unit) => {
      gl.activeTexture(gl.TEXTURE0 + unit);
      if (t) this.filter(t, nearest);
      else gl.bindTexture(gl.TEXTURE_2D, this.blank);
      this.int(name, unit);
    });
    gl.activeTexture(gl.TEXTURE0 + slots.length);
    gl.bindTexture(gl.TEXTURE_2D, this.lut);
    this.int("u_lut", slots.length);
    gl.activeTexture(gl.TEXTURE0);
  }

  private commonUniforms(target: Size, image: Size, gain: number, nearest: boolean, linearOut: boolean): void {
    this.int("u_decode", this.floatOk ? 0 : 1);
    this.int("u_linearOut", linearOut ? 1 : 0);
    this.int("u_flipY", linearOut ? 0 : 1);
    this.int("u_hasPlateEdges", this.masks.plateEdges ? 1 : 0);
    this.int("u_hasCandEdges", this.masks.candEdges ? 1 : 0);
    this.int("u_nearest", nearest ? 1 : 0);
    this.float("u_target", target.width, target.height);
    this.float("u_image", image.width, image.height);
    this.float("u_gain", gain);
    this.float("u_edgeBase", EDGE_BASE);
    this.float("u_deMax", DE00_MAX);
    this.float("u_bg", ...BG_DISPLAY);
  }

  /** Mode actually drawable with what is uploaded (B or the ΔE00 map may still be loading). */
  private drawable(mode: Exclude<DisplayedMode, "tiles">): Exclude<DisplayedMode, "tiles"> {
    if ((mode === "b" || mode === "wipe" || mode === "diff") && !this.images.b) return "a";
    if (mode === "de00" && !this.heat) return "a";
    return mode;
  }

  draw(p: DrawParams): void {
    const gl = this.gl;
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, p.target.width, p.target.height);
    gl.disable(gl.SCISSOR_TEST);
    gl.clearColor(BG_DISPLAY[0], BG_DISPLAY[1], BG_DISPLAY[2], 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    const a = this.images.a;
    if (!a) return;
    const image = { width: a.width, height: a.height };
    const nearest = p.mode === "tiles" || p.view.scale >= 1;
    gl.useProgram(this.view);
    gl.bindVertexArray(this.vao);
    this.bindInputs(nearest);
    this.commonUniforms(p.target, image, p.gain, nearest, false);
    if (p.mode === "tiles") {
      // separate quads: each tile is a scissored draw at exactly 100%, its source rect pinned to the cell corner
      gl.enable(gl.SCISSOR_TEST);
      this.float("u_scale", 1);
      for (const c of p.tiles ?? []) {
        if (c.slot === "b" && !this.images.b) continue;
        gl.scissor(c.x, p.target.height - c.y - c.h, c.w, c.h);
        this.int("u_mode", c.slot === "a" ? VIEW_MODE.A : VIEW_MODE.B);
        this.float("u_translate", c.x - c.src.x0, c.y - c.src.y0);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
      }
      gl.disable(gl.SCISSOR_TEST);
      return;
    }
    this.int("u_mode", MODE_ID[this.drawable(p.mode)]);
    this.float("u_scale", p.view.scale);
    this.float("u_translate", p.view.tx, p.view.ty);
    this.float("u_wipeX", p.wipeX);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  /**
   * Linear float values of A, B or |A - B| * gain at image resolution (no zoom, pan or OETF), rows top-first, RGBA.
   * Null on the 8-bit path or when the inputs are missing. This is the parity hook for the browser smoke.
   */
  readAnalysis(kind: AnalysisKind, gain = 1): Float32Array | null {
    const gl = this.gl;
    const a = this.images.a;
    if (!this.floatOk || !a || (kind !== "a" && !this.images.b)) return null;
    const { width, height } = a;
    const t = this.newTex();
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA16F, width, height, 0, gl.RGBA, gl.HALF_FLOAT, null);
    let out: Float32Array | null = null;
    if (this.attach(t)) {
      gl.viewport(0, 0, width, height);
      gl.disable(gl.SCISSOR_TEST);
      gl.useProgram(this.view);
      gl.bindVertexArray(this.vao);
      this.bindInputs(true);
      const size = { width, height };
      this.commonUniforms(size, size, gain, true, true);
      this.int("u_mode", kind === "a" ? VIEW_MODE.A : kind === "b" ? VIEW_MODE.B : VIEW_MODE.DIFF);
      this.float("u_scale", 1);
      this.float("u_translate", 0, 0);
      this.float("u_wipeX", 0);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      out = new Float32Array(width * height * 4);
      gl.readPixels(0, 0, width, height, gl.RGBA, gl.FLOAT, out);
    }
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.deleteTexture(t);
    return out;
  }

  dispose(): void {
    const gl = this.gl;
    for (const t of Object.values(this.images)) this.drop(t);
    for (const t of Object.values(this.masks)) this.drop(t);
    this.drop(this.heat);
    this.images = {};
    this.masks = {};
    this.heat = null;
    gl.deleteTexture(this.blank);
    gl.deleteTexture(this.lut);
    gl.deleteFramebuffer(this.fbo);
    gl.deleteBuffer(this.quad);
    gl.deleteVertexArray(this.vao);
    gl.deleteProgram(this.linearize);
    gl.deleteProgram(this.view);
  }
}
