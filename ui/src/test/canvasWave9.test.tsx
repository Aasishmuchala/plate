// Wave 9 canvas contract (T81): the Reference, Frame, Atmosphere and Sky kinds; the refs / end / elements / layers /
// mask / clip / sky ports; the REFERENCE_ONLY and ANNOTATION rules; VIDEO_LOCKED on the Motion end still; the Frame
// annotation box; the Edit card's prompt caption; the make modes of the new and the newly numeric kinds. The shared
// vectors (spec/canvas_rules_vectors.json) run in canvasRules.vectors.test.ts; this file checks what they cannot
// (handle-aware messages, rendering). `fetch` is a 404 stub; no daemon runs.

import { ReactFlowProvider } from "@xyflow/react";
import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type EdgeLike, GENERATING_KINDS, type GraphNode, generationDepth, REFERENCE_HANDLES, validateEdge, validateGraph } from "../canvasRules";
import { MAKE_MODES } from "../makebar/engines";
import { HANDLE_TYPES, makeNodeData, makePlateNodeData, NODE_KINDS, type NodeKind, type PlateNodeData } from "../nodeTypes";
import { FRAME_DEFAULT_HEIGHT, FRAME_DEFAULT_WIDTH } from "../nodes/FrameNode";
import { KIND_SUMMARY, PROMPT_CAPTION_CHARS } from "../nodes/kinds";
import { checkHandles, edgeType, PORTS } from "../nodes/ports";
import PlateCanvas from "../PlateCanvas";
import { createInitialGraph, useGraphStore } from "../store";

const node = (id: string, kind: NodeKind, extra: Partial<PlateNodeData> = {}): GraphNode => ({
  id,
  position: { x: 0, y: 0 },
  data: kind === "Plate" ? makePlateNodeData("pkt_a", "A", extra) : makeNodeData(kind, extra),
});

const edge = (source: string, target: string, sourceHandle: string | null = null, targetHandle: string | null = null): EdgeLike => ({
  id: `${source}->${target}`,
  source,
  target,
  sourceHandle,
  targetHandle,
});

const approved: Partial<PlateNodeData> = { gateStatus: "pass", approved: true };

function stubFetch(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: false, status: 404, text: async () => '{"error":"not_found"}' }) as unknown as Response),
  );
}

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------- vocabulary

describe("the Wave 9 vocabulary", () => {
  it("appends Reference, Frame, Atmosphere and Sky to NODE_KINDS and reference to HANDLE_TYPES", () => {
    expect(NODE_KINDS.slice(-4)).toEqual(["Reference", "Frame", "Atmosphere", "Sky"]);
    expect(NODE_KINDS.slice(0, 19)).toContain("Concept");
    expect(HANDLE_TYPES).toContain("reference");
    for (const k of ["Reference", "Frame", "Atmosphere", "Sky"] as const) expect(GENERATING_KINDS.has(k)).toBe(false);
    expect([...GENERATING_KINDS].sort()).toEqual(["CropPair", "Edit", "Motion", "People"]);
    expect([...REFERENCE_HANDLES].sort()).toEqual(["elements", "refs", "sky"]);
  });

  it("declares the new ports exactly", () => {
    const ids = (k: NodeKind, side: "inputs" | "outputs") => PORTS[k][side].map((p) => `${p.id}:${p.type}`);
    expect(ids("Reference", "inputs")).toEqual([]);
    expect(ids("Reference", "outputs")).toEqual(["ref:reference"]);
    expect(ids("Frame", "inputs")).toEqual([]);
    expect(ids("Frame", "outputs")).toEqual([]);
    expect(ids("Edit", "inputs")).toEqual(["image:image16", "mask:mask", "refs:reference"]);
    expect(ids("People", "inputs")).toEqual(["image:image16", "packet:packet", "refs:reference", "clip:video"]);
    expect(ids("Motion", "inputs")).toEqual(["image:image16", "end:image16", "packet:packet", "elements:reference"]);
    expect(ids("Comp", "inputs")).toEqual(["base:image16", "layers:image16"]);
    expect(ids("Colour", "inputs")).toEqual(["image:image16", "mask:mask"]);
    expect(ids("Atmosphere", "inputs")).toEqual(["packet:packet"]);
    expect(ids("Atmosphere", "outputs")).toEqual(["image:image16"]);
    expect(ids("Sky", "inputs")).toEqual(["packet:packet", "sky:reference"]);
    expect(ids("Sky", "outputs")).toEqual(["image:image16"]);
    const label = (k: NodeKind, id: string) => PORTS[k].inputs.find((p) => p.id === id)?.label;
    expect(label("Edit", "refs")).toBe("references");
    expect(label("Motion", "end")).toBe("end still");
    expect(label("Motion", "image")).toBe("start");
    expect(label("People", "clip")).toBe("base clip");
    expect(label("Comp", "layers")).toBe("layers");
  });

  it("gives every new kind a make mode; Comp and Colour are numeric now", () => {
    expect(MAKE_MODES.Reference).toEqual({ mode: "none" });
    expect(MAKE_MODES.Frame).toEqual({ mode: "none" });
    expect(MAKE_MODES.Atmosphere).toEqual({ mode: "inprocess" });
    expect(MAKE_MODES.Sky).toEqual({ mode: "inprocess" });
    expect(MAKE_MODES.Comp).toEqual({ mode: "inprocess" });
    expect(MAKE_MODES.Colour).toEqual({ mode: "inprocess" });
  });
});

