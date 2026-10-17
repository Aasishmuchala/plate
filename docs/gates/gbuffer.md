# G-buffer edges, circle-of-confusion weighting, invention flags (`plate_gates.gbuffer`, `coc`, `invention`)

The hard geometry gate of BUILD_PLAN decision 14: **`gbuffer_edge_recall_r2 >= 0.90` for delivery, `>= 0.85`
for preview, measured at 2048 wide, Tier A only**, and **`invention_flags = 0`**. The reference edges come from
the packet's own G-buffer (depth, camera-space normals, rank-0 Cryptomatte ids), never from an image, so the
gate asks one question of the AI pixels: *is every edge the geometry has still there, and is there no new one
where the geometry is flat?* Image-only Canny agreement (`align.py`) stays advisory.

Tier C packets (no G-buffer) cannot run either gate; they get `depth_agreement` (T16) and the advisory
image agreement instead. `invention.report()` says `tier C: no planar ids`.

## 1. Working width

Everything is measured at a working width, 2048 by default (`EdgeParams.width`). Resampling: `ids` and `Z` by
nearest neighbour (a blended id or depth is a value that exists nowhere), normals and images by area averaging
(cv2 `INTER_AREA`; on the rare upsample normals go nearest and images linear, because cv2's area filter
degenerates to nearest on zoom). A source narrower than the working width is **not upsampled**: upsampling adds
no edges and only dilutes the pixel tolerance, so the working width is `min(width, native)` and `EdgeMap.params`
records `width_native`, `width` and `scale`. The 640 px fixtures therefore evaluate at 640; a 3840 px render at
2048. The tolerance `r` (default 2) is in working-width pixels and is deliberately not rescaled; the report
carries the width.

## 2. Structural edges (`gbuffer.structural_edges(z, normals_cam, ids, K, params) -> EdgeMap`)

`EdgeMap` holds `edges` (bool, H x W), `kind` (uint8, bit-or of 1 depth, 2 normal, 4 id), `strength`
(float32 in [0, 1]) and `params` (every knob, for the gate report). Rules on 4-neighbour pairs `(p, q)`, q the
right or lower neighbour, the edge pixel being `p`:

| kind | rule | excluded |
|---|---|---|
| depth (1) | `abs(Z(p) - Z(q)) / min(Z) > 0.02` **and** the step is not explained by either side's tangent plane | either side background (`Z >= background_value`, `Z <= 0`, non-finite) |
| normal (2) | angle between unit normals `> 20 deg` | background, or a normal with norm below 0.5 |
| id (4) | rank-0 object id differs; a transition to id 0 (nothing rendered) counts, a roofline against the sky is an edge the output must keep | none |

