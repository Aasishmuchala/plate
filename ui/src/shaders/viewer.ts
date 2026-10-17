// GLSL ES 3.00 for the WebGL2 viewer (ui/src/viewer/renderer.ts); shader text stays ASCII, comments included. Two
// programs share one fullscreen-triangle vertex shader and work from gl_FragCoord (no texture-coordinate attribute):
//   LINEARIZE  source texel -> linear RGBA16F target, once per upload (sRGB EOTF for srgb8/png16, copy for exr-half)
//   VIEW       linear A/B (+ edge masks, ΔE00 map, LUT) -> canvas, or -> a float FBO for read-back
// Row 0 of every texture is the top row of the image. The canvas pass flips gl_FragCoord.y (u_flipY = 1); FBO passes
// do not, so a read-back returns rows top-first. The sRGB constants match gates/plate_gates/color.py.

import { DE00_LUT_SIZE } from "../viewer/math";

/** u_mode values; the TS side and the GLSL side read the same numbers. */
export const VIEW_MODE = { A: 0, B: 1, WIPE: 2, DIFF: 3, EDGES: 4, DE00: 5 } as const;

/** Display colour outside the image (the UI background, #101114). */
export const BG_DISPLAY: readonly [number, number, number] = [16 / 255, 17 / 255, 20 / 255];

export const VERT = `#version 300 es
layout(location = 0) in vec2 a_pos;
void main() {
  gl_Position = vec4(a_pos, 0.0, 1.0);
}
`;

const SRGB = `
vec3 srgbToLinear(vec3 c) {
  vec3 lo = c / 12.92;
  vec3 hi = pow(max((c + 0.055) / 1.055, vec3(0.0)), vec3(2.4));
  return mix(lo, hi, step(vec3(0.04045), c));
}
vec3 linearToSrgb(vec3 c) {
  vec3 lo = c * 12.92;
  vec3 hi = 1.055 * pow(max(c, vec3(0.0)), vec3(1.0 / 2.4)) - 0.055;
  return mix(lo, hi, step(vec3(0.0031308), c));
}
`;

export const LINEARIZE_FRAG = `#version 300 es
precision highp float;
uniform sampler2D u_src;
uniform int u_decode; // 1: the source holds sRGB code values; 0: already linear
out vec4 outColor;
${SRGB}
void main() {
  vec4 s = texelFetch(u_src, ivec2(gl_FragCoord.xy), 0);
  outColor = vec4(u_decode == 1 ? srgbToLinear(s.rgb) : s.rgb, 1.0);
}
`;

