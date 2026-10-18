// T112 parameter panels: the Comp, Colour, Atmosphere, Sky and Frame editors, the multi-material Recolour pass, the
// LightMix RGB gains / every shipped preset / max gain / base / seed, and the Edit editor's `@ref` hint. The daemon is a
// fetch stub that answers by "METHOD /path" and records every call, so each test also pins the request the editor
// sent. The param names are pinned to the daemon itself: the node modules (`daemon/plate_daemon/nodes/*_node.py`) and
// `plate_gates.lightmix.mood_presets` are read as text, and every key an editor writes must be one the daemon reads.
// No daemon, no GPU.

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearThumbCache } from "../canvas/assets";
import { pathEditorFor } from "../editors";
import { AtmosphereEditor, NEUTRAL_FOG, VISIBILITY } from "../editors/AtmosphereEditor";
import { hexToLinear, linearToHex, MOOD_PRESETS, presetGains, presetLabel, readGain } from "../editors/colour";
import { ColourEditor, GRADE_CONTROLS, GRADE_KEYS, KELVIN_RANGE, readIds } from "../editors/ColourEditor";
import { CompEditor, moveLayer, orderedLayers, wiredInto } from "../editors/CompEditor";
import { EditEditor, refTokenHint, wiredReferences } from "../editors/EditEditor";
import { FrameEditor } from "../editors/FrameEditor";
import { LightMixEditor } from "../editors/LightMixEditor";
import { MAX_MATERIALS, readEntries, RecolourEditor } from "../editors/RecolourEditor";
import { SkyEditor } from "../editors/SkyEditor";
import { useMakeStore } from "../makebar/makeStore";
import type { NodeKind } from "../nodeTypes";
import { FrameNode } from "../nodes/FrameNode";
import { createInitialGraph, useGraphStore } from "../store";

// ------------------------------------------------------------------------------------------------ the daemon stub

interface Call {
  method: string;
  path: string;
  search: string;
  body: unknown;
}

type Route = unknown | ((call: Call) => unknown | Promise<unknown>);

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** Answers `METHOD /path` (the /api/v0 prefix and the query string are stripped); anything else is a 404 and recorded. */
function mockApi(routes: Record<string, Route> = {}): Call[] {
  const calls: Call[] = [];
  const all: Record<string, Route> = { "PUT /projects/p1/graph": (c: Call) => c.body, "GET /recipes": [], ...routes };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://127.0.0.1");
      const method = (init?.method ?? "GET").toUpperCase();
      const path = url.pathname.replace(/^\/api\/v0/, "");
      const raw = init?.body;
      const body = typeof raw === "string" ? JSON.parse(raw) : (raw ?? null);
      const call: Call = { method, path, search: url.search, body };
      calls.push(call);
      const route = all[`${method} ${path}`];
      if (route === undefined) return jsonResponse({ error: `no stub for ${method} ${path}` }, 404);
      const out = typeof route === "function" ? await (route as (c: Call) => unknown)(call) : route;
      return out instanceof Response ? out : jsonResponse(out);
    }),
  );
  return calls;
}

const posts = (calls: Call[], path: string): Call[] => calls.filter((c) => c.method === "POST" && c.path === path);
const PREVIEW = "/projects/p1/nodes/n1/preview";

// ------------------------------------------------------------------------------------------------ fixtures

const PLATE = { width: 640, height: 360 };
const HASH_A = "a1".repeat(32);

type Extra = Record<string, unknown>;
interface WireSpec {
  source: string;
  target?: string;
  targetHandle?: string | null;
}

/** Plate -> `kind` node `n1` (plus `more` nodes and wires), project p1 open. `extra` is merged into n1's data. */
function openGraph(kind: NodeKind, extra: Extra = {}, opts: { plate?: Extra; more?: Extra[]; wires?: WireSpec[]; plateWire?: string | null | false } = {}): void {
  const wires: WireSpec[] = [];
  if (opts.plateWire !== false) wires.push({ source: "plate-1", targetHandle: opts.plateWire ?? null });
  wires.push(...(opts.wires ?? []));
  useGraphStore.getState().loadProject(
    {
      schema: 1,
      nodes: [
        { id: "plate-1", kind: "Plate", label: "Plate", packetId: "room_a", tier: "A", outputHash: "h_plate", outputSize: PLATE, ...opts.plate },
        { id: "n1", kind, label: kind, ...extra },
        ...(opts.more ?? []),
      ],
      edges: wires.map((w, i) => ({ id: `w${i}`, source: w.source, target: w.target ?? "n1", sourceHandle: null, targetHandle: w.targetHandle ?? null })),
    } as never,
    "p1",
  );
}

const paramsOf = (id = "n1"): Record<string, unknown> => useGraphStore.getState().nodes.find((n) => n.id === id)?.data.params ?? {};

/** Pointer down at a fraction of a stage (jsdom reports every element as 1280 x 720 at the origin). */
const pointerAt = (stage: HTMLElement, fx: number, fy: number): void => {
  fireEvent.pointerDown(stage, { clientX: fx * 1280, clientY: fy * 720, pointerId: 1 });
};
const change = (testId: string, value: string): void => {
  fireEvent.change(screen.getByTestId(testId), { target: { value } });
};

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  useMakeStore.getState().reset();
  clearThumbCache();
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ------------------------------------------------------------------------------------------------ the daemon, as text

const NODE_SOURCES = import.meta.glob("../../../daemon/plate_daemon/nodes/*_node.py", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
const LIGHTMIX_GATES = Object.values(import.meta.glob("../../../gates/plate_gates/lightmix.py", { query: "?raw", import: "default", eager: true }) as Record<string, string>)[0];

const nodeSource = (file: string): string | undefined => Object.entries(NODE_SOURCES).find(([path]) => path.endsWith(`/${file}`))?.[1];

/**
 * Does the daemon read `name`? True when the node module names it in a `pick(...)` / `q.param(...)` call (the way
 * every node reads its params) or as a key of a parameter table such as `colour_node.RANGES` (`"ev": (-4.0, 4.0)`).
 */
function daemonReads(src: string, name: string): boolean {
  const q = `"${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"`;
  return new RegExp(`(?:pick|param)\\([^)]*${q}`).test(src) || new RegExp(`${q}:\\s*\\(`).test(src);
}

/** The keys an editor wrote that the daemon does not read (display-only keys the editor documents are allowed). */
function unread(file: string, params: Record<string, unknown>, displayOnly: readonly string[] = []): string[] {
  const src = nodeSource(file) ?? "";
  return Object.keys(params).filter((k) => !displayOnly.includes(k) && !daemonReads(src, k));
}

/** `mood_presets` of plate_gates/lightmix.py: the dict literal's lines, read as JSON (the file uses JSON-shaped literals). */
function daemonPresets(text: string): Record<string, Record<string, unknown>> {
  const start = text.indexOf("mood_presets: dict");
  const lines: string[] = [];
  for (const line of text.slice(start).split(/\r?\n/).slice(1)) {
    if (/^\}/.test(line)) break;
    lines.push(line.trim());
  }
  return JSON.parse(`{${lines.join("").replace(/,\s*$/, "")}}`) as Record<string, Record<string, unknown>>;
}

