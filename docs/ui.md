# UI: the PLATE canvas

`ui/` is a Vite + React 19 + TypeScript app: `@xyflow/react` draws the graph, `zustand` holds it, `vitest` (jsdom) tests it. It is the artist's canvas: typed nodes with the doctrine rules enforced as you wire them, a Make bar that quotes before it spends, a best-of-N candidate strip, a WebGL2 compare viewer, a node editor per kind, and panels for projects, history, gates, custody, the human-minute timer and the verdict keys. Ports (docs/P0_AUDIT.md, Reconciliation): in development the Vite dev server runs on 5173 and proxies `/api` to the daemon on 4610 (`PLATE_DEV=1` adds the 5173 origins to the daemon allowlist); the built UI (`ui/dist`) is served by the daemon itself at `http://127.0.0.1:4610/`. The token arrives once in the launch URL's `#token=` fragment, is moved to `sessionStorage` and sent as `Authorization: Bearer`. How to use it is the [artist manual](artist-manual.md).

## Run it

Node 22 (`>=22.12`). From the repo root:

| Command | What it does |
|---|---|
| `npm ci` (or `npm install`) | Installs the `ui` workspace; the root `package-lock.json` is the single lockfile. |
| `npm run dev` | Vite on `http://127.0.0.1:5173` (loopback only, `--strictPort` so it never drifts off the allowlisted origin). |
| `npm test` | `vitest run` once, no watch (this is what CI and `make test-ui` run). |
| `npm run build` | `tsc -b && vite build` into `ui/dist/` (git-ignored). |
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

Generating nodes (`Edit`, `People`, `Motion`, `CropPair`) each cost one generation of depth. A `PREVIS_NOT_DELIVERABLE` rule (previs output to Export, Review or Delivery) is added by the previs task to both rule sets and their shared vectors.

## Tests and the browser

`npm test` runs the vitest suites in jsdom: no daemon, no GPU, `fetch` and `EventSource` stubbed. The viewer additionally has a real-GPU smoke page (`/src/viewer/smoke.html` under the dev server). The canvas has not yet been driven by hand in a real browser against a real daemon; the T53 smoke steps in `docs/VERIFICATION.md` are the first time that happens.

## Gaps between the UI and the daemon (as of this writing)

The UI calls some routes that no daemon task owns, and shows a fallback or a refusal when they are missing:

| the UI calls | used by | what happens today |
|---|---|---|
| `GET /api/v0/engines` | Make bar | falls back to the built-in engine list in `makebar/engines.ts` (a test keeps it equal to the manifests); no prices or parameters, the quote still comes from the daemon |
| `GET /api/v0/projects/{id}/runs` | History panel | the list stays empty |
| `POST /api/v0/runs/{id}/gate/rescore` | Gate panel, Rescore | the button reports the refusal |
| `POST /api/v0/pixels/idpick`, `/pixels/segment`, `/pixels/ocr` | Mask, People and Recolour editors | the pick reports the refusal; the pixels worker has the segment and OCR routes (`docs/pixels.md`) but the daemon does not proxy them |
| `POST /api/v0/assets?kind=mask` | Mask editor, save a painted mask | the save reports the refusal |

Two things are missing from the canvas itself: a packet picker that binds a Plate node to a registered packet (a node holds `packetId`; the canvas never sets it), and an export action on the Export node (export goes through `plate export` or `POST /api/v0/runs/{id}/export`). The full verification pass (`docs/VERIFICATION.md`, when written) records which of these have been closed.
