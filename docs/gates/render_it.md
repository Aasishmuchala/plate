# Render-it router math (`plate_gates.router_math`)

The pre-spend numbers behind BUILD_PLAN decision 15: before a generator is asked to invent geometry, the packet's
own depth, ids and camera path say whether it can. Three pure functions over packet geometry, no images, no
engine. Advisory in P1, blocking in P3 (`mode`), calibrated by the three-key labelling (T23) before it blocks.

Tier C packets (estimated, relative depth) cannot feed any of this: the router reports `unknown` and a human
decides (PACKET.md section 11).

## 1. Detail resolvability

```
D(p) = fx * s_units / Z(p)        s_units = detail_size_m / meters_per_unit
```

`detail_resolvability(z, ids, legend, K, units, detail_m=None, *, bg_value=None)`

| input | meaning |
|---|---|
| `z` | planar depth (H, W), scene units, from the beauty EXR `zdepth` element |
| `ids` | rank-0 Cryptomatte object ids (H, W) uint32 (`read_cryptomatte(...)[0][..., 0]`; a full ranks array is accepted) |
| `legend` | `id_legend.json` (or its `entries`) |
| `K` | 3x3 or the manifest `intrinsics_px` block; only `fx` is used |
| `units` | manifest `units` block or `meters_per_unit` |
| `detail_m` | per-class overrides in metres |

Characteristic detail sizes (metres): vegetation (leaf) **0.06**, ground (brick) **0.065**, ground whose
material or node name contains `sett` **0.10**, furniture (fabric weave) **0.02**. A legend entry's own
`detail_size_m` wins over the class default (`fixtures/packets/room_a` carries `0.1` on `HARD_floor` and `0.06`
on the shrub). Every other class (architecture, glazing, signage, sky, people, water, vehicle, other) is
untextured for this purpose and is excluded. Background pixels (`bg_value`, 1e10 in V-Ray and the fixtures) and
non-finite depth are excluded.

The median is taken over the union of textured pixels (`D_px_median`) and per class (`per_class`) and per id
(`per_id`). Verdict:

| `D_px_median` | verdict | meaning |
|---|---|---|
| `< 8` px | `vray_only` | the generator cannot resolve a leaf; V-Ray renders, AI only for people and patches by region |
| `8 .. 24` px | `ai_by_region` | AI edits inside ID masks only |
| `>= 24` px | `whole_frame_ok` | a whole-frame re-photograph is allowed for close and medium frames (the engine router still checks `shot_class`) |
| no textured pixel | `unknown` | |

Worked vectors (`gates/tests/test_router_math.py`): a leaf at 10 m with `fx = 3733` (35 mm on 3840 px) gives
`0.06 * 3733 / 10 = 22.4` px (`ai_by_region`); the same leaf at 30 m gives `7.5` px (`vray_only`). The same
scene in millimetres with `meters_per_unit = 0.001` gives the same numbers.

Fixture stills (Blender conformance room, computed on this Mac): `room_a` (640 px, `fx` 622.2) `D_px_median`
**19.13 px**, ground 14.00, vegetation 19.43, `ai_by_region`; `dolly_hero` (320 px, `fx` 497.8) **13.14 px**,
`ai_by_region`.

## 2. Disocclusion budget

`disocclusion_budget(z_start, cam_start, z_end, cam_end, K, bg_value, *, pivot=None, depth_kind="planar",
K_start=None, rel_tol=0.01, abs_tol=0.005)`

For every end-frame pixel with valid depth: back-project to world with `cam_end` (`plate-cam-v0`: pixel centres
at +0.5, camera looks down -Z, planar depth along the view axis, `world_from_camera` with column vectors), project
into `cam_start`, and call it **invisible** when it is

- **behind** the start camera (`depth_in_start <= 0`),
- **outside** the start frame (`u < 0`, `u >= W`, `v < 0` or `v >= H`), or
- **occluded**: `depth_in_start > Z_start(bilinear at the landing point) * (1 + 0.01) + 0.005 * depth_in_start`.

`fraction = invisible / valid`. The bilinear sample renormalises its weights over the valid (non-background)
neighbours; where no neighbour is valid nothing is in front and the pixel counts as visible. The 1 % + 0.5 %
tolerance stops a sloped surface from hiding itself after resampling (tested on a 20 degree pitched wall: zero
occluded pixels). `depth_kind: radial` (PACKET.md 4) is converted to planar by the ray length before use; the
vectorised projection is the same formula as `plate_spec.project_point`.

**Orbit** `= angle between the forward axes (deg) + angle the two camera centres subtend at the look-at pivot`
(when a pivot is given; `forward_deg` and `pivot_deg` are reported separately).

| condition | verdict |
|---|---|
| `fraction <= 0.02` | `ai_interpolate` |
| `0.02 < fraction <= 0.10` | `rendered_endpoints_plus_clay` (rendered start and end frames plus the clay motion reference) |
| `fraction > 0.10` or `orbit > 30` deg | `render_move` (Vantage/V-Ray renders the move; AI does people only) |
| no valid end-frame depth | `unknown` |

