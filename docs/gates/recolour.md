# Recolour by element recomposition (`plate_gates.recolour`)

The Recolour node (BUILD_PLAN "math owns photometry") changes one material's albedo to a RAL or swatch colour with
the render's own passes, never with a generator. Pixels outside the material are the beauty bit for bit; inside, the
diffuse term is rescaled and every other term (reflection, refraction, specular, self-illumination) is left where
V-Ray put it. The re-render harness measures the result against a real re-render of the same camera, which is the
P4 exit (`verify-on-box`: one RAL panel recoloured in PLATE versus a V-Ray re-render, dE00 <= 1).

## 1. The model

V-Ray's beauty, in linear light, is the sum of its render elements:

```
beauty = diffuse_filter * raw_lighting + diffuse_filter * raw_gi
       + reflection + refraction + specular + self_illumination
```

`VRayDiffuseFilter` is the diffuse colour **as the renderer applies it**: the nominal albedo `rho` dimmed by the
energy-conserving Fresnel term of the reflective layer, `df = rho * (1 - F)` (a chrome VRayMtl has a black diffuse
filter). `VRayRawLighting` and `VRayRawGlobalIllumination` are pre-albedo, so the diffuse term is
`df * (raw_lighting + raw_gi)`. The Blender fixtures (`fixtures/blender/build_packets.py`) repack Cycles' Diffuse
Color / Diffuse Direct / Diffuse Indirect / Glossy passes to exactly these semantics; on `room_a` the identity holds
to 0.9% inside the panel (the raw layers are quantised to save fixture bytes) and `df / rho` measures a constant
0.952, Blender's `1 - F0` for its specular level of 0.6.

## 2. The three methods

Target albedo `rho'` is linear Rec.709 RGB from the target Lab (D65, through sRGB: `lab_to_linear`). With the
per-channel ratio `r = rho' / rho`:

| method | needs | formula inside the mask | `expected_de00` |
|---|---|---|---|
| `recomposition` | `diffuse_filter`, `raw_lighting`, `raw_gi` | `beauty' = beauty + (df' - df) * (raw_lighting + raw_gi)`, `df' = df * r` | 1.0 |
| `ratio` | at least one of `reflection`, `refraction`, `specular`, `self_illumination` | `beauty' = beauty + D * (r - 1)`, `D = beauty - sum(non-diffuse elements present)` | 2.5 |
| `degraded` | nothing but `beauty` (+ `diffuse_filter` for the source estimate) | `beauty' = beauty * r` (the reflections get tinted too), `degraded: true` | 4.0 |

`apply` picks the best method the elements allow, or `method=` forces one (an error when its elements are missing,
never a silent fallback). The result is clipped at zero (the fixture's quantised raw layers can subtract a hair more
than the diffuse term).

**Why `df' = df * r` and not the bare `rho'`.** The planning note wrote the recomposition as
`beauty - df * (rl + gi) + rho' * (rl + gi)`. Substituting `rho'` drops the renderer's Fresnel dimming and the
material's texture modulation, so the panel comes out `1 / (1 - F)` (about 5%) too bright. Measured on the fixture
(dE00 of the mean Lab against the re-render): bare `rho'` 0.57, `df * r` with the known source 0.18, `df * r` with the
Fresnel-aware estimate 0.07. Scaling is what a V-Ray re-render does.

## 3. The source albedo `rho`

In order:

1. `source_lab` (the swatch the material carries today; the artist picks it in the node),
2. the legend entry's `albedo_lab`, `albedo_srgb` / `base_srgb` (hex) or `ral`,
3. an estimate from the diffuse filter inside the mask: the per-channel median of `df / (1 - F)` over the core
   pixels (coverage >= 0.5, eroded 2 px). `F` is Schlick's Fresnel at `f0` (default 0.04, a plain dielectric) with
   the per-pixel view angle from the camera-space normals and the intrinsics when both are given
   (`fresnel: schlick_per_pixel`), else the constant `1 - f0` (`fresnel: constant`); `f0=None` gives the plain median.

On `room_a` the estimate lands within 1% of the RAL 7016 albedo the scene was built from (0.0392/0.0478/0.0539
against 0.0395/0.0482/0.0545). `source_albedo_origin` records which route was taken. A black source albedo has no
colour to scale: channels below `1e-4` are floored and a note is added.

**To confirm on the box:** that `VRayDiffuseFilter` carries the Fresnel dimming (the V-Ray docs describe the diffuse
being dimmed by the reflection for energy preservation). If V-Ray writes the undimmed colour instead, the estimate
runs `1 / (1 - f0)` high and the recolour `1 - f0` dark (about 4%, 0.6 dE00 at mid grey): then set `f0=None` for
V-Ray packets. The known-source route is exact either way, which is why the node asks for the current swatch.

## 4. Mask, metal flag, expected error

