# recipes

Lensbook's archviz doctrine as data (ported from `~/Desktop/lensbook/lib/data.ts` and `assemble.ts`) and the runnable
recipes: the three P1 recipes plus Lensbook's stills a01 to a10 promoted to runnable (T83). Loaders, the token filler and
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
croppair: {z0: 1.0, z1: 1.18, center: [0.5, 0.5]}   # motion_croppair only; 1.0 <= z0 < z1 <= 3.0
motion: {move: push_in, start_end: true, duration_s: 5}
overrides:                    # the recipe's own token defaults, keyed by the literal "[TOKEN]" text
  "[2700K]": "2700K"
prompt: |-                    # template; [TOKENS] are filled from overrides, then the Project Sheet
negative_prompt: text
```

`assemble_runnable(recipe, sheet)` is the template-only text (no model involved). Tokens nobody fills stay as
`[TOKEN]` and are counted by `unfilled_count`; `unfilled_tokens(text)` lists them. A recipe's own `overrides` beat the
Project Sheet, so the shipped recipes use short recipe-local tokens (`[TARGET HOUR]`, `[SEASON]`, `[OCCASION]`) that no
sheet rule matches, and give every one a default: they run on an empty sheet with no `[TOKEN]` left. A caller changes one
with the `overrides` argument of `assemble_runnable` (an Edit node carries it as `params.tokens`, wired by T92);
`GET /recipes/{id}/assemble?tokens=` shows the result.

`editable_classes` are packet legend classes (`id_class` in `spec/packet.schema.json`) minus the never-editable ones
(architecture, glazing, signage, other). Layers `ENT_*` land in the class `furniture`.