describe("param names are the daemon's", () => {
  const files = ["comp_node.py", "colour_node.py", "atmosphere_node.py", "sky_node.py", "lightmix_node.py", "recolour_node.py"];

  it.skipIf(Object.keys(NODE_SOURCES).length === 0)("every param an editor writes is read by its node module", () => {
    const table: Record<string, readonly string[]> = {
      "comp_node.py": ["order", "require_accept"],
      "colour_node.py": ["scope", "ids", ...GRADE_KEYS],
      "atmosphere_node.py": ["visibility_m", "colour", "sky_fog", "height_falloff_m"],
      "sky_node.py": ["skyHash", "scale", "horizon_frac", "dx_px", "tone_match"],
      "lightmix_node.py": ["gains", "preset", "targetHash", "max_gain", "keep_base", "seed"],
      "recolour_node.py": ["materials", "material", "target", "targetHex", "method", "source_lab"],
    };
    for (const file of files) {
      const src = nodeSource(file);
      expect(src, file).toBeDefined();
      for (const name of table[file] ?? []) expect(daemonReads(src ?? "", name), `${file} reads ${name}`).toBe(true);
    }
    // and the check itself is not vacuous
    expect(daemonReads(nodeSource("colour_node.py") ?? "", "exposure")).toBe(false);
    expect(daemonReads(nodeSource("sky_node.py") ?? "", "horizonFraction")).toBe(false);
  });

  it.skipIf(Object.keys(NODE_SOURCES).length === 0)("Colour's grade controls are colour_node.RANGES, range for range", () => {
    const src = nodeSource("colour_node.py") ?? "";
    for (const c of [...GRADE_CONTROLS, { key: "kelvin", min: KELVIN_RANGE.min, max: KELVIN_RANGE.max }]) {
      const m = new RegExp(`"${c.key}":\\s*\\(([-\\d.]+),\\s*([-\\d.]+)\\)`).exec(src);
      expect(m, c.key).not.toBeNull();
      expect([Number(m?.[1]), Number(m?.[2])]).toEqual([c.min, c.max]);
    }
  });

  it.skipIf(LIGHTMIX_GATES === undefined)("MOOD_PRESETS mirrors plate_gates.lightmix.mood_presets: names, order, patterns and gains", () => {
    const shipped = daemonPresets(LIGHTMIX_GATES ?? "");
    expect(Object.keys(shipped)).toEqual(expect.arrayContaining(["dusk", "moonlight", "luxury_exterior", "artificial_light"]));
    expect(Object.keys(MOOD_PRESETS)).toEqual(Object.keys(shipped));
    expect(JSON.parse(JSON.stringify(MOOD_PRESETS))).toEqual(shipped);
    // the node refuses any preset name that is not a key of that table
    expect(nodeSource("lightmix_node.py") ?? "").toMatch(/preset not in lightmix\.mood_presets/);
  });
});

// ================================================================================================ Comp

