# UI: the PLATE canvas

`ui/` is a Vite + React 19 + TypeScript app: `@xyflow/react` draws the graph, `zustand` holds it, `vitest` (jsdom) tests it. It is the artist's canvas: typed nodes with the doctrine rules enforced as you wire them, a Make bar that quotes before it spends, a best-of-N candidate strip, a WebGL2 compare viewer, a node editor per kind, and panels for projects, history, gates, custody, the human-minute timer and the verdict keys. Ports (docs/P0_AUDIT.md, Reconciliation): in development the Vite dev server runs on 5173 and proxies `/api` to the daemon on 4610 (`PLATE_DEV=1` adds the two 5173 origins to the daemon's Origin allowlist; `127.0.0.1:4610` and `localhost:4610` are always allowed, with or without it); the built UI (`ui/dist`) is served by the daemon itself at `http://127.0.0.1:4610/`. The token arrives once in the launch URL's `#token=` fragment, is moved to `sessionStorage` and sent as `Authorization: Bearer` (its three states, `present`, `rejected` and `none`, are under "Token states" below). How to use it is the [artist manual](artist-manual.md).

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
| `src/nodeTypes.ts`, `src/canvasRules.ts`, `src/store.ts` | the 23 node kinds and the typed graph model, the pure rules (`canConnect`, `validateEdge`, `generationDepth`, `canAccept`, `applyOverride`), `useGraphStore` | `spec/canvas_rules_vectors.json` is shared with the daemon's `canvas_rules.py` |
| `src/PlateCanvas.tsx`, `src/App.tsx` | the canvas (status bar with the save-state, alert and Max dock chips, `+ node` palette, node list, toast), right-click menus, Tidy, the Enlarge key handling, and the slots for the editor, viewer and panels | |
| `src/nodes/` | one component per kind with the shared header (kind, tier, gate, approval, risk), typed handles (`image16`, `mask`, `packet`, `video`, `report`, `reference`) | `ports.ts`; `FrameNode.tsx` is the annotation box |
| `src/canvas/` | wiring checks, run store and SSE, the candidate strip (Set as A / B), the compare viewer slot and its Enlarge stage (`enlarge.css`), the canvas menus (`ContextMenu.tsx`), Tidy (`layout.ts`), the theme | |
| `src/makebar/` | engine select filtered by node and custody with engine-card numbers, the capability and route-health chips, Compare engines (`quote_many`), preset (not for Edit), N 1 to 6, region crop, quote with countdown and price tier, approval dialog, fallback engine list | |
| `src/viewer/` | the WebGL2 viewer: wipe, toggle, difference, edge overlay, ΔE00 heatmap, 100% tiles | [src/viewer/README.md](../ui/src/viewer/README.md) |
| `src/panels/` | Projects (with the Project Sheet and Apply template), History, Gate, Custody, Timer, Spend, Settings, verdict keys and the full-scrub gate, drop-to-restore | [src/panels/README.md](../ui/src/panels/README.md) |
| `src/editors/` | Plate, Mask, Edit (recipes, tokens, Save as recipe), Reference, CropPair, Motion, People, Set, LightMix, Recolour (RAL grid, swatches), Light, Scout, Export (clips, batch, cutdowns), Review, the Re-mask bar, the video player and VideoCompare | [src/editors/README.md](../ui/src/editors/README.md) |
| `src/api/` | the fetch client, token capture and the daemon heartbeat | |
| `src/test/` | `setup.ts` (jest-dom, RTL cleanup, the jsdom polyfills xyflow needs) and the canvas, panels, editors and viewer suites | |

## Canvas rules enforced in code

`canConnect(nodes, edges, sourceId, targetId)` returns `{ ok: true }` or `{ ok: false, reason }`. The store calls it for every new edge; a refusal is stored in `lastRefusal`, shown on the target node for four seconds and the edge is not added. The daemon re-checks every edge when the graph is saved (`PUT /api/v0/projects/{id}/graph`), so the browser check is UX and the server is the enforcement point. Both implement the same ordered checks and codes, and both run the shared vectors:

