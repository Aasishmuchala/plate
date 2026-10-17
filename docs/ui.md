# UI: the PLATE canvas

`ui/` is a Vite + React 19 + TypeScript app: `@xyflow/react` draws the graph, `zustand` holds it, `vitest` (jsdom) tests it. It is the artist's canvas: typed nodes with the doctrine rules enforced as you wire them, a Make bar that quotes before it spends, a best-of-N candidate strip, a WebGL2 compare viewer, a node editor per kind, and panels for projects, history, gates, custody, the human-minute timer and the verdict keys. Ports (docs/P0_AUDIT.md, Reconciliation): in development the Vite dev server runs on 5173 and proxies `/api` to the daemon on 4610 (`PLATE_DEV=1` adds the two 5173 origins to the daemon's Origin allowlist; `127.0.0.1:4610` and `localhost:4610` are always allowed, with or without it); the built UI (`ui/dist`) is served by the daemon itself at `http://127.0.0.1:4610/`. The token arrives once in the launch URL's `#token=` fragment, is moved to `sessionStorage` and sent as `Authorization: Bearer`. How to use it is the [artist manual](artist-manual.md).

## Run it

Node 22 (`>=22.12`). From the repo root:

| Command | What it does |
|---|---|
| `npm ci` (or `npm install`) | Installs the `ui` workspace; the root `package-lock.json` is the single lockfile. |
| `npm run dev` | Vite on `http://127.0.0.1:5173` (loopback only, `--strictPort` so it never drifts off the allowlisted origin). |
| `npm test` | `vitest run` once, no watch (this is what CI and `make test-ui` run). |
| `npm run build` | `tsc -b && vite build` into `ui/dist/` (git-ignored). `scripts/bootstrap.sh` and `.ps1` run it as `npm run build -w ui` right after `npm ci`, and `scripts/start-daemon.sh` and `.ps1` run it when `ui/dist/index.html` is missing, so the daemon always has a UI to serve. |
| `npm run typecheck --workspace ui` | `tsc -b --noEmit`. |

All npm scripts are shell-neutral (node binaries only), so they run unchanged in `cmd`, PowerShell and zsh; `tests/test_portability.py` lints them.

Dependency note: versions are pinned exactly. `package-lock.json` was generated with npm 11 because npm 10.9 (what `actions/setup-node` ships for Node 22) crashes while resolving vitest 4.1+/5 peer sets (`Cannot read properties of null (reading 'edgesOut')`). `npm ci` and a no-op `npm install` work on npm 10 with that lockfile; if you add or bump a dependency, regenerate with `npx npm@11 install`.

## Layout

| folder | what | its own notes |
|---|---|---|
| `src/nodeTypes.ts`, `src/canvasRules.ts`, `src/store.ts` | the 19 node kinds and the typed graph model, the pure rules (`canConnect`, `validateEdge`, `generationDepth`, `canAccept`, `applyOverride`), `useGraphStore` | `spec/canvas_rules_vectors.json` is shared with the daemon's `canvas_rules.py` |
| `src/PlateCanvas.tsx`, `src/App.tsx` | the canvas (status bar, `+ node` palette, node list, toast) and the slots for the editor, viewer and panels | |
| `src/nodes/` | one component per kind with the shared header (kind, tier, gate, approval, risk), typed handles (`image16`, `mask`, `packet`, `video`, `report`) | `ports.ts` |
| `src/canvas/` | wiring checks, run store and SSE, the candidate strip, the compare viewer slot, the theme | |
| `src/makebar/` | engine select filtered by node and custody, preset, N 1 to 6, region crop, quote with countdown, approval dialog, fallback engine list | |
| `src/viewer/` | the WebGL2 viewer: wipe, toggle, difference, edge overlay, ΔE00 heatmap, 100% tiles | [src/viewer/README.md](../ui/src/viewer/README.md) |
| `src/panels/` | Projects, History, Gate, Custody, Timer, verdict keys and the full-scrub gate, drop-to-restore | [src/panels/README.md](../ui/src/panels/README.md) |
| `src/editors/` | Mask, CropPair, Motion, People, Set, LightMix, Recolour, Light, Scout, and the video player | [src/editors/README.md](../ui/src/editors/README.md) |
| `src/api/` | the fetch client, token capture and the daemon heartbeat | |
| `src/test/` | `setup.ts` (jest-dom, RTL cleanup, the jsdom polyfills xyflow needs) and the canvas, panels, editors and viewer suites | |

