# Changelog

All notable changes to PLATE are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow the daemon's `__version__` and the Max plugin's `PLUGIN_VERSION`. The Shot Packet contract is versioned separately (`spec/PACKET.md`, section 14).

Entries describe what each build task delivers. Whether it holds on the render box is a separate question, answered in `docs/VERIFICATION.md` when that pass is written; nothing below has run inside 3ds Max or against a live vendor.

## [0.1.0] - Unreleased

### Added

Wave 0, the scaffold audit
- The day-1 scaffold audited against the plan and normalised to one layout (`docs/P0_AUDIT.md`): app-data folder, `config.json` with a per-install token, ports 4610 (daemon and built UI), 4611 (pixels), 4612 (labeller), 5173 (Vite, `--strictPort`).
- Shot Packet 0.1.1: `render.session: fixture` for repository fixtures, optional `beauty_exr.parts`, `PLATE_TOKEN_FILE` and the `#token=` launch URL.

Wave 1, foundations
- Blender-rendered fixture packets (`room_a`, `room_b`, `room_a_v2`, `room_a_ral3020`, `dolly_hero`, `dolly_12f`) and numpy synthetics, so no gate depends on client data.
- Pixels image IO and the OCIO colour core; egress and ingress transforms (overscan pad and unpad, region crop, ECC registration, identity round trip).
- Daemon core: token auth with Host and Origin allowlists, SQLite WAL index in app data, per-project content-addressed store, the project folder format, single-writer lockfile, SSE bus.
- Gates consolidated into `plate_gates` with versioned reports, thresholds as data (`thresholds.yaml`) and golden tests that always run.
- Fourteen engine manifests with custody grades, the router's custody-first order, and Atlas's quirks pinned as contract tests.
- A stateful fake `pymxs`, the plugin's standard-library floor, the version handshake and HTTP client, lint and the auto-tagger.
- UI skeleton (Vite, React 19, TypeScript, `@xyflow/react`, zustand) with the canvas-rule engine; Windows and Mac scripts (`bootstrap`, `test`, `start-daemon`, a logon task), the Windows-hazard scanner and the CI matrix.

Wave 2, the engine room
- Max bridge: camera extraction, render elements, EXR and TIFF16 scripted save, scene digest, Vantage export (all behind candidate property names awaiting the on-box checklist).
- The job state machine with QUOTED first, idempotency keys, crash resume, priority classes, budgets and envelopes; the hash-chained `runs.jsonl` ledger with daily reconciliation against billing fixtures.
- The egress guard: custody enforcement, host allowlist, outbound log, metadata stripping, opaque ids, leak scan.
- Gates: G-buffer structural edges with circle-of-confusion weighting and invention flags; depth agreement for Tier C, per-material ΔE00 by Cryptomatte, mask difference by ID, OCR text-lock masks; Blender-rendered goldens for stills, texture and video.
- Image engine adapters (Gemini, OpenAI Images, Atlas Seedream, FLUX 3, local Qwen stub, upscalers) and video adapters (Seedance 2.5, Kling O3 4K and v3 Pro, Veo 3.1, local Wan 2.2 stub, BytePlus and WaveSpeed fallbacks), all fixture-tested with output-depends-on-input checks.
- The Max dock (PySide6, offscreen-tested), startup script, `install.ps1` and `uninstall.ps1`; the client packet kit (a MAXScript exporter); the pixels worker (FastAPI, stub mode, NVML admission, licence registry and its CI check).
- Calibration: the three-key labelling tool with reason codes, AUC, threshold fitting and the calibration report.
- The WebGL2 viewer: wipe, toggle, difference, red-cyan edge overlay, ΔE00 heatmap, auto 100% tiles.