describe("CompEditor", () => {
  const LAYERS: Extra[] = [
    { id: "edit-a", kind: "Edit", label: "Planting", approved: true, outputHash: "h_a", outputSize: PLATE },
    { id: "edit-b", kind: "Edit", label: "People", approved: false, outputHash: "h_b", outputSize: PLATE },
  ];
  const openComp = (extra: Extra = {}): void =>
    openGraph("Comp", extra, {
      plateWire: "base",
      more: LAYERS,
      wires: [
        { source: "plate-1", target: "edit-a" },
        { source: "plate-1", target: "edit-b" },
        { source: "edit-a", targetHandle: "layers" },
        { source: "edit-b", targetHandle: "layers" },
      ],
    });

  it("lists the base and the wired layers in wiring order, with their accept state", () => {
    mockApi();
    openComp();
    render(<CompEditor nodeId="n1" />);
    expect(screen.getByTestId("comp-base-node")).toHaveTextContent("the plate (Plate)");
    expect(screen.getAllByTestId("comp-layer").map((li) => li.getAttribute("data-node"))).toEqual(["edit-a", "edit-b"]);
    expect(screen.getByTestId("comp-layer-accepted-edit-a")).toHaveTextContent("accepted");
    expect(screen.getByTestId("comp-layer-accepted-edit-b")).toHaveAttribute("data-tone", "fail");
    expect(screen.getByTestId("comp-order-source")).toHaveTextContent("order: as wired");
  });

  it("moving a layer writes params.order as the full id list; Wiring order removes it", () => {
    mockApi();
    openComp();
    render(<CompEditor nodeId="n1" />);
    expect(screen.getByTestId("comp-layer-earlier-edit-a")).toBeDisabled();
    fireEvent.click(screen.getByTestId("comp-layer-later-edit-a"));
    expect(paramsOf().order).toEqual(["edit-b", "edit-a"]);
    expect(screen.getAllByTestId("comp-layer").map((li) => li.getAttribute("data-node"))).toEqual(["edit-b", "edit-a"]);
    expect(screen.getByTestId("comp-order-source")).toHaveTextContent("order: set here");
    fireEvent.click(screen.getByTestId("comp-order-reset"));
    expect(paramsOf().order).toBeUndefined();
  });

  it("require_accept is a toggle, on by default; an unaccepted layer is advisory once it is off", () => {
    mockApi();
    openComp();
    render(<CompEditor nodeId="n1" />);
    const toggle = screen.getByTestId("comp-require-accept");
    expect(toggle).toBeChecked();
    fireEvent.click(toggle);
    expect(paramsOf().require_accept).toBe(false);
    expect(screen.getByTestId("comp-layer-accepted-edit-b")).toHaveAttribute("data-tone", "advisory");
    fireEvent.click(toggle);
    expect(paramsOf().require_accept).toBe(true);
  });

  it("drops ids from params.order that are no longer wired (the daemon refuses them)", async () => {
    mockApi();
    openComp({ params: { order: ["gone", "edit-b", "edit-b"] } });
    render(<CompEditor nodeId="n1" />);
    await waitFor(() => expect(paramsOf().order).toEqual(["edit-b"]));
    expect(screen.getAllByTestId("comp-layer").map((li) => li.getAttribute("data-node"))).toEqual(["edit-b", "edit-a"]);
  });

  it("Preview coverage asks the comp preview and shows each layer's share, the union and the overlap", async () => {
    const calls = mockApi({ [`POST ${PREVIEW}`]: { layers: [{ node: "edit-a", coverage: 0.125 }, { node: "edit-b", coverage: 0.05 }], overlap_px: 320, union_coverage: 0.16 } });
    openComp({ params: { order: ["edit-b", "edit-a"], require_accept: false } });
    render(<CompEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("comp-preview"));
    expect(await screen.findByTestId("comp-overlap")).toHaveTextContent("overlap 320 px");
    expect(posts(calls, PREVIEW)[0]?.body).toEqual({ what: "comp", params: { require_accept: false, order: ["edit-b", "edit-a"] } });
    expect(screen.getByTestId("comp-layer-coverage-edit-a")).toHaveTextContent("12.5 %");
    expect(screen.getByTestId("comp-union")).toHaveTextContent("union 16.0 %");
    expect(unread("comp_node.py", paramsOf())).toEqual([]);
  });

  it("with no layers it says what to wire, and a take on the base is named", () => {
    mockApi();
    openGraph("Comp", {}, { plateWire: false, more: [{ id: "edit-a", kind: "Edit", label: "Relight" }], wires: [{ source: "edit-a", targetHandle: "base" }] });
    render(<CompEditor nodeId="n1" />);
    expect(screen.getByTestId("comp-nolayers")).toBeInTheDocument();
    expect(screen.getByTestId("comp-base-node")).toHaveTextContent("Relight (edit-a): its picked take");
    expect(screen.getByTestId("comp-preview")).toBeDisabled();
  });

  it("wiredInto resolves a missing handle as the canvas rules do; orderedLayers and moveLayer are pure", () => {
    openComp();
    const { nodes, edges } = useGraphStore.getState();
    // the plate's wire has targetHandle "base"; a null handle from an image would land on base too (its first image16 input)
    expect(wiredInto(nodes, [...edges, { id: "x", source: "edit-a", target: "n1", sourceHandle: null, targetHandle: null }], "n1", "base").map((n) => n.id)).toEqual(["plate-1", "edit-a"]);
    expect(wiredInto(nodes, edges, "n1", "layers").map((n) => n.id)).toEqual(["edit-a", "edit-b"]);
    const layers = wiredInto(nodes, edges, "n1", "layers");
    expect(orderedLayers(layers, ["edit-b", "nope"]).map((n) => n.id)).toEqual(["edit-b", "edit-a"]);
    expect(moveLayer(["a", "b", "c"], 2, -1)).toEqual(["a", "c", "b"]);
    expect(moveLayer(["a", "b"], 0, -1)).toEqual(["a", "b"]);
  });
});

// ================================================================================================ Colour

