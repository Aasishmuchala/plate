# Labelling tool (`plate_label`) and calibration

A three-key verdict tool for rating takes: **Ship**, **Fix** or **Render it**, with reason codes. Every verdict is one line appended to `calib/labels.jsonl`. The file is the ground truth the P1 calibration scores the gates against (the bet: gate ensemble AUC at least 0.85 on at least 100 held-out takes with at least 60 rejects, false-pass at most 5%). The labels are client-work data, so `calib/labels.jsonl` is gitignored and stays on the machine. The canonical description, with the calibration report options and the record format, is [calib/README.md](../calib/README.md); this page is the operator's summary.

Stdlib plus PyYAML (for the code table). Runs on macOS and Windows 10/11, with a loopback page or headless. No network beyond `127.0.0.1`.

## Run it

```
make label DIR=<folder>                              # same as: python -m plate_label serve <folder>
python -m plate_label --pairs pairs.json --rater aasish    # then open http://127.0.0.1:4612/
python -m plate_label <folder> --rater aasish
python -m plate_label stats                          # counts per rater, verdict and reason code
python -m plate_label --headless --take-id S3 --candidate out/a_ai.png --plate in/a.png --verdict fix --reasons P1,A2
```

Common options: `--out PATH` (default `<repo>/calib/labels.jsonl`), `--rater NAME` (default `$USER`), `--port` (default 4612), `--no-open`. `--headless` writes exactly one record and exits 0; a validation error prints on stderr and exits 2.

**Input.** `--pairs` takes a JSON list of `{take_id, plate, candidate, gate_report, run_id?}` (relative paths resolve against the file). A folder scan understands `<take>/plate.png` with `<take>/candidate*.png`, and `<take>.plate.png` with `<take>.candidate*.png`; a folder with neither is labelled image by image with no plate to wipe against. Images are served from where they are, never copied, and only the files named by the pairs.

## Keys (page)

`1` Ship (saves and advances), `2` Fix, `3` Render it (then the reason picker: click chips, or type `A2 P4`, then Enter), `n` next (saves a pending verdict first, else skips), `u` undo the last label this session, `z` 100% view, `t` flip plate and candidate, `[` `]` move the wipe, arrows browse, `Esc` cancels. Reasons are optional on Fix and Render it; a Ship carries none. The page never shows a gate score or verdict, so a label is independent of the gates it will be used to calibrate.

## Reason codes

One table, `calib/reason_codes.yaml`, shared by the page, the headless flag, the canvas verdict panel and the calibration report. A code is never renumbered or reused.

| group | codes |
|---|---|
| A (Lensbook, stills) | `A1` geometry warp, `A2` texture crawl, `A3` scale drift, `A4` vegetation slop, `A5` re-cladding drift |
| F (Lensbook, film) | `F2` slideshow, `F4` action ignored, `F6` camera improvises, `F7` continuity, `F9` extra people or objects, `F11` text or logos, `F12` light shift |
| P (PLATE gates) | `P1` registration, `P2` invented edge, `P3` colour shift, `P4` texture band, `P5` tone band, `P6` outside-mask leak, `P7` people implausible |

## Record

One JSON object per line, UTF-8, LF line endings:

```json
{"v":1,"at":"2026-10-17T09:41:07Z","rater":"aasish","take_id":"S3","plate":"in/a.png","candidate":"out/a_ai.png","run_id":"r12","verdict":"fix","reasons":["P1","A2"],"seconds":14.2,"gate_report":"gates/r12.1.json"}
```

`run_id` and `gate_report` are left out when unknown; `plate` is null for a candidate-only label. An image already labelled by this rater is skipped on the next session; if the same take is labelled twice the calibration keeps the latest record per rater and take. A torn last line (a crash) is skipped with a warning and the next append starts on a fresh line.

## From labels to a threshold

```
python -m plate_gates.calibration report --reports <project>/gates --labels calib/labels.jsonl --rater aasish
python -m plate_gates.calibration report --reports <dir> --threshold 0.93 --strict    # held-out run, exit 1 if the bet fails
```

Positive class (reject) is Fix or Render it; scores are goodness from the gate ensemble; the operating point is the highest recall of Ships with false-pass at most 5%. Fit the point once, freeze the number, then evaluate held-out takes with `--threshold`: a point fitted and scored on the same takes is optimistic, and the report says so. A second rater double-labelling about 100 takes gives the agreement figure (`--rater` another name). Use the reason codes to check that the right gate fires: P1 against registration, P2 against invention flags, P3 against material colour, P4 and P5 against the texture and tone bands, P6 against the outside-mask check, F codes against the video gates. A reject whose reason no gate flags is a gate gap, not noise.

## Security

The server binds `127.0.0.1` only, serves images by pair index and role from the pair roots only, and refuses requests whose `Host` is not a loopback name or whose `Origin` is another site, so a web page elsewhere cannot drive it through your browser.