// ---------------------------------------------------------------------------- checkHandles

describe("checkHandles on the new ports", () => {
  it("a reference lands on refs, elements or sky, by name or by type", () => {
    for (const [kind, port] of [
      ["Edit", "refs"],
      ["People", "refs"],
      ["Motion", "elements"],
      ["Sky", "sky"],
    ] as const) {
      expect(checkHandles("Reference", "ref", kind, port)).toEqual({ ok: true, type: "reference", sourceHandle: "ref", targetHandle: port });
      expect(checkHandles("Reference", null, kind, null)).toMatchObject({ ok: true, type: "reference", targetHandle: port });
    }
    expect(edgeType("Reference", null)).toBe("reference");
  });

  it("a reference is refused by every compositing or delivery input", () => {
    expect(checkHandles("Reference", "ref", "Edit", "image")).toEqual({
      ok: false,
      code: "HANDLE_TYPE",
      message: "Edit image takes image16, not reference",
    });
    expect(checkHandles("Reference", null, "Comp", "base")).toMatchObject({ ok: false, code: "HANDLE_TYPE" });
    expect(checkHandles("Reference", null, "Comp", null)).toMatchObject({ ok: false, code: "HANDLE_TYPE" });
    expect(checkHandles("Reference", null, "Export", null)).toMatchObject({ ok: false, code: "HANDLE_TYPE" });
    expect(checkHandles("Reference", null, "Colour", null)).toMatchObject({ ok: false, code: "HANDLE_TYPE" });
    // and only a reference reaches a reference input
    expect(checkHandles("Plate", "image", "Edit", "refs")).toEqual({
      ok: false,
      code: "HANDLE_TYPE",
      message: "Edit references takes reference, not image16",
    });
  });

  it("the end still, the layers, the Colour mask, the People base clip and the numeric packets plug in", () => {
    expect(checkHandles("Edit", "image", "Motion", "end")).toMatchObject({ ok: true, type: "image16", targetHandle: "end" });
    expect(checkHandles("Edit", null, "Motion", null)).toMatchObject({ ok: true, targetHandle: "image" });
    expect(checkHandles("Plate", "packet", "Motion", null)).toMatchObject({ ok: true, targetHandle: "packet" });
    expect(checkHandles("Edit", "image", "Comp", "layers")).toMatchObject({ ok: true, targetHandle: "layers" });
    expect(checkHandles("Mask", null, "Colour", null)).toMatchObject({ ok: true, type: "mask", targetHandle: "mask" });
    expect(checkHandles("Motion", "video", "People", null)).toMatchObject({ ok: true, type: "video", targetHandle: "clip" });
    expect(checkHandles("Plate", "packet", "Atmosphere", null)).toMatchObject({ ok: true, targetHandle: "packet" });
    expect(checkHandles("Plate", "packet", "Sky", null)).toMatchObject({ ok: true, targetHandle: "packet" });
    expect(checkHandles("Atmosphere", null, "Export", null)).toMatchObject({ ok: true, type: "image16", targetHandle: "media" });
    expect(checkHandles("Sky", "image", "Comp", "layers")).toMatchObject({ ok: true });
  });

  it("Comp has no mask or layer port any more (a layer carries its own stored mask)", () => {
    expect(checkHandles("Mask", "mask", "Comp", "mask")).toMatchObject({ ok: false, code: "HANDLE_TYPE" });
    expect(checkHandles("Mask", "mask", "Comp", null)).toMatchObject({ ok: false, code: "HANDLE_TYPE" });
    expect(checkHandles("Edit", "image", "Comp", "layer")).toMatchObject({ ok: false, code: "HANDLE_TYPE" });
  });

  it("a Frame refuses every edge because it has no ports", () => {
    expect(checkHandles("Plate", "image", "Frame", null)).toEqual({ ok: false, code: "HANDLE_TYPE", message: "Frame has no inputs" });
    expect(checkHandles("Frame", null, "Export", null)).toEqual({ ok: false, code: "HANDLE_TYPE", message: "Frame has no outputs" });
    expect(checkHandles("Reference", null, "Frame", null)).toMatchObject({ ok: false, code: "HANDLE_TYPE" });
  });
});

