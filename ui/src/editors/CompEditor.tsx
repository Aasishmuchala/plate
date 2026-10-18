// Comp node editor (T112 for the T93 node, decision 48): approved, mask-locked layers stacked in order over a base. The
// base is the node wired into `base` (a Plate, or nothing: the packet's display master); the layers are the nodes
// wired into `layers`. Up and down change the stacking order (the later layer wins where two overlap); the order is
// written as `params.order` (layer node ids) only once the artist moves one, else the wiring order stands. The list
// reads bottom to top: the first layer is stacked first, the last one shows over all the others. With
// `params.require_accept` (default on) a layer whose take carries no accept note is refused (409 LAYER_NOT_ACCEPTED);
// switching it off stacks finished takes that were never accepted. `Preview coverage` asks the daemon's numeric preview
// (`comp_node.preview`) how much of the frame each layer covers and how many pixels two layers claim.
// Node params: `order` (string[], wired layer ids only), `require_accept` (boolean).
//
// `wiredInto` mirrors `comp_node.wired`: the nodes wired into one input handle, in edge order, an edge without a
// target handle resolving as the canvas rules resolve it. The Colour and Sky editors use it too.

import { type ReactElement, useEffect, useMemo, useState } from "react";
import { postJson } from "../api";
import type { EdgeLike, GraphNode } from "../canvasRules";
import { checkHandles } from "../nodes/ports";
import { useGraphStore } from "../store";
import { errorMessage } from "./api";
import { asBool, asStringList, useNodeEditor } from "./params";
import { E } from "./theme";
import { Button, Chip, EditorFrame, Notice, Section, Toggle, useAction } from "./widgets";

/** The nodes wired into input `handle` of `nodeId`, in edge order (each once). */
export function wiredInto(nodes: readonly GraphNode[], edges: readonly EdgeLike[], nodeId: string, handle: string): GraphNode[] {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const target = byId.get(nodeId);
  if (!target) return [];
  const out: GraphNode[] = [];
  for (const e of edges) {
    if (e.target !== nodeId) continue;
    const source = byId.get(e.source);
    if (!source || out.includes(source)) continue;
    const v = checkHandles(source.data.kind, e.sourceHandle, target.data.kind, e.targetHandle);
    const port = v.ok ? v.targetHandle : e.targetHandle;
    if (port === handle) out.push(source);
  }
  return out;
}

/** The layers in stacking order: the ids `order` names (wired ones only), then the rest in wiring order. Pure. */
export function orderedLayers(layers: readonly GraphNode[], order: readonly string[]): GraphNode[] {
  const byId = new Map(layers.map((n) => [n.id, n]));
  const named = Array.from(new Set(order)).flatMap((id) => {
    const n = byId.get(id);
    return n ? [n] : [];
  });
  return [...named, ...layers.filter((n) => !named.includes(n))];
}

/** `order` with `index` moved by `delta` (one up is -1), as the full id list the daemon reads. Pure. */
export function moveLayer(ids: readonly string[], index: number, delta: number): string[] {
  const to = index + delta;
  if (index < 0 || index >= ids.length || to < 0 || to >= ids.length) return [...ids];
  const out = [...ids];
  const [id] = out.splice(index, 1);
  if (id !== undefined) out.splice(to, 0, id);
  return out;
}

interface CompCoverage {
  layers: { node: string; coverage: number | null }[];
  overlapPx: number | null;
  unionCoverage: number | null;
}

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

function parseCoverage(body: unknown): CompCoverage {
  const o = typeof body === "object" && body !== null ? (body as Record<string, unknown>) : {};
  const layers = Array.isArray(o.layers)
    ? o.layers.flatMap((l) => {
        const r = typeof l === "object" && l !== null ? (l as Record<string, unknown>) : null;
        return r && typeof r.node === "string" ? [{ node: r.node, coverage: num(r.coverage) }] : [];
      })
    : [];
  return { layers, overlapPx: num(o.overlap_px), unionCoverage: num(o.union_coverage) };
}

const pct = (v: number | null): string => (v === null ? "n/a" : `${(v * 100).toFixed(1)} %`);

