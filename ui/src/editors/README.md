# Node editors (T47)

One editor per node kind that has parameters: Mask, CropPair, Motion, People, Set, LightMix, Recolour, Light and Scout,
plus the video player the Motion editor uses. `editorFor(kind)` (in `index.ts`) hands App the editor of the selected
node; each editor is keyed by node id, so no local state leaks from one node to the next. Editors read and write
`node.data.params` of the graph store (`params.ts`); the Make bar and the daemon read the same params. Tests:
`ui/src/test/editors.test.tsx` (fetch stub, no daemon, no GPU).

| file | what |
|---|---|
| `MaskEditor.tsx`, `maskMath.ts` | Cryptomatte pick, brush paint, SAM click, OCR text lock; the paint raster and its PNG encoder |
| `CropPairEditor.tsx`, `cropBoxes.ts` | centre picker, zoom sliders, live rectangles; `cut_crops` geometry in TypeScript |
| `MotionEditor.tsx`, `movePolicy.ts` | move ladder, dials, router card, override, Quote / Make; the Lensbook risk ladder |
| `VideoPlayer.tsx`, `timeMap.ts` | HTML5 player with frame stepping, time map readout, landing / lock / drift chips |
| `PeopleEditor.tsx` | cast, behaviour, light, mode, ground-point picker, person depth |
| `SetEditor.tsx` | hero select, material targets table (Lab on the hero, ΔE00 per view), cross-view p90 chip |
| `LightMixEditor.tsx` | gain sliders seeded by NNLS, mood presets |
| `RecolourEditor.tsx`, `colour.ts` | material pick, RAL / hex target, expected ΔE00; RAL table and colour helpers |
| `LightEditor.tsx` | reference image, MaxGaffer locks, Match in Max, Vantage preview |
| `ScoutEditor.tsx` | pose count, keep toggle |
| `ImageStage.tsx`, `widgets.tsx`, `theme.ts`, `params.ts`, `api.ts` | shared stage (pointer to image pixel), widgets, palette, node params, daemon calls |

## Node params each editor owns

| kind | params |
|---|---|
| Mask | `ids` (string[]), `idInfo` ({id, name, class}[]), `maskHash` (painted PNG mask in the CAS), `samHash`, `samPoints` ([x, y][]), `textLock` |
| CropPair | `cx`, `cy` (image pixels), `zoomStart`, `zoomEnd` (1.0 <= start < end <= 3.0) |
| Motion | `move` (a risk-ladder key), `duration`, `engine_id`, dials `revealM`, `orbitDeg`, `sliderCm`; the node's `riskLabel`, `overrides` and `labels` are set through the store |
| People | `cast`, `behavior`, `placement` {x, y}, `light`, `personDepthM`, `depthSource` ("z" or "manual"), `mode` ("i2v", "retake", "wan-edit") |
| Set | `hero` (the hero's packet id) |
| LightMix | `lights` (cached names), `gains` ({light: gain}), `preset`, `targetHash` |
| Recolour | `material` (m: id), `materialName`, `target` (`RAL 7016` or `#383E42`), `targetHex` |
| Light | `referenceHash`, `camera`, `locks` (MaxGaffer parameter keys, sorted), `deep` |
| Scout | `count` (1 to 24), `keep` |

`nodes/kinds.tsx` already reads `ids`, `textLock`, `zoomStart`, `zoomEnd`, `move`, `engine_id`, `cast`, `target` and
`count` for the node's summary line.

## Routes the editors call

All under `/api/v0`, through `fetchJson` (bearer token). A route a daemon does not have yet answers 404; the editor shows
the refusal in a notice and stays usable by hand.

| call | used by | owner |
|---|---|---|
| `POST /pixels/idpick {packet_id, x, y, kind?: "object" or "material"}` -> `{id, name, class, kind?, editable?, depth_m?}` | Mask, People, Recolour | T47 spec; `depth_m` (packet Z at the pixel) is optional and feeds the People ground-point picker |
| `POST /pixels/segment {packet_id, points: [[x, y], ...]}` -> `{mask_hash?, segments}` | Mask (SAM click) | T47 spec: the daemon proxies the pixels worker's `/segment` (that route takes file paths) |
| `POST /pixels/ocr {packet_id}` -> `{boxes: [{bbox: [x0, y0, x1, y1], text, conf}], shape: [h, w]}` | Mask (text lock) | **no task owns it**: the daemon proxies the pixels worker's `/ocr` |
| `POST /assets?kind=mask` (body = PNG bytes, `Content-Type: image/png`) -> `{hash}` | Mask (Save mask) | **no task owns it**: stores a mask in the project CAS |
| `POST /projects/{pid}/nodes/{node}/preview {what, params}` | Motion, Set, LightMix, Recolour | **no task owns it** (T43 for `router`, T48 for the rest); numeric, no spend, no engine |
| `POST /plugin/jobs {action, ...}` -> `{id, state}` | Light | T48 owns the plugin queue (`GET /plugin/jobs`, `POST /plugin/jobs/{id}/result`); creating a job is the missing half |
| `POST /projects/{pid}/nodes/{node}/quote`, `POST /runs/{id}/approve` | Motion (Quote / Make) | T25, through the Make store the Make bar uses |
| `GET /runs/{id}/gate` | Motion (video chips and time map) | T25 |
| `GET /assets/{hash}`, `?fmt=thumb` | every stage, the player | T25 |

### `preview` bodies

| `what` | `params` | response |
|---|---|---|
| `router` | `{move, duration, engine_id?, revealM?, orbitDeg?, sliderCm?}` | `RouterVerdict` JSON: `{mode, verdict, numbers: {D_px_median, disocclusion_fraction, orbit_deg, detail_verdict, crossings}, explanation}` |
| `set` | `{hero, views: [packet_id, ...]}` | `setgate.run` JSON (or `{report}`): `targets` keyed by material id, `per_view[]`, `per_material`, `cross_view_p90`, `verdict` |
| `lightmix` | `{}` (learn the light selects) or `{target_hash}` (NNLS seed) | `{lights: [...], gains: {name: gain}, residual_rms?, clipped?: [...]}` |
| `recolour` | `{material_id, ral?: "7016", hex?: "#383E42"}` | `{method, expected_de00, degraded?, metal?}` |

### Plugin jobs

`light.match {packet_id, camera?, reference_hash, locks, deep}`, `light.vantage_preview {packet_id, camera?}`. The Scout
editor queues nothing: Make runs the Scout node, which enqueues `scout {count, keep}` (T48).

## Make and the Motion editor

The Motion editor's Quote and Make buttons use `useMakeStore` for the same node as the Make bar, so the bar shows the
same quote and its approval dialog. Make is disabled while:

- the move is high or highest risk, no override is logged on the node and the typed reason is shorter than 10 characters
  (Make logs the override first, then approves; the node gets the `unseen geometry guessed` label);
- the router verdict is `render_move` in blocking mode (no override lifts this; render the move in V-Ray).

The editor also writes the move's risk to `node.data.riskLabel`, which the canvas rule `HIGH_RISK_MOVE` reads.

## Not done here

- The video player fetches a whole CAS clip into a blob (a `<video src>` cannot send the bearer token) and only on
  request. A proxy rendition from `GET /assets/{hash}?fmt=proxy` would avoid holding a 4K clip in memory.
- Paint is at working resolution (longest side 1024); the daemon scales the PNG to the plate with nearest neighbour.
- No Windows- or browser-only behaviour was verified on a real GPU browser (jsdom only): pointer capture while painting,
  the clip blob and the key handling of a focused `<video>` need one look in a browser (T53 UI smoke).