describe("ColourEditor", () => {
  it("every grade control writes its daemon param; zero removes it", () => {
    mockApi();
    openGraph("Colour");
    render(<ColourEditor nodeId="n1" />);
    const values: Record<string, string> = { ev: "1.5", tint: "20", contrast: "-10", saturation: "-100", hue_deg: "45" };
    for (const c of GRADE_CONTROLS) {
      const slider = screen.getByTestId(`colour-${c.key}`);
      expect(slider).toHaveAttribute("min", String(c.min));
      expect(slider).toHaveAttribute("max", String(c.max));
      change(`colour-${c.key}`, values[c.key] ?? "0");
    }
    expect(paramsOf()).toEqual({ ev: 1.5, tint: 20, contrast: -10, saturation: -100, hue_deg: 45 });
    expect(screen.getByTestId("colour-ev-value")).toHaveTextContent("+1.50 EV");
    change("colour-ev", "0");
    expect(paramsOf().ev).toBeUndefined();
    fireEvent.click(screen.getByTestId("colour-reset"));
    expect(paramsOf()).toEqual({});
  });

  it("kelvin is off (keep the white) until switched on, then starts at the neutral 6500 K", () => {
    mockApi();
    openGraph("Colour");
    render(<ColourEditor nodeId="n1" />);
    expect(screen.queryByTestId("colour-kelvin")).toBeNull();
    fireEvent.click(screen.getByTestId("colour-kelvin-on"));
    expect(paramsOf().kelvin).toBe(6500);
    const slider = screen.getByTestId("colour-kelvin");
    expect([slider.getAttribute("min"), slider.getAttribute("max")]).toEqual(["2000", "12000"]);
    change("colour-kelvin", "3200");
    expect(paramsOf().kelvin).toBe(3200);
    expect(screen.getByTestId("colour-kelvin-value")).toHaveTextContent("3200 K");
    fireEvent.click(screen.getByTestId("colour-kelvin-on"));
    expect(paramsOf().kelvin).toBeUndefined();
  });

  it("scope: auto follows the wiring; mask without a Mask and ids without ids are refused before the daemon", () => {
    mockApi();
    openGraph("Colour");
    render(<ColourEditor nodeId="n1" />);
    expect(screen.getByTestId("colour-scope")).toHaveValue("");
    expect(within(screen.getByTestId("colour-scope")).getAllByRole("option")[0]).toHaveTextContent("the whole frame: no Mask is wired");
    change("colour-scope", "mask");
    expect(paramsOf().scope).toBe("mask");
    expect(screen.getByTestId("colour-scope-problem")).toHaveTextContent("needs a Mask node wired into the mask input");
    expect(screen.getByTestId("colour-preview")).toBeDisabled();
    change("colour-scope", "frame");
    expect(paramsOf().scope).toBe("frame");
    expect(screen.queryByTestId("colour-scope-problem")).toBeNull();
    change("colour-scope", "");
    expect(paramsOf().scope).toBeUndefined();
  });

  it("auto scope names the wired Mask", () => {
    mockApi();
    openGraph("Colour", {}, { more: [{ id: "mask-1", kind: "Mask", label: "Facade mask" }], wires: [{ source: "plate-1", target: "mask-1" }, { source: "mask-1", targetHandle: "mask" }] });
    render(<ColourEditor nodeId="n1" />);
    expect(within(screen.getByTestId("colour-scope")).getAllByRole("option")[0]).toHaveTextContent("auto (the wired mask)");
    expect(screen.getByTestId("colour-scope-mask")).toHaveTextContent("inside Facade mask (mask-1)");
  });

  it("ids scope: typed ids (lower-cased), a click on the plate adds the id under it, × takes one out", async () => {
    const calls = mockApi({ "POST /pixels/idpick": { id: "M:DEADBEEF", name: "MAT_render", class: "architecture", kind: "material" } });
    openGraph("Colour", { params: { scope: "ids" } });
    render(<ColourEditor nodeId="n1" />);
    change("colour-ids-input", "o:1A2B3C4D, o:1a2b3c4d  s:3");
    fireEvent.click(screen.getByTestId("colour-ids-add"));
    expect(paramsOf().ids).toEqual(["o:1a2b3c4d", "s:3"]);
    change("colour-ids-input", "wall");
    fireEvent.click(screen.getByTestId("colour-ids-add"));
    expect(screen.getByTestId("colour-error")).toHaveTextContent("not a legend id: wall");
    fireEvent.click(screen.getByTestId("colour-pick-material"));
    pointerAt(screen.getByTestId("colour-stage"), 0.5, 0.5);
    await waitFor(() => expect(paramsOf().ids).toEqual(["o:1a2b3c4d", "s:3", "m:deadbeef"]));
    expect(posts(calls, "/pixels/idpick")[0]?.body).toEqual({ packet_id: "room_a", x: 320, y: 180, kind: "material" });
    fireEvent.click(screen.getByTestId("colour-id-remove-s:3"));
    expect(paramsOf().ids).toEqual(["o:1a2b3c4d", "m:deadbeef"]);
    expect(readIds("o:1, M:2 bad")).toEqual({ ids: ["o:1", "m:2"], bad: ["bad"] });
  });

  it("Preview sends the scope and the grade and shows the coverage, the means and the locked ids", async () => {
    const calls = mockApi({
      [`POST ${PREVIEW}`]: { scope: "ids", px: 23040, coverage: 0.1, mean_lab_before: [50, 2, 3], mean_lab_after: [60.5, 2, 3], locked_ids_in_scope: ["o:1a2b3c4d"] },
    });
    openGraph("Colour", { params: { scope: "ids", ids: ["o:1a2b3c4d"], ev: 0.5, kelvin: 5600, hue_deg: -10 } });
    render(<ColourEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("colour-preview"));
    expect(await screen.findByTestId("colour-coverage")).toHaveTextContent("ids: 23040 px (10.0 %)");
    expect(posts(calls, PREVIEW)[0]?.body).toEqual({ what: "colour", params: { scope: "ids", ids: ["o:1a2b3c4d"], ev: 0.5, hue_deg: -10, kelvin: 5600 } });
    expect(screen.getByTestId("colour-locked")).toHaveTextContent("grades locked: o:1a2b3c4d");
    expect(screen.getByTestId("colour-means")).toHaveTextContent("mean inside: L 50.0 a 2.0 b 3.0 → L 60.5 a 2.0 b 3.0");
    expect(unread("colour_node.py", paramsOf())).toEqual([]);
  });
});

// ================================================================================================ Atmosphere

