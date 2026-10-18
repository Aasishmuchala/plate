// T97: the Edit editor's recipe workflow (grouped select, assembled prompt, token fields, Start from this, the prompt
// modal, Save as recipe) and the Reference node editor (upload by file, drop or paste; role, note; the project's
// references). The daemon is a fetch stub that answers by "METHOD /path" and records every call, so each test also pins
// the request the editor sent. No daemon, no GPU.

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearThumbCache } from "../canvas/assets";
import { editorFor, pathEditorFor } from "../editors";
import { connectedMaskClasses, EditEditor, groupRecipes, RECIPE_CLASSES, recipeLabel, tokenSlug, tokensOf } from "../editors/EditEditor";
import { assembleRecipe, listReferences, listRecipes, parseAssembled, parseRecipe, parseReference, uploadReference } from "../editors/nodeApi";
import { stillRef } from "../editors/params";
import { PromptModal } from "../editors/PromptModal";
import { imageFromClipboard, MAX_REFERENCE_BYTES, ReferenceEditor, referenceContentType } from "../editors/ReferenceEditor";
import { useMakeStore } from "../makebar/makeStore";
import { type LoadableNode } from "../nodeTypes";
import { createInitialGraph, useGraphStore } from "../store";

// ------------------------------------------------------------------------------------------------ the daemon stub

interface Call {
  method: string;
  path: string;
  search: string;
  body: unknown;
  contentType: string | null;
}

type Route = unknown | ((call: Call) => unknown | Promise<unknown>);

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** Answers `METHOD /path` (the /api/v0 prefix and the query string are stripped); anything else is a 404 and recorded.
 * The graph PUT echoes, so a store that autosaves never sees a failure the test did not ask for. */
function mockApi(routes: Record<string, Route> = {}): Call[] {
  const calls: Call[] = [];
  const all: Record<string, Route> = { "PUT /projects/p1/graph": (c: Call) => c.body, ...routes };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://127.0.0.1");
      const method = (init?.method ?? "GET").toUpperCase();
      const path = url.pathname.replace(/^\/api\/v0/, "");
      const raw = init?.body;
      const body = typeof raw === "string" ? JSON.parse(raw) : (raw ?? null);
      const call: Call = { method, path, search: url.search, body, contentType: new Headers(init?.headers).get("content-type") };
      calls.push(call);
      const route = all[`${method} ${path}`];
      if (route === undefined) return jsonResponse({ error: `no stub for ${method} ${path}` }, 404);
      const out = typeof route === "function" ? await (route as (c: Call) => unknown)(call) : route;
      return out instanceof Response ? out : jsonResponse(out);
    }),
  );
  return calls;
}

const called = (calls: Call[], method: string, path: string): Call[] => calls.filter((c) => c.method === method && c.path === path);

// ------------------------------------------------------------------------------------------------ fixtures

/** `GET /recipes` as T83 serves it: grouped, with origins, tokens and defaults; one motion recipe to be filtered out. */
const recipe = (over: Record<string, unknown>) => ({
  kind: "still_edit",
  engine: "nano-banana-2.1",
  shot_class: "wide",
  library: null,
  mask: { editable_classes: ["sky", "vegetation"], region: "none", lock_rest: true },
  params: {},
  origin: "shipped",
  group: null,
  thumbnail: null,
  task: null,
  tokens: [],
  overrides: {},
  ...over,
});

const RECIPES = [
  recipe({ id: "relight_same_family", title: "Relight, same family", group: "lighting", task: "relight", tokens: ["[TARGET HOUR]"] }),
  recipe({ id: "twilight_hero", title: "Twilight hero", group: "lighting" }),
  recipe({ id: "season_swap", title: "Season swap", group: "season", overrides: { "[SEASON]": "autumn" } }),
  recipe({ id: "studio_dusk", title: "Studio dusk", group: "lighting", origin: "studio" }),
  recipe({ id: "my_palms", title: "Palm refresh", origin: "project" }),
  recipe({ id: "croppair_pushin", title: "CropPair push-in", kind: "motion_croppair", engine: "kling-o3-4k", group: "motion" }),
];

/** `GET /recipes/relight_same_family/assemble`: the token in the query fills `[TARGET HOUR]`. */
const assembleRelight = (c: Call) => {
  const tokens = JSON.parse(new URLSearchParams(c.search).get("tokens") ?? "{}") as Record<string, string>;
  const hour = tokens["[TARGET HOUR]"];
  const prompt = `Relight to ${hour ?? "[TARGET HOUR]"}. Keep every locked surface.`;
  return {
    prompt,
    negative_prompt: "no new windows, no invented trees",
    unfilled: hour ? 0 : 1,
    unfilled_tokens: hour ? [] : ["[TARGET HOUR]"],
    prompt_source: "recipe",
  };
};

const edge = (source: string, target: string, sourceHandle: string | null = null, targetHandle: string | null = null) => ({
  id: `${source}->${target}`,
  source,
  target,
  sourceHandle,
  targetHandle,
});

