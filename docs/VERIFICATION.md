# PLATE verification record

Written by the final verification pass (router task T53, Opus 5.5) on 2026-10-18 between 12:20 and 13:00 IST, after Wave 10 (T111 to T115), on the Mac (macOS 27.0 arm64, Python 3.12.12, Node 22.22.3, npm 10.9.8, Blender 5.1.1, ffmpeg 8.1 from Homebrew, no `oiiotool`). Commit `48d7910` plus the uncommitted Wave 10 edits in the working tree (18 modified and 9 new files, all T111 to T115). Nothing here ran on the Windows render box, inside 3ds Max or against a live vendor; those lines are in the box-day checklist (section 9).

Earlier passes: the first (03:00 to 04:00 IST, commit `dd78f02`) found two failing tests and gap G1 (viewer edges and ΔE00 heatmap not fed by the daemon); the orchestrator fixed the tests in `8d65859` and T109 closed G1. The second (07:20 to 07:50 IST, `23c147e` plus T107/T108) found G2 (a new take on an approved Edit blocked every graph save while a Motion hung off it); Wave 10 was written from it. This pass re-ran everything: **G2 is closed** (T111, rows W2 and W3), the three Wave 9 gaps T112 to T114 work in the browser, and **one new defect, G3**, fails `make test` (section 2).

## Result

| check | result |
|---|---|
| Windows-hazard scan (`scripts/hazard_scan.py`, `--selftest`, `--strict-allow`) | pass |
| `make lint` (ruff check and format check) | pass |
| `make test` (fidelity selftest, hazard, pytest for every package, vitest) | pass (after the orchestrator closed G3, see "Closing pass"): pytest `5945 passed, 1 skipped`, vitest `798 passed` |
| `npm run build` in `ui` | pass |
| licence registry check | pass |
| claims as tests (`scripts/claims_check.py`) | pass: 28 claims, 136 tests |
| `PLATE_FAKE_ENGINE=1 python -m plate_daemon.cli smoke` | pass: 11 adapters ok |
| fixture render idempotency (`bash fixtures/blender/render.sh` twice) | pass: 0 changed files |
| scripted end-to-end smoke (`scripts/smoke_e2e.py --build-ui`, T79 and T115) | pass: `SMOKE PASS`, 36 steps |
| UI smoke in a real browser against a real daemon (section 2) | pass: every step of the plan's smoke, plus G2's repro and the Wave 10 surfaces |

UI smoke: PASS

## Closing pass (orchestrator, 2026-10-18, after T53)

G3 is closed. The orchestrator applied the patch in section 2 ("Gap G3"): `full_frame` on `POST /runs/{id}/export`, with the route's framing (`crop` or `full_frame`) in its response and ledger record, and `test_app.py` asserting the 409 on a take of another aspect followed by a `full_frame` export. The follow-ups the last builders flagged were fixed at the same time, each with a test:

- **Accept also picks the take.** The UI's accept now picks the accepted take too. Since G2 the daemon reads approval from the run behind the picked take, so before this an accept on a compared take left the node unapproved. The regression test fails on the old code.
- **CLI holds locked edges.** `plate run` no longer refuses a project holding an edge the daemon stored with `locked: true`. It drops only those held VIDEO_LOCKED problems.
- **Whole frame from every surface.** The Export editor has a "whole frame (keep overscan)" box and `plate export` takes `--full-frame`. Both reach the single-run and batch routes.
- **Per-engine reference wording.** An engine manifest can state its wording for a reference image (`reference_token` on its images input). The daemon's default, `reference image {k}`, applies otherwise.

The orchestrator re-ran the plan's whole done-when chain independently on that tree. Every step exits 0:

| check | result |
|---|---|
| `make lint` | `All checks passed!`, 519 files formatted |
| pytest, every package | `5945 passed, 1 skipped` (no drawtext in this Mac's ffmpeg) |
| `npm run typecheck`, `vitest run`, `npm run build` in `ui` | clean; `798 passed`; built |
| `scripts/hazard_scan.py --strict-allow` | clean, 977 files |
| `scripts/claims_check.py` | all 28 claims hold (136 tests) |
| `python -m plate_pixels.licences --check` | OK |
| `pytest scripts/tests` | pass |
| `scripts/smoke_e2e.py` | `SMOKE PASS` |

## 1. Command record, in the order the plan gives

All from the repo root.

| # | command | exit | key output |
|---|---|---|---|
| 1 | `.venv/bin/python scripts/hazard_scan.py` | 0 | `hazard_scan: clean (975 files scanned, 2 allowlist entries)` |
| 1a | `.venv/bin/python scripts/hazard_scan.py --selftest` | 0 | `hazard_scan selftest ok: 13 planted hazards caught, clean sample passes` |
| 1b | `.venv/bin/python scripts/hazard_scan.py --strict-allow` | 0 | `hazard_scan: clean (975 files scanned, 2 allowlist entries)` |
| 2 | `make lint` | 0 | `All checks passed!`, `517 files already formatted` |
| 3 | `make test PYTEST=".venv/bin/pytest -rs --junitxml=<scratch>/junit2.xml"` (the same targets; the two flags only add skip reasons and per-package counts) | 2 | `SELFTEST PASS`; hazard selftest and scan clean; pytest `1 failed, 5936 passed, 1 skipped in 814.67s`, the failure `FAILED daemon/tests/test_app.py::test_packet_to_export_on_the_project_cas` (`assert (409 == 200)`, G3); 4K gate lines `run_gates 3840x2160: wall 4.70s per_material 0.99s` and `run_gates 3840x2160 with overlays: wall 4.95s overlays 0.26s` |
| 3a | `npm test` (what `make test` would have run next) | 0 | `Test Files 27 passed (27)`, `Tests 796 passed (796)` |
| 4 | `npm run build` (in `ui/`: `tsc -b && vite build`) | 0 | `dist/assets/index-B9EOQ_Z6.js 788.34 kB`, `RecompositeBar-*.js 2.39 kB`, `built in 1.41s` (Vite's 500 kB chunk warning only) |
| 5 | `.venv/bin/python -m plate_pixels.licences --check` | 0 | `licence check OK: 17 files scanned, 7 blocked models absent` |
| 6 | `.venv/bin/python scripts/claims_check.py` | 0 | `claims_check: all 28 claims hold (136 tests passed)`; `--skip-ui` gives the same |
| 7 | `PLATE_FAKE_ENGINE=1 .venv/bin/python -m plate_daemon.cli smoke` | 0 | `fixture smoke: 11 adapters ok` (8 routes skipped, listed in section 10) |
| 8 | `bash fixtures/blender/render.sh` twice, then `git status --porcelain fixtures \| wc -l` | 0 | stamps current (`base`, `ral3020`, `v2`: up to date), repack validated 6 packets; `0` changed files before and after each run, and `diff -r` against a copy of `fixtures/packets` taken before the runs is empty |
| 9 | `perl -e 'alarm shift; exec @ARGV' 900 .venv/bin/python scripts/smoke_e2e.py --build-ui` | 0 | 36 steps `ok`: UI suite 796, wiring 19, build, daemon start, `/health` plugin block and `alerts_recent`, project, packet stage/post/ingest in 0.3 s/list, thumb, 15 engines incl. fake, summary, graph, quote with `router ai_by_region`, approve, DONE in 1.0 s, gate `advisory`, scrub, accept, server-derived approval, export `640x268 (crop y 46)` + sidecar, `full_frame` export `640x360`, lineage, shipped, runs list, rescore `versions [1, 2]`, `@ref` quote on nano-banana-2.1 (`reference image 1`), G2 Motion wired, recomposite, G2 autosave still 200, `edit_chain` template, `night_chain` refused `['no_base']`, `index.html`, `text/javascript` asset, JSON 404; last line `SMOKE PASS`; `.smoke/` removed |
| 10 | `.venv/bin/pytest scripts/tests` (the `make ci` step outside `make test`) | 0 | `79 passed` |
| 11 | `git ls-files -z fixtures \| xargs -0 du -ch \| tail -1` | 0 | `17M total` (cap 25 MB) |
| 12 | 13:13 to 13:30 IST retry, same tree: `make lint && make test && .venv/bin/python scripts/hazard_scan.py && .venv/bin/python scripts/claims_check.py` (the plan's done-when, verbatim) | 2 | lint `All checks passed!`, `517 files already formatted`; `SELFTEST PASS`; hazard clean; pytest `1 failed, 5936 passed, 1 skipped in 802.30s`, again only `test_app.py::test_packet_to_export_on_the_project_cas` (G3); 4K gate `wall 4.59s`. Run on their own afterwards: `hazard_scan.py` exit 0 (clean, 975 files), `claims_check.py` exit 0 (`all 28 claims hold (136 tests passed)`), `npm test` `796 passed (796)` |

Pytest per package (junit of row 3): daemon 2,155 (1 failed, 1 skipped), engines 673, fixtures 130, gates 601, pixels 315, plugins/max 926, spec 115, repo-root `tests` 960, tools/plate_label 63.

Row 3 is the second `make test` of this pass. The first one overlapped row 8 and also errored `daemon/tests/test_carryover_wave9.py::test_with_nothing_accepted_nothing_is_carried_but_the_record_is_still_published` with `FileNotFoundError: .../fixtures/packets/room_a/manifest.json`: `render.sh` re-validates the packets in place while that test read them. That was this pass's doing, not a defect; the test passes alone and in run 3. Do not run `render.sh` and pytest together. Both `make test` runs overlapped the browser smoke below (two idle daemons and a headless browser); the 4K gate stayed at 4.70 s (4.95 s with the overlays), under T67's 10 s.

## 2. UI smoke

### How it was driven

The orchestrator's hand-driven browser pass was done by script in a real browser: chrome-headless-shell 153.0.8010.12 (Playwright's cached `chromium_headless_shell-1243`), driven over CDP by `playwright-core` from an existing `node_modules` on this Mac, WebGL2 through SwiftShader, viewport 1600×1000. Every click, wire and key press goes through the page as an artist's would: palette buttons, handles dragged with the mouse, editor fields, file inputs, viewer keys. Screenshots were checked at every step. Nothing was installed or downloaded; the driver scripts, the daemon homes and the screenshots stayed in the session scratchpad. The only image uploaded was a synthetic 256×256 swatch made in the scratchpad (no client imagery).

### The exact steps (as run, corrected where the plan's text no longer matches the code)

Start a daemon on a throw-away home. `PLATE_LABELS` is not needed: verdict labels land in `<PLATE_HOME>/calib/labels.jsonl` (T110). On Windows, set each variable with `$env:NAME = 'value'`.

```bash
cd /Users/aasish/Desktop/plate
export SMOKE="$HOME/plate-smoke-home"
PLATE_HOME="$SMOKE" PLATE_FAKE_ENGINE=1 PLATE_PIXELS_INPROC=1 PLATE_NIGHTLY=0 \
  .venv/bin/python -m plate_daemon
```

Stage the fixture under the project's id (the fixture's manifest names project `plate-fixture`; see C3) and read the token.

```bash
mkdir -p "$SMOKE/packets" && cp -R fixtures/packets/room_a "$SMOKE/packets/room_a"
.venv/bin/python -c "import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); m=json.loads(p.read_text(encoding='utf-8')); m['project_id']='smoke'; p.write_text(json.dumps(m, indent=1)+'\n', encoding='utf-8', newline='\n')" "$SMOKE/packets/room_a/manifest.json"
TOKEN=$(PLATE_HOME="$SMOKE" .venv/bin/python scripts/plate_config.py token)
```

For rows 9c and 18 a second daemon ran beside it with a fake that shifts every take 12 px: `PLATE_HOME="$SMOKE-shift" PLATE_PORT=4620 PLATE_FAKE_ENGINE='{"echo": true, "shift_px": 12}' PLATE_PIXELS_INPROC=1 PLATE_NIGHTLY=0 .venv/bin/python -m plate_daemon`, set up with rows 2 to 6.

| # | step | observed | result |
|---|---|---|---|
| 1 | Open the printed `http://127.0.0.1:4610/#token=...` as is | status bar `daemon: up v0.1.0 (stub engine)`, `token: present`, `Max dock · never polled`; the fragment is gone from the address bar and the token sits in `sessionStorage` (`plate.token`) | pass |
| 2 | Projects tab, New project: id `smoke`, NDA unticked, grades C0 C1 C2 D, Create project | `smoke` listed and opened with its four grade chips | pass |
| 3 | `curl -X POST http://127.0.0.1:4610/api/v0/packets -H "Authorization: Bearer $TOKEN" -H "X-Plate-Packet-Version: 0.1.1" -H "Content-Type: application/json" -d "{\"packet_path\": \"$SMOKE/packets/room_a\", \"sender\": {\"name\": \"curl\", \"version\": \"0\"}}"` | the plan's literal command (no content type) gives `422`; with it, `202 {"packet_id":"pkt_2ZHQ7DX53BCK1J9P8GJB9Z2V5G","status":"ingesting",...}`, ingested in 0.12 s; `GET /projects/smoke/packets` lists it (plate `807554a6...`, 640×360, camera `CAM_a`) | pass |
| 4 | `+ node`: Plate, Mask, Edit, Export; drag Plate `packet` to Mask `source`, Plate `beauty` to Edit `image`, Mask `mask` to Edit `mask`, Edit `image` to Export `media` | 4 edges, no refusal; the save chip reads `saved 12:26:02` (`PUT /projects/smoke/graph` 200) | pass |
| 5 | Select Plate; in the Plate editor, link `room-a` (Tier A, CAM_a, 640 x 360) | node thumbnail of the plate (served by reference from the packet folder); summary `31 ids · 8 editable · 23 locked` | pass |
| 6 | Select Mask; tab Pick id; click the shrub at the bottom left (image pixel 56, 324) | tabs `Pick id, Paint, SAM click, Text lock`; last pick `VEG_shrub_A vegetation`, `not locked`, coverage 100%, depth 1.88 m; chip added; `output: 1 id` | pass |
| 7 | Select Edit; type a prompt; Make bar engine; Quote | Nano Banana 2.1 and Seedream 5 Pro are listed but disabled, titled `model id unknown until the P0 smoke test` (C4). Quoted `Nano Banana Pro · D` instead: chips `max 4096×4096 \| refs ≤ 13 \| per image`, quote `gemini · nano-banana-pro · 0.24 USD · D · ETA ~60 s unconfirmed expires in 15:00`. Not made (cloud route). No `@ref` hint (no Reference wired) | pass (C4) |
| 8 | Engine `Fake (echo) · C0`, N 2, Quote, Make | `fake · fake · 0.080 USD (2 × 0.040 USD) · C0 · ETA ~60 s`; Make is the human approval (no dialog: the daemon did not refuse); two candidates `DONE`, score 1.00 each; both runs `approved_by: human` | pass |
| 9a | Click candidate 1's thumbnail: the viewer shows `A plate · B candidate 1`; wipe, then `` ` `` toggle (candidate 1, then plate), then `d` difference ×4 | each mode drew, no viewer error | pass |
| 9b | `e` edges, `h` ΔE00 heatmap | both draw with no notice: red plate G-buffer edges, cyan candidate edges, white where both agree; the ΔE00 map with its 0 to 10 legend, near 0 on the echo take. On the 12 px shifted take the cyan edges sit off the red ones on the shrub | pass |
| 9c | `t` tiles | on this passing take: `no gate-flagged points for tiles`. On the shifted daemon the take is `GATE_FAIL` on `registration.shift_px 12 > 8` (nothing else fails: the composite is still mask-locked); Gate panel `Open tiles (2)` puts the viewer in Tiles mode with plate/candidate pairs at 100% | pass |
| 10 | Gate tab | verdict `advisory`, version `v1`, 14 checks with value, threshold and status: advisory on `feather_band.de00_p99 0.2095` (decision 35), `texture.lapvar 707.3`, `tone.luma 121.8`; pass on registration (0.0083 px, shift 0.0006 px), `outside_mask.de00 0`, `gbuffer_edge_recall_r2 1`, `invention_flags 0`, material ΔE00 0/0, `texture.fine_ratio 0.5714`, tone contrast and saturation, image edge agreement 0.9999. See C6 | pass |
| 11 | Verdict `1` with focus outside the viewer, before any scrub | `a full scrub must be logged before a verdict`; Full scrub and Accept take disabled | pass |
| 12 | Focus the viewer, `1` (100%), shift+arrows | `4/4 quadrants at 100%`; tick Full scrub: `Full scrub (logged)` | pass |
| 13 | `1` again, then Accept take | `Ship recorded`, Accept offered, then `Accepted`; the Edit node shows `gate pass` and `✓ approved`; the stored graph has `edit-1` `gateStatus: pass`, `approved: true`, `approvedBy: human`, `scrubLogId: scrub-291e65a1d7f1`, all server-derived. The verdict went to `<PLATE_HOME>/calib/labels.jsonl` (1 line); the repo's `calib/` holds only `README.md` and `reason_codes.yaml` | pass |
| 13b | Focus the candidate strip, press `1` | candidate 1 becomes the node's output: the Edit card shows the take and `picked`, the stored graph has `outputHash 2392c48c...` | pass |
| 14 | Select Export; the accepted take; format png16; out dir `$SMOKE/out`; Export | `run_01M56WR3GT6N6YNQY5KQ9DRQME.png`, **640×268** (the packet's 2.39:1 safe frame, T114; C10), 3 channels, uint16, 2,528-byte ICC profile, and `.gate.json` whose `export.crop` is `{x: 0, y: 46, width: 640, height: 268}`; `lineage.read` names project `smoke`, node `edit-1`, the run, packet, engine `fake` and `gates/<run>.1.json`; `GET /runs/{id}/gate` now `shipped: true` | pass |
| 15 | Add Review, wire from Edit, tick one take, Build bundle | Build bundle stays disabled (`pick at least one take`) until a take is ticked; then `projects/smoke/review/review-daa01e902883ec16.zip`; its `index.html` opened from `file://` shows 1 take, the ADVISORY badge, wipe and toggle, 2 images, no page errors | pass |
| 16 | Add Concept, wire Plate `beauty` to it, then Concept to a new Export | refused on the node: `CONCEPT_TO_DELIVERY concept tier cannot reach Export`; no edge added | pass |
| 17 | Add a second Edit (not approved) from Plate, then wire it to a new Motion | refused: `VIDEO_LOCKED motion is locked until the upstream still passes gates and is approved`. Control: Plate `packet` to Motion `packet` and the approved `edit-1` to Motion `start` are accepted (edges 7 to 9, saved); the Motion editor shows start `Edit (edit-1) approved` | pass |
| 18 | On the shifted daemon: Export the `GATE_FAIL` take | the reason field appears and Export stays disabled until a reason is typed; nothing written | pass |
| 19 | Allowlist and auth probes (curl against the smoke daemon) | Origin `http://127.0.0.1:4610` and `http://localhost:4610` 200; `http://localhost:5173`, `http://127.0.0.1:5173` (no `PLATE_DEV`) and a foreign origin 403; foreign Host 403; no token and a wrong token 401; `POST /api/v0/nope` a JSON 404; `/health` carries `plugin {last_poll_utc, queued}` and `alerts_recent` | pass |

Wave 9 and Wave 10 in the same browser session, beyond the plan's steps:

| # | step | observed | result |
|---|---|---|---|
| W1 | Viewer on candidate 1; Re-mask (free) with feather 8 | `Re-masked as run_01M56X2H04XSZ598H20B0TAH9M · DONE · 0.00 USD`; the strip now holds 3 takes, the accepted one still picked | pass |
| W2 | G2 repro: with the approved `edit-1` wired into Motion and the re-mask take beside it, Make bar `Compare engines…`; tick Fake and Nano Banana Pro (Nano Banana 2.1 and Seedream 5 Pro cannot be ticked); Quote | `2 picked (2 to 5)`; quote `fake 0.040 USD · nano-banana-pro 0.24 USD unconfirmed · 0.28 USD (2 engines) · D · ETA ~60 s`; the save chip keeps saving; the daemon log shows 28 `PUT /projects/smoke/graph` 200 and no 422; `edit-1` still `approved: true` on its picked take | **pass (G2 closed)** |
| W3 | G2, second half: pick the re-mask take (strip key `2`), then the accepted one again (`1`) | picking the unaccepted take drops `✓ approved`, draws the Edit-to-Motion edge red and dashed and the Motion card says it is locked; the save still answers 200, with `violations: [{edge edit-1 → motion-1, code VIDEO_LOCKED}]` and the stored edge `locked: true` (T111). Picking the accepted take back restores `approved: true` and clears the lock | pass |
| W4 | Spend, Settings, Enlarge | Spend: `quoted 0.080 USD`, `billed 0.00 USD`, 3 generations, 1 accepted, by week and engine; Settings: read-only config; Enlarge: the viewer fills the canvas (1279×652) | pass |
| W5 | History tab | lists only the runs it had when it last loaded; Refresh shows the new ones. The panel does not follow run events | minor (unchanged) |
| W6 | T112: a clean project `panels`; add Comp, Colour, Atmosphere, Sky, Frame, Recolour, LightMix and open each editor | every one has a panel: Comp (layer order and reset, require accepted takes, preview coverage), Colour (scope auto/mask/ids/frame, exposure EV, tint, contrast, saturation, hue, white balance toggle, reset, preview), Atmosphere (visibility slider and metres field, fog colour from the sky, sky fog, height falloff, preview), Sky (picture hash or wired Reference, scale, horizon, slide, tone match, preview), Frame (title, width, height, shot/set/sequence sizes), Recolour (target with the RAL grid, project swatches, method auto/recomposition/ratio/degraded, source Lab, a pass of up to 8 materials with add/estimate/clear), LightMix (gains with reset, presets dusk, moonlight, luxury exterior, artificial light, max gain, keep base, NNLS seed). The graph saves 200 throughout | pass |
| W7 | T113: add a Reference, upload the synthetic swatch, wire it into `edit-1`'s `references`; prompt with `@ref1`, Quote on Nano Banana Pro; then `@ref2` | upload `POST /assets?kind=reference&project_id=smoke` 201 (`reference 97034c346f`, 256 x 256); the Edit editor shows `@ref1 names the wired reference (@ref1 = Reference); the engine's own wording replaces each at the quote`; the quote's runs carry `prompt: "smoke: match the shrub foliage to reference image 1, same light"`, the artist's text in `prompt_raw` and `reference_images`; `@ref2` gives `422 bad_ref_token: the prompt names @ref2, but 1 Reference image is wired to this Edit; wire the Reference node or fix the number` in the Make bar and no run row | pass |
| W8 | T114: the UI export is the safe frame (row 14); then `full_frame: true` by curl on both still routes | `POST /projects/smoke/export {run_ids, out_dir, format: png16, full_frame: true}` writes `edit-1_KQ9DRQME.png` at 640×360 with `full_frame: true` and the placed `safe_frame` in its entry. `POST /runs/{id}/export {..., full_frame: true}` answers `422 {"errors":["full_frame: Extra inputs are not permitted"]}` | **fail (G3)** |

### G2 is closed

T111 lets the run behind the node's picked `outputHash` decide approval. Rows W1 to W3 show both halves on a real daemon: a newer, unaccepted take no longer unapproves the node or blocks saves and quotes, and picking that take marks the Motion edge locked inside a 200 save instead of refusing the graph. `scripts/smoke_e2e.py` now walks the same path (`g2.motion_wired`, `recomposite`, `g2.autosave`).

### Gap G3: the single-run export has no `full_frame`, and `make test` fails on it

Repro: `.venv/bin/pytest daemon/tests/test_app.py::test_packet_to_export_on_the_project_cas` (fails alone and in the suite). The test ingests the `tierA_still_clubhouse` example (3840×2160, `camera.safe_frame {x 0, y 277, w 3840, h 1606}`), runs the seed fake (a 64×64 take) and exports through `POST /runs/{id}/export`. Since T114 the export places the safe frame by default, cannot place it on a take of another aspect, and refuses: `409 not_exportable: the take is 64 x 64 and the packet renders 3840 x 2160 with the safe frame {...}; ... Export it with full_frame to keep the whole take`. That route's `ExportBody` (`daemon/plate_daemon/routes/runs.py`, `extra="forbid"`) has no `full_frame`, so the advice it gives is refused with 422 (row W8).

Cause: T114's spec covers "still export" with `full_frame: true` in the request, but its `files:` list had `export.py`, `routes/export_batch.py`, `routes/delivery.py` and `delivery/frames.py`, not `routes/runs.py`, where the single still export route lives, nor `daemon/tests/test_app.py`. `export.export(..., full_frame=...)` exists; nothing passes it from that route. The refusal itself is right (a safe frame must not be guessed onto a take of another aspect); the gaps are the route and the stale test. The UI Export editor and `plate export` also have no way to ask for the whole frame (the batch export and delivery routes do).

Fix (needs a task; files `daemon/plate_daemon/routes/runs.py`, `daemon/tests/test_app.py`, optionally `ui/src/editors/ExportEditor.tsx`, `daemon/plate_daemon/cli.py` and the stale comment at `scripts/smoke_e2e.py:671`): add `full_frame: bool = False` to `ExportBody` and pass it to `export.export`; put the framing (`crop` or `full_frame`) into the route's ledger record and response the way `routes/export_batch.py` does; change `test_app.py` to assert the 409 on the mismatched take and then export with `full_frame: true` (the same shape T114 gave `test_routes_runs_wave8.py:452`); optionally a "whole frame" checkbox in the Export editor and `--full-frame` on `plate export`. Then `make test` should be green again; rerun T53's done-when.

The patch the 13:10 retry wrote (reverted, not applied):

```diff
--- daemon/plate_daemon/routes/runs.py  (ExportBody)
     override: bool = False
     reason: str | None = None
+    full_frame: bool = False
--- daemon/plate_daemon/routes/runs.py  (export_route)
-            done = export(
-                run, fmt, out_dir, typed, conn=conn, cas=c.cas_for(pid), project_dir=project_dir, name=name
-            )
+            done = export(
+                run,
+                fmt,
+                out_dir,
+                typed,
+                conn=conn,
+                cas=c.cas_for(pid),
+                project_dir=project_dir,
+                name=name,
+                full_frame=body.full_frame,
+            )
                 "by": "human",
+                **done.framing.record(),
             },
         ...
             "shipped": shipped,
+            **done.framing.record(),
         }
--- daemon/tests/test_app.py  (test_packet_to_export_on_the_project_cas)
         dest = tmp_path / "out" / "result.png"
         r = c.post(f"/api/v0/runs/{run_id}/export", json={"dest": str(dest)}, headers=auth)
-        assert r.status_code == 200 and dest.is_file()
+        assert r.status_code == 409 and r.json()["error"] == "not_exportable", r.text
+        assert "full_frame" in r.json()["detail"] and not dest.exists()
+        r = c.post(f"/api/v0/runs/{run_id}/export", json={"dest": str(dest), "full_frame": True}, headers=auth)
+        assert r.status_code == 200 and dest.is_file(), r.text
+        assert r.json()["full_frame"] is True and "crop" not in r.json()
```

Also update the route's module docstring (the `POST /runs/{id}/export` line) to list `full_frame?`. A suggested done-when: `.venv/bin/pytest daemon/tests/test_app.py daemon/tests/test_routes_runs_wave8.py daemon/tests/test_export_safe_frame.py daemon/tests/test_pipeline_e2e.py -q` passes, then T53's chain.

### Corrections to the plan's smoke text (T53 spec)

| | the plan says | the code does | what the steps above use |
|---|---|---|---|
| C1 | `curl ... -d '{...}'` with no content type | `422 {"errors":["<body>: Input should be a valid dictionary ..."]}` | `-H "Content-Type: application/json"` |
| C2 | token in `/tmp/plate-smoke/token` | no token file; the token is in `<home>/config.json` (`PLATE_TOKEN_FILE` only when set) | `scripts/plate_config.py token` |
| C3 | register `fixtures/packets/room_a` for project `smoke` | the manifest names `plate-fixture`; posted by path, the packet is filed under an auto-created `plate-fixture` project and `smoke` lists nothing | stage a copy with `project_id: smoke` before the first post (what `scripts/smoke_e2e.py` does) |
| C4 | Edit with engine `nano-banana-2.1`, Quote, Make | Nano Banana 2.1 is disabled in the Make bar until its manifest has a vendor model id (T102, decision 51: `model id unknown until the P0 smoke test`) | quote Nano Banana Pro for the D quote line, Make on `fake`; box-day item 20 fills the model id |
| C5 | `localhost` is allowed only under `PLATE_DEV=1` | `http://localhost:4610` is always allowed; `PLATE_DEV=1` adds only the two `:5173` origins (as `docs/ui.md` says) | the printed `127.0.0.1` URL |
| C6 | the Gate panel shows a pass report | `advisory`: decision 35 makes `feather_band.de00_p99` advisory on every in-mask edit until calibration | advisory accepted as the expected verdict |
| C7 | (first pass) the verdict wrote into the repo's `calib/labels.jsonl` | resolved by T110: labels live in `<PLATE_HOME>/calib/labels.jsonl` | nothing extra |
| C8 | `PLATE_HOME=/tmp/plate-smoke` | any throw-away folder works | `$HOME/plate-smoke-home` |
| C9 | candidates appear, then viewer and accept | clicking a candidate compares it; its digit key in the strip picks it as the node's output (what an Edit to Edit chain and a Motion start need, decision 47); Export picks from its own list either way | row 13b |
| C10 | Export (png16) writes a file with lineage | it writes the packet's safe frame by default (T114): `room_a`'s 2.39:1 band, 640×268 of the 640×360 render; the whole render needs `full_frame: true` (batch export and delivery today; see G3) | row 14 |