describe("AtmosphereEditor", () => {
  it("visibility: the metres field and the log slider write visibility_m; the default 800 is not written", () => {
    mockApi();
    openGraph("Atmosphere");
    render(<AtmosphereEditor nodeId="n1" />);
    expect(screen.getByTestId("atmosphere-visibility")).toHaveValue(800);
    change("atmosphere-visibility", "2000");
    expect(paramsOf().visibility_m).toBe(2000);
    expect(screen.getByTestId("atmosphere-visibility-slider-value")).toHaveTextContent("2 km");
    change("atmosphere-visibility", "5");
    expect(paramsOf().visibility_m).toBe(2000); // below 20 m is not taken
    change("atmosphere-visibility-slider", String(Math.log10(5000)));
    expect(paramsOf().visibility_m).toBe(5000);
    change("atmosphere-visibility-slider", String(Math.log10(VISIBILITY.min)));
    expect(paramsOf().visibility_m).toBe(20);
    change("atmosphere-visibility", "800");
    expect(paramsOf().visibility_m).toBeUndefined();
  });

  it("the fog colour is the sky's until set; a picked colour is written as linear [r, g, b]", () => {
    mockApi();
    openGraph("Atmosphere");
    render(<AtmosphereEditor nodeId="n1" />);
    const auto = screen.getByTestId("atmosphere-colour-auto");
    expect(auto).toBeChecked();
    fireEvent.click(auto);
    expect(paramsOf().colour).toEqual([...NEUTRAL_FOG]);
    change("atmosphere-colour", "#ff8000");
    const c = paramsOf().colour as number[];
    expect(c[0]).toBe(1);
    expect(c[1]).toBeCloseTo(0.2158, 3);
    expect(c[2]).toBe(0);
    expect(screen.getByTestId("atmosphere-colour-linear")).toHaveTextContent("linear 1.000 0.216 0.000");
    fireEvent.click(auto);
    expect(paramsOf().colour).toBeUndefined();
  });

  it("sky fog and the height falloff write sky_fog and height_falloff_m", () => {
    mockApi();
    openGraph("Atmosphere");
    render(<AtmosphereEditor nodeId="n1" />);
    expect(screen.getByTestId("atmosphere-sky-fog-value")).toHaveTextContent("0.60");
    change("atmosphere-sky-fog", "0.3");
    expect(paramsOf().sky_fog).toBe(0.3);
    change("atmosphere-sky-fog", "0.6");
    expect(paramsOf().sky_fog).toBeUndefined();
    fireEvent.click(screen.getByTestId("atmosphere-falloff-on"));
    expect(paramsOf().height_falloff_m).toBe(30);
    change("atmosphere-falloff", "120");
    expect(paramsOf().height_falloff_m).toBe(120);
    change("atmosphere-falloff", "0");
    expect(paramsOf().height_falloff_m).toBe(120);
    fireEvent.click(screen.getByTestId("atmosphere-falloff-on"));
    expect(paramsOf().height_falloff_m).toBeUndefined();
  });

  it("Preview sends the params and shows the fog fraction and where the colour came from", async () => {
    const calls = mockApi({ [`POST ${PREVIEW}`]: { f: { min: 0, median: 0.412, max: 0.98 }, f_p90: 0.731, colour: [0.7, 0.75, 0.8], colour_source: "sky_band", sky_px: 4000, notes: ["height falloff: the packet has no world_position element, so the fog is uniform"] } });
    openGraph("Atmosphere", { params: { visibility_m: 400, sky_fog: 0.8, height_falloff_m: 50 } });
    render(<AtmosphereEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("atmosphere-preview"));
    expect(await screen.findByTestId("atmosphere-fog")).toHaveTextContent("fog median 0.412 · p90 0.731");
    expect(posts(calls, PREVIEW)[0]?.body).toEqual({ what: "atmosphere", params: { visibility_m: 400, sky_fog: 0.8, height_falloff_m: 50 } });
    expect(screen.getByTestId("atmosphere-colour-source")).toHaveTextContent("colour from the sky");
    expect(screen.getByTestId("editor-atmosphere")).toHaveTextContent("no world_position element");
    expect(unread("atmosphere_node.py", paramsOf())).toEqual([]);
  });

  it("a Tier C plate is refused before the daemon (it has no Z)", () => {
    mockApi();
    openGraph("Atmosphere", {}, { plate: { tier: "C" } });
    render(<AtmosphereEditor nodeId="n1" />);
    expect(screen.getByTestId("atmosphere-tier")).toHaveTextContent("no Z");
    expect(screen.getByTestId("atmosphere-preview")).toBeDisabled();
  });

  it("sRGB hex <-> linear round-trips", () => {
    for (const hex of ["#000000", "#FFFFFF", "#BFC4D1", "#FF8000", "#383E42"]) expect(linearToHex(hexToLinear(hex))).toBe(hex);
    expect(hexToLinear("#ffffff")).toEqual([1, 1, 1]);
    expect(linearToHex([2, -1, Number.NaN])).toBe("#FF0000");
  });
});

// ================================================================================================ Sky

describe("SkyEditor", () => {
  it("without a wired Reference the picture is params.skyHash, checked as a CAS hash", () => {
    mockApi();
    openGraph("Sky");
    render(<SkyEditor nodeId="n1" />);
    expect(screen.getByTestId("sky-reference")).toHaveTextContent("no Reference is wired");
    expect(screen.getByTestId("sky-preview")).toBeDisabled();
    change("sky-hash", "not-a-hash");
    expect(screen.getByTestId("sky-hash-problem")).toBeInTheDocument();
    change("sky-hash", ` ${HASH_A} `);
    expect(paramsOf().skyHash).toBe(HASH_A);
    expect(screen.queryByTestId("sky-hash-problem")).toBeNull();
    expect(screen.getByTestId("sky-preview")).toBeEnabled();
    change("sky-hash", "");
    expect(paramsOf().skyHash).toBeUndefined();
  });

  it("a Reference wired into sky wins and is named", () => {
    mockApi();
    openGraph("Sky", {}, { plateWire: "packet", more: [{ id: "ref-1", kind: "Reference", label: "Dusk sky", params: { referenceHash: HASH_A } }], wires: [{ source: "ref-1", targetHandle: "sky" }] });
    render(<SkyEditor nodeId="n1" />);
    expect(screen.getByTestId("sky-reference")).toHaveTextContent(`from Dusk sky (ref-1) · ${HASH_A.slice(0, 8)}`);
    expect(screen.getByTestId("sky-preview")).toBeEnabled();
  });

  it("scale, horizon, slide and tone match write scale, horizon_frac, dx_px and tone_match", () => {
    mockApi();
    openGraph("Sky");
    render(<SkyEditor nodeId="n1" />);
    const scale = screen.getByTestId("sky-scale");
    expect([scale.getAttribute("min"), scale.getAttribute("max")]).toEqual(["0.05", "8"]);
    change("sky-scale", "2");
    change("sky-horizon-frac", "0.35");
    change("sky-dx", "120");
    fireEvent.click(screen.getByTestId("sky-tone-match"));
    expect(paramsOf()).toEqual({ scale: 2, horizon_frac: 0.35, dx_px: 120, tone_match: false });
    expect(screen.getByTestId("sky-dx")).toHaveAttribute("max", String(8 * PLATE.width));
    change("sky-scale", "1");
    change("sky-horizon-frac", "0.5");
    change("sky-dx", "0");
    fireEvent.click(screen.getByTestId("sky-tone-match"));
    expect(paramsOf()).toEqual({});
  });

  it("Preview sends the params and shows the horizon row, the pitch and the sky's share", async () => {
    const calls = mockApi({ [`POST ${PREVIEW}`]: { horizon_y: 171.25, pitch_deg: 1.6, coverage: 0.22, alpha_px: 50688, tone_shift_lab: [-4.2, 0.5, 1.1] } });
    openGraph("Sky", { params: { skyHash: HASH_A, scale: 1.5, tone_match: false } });
    render(<SkyEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("sky-preview"));
    expect(await screen.findByTestId("sky-horizon")).toHaveTextContent("horizon y 171.3 · pitch 1.6°");
    expect(posts(calls, PREVIEW)[0]?.body).toEqual({ what: "sky", params: { skyHash: HASH_A, scale: 1.5, tone_match: false } });
    expect(screen.getByTestId("sky-coverage")).toHaveTextContent("sky 22.0 %");
    expect(screen.getByTestId("sky-tone-shift")).toHaveTextContent("L -4.2 a 0.5 b 1.1");
    expect(unread("sky_node.py", paramsOf())).toEqual([]);
  });
});

// ================================================================================================ Frame

