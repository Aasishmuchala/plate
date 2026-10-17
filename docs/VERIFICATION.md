# PLATE verification record

Written by the final verification pass (router task T53, Opus 5.5) on 2026-10-18 between 03:00 and 04:00 IST, on the Mac (macOS 27.0 arm64, Python 3.12.12, Node 22.22.3, npm 10.9.8, Blender 5.1.1, ffmpeg from Homebrew, no `oiiotool`). Commit `dd78f02` plus the untracked `scripts/smoke_e2e.py` (T79). Nothing here ran on the Windows render box, inside 3ds Max or against a live vendor; those lines are in the box-day checklist (section 7).

## Result

| check | result |
|---|---|
| Windows-hazard scan (`scripts/hazard_scan.py`, `--selftest`, `--strict-allow`) | pass |
| `make lint` (ruff check and format check) | pass |
| `make test` | **fail**: 2 of 5,008 pytest tests fail (section 2); fidelity selftest, hazard scan and vitest pass |
| `npm run build` in `ui` | pass |
| licence registry check | pass |
| claims as tests (`scripts/claims_check.py`) | pass: 28 claims, 136 tests |
| `PLATE_FAKE_ENGINE=1 python -m plate_daemon.cli smoke` | pass: 11 adapters ok |
| fixture render idempotency (`bash fixtures/blender/render.sh` twice) | pass: 0 changed files |
| scripted end-to-end smoke (`scripts/smoke_e2e.py --build-ui`, T79) | pass: `SMOKE PASS` |
| UI smoke in a real browser against a real daemon (section 3) | **partial**: every step passes except the viewer's edge overlay and ΔE00 heatmap (gap G1) |