## 3. Tests skipped on this Mac

| suite | skipped | why |
|---|---|---|
| pytest | `daemon/tests/test_previs.py:396` | Homebrew's ffmpeg has no `drawtext` filter (built without libfreetype); the overlay backend that burns the PREVIS watermark is covered instead |
| pytest | none of the client-data tests | `/Users/aasish/Desktop/test` is present, so they ran; on a machine without it they skip through `plate_gates.tests.clientdata.path` |
| pytest | none of the PySide6 dock tests | they ran offscreen |
| vitest, claims, scripts | none | |

Not skipped, but outside what a test can show here: no test talks to a real `pymxs`, V-Ray, Vantage, CUDA, NVML, STAFFAGE or a vendor; the gate's optical flow ran on OpenCV DIS (the report says so), not RAFT; Real-ESRGAN (T100) and the Tier C models are stubs.

## 4. Verifier pass: BUILD_PLAN decisions 1 to 30

Status words: **done** (built and tested here), **partial (task)** (some of it is missing; the task named owns the rest), **deferred-to-box** (code done and fake-tested; the named verify-on-box line proves it), **deferred-to-P5**, and **n/a** (not a code decision; who owns it is named).

| # | decision | status | evidence or remainder |
|---|---|---|---|
| 1 | Input scope: Tier A packets, Tier C plates, client kit | done; deferred-to-box (T21, T22) | Tier A ingest (smoke above), Tier C route with consent record (T38, T72), kit MAXScript and `kit_to_packet` (T21), external still and video import as runs (T74, T87). On the Mac the Tier C models are stubs |
| 2 | Colour master is the VFB TIFF16; P0 OCIO re-derivation check | partial (T9 on the box) | OCIO core and TIFF16 master (T3) done; exports carry an ICC profile (row 14, T80). The TIFF16 vs OCIO ΔE00 p99 ≤ 0.5 check and the un-tone-mapped egress A/B need the box and a live vendor (`docs/colour.md` rows open) |
| 3 | Egress: 8-bit sRGB PNG with ICC, overscan pad, region crop | done | T4, T14; region crop never upsamples (open question 9) |
| 4 | Ingress: ECC registration, Lanczos, ring tone match, feather inside the mask | done | T4, T25; AI layers smaller than the region may go through Real-ESRGAN instead of Lanczos, plates never (T100, decision 55); outside-mask ΔE00 0 in every gate report above |
| 5 | Default element set, overhead caps | deferred-to-box (T19) | elements forced by the plugin (T9, fake-tested); the overhead number is a box measurement |
| 6 | Packets render in the interactive Max session; Vantage by vrscene | deferred-to-box (T9, T37) | Chaos questions (CLI licence, background renders) are Aasish's emails |
| 7 | Backend shape: one daemon, WAL index in app data, CAS, separate GPU worker, UI served by the daemon | done | proven live in this pass (two daemons, index under `PLATE_HOME`, built UI at `/`, in-process pixels backends); the Tailscale bind is off by default |
| 8 | LLM route: Anthropic API, Sonnet composer, Opus two-sample judge | done | T33 on recorded fixtures only; Anthropic is effective grade D until terms are filed, so NDA projects get no composer and no judge (claim `judge_cannot_overrule_numbers`) |
| 9 | Custody grades C0 to D, NDA on C0/C1 | done | T14; claims `nda_c0_c1_only`, `pending_terms_count_as_d`, `nda_egress` |
| 10 | Retroactive contract review | n/a (Aasish, counsel) | the code side is done: consent template (`docs/consent-template.md`) and custody per project |
| 11 | GPU and RAM admission by NVML, explicit unload, RAFT ≤ 960 px | deferred-to-box (T22) | admission, windows and slots built and tested (T22) |
| 12 | Host scope: 3ds Max 2026 only | done | schema `host.app: 3dsmax \| none`; no other sender |
| 13 | Engine roster, pins, retirement alerts, same-model fallback only | done; deferred-to-box (live smoke) | 14 manifests plus the fake (Veo 3.1 as a start/end engine, T94), adapters on recorded fixtures (T17, T18), pins and alerts (T27), price tiers (T84), route health (T85), `reference_token` wording per engine (T113); prices `unconfirmed`, 9 manifests flagged `unverified_p0`; Nano Banana 2.1 and Seedream 5 Pro cannot be picked in the UI until their model ids are known (C4) |
| 14 | One gate value per metric, resolution stated | done | values as corrected by plan decision 3 and decision 35, with `calibration_needed` where the plan says so; 4K gate 4.70 s on CPU, 4.95 s with the viewer overlays, on a loaded machine (T67, T109) |
| 15 | Render-it router: advisory in P1, blocking in P3 | done (advisory) | T28; `PLATE_ROUTER_MODE=blocking` exists; turning it on is a P3 decision after calibration. Every quote above carried the router verdict (`ai_by_region`) |
| 16 | Re-photograph close and medium frames only | done | router doctrine by shot class; cutdowns are downsample-only crops, never generated close-ups (T86) |
| 17 | Client revisions: packet invalidation, approved layers carry over | done | T41; automatic carry-over on supersede (T96, T107, default on) |
| 18 | Productisation gate at week 16 | deferred-to-P5 | numbers from the ledger and the interviews |
| 19 | Hiring | n/a (Aasish) | |
| 20 | Phase order: stills first | n/a (sequencing) | the build followed it; the swap decision is a box-day baseline number (open question 11) |
| 21 | Client review as a static bundle and a comment form | done | T40, T86 (review cover); a bundle built from the UI opened offline in this pass; R2 hosting is a documented manual step (`review_template/HOSTING.md`) |
| 22 | Agent permissions over MCP | done | T39, T80; claim `mcp_cannot_approve_or_deliver` |
| 23 | Backup, retention, hash-chained ledger, purge | done | T42, T13; the offsite copy and the quarterly restore drill are operations (`docs/runbooks/backup-restore.md`) |
| 24 | VizMaker benchmark on owned scenes | n/a (optional, Aasish) | the parity audit (Wave 9) compared features, not output quality |
| 25 | Max-side install: startup script, handshake, uninstall, Safe Scene check, shared bridge | deferred-to-box (T20, T49) | T8, T20, T49, T75, T77, T104 fake-tested |
| 26 | Lineage inside every output plus a phash index | done | T26 (EXR attribute, PNG iTXt, TIFF XMP, MP4/MOV udta), IPTC `digitalSourceType` on AI-touched outputs (T86); PNG lineage read back in this pass |
| 27 | Gate report versioning; re-scoring never rewrites a shipped report | done | `rescore` gave versions `[1, 2]` in the scripted smoke; claim `shipped_report_never_rewritten` |
| 28 | Human-minute timer in the app and a CLI | done | T36, T38, T78 (`plate timer`); the P0 baseline is Aasish's |
| 29 | One consolidated budget | n/a (Aasish) | `plate spend` and the Spend tab (T88, T102; row W4) report it |
| 30 | Windows first-class from day 1 | partial (T11 and T80 on the box) | hazard scan, portability and PowerShell static tests are green on the Mac. The suite has never run on Windows, and CI has never run because there is no git remote (open question 13) |

## 5. Verifier pass: section 4 and 5 deliverables, P0 to P4

### Architecture (section 4)

| component | status | note |
|---|---|---|
| Shot Packet v1 (manifest, schema, legend, policy, transport over HTTP, BLAKE3 on ingest, SSE) | done | spec 0.1.1; packet events streamed in this pass |
| Max plugin actions: Send Packet, Batch Cameras, Send Motion, Receive, Light, Scout, Vantage hooks, level-and-crop, Send + quote | deferred-to-box (T19, T37, T77, T104) | all fake-pymxs tested; every V-Ray/Vantage property name is an unverified candidate (the code marks each one) until `plugins/max/ONBOX.md` runs |
| Daemon state machine QUOTED-first, idempotency, boot re-poll, best-of-N under one quote, priority classes, budgets | done | fan-out of 2 under one quote, one approval for the group, in this pass |
| Routing: manifests as data, custody first, nightly prices, daily reconciliation, contract tests, engine cards, canaries | done; deferred-to-box (live) | the daemon assumes routes healthy until canaries run (decision 44) and says so in the quote; route degradation after 3 failures (T85) |
| Pixels service with the licence registry | done (stub); deferred-to-box (T22) | real DA-V2 Small, GeoCalib, SAM 2.1, RAFT, PaddleOCR need the CUDA extra |
| Canvas: node kinds, viewer, enforced rules, project folder, rebuildable index | done | everything drove in a real browser; G2 closed (T111); every numeric node has a parameter panel (T112) |
| Export and delivery framing | partial (G3) | the safe frame is the default for still export, batch export and frame/clip delivery (T114, row 14); `full_frame` works on batch export and delivery but not on the single-run export route the UI uses |

### P0 deliverables and exits

| item | status |
|---|---|
| PACKET.md and schema | done (0.1.1) |
| on-box spike report | deferred-to-box (T9; `docs/onbox-spikes.md` rows all "not run") |
| conformance scene with the reprojection check | done for the Blender fixture (`fixtures/tests/test_packets_valid.py::test_marker_reprojection_rms_under_half_a_pixel_at_640_wide`, 8 markers); the Max scene is box work (open question 8). The plan's exit says 3840 wide; the fixture measures at 640 |
| provider matrix for ten engines | partial: manifests and `docs/custody.md` done; billed price to the cent needs `plate smoke --live` (≤ USD 5) |
| labelling UI and the first 100 labels | UI done (T23); labels are Aasish's (none yet); they live in the data folder (T110) |
| baseline of two live jobs; day-1 emails | n/a (Aasish) |
| client-consent template | done |
| exits: real camera packet reprojects ≤ 1 px; markers ≤ 0.5 px RMS at 3840; TIFF16 vs OCIO; element overhead; billed price; Windows suite and CI green | deferred-to-box (T9, T11, T19); identity round trip at ΔE00 p99 ≤ 0.5 is done (T4) |

### P1 deliverables and exits

| item | status |
|---|---|
| gates package (register, comp_masked, labmatch, gbuffer edges, invention, depth agreement, maskdiff, texture, tone, crop pair, video) with goldens | done (601 gate tests, procedural and Blender goldens) |
| adapters for Nano Banana 2.1 and Pro, GPT Image 2.5, Seedream 5 Pro, Kling O3 4K and v3 Pro, Seedance 2.5 i2v and r2v, direct Gemini and OpenAI | done on fixtures; deferred-to-box (live smoke); Nano Banana 2.1 via Gemini, Magnific and Seedance r2v via BytePlus/WaveSpeed have no recorded call yet |
| ledger and daily reconciliation | done (T13) |
| `plate run / gate / export` with three Lensbook recipes | done; the Lensbook stills a01 to a10 ship as runnable recipes and 12 motion recipes from the shot library (T83, T94); `plate export` has no `--full-frame` yet (G3) |
| advisory render-it router; calibration report | done (the report needs labels) |
| exits: AUC ≥ 0.85 on ≥ 100 takes; two live client jobs; ledger equals billing within 1% | deferred (labels and live jobs) |
| exit: gate run on a 4K still ≤ 10 s | done on CPU: `run_gates 3840x2160: wall 4.70s` |

### P2 deliverables and exits

| item | status |
|---|---|
| canvas, Make bar (provider, model, price, custody, ETA), N fan-out, enforced rules | done (this pass), including the cross-engine compare quote (T95, T102; row W2) |
| WebGL viewer | done: wipe, toggle, difference, edges, ΔE00 heatmap, tiles, Enlarge, all live |
| lineage restore by drop; open project format | done (T26, T36; drop was not exercised in the browser here) |
| Max dock (Send Packet, Batch Cameras, Receive), startup installer, fake-pymxs and import-contract tests | deferred-to-box (T19, T20) |
| MCP server; static review bundles; Tier C ingestion | done |
| exits: a ≥ 6-camera client set; minutes ≤ 50% of baseline; Max click to first candidate ≤ 60 s; a client opens a bundle; a Sonnet agent runs a batch over MCP; the canvas from the box, a second Windows machine and the Mac; a fresh Windows bootstrap in < 15 min | deferred-to-box; the Mac browser leg is done (this pass) |

### P3 deliverables and exits

| item | status |
|---|---|
| Move node with the Lensbook risk policy and the router | done (T43, T94; advisory until P3 flips the mode) |
| CropPair; Kling O3 4K and v3 Pro start/end adapter; Veo 3.1 start/end | done (fixtures) |
| Send Motion in Max | deferred-to-box (T37, T77) |
| Seedance 2.5 r2v previs with PREVIS watermark | done (T44, fixtures; watermark by the overlay backend on this Mac) |
| video gates with drift and the frame-rate time map | done (T29; DIS here, RAFT on the box: T29 line) |
| People node over STAFFAGE with packet-Z occlusion; people in motion | done against the fake STAFFAGE (T45, T105) |
| FCPXML, ProRes 4444, EXR delivery | done (T46, route T74, UI T106; safe-frame crop by default, `full_frame` on the deliver route, T114) |
| local Wan 2.2 VACE previs | deferred-to-box (stub; T22) |
| exits: a shipped 30 to 60 s film section; gates agree with ≥ 80% of ≥ 30 rejections; ≤ 2.5 generations per clip; zero accepts without a scrub | deferred (live work); the scrub rule is enforced (this pass, row 11) |

### P4 deliverables and exits

| item | status |
|---|---|
| Set node with per-material targets and the cross-view gate | done (T30, T48) |
| Light node over MaxGaffer with Vantage live-link preview | deferred-to-box (T37, T77) |
| LightMix NNLS | done (T32; presets and per-channel gains T89; every preset and gain has a control, T112, row W6) |
| Recolour by recomposition, validated once against a V-Ray re-render | code done (T31, T48, several materials in one pass T90, editor T91/T112); validation deferred-to-box (T31) |
| Scout node; shared Max bridge | deferred-to-box (T37, T49) |
| client-revision carry-over; backup and purge | done (T41, T42, T96) |
| runbooks and the manual-pipeline fallback | done (T51, T80, T108: twelve runbooks) |
| exits: eight-camera villa set at cross-view p90 ≤ 3; a V-Ray dusk variant through the Light node; RAL panel ≤ 1 vs a re-render; ≥ 3 deliverables a week | deferred-to-box and live work |

## 6. Verifier pass: Wave 9 decisions 46 to 58 (VizMaker parity)

