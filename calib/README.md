# calib: labels, reason codes and the calibration report

The bet (BUILD_PLAN P1 exit) is that the gate ensemble agrees with Aasish's eye: ROC AUC >= 0.85 on at
least 100 held-out takes containing at least 60 rejects, with false-pass <= 5% at the chosen operating
point. This folder holds the human side of that measurement.

| file | what | in git |
|---|---|---|
| `reason_codes.yaml` | the reason-code table (Lensbook A/F codes plus PLATE P codes); the one source for the labeller and the report | yes |
| `labels.jsonl` | one JSON line per Ship / Fix / Render-it verdict, written by `tools/plate_label` and by the daemon's verdict route | no: it lives in the data folder, not here (see below) |
| `report_<date>.md` | calibration report written by `gates/plate_gates/calibration.py` | review first: it names takes and candidate paths |

## Where labels live

Labels are calibration data made from client work, so they never land inside the repo by default. The
file is `<data home>/calib/labels.jsonl`:

| OS | default path |
|---|---|
| Windows | `%LOCALAPPDATA%\Plate\calib\labels.jsonl` |
| macOS | `~/Library/Application Support/Plate/calib/labels.jsonl` |

`PLATE_HOME` moves the data home (the daemon's `config.Settings.home`, the same folder as `config.json` and
`index/`). The folder is created on the first label. The labelling tool and the daemon's verdict route
(`POST /runs/{id}/verdict`, the `1` / `2` / `3` keys on the canvas) resolve the same file, so a canvas verdict
and a three-key label are lines of one file. The override order, for both, is:

1. `--out FILE` (labelling tool only) or `settings.labels_path` (daemon only),
2. `PLATE_LABELS` (the exact file),
3. `<data home>/calib/labels.jsonl`.

`python -m plate_label stats` prints the file it read (`N label(s) in <path>`), which is the path to hand to
the calibration report:

```
python -m plate_label stats
python -m plate_gates.calibration report --reports <project>/gates --labels "<that path>"
```

Pass `--labels` every time: the calibration CLI is pointed at the file, not at a default location.

An older build wrote `calib/labels.jsonl` inside the repo. If one is still there it is read once per process
(the labelling tool counts it with the default file; the daemon only notices it) with a printed warning that
names where labels go now, and it is never written. Move it into the data folder to keep the labels, or delete
it; lines from audit and smoke runs do not belong in a calibration set. Pointing `PLATE_LABELS` or `--out` at a
repo path on purpose is still allowed: that is an explicit choice, and the file then counts as the target, not
as a legacy file.

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
python -m plate_gates.calibration report --reports <project>/gates --labels <labels.jsonl, see above> [--rater aasish] [--out FILE]
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
