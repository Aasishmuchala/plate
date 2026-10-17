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
