# plate_max on the box: the checklist that turns TODO-verify into facts

Everything in `plate_max/digest.py` and `plate_max/bridge/*.py` that names a V-Ray, Vantage or Max
property, enum value or command is a hypothesis until this list has run on the render box (3ds Max
2026, V-Ray 7, Vantage 3.3; BUILD_PLAN section 9, days 2 to 3). Each name is marked `TODO-verify`
where it is defined. Work through the steps in order on one real TULA (or Whispers of Trees) camera,
record every answer in `docs/onbox-spikes.md` (colour answers in `docs/colour.md`), then edit the
constant, delete its `TODO-verify` marker, and rerun `scripts\test.ps1`.

Setup: `scripts\bootstrap.ps1`, `plugins\max\scripts\install.ps1 -RepoPath <clone>`, start the
daemon (`scripts\start-daemon.ps1`), open the scene in Max, select the camera. Run the snippets in
the MAXScript Listener in Python mode (or save them as a `.py` and `python.ExecuteFile`). Work on a
copy of the scene: `plate_max.bridge.safety.backup()` first.

## 1. Property names (every candidate tuple)

Dump what the live objects really have, then compare with the tuples:

```python
from pymxs import runtime as rt
from plate_max import digest
from plate_max.bridge import colour, elements, render


def names(obj):
    return sorted(str(n) for n in rt.getPropNames(obj))


cam = rt.selection[0]
print("camera", rt.classOf(cam), names(cam))
print("renderer", rt.classOf(rt.renderers.current), names(rt.renderers.current))
for cls in (
    "VRayZDepth",
    "VRayNormals",
    "VRayCryptomatte",
    "VRayWireColor",
    "VRayDiffuseFilter",
    "VRaySamplerInfo",
    "VRayLightSelect",
    "VRayVelocity",
    "VRayRawLighting",
    "VRayRawGlobalIllumination",
    "VRayReflection",
    "VRayRefraction",
    "VRaySpecular",
    "VRaySelfIllumination",
):
    make = getattr(rt, cls, None)
    print(cls, "MISSING" if make is None else names(make()))
print("ColorPipelineMgr", rt.showInterface(rt.ColorPipelineMgr))
print("memory", list(rt.sysInfo.getMAXMemoryInfo()))
print(digest.camera_digest(cam.name)["read_from"], digest.camera_digest(cam.name)["warnings"])
```

Check each list against the code and fix the constants:

| what | module constant(s) | question |
|---|---|---|
| camera lens | `digest.FOCAL_NAMES`, `FOV_NAMES`, `SPECIFY_FOV_NAMES`, `FILM_NAMES`, `ZOOM_NAMES` | which exist; does `specify_fov` on make V-Ray ignore `focal_length`; how does `zoom_factor` combine with it |
| focus and DOF | `F_NUMBER_NAMES`, `FOCUS_NAMES`, `SPECIFY_FOCUS_NAMES`, `TARGET_DISTANCE_NAMES`, `DOF_NAMES` | with `specify_focus` off, is the focus the target distance |
| exposure | `EXPOSURE_ON_NAMES`, `EV_NAMES`, `ISO_NAMES`, `SHUTTER_SPEED_NAMES`, `CAMERA_TYPE_NAMES`, `SHUTTER_ANGLE_NAMES` | the `exposure` enum (none / physical / EV), whether EV is EV100, `shutter_speed` is 1/s, type 1 = movie |
| white balance | `WHITE_BALANCE_NAMES` | is the Kelvin value `temperature`, and is `white_balance` a colour swatch |
| shift, tilt, distortion | `SHIFT_H_NAMES`, `SHIFT_V_NAMES`, `TILT_NAMES`, `TILT_AUTO_NAMES`, `DISTORTION_NAMES`, `DISTORTION_TYPE_NAMES` | which axis `lens_shift` is; the auto-tilt name |
| element common | `elements.ELEMENT_NAME`, `ENABLED`, `FILTER`, `DENOISE` | per-element filtering and denoise switch names |
| Z depth | `ZDEPTH_CLAMP`, `ZDEPTH_MIN`, `ZDEPTH_MAX`, `ZDEPTH_CLIP`, `ZDEPTH_UNCLAMPED_MAX` | names; can `zdepth_max` take 1e9 |
| Cryptomatte | `CRYPTO_ID_TYPE`, `CRYPTO_ID_OBJECT` (0), `CRYPTO_ID_MATERIAL` (2), `CRYPTO_LEVELS` | **the idType enum order**: if material is 1, change `CRYPTO_ID_MATERIAL` (the legend binds `material_name`) |
| world position, light select | `SAMPLER_TYPE`, `SAMPLER_COORDS`, `LIGHT_SELECT_LIGHTS`, `LIGHT_SELECT_MODE` | point + world enum values; the light-select mode that sums to the beauty (LightMix needs "full") |
| raw EXR output | `render.RAW_SAVE`, `RAW_FILE`, `RAW_PREVIEW`, `VFB_ON`, `SPLIT_SAVE`, `EXR_BITS`, `EXR_HALF_BEAUTY`, `EXR_COMPRESSION`, `EXR_COMPRESSION_ZIP` | names, the bit-depth switch, the compression enum value for ZIP |
| VFB save | `render.SAVE_IMAGE`, `SET_SAVE_FORMAT`, `TIFF16_FORMAT` | does `vfbControl #setsaveformat` exist; its arguments for 16-bit TIFF |
| memory | `render.MEMORY_INFO`, `PEAK_WORKING_SET_INDEX` | which index of `getMAXMemoryInfo()` is PeakWorkingSetSize (bytes) |
| colour | `colour.MANAGER`, `MODE_NAMES`, `CONFIG_PATH_NAMES`, `DISPLAY_NAMES`, `VIEW_NAMES`, `LOOK_NAMES`, `RENDERING_SPACE_NAMES`, gamma names | the mode values (`#OCIO_Default`? `#Gamma`? `#Unmanaged`? an integer?) and how `colour._mode_of` must map them |
| scene digest | `digest.LIGHT_PARAM_NAMES`, `_mesh_fingerprint` | `getPolygonCount` returns `#(faces, verts)`; `node.min/max` on V-Ray proxies and Forest Pack; time `digest.scene_digest()` on the heavy scene (target under 10 s; else move it to one `rt.execute` MAXScript pass) |
| time | `bridge.scene.at_time` | `camera_digest(name, frame=12)` equals the digest read with the slider on 12 |
| undo, Vantage | `safety.one_undo` (`theHold`), `vantage.LIVE_LINK_GLOBALS`, `EXPORTER` | `theHold.Begin/Accept/Cancel`; which live-link entry exists; `vrayExportVRScene` keyword names |

