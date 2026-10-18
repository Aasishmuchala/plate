# PLATE verification record

Written by the final verification pass (router task T53, Opus 5.5) on 2026-10-18 between 07:20 and 07:50 IST, after Wave 9 (T81 to T110), on the Mac (macOS 27.0 arm64, Python 3.12.12, Node 22.22.3, npm 10.9.8, Blender 5.1.1, ffmpeg from Homebrew, no `oiiotool`). Commit `23c147e` plus the uncommitted T107 (api wiring) and T108 (docs) edits in the working tree. Nothing here ran on the Windows render box, inside 3ds Max or against a live vendor; those lines are in the box-day checklist (section 8).

The first pass (03:00 to 04:00 IST, commit `dd78f02`) found two failing tests and gap G1 (viewer edges and ΔE00 heatmap not fed by the daemon). The orchestrator fixed both tests in `8d65859`; T109 closed G1. This pass re-ran everything and found one new defect, G2 (section 2).

## Result

| check | result |
|---|---|
| Windows-hazard scan (`scripts/hazard_scan.py`, `--selftest`, `--strict-allow`) | pass |
| `make lint` (ruff check and format check) | pass |
| `make test` (fidelity selftest, hazard, pytest for every package, vitest) | pass: 5,877 pytest passed, 1 skipped; 751 vitest passed |
| `npm run build` in `ui` | pass |
| licence registry check | pass |
| claims as tests (`scripts/claims_check.py`) | pass: 28 claims, 136 tests |
| `PLATE_FAKE_ENGINE=1 python -m plate_daemon.cli smoke` | pass: 11 adapters ok |
| fixture render idempotency (`bash fixtures/blender/render.sh` twice) | pass: 0 changed files |
| scripted end-to-end smoke (`scripts/smoke_e2e.py --build-ui`, T79) | pass: `SMOKE PASS` |
| UI smoke in a real browser against a real daemon (section 2) | pass: every step of the plan's smoke, including the edges and ΔE00 modes |

UI smoke: PASS

One defect outside the plan's smoke steps was found while exercising Wave 9 in the browser: G2, a new take on an approved Edit blocks every graph save while a Motion node hangs off it (section 2). It does not touch any step of the plan's smoke, so the line above stays PASS; it needs a task.

## 1. Command record, in the order the plan gives

All from the repo root.

