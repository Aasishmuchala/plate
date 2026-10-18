# Shot Packet v0.1 (draft)

Status: **v0, draft**, written 2026-10-17 before the P0 on-box spikes. Schema: [`packet.schema.json`](packet.schema.json) (JSON Schema 2020-12, `$id` `urn:sthyra:plate:packet:0.1`). Reference code: [`plate_spec/`](plate_spec/__init__.py). Examples: [`examples/`](examples/). Plan: `BUILD_PLAN.md` sections 3 and 4.

A Shot Packet is what the 3ds Max plugin sends instead of a screenshot. It is an open, versioned folder: the approved render, its geometry passes, the exact camera and the ID policy that decides what AI may touch. Everything PLATE does to a frame (egress padding, mask-locked compositing, the fidelity gates, the render-it router, client-revision carry-over) reads it. The spec is published so that any host (Blender after the week-16 gate, SketchUp or anything else later) can write a sender in a few hundred lines.

The words MUST, MUST NOT and SHOULD are normative. Where JSON Schema cannot express a rule, section 8 lists it and `plate_spec.validate_manifest` / `validate_packet_dir` enforce it. A packet is valid only when both the schema and the semantic rules pass.

## 1. Folder layout

```
<packet folder>/                 any name; the daemon keys on packet_id, never on the folder name
  manifest.json                  this spec
  id_legend.json                 ID -> node, material, layer, class (section 4)
  beauty.exr                     tier A: multilayer EXR, linear beauty + data passes
  display.tif                    tier A: VFB-corrected 16-bit TIFF, the approved display master
  camera_frames.json             motion only: per-frame camera matrices
  motion/clay.%04d.png           motion only: clay sequence, ID and Z sequences, 720p
  motion/id.%04d.exr
  motion/z.%04d.exr
  motion/<role>.blake3           one "<blake3 hex>  <relpath>" line per frame
  client_plate.png               tier C only (with depth_estimated.exr and segments.png)
```

File names other than `manifest.json` are free: the manifest's `files` map is the only index. Every path in it MUST be packet-relative POSIX: no leading `/`, no drive letter, no backslash, no `..` segment. The Windows plugin writes forward slashes. Because packets are written on Windows and may sit on an SMB share, file names MUST avoid `< > : " \ | ? *`, MUST NOT differ only by case, and SHOULD keep the packet folder path plus the longest relative path under 240 characters (MAX_PATH is 260; LongPathsEnabled is optional, not assumed). A file outside the folder cannot be referenced (`vrscene_path` in `render` is informational and never read by the daemon).

Every file entry carries `bytes` and a BLAKE3 `hash`; every sequence carries a hashes file. The daemon ingests by reference: it re-hashes on ingest and refuses a packet whose bytes do not match (HTTP 422).

## 2. Identity and versions

| field | rule |
|---|---|
| `packet_version` | `0.1.<patch>`. A reader accepts any patch of a major.minor it knows. |
| `packet_id` | `pkt_` + a 26-character Crockford base32 ULID. Time-sortable and opaque: it carries no client, project or file name, so it can appear in vendor-side ids and logs. |
| `kind` | `still` or `motion`. |
| `tier` | `A` scene-backed (3ds Max + V-Ray, full gates) or `C` client plate with estimated geometry (decision 1). Tier B is deferred. |
| `project_id`, `shot_id` | lower-case slugs. Local only; never sent to a vendor (section 11). |
| `supersedes` | the `packet_id` this packet replaces after a client change, or null (decision 17). |
| `host.plugin`, `renderer` | name and version, used by the version handshake (section 9). |
| `render.session` | `interactive` or `batch` (3ds Max), `client_supplied` (tier C), or `fixture`: a repo test packet rendered by `fixtures/blender` (Cycles) and repacked to V-Ray layer names. A fixture packet is tier A with `host.app: none` and `renderer.name: vray` (version `fixture-cycles-<blender>`); every other tier A packet has `host.app: 3dsmax`. Fixture packets are test data, never client work. |

## 3. Camera

### 3.1 The convention (`plate-cam-v0`)

There is exactly one convention in v0.1, written into every manifest as a block of constants (`camera.convention`) so a sender cannot silently use another one:

