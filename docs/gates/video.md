# Video gates: time map, reprojection drift, camera lock, assembly (`plate_gates.timemap`, `reprojection`, `video_gates`)

The motion half of BUILD_PLAN decision 14. A delivered clip is scored on five things and one report:

| metric (thresholds.yaml, `ts-2026.10.17-draft`) | bound | status | where |
|---|---|---|---|
| `video.first_frame` / `video.last_frame` | <= 12 / <= 16 levels of 255 | calibration_needed | `video.landing` (T6) |
| `video.camera_lock_median_flow_px` | <= 1.0 px at 960 wide | **hard on RAFT**, advisory on DIS | `reprojection.camera_lock` |
| `video.reproj_edge_recall` | >= 0.8 | **hard** | `reprojection.drift` |
| `video.duration_s` | >= the shot | **hard** | `video.duration_check` (T6) |
| flicker (`mean_abs_luma_delta`, `p95`) | none yet | reported | `video.flicker` (T6) |

`video_gates.run(video, hero_still, end_still, packet_motion, timemap, flow_backend)` assembles them into a
`report.GateReport` (as plain JSON data) whose `backend_notes` name the flow backend and width, the drift
reference, the time map and everything that was skipped and why. The phase-correlation lock in `video.py` and
the image-only edge agreement in `align.py` stay triage.

## 1. The time map (`timemap.TimeMap`)

A motion packet is rendered at the scene's rate (Max 25 or 30 fps); engines deliver 24 fps (Seedance, Kling) or
whatever their adapter reports. PACKET.md section 10 keeps the map **out of the packet** and on the motion job,
so one packet feeds any engine. Everything is exact `Fraction` arithmetic:

```
packet_frame_for(j) = start + j * packet_fps / engine_fps      fractional packet frame k at engine frame j
engine_frame_for(k) = (k - start) * engine_fps / packet_fps
duration_s          = (end - start + 1) / packet_fps
engine_frames       = round_half_up(duration_s * engine_fps)   the nominal count that covers the shot
```

25 -> 24 over 125 packet frames: 5 s, 120 engine frames, `packet_frame_for(119) = 123.9583`. The dolly fixture
(12 frames at 25) gives 0.48 s and 12 engine frames at 24; engine frame 11 maps to packet frame 11.458, which
**clamps** to 11 (`clamp()`, `nearest_packet_frame()`; the per-frame drift record says `clamped: true`). A
Kling clip is `ceil(shot_s)` seconds long, so frames past `engine_frames` exist and are not the shot: the drift
gate samples only `min(engine_frames, clip frames)`.

`method: nearest` says how a packet **frame** (Z, ids, clay) is chosen for an engine frame. The **camera** is
never snapped: `timemap.camera_at(camera_frames, k, manifest_camera)` interpolates the pose at the fractional k
(translation lerp, rotation slerp through `scipy.spatial.transform.Slerp`, focal and focus lerp when
`camera_frames.json` keys them; the intrinsics are rebuilt from the interpolated focal, PACKET.md 3.3). The slerp
midpoint between a 0 deg and a 30 deg yaw is 15 deg to 1e-6. `to_json()` writes fps as `"25/1"` text with the
exact duration, `from_json()` reads it back; `describe()` is the `backend_notes` line.

## 2. Reprojection drift (`reprojection.drift`)

```
drift(video, packet_motion, z_seq, id_seq, camera_frames, timemap, samples=8, width=960, *, clay_seq=None)
```

`packet_motion` is the packet folder (the sequences and `camera_frames.json` are then read from the manifest) or
the manifest mapping with explicit `{frame: path}` sequences (or lists indexed from the packet's first frame).
Per sampled engine frame j (evenly spaced, first and last included):

1. k = `packet_frame_for(j)`, n = `nearest_packet_frame(j)`.
2. **Reference edges** of packet frame n: `gbuffer.structural_edges` on Z_n and the rank-0 ids at the packet's
   native size (depth + id kinds; a motion packet has no normals). The channel names come from the manifest's
   `zdepth` / `cryptomatte_object` elements (`VRayZDepth.Z`, `CryptoObject00.R` viewed as uint32), with a
   suffix fallback.
3. **Weights.** Visibility: an edge the packet's own clay frame n does not show (same Canny detector as the
   still gate, dilated by r = 2) gets weight 0, exactly T15's rule for the plate. CoC: `coc.weights` when the
   camera has DOF (all ones otherwise). This is load-bearing: on the dolly fixture the clay shows 0.73-0.81 of
   the raw G-buffer edges even at perfect alignment (marker-sphere rings of 1-2 px, coplanar object seams), so
   an unweighted gate at 0.8 would fail a perfect clip. `use_clay=False` and a packet without a clay sequence
   report `unweighted` in the notes.
4. **Warp.** Every weighted edge pixel is back-projected with Z_n (PACKET.md 3.3 inverted), moved to world with
   frame n's `world_from_camera`, and projected into the camera interpolated at k (`warp_pixels`, vectorised;
   identity to 6e-5 px, frame 0 -> 1 on the dolly agrees with frame 1's own G-buffer at IoU 0.999). The warp is
   at most half a packet frame of camera travel (0.0-0.42 px on the dolly: `camera_delta_px`), so occlusion is
   not re-solved; the per-frame record carries the delta so a packet rendered too coarsely for its engine rate
   is visible in the report.
