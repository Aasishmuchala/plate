# Runbook: two-point perspective (vertical tilt correction and shifted cameras)

Use when a V-Ray camera keeps its verticals vertical (auto vertical tilt, a vertical tilt value, or a vertical lens shift), when the dock lint shows `SHIFTED_CAMERA`, or when a packet's overscan note starts `level-and-crop`. Architects use these cameras all the time; a Shot Packet 0.1 camera is a plain pinhole, so PLATE sends them a different way. The geometry and the code are in [max-plugin.md](../max-plugin.md) ("Level-and-crop"); the artist's view is in [artist-manual.md](../artist-manual.md) section 4.

## 1. What PLATE does

Level-and-crop (decision 58). A two-point image is a crop of a level pinhole render, so PLATE renders the level one and records your frame as the safe frame:

- the level camera keeps your camera's position, heading (yaw) and roll, and has pitch zero;
- it renders `width × (height + 2 × ceil(|dy|))` pixels, where `dy` is how far your frame sits off the centre of the level image: `dy = fy × tan(pitch)` for a tilt-corrected camera (looking up is positive), or `dy = s × height` for a manual vertical shift `s`;
- the packet's `camera` is the level one, with zero tilt and shift, and its `safe_frame` is your original frame inside the taller render, so every gate runs on an ordinary v0.1 packet;
- your camera in the scene is never touched. Send renders a temporary copy named `__PLATE_level_<camera>` and deletes it afterwards.

A worked example: a 24 mm lens on a 36 mm film gate rendering 3840 × 2160 has `fy = 2560` px. Pitched up 3.55° with auto tilt, `dy = 2560 × tan 3.55° = 158.8` px, so the level render is 3840 × 2478 (`2 × ceil(158.8)` = 318 extra rows) and the safe frame is the 3840 × 2160 rectangle at `y = 0`: a camera looking up keeps its frame at the top of the level render and the 318 extra rows lie below it (a camera looking down is the mirror image, frame at the bottom). Pitched 8°, `dy` is 359.8 px and the render is 3840 × 2880.

| camera | what Send does |
|---|---|
| auto vertical tilt, or a vertical tilt value | level-and-crop; `SHIFTED_CAMERA` shows as a warning |
| a vertical lens shift | level-and-crop; same warning |
| tilt and shift together | stops: zero one of them |
| a horizontal shift or horizontal tilt | stops |
| lens distortion | stops |
| a pitch beyond 45 degrees | stops (the frame would be mostly stretch, and the render over twice the size) |
| a rolled camera | stops |
| a plain pinhole | no change |

## 2. Before you Send

1. Read the dock's lint. `SHIFTED_CAMERA` as a **warning** is fine; as a **stop** it names which of the rows above you hit.
2. Check the recipe's overscan. A recipe overscan aspect still applies on top, so the render can be taller still; the safe frame moves with it.
3. Send. The overscan note on the manifest reads `level-and-crop from a tilt-corrected camera: pitch X deg, dy Y px; rendered level at WxH` (or `... from a vertically shifted camera: shift s ...`). Keep that line in the job notes.
4. If Send stops at `render` with a message about the copy's pose, the camera is animated or constrained and the temporary copy would not take the level pose. Bake the camera for the still, or render a level camera yourself and Send that.
5. If Max crashes mid-render, a camera named `__PLATE_level_<camera>` may be left in the scene. It is a copy; delete it. Your own camera was not changed.

## 3. What is different downstream

- **The file is taller than your frame.** The Mask, Edit and every gate work on the whole level render; the extra rows are real context the engine sees, which is the point of overscan.
- **The deliverable is the safe frame.** Export writes the whole rendered frame. The safe frame is in the packet's `manifest.json` under `camera.safe_frame`. Social cutdowns and review bundles crop to it by themselves; for a full-size file, crop the export to it in retouch or Resolve (the rectangle starts at the left edge, full width, at the `y` the manifest gives).
- **Registration.** A point at `(u, v)` in the level render is at `(u, v - safe_frame.y)` in your original frame, within 0.5 px (the test that settles the sign checks five points at 2°, 3.55°, 8° and -3.55°).
- **Do not "fix" verticals later.** Do not straighten or perspective-crop a finished image in retouch: the packet is already correct, and a post-hoc warp is an invented camera.

## 4. The box measurement (open question 14)

The geometry is tested against a model of V-Ray's optics, not against V-Ray, so four things are unverified until someone measures them on the render box with the conformance scene (eight markers; `plugins/max/ONBOX.md` step 4, `docs/onbox-spikes.md` row 4):

1. **The unit of V-Ray's vertical shift.** PLATE reads `s` as a fraction of the frame height. Set `vertical_shift = 0.1`, render the markers, measure how far the principal point moves in pixels, and write the unit into `camera_level.SHIFT_UNIT` and the `dy` formula in `plugins/max/plate_max/bridge/camera_level.py`. Until then a **shifted** camera is a guess; its overscan note says so.
2. **The sign of a positive shift.** Does it raise the view? The code puts the frame at the top of the level render for a positive `dy`.
3. **What a manual `vertical_tilt` value corrects.** PLATE reads it as the full correction of the pitch, which is what *Guess vertical tilt* sets. Compare against auto.
4. **The reprojection exit.** For a V-Ray Physical Camera with auto vertical tilt: render the camera as the artist has it and render the level-and-crop packet; crop the packet to its safe frame; the markers must agree within 0.5 px.

Record the numbers in the checklist and fix the constants; do not change the geometry to fit a single scene.

Until the measurement is done, treat a level-and-crop packet as unconfirmed for client work: render one ordinary reference of the same camera (the Vantage or V-Ray frame the client has already seen) and check two or three vertical edges and a corner against the crop before the first real run. A tilt-corrected camera is lower risk than a shifted one, because its offset comes from the pitch and the focal length rather than from V-Ray's shift unit.

## 5. What not to do

- Do not zero the tilt in the camera and Send as an ordinary pinhole: the verticals lean and the client sees a different picture. Let level-and-crop do it.
- Do not change the shift or tilt between Send and a re-send and expect carried-over layers to land: a changed camera is a new picture, and an approved layer carries over only where its mask still overlaps the new plate by at least 0.98.