export function CompEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const nodes = useGraphStore((s) => s.nodes);
  const edges = useGraphStore((s) => s.edges);
  const projectId = useGraphStore((s) => s.projectId);
  const [coverage, setCoverage] = useState<CompCoverage | null>(null);

  const base = useMemo(() => wiredInto(nodes, edges, nodeId, "base")[0] ?? null, [nodes, edges, nodeId]);
  const wiredLayers = useMemo(() => wiredInto(nodes, edges, nodeId, "layers"), [nodes, edges, nodeId]);
  const order = asStringList(params.order);
  const layers = orderedLayers(wiredLayers, order);
  const requireAccept = asBool(params.require_accept, true);

  // The daemon refuses an order that names a layer no longer wired (or names one twice): keep it to the wired ids.
  const wiredKey = wiredLayers.map((n) => n.id).join("\u0000");
  useEffect(() => {
    if (order.length === 0) return;
    const wired = new Set(wiredLayers.map((n) => n.id));
    const clean = Array.from(new Set(order)).filter((id) => wired.has(id));
    if (clean.length !== order.length) set({ order: clean.length > 0 ? clean : undefined });
    // follows the wiring and the stored order only
  }, [wiredKey, order.join("\u0000")]);

  const move = (index: number, delta: number): void => {
    set({ order: moveLayer(layers.map((n) => n.id), index, delta) });
    setCoverage(null);
  };

  const [measure, state] = useAction(async () => {
    if (!projectId) throw new Error("open a project to preview");
    const body: Record<string, unknown> = { require_accept: requireAccept };
    if (order.length > 0) body.order = layers.map((n) => n.id);
    setCoverage(parseCoverage(await postJson<unknown>(`/projects/${encodeURIComponent(projectId)}/nodes/${encodeURIComponent(nodeId)}/preview`, { what: "comp", params: body })));
  }, errorMessage);

  const coverageOf = (id: string): number | null => coverage?.layers.find((l) => l.node === id)?.coverage ?? null;

  return (
    <EditorFrame kind="comp" nodeId={nodeId} title="Comp">
      <Section title="Base" testId="comp-base">
        <span style={E.note} data-testid="comp-base-node">
          {base === null || base.data.kind === "Plate" ? `the plate${base ? ` (${base.data.label})` : ""}: the packet's display master` : `${base.data.label} (${base.id}): its picked take`}
        </span>
      </Section>

      <Section title="Layers (bottom to top)" testId="comp-layers">
        {layers.length === 0 ? (
          <span style={E.note} data-testid="comp-nolayers">
            no layers: wire accepted takes into the layers input
          </span>
        ) : (
          <ol style={{ margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 4 }} data-testid="comp-layer-list">
            {layers.map((n, i) => (
              <li key={n.id} data-testid="comp-layer" data-node={n.id}>
                <div style={E.row}>
                  <span>
                    {n.data.label} <span style={E.note}>({n.id})</span>
                  </span>
                  <Chip tone={n.data.approved ? "pass" : requireAccept ? "fail" : "advisory"} testId={`comp-layer-accepted-${n.id}`} title="a layer stacks its accepted take">
                    {n.data.approved ? "accepted" : "not accepted"}
                  </Chip>
                  {coverage ? (
                    <Chip tone="info" testId={`comp-layer-coverage-${n.id}`} title="share of the frame this layer's mask covers">
                      {pct(coverageOf(n.id))}
                    </Chip>
                  ) : null}
                  <Button onClick={() => move(i, -1)} disabled={i === 0} testId={`comp-layer-earlier-${n.id}`} title="stack it earlier: under the layer before it">
                    ↑
                  </Button>
                  <Button onClick={() => move(i, 1)} disabled={i === layers.length - 1} testId={`comp-layer-later-${n.id}`} title="stack it later: over the layer after it">
                    ↓
                  </Button>
                </div>
              </li>
            ))}
          </ol>
        )}
        <div style={E.row}>
          <Button onClick={() => set({ order: undefined })} disabled={order.length === 0} testId="comp-order-reset" title="stack the layers in the order they are wired">
            Wiring order
          </Button>
          <span style={E.note} data-testid="comp-order-source">
            {order.length > 0 ? "order: set here" : "order: as wired"}; the later layer wins where two overlap
          </span>
        </div>
      </Section>

      <Section title="Rules" testId="comp-rules">
        <Toggle
          label="layers must be accepted takes"
          checked={requireAccept}
          onChange={(v) => set({ require_accept: v })}
          testId="comp-require-accept"
          title="off: any finished take stacks (DONE, or a gate fail with a logged override)"
        />
        <div style={E.row}>
          <Button onClick={() => void measure()} disabled={!projectId || layers.length === 0 || state.pending} testId="comp-preview">
            {state.pending ? "Measuring" : "Preview coverage"}
          </Button>
          {coverage ? (
            <>
              <Chip tone="info" testId="comp-union" title="share of the frame any layer changes">
                union {pct(coverage.unionCoverage)}
              </Chip>
              <Chip tone={coverage.overlapPx ? "advisory" : "muted"} testId="comp-overlap" title="pixels two or more layers claim; the later layer shows there">
                overlap {coverage.overlapPx ?? "n/a"} px
              </Chip>
            </>
          ) : null}
        </div>
      </Section>
      {state.error ? (
        <Notice tone="error" testId="comp-error">
          {state.error}
        </Notice>
      ) : null}
    </EditorFrame>
  );
}