5. **Compare** at the working width `min(width, clip width, packet width)`, never upsampled (the report says
   which): warped pixels scaled to it, the clip's frame j (`io.frame_at`, a quarter frame before its timestamp)
   resized by area, `gbuffer.test_edges` (Canny on RGB, sigma 1.2, 20/50), dilated by r = 2.
   `edge_recall_r2` = weighted share of warped reference pixels that land on a dilated clip edge.
   `median_flow_px` = weighted median distance from a warped reference pixel to the nearest clip edge (distance
   transform, capped at 32 px); **1 px is the aligned baseline** because Canny peaks beside the G-buffer step.
   `shift_px` = `structure.best_shift` of the rasterised reference against the clip edges (+-8 px window).

`min_recall` over the samples is the gate value (`video.reproj_edge_recall >= 0.8`, hard); `mean_recall`,
`verdict`, the time map, the widths, `visibility_weighted`, `coc_weighted`, `edge_kinds` and `notes` ride along.

Measured on the dolly fixture (clay frames encoded at 25 fps, retimed to 24 by ffmpeg's `fps` filter, lossless
H.264): min recall **0.980**, mean 0.990, `median_flow_px` 1.0 on every sample. The same frames with a 6 px/frame
drift injected (frame j shifted by 6 j px): min recall **0.265**, mean 0.446, frame 0 still 0.995 (no drift yet),
every later sample < 0.47 with `median_flow_px` climbing 3.2 -> 12.8 and `shift_px` saturating at -8 toward the
packet. Unweighted, the good clip measures 0.738: the visibility rule is what makes 0.8 a fair line.

## 3. Camera lock on optical flow (`reprojection.camera_lock`)

```
camera_lock(video, backend=None, width=960, samples=8) -> {median_flow_px, p95, backend, status, per_pair, ...}
```

`samples` frames evenly over the clip; flow from the first sampled frame to each later one on a
`plate_pixels` `FlowBackend` after resizing to at most `width` wide, so the numbers are the threshold's pixels.
`median_flow_px` and `p95` are the **maxima over pairs** of the per-pair median / 95th percentile of the flow
magnitude: the worst sampled moment, so a camera that drifts late cannot hide behind a steady start. The status
comes from `thresholds.yaml` with the backend applied (`status_by_backend`): `raft` hard, `dis` advisory, and the
report's `backend` / `threshold_status` say which. `backend=None` builds `plate_pixels.flow.DisFlow` (OpenCV
DIS, medium preset, no weights, real on every machine). The clip-level `backend_notes` line is
`flow backend dis @960`.

Measured: a static clip (one clay frame repeated) gives median **0.0** on DIS; a 3 px/frame pan gives per-pair
medians 6.0, 9.03, 15.01, 18.0, 24.0, 26.99, 33.01 for sampled frames 2, 3, 5, 6, 8, 9, 11 (DIS tracks a pure
translation to 0.03 px); the dolly clip itself measures 14.5 px, which is why the lock is only **scored** on
clips meant to be locked.

## 4. Assembly (`video_gates.run`)

```
run(video, hero_still, end_still|None, packet_motion|None, timemap|None, flow_backend|None,
    *, shot_duration_s=None, expect_locked=None, engine=None, tier="A",
    z_seq=None, id_seq=None, clay_seq=None, camera_frames=None, samples=8, width=960) -> dict
```

- landing against the hero / start still and the end still (no end still: `last` not scored, noted);
- camera lock always computed; **scored only when `expect_locked`** (default: no motion packet). A push-in or
  a dolly moves by design and the number is then a description under `metrics.camera_lock`, not a fault;
- drift when a motion packet is given; a missing time map is built from the packet's `camera.frames.fps` and
  the clip's probed fps (`build_timemap`) and noted;
- flicker reported (no threshold in this set);
- duration against `shot_duration_s`, else the time map's duration, else not scored (noted);
- `metrics.output`: `fps` as `"24/1"`, size, codec, `pix_fmt`, `bit_depth` (8 for `yuv420p`, 10 for
  `yuv420p10le`, 12 for `gbrp12le`, 16 for `rgb48le`), frame count, duration.

The result is `GateReport.score(...)` dumped to JSON: flat scored values plus the nested `landing`,
`camera_lock`, `drift`, `flicker`, `duration`, `output`, `timemap` records, `checks`, `verdict` (fail iff a
hard check fails), `backend_notes`, `metric_code_hash`, `threshold_set_id`. `video_gates.score` returns the
pydantic model for callers that write it with `report.write`. T43's Kling job records it next to the time map.

## 5. Tests and fixtures

`gates/tests/test_timemap.py` (pure): the 125 -> 120 case and 123.96, `24000/1001`, clamping, round-half-up,
JSON round trip, slerp midpoint 15 deg, translation / focal / focus lerp, `camera_at` intrinsics and
`plate_spec.project_point` agreement. `gates/tests/test_reprojection.py` (skips without ffmpeg or the dolly
fixture): readers follow the manifest channel names, `warp_pixels` is the identity on one camera and matches
`project_point` across two, visibility weighting, the good / drifted clips above, explicit-sequence and
manifest-mapping inputs, sampling bounded by the time map, DIS static lock 0 +- 0.3, the pan, hard-on-RAFT vs
advisory-on-DIS through a fake backend, and the assembled report for a motion job, a locked clip and a job
without a time map.

## 6. Verify on the box

RAFT at 960 wide (`plate_pixels.flow.RaftFlow`, CUDA) is the measurement of record for the lock. Run
`camera_lock(clip, RaftFlow(), 960)` on one accepted and one rejected Kling take and record the medians here:

| take | verdict (human) | DIS median px (Mac) | RAFT median px (box) |
|---|---|---|---|
| accepted locked take | accepted | to record | to record |
| rejected drifting take | rejected | to record | to record |

Until then every lock report on this Mac says `flow backend dis @960` and the check is advisory.
