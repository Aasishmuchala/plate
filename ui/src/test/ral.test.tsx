// T91 Recolour editor: the RAL Classic table, its search, the popover grid (keyboard and mouse) and the project
// swatches kept in localStorage. The daemon is a fetch stub that answers by "METHOD /path" and records every call.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RAL_SRGB, hexToLab, labToHex, loadSwatches, parseTarget, readSwatchInput, swatchStorageKey } from "../editors/colour";
import { RAL_BY_CODE, RAL_CLASSIC, searchRal } from "../editors/ralTable";
import { RecolourEditor } from "../editors/RecolourEditor";
import { createInitialGraph, useGraphStore } from "../store";

// ------------------------------------------------------------------------------------------------ the daemon stub

interface Call {
  method: string;
  path: string;
  body: unknown;
}

function mockApi(routes: Record<string, unknown> = {}): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://127.0.0.1");
      const method = (init?.method ?? "GET").toUpperCase();
      const path = url.pathname.replace(/^\/api\/v0/, "");
      const raw = init?.body;
      calls.push({ method, path, body: typeof raw === "string" ? JSON.parse(raw) : (raw ?? null) });
      const route = routes[`${method} ${path}`];
      const status = route === undefined ? 404 : 200;
      return new Response(JSON.stringify(route === undefined ? { error: `no stub for ${method} ${path}` } : route), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

const posts = (calls: Call[], path: string): Call[] => calls.filter((c) => c.method === "POST" && c.path === path);

/** Plate -> Recolour node `n1`, project `projectId` open. */
function openGraph(extra: Record<string, unknown> = {}, projectId = "p1"): void {
  useGraphStore.getState().loadProject(
    {
      schema: 1,
      nodes: [
        { id: "plate-1", kind: "Plate", label: "Plate", packetId: "room_a", tier: "A", outputHash: "h_plate", outputSize: { width: 5504, height: 3072 } },
        { id: "n1", kind: "Recolour", label: "Recolour", ...extra },
      ],
      edges: [{ id: "plate-1->n1", source: "plate-1", target: "n1", sourceHandle: null, targetHandle: null }],
    } as never,
    projectId,
  );
}

const paramsOf = (): Record<string, unknown> => useGraphStore.getState().nodes.find((n) => n.id === "n1")?.data.params ?? {};

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const openPopover = (): HTMLElement => {
  fireEvent.click(screen.getByTestId("recolour-ral-button"));
  return screen.getByTestId("recolour-ral-popover");
};

// ================================================================================================ the table

// the 15 values `colour.ts` carried before T91 (and `plate_gates.recolour.RAL_SRGB` uses); the table must not move them
const PINNED: Record<string, string> = {
  "1015": "#E6D2B5",
  "1023": "#F7B500",
  "3000": "#A72920",
  "3020": "#CC0605",
  "5002": "#00387B",
  "6005": "#114232",
  "6018": "#57A639",
  "7016": "#383E42",
  "7021": "#2F3234",
  "7024": "#45494E",
  "7035": "#CBD0CC",
  "7037": "#7A7B7A",
  "9005": "#0E0E10",
  "9010": "#F1ECE1",
  "9016": "#F1F0EA",
};

// listed on Wikipedia now but not in its pre-2020 list of 213
const LATER_CODES = ["2017", "6031", "6039", "9012"];

describe("RAL_CLASSIC", () => {
  it("is the 213 codes of the pre-2020 list plus the four later ones, each once, in code order", () => {
    expect(RAL_CLASSIC).toHaveLength(217);
    const codes = RAL_CLASSIC.map((c) => c.code);
    expect(new Set(codes).size).toBe(217);
    expect(codes).toEqual([...codes].sort());
    for (const code of LATER_CODES) expect(RAL_BY_CODE.has(code)).toBe(true);
    expect(RAL_CLASSIC.filter((c) => !LATER_CODES.includes(c.code))).toHaveLength(213);
    for (const c of RAL_CLASSIC) {
      expect(c.code).toMatch(/^[1-9]\d{3}$/);
      expect(c.hex).toMatch(/^#[0-9A-F]{6}$/);
      expect(c.name.length).toBeGreaterThan(2);
    }
  });

  it("holds the 15 codes colour.ts carried at exactly their values", () => {
    expect(Object.keys(PINNED)).toHaveLength(15);
    for (const [code, hex] of Object.entries(PINNED)) {
      expect(RAL_BY_CODE.get(code)?.hex, `RAL ${code}`).toBe(hex);
      expect(RAL_SRGB[code], `RAL_SRGB ${code}`).toBe(hex);
    }
    expect(Object.keys(RAL_SRGB).sort()).toEqual(Object.keys(PINNED).sort());
  });

  it("spot checks: 3020 and 9010 keep the colour.ts values, 7016 is Anthracite grey", () => {
    // the plan's 3020 -> #C1121C is Wikipedia's value; colour.ts (and the daemon's table) say #CC0605, and the existing
    // editor tests pin that, so the existing value stays (see ralTable.ts)
    expect(RAL_BY_CODE.get("3020")).toEqual({ code: "3020", name: "Traffic red", hex: "#CC0605" });
    expect(RAL_BY_CODE.get("7016")).toEqual({ code: "7016", name: "Anthracite grey", hex: "#383E42" });
    expect(RAL_BY_CODE.get("9010")).toEqual({ code: "9010", name: "Pure white", hex: "#F1ECE1" });
    // codes outside the 15 are Wikipedia's
    expect(RAL_BY_CODE.get("7034")).toEqual({ code: "7034", name: "Yellow grey", hex: "#939176" });
    expect(RAL_BY_CODE.get("2002")?.hex).toBe("#C63927");
    expect(RAL_BY_CODE.get("6020")?.name).toBe("Chromium green");
  });

  it("reads a name without wiki markup", () => {
    for (const c of RAL_CLASSIC) expect(c.name, `RAL ${c.code}`).toMatch(/^[A-Za-z][A-Za-z0-9 ]*$/);
    expect(RAL_BY_CODE.get("7013")?.name).toBe("Brown grey");
    expect(RAL_BY_CODE.get("1006")?.name).toBe("Maize yellow");
  });
});

describe("searchRal", () => {
  const codes = (q: string): string[] => searchRal(q).map((c) => c.code);

  it("finds anthracite as 7016 and nothing else", () => {
    expect(codes("anthracite")).toEqual(["7016"]);
    expect(codes("ANTHRACITE")).toEqual(["7016"]);
  });

  it("finds a code with or without the RAL prefix, and a code prefix", () => {
    expect(codes("7016")).toEqual(["7016"]);
    expect(codes("RAL 7016")).toEqual(["7016"]);
    expect(codes("ral7016")).toEqual(["7016"]);
    const grey = codes("70");
    expect(grey.length).toBeGreaterThan(30);
    expect(grey.every((c) => c.startsWith("70"))).toBe(true);
  });

  it("needs every word of the query", () => {
    expect(codes("light grey")).toContain("7035");
    expect(codes("light grey")).not.toContain("7016");
    expect(codes("grey 70")).toContain("7016");
    expect(codes("grey 30")).not.toContain("7016");
  });

  it("returns the whole table for an empty query and nothing for nonsense", () => {
    expect(searchRal("")).toHaveLength(217);
    expect(searchRal("  ral ")).toHaveLength(217);
    expect(searchRal("zzzz")).toEqual([]);
    expect(searchRal("9999")).toEqual([]);
  });
});

describe("colour.ts reads the whole table", () => {
  it("parseTarget accepts any code in the table and still refuses one that is not", () => {
    expect(parseTarget("RAL 7034")).toEqual({ ok: true, target: { kind: "ral", code: "7034", hex: "#939176", label: "RAL 7034" } });
    expect(parseTarget("9012")).toMatchObject({ ok: true, target: { kind: "ral", code: "9012" } });
    expect(parseTarget("RAL 9999")).toMatchObject({ ok: false, reason: expect.stringMatching(/not in the built-in table/) });
  });

  it("readSwatchInput takes a hex or a Lab and says what is wrong otherwise", () => {
    expect(readSwatchInput(" Sample A ", "#383e42", " spectro, D50 ")).toEqual({ ok: true, swatch: { name: "Sample A", hex: "#383E42", note: "spectro, D50" } });
    expect(readSwatchInput("B", "54.2 3.1 -2.0", "")).toEqual({ ok: true, swatch: { name: "B", lab: [54.2, 3.1, -2] } });
    expect(readSwatchInput("B", "L 54.2  a 3.1  b -2.0", "")).toMatchObject({ ok: true, swatch: { lab: [54.2, 3.1, -2] } });
    expect(readSwatchInput("B", "54.2, 3.1, -2", "")).toMatchObject({ ok: true });
    expect(readSwatchInput("", "#383E42", "")).toMatchObject({ ok: false, reason: expect.stringMatching(/name/) });
    expect(readSwatchInput("B", "teal", "")).toMatchObject({ ok: false });
    expect(readSwatchInput("B", "54 3", "")).toMatchObject({ ok: false });
    expect(readSwatchInput("B", "140 3 -2", "")).toMatchObject({ ok: false, reason: expect.stringMatching(/L is between/) });
    expect(readSwatchInput("B", "50 300 0", "")).toMatchObject({ ok: false, reason: expect.stringMatching(/a and b/) });
  });

  it("loadSwatches keeps what reads as a swatch and survives a broken store", () => {
    expect(loadSwatches(null)).toEqual([]);
    window.localStorage.setItem(
      swatchStorageKey("px"),
      JSON.stringify([{ name: "ok", hex: "#112233" }, { name: "lab", lab: [50, 1, 2], note: "n" }, { name: "" }, { hex: "#fff" }, { name: "bad", lab: [1, 2] }, "x", null]),
    );
    expect(loadSwatches("px")).toEqual([
      { name: "ok", hex: "#112233" },
      { name: "lab", lab: [50, 1, 2], note: "n" },
    ]);
    window.localStorage.setItem(swatchStorageKey("py"), "{not json");
    expect(loadSwatches("py")).toEqual([]);
    window.localStorage.setItem(swatchStorageKey("pz"), JSON.stringify({ not: "a list" }));
    expect(loadSwatches("pz")).toEqual([]);
  });
});

// ================================================================================================ the popover grid

describe("RecolourEditor: RAL… grid", () => {
  it("opens a six column grid of every RAL Classic code with its swatch, code and name", () => {
    mockApi({});
    openGraph();
    render(<RecolourEditor nodeId="n1" />);
    expect(screen.queryByTestId("recolour-ral-popover")).toBeNull();
    const button = screen.getByTestId("recolour-ral-button");
    expect(button).toHaveAttribute("aria-expanded", "false");
    const pop = openPopover();
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(pop).toHaveAttribute("role", "dialog");
    const grid = within(pop).getByRole("grid");
    expect(grid).toHaveAttribute("aria-colcount", "6");
    expect(within(grid).getAllByRole("gridcell")).toHaveLength(217);
    const rows = within(grid).getAllByRole("row");
    expect(rows).toHaveLength(Math.ceil(217 / 6));
    expect(within(rows[0] as HTMLElement).getAllByRole("gridcell")).toHaveLength(6);
    expect(screen.getByTestId("recolour-ral-count")).toHaveTextContent("217 colours");
    const cell = screen.getByTestId("recolour-ral-7016");
    expect(cell).toHaveTextContent("7016");
    expect(cell).toHaveTextContent("Anthracite grey");
    expect(cell).toHaveAttribute("aria-label", "RAL 7016 Anthracite grey");
    expect((cell.firstElementChild as HTMLElement).style.background).toMatch(/rgb\(56, 62, 66\)|#383e42/i);
    expect(screen.getByTestId("recolour-ral-search")).toHaveFocus();
  });

  it("searches by name: anthracite finds 7016, and clicking it writes RAL 7016", () => {
    mockApi({});
    openGraph();
    render(<RecolourEditor nodeId="n1" />);
    openPopover();
    fireEvent.change(screen.getByTestId("recolour-ral-search"), { target: { value: "anthracite" } });
    expect(screen.getByTestId("recolour-ral-count")).toHaveTextContent("1 of 217");
    const grid = screen.getByRole("grid");
    expect(within(grid).getAllByRole("gridcell")).toHaveLength(1);
    fireEvent.click(screen.getByTestId("recolour-ral-7016"));
    expect(paramsOf()).toMatchObject({ target: "RAL 7016", targetHex: "#383E42" });
    expect(screen.queryByTestId("recolour-ral-popover")).toBeNull();
    expect(screen.getByTestId("recolour-target")).toHaveValue("RAL 7016");
    expect(screen.getByTestId("recolour-hex")).toHaveTextContent("RAL 7016 · #383E42");
    expect(screen.getByTestId("recolour-ral-button")).toHaveFocus();
  });

  it("a code outside the old 15 writes its own code and hex, and the preview asks about it", async () => {
    const calls = mockApi({ "POST /projects/p1/nodes/n1/preview": { method: "recomposition", expected_de00: 0.9 } });
    openGraph({ params: { material: "m:7016abcd", materialName: "MAT_panel" } });
    render(<RecolourEditor nodeId="n1" />);
    openPopover();
    fireEvent.change(screen.getByTestId("recolour-ral-search"), { target: { value: "7034" } });
    fireEvent.click(screen.getByTestId("recolour-ral-7034"));
    expect(paramsOf()).toMatchObject({ target: "RAL 7034", targetHex: "#939176" });
    await screen.findByTestId("recolour-de");
    expect(posts(calls, "/projects/p1/nodes/n1/preview")[0]?.body).toEqual({ what: "recolour", params: { material_id: "m:7016abcd", ral: "7034" } });
  });

  it("searches by code, shows an empty result, and Enter in the search box picks the first match", () => {
    mockApi({});
    openGraph();
    render(<RecolourEditor nodeId="n1" />);
    openPopover();
    const search = screen.getByTestId("recolour-ral-search");
    fireEvent.change(search, { target: { value: "ral 90" } });
    expect(within(screen.getByRole("grid")).getAllByRole("gridcell").map((c) => c.textContent)).toEqual(
      expect.arrayContaining([expect.stringContaining("9001"), expect.stringContaining("9023")]),
    );
    fireEvent.change(search, { target: { value: "no such colour" } });
    expect(screen.getByTestId("recolour-ral-none")).toHaveTextContent("no RAL code or name matches");
    expect(screen.queryByRole("grid")).toBeNull();
    fireEvent.change(search, { target: { value: "7035" } });
    fireEvent.keyDown(search, { key: "Enter" });
    expect(paramsOf()).toMatchObject({ target: "RAL 7035", targetHex: "#CBD0CC" });
    expect(screen.queryByTestId("recolour-ral-popover")).toBeNull();
  });

  it("moves with the arrow keys over six columns, Home and End, and goes back up to the search box", () => {
    mockApi({});
    openGraph();
    render(<RecolourEditor nodeId="n1" />);
    openPopover();
    const first = RAL_CLASSIC[0]?.code ?? "";
    const at = (i: number): HTMLElement => screen.getByTestId(`recolour-ral-${RAL_CLASSIC[i]?.code}`);
    fireEvent.keyDown(screen.getByTestId("recolour-ral-search"), { key: "ArrowDown" });
    expect(at(0)).toHaveFocus();
    expect(at(0)).toHaveAttribute("tabindex", "0");
    expect(at(1)).toHaveAttribute("tabindex", "-1");
    fireEvent.keyDown(at(0), { key: "ArrowRight" });
    expect(at(1)).toHaveFocus();
    fireEvent.keyDown(at(1), { key: "ArrowDown" });
    expect(at(7)).toHaveFocus();
    fireEvent.keyDown(at(7), { key: "ArrowLeft" });
    expect(at(6)).toHaveFocus();
    fireEvent.keyDown(at(6), { key: "ArrowUp" });
    expect(at(0)).toHaveFocus();
    fireEvent.keyDown(at(0), { key: "ArrowLeft" });
    expect(at(0)).toHaveFocus();
    fireEvent.keyDown(at(0), { key: "End" });
    expect(at(216)).toHaveFocus();
    fireEvent.keyDown(at(216), { key: "ArrowDown" });
    expect(at(216)).toHaveFocus();
    fireEvent.keyDown(at(216), { key: "ArrowRight" });
    expect(at(216)).toHaveFocus();
    fireEvent.keyDown(at(216), { key: "Home" });
    expect(at(0)).toHaveFocus();
    fireEvent.keyDown(at(0), { key: "ArrowUp" });
    expect(screen.getByTestId("recolour-ral-search")).toHaveFocus();
    expect(first).toBe("1000");
  });

  it("Escape closes the popover and returns focus to the button; a press outside closes it too", () => {
    mockApi({});
    openGraph();
    render(<RecolourEditor nodeId="n1" />);
    openPopover();
    fireEvent.keyDown(screen.getByTestId("recolour-ral-search"), { key: "Escape" });
    expect(screen.queryByTestId("recolour-ral-popover")).toBeNull();
    expect(screen.getByTestId("recolour-ral-button")).toHaveFocus();
    expect(paramsOf().target).toBeUndefined();

    openPopover();
    fireEvent.mouseDown(screen.getByTestId("recolour-ral-search"));
    expect(screen.getByTestId("recolour-ral-popover")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByTestId("recolour-target"));
    expect(screen.queryByTestId("recolour-ral-popover")).toBeNull();

    // the button toggles, and a reopened popover starts with an empty search
    openPopover();
    fireEvent.change(screen.getByTestId("recolour-ral-search"), { target: { value: "grey" } });
    fireEvent.click(screen.getByTestId("recolour-ral-button"));
    expect(screen.queryByTestId("recolour-ral-popover")).toBeNull();
    openPopover();
    expect(screen.getByTestId("recolour-ral-search")).toHaveValue("");
  });

  it("marks the code the node already has", () => {
    mockApi({});
    openGraph({ params: { target: "RAL 7016", targetHex: "#383E42" } });
    render(<RecolourEditor nodeId="n1" />);
    openPopover();
    expect(screen.getByTestId("recolour-ral-7016")).toHaveAttribute("aria-current", "true");
    expect(screen.getByTestId("recolour-ral-7021")).not.toHaveAttribute("aria-current");
  });
});

// ================================================================================================ project swatches

describe("RecolourEditor: project swatches", () => {
  const add = (name: string, value: string, note = ""): void => {
    fireEvent.change(screen.getByTestId("recolour-swatch-name"), { target: { value: name } });
    fireEvent.change(screen.getByTestId("recolour-swatch-value"), { target: { value } });
    fireEvent.change(screen.getByTestId("recolour-swatch-note"), { target: { value: note } });
    fireEvent.click(screen.getByTestId("recolour-swatch-add"));
  };

  it("adds a swatch, keeps it in localStorage under the project, and it survives a remount", () => {
    mockApi({});
    openGraph();
    const first = render(<RecolourEditor nodeId="n1" />);
    expect(screen.getByTestId("recolour-swatches-empty")).toBeInTheDocument();
    add("Sample 7", "#4a5560", "spectro D50, 2026-10-12");
    const item = screen.getByTestId("recolour-swatch-item");
    expect(item).toHaveAttribute("data-name", "Sample 7");
    expect(item).toHaveTextContent("#4A5560");
    expect(item).toHaveTextContent("spectro D50, 2026-10-12");
    expect(JSON.parse(window.localStorage.getItem("plate.swatches.p1") ?? "null")).toEqual([{ name: "Sample 7", hex: "#4A5560", note: "spectro D50, 2026-10-12" }]);
    // the form is cleared
    expect(screen.getByTestId("recolour-swatch-name")).toHaveValue("");

    first.unmount();
    render(<RecolourEditor nodeId="n1" />);
    expect(screen.getByTestId("recolour-swatch-item")).toHaveAttribute("data-name", "Sample 7");
  });

  it("picking a hex swatch writes params.target, and the preview asks about its hex", async () => {
    const calls = mockApi({ "POST /projects/p1/nodes/n1/preview": { method: "recomposition", expected_de00: 0.7 } });
    openGraph({ params: { material: "m:1" } });
    render(<RecolourEditor nodeId="n1" />);
    add("Sample 7", "#4a5560");
    fireEvent.click(screen.getByTestId("recolour-swatch-use-0"));
    expect(paramsOf()).toMatchObject({ target: "#4A5560", targetHex: "#4A5560" });
    expect(screen.getByTestId("recolour-target")).toHaveValue("#4A5560");
    await screen.findByTestId("recolour-de");
    expect(posts(calls, "/projects/p1/nodes/n1/preview")[0]?.body).toEqual({ what: "recolour", params: { material_id: "m:1", hex: "#4A5560" } });
  });

  it("picking a Lab swatch keeps its exact numbers in params.target", async () => {
    const calls = mockApi({ "POST /projects/p1/nodes/n1/preview": { method: "recomposition", expected_de00: 0.7 } });
    openGraph({ params: { material: "m:1" } });
    render(<RecolourEditor nodeId="n1" />);
    add("Spectro B", "L 54.2 a 3.1 b -2.0", "i1Pro, D50");
    expect(JSON.parse(window.localStorage.getItem("plate.swatches.p1") ?? "null")).toEqual([{ name: "Spectro B", lab: [54.2, 3.1, -2], note: "i1Pro, D50" }]);
    expect(screen.getByTestId("recolour-swatch-item")).toHaveTextContent("L 54.2 a 3.1 b -2.0");
    fireEvent.click(screen.getByTestId("recolour-swatch-use-0"));
    const hex = labToHex([54.2, 3.1, -2]);
    expect(paramsOf().target).toEqual({ lab: [54.2, 3.1, -2], label: "Spectro B" });
    expect(paramsOf().targetHex).toBe(hex);
    expect(screen.getByTestId("recolour-target")).toHaveValue("");
    expect(screen.getByTestId("recolour-hex")).toHaveTextContent(`swatch Spectro B · L 54.2 a 3.1 b -2.0 · ${hex}`);
    expect(screen.queryByTestId("recolour-target-error")).toBeNull();
    await screen.findByTestId("recolour-de");
    expect(posts(calls, "/projects/p1/nodes/n1/preview")[0]?.body).toEqual({ what: "recolour", params: { material_id: "m:1", hex } });
    // the Lab round trips through sRGB to well under one ΔE
    const back = hexToLab(hex);
    expect(Math.abs(back[0] - 54.2)).toBeLessThan(0.5);

    // typing a colour replaces the swatch target; clearing the field leaves no target
    fireEvent.change(screen.getByTestId("recolour-target"), { target: { value: "RAL 7016" } });
    expect(paramsOf().target).toBe("RAL 7016");
    fireEvent.change(screen.getByTestId("recolour-target"), { target: { value: "" } });
    expect(paramsOf().target).toBeUndefined();
  });

  it("shows a Lab swatch target again when the editor is reopened on the node", () => {
    mockApi({});
    openGraph({ params: { target: { lab: [54.2, 3.1, -2], label: "Spectro B" }, targetHex: "#7C7F83" } });
    render(<RecolourEditor nodeId="n1" />);
    expect(screen.getByTestId("recolour-target")).toHaveValue("");
    expect(screen.getByTestId("recolour-hex")).toHaveTextContent(/^swatch Spectro B · L 54\.2/);
  });

  it("a swatch with the same name is replaced; remove deletes it from the list and the store", () => {
    mockApi({});
    openGraph();
    render(<RecolourEditor nodeId="n1" />);
    add("Sample 7", "#111111");
    add("Other", "#222222");
    add("sample 7", "#333333", "re-measured");
    const names = screen.getAllByTestId("recolour-swatch-item").map((li) => li.getAttribute("data-name"));
    expect(names).toEqual(["Other", "sample 7"]);
    expect(JSON.parse(window.localStorage.getItem("plate.swatches.p1") ?? "null")).toEqual([{ name: "Other", hex: "#222222" }, { name: "sample 7", hex: "#333333", note: "re-measured" }]);
    fireEvent.click(screen.getByTestId("recolour-swatch-remove-0"));
    expect(screen.getAllByTestId("recolour-swatch-item")).toHaveLength(1);
    expect(JSON.parse(window.localStorage.getItem("plate.swatches.p1") ?? "null")).toEqual([{ name: "sample 7", hex: "#333333", note: "re-measured" }]);
    fireEvent.click(screen.getByTestId("recolour-swatch-remove-0"));
    expect(screen.getByTestId("recolour-swatches-empty")).toBeInTheDocument();
    expect(JSON.parse(window.localStorage.getItem("plate.swatches.p1") ?? "null")).toEqual([]);
  });

  it("refuses a swatch without a name or a readable colour and says why; Enter in a field adds", () => {
    mockApi({});
    openGraph();
    render(<RecolourEditor nodeId="n1" />);
    add("", "#111111");
    expect(screen.getByTestId("recolour-swatch-error")).toHaveTextContent("name the swatch");
    add("Sample", "teal");
    expect(screen.getByTestId("recolour-swatch-error")).toHaveTextContent(/hex \(#383E42\) or a Lab/);
    expect(window.localStorage.getItem("plate.swatches.p1")).toBeNull();
    fireEvent.change(screen.getByTestId("recolour-swatch-name"), { target: { value: "Sample" } });
    fireEvent.change(screen.getByTestId("recolour-swatch-value"), { target: { value: "#123456" } });
    fireEvent.keyDown(screen.getByTestId("recolour-swatch-value"), { key: "Enter" });
    expect(screen.queryByTestId("recolour-swatch-error")).toBeNull();
    expect(screen.getAllByTestId("recolour-swatch-item")).toHaveLength(1);
  });

  it("keeps swatches per project", () => {
    mockApi({});
    openGraph({}, "p1");
    const first = render(<RecolourEditor nodeId="n1" />);
    add("Only in p1", "#111111");
    first.unmount();
    openGraph({}, "p2");
    render(<RecolourEditor nodeId="n1" />);
    expect(screen.getByTestId("recolour-swatches-empty")).toBeInTheDocument();
    expect(window.localStorage.getItem("plate.swatches.p2")).toBeNull();
    expect(window.localStorage.getItem("plate.swatches.p1")).not.toBeNull();
  });

  it("without an open project the list says so and adding is off", () => {
    mockApi({});
    useGraphStore.getState().loadProject(
      { schema: 1, nodes: [{ id: "n1", kind: "Recolour", label: "Recolour" }], edges: [] } as never,
      null,
    );
    render(<RecolourEditor nodeId="n1" />);
    expect(screen.getByTestId("recolour-swatches-noproject")).toBeInTheDocument();
    expect(screen.getByTestId("recolour-swatch-add")).toBeDisabled();
    expect(screen.getByTestId("recolour-swatch-name")).toBeDisabled();
  });

  it("says so when the browser will not keep the swatches", async () => {
    mockApi({});
    openGraph();
    render(<RecolourEditor nodeId="n1" />);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    add("Sample", "#123456");
    await waitFor(() => expect(screen.getByTestId("recolour-swatch-error")).toHaveTextContent(/would not keep the swatches/));
    // it is still usable for this session
    expect(screen.getAllByTestId("recolour-swatch-item")).toHaveLength(1);
  });
});