UI smoke: PARTIAL (every step passes except the viewer's edges and ΔE00 heatmap modes, which no daemon route feeds; gap G1 in section 3)

The task's done-when asks for a "UI smoke: PASS" line and a green `make test`. Neither is written as passing because neither passed; sections 2 and 3 name the three defects and their owners.

## 1. Command record, in the order the plan gives

All from the repo root.

| # | command | exit | key output |
|---|---|---|---|
| 1 | `.venv/bin/python scripts/hazard_scan.py` | 0 | `hazard_scan: clean (859 files scanned, 2 allowlist entries)` |
| 1a | `.venv/bin/python scripts/hazard_scan.py --selftest` | 0 | `hazard_scan selftest ok: 13 planted hazards caught, clean sample passes` |
| 1b | `.venv/bin/python scripts/hazard_scan.py --strict-allow` | 0 | `hazard_scan: clean (859 files scanned, 2 allowlist entries)` |
| 2 | `make lint` | 0 | `All checks passed!`, `458 files already formatted` |
| 3 | `make test` | 2 | `SELFTEST PASS`; hazard selftest and scan clean; pytest `2 failed, 5005 passed, 1 skipped in 529.12s`; make stops there, so vitest ran as step 3a |
| 3a | `npm test` | 0 | `Test Files 14 passed (14)`, `Tests 506 passed (506)` |
| 3b | `.venv/bin/pytest -rs` (re-run for skip reasons) | 1 | same: `2 failed, 5005 passed, 1 skipped in 545.10s` |
| 4 | `npm run build` (in `ui/`) | 0 | `dist/index.html`, `dist/assets/index-*.js 612.63 kB`, `built in 1.28s` (Vite's 500 kB chunk warning only) |
| 5 | `.venv/bin/python -m plate_pixels.licences --check` | 0 | `licence check OK: 17 files scanned, 7 blocked models absent` |
| 6 | `.venv/bin/python scripts/claims_check.py` | 0 | `claims_check: all 28 claims hold (136 tests passed)` |
| 7 | `PLATE_FAKE_ENGINE=1 .venv/bin/python -m plate_daemon.cli smoke` | 0 | `fixture smoke: 11 adapters ok` (8 routes skipped, listed in section 6) |
| 8 | `bash fixtures/blender/render.sh`, then `git status --porcelain fixtures \| wc -l` | 0 | stamps current (`base`, `ral3020`, `v2`: up to date), repack validated 6 packets; `0` changed files, and `diff -r` against a copy taken before the run is empty |
| 9 | `perl -e 'alarm shift; exec @ARGV' 900 .venv/bin/python scripts/smoke_e2e.py --build-ui` | 0 | 28 steps `ok` (UI suite 506, wiring 19, build, daemon start, project, packet stage/post/ingest/list, thumb, engines incl. fake, summary, graph, quote with `router ai_by_region`, approve, DONE, gate `advisory`, scrub, accept, server-derived approval, export + lineage, shipped, runs list, rescore `versions [1, 2]`, `index.html`, `text/javascript` asset, JSON 404), last line `SMOKE PASS`; the `.smoke/` home was removed |
| 10 | `.venv/bin/pytest scripts/tests` (the `make ci` step outside `make test`) | 0 | `79 passed` |
| 11 | `git ls-files -z fixtures \| xargs -0 du -ch \| tail -1` | 0 | `17M total` (cap 25 MB) |

Pytest per package (junit of run 3b): daemon 1,492 passed, 1 failed, 1 skipped; engines 633; fixtures 130; gates 554; pixels 314 passed, 1 failed; plugins/max 867; spec 115; repo-root `tests` 850; tools/plate_label 50.

## 2. The two failing tests

Both fail deterministically when run on their own. Neither is in this task's files, so neither was edited here.

| test | why it fails | fix | file owner |
|---|---|---|---|
| `daemon/tests/test_people.py::test_run_places_a_person_behind_the_box_and_keeps_every_other_pixel` | It asserts `checks["feather_band.de00_p99"] == "pass"`. Wave 8 decision 35 (T60) made the feather band advisory in `gating.run_gates` (`status="advisory"`, `daemon/plate_daemon/gating.py:335`), so the check now reports `advisory`. The test predates that change and was not in T60's run. The People result itself is right: alpha 0 behind the box, outside-mask bit identity, footprint under 10%. | assert `== "advisory"` for `feather_band.de00_p99` (keep `outside_mask.de00 == "pass"`) | T45 |
| `pixels/tests/test_flow_nvml.py::test_model_slot_admits_and_uses_the_backends_model_id_for_windows` | Depends on the wall clock. It builds `Admission(windows="01:00-06:00")` and expects "outside the scheduled GPU window", but `Admission.in_window()` defaults to `datetime.now()`, and this pass ran at 03:09 to 03:40 IST, inside the window. It passes from 06:00 to 01:00. | take the window from the clock (for example `now + 2 h` to `now + 3 h`), or patch `in_window` to `False` in the test | T22 |

## 3. UI smoke

### How it was driven

The orchestrator's hand-driven browser pass was done by script in a real browser: chrome-headless-shell 153 (Playwright's cached `chromium_headless_shell-1243`), driven by `playwright-core` over CDP, WebGL2 through SwiftShader, viewport 1600×1000, device pixel ratio 1. Every click, wire and key press goes through the page as an artist's would: palette buttons, handles dragged with the mouse, editor fields, viewer keys. Screenshots were checked at every step. Nothing was installed and nothing was downloaded.

### The exact steps (as run, corrected where the plan's text no longer matches the code)

Start a daemon on a throw-away home. `PLATE_LABELS` keeps the verdict out of the repo's `calib/labels.jsonl` (see C7). On Windows, set each variable with `$env:NAME = 'value'`.

```bash
cd /Users/aasish/Desktop/plate
export SMOKE="$HOME/plate-smoke-home"
PLATE_HOME="$SMOKE" PLATE_LABELS="$SMOKE/labels.jsonl" PLATE_FAKE_ENGINE=1 PLATE_PIXELS_INPROC=1 PLATE_NIGHTLY=0 \
  .venv/bin/python -m plate_daemon
```

Stage the fixture under the project's id. The fixture's manifest names project `plate-fixture`; see C3.

```bash
mkdir -p "$SMOKE/packets" && cp -R fixtures/packets/room_a "$SMOKE/packets/room_a"
.venv/bin/python -c "import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); m=json.loads(p.read_text(encoding='utf-8')); m['project_id']='smoke'; p.write_text(json.dumps(m, indent=1)+'\n', encoding='utf-8', newline='\n')" "$SMOKE/packets/room_a/manifest.json"
TOKEN=$(PLATE_HOME="$SMOKE" .venv/bin/python scripts/plate_config.py token)
```

| # | step | observed | result |
|---|---|---|---|
| 1 | Open the printed `http://127.0.0.1:4610/#token=...` as is | status bar `daemon: up v0.1.0 (stub engine)`, `token: present`; the fragment is gone from the address bar and the token sits in `sessionStorage` (`plate.token`) | pass |
| 2 | Projects tab, New project: id `smoke`, NDA unticked, grades C0 C1 C2 D, Create project | `smoke` listed and opened, `not NDA · C0 C1 C2 D`, empty canvas | pass |
| 3 | `curl -X POST http://127.0.0.1:4610/api/v0/packets -H "Authorization: Bearer $TOKEN" -H "X-Plate-Packet-Version: 0.1.1" -H "Content-Type: application/json" -d "{\"packet_path\": \"$SMOKE/packets/room_a\", \"sender\": {\"name\": \"curl\", \"version\": \"0\"}}"` | `202 {"packet_id":"pkt_2ZHQ7DX53BCK1J9P8GJB9Z2V5G","status":"ingesting",...}`, ingested in 0.12 s; `GET /projects/smoke/packets` lists it with plate `807554a6...`, 640×360, camera `CAM_a` | pass |
| 4 | `+ node`: Plate, Mask, Edit, Export; drag Plate `packet` to Mask `source`, Plate `beauty` to Edit `image`, Mask `mask` to Edit `mask`, Edit `image` to Export `media` | 4 edges, no refusal, autosave `PUT /projects/smoke/graph` 200 | pass |
| 5 | Select Plate; in the Plate editor, link `room-a` (Tier A, CAM_a, 640 x 360) | node thumbnail of the plate (served by reference from the packet folder); summary `31 ids · 8 editable · 23 locked` | pass |
| 6 | Select Mask; Pick id; click the shrub at the bottom left (image pixel 56, 324) | last pick `VEG_shrub_A vegetation`, chip added, `output: 1 id` (`o:55cbe981`) | pass |
| 7 | Select Edit; type a prompt; Make bar engine `Nano Banana 2.1 · D`; Quote | `gemini · nano-banana-2.1 · 0.034 USD · D · ETA ~60 s · unconfirmed · expires in 15:00`. The list offered FLUX 3 Image D, GPT Image 2.5 D, Nano Banana 2.1 D, Nano Banana Pro D, Qwen-Image-Edit-2509 C0, Seedream 5 Pro D, Fake (echo) C0. Not made (cloud route; see C4) | pass |
| 8 | Engine `Fake (echo) · C0`, N 2, Quote, Make | `fake · fake · 0.080 USD (2 × 0.040 USD) · C0 · ETA ~60 s`; Make is the human approval (the dialog opens only on a refusal); `POST .../fanout` and `.../approve` 200; two candidates `DONE`, score 1.00 each, both runs `approved_by: human` | pass |
| 9a | Click candidate 1: the viewer shows `A plate · B candidate 1`; wipe, then `` ` `` toggle (candidate 1, then plate), then `d` difference ×4 | each mode drew, no viewer error | pass |
| 9b | `e` edges, `h` ΔE00 heatmap | notices `no edge masks for this view`, `no ΔE00 map for this view`; the mode stays on Diff | **fail (G1)** |
| 9c | `t` tiles | on this passing take: `no gate-flagged points for tiles` (correct: the report flags none). On a second daemon whose fake shifts the take 12 px (`PLATE_FAKE_ENGINE='{"echo": true, "shift_px": 12}'`, port 4620), the take is `GATE_FAIL` on `registration.shift_px 12 > 8`; Gate panel `Open tiles (2)` puts the viewer in Tiles mode with 2 plate/candidate pairs at 100% | pass |
| 10 | Gate tab | verdict `advisory`, version `v1`, 14 checks with value, threshold and status: advisory on `feather_band.de00_p99 0.2095` (decision 35), `texture.lapvar 707.3`, `tone.luma 121.8`; pass on registration (0.008 px, shift 0.0006 px), `outside_mask.de00 0`, `gbuffer_edge_recall_r2 1.0`, `invention_flags 0`, material ΔE00 0/0, `texture.fine_ratio 0.571`, tone contrast and saturation, image edge agreement 0.9999. The plan expected "pass"; see C6 | pass |
| 11 | Verdict `1` with focus outside the viewer, before any scrub | `a full scrub must be logged before a verdict`; Full scrub and Accept take disabled | pass |
| 12 | Focus the viewer, `1` (100%), shift+arrows | `4/4 quadrants at 100%`; tick Full scrub: `POST /runs/{id}/scrub` 200, `Full scrub (logged)` | pass |
| 13 | `1` again, then Accept take | `Ship recorded`, Accept offered, then `Accepted`; the Edit node shows `gate pass` and `approved`; the stored graph has `edit-1` `gateStatus: pass`, `approved: true`, `approvedBy: human`, `scrubLogId: scrub-11bfbb4bf26d`, all server-derived | pass |
| 14 | Select Export; candidate `run_01M5... · attempt 1 · fake · DONE`; format png16; out dir `$SMOKE/out`; Export | `run_01M55WVBB4DNFK19FVVW9KCNN2.png` (640×360, 3 channels, uint16) and `.gate.json`; `lineage.read` names project `smoke`, node `edit-1`, the run, packet, engine `fake` and `gates/<run>.1.json`; `GET /runs/{id}/gate` now `shipped: true` | pass |
| 15 | Add Review, wire from Edit, tick one take, Build bundle | `projects/smoke/review/review-51e99f405c473bd2.zip`; unzipped `index.html` opened from `file://` shows the take, the ADVISORY badge and wipe/toggle; no page errors | pass |
| 16 | Add Concept, wire Plate `beauty` to it, then Concept to a new Export | refused on the node: `CONCEPT_TO_DELIVERY concept tier cannot reach Export`; no edge added | pass |
| 17 | Add a second Edit (not approved) from Plate, then wire it to a new Motion | refused: `VIDEO_LOCKED motion is locked until the upstream still passes gates and is approved` | pass |
| 18 | On the shifted daemon: Export the `GATE_FAIL` take | the Export button stays disabled until an override reason is typed; nothing written | pass |
| 19 | Allowlist and auth probes (curl against the smoke daemon) | Origin `http://localhost:4610` 200, `http://localhost:5173` 403 (no `PLATE_DEV`), foreign origin 403, foreign Host 403, no token 401, `POST /api/v0/nope` a JSON 404 | pass |

### Gap G1: edge overlay and ΔE00 heatmap are not wired in the running app

`ui/src/canvas/CompareViewer.tsx` hands the viewer `a` and `b` only, and `ui/src/panels/ConnectedViewer.tsx` adds `tiles`. Nothing passes `overlays.edges` or `overlays.heatmap`, and no daemon route serves a run's G-buffer edge mask or per-pixel ΔE00 map: `GET /runs/{id}/gate` returns JSON, and `GET /assets/{hash}` serves the CAS and plates. The two modes work in vitest and on the dev demo page, where the sources are synthetic. They can never draw against a real daemon. A fix would have gating store `edges_plate.png`, `edges_candidate.png` (8-bit, white = edge) and the ΔE00 map (R16F) in the project CAS at gate time, list their hashes in the gate response, and have `CompareViewer` map them onto `overlays`. That needs a new task across `daemon/plate_daemon/gating.py`, `routes/runs.py` and `ui/src/canvas/CompareViewer.tsx`.

### Corrections to the plan's smoke text (T53 spec)

| | the plan says | the code does | what the steps above use |
|---|---|---|---|
| C1 | `curl ... -d '{...}'` with no content type | `422 {"errors":["<body>: Input should be a valid dictionary ..."]}` | `-H "Content-Type: application/json"` |
| C2 | token in `/tmp/plate-smoke/token` | no token file; the token is in `<home>/config.json` (`PLATE_TOKEN_FILE` only when set) | `scripts/plate_config.py token` |
| C3 | register `fixtures/packets/room_a` for project `smoke` | the manifest names `plate-fixture`; posted by path, the packet is filed under an auto-created `plate-fixture` project (custody unset, C0 only) and `smoke` lists nothing. Re-posting the same `packet_id` with a changed manifest gives 409 | stage a copy with `project_id: smoke` before the first post (what `scripts/smoke_e2e.py` does) |
| C4 | Edit with engine `nano-banana-2.1`, Make | quotes (grade D, price `unconfirmed`); a Make would need a live Gemini key. The route has no recorded fixture yet (section 6) | quote nano-banana-2.1, Make on `fake` |
| C5 | `localhost` is allowed only under `PLATE_DEV=1` | `http://localhost:4610` is always allowed; `PLATE_DEV=1` adds only the two `:5173` origins (as `docs/ui.md` says) | the printed `127.0.0.1` URL |
| C6 | the Gate panel shows a pass report | `advisory`: decision 35 makes `feather_band.de00_p99` advisory on every in-mask edit until calibration | advisory accepted as the expected verdict |
| C7 | (not mentioned) | the verdict route appends to `<repo>/calib/labels.jsonl` unless `PLATE_LABELS` is set. This pass did not set it, so the Ship in row 13 added one line (`run_01M55WVBB4DNFK19FVVW9KCNN2`); the file now holds 4 lines. Open question 17 already says to delete it before any calibration run | `PLATE_LABELS` in the smoke home |
| C8 | `PLATE_HOME=/tmp/plate-smoke` | any throw-away folder works | `$HOME/plate-smoke-home` |

## 4. Tests skipped on this Mac

| suite | skipped | why |
|---|---|---|
| pytest | `daemon/tests/test_previs.py:396` | Homebrew's ffmpeg has no `drawtext` filter (built without libfreetype); the overlay backend that burns the PREVIS watermark is covered instead |
| pytest | none of the client-data tests | `/Users/aasish/Desktop/test` is present, so they ran; on a machine without it they skip through `plate_gates.tests.clientdata.path` |
| pytest | none of the PySide6 dock tests | they ran offscreen |
| vitest, claims, scripts | none | |

Not skipped, but outside what a test can show here: no test talks to a real `pymxs`, V-Ray, Vantage, CUDA, NVML, STAFFAGE or a vendor; the gate's optical flow ran on OpenCV DIS (the report says so), not RAFT.

## 5. Verifier pass: BUILD_PLAN decisions 1 to 30

Status words: **done** (built and tested here), **partial (task)** (some of it is missing; the task named owns the rest), **deferred-to-box** (code done and fake-tested; the named verify-on-box line proves it), **deferred-to-P5**, and **n/a** (not a code decision; who owns it is named).

| # | decision | status | evidence or remainder |
|---|---|---|---|
| 1 | Input scope: Tier A packets, Tier C plates, client kit | done; deferred-to-box (T21, T22) | Tier A ingest (smoke above), Tier C route with consent record (T38, T72), kit MAXScript and `kit_to_packet` (T21). On the Mac the Tier C models are stubs |
| 2 | Colour master is the VFB TIFF16; P0 OCIO re-derivation check | partial (T9 on the box) | OCIO core and TIFF16 master (T3) done. The TIFF16 vs OCIO ΔE00 p99 ≤ 0.5 check and the un-tone-mapped egress A/B need the box and a live vendor (`docs/colour.md` rows open) |
| 3 | Egress: 8-bit sRGB PNG with ICC, overscan pad, region crop | done | T4, T14; region crop never upsamples (open question 9) |
| 4 | Ingress: ECC registration, Lanczos, ring tone match, feather inside the mask | done | T4, T25; identity round trip test; outside-mask ΔE00 0 in every gate report above |
| 5 | Default element set, overhead caps | deferred-to-box (T19) | elements forced by the plugin (T9, fake-tested); the overhead number is a box measurement |
| 6 | Packets render in the interactive Max session; Vantage by vrscene | deferred-to-box (T9, T37) | Chaos questions (CLI licence, background renders) are Aasish's emails |
| 7 | Backend shape: one daemon, WAL index in app data, CAS, separate GPU worker, UI served by the daemon | done | proven live in this pass (daemon, index under `PLATE_HOME`, built UI at `/`, in-process pixels backends); the Tailscale bind is off by default |
| 8 | LLM route: Anthropic API, Sonnet composer, Opus two-sample judge | done | T33 on recorded fixtures only; Anthropic is effective grade D until terms are filed, so NDA projects get no composer and no judge (claim `judge_cannot_overrule_numbers`) |
| 9 | Custody grades C0 to D, NDA on C0/C1 | done | T14; claims `nda_c0_c1_only`, `pending_terms_count_as_d`, `nda_egress` |
| 10 | Retroactive contract review | n/a (Aasish, counsel) | the code side is done: consent template (`docs/consent-template.md`) and custody per project |
| 11 | GPU and RAM admission by NVML, explicit unload, RAFT ≤ 960 px | deferred-to-box (T22) | admission, windows and slots built (T22); the window test is clock-dependent (section 2) |
| 12 | Host scope: 3ds Max 2026 only | done | schema `host.app: 3dsmax \| none`; no other sender |
| 13 | Engine roster, pins, retirement alerts, same-model fallback only | done; deferred-to-box (live smoke) | 14 manifests plus the fake, adapters on recorded fixtures (T17, T18), pins and alerts (T27); prices `unconfirmed`, 9 manifests flagged `unverified_p0`, Nano Banana 2.1's direct route has no recording yet |
| 14 | One gate value per metric, resolution stated | done | values as corrected by plan decision 3 and decision 35, with `calibration_needed` where the plan says so; 4K gate run 4.82 s on CPU (T67) |
| 15 | Render-it router: advisory in P1, blocking in P3 | done (advisory) | T28; `PLATE_ROUTER_MODE=blocking` exists; turning it on is a P3 decision after calibration. Every quote above carried the router verdict (`ai_by_region`, D median 19.1 px) |
| 16 | Re-photograph close and medium frames only | done | router doctrine by shot class (quote reason `doctrine: task 'relight' on a wide shot`) |
| 17 | Client revisions: packet invalidation, approved layers carry over | done | T41 |
| 18 | Productisation gate at week 16 | deferred-to-P5 | numbers from the ledger and the interviews |
| 19 | Hiring | n/a (Aasish) | |
| 20 | Phase order: stills first | n/a (sequencing) | the build followed it; the swap decision is a box-day baseline number (open question 11) |
| 21 | Client review as a static bundle and a comment form | done | T40; a bundle built from the UI opened offline in this pass; R2 hosting is a documented manual step (`review_template/HOSTING.md`) |
| 22 | Agent permissions over MCP | done | T39, T80; claim `mcp_cannot_approve_or_deliver` |
| 23 | Backup, retention, hash-chained ledger, purge | done | T42, T13; the offsite copy and the quarterly restore drill are operations (`docs/runbooks/backup-restore.md`) |
| 24 | VizMaker benchmark on owned scenes | n/a (optional, Aasish) | |
| 25 | Max-side install: startup script, handshake, uninstall, Safe Scene check, shared bridge | deferred-to-box (T20, T49) | T8, T20, T49, T75, T77 fake-tested |
| 26 | Lineage inside every output plus a phash index | done | T26 (EXR attribute, PNG iTXt, TIFF XMP, MP4/MOV udta); PNG lineage read back in this pass |
| 27 | Gate report versioning; re-scoring never rewrites a shipped report | done | `rescore` gave versions `[1, 2]` in the scripted smoke; claim `shipped_report_never_rewritten` |
| 28 | Human-minute timer in the app and a CLI | done | T36, T38, T78 (`plate timer`); the P0 baseline is Aasish's |
| 29 | One consolidated budget | n/a (Aasish) | `plate spend` and the ledger report it |
| 30 | Windows first-class from day 1 | partial (T11 and T80 on the box) | hazard scan, portability and PowerShell static tests are green on the Mac. The suite has never run on Windows, and CI has never run because there is no git remote (open question 13) |

## 6. Verifier pass: section 4 and 5 deliverables, P0 to P4

### Architecture (section 4)

| component | status | note |
|---|---|---|
| Shot Packet v1 (manifest, schema, legend, policy, transport over HTTP, BLAKE3 on ingest, SSE) | done | spec 0.1.1; packet events streamed in this pass |
| Max plugin actions: Send Packet, Batch Cameras, Send Motion, Receive, Light, Scout, Vantage hooks | deferred-to-box (T19, T37, T77) | all fake-pymxs tested; every V-Ray/Vantage property name is `TODO-verify` until `plugins/max/ONBOX.md` runs |
| Daemon state machine QUOTED-first, idempotency, boot re-poll, best-of-N under one quote, priority classes, budgets | done | fan-out of 2 under one quote, one approval for the group, in this pass |
| Routing: manifests as data, custody first, nightly prices, daily reconciliation, contract tests, engine cards, canaries | done; deferred-to-box (live) | the daemon assumes routes healthy until canaries run (decision 44) and says so in the quote |
| Pixels service with the licence registry | done (stub); deferred-to-box (T22) | real DA-V2 Small, GeoCalib, SAM 2.1, RAFT, PaddleOCR need the CUDA extra |
| Canvas: node kinds, viewer, enforced rules, project folder, rebuildable index | partial (G1) | everything drove in a real browser except the viewer's edges and heatmap modes |

### P0 deliverables and exits

| item | status |
|---|---|
| PACKET.md and schema | done (0.1.1) |
| on-box spike report | deferred-to-box (T9; `docs/onbox-spikes.md` rows all "not run") |
| conformance scene with the reprojection check | done for the Blender fixture (`fixtures/tests/test_packets_valid.py::test_marker_reprojection_rms_under_half_a_pixel_at_640_wide`, 8 markers); the Max scene is box work (open question 8). The plan's exit says 3840 wide; the fixture measures at 640 |
| provider matrix for ten engines | partial: manifests and `docs/custody.md` done; billed price to the cent needs `plate smoke --live` (≤ USD 5) |
| labelling UI and the first 100 labels | UI done (T23); labels are Aasish's (none yet; `calib/labels.jsonl` holds 4 audit/smoke lines to delete) |
| baseline of two live jobs; day-1 emails | n/a (Aasish) |
| client-consent template | done |
| exits: real camera packet reprojects ≤ 1 px; markers ≤ 0.5 px RMS at 3840; TIFF16 vs OCIO; element overhead; billed price; Windows suite and CI green | deferred-to-box (T9, T11, T19); identity round trip at ΔE00 p99 ≤ 0.5 is done (T4) |

### P1 deliverables and exits

| item | status |
|---|---|
| gates package (register, comp_masked, labmatch, gbuffer edges, invention, depth agreement, maskdiff, texture, tone, crop pair, video) with goldens | done (554 gate tests, procedural and Blender goldens) |
| adapters for Nano Banana 2.1 and Pro, GPT Image 2.5, Seedream 5 Pro, Kling O3 4K and v3 Pro, Seedance 2.5 i2v and r2v, direct Gemini and OpenAI | done on fixtures; deferred-to-box (live smoke); Nano Banana 2.1 via Gemini, Magnific and Seedance r2v via BytePlus/WaveSpeed have no recorded call yet |
| ledger and daily reconciliation | done (T13) |
| `plate run / gate / export` with three Lensbook recipes | done (`night_relight_changelist`, `sunburst_rephotograph_close`, `croppair_pushin`) |
| advisory render-it router; calibration report | done (the report needs labels) |
| exits: AUC ≥ 0.85 on ≥ 100 takes; two live client jobs; ledger equals billing within 1% | deferred (labels and live jobs) |
| exit: gate run on a 4K still ≤ 10 s | done on CPU: `run_gates 3840x2160: wall 4.82s` |

### P2 deliverables and exits

| item | status |
|---|---|
| canvas, Make bar (provider, model, price, custody, ETA), N fan-out, enforced rules | done (this pass) |
| WebGL viewer | partial (G1): wipe, toggle, difference, tiles live; edges and heatmap only in tests |
| lineage restore by drop; open project format | done (T26, T36; drop was not exercised in the browser here) |
| Max dock (Send Packet, Batch Cameras, Receive), startup installer, fake-pymxs and import-contract tests | deferred-to-box (T19, T20) |
| MCP server; static review bundles; Tier C ingestion | done |
| exits: a ≥ 6-camera client set; minutes ≤ 50% of baseline; Max click to first candidate ≤ 60 s; a client opens a bundle; a Sonnet agent runs a batch over MCP; the canvas from the box, a second Windows machine and the Mac; a fresh Windows bootstrap in < 15 min | deferred-to-box; the Mac browser leg is done (this pass) |

### P3 deliverables and exits

| item | status |
|---|---|
| Move node with the Lensbook risk policy and the router | done (T43; advisory until P3 flips the mode) |
| CropPair; Kling O3 4K and v3 Pro start/end adapter | done (fixtures) |
| Send Motion in Max | deferred-to-box (T37, T77) |
| Seedance 2.5 r2v previs with PREVIS watermark | done (T44, fixtures; watermark by the overlay backend on this Mac) |
| video gates with drift and the frame-rate time map | done (T29; DIS here, RAFT on the box: T29 line) |
| People node over STAFFAGE with packet-Z occlusion | done against the fake STAFFAGE (T45); one stale test, section 2 |
| FCPXML, ProRes 4444, EXR delivery | done (T46, route T74) |
| local Wan 2.2 VACE previs | deferred-to-box (stub; T22) |
| exits: a shipped 30 to 60 s film section; gates agree with ≥ 80% of ≥ 30 rejections; ≤ 2.5 generations per clip; zero accepts without a scrub | deferred (live work); the scrub rule is enforced (this pass, row 11) |

### P4 deliverables and exits

| item | status |
|---|---|
| Set node with per-material targets and the cross-view gate | done (T30, T48) |
| Light node over MaxGaffer with Vantage live-link preview | deferred-to-box (T37, T77) |
| LightMix NNLS | done (T32) |
| Recolour by recomposition, validated once against a V-Ray re-render | code done (T31, T48); validation deferred-to-box (T31) |
| Scout node; shared Max bridge | deferred-to-box (T37, T49) |
| client-revision carry-over; backup and purge | done (T41, T42) |
| runbooks and the manual-pipeline fallback | done (T51, T80: nine runbooks) |
| exits: eight-camera villa set at cross-view p90 ≤ 3; a V-Ray dusk variant through the Light node; RAL panel ≤ 1 vs a re-render; ≥ 3 deliverables a week | deferred-to-box and live work |

## 7. Box-day checklist

Every `verify-on-box:` line in the router plan, in task order, then the box-only exits that have no task line.

1. T8: none yet (fake only); the property names in `FakeCamera` are a hypothesis until T9's on-box list runs.
2. T9: run `plugins/max/ONBOX.md` steps 1 to 7 in Max 2026 with V-Ray 7 on a TULA camera; record results in `docs/colour.md` and `docs/onbox-spikes.md`; fix every `TODO-verify` name.
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
14. P0: TIFF16 vs OCIO re-derivation ΔE00 p99 ≤ 0.5, or the mismatch understood and the master chosen (`docs/colour.md`).
15. P0: the conformance scene built in Max from `fixtures/blender/markers.json`; eight markers reproject within 0.5 px RMS at 3840 wide.
16. P0: `plate smoke --live` with a stated budget (≤ USD 5): billed price per engine to the cent, the vendor model ids behind every `unverified_p0` flag, and a recorded minimum-cost call for Nano Banana 2.1 on the Gemini route.
17. P0: push the repo to a private remote so the `windows-latest` and `macos-latest` CI legs run once (open question 13).
18. P2: the same daemon driven from a browser on the box and on a second Windows machine (this pass covered the Mac); a fresh Windows machine from bootstrap to first candidate in under 15 minutes.
19. Before any calibration run: delete `calib/labels.jsonl` (open question 17; this pass added one smoke line to it).

## 8. Other observations

- `claims.yaml`, claim `previs_is_not_deliverable`: the caveat says the artist manual still says previs can never reach Review. `docs/artist-manual.md:321` already reads "can never reach Export or Delivery ... It may go into a Review bundle". The caveat is stale; the claim holds.
- CLI fixture smoke skipped 8 routes: Magnific and Nano Banana 2.1 via Gemini (no recorded call, no vendor model id: `unverified_p0`), Nano Banana 2.1 via Atlas (no adapter speaks the route), Seedance 2.5 r2v via BytePlus and WaveSpeed (no recording), and the three local stubs (Qwen, Real-ESRGAN, Wan 2.2). So the default stills engine on its direct route has not been exercised even on fixtures.
- The full scrub on the 640×360 fixture plate in a 320×180 viewer is complete at the centre view at 100%: the four quadrant centres fall exactly on the viewport edges, and the comparison is inclusive. That is correct by the rule's definition (each centre is in view at 100%). A 4K plate needs real panning.
- With a fresh quote, Make approves directly as the human (`approved_by: human`); the approval dialog appears only when the daemon refuses (auto cap, envelope). This matches `docs/ui.md`.
- `room_a` and `room_a_ral3020` are 3.78 MB, over the 3 MB per-packet guideline (known since wave 2); the 25 MB total cap holds at 17 MB.
- `make test` stops at the first failing step, so a pytest failure hides the vitest result; `npm test` was run on its own here (506 passed).