export const VIEW_FRAG = `#version 300 es
precision highp float;
const int MODE_A = ${VIEW_MODE.A};
const int MODE_B = ${VIEW_MODE.B};
const int MODE_WIPE = ${VIEW_MODE.WIPE};
const int MODE_DIFF = ${VIEW_MODE.DIFF};
const int MODE_EDGES = ${VIEW_MODE.EDGES};
const int MODE_DE00 = ${VIEW_MODE.DE00};
const float LUT_SIZE = ${DE00_LUT_SIZE}.0;
uniform sampler2D u_a;
uniform sampler2D u_b;
uniform sampler2D u_plateEdges;
uniform sampler2D u_candEdges;
uniform sampler2D u_heat;
uniform sampler2D u_lut;
uniform int u_mode;
uniform int u_decode;     // 1 in the RGBA8 fallback: A/B hold sRGB code values, decode per sample
uniform int u_linearOut;  // 1 for the float read-back target: no OETF, zero outside the image
uniform int u_flipY;      // 1 for the canvas (GL rows grow upward), 0 for FBOs (row 0 = image row 0)
uniform int u_hasPlateEdges;
uniform int u_hasCandEdges;
uniform int u_nearest;    // zoom >= 100%: edge masks are binary
uniform vec2 u_target;    // render target size, device pixels
uniform vec2 u_image;     // image size, pixels
uniform float u_scale;    // device pixels per image pixel
uniform vec2 u_translate; // device position of image pixel (0, 0), top-left origin
uniform float u_wipeX;    // wipe line, device pixels from the left
uniform float u_gain;     // diff gain
uniform float u_edgeBase; // brightness of the plate under the edge overlay
uniform float u_deMax;    // top of the heatmap LUT (dE00 10)
uniform vec3 u_bg;        // display colour outside the image
out vec4 outColor;
${SRGB}
vec3 lin(sampler2D t, vec2 uv) {
  vec3 c = texture(t, uv).rgb;
  return u_decode == 1 ? srgbToLinear(c) : c;
}
float edgeAt(sampler2D t, vec2 uv) {
  float v = texture(t, uv).r;
  return u_nearest == 1 ? step(0.5, v) : clamp(v * 4.0, 0.0, 1.0);
}
vec4 display(vec3 linear) {
  if (u_linearOut == 1) return vec4(linear, 1.0);
  return vec4(linearToSrgb(clamp(linear, 0.0, 1.0)), 1.0);
}
void main() {
  vec2 frag = vec2(gl_FragCoord.x, u_flipY == 1 ? u_target.y - gl_FragCoord.y : gl_FragCoord.y);
  vec2 img = (frag - u_translate) / u_scale;
  if (img.x < 0.0 || img.y < 0.0 || img.x >= u_image.x || img.y >= u_image.y) {
    outColor = u_linearOut == 1 ? vec4(0.0) : vec4(u_bg, 1.0);
    return;
  }
  vec2 uv = img / u_image;
  if (u_mode == MODE_A) {
    outColor = display(lin(u_a, uv));
  } else if (u_mode == MODE_B) {
    outColor = display(lin(u_b, uv));
  } else if (u_mode == MODE_WIPE) {
    outColor = display(frag.x < u_wipeX ? lin(u_a, uv) : lin(u_b, uv));
  } else if (u_mode == MODE_DIFF) {
    outColor = display(abs(lin(u_a, uv) - lin(u_b, uv)) * u_gain);
  } else if (u_mode == MODE_EDGES) {
    float pe = u_hasPlateEdges == 1 ? edgeAt(u_plateEdges, uv) : 0.0;
    float ce = u_hasCandEdges == 1 ? edgeAt(u_candEdges, uv) : 0.0;
    vec3 base = linearToSrgb(clamp(lin(u_a, uv), 0.0, 1.0)) * u_edgeBase;
    float k = max(pe, ce);
    // red = plate only, cyan = candidate only, white = both (gates align.edge_agreement overlay)
    vec3 edge = k > 0.0 ? vec3(pe, ce, ce) / k : vec3(0.0);
    outColor = vec4(mix(base, edge, k), 1.0);
  } else if (u_mode == MODE_DE00) {
    float de = texture(u_heat, uv).r;
    float t = isnan(de) ? 1.0 : clamp(de / u_deMax, 0.0, 1.0);
    outColor = vec4(texture(u_lut, vec2((t * (LUT_SIZE - 1.0) + 0.5) / LUT_SIZE, 0.5)).rgb, 1.0);
  } else {
    outColor = vec4(1.0, 0.0, 1.0, 1.0);
  }
}
`;

/** Every uniform the renderer sets, per program (the test checks each is declared, so a typo cannot go silent). */
export const LINEARIZE_UNIFORMS = ["u_src", "u_decode"] as const;
export const VIEW_UNIFORMS = [
  "u_a",
  "u_b",
  "u_plateEdges",
  "u_candEdges",
  "u_heat",
  "u_lut",
  "u_mode",
  "u_decode",
  "u_linearOut",
  "u_flipY",
  "u_hasPlateEdges",
  "u_hasCandEdges",
  "u_nearest",
  "u_target",
  "u_image",
  "u_scale",
  "u_translate",
  "u_wipeX",
  "u_gain",
  "u_edgeBase",
  "u_deMax",
  "u_bg",
] as const;

export type LinearizeUniform = (typeof LINEARIZE_UNIFORMS)[number];
export type ViewUniform = (typeof VIEW_UNIFORMS)[number];
