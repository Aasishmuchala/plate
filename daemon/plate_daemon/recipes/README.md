# recipes

Lensbook's archviz doctrine as data (ported from `~/Desktop/lensbook/lib/data.ts` and `assemble.ts`) and the three
runnable P1 recipes. Loaders and the token filler live in `__init__.py`; the composer (`../llm/composer.py`) is the
only caller that sends recipe text to a model.

| file | what |
|---|---|
| `arch_stills.yaml` | Scene Lock `af-a`, stills `a01` to `a11` |
| `arch_shots.yaml` | shots `as01` to `as14` (with `risk`) and the Style Prefix |
| `risk_ladder.yaml` | move risk ladder, keyed for the Move policy (T43) |
| `repairs.yaml` | repair codes, vegetation tells, material tells |
| `project_sheet.yaml` | the 14 Project Sheet fields and the 17 token rules |
| `night_relight_changelist.yaml`, `sunburst_rephotograph_close.yaml`, `croppair_pushin.yaml` | runnable recipes |

## Runnable recipe schema (`schema: 1`)

```yaml
id: lower_snake_case
title: text
kind: still_edit | still_rephotograph | motion_croppair
library: a01                  # optional Lensbook recipe id this applies (must exist)
engine: nano-banana-2.1       # a manifest id in engines/plate_engines/manifests
shot_class: wide | medium | close | detail
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
`[TOKEN]` and are counted by `unfilled_count`.