The tangent-plane term is the one addition to the plan's depth rule. With the pixel ray `r = ((u - cx)/fx,
-(v - cy)/fy, -1)` (plate-cam-v0: +Y up, the camera looks down -Z) and `p`'s camera-space normal `n_p`, the
depth `p`'s own plane predicts at `q` is

```
Z_q^ = Z_p * (n_p . r_p) / (n_p . r_q)
```

and the depth edge survives only if `min(|Z_q - Z_q^|, |Z_p - Z_p^|) / min(Z) > 0.02` as well. A floor or a
driveway seen at a grazing angle changes depth by more than 2 % per pixel without any discontinuity (on the
synthetic scene the box's side face alone adds 78 false depth-edge pixels, precision 0.845 -> 1.000 with the
test); an occlusion boundary is never explained by a plane. The term can only suppress, so a host with the
wrong normal convention degrades to the raw rule, never past it. It needs `K` (3x3, the manifest
`intrinsics_px` block or the whole camera block) and normals; without them the raw rule runs and
`params.plane_test` is false.

Each kind is thinned to 1 px (Zhang-Suen, `gbuffer.thin`) before the bit-or. Strength: depth step / (10 x
0.02), normal angle / 90 deg, id 1.0, max over kinds. `EdgeMap.by_kind("depth" | "normal" | "id")`,
`counts()`, `to_report()`.

`gbuffer.from_packet(packet_dir)` reads `manifest.json` + `beauty.exr` through `plate_pixels.exr` (lazy import)
and returns `(EdgeMap, ctx)` with the native `z`, `normals`, `ids`, `ranks`, `coverage`, `legend`, `camera`
and `units` for the weights and class splits.

## 3. Circle-of-confusion weighting (`coc.py`)

Thin lens, per pixel, with `Z` and `Zf` in mm (`x meters_per_unit x 1000`):

```
A    = f / N                                  aperture, mm
c_mm = A * |Z - Zf| / Z * f / (Zf - f)        blur disc on the sensor
c_px = c_mm * width_px / film_width_mm        the disc at the working width
w    = clip(1 - c_px / c_max, 0, 1)           c_max = 6 px at 2048, scaled with the width
```

`dof_enabled: false` gives `c_px = 0` and `w = 1` everywhere. Worked number: f 35 mm, N 2.8, Zf 2.5 m, Z 4 m
-> A 12.5 mm, `c_mm = 12.5 x 1.5 / 4 x 35 / 2465 = 0.0666 mm` -> at 2048 px / 36 mm = **3.79 px** -> **w 0.37**.
Because `c_px` and `c_max` both scale with the width, the weight of a pixel is the same at 640 and at 2048.
On `fixtures/packets/room_a` (CAM_a, f/2.8 at 2.5 m) pixels at the focus distance carry w >= 0.97 and the far
wall (Z > 5 m) at most 0.16.

`coc.coc_px(z, camera, width_px)`, `coc.weights(z, camera, width_px)`; `camera` is the manifest camera block
(or the whole manifest, whose `units.meters_per_unit` is then used). Pass the depth already resampled to the
EdgeMap's size (`gbuffer.resample_z`).

## 4. Edge recall (`gbuffer.edge_recall(ref_edges, test_rgb, weights, r=2, *, plate_rgb, ids, legend)`)

The test image is brought to the EdgeMap's size and run through `gbuffer.test_edges`: Gaussian blur sigma 1.2,
then Canny on all three channels (the gradient is the largest over R, G, B) with **fixed** hysteresis
thresholds 20 / 50 on the L1 Sobel magnitude (a step of about 12 levels after the blur). Test edges are dilated
by `r` (ellipse `(2r+1)^2`) and

```
recall_r2 = sum(w . [ref & dilate_r(test)]) / sum(w)      over reference edge pixels
```

`w` is the product of the CoC weight and, when `plate_rgb` is given, a **visibility** term: a reference edge
the plate itself does not show (same detector, within `r`) weighs 0. A frameless glass pane against the sky,
two coplanar walls of one material in shadow: geometry the render cannot show is not demanded of the output,
and a plate scores exactly 1.0 against its own G-buffer. The report keeps the unfiltered numbers:
`recall_r2_all` (CoC only), `recall_r2_unweighted`, and `plate_visibility` (the weighted share of reference
edges the plate shows). Also `recall_r0`, `precision_r2` (share of test edges within `r` of a reference edge,
unweighted; advisory, it drops on grain and on texture), `recall_by_kind`, `recall_by_class` (an edge pixel
counts for every legend class touching its 3x3 neighbourhood, so the wall/shrub boundary counts for both
architecture and vegetation), `best_shift` (`structure.best_shift` on the visible reference edges, +-8 px),
`n_ref`, `n_ref_visible`, `n_test`, `backend`, `detector`, `canny`, `width`, `r`. `gbuffer.verdict(result,
"delivery" | "preview")` scores it against `thresholds.yaml`.

### Why not the fidelity_bench thresholds

The plan said "Canny on the test (fidelity_bench per-image thresholds)". Those thresholds are `0.66 x` and
`1.33 x` the median *intensity* (floored at 20 / 50), a photographic heuristic unrelated to the gradient. On
`room_a` the display finds only 2455 edge pixels against 5559 reference pixels: **self-recall 0.53** with
precision 0.985, i.e. far too conservative for a gate whose job is recall. Measured alternatives on the same
fixture (clean / grain sigma 8 added / blur sigma 3 applied), unweighted recall and precision:

| detector | clean | grain 8 | blur 3 |
|---|---|---|---|
| fidelity_bench (median-adaptive, gray) | 0.534 / 0.985 | 0.528 / 0.982 | 0.076 / 0.997 |
| noise-adaptive (6 x Immerkaer sigma) | 0.836 / 0.849 | 0.670 / 0.877 | 0.859 / 0.110 |
| gradient-MAD adaptive (4 x) | 0.901 / 0.235 | 0.887 / 0.374 | 0.746 / 0.722 |
| fixed 20 / 50, gray | 0.845 / 0.764 | 0.856 / 0.726 | 0.481 / 0.953 |
| **fixed 20 / 50, RGB (shipped)** | **0.890 / 0.704** | 0.900 / 0.405 | 0.656 / 0.941 |

Fixed thresholds are the only rule robust in both directions: grain does not inflate recall and a blur is
caught (every adaptive rule either inflates on grain or lets the blurred output through). Colour matters: the
synthetic `window_added` pair paints a chroma edge with under 10 levels of luma contrast, which gray Canny
misses entirely (0 edge pixels in the rectangle; 109 with RGB). `detector="fidelity_bench"` is kept for
comparison with the historical numbers and reported in `backend`.

### Measured on the fixtures

| vector | value |
|---|---|
| synthetic `make_depth_scene` G-buffer edges vs analytic edges | recall 0.995 at r = 0, 1.000 at r = 1, precision 1.000 |
| synthetic render vs its G-buffer | recall_r2 1.000, precision 1.000 |
| `room_a` display vs its G-buffer, CoC-weighted, no plate | **0.907** (unweighted 0.890; the gap is the frameless pane against the sky and the shadowed N/E wall corner) |
| the same with `plate_rgb` = display | 1.000, plate_visibility 0.907 |
| display shifted 3 px right | 0.534, `best_shift` (-3, 0) recovers it at 1.000 |
| display blurred sigma 3 | 0.733 (fail) |

The 0.907 is the plan's "the render agrees with its own geometry >= 0.90" sanity vector; it holds only with the
CoC weighting the plan itself specifies and with the RGB detector, and by 0.007. The daemon must pass the
plate: the number the gate blocks on is the visibility-weighted `recall_r2`.

## 5. Invention flags (`invention.flags(test_rgb, ref_edges, ids, legend, width=2048, *, plate_rgb)`)

Per legend entry with `planar: true` and class in `{architecture, ground, glazing}`: the rank-0 id mask eroded
by 3 px is the region; inside it, `test_edges(test)` minus every pixel within 2 px of a reference edge (and of a
plate edge when `plate_rgb` is given); 8-connected components, gaps of 1 px bridged so a broken outline is one
flag, with pixel count `>= L_long = round(30 x width / 1500)` (41 at 2048, `thresholds.long_edge_min_px`) give
one `Flag {id, class, name, bbox, length_px, n_edges, centroid}` each, longest first. `invention.report()`
wraps it as `{metric_id, count, flags, skipped, reason, status, bound, params}`; Tier C, a missing EdgeMap or
ids, or a legend without planar entries of a scanned class returns `None` / `reason: "tier C: no planar ids"`.

"New" means new against the plate as well as the geometry. A planar id is not a flat colour: floor tiles, a
shadow line, clouds seen through a pane (the pane owns rank 0 there) are long edges inside a planar id that the
render already shows. On `room_a` the display scores 7 flags against its G-buffer alone and **0** with its own
edges subtracted, while a 60 px rectangle drawn on `ARCH_wall_N` is still one flag with the rectangle inside
its bbox. Synthetic vectors: `window_added` -> exactly one flag on the wall id, bbox containing the rectangle;
`along_edge=True` (the box's front face repainted, its outline on existing geometry) -> 0; the unchanged render
-> 0.

Limits: what is seen *through* glazing has no G-buffer edges (rank 0 is the pane), so an invented interior
behind glass is caught only when it adds long edges on the pane; signage lettering is the OCR text-lock's job
(T16), not this gate's.

## 6. Thresholds and report fields

`thresholds.yaml`: `gbuffer_edge_recall_r2 >= 0.90` (variants delivery 0.90 / preview 0.85, tier A,
resolution 2048, hard), `invention_flags = 0` (hard), `long_edge_min_px 30 @ 1500` (parameter). A gate report
(T6 `report.GateReport`) stores the `edge_recall` dict under `metrics`, `EdgeMap.to_report()` for the edge
parameters, the invention `report()` dict, and `backend` in `backend_notes`.