// ---------------------------------------------------------------------------- the two new codes

describe("REFERENCE_ONLY and ANNOTATION", () => {
  const plateEdit = () => [node("p", "Plate"), node("r", "Reference", { params: { referenceHash: "b3_ref", role: "style" } }), node("e", "Edit")];

  it("a reference feeds an engine's reference input and nothing else", () => {
    const nodes = [...plateEdit(), node("c", "Comp"), node("x", "Export"), node("m", "Mask"), node("cl", "Colour")];
    const edges = [edge("p", "e", "image", "image")];
    expect(validateEdge({ nodes, edges }, edge("r", "e", "ref", "refs"))).toEqual({ ok: true });
    expect(validateEdge({ nodes, edges }, edge("r", "e"))).toEqual({ ok: true }); // null resolves to refs
    const msg = "a reference feeds an engine's reference input only; it is never composited";
    for (const bad of [edge("r", "e", "ref", "image"), edge("r", "e", "ref", "mask"), edge("r", "c", "ref", "base"), edge("r", "c"), edge("r", "x"), edge("r", "m"), edge("r", "cl")]) {
      expect(validateEdge({ nodes, edges }, bad)).toEqual({ ok: false, code: "REFERENCE_ONLY", message: msg });
    }
  });

  it("a reference never anchors: the engine it feeds must hang off a plate through its other inputs", () => {
    const nodes = plateEdit();
    expect(validateEdge({ nodes, edges: [] }, edge("r", "e", "ref", "refs"))).toMatchObject({ ok: false, code: "NO_PLATE_ANCHOR" });
    // once the plate is wired the same reference edge is fine, and a loaded graph with both re-checks clean
    const edges = [edge("p", "e", "image", "image"), edge("r", "e", "ref", "refs")];
    expect(validateGraph({ nodes, edges })).toEqual([]);
    // and the reference adds no generation
    expect(generationDepth(nodes, edges, "e")).toBe(1);
    expect(generationDepth(nodes, edges, "r")).toBe(0);
  });

  it("references feed Motion elements, People refs and the Sky without a lock or an anchor of their own", () => {
    const nodes = [
      node("p", "Plate"),
      node("r", "Reference"),
      node("e", "Edit", approved),
      node("m", "Motion", { riskLabel: "low" }),
      node("pp", "People"),
      node("s", "Sky"),
    ];
    const edges = [edge("p", "e"), edge("e", "m", "image", "image"), edge("p", "pp"), edge("p", "s", "packet", "packet")];
    expect(validateEdge({ nodes, edges }, edge("r", "m", "ref", "elements"))).toEqual({ ok: true });
    expect(validateEdge({ nodes, edges }, edge("r", "pp", "ref", "refs"))).toEqual({ ok: true });
    expect(validateEdge({ nodes, edges }, edge("r", "s", "ref", "sky"))).toEqual({ ok: true });
    // Sky is numeric: it needs no plate anchor at all
    expect(validateEdge({ nodes: [node("r", "Reference"), node("s", "Sky")], edges: [] }, edge("r", "s"))).toEqual({ ok: true });
  });

  it("a Frame takes no edges, in or out, and the annotation check comes first", () => {
    const nodes = [node("p", "Plate"), node("f", "Frame"), node("r", "Reference"), node("x", "Export"), node("k", "Concept")];
    const msg = "a Frame is an annotation; it takes no edges";
    for (const e of [edge("p", "f"), edge("f", "x"), edge("r", "f"), edge("k", "f"), edge("f", "f")]) {
      const got = validateEdge({ nodes, edges: [] }, e);
      if (e.source === e.target) expect(got).toMatchObject({ ok: false, code: "CYCLE" });
      else expect(got).toEqual({ ok: false, code: "ANNOTATION", message: msg });
    }
    // a Frame on the canvas changes nothing for the other nodes
    const withFrame = [...nodes, node("e", "Edit")];
    expect(validateEdge({ nodes: withFrame, edges: [] }, edge("p", "e"))).toEqual({ ok: true });
    expect(generationDepth(withFrame, [], "f")).toBe(0);
  });
});