| # | command | exit | key output |
|---|---|---|---|
| 1 | `.venv/bin/python scripts/hazard_scan.py` | 0 | `hazard_scan: clean (966 files scanned, 2 allowlist entries)` |
| 1a | `.venv/bin/python scripts/hazard_scan.py --selftest` | 0 | `hazard_scan selftest ok: 13 planted hazards caught, clean sample passes` |
| 1b | `.venv/bin/python scripts/hazard_scan.py --strict-allow` | 0 | `hazard_scan: clean (966 files scanned, 2 allowlist entries)` |
| 2 | `make lint` | 0 | `All checks passed!`, `514 files already formatted` |
| 3 | `make test PYTEST=".venv/bin/pytest -rs --junitxml=<scratch>/junit.xml"` (the same targets; the two flags only add skip reasons and per-package counts) | 0 | `SELFTEST PASS`; hazard selftest and scan clean; pytest `5877 passed, 1 skipped in 646.63s`; vitest `Test Files 26 passed (26)`, `Tests 751 passed (751)`; 4K gate lines `run_gates 3840x2160: wall 3.68s per_material 0.76s` and `run_gates 3840x2160 with overlays: wall 4.19s overlays 0.21s` |
| 4 | `npm run build` (in `ui/`: `tsc -b && vite build`) | 0 | `dist/assets/index-CMwEfprQ.js 755.25 kB`, `RecompositeBar-*.js 2.39 kB`, `built in 1.18s` (Vite's 500 kB chunk warning only) |
| 5 | `.venv/bin/python -m plate_pixels.licences --check` | 0 | `licence check OK: 17 files scanned, 7 blocked models absent` |
| 6 | `.venv/bin/python scripts/claims_check.py` | 0 | `claims_check: all 28 claims hold (136 tests passed)` |
| 7 | `PLATE_FAKE_ENGINE=1 .venv/bin/python -m plate_daemon.cli smoke` | 0 | `fixture smoke: 11 adapters ok` (8 routes skipped, listed in section 9) |
| 8 | `bash fixtures/blender/render.sh` twice, then `git status --porcelain fixtures \| wc -l` | 0 | stamps current (`base`, `ral3020`, `v2`: up to date), repack validated 6 packets; `0` changed files after each run, and `diff -r` against a copy of `fixtures/packets` taken before the runs is empty |
| 9 | `perl -e 'alarm shift; exec @ARGV' 900 .venv/bin/python scripts/smoke_e2e.py --build-ui` | 0 | 28 steps `ok` (UI suite 751, wiring 19, build, daemon start, project, packet stage/post/ingest in 0.3 s/list, thumb, 15 engines incl. fake, summary, graph, quote with `router ai_by_region`, approve, DONE in 0.8 s, gate `advisory`, scrub, accept, server-derived approval, export + lineage, shipped, runs list, rescore `versions [1, 2]`, `index.html`, `text/javascript` asset, JSON 404); last line `SMOKE PASS`; `.smoke/` removed |
| 10 | `.venv/bin/pytest scripts/tests` (the `make ci` step outside `make test`) | 0 | `79 passed` |
| 11 | `git ls-files -z fixtures \| xargs -0 du -ch \| tail -1` | 0 | `17M total` (cap 25 MB) |

Pytest per package (junit of run 3): daemon 2,101 (1 skipped), engines 673, fixtures 130, gates 601, pixels 315, plugins/max 926, spec 115, repo-root `tests` 954, tools/plate_label 63; no failures anywhere. The two tests that failed in the first pass (`test_people.py` feather band, `test_flow_nvml.py` GPU window) are fixed in code, not by the clock: the window test now builds its window from `now + 2 h`.

The `make test` run overlapped the browser smoke below (an idle daemon and a headless browser); the 4K gate stayed at 3.68 s (4.19 s with the overlays), under T67's 10 s.

## 2. UI smoke

### How it was driven

The orchestrator's hand-driven browser pass was done by script in a real browser: chrome-headless-shell 153 (Playwright's cached `chromium_headless_shell-1243`), driven by `playwright-core` 1.59.1 from an existing `node_modules` on this Mac over CDP, WebGL2 through SwiftShader, viewport 1600×1000. Every click, wire and key press goes through the page as an artist's would: palette buttons, handles dragged with the mouse, editor fields, viewer keys. Screenshots were checked at every step. Nothing was installed or downloaded; the driver scripts and screenshots stayed in the session scratchpad.

### The exact steps (as run, corrected where the plan's text no longer matches the code)