- `mask` is the Cryptomatte coverage (float 0..1, `read_cryptomatte` share of the id) or a bool. Pixels of weight 0
  are copied from the beauty bit for bit (`outside_identical` in the harness); soft edges blend the delta by the
  coverage, an approximation within the filter footprint (the elements themselves are filtered), which the gates
  never score (3 px erosion).
- `metal: true` when the `specular` (or, failing that, `reflection`) energy inside the mask exceeds 30% of the
  beauty (`METAL_SHARE`). Tinted reflections of coloured metals are out of scope: the reflection is kept untinted,
  the flag tells the gate to expect a larger error. The fixture panel measures 0.24 (dark panel, glossy finish).
- `expected_de00` is the table above; the harness marks `passes` when the measured sigma-blurred median is at or
  below it. The thresholds are not in `thresholds.yaml` yet (that file is owned by the gates consolidation); they
  live in `EXPECTED_DE00` until a threshold-set id carries them.

## 5. Display and comparison

`to_display(rgb_linear, manifest)` runs the packet's colour pipeline (`plate_pixels.ocio.Pipeline.from_manifest`,
T3: OCIO view or the legacy sRGB OETF) so the recolour is compared with the TIFF16 master the way the artist sees
it. `compare_display(out8, ref8, mask, sigma=6, erode_px=3)` reports, on the eroded mask:

| key | meaning |
|---|---|
| `de00_median`, `de00_p90` | CIEDE2000 on the sigma-blurred display images, blur normalised inside the mask (the `material_de` convention: judge colour, not texture or noise) |
| `de00_median_px`, `de00_p90_px` | the raw per-pixel numbers |
| `de00_of_means` | dE00 between the two mean Lab values |
| `lab_mean_out`, `lab_mean_ref`, `n_px` | |

Two 32-spp Cycles renders carry independent Monte-Carlo noise on the recoloured material (the albedo changes the
lobe sampling), so the raw per-pixel median on the fixture is 1.2 while the colour itself is right to 0.1: the gate
scores the blurred number and reports both.

## 6. The re-render harness

```
validate_against_rerender(packet_dir, rerender_dir, select_id, target_lab, *,
                          source_lab=None, methods=("recomposition", "ratio", "degraded"),
                          sigma=6.0, erode_px=3, f0=0.04) -> report
```

Loads both packets, refuses a re-render that is not the same view (resolution, `world_from_camera`, focal length:
the harness validates a recolour, it never invents an angle), selects the material by `o:` / `m:` id, recolours with
each method and scores it against the re-render's display master. `before` is the untouched source against the
re-render (the size of the move). The report is JSON-safe (no pixels).

Fixture result on this Mac, `room_a` (panel RAL 7016, `o:735a8655`) to RAL 3020 against `room_a_ral3020`, source
estimated (`gates/tests/test_recolour.py`):

| method | de00_median (blur) | de00_p90 | de00_median_px | de00_of_means | passes |
|---|---|---|---|---|---|
| before | 29.03 | 30.43 | 29.27 | 29.00 | |
| recomposition | **0.11** | 0.21 | 1.21 | 0.07 | yes |
| recomposition, known source | 0.18 | 0.30 | 1.24 | 0.18 | yes |
| ratio (`beauty - reflection`) | 0.10 | 0.26 | 1.20 | 0.04 | yes |
| degraded (whole beauty scaled) | 5.89 | 7.47 | 5.51 | 5.53 | **no** |

The degraded path fails its 4.0 expectation on this data and that is the right verdict: the 7016 panel is dark (L*
26) with 24% of its energy in reflections, and the 7016 to 3020 ratio is 15x in red and 0.03x in green and blue, so
scaling the whole pixel paints the reflections red. The plan expected <= 4 for this path; the fixture measures 5.9
and the test pins `4 < median < 7.5` with `passes == False`. A degraded recolour of a dark glossy material to a
saturated colour is not trustworthy, and the gate says so; ask for the raw elements.

Reverse direction (`room_a_ral3020` to RAL 7016 against `room_a`): recomposition 1.01, ratio 0.28. The fixture
truncates the raw lighting layers' low mantissa bits (a bias of about 1.5%, `build_packets.QUANT`) and a diffuse
term 15x brighter than the target amplifies it into a pink cast; V-Ray's full-float raw passes do not have this.
The test holds this direction to 1.5 and documents why.

## 7. RAL table

`RAL_SRGB` holds published sRGB approximations of fifteen common archviz codes (7016 `#383E42` and 3020 `#CC0605`
are the fixture panels, `fixtures/blender/scene.py`). The official RAL Lab values are licensed: a job enters the
swatch's measured Lab (`target_lab`, `source_lab`) or the hex the material was built from (`hex_lab`). `ral_lab`
raises `KeyError` for a code outside the table rather than guessing.

## 8. Doctrine

No generator, no text-to-image, no upscaling, no new angle: the recolour is arithmetic on the packet's own
elements, the comparison is against a re-render of the same camera, and the result is only as good as the
elements the packet carries, which the method name and `expected_de00` say out loud.