// ---------------------------------------------------------------------------- the Motion end still

describe("VIDEO_LOCKED on every still into Motion", () => {
  const graph = (endStill: Partial<PlateNodeData>) => ({
    nodes: [node("p", "Plate"), node("s", "Edit", approved), node("t", "Edit", endStill), node("m", "Motion", { riskLabel: "low" })],
    edges: [edge("p", "s"), edge("p", "t"), edge("s", "m", "image", "image")],
  });

  it("an unapproved still wired to end is refused, and the message names the end still", () => {
    const got = validateEdge(graph({ gateStatus: "pass", approved: false }), edge("t", "m", "image", "end"));
    expect(got).toEqual({ ok: false, code: "VIDEO_LOCKED", message: "motion is locked until the end still passes gates and is approved" });
    const failed = validateEdge(graph({ gateStatus: "fail", approved: true }), edge("t", "m", "image", "end"));
    expect(failed).toMatchObject({ ok: false, code: "VIDEO_LOCKED" });
    expect(failed.ok ? "" : failed.message).toContain("end still");
  });

  it("an approved end still is allowed; the start keeps its message", () => {
    expect(validateEdge(graph(approved), edge("t", "m", "image", "end"))).toEqual({ ok: true });
    const start = validateEdge(graph({}), edge("t", "m", "image", "image"));
    expect(start).toEqual({ ok: false, code: "VIDEO_LOCKED", message: "motion is locked until the upstream still passes gates and is approved" });
    // with no handle the edge lands on the start (the first image input), as checkHandles resolves it
    expect(validateEdge(graph({}), edge("t", "m"))).toMatchObject({ ok: false, code: "VIDEO_LOCKED" });
  });

  it("a packet edge into Motion is not a still; a crop pair still is", () => {
    const g = graph(approved);
    expect(validateEdge(g, edge("p", "m", "packet", "packet"))).toEqual({ ok: true });
    // the plate's beauty is a still: locked until the plate itself is gated and approved
    expect(validateEdge(g, edge("p", "m", "image", "image"))).toMatchObject({ ok: false, code: "VIDEO_LOCKED" });
    // ports that cannot be resolved fail closed
    expect(validateEdge(g, edge("p", "m", "bogus", "packet"))).toMatchObject({ ok: false, code: "VIDEO_LOCKED" });
    const cp = { nodes: [node("p", "Plate"), node("e", "Edit"), node("cp", "CropPair")], edges: [edge("p", "e")] };
    expect(validateEdge(cp, edge("e", "cp", "image", "image"))).toEqual({
      ok: false,
      code: "VIDEO_LOCKED",
      message: "crop pair is locked until the upstream still passes gates and is approved",
    });
  });

  it("Comp, Colour, Atmosphere and Sky add no depth", () => {
    const nodes = [node("p", "Plate"), node("e1", "Edit"), node("e2", "Edit"), node("c", "Comp"), node("cl", "Colour"), node("a", "Atmosphere"), node("s", "Sky")];
    const edges = [edge("p", "e1"), edge("e1", "e2"), edge("e2", "c", "image", "layers"), edge("c", "cl"), edge("p", "a", "packet", "packet"), edge("p", "s", "packet", "packet")];
    expect(generationDepth(nodes, edges, "cl")).toBe(2);
    expect(generationDepth(nodes, edges, "a")).toBe(0);
    expect(generationDepth(nodes, edges, "s")).toBe(0);
    expect(validateGraph({ nodes, edges })).toEqual([]);
  });
});

// ---------------------------------------------------------------------------- cards

