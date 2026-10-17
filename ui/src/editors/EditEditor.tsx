// Edit node editor (T69): what the engine is asked to change. A recipe (from `GET /recipes`, the still recipes only)
// carries the change-list prompt, the engine and the classes the mask lets the engine touch; a typed prompt overrides
// the recipe's (decision 33: explicit > recipe; the daemon composes or fills the recipe template at the quote).
// `task` narrows the Make bar's engines to those that list it; `shot_class` is what the render-it router checks.
// Node params: `recipe` (recipe id), `prompt` (explicit prompt text), `task`, `shot_class`. The Make bar sends all
// four with the quote; the engine itself is the Make bar's (picking a recipe fills it when the bar has none).

import { type ReactElement, useEffect, useState } from "react";
import { useMakeStore } from "../makebar/makeStore";
import { errorMessage } from "./api";
import { listRecipes, type RecipeItem } from "./nodeApi";
import { asString, useNodeEditor } from "./params";
import { E } from "./theme";
import { Chip, EditorFrame, Field, Notice, Section, useAction } from "./widgets";

/** The image tasks an Edit node can make (the Make bar's `MAKE_MODES.Edit.tasks`). */
export const EDIT_TASKS = ["relight", "people", "vegetation", "sky", "rephotograph_close"] as const;
export const SHOT_CLASSES = ["wide", "medium", "close"] as const;
/** Recipe kinds an Edit node runs: the still recipes (a `motion_croppair` recipe belongs to CropPair / Motion). */
export const EDIT_RECIPE_KINDS: readonly string[] = ["still_edit", "still_rephotograph"];

export type PromptSource = "explicit" | "recipe" | "none";

/** Which prompt the daemon will use for these params (decision 33). Pure. */
export function promptSource(params: Record<string, unknown>): PromptSource {
  if (asString(params.prompt).trim() !== "") return "explicit";
  if (asString(params.recipe) !== "") return "recipe";
  return "none";
}

export function EditEditor({ nodeId }: { nodeId: string }): ReactElement {
  const { params, set } = useNodeEditor(nodeId);
  const [recipes, setRecipes] = useState<RecipeItem[] | null>(null);

  const [load, loadState] = useAction(async () => {
    const all = await listRecipes();
    setRecipes(all.filter((r) => EDIT_RECIPE_KINDS.includes(r.kind)));
  }, errorMessage);
  useEffect(() => {
    void load();
  }, [load]);

  const recipeId = asString(params.recipe);
  const [text, setText] = useState(() => asString(params.prompt));
  const task = asString(params.task);
  const shotClass = asString(params.shot_class);
  const recipe = recipes?.find((r) => r.id === recipeId) ?? null;
  const source = promptSource(params);

  const pickRecipe = (id: string): void => {
    set({ recipe: id || undefined });
    const chosen = recipes?.find((r) => r.id === id);
    if (!chosen?.engine) return;
    const make = useMakeStore.getState();
    if (!make.entry(nodeId).draft.engineId) make.setDraft(nodeId, { engineId: chosen.engine });
  };

  return (
    <EditorFrame kind="edit" nodeId={nodeId} title="Edit">
      <Section title="Recipe" testId="edit-recipe-section">
        <Field label="recipe">
          <select style={E.wide} value={recipeId} data-testid="edit-recipe" aria-label="recipe" onChange={(e) => pickRecipe(e.target.value)}>
            <option value="">{recipes === null ? (loadState.pending ? "loading recipes" : "no recipes") : "no recipe"}</option>
            {recipeId && !recipe ? <option value={recipeId}>{recipeId} (not on this daemon)</option> : null}
            {(recipes ?? []).map((r) => (
              <option key={r.id} value={r.id}>
                {r.title} · {r.engine ?? "any engine"}
              </option>
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
      </Section>

      <Section title="Prompt" testId="edit-prompt-section">
        <textarea
          style={{ ...E.wide, minHeight: 72, resize: "vertical" }}
          value={text}
          placeholder={recipe ? "leave empty to use the recipe's change list" : "what changes (a change list beats a 'keep everything' paragraph)"}
          aria-label="prompt"
          data-testid="edit-prompt"
          onChange={(e) => {
            // whitespace alone is no prompt: the recipe (or nothing) stays the source
            setText(e.target.value);
            set({ prompt: e.target.value.trim() === "" ? undefined : e.target.value });
          }}
        />
        <span style={E.note} data-testid="edit-prompt-source" data-source={source}>
          prompt source: {source === "none" ? "none (pick a recipe or type a prompt)" : source}
        </span>
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
    </EditorFrame>
  );
}