Wave 3, the pipeline
- Max actions: Send Packet, Batch Cameras (per-camera recipes), Receive, with the handshake and lint in the loop.
- The daemon pipeline and routes: packet ingest, projects and graph (canvas rules enforced server-side), quote, approve, run, gate wiring, assets, SSE events.
- Lineage embedded in PNG, TIFF, EXR and MP4 files, the perceptual-hash index and drop-to-resolve, gate-report versioning that never rewrites a shipped report.
- Routing policy, nightly price refresh, schema diff, engine cards with Wilson bounds, canaries and retirement alerts.
- The render-it router: detail resolvability, disocclusion budget, orbit angle and threshold crossings; the reprojection-drift gate with the frame-rate time map and camera-lock with flow backends; cross-view correspondences and the Set-consistency gate; recolour by element recomposition; LightMix by non-negative least squares.
- The Anthropic prompt composer and two-sample consensus judge behind a custody gate, with the NDA derivative policy; Lensbook recipes ported as data.
- The canvas (node kinds, typed handles, enforced rules, Make bar with quote and approval, N-candidate fan-out, run status over SSE) and the panels (history, drop-to-restore, projects, gate with crop tiles, custody, verdict keys with the full-scrub gate, human-minute timer).

Wave 4, motion, delivery and agents
- The `plate` command line: run, gate, export, reindex, gc, pack and smoke, with a gated `--live` path.
- Max: Send Motion, Light (a MaxGaffer wrapper) and Scout (MaxDirector poses), and Vantage live-link hooks.
- Tier C ingestion, client-kit ingestion, verdict, scrub, timer, resolve and outbound-log routes.
- The MCP server with eight tools and no approve, deliver, override or master export.
- Static client review bundles with watermarked proxies; client-revision invalidation and approved-layer carry-over; backup, purge and reindex tooling with vendor-side deletion tracking and budget alerts.
- Motion: the CropPair generator, the Move policy (Lensbook risk ladder plus the router verdict) and the Kling start/end job with the time map; Seedance 2.5 reference-to-video previs with a burned-in PREVIS watermark; the People node over STAFFAGE with packet-Z occlusion.
- Delivery: FCPXML 1.10, ProRes 4444, HEVC with colour tags, EXR sequence, and the AV1 re-tag for Vantage exports.
- Node editors for Mask, CropPair, Motion, People, Set, LightMix, Recolour, Light and Scout, and a video player with the time-map readout; numeric node execution for Set, Light, Recolour and LightMix; the engine-card and canary scheduler with the nightly jobs.

Wave 5, shared bridge and documentation
- The shared Max bridge: one dock host, a callback multiplexer and a PySide6 collision guard for MaxDirector, MaxGaffer and PLATE.
- Documentation: the artist manual, runbooks (daemon will not start, vendor down, key revoked, VRAM out of memory, schema change, Max update breaks the dock, manual-pipeline fallback, backup and restore, nightly jobs), the one-page client explainer, the colour skeleton with the on-box table, the on-box spike log, a docs index, a regenerated custody matrix, and refreshed pages for the pixels worker, the UI, the labeller, the Max plugin and the engines.

Waves 6 and 7 (claims-as-tests, licence and hazard CI gates, the full verification pass) add their entries when they land.

