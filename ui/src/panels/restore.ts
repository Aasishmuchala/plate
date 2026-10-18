// Drop-to-restore, action side. A dropped image becomes `POST /assets/resolve {path}`; a match with a run becomes
// a "Load N nodes" offer; accepting it puts the producing subgraph back on the canvas, selected, and selects the run.
//
// A browser does not hand a page the file system path of a dropped file. `pathFromDrop` takes what it can get
// (Electron's `File.path`, a file:// URL some apps put on the drag, a plain absolute path in the text flavour).
// When none of those is there the panel asks for the path instead of guessing one: the daemon resolves by path
// (T26 `resolve_drop`), so there is nothing else to send. The daemon route gap is recorded in the T36 report.
//
// T101: a History row opens in the viewer. A click compares the run's output (B) with its plate (A) and selects it,
// as a candidate click does; shift-click puts the run on side A only, for any pairing (the scrub stays unbound).

import { runLabel, type SideRef, useRunStore } from "../canvas/runStore";
import { useGraphStore } from "../store";
import { errorMessage, getRun, resolveAsset } from "./api";
import { shortId } from "./format";
import { planRestore, type Restore } from "./subgraph";
import { usePanelsStore } from "./store";
import type { ResolveResult, RunDetail, RunRow } from "./types";

const IMAGE_EXT = /\.(png|jpe?g|tiff?|exr|webp|bmp)$/i;

export const isImageFile = (file: { name: string; type: string }): boolean =>
  file.type.startsWith("image/") || IMAGE_EXT.test(file.name);

const ABSOLUTE = /^(\/|[A-Za-z]:[\\/]|\\\\)/;

/** A file:// URL -> a path (`file:///C:/a%20b.png` -> `C:/a b.png`, `file:///srv/a.png` -> `/srv/a.png`). */
export function pathFromFileUrl(url: string): string | null {
  if (!url.toLowerCase().startsWith("file://")) return null;
  try {
    const u = new URL(url);
    let p = decodeURIComponent(u.pathname);
    if (/^\/[A-Za-z]:/.test(p)) p = p.slice(1);
    if (u.hostname && u.hostname !== "localhost") p = `//${u.hostname}${p}`;
    return p;
  } catch {
    return null;
  }
}

export interface DropLike {
  getData(format: string): string;
}

/** The file system path of a dropped file when the browser exposes one; null otherwise. */
export function pathFromDrop(file: File, dt: DropLike | null | undefined): string | null {
  const direct = (file as File & { path?: unknown }).path;
  if (typeof direct === "string" && direct !== "") return direct;
  if (!dt) return null;
  for (const flavour of ["text/uri-list", "text/plain"]) {
    let raw = "";
    try {
      raw = dt.getData(flavour).split(/\r?\n/)[0]?.trim() ?? "";
    } catch {
      raw = "";
    }
    if (!raw || raw.startsWith("#")) continue;
    const fromUrl = pathFromFileUrl(raw);
    if (fromUrl) return fromUrl;
    if (ABSOLUTE.test(raw)) return raw;
  }
  return null;
}

/** Resolve a path with the daemon and set the drop state: a match (with the run and the restore plan) or an error. */
export async function resolveByPath(path: string, name: string = path): Promise<void> {
  const { setDrop } = usePanelsStore.getState();
  setDrop({ kind: "resolving", name });
  let result: ResolveResult;
  try {
    result = await resolveAsset(path);
  } catch (err) {
    setDrop({ kind: "error", message: errorMessage(err) });
    return;
  }
  let run: RunDetail | null = null;
  let plan: Restore | null = null;
  let note: string | undefined;
  if (result.run_id) {
    try {
      run = await getRun(result.run_id);
      const g = useGraphStore.getState();
      plan = planRestore(run, { nodes: g.nodes, edges: g.edges, wire: g.toGraph() });
    } catch (err) {
      note = `run ${shortId(result.run_id)} could not be read: ${errorMessage(err)}`;
    }
  }
  setDrop({ kind: "match", result, run, plan, note });
}

/** Handle a dropped image: resolve by path when there is one, else ask for it. */
export async function handleDroppedFile(file: File, dt: DropLike | null | undefined): Promise<void> {
  const path = pathFromDrop(file, dt);
  if (path === null) {
    const panels = usePanelsStore.getState();
    panels.setTab("history"); // the path box lives there
    panels.setDrop({ kind: "needs-path", name: file.name });
    return;
  }
  await resolveByPath(path, file.name);
}

/** Select a run's producing node on the canvas and make the run the selected candidate. */
export function focusRun(run: RunRow): void {
  const g = useGraphStore.getState();
  g.onNodesChange(g.nodes.map((n) => ({ type: "select" as const, id: n.id, selected: n.id === run.node_id })));
  usePanelsStore.getState().selectRun(run.id);
}

/** A run row as a viewer side: labelled `<node label> · <engine_id> · attempt n`, carrying its output hash. */
export function runSide(run: RunRow): SideRef {
  const extra = run as RunRow & { attempt?: unknown; outputs?: unknown };
  const nodeId = run.node_id ?? undefined;
  const node = nodeId ? useGraphStore.getState().nodes.find((n) => n.id === nodeId) : undefined;
  const outputs = Array.isArray(extra.outputs) ? extra.outputs.filter((o): o is string => typeof o === "string") : [];
  const outputHash = outputs[0] ?? run.result_key ?? undefined;
  const attempt = typeof extra.attempt === "number" ? extra.attempt : null;
  return {
    kind: "run",
    runId: run.id,
    ...(nodeId ? { nodeId } : {}),
    label: runLabel(node?.data.label ?? nodeId ?? `run ${shortId(run.id)}`, run.route?.engine_id, attempt),
    ...(outputHash ? { outputHash } : {}),
  };
}

/** History's row click: select the run and its node, and compare its output (B) with its plate (A). */
export function openRunInViewer(run: RunRow): void {
  focusRun(run);
  const runs = useRunStore.getState();
  runs.closeCompare();
  runs.setCompareSide("b", runSide(run)); // with nothing open, A becomes the plate of the run's node
}

/** History's shift-click: the run on side A, whatever B is. */
export function setRunAsA(run: RunRow): void {
  useRunStore.getState().setCompareSide("a", runSide(run));
}

/** Accept the "Load N nodes" offer. Returns the number of nodes now selected on the canvas. */
export function loadDropped(): number {
  const panels = usePanelsStore.getState();
  const drop = panels.drop;
  if (drop.kind !== "match" || !drop.run || !drop.plan) return 0;
  const { run, plan } = drop;
  const g = useGraphStore.getState();
  if (plan.added > 0) g.loadProject(plan.graph, g.projectId);
  const ids = new Set(plan.nodeIds);
  const after = useGraphStore.getState();
  after.onNodesChange(after.nodes.map((n) => ({ type: "select" as const, id: n.id, selected: ids.has(n.id) })));
  if (!panels.runs.some((r) => r.id === run.id)) usePanelsStore.setState({ runs: [run, ...panels.runs] });
  panels.selectRun(run.id);
  panels.setTab("history");
  panels.setDrop({ kind: "idle" });
  return plan.nodeIds.length;
}