## 2. VRayZDepth: planar or radial, raw or normalised

Conformance scene (or a scratch scene in the TULA units): a large plane perpendicular to the camera
axis at a known distance D (e.g. 10 000 mm), filling the frame. `elements.ensure_default_set()`, then
`render.render_packet_frame(cam, "C:/plate-spike/z.exr", "C:/plate-spike/z.tif", 1920, 1080, 0)`.
Read the Z channel at the centre pixel and at a corner pixel (OpenImageIO on the box,
`oiiotool --printinfo -v` and `--printstats`):

- centre equals D and the corner equals D: **planar**. The corner equals D / cos(angle to the axis):
  **radial**. Set `elements.ZDEPTH_KIND` accordingly.
- if the centre is not D but 1 - D/zdepth_max (or similar): the channel is **normalised**. Then
  either switch V-Ray to raw depth (min 0, max 1, invert) or read V-Ray's raw `Z` channel; change the
  rules in `elements.DEFAULT_SET` and the channel name in its `channels=`.
- empty pixels (sky): record the value as the zdepth element's `background_value`.

## 3. VRayNormals: space and sign

A sphere in front of the camera. In the normals layer at the sphere's right edge X must be about +1,
at the top Y about +1, at the centre Z about +1 (toward the viewer): camera space, signed
(`elements.NORMALS_SPACE`, `NORMALS_ENCODING`). If V-Ray writes world space, or remaps to 0..1, the
plugin must convert before writing (PACKET.md 4.2 requires camera, signed): note it and raise it as a
plan item.

## 4. Lens shift, tilt and the principal point

On the conformance scene with the eight markers: with every shift and tilt zero, reproject the
markers through `plate_spec.project_point(manifest["camera"], p)`: under 0.5 px RMS at 3840 wide,
principal point within 0.5 px of the centre. Then set a vertical shift of 0.1, and separately auto
vertical tilt: confirm `policy.lint_scene(cam)` reports `SHIFTED_CAMERA` and `packet.build_camera_block`
refuses. Measure where the principal point moves for the shift (for the later minor version of the
spec). Also reproject three known vertices of the TULA building within 1 px (the P0 exit).

## 5. Element overhead (caps: 15% time, 10% RSS)

On the heavy scene: render the camera beauty-only (all elements disabled), then with the default set,
both through `render.render_packet_frame`. Record `time_s` and `peak_rss_gb` of each into
`render.element_overhead` (`beauty_only_time_s`, `time_ratio`, `rss_ratio`). Note that
PeakWorkingSetSize is a process-lifetime peak: restart Max between the two runs.

## 6. The two files: EXR channels and the VFB TIFF16

- `oiiotool --info -v beauty.exr`: one part, every layer of `RenderResult.element_layers` present,
  the beauty layer's name (`elements.BEAUTY_LAYER`), Cryptomatte channels 32-bit float, the
  `cryptomatte/<id>/manifest` header attributes present, compression ZIP. Record each layer's channel
  names and pixel types: T19's EXR header reader overrides `elements.manifest_elements(dtypes=,
  channels=)` from them, but the defaults should already match.
- `display.tif`: `render.tiff_bits_per_sample()` is 16; it carries the VFB corrections (compare with
  a VFB screenshot) and the OCIO display transform (spike S/#19 risk: a save that skips the display
  transform). Then the colour harness's TIFF16-versus-OCIO check (ΔE00 p99 at most 0.5).
- `colour.detect_colour_pipeline(config)` on the box: mode, config path, display, view, config hash;
  write the raw dict into `docs/colour.md`.

## 7. Vantage

- `vantage.start_live_link()` starts the link (and a second call stops it); record which entry
  point answered.
- `vantage.export_vrscene("C:/plate-spike/cam.vrscene", cam, 0, 24)` writes an animated scene
  (open it in Vantage, scrub to frame 24); a still export writes one frame.
- The Vantage element question (PACKET.md 13): does a Vantage render of that .vrscene write any
  render elements (Z, normals, Cryptomatte)? A `renderer.name: vantage` packet must still carry the
  default set; if Vantage writes none, Vantage packets are beauty-only and the elements come from a
  V-Ray pass of the same camera.
