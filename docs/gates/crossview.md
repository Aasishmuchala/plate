# Cross-view correspondences and the Set-consistency gate (`plate_gates.crossview`, `plate_gates.setgate`)

The measurement behind BUILD_PLAN P4's Set node: N cameras of one set must show one look. A Lambertian surface
point has one colour whatever the camera, so colour is compared **at matched world points**, never between
whole materials: each camera sees a different part of a wall under different shading, and on the fixture room the
whole-mask Lab means of identical materials differ by 2-10 dE00 on an identical render. Exit: cross-view
per-material dE00 **p90 <= 3, hard**.

Two modules, pure functions over packet arrays and display images, no engine:

| | |
|---|---|
| `crossview.PacketView.load(dir)` | camera, planar Z, camera-space normals, material Cryptomatte, legend, optional `world_position`, `policy.editable_ids` |
| `crossview.correspondences(a, b, world_pos_a=None, world_pos_b=None, n=2000, seed=0)` | up to `n` matched world points A -> B |
| `crossview.delta_e(corr, rgb_a, rgb_b, window=5, view_a=, view_b=)` | dE00 at the correspondences, per material |
| `setgate.run(hero, hero_rgb, [(packet, rgb), ...])` | targets, per-view scores, pooled per-material p90, verdict |
| `setgate.numeric_correction(rgb, crypto_material, targets, offsets=None)` | per-id Lab mean shift, clipped, chroma-preserving |
| `setgate.gate_report(result)` | the versioned `GateReport` envelope |

## 1. Correspondences

Pixels of A are sampled (seeded, uniform over the candidate pool) on materials both packets carry. For each:

1. **World point.** From the opt-in `world_position` pass when A has it (`source: 'world_position'`), else from
   A's planar Z back-projected through the packet camera (`source: 'z_backproject'`; Tier A always has Z).
   `backproject` is the exact numerical inverse of `plate_spec.project_point` (it inverts R^T rather than
   assuming the rounded packet matrix is orthonormal), so a round trip is exact to float precision.
2. **Projection into B** with the normative `plate_spec.project_point`; kept when it lands in frame.
3. **Visible.** B's Z at the landing pixel agrees with the projected depth within `z_tol` (1 %); B's dominant
   material id (coverage >= 0.5) is the same material. When B carries `world_position` too, its value at the
   landing pixel must agree with the world point within 1 % of depth.
4. **Lambertian candidate.** Class not glazing / water (`exclude_classes`); normal . view >= 0.3 in both views
   (`min_n_dot_v`), with the packet's camera-space normals (+Z toward the camera) and the unit vector from the
   surface point to the camera.
5. **Same surface patch.** The two ends' world normals agree within `max_normal_deg` (20 deg), and the
   `window` x `window` patch of that material around each end is one surface: at least `min_patch_fill` (60 %)
   of the window is the material, its depth stays within 1 % of the centre depth and its normals within 20 deg
   of the centre normal. This is what makes the patch `delta_e` averages over a single surface: silhouettes,
   leaf clusters, marker spheres and other structure smaller than the window are not correspondences, and
   neither is a grazing strip (5 px at 72 deg spans more than 1 % of depth).

Record: `{id_m, p_world, uv_a, uv_b, n_dot_v_a, n_dot_v_b, source}`; `uv_a` is the sampled pixel's centre,
`uv_b` the exact projected position (render-frame coordinates, origin top-left corner, centres at +0.5).
Same inputs and seed give the same list. Materials with pixels in only one view yield nothing; no shared
material yields `[]`.

## 2. dE00 at the correspondences

Both views go to 8-bit gamma RGB (the fidelity_bench convention), are blurred with sigma = 2 and converted to
Lab; each end of a correspondence contributes the mean Lab over its `window` x `window` (5 x 5) patch and the
pair is scored with CIEDE2000. With `view_a` / `view_b` passed the blur and the patch mean are **isolated per
material** (normalised convolution, as `material_de` does), so an edited neighbour never bleeds across a
border into the number; the global-blur variant exists for comparison only.

Per material: `de00_median`, `de00_p90`, `de00_mean`, `n`, and `dlab_mean` = mean(Lab_a - Lab_b), the additive
offset that moves B's material onto A's at the matched points (with `return_values=True` also the per-record
`de00`). Plus `overall_p90`, `overall_median`, `n`, `isolated`, `source`.

## 3. The Set gate

`setgate.run(hero_packet, hero_rgb, others)`:

- `targets`: every material's mean Lab on the hero over the eroded Cryptomatte mask (`material_de`
  convention): `{m_id: {lab, n_px, name, class, editable}}`. This is the Set editor's targets table.
- `per_view[i]`: `n_corr`, `source`, `status` (`ok` | `insufficient` when `n_corr < min_corr` = 50), `p90`,
  `median`, `per_material` (the `delta_e` rows plus `n_dot_v_median`, the view's own whole-mask `lab_mean` and
  `target_de` against the hero target, both informational) and `offsets` = `{m_id: [dL, da, db]}`, the
  correspondence-measured shift to apply to that view.