describe("FrameEditor", () => {
  it("title, width and height write title, w and h, which the FrameNode box draws", () => {
    mockApi();
    openGraph("Frame", {}, { plateWire: false });
    const view = render(<FrameEditor nodeId="n1" />);
    expect(screen.getByTestId("frame-w")).toHaveValue(480);
    expect(screen.getByTestId("frame-h")).toHaveValue(320);
    change("frame-title-input", "Terrace set");
    change("frame-w", "640");
    change("frame-h", "400");
    expect(paramsOf()).toEqual({ title: "Terrace set", w: 640, h: 400 });
    view.unmount();
    const data = useGraphStore.getState().nodes.find((n) => n.id === "n1")?.data;
    render(createElement(FrameNode, { id: "n1", data, selected: false } as never));
    expect(screen.getByTestId("frame-title")).toHaveTextContent("Terrace set");
    expect(screen.getByTestId("node-Frame").style.width).toBe("640px");
    expect(screen.getByTestId("node-Frame").style.height).toBe("400px");
  });

  it("size presets, defaults removed, and an empty title falls back to the box's own", () => {
    mockApi();
    openGraph("Frame", { params: { title: "x", w: 700 } }, { plateWire: false });
    render(<FrameEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("frame-size-set"));
    expect(paramsOf()).toEqual({ title: "x", w: 960, h: 540 });
    expect(screen.getByTestId("frame-size-set")).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByTestId("frame-size-shot"));
    change("frame-title-input", "  ");
    expect(paramsOf()).toEqual({});
  });
});

// ================================================================================================ Recolour: the pass

describe("RecolourEditor pass (several materials)", () => {
  const PICKS: Record<string, { id: string; name: string }> = {
    left: { id: "m:7016abcd", name: "MAT_panel" },
    right: { id: "m:3020beef", name: "MAT_door" },
  };
  /** idpick answers the left material left of centre, the right one right of it. */
  const pickRoute = (c: Call) => {
    const x = (c.body as { x: number }).x;
    const m = x < PLATE.width / 2 ? PICKS.left : PICKS.right;
    return { id: m?.id, name: m?.name, class: "architecture", kind: "material" };
  };
  const estimate = (c: Call) => {
    const p = (c.body as { params: Record<string, unknown> }).params;
    if (Array.isArray(p.materials)) {
      return {
        method: "ratio",
        expected_de00: 2.4,
        materials: (p.materials as { material: string }[]).map((m, i) => ({ material: m.material, method: i === 0 ? "recomposition" : "ratio", expected_de00: i === 0 ? 0.7 : 2.4 })),
      };
    }
    return { method: "recomposition", expected_de00: 0.8 };
  };

  it("adds the picked material and colour to params.materials; a new colour updates its entry while it is picked", async () => {
    mockApi({ "POST /pixels/idpick": pickRoute, [`POST ${PREVIEW}`]: estimate });
    openGraph("Recolour");
    render(<RecolourEditor nodeId="n1" />);
    expect(screen.getByTestId("recolour-pass-empty")).toBeInTheDocument();
    expect(screen.getByTestId("recolour-pass-add")).toBeDisabled();
    pointerAt(screen.getByTestId("recolour-stage"), 0.25, 0.5);
    await waitFor(() => expect(paramsOf().material).toBe("m:7016abcd"));
    change("recolour-target", "RAL 7016");
    fireEvent.click(screen.getByTestId("recolour-pass-add"));
    expect(paramsOf().materials).toEqual([{ material: "m:7016abcd", materialName: "MAT_panel", target: "RAL 7016" }]);
    expect(screen.getByTestId("recolour-pass-add")).toHaveTextContent("Update in the pass");
    change("recolour-target", "#4a5560");
    expect(paramsOf().materials).toEqual([{ material: "m:7016abcd", materialName: "MAT_panel", target: "#4A5560" }]);

    // a second material: picked, coloured, added
    pointerAt(screen.getByTestId("recolour-stage"), 0.75, 0.5);
    await waitFor(() => expect(paramsOf().material).toBe("m:3020beef"));
    expect(screen.getByTestId("recolour-pass-note")).toHaveTextContent("MAT_door above is not in the pass until you add it");
    change("recolour-target", "RAL 3020");
    expect((paramsOf().materials as unknown[]).length).toBe(1);
    fireEvent.click(screen.getByTestId("recolour-pass-add"));
    expect(paramsOf().materials).toEqual([
      { material: "m:7016abcd", materialName: "MAT_panel", target: "#4A5560" },
      { material: "m:3020beef", materialName: "MAT_door", target: "RAL 3020" },
    ]);
    expect(screen.getAllByTestId("recolour-pass-row")).toHaveLength(2);
    expect(screen.getByTestId("recolour-pass")).toHaveTextContent("Pass (2 of 8 materials)");

    // clicking a listed material on the plate loads its colour back
    pointerAt(screen.getByTestId("recolour-stage"), 0.25, 0.5);
    await waitFor(() => expect(paramsOf().material).toBe("m:7016abcd"));
    expect(screen.getByTestId("recolour-target")).toHaveValue("#4A5560");
    expect(paramsOf()).toMatchObject({ target: "#4A5560", targetHex: "#4A5560" });
  });

  it("per-entry and default methods, the source Lab, Edit, Remove and Clear", () => {
    mockApi({ [`POST ${PREVIEW}`]: estimate });
    openGraph("Recolour", {
      params: {
        material: "m:7016abcd",
        materialName: "MAT_panel",
        target: "RAL 7016",
        materials: [
          { material: "m:7016abcd", materialName: "MAT_panel", target: "RAL 7016" },
          { material: "m:3020beef", materialName: "MAT_door", target: { lab: [42.7, 67.2, 55.2], label: "Spectro" } },
        ],
      },
    });
    render(<RecolourEditor nodeId="n1" />);
    change("recolour-pass-method-1", "ratio");
    expect((paramsOf().materials as Extra[])[1]).toEqual({ material: "m:3020beef", materialName: "MAT_door", target: { lab: [42.7, 67.2, 55.2], label: "Spectro" }, method: "ratio" });
    change("recolour-pass-method-1", "");
    expect((paramsOf().materials as Extra[])[1]).not.toHaveProperty("method");
    change("recolour-method-default", "recomposition");
    expect(paramsOf().method).toBe("recomposition");
    change("recolour-method-default", "");
    expect(paramsOf().method).toBeUndefined();

    // the source Lab of the picked material (it is in the pass, so its entry follows)
    change("recolour-source-lab", "L 54 a 3.5 b -2");
    expect(paramsOf().source_lab).toEqual([54, 3.5, -2]);
    expect((paramsOf().materials as Extra[])[0]).toEqual({ material: "m:7016abcd", materialName: "MAT_panel", target: "RAL 7016", source_lab: [54, 3.5, -2] });
    change("recolour-source-lab", "54 3");
    expect(screen.getByTestId("recolour-source-lab-error")).toBeInTheDocument();
    expect(paramsOf().source_lab).toEqual([54, 3.5, -2]);
    change("recolour-source-lab", "");
    expect(paramsOf().source_lab).toBeUndefined();
    expect((paramsOf().materials as Extra[])[0]).not.toHaveProperty("source_lab");

    // Edit loads the second entry (a Lab swatch stays in params.target, the field empties)
    fireEvent.click(screen.getByTestId("recolour-pass-edit-1"));
    expect(paramsOf()).toMatchObject({ material: "m:3020beef", materialName: "MAT_door", target: { lab: [42.7, 67.2, 55.2], label: "Spectro" } });
    expect(screen.getByTestId("recolour-target")).toHaveValue("");
    expect(screen.getByTestId("recolour-hex")).toHaveTextContent("swatch Spectro");

    fireEvent.click(screen.getByTestId("recolour-pass-remove-0"));
    expect((paramsOf().materials as Extra[]).map((e) => e.material)).toEqual(["m:3020beef"]);
    fireEvent.click(screen.getByTestId("recolour-pass-clear"));
    expect(paramsOf().materials).toBeUndefined();
    expect(unread("recolour_node.py", paramsOf(), ["materialName"])).toEqual([]);
  });

  it("Estimate the pass sends the entries (no display names) and shows each material's expected ΔE00", async () => {
    const calls = mockApi({ [`POST ${PREVIEW}`]: estimate });
    openGraph("Recolour", {
      params: {
        method: "ratio",
        materials: [
          { material: "m:7016abcd", materialName: "MAT_panel", target: "RAL 7016", source_lab: [50, 0, 0] },
          { material: "m:3020beef", materialName: "MAT_door", target: "#CC0605", method: "degraded" },
        ],
      },
    });
    render(<RecolourEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("recolour-pass-estimate"));
    expect(await screen.findByTestId("recolour-pass-de-1")).toHaveTextContent("ΔE00 2.40");
    expect(posts(calls, PREVIEW).at(-1)?.body).toEqual({
      what: "recolour",
      params: {
        materials: [
          { material: "m:7016abcd", target: "RAL 7016", source_lab: [50, 0, 0] },
          { material: "m:3020beef", target: "#CC0605", method: "degraded" },
        ],
        method: "ratio",
      },
    });
    expect(screen.getByTestId("recolour-pass-de-0")).toHaveAttribute("data-tone", "pass");
    expect(screen.getByTestId("recolour-pass-de-1")).toHaveAttribute("data-tone", "advisory");
    for (const entry of paramsOf().materials as Extra[]) expect(unread("recolour_node.py", entry, ["materialName"])).toEqual([]);
  });

  it(`holds at most ${MAX_MATERIALS} materials; readEntries keeps well-formed, distinct entries`, () => {
    mockApi();
    const full = Array.from({ length: MAX_MATERIALS }, (_, i) => ({ material: `m:0000000${i}`, target: "RAL 7016" }));
    openGraph("Recolour", { params: { material: "m:7016abcd", target: "RAL 9010", materials: full } });
    render(<RecolourEditor nodeId="n1" />);
    expect(screen.getByTestId("recolour-pass-add")).toBeDisabled();
    expect(screen.getByTestId("recolour-pass-add")).toHaveAttribute("title", "a pass holds at most 8 materials");
    expect(
      readEntries([
        { material: "m:1", target: "RAL 7016", method: "auto", materialName: "" },
        { material: "m:1", target: "#FFFFFF" },
        { material: "m:2" },
        { target: "RAL 7016" },
        { material: "m:3", target: { lab: [1, 2, 3], label: "s" }, source_lab: [1, 2] },
        "nope",
      ]),
    ).toEqual([
      { material: "m:1", target: "RAL 7016" },
      { material: "m:3", target: { lab: [1, 2, 3], label: "s" } },
    ]);
  });
});