| code | message | doctrine (CLAUDE.md) |
|---|---|---|
| `UNKNOWN_NODE` | `unknown node` | |
| `CYCLE` | `cycle` | the graph stays a DAG so depth is defined |
| `REFERENCE_ONLY` | `a reference feeds an engine's reference input only; it is never composited` (an edge from a Reference may only target `refs`, `elements` or `sky`) | a reference is an input, never pixels |
| `ANNOTATION` | `a Frame is an annotation; it takes no edges` | |
| `CONCEPT_TO_DELIVERY` | `concept tier cannot reach Export` (or `Review`), directly or through other nodes | 1, geometry first |
| `NO_PLATE_ANCHOR` | `generation must be anchored to a plate camera (source-view-only)` | 2 |
| `DEPTH_EXCEEDED` | `generation depth > 2 from the plate`, for the target and everything downstream | 7 |
| `VIDEO_LOCKED` | `motion is locked until the upstream still passes gates and is approved` (or `crop pair`); it also holds for a still wired to a Motion's `end` input, and the message names the `end still` | 7 |
| `HIGH_RISK_MOVE` | `<risk> risk move is blocked until it is overridden with a reason` | 7 |
| `INVENTED_ANGLE` | `a camera override needs an explicit override and the label 'unseen geometry guessed'` | 2 |
| `SCRUB_REQUIRED` | `'<label>' cannot be accepted without a logged full scrub` | 7 |

Generating nodes (`Edit`, `People`, `Motion`, `CropPair`) each cost one generation of depth; `Reference`, `Frame`, `Comp`, `Colour`, `Atmosphere`, `Sky` and the other numeric nodes cost none, and a Reference alone never anchors an Edit (`NO_PLATE_ANCHOR`). The two new codes are checked right after `UNKNOWN_NODE` and `CYCLE`, in both languages. A `PREVIS_NOT_DELIVERABLE` rule (a previs output never reaches Export or Delivery; a Review may show it, watermarked) is part of both rule sets and their shared vectors.

## The graph is saved for you, and the server owns status

The store saves the graph by itself: 400 ms after any structural change (a node added or removed, a wire, a parameter), 1.5 s after a drag of nodes ends (a positions-only save), and always right before a quote. The status bar chip reads `saved HH:MM:SS`, `unsaved`, `saving…`, in red `save failed: <message>`, or `conflict: reload`; the tab title is `PLATE · <project>` with a trailing ` *` while there is something unsaved. A project with no open graph shows an empty-state card; no placeholder nodes are loaded. Every request carries `X-Plate-Client: ui`, which is how the daemon knows a person is at the keyboard: only a request with that header (or an approval made as a person) resets the unattended-spend clock, so a terminal, curl or an agent never does. What the server stores for each node's `gateStatus`, `approved`, `approvedBy` and `scrubLogId` is computed from the run history and the ledger, not taken from the browser; the answer to the save is applied back onto the nodes. A save the daemon refuses (a wire that breaks a canvas rule) marks that wire as refused and shows the reason.

### Save retry, revision and conflict

The daemon stores a `graph_revision` in `project.json`; every `GET /projects/{id}/graph`, every save answer and the `graph` in other answers carry it as `revision`. The store keeps the one it last loaded or saved and sends it as `revision` in the body of the next `PUT`.

