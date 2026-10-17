# Panels (T36)

History, project browser, gate, custody, verdict keys with the full-scrub gate, and the human-minute timer. State shared
between them is in `store.ts`; the graph stays in `useGraphStore`. Everything talks to the daemon through
`api.ts` (`fetchJson` with the bearer token). Tests: `ui/src/test/panels.test.tsx` (fetch stub, no daemon).

## Routes the panels call

| call | used by | spec |
|---|---|---|
| `GET /projects`, `POST /projects {project_id, custody:{nda, allowed_grades}, engine_pins}` | project browser, custody | T25 |
| `GET /projects/{id}/graph` (graph or `{graph}`) | open project | T25 |
| `GET /projects/{id}/runs` (array or `{runs}`) | history, custody | T36 spec |
| `GET /runs/{id}` (a `Run`, plus optional `subgraph {nodes, edges}`) | drop-to-restore | T25 |
| `GET /runs/{id}/gate[?n=]` (report, or `{report, n, shipped, versions}`; 404 = none yet) | gate panel | T25 |
| `POST /runs/{id}/gate/rescore` | "Rescore" | **not in any task spec**: add it (T40 or T25 follow-up) |
| `GET /projects/{id}/outbound?tail=50` | custody | T38 |
| `POST /runs/{id}/scrub {quadrants_visited, zoom_100}` -> `{scrub_log_id}` | "Full scrub" box | T38 |
| `POST /runs/{id}/verdict {verdict, reasons, scrub_log_id, rater}` | keys 1/2/3 | T38 |
| `POST /timer {deliverable_id, action}`, `GET /timer/today` | timer | T38 |
| `POST /assets/resolve {path}` -> `{by, content_hash, run_id?, distance?}` | drop-to-restore | T38 |

A browser does not expose the file system path of a dropped file, and the resolve route takes a path. The drop
reads `File.path` (Electron), a `file://` URL or an absolute path on the drag; otherwise the panel asks for the path.
A route that accepts the dropped bytes would remove that step.

## Wiring other tasks must do

- Mount `<ConnectedViewer a={...} b={...} />` (exported from `panels`) wherever a candidate is compared with its
  plate. It turns the viewer's `onChange` into the scrub progress the verdict panel waits for and carries the
  gate panel's "Open tiles" request into the viewer. A bare `<Viewer />` never enables the "Full scrub" box.
- Call `usePanelsStore.getState().selectRun(runId)` when a candidate is picked on the canvas (the candidate strip).
- A widget that owns the digit keys while focused (the viewer does; a `1..6` candidate strip would) marks itself
  `data-plate-keys`; plain `1/2/3` verdict keys are ignored inside it. `Shift+1/2/3` always works.
- To accept a take on the canvas, pass `scrubLogFor(runId)` to `useGraphStore.setNodeStatus(id, {approved: true}, scrub)`.

## Manual smoke (T53, in a browser, daemon up)

1. Projects tab: create `smoke` (NDA on: C2 and D are locked), open it; History lists runs newest first.
2. Click a run: its node is selected on the canvas, Gate shows the checks, failures first.
3. Viewer: press `1` (100%), pan with Shift+arrows through all four quadrants; the box enables; tick it.
4. Press `Shift+1` in the viewer or `1` outside it: Ship is recorded; `2`/`3` open the reason picker (Enter/Esc).
5. Drop a rendered PNG on the window: "Load N nodes" selects the producing subgraph.
6. Timer tab: type a deliverable, focus the panel, Space starts and stops; today's minutes update.
