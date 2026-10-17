# Colour: how PLATE keeps what you signed off

This file is a skeleton. The rules below are settled (BUILD_PLAN decision 2, `spec/PACKET.md` section 6). The table at the end is the list of colour questions that only the render box can answer; each row is empty until someone measures it on a real Max 2026 and V-Ray 7 scene, and the answer goes in the last column.

## The rules

1. **What you signed off in the VFB is what ships.** For a Tier A packet the master is `display.tif`, the VFB-corrected 16-bit TIFF. The linear EXR is data (beauty for relighting math, Z, normals, Cryptomatte) and never overrides the TIFF. If an OCIO re-derivation of the EXR does not match the TIFF, the TIFF still wins and the EXR stays data only.
2. **The Max colour mode is read at Send time and an unknown mode blocks Send.** `max_colour_mode` is `ocio` (the packet then names the config, a BLAKE3 of the config, display, view and optional look) or `legacy_gamma` (the packet then carries the gamma). A Tier C client plate is `not_applicable`: its master is the client's plate. Nothing guesses.
3. **The working space** of the EXR beauty is `lin_rec709` or `acescg`, stated in the packet.
4. **Maths owns photometry.** Exposure, white balance, tone, RAL recolour and LightMix are numeric nodes. No generator is ever asked to fix colour, and the plate is never upscaled.
5. **Egress is 8-bit sRGB PNG with an ICC profile, never JPEG.** Every other chunk that could carry a name is stripped before a PNG leaves the box (`pixels/plate_pixels/icc.py`).
6. **Compositing and the gates work in sRGB-encoded 16-bit.** Colour differences are CIEDE2000 in Lab with a D65 white (`gates/plate_gates/color.py`); the viewer decodes the sRGB transfer curve before it blends or differences and encodes last (`ui/src/viewer/README.md`). Display values that come from an EXR go through the same OCIO pipeline the packet names (`pixels/plate_pixels/ocio.py`; `PLATE_OCIO_CONFIG` names a config file when the built-in one has no matching view).

## Where it lives in the code

| what | where |
|---|---|
| detect the Max colour mode, refuse an unknown one | `plugins/max/plate_max/bridge/colour.py` |
| the packet's `colour` block and its rules | `spec/PACKET.md` section 6, `spec/plate_spec` |
| OCIO pipeline, re-derive display from the EXR, ΔE00 p99 | `pixels/plate_pixels/ocio.py` |
| ICC profile and PNG metadata stripping | `pixels/plate_pixels/icc.py` |
| ΔE00, Lab, Reinhard tone match | `gates/plate_gates/color.py`, `labmatch.py` |
| recolour by recomposition, per-material ΔE | `gates/plate_gates/recolour.py`, `material_de.py` |

## On-box table (to be measured; from `spec/PACKET.md` section 13)

Run `plugins/max/ONBOX.md` on one real camera and write each answer here with the Max, V-Ray and Vantage versions and the date. A row that is still empty is an open question, not a default. Mac and CI runs use the Blender-rendered fixtures, which exercise the same file layout but say nothing about V-Ray's behaviour.

| question | field that records the answer | measured answer (box) | versions and date |
|---|---|---|---|
| Max 2026 colour mode (spike S/#19) | `colour.max_colour_mode` | not measured | |
| Is VRayZDepth planar or radial? What do empty pixels hold? | `elements[zdepth].depth_kind`, `background_value` | not measured | |
| Are VRayNormals camera-space and signed? | `elements[normals].space`, `encoding` (the schema requires camera and signed; if V-Ray differs the plugin converts) | not measured | |
| V-Ray lens shift and vertical tilt as an effective image-plane rotation and principal point | refused in packet 0.1 (`lens_shift` must be zero, tilt off); added in a minor version once measured | not measured | |
| Which Max transform the V-Ray camera renders from (expected `objecttransform`) | `camera.world_from_camera`, checked by the vertex reprojection exit | not measured | |
| Does an OCIO re-derivation match the VFB TIFF? (target: ΔE00 p99 at most 0.5) | daemon ingest report, not the packet | not measured | |
| Element overhead on the heavy scene (caps 15% time, 10% RSS) | `render.element_overhead` | not measured | |
| Does Vantage write any elements? | `renderer.name: vantage` packets are allowed but must carry the default set | not measured | |

## Recording an answer

1. Write it in the last two columns above.
2. Change the constant or the manifest field it decides (named in `plugins/max/ONBOX.md`), delete the unverified marker next to it (`grep -rn "\-verify" plugins/max/plate_max` finds them), and rerun `scripts\test.ps1`.
3. If the answer changes the packet contract, bump the packet version as `spec/PACKET.md` section 14 says; an old packet is never rewritten.
