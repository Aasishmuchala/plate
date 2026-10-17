# plugins/max/maxscript: the client packet kit

`plate_client_kit.ms` is the free MAXScript that turns a client's finished render into a tier A Shot
Packet input (BUILD_PLAN decision 1, `spec/PACKET.md` section 11). It runs in the **client's** 3ds Max with
V-Ray, exports one camera, and leaves three files in one folder. PLATE does the rest on its side
(`plate_max.kit_ingest.kit_to_packet`).

It is ASCII with CRLF line endings, under 400 lines, local-files-only: no network, no scripting bridge, no
shell, no callbacks, no `execute`. It changes nothing in the scene that it does not restore (see "What it
touches").

## Running it (client side)

1. Open the scene, set V-Ray as the renderer and the render resolution to the size of the plate.
2. MAXScript menu, Run Script, pick `plate_client_kit.ms`. The dialog "PLATE Packet Kit" opens.
3. Pick the camera and an output folder, press Export. The dialog shows what happened; the 3ds Max
   Listener (F11) shows every warning.
4. Send the folder and the plate (the approved image, a 16-bit TIFF if there is one) to Sthyra.

The kit stops with a message box when V-Ray is not the current renderer, and before rendering when the
camera has lens shift, tilt or distortion (packet v0.1 is a pinhole model, `spec/PACKET.md` 3.3).

## What it writes

For a camera named `CAM 1` the stem is `CAM_1` (every character outside `A-Za-z0-9_.-` becomes `_`;
`plate_max.kit_ingest.kit_stem` does the same replacement).

| file | content |
|---|---|
| `<stem>_packet.exr` | V-Ray raw image, multilayer EXR: `RGBA`, `VRayZDepth.Z`, `VRayNormals.X/Y/Z`, `CryptoObject00..` and `CryptoMaterial00..` (6 levels), `VRayWireColor.R/G/B`, `VRayDiffuseFilter.R/G/B` |
| `<stem>_camera.json` | camera and render facts, below |
| `<stem>_legend.txt` | one `node|layer|material|r,g,b` line per renderable node on a visible layer; fields percent-encoded (`%` as `%25`, `|` as `%7C`, line breaks as `%0D` `%0A`) |

`<stem>_camera.json` keys: `kit`, `kit_version`, `camera`, `camera_class`, `transform` (the node's
`objecttransform`, Max `Matrix3` rows `row1..row4`, 4 x 3), `fov`, `focal_length_mm`, `film_width` (mm; 36
when the camera has none, with `focal_length_mm` then derived from `fov`), `zoom_factor`, `render_width`,
`render_height`, `pixel_aspect`, `frame`, `fps`, `units` (`system_type` from `units.SystemType`,
`system_scale` from `units.SystemScale`), `renderer` (the renderer's class name), `max_version`
(`maxVersion()`), `colour_mode` (`ocio`, `legacy_gamma` or `unknown`) with `colour_mode_raw` and `gamma`,
`f_number`, `focus_distance`, `dof_enabled`, `iso`, `shutter_speed`, `lens_shift`, `tilt`, `distortion`,
`render_time_s`, `exr` and `legend` (the two file names), `elements` (the EXR layer name of each manifest
element) and `warnings`. `tests/kit_fixture/CAM_a_camera.json` is a worked example, and
`test_client_kit_static.py` fails if the script and the fixture stop agreeing on the keys.

## What it touches

The user's own render elements are disabled for the render and re-enabled afterwards, the kit's six
elements are removed again, "elements active" and the V-Ray raw-image settings (`output_saveRawImage`,
`output_rawFileName`, `output_splitgbuffer`) are put back. The scene is not saved and no scene script or
callback is installed.

## The daemon half: `plate_max.kit_ingest.kit_to_packet`

```python
kit_to_packet(folder, "CAM 1", plate_path, *, hasher, pixel_shares, plate_to_tiff16=None,
              project_id="client-kit", shot_id=None, out_dir=None, colour=None, ...) -> packet_dir
```

`plate_max` is standard-library-only (Max's Python), so the work that needs BLAKE3, EXR pixels or an image
library is injected by the caller, the daemon route in router plan T38:

- `hasher(Path | bytes) -> blake3 hex` (the daemon's `hash_contained_file` result is accepted too),
- `pixel_shares` as `{"o:<hex>": share}` or `(exr_path, elements) -> that`; `elements` feeds
  `plate_pixels.exr.read_cryptomatte`,
- `plate_to_tiff16(src, dst)` for any plate that is not already a 16-bit RGB(A) TIFF.

Everything else is done here: the EXR header is read with the standard library (channels, half or float,
compression, the Cryptomatte manifest), so a missing layer, a half-float Cryptomatte or a lossy EXR is
refused with the reason; the camera block comes from `plate_max.packet.build_camera_block`, which the tests
check against `plate_spec`; ids the render covers but the legend does not name are named from the EXR's
Cryptomatte manifest and stay unlabelled and locked; unlabelled objects get an autotag class but never
become editable; the plate is copied (or promoted) to `display.tif` and is never resampled.

Anything the packet cannot say is recorded in `kit_flags.json` (file role `x_kit_flags`):

| flag | meaning |
|---|---|
| `client_plate_8bit` | the plate is 8-bit. The schema fixes `display_tiff16.bit_depth` at 16, so it was promoted exactly (v * 257); nothing was invented, but the master has 8-bit precision |
| `client_plate_jpeg` | the plate is a JPEG: lossy as well |
| `depth_kind_assumed` | `VRayZDepth` is recorded as planar; V-Ray does not say, and the on-box measurement decides |
| `f_number_nominal`, `focus_distance_nominal` | a camera without those values and with DOF off gets f/8 and 1 unit; gates ignore both while `dof_enabled` is false |
| `vfb_corrections_unknown` | whether VFB corrections are baked into the plate is not knowable from the kit |
| `legend_nodes_not_in_exr` | kit legend nodes the Cryptomatte manifest does not list (not visible, or a different ID type) |
| `exr_manifest_hash_differs` | the EXR's manifest ids differ from the Cryptomatte hash of the names |
| `material_layer_names_unmatched` | the material layer names none of the legend's materials (multi-sub materials hash their sub-materials) |
| `cryptomatte_manifest_missing` | the EXR header carries no Cryptomatte manifest to cross-check |

## Unverified on a Max box (verify-on-box)

Nothing here could run on the Mac. The V-Ray property names are the best known, tried in order through
`isProperty` and labelled "(unverified)" in the script's warning text; every miss is a line under
`warnings` plus the element's property list in the Listener, so the right name can be read off in one run:

- `VRayZDepth`: `zdepth_clamp`, `filterOn` (must end unclamped and unfiltered),
- `VRayCryptomatte`: `levels` (6) and the ID type property (`id_type`...), with the enumeration values 0
  (node name) and 1 (material name) guessed; `kit_to_packet` refuses a file whose object layer does not
  hash node names,
- V-Ray raw image: `output_saveRawImage`, `output_rawFileName`, `output_splitgbuffer` writing one
  multilayer EXR with every element, with lossless compression and 32-bit Cryptomatte and Z,
- `createFile ... encoding:#utf8` (a fallback without the keyword is in the script),
- `IDisplayGamma.colorCorrectionMode` / `.gamma` for `colour_mode`; the OCIO block cannot be filled, so an
  OCIO scene needs `colour=` on the ingest call,
- lens shift, tilt and distortion property names (shared with `plate_max.digest`),
- `fov` read as the horizontal FOV, and `maxVersion()[1]` as 28000 for Max 2026.

Run the kit on a Sthyra-owned scene in Max 2026, check the EXR with `plate_pixels.exr` on the box (the
seven layers, Cryptomatte floats, Z unclamped), then `kit_to_packet` it and run `validate_packet_dir`.