Start a daemon on a throw-away home. `PLATE_LABELS` is no longer needed: verdict labels land in `<PLATE_HOME>/calib/labels.jsonl` (T110). On Windows, set each variable with `$env:NAME = 'value'`.

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
| 3 | `curl -X POST http://127.0.0.1:4610/api/v0/packets -H "Authorization: Bearer $TOKEN" -H "X-Plate-Packet-Version: 0.1.1" -H "Content-Type: application/json" -d "{\"packet_path\": \"$SMOKE/packets/room_a\", \"sender\": {\"name\": \"curl\", \"version\": \"0\"}}"` | the plan's literal command (no content type) gives `422`; with it, `202 {"packet_id":"pkt_2ZHQ7DX53BCK1J9P8GJB9Z2V5G","status":"ingesting",...}`, ingested in 0.10 s; `GET /projects/smoke/packets` lists it (plate `807554a6...`, 640×360, camera `CAM_a`) | pass |
| 4 | `+ node`: Plate, Mask, Edit, Export; drag Plate `packet` to Mask `source`, Plate `beauty` to Edit `image`, Mask `mask` to Edit `mask`, Edit `image` to Export `media` | 4 edges, no refusal; the save chip goes `unsaved` then `saved 07:24:19` (`PUT /projects/smoke/graph` 200) | pass |
| 5 | Select Plate; in the Plate editor, link `room-a` (Tier A, CAM_a, 640 x 360) | node thumbnail of the plate (served by reference from the packet folder); summary `31 ids · 8 editable · 23 locked` | pass |
| 6 | Select Mask; tab Pick id; click the shrub at the bottom left (image pixel 56, 324) | tabs `Pick id, Paint, SAM click, Text lock`; last pick `VEG_shrub_A vegetation`, `not locked`, coverage 100%, depth 1.88 m; chip added; `output: 1 id` | pass |
| 7 | Select Edit; type a prompt; Make bar engine; Quote | `Nano Banana 2.1 · D` is listed but disabled, titled `model id unknown until the P0 smoke test` (also Seedream 5 Pro); see C4. Quoted `Nano Banana Pro · D` instead: chips `max 4096×4096 \| refs ≤ 13 \| per image`, quote `gemini · nano-banana-pro · 0.24 USD · D · ETA ~60 s unconfirmed expires in 15:00`. Not made (cloud route) | pass (C4) |
| 8 | Engine `Fake (echo) · C0`, N 2, Quote, Make | `fake · fake · 0.080 USD (2 × 0.040 USD) · C0 · ETA ~60 s`; Make is the human approval (the dialog opens only on a refusal); `POST .../fanout` and `.../approve` 200; two candidates `DONE`, score 1.00 each; both runs `approved_by: human` | pass |
| 9a | Click candidate 1's thumbnail: the viewer shows `A plate · B candidate 1`; wipe, then `` ` `` toggle (candidate 1, then plate), then `d` difference ×4 | each mode drew, no viewer error | pass |
| 9b | `e` edges, `h` ΔE00 heatmap | both modes draw with no notice: red plate G-buffer edges, cyan candidate edges, white where both agree; the ΔE00 map with its 0 to 10 legend. `GET /runs/{id}/gate` carries `overlays` (`edges_plate` and `edges_candidate` png8 640×360, `de00_map` r16f 640×360). On the echo take the map is near 0 everywhere; on the 12 px shifted take the map lights the shrub only and the cyan edges sit 12 px off the red ones there | pass (G1 closed by T109) |
| 9c | `t` tiles | on this passing take: `no gate-flagged points for tiles` (the report flags none). On the shifted daemon the take is `GATE_FAIL` on `registration.shift_px 12 > 8` (nothing else fails: the composite is still mask-locked); Gate panel `Open tiles (2)` puts the viewer in Tiles mode with plate/candidate pairs at 100% | pass |
| 10 | Gate tab | verdict `advisory`, version `v1`, 14 checks with value, threshold and status: advisory on `feather_band.de00_p99 0.2095` (decision 35), `texture.lapvar 707.3`, `tone.luma 121.8`; pass on registration (0.0083 px, shift 0.0006 px), `outside_mask.de00 0`, `gbuffer_edge_recall_r2 1`, `invention_flags 0`, material ΔE00 0/0, `texture.fine_ratio 0.5714`, tone contrast and saturation, image edge agreement 0.9999. See C6 | pass |
| 11 | Verdict `1` with focus outside the viewer, before any scrub | `a full scrub must be logged before a verdict`; Full scrub and Accept take disabled | pass |
| 12 | Focus the viewer, `1` (100%), shift+arrows | `4/4 quadrants at 100%`; tick Full scrub: `POST /runs/{id}/scrub` 200, `Full scrub (logged)` | pass |
| 13 | `1` again, then Accept take | `Ship recorded`, Accept offered, then `Accepted`; the Edit node shows `gate pass` and `✓ approved`; the stored graph has `edit-1` `gateStatus: pass`, `approved: true`, `approvedBy: human`, `scrubLogId: scrub-f5998821e2a8`, all server-derived. The verdict went to `<PLATE_HOME>/calib/labels.jsonl` (1 line); the repo has no `calib/labels.jsonl` | pass |
| 13b | Focus the candidate strip, press `1` | candidate 1 becomes the node's output: the Edit card shows the take, the stored graph has `outputHash 9694c658...` | pass |
| 14 | Select Export; candidate `run_01M5... · attempt 1 · fake · DONE`; format png16; out dir `$SMOKE/out`; Export | `run_01M56BJ8CH1NKP4TMGDNBGJDVF.png` (640×360, 3 channels, uint16, 2,528-byte ICC profile) and `.gate.json`; `lineage.read` names project `smoke`, node `edit-1`, the run, packet, engine `fake` and `gates/<run>.1.json`; `GET /runs/{id}/gate` now `shipped: true` | pass |
| 15 | Add Review, wire from Edit, tick one take, Build bundle | `projects/smoke/review/review-3f82e349b585df13.zip`; its `index.html` opened from `file://` shows 1 take, the ADVISORY badge, wipe and toggle, 2 images, no page errors | pass |
| 16 | Add Concept, wire Plate `beauty` to it, then Concept to a new Export | refused on the node: `CONCEPT_TO_DELIVERY concept tier cannot reach Export`; no edge added | pass |
| 17 | Add a second Edit (not approved) from Plate, then wire it to a new Motion | refused: `VIDEO_LOCKED motion is locked until the upstream still passes gates and is approved`. Control: Plate `packet` to Motion `packet` and the approved `edit-1` to Motion are accepted; the Motion editor shows start `Edit (edit-1) approved` | pass |
| 18 | On the shifted daemon: Export the `GATE_FAIL` take | the reason field appears and Export stays disabled until a reason is typed; nothing written | pass |
| 19 | Allowlist and auth probes (curl against the smoke daemon) | Origin `http://127.0.0.1:4610` and `http://localhost:4610` 200; `http://localhost:5173`, `http://127.0.0.1:5173` (no `PLATE_DEV`) and a foreign origin 403; foreign Host 403; no token and a wrong token 401; `POST /api/v0/nope` a JSON 404; `/health` carries `plugin {last_poll_utc, queued}` and `alerts_recent` (T107) | pass |