| item | value |
|---|---|
| world | right-handed, **+Z up** (3ds Max native) |
| camera axes | looks down local **−Z**, **+Y** up, **+X** right |
| matrix | `world_from_camera` (camera-to-world), 4×4 |
| storage | row-major nested list `[[r00, r01, r02, tx], [r10, r11, r12, ty], [r20, r21, r22, tz], [0, 0, 0, 1]]` |
| vectors | column vectors: `p_world = M @ [x, y, z, 1]ᵀ` |
| lengths | **scene units** everywhere in the manifest and in the Z pass; metres = value × `units.meters_per_unit` |
| focal, film | millimetres (camera industry convention) |
| pixels | origin at the **top-left corner** of the top-left pixel; pixel centres at +0.5; u right, v down |

The rotation block MUST be orthonormal with determinant +1 (tolerance 1e-4): no scale, shear or mirror on a camera node. A scaled camera in Max is a lint failure the plugin reports before Send.

### 3.2 From a 3ds Max `Matrix3`

Max stores transforms in row-vector form: `row1`, `row2`, `row3` are the camera's local X, Y and Z axes in world space and `row4` is the position. Max cameras already look down local −Z with +Y up, so the conversion is a transpose and nothing else:

```
M[i][j] = row(j+1)[i]   for i in 0..2, j in 0..3
M[3]    = [0, 0, 0, 1]
```

Reference: `plate_spec.world_from_camera_from_max_rows(rows)`. Use the camera node's **`objecttransform`** (world space, including any object offset), not `transform`: a camera whose pivot was moved with Affect Pivot Only renders from its object, not its pivot. Evaluate it at the hero frame.

### 3.3 Intrinsics and the projection (normative)

`camera.intrinsics_px` is the authoritative pinhole model, in pixels of the **rendered** frame (overscan included):

```
K = [[fx, 0,  cx],
     [0,  fy, cy],
     [0,  0,  1 ]]

fx = focal_length_mm × zoom_factor × width_px / film_width_mm
fy = fx × pixel_aspect                (pixel_aspect = pixel width / pixel height)
```

Projecting a world point `p`:

```
[xc, yc, zc] = Rᵀ (p − t)             R, t from world_from_camera
depth        = −zc                    planar depth along the view axis, > 0 in front
u            = cx + fx × xc / depth
v            = cy − fy × yc / depth   (v grows downward)
```

Reference: `plate_spec.project_point(camera, p)`. This is the formula the P0 exits test (three known vertices within 1 px; eight conformance markers within 0.5 px RMS at 3840 wide).

