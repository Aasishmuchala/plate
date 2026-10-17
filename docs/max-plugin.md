# The 3ds Max plugin (`plugins/max/plate_max`)

`plate_max` is the sender: it reads the camera and scene through pymxs, derives the ID legend and the AI-edit policy, writes a Shot Packet folder ([`spec/PACKET.md`](../spec/PACKET.md)) and tells the daemon where it is over loopback HTTP. It runs inside Max's Python 3.11 and imports the **standard library only** (a test enforces it). `import pymxs` happens in exactly one place, inside `rt.runtime()`, so everything imports and tests off-Max with a fake runtime (`plugins/max/tests/fake_pymxs.py`). Nothing in this scaffold has run inside Max yet; the items under "Unverified on a Max box" are the P0 spike list.

## Module map

| module | what it does |
|---|---|
| `__init__.py` | `PLUGIN_VERSION`, `PACKET_VERSION` (`0.1.0`, the `X-Plate-Packet-Version` header and `manifest.packet_version`), `SENDER` |
| `rt.py` | `runtime()` (lazy pymxs, or the injected fake), `set_runtime()` |
| `config.py` | app-data folder, `config.json` load/save, `packets_dir()` |
| `fsutil.py` | temp-then-`os.replace` writes with retry, the JSON byte form, canonical JSON for hashing |
| `digest.py` | `camera_digest`, `scene_digest`, `scene_hash`, `scene_nodes`, `scene_globals`, the Max-matrix conversion |
| `legend.py` | murmur3 and the Cryptomatte hash, `build_id_legend` |
| `policy.py` | class rules, `derive_policy`, `lint` |
| `packet_id.py` | `new_packet_id()` (`pkt_` + ULID) |
| `packet.py` | `build_camera_block`, `build_manifest`, `write_packet`, the hash-payload helpers |
| `transport.py` | `handshake`, `post_packet` |

## Camera convention (`plate-cam-v0`)

Max stores a transform as a `Matrix3` in row-vector form: `row1`, `row2`, `row3` are the node's local X, Y and Z axes in world space and `row4` is its position. A Max camera already looks down local -Z with +Y up, so the plate-cam-v0 `world_from_camera` (4x4, row-major, column vectors) is a transpose and nothing else:

```
M[i][j] = row(j+1)[i]   for i in 0..2, j in 0..3,   M[3] = [0, 0, 0, 1]
```

The pose is read from the node's **`objecttransform`**, not `transform`: V-Ray renders from the object, and a camera whose pivot was moved with Affect Pivot Only has a different `transform`. If `objecttransform` cannot be read the pose is `None` and Send is refused; there is no fallback to the pivot. The time slider must sit on the hero frame when `camera_digest` runs (the digest records the frame it read, `hero_frame`). `fx = focal * zoom * width / film_width`, `fy = fx * pixel_aspect`, the principal point is the frame centre, and `fx` and the FOVs are written at full float precision because the spec checks `fx` to 0.01% and the FOV to 0.005 degrees. `plate_spec.world_from_camera_from_max_rows` and `plate_spec.cryptomatte_id` are the reference implementations; the tests compare against them on random inputs.

Send **refuses** (a `ManifestError` with the reason) a camera that is shifted, tilted (V-Ray vertical/horizontal tilt or auto vertical tilt), distorted, scaled, sheared or mirrored, a partial safe frame without a matching `overscan.engine_aspect`, an unknown colour mode, units the schema cannot express, and an NDA custody floor without `allowed_grades` limited to C0/C1.

## The ID legend and the policy

Ids are `o:<8 hex>` (object, hash of the node name) and `m:<8 hex>` (material, hash of the material name): MurmurHash3_x86_32 with seed 0 and the Cryptomatte exponent fix (bit 23 flipped when the exponent byte is 0 or 255). The manifest records `id_type` `node_name` / `material_name`. Every entry carries a `digest` (BLAKE3 of the node's or material's record) and `pixel_share`; `pixel_share` comes from the render (the Cryptomatte EXR), so the render step passes it per node. `build_id_legend` takes **only the nodes visible in the camera**.