Wave 9 in the same browser session, beyond the plan's steps:

| # | step | observed | result |
|---|---|---|---|
| W1 | Viewer on candidate 1; Re-mask (free) with feather 8 | `Re-masked as run_01M56C3TDBYK9S6MV99FZC5G0N · DONE · 0.00 USD`; the run joins the strip | pass |
| W2 | Make bar `Compare engines…`; tick Fake and Nano Banana Pro (Nano Banana 2.1 and Seedream 5 Pro cannot be ticked); Quote | `2 picked (2 to 5)`; the quote is refused: `the graph is not saved: motion is locked until the upstream still passes gates and is approved` | **fail (G2)** |
| W3 | Spend, Settings, Enlarge | Spend: `quoted 0.080 USD`, `billed 0.00 USD`, 3 generations, 1 accepted, by week and engine; Settings: read-only config; Enlarge: the viewer fills the canvas (1279×652) | pass |
| W4 | History tab | `No runs yet.` until Refresh is pressed; then the four runs (re-mask 0.00 USD, two fake 0.04 USD with the B and Ship marks, the quoted Nano Banana Pro run). The panel does not follow run events | minor |

### G1 is closed

T109 stores the G-buffer edge masks and the ΔE00 map in the project CAS at gate time, lists them under the gate report's `overlays`, and `CompareViewer` hands them to the viewer. Row 9b shows both modes drawing on a real daemon, on a passing and on a failing take.

