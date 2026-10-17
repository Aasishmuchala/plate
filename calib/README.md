# calib: labels, reason codes and the calibration report

The bet (BUILD_PLAN P1 exit) is that the gate ensemble agrees with Aasish's eye: ROC AUC >= 0.85 on at
least 100 held-out takes containing at least 60 rejects, with false-pass <= 5% at the chosen operating
point. This folder holds the human side of that measurement.

| file | what | in git |
|---|---|---|
| `reason_codes.yaml` | the reason-code table (Lensbook A/F codes plus PLATE P codes); the one source for the labeller and the report | yes |
| `labels.jsonl` | one JSON line per Ship / Fix / Render-it verdict, written by `tools/plate_label` | no (client-work data, gitignored) |
| `report_<date>.md` | calibration report written by `gates/plate_gates/calibration.py` | review first: it names takes and candidate paths |

## Labelling

```
python -m plate_label --pairs pairs.json --rater aasish      # then open http://127.0.0.1:4612/
python -m plate_label <folder> --rater aasish                # folder scan
python -m plate_label stats                                  # counts per rater / verdict / code
```

`pairs.json` is a JSON list of `{take_id, plate, candidate, gate_report, run_id?}`. Relative paths are
relative to the file. `gate_report` is the path of the gate report that scored the candidate (calibration
finds it again by path, by file name under `--reports`, or by `run_id`). Images are served from where they
are; nothing is copied, and only the files named by the pairs (and only from their folders) can be fetched.

A folder scan understands `<take>/plate.png` with `<take>/candidate*.png`, and `<take>.plate.png` with
`<take>.candidate*.png`. A folder with neither is labelled image by image with no plate (no wipe).

Keys: `1` Ship (saves and advances), `2` Fix, `3` Render-it (then reason codes: click chips, or type
`A2 P4`, then `Enter`), `n` next (saves a pending verdict first, else skips), `u` undo the last label this
session, `z` 100% view, `t` flip plate/candidate, `[` `]` move the wipe, arrows browse, `Esc` cancels.
Reasons are optional on Fix / Render-it; a Ship carries none. The page never shows a gate score or verdict:
a rater who has seen the gates' opinion is not an independent label for calibrating them.

One line per verdict, `run_id` and `gate_report` left out when unknown:

```
{"v":1,"at":"2026-10-17T09:41:07Z","rater":"aasish","take_id":"S3","plate":"in/a.png","candidate":"out/a_ai.png","run_id":"r12","verdict":"fix","reasons":["P1","A2"],"seconds":14.2,"gate_report":"gates/r12.1.json"}
```

A second rater double-labels about 100 takes (`--rater` another name) for the agreement figure.

## Calibration report

```
python -m plate_gates.calibration report --reports <project>/gates [--labels calib/labels.jsonl] [--rater aasish] [--out FILE]
python -m plate_gates.calibration report --reports <dir> --threshold 0.93 --strict    # held-out run, exit 1 if the bet fails
```

Conventions: a reject is a Fix or Render-it; scores are goodness (higher = the gates like it more); the
gates ACCEPT a candidate when its ensemble score >= the threshold. `false_pass` = rejects accepted /
rejects (zero misses in 60 rejects bounds it under 5% by the rule of three, which is why the bet wants
60). The operating point is the highest recall of ships with false-pass <= 5%; a point that accepts no
ship meets any cap vacuously, so the bet then uses the lowest false-pass at which any ship is accepted.
Run it once to fit the point, freeze the number, then evaluate held-out takes with `--threshold`: a point
fitted and scored on the same takes is optimistic, and the report says so.

The ensemble is a weighted mean of per-gate goodness (1 when the metric meets its threshold, falling to 0
one `scale` beyond the bound). Weights come from an `ensemble:` block in `gates/plate_gates/thresholds.yaml`
(next to `metrics:`); until that block exists `DEFAULT_ENSEMBLE` in `calibration.py` is used:

```yaml
ensemble:
  registration.residual_px: 2.0
  gbuffer_edge_recall_r2: 3.0
  texture.lapvar: {weight: 2.0, scale: 300}   # scale optional: default |bound| / range width / 1
```

Weights are a first guess. Refit them (and re-issue the threshold set id) only once there are enough
labels to fit on a different set from the one the bet is scored on.
