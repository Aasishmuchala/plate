# Viewer (T24)

WebGL2 compare viewer for the canvas: wipe, toggle, difference, red-cyan G-buffer edge overlay, ΔE00 heatmap and
auto-placed 100% crop tiles at gate-flagged points. Pixels stay 16-bit from the daemon to the shader.

| file | what |
|---|---|
| `Viewer.tsx` | the React component, HUD, keyboard and pointer handling, the `ViewerHandle` |
| `renderer.ts` | the WebGL2 renderer (uploads, LINEARIZE pass, VIEW pass, tiles, float read-back) |
| `state.ts` | the mode state machine and keyboard map (pure reducer) |
| `math.ts` | sRGB curves, half-float packing, `tileLayout` / `tileGrid`, view transform, ΔE00 LUT, `referenceDiff` |
| `loaders.ts` | fetch + decode of the sources, byte-count checks, bearer token on this origin only |
| `types.ts` | `ImageSource`, `EdgeSource`, `HeatSource`, `Overlays`, `ViewerMode`, `Zoom`, `Pan` |
| `../shaders/viewer.ts` | GLSL ES 3.00 sources and the uniform lists |
| `smoke.html` / `smoke.ts` | dev-only GPU smoke (real browser, automatic PASS/FAIL) |
| `demo.html` / `demo.tsx` | dev-only demo of the real component on synthetic sources (manual checks) |
| `../test/viewer.test.ts` | vitest: math, tile parity, state machine, loaders, renderer against a recording GL, DOM |

## Props

```ts
<Viewer
  a={{ url, width, height, encoding: "srgb8" | "png16" | "exr-half", label?, data? }}
  b={...}                                     // optional; wipe, toggle and diff need it
  mode="wipe" | "toggle" | "diff" | "edges" | "de00" | "tiles"
  wipe={0.5}                                  // fraction of the viewer width
  zoom={1 | "fit"}                            // device pixels per image pixel; 1 = true 100%
  pan={{ x, y } | null}                       // image pixel at the viewport centre; null = image centre
  overlays={{ edges?: { plate?, candidate? }, heatmap?: { url, width, height }, tiles?: Point[] }}
  diffGain={4}
  onChange={(c) => ...}                       // { mode, wipe, zoom, pan, showB } after a user change
  ref={handle}
/>
```