Analytic test scene (two planes: wall at 4 m, box 1.0 x 0.8 m at 2.5 m, 320 x 180, `f` 200 px):

| move | analytic | measured | verdict |
|---|---|---|---|
| lateral dolly 1 m | `(50 * 180 + 30 * 64) / 57600 = 0.1896` | exact (integer pixel shifts) | `render_move` |
| lateral dolly 0.37 m | `0.0701` | within 0.5 points | `rendered_endpoints_plus_clay` |
| push-in 0.3 m | 0 (the box shadow grows) | `< 0.02` | `ai_interpolate` |
| 35 degree orbit about a pivot 3 m ahead | orbit 35 (70 with the pivot) | 35.0 | `render_move` |
| turn round behind the wall | everything behind | `fraction 1.0`, `behind == n_valid` | `render_move` |

The analytic strip widths: outside `f * d / Zw` px wide over the full height; occluded `f * d * (Zw / Zb - 1) / Zw`
px wide and `f * (by1 - by0) / Zb` px tall.

**Committed fixture** `fixtures/packets/dolly_12f` (0.6 m forward dolly over 12 frames at 320 x 180, frames 0
and 11): `fraction = 0.0152` (818 of 53 950 valid pixels, all `occluded`, none outside or behind), `orbit 0.0`
deg, verdict **`ai_interpolate`**. The 818 are the one-pixel silhouette bands of the near objects where bilinear
resampling mixes two depths, plus the floor revealed under the cabinet edge; the test freezes the number at
`0.0152 +- 0.002`.

## 3. `route_move`

`route_move(packet_motion, z_seq_first, z_seq_last, crossings=None, *, mode=None, detail=None) -> RouterVerdict`

`packet_motion` is a mapping with `cam_start`, `cam_end` (4x4 or camera block), `K` (sequence-frame intrinsics),
`bg_value`, and optionally `pivot`, `depth_kind`, `crossings` (the manifest's `motion.path_crossings`),
`packet_id`. `load_motion(packet_dir)` builds it from a motion packet folder: cameras from `camera_frames.json`,
`K` scaled by `s = w_seq / W` (PACKET.md 4), `bg_value` and `depth_kind` from the `zdepth` element, Z frames
read through `plate_pixels.exr.read_elements` with the same manifest.

Any path crossing (`threshold`, `room_change`, `occluder`) makes the verdict `render_move`: a cut for the
generator is a cut. `crossings` passed as an argument are appended to the manifest's.

`RouterVerdict` (pydantic): `mode` (`advisory` | `blocking`), `verdict`, `numbers`, `explanation`, and the
computed `blocks = mode == "blocking" and verdict == "render_move"` which the Move policy (T43) acts on and the
Motion editor's verdict card (T47) shows. `numbers` carries `disocclusion_fraction`, `orbit_deg`, `forward_deg`,
`pivot_deg`, the pixel counts (`n_valid`, `n_invisible`, `outside`, `behind`, `occluded`), `crossings`, the
thresholds, and when a `detail_resolvability` result of the hero still is passed, `D_px_median`,
`detail_verdict`, `detail_per_class` (reported, not folded into the move verdict: resolvability governs what
AI may re-photograph in a still, the budget governs whether it may move).

`mode`: explicit argument > `PLATE_ROUTER_MODE` environment variable > `advisory`. The daemon passes the
project's setting explicitly (P1 ships advisory, P3 flips to blocking after calibration); the environment
variable is the seam for the CLI and tests until the daemon's settings carry the field.

## Thresholds and provenance

| constant | value | source |
|---|---|---|
| `DETAIL_M` | vegetation 0.06, ground 0.065, furniture 0.02 m; `GROUND_SETT_M` 0.10 | BUILD_PLAN decision 15 (leaf, brick, sett); fabric added by T28 |
| `D_VRAY_ONLY_PX` / `D_WHOLE_FRAME_PX` | 8 / 24 px | BUILD_PLAN decision 15 |
| `DISOCC_AI_MAX` / `DISOCC_CLAY_MAX` | 0.02 / 0.10 | BUILD_PLAN decision 15 |
| `ORBIT_MAX_DEG` | 30 | BUILD_PLAN decision 15; Lensbook risk ladder `orbit_le_30` |
| `OCCLUSION_REL_TOL` / `OCCLUSION_ABS_TOL` | 0.01 / 0.005 | router plan T28 |

They live in `router_math.py` as constants with these provenances so the module imports without the YAML
loader; when calibration (T23) moves a number, register the metrics (`router.D_px_median`,
`router.disocclusion_fraction`, `router.orbit_deg`) under a new threshold-set id in `thresholds.yaml` and point
the constants at it. Changing a number never rewrites a shipped verdict.