## Canvas rules enforced in code

`canConnect(nodes, edges, sourceId, targetId)` returns `{ ok: true }` or `{ ok: false, reason }`. The store calls it for every new edge; a refusal is stored in `lastRefusal`, shown on the target node for four seconds and the edge is not added. The daemon re-checks every edge when the graph is saved (`PUT /api/v0/projects/{id}/graph`), so the browser check is UX and the server is the enforcement point. Both implement the same ordered checks and codes, and both run the shared vectors:

| code | message | doctrine (CLAUDE.md) |
|---|---|---|
| `UNKNOWN_NODE` | `unknown node` | |
| `CYCLE` | `cycle` | the graph stays a DAG so depth is defined |
| `CONCEPT_TO_DELIVERY` | `concept tier cannot reach Export` (or `Review`), directly or through other nodes | 1, geometry first |
| `NO_PLATE_ANCHOR` | `generation must be anchored to a plate camera (source-view-only)` | 2 |
| `DEPTH_EXCEEDED` | `generation depth > 2 from the plate`, for the target and everything downstream | 7 |
| `VIDEO_LOCKED` | `motion is locked until the upstream still passes gates and is approved` (or `crop pair`) | 7 |
| `HIGH_RISK_MOVE` | `<risk> risk move is blocked until it is overridden with a reason` | 7 |
| `INVENTED_ANGLE` | `a camera override needs an explicit override and the label 'unseen geometry guessed'` | 2 |
| `SCRUB_REQUIRED` | `'<label>' cannot be accepted without a logged full scrub` | 7 |

Generating nodes (`Edit`, `People`, `Motion`, `CropPair`) each cost one generation of depth. A `PREVIS_NOT_DELIVERABLE` rule (a previs output never reaches Export or Delivery; a Review may show it, watermarked) is part of both rule sets and their shared vectors.

## The graph is saved for you, and the server owns status

The store saves the graph by itself: 400 ms after any structural change (a node added or removed, a wire, a parameter; not a move), and always right before a quote. Every request carries `X-Plate-Client: ui`, which is how the daemon knows a person is at the keyboard. What the server stores for each node's `gateStatus`, `approved`, `approvedBy` and `scrubLogId` is computed from the run history and the ledger, not taken from the browser; the answer to the save is applied back onto the nodes. A save the daemon refuses (a wire that breaks a canvas rule) marks that wire as refused and shows the reason.

## Daemon routes the UI calls

Every route below is in the daemon (under `/api/v0`). Bearer token on all of them (`/events` also takes `?token=` because `EventSource` cannot set headers).