| # | decision | status | evidence |
|---|---|---|---|
| 46 | References are a node, never pixels of the output | done | T81, T82, T92, T113: `test_canvas_rules_wave9`, `test_mask_tools_wave9`, `test_pipeline_wave9`, `test_prompt_refs`; row W7 |
| 47 | An Edit downstream of a take starts from that take (`inputs.base`), 422 `no_base` | done | T92 (`test_pipeline_wave9`); the base is the upstream node's picked `outputHash` (C9); `night_chain` reports `no_base` in the scripted smoke |
| 48 | Comp, Colour, Atmosphere, Sky are numeric nodes | done | T93 (`test_numeric_nodes_wave9`); their editors T112 (row W6) |
| 49 | Motion end still and elements; locked and pull-back crop pairs | done | T94 (`test_motion_wave9`); end-still camera check and VIDEO_LOCKED on either still |
| 50 | Recipes from shipped, studio and project folders; saved from the UI | done | T83, T97 (`test_recipes_wave9`) |
| 51 | Engine guidance measured: cards, capability chips, route health, quote_many | done | T83, T85, T95, T102; chips in row 7, compare quote in row W2 |
| 52 | Prices tiered by the params that change the bill | done | T84 (`test_price_tiers`, `test_quotes_tiers`); Nano Banana 2K/4K `estimated` |
| 53 | Batch is a template, not a canvas-wide Make; Send + quote | done; deferred-to-box (T104) | T95 (`test_templates_wave9`; `edit_chain` in the scripted smoke), T104 fake-tested |
| 54 | Recomposite is a free attempt | done | T99 (`test_recomposite_wave9`); row W1: 0.00 USD run |
| 55 | AI layers may be upscaled at ingress; plates never; Magnific moved | done (stub) | T100 (`test_layer_upscale`, `test_upscale_wave9`); Real-ESRGAN is a stub on the Mac |
| 56 | Social cutdowns are downsample-only crops with IPTC provenance | done | T86 (`test_export_wave9`) |
| 57 | Frames are annotations | done | T81 (`test_canvas_rules_wave9`); the Frame editor sets title and size only (row W6) |
| 58 | Two-point perspective cameras become level-and-crop packets | deferred-to-box (T104, T114) | `plugins/max/tests/test_camera_level.py`; exports now crop to the safe frame these packets declare (T114); the V-Ray shift unit is a box measurement |

Doctrine held across Waves 9 and 10: no text-to-image path (generation is anchored to a plate camera; the Make bar refuses a node with no plate upstream), no plate upscaling (T100 upscales AI layers only), no invented angles (cutdowns and safe-frame exports are pixel crops; references are engine inputs and never composite).

## 7. Wave 10 (T111 to T115)

Each task's own done-when, re-run here, and what the browser showed.

| task | done-when re-run | browser | status |
|---|---|---|---|
| T111 G2: a new take on an approved Edit does not lock the graph | `pytest daemon/tests/test_projects_g2.py daemon/tests/test_projects_wave8.py daemon/tests/test_routes_packets.py`: `85 passed` | rows W1 to W3 | done |
| T112 parameter panels for the nodes that had none | `npm run typecheck` exit 0; `vitest run src/test/nodeParamPanels.test.tsx`: `44 passed`; the whole UI suite 796 | row W6; the `@ref` hint in row W7 | done |
| T113 `@ref1..@refN` in prompts | `pytest daemon/tests/test_prompt_refs.py daemon/tests/test_pipeline_wave8.py daemon/tests/test_pipeline_wave9.py`: `61 passed` | row W7 | done; the wording per engine is box-day item 16 |
| T114 export crops to the camera's safe frame | `pytest daemon/tests/test_export_safe_frame.py daemon/tests/test_export_wave9.py daemon/tests/test_routes_runs_wave8.py`: `93 passed` | rows 14 and W8 | partial (G3): `daemon/tests/test_app.py` fails and the single-run route lacks `full_frame` |
| T115 smoke covers Wave 9; housekeeping | `scripts/smoke_e2e.py`: `SMOKE PASS`; `claims_check.py --skip-ui`: 28 claims, 136 tests; `hazard_scan.py --strict-allow`: clean | (scripted) | done; `.smoke/` is in `.gitignore` and the stale `previs_is_not_deliverable` caveat is gone |

## 8. Wave 9 open questions running on their defaults

The build used these defaults (router plan, open questions 19 to 28). Each is a decision for Aasish; changing one is a small task.

| # | question | default in effect |
|---|---|---|
| 19 | Prices for unbilled tiers | Nano Banana 2K and 4K quote at 2× and 4× the 1K figure with `price_status: estimated`, shown amber |
| 20 | Reference as a node or an Edit param | a node (thumbnail, reuse across Edits, a typed port) |
| 21 | Painting at plate resolution inside the viewer | deferred; the paint raster is capped at 2048 on the long side and the daemon scales masks to the packet |
| 22 | People in motion in Wave 9 | built (T105), self-contained behind an install-wrapper |
| 23 | RAL Classic table source | community sRGB approximations with a licence note; the 15 existing values pinned |
| 24 | Magnific endpoint before the smoke test | switched to `api.magnific.com` now |
| 25 | Automatic carry-over on supersede | on (`settings.auto_carry_over: true`); every carried layer is a new ledgered run the artist still accepts |
| 26 | Frame nodes | a plain annotation box, no grouping or collapse |
| 27 | Colour node gate | structure and outside-scope hard, material ΔE advisory; the output carries `graded` in its lineage |
| 28 | Veo 3.1 as a start/end engine | in `START_END_ENGINES`, custody D until written terms (NDA projects never see it) |

## 9. Box-day checklist

Every `verify-on-box:` line in the router plan, in task order, then the box-only exits that have no task line.

1. T8: none yet (fake only); the property names in `FakeCamera` are a hypothesis until T9's on-box list runs.
2. T9: run `plugins/max/ONBOX.md` steps 1 to 7 in Max 2026 with V-Ray 7 on a TULA camera; record results in `docs/colour.md` and `docs/onbox-spikes.md`; fix every candidate name the code still marks as unverified.
3. T11: `scripts\bootstrap.ps1` then `scripts\test.ps1` on the render box, all green (P0 exit).
4. T19: Send Packet on one TULA camera; `plate_spec.validate_packet_dir` passes on the box; three known vertices reproject within 1 px; element overhead ≤ 15% time / ≤ 10% RSS recorded in `render.element_overhead`.
5. T20: `plugins\max\scripts\install.ps1 -RepoPath <clone>`; restart Max; the `PLATE_Dock` macro opens the dock; Safe Scene Script Execution leaves it running; `uninstall.ps1` removes it cleanly.
6. T21: run the kit on a Sthyra-owned scene in Max 2026; the EXR has the 7 layers (check with `plate_pixels.exr` on the box); `kit_to_packet` validates.
7. T22: `uv sync --extra gpu` with torch for CUDA 12.8+; `PLATE_PIXELS_REAL=1 python -m plate_pixels` loads DA-V2 Small, SAM 2.1, RAFT and GeoCalib on the RTX 5090; `/admission` reports real headroom with Vantage running.
8. T29: run `camera_lock` with `backend=raft` at 960 wide on one accepted and one rejected Kling take; record the medians in `docs/gates/video.md`.
9. T31: one RAL panel recoloured in PLATE vs a V-Ray re-render, ΔE00 ≤ 1 (P4 exit).
10. T37: Send Motion on a 25-frame TULA move; the packet validates and frame 0 matches the hero still. Light match on one camera with MaxGaffer installed; the re-rendered packet supersedes the old one. The Vantage live link starts.
11. T49: PLATE and MaxGaffer docks open together without a PySide6 or callback collision across a file open and a render.
12. T77: a Light match from the canvas reaches Max, re-renders and supersedes; Send Motion from the dock on a 25-frame move.
13. T80: `scripts\test.ps1` on the render box runs the same steps as `make ci`.
14. T104: a V-Ray Physical Camera with auto vertical tilt on the conformance scene: the level-and-crop packet's safe-frame crop matches the tilt-corrected render within 0.5 px (reprojection exit); the manual `vertical_shift` unit is measured and written into `camera_level.py`.
15. T109: in a browser against the box daemon, `e` and `h` draw on a real take.
16. T113: the live smoke confirms each engine's `reference_token` wording.
17. T114: a level-and-crop packet from T104 exports at the camera's delivery size.
18. P0: TIFF16 vs OCIO re-derivation ΔE00 p99 ≤ 0.5, or the mismatch understood and the master chosen (`docs/colour.md`).
19. P0: the conformance scene built in Max from `fixtures/blender/markers.json`; eight markers reproject within 0.5 px RMS at 3840 wide.
20. P0: `plate smoke --live` with a stated budget (≤ USD 5): billed price per engine to the cent, the vendor model ids behind every `unverified_p0` flag (this also enables Nano Banana 2.1 and Seedream 5 Pro in the Make bar), and a recorded minimum-cost call for Nano Banana 2.1 on the Gemini route.
21. P0: push the repo to a private remote so the `windows-latest` and `macos-latest` CI legs run once (open question 13).
22. P2: the same daemon driven from a browser on the box and on a second Windows machine (this pass covered the Mac); a fresh Windows machine from bootstrap to first candidate in under 15 minutes.

## 10. Other observations

- G3 (section 2) is the one open defect; it keeps `make test` red and needs a small task before the next commit. The patch is written out in section 2; T53 may not apply it (outside its files).
- Do not run `bash fixtures/blender/render.sh` while pytest runs: it re-validates the committed packets in place and a test that reads `fixtures/packets/room_a` at that moment fails with `FileNotFoundError` (section 1).
- The Export editor's take picker labels fan-out siblings identically (`run_01M5 · attempt 1 · fake · DONE` twice): runs made in the same minute share the ULID prefix the label shows, so the artist cannot tell the accepted take from its sibling there. Showing the accepted mark or more of the id would fix it.
- After a refused wire into Motion (row 17), the `VIDEO_LOCKED` note stays on the Motion card when a later, allowed wire succeeds; it clears when the project is reopened. Cosmetic.
- The History tab does not reload on run events (row W5); Refresh shows the runs.
- An Edit card keeps saying `engine: pick in the Make bar` after its runs finish on `fake`; the Make bar does not write the engine into the node's params. Cosmetic.
- CLI fixture smoke skipped 8 routes: Magnific and Nano Banana 2.1 via Gemini (no recorded call, no vendor model id: `unverified_p0`), Nano Banana 2.1 via Atlas (no adapter speaks the route), Seedance 2.5 r2v via BytePlus and WaveSpeed (no recording), and the three local stubs (Qwen, Real-ESRGAN, Wan 2.2). Seedream 5 Pro passes the fixture smoke yet is disabled in the Make bar: the UI goes by the manifest's model id, the smoke by the recording. Both clear with box-day item 20.
- The full scrub on the 640×360 fixture plate in a small viewer is complete at the centre view at 100%: the four quadrant centres fall on the viewport edges, and the comparison is inclusive. That is correct by the rule's definition. A 4K plate needs real panning.
- With a fresh quote, Make approves directly as the human (`approved_by: human`); the approval dialog appears only when the daemon refuses (auto cap, envelope). This matches `docs/ui.md`.
- `room_a` and `room_a_ral3020` are 3.78 MB, over the 3 MB per-packet guideline (known since wave 2); the 25 MB total cap holds at 17 MB.
- `make test` stops at the first failing step, so a pytest failure hides the vitest result; run `npm test` on its own when that happens (row 3a).

## 11. Wave 11 (T116 to T125): Runway as the video provider

Written by the Wave 11 verification pass (router task T125, Opus 5.5) on 2026-10-18 between 18:25 and 19:08 IST, on the same Mac. Commit `98cc92f` plus the uncommitted Wave 11 edits in the working tree (T116 to T124, and the two fixes below). Nothing here called Runway or any other vendor. Every Runway number comes from the recorded synthetic fixtures under `engines/plate_engines/contract/runway/`, and the live smoke is box-day item 23.

### 11.1 What failed and the fixes

The first two `make ci` runs each stopped at one step. Both causes were cosmetic leftovers of wave 19. Neither touched behaviour or a Wave 11 decision.

- **`make lint`: `ruff format --check` flagged 2 files.** These were T120's edits to `daemon/tests/test_motion_croppair.py` (two lines past the wrap, one of them an assert over three `and` terms) and `daemon/tests/test_motion_wave9.py` (the Veo 5 s → 6 s assert). Fix: `ruff format` on the two files. The trailing comment on `make_plan(still, engine_id="seedance-2.5-r2v")` moved to its own line above the call. No assertion changed.
- **`make build`: `tsc -b` stopped with `src/test/makeBarRunway.test.tsx(14,23): error TS6133: 'MakeBar' is declared but its value is never read`.** This was T123's new test. vitest does not typecheck, so the suite had passed. Fix: the unused `MakeBar` import is gone (`engineChips` stays). `npx tsc --noEmit -p .` and `tsc -b` are clean.

### 11.2 Command record

All commands ran from the repo root.

| # | command | exit | key output |
|---|---|---|---|
| 1 | `perl -e 'alarm shift; exec @ARGV' 1500 make ci` (first) | 2 | `SELFTEST PASS`; hazard selftest and scan clean; `ruff check` `All checks passed!`; `ruff format --check` `2 files would be reformatted, 529 files already formatted` (fix 1) |
| 2 | the same (second) | 2 | lint `531 files already formatted`; licences OK; pytest `6241 passed, 1 skipped in 704.84s`, 4K gate `run_gates 3840x2160: wall 3.85s`; `scripts/tests` `79 passed`; claims `all 28 claims hold (136 tests passed)`; vitest `Test Files 28 passed (28)`, `Tests 805 passed (805)`; `npm run build` TS6133 (fix 2) |
| 3 | the same (final) | 0 | `SELFTEST PASS`; hazard `clean (1026 files scanned, 2 allowlist entries)`; `All checks passed!`, `531 files already formatted`; licences `17 files scanned, 7 blocked models absent`; pytest `6241 passed, 1 skipped in 708.00s`, 4K gate `wall 3.85s` (`4.17s` with overlays); `scripts/tests` `79 passed`; `all 28 claims hold (136 tests passed)`; vitest `28 passed (28)` files, `805 passed (805)`; `built in 1.22s`; 12 min 58 s wall |
| 4 | `scripts/test.sh` | 0 | the same eleven steps in the same order: pytest `6241 passed, 1 skipped in 696.39s`, scripts `79 passed`, claims 28 and 136, vitest `805 passed (805)`, built; last line `All PLATE checks passed.` |
| 5 | `.venv/bin/python -m plate_daemon.cli smoke` | 0 | last line `fixture smoke: 17 adapters ok`, no `failed`; 8 skipped (the same 8 as section 10) |
| 6 | `perl -e 'alarm shift; exec @ARGV' 900 .venv/bin/python scripts/smoke_e2e.py` (after `make build`) | 0 | 36 steps `ok`: UI suite `805 passed`, wiring `19 passed`, `16 engines incl. fake`, quote `router ai_by_region`, export `640x268 (crop y 46)`, `full_frame` `640x360`, rescore `versions [1, 2]`, `@ref` quote on nano-banana-2.1, G2 autosave, `night_chain` refused `['no_base']`; last line `SMOKE PASS` |
| 7 | `git grep -n "key_[0-9a-f]\{32\}"`, and the same with `--untracked` (the Wave 11 files are not committed yet) | 1 and 1 | nothing found. The two adapter tests build their fake key at run time (`"key_" + "0123456789abcdef" * 8`), so no literal key exists in the tree |
| 8 | `RUNWAYML_API_SECRET` in the recordings and fixtures: `git grep --untracked -I "RUNWAYML_API_SECRET" -- 'engines/plate_engines/contract/**' 'fixtures/**' '*.json' '*.jsonl'`; `grep -rni "bearer\|authorization\|key_" engines/plate_engines/contract/runway` | 1; 1 | nothing found. The name appears only as an environment variable name: in `runway_schema.KEY_ENV`, in the CLI's key table, in tests that set or delete it with `monkeypatch` (`test_cli_runway`, `test_motion_runway`), and in the manifests' custody `note` |
| 9 | The Wave 11 done-when commands that are not pytest | 0 | T119 `15 ['atlas', 'runway'] [4, 6, 8]`; T121 `smoke --engines kling-o3-4k,gen-4.5 \| grep -c "via runway: ok"` `2`; T122 `claims_check.py --static` `static ok (28 claims, 136 tests named, documents covered)`, every runway grep count ≥ 1 (engines.md 13, artist-manual 5, runway.md 22, README 1, key-revoked 1, vendor-down 1), `14 files` absent from engines.md; T123 `npx tsc --noEmit -p .` clean, `vitest run makeBarRunway makeBarWave9` `18 passed`; T124 the generated custody matrix matches `docs/custody.md` (empty diff) |