Locked by default. A node is editable only when **all** of these hold: its layer starts with one of `VEG_`, `SKY`, `PEOPLE`, `WATER`, `ENT_`, `GRASS`; it has no `architecture`, `glazing` or `signage` tag (user property `plate_tags`, comma separated); its class is not architecture, glazing, signage or other; and it is not unlabelled. A material is editable only if every visible object that uses it is. Everything else is locked. A node on no layer (or Max's default layer `0`), or on a layer no class rule matches, is **unlabelled** (class `other`, locked). A never-editable tag overrides the layer's class (`class_source: manual`). The class rules (`policy.CLASS_RULES`) are written into the manifest so `plate_spec.validate_legend` re-checks every `rule` entry.

`lint` blocks Send when unlabelled ids cover more than `max_unlabelled_share` (default 0.05, schema cap 0.25), when an id is both locked and editable, or when an unlabelled id is editable. Duplicate visible node names, and two different materials sharing a name, raise `LegendError` (they would merge into one id with one class). Coverage is `labelled + unlabelled + uncovered = 1`; the uncovered part (environment background) is always locked.

## Hashes: the BLAKE3 gap

The schema wants BLAKE3 for every file, for `scene_hash`, for `view_hash` and for every legend `digest`, and BLAKE3 is not in the standard library or in Max's Python. The plugin therefore never computes one: `file_ref(...)`, `build_manifest(scene_hash=, view_hash=)` and `build_id_legend(digest_fn=)` take values (or the hash function) from the caller, and the tests supply them with the dev venv's `blake3`. The helpers give the caller the exact bytes to hash: `legend_bytes`, `camera_frames_bytes`, `scene_hash_payload`, `view_hash_payload`. `hash_file_sha256` is a stdlib convenience, not what the schema accepts. **Open item for the render-step task:** either `pip install blake3` into Max's Python (the installer does not do this), a small pure-Python BLAKE3 inside `plate_max`, or a daemon-side sealing step; until one exists a Max-written packet cannot pass `validate_packet_dir`.

Digest formats are interim (`digest_version` `0`): `scene_hash` covers the `.max` file's name, size and mtime (`file-meta-v0`); a node's `digest` covers its name, layer, material, tags and `objecttransform`; a material's covers its name; `view_hash` covers the camera block, the colour block, the pixel-affecting render settings (sampler, noise threshold, max subdivs, denoiser; not timings), `scene_globals()` (light names, classes, transforms, on/multiplier; environment map; renderer) and the node digests. Mesh topology, shader parameters and full light parameters join in digest version 1 (P0). Until then an edited material parameter or mesh does not change a digest.

## Writing a packet

```
cam   = digest.camera_digest("CAM_x")        # time slider on the hero frame
scene = digest.scene_digest()
leg   = legend.build_id_legend(visible_nodes_with_pixel_share, digest_fn=blake3_hex)
pol   = policy.derive_policy(leg)
files = {...}   # packet.file_ref(...) per file, hashes from the render step
m     = packet.build_manifest(cam, scene, files, leg, pol, project_id, shot_id,
                              host, renderer, colour, render, scene_hash=..., view_hash=...)
packet.write_packet(config.packets_dir() / m["packet_id"], m, leg)
transport.post_packet(that_folder, config.load_config())
```

`write_packet` writes `id_legend.json` (and `camera_frames.json` for motion) first and `manifest.json` last, each as temp-then-`os.replace` with retry and backoff, UTF-8, LF, no BOM. It refuses to write a sidecar whose size differs from the manifest (hash the bytes from `legend_bytes()` / `camera_frames_bytes()`). The beauty EXR, display TIFF and sequences are written by the render step; their names must avoid `< > : " \ | ? *` and may not differ only by case.

## Transport (what the dock will show)

`transport.handshake(daemon_url)` calls `GET /api/v0/health` before Send and lists `problems` when `0.1` is not in the daemon's `packet_versions`. `transport.post_packet(folder, config)` posts `{"packet_path": <absolute path>, "sender": {"name": "plate_max", "version": "0.1.0"}}` to `/api/v0/packets` with `Authorization: Bearer <token>` and `X-Plate-Packet-Version: 0.1.0`. Proxies are disabled for these calls.

| daemon answer | `post_packet` | the dock shows |
|---|---|---|
| 202 `{packet_id, status: ingesting, events_url}` | returns `status_code 202` | "Sent, ingesting" with a link to the events stream |
| 200 `{status: known}` | returns 200 | "Already sent" (idempotent re-send) |
| 409 | returns 409 | "This packet id exists with different content: rebuild the packet" |
| 422 `{errors: [...]}` | returns 422 | the error list from `validate_packet_dir` |
| 426 `{supported: ["0.1"]}` | returns 426 | "Update the plugin or the daemon: versions do not match" |
| 401 | raises `Unauthorized` | "Token rejected: re-run the installer or fix config.json" |
| no answer | raises `DaemonUnreachable` | "Daemon not running at <url>" |
| anything else | raises `UnexpectedStatus` | the status and body |

## `config.json`

`%LOCALAPPDATA%\Plate\config.json` on Windows, `~/Library/Application Support/Plate/config.json` on macOS (`~/.local/share/Plate` elsewhere). `PLATE_HOME` moves the folder, `PLATE_CONFIG` names the file. Keys: `token`, `daemon_url` (default `http://127.0.0.1:4610`), optional `project_id`, `packets_dir` (a drive or UNC path; default `<app-data>\packets`), `repo_path` (written by the installer), and `plate_max_installed_keys` (the installer's record for uninstall). The daemon creates the file when it is missing; the installer writes it BOM-less so Python's `json` reads it.

## Install and uninstall (PowerShell, Windows)

```powershell
powershell -ExecutionPolicy Bypass -File plugins\max\scripts\install.ps1      # or double-click install.bat
powershell -ExecutionPolicy Bypass -File plugins\max\scripts\uninstall.ps1
```

`install.ps1` copies `plugins\max\startup\plate_startup.py` into `%LOCALAPPDATA%\Autodesk\3dsMax\2026 - 64bit\ENU\scripts\startup` and writes `token`, `daemon_url` and `repo_path` into `config.json`; `uninstall.ps1` removes the startup file and exactly the keys the installer added. The startup script puts `<repo_path>\plugins\max` on `sys.path`, imports `plate_max`, prints one version line and registers nothing else; any failure is printed and swallowed so Max still starts. Details, the Safe Scene Script Execution check and the delivery-week rule are in `plugins/max/scripts/README.md`. The PowerShell scripts were written without a Windows PowerShell to run them on this Mac: run them once on the box before relying on them.

## Unverified on a Max box (P0 spike list)

- `objecttransform` on a V-Ray Physical Camera is the pose V-Ray renders from (the vertex-reprojection exit checks it).
- The property names read for lens shift (`horizontal_shift`/`horizontal_offset`, `vertical_shift`/`vertical_offset`), tilt (`vertical_tilt`, `horizontal_tilt`, `auto_vertical_tilt`/`guess_vertical_tilt`) and distortion (`distortion`, `distortion_type`). A name that cannot be read is a digest warning and counts as zero, so check the warnings on the first real camera.
- `shutter_speed` is 1/seconds, `ISO`, `f_number`, `focus_distance`, `film_width`, `focal_length`, `zoom_factor`, `use_dof` on the V-Ray Physical Camera; `white_balance` is a Kelvin number (a colour swatch is sent as neutral).
- `rt.units.SystemType` / `SystemScale`, `rt.currentTime`, `rt.animationRange`, `rt.frameRate`, `rt.getHandleByAnim`, `rt.getUserProp(node, "plate_tags")`, `rt.lights`, `rt.environmentMap`, `rt.renderers.current`.
- Cryptomatte material names for Multi/Sub-Object materials (the legend uses one material name per node).

## Deferred

The dock and the Send action, render-element setup and the EXR/TIFF writers, reading Cryptomatte coverage from the EXR (`pixel_share`, the uncovered share), motion-packet rendering, Vantage, the real scene digest (digest version 1), BLAKE3 inside Max (see above), and `ENT_` entourage classes beyond the single `furniture` class.