| situation | what the store does |
|---|---|
| the save fails and might pass: a network error, a 5xx, 409 `graph_busy` (another save of the project was in progress for more than 5 s), 429 `too_many_saves` (more than four saves already waiting) | the chip stays red, `save failed: <message>`, and the save is retried after 1, 2, 4 and 8 seconds and then every 15, until it succeeds or you open another project. When the daemon heartbeat goes from down to up the store saves at once if `saveState` is not `saved` |
| 422, a wire that breaks a canvas rule | not retried; the wire is marked refused with the reason (the daemon's answer is the truth) |
| 409 `graph_conflict` (`current`, `sent`) | the daemon holds a newer revision than the one sent. `saveState` becomes `conflict`, the chip reads `conflict: reload`, and nothing is written. **Reload** fetches the stored graph and replaces the local one. The only local work kept is an Edit's prompt text, in `conflictLocal` and offered as **copy prompt**; every other local change is dropped |
| the `PUT` carries no `revision` (an older client) | accepted without a check; the daemon only enforces the protocol on clients that send one |
| the graph is over 4 MiB, or a node or edge has no string id | 422 `graph_too_large` or the shape errors (`UNKNOWN_NODE` for an id that is an object or array), never a 500; a request body over 8 MiB is refused 413 before it is read |

**Debounce ceiling.** The 400 ms debounce is capped at `SAVE_MAX_WAIT_MS = 2000`: typing without a pause still saves at least every two seconds.

**Pagehide.** `pagehide` and `visibilitychange` to hidden flush the pending `PUT` with `fetch(..., { keepalive: true })`, so closing a tab right after an edit does not lose it. While `saveState` is not `saved`, a `beforeunload` handler sets `event.returnValue`, so the browser asks before the tab closes or reloads.

**Another writer.** A `graph.saved` event on the project stream is not trusted for its content. When one arrives and this tab has nothing in flight, the store fetches the head of the graph (`GET /projects/{id}/graph`) and compares revisions: a higher one than the store's reloads the graph (a node added by another tab or by an agent appears), or becomes `conflict` when this tab has unsaved changes. The stream cursor passed on reconnect is the last SSE id exactly as received (`<epoch>.<seq>`; the epoch changes when the daemon rebuilds its index, and then replay starts from the beginning of the new epoch), never parsed as a number.

**Reopen after a reload.** The open project id is kept in `sessionStorage`; reloading the page opens the same project again instead of the empty-state card.

### Token states

The status bar chip has three token states. `token: present`: a token is in `sessionStorage`. `token: rejected`: the health check or any call answered 401, so the token this tab holds is not the daemon's (the chip no longer claims `present` just because something is stored). `token: missing`: the tab has no token, and a box appears to paste the `#token=` value into. An error from the daemon that is not JSON (a proxy's HTML page, a bare 502) is shown as `<status> <statusText> (non-JSON response)`, never as the body.

## Daemon routes the UI calls

Every route below is in the daemon (under `/api/v0`). Bearer token on all of them (`/events` also takes `?token=` because `EventSource` cannot set headers).

