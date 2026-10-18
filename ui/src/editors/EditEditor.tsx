// Edit node editor (T69, recipes T97): what the engine is asked to change. A recipe (from `GET /recipes`, the still
// recipes only) carries the change-list prompt, the engine and the classes the mask lets the engine touch; a typed
// prompt overrides the recipe's (decision 33: explicit > recipe; the daemon composes or fills the recipe template at
// the quote).
//
// T97 adds the recipe workflow (decision 50). The select is grouped by the recipe's `group`; each option reads
// `title · engine`, plus `(studio)` or `(project)` for a recipe that did not ship with the daemon. The `Assembled
// prompt` disclosure shows the text the recipe would send (`GET /recipes/{id}/assemble`: its defaults, then
// `params.tokens`, then the Project Sheet), the negative prompt and how many `[TOKEN]`s are still open. Every token the
// recipe has gets an input that writes `params.tokens["[TOKEN]"]` (an open one is marked, a defaulted one shows its
// default), `Start from this` copies the assembled text into `params.prompt`, `Expand` opens the prompt modal and
// `Save as recipe…` posts the current prompt to `POST /recipes` (studio or project folder).
//
// Node params: `recipe` (recipe id), `prompt` (explicit prompt text), `tokens` ({"[TOKEN]": text}), `task`,
// `shot_class`. The Make bar sends `prompt` / `recipe` / `task` / `shot_class` with the quote; the engine itself is the
// Make bar's (picking a recipe fills it when the bar has none). The Make bar's `preset` plays no part here (T102).

import { type ReactElement, useEffect, useRef, useState } from "react";
import { useMakeStore } from "../makebar/makeStore";
import { useGraphStore } from "../store";
import { errorMessage } from "./api";
import { ID_CLASSES, NEVER_EDITABLE } from "./maskMath";
import {
  type AssembledRecipe,
  assembleRecipe,
  listRecipes,
  type RecipeDoc,
  RECIPE_SCOPES,
  type RecipeItem,
  type RecipeScope,
  type SavedRecipe,
  saveRecipe,
} from "./nodeApi";
import { asString, useNodeEditor } from "./params";
import { PromptModal } from "./PromptModal";
import { C, E } from "./theme";
import { Button, Chip, EditorFrame, Field, Notice, Section, useAction } from "./widgets";

/** The image tasks an Edit node can make (the Make bar's `MAKE_MODES.Edit.tasks`). */
export const EDIT_TASKS = ["relight", "people", "vegetation", "sky", "rephotograph_close"] as const;
export const SHOT_CLASSES = ["wide", "medium", "close"] as const;
/** Recipe kinds an Edit node runs: the still recipes (a `motion_croppair` recipe belongs to CropPair / Motion). */
export const EDIT_RECIPE_KINDS: readonly string[] = ["still_edit", "still_rephotograph"];
/** The classes a saved recipe may open: every legend class that policy can ever let an engine change. */
export const RECIPE_CLASSES: readonly string[] = ID_CLASSES.filter((c) => !NEVER_EDITABLE.has(c));
/** A recipe id is a file stem: lower_snake_case, starting with a letter (the daemon's own rule). */
export const RECIPE_ID_PATTERN = /^[a-z][a-z0-9_]*$/;
/** How long a token edit waits before the assembled text is fetched again (a recipe change fetches at once). */
const ASSEMBLE_DEBOUNCE_MS = 250;

export type PromptSource = "explicit" | "recipe" | "none";

/** Which prompt the daemon will use for these params (decision 33). Pure. */
export function promptSource(params: Record<string, unknown>): PromptSource {
  if (asString(params.prompt).trim() !== "") return "explicit";
  if (asString(params.recipe) !== "") return "recipe";
  return "none";
}

/** `params.tokens` as the daemon takes it: literal `[TOKEN]` keys with text values; anything else is dropped. Pure. */
export function tokensOf(params: Record<string, unknown>): Record<string, string> {
  const raw = params.tokens;
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return {};
  return Object.fromEntries(Object.entries(raw).filter((e): e is [string, string] => typeof e[1] === "string" && e[1] !== ""));
}