### Gap G2: a new take on an approved node blocks every graph save while a Motion node hangs off it

Repro: rows 1 to 13 and 17's control (the approved `edit-1` wired into a Motion), then any new take on `edit-1`; here the free Re-mask of W1. From then on every autosave `PUT /projects/smoke/graph` answers `422 VIDEO_LOCKED`; the status bar reads `save failed: motion is locked until the upstream still passes gates and is approved`, the canvas marks the `edit-1` to Motion edge locked, and every quote in the project fails first with `the graph is not saved: ...`. The node's picked output (`outputHash`) is still the accepted take.

Cause: `daemon/plate_daemon/routes/projects.py::_node_status` lets the node's latest live run (or its sibling group) decide approval. The re-mask run is newer and not accepted, so `edit-1` derives `approved: false`, and `canvas_rules.validate_graph` refuses the whole graph. That is decision 31 as written ("latest run of that node"), but its consequence was not planned: the artist can save nothing, on any node, until the new take is accepted or the Motion edge is deleted, and the message does not say which node. The same path follows a plain second Make on an approved Edit. The scripted smoke never makes a take after an accept, so it cannot see this.

Fix (needs a task; `routes/projects.py` was T59's): let the run whose output is the node's `outputHash` decide when it is one of the node's runs, else the latest; or keep the save and mark the locked edge instead of refusing the graph. Add a second-take-after-accept step to `scripts/smoke_e2e.py` either way.

### Corrections to the plan's smoke text (T53 spec)

| | the plan says | the code does | what the steps above use |
|---|---|---|---|
| C1 | `curl ... -d '{...}'` with no content type | `422 {"errors":["<body>: Input should be a valid dictionary ..."]}` | `-H "Content-Type: application/json"` |
| C2 | token in `/tmp/plate-smoke/token` | no token file; the token is in `<home>/config.json` (`PLATE_TOKEN_FILE` only when set) | `scripts/plate_config.py token` |
| C3 | register `fixtures/packets/room_a` for project `smoke` | the manifest names `plate-fixture`; posted by path, the packet is filed under an auto-created `plate-fixture` project and `smoke` lists nothing | stage a copy with `project_id: smoke` before the first post (what `scripts/smoke_e2e.py` does) |
| C4 | Edit with engine `nano-banana-2.1`, Quote, Make | Nano Banana 2.1 is disabled in the Make bar until its manifest has a vendor model id (T102, decision 51: `model id unknown until the P0 smoke test`); the first pass could still quote it | quote Nano Banana Pro for the D quote line, Make on `fake`; box-day item 16 fills the model id |
| C5 | `localhost` is allowed only under `PLATE_DEV=1` | `http://localhost:4610` is always allowed; `PLATE_DEV=1` adds only the two `:5173` origins (as `docs/ui.md` says) | the printed `127.0.0.1` URL |
| C6 | the Gate panel shows a pass report | `advisory`: decision 35 makes `feather_band.de00_p99` advisory on every in-mask edit until calibration | advisory accepted as the expected verdict |
| C7 | (first pass) the verdict wrote into the repo's `calib/labels.jsonl` | resolved by T110: labels live in `<PLATE_HOME>/calib/labels.jsonl`; the repo file is gone | nothing extra |
| C8 | `PLATE_HOME=/tmp/plate-smoke` | any throw-away folder works | `$HOME/plate-smoke-home` |
| C9 | candidates appear, then viewer and accept | clicking a candidate compares it; its digit key in the strip picks it as the node's output (what an Edit to Edit chain needs, decision 47); Export picks from its own list either way | row 13b |

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
| 7 | Backend shape: one daemon, WAL index in app data, CAS, separate GPU worker, UI served by the daemon | done | proven live in this pass (daemon, index under `PLATE_HOME`, built UI at `/`, in-process pixels backends); the Tailscale bind is off by default |
| 8 | LLM route: Anthropic API, Sonnet composer, Opus two-sample judge | done | T33 on recorded fixtures only; Anthropic is effective grade D until terms are filed, so NDA projects get no composer and no judge (claim `judge_cannot_overrule_numbers`) |
| 9 | Custody grades C0 to D, NDA on C0/C1 | done | T14; claims `nda_c0_c1_only`, `pending_terms_count_as_d`, `nda_egress` |
| 10 | Retroactive contract review | n/a (Aasish, counsel) | the code side is done: consent template (`docs/consent-template.md`) and custody per project |
| 11 | GPU and RAM admission by NVML, explicit unload, RAFT ≤ 960 px | deferred-to-box (T22) | admission, windows and slots built and tested (T22) |
| 12 | Host scope: 3ds Max 2026 only | done | schema `host.app: 3dsmax \| none`; no other sender |
| 13 | Engine roster, pins, retirement alerts, same-model fallback only | done; deferred-to-box (live smoke) | 14 manifests plus the fake (Veo 3.1 added as a start/end engine, T94), adapters on recorded fixtures (T17, T18), pins and alerts (T27), price tiers (T84), route health (T85); prices `unconfirmed`, 9 manifests flagged `unverified_p0`; Nano Banana 2.1 and Seedream 5 Pro cannot be picked in the UI until their model ids are known (C4) |
| 14 | One gate value per metric, resolution stated | done | values as corrected by plan decision 3 and decision 35, with `calibration_needed` where the plan says so; 4K gate 3.68 s on CPU, 4.19 s with the viewer overlays (T67, T109) |
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
| 29 | One consolidated budget | n/a (Aasish) | `plate spend` and the Spend tab (T88, T102; row W3) report it |
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
| Canvas: node kinds, viewer, enforced rules, project folder, rebuildable index | done; one open defect (G2) | everything drove in a real browser, edges and heatmap included; G2 blocks saving after a second take on an approved node with a Motion downstream |

### P0 deliverables and exits

| item | status |
|---|---|
| PACKET.md and schema | done (0.1.1) |
| on-box spike report | deferred-to-box (T9; `docs/onbox-spikes.md` rows all "not run") |
| conformance scene with the reprojection check | done for the Blender fixture (`fixtures/tests/test_packets_valid.py::test_marker_reprojection_rms_under_half_a_pixel_at_640_wide`, 8 markers); the Max scene is box work (open question 8). The plan's exit says 3840 wide; the fixture measures at 640 |
| provider matrix for ten engines | partial: manifests and `docs/custody.md` done; billed price to the cent needs `plate smoke --live` (≤ USD 5) |
| labelling UI and the first 100 labels | UI done (T23); labels are Aasish's (none yet). Labels now live in the data folder (T110), and the repo's audit/smoke lines are gone |
| baseline of two live jobs; day-1 emails | n/a (Aasish) |
| client-consent template | done |
| exits: real camera packet reprojects ≤ 1 px; markers ≤ 0.5 px RMS at 3840; TIFF16 vs OCIO; element overhead; billed price; Windows suite and CI green | deferred-to-box (T9, T11, T19); identity round trip at ΔE00 p99 ≤ 0.5 is done (T4) |

### P1 deliverables and exits

| item | status |
|---|---|
| gates package (register, comp_masked, labmatch, gbuffer edges, invention, depth agreement, maskdiff, texture, tone, crop pair, video) with goldens | done (601 gate tests, procedural and Blender goldens) |
| adapters for Nano Banana 2.1 and Pro, GPT Image 2.5, Seedream 5 Pro, Kling O3 4K and v3 Pro, Seedance 2.5 i2v and r2v, direct Gemini and OpenAI | done on fixtures; deferred-to-box (live smoke); Nano Banana 2.1 via Gemini, Magnific and Seedance r2v via BytePlus/WaveSpeed have no recorded call yet |
| ledger and daily reconciliation | done (T13) |
| `plate run / gate / export` with three Lensbook recipes | done; the Lensbook stills a01 to a10 now ship as runnable recipes and 12 motion recipes from the shot library (T83, T94) |
| advisory render-it router; calibration report | done (the report needs labels) |
| exits: AUC ≥ 0.85 on ≥ 100 takes; two live client jobs; ledger equals billing within 1% | deferred (labels and live jobs) |
| exit: gate run on a 4K still ≤ 10 s | done on CPU: `run_gates 3840x2160: wall 3.68s` |

### P2 deliverables and exits

| item | status |
|---|---|
| canvas, Make bar (provider, model, price, custody, ETA), N fan-out, enforced rules | done (this pass); cross-engine compare quote built (T95, T102) but blocked in this session by G2 |
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
| FCPXML, ProRes 4444, EXR delivery | done (T46, route T74, UI T106) |
| local Wan 2.2 VACE previs | deferred-to-box (stub; T22) |
| exits: a shipped 30 to 60 s film section; gates agree with ≥ 80% of ≥ 30 rejections; ≤ 2.5 generations per clip; zero accepts without a scrub | deferred (live work); the scrub rule is enforced (this pass, row 11) |

### P4 deliverables and exits

| item | status |
|---|---|
| Set node with per-material targets and the cross-view gate | done (T30, T48) |
| Light node over MaxGaffer with Vantage live-link preview | deferred-to-box (T37, T77) |
| LightMix NNLS | done (T32; presets and per-channel gains T89) |
| Recolour by recomposition, validated once against a V-Ray re-render | code done (T31, T48, several materials in one pass T90); validation deferred-to-box (T31) |
| Scout node; shared Max bridge | deferred-to-box (T37, T49) |
| client-revision carry-over; backup and purge | done (T41, T42, T96) |
| runbooks and the manual-pipeline fallback | done (T51, T80, T108: twelve runbooks) |
| exits: eight-camera villa set at cross-view p90 ≤ 3; a V-Ray dusk variant through the Light node; RAL panel ≤ 1 vs a re-render; ≥ 3 deliverables a week | deferred-to-box and live work |

## 6. Verifier pass: Wave 9 decisions 46 to 58 (VizMaker parity)

| # | decision | status | evidence |
|---|---|---|---|
| 46 | References are a node, never pixels of the output | done | T81, T82, T92: `test_canvas_rules_wave9`, `test_mask_tools_wave9`, `test_pipeline_wave9`; the Edit node's `references` port is on the canvas above |
| 47 | An Edit downstream of a take starts from that take (`inputs.base`), 422 `no_base` | done | T92 (`test_pipeline_wave9`); the base is the upstream node's picked `outputHash` (C9) |
| 48 | Comp, Colour, Atmosphere, Sky are numeric nodes | done | T93 (`test_numeric_nodes_wave9`, 30 tests) |
| 49 | Motion end still and elements; locked and pull-back crop pairs | done | T94 (`test_motion_wave9`); end-still camera check and VIDEO_LOCKED on either still |
| 50 | Recipes from shipped, studio and project folders; saved from the UI | done | T83, T97 (`test_recipes_wave9`); the Edit editor shows the recipe select, Start from this, Expand, Save as recipe |
| 51 | Engine guidance measured: cards, capability chips, route health, quote_many | done; G2 blocks the compare quote in the session above | T83, T85, T95, T102; chips seen in row 7 |
| 52 | Prices tiered by the params that change the bill | done | T84 (`test_price_tiers`, `test_quotes_tiers`); Nano Banana 2K/4K `estimated` |
| 53 | Batch is a template, not a canvas-wide Make; Send + quote | done; deferred-to-box (T104) | T95 (`test_templates_wave9`), T104 fake-tested |
| 54 | Recomposite is a free attempt | done | T99 (`test_recomposite_wave9`); row W1: 0.00 USD run |
| 55 | AI layers may be upscaled at ingress; plates never; Magnific moved | done (stub) | T100 (`test_layer_upscale`, `test_upscale_wave9`); Real-ESRGAN is a stub on the Mac |
| 56 | Social cutdowns are downsample-only crops with IPTC provenance | done | T86 (`test_export_wave9`, 61 tests) |
| 57 | Frames are annotations | done | T81 (`test_canvas_rules_wave9`) |
| 58 | Two-point perspective cameras become level-and-crop packets | deferred-to-box (T104) | `plugins/max/tests/test_camera_level.py` (27 tests); the V-Ray shift unit is a box measurement |

Doctrine held across Wave 9: no text-to-image path (generation is anchored to a plate camera; the Make bar refuses a node with no plate upstream), no plate upscaling (T100 upscales AI layers only), no invented angles (cutdowns are crops; references never composite).

## 7. Wave 9 open questions running on their defaults

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

## 8. Box-day checklist

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
16. P0: TIFF16 vs OCIO re-derivation ΔE00 p99 ≤ 0.5, or the mismatch understood and the master chosen (`docs/colour.md`).
17. P0: the conformance scene built in Max from `fixtures/blender/markers.json`; eight markers reproject within 0.5 px RMS at 3840 wide.
18. P0: `plate smoke --live` with a stated budget (≤ USD 5): billed price per engine to the cent, the vendor model ids behind every `unverified_p0` flag (this also enables Nano Banana 2.1 and Seedream 5 Pro in the Make bar), and a recorded minimum-cost call for Nano Banana 2.1 on the Gemini route.
19. P0: push the repo to a private remote so the `windows-latest` and `macos-latest` CI legs run once (open question 13).
20. P2: the same daemon driven from a browser on the box and on a second Windows machine (this pass covered the Mac); a fresh Windows machine from bootstrap to first candidate in under 15 minutes.

The first pass's item "delete `calib/labels.jsonl` before any calibration run" is done: the file is gone and T110 keeps labels out of the repo.

## 9. Other observations

- G2 (section 2) is the one open defect found; it needs a task before artists use Motion on a node they keep iterating.
- `claims.yaml`, claim `previs_is_not_deliverable`: the caveat says the artist manual still says previs can never reach Review. `docs/artist-manual.md` (the previs paragraph) already reads "can never reach Export or Delivery ... It may go into a Review bundle". The caveat is stale; the claim holds. `claims.yaml` belongs to T52/T80.
- CLI fixture smoke skipped 8 routes: Magnific and Nano Banana 2.1 via Gemini (no recorded call, no vendor model id: `unverified_p0`), Nano Banana 2.1 via Atlas (no adapter speaks the route), Seedance 2.5 r2v via BytePlus and WaveSpeed (no recording), and the three local stubs (Qwen, Real-ESRGAN, Wan 2.2). Seedream 5 Pro passes the fixture smoke yet is disabled in the Make bar: the UI goes by the manifest's model id, the smoke by the recording. Both clear with box-day item 18.
- The History tab does not reload on run events (row W4); Refresh shows the runs.
- An Edit card keeps saying `engine: pick in the Make bar` after its runs finish on `fake`; the Make bar does not write the engine into the node's params. Cosmetic.
- The full scrub on the 640×360 fixture plate in a small viewer is complete at the centre view at 100%: the four quadrant centres fall on the viewport edges, and the comparison is inclusive. That is correct by the rule's definition. A 4K plate needs real panning.
- With a fresh quote, Make approves directly as the human (`approved_by: human`); the approval dialog appears only when the daemon refuses (auto cap, envelope). This matches `docs/ui.md`.
- `room_a` and `room_a_ral3020` are 3.78 MB, over the 3 MB per-packet guideline (known since wave 2); the 25 MB total cap holds at 17 MB.
- `make test` stops at the first failing step, so a pytest failure hides the vitest result; run `npm test` on its own when that happens.
