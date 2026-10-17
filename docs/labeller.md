# Labelling tool (`plate_label`)

A three-key verdict tool for rating takes: **Ship**, **Fix** or **Render it**, with reason codes. Every verdict is one line appended to `calib/labels.jsonl`. The file is the ground truth the P1 calibration scores the gates against. The labels are client-work data, so `calib/labels.jsonl` is gitignored and stays on the machine.

Stdlib plus `blake3`. Runs on macOS and Windows 10/11, headless or with the loopback UI. No network beyond `127.0.0.1`.

## Run it

```
make label DIR=<folder of images>                    # same as: python -m plate_label serve <folder>
python -m plate_label FOLDER --rater aasish          # terminal loop
python -m plate_label FOLDER --serve [--port 4612] [--no-open]
python -m plate_label --headless IMAGE --verdict fix --reasons G01,C01 [--note "..."]
python -m plate_label --stats                        # counts per verdict and per reason code
```

Common options: `--out PATH` (default `<repo>/calib/labels.jsonl`), `--rater NAME` (default `$USER`, then `%USERNAME%`, then `unknown`), `--take-id ID` (stored on every record). Images already in the labels file (matched by BLAKE3 of the file, so a renamed file still counts) are skipped; stop with `q` and resume later.

`--headless` writes exactly one record and exits 0. A validation error (unknown code, fix without a reason, O99 without a note, missing image) prints the message on stderr and exits 2.

Image types: png, jpg, jpeg, tif, tiff, webp, matched case-insensitively, found recursively, dot-files skipped, sorted case-insensitively by relative path. Chrome and Edge cannot display TIFF, so the UI re-encodes TIFF as PNG when Pillow can read it (8-bit and 16-bit grey, 8-bit RGB). A 16-bit RGB TIFF falls back to the raw file, which only Safari shows. The hash is always of the original file.

## Keys

Terminal loop (prints `n/N <image>` then prompts):

| input | meaning |
|---|---|
| `s` | ship |
| `f G01,C01` | fix, with reason codes (comma or space separated, any case) |
| `r G03` | render it, with reason codes |
| `n` | prompt for a note on this image (kept until you give a verdict) |
| `?` | help and the code table |
| `q` | quit |

Browser UI (`http://127.0.0.1:4612/`):

| key | action |
|---|---|
| `S` / `F` / `R` | select Ship / Fix / Render it |
| reason checkboxes | grouped by prefix: Geometry, Colour, Texture, People, Motion, Lettering, Other |
| `Enter` | save and jump to the next unlabelled image (also works inside the note box) |
| `Left` / `Right` | previous / next image (labelled or not) |
| click image, or `Z` | toggle a 100% crop at the clicked point (true device pixels, so 100% on a Retina screen is not blurred by CSS scaling); `Esc` or another click returns to fit |

Re-labelling an image appends a second record; the later record wins in analysis.

## Rules

- `fix` and `render_it` need at least one reason code. `ship` needs none (reasons are allowed and kept).
- `O99` needs a non-empty note.
- Unknown codes are rejected (terminal message, `--headless` exit 2, HTTP 400).

## Reason codes (fixed)

| code | meaning |
|---|---|
| G01 | camera shift / registration |
| G02 | invented edges or openings on architecture |
| G03 | unseen geometry guessed |
| G04 | aspect or preset drift |
| C01 | material restyled |
| C02 | cross-view colour drift |
| C03 | tone or exposure off |
| T01 | AI over-texture (fails texture band) |
| T02 | soft / mushy detail |
| T03 | flicker |
| P01 | implausible people |
| P02 | people scale wrong |
| M01 | camera not locked |
| M02 | first/last frame mismatch |
| M03 | drift or invention over time |
| X01 | lettering or logo changed |
| O01 | moderation refused |
| O99 | other (note required) |

The table lives in `tools/plate_label/codes.py`. Do not renumber or reword a code once labels exist.

## Record schema

One JSON object per line, UTF-8, LF line endings, keys in this order:

```json
{"ts":"2026-10-17T14:41:07Z","image":"sub/shot_03.png","image_blake3":"<64 hex>","verdict":"fix","reasons":["G01","C01"],"note":"","rater":"aasish","take_id":null,"tool_version":"0.1.0"}
```

- `ts`: UTC, ISO-8601 with a trailing `Z` (colons are fine inside the content; no generated file name ever contains one).
- `image`: path relative to the labelled folder, forward slashes on both OSes.
- `image_blake3`: BLAKE3 hex of the image file; the join key to everything else.
- `verdict`: `ship`, `fix` or `render_it`. `reasons`: codes from the table. `note`: free text. `rater`, `take_id`, `tool_version`: provenance.

Appends are a single `write` of one line opened with `encoding="utf-8", newline="\n"`, so they behave the same on Windows. If a crash left a torn last line, the next append starts on a fresh line and the reader skips the torn one with a warning.

## How the labels feed the P1 calibration

The P1 bet is: the gate ensemble's ROC AUC against these verdicts is at least 0.85 on at least 100 held-out takes containing at least 60 rejects, with false-pass at or under 5% at the chosen operating point.

1. Take the latest record per `image_blake3`. The positive class (reject) is `fix` or `render_it`; `ship` is negative. Join to the gate reports on the hash of the image each report scored.
2. Compute AUC per gate score and for the ensemble against that label, and pick the operating point from the false-pass bound.
3. Use the reason codes to check that the right gate fires: G codes against the registration, shift and invention-flag gates; C codes against the colour and tone gates; T codes against the texture band; M codes against the video gates; P and X codes against the people and text-lock checks. A reject whose reason no gate flags is a gate gap, not noise.
4. `O01` (moderation refused) is not an image-quality verdict and `O99` is judged from its note; both are left out of the per-gate AUC.
5. A second rater double-labelling 100 takes gives inter-rater agreement; `rater` is on every record for that.

`python -m plate_label --stats` gives the running counts per verdict and code while the set grows.

## Security notes

The server binds `127.0.0.1` only. It serves nothing outside the labelled folder (path resolved, then `is_relative_to` check, otherwise 403) and only image files. Requests whose `Host` is not a loopback name or whose `Origin` is a different site are refused, so a web page elsewhere cannot write labels through your browser. The page loads no script or style from anywhere else (CSP with a per-request nonce).
