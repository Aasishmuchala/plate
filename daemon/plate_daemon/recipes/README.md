# recipes

Lensbook's archviz doctrine as data (ported from `~/Desktop/lensbook/lib/data.ts` and `assemble.ts`) and the runnable
recipes: the three P1 recipes, Lensbook's stills a01 to a10 promoted to runnable (T83) and twelve motion recipes from the shot library (T94). Loaders, the token filler and
`save_runnable` live in `__init__.py`; the composer (`../llm/composer.py`) is the only caller that sends recipe text to a
model.

| file | what |
|---|---|
| `arch_stills.yaml` | Scene Lock `af-a`, stills `a01` to `a11` |
| `arch_shots.yaml` | shots `as01` to `as14` (with `risk`) and the Style Prefix |
| `risk_ladder.yaml` | move risk ladder, keyed for the Move policy (T43) |
| `repairs.yaml` | repair codes, vegetation tells, material tells |
| `project_sheet.yaml` | the 14 Project Sheet fields and the 17 token rules |
| `night_relight_changelist.yaml`, `sunburst_rephotograph_close.yaml`, `croppair_pushin.yaml` | runnable recipes (P1) |
| `relight_same_family` (a01), `relight_cross_family` (a02), `twilight_hero` (a03), `monsoon_pass` (a04), `season_swap` (a05), `staffage_walkers` (a06), `interior_occupants` (a07), `imperfections_close` (a08), `planting_refresh` (a09), `night_exterior` (a10), `festive_dress` (a05 family) | runnable recipes (T83); every `[TOKEN]` has a default |
| `motion_locked_living` (as01), `motion_slider_drift` (as03), `motion_crane_rise` (as04), `motion_light_sweep` (as08), `motion_weather_pass` (as09), `motion_night_glow` (as12), `motion_vegetation_breeze` (as14), `motion_material_macro` (as10), `motion_water_study` (as11), `motion_entourage_life` (as07), `motion_pullback_reveal` (as02 reversed), `motion_time_of_day` (as08 adapted) | `motion_croppair` recipes (T94), none above medium risk; the high-risk shots as05, as06 and as13 are deliberately not shipped (render them) |
| `templates/` (a sibling package, T95) | graph templates, not recipes: `edit_chain`, `night_chain`, `set_chain` (the night chain's Edit runs `night_relight_changelist`) |

## Three folders

A runnable recipe is a `<id>.yaml` file (the file stem is the id) in one of three folders. A later folder shadows an
earlier id:

| origin | folder | written by |
|---|---|---|
| `shipped` | this package | the repo |
| `studio` | `<app-data>/recipes/` (`PLATE_HOME` moves it) | `save_runnable(doc, "studio")`, `POST /recipes` |
| `project` | `<project>/recipes/` | `save_runnable(doc, "project", project_dir)`, `POST /recipes` |

`list_runnable(project_dir=None)`, `load_runnable(id_or_path, project_dir=None)` and `runnable_entries(project_dir=None)`
read them; `save_runnable(doc, scope, project_dir=None, overwrite=False)` validates the doc first, refuses an id that
already exists in any folder unless `overwrite` (`RecipeExists`) and never writes the shipped folder, so a shipped id can
only be shadowed. `origin` is derived from the folder and never written; a loose file passed by path counts as `project`.

## Runnable recipe schema (`schema: 1`)

```yaml
id: lower_snake_case
title: text
kind: still_edit | still_rephotograph | motion_croppair
library: a01                  # optional Lensbook recipe id this applies (must exist)
engine: nano-banana-2.1       # a manifest id in engines/plate_engines/manifests
shot_class: wide | medium | close | detail
group: lighting               # optional; heads the recipe in the Edit editor's grouped select
task: relight                 # optional; an engine-card task (plate_engines.tasks). None = the node decides
thumbnail: <CAS hash>         # optional; a project-CAS image shown beside the recipe
# origin: shipped | studio | project   (derived from the folder, never written; a doc that carries it is saved without it)
mask:
  editable_classes: [sky, vegetation, people]   # packet classes the engine may change; the rest is locked
  region: none | mask_bbox                      # none = the whole frame goes to the engine
  lock_rest: true
router_check:                 # optional; read by the render-it router (T28) before a quote
  require_shot_class: close
  refuse_verdicts: [render_it, render_move]
params: {n: 1, size: native}  # engine-facing defaults
croppair:                     # motion_croppair only
  z0: 1.0
  z1: 1.18
  center: [0.5, 0.5]
  direction: in               # in (default: 1.0 <= z0 < z1 <= 3.0), out (a pull-back: 1.0 <= z1 < z0 <= 3.0) or locked (one zoom: z0 == z1)
  center_end: [0.53, 0.5]     # optional: where the end crop is centred (a slider drift); default the start centre
motion:
  move: push_in               # a rung of the move-risk ladder (risk_ladder.yaml); the dials only ever raise its risk
  start_end: true             # false: a single-image take (no end frame, no `image_tail`); only a frame that holds may do it
  two_stills: false           # true: the move runs between two approved stills of ONE camera, so the Motion node needs its `end` input wired
  duration_s: 5
  camera_fixed: true          # informational on the Kling job (the Seedance previs path reads it)
overrides:                    # the recipe's own token defaults, keyed by the literal "[TOKEN]" text
  "[2700K]": "2700K"
prompt: |-                    # template; [TOKENS] are filled from overrides, then the Project Sheet
negative_prompt: text
```

`assemble_runnable(recipe, sheet)` is the template-only text (no model involved). Tokens nobody fills stay as
`[TOKEN]` and are counted by `unfilled_count`; `unfilled_tokens(text)` lists them. A recipe's own `overrides` beat the
Project Sheet, so the shipped recipes use short recipe-local tokens (`[TARGET HOUR]`, `[SEASON]`, `[OCCASION]`) that no
sheet rule matches, and give every one a default: they run on an empty sheet with no `[TOKEN]` left. A caller changes one
with the `overrides` argument of `assemble_runnable` (an Edit node carries it as `params.tokens`, one field per token
in the Edit editor); `GET /recipes/{id}/assemble?tokens=` shows the result.

`editable_classes` are packet legend classes (`id_class` in `spec/packet.schema.json`) minus the never-editable ones
(architecture, glazing, signage, other). Layers `ENT_*` land in the class `furniture`.

## Motion recipes (`kind: motion_croppair`)

A motion recipe describes a camera move cut from, or between, approved stills (never an invented angle). `croppair`
says how the crops are cut and `motion` how the job runs:

| key | meaning |
|---|---|
| `croppair.direction` | `in` (a push-in, the default), `out` (a pull-back, a push-in played outwards, same risk row) or `locked` (one zoom, `z0 == z1`: a held frame, or a slider drift when `center_end` is set). The zoom pair has to match its direction, or the recipe fails to load |
| `croppair.center_end` | an end crop centre different from the start (slider drift); a locked tripod ignores it, because its end crop is its start crop and the camera-lock gate measures the hold |
| `motion.move` | the move on the risk ladder; a blocked move (high, highest, or a router `render_move`) is refused before a price exists |
| `motion.start_end: false` | single-image take: only the start frame is sent, nothing is pinned at the end, no pace words are stripped. Allowed only for a frame that holds |
| `motion.two_stills: true` | the move is between two approved stills of one camera (a time of day change). The recipe cannot be quoted from one still (`end_required`); wire the second to the node's `end` input |
| `motion.duration_s` | the shot length; the job rounds it up and clamps it to the engine's range |
| `motion.camera_fixed` | carried as data; the start/end job does not read it |

The prompt law is applied when the plan is built: with an end frame pinned, pace words (slow, slowly, gently,
leisurely, unhurried) are stripped from the prompt, because the end frame decides the pace; the negative prompt stays
under 30 words. The start/end engines are `kling-o3-4k`, `kling-v3-pro` and `veo-3.1` (`veo-3.1` is custody D until
its terms are filed). Recipes are loaded from the same three folders as still recipes and show in the Motion editor's
`recipe` select (`GET /recipes?kind=motion_croppair`).