/** `[TARGET HOUR]` -> `target-hour`, for test ids and element ids. Pure. */
export const tokenSlug = (token: string): string =>
  token
    .replace(/[^A-Za-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .toLowerCase();

/** The option text of a recipe: `title · engine`, with the folder it came from when it is not the shipped one. */
export function recipeLabel(r: RecipeItem): string {
  const suffix = r.origin === "studio" || r.origin === "project" ? ` (${r.origin})` : "";
  return `${r.title} · ${r.engine ?? "any engine"}${suffix}`;
}

export interface RecipeGroups {
  /** Recipes with no `group`, in the daemon's order. */
  ungrouped: RecipeItem[];
  /** One entry per group name, in order of first appearance. */
  groups: { name: string; items: RecipeItem[] }[];
}

/** Split a recipe list for a select with optgroups. Pure. */
export function groupRecipes(recipes: readonly RecipeItem[]): RecipeGroups {
  const ungrouped: RecipeItem[] = [];
  const groups: { name: string; items: RecipeItem[] }[] = [];
  for (const r of recipes) {
    if (r.group === null) {
      ungrouped.push(r);
      continue;
    }
    const held = groups.find((g) => g.name === r.group);
    if (held) held.items.push(r);
    else groups.push({ name: r.group, items: [r] });
  }
  return { ungrouped, groups };
}

/** The editable classes of the Mask node wired into `nodeId` (from its picked ids' `idInfo`), in legend order. Pure. */
export function connectedMaskClasses(
  nodes: readonly { id: string; data: { kind: string; params?: Record<string, unknown> } }[],
  edges: readonly { source: string; target: string }[],
  nodeId: string,
): string[] {
  const found = new Set<string>();
  for (const e of edges) {
    if (e.target !== nodeId) continue;
    const src = nodes.find((n) => n.id === e.source);
    if (src?.data.kind !== "Mask") continue;
    const info = src.data.params?.idInfo;
    if (!Array.isArray(info)) continue;
    for (const item of info) {
      const cls = typeof item === "object" && item !== null ? (item as Record<string, unknown>).class : undefined;
      if (typeof cls === "string" && RECIPE_CLASSES.includes(cls)) found.add(cls);
    }
  }
  return RECIPE_CLASSES.filter((c) => found.has(c));
}

// ---------------------------------------------------------------------------------------------------- save dialog

interface SaveDialogProps {
  projectId: string | null;
  engine: string | null;
  prompt: string;
  negativePrompt: string;
  task: string;
  shotClass: string;
  classes: string[];
  groups: string[];
  onSaved: (saved: SavedRecipe) => void;
  onCancel: () => void;
}

function SaveRecipeDialog(p: SaveDialogProps): ReactElement {
  const [id, setId] = useState("");
  const [title, setTitle] = useState("");
  const [scope, setScope] = useState<RecipeScope>(p.projectId ? "project" : "studio");
  const [group, setGroup] = useState("");
  const [task, setTask] = useState(p.task);
  const [classes, setClasses] = useState<string[]>(p.classes);
  const [prompt, setPrompt] = useState(p.prompt);
  const [negative, setNegative] = useState(p.negativePrompt);
  const [overwrite, setOverwrite] = useState(false);

  const idOk = RECIPE_ID_PATTERN.test(id);
  const problem =
    p.engine === null
      ? "pick an engine in the Make bar first: the recipe saves with it"
      : !idOk
        ? null
        : title.trim() === ""
          ? "a recipe needs a title"
          : prompt.trim() === ""
            ? "a recipe needs a prompt"
            : scope === "project" && !p.projectId
              ? "open a project to save into it"
              : null;
  const ready = p.engine !== null && idOk && title.trim() !== "" && prompt.trim() !== "" && !(scope === "project" && !p.projectId);

  const [save, saveState] = useAction(async () => {
    if (p.engine === null) return;
    const doc: RecipeDoc = {
      schema: 1,
      id,
      title: title.trim(),
      kind: "still_edit",
      engine: p.engine,
      prompt: prompt.trim(),
      ...(negative.trim() !== "" ? { negative_prompt: negative.trim() } : {}),
      ...(group.trim() !== "" ? { group: group.trim() } : {}),
      ...(task !== "" ? { task } : {}),
      ...(p.shotClass !== "" ? { shot_class: p.shotClass } : {}),
      mask: { editable_classes: RECIPE_CLASSES.filter((c) => classes.includes(c)) },
    };
    p.onSaved(await saveRecipe(scope, p.projectId, doc, overwrite));
  }, errorMessage);

  const toggle = (cls: string, on: boolean): void => setClasses((cur) => (on ? [...cur, cls] : cur.filter((c) => c !== cls)));

  return (
    <form
      role="dialog"
      aria-label="Save as recipe"
      data-testid="save-recipe"
      style={{ ...E.section, background: C.bg, border: `1px solid ${C.line}` }}
      onSubmit={(e) => {
        e.preventDefault();
        if (ready && !saveState.pending) void save();
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape") p.onCancel();
      }}
    >
      <h4 style={E.h}>Save as recipe</h4>
      <Field label="id">
        <input
          style={E.wide}
          value={id}
          placeholder="lower_snake_case"
          aria-label="recipe id"
          aria-invalid={id !== "" && !idOk}
          data-testid="save-recipe-id"
          onChange={(e) => setId(e.target.value)}
        />
      </Field>
      {id !== "" && !idOk ? (
        <Notice tone="error" testId="save-recipe-id-error">
          the id is lower_snake_case: a letter first, then a-z, 0-9 and _
        </Notice>
      ) : null}
      <Field label="title">
        <input style={E.wide} value={title} aria-label="recipe title" data-testid="save-recipe-title" onChange={(e) => setTitle(e.target.value)} />
      </Field>
      <div style={E.row}>
        <Field label="scope">
          <select style={E.input} value={scope} aria-label="recipe scope" data-testid="save-recipe-scope" onChange={(e) => setScope(e.target.value as RecipeScope)}>
            {RECIPE_SCOPES.map((s) => (
              <option key={s} value={s} disabled={s === "project" && !p.projectId}>
                {s}
              </option>
            ))}
          </select>
        </Field>
        <Field label="group">
          <input
            style={E.input}
            value={group}
            list="save-recipe-groups"
            aria-label="recipe group"
            placeholder="optional"
            data-testid="save-recipe-group"
            onChange={(e) => setGroup(e.target.value)}
          />
          <datalist id="save-recipe-groups">
            {p.groups.map((g) => (
              <option key={g} value={g} />
            ))}
          </datalist>
        </Field>
      </div>
      <div style={E.row}>
        <Field label="engine">
          <span data-testid="save-recipe-engine" style={p.engine ? undefined : E.error}>
            {p.engine ?? "none picked"}
          </span>
        </Field>
        <Field label="task">
          <select style={E.input} value={task} aria-label="recipe task" data-testid="save-recipe-task" onChange={(e) => setTask(e.target.value)}>
            <option value="">any</option>
            {EDIT_TASKS.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <div style={E.row} role="group" aria-label="editable classes" data-testid="save-recipe-classes">
        <span style={E.label}>editable</span>
        {RECIPE_CLASSES.map((cls) => (
          <label key={cls} style={E.field}>
            <input type="checkbox" checked={classes.includes(cls)} data-testid={`save-recipe-class-${cls}`} onChange={(e) => toggle(cls, e.target.checked)} />
            <span>{cls}</span>
          </label>
        ))}
      </div>
      <span style={E.note}>
        {classes.length === 0 ? "no class ticked: the recipe edits the whole frame and the gates still hold every locked id" : "everything else is composited back from the plate"}
      </span>
      <textarea
        style={{ ...E.wide, minHeight: 64, resize: "vertical" }}
        value={prompt}
        placeholder="prompt"
        aria-label="recipe prompt"
        data-testid="save-recipe-prompt"
        onChange={(e) => setPrompt(e.target.value)}
      />
      <textarea
        style={{ ...E.wide, minHeight: 36, resize: "vertical" }}
        value={negative}
        placeholder="negative prompt (optional)"
        aria-label="recipe negative prompt"
        data-testid="save-recipe-negative"
        onChange={(e) => setNegative(e.target.value)}
      />
      <label style={E.field} title="replace a recipe that already has this id">
        <input type="checkbox" checked={overwrite} data-testid="save-recipe-overwrite" onChange={(e) => setOverwrite(e.target.checked)} />
        <span>overwrite if the id exists</span>
      </label>
      {problem ? (
        <span style={E.note} data-testid="save-recipe-problem">
          {problem}
        </span>
      ) : null}
      {saveState.error ? (
        <Notice tone="error" testId="save-recipe-error">
          {saveState.error}
        </Notice>
      ) : null}
      <div style={E.row}>
        <Button onClick={() => void save()} disabled={!ready || saveState.pending} testId="save-recipe-submit">
          {saveState.pending ? "Saving" : "Save"}
        </Button>
        <Button onClick={p.onCancel} testId="save-recipe-cancel">
          Cancel
        </Button>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------------------------------------------- editor

const preStyle = {
  margin: 0,
  padding: 6,
  fontFamily: "inherit",
  fontSize: 11,
  whiteSpace: "pre-wrap",
  overflow: "auto",
  maxHeight: 180,
  background: C.bg,
  borderRadius: 4,
} as const;

export function EditEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const projectId = useGraphStore((s) => s.projectId);
  const graphNodes = useGraphStore((s) => s.nodes);
  const graphEdges = useGraphStore((s) => s.edges);
  const draftEngine = useMakeStore((s) => s.entries[nodeId]?.draft.engineId ?? null);
  const [recipes, setRecipes] = useState<RecipeItem[] | null>(null);

  const [load, loadState] = useAction(async () => {
    const all = (await listRecipes(projectId)).filter((r) => EDIT_RECIPE_KINDS.includes(r.kind));
    setRecipes(all);
    return all;
  }, errorMessage);
  useEffect(() => {
    void load();
  }, [load, projectId]);

  const recipeId = asString(params.recipe);
  const [text, setText] = useState(() => asString(params.prompt));
  const task = asString(params.task);
  const shotClass = asString(params.shot_class);
  const tokens = tokensOf(params);
  const tokensKey = JSON.stringify(tokens);
  const recipe = recipes?.find((r) => r.id === recipeId) ?? null;
  const source = promptSource(params);

  // The assembled text of the picked recipe. A recipe change fetches at once; a token edit waits a moment so typing is
  // one request. A reply for another recipe or an older token set is dropped.
  const [assembled, setAssembled] = useState<{ recipe: string; value: AssembledRecipe } | null>(null);
  const [assembleError, setAssembleError] = useState<{ recipe: string; message: string } | null>(null);
  const fetchedFor = useRef("");
  useEffect(() => {
    if (recipeId === "") {
      fetchedFor.current = "";
      setAssembled(null);
      setAssembleError(null);
      return undefined;
    }
    let alive = true;
    const delay = fetchedFor.current === recipeId ? ASSEMBLE_DEBOUNCE_MS : 0;
    fetchedFor.current = recipeId;
    const timer = window.setTimeout(() => {
      assembleRecipe(recipeId, projectId, JSON.parse(tokensKey) as Record<string, string>).then(
        (value) => {
          if (!alive) return;
          setAssembled({ recipe: recipeId, value });
          setAssembleError(null);
        },
        (err: unknown) => {
          if (!alive) return;
          setAssembled(null);
          setAssembleError({ recipe: recipeId, message: errorMessage(err) });
        },
      );
    }, delay);
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [recipeId, projectId, tokensKey]);
  const current = assembled?.recipe === recipeId ? assembled.value : null;
  const currentError = assembleError?.recipe === recipeId ? assembleError.message : null;

  // Every token the picked recipe has: the open ones first, then the ones it ships a default for, then any the node
  // already holds. Remembered per recipe, so a field does not vanish while the artist types into it.
  const seen = useRef<{ recipe: string; list: string[] }>({ recipe: "", list: [] });
  if (seen.current.recipe !== recipeId) seen.current = { recipe: recipeId, list: [] };
  if (recipeId !== "") {
    for (const t of [...(recipe?.tokens ?? []), ...(current?.unfilledTokens ?? []), ...Object.keys(recipe?.overrides ?? {}), ...Object.keys(tokens)]) {
      if (!seen.current.list.includes(t)) seen.current.list.push(t);
    }
  }
  const tokenList = seen.current.list;
  const openTokens = new Set(current ? current.unfilledTokens : (recipe?.tokens ?? []));

  const applyRecipe = (id: string, list: readonly RecipeItem[] | null): void => {
    set({ recipe: id || undefined, ...(id !== recipeId ? { tokens: undefined } : {}) });
    const chosen = list?.find((r) => r.id === id);
    if (!chosen?.engine) return;
    const make = useMakeStore.getState();
    if (!make.entry(nodeId).draft.engineId) make.setDraft(nodeId, { engineId: chosen.engine });
  };
  const pickRecipe = (id: string): void => applyRecipe(id, recipes);

  const setToken = (token: string, value: string): void => {
    const next = { ...tokens };
    if (value.trim() === "") delete next[token];
    else next[token] = value;
    set({ tokens: Object.keys(next).length > 0 ? next : undefined });
  };

  const writePrompt = (value: string): void => {
    // whitespace alone is no prompt: the recipe (or nothing) stays the source
    setText(value);
    set({ prompt: value.trim() === "" ? undefined : value });
  };

  const [modal, setModal] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savedNote, setSavedNote] = useState<string | null>(null);
  const grouped = groupRecipes(recipes ?? []);
  const optionFor = (r: RecipeItem): ReactElement => (
    <option key={r.id} value={r.id}>
      {recipeLabel(r)}
    </option>
  );
  const maskClasses = connectedMaskClasses(graphNodes, graphEdges, nodeId);
  const explicitPrompt = asString(params.prompt);

  const onSaved = async (saved: SavedRecipe): Promise<void> => {
    setSaving(false);
    setSavedNote(`saved ${saved.id} (${saved.origin})`);
    applyRecipe(saved.id, (await load()) ?? recipes);
  };

  return (
    <EditorFrame kind="edit" nodeId={nodeId} title="Edit">
      <Section title="Recipe" testId="edit-recipe-section">
        <Field label="recipe">
          <select style={E.wide} value={recipeId} data-testid="edit-recipe" aria-label="recipe" onChange={(e) => pickRecipe(e.target.value)}>
            <option value="">{recipes === null ? (loadState.pending ? "loading recipes" : "no recipes") : "no recipe"}</option>
            {recipeId && !recipe ? <option value={recipeId}>{recipeId} (not on this daemon)</option> : null}
            {grouped.ungrouped.map(optionFor)}
            {grouped.groups.map((g) => (
              <optgroup key={g.name} label={g.name}>
                {g.items.map(optionFor)}
              </optgroup>
            ))}
          </select>
        </Field>
        {recipe ? (
          <div style={E.row} data-testid="edit-recipe-classes">
            <span style={E.label}>mask</span>
            {recipe.maskClasses.length > 0 ? (
              recipe.maskClasses.map((c) => (
                <Chip key={c} tone="info">
                  {c}
                </Chip>
              ))
            ) : (
              <Chip tone="muted" title="the recipe edits the whole frame; the gates still hold every locked id">
                whole frame
              </Chip>
            )}
            {recipe.region === "mask_bbox" ? <Chip tone="muted">region: mask box</Chip> : null}
            {recipe.shotClass ? <Chip tone="muted">recipe shot: {recipe.shotClass}</Chip> : null}
          </div>
        ) : null}
        {recipeId ? (
          <details data-testid="edit-assembled" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <summary style={{ cursor: "pointer", color: C.muted }}>
              Assembled prompt{" "}
              {current ? (
                <Chip tone={current.unfilled > 0 ? "advisory" : "muted"} testId="edit-unfilled" title="[TOKEN]s still open in the text; the artist fills them below or in the Project Sheet">
                  {current.unfilled} unfilled
                </Chip>
              ) : null}
            </summary>
            {current ? (
              <>
                <pre style={preStyle} data-testid="edit-assembled-text">
                  {current.prompt}
                </pre>
                <span style={E.label}>negative prompt</span>
                <pre style={preStyle} data-testid="edit-assembled-negative">
                  {current.negativePrompt}
                </pre>
              </>
            ) : (
              <span style={E.note}>{currentError ? "not available" : "assembling the prompt"}</span>
            )}
          </details>
        ) : null}
        {currentError ? (
          <Notice tone="error" testId="edit-assemble-error">
            assembled prompt: {currentError}
          </Notice>
        ) : null}
        {tokenList.length > 0 ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }} data-testid="edit-tokens">
            {tokenList.map((token) => {
              const slug = tokenSlug(token);
              const open = openTokens.has(token);
              return (
                <Field key={token} label={token}>
                  <input
                    style={E.wide}
                    value={tokens[token] ?? ""}
                    placeholder={recipe?.overrides[token] ?? (open ? "required" : "")}
                    aria-label={token}
                    data-testid={`edit-token-${slug}`}
                    onChange={(e) => setToken(token, e.target.value)}
                  />
                  {open ? (
                    <Chip tone="advisory" testId={`edit-token-open-${slug}`} title="no default and the Project Sheet leaves it empty">
                      open
                    </Chip>
                  ) : null}
                </Field>
              );
            })}
          </div>
        ) : null}
      </Section>

      <Section title="Prompt" testId="edit-prompt-section">
        <textarea
          style={{ ...E.wide, minHeight: 72, resize: "vertical" }}
          value={text}
          placeholder={recipe ? "leave empty to use the recipe's change list" : "what changes (a change list beats a 'keep everything' paragraph)"}
          aria-label="prompt"
          data-testid="edit-prompt"
          onChange={(e) => writePrompt(e.target.value)}
        />
        <span style={E.note} data-testid="edit-prompt-source" data-source={source}>
          prompt source: {source === "none" ? "none (pick a recipe or type a prompt)" : source}
        </span>
        <div style={E.row}>
          <Button
            onClick={() => current && writePrompt(current.prompt)}
            disabled={!current}
            title={current ? "copy the assembled text into the prompt so it can be edited; the prompt then wins over the recipe" : "pick a recipe first"}
            testId="edit-start-from"
          >
            Start from this
          </Button>
          <Button onClick={() => setModal(true)} title="edit the prompt in a larger box" testId="edit-expand">
            Expand
          </Button>
          <Button
            onClick={() => {
              setSavedNote(null);
              setSaving(true);
            }}
            disabled={saving}
            title="keep this prompt as a recipe in the studio or project folder"
            testId="edit-save-recipe"
          >
            Save as recipe…
          </Button>
        </div>
        {savedNote ? (
          <Notice testId="edit-saved">{savedNote}</Notice>
        ) : null}
        {saving ? (
          <SaveRecipeDialog
            projectId={projectId}
            engine={draftEngine}
            prompt={explicitPrompt.trim() !== "" ? explicitPrompt : (current?.prompt ?? "")}
            negativePrompt={current?.negativePrompt ?? ""}
            task={task}
            shotClass={shotClass}
            classes={maskClasses}
            groups={grouped.groups.map((g) => g.name)}
            onSaved={(saved) => void onSaved(saved)}
            onCancel={() => setSaving(false)}
          />
        ) : null}
      </Section>

      <Section title="Shot" testId="edit-shot-section">
        <div style={E.row}>
          <Field label="task">
            <select value={task} data-testid="edit-task" aria-label="task" style={E.input} onChange={(e) => set({ task: e.target.value || undefined })}>
              <option value="">any</option>
              {EDIT_TASKS.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </Field>
          <Field label="shot class">
            <select value={shotClass} data-testid="edit-shot-class" aria-label="shot class" style={E.input} onChange={(e) => set({ shot_class: e.target.value || undefined })}>
              <option value="">{recipe?.shotClass ? `recipe (${recipe.shotClass})` : "unset"}</option>
              {SHOT_CLASSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <span style={E.note}>the task narrows the engines in the Make bar; the router checks the shot class (AI re-photography is for close shots)</span>
      </Section>

      {loadState.error ? (
        <Notice tone="error" testId="edit-error">
          recipes: {loadState.error}
        </Notice>
      ) : null}

      {modal ? <PromptModal value={text} onChange={writePrompt} onClose={() => setModal(false)} title="Edit prompt" placeholder="what changes (a change list beats a 'keep everything' paragraph)" /> : null}
    </EditorFrame>
  );
}
