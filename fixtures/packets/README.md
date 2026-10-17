# fixtures/packets

Committed Tier A Shot Packets (spec 0.1.1), rendered by `fixtures/blender` and validated by
`plate_spec.validate_packet_dir(..., check_files=True)`. Regenerate, never hand-edit: `bash fixtures/blender/render.sh`.

| packet | kind | size | camera | notes |
|---|---|---|---|---|
| `room_a` | still | 640 x 360 | `CAM_a` 35 mm, f/2.8 focus 2.5 m, safe frame 640 x 268 (2.39:1) | opt-in `raw_lighting`, `raw_gi`, `reflection` elements |
| `room_b` | still | 640 x 360 | `CAM_b` 24 mm, full frame | same scene as `room_a` (equal `scene_hash`), second camera |
| `room_a_v2` | still | 640 x 360 | `CAM_a` | the east wall recoloured; `supersedes` = `room_a.packet_id` |
| `room_a_ral3020` | still | 640 x 360 | `CAM_a` | `ARCH_panel_7016` albedo set to RAL 3020 (#CC0605); raw elements; T31's re-render target |
| `dolly_hero` | still | 640 x 360 | `CAM_dolly` frame 0 | the hero still of the motion packet |
| `dolly_12f` | motion | 320 x 180 | `CAM_dolly`, 12 frames, 0.6 m forward | `motion/clay.%04d.png` (uint16), `id.%04d.exr`, `z.%04d.exr`, `camera_frames.json`, `path_crossings: []` |

Every packet: `manifest.json`, `beauty.exr` (single part, V-Ray layer names), `display.tif` (16-bit master),
`id_legend.json` (30+ entries: objects and materials, classes by layer-prefix rule). Packet ids are deterministic
(`pkt_` + 26 Crockford characters derived from the shot name).

Eight conformance markers (`ARCH_marker_01..08`, 2 cm spheres, coordinates in `../blender/markers.json`) are visible
in every camera; projecting them with `plate_spec.project_point` lands within 0.05 px of their Cryptomatte
centroids (`tests/test_packets_valid.py`). Client plates are not here and never will be.