function openNode(kind: "Edit" | "Reference", extra: Partial<LoadableNode> = {}, projectId: string | null = "p1"): void {
  useGraphStore.getState().loadProject({ schema: 1, nodes: [{ id: "n1", kind, label: kind, ...extra }], edges: [] }, projectId);
}

const nodeOf = (id = "n1") => {
  const n = useGraphStore.getState().nodes.find((x) => x.id === id);
  if (!n) throw new Error(`no node ${id}`);
  return n;
};
const paramsOf = (id = "n1"): Record<string, unknown> => nodeOf(id).data.params ?? {};

beforeEach(() => {
  useGraphStore.setState(createInitialGraph());
  useMakeStore.getState().reset();
  clearThumbCache();
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const optionValues = (el: HTMLElement): string[] => within(el).getAllByRole("option").map((o) => (o as HTMLOptionElement).value);

// ================================================================================================ pure

describe("recipe helpers", () => {
  it("reads origin, group, thumbnail, task, tokens and defaults off a recipe row", () => {
    const r = parseRecipe(RECIPES[2]);
    expect(r).toMatchObject({ id: "season_swap", origin: "shipped", group: "season", thumbnail: null, task: null, tokens: [], overrides: { "[SEASON]": "autumn" } });
    expect(parseRecipe(RECIPES[0])).toMatchObject({ tokens: ["[TARGET HOUR]"], task: "relight" });
    expect(parseRecipe({ id: "bare", title: "Bare" })).toMatchObject({ origin: null, group: null, tokens: [], overrides: {} });
  });

  it("labels an option title · engine, with the folder for studio and project recipes only", () => {
    const labels = RECIPES.flatMap((r) => parseRecipe(r) ?? []).map(recipeLabel);
    expect(labels.slice(0, 5)).toEqual([
      "Relight, same family · nano-banana-2.1",
      "Twilight hero · nano-banana-2.1",
      "Season swap · nano-banana-2.1",
      "Studio dusk · nano-banana-2.1 (studio)",
      "Palm refresh · nano-banana-2.1 (project)",
    ]);
    expect(recipeLabel({ ...parseRecipe(RECIPES[0])!, engine: null })).toBe("Relight, same family · any engine");
  });

  it("groups by first appearance and keeps the ungrouped apart", () => {
    const g = groupRecipes(RECIPES.slice(0, 5).flatMap((r) => parseRecipe(r) ?? []));
    expect(g.groups.map((x) => [x.name, x.items.map((r) => r.id)])).toEqual([
      ["lighting", ["relight_same_family", "twilight_hero", "studio_dusk"]],
      ["season", ["season_swap"]],
    ]);
    expect(g.ungrouped.map((r) => r.id)).toEqual(["my_palms"]);
  });

  it("reads tokens off params, slugs them and finds the classes of the Mask wired in", () => {
    expect(tokensOf({ tokens: { "[A]": "x", "[B]": "", "[C]": 3 } })).toEqual({ "[A]": "x" });
    expect(tokensOf({ tokens: ["[A]"] })).toEqual({});
    expect(tokenSlug("[TARGET HOUR]")).toBe("target-hour");
    expect(tokenSlug("[2700K]")).toBe("2700k");
    const nodes = [
      { id: "m", data: { kind: "Mask", params: { idInfo: [{ id: "o:1", class: "vegetation" }, { id: "o:2", class: "architecture" }, { id: "o:3", class: "sky" }] } } },
      { id: "e", data: { kind: "Edit" } },
      { id: "p", data: { kind: "Plate" } },
    ];
    expect(connectedMaskClasses(nodes, [{ source: "m", target: "e" }, { source: "p", target: "e" }], "e")).toEqual(["vegetation", "sky"]);
    expect(connectedMaskClasses(nodes, [], "e")).toEqual([]);
    expect(RECIPE_CLASSES).not.toContain("architecture");
  });

  it("reads the assembled reply and builds the assemble URL with project and tokens", async () => {
    expect(parseAssembled({ prompt: "p", negative_prompt: "n", unfilled: 2, unfilled_tokens: ["[A]", "[B]"], prompt_source: "recipe" })).toEqual({
      prompt: "p",
      negativePrompt: "n",
      unfilled: 2,
      unfilledTokens: ["[A]", "[B]"],
      promptSource: "recipe",
    });
    expect(parseAssembled({ prompt: "p", unfilled_tokens: ["[A]"] }).unfilled).toBe(1);
    const calls = mockApi({ "GET /recipes/season_swap/assemble": { prompt: "x", negative_prompt: "", unfilled: 0, unfilled_tokens: [] } });
    await assembleRecipe("season_swap", "p1", { "[SEASON]": "winter" });
    await assembleRecipe("season_swap");
    const [withTokens, bare] = calls;
    expect(new URLSearchParams(withTokens?.search).get("project_id")).toBe("p1");
    expect(JSON.parse(new URLSearchParams(withTokens?.search).get("tokens") ?? "")).toEqual({ "[SEASON]": "winter" });
    expect(bare?.search).toBe("");
  });

  it("lists recipes with the project when given and posts a reference with its type", async () => {
    const calls = mockApi({
      "GET /recipes": [],
      "POST /assets": { hash: "ab".repeat(32), width: 4096, height: 2304, kind: "reference", project_id: "p1", scaled_from: [5000, 2813] },
      "GET /projects/p1/references": { references: [{ hash: "cd".repeat(32), width: 10, height: 20, bytes: 99, uploaded_utc: "2026-10-18T10:00:00Z", note: "dusk sky" }] },
    });
    await listRecipes("p1");
    await listRecipes();
    expect(calls.map((c) => c.search)).toEqual(["?project_id=p1", ""]);
    const stored = await uploadReference(new Uint8Array([1, 2, 3]), "p1", "image/jpeg");
    expect(stored).toEqual({ hash: "ab".repeat(32), width: 4096, height: 2304, scaledFrom: [5000, 2813] });
    expect(calls[2]).toMatchObject({ method: "POST", path: "/assets", search: "?kind=reference&project_id=p1", contentType: "image/jpeg" });
    expect(await listReferences("p1")).toEqual([{ hash: "cd".repeat(32), width: 10, height: 20, bytes: 99, uploadedUtc: "2026-10-18T10:00:00Z", note: "dusk sky" }]);
    expect(parseReference({ hash: "" })).toBeNull();
  });
});

describe("reference helpers", () => {
  it("accepts a PNG or JPEG by type or by extension and refuses the rest", () => {
    expect(referenceContentType({ name: "a.png", type: "image/png", size: 10 })).toBe("image/png");
    expect(referenceContentType({ name: "a.JPG", type: "", size: 10 })).toBe("image/jpeg");
    expect(referenceContentType({ name: "a.jpeg", type: "application/octet-stream", size: 10 })).toBe("image/jpeg");
    expect(() => referenceContentType({ name: "a.gif", type: "image/gif", size: 10 })).toThrow("PNG or JPEG");
    expect(() => referenceContentType({ name: "a.txt", type: "text/plain", size: 10 })).toThrow("PNG or JPEG");
    expect(() => referenceContentType({ name: "big.png", type: "image/png", size: MAX_REFERENCE_BYTES + 1 })).toThrow("32 MiB");
  });

  it("takes the first image off a clipboard and ignores text", () => {
    const png = new File([new Uint8Array([1])], "x.png", { type: "image/png" });
    const items = [
      { kind: "string", type: "text/plain", getAsFile: () => null },
      { kind: "file", type: "image/png", getAsFile: () => png },
    ];
    expect(imageFromClipboard({ items: items as never, files: [] as never })).toBe(png);
    expect(imageFromClipboard({ items: [{ kind: "string", type: "text/plain", getAsFile: () => null }] as never, files: [] as never })).toBeNull();
    expect(imageFromClipboard({ items: [] as never, files: [png] as never })).toBe(png);
    expect(imageFromClipboard(null)).toBeNull();
  });

  it("stillRef never takes a Reference's hash for the image an editor works on", () => {
    const nodes = [
      { id: "plate-1", data: { kind: "Plate", outputHash: "h_plate", packetId: "pk", tier: "A" } },
      { id: "ref-1", data: { kind: "Reference", outputHash: "h_ref" } },
      { id: "edit-1", data: { kind: "Edit" } },
    ] as never;
    const edges = [edge("ref-1", "edit-1", "ref", "refs"), edge("plate-1", "edit-1", "image", "image")];
    expect(stillRef(nodes, edges, "edit-1")?.hash).toBe("h_plate");
  });

  it("registers the Reference editor with the path editors only", () => {
    expect(pathEditorFor("Reference")).not.toBeNull();
    expect(editorFor("Reference")).toBeNull();
  });
});

// ================================================================================================ Edit editor

describe("EditEditor recipes", () => {
  it("lists the project's still recipes in optgroups with origin suffixes, ungrouped first", async () => {
    const calls = mockApi({ "GET /recipes": RECIPES });
    openNode("Edit");
    render(<EditEditor nodeId="n1" />);
    const select = screen.getByTestId("edit-recipe");
    await waitFor(() => expect(within(select).getAllByRole("option").length).toBeGreaterThan(1));
    expect(called(calls, "GET", "/recipes")[0]?.search).toBe("?project_id=p1");
    expect(optionValues(select)).toEqual(["", "my_palms", "relight_same_family", "twilight_hero", "studio_dusk", "season_swap"]);
    expect([...select.querySelectorAll("optgroup")].map((g) => g.label)).toEqual(["lighting", "season"]);
    expect(within(select).getByText("Studio dusk · nano-banana-2.1 (studio)")).toBeInTheDocument();
    expect(within(select).getByText("Palm refresh · nano-banana-2.1 (project)")).toBeInTheDocument();
    expect(within(select).getByText("Twilight hero · nano-banana-2.1")).toBeInTheDocument();
    expect(within(select).queryByText(/CropPair/)).toBeNull();
    expect(select.querySelector(":scope > option[value='my_palms']")).not.toBeNull();
  });

  it("fetches the assembled text with the unfilled count, and a token input writes params.tokens and refetches", async () => {
    const calls = mockApi({ "GET /recipes": RECIPES, "GET /recipes/relight_same_family/assemble": assembleRelight });
    openNode("Edit");
    render(<EditEditor nodeId="n1" />);
    await waitFor(() => expect(within(screen.getByTestId("edit-recipe")).getAllByRole("option").length).toBeGreaterThan(1));
    expect(screen.queryByTestId("edit-assembled")).toBeNull();
    fireEvent.change(screen.getByTestId("edit-recipe"), { target: { value: "relight_same_family" } });

    await waitFor(() => expect(screen.getByTestId("edit-assembled-text")).toHaveTextContent("Relight to [TARGET HOUR]. Keep every locked surface."));
    expect(screen.getByTestId("edit-assembled")).toBeInTheDocument();
    expect(screen.getByTestId("edit-assembled-negative")).toHaveTextContent("no new windows, no invented trees");
    expect(screen.getByTestId("edit-unfilled")).toHaveTextContent("1 unfilled");
    expect(screen.getByTestId("edit-unfilled")).toHaveAttribute("data-tone", "advisory");
    const first = called(calls, "GET", "/recipes/relight_same_family/assemble")[0];
    expect(new URLSearchParams(first?.search).get("project_id")).toBe("p1");
    expect(new URLSearchParams(first?.search).get("tokens")).toBeNull();

    const input = screen.getByTestId("edit-token-target-hour");
    expect(input).toHaveAttribute("placeholder", "required");
    expect(screen.getByTestId("edit-token-open-target-hour")).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "blue hour" } });
    expect(paramsOf()).toEqual({ recipe: "relight_same_family", tokens: { "[TARGET HOUR]": "blue hour" } });

    await waitFor(() => expect(screen.getByTestId("edit-assembled-text")).toHaveTextContent("Relight to blue hour."));
    const refetch = called(calls, "GET", "/recipes/relight_same_family/assemble").at(-1);
    expect(JSON.parse(new URLSearchParams(refetch?.search).get("tokens") ?? "")).toEqual({ "[TARGET HOUR]": "blue hour" });
    expect(screen.getByTestId("edit-unfilled")).toHaveTextContent("0 unfilled");
    expect(screen.getByTestId("edit-unfilled")).toHaveAttribute("data-tone", "muted");
    // the filled token keeps its field, and the open marker is gone
    expect(screen.getByTestId("edit-token-target-hour")).toHaveValue("blue hour");
    expect(screen.queryByTestId("edit-token-open-target-hour")).toBeNull();

    // clearing the field removes the key, and a different recipe starts with no tokens
    fireEvent.change(screen.getByTestId("edit-token-target-hour"), { target: { value: "" } });
    expect(paramsOf()).toEqual({ recipe: "relight_same_family" });
    fireEvent.change(screen.getByTestId("edit-token-target-hour"), { target: { value: "noon" } });
    fireEvent.change(screen.getByTestId("edit-recipe"), { target: { value: "twilight_hero" } });
    expect(paramsOf()).toEqual({ recipe: "twilight_hero" });
  });

  it("offers a field for a token the recipe ships a default for, with the default as its placeholder", async () => {
    mockApi({ "GET /recipes": RECIPES, "GET /recipes/season_swap/assemble": { prompt: "Autumn.", negative_prompt: "", unfilled: 0, unfilled_tokens: [] } });
    openNode("Edit");
    render(<EditEditor nodeId="n1" />);
    await waitFor(() => expect(within(screen.getByTestId("edit-recipe")).getAllByRole("option").length).toBeGreaterThan(1));
    fireEvent.change(screen.getByTestId("edit-recipe"), { target: { value: "season_swap" } });
    expect(screen.getByTestId("edit-token-season")).toHaveAttribute("placeholder", "autumn");
    expect(screen.queryByTestId("edit-token-open-season")).toBeNull();
    fireEvent.change(screen.getByTestId("edit-token-season"), { target: { value: "winter" } });
    expect(paramsOf().tokens).toEqual({ "[SEASON]": "winter" });
  });

  it("Start from this copies the assembled text into params.prompt and the source line flips to explicit", async () => {
    mockApi({ "GET /recipes": RECIPES, "GET /recipes/twilight_hero/assemble": { prompt: "Twilight, fixtures lit.", negative_prompt: "", unfilled: 0, unfilled_tokens: [] } });
    openNode("Edit");
    render(<EditEditor nodeId="n1" />);
    expect(screen.getByTestId("edit-start-from")).toBeDisabled();
    await waitFor(() => expect(within(screen.getByTestId("edit-recipe")).getAllByRole("option").length).toBeGreaterThan(1));
    fireEvent.change(screen.getByTestId("edit-recipe"), { target: { value: "twilight_hero" } });
    expect(screen.getByTestId("edit-prompt-source")).toHaveAttribute("data-source", "recipe");
    await waitFor(() => expect(screen.getByTestId("edit-start-from")).toBeEnabled());
    fireEvent.click(screen.getByTestId("edit-start-from"));
    expect(paramsOf()).toEqual({ recipe: "twilight_hero", prompt: "Twilight, fixtures lit." });
    expect(screen.getByTestId("edit-prompt")).toHaveValue("Twilight, fixtures lit.");
    expect(screen.getByTestId("edit-prompt-source")).toHaveAttribute("data-source", "explicit");
  });

  it("says why the assembled text is missing when the daemon cannot assemble the recipe", async () => {
    mockApi({ "GET /recipes": RECIPES });
    openNode("Edit", { params: { recipe: "twilight_hero" } });
    render(<EditEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("edit-assemble-error")).toBeInTheDocument());
    expect(screen.getByTestId("edit-start-from")).toBeDisabled();
    expect(screen.queryByTestId("edit-unfilled")).toBeNull();
  });
});

