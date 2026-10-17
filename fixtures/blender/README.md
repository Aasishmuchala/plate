# fixtures/blender

The fixture renderer. A Blender 5.1 scene (`scene.py`, runs inside Blender with `bpy`) renders the conformance room
with Cycles; `build_packets.py` (runs in the project venv) repacks the raw renders into Shot Packets whose
`beauty.exr` has exactly the layer layout the 3ds Max plugin writes, so every gate on the Mac reads the same file
shape as on the render box. This is a fixture renderer, not a sender (BUILD_PLAN decision 10).

```
bash fixtures/blender/render.sh [--force] [--skip-render]
```

`render.sh` is a one-line POSIX wrapper over `render.py` (pure pathlib and argument lists, so it also runs as
`python fixtures\blender\render.py` on Windows). `render.py` finds Blender through `$BLENDER`, the usual macOS and
Windows install folders, then `PATH`; runs `Blender -b --factory-startup -P scene.py -- <work> --variant <v>` per
variant; then runs `build_packets.py <work> fixtures/packets`. A stamp (sha256 of `scene.py` + `markers.json`) in
`_work/<variant>/stamp.txt` skips renders that are up to date. `_work/` (about 140 MB) is gitignored.

## The scene

A 5 x 4 x 3 m room (x -2.5..2.5, y -2..2, z 0..3, metres, Z up). Object names carry the layer prefix, and
`legend.layer = name.split('_')[0] + '_'`.

| object | notes |
|---|---|
| `ARCH_marker_01..08` | 2 cm spheres at the coordinates in `markers.json` (the 3ds Max conformance scene is built from the same file) |
| `ARCH_wall_N/S/E/W`, `HARD_floor` | planar; the north wall has a window opening holding `GLZ_pane` (material `GLZ_pane`) |
| `SKY_dome` | emissive backdrop seen through the window |
| `SIGN_plate01` | lettered text object "PLATE 01", material `SIGN_a` |
| `ARCH_panel_7016`, `ARCH_panel_3020` | RAL 7016 #383E42 and RAL 3020 #CC0605 panels |
| `VEG_shrub_A` | ico-sphere cluster foliage proxy |
| `PEOPLE_dummy` | capsule |
| `WATER_pool` | reflective plane |

Cameras (sensor 36 mm, `sensor_fit` HORIZONTAL): `CAM_a` 35 mm, 640 x 360 render with a 640 x 268 safe frame
(2.39:1 inside 16:9), depth of field on (f/2.8, focus 2.5 m) so circle-of-confusion weighting has something to
weigh; `CAM_b` 24 mm, 640 x 360, full frame; `CAM_dolly` 28 mm, 12 frames travelling 0.6 m along its view axis.

Shots (`scene.SHOTS`): `base` renders `room_a`, `room_b`, `dolly_hero` (640 x 360 still at the dolly's frame 0)
and `dolly_12f` (320 x 180, clay material override); `ral3020` renders `room_a_ral3020` (the 7016 panel's albedo
set to RAL 3020, nothing else changed; T31's recolour validation); `v2` renders `room_a_v2` (the east wall
recoloured: a client revision for the `supersedes` and carry-over tests).

## Render settings (verified on Blender 5.1.1, Mac CPU)

Cycles, 32 samples, fixed seed, no adaptive sampling, pixel filter 1.5 px, Standard view transform. Passes: Combined,
Z, Normal, Diffuse Color/Direct/Indirect, Glossy Color/Direct/Indirect, Cryptomatte object and material at depth 6.
Multilayer EXR is `media_type='MULTI_LAYER_IMAGE'` + `file_format='OPEN_EXR_MULTILAYER'`, 32-bit, ZIP (Blender 5.1
renamed these controls). Per shot the scene writes `ml.exr`, `display.tif` (16-bit, same render), `zu.exr` (a
1-sample, 0.01 px filter, no-DOF Depth render, because Cycles filters its Depth pass and V-Ray's `VRayZDepth` is
unfiltered) and `info.json`.

## What the repack does

`beauty.exr` is one part with these layers:

```
R G B A (half)                           Combined (filtered, not denoised)
VRayZDepth.Z (float)                     planar, unfiltered, background 1e10
VRayNormals.X/Y/Z (half)                 CAMERA space: R^T n_world, renormalised (Blender writes world space)
CryptoObject00..02.R/G/B/A (float)       6 ranks; header attributes rewritten for the new layer name
CryptoMaterial00..02.R/G/B/A (float)
VRayWireColor.R/G/B (half)               per-object colour from the rank-0 id
VRayDiffuseFilter.R/G/B (half)           Diffuse Color
VRayRawLighting / VRayRawGlobalIllumination / VRayReflection   room_a and room_a_ral3020 only (opt-in elements)
```

Cycles' direct and indirect diffuse passes are pre-albedo lighting, which is V-Ray's raw-lighting semantics, so
`beauty ~= diffuse_filter x (raw_lighting + raw_gi) + reflection` holds on opaque non-emissive pixels (median error
about 1% on the panels after quantisation; `test_recomposition_identity_holds_on_the_fixture`).

The manifest is `host.app none`, `renderer {vray, fixture-cycles-5.1}`, `render.session fixture`, legacy gamma 2.2,
`working_space lin_rec709`, `master display_tiff16`; `world_from_camera` is Blender's `matrix_world` (already
plate-cam-v0) and `fx = lens x width / sensor_width`. Packet ids, `created_utc` and hashes are deterministic.
`id_legend.json` comes from the Cryptomatte manifest plus the class rules (`class_source: rule`), `planar` for
walls, floor and panels, `pixel_share` from rank-0 coverage. The motion packet adds `camera_frames.json`,
`motion/clay.%04d.png` (uint16), `motion/id.%04d.exr`, `motion/z.%04d.exr` and their `.blake3` files, with
`hero_still_packet_id` pointing at `dolly_hero`.

### Size budget and quantisation

A 640 x 360 Cycles render is about 6 MB of float data, mostly noise. To keep packets near 3 MB the repack drops low
mantissa bits (lossy but deterministic): half layers lose 3 to 6 bits (at most 1.6% relative on the beauty, 3% on the
raw layers), Z is rounded to a 0.5 mm grid, the display TIFF and clay PNGs drop 4 and 6 low bits. Cryptomatte ids and
coverage are never touched. The clay frames are OIDN-denoised (they are a conditioning image, not a data pass).

### Blender quirk found while building these

Blender writes the raw MurmurHash3 into the Cryptomatte manifest but the denormal-safe value (bit 23 flipped when
the float exponent is 0 or 255) into the pixels. `ARCH_marker_04` hits this: the manifest said `805a502f`, the pixels
hold `80da502f`. The repack rewrites every manifest entry with the spec hash (`plate_spec.cryptomatte_id`), so the
manifest, legend and pixels agree. `test_every_visible_marker_is_in_the_legend` pins it.

## Not provable here

Anything about V-Ray: that the plugin's real EXR has these layers, colour-mode handling, element overhead on a heavy
scene, Vantage output. Those are box-day checks.