| route | used by | what it answers |
|---|---|---|
| `GET /engines[?project_id=]` | Make bar | manifest summaries: routes with provider, custody grade, status and `health` (`ok`, `degraded`, `down`, `suspended`), parameters, presets, price, legal sizes and `capabilities` (`largest_size`, `image_tail`, `reference_images_max`, `elements`, `price_unit`, `runnable`, `unverified`); `project_id` adds `allowed`. The daemon's own fake engine is listed when it runs on the fake |
| `GET /recipes[?project_id=&kind=]` | Edit and Motion editors, Apply template | the runnable recipes from the shipped, studio and project folders: id, title, kind, engine, shot class, mask classes, `origin`, `group`, `thumbnail`, `task`, the open `tokens` and their `overrides` |
| `GET /projects/{id}/packets[?status=all]` | Plate editor | the project's packets with plate hash and size, camera name, `supersedes` and `superseded_by` |
| `GET /packets/{id}/summary` | Plate and Mask editors | size, plate, camera, policy and the legend entries (`editable`, `locked`, `planar`) of an ingested packet |
| `GET /projects/{id}/runs[?node_id=&state=&limit=]` | History panel, Export and Review editors | run views, newest first |
| `POST /runs/{id}/gate/rescore` | Gate panel, Rescore | a new gate-report version; older versions, and a shipped one, are never rewritten |
| `POST /pixels/idpick`, `/pixels/segment`, `/pixels/ocr` | Mask, People and Recolour editors | the daemon forwards to the pixels worker (or calls the backends in-process under `PLATE_PIXELS_INPROC=1`) |
| `POST /assets?kind=mask&project_id=` | Mask editor, save a painted mask | stores an 8-bit grey PNG in the project's store and answers its hash |
| `GET /assets/{hash}[?fmt=]` | viewer, thumbnails | a plate by reference from its packet folder, or a stored take; Range requests work on raw files |
| `GET /projects/{id}/graph`, `PUT /projects/{id}/graph` | the store | the graph with its `revision`; the save answers the graph with server-derived node status and the new revision, 409 `graph_conflict` for a stale `revision`, 409 `graph_busy` and 429 `too_many_saves` under a save storm, 422 for a rule refusal or `graph_too_large` |
| `POST /projects/{id}/nodes` | none yet (API; the MCP `add_node`) | appends one node (and the edges from `connect_from`) under the project lock and answers `{node_id, revision, graph}`; no stale-graph overwrite is possible |
| `POST /runs/{id}/accept`, `/scrub` | verdict panel | the logged full scrub and the acceptance that needs it |
| `POST /runs/{id}/export` | Export editor | the exported file, its sidecar and format; a GATE_FAIL take needs an override reason |
| `POST /projects/{id}/review` | Review editor | the review zip: path, size, takes |
| `POST /projects/{id}/import` | none yet (API) | an external render (Higgsfield, Runway or a file by hand) imported as a run and gated |
| `POST /runs/{id}/deliver` | Export editor (clip takes), video player Deliver… | a video delivery format from a finished clip take |
| `POST /assets?kind=reference&project_id=` | Reference editor | stores an uploaded PNG or JPEG (at most 32 MiB) as an 8-bit RGB PNG with no metadata, a side over 4096 px shrunk (`scaled_from`), and lists it in the project's `references.json`; answers `{hash, width, height, kind, project_id, scaled_from}` |
| `GET /projects/{id}/references` | Reference editor | the project's uploaded references: hash, size, bytes, upload time, note |
| `POST /recipes` | Edit editor, Save as recipe | writes a schema-1 recipe to the studio or project folder (`scope`, `project_id`, `overwrite`, `recipe`); 409 `recipe_exists`, 422 for a bad document or an engine not in the manifests |
| `GET /recipes/{id}/assemble[?project_id=&tokens=]` | Edit and Motion editors | the prompt and negative prompt the recipe would send, with `unfilled` and `unfilled_tokens`, from the Project Sheet and the `tokens` overrides |
| `GET /engines/cards[?project_id=&task=&shot_class=]` | Make bar, Motion editor | the engine cards folded from the project's ledger (`n`, `passes`, `wilson_lower_95`, `mean_cost`, `steering`); no project answers `[]` |
| `GET /alerts[?since=&limit=]` | none yet (API; the status bar follows the event bus) | the lines of every alert file, newest first, each with a kind, a `level`, a message, the time and the engine or project it names |
| `POST /projects/{id}/export` | Export editor, Batch export | many takes through the single export's guards: `{exported, refused}`; `run_ids` or `accepted`, `out_dir`, `format`, `name_pattern` |
| `POST /runs/{id}/cutdown` | Export editor, Cutdown | a 9:16, 4:5, 1:1 or 16:9 downsample-only crop of an approved master, with its lineage, the IPTC AI marker and a `.cutdown.json`; `{path, sidecar, box}` |
| `GET /projects/{id}/settings`, `PUT /projects/{id}/settings` | Projects panel, Project Sheet | the 14 Project Sheet fields with their defaults, `default_engine`, `auto_carry_over` and the policy; the PUT replaces the keys it carries |
| `GET /projects/{id}/spend[?since=&by=]`, `GET /spend?all_projects=1` | Spend tab | the ledger folded into quoted, billed, accepted and cost per accepted, by engine, day or week |
| `GET /docs/`, `GET /docs/{name}.md` | Settings, Help | the repo's `docs/**.md` as Markdown, confined to the docs folder |
| `GET /templates[?project_id=]` | Projects panel, Apply template | the shipped and project graph templates: id, title, nodes per packet, `set` |
| `POST /projects/{id}/templates/apply` | Projects panel, Max dock Send + quote | lays one chain per packet, quotes every new Edit and optionally auto-approves under the cap: `{nodes_added, edits, runs, total_usd, refused, awaiting_approval, graph}` |
| `POST /projects/{id}/nodes/{node}/quote_many` | Make bar, Compare engines | quotes one node on 2 to 5 engines as one sibling group: `{sibling_group, runs, total_usd}`; all or nothing |
| `POST /packets/{id}/carry-over` | Plate editor | carries the approved layers of the packet this one supersedes (or `from_packet_id`) onto it: one `carried` or `dropped` result per layer |
| `GET /plugin/status` | the Max dock chip in the status bar | `{last_poll_utc, queued, claimed}` of the dock's job queue |
| `POST /runs/{id}/recomposite` | Re-mask bar | a free re-mask of a stored raw take (`feather_px`, `grow_px`, `mask_hash`): quoted at 0 USD, approved as a person, run, answered as `GET /runs/{id}` |
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

Two routes have no canvas control yet: importing an external render (`POST /projects/{id}/import`; `plate import` does it) and the alert list (`GET /alerts`; the status bar shows the latest alert from the event bus). The numeric nodes Comp, Colour, Atmosphere and Sky have a node and a Make bar entry but no parameter panel, so their values are set on the node's `params`. Everything else the UI calls is in the daemon. `docs/VERIFICATION.md` records which of these were exercised in a real browser.