describe("PromptModal", () => {
  it("Expand opens a large box bound to params.prompt; Copy and Close work; Escape closes", async () => {
    mockApi({ "GET /recipes": RECIPES });
    openNode("Edit", { params: { prompt: "warm the sky" } });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    try {
      render(<EditEditor nodeId="n1" />);
      expect(screen.queryByTestId("prompt-modal")).toBeNull();
      fireEvent.click(screen.getByTestId("edit-expand"));
      const area = screen.getByTestId("prompt-modal-text");
      expect(area).toHaveValue("warm the sky");
      fireEvent.change(area, { target: { value: "warm the sky, lights on" } });
      expect(paramsOf().prompt).toBe("warm the sky, lights on");
      expect(screen.getByTestId("edit-prompt")).toHaveValue("warm the sky, lights on");

      fireEvent.click(screen.getByTestId("prompt-modal-copy"));
      await waitFor(() => expect(screen.getByTestId("prompt-modal-note")).toHaveTextContent("copied"));
      expect(writeText).toHaveBeenCalledWith("warm the sky, lights on");

      fireEvent.click(screen.getByTestId("prompt-modal-close"));
      expect(screen.queryByTestId("prompt-modal")).toBeNull();
      fireEvent.click(screen.getByTestId("edit-expand"));
      fireEvent.keyDown(screen.getByTestId("prompt-modal-text"), { key: "Escape" });
      expect(screen.queryByTestId("prompt-modal")).toBeNull();
      // an emptied box is no prompt
      fireEvent.click(screen.getByTestId("edit-expand"));
      fireEvent.change(screen.getByTestId("prompt-modal-text"), { target: { value: "" } });
      expect(paramsOf()).toEqual({});
    } finally {
      delete (navigator as unknown as Record<string, unknown>).clipboard;
    }
  });

  it("selects the text and says so when the browser has no clipboard", async () => {
    const onClose = vi.fn();
    render(<PromptModal value="a change list" onChange={() => undefined} onClose={onClose} />);
    fireEvent.click(screen.getByTestId("prompt-modal-copy"));
    await waitFor(() => expect(screen.getByTestId("prompt-modal-note")).toHaveTextContent("clipboard unavailable"));
    fireEvent.click(screen.getByTestId("prompt-modal-close"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("Save as recipe", () => {
  /** A stateful daemon: `POST /recipes` adds to the list `GET /recipes` serves. */
  function recipeDaemon(post?: (c: Call) => unknown): Call[] {
    const rows = [...RECIPES];
    return mockApi({
      "GET /recipes": () => rows,
      "POST /recipes": (c: Call) => {
        if (post) return post(c);
        const doc = (c.body as { recipe: Record<string, unknown> }).recipe;
        rows.push(recipe({ id: doc.id, title: doc.title, group: doc.group ?? null, origin: (c.body as { scope: string }).scope, mask: doc.mask }));
        return new Response(JSON.stringify({ id: doc.id, path: `/recipes/${String(doc.id)}.yaml`, origin: (c.body as { scope: string }).scope }), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        });
      },
    });
  }

  /** Mask (vegetation, architecture, sky) -> Edit n1 with a typed prompt, the Make bar on nano-banana-2.1. */
  function openWithMask(): void {
    useGraphStore.getState().loadProject(
      {
        schema: 1,
        nodes: [
          { id: "mask-1", kind: "Mask", label: "Mask", params: { idInfo: [{ id: "o:1", name: "VEG_a", class: "vegetation" }, { id: "o:2", name: "WALL", class: "architecture" }, { id: "o:3", name: "SKY", class: "sky" }] } },
          { id: "n1", kind: "Edit", label: "Edit", params: { prompt: "late blue hour, fixtures on", task: "relight" } },
        ],
        edges: [edge("mask-1", "n1", "mask", "mask")],
      },
      "p1",
    );
    useMakeStore.getState().setDraft("n1", { engineId: "nano-banana-2.1" });
  }

  it("validates the id, preselects the Mask's classes, posts the recipe and selects the saved one", async () => {
    const calls = recipeDaemon();
    openWithMask();
    render(<EditEditor nodeId="n1" />);
    await waitFor(() => expect(within(screen.getByTestId("edit-recipe")).getAllByRole("option").length).toBeGreaterThan(1));
    fireEvent.click(screen.getByTestId("edit-save-recipe"));
    const dialog = screen.getByTestId("save-recipe");
    expect(dialog).toHaveAttribute("role", "dialog");
    expect(screen.getByTestId("save-recipe-engine")).toHaveTextContent("nano-banana-2.1");
    expect(screen.getByTestId("save-recipe-prompt")).toHaveValue("late blue hour, fixtures on");
    expect(screen.getByTestId("save-recipe-task")).toHaveValue("relight");
    expect(screen.getByTestId("save-recipe-scope")).toHaveValue("project");
    // the Mask's editable classes are ticked; architecture is never offered
    expect((screen.getByTestId("save-recipe-class-vegetation") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByTestId("save-recipe-class-sky") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByTestId("save-recipe-class-people") as HTMLInputElement).checked).toBe(false);
    expect(screen.queryByTestId("save-recipe-class-architecture")).toBeNull();

    // the id must be lower_snake_case; Save waits for an id and a title
    expect(screen.getByTestId("save-recipe-submit")).toBeDisabled();
    fireEvent.change(screen.getByTestId("save-recipe-id"), { target: { value: "Blue-Hour" } });
    expect(screen.getByTestId("save-recipe-id-error")).toBeInTheDocument();
    expect(screen.getByTestId("save-recipe-id")).toHaveAttribute("aria-invalid", "true");
    fireEvent.change(screen.getByTestId("save-recipe-title"), { target: { value: "Blue hour" } });
    expect(screen.getByTestId("save-recipe-submit")).toBeDisabled();
    fireEvent.change(screen.getByTestId("save-recipe-id"), { target: { value: "blue_hour" } });
    expect(screen.queryByTestId("save-recipe-id-error")).toBeNull();
    expect(screen.getByTestId("save-recipe-submit")).toBeEnabled();

    fireEvent.change(screen.getByTestId("save-recipe-group"), { target: { value: "lighting" } });
    fireEvent.click(screen.getByTestId("save-recipe-class-people"));
    fireEvent.click(screen.getByTestId("save-recipe-submit"));

    await waitFor(() => expect(screen.getByTestId("edit-saved")).toHaveTextContent("saved blue_hour (project)"));
    const post = called(calls, "POST", "/recipes")[0];
    expect(post?.body).toEqual({
      scope: "project",
      project_id: "p1",
      recipe: {
        schema: 1,
        id: "blue_hour",
        title: "Blue hour",
        kind: "still_edit",
        engine: "nano-banana-2.1",
        prompt: "late blue hour, fixtures on",
        group: "lighting",
        task: "relight",
        mask: { editable_classes: ["vegetation", "sky", "people"] },
      },
    });
    // the list was read again, the dialog is gone and the select moved to the saved recipe
    expect(screen.queryByTestId("save-recipe")).toBeNull();
    await waitFor(() => expect(screen.getByTestId("edit-recipe")).toHaveValue("blue_hour"));
    expect(paramsOf().recipe).toBe("blue_hour");
    expect(called(calls, "GET", "/recipes")).toHaveLength(2);
    expect(within(screen.getByTestId("edit-recipe")).getByText("Blue hour · nano-banana-2.1 (project)")).toBeInTheDocument();
  });

  it("saves the assembled text when the node has no prompt of its own, with the negative prompt", async () => {
    openNode("Edit", { params: { recipe: "twilight_hero" } });
    useMakeStore.getState().setDraft("n1", { engineId: "nano-banana-2.1" });
    const calls = mockApi({
      "GET /recipes": RECIPES,
      "GET /recipes/twilight_hero/assemble": { prompt: "Twilight, fixtures lit.", negative_prompt: "no new windows", unfilled: 0, unfilled_tokens: [] },
      "POST /recipes": { id: "dusk_two", path: "x", origin: "studio" },
    });
    render(<EditEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("edit-start-from")).toBeEnabled());
    fireEvent.click(screen.getByTestId("edit-save-recipe"));
    expect(screen.getByTestId("save-recipe-prompt")).toHaveValue("Twilight, fixtures lit.");
    expect(screen.getByTestId("save-recipe-negative")).toHaveValue("no new windows");
    fireEvent.change(screen.getByTestId("save-recipe-scope"), { target: { value: "studio" } });
    fireEvent.change(screen.getByTestId("save-recipe-id"), { target: { value: "dusk_two" } });
    fireEvent.change(screen.getByTestId("save-recipe-title"), { target: { value: "Dusk two" } });
    fireEvent.click(screen.getByTestId("save-recipe-submit"));
    await waitFor(() => expect(screen.getByTestId("edit-saved")).toHaveTextContent("saved dusk_two (studio)"));
    expect(called(calls, "POST", "/recipes")[0]?.body).toMatchObject({
      scope: "studio",
      project_id: "p1",
      recipe: { id: "dusk_two", prompt: "Twilight, fixtures lit.", negative_prompt: "no new windows", mask: { editable_classes: [] } },
    });
  });

  it("keeps the dialog open with the daemon's refusal and posts overwrite when asked", async () => {
    const bodies: unknown[] = [];
    const calls = recipeDaemon((c) => {
      bodies.push(c.body);
      return bodies.length === 1
        ? jsonResponse({ error: "recipe_exists", id: "twilight_hero", errors: ["recipe 'twilight_hero' already exists"] }, 409)
        : jsonResponse({ id: "twilight_hero", path: "x", origin: "project" }, 201);
    });
    openWithMask();
    render(<EditEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("edit-save-recipe"));
    fireEvent.change(screen.getByTestId("save-recipe-id"), { target: { value: "twilight_hero" } });
    fireEvent.change(screen.getByTestId("save-recipe-title"), { target: { value: "Mine" } });
    fireEvent.click(screen.getByTestId("save-recipe-submit"));
    await waitFor(() => expect(screen.getByTestId("save-recipe-error")).toBeInTheDocument());
    expect(screen.getByTestId("save-recipe")).toBeInTheDocument();
    expect(screen.queryByTestId("edit-saved")).toBeNull();
    expect((bodies[0] as { overwrite?: boolean }).overwrite).toBeUndefined();

    fireEvent.click(screen.getByTestId("save-recipe-overwrite"));
    fireEvent.click(screen.getByTestId("save-recipe-submit"));
    await waitFor(() => expect(screen.getByTestId("edit-saved")).toHaveTextContent("saved twilight_hero (project)"));
    expect((bodies[1] as { overwrite?: boolean }).overwrite).toBe(true);
    expect(called(calls, "POST", "/recipes")).toHaveLength(2);
  });

  it("needs an engine from the Make bar and can be cancelled", async () => {
    mockApi({ "GET /recipes": RECIPES });
    openNode("Edit", { params: { prompt: "x" } });
    render(<EditEditor nodeId="n1" />);
    fireEvent.click(screen.getByTestId("edit-save-recipe"));
    expect(screen.getByTestId("save-recipe-engine")).toHaveTextContent("none picked");
    fireEvent.change(screen.getByTestId("save-recipe-id"), { target: { value: "ok_id" } });
    fireEvent.change(screen.getByTestId("save-recipe-title"), { target: { value: "Ok" } });
    expect(screen.getByTestId("save-recipe-submit")).toBeDisabled();
    expect(screen.getByTestId("save-recipe-problem")).toHaveTextContent("Make bar");
    fireEvent.click(screen.getByTestId("save-recipe-cancel"));
    expect(screen.queryByTestId("save-recipe")).toBeNull();
    expect(RECIPE_CLASSES.length).toBeGreaterThan(0);
  });
});

// ================================================================================================ Reference editor

const REF_HASH = "ab".repeat(32);
const OLD_HASH = "cd".repeat(32);
const PNG_BYTES = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10, 1, 2, 3]);

/** A paste event carrying `items`; the editor listens on the document. */
function paste(items: { kind: string; type: string; getAsFile: () => File | null }[]): Event {
  const evt = new Event("paste", { bubbles: true, cancelable: true });
  Object.defineProperty(evt, "clipboardData", { value: { items, files: [] } });
  document.dispatchEvent(evt);
  return evt;
}

describe("ReferenceEditor", () => {
  const stored = { hash: REF_HASH, width: 800, height: 600, kind: "reference", project_id: "p1", scaled_from: null };

  it("uploads a pasted image and sets params.referenceHash and outputHash", async () => {
    const calls = mockApi({ "GET /projects/p1/references": [], "POST /assets": stored });
    openNode("Reference");
    render(<ReferenceEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("reference-empty")).toBeInTheDocument());
    const file = new File([PNG_BYTES], "image.png", { type: "image/png" });
    const evt = paste([{ kind: "file", type: "image/png", getAsFile: () => file }]);
    expect(evt.defaultPrevented).toBe(true);

    await waitFor(() => expect(paramsOf().referenceHash).toBe(REF_HASH));
    expect(nodeOf().data.outputHash).toBe(REF_HASH);
    const post = called(calls, "POST", "/assets")[0];
    expect(post?.search).toBe("?kind=reference&project_id=p1");
    expect(post?.contentType).toBe("image/png");
    expect([...(post?.body as Uint8Array)]).toEqual([...PNG_BYTES]);
    expect(screen.getByTestId("reference-stored")).toHaveTextContent("stored abababab");
    expect(screen.getByTestId("reference-stored")).toHaveTextContent("800x600");
    // the new upload joins the project list without another request
    expect(screen.getByTestId("reference-pick-abababab")).toHaveAttribute("aria-pressed", "true");
    expect(called(calls, "GET", "/projects/p1/references")).toHaveLength(1);
  });

  it("ignores a paste that carries no image and stops listening when closed", async () => {
    const calls = mockApi({ "GET /projects/p1/references": [], "POST /assets": stored });
    openNode("Reference");
    const view = render(<ReferenceEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("reference-empty")).toBeInTheDocument());
    const text = paste([{ kind: "string", type: "text/plain", getAsFile: () => null }]);
    expect(text.defaultPrevented).toBe(false);
    view.unmount();
    const file = new File([PNG_BYTES], "image.png", { type: "image/png" });
    paste([{ kind: "file", type: "image/png", getAsFile: () => file }]);
    await new Promise((r) => setTimeout(r, 20));
    expect(called(calls, "POST", "/assets")).toHaveLength(0);
  });

  it("uploads a chosen file and a dropped file, and refuses a file that is not a PNG or JPEG", async () => {
    const calls = mockApi({ "GET /projects/p1/references": [], "POST /assets": stored });
    openNode("Reference");
    render(<ReferenceEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("reference-empty")).toBeInTheDocument());

    fireEvent.change(screen.getByTestId("reference-file"), { target: { files: [new File([PNG_BYTES], "a.jpg", { type: "image/jpeg" })] } });
    await waitFor(() => expect(paramsOf().referenceHash).toBe(REF_HASH));
    expect(called(calls, "POST", "/assets")[0]?.contentType).toBe("image/jpeg");

    fireEvent.drop(screen.getByTestId("reference-drop"), { dataTransfer: { items: [], files: [new File([PNG_BYTES], "b.png", { type: "image/png" })] } });
    await waitFor(() => expect(called(calls, "POST", "/assets")).toHaveLength(2));

    fireEvent.change(screen.getByTestId("reference-file"), { target: { files: [new File(["gif"], "c.gif", { type: "image/gif" })] } });
    await waitFor(() => expect(screen.getByTestId("reference-error")).toHaveTextContent("PNG or JPEG"));
    expect(called(calls, "POST", "/assets")).toHaveLength(2);
  });

  it("shows the daemon's refusal and keeps the node as it was", async () => {
    mockApi({ "GET /projects/p1/references": [], "POST /assets": jsonResponse({ error: "bad_image", detail: "a reference is a PNG or JPEG image" }, 422) });
    openNode("Reference", { params: { referenceHash: OLD_HASH } });
    render(<ReferenceEditor nodeId="n1" />);
    fireEvent.change(screen.getByTestId("reference-file"), { target: { files: [new File([PNG_BYTES], "a.png", { type: "image/png" })] } });
    await waitFor(() => expect(screen.getByTestId("reference-error")).toBeInTheDocument());
    expect(paramsOf().referenceHash).toBe(OLD_HASH);
  });

  it("writes role and note, picks one of the project's references, and clears the image", async () => {
    mockApi({
      "GET /projects/p1/references": [
        { hash: OLD_HASH, width: 1024, height: 683, bytes: 5, uploaded_utc: "2026-10-18T09:00:00Z", note: "dusk sky" },
        { hash: REF_HASH, width: 640, height: 360, bytes: 5, uploaded_utc: "2026-10-18T09:05:00Z" },
      ],
    });
    openNode("Reference");
    render(<ReferenceEditor nodeId="n1" />);
    await waitFor(() => expect(screen.getByTestId("reference-list")).toBeInTheDocument());
    expect(screen.getByTestId("reference-role")).toHaveValue("style");
    expect(optionValues(screen.getByTestId("reference-role"))).toEqual(["style", "object", "sky", "person", "swatch"]);
    fireEvent.change(screen.getByTestId("reference-role"), { target: { value: "sky" } });
    fireEvent.change(screen.getByTestId("reference-note"), { target: { value: "take the cloud shape" } });
    expect(paramsOf()).toEqual({ role: "sky", note: "take the cloud shape" });
    fireEvent.change(screen.getByTestId("reference-note"), { target: { value: "" } });
    expect(paramsOf()).toEqual({ role: "sky" });

    expect(screen.getByTestId("reference-pick-cdcdcdcd")).toHaveTextContent("1024 x 683");
    expect(screen.getByTestId("reference-pick-cdcdcdcd")).toHaveTextContent("dusk sky");
    fireEvent.click(screen.getByTestId("reference-pick-cdcdcdcd"));
    expect(paramsOf()).toEqual({ role: "sky", referenceHash: OLD_HASH });
    expect(nodeOf().data.outputHash).toBe(OLD_HASH);
    expect(screen.getByTestId("reference-pick-cdcdcdcd")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("reference-hash")).toHaveTextContent("reference cdcdcdcdcd");

    fireEvent.click(screen.getByTestId("reference-clear"));
    expect(paramsOf()).toEqual({ role: "sky" });
    expect(nodeOf().data.outputHash).toBeUndefined();
    expect(screen.getByTestId("reference-clear")).toBeDisabled();
  });

  it("needs an open project to store anything", async () => {
    const calls = mockApi({});
    openNode("Reference", {}, null);
    render(<ReferenceEditor nodeId="n1" />);
    expect(screen.getByTestId("reference-no-project")).toBeInTheDocument();
    expect(screen.getByTestId("reference-choose")).toBeDisabled();
    paste([{ kind: "file", type: "image/png", getAsFile: () => new File([PNG_BYTES], "image.png", { type: "image/png" }) }]);
    await waitFor(() => expect(screen.getByTestId("reference-error")).toHaveTextContent("open a project"));
    expect(called(calls, "POST", "/assets")).toHaveLength(0);
  });
});
