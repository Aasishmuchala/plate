# The 3ds Max plugin (`plugins/max/plate_max`)

`plate_max` is the sender: it reads the camera and scene through pymxs, derives the ID legend and the AI-edit policy, writes a Shot Packet folder ([`spec/PACKET.md`](../spec/PACKET.md)) and tells the daemon where it is over loopback HTTP. It runs inside Max's Python 3.11 and imports the **standard library only** (a test enforces it). `import pymxs` happens in exactly one place, inside `rt.runtime()`, so everything imports and tests off-Max with a fake runtime (`plugins/max/tests/fake_pymxs.py`). Nothing here has run inside Max yet: the plugin is tested against a fake `pymxs` (`plugins/max/tests/fake_pymxs.py`), and the items under "Unverified on a Max box" plus `plugins/max/ONBOX.md` are the checklist that turns the candidate names into facts. How an artist uses it is in the [artist manual](artist-manual.md) section 4.

## Module map

| module | what it does |
|---|---|
| `__init__.py` | `PLUGIN_VERSION`, `PACKET_VERSION` (`0.1.1`, the `X-Plate-Packet-Version` header and `manifest.packet_version`), `SENDER` |
| `rt.py` | `runtime()` (lazy pymxs, or the injected fake), `set_runtime()` |
| `config.py` | app-data folder, `config.json` load/save, `packets_dir()`, the optional `project_id()` |
| `fsutil.py` | temp-then-`os.replace` writes with retry, the JSON byte form, canonical JSON for hashing |
| `digest.py` | `camera_digest`, `scene_digest`, `scene_hash`, `scene_nodes`, `scene_globals`, the Max-matrix conversion |
| `legend.py` | murmur3 and the Cryptomatte hash, `build_id_legend` |
| `policy.py` | class rules, `derive_policy`, `lint` and `lint_scene` (problem codes `NO_VRAY`, `SCALED_CAMERA`, `SHIFTED_CAMERA`, `HIDDEN_CAMERA`, `BAD_NAME_CHARS`, `UNLABELLED_SHARE`, `DUPLICATE_NODE_NAME`, `DUPLICATE_CAMERA_NAME`, `BAD_SHOT_SLUG`, `BAD_LENS`, `BAD_POSE` ...) |
| `autotag.py` | a guess at a node's class from the words in its name, material and layer (a suggestion for the artist, never applied silently) |
| `recipes.py` | per-camera recipes (engine, preset, safe frame, overscan, editable classes, opt-in elements) kept in `<scene>.plate.json` (under `<app data>/sidecars/` for a scene name too long to sit beside it) |
| `packet_id.py` | `new_packet_id()` (`pkt_` + ULID) |
| `packet.py` | `build_camera_block`, `build_manifest`, `write_packet`, the hash-payload helpers |
| `exr_header.py` | reads V-Ray's multilayer EXR header, Cryptomatte manifests and rank-0 coverage (the legend's `pixel_share`) with the standard library only |
| `transport.py` | `handshake`, `session`, `post_packet`, `follow_ingest`, `hash_files` / `hash_payloads`, and the bearer calls `get_json`, `post_json`, `get_bytes` |
| `jobs.py` | the dock's job poller: claims the canvas's plugin jobs (`light.match`, `light.vantage_preview`, `scout`), runs them in the open scene and answers |
| `kit_ingest.py` | turns the client packet kit's folder plus the client's plate into a Tier A packet |
| `scout_poses.py` | pure geometry for the Scout action's temporary cameras |
| `bootstrap.py` | `register_macro` (the `PLATE_Dock` macro) and `launch()`, which opens PLATE's tab of the shared dock through `ui/dock.show_dock()`; Qt is imported lazily |
| `shared/` | the shared Max bridge: one dock with a tab per tool, one set of Max callbacks, the PySide6 collision guard (`plugins/max/SHARED_BRIDGE.md`) |
| `bridge/` | every pymxs touch: `scene`, `colour`, `elements`, `render`, `motion`, `safety` (backup, one undo record, journal), `hashing` (BLAKE3 from the daemon), `vantage`, `camera_level` (level-and-crop of two-point cameras) |
| `actions/` | Send Packet, Batch Cameras, Send Motion, Light (MaxGaffer), Scout, Receive |
| `ui/` | the PySide6 dock (`dock.py`, plus `max_main_window()`) over a controller (`controller.py`: actions, project binding, the poller's factory) that a fake replaces in tests; Qt is allowed only here |

## Camera convention (`plate-cam-v0`)

Max stores a transform as a `Matrix3` in row-vector form: `row1`, `row2`, `row3` are the node's local X, Y and Z axes in world space and `row4` is its position. A Max camera already looks down local -Z with +Y up, so the plate-cam-v0 `world_from_camera` (4x4, row-major, column vectors) is a transpose and nothing else:

```
M[i][j] = row(j+1)[i]   for i in 0..2, j in 0..3,   M[3] = [0, 0, 0, 1]
```

The pose is read from the node's **`objecttransform`**, not `transform`: V-Ray renders from the object, and a camera whose pivot was moved with Affect Pivot Only has a different `transform`. If `objecttransform` cannot be read the pose is `None` and Send is refused; there is no fallback to the pivot. The time slider must sit on the hero frame when `camera_digest` runs (the digest records the frame it read, `hero_frame`). `fx = focal * zoom * width / film_width`, `fy = fx * pixel_aspect`, the principal point is the frame centre, and `fx` and the FOVs are written at full float precision because the spec checks `fx` to 0.01% and the FOV to 0.005 degrees. `plate_spec.world_from_camera_from_max_rows` and `plate_spec.cryptomatte_id` are the reference implementations; the tests compare against them on random inputs.

Send **refuses** (a `ManifestError` with the reason) a camera that is shifted, tilted (V-Ray vertical/horizontal tilt or auto vertical tilt), distorted, scaled, sheared or mirrored, a partial safe frame without a matching `overscan.engine_aspect`, an unknown colour mode, units the schema cannot express, and an NDA custody floor without `allowed_grades` limited to C0/C1. The one exception is a two-point camera (vertical tilt correction, or a vertical lens shift), which Send renders as a level pinhole and crops: see the next section.

## Level-and-crop: two-point perspective cameras (decision 58)

V-Ray's vertical tilt correction (`auto_vertical_tilt`, or a `vertical_tilt` value) keeps the image plane vertical while the camera is pitched, and a vertical lens shift slides the image across the film. Neither is a v0.1 pinhole, but both are a crop of one. With `recipe.level_and_crop` on (the default), Send does this instead of refusing (`bridge/camera_level.py`):

- **The level camera.** Same position, same heading (yaw) and roll, pitch zero. A shifted camera keeps its whole pose: its image plane is already square to its axis, only the principal point moved.
- **The offset.** `dy_px = fy · tan(pitch)` for a tilt-corrected camera (pitch > 0 looks up), `dy_px = s · height` for a manual vertical shift `s`.
- **The render.** `width × (height + 2·ceil(|dy_px|))`, principal point at the centre. The width and the film gate width do not change, so `fx` does not either (checked against `FX_TOLERANCE`); the film gate height grows with the frame.
- **The safe frame.** `{x: 0, y: ceil(|dy_px|) − dy_px rounded to a whole pixel, width, height}`. A positive `dy_px` (looking up, or a view raised by the shift) puts the frame at the top of the level render. The rounding costs at most 0.5 px. The sign comes from `tests/test_camera_level.py`: five world points projected through the tilt-corrected camera (a pinhole with the level pose whose principal point puts the pitched aim point at the frame centre) land where the level camera puts them, minus the safe-frame origin, within 0.5 px, at pitches of 2°, 3.55°, 8° and −3.55°. A flipped sign fails it.
- **The packet.** The manifest `camera` is the level one, under the artist's camera name, with zero tilt and shift, a partial safe frame and an `overscan` block. Its `engine_aspect` is the level render's aspect, and its `note` starts with `level-and-crop from a tilt-corrected camera: pitch X deg, dy Y px` (or `... from a vertically shifted camera: shift s ...`). Every gate then runs on a v0.1 packet. A recipe overscan aspect still applies on top: the level render is grown to it, and the frame moves with the offset.
- **The scene.** The artist's camera is never touched. Send renders a temporary copy (`rt.copy`, named `__PLATE_level_<camera>`, targeted off, vertical tilt and auto tilt off or every vertical-shift property zero), posed so that its `objecttransform` is the level pose at the hero frame (`transform' = transform · objecttransform⁻¹ · level`, so a moved pivot is kept). The copy is checked through its own digest before the render and deleted afterwards. A copy whose pose does not take (an animated or constrained camera) stops the Send at `render` with the reason.
- **Lint.** `policy.lint_scene(camera, level_and_crop=True)` reports such a camera as a `SHIFTED_CAMERA` **warning** (`LintReport.warnings`, shown in the dock's lint panel without blocking). Without the flag, or with `level_and_crop: false`, the refusal stays. Distortion, a horizontal shift or tilt, tilt and shift together, a pitch beyond 45°, and a rolled camera stay refused, and the lint line says why.
- **Where the flag comes from.** `CameraRecipe` has no `level_and_crop` field yet. Send reads it from the recipe object when its type has one, else from a recipe dict passed in `options.recipes[camera]`, else from `options.level_and_crop`, and defaults to true.

**Box measurements (open question 14).** V-Ray's unit for the vertical shift `s` is **unverified**: until it is measured, `s` is read as a fraction of the frame height, and the manifest's `overscan.note` says so. On the conformance scene, measure where the principal point moves for `vertical_shift = 0.1` and write the unit into `camera_level.SHIFT_UNIT` and the `dy_px` formula. Also measure: whether the sign of a positive shift raises the view; whether a manual `vertical_tilt` value is the full correction of the pitch (what Guess vertical tilt sets: it is read that way now); and, for a V-Ray Physical Camera with auto vertical tilt, whether the level-and-crop packet's safe-frame crop matches the tilt-corrected render within 0.5 px (the reprojection exit).

## The ID legend and the policy

Ids are `o:<8 hex>` (object, hash of the node name) and `m:<8 hex>` (material, hash of the material name): MurmurHash3_x86_32 with seed 0 and the Cryptomatte exponent fix (bit 23 flipped when the exponent byte is 0 or 255). The manifest records `id_type` `node_name` / `material_name`. Every entry carries a `digest` (BLAKE3 of the node's or material's record) and `pixel_share`; `pixel_share` comes from the render (the Cryptomatte EXR), so the render step passes it per node. `build_id_legend` takes **only the nodes visible in the camera**.

Locked by default. A node is editable only when **all** of these hold: its layer starts with one of `VEG_`, `SKY`, `PEOPLE`, `WATER`, `ENT_`, `GRASS`; it has no `architecture`, `glazing` or `signage` tag (user property `plate_tags`, comma separated); its class is not architecture, glazing, signage or other; and it is not unlabelled. A material is editable only if every visible object that uses it is. Everything else is locked. A node on no layer (or Max's default layer `0`), or on a layer no class rule matches, is **unlabelled** (class `other`, locked). A never-editable tag overrides the layer's class (`class_source: manual`). The class rules (`policy.CLASS_RULES`) are written into the manifest so `plate_spec.validate_legend` re-checks every `rule` entry.

`lint` blocks Send when unlabelled ids cover more than `max_unlabelled_share` (default 0.05, schema cap 0.25), when an id is both locked and editable, or when an unlabelled id is editable. Duplicate visible node names are caught first, at the lint step, as `DUPLICATE_NODE_NAME` (next section); if one ever reached the legend, `legend_nodes` and `build_id_legend` raise `LegendError` for it, as they do for two different materials sharing a name (they would merge into one id with one class), and never let the last node of that name win. Coverage is `labelled + unlabelled + uncovered = 1`; the uncovered part (environment background) is always locked.

## What Send checks before it renders

`policy.lint_scene` runs at the `lint` step, before the first render (`renders == 0`), and blocks Send on the codes below as well as the older ones. Each would otherwise cost a full render and end in a packet that is wrong or cannot be written.

| code | blocks Send when | what to do |
|---|---|---|
| `DUPLICATE_NODE_NAME` | two visible geometry nodes share a name; the detail lists their layers | rename one. A name is one legend id, so a locked wall and an editable tree sharing a name would merge into one id, and which class it got used to depend on scene order. Locked geometry must never become editable by accident |
| `DUPLICATE_CAMERA_NAME` | two camera nodes share a name | rename one: the camera name is the shot's identity |
| `BAD_SHOT_SLUG` | the camera name cannot be turned into a shot slug that stands for it: it has no Latin letter or digit at all (Arabic, emoji, only spaces), or only a fragment of it survives (`客户A` becomes `a`) | rename the camera with ASCII letters, digits, `_` and `-` |
| `BAD_LENS` | the focal length, film gate, zoom, f-number, ISO or shutter is zero, negative or not a finite number; the detail names the property. A non-positive film gate is an error, no longer an assumed 36 mm (the "assumed" fallback is only for a property that cannot be read at all) | fix the camera |
| `BAD_POSE` | any of the 16 numbers of `world_from_camera` is NaN or infinite, or the rotation is not orthonormal within tolerance (`rotation_error`; a NaN now fails it) | fix or re-create the camera; look for a broken constraint or controller |

The shot slug and the project id are worked out before the render, not after. A Send that stops at `manifest` or later and left a packet folder without a `manifest.json` removes that folder, so a failed Send leaves no orphan packet (the option `keep_failed_packets` keeps it for debugging). **A daemon failure after the render removes the folder too**, whichever stage it hit (`legend`, `hash`, `manifest`, `post`, `ingest`): a dropped connection (`DaemonUnreachable`), a timeout (`DaemonTimeout`) or any other `TransportError` that the call's retries did not recover ends the camera, and the warning reads `the packet folder <folder> was removed: the daemon failed at <stage> (<error>) after the render; re-send when it is back (set keep_failed_packets to keep failed renders)`. A problem found in the scene after the render, such as the unlabelled-share abort at `legend`, keeps its render for inspection as before, and with `keep_failed_packets` the folder stays whatever the cause. Manifests and sidecars are written with `allow_nan=False`: a NaN that got past every check raises and aborts the Send at `manifest` instead of writing JSON that is not JSON.

## Hashes: BLAKE3 comes from the daemon

The schema wants BLAKE3 for every file, for `scene_hash`, for `view_hash` and for every legend `digest`, and BLAKE3 is not in the standard library or in Max's Python. The plugin therefore never computes one: it asks the loopback daemon (`POST /api/v0/hash`, token-authenticated, paths confined to the packets folder, payload bytes accepted for the small JSON documents; `bridge/hashing.py`) and the daemon **rehashes everything again on ingest**, so a wrong hash can only get a packet refused, never accepted. `file_ref(...)`, `build_manifest(scene_hash=, view_hash=)` and `build_id_legend(digest_fn=)` take values (or the hash function) from the caller, and the tests supply them with the dev venv's `blake3`. The helpers give the caller the exact bytes to hash: `legend_bytes`, `camera_frames_bytes`, `scene_hash_payload`, `view_hash_payload`. Rejected alternatives: a `sha256` option in the schema (two algorithms forever) and a pure-Python BLAKE3 inside Max (minutes per 400 MB EXR).

Digest formats: `scene_hash` is `content-v1` (`digest_version` `1`): one canonical text line per geometry node, light, the environment, the units and the render setup, sorted, written into the packet as `scene_digest.txt` and hashed by path; a scene with no digest lines falls back to `file-meta-v0` (the `.max` file's name, size and mtime, `digest_version` `0`). The rest is still the interim `digest_version` `0`: a node's `digest` covers its name, layer, material, tags and `objecttransform`; a material's covers its name; `view_hash` covers the camera block, the colour block, the pixel-affecting render settings (sampler, noise threshold, max subdivs, denoiser; not timings), `scene_globals()` (light names, classes, transforms, on/multiplier; environment map; renderer) and the node digests. Shader parameters and full light parameters are not in those yet, so an edited material parameter does not change a legend digest or the `view_hash`.

## Writing a packet

The dock's **Send Packet** does all of this per camera (`actions/send_packet.py`): detect the colour mode, lint the scene, render the beauty EXR and the VFB-corrected TIFF16, read the EXR back, build the legend from the Cryptomatte manifest, hash through the daemon, write the manifest last, post it. The pieces, for scripting:

```
cam   = digest.camera_digest("CAM_x")        # time slider on the hero frame
scene = digest.scene_digest()
leg   = legend.build_id_legend(visible_nodes_with_pixel_share, digest_fn=daemon_blake3_hex)
pol   = policy.derive_policy(leg)
files = {...}   # packet.file_ref(...) per file, hashes from the daemon
m     = packet.build_manifest(cam, scene, files, leg, pol, project_id, shot_id,
                              host, renderer, colour, render, scene_hash=..., view_hash=...)
packet.write_packet(config.packets_dir() / m["packet_id"], m, leg)
transport.post_packet(that_folder, config.load_config())
```

`write_packet` writes `id_legend.json` (and `camera_frames.json` for motion) first and `manifest.json` last, each as temp-then-`os.replace` with retry and backoff, UTF-8, LF, no BOM. It refuses to write a sidecar whose size differs from the manifest (hash the bytes from `legend_bytes()` / `camera_frames_bytes()`). The beauty EXR, display TIFF and sequences are written by the render step; their names must avoid `< > : " \ | ? *` and may not differ only by case.

## Transport (what the dock will show)

`transport.handshake(daemon_url)` calls `GET /api/v0/health` before Send and lists `problems` when `0.1` is not in the daemon's `packet_versions` (the daemon lists major.minor versions; the plugin sends `0.1.1`). `transport.post_packet(folder, config)` posts `{"packet_path": <absolute path>, "sender": {"name": "plate_max", "version": "0.1.0"}}` to `/api/v0/packets` with `Authorization: Bearer <token>` and `X-Plate-Packet-Version: 0.1.1`. Proxies are disabled for these calls.

| daemon answer | `post_packet` | the dock shows |
|---|---|---|
| 202 `{packet_id, status: ingesting, events_url}` | returns `status_code 202`; Send follows `events_url` for up to 5 s | "Sent, ingesting", "Sent, ingested", or "Ingest failed: <errors>" |
| 200 `{status: known}` | returns 200 | "Already sent" (idempotent re-send); a 200 or 202 whose body is not the daemon's JSON object is `UnexpectedStatus`, never "Already sent" |
| 409 | returns 409 | "This packet id exists with different content: rebuild the packet" |
| 422 `{errors: [...]}` | returns 422 | the error list from `validate_packet_dir` |
| 426 `{supported: ["0.1"]}` | returns 426 | "Update the plugin or the daemon: versions do not match" |
| 401 | raises `Unauthorized` | "Token rejected: re-run the installer or fix config.json" |
| no answer | raises `DaemonUnreachable` | "Daemon not running at <url>" |
| anything else | raises `UnexpectedStatus` | the status and body |

## `config.json`

`%LOCALAPPDATA%\Plate\config.json` on Windows, `~/Library/Application Support/Plate/config.json` on macOS (`~/.local/share/Plate` elsewhere). `PLATE_HOME` moves the folder, `PLATE_CONFIG` names the file. Keys: `token`, `daemon_url` (default `http://127.0.0.1:4610`), optional `project_id` (the fallback project of a scene whose `<scene>.plate.json` names none), `packets_dir` (a drive or UNC path; default `<app-data>\packets`), `repo_path` (written by the installer), and `plate_max_installed_keys` (the installer's record for uninstall). The daemon creates the file when it is missing; the installer writes it BOM-less so Python's `json` reads it. The poller caches reference pictures under `<app-data>\cache\references\<hash>.<ext>`.

## The dock

`PLATE_Dock` (the macro) calls `bootstrap.launch()`, which calls `ui/dock.show_dock()`: PLATE opens as the `PLATE` tab of the shared dock (`shared.launch_plate(controller, parent=max_main_window())`, one `PlateSharedDock` for PLATE, MaxDirector and MaxGaffer; see `plugins/max/SHARED_BRIDGE.md`). A second click raises the same dock and tab. `PLATE_STANDALONE_DOCK=1` opens the old floating `PlateDock` instead, for side-by-side checks on the box; only one PLATE dock is ever open.

- **Project.** The combo lists the daemon's projects (`GET /api/v0/projects`). Pick one, or type a new id (it is slugged; a first packet creates the project on the daemon with custody unset). The choice is kept in `<scene>.plate.json` next to the `.max` file under `project_id`, next to the recipes. Without one, the binding is `config.json`'s `project_id`, then the `.max` file's stem as a slug. A stem that makes no usable slug (no Latin letter or digit, or only a fragment of a longer name, such as `客户A_别墅.max` or a name of spaces) is not used: Send is disabled and the dock says `pick a project in the Project combo: '<stem>' cannot name one` (`ProjectUnresolved`), so a scene never silently binds itself to a project called `a`. An unsaved scene keeps the choice until another scene opens, and binds to `untitled` until then. A `<scene>.plate.json` that exists but cannot be read (a trailing comma, a UTF-16 byte-order mark, an empty file, merge-conflict markers, a `project_id` that is not a string) is never treated as "no binding" and never overwritten: the dock shows `<scene>.plate.json is unreadable (<reason>): fix or delete it before sending` and disables Send, and choosing a project or saving recipes raises the same error and leaves the file byte for byte as it was (`SidecarUnreadable`). **A long scene name** (a `<scene>.plate.json` whose file name passes 255 bytes, or, on Windows, whose path passes 259 characters) keeps its sidecar under `<app data>\sidecars\<hash>.plate.json` instead, the hash being of the scene's full path, and the dock logs once `sidecar stored under <folder> (the scene name is too long for one beside it)`. Binding, recipes and Send work exactly as for any other scene. The safety backup follows the same rule: `<scene>.plate-backup.<time>.max` beside the scene, or, for a name that does not fit, `plate-<hash>.plate-backup.<time>.max` in the system temp folder, and the backup's log line names where it went. A sidecar that cannot be written for another reason (a read-only folder, a full disk) is a message of the same kind as above, not a bare OS error. Every send carries `project_id`, and `custody` (`{nda, allowed_grades}`) once the project's custody is set. The label beside the combo shows the custody. After a send to a project whose custody is unset it turns amber: `custody unset (C0 only)`, because only local C0 engines run until someone sets it in the canvas's Project panel.
- **Recipe.** Engine and Preset are labelled `used by Send + quote`. They leave Max only through Send + quote. A plain Send Packet carries neither, and the canvas's Make bar picks the engine for every other edit.
- **Send Packet / Batch Cameras.** Health and version check, lint, then the send. The log says `Sent, ingesting`, `Sent, ingested`, `Already sent` or `Ingest failed: …` per camera. Tick `supersedes last` to make each new packet supersede the camera's last packet sent from this dock (approved layers carry over where the IDs are unchanged).
- **Send + quote** (beside Send Packet, decision 53). It runs Send Packet first. Then, for each camera whose packet the daemon ingested (its event log is followed for up to 60 s while it is still ingesting), it posts `POST /api/v0/projects/{pid}/templates/apply {template_id: "edit_chain", packet_ids: [id], engine_id, recipe (the Preset; left out when empty), quote: true, auto_approve}`. The daemon lays an Edit chain (Plate, Mask, Edit, Judge, Export) on the canvas and quotes the Edit. The log adds one row per camera: `quoted <usd> USD · waiting for approval`, `approved · running`, or `not quoted: <why>` (a failed send or ingest, an ingest still running after the wait, a refused quote, the daemon's error). `auto-approve under cap` is off by default. Ticked, it asks the daemon to approve each quote under the project's per-run auto cap; a quote over the cap stays waiting, and the row says `(auto-approve refused: auto_cap)`. Nothing is spent until a quote is approved.
- **Send Motion.** The focused camera's move over the scene's animation range, as a motion packet of its hero still: the last still this dock sent of the camera, else one the artist picks from the camera's stills of this project in the packets folder (newest first; a packet id can be pasted).
- **Light.** Asks for a reference picture, then MaxGaffer's match for the focused camera, the best state applied, and a re-rendered packet that supersedes the camera's last one (`actions.light.run`: `light.match`, `apply`, `rerender_packet`). Without MaxGaffer in this Max the button is disabled and its tooltip starts with `MAXGAFFER_MISSING`.
- **Scout.** The scout poses as temporary `__PLATE_scout_N` cameras with low-resolution V-Ray previews; tick `keep scout cameras` to leave them in the scene. No packet is made from the dock's Scout: a scout pose becomes a plate only when it is sent.

## Canvas jobs (`jobs.py`)

A Light or Scout node on the canvas cannot run in the daemon: it queues a plugin job, and the dock's poller picks it up. The poller runs on the dock's `QTimer` every 5 s. It starts with the dock and stops when the dock closes, so canvas jobs reach Max only while the dock is open (`canvas jobs: on` in the progress card).

| step | what happens |
|---|---|
| claim | `GET /api/v0/plugin/jobs?claim=1&since=<cursor>&limit=1`: one job at a time; the others stay queued |
| `light.match` | the reference picture `GET /api/v0/assets/{reference_hash}` into the cache (once per hash), then `light.match` with the job's `locks` and `deep`, `apply`, and `rerender_packet` with `supersedes` = the job's packet, sent into the job's project |
| `light.vantage_preview` | `light.vantage_preview(camera)`: the camera into the viewport, the Vantage live link toggled |
| `scout` | `actions.scout.run` with views `0..count-1` and `keep`; kept cameras are also sent as full packets |
| answer | `POST /api/v0/plugin/jobs/{id}/result {ok, detail?, packet_id?, metrics?, poses?, packets?}` |

A failing or raising action answers `ok: false` with the text, and nothing reaches Max as an exception. A Light or Scout job is checked against the open scene when the dock can tell. A job whose packet came from a different `.max` than the open one fails with that reason, and so does a job whose project differs from the project the open scene is bound to, so the wrong scene is not relit and a result is not sent into the wrong project. When the dock cannot compare, it does not guess either way: it runs the job and logs a visible warning, `packet <id>: scene check skipped (<why>)`. A job also fails when MaxGaffer is missing, the camera is not in the scene, or the reference cannot be fetched. While a job runs the dock is busy, and a tick waits while the artist is in an action, a daemon call is in flight, or a dialog is open. An answer the daemon missed is posted again first on the next tick. A daemon that does not answer is asked less and less often, down to once a minute, and the error is logged once.

## Install and uninstall (PowerShell, Windows)

```powershell
powershell -ExecutionPolicy Bypass -File plugins\max\scripts\install.ps1      # or double-click install.bat
powershell -ExecutionPolicy Bypass -File plugins\max\scripts\uninstall.ps1
```

`install.ps1` copies `plugins\max\startup\plate_startup.py` into `%LOCALAPPDATA%\Autodesk\3dsMax\2026 - 64bit\ENU\scripts\startup` and writes `token`, `daemon_url` and `repo_path` into `config.json`; `uninstall.ps1` removes the startup file and exactly the keys the installer added. The startup script puts `<repo_path>\plugins\max` on `sys.path`, imports `plate_max`, prints one version line and registers nothing else; any failure is printed and swallowed so Max still starts. Details, the Safe Scene Script Execution check and the delivery-week rule are in `plugins/max/scripts/README.md`. The PowerShell scripts were written without a Windows PowerShell to run them on this Mac: run them once on the box before relying on them.

## Unverified on a Max box (P0 spike list)

- `objecttransform` on a V-Ray Physical Camera is the pose V-Ray renders from (the vertex-reprojection exit checks it).
- Level-and-crop: `rt.copy(node)` of a (targeted) V-Ray Physical Camera, `targeted = false` on the copy, `copy.transform = matrix3 ...` landing as the level `objecttransform`, `delete` taking the copy (and any target) away; the vertical shift unit and sign, and the meaning of a manual `vertical_tilt` (see the box measurements above).
- The property names read for lens shift (`horizontal_shift`/`horizontal_offset`, `vertical_shift`/`vertical_offset`), tilt (`vertical_tilt`, `horizontal_tilt`, `auto_vertical_tilt`/`guess_vertical_tilt`) and distortion (`distortion`, `distortion_type`). A name that cannot be read is a digest warning and counts as zero, so check the warnings on the first real camera.
- `shutter_speed` is 1/seconds, `ISO`, `f_number`, `focus_distance`, `film_width`, `focal_length`, `zoom_factor`, `use_dof` on the V-Ray Physical Camera; `white_balance` is a Kelvin number (a colour swatch is sent as neutral).
- `rt.units.SystemType` / `SystemScale`, `rt.currentTime`, `rt.animationRange`, `rt.frameRate`, `rt.getHandleByAnim`, `rt.getUserProp(node, "plate_tags")`, `rt.lights`, `rt.environmentMap`, `rt.renderers.current`.
- Cryptomatte material names for Multi/Sub-Object materials (the legend uses one material name per node).
- A scene saved with a 250-character name binds, keeps its recipes under `<app data>\sidecars\` and sends, and its safety backup lands in the temp folder (the tests only simulate the 255-byte and 259-character limits).
- A daemon restarted in the middle of a Send removes the half packet (the folder is gone, the warning names the stage) and a re-send works once the daemon is back.
- The Send preflight (`DUPLICATE_NODE_NAME`, `DUPLICATE_CAMERA_NAME`, `BAD_SHOT_SLUG`, `BAD_LENS`, `BAD_POSE`) on a real merged scene that has duplicate names, a non-Latin camera name and a degenerate lens: the checks have only met the fake `pymxs` scenes so far.

## What is not done

- Everything under "Unverified on a Max box" and `plugins/max/ONBOX.md`: candidate property names for V-Ray and Vantage, the colour manager API, `vfbControl` commands for Receive, the live-link entry points.
- On the box: a Light match queued from the canvas reaches Max through the poller, re-renders and supersedes the old packet; Send Motion from the dock on a 25-frame move; the poller's `QTimer` keeps ticking while the dock is a background tab of the shared dock.
- `ENT_` entourage classes beyond the single `furniture` class.
- Digest version 1 (mesh topology, shader parameters, full light parameters in `scene_hash`/`view_hash`): until then an edited material parameter or mesh does not change a digest.
- MaxDirector and MaxGaffer joining the shared dock: PLATE opens through it, the other two need the small adapter in their own repos (`plugins/max/SHARED_BRIDGE.md`).
- A Max 2027 or Blender sender (a later phase).