// ================================================================================================ LightMix

describe("LightMixEditor (T112)", () => {
  const LIGHTS = ["sun", "sky", "interior_1", "facade_wash", "landscape_up", "fill"];

  it("offers every shipped preset, and each sets the sliders by light name", () => {
    mockApi();
    openGraph("LightMix", { params: { lights: LIGHTS } });
    render(<LightMixEditor nodeId="n1" />);
    for (const name of Object.keys(MOOD_PRESETS)) {
      const button = screen.getByTestId(`lightmix-preset-${name}`);
      expect(button).toHaveTextContent(presetLabel(name));
      fireEvent.click(button);
      expect(paramsOf().preset).toBe(name);
      expect(paramsOf().gains).toEqual(presetGains(MOOD_PRESETS[name] ?? {}, LIGHTS));
      expect(button).toHaveAttribute("aria-pressed", "true");
    }
    fireEvent.click(screen.getByTestId("lightmix-preset-luxury_exterior"));
    expect(paramsOf().gains).toEqual({ sun: 0, sky: 0.15, interior_1: 0, facade_wash: 2.5, landscape_up: 1.5, fill: 1 });
    fireEvent.click(screen.getByTestId("lightmix-preset-moonlight"));
    expect(paramsOf().gains).toEqual({ sun: 0, sky: 0.08, interior_1: 0.6, facade_wash: 0.3, landscape_up: 1, fill: 1 });
    fireEvent.click(screen.getByTestId("lightmix-preset-artificial_light"));
    expect(paramsOf().gains).toEqual({ sun: 0, sky: 0.05, interior_1: 3, facade_wash: 1, landscape_up: 1, fill: 1 });
  });

  it("RGB splits a light's gain into [r, g, b] and back to one number (the mean)", () => {
    mockApi();
    openGraph("LightMix", { params: { lights: ["sun", "sky"], gains: { sun: 0.5, sky: 1 } } });
    render(<LightMixEditor nodeId="n1" />);
    expect(screen.getByTestId("lightmix-light-sun")).toHaveAttribute("data-channels", "scalar");
    fireEvent.click(screen.getByTestId("lightmix-rgb-sun"));
    expect(paramsOf().gains).toEqual({ sun: [0.5, 0.5, 0.5], sky: 1 });
    expect(screen.getByTestId("lightmix-light-sun")).toHaveAttribute("data-channels", "rgb");
    expect(screen.queryByTestId("lightmix-gain-sun")).toBeNull();
    change("lightmix-gain-sun-r", "0.8");
    change("lightmix-gain-sun-b", "1.1");
    expect(paramsOf().gains).toEqual({ sun: [0.8, 0.5, 1.1], sky: 1 });
    expect(screen.getByTestId("lightmix-gain-sun-b-value")).toHaveTextContent("1.10");
    fireEvent.click(screen.getByTestId("lightmix-rgb-sun"));
    expect(paramsOf().gains).toEqual({ sun: 0.8, sky: 1 });
    expect(unread("lightmix_node.py", paramsOf(), ["lights"])).toEqual([]);
  });

  it("max gain ends the sliders and flags a gain over it; keep base and the sample seed write keep_base and seed", () => {
    mockApi();
    openGraph("LightMix", { params: { lights: ["sun"], gains: { sun: [6, 1, 1] } } });
    render(<LightMixEditor nodeId="n1" />);
    expect(screen.getByTestId("lightmix-max-gain")).toHaveValue(8);
    expect(screen.queryByTestId("lightmix-over-max")).toBeNull();
    change("lightmix-max-gain", "4");
    expect(paramsOf().max_gain).toBe(4);
    expect(screen.getByTestId("lightmix-gain-sun-r")).toHaveAttribute("max", "4");
    expect(screen.getByTestId("lightmix-over-max")).toHaveTextContent("over max gain 4: sun");
    change("lightmix-max-gain", "");
    expect(paramsOf().max_gain).toBeUndefined();
    fireEvent.click(screen.getByTestId("lightmix-keep-base"));
    expect(paramsOf().keep_base).toBe(true);
    change("lightmix-seed-value", "7");
    expect(paramsOf().seed).toBe(7);
    fireEvent.click(screen.getByTestId("lightmix-keep-base"));
    expect(paramsOf().keep_base).toBeUndefined();
    expect(unread("lightmix_node.py", paramsOf(), ["lights"])).toEqual([]);
  });

  it("the NNLS seed sends the max gain, base and seed the artist set", async () => {
    const calls = mockApi({ [`POST ${PREVIEW}`]: { gains: { sun: 0.4 }, residual_rms: 0.002, clipped: [] } });
    openGraph("LightMix", { params: { lights: ["sun"], targetHash: HASH_A, max_gain: 4, keep_base: true, seed: 3 } });
    render(<LightMixEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("lightmix-seed-run"));
    await screen.findByTestId("lightmix-residual");
    expect(posts(calls, PREVIEW).at(-1)?.body).toEqual({ what: "lightmix", params: { target_hash: HASH_A, max_gain: 4, keep_base: true, seed: 3 } });
    expect(paramsOf().gains).toEqual({ sun: 0.4 });
  });

  it("presetGains passes vector gains through like lightmix.preset_gains; readGain takes numbers and triples only", () => {
    expect(presetGains({ "moon*": [0.6, 0.7, 1], "*": 2 }, ["moon_key", "lamp"])).toEqual({ moon_key: [0.6, 0.7, 1], lamp: 2 });
    expect(presetGains({}, ["a"], [1, 2, 3])).toEqual({ a: [1, 2, 3] });
    expect([readGain(2), readGain([1, 2, 3]), readGain(-1), readGain([1, 2]), readGain("2")]).toEqual([2, [1, 2, 3], null, null, null]);
  });
});