| route | used by | what it answers |
|---|---|---|
| `GET /engines[?project_id=]` | Make bar | manifest summaries: routes with provider, custody grade and status, parameters, presets, price, legal sizes; `project_id` adds `allowed`. The daemon's own fake engine is listed when it runs on the fake |
| `GET /recipes` | Edit editor | the runnable recipes (id, title, kind, engine, shot class, mask classes) |
| `GET /projects/{id}/packets[?status=all]` | Plate editor | the project's packets with plate hash and size, camera name, `supersedes` and `superseded_by` |
| `GET /packets/{id}/summary` | Plate and Mask editors | size, plate, camera, policy and the legend entries (`editable`, `locked`, `planar`) of an ingested packet |
| `GET /projects/{id}/runs[?node_id=&state=&limit=]` | History panel, Export and Review editors | run views, newest first |
| `POST /runs/{id}/gate/rescore` | Gate panel, Rescore | a new gate-report version; older versions, and a shipped one, are never rewritten |
| `POST /pixels/idpick`, `/pixels/segment`, `/pixels/ocr` | Mask, People and Recolour editors | the daemon forwards to the pixels worker (or calls the backends in-process under `PLATE_PIXELS_INPROC=1`) |
| `POST /assets?kind=mask&project_id=` | Mask editor, save a painted mask | stores an 8-bit grey PNG in the project's store and answers its hash |
| `GET /assets/{hash}[?fmt=]` | viewer, thumbnails | a plate by reference from its packet folder, or a stored take; Range requests work on raw files |
| `POST /runs/{id}/accept`, `/scrub` | verdict panel | the logged full scrub and the acceptance that needs it |
| `POST /runs/{id}/export` | Export editor | the exported file, its sidecar and format; a GATE_FAIL take needs an override reason |
| `POST /projects/{id}/review` | Review editor | the review zip: path, size, takes |
| `POST /projects/{id}/import` | none yet (API) | an external render (Higgsfield, Runway or a file by hand) imported as a run and gated |
| `POST /runs/{id}/deliver` | none yet (API) | a video delivery format from a finished clip take |
| `POST /projects/{id}/tierc`, `/kit` | Projects panel, Import client plate | a Tier C plate (consent record required) or a client kit, ingested as a packet |
| `PUT /projects/{id}/custody` | Projects panel, Custody row | the project's NDA flag and allowed grades; loosening needs a reason |

## Tests and the browser

`npm test` runs the vitest suites in jsdom: no daemon, no GPU, `fetch` and `EventSource` stubbed. The viewer additionally has a real-GPU smoke page (`/src/viewer/smoke.html` under the dev server). The canvas has not yet been driven by hand in a real browser against a real daemon; the smoke steps below, and the T53 pass in `docs/VERIFICATION.md`, are the first time that happens.

### Smoke steps against a real daemon

Use a throw-away app-data folder outside the repo, the fake engine and the in-process pixels backends, so nothing is spent and no worker is needed. From the repo root (Mac shown; on Windows set the variables with `$env:NAME = 'value'`):

```
PLATE_HOME="$HOME/plate-smoke-home" PLATE_FAKE_ENGINE=1 PLATE_PIXELS_INPROC=1 PLATE_NIGHTLY=0 scripts/start-daemon.sh
```

1. Open the URL the daemon prints (`http://127.0.0.1:4610/#token=...`; the `localhost` spelling on the same port works too, but it is a different browser origin and needs the full URL once).
2. For curl, read the token with the same `PLATE_HOME`: `TOKEN=$(PLATE_HOME="$HOME/plate-smoke-home" .venv/bin/python scripts/plate_config.py token)`. The token is in `config.json`; there is no separate token file.
3. Create the project `smoke` (not NDA) in the Projects tab, then register a packet: `curl -X POST http://127.0.0.1:4610/api/v0/packets -H "Authorization: Bearer $TOKEN" -H "X-Plate-Packet-Version: 0.1" -H "Content-Type: application/json" -d '{"packet_path": "<repo>/fixtures/packets/room_a", "sender": {"name": "curl", "version": "0"}}'` answers 202. The packet shows up in the Plate editor's list once ingested.
4. On the canvas: Plate (link `room_a` in the Plate editor) then Mask (pick `VEG_shrub_A`) then Edit (engine `fake`, type a prompt, Quote shows price, custody grade and ETA, Make) then candidates, viewer, Gate panel, full scrub, **Accept take**, Export (`png16` into a folder you name), Review (**Build bundle**).
5. Rule checks: Concept wired to Export shows the rule message; Motion before the still is approved shows the locked message.

## Routes without a canvas control

Two routes have no canvas control yet: importing an external render (`POST /projects/{id}/import`) and the video delivery formats (`POST /runs/{id}/deliver`). Everything else the UI calls is in the daemon. `docs/VERIFICATION.md` records which of these were exercised in a real browser.