All props are optional so App can mount `<Viewer />` before any source exists (it shows "No image selected"). The
mode, wipe, zoom and pan props seed the viewer's state and re-sync it whenever they change; keyboard and pointer
changes come back through `onChange`. A mode whose inputs are missing is refused with a short notice ("no B image to
compare") and the viewer draws what it can (the last compare mode, else A).

## Transport (T25 serves this)

- `png16` and `exr-half`: `GET /api/v0/assets/<hash>?fmt=rgba16f` (the viewer appends `fmt=rgba16f` when the URL
  has no `fmt`). Body = raw little-endian IEEE binary16, RGBA interleaved, `width * height * 8` bytes, rows top-first,
  no header. `png16` values are the 16-bit file's sRGB code values / 65535; `exr-half` values are already linear.
  Any other byte count is an error shown in the frame.
- `srgb8`: an ordinary PNG/JPEG, decoded with `createImageBitmap(..., { colorSpaceConversion: "none" })`.
- Heatmap: the URL is used as given. Body = raw binary16, `width * height * 2` bytes (R16F) or `* 8` (RGBA16F, ΔE in
  R), the per-pixel CIEDE2000 the daemon computed. The viewer only colour-maps it.
- Edge masks: 8-bit PNGs, white = edge (threshold 0.5): `plate` = G-buffer structural edges, `candidate` = Canny on
  the candidate from the gate assets.
- `data` (Blob or ArrayBuffer) on a source replaces the fetch; tests and the demo use it.
- The bearer token is sent to relative and same-origin URLs only, never to a blob:, data: or third-party URL.

## Colour and sampling

- Upload: half floats as `RGBA16F` / `HALF_FLOAT`, 8-bit images as `RGBA8`. The LINEARIZE pass applies the sRGB EOTF
  (srgb8, png16) or copies (exr-half) into a linear `RGBA16F` render target, which needs `EXT_color_buffer_float`
  (also probed for framebuffer completeness). The VIEW pass works in linear light and applies the sRGB OETF last.
  Constants are those of `gates/plate_gates/color.py` (0.04045, 12.92, 2.4, 0.0031308).
- Without float targets every image goes to `RGBA8` holding sRGB code values, decoded per sample, and the HUD shows
  **8-bit preview**. `readAnalysis` returns null on that path.
- Sampling is `NEAREST` whenever zoom ≥ 100% (and in tiles); trilinear mipmaps below 100%. At ≥ 100% the image is
  placed on whole device pixels.
- diff = |A − B| × gain in linear light, then the OETF. Edges: red = plate only, cyan = candidate only, white = both,
  over the plate at 35% brightness (the colour convention of `gates/plate_gates/align.edge_agreement`).
- ΔE00 LUT, fixed, 0..10, linear in sRGB 8-bit between stops on the gate thresholds:
  0 `#0b0d12`, 1 `#2b4fb8` (feather p99 / RAL limit), 2 `#29b6c8` (material median), 3.5 `#f2d43d`,
  5 `#f08a24` (material p90), 10 `#e0245e` (and above, and NaN).
- Tiles: one native-pixel crop per flagged point, centred and clamped exactly as `gates/plate_gates/crops.point_tiles`
  (Python's half-to-even rounding included), drawn as separate scissored quads at 100%, A with B beside it. The crop
  size is the largest ≤ 512 px at which every tile fits below the HUD.
- Wipe and toggle with a B of a different size stretch B onto A's frame and say so in a badge.

## Keys and pointer (viewer focused)

`` ` `` toggle A/B · `[` `]` wipe ±0.05 (`{` `}` ±0.01) · `1` 100% · `f` fit · `d` diff · `e` edges · `h` ΔE00
heatmap · `t` tiles (each of d/e/h/t pressed again returns to wipe or toggle) · arrows pan 64 device px (shift 256) ·
`+` `-` zoom by powers of two · `?` key list. Drag pans; dragging the wipe line moves it; the wheel zooms about the
cursor (snapping to 100% within 3%); double-click toggles fit and 100% at the cursor. Ctrl/Cmd/Alt combinations are
left to the browser.

## Handle and DOM hooks

`ref` (and `element.plateViewer` on the `[data-testid=viewer]` element, for the browser smoke) expose
`readAnalysis(kind: "a" | "b" | "diff", gain = 1)` (linear floats at image size, RGBA, rows top-first),
`ready(slot = "a")`, `gpu()` (`"ok"`, `"8-bit"`, `"unavailable"`, `"lost"`, `"error"`, `"idle"`) and `state()`.
The root also carries `data-mode` (what is drawn: `a b wipe diff edges de00 tiles empty`), `data-gpu`
(`float`, `8-bit`, `unavailable`, ...) and `data-ready` (loaded slots: `a b plateEdges candEdges heat`).

## Automated checks

- `npm test` runs `src/test/viewer.test.ts` (no GPU).
- GPU smoke, real browser: start Vite (`npm run dev`, or any free port with
  `npx vite --host 127.0.0.1 --port 5199 --strictPort` from `ui/`), then open `/src/viewer/smoke.html`. The title
  turns `PASS` or `FAIL` and `#result` holds the JSON report. Headless:

  ```
  "<chrome>" --headless=new --use-angle=metal --virtual-time-budget=15000 --dump-dom http://127.0.0.1:5199/src/viewer/smoke.html
  ```

  (`--use-angle=swiftshader --enable-unsafe-swiftshader` for the software path; on Windows use `--use-angle=d3d11`.)
  It checks: shaders compile; the 64×64 half-float diff of a buffer against itself reads back all zeros; float
  read-back of |A − B| × gain matches `math.referenceDiff` for png16 and exr-half; the EOTF and top-first row order of
  the linear target; OETF and orientation on the canvas at 100%; wipe split; LUT colours; red/cyan/white edges; a tile
  corner pixel; the RGBA8 fallback within 8-bit rounding. Verified 2026-10-17 on ANGLE Metal (Apple M1 Pro) and
  SwiftShader: all PASS.

## Manual checklist for the T53 browser smoke

Run on `/src/viewer/demo.html` under the dev server, and again inside the built UI on `:4610` once T25 serves
`?fmt=rgba16f` and a panel passes real sources. Each line is pass/fail.

1. The frame shows the image at fit with `NN% fit`; no console errors; `data-gpu="float"` (or the amber
   **8-bit preview** badge where float targets are missing).
2. `1` shows `100%`; pixels are hard-edged (no smoothing); `f` returns to fit.
3. `+` from fit goes to 50% → 100% → 200%; the wheel zooms about the cursor and lands on exactly 100% near it.
4. Arrows pan by a fixed device step; drag pans; the image cannot be panned out of view.
5. In wipe, `[`/`]` move the line by 5% and stop at the edges; dragging the line moves it; A labels left, B right.
6. `` ` `` alternates A and B (HUD shows the label); `d`, `e`, `h`, `t` each toggle their mode and back.
7. `d`: identical regions are black; differences are bright; the HUD shows `Diff ×4`.
8. `e`: red where only the plate has edges, cyan where only the candidate does, white where both; legend shows.
9. `h`: the ΔE00 legend matches the colours; regions with ΔE 0 are near black, ≥ 10 saturate to red-magenta.
10. `t`: one A|B pair per flagged point, at 100%, below the HUD chips, labelled 1..n; sizes shrink to fit, never
    upscale.
11. Keys with no inputs (e.g. `e` without masks) show a short notice and change nothing.
12. On the viewer element, `el.plateViewer.readAnalysis("diff")` with A = B returns all-zero RGB.
13. Resize the window: the canvas stays sharp (backing store follows the device pixel ratio).