- `per_material`: pooled across views: `de00_p90`, `de00_median`, `n`, `n_dot_v_median`, `scored`
  (`n >= min_n` = 30: a p90 over a handful of samples is not a statistic), `hard`.
- `cross_view_p90`: the worst scored **hard** material's p90. Hard materials are those not in the hero packet's
  `policy.editable_ids` (override with `hard_ids`). Editable classes (vegetation, people, sky, water) are the
  ones AI regenerates per camera, so their cross-view drift is scored and reported but only ever advisory.
- `verdict`: `fail` when `cross_view_p90 > 3` (threshold `set.cross_view_de00_p90`, `le 3`, hard; read from
  `thresholds.yaml` when that entry exists, else the module constant; `p90_max` overrides), `advisory` when a
  view is insufficient, when only editable materials miss, or when nothing could be scored, else `pass`.
  `checks` follows `report.Check`; `gate_report(result)` wraps it in the versioned `GateReport` (offsets are
  dropped from the envelope).

## 4. Numeric correction

`numeric_correction(rgb, crypto_material, targets, *, offsets=None)` applies a per-id additive Lab shift:

- `crypto_material`: `(ranks, coverage)` as `read_cryptomatte` returns them, a `PacketView`, or a 2-D id image.
- For an id in `offsets` the shift is that offset (`per_view[i].offsets`, measured at correspondences). For an
  id only in `targets` it is `target - the view's own mean over the eroded id mask` (the fallback for a view
  that shares no surface with the hero; weaker, see the opening paragraph).
- Clipped to +-8 L and +-6 a/b, so a bad target can never restyle a material.
- Weighted by the id's Cryptomatte coverage, so anti-aliased edges move proportionally; pixels of ids in
  neither map are **bit-identical** to the input (outside-mask dE00 = 0 by construction, plan decision 4).
- Additive in Lab: every pixel keeps its own lightness and chroma variation (texture, shading); only the
  material's mean moves. Input dtype (uint8 / uint16 / float 0..1) is kept.

Pass `targets` restricted to the ids you mean to move and `offsets` for the ids that have correspondences.

## 5. Measured on the fixture (room_a hero, room_b other; one Cycles render, 640 x 360)

| material | n (of 2000 samples) | p90 | note |
|---|---|---|---|
| ARCH_wall_N (matte, rough 0.8 / spec 0.2) | ~550 | 1.0 | median 0.5, `dlab_mean` < 0.05: the noise floor of a 32-spp render |
| ARCH_panel_3020 (semi-gloss, rough 0.38 / spec 0.6) | ~370 | 2.1 | a moving glossy highlight is view-dependent |
| PEOPLE_dummy (capsule) | ~70 | 1.0-1.3 | editable: advisory |
| VEG_leaf (ico-sphere cluster) | 20-28 | 3-5 | spheres a few pixels wide at 640 px; editable: advisory |
| ARCH_panel_7016 (dark semi-gloss, seen at n . v = 0.31 in CAM_b) | 0 | - | grazing patches fail the patch check; at 0.3 < n . v < 0.5 Fresnel made it 6-7 dE00 brighter and bluer |
| GLZ_pane, WATER_pool | 0 | - | excluded by class |

About 1000 correspondences per pair; `overall_p90` 1.8-1.9. A +6 L* step on the wall in CAM_b measures p90
6.3 (median 5.4, analytic dE00 of the step 5.6); a +6 b* step measures 4.7 (analytic 4.3: CIEDE2000
compresses chroma steps on a low-chroma material). The correspondence offsets recover the step within 0.2 and
`numeric_correction` returns the wall to its noise floor (median <= 1, p90 ~1.0).

## 6. Calibration notes

- `min_n_dot_v = 0.3` is the plan's number and the sampling cut. Fresnel makes any semi-gloss surface
  view-dependent well before 0.3 (Schlick: the specular term at cos 0.3 is about 4 x F0, at 0.5 about 1.6 x);
  the patch check removes most grazing windows in practice, and `n_dot_v_median` per material says how
  frontally it was seen. T23's labelling should decide whether the Set gate samples at 0.5.
- At delivery resolution a modelled leaf is many windows wide and vegetation measures like any surface; the
  fixture's leaf proxies are under-resolved at 640 px, which is why their row is 3-5 and why editable classes
  are advisory rather than a resolution-dependent hard check.
- `thresholds.yaml` does not yet carry `set.cross_view_de00_p90`; `setgate` reads it when present (same id,
  `le 3`, hard) and otherwise uses its constant with the committed set id.

## 7. Used by

T48 `nodes/set_node.py`: `run()` on the hero and the other cameras' outputs, `per_view[i].offsets` into
`numeric_correction` per view, `gate_report(result)` written as the run's gate JSON. T47's Set editor renders
`targets` and `per_material` / `per_view` rows and the `cross_view_p90` chip.