### 11.3 The numbers

| measure | after Wave 10 | after Wave 11 |
|---|---|---|
| pytest, every package | 5945 passed, 1 skipped | **6241 passed, 1 skipped** (+296; the skip is still the drawtext-less ffmpeg) |
| vitest | 798 | **805** (28 files; +7 in `makeBarRunway.test.tsx`) |
| engine manifests | 14 | **15** (`gen-4.5`, video_i2v, `runway` only) |
| video manifests with a `runway` route | 0 | 6: `gen-4.5`, `kling-o3-4k`, `kling-v3-pro`, `seedance-2.5-i2v`, `seedance-2.5-r2v`, `veo-3.1`; Atlas, Vertex, BytePlus and WaveSpeed stay first in `routes:` and remain the fallbacks |
| fixture CLI smoke | 11 adapters ok | **17 adapters ok**, 0 failed, 8 skipped |
| Runway smoke rows | none | **6**, each `ok`, each quoting from the route's own price: gen-4.5 USD 0.24, kling-o3-4k USD 1.29, kling-v3-pro USD 0.36, seedance-2.5-i2v USD 0.80, seedance-2.5-r2v USD 0.80, veo-3.1 USD 0.80, all custody D |
| claims as tests | 28 claims, 136 tests | 28 claims, 136 tests (`claims.yaml`, `README.md` and `client-explainer.md` untouched, as the wave planned) |
| hazard scan | clean, 977 files | clean, 1026 files, 2 allowlist entries |
| new Wave 11 test functions | | `test_prefer` 33, `test_runway_schema` 37, `test_adapters_runway` 48, `test_motion_runway` 15, `test_cli_runway` 15, `test_routes_engines_runway` 7, `test_quotes_route_price` 7, plus edits to the custody, manifest, motion, previs, settings and engines-route suites |

### 11.4 Decisions 59 to 70

| # | decision | status | evidence |
|---|---|---|---|
| 59 | Runway over its Dev REST API with the user's key, through `Egress`; no MCP in the daemon | done; the live call is a box-day item | `RunwayVideo` sends `X-Runway-Version: 2024-11-06`; no `httpx.Client(` in `runway_video.py`; `test_egress.py::test_no_httpx_client_outside_egress_and_transport`; `test_the_key_appears_in_the_header_only`, `test_no_key_fails_before_anything_leaves` |
| 60 | One adapter with per-model closed field allowlists | done | `test_runway_schema` (`test_no_requested_model_has_resolution_or_unlisted_fields`, `test_check_body_names_every_offending_key`); `test_it_drives_only_its_engines_and_only_the_runway_route` |
| 61 | Prices on the route; preference is a rule, not list order | done | `test_prefer` (`test_estimate_price_uses_the_route_price_and_its_floor`, `test_choose_prefers_runway_on_a_video_manifest`); `test_quotes_route_price`; `test_routes_engines_runway` |
| 62 | Custody D on the evidence, `private` uploads, a written path to C2 | done | `test_custody` (Dev API row apart from the web-app row, generated matrix); `test_router.py::test_nda_video_has_no_cloud_path`; `test_motion_runway.py::test_an_nda_project_gets_no_video_route_at_all` |
| 63 | Runway first when custody allows and the frame fits; overrides in order | done | `test_pick_route_orders_by_the_rule_and_says_why`, `test_an_off_ratio_start_crop_goes_to_atlas_with_the_skip_reason`, `test_project_settings_override_the_preference_in_order`, `test_the_duration_is_one_every_active_route_takes` |
| 64 | Data URI up to 5,242,880 characters, else one ephemeral upload; hosts allowlisted by suffix | done; hosts unverified (box-day item 23) | `test_a_4mb_start_frame_goes_through_uploads`, `test_an_upload_host_off_the_list_fails_closed_naming_the_host`, `test_the_recordings_cover_only_runway_hosts`; `runway_hosts_unverified` on the six manifests |
| 65 | No blind retry, no lookup | done | `test_429_503_and_401_raise_after_exactly_one_create`, `test_a_create_timeout_propagates_and_is_never_retried`, `test_a_second_look_inside_5_seconds_makes_no_request`; `idempotent_submit = False`, `supports_lookup = False` |
| 66 | Failure mapping by `failureCode` prefix | done | `test_classify_failure_vectors`, `test_failure_codes_map_by_prefix`, `test_a_safety_400_is_a_stateless_refusal` |
| 67 | No `video_to_video`, no `video_upscale` | done (nothing built) | `docs/engines.md` "Not routed" paragraph |
| 68 | Seedance `draft` is the 480P tier on Runway | done; the enhance path is adapter-only (open question 33) | `test_seedance_480p_is_a_runway_draft`, `test_draft_enhance_sends_exactly_two_keys`, `test_a_previs_draft_on_seedance_r2v_goes_to_runway_as_a_draft` |
| 69 | What a model cannot express is refused or folded, never dropped | done | `test_prompt_text_folds_negative_into_models_without_the_field`, `test_a_seed_on_kling_is_a_param_error`, `test_elements_on_kling_v3_are_an_input_error`, `test_seedance_flags_runway_cannot_express_are_param_errors` |
| 70 | Start-frame-only engines are Motion engines | done | `test_motion_engines_are_every_start_frame_video_manifest`, `test_seedance_i2v_is_a_single_image_motion_engine_on_runway`, `test_an_end_still_wired_to_gen45_is_422_end_unsupported` |

The doctrine held. Runway is reached only from a plate: `text_to_video` carries Seedance reference mode and refuses an empty reference list. No plate is upscaled, and `video_upscale` is not a route. An off-ratio frame never goes to Runway, because the vendor's centre crop would move a crop pair's landing.

### 11.5 Wave 11 open questions running on their defaults

The build used these defaults (router plan, open questions 29 to 34). Each is a decision for Aasish. Changing one is a small task.

| # | question | default in effect |
|---|---|---|
| 29 | Runway as a REST route or an MCP | the Dev REST API with the user's key (`RUNWAYML_API_SECRET`), the same account the Runway MCP fronts; no MCP in the daemon |
| 30 | Runway custody | `D / confirmed / private` on the evidence of Terms of Use s4.4; the C2 promotion is a manifest edit once a signed Enterprise agreement is filed |
| 31 | Upload and output hosts | allowlisted by suffix (`.amazonaws.com`, `.cloudfront.net`, plus `api.dev.runwayml.com`), flagged `runway_hosts_unverified`, narrowed after the first live call |
| 32 | Durations across routes | ceiled to the intersection of every active route (Veo 5 s → 6 s on Vertex too; Kling under 3 s → 3 s), so a plan is legal and correctly priced on whichever route the quote picks |
| 33 | Seedance `draftTaskId` enhance | adapter-only; previs `quality: complete` stays a fresh 1080p request |
| 34 | Runway billing reconciliation | none yet; `cost.credits` is recorded on every terminal task in `JobStatus.usage` and the ledger, and the first bill is read by hand |

### 11.6 Box-day lines added by Wave 11

These continue the section 9 checklist.

23. **Live smoke hosts (T118, T121).** Run `PLATE_LIVE=1 RUNWAYML_API_SECRET=<key> .venv/bin/python -m plate_daemon.cli smoke --live --budget-usd 5 --engines kling-v3-pro,seedance-2.5-i2v --out <file>` on the Mac or the box. That is one 3 s Kling 3.0 Pro clip at USD 0.36 and one 4 s Seedance 480p draft at USD 0.80. Read the real `uploadUrl` and `output` hosts from `outbound.jsonl` and record them in `docs/runbooks/runway.md`. Then narrow `allowed_hosts` on the six `runway` routes and drop `runway_hosts_unverified`.
24. **Enterprise terms.** Runway stays custody D, so NDA projects never reach it, until a signed Runway Dev Enterprise agreement (no training, zero retention) is filed. Then, on every `runway` route: `custody_grade: C2`, `custody_status: confirmed`, `terms_hash` = the sha256 of the signed agreement. Regenerate the matrix in `docs/custody.md`. Nothing else changes.

The Vantage, Max and other vendor lines in section 9 stay on the box list unchanged.

## 12. Wave 12 (T126 to T150): stress-test fixes