describe("node cards", () => {
  it("the Edit card carries the recipe, else a 48-character prompt caption", () => {
    const edit = (params: Record<string, unknown>) => KIND_SUMMARY.Edit(makeNodeData("Edit", { params }));
    expect(edit({})).toBe("engine: pick in the Make bar");
    expect(edit({ engine_id: "nano-banana-2.1", task: "relight", recipe: "twilight_hero" })).toBe("nano-banana-2.1 · relight · recipe twilight_hero");
    const long = "warm dusk relight over the terrace, keep every paver and the railing exactly as rendered";
    const got = edit({ engine_id: "gpt-image-2.5", prompt: long });
    expect(PROMPT_CAPTION_CHARS).toBe(48);
    expect(got).toBe(`gpt-image-2.5 · ${long.slice(0, 48)}…`);
    expect(edit({ prompt: "short prompt" })).toBe("engine: pick in the Make bar · short prompt");
    // the recipe wins over the prompt, and the camera override is still named
    expect(edit({ recipe: "season_swap", prompt: "winter", cameraOverride: true })).toBe("engine: pick in the Make bar · recipe season_swap · camera override");
    expect(edit({ prompt: "   " })).toBe("engine: pick in the Make bar");
  });

  it("Reference, Frame, Atmosphere and Sky have their facts lines", () => {
    const s = (kind: NodeKind, params?: Record<string, unknown>) => KIND_SUMMARY[kind](makeNodeData(kind, params ? { params } : {}));
    expect(s("Reference")).toBe("no image");
    expect(s("Reference", { referenceHash: "b3abcdef0123456789", role: "sky" })).toBe("sky · b3abcdef");
    expect(s("Reference", { referenceHash: "b3abcdef0123456789" })).toBe("style · b3abcdef");
    expect(s("Frame")).toBe("frame");
    expect(s("Frame", { title: "terrace set" })).toBe("terrace set");
    expect(s("Atmosphere")).toBe("visibility 800 m");
    expect(s("Atmosphere", { visibility_m: 2500 })).toBe("visibility 2500 m");
    expect(s("Sky")).toBe("sky: from the sky input · tone match on");
    expect(s("Sky", { skyHash: "b3skyhash99", tone_match: false })).toBe("sky: b3skyhas · tone match off");
  });
});

// ---------------------------------------------------------------------------- rendering

describe("the Frame annotation box", () => {
  const mount = () =>
    render(
      <ReactFlowProvider>
        <PlateCanvas />
      </ReactFlowProvider>,
    );

  it("renders a titled box of params.w × params.h with no handles and no badges", () => {
    stubFetch();
    useGraphStore.getState().loadProject({
      schema: 1,
      nodes: [
        { id: "f", kind: "Frame", label: "Frame", params: { title: "terrace set", w: 640, h: 360 }, position: { x: 0, y: 0 } },
        { id: "r", kind: "Reference", label: "Reference", params: { referenceHash: "b3abcdef01", role: "style" }, position: { x: 700, y: 0 } },
      ],
      edges: [],
    });
    mount();
    const frame = screen.getByTestId("node-Frame");
    expect(frame).toHaveClass("kind-frame");
    expect(frame).toHaveStyle({ width: "640px", height: "360px" });
    expect(frame.style.zIndex).toBe("-1");
    expect(within(frame).getByTestId("frame-title")).toHaveTextContent("terrace set");
    expect(within(frame).queryByTestId(/handle-/)).toBeNull();
    expect(within(frame).queryByTestId("badge-kind")).toBeNull();
    // the xyflow wrapper is not marked nodrag, so the Frame moves like any node
    expect(frame.closest(".react-flow__node")?.classList.contains("nodrag")).toBe(false);
    // a Reference uses the shared shell, with its one typed output
    const ref = screen.getByTestId("node-Reference");
    expect(within(ref).getByTestId("handle-out-ref")).toHaveAttribute("data-type", "reference");
    expect(within(ref).queryByTestId(/handle-in-/)).toBeNull();
    expect(within(ref).getByText("style · b3abcdef")).toBeInTheDocument();
  });

  it("defaults to 480 × 320 and the title 'frame'", () => {
    stubFetch();
    useGraphStore.getState().loadProject({ schema: 1, nodes: [{ id: "f", kind: "Frame", position: { x: 0, y: 0 } }], edges: [] });
    mount();
    const frame = screen.getByTestId("node-Frame");
    expect(FRAME_DEFAULT_WIDTH).toBe(480);
    expect(FRAME_DEFAULT_HEIGHT).toBe(320);
    expect(frame).toHaveStyle({ width: "480px", height: "320px" });
    expect(within(frame).getByTestId("frame-title")).toHaveTextContent("frame");
  });

  it("Atmosphere and Sky render the shared shell with their ports", () => {
    stubFetch();
    useGraphStore.getState().loadProject({
      schema: 1,
      nodes: [
        { id: "a", kind: "Atmosphere", position: { x: 0, y: 0 } },
        { id: "s", kind: "Sky", position: { x: 300, y: 0 } },
      ],
      edges: [],
    });
    mount();
    const sky = screen.getByTestId("node-Sky");
    expect(within(sky).getByTestId("handle-in-packet")).toHaveAttribute("data-type", "packet");
    expect(within(sky).getByTestId("handle-in-sky")).toHaveAttribute("data-type", "reference");
    expect(within(sky).getByTestId("handle-out-image")).toHaveAttribute("data-type", "image16");
    expect(within(screen.getByTestId("node-Atmosphere")).getByText("visibility 800 m")).toBeInTheDocument();
  });
});