Wave 12 — stress-test fixes (decisions 71 to 82, from the nine-attacker stress test: api-fuzz, doctrine, money, crash, engine-faults, scale, ui, max-plugin, mcp-cli-imports)
- 71. Only the UI resets the unattended clock: `touch_human` runs only for a request with `X-Plate-Client: ui` or an approval by a person, so `plate timer`, curl and agents no longer count as attended.
- 72. A base is a take of its node: `base_input` takes only a DONE run's output (or a GATE_FAIL one with a recorded override), refuses the rest as `base_not_a_take` / `base_gate_fail`, counts lineage depth through the chain of base runs (`DEPTH_EXCEEDED`), and `prior_mask` is the base run's mask union only (the pixel-difference term is gone).
- 73. Ledger records are never dropped: `override` joins `quoted` and `approved` as fail-closed (503 `ledger_unavailable`), every other kind that cannot be appended goes to a `ledger_outbox` table and is drained in order, records carry `data.event_seq`, and a run counts as a generation when it has a provider job id or a fetched, gated or done record.
- 74. Exports claim their destination with `O_CREAT | O_EXCL`, so concurrent exports of one name have one winner; a byte-identical re-export completes the record (`repeat: true`); a project folder that cannot take the shipped mark refuses before any client file is written.
- 75. Runs execute on a bounded `RunExecutor` (`PLATE_RUN_WORKERS`, default 8, per-route `max_inflight`, a 30 s orphan watchdog); a run the daemon stopped on twice is failed `resume_limit`; a locked or busy SQLite is retried with backoff instead of abandoning the run.
- 76. Vendor faults are classified, never guessed: an ambiguous submit resolves by lookup, idempotent re-stage or FAILED `submit_state_unknown` with `billing_unknown` (never `refunded`); `staging_error` is not a provider failure; five poll transport errors are tolerated and a stale result URL is re-polled once; results are checked (non-empty, known magic, pixel cap, streamed under a byte cap and a deadline) before the store; cancel never waits on a mid-step run (202).
- 77. The graph has a revision: `graph_revision` in `project.json`, a stale `PUT` is 409 `graph_conflict`, `POST /projects/{id}/nodes` appends one node under the project lock, saves queue behind a bounded wait (429 `too_many_saves`), and the UI retries with backoff, reloads on another writer's save, flushes on pagehide and warns on unload.
- 78. The NDA scrub is an allowlist (ids, kinds, hashes, numbers, booleans and enum-valued params pass, every other string is withheld), NDA error details are withheld, `export_review` meets the same guard as `POST /review`, `run_node` lets the daemon number attempts and `add_node` uses the append endpoint.
- 79. Custody `unset` is server-owned: `check_custody` strips it from client input, and the fill-in branch of `POST /projects` applies only to a custody the server marked unset and is ledgered as a custody override.
- 80. The quote validates everything the run will need (priority, finite params, the adapter's `validate_params`, mask ids and size, attempt bound, envelope bounds); attempt numbering and run creation happen under a per-node lock; the daily and unattended caps sum the spend of every project index in the home.
- 81. Send preflight catches what the render would waste: `DUPLICATE_NODE_NAME`, `DUPLICATE_CAMERA_NAME`, `BAD_SHOT_SLUG`, `BAD_LENS` and `BAD_POSE` are lint failures before `step("render")`, a manifest-stage abort removes its orphan folder, manifests are written with `allow_nan=False`, and an unsluggable scene name or an unreadable sidecar blocks Send.
- 82. The stress scripts are an opt-in target: `make stress` / `just stress` / a `workflow_dispatch`-only workflow run `scripts/stress/run_all.py`, and they are never part of `ci`.
- Documentation corrected where the code never met the claim: the project lock is taken per write, not held while a project is open, and two daemons on one folder are unsupported until a lease lands (it used to read "only one machine writes a project at a time"); "export never overwrites" now holds under concurrency through the exclusive claim; the Max scene guard reads "when the dock can tell", adds the project comparison and warns when it cannot compare.

Wave 13 — every remaining stress finding (decisions 83 to 93, from the 39 low findings and the 8 items Wave 12 dropped)
- 83. A project is leased by one daemon at a time: `<project>/lease.json` (host, pid, daemon id, start, heartbeat refreshed every 20 s) is taken by the first daemon that writes, any write from another daemon is refused 409 `project_leased` while the heartbeat is under 90 s old (`PLATE_LEASE_TTL_S`), a stale lease is taken over with a warning, reads still work, and a second daemon leaves the first one's in-flight runs alone.
- 84. The render-it router's input is computed once, at ingest: the `packet_router` table in the project index holds `detail_resolvability`'s answer, the quote decodes the EXR only when the row is missing or its version is old, and a cold 4K quote is under 0.5 s.
- 85. A scripted crash exits the process on every execution path (`os._exit(70)` on the executor threads and the inline task, as on the dev `step` route), so the crash matrix measures real restarts.
- 86. Result format is recorded, not judged: `normalized.ingress` carries `raw_format`, `raw_bit_depth`, `raw_channels`, `alpha_dropped`, `alpha_transparent` and `lossy`, the gate report names a lossy or alpha-dropped result in its notes, and a result with non-finite pixels fails `normalize_error`.
- 87. An export in flight holds a marker (`.<name>.<tag>.lock`, an exclusive lock): the sweep removes a sibling whose marker is free whatever its age and keeps one whose marker is held, so a retry seconds after a `kill -9` cleans up; a sibling with no marker keeps the one-hour rule.
- 88. Inputs the daemon trusts are validated on load: a token file or `config.json` token must be one line of printable ASCII without spaces, 16 to 256 characters; a recipe path over the API must resolve inside the shipped, studio or project recipe folder; a node's `params` are at most 262144 bytes (422 `params_too_large`); and the newline suite pins every name validator.
- 89. Externally declared spend never blocks automation: an import's `cost_usd` is marked `external`, left out of the daily and unattended caps, reported as `external_today_usd`, and a declaration above the daily cap is accepted with a `warnings` line.
- 90. A metric that was not measured says so: `camera_lock` and `flicker` on a clip of fewer than two frames are `unscored` (no number, a note in the report), never a pass.
- 91. MCP mutating tools probe the outbound log first: `run_node`, `estimate_run` and `add_node` open `outbound.jsonl` for append before the daemon is called (`outbound_log_failed` with nothing done), and `--usd`, `--hours` and `--per-run-usd` of `--new-envelope` are finite, positive and bounded.
- 92. A render is never kept for a daemon failure: a transport failure after the render removes the manifest-less packet folder unless `keep_failed_packets`, and a scene name too long for a sidecar or a backup beside it stores them under app data and the temp folder with a visible note.
- 93. The viewer caches decoded images by asset (an LRU of 320 MiB and 4 entries, so the plate is fetched once per compare session), and candidate strips hydrate from run history when a project opens.
- Documentation corrected: the "unsupported until a lease lands" sentences in the artist manual and the daemon runbook now describe the lease (the 409 text, the 90 s lapse, reads still work), and the restore steps leave `lease.json` out (the backup tool copies it today).

### Changed

- Texture fine-detail ratio ships as an advisory band of 0.40 to 0.60 (`calibration_needed`) because movie-grade references measure 0.45 to 0.53 under the current formula; the plan's 0.35 to 0.45 was wrong for it. Lapvar 300 to 600 stays a hard check at native 4K.
- First and last frame landing for video ships at 12/255 and 16/255 with an aspect-correct crop (`calibration_needed`): accepted delivered clips measure 3.8 to 14.2.
- Image-only edge agreement and phase-correlation camera lock are advisory triage. The 0.90 edge-recall gate exists only against G-buffer edges (Tier A). Optical flow: RAFT on the box (hard), OpenCV DIS elsewhere (advisory); the report names the backend.
- Compositing: the feather lives inside the mask (4 to 12 px); the old outside-box blur is retired, so outside-mask ΔE00 is exactly 0 by construction.
- The Anthropic API route is treated as custody D until written terms are filed: NDA projects run with no Claude composer and no judge.
- BLAKE3 hashes for packets the Max plugin writes come from the loopback daemon (`POST /api/v0/hash`); the daemon rehashes on ingest.

### Security

- Per-install bearer token on every `/api/` route; Host and Origin allowlists; the daemon, the pixels worker and the labeller bind loopback only and refuse an unspecified address.
- Egress is PNG only with metadata stripped, opaque file names, a leak scan for project, packet, camera, layer and material names, and an fsync'd outbound log before every request.
- The licence registry blocks non-commercial weights in CI (Depth Anything V2 Base and Large, UniDepth, SuperPoint, FLUX dev, MatAnyone; RVM stays inside STAFFAGE).
- No live vendor call is made by tests or CI; `PLATE_LIVE=1` plus a stated budget is required for any.

### Known limits

- The Max plugin has run only against a fake `pymxs`; the on-box checklist is `plugins/max/ONBOX.md`.
- Vendor prices are placeholders (`unconfirmed`) and no cloud custody terms are held in writing, so every cloud route counts as grade D (`docs/custody.md`).
- The GPU worker runs in stub mode; no model weights are downloaded by the repository.
- The canvas has no packet picker for a Plate node and no Export button yet (see `docs/ui.md`).