Written by the Wave 12 verification pass (router task T150, Opus 5.5) on 2026-10-19 between 02:36 and 03:20 IST, on the same Mac. Commit `e6fd23f` (T126 to T148 in `83fb756`, the orchestrator's integration fixes in `e6fd23f`) plus T149's uncommitted stress target (`scripts/stress/`, `Makefile`, `justfile`, `docs/stress.md`, `.github/workflows/stress.yml`, `scripts/tests/test_stress_scripts_static.py`). Nothing called a vendor: every daemon here ran the fake engine, and every adapter met its recorded fixtures.

### 12.1 What failed and the fixes

Nothing failed. Every command in 12.2 passed on its first run, and this pass made no code or test edit. Both orchestrator notes were already settled when it started:

- **`test_motion_runs.py::test_the_lock_is_checked_again_before_the_submit`.** T130 is right. A refusal raised before any byte leaves (the lock re-check before the submit) is `staging_error`, and it never counts as a provider failure (decision 76). The orchestrator's integration commit `e6fd23f` changed the test to expect `staging_error`, and it passes.
- **Seedance 2.5 Motion is Runway-only.** This is open question 41 in the router plan, listed in 12.6.

The stress run turned up one thing to note. It is not a failure, and Wave 13 owns it. The `engine_faults` check `submit-timeout` prints `the scripted crash did not end the process; the harness killed it`. The plan's dropped item "fake-crash-not-exiting-in-background" expected T145's executor threads to make `SimulatedCrash` end the daemon. They do not: the executor logs the crash and carries on. The check still passes because the harness kills the daemon itself and asserts the restart (one provider job, no refund). The fix is T152 (decision 85, `os._exit(70)` on every execution path), whose test (4) asserts the exit code.

### 12.2 Command record

All commands ran from the repo root, one after another, with nothing else running.

| # | command | exit | key output |
|---|---|---|---|
| 1 | `perl -e 'alarm shift; exec @ARGV' 1800 make ci` | 0 | `SELFTEST PASS`; hazard selftest `13 planted hazards caught`, scan `clean (1067 files scanned, 2 allowlist entries)`; `All checks passed!`, `570 files already formatted`; licences `18 files scanned, 7 blocked models absent`; pytest `7446 passed, 2 skipped in 991.44s`, 4K gate `run_gates 3840x2160: wall 4.21s` (`4.08s` with overlays); `scripts/tests` `117 passed`; `all 28 claims hold (136 tests passed)`; vitest `Test Files 29 passed (29)`, `Tests 835 passed (835)`; `built in 1.30s`; 17 min 58 s wall |
| 2 | `perl -e 'alarm shift; exec @ARGV' 3600 make stress` | 0 | nine scripts, each `0 findings`; last line `stress: 9 scripts, 75 checks, 75 ok, 0 skipped, 0 findings (290 s)`; 4 min 50 s wall |
| 3 | `perl -e 'alarm shift; exec @ARGV' 900 .venv/bin/python scripts/smoke_e2e.py` (after `make ci` built `ui/dist`) | 0 | 36 steps `ok`: UI suite `835 passed`, wiring `19 passed`, `16 engines incl. fake`, quote `router ai_by_region`, `DONE in 0.8 s`, export `640x268 (crop y 46)`, `full_frame` `640x360`, rescore `versions [1, 2]`, `@ref` quote on nano-banana-2.1, G2 autosave, `night_chain` refused `['no_base']`; last line `SMOKE PASS` |
| 4 | `.venv/bin/python -m plate_daemon.cli smoke` | 0 | last line `fixture smoke: 17 adapters ok`, no `failed`; 8 skipped (the same 8 as section 10) |
| 5 | `PYTEST_ADDOPTS=-rs scripts/test.sh` | 0 | the same eleven steps in the same order: pytest `7446 passed, 2 skipped in 1008.69s`, scripts `117 passed`, claims 28 and 136, vitest `835 passed (835)`, built; last line `All PLATE checks passed.` The two skips: `test_previs.py:396` (this ffmpeg has no drawtext, as before) and `test_stress_wave12_ingest.py:241` (`needs an environment without imagecodecs`: T135's no-imagecodecs fallback; `e6fd23f` added imagecodecs, so it skips here and on CI) |
| 6 | the three `git grep` checks in 12.3 | 0 (each found matches; every match was read) | no test or doc pins the old behaviour |

Per stress script (command 2):

| script | checks | ok | skipped | findings | seconds |
|---|---|---|---|---|---|
| `api_fuzz` | 17 | 17 | 0 | 0 | 15 |
| `doctrine` | 4 | 4 | 0 | 0 | 10 |
| `money` | 9 | 9 | 0 | 0 | 61 |
| `crash` | 8 | 8 | 0 | 0 | 26 |
| `engine_faults` | 8 | 8 | 0 | 0 | 73 |
| `scale` | 4 | 4 | 0 | 0 | 67 |
| `ui` | 3 | 3 | 0 | 0 | 2 |
| `max_plugin` | 11 | 11 | 0 | 0 | 18 |
| `mcp_cli_imports` | 11 | 11 | 0 | 0 | 16 |

The measured lines the stress run printed: `scale` `autosave-storm` 150 overlapping PUTs all 200, PUT p50 0.04 s, max 0.10 s, `/health` max 47 ms; `deep-graph` chain-200 PUT 0.02 s; `ingest-unlisted-ids` ingest 0.3 s, cold quote 0.25 s at 640x360 (recorded, not asserted); `ledger-reread` 50,005 records, approve median 8 ms, quote median 14 ms; `engine_faults` `inflight-threads` 45 approvals leave `{'QUEUED': 43, 'RUNNING': 2}` (the route's `max_inflight` of 2); `crash` `sigkill-export-part`: a real SIGKILL mid-export left one hidden temp sibling, and the next export swept it. Neither `crash` check that needs a special volume skipped. `ro-project-volume` ran on a `chmod 0o500` `gates` folder. `low-disk` raised the free-space floor (`PLATE_MIN_FREE_MB`) instead of filling a disk: the run failed `disk_full` with no vendor call.

### 12.3 What no test or doc may still pin

- **The pixel-difference prior mask.** `git grep -n -i "pixel.difference\|pixel diff\|prior_mask"` finds only statements that the term is gone: `CHANGELOG.md:61`, the `pipeline.py` docstring and `Pipeline.prior_mask` (the base run's `mask_union_key`, else its `mask_key`, else `base_mask_missing`), `docs/artist-manual.md:418`, and two tests that assert the new rule (`test_pipeline_wave9.py::test_a_take_made_outside_the_pipeline_is_not_a_base`, `test_stress_wave12_quote.py:322`). Comp's rule that a numeric layer with no stored mask lands where it differs from the base (`nodes/comp_node.py:202`, artist manual line 422) is a different thing. It applies to a take the daemon computed itself (decision 48) and never to the Edit base.
- **The unconditional `touch_human`.** `git grep -n "touch_human"` finds 13 call sites, and every one is guarded: `from_ui(request)` in `routes/misc.py` (scrub, verdict, both timer actions), `routes/runs.py` (override, accept), `routes/projects.py` and `routes/project_settings.py`; the same header check in `routes/carryover.py`; and `by == "human"` in `Pipeline.approve`, `nodes/runner.py`, `recomposite.py` and `imports.py`. The artist manual's timer paragraph says `plate timer` in a terminal does not reset the clock.
- **`exists()`-then-replace exports.** `export.export` and `cutdown._place` claim the destination with `O_CREAT | O_EXCL` (`export.claim`). `cutdown._check_destination` keeps an early `exists()` that refuses before the encode starts, and the claim still decides the race. The tests that pinned the old refusal now expect a repeat (`test_cli.py:399`, `test_export_wave9.py:750`, `test_routes_runs_wave8.py:292`), and no doc describes check-then-write. Two writers that decision 74 does not cover still check, then replace. No test or doc pins either one. The first is `export.export_run`, the seed's byte copy, which nothing calls. The second is the delivery library: `delivery/guard.embed_and_place`, `delivery/fcpxml.py:386` and `delivery/video.py:145,443`. Two deliveries racing to one name could still overwrite each other. Moving them onto `export.claim` is a small task for a later wave.

### 12.4 The numbers

| measure | after Wave 11 | after Wave 12 |
|---|---|---|
| pytest, every package | 6241 passed, 1 skipped | **7446 passed, 2 skipped** (+1205; the new skip is the no-imagecodecs fallback test) |
| vitest | 805 (28 files) | **835** (29 files; +25 in the new `saveResilience.test.tsx`, +5 in `store.test.ts`) |
| `scripts/tests` | 79 | **117** (+38, all in `test_stress_scripts_static.py`: 10 test functions, parametrised over the scripts) |
| stress target | none | **9 scripts, 75 checks**, 0 findings, about 5 minutes on this Mac |
| fixture CLI smoke | 17 adapters ok, 8 skipped | 17 adapters ok, 8 skipped |
| scripted end-to-end smoke | 36 steps, `SMOKE PASS` | 36 steps, `SMOKE PASS` |
| claims as tests | 28 claims, 136 tests | 28 claims, 136 tests (`claims.yaml`, `README.md` and `client-explainer.md` untouched, as the wave planned) |
| hazard scan | clean, 1026 files, 2 allowlist entries | clean, **1067 files**, 2 allowlist entries |
| ruff | 531 files | **570 files** |
| `make ci` wall time | 12 min 58 s (pytest 708 s) | 17 min 58 s (pytest 991 s) |

### 12.5 Decisions 71 to 82

| # | decision | status | tests |
|---|---|---|---|
| 71 | Only the UI resets the unattended clock | done | `test_stress_wave12_routes.py::test_only_a_ui_request_resets_the_unattended_clock`, `test_a_header_less_timer_no_longer_lifts_the_unattended_cap`; `test_carryover_wave9.py` (curl is not a human); stress `api_fuzz unattended-clock`, `money unattended-clock-reset` |
| 72 | A base is a take of its node; depth counts imports; no pixel-difference prior mask | done | `test_stress_wave12_quote.py::test_repro_a_an_uploaded_reference_relayed_as_a_base_is_not_a_take`, `test_repro_b_a_gate_fail_import_is_a_base_only_after_its_override`, `test_repro_c_a_depth_two_take_relayed_or_rewired_is_depth_exceeded`, `test_an_honest_chain_is_the_plate_outside_the_union_of_its_two_masks`, `test_a_base_run_without_a_readable_mask_fails_normalizing`, `test_the_lineage_walk_stops_on_a_cycle_and_after_eight_hops`; `test_pipeline_wave9.py::test_a_take_made_outside_the_pipeline_is_not_a_base`; stress `doctrine base-any-cas-blob` |
| 73 | Ledger records are never dropped (fail-closed `override`, the outbox, `event_seq` folding, submitted by evidence) | done | `test_stress_wave12_ledger.py` (outbox, memo and evidence, 27 tests, e.g. `test_an_unwritable_ledger_parks_records_and_drains_them_after_the_mode_is_restored`, `test_a_run_with_no_submitted_record_but_later_ones_counts_as_one_generation_at_its_approved_price`); `test_stress_wave12_pipeline.py::test_a_project_lock_held_through_the_submit_loses_no_record`, `test_a_read_only_ledger_during_the_run_parks_the_records_until_the_next_append`, `test_an_override_the_ledger_cannot_take_is_503_and_leaves_no_note`; `test_stress_wave12_index.py::test_fold_takes_the_state_of_the_highest_event_seq_and_terminal_states_win`, `test_a_rebuild_of_a_raced_ledger_agrees_with_the_live_state`; `test_stress_wave12_cli.py::test_a_ledger_that_refuses_the_override_stops_the_export`; stress `money lost-submitted-record`, `ledger-order-race`, `expired-ledgered`, `doctrine override-not-ledgered`, `cli-sidecar-reason` |
| 74 | Exports claim their destination; a byte-identical repeat completes; an unwritable project refuses first | done for `export` and `cutdown` (delivery is outside the decision, see 12.3) | `test_stress_wave12_export.py::test_eight_concurrent_exports_of_two_runs_to_one_name_have_exactly_one_winner`, `test_the_same_run_exported_twice_to_one_name_is_a_repeat_and_another_run_is_refused`, `test_a_project_whose_gates_folder_is_read_only_refuses_every_export_with_503`, `test_six_concurrent_cutdowns_to_one_name_write_one_file`, `test_a_failure_after_the_claim_gives_the_name_back_and_keeps_a_shared_sidecar`; `test_stress_wave12_routes.py::test_a_byte_identical_repeat_still_ships_and_is_ledgered_as_a_repeat`; stress `api_fuzz export-race`, `crash sigkill-export-part` |
| 75 | Runs execute on a bounded executor; orphans re-driven; resume limit; busy retried | done | `test_executor.py` (18 tests, e.g. `test_route_cap_one_starts_the_second_run_only_after_the_first_finishes`, `test_watchdog_runs_an_orphan_once`); `test_stress_wave12_pipeline.py::test_45_slow_approvals_run_on_the_executor_and_never_freeze_the_api`, `test_a_run_survives_an_index_held_busy_for_12_s`, `test_a_run_the_daemon_stopped_on_twice_in_normalizing_fails_resume_limit`; stress `engine_faults inflight-threads`, `resume-starvation`, `money run-stuck-after-db-locked` |
| 76 | Vendor faults are classified, never guessed (unknown submits, poll tolerance, stale URL, result checks, streamed downloads, non-blocking cancel) | done | `test_stress_wave12_worker.py::test_a_lost_submit_answer_without_lookup_fails_submit_state_unknown_and_never_resubmits`, `test_submit_faults_are_classified`, `test_every_refusal_on_this_side_is_a_staging_error`, `test_five_transport_faults_in_a_row_fail_the_run`, `test_a_stale_result_url_is_repolled_once_for_a_fresh_one`, `test_a_declared_decompression_bomb_is_refused_on_its_header`, `test_a_cancel_during_a_stalled_vendor_call_answers_at_once_and_lands_after_the_stall`; `engines/tests/test_adapter_fetch_stream.py` (12 tests); `test_stress_wave12_pipeline.py::test_a_submit_whose_answer_was_lost_is_ledgered_billing_unknown_never_refunded`; stress `engine_faults` (all 8) |
| 77 | The graph has a revision; append-node; bounded save queue; the UI retries, reloads and flushes | done | `test_stress_wave12_projects.py::test_a_put_on_a_stale_revision_is_a_conflict_and_an_old_client_still_saves`, `test_twelve_concurrent_appends_each_get_their_own_node`, `test_a_storm_of_big_puts_is_answered_never_500_or_project_locked`, `test_a_full_queue_is_shed_at_once_and_a_long_wait_is_graph_busy`; `ui/src/test/saveResilience.test.tsx` (25 tests: the revision on every PUT, backoff 1, 2, 4, 8, 15 s, `conflict: reload`, a foreign `graph.saved`, pagehide keepalive, reopen after reload); stress `ui` (all 3), `scale autosave-storm`, `deep-graph` |
| 78 | The NDA scrub is an allowlist; `export_review` guarded; the daemon numbers attempts | done | `test_stress_wave12_mcp.py::test_an_nda_graph_full_of_free_text_hands_over_none_of_it_and_names_every_path`, `test_a_daemon_refusal_that_echoes_project_text_is_withheld_on_nda`, `test_export_review_refuses_a_gate_fail_take_until_it_is_overridden`, `test_six_concurrent_run_node_calls_on_one_node_are_six_done_attempts`, `test_twelve_concurrent_add_node_calls_leave_twelve_nodes`, `test_a_log_failure_after_run_node_names_the_run_it_started`; stress `mcp_cli_imports nda-scrub`, `export-review-guard`, `concurrent-run-node`, `concurrent-add-node`, `outbound-log-failure` |
| 79 | Custody `unset` is server-owned | done | `test_stress_wave12_projects.py::test_a_client_sent_unset_is_dropped_and_never_reopens_the_custody`, `test_an_unset_mark_in_a_custody_edit_is_dropped_too`, `test_a_packet_first_project_takes_its_custody_once_and_the_fill_in_is_ledgered`, `test_a_fill_in_the_ledger_cannot_hold_does_not_happen`; stress `api_fuzz custody-unset` |
| 80 | The quote validates everything the run will need; caps sum every project in the home | done | `test_stress_wave12_quote.py::test_mask_ids_and_a_mask_take_are_checked_before_any_run_row`, `test_ten_concurrent_quotes_of_one_node_number_one_to_ten_and_all_run`, `test_attempts_are_bounded`, `test_priority_and_params_are_refused_before_a_run_row`, `test_the_daily_cap_counts_every_project_in_the_home`, `test_approving_an_expired_or_future_quote_is_409_and_ledgered`, `test_envelopes_and_price_counts_are_bounded`; `test_stress_wave12_motion.py::test_a_pinned_route_that_cannot_take_the_params_is_422_params_and_no_run`, `test_a_param_no_route_can_take_is_422_params_and_no_run`; stress `api_fuzz quote-validation`, `attempt-poison`, `envelope-inf`, `money daily-cap`, `concurrent-quote-same-attempt`, `quote-ttl-clock-back`, `doctrine mask-errors-at-quote` |
| 81 | Send preflight catches what the render would waste | done on the fake; a real merged scene is box-day item 25 | `plugins/max/tests/test_stress_wave12_send.py::test_two_nodes_named_shared_stop_the_send_at_lint_in_either_order`, `test_two_cameras_named_cam_dup_stop_at_lint`, `test_a_camera_name_that_cannot_be_a_shot_id_stops_at_lint`, `test_degenerate_lens_values_are_bad_lens_before_the_render`, `test_a_non_finite_pose_is_bad_pose_before_the_render`, `test_a_forced_manifest_abort_leaves_no_orphan_folder`, `test_dump_json_bytes_refuses_nan_and_infinity`; stress `max_plugin` (all 11) |
| 82 | The stress scripts are an opt-in target | done | `scripts/tests/test_stress_scripts_static.py` (10 tests, incl. `test_the_stress_target_is_opt_in_everywhere`, `test_the_stress_workflow_is_manual_with_the_ci_matrix_and_setup`); `scripts/tests/test_ci_parity.py` unmodified and green; `make stress` 75 of 75 (12.2) |

### 12.6 Wave 12 open questions running on their defaults

The build used these defaults (router plan, open questions 35 to 41). Each is a decision for Aasish. Changing one is a small task.

| # | question | default in effect |
|---|---|---|
| 35 | Scope of the daily and unattended caps | summed across every project index in this home (`budgets.spend_snapshot_all`), so N projects no longer multiply the cap; the 409 names the project count |
| 36 | `by: "human"` from a client that is not the UI | unchanged from decision 37: an approval as a human bypasses `budgets.authorize` and resets the unattended clock whatever client sent it; the CLI approves as a human and a token holder is trusted |
| 37 | Whether an external import counts as a generation | yes: an imported retouch, then one Edit, then another Edit is refused `DEPTH_EXCEEDED` |
| 38 | Executor size | `PLATE_RUN_WORKERS=8` concurrent runs per daemon, per-route `max_inflight` from the manifest (default 2); the rest wait in QUEUED (the stress run's 45 approvals: 2 RUNNING, 43 QUEUED) |
| 39 | Result caps | `PLATE_RESULT_MAX_PIXELS` 64,000,000 (8000 × 8000); downloads 256 MiB for image kinds and 2 GiB for video kinds, under a 600 s wall clock; uploads and imports 100,000,000 px |
| 40 | Clips under two frames at import | refused 422 `unreadable_video` |
| 41 | Seedance 2.5 Motion routes | Runway-only, because the Atlas, BytePlus and WaveSpeed Seedance manifests declare no `negative_prompt` and a recipe's negative prompt cannot be cleared (`motion_runs` treats `""` as unset); this holds until a fallback manifest declares the field or the recipe negative can be cleared |

### 12.7 Box-day lines added by Wave 12

Wave 12 adds no vendor or render-box exit. These two lines continue the section 9 checklist, after items 23 and 24 (section 11.6):

25. **Send preflight on a real scene (T140).** In Max 2026, merge a scene that brings in a second copy of the same geometry (duplicate visible node names) and a duplicated camera. Send Packet must stop at lint with `DUPLICATE_NODE_NAME` / `DUPLICATE_CAMERA_NAME`, render nothing, and leave no packet folder. Three more cameras must stop at lint before `step("render")`: one whose name cannot become a shot id (a non-Latin name) with `BAD_SHOT_SLUG`, one with a zeroed focal length, film gate or exposure with `BAD_LENS`, and one driven by a script to a non-finite pose with `BAD_POSE`.
26. **`plate timer` with the dock open (decision 71).** Connect the Max dock and close the canvas. After 30 idle minutes, `plate timer start <deliverable>` in a terminal must not lift the unattended cap: agent approvals past the USD 20 unattended cap are still refused (`unattended_cap`). The dock's own requests must not lift it either. Any request from the canvas lifts it.

The stress workflow (`.github/workflows/stress.yml`, `workflow_dispatch`) has not run on `windows-latest` yet. Start it once by hand after this wave is pushed. `docs/stress.md` lists the checks that skip there.

## 13. Wave 13 (T151 to T162): every remaining stress finding

Written by the Wave 13 verification pass (router task T162, Opus 5.5) on 2026-10-19 between 05:10 and 08:50 IST, on the same Mac. Commit `3d55f43` (T154, completing `aa302ac`) plus the uncommitted Wave 13 tree (T151 to T153 and T155 to T161, and the edits below). Nothing called a vendor: every daemon here ran the fake engine, and every adapter met its recorded fixtures.

### 13.1 What failed and the fixes

The first `make ci` passed with no edit (row 1). Five things still needed one. The three `git grep` checks of 13.3 found one route and two docs that predate Wave 13. Three stress checks still expected the behaviour from before Wave 13, and one of them, once moved to the new contract, found a real defect. Each fix is the smallest one that keeps decisions 83 to 93. `make stress`, both smokes, `scripts/test.sh` and the closing `make ci` ran after the fixes.

- **The Motion quote read `detail_resolvability` directly (decision 84).** T152 moved the Edit quote (`quote_node` → `render_it(conn)`) onto the stored `packet_router` row. A Motion or CropPair quote whose plate has a motion packet still built its router card from `motion_runs._detail`, which decodes the still's Z and object Cryptomatte and calls `router_math.detail_resolvability` on every quote. Fix in `daemon/plate_daemon/motion_runs.py`: `router_card(..., conn=None)` and `_stored_detail`. With the quote's `conn`, a motion-packet card takes the still's D from `Pipeline.router_summary`. That is the row written at ingest, or computed and stored if absent, so the still is no longer decoded. The preview route (no `conn`) and the still-only branch, which needs the Z for the move itself, are unchanged. New test: `daemon/tests/test_motion_runs.py::test_a_motion_quote_takes_the_stills_d_from_the_stored_router_summary`. It asserts the row is there after ingest, that `_still_geometry` is never called at the quote, and that the card's `D_px_median` equals the row's.
- **A kill -9 in an export's first instant left a hidden `.lock` in the client folder for an hour (decision 87).** `crash sigkill-export-part` aged every leftover two hours before the retry, a Wave 12 workaround that hid the new rule. It now ages only a temp with no marker (the planted one). Run that way, it found a defect: a real SIGKILL at 30 ms leaves the in-flight marker alone, with no temp yet, on each of the four runs here (`SIGKILL left ['.run_…exr.<tag>.lock']`). T153's sweep kept a free marker found alone under the one-hour rule, so the retry left `.run_….exr.<tag>.lock` beside the shipped pair. Fix in `daemon/plate_daemon/export.py` (`sweep_stale`): a marker nobody holds goes at once, alone or not. This is safe because the marker name carries a unique tag. A live writer that has not locked its marker yet has written nothing beside it, and once the marker is gone its later temps fall under the age rule, which keeps young files. T153's test `test_stress_wave13_export.py::test_sweep_stale_follows_the_marker_not_the_age` now expects the young lone marker to be removed. The module docstring, `CHANGELOG.md` decision 87 and `docs/stress.md` say so.
- **`mcp_cli_imports outbound-log-failure` pinned the Wave 12 behaviour.** It asserted that `run_node`'s and `add_node`'s `outbound_log_failed` name the run and the node the daemon had already made. After T157 (decision 91) nothing is made. The check now asserts `outbound_log_failed`, no run, no new node, and no `run_id` or `node_id` in either message. The script docstring and `docs/stress.md` were updated to match.
- **`engine_faults submit-timeout` only noted a crash that did not exit.** Decision 85 makes the scripted crash end the process with code 70 on the executor's threads, so the note became a check: the daemon must exit by itself with return code 70 within 10 s. The harness still kills a daemon that does not, and that is now a finding. On `1f272f2` only this new leg would find a defect, and `docs/stress.md` says so.
- **Two docs still described the lockfile without the lease.** `docs/windows.md` said a project on the NAS is "protected by the project lockfile only". The repo's `CLAUDE.md` (Windows rules) said the same. Both now name `lease.json` and the 409 `project_leased`.

Two process notes. The first `make ci` ran across a system sleep: its `real` was 9845 s, but pytest's own clock read 1221 s. `alarm` does not count while the Mac sleeps, so the 1800 s alarm never fired. Every later run here was wrapped in `caffeinate -i -s`, and those wall times are real. Some edits above landed while that first `make ci` was in pytest, so the closing `make ci` (row 7) is the one that counts.

### 13.2 Command record

All commands ran from the repo root, one after another (rows 1 to 7). Row 1's lint step ran before any edit of 13.1.

| # | command | exit | key output |
|---|---|---|---|
| 1 | `perl -e 'alarm shift; exec @ARGV' 1800 make ci` (first; some 13.1 edits landed during its pytest step) | 0 | `SELFTEST PASS`; hazard selftest `13 planted hazards caught`, scan `clean (1079 files scanned, 2 allowlist entries)`; `All checks passed!`, `580 files already formatted`; licences `18 files scanned, 7 blocked models absent`; pytest `7651 passed, 2 skipped in 1221.56s`, 4K gate `run_gates 3840x2160: wall 4.61s` (`4.94s` with overlays); `scripts/tests` `117 passed`; `all 28 claims hold (136 tests passed)`; vitest `Test Files 31 passed (31)`, `Tests 863 passed (863)`; `built in 1.46s` |
| 2 | `.venv/bin/python scripts/stress/mcp_cli_imports.py --only outbound-log-failure`, `crash.py --only sigkill-export-part` (after their 13.1 updates) | 0 / 1, then 0 | the MCP check `ok`; the crash check `FINDING ... holds ['.run_….exr.<tag>.lock', ...]` twice in a row (the lone marker), then `ok` after the `sweep_stale` fix |
| 3 | `perl -e 'alarm shift; exec @ARGV' 3600 make stress` | 0 | nine scripts, each `0 findings`; last line `stress: 9 scripts, 75 checks, 75 ok, 0 skipped, 0 findings (290 s)`; `sigkill-export-part` `SIGKILL left ['.run_01M58ZZK2SP4PWQEASS']; 1 in-flight marker(s) left by the kill: the retry runs at once, nothing aged`; `submit-timeout` `DONE` (exited 70 by itself, no harness kill) |
| 4 | `perl -e 'alarm shift; exec @ARGV' 900 .venv/bin/python scripts/smoke_e2e.py` | 0 | 36 steps `ok`: UI suite `863 passed`, wiring `19 passed`, `16 engines incl. fake`, ingest `0.3 s`, quote `router ai_by_region`, `DONE in 0.8 s`, export `640x268 (crop y 46)`, `full_frame` `640x360`, rescore `versions [1, 2]`, `@ref` quote on nano-banana-2.1, G2 autosave, `night_chain` refused `['no_base']`; last line `SMOKE PASS` |
| 5 | `.venv/bin/python -m plate_daemon.cli smoke` | 0 | last line `fixture smoke: 17 adapters ok`; 8 skipped (the same 8 as section 10) |
| 6 | `PYTEST_ADDOPTS=-rs scripts/test.sh` | 0 | the same eleven steps in the same order: hazard `clean (1079 files scanned, 2 allowlist entries)`, `580 files already formatted`, pytest `7652 passed, 2 skipped in 1193.45s`, 4K gate `run_gates 3840x2160: wall 4.60s`, scripts `117 passed`, claims 28 and 136, vitest `863 passed (863)`, built; last line `All PLATE checks passed.`; `real 1280.55` (21 min 21 s). The two skips are the same as in Wave 12: `test_previs.py:396` (no drawtext in this ffmpeg) and `test_stress_wave12_ingest.py:241` (`needs an environment without imagecodecs`) |
| 7 | `perl -e 'alarm shift; exec @ARGV' 1800 make ci` (closing, every edit in place) | 0 | `SELFTEST PASS`; hazard selftest `13 planted hazards caught`, scan `clean (1079 files scanned, 2 allowlist entries)`; `All checks passed!`, `580 files already formatted`; licences `18 files scanned, 7 blocked models absent`; pytest `7652 passed, 2 skipped in 1199.13s`, 4K gate `run_gates 3840x2160: wall 4.52s` (`4.86s` with overlays); `scripts/tests` `117 passed`; `all 28 claims hold (136 tests passed)`; vitest `Test Files 31 passed (31)`, `Tests 863 passed (863)`; `built in 1.43s`; `real 1286.40` (21 min 26 s) |
| 8 | the three `git grep` checks in 13.3 | 0 (each found matches; every match was read) | after the fixes, no test, doc or route pins the old behaviour |

Per stress script (row 3):

| script | checks | ok | skipped | findings | seconds |
|---|---|---|---|---|---|
| `api_fuzz` | 17 | 17 | 0 | 0 | 16 |
| `doctrine` | 4 | 4 | 0 | 0 | 11 |
| `money` | 9 | 9 | 0 | 0 | 64 |
| `crash` | 8 | 8 | 0 | 0 | 25 |
| `engine_faults` | 8 | 8 | 0 | 0 | 67 |
| `scale` | 4 | 4 | 0 | 0 | 68 |
| `ui` | 3 | 3 | 0 | 0 | 3 |
| `max_plugin` | 11 | 11 | 0 | 0 | 18 |
| `mcp_cli_imports` | 11 | 11 | 0 | 0 | 16 |

The measured lines the stress run printed: `scale` `autosave-storm` 150 overlapping PUTs all 200, PUT p50 0.05 s, max 0.09 s, `/health` max 49 ms; `ingest-unlisted-ids` ingest 0.3 s, cold quote 0.26 s at 640x360; `ledger-reread` 50,005 records, approve median 8 ms, quote median 15 ms; `engine_faults` `inflight-threads` `{'QUEUED': 43, 'RUNNING': 2}`; `max_plugin` `hash-503-retry` waited `[1.0]` before the second `/hash`.

### 13.3 What no test, doc or route may still pin

- **The lock is per write, and the lease keeps a second daemon out.** Search: `git grep -n -i "per write\|each write\|lockfile only\|lock only\|two daemons"` and `git grep -n -i "project lock\|project\.lock"`. Every doc that says the lock is taken per write names the lease in the same paragraph or row: `docs/artist-manual.md:80`, `docs/runbooks/backup-restore.md:91,143`, `docs/runbooks/daemon-wont-start.md:16,55`, and the lease test's docstring (`test_stress_wave13_lease.py:7`). `CHANGELOG.md:72` is the Wave 12 entry ("unsupported until a lease lands"), kept as history beside Wave 13's decision 83 entry. Two places did not name it, and both are fixed (13.1): `docs/windows.md:28` and `CLAUDE.md:41`. Other hits are about something else: `lock.py`'s module docstring describes the lease, and "per call" in other modules is not about the lock.
- **No route reads `detail_resolvability` at the quote.** Search: `git grep -n "detail_resolvability"`, every call site read. The Edit quote goes `quote_node` → `render_it(pv, task, conn)` → `router_summary` (the `packet_router` row). The Motion/CropPair quote goes `_quote_kling` → `router_card(..., conn=conn)` → `_stored_detail` → `router_summary` (fixed in 13.1). Without a `conn`, only three places compute D: `PacketView.detail_resolvability` (what `router_summary` and `warm_router` call), the Motion editor's router preview (`RouterPreview.preview`: numbers only, no run, no quote) and `cli._router_check`. The last is the in-process `plate run` preflight for recipes that declare `router_check`; it is not a route, and the daemon quote it leads to reads the row.
- **No `settle_failed_folder` caller drops the transport flag.** Search: `git grep -n "settle_failed_folder"`. There is one caller, `send_packet.py:1304`, in the per-camera loop of `send_packet`. It passes `transport_failure=transport_failure`, which the `except transport.TransportError` branch sets from `daemon_failed(exc)`. No other action (`batch_cameras`, `send_motion`, `light`) settles a folder itself. `keep_failed_packets` appears only in `send_packet.py`.

### 13.4 The numbers

| measure | after Wave 12 | after Wave 13 |
|---|---|---|
| pytest, every package | 7446 passed, 2 skipped | **7652 passed, 2 skipped** (+206: the Wave 13 test files below and this pass's one test; the same two skips) |
| vitest | 835 (29 files) | **863** (31 files; +14 in `viewerCache.test.ts`, +14 in `stripHydration.test.tsx`) |
| `scripts/tests` | 117 | 117 |
| stress target | 9 scripts, 75 checks | 9 scripts, **75 checks**, 0 findings, 290 s (three checks moved to the Wave 13 contract, 13.1) |
| fixture CLI smoke | 17 adapters ok, 8 skipped | 17 adapters ok, 8 skipped |
| scripted end-to-end smoke | 36 steps, `SMOKE PASS` | 36 steps, `SMOKE PASS` |
| claims as tests | 28 claims, 136 tests | 28 claims, 136 tests (`claims.yaml`, `README.md` and `client-explainer.md` untouched, as the wave planned) |
| hazard scan | clean, 1067 files, 2 allowlist entries | clean, **1079 files**, 2 allowlist entries |
| ruff | 570 files | **580 files** |
| `make ci` wall time | 17 min 58 s (pytest 991 s) | 21 min 26 s (pytest 1199 s), under `caffeinate` |

New Wave 13 test files and their test counts: `daemon/tests/test_stress_wave13_lease.py` 16, `test_stress_wave13_pipeline.py` 8, `test_stress_wave13_export.py` 20, `test_stress_wave13_api.py` 21, `test_stress_wave13_budgets.py` 7, `test_stress_wave13_mcp.py` 9, `gates/tests/test_video_unscored.py` 7, `pixels/tests/test_ingress_format.py` 6, `plugins/max/tests/test_stress_wave13_plugin.py` 24, `ui/src/test/viewerCache.test.ts` 14, `ui/src/test/stripHydration.test.tsx` 14; plus this pass's one test in `test_motion_runs.py`.

### 13.5 Decisions 83 to 93

| # | decision | status | tests |
|---|---|---|---|
| 83 | A project is leased by one daemon at a time (`lease.json`, 20 s heartbeat, 90 s TTL, 409 `project_leased` on writes, reads served, no adoption of a leased project's runs) | done on this Mac (two daemons, one folder); the NAS from two machines is box-day item 27 | `test_stress_wave13_lease.py` (16), e.g. `test_a_second_identity_is_refused_with_the_first_holders_host_and_pid`, `test_a_lease_91_s_old_is_taken_over_with_a_warning_naming_the_old_pid`, `test_a_live_foreign_lease_is_refused_and_one_from_a_dead_pid_on_this_host_is_not`, `test_a_live_foreign_lease_refuses_writes_409_and_serves_reads`, `test_resume_and_the_orphan_watch_leave_a_leased_projects_runs_alone`, `test_a_second_daemon_on_the_folder_leaves_the_first_daemons_run_alone`, `test_plate_reindex_still_works_on_a_leased_project` |
| 84 | The router's inputs are computed once, at ingest (`packet_router`, `ROUTER_SUMMARY_VERSION` `2026.10.19`) | done (the Motion quote completed in 13.1) | `test_stress_wave13_pipeline.py::test_the_router_summary_is_stored_at_ingest_and_the_quote_reads_it` (no decode, under 0.5 s), `test_a_row_of_an_older_version_is_recomputed_and_replaced`, `test_a_rebuild_leaves_the_table_empty_and_the_next_quote_refills_it`, `test_a_warm_up_that_fails_is_a_warning_and_the_quote_computes_lazily`, `test_z_and_the_object_cryptomatte_come_from_one_open_of_the_exr`; `test_motion_runs.py::test_a_motion_quote_takes_the_stills_d_from_the_stored_router_summary`; stress `scale ingest-unlisted-ids` (cold quote 0.26 s) |
| 85 | A scripted crash exits the process on every execution path (`os._exit(70)`) | done | `test_stress_wave13_pipeline.py::test_a_scripted_crash_exits_the_process_and_the_restart_spends_once` (`PLATE_RUN_WORKERS` 4 and 0); stress `engine_faults submit-timeout` (now asserts code 70) |
| 86 | Result format is recorded, not judged; non-finite results fail `normalize_error` | done | `test_stress_wave13_pipeline.py::test_a_result_reaches_done_with_its_format_recorded`, `test_a_float_tiff_with_a_nan_inside_the_mask_fails_normalizing`; `pixels/tests/test_ingress_format.py` (6) |
| 87 | An export in flight holds a marker; the sweep follows the marker, not the age | done (the lone marker completed in 13.1); delivery writers claim their names too | `test_stress_wave13_export.py` (20), e.g. `test_a_kill_minus_nine_leftover_is_swept_by_an_export_seconds_later`, `test_sweep_stale_follows_the_marker_not_the_age`, `test_a_live_exports_marker_keeps_its_files_and_a_dead_ones_are_swept`, `test_a_marker_less_temp_follows_the_one_hour_rule`, `test_a_cutdown_sweeps_a_dead_cutdowns_leftovers_and_keeps_a_live_ones`, `test_embed_and_place_has_exactly_one_winner_among_eight_racing_deliveries`, `test_two_deliveries_racing_to_one_name_over_the_route_have_one_winner`, `test_the_dead_seed_copy_is_gone_and_the_claim_stays_shared`; stress `crash sigkill-export-part` |
| 88 | Inputs the daemon trusts are validated on load (token 16 to 256 printable ASCII on one line, recipe paths confined, `NODE_PARAMS_MAX_BYTES` 262144) | done | `test_stress_wave13_api.py` (21), e.g. `test_a_token_file_with_a_second_line_is_a_config_error`, `test_a_config_json_token_with_a_space_names_config_json`, `test_a_recipe_path_outside_the_recipe_folders_is_refused_before_it_is_read`, `test_a_link_out_of_a_recipe_folder_is_judged_by_where_it_points`, `test_a_300_kib_prompt_is_422_params_too_large_and_200_kib_is_stored`, `test_an_appended_node_is_held_to_the_same_cap`, `test_a_name_ending_in_a_newline_is_422_and_leaves_no_such_path`; stress `api_fuzz recipe-yaml-leak`, `newline-slugs`, `max_plugin token-second-line` |
| 89 | Externally declared spend never blocks automation | done | `test_stress_wave13_budgets.py` (7), e.g. `test_a_declared_import_above_the_cap_warns_and_blocks_no_approval`, `test_the_unattended_cap_does_not_count_a_declared_import_either`, `test_a_vendor_priced_run_still_trips_the_daily_cap`, `test_the_ledger_still_carries_the_declared_price_and_plate_spend_is_unchanged`; stress `mcp_cli_imports declared-cost-bound` |
| 90 | A metric that was not measured says so (`camera_lock`, `flicker` on fewer than two frames are `unscored`) | done | `gates/tests/test_video_unscored.py` (7), e.g. `test_camera_lock_on_a_one_frame_clip_is_unscored_and_decodes_nothing`, `test_flicker_on_a_one_frame_clip_is_unscored`, `test_the_assembled_report_has_no_camera_lock_value_and_says_why`, `test_control_a_twelve_frame_static_clip_still_scores_and_passes`; `gates/tests/test_video_gates.py` (updated by T156) |
| 91 | MCP mutating tools probe the outbound log before acting; finite `--new-envelope` arguments | done | `test_stress_wave13_mcp.py` (9), e.g. `test_a_failed_probe_stops_the_tool_before_any_post_reaches_the_daemon`, `test_an_unwritable_outbound_log_stops_run_node_estimate_and_add_node_and_reads_nothing`, `test_a_non_finite_or_out_of_range_envelope_argument_is_a_usage_error_and_makes_nothing`; stress `mcp_cli_imports outbound-log-failure` (updated, 13.1), `cli-envelope-edges` |
| 92 | A render is never kept for a daemon failure; long scene names get an app-data sidecar | done on the fake; a real scene is box-day items 28 and 29 | `plugins/max/tests/test_stress_wave13_plugin.py` (24), e.g. `test_a_503_on_the_first_hash_after_the_render_removes_the_packet_folder`, `test_keep_failed_packets_keeps_the_render_of_a_daemon_failure`, `test_a_daemon_that_refuses_keeps_the_render_for_inspection`, `test_the_unlabelled_share_abort_still_keeps_its_render`, `test_a_long_stem_keeps_its_sidecar_under_app_data`, `test_the_name_limit_is_255_utf8_bytes_not_characters`, `test_windows_adds_the_259_character_path_limit`, `test_send_proceeds_for_a_254_character_scene_and_logs_where_the_backup_went`; stress `max_plugin hash-503-retry` (one 503, then the retry: the Send still succeeds) |
| 93 | The viewer caches decoded images by asset (320 MiB, 4 entries); strips hydrate from history on project open | done | `ui/src/test/viewerCache.test.ts` (14), e.g. "A (plate) + B1, then B2 and B3 with the same A: one fetch for A, one per B", "fetches the plate once across take switches and a remount, and uploads on every source change"; `ui/src/test/stripHydration.test.tsx` (14), e.g. "a best-of-3 group fills the strip with its three candidates; an ungrouped newest run comes back alone", "a RUNNING take is shown and watched: an EventSource opens for it and its next state moves the chip", "a node with a run in flight from this session is not replaced; the other nodes still hydrate" |

### 13.6 Every finding and where it ended

The router plan's outcome table, in short form: the 39 lows and the 8 items Wave 12 dropped. "W12" means fixed in Wave 12 by the task named, and pinned by its `test_stress_wave12_*.py` file. A bold task id is a Wave 13 task.

| finding | outcome |
|---|---|
| unbounded-result-download | W12 T130 (streamed downloads, `FETCH_MAX_BYTES`, magic check) |
| result-format-unrecorded | **T152** (decision 86) |
| runway-succeeded-no-output-terminal | W12 T138, T130 |
| attempt-poison | W12 T128, T129 |
| newline-slugs | W12 `fullmatch`; **T154** the cross-route newline suite |
| unbounded-body | W12 T132, T128; **T154** the per-node params cap |
| decode-bomb | W12 T136 |
| pattern-width | W12 T127 |
| docs-long-name | W12 T132 |
| graph-unhashable-id | W12 T128 |
| method-404 | W12 T132 |
| recipe-yaml-leak | W12 T129 (path only); **T154** recipe paths confined |
| symlink-manifest | W12 T135 |
| expired-and-failclosed-cancel-not-ledgered | W12 T129, T145 |
| fake-crash-not-exiting-in-background | **T152** (decision 85) |
| sigkill-export-part-left | W12 T127 (one-hour sweep); **T153** liveness-based sweep, completed by T162 (lone marker) |
| quote-ttl-clock-back | W12 T129 |
| no-overall-http-deadline | W12 T142 |
| malformed-token-raises-and-leaks | W12 T142 (plugin); **T154** the daemon's own token file |
| transient-daemon-error-loses-render | W12 T142 (`/hash` retries); **T158** the manifest-less folder removed |
| any-200-counts-as-sent | W12 T142 |
| dock-log-shows-html-entities | W12 T142 |
| long-scene-name-breaks-sidecar | W12 T141 (clear message); **T158** long names work |
| import-decodes-before-size-check | W12 T137 |
| mcp-outbound-log-failure-after-spend | W12 T146 (ids in the error); **T157** probe before acting |
| import-still-named-mp4-is-a-clip | W12 T137 (refused); **T156** one-frame metrics unscored |
| import-long-extension-500 | W12 T137 |
| kit-camera-name-escapes-folder | W12 T137 |
| cli-envelope-edges | W12 T143, T126, T129; **T157** the MCP `--new-envelope` leg |
| kit-exr-header-unbounded | W12 T135 |
| declared-import-cost-unbounded | W12 T137 (ceiling); **T155** external spend outside the caps |
| reload-in-debounce-loses-edits | W12 T147 |
| viewer-4k-refetch | **T159** (decision 93) |
| token-status-misleading | W12 T147 |
| makebar-raw-html-error | W12 T147 |
| strip-not-hydrated-after-reload | **T160** (decision 93) |
| graph-validate-superquadratic | W12 T133 |
| quote-cold-4k-over-2s | **T152** (decision 84), completed by T162 for the Motion quote |
| ingest-unlisted-ids-quadratic | W12 T135 |
| dropped: second-daemon-same-project-adopts-runs | **T151** (decision 83) |
| dropped: quote-cold-4k-over-2s | **T152** |
| dropped: viewer-4k-refetch | **T159** |
| dropped: strip-not-hydrated-after-reload | **T160** |
| dropped: fake-crash-not-exiting-in-background | **T152** |
| dropped: result-format-unrecorded | **T152** |
| dropped: the attackers' Playwright scripts | by design: harness files, not findings; pinned by `saveResilience.test.tsx` (T147) and `scripts/stress/ui.py` (T149) |
| dropped: "folded into tasks rather than separate" (19 lows) | each fixed in Wave 12 by the task T148's list names; the rows above name the file that pins it |

The section 12.3 gap is closed as well. T153 moved the delivery writers (`guard.embed_and_place`, `fcpxml.write`, `video.exr_sequence`) onto `export.claim` and removed the dead seed copy `export.export_run`. The other by-design items are in the router plan's Wave 13 list: Tier C of any plate size, a deliberate one-frame import, no void for an import declaration, `lie_low` truncation, and the UTF-16 token fallback.

### 13.7 Partial tasks

None. No Wave 13 test file records `not reproducible on 83fb756`, and every one of T151 to T161 had passed when this pass started (its dependency list). The one repro note that names the commit is T151's module docstring, and it is a reproduction, not a miss: "Re-run on 83fb756 (test (3) without the lease checks): daemon B logged `resumed 1 pipeline run(s) a stopped daemon left in flight` and A's ledger for the run read `quoted, approved, submitted, failed`, the `failed` written by B." Two Wave 13 decisions were not fully met by their tasks, and this pass completed them (13.1): decision 84 for the Motion quote and decision 87 for a lone marker.

### 13.8 Wave 13 open questions running on their defaults

The build used these defaults (router plan, open questions 41 to 45; 41 is carried from Wave 12). Each is a decision for Aasish. Changing one is a small task.

| # | question | default in effect |
|---|---|---|
| 41 | Seedance 2.5 Motion routes | Runway-only, unchanged from 12.6: the Atlas, BytePlus and WaveSpeed Seedance manifests declare no `negative_prompt` |
| 42 | Lease scope | writes only: graph, runs, assets and review bundles are served to any daemon, so a second machine can look at a NAS project while the first one drives it; every write is 409 `project_leased` |
| 43 | Lease TTL and heartbeat | 90 s TTL, 20 s heartbeat (`HEARTBEAT_S = LEASE_TTL_S / 4.5`); `PLATE_LEASE_TTL_S` tunes it, and a value that is not a positive finite number falls back to 90 with a warning; a lease of a dead process on the same host is taken over at once |
| 44 | External import spend and the caps | recorded with `external: true` and left out of the daily and unattended caps; the ledger, `plate spend` and `Spend.external_today_usd` still show it; a declaration above the daily cap answers 201 with a `warnings` line |
| 45 | Recipe paths over the API | confined to the project's `recipes/`, the studio recipes folder and the shipped recipes (`recipes.recipe_roots`); anything else is 422 `unknown_recipe` with "recipe paths must live in ..." and no line of the file |

### 13.9 Box-day lines added by Wave 13

These three lines continue the section 9 checklist, after items 25 and 26 (section 12.7):

27. **The lease on the NAS from two machines (T151, decision 83).** Open one project folder on the NAS from the render box's daemon and from a second machine's daemon. While the first runs, the second's writes (a graph save from its canvas, a Send from its dock) are refused 409 `project_leased`, and its dock shows the 409 text naming the first machine and pid. Its reads still work. Stop the first daemon cleanly: the lease is removed and the second writes at once. Kill the first instead (Task Manager): the second takes the lease over within 90 s and logs a warning. Also check that a 30 s SMB hiccup on the share does not lose the lease.
28. **A 250-character scene name (T158, decision 92).** In Max 2026, save a scene whose file stem is 250 characters in a folder deep enough that `<stem>.plate.json` would pass Windows' 259-character path limit. Bind it to a project and Send. The sidecar lives under `%LOCALAPPDATA%\Plate\sidecars\<hash>.plate.json`, the dock shows the note saying where, the binding survives a Max restart, and the scene backup lands in the temp folder.
29. **A daemon restart mid-send (T158, decision 92).** Start a Send on a real camera and restart the daemon after the render, while the plugin is at the legend or hash stage. After T142's retries the Send fails with a daemon error, and the manifest-less packet folder is removed (the dock's warning names it). With `keep_failed_packets` on, the folder stays. Re-sending once the daemon is back ingests normally.

The Wave 13 tree has not run on `windows-latest` yet. Push it, then confirm that the CI leg passes, including `test_windows_adds_the_259_character_path_limit` (the MAX_PATH case that failed on run 39171560042 and that T158 owns). Then start the stress workflow once by hand (`.github/workflows/stress.yml`, `workflow_dispatch`).