// ================================================================================================ Edit: @ref hint

describe("EditEditor @ref hint", () => {
  it("names @ref1..@refN after the wired references, in edge order", async () => {
    mockApi();
    openGraph("Edit", {}, {
      more: [
        { id: "ref-b", kind: "Reference", label: "Brass sample" },
        { id: "ref-a", kind: "Reference", label: "Mood board" },
      ],
      wires: [
        { source: "ref-b", targetHandle: "refs" },
        { source: "ref-a", targetHandle: "refs" },
      ],
    });
    render(<EditEditor nodeId="n1" />);
    expect(await screen.findByTestId("edit-ref-hint")).toHaveTextContent(
      "@ref1..@ref2 name the wired references (@ref1 = Brass sample, @ref2 = Mood board); the engine's own wording replaces each at the quote",
    );
  });

  it("is absent with no Reference wired, and singular with one", () => {
    mockApi();
    openGraph("Edit");
    const view = render(<EditEditor nodeId="n1" />);
    expect(screen.queryByTestId("edit-ref-hint")).toBeNull();
    view.unmount();
    const { nodes, edges } = useGraphStore.getState();
    expect(wiredReferences(nodes, edges, "n1")).toEqual([]);
    expect(refTokenHint([{ id: "r", label: "Sky" }])).toBe("@ref1 names the wired reference (@ref1 = Sky); the engine's own wording replaces each at the quote");
    // a non-Reference on refs names no image (the quote refuses it), so it takes no number
    expect(wiredReferences([{ id: "m", data: { kind: "Mask" } }, { id: "r", data: { kind: "Reference", label: "" } }], [{ source: "m", target: "e" }, { source: "r", target: "e" }], "e")).toEqual([{ id: "r", label: "r" }]);
  });
});

// ================================================================================================ registry

describe("the new panels in the registry", () => {
  it.each(["Comp", "Colour", "Atmosphere", "Sky", "Frame"] as const)("the %s panel mounts from pathEditorFor for its node", (kind) => {
    mockApi();
    openGraph(kind);
    const Editor = pathEditorFor(kind);
    if (!Editor) throw new Error(`no ${kind} editor`);
    render(<Editor nodeId="n1" />);
    const frame = screen.getByTestId(`editor-${kind.toLowerCase()}`);
    expect(frame).toHaveAttribute("data-node", "n1");
    expect(frame).toHaveAccessibleName(/editor$/);
  });
});