`focal_length_mm`, `film_width_mm`, `zoom_factor` and `fov_deg` are kept for humans and for the circle-of-confusion weighting, and MUST agree with `K` (section 8; fx within 0.01%, FOV within 0.005°, about 0.2 px at the edge of a 3840 frame). The film gate is fitted horizontally (Max's default aperture-width fit); `fov_deg.vertical` follows from the aspect.

**Lens shift, tilt and distortion: not in v0.1.** The pinhole model above cannot represent them, so v0.1 accepts only cameras with zero horizontal and vertical shift, `vertical_tilt_correction: false` (V-Ray's 2-point-perspective tilt levels the image plane, which no principal-point offset reproduces: on the clubhouse pose a 3.55° pitch leaves 48 px of error) and `distortion: none`. The plugin refuses Send otherwise and tells the artist why. With zero shift the principal point MUST be the frame centre within 0.5 px. How V-Ray's shift and tilt map to an effective image-plane rotation and principal point is a P0 measurement on the conformance scene; a later minor version adds them, and then `world_from_camera` becomes the effective image-plane rotation, not the node's.

### 3.4 Resolution, overscan and safe frame

`resolution` is the frame actually rendered. Decision 3: packets are rendered with overscan to the nearest engine-legal aspect, so the vendor sees real context instead of padding, and the deliverable is cropped from that. `safe_frame` is the deliverable rectangle inside the rendered frame (render pixels, origin top-left). `overscan.engine_aspect` records which engine aspect the frame was padded to. With no overscan, `safe_frame` is the whole frame. The plate is never upscaled: render at delivery resolution.

### 3.5 Lens, exposure, white balance, frames

- `f_number`, `focus_distance` (scene units) and `dof_enabled` feed the edge-recall gate's circle-of-confusion weighting (decision 14). If `dof_enabled` is false, gates apply no CoC weighting. Both are null only in tier C.
- `exposure.mode`: `physical` requires `iso`, `shutter_s` and `ev100`, and `ev100 = log2(N² / t) − log2(ISO / 100)` within 0.05. `ev` requires `ev100`. `none` means the camera has no exposure control.
- `white_balance`: `kelvin`, `rgb` or `neutral`.
- `frames`: `hero`, `start`, `end`, `fps`. A still has `start == hero == end`. A motion packet's first frame is the hero frame (`hero == start`), because frame 0 is identical to the hero still by construction.

## 4. Files, render elements and the ID legend

### 4.1 Files

`files` maps a role to a file or a sequence. Known roles:

| role | tier/kind | content |
|---|---|---|
| `beauty_exr` | A | multilayer EXR: linear beauty plus the data passes listed in `elements` |
| `display_tiff16` | A | VFB-corrected 16-bit TIFF, RGB or RGBA: the approved display master |
| `id_legend` | all | `id_legend.json` |
| `camera_frames` | A motion | `camera_frames.json` (schema `#/$defs/camera_frames_file`) |
| `clay_seq`, `id_seq`, `z_seq` | A motion | frame sequences at `motion.sequence_resolution` (720p) |
| `client_plate`, `depth_estimated`, `segments` | C | the client's plate, the estimated depth and a 16-bit segment label map |
| `x_<name>` | any | extension roles, e.g. an opt-in pass written to its own file |

`display_tiff16` may use any compression tifffile+imagecodecs decode (none, Deflate, LZW, PackBits); JPEG-compressed TIFF is refused.

`beauty_exr.parts` is `single` (the default when absent: one EXR part holds every layer, which is what V-Ray writes) or `multi` (one part per layer, Blender's layout). Readers find each element by its `layer` and `channels` across all parts either way.

Any other role is rejected. `beauty_exr`, `display_tiff16`, `client_plate`, `depth_estimated` and `segments` MUST have the rendered resolution exactly. EXR compression MUST be lossless (`none`, `rle`, `zip`, `zips`, `piz`): DWA and PXR24 quantise float channels and corrupt Cryptomatte hashes and world positions.

**Sequences.** A sequence pattern contains exactly one `%0Nd` and no other `%`. `z_seq` and `id_seq` are float EXR with the same semantics as the beauty EXR's Z and Cryptomatte elements (unclamped, unfiltered, the same `depth_kind`). Sequence frames have exactly the render's aspect (`w_seq · H == h_seq · W`), so their intrinsics are `K` scaled by `s = w_seq / W` on both axes: `fx_seq = s·fx`, `cx_seq = s·cx` (exact under the top-left-corner pixel convention).

### 4.2 Render elements (decision 5)

`files.beauty_exr.elements` lists every layer in the EXR with its dtype, whether it was filtered and whether it was denoised. The **default set is required** in tier A: `beauty`, `zdepth`, `normals`, `cryptomatte_object`, `cryptomatte_material`, `wire_colour`, `diffuse_filter`. Opt-in per shot: `world_position` (cross-view correspondences), `light_select` (one entry per light, `light_name` required), `velocity`, `raw_lighting`, `raw_gi`, `reflection`, `refraction`, `specular`, `self_illumination`, `alpha`.

Rules the schema enforces:

- **Z depth** is `float`, unfiltered, never denoised, `normalized: false`, `clamped: false` (VRayZDepth forced unclamped), in scene units. `depth_kind` states `planar` (distance along the view axis, the quantity in 3.3) or `radial` (distance to the camera centre). Which one VRayZDepth writes is a P0 measurement; readers MUST honour the field rather than assume. `background_value` records what empty pixels hold, or null if unknown.
- **Cryptomatte** layers are `float` (32-bit: half-float corrupts the hashes) and never denoised, with their `rank_levels`.
- **Normals** are in camera space (`plate-cam-v0` axes: +X right, +Y up, +Z toward the viewer), `encoding: signed` in [−1, 1], never denoised.
- **World position** is `float`, world space, never denoised.
- No data pass (Z, normals, Cryptomatte, world position) may appear in `render.denoiser.denoised_elements`.

### 4.3 The ID legend

`id_legend.json` (schema `#/$defs/id_legend_file`) maps every ID visible in this camera to what it is:

```json
{"id": "o:dc37ac5e", "kind": "object", "name": "VEG_palm_row_A", "layer": "VEG_palms",
 "material": "MAT_leaf_palm", "tags": [], "class": "vegetation", "class_source": "rule",
 "pixel_share": 0.095, "detail_size_m": 0.06, "digest": "<64-hex BLAKE3 of the node record>"}
```

- **Ids.** `o:<8 hex>` Cryptomatte object, `m:<8 hex>` Cryptomatte material, `s:<5 digits>` tier C segment (the 16-bit label value, zero-padded, 00001 to 65535). The hex is the Cryptomatte hash: MurmurHash3_x86_32 of the UTF-8 name with seed 0, with bit 23 flipped when the exponent byte is 0 or 255 (Cryptomatte spec, so the float32 id is never denormal, infinite or NaN). Reference: `plate_spec.cryptomatte_id(name, kind)` and `cryptomatte_float(name)`.
- **Name binding.** `id_legend.cryptomatte.<layer>.id_type` says what V-Ray hashed (`node_name`, `node_path`, `material_name`, `layer_name`), and each entry's `name` is exactly that string, so `id == cryptomatte_id(name)` is checked for every entry. Two visible nodes or materials with the same name would merge into one ID with one class, so duplicate visible names are a scene-lint failure that blocks Send.
- **Digest.** Every tier A entry carries `digest`, the BLAKE3 of that node's (or material's) record in the scene digest, so carry-over after a client change can tell an unchanged ID from an edited one with the same name.
- **`detail_size_m`** (optional) is the characteristic detail size the render-it router uses (leaf 0.06 m, brick 0.065 m, sett 0.10 m); the auto-tagger fills it from class and material.
- **Classes.** `architecture, glazing, signage, ground, vegetation, sky, people, water, vehicle, furniture, other`.
- **`class_source`.** `rule` (a `policy.class_rules` entry matched, and the first matching rule MUST give this class), `autotag`, `manual`, or `unlabelled` (class `other`).
- **`pixel_share`.** Fraction of the frame covered by that ID at rank 0. Shares are summed over object ids (tier A) or segment ids (tier C) to produce `id_legend.coverage`; material ids cover the same pixels again and are not summed. `coverage` has three parts that sum to 1: `labelled_share`, `unlabelled_share` and `uncovered_share` (pixels with no ID, such as the environment background). Uncovered pixels are locked by construction: nothing can make them editable. The sender's coverage is a claim; the daemon recomputes coverage from the Cryptomatte layers at ingest (P1, needs OpenEXR on the box) and rejects a packet whose legend omits visible IDs.
- **`planar`** marks large planar surfaces for the invention-flag gate (new long edges inside a planar single-ID region).

The manifest's `id_legend` block names the file, the source (`cryptomatte` with both layer configs, or `segmentation` with its model), the entry count and the coverage.

## 5. Scene hash, view hash and invalidation

- `scene_hash` is BLAKE3 over the plugin's canonical scene digest (`digest_version` names its format): sorted node names, world transforms, material assignments, mesh topology fingerprints, light parameters and render settings. It is not a hash of the .max bytes: a resave with no change keeps the hash, and hashing a multi-GB file per Send would be too slow. `max_file` records the file name, size and mtime for humans.
- `view_hash` is BLAKE3 over the camera block, the global state that affects every pixel (lights, environment and HDRI, sun, render settings, colour management) and the `digest` of every ID visible in this camera (the frustum-visible node set from the Cryptomatte pass, which includes objects added since the last packet).

Decision 17: a client change invalidates only the cameras whose `view_hash` changed. A new packet for such a camera sets `supersedes`; approved AI layers carry over onto the new plate wherever their IDs are unchanged.

## 6. Colour (decision 2)

- `max_colour_mode` is `ocio` (then the `ocio` block is required: config name, BLAKE3 of the config, display, view, optional look) or `legacy_gamma` (then `legacy_gamma` is required). The plugin MUST detect this at Send time; a packet with an unknown mode is not sent. `not_applicable` is for tier C only.
- `working_space` (`lin_rec709` or `acescg`) is the space of the EXR beauty. Tier C uses `display_only`.
- `master` is `display_tiff16` for tier A: **what Aasish signed off in the VFB is what ships.** P0 checks that an OCIO re-derivation from the EXR matches the TIFF at ΔE00 p99 ≤ 0.5; if it does not, the TIFF still wins and the EXR is data only. Tier C's master is `client_plate`.
- `vfb_corrections_baked` says whether VFB layers are baked into the TIFF; `vfb_layers` names them.
- `display_encoding` (primaries, transfer, ICC) is how the TIFF is tagged. Egress to vendors is always 8-bit sRGB PNG with ICC, never JPEG (decision 3); that is the egress module's job, not the packet's.

## 7. Policy: what AI may touch

```json
"policy": {"rules_version": "1", "class_rules": [{"source": "layer", "match": "prefix", "pattern": "VEG_", "class": "vegetation"}],
           "lock_by_default": true, "locked_ids": ["o:..."], "editable_ids": ["o:..."], "text_lock": true,
           "lint": {"max_unlabelled_share": 0.05, "unlabelled_share": 0.004, "passed": true},
           "custody": {"nda": true, "allowed_grades": ["C0", "C1"]}}
```

- **Class rules** derive classes from layer, node or material prefixes (`VEG_`, `SKY`, `PEOPLE`, `WATER`, `ARCH_`, `GLZ_`, `SIGN_`, `HARD_`, `VEH_` in the examples); an auto-tagger fills only the IDs no rule matches and marks them `autotag`. **Rules win:** an ID a rule matches takes the first matching rule's class unless it is a logged `manual` override, and an ID on a never-editable rule's layer stays non-editable even when overridden.
- **Object ids decide, material ids narrow.** The editable mask is the union of editable object IDs; an editable material ID only narrows an editable object to that material's pixels. A material ID used by any locked object may not be editable, so a living-wall material cannot open up a locked facade.
- **Locked by default.** `lock_by_default` is the constant `true`. Every legend ID is in exactly one of `locked_ids` or `editable_ids`. Outside an editable mask the delivered pixels are the render by construction.
- **Never editable:** `architecture`, `glazing`, `signage` and `other`, plus anything `unlabelled`. Recolouring a RAL panel is element recomposition (math), not an AI edit, so it needs no editable ID.
- **Text lock.** `text_lock` is the constant `true`: signage is locked and gets an OCR text-lock mask composited back from the plate.
- **Lint.** The plugin blocks Send when unlabelled IDs cover more than `max_unlabelled_share` of the frame (default 0.05, to be calibrated in P0, schema cap 0.25), when two visible names collide, when the camera is shifted, tilted, distorted, scaled or mirrored, and when the colour mode is unknown. A manifest therefore always has `passed: true`, and `unlabelled_share` MUST equal `id_legend.coverage.unlabelled_share`. The sender's rules and threshold are advisory: the daemon re-applies the project's own class rules and threshold at ingest and the stricter result wins.
- **Custody.** Optional floor known to the plugin. The daemon's project setting also applies and the stricter wins. NDA projects allow only C0 and C1 (decision 9).

## 8. Semantic rules (enforced by `plate_spec`, not expressible in JSON Schema)

1. `world_from_camera` rotation is orthonormal (|RᵀR − I| ≤ 1e-4) with det +1; tier C's matrix is identity. Lens shift is zero, tilt is off, distortion is `none` (schema).
2. `fov_deg.horizontal = 2·atan(film_width / (2·focal·zoom))` and `fov_deg.vertical` follows from `height / (width · pixel_aspect)`, both within 0.005°.
3. `fx = focal·zoom·width / film_width` within 0.01%; `fy = fx · pixel_aspect`; `K` has no skew and matches `fx, fy, cx, cy`; the principal point is inside the frame and centred within 0.5 px when the lens shift is zero.
4. `safe_frame` lies inside the rendered resolution; a partial safe frame requires `overscan`, whose `engine_aspect` matches the rendered aspect within 1%.
5. Frames: `start ≤ hero ≤ end`; a still has `start == hero == end`; a motion packet has `hero == start`.
6. Physical exposure: `ev100` agrees with `f_number`, `shutter_s` and `iso` within 0.05.
7. File dimensions match `camera.resolution`; EXR elements are unique; Cryptomatte element layers and rank levels match `id_legend.cryptomatte`; no data pass is listed as denoised.
8. Motion: every sequence covers `start..end` at `motion.sequence_resolution`, whose aspect equals the render's exactly; `hero_still_packet_id` names another packet; `camera_frames.json` is contiguous `start..end`, at the manifest's fps, orthonormal per frame, and its hero frame equals `camera.world_from_camera` within 1e-6.
9. Policy: `locked ∩ editable = ∅`; together they cover every legend ID and nothing else; never-editable classes, IDs on never-editable rule layers and unlabelled IDs are not editable; a rule-matched ID has its first rule's class unless `manual`; no editable material is used by a locked object; coverage shares equal the sums over entries within 1e-3 and the three shares sum to 1; lint share ≤ max and equal to coverage; NDA custody lists its grades and allows C0 and C1 only.
10. Legend: `packet_id` matches; ids are unique; the id prefix matches `kind`; every object and material id is the Cryptomatte hash of its `name`; segment ids are 1 to 65535; tier A entries carry a `digest`; Cryptomatte legends hold no segments and segmentation legends only segments; `entry_count` matches.
11. Files (when checked): every path resolves inside the folder; no two paths differ only by case; sizes and BLAKE3 hashes match; each sequence's hashes file has one well-formed line per frame naming the pattern's file. JSON files are UTF-8; a BOM is tolerated on read and never written. A malformed file is reported as an error, never a crash.

## 9. Transport and the version handshake

The plugin writes the folder locally (or on the NAS) and then tells the daemon where it is. HTTP rather than a named pipe, so any sender is a few hundred lines.

```
POST http://127.0.0.1:4610/api/v0/packets
Authorization: Bearer <token>
X-Plate-Packet-Version: 0.1.1
{"packet_path": "<absolute folder path>", "sender": {"name": "plate_max", "version": "0.1.0"}}
```

- Token and daemon URL live in `config.json` in the PLATE app-data folder: `%LOCALAPPDATA%\Plate\config.json` on Windows (written by the plugin's `install.ps1`), `~/Library/Application Support/Plate/config.json` on macOS, as `{"token": "...", "daemon_url": "http://127.0.0.1:4610"}`. The folder is `platformdirs.user_data_dir("Plate", appauthor=False)`; `PLATE_HOME` moves the whole folder, `PLATE_CONFIG` overrides the config path, and `PLATE_TOKEN_FILE`, when set, names a file holding only the token, which wins over `config.json`'s `token`. On start the daemon prints `http://127.0.0.1:4610/#token=<token>`; the UI keeps the token in sessionStorage and sends `Authorization: Bearer`.
- `packet_path` is the folder's absolute path on the daemon's machine: a drive path (`D:\PLATE\packets\...`), a UNC path to the NAS (`\\nas\plate\...`) or a POSIX path. The daemon resolves it with pathlib; everything inside the folder stays packet-relative POSIX (section 1).
- `202 {"packet_id", "status": "ingesting", "events_url": "/api/v0/packets/<id>/events"}` on a new packet.
- `200 {"packet_id", "status": "known"}` when the same `packet_id` arrives again with the same manifest hash (idempotent re-send).
- `409` when the same `packet_id` arrives with a different manifest hash.
- `401` bad or missing token; `422 {"errors": [...]}` from `validate_packet_dir`; `426 {"supported": ["0.1"]}` on an unknown major.minor.
- `GET /api/v0/packets/<id>` returns the stored record; `GET /api/v0/packets/<id>/events` streams server-sent events.
- `GET /api/v0/health` returns `{"ok": true, "daemon_version", "packet_versions": ["0.1"]}`; the plugin calls it before Send and refuses to send a version the daemon does not list (decision 25).

The daemon binds 127.0.0.1 by default and never 0.0.0.0. Binding the Tailscale address is opt-in; on Windows it needs an inbound firewall rule scoped to the Tailscale interface (see `docs/windows.md`). Packet folders may live on the NAS; the daemon's SQLite index never does (WAL is unsafe on SMB), it lives in the local app-data folder.

## 10. Motion packets

A motion packet (`kind: motion`) is the clay sequence rendered from the real camera path, for previs and for the drift gates. It references its hero still (`motion.hero_still_packet_id`) and carries:

- `camera_frames.json`: one `world_from_camera` per frame, optionally per-frame focal and focus distance.
- clay, ID and Z sequences at `motion.sequence_resolution` (720p), each with a hashes file.
- `clay_override`: the override material and whether glass and lights were kept.
- `frame0_matches_hero: true` (constant).
- optional `path_crossings` (thresholds, room changes, occluders) that feed the disocclusion budget and Seedance timing hints.

The frame-rate time map between the packet's fps and an engine's (Seedance 24 fps versus Max 25 or 30) is recorded on each motion job, not in the packet.

## 11. Tier C: client plates with estimated geometry

When a client supplies only a plate, the daemon builds a tier C packet: `host.app: none`, `renderer.name: client`, `units.system_unit: relative` (depth is not metric), `camera.source: estimated` with intrinsics from GeoCalib expressed on a 36 mm full-frame-equivalent film width, `dof_enabled: false`, `files` holding `client_plate`, `depth_estimated` (float EXR) and a 16-bit `segments` label map, and an `estimated` block naming each model, version and licence. In tier C there is no world: the world frame is the camera frame, so the pose is identity and the +Z-up convention does not apply; GeoCalib's gravity estimate is kept as `estimated.gravity_camera` (a unit vector in camera axes). `estimated.depth_semantics` says whether the depth file holds Depth Anything V2's relative inverse depth or relative depth; readers MUST NOT treat it as Z. Tier C has no `scene_hash` and no motion. Gates degrade honestly: no invention flags on planar IDs, no per-material targets, and everything downstream is badged "estimated geometry". The render-it router cannot compute detail resolvability or a disocclusion budget from relative depth, so on tier C it reports "unknown" and a human decides. The free client packet kit (a MAXScript that exports Z, Cryptomatte and camera next to a client's plate) turns a tier C job into tier A.

## 12. Custody of the packet itself

The manifest and legend hold client names, file names and layer names. They are **C0: they never leave the box.** Egress sends only pixel derivatives under opaque ids with metadata stripped. The packet folder is client work product and is covered by the project's backup, retention and purge rules (decision 23).

## 13. Measured on the box in P0 (the spec defers to the measurement)

| question | field that records the answer |
|---|---|
| Max 2026 colour mode (spike S/#19) | `colour.max_colour_mode` |
| Is VRayZDepth planar or radial? What do empty pixels hold? | `elements[zdepth].depth_kind`, `background_value` |
| Are VRayNormals camera-space and signed? | `elements[normals].space`, `encoding` (schema requires camera and signed: if V-Ray differs, the plugin converts) |
| V-Ray lens shift and vertical tilt as an effective image-plane rotation and principal point | refused in v0.1 (`lens_shift` must be zero, tilt off); added in a minor version once measured |
| Which Max transform the V-Ray camera renders from (expected `objecttransform`) | `camera.world_from_camera`, checked by the vertex reprojection exit |
| Does an OCIO re-derivation match the VFB TIFF? | daemon ingest report, not the packet |
| Element overhead on the heavy scene (caps 15% time, 10% RSS) | `render.element_overhead` |
| Does Vantage write any elements? | `renderer.name: vantage` packets are allowed by the schema but must still carry the default set |

## 14. Versioning

The schema is closed (`additionalProperties: false`), so **any new field or role is a minor bump** (`0.2`) with a new schema `$id`, as is removing or renaming a field or changing a constant in the convention. A patch release (`0.1.x`) only fixes prose and validator bugs or enforces a constraint the prose already stated; it never makes a previously valid, prose-conforming packet invalid. Extension data that cannot wait for a minor bump goes in `x_` file roles. One exception, taken in 0.1.1 while `plate_spec` is the only validator and no third-party sender exists: an optional addition that every earlier 0.1 packet still satisfies (a new enum value, an optional field whose default is the old behaviour) may ship as a patch. From the first external sender on, such additions are minor bumps. The daemon lists the major.minor versions it reads in `/api/v0/health`. Old packets are never rewritten; a converter writes a new packet that `supersedes` the old one.

## Changelog

- 0.1.1 (2026-10-17): `render.session: fixture` for repo test packets (tier A with `host.app: none`, section 2); optional `files.beauty_exr.parts: single | multi` (default single, section 4.1); `PLATE_TOKEN_FILE` and the `#token=` launch URL (section 9); patch-level additions allowed until the first external sender (section 14). The three examples move to 0.1.1. Every 0.1.0 packet stays valid.
- 0.1.0 (2026-10-17): first draft, pre-P0, revised the same day after an adversarial Opus review (12 confirmed holes closed: shift, tilt and distortion refused; uncovered share; view hash and per-ID digests; object-decides mask semantics; rule precedence; lossless EXR only; float data sequences at exact aspect; Cryptomatte name binding and `id_type`; tier C depth semantics and gravity; closed-schema versioning; malformed files reported, not crashed on; 5-digit segment ids, NDA grade list, tier C DOF off, engine-aspect check). Three examples: `tierA_still_clubhouse` (2.39:1 deliverable inside 16:9 overscan, NDA custody), `tierA_motion_drive_in` (125-frame dolly with clay, ID and Z sequences), `tierC_client_plate` (5504×3072 client plate, estimated geometry).
