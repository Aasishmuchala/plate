# UI: the PLATE canvas (placeholder)

`ui/` is a Vite + React 19 + TypeScript app: `@xyflow/react` draws the graph, `zustand` holds it, `vitest` (jsdom) tests it. It is a scaffold that renders a placeholder canvas and enforces the canvas rules in code. It does not talk to the daemon yet. Ports (docs/P0_AUDIT.md, Reconciliation): in development the Vite dev server runs on 5173 and proxies `/api` to the daemon on 4610 (`PLATE_DEV=1` adds the 5173 origins to the daemon allowlist); the built UI (`ui/dist`) is served by the daemon itself at `http://127.0.0.1:4610/` (router plan T5).

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

- `src/nodeTypes.ts`: the 17 node types and `PlateNodeData`. There is no text-to-image node.
- `src/canvasRules.ts`: pure `generationDepth` and `canConnect`.
- `src/store.ts`: `useGraphStore` (nodes, edges, `addNode`, `onNodesChange`, `onEdgesChange`, `connect`, `lastRefusal`). Initial graph is Plate to Comp to Gate.
- `src/PlateCanvas.tsx`: `MakeBar`, the xyflow canvas, `NodeList` (shows the nodes and the last refusal).
- `src/main.tsx`: wraps `App` in `ReactFlowProvider` and imports the CSS (components import no CSS, so tests never load it).
- `src/test/setup.ts`: jest-dom, RTL cleanup, and the jsdom polyfills xyflow needs (`ResizeObserver`, `DOMMatrixReadOnly`, a 1280x720 `getBoundingClientRect`).

## Canvas rules enforced in code

`canConnect(nodes, edges, sourceId, targetId)` returns `{ ok: true }` or `{ ok: false, reason }`. The store calls it for every new edge; a refusal is stored in `lastRefusal` and the edge is not added. Checks run in this order:

| Rule | Reason string | Doctrine (CLAUDE.md) |
|---|---|---|
| No cycles (self loops included) | `cycle` | graph must stay a DAG so depth is defined |
| The concept tier never reaches `Export` or `Review`, directly or through other nodes (a node is concept tier if it, or anything feeding it, is a `Concept` node or has `tier: "concept"`) | `concept tier cannot reach Export` (or `Review`) | 1, geometry first: prompt-only images stay in the watermarked tier |
| A generating node (`Edit`, `People`, `Motion`, `CropPair`, `Recolour`) can only be fed from a node that has a `Plate` upstream | `generation must be anchored to a plate camera (source-view-only)` | 2, source-view-only: every generation is anchored to a packet's camera |
| Generation depth from the plate stays at most 2, for the target and everything downstream of it | `generation depth > 2 from the plate` | 7, canvas rules |
| A `Motion` target needs a source with `gatePassed` and `humanApproved` | `motion is locked until the upstream still passes gates and is approved` | 7, video stays locked until the still passed gates and a human approved it |

`generationDepth` counts generating nodes on the longest upstream path from a `Plate` node (the node itself included when it generates); a node with no plate upstream has depth 0 and cannot feed a generator.

## What is placeholder

- The Make bar is static text (`provider · model · price · custody · ETA`, "quote pending"). Real values come from the daemon quote (doctrine 8); nothing is priced here.
- `gatePassed` and `humanApproved` are plain booleans on the node data. Nothing sets them yet; the gate report and the approval click will.
- Not modelled yet: the invented-camera override and its `unseen geometry guessed` label (doctrine 2), "a take is accepted only after a logged full scrub" and the high-risk-move override (doctrine 7), the Delivery node (today `Export` and `Review` are the delivery nodes), custody filtering (doctrine 9).
- No node palette, no persistence, no node-specific components, no daemon calls.
