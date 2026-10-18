"""Engine and recipe catalogue routes: what the Make bar can offer (router plan T61, T83).

- `GET /engines[?project_id=]` lists every engine manifest (`c.pipeline.manifests`, sorted by id) in one fixed
  shape: `{id, name, kind, allowed_tasks, model_id, version, routes, params, presets, price, sizes, flags,
  capabilities}`. A route is `{route_id, provider, region, custody_grade, custody_status, effective_grade, status,
  upload_visibility, health, price, preferred}`, where `effective_grade` is `custody.effective_grade` (a pending
  route counts as D, a public upload is D) and is what the quote acts on, so the UI never has to repeat that rule,
  and `health` is `ok | degraded | down | suspended` from `c.pipeline.health_board()`
  (`plate_engines.health.route_health`: a suspended engine reads suspended on every route, a route with no reading
  is down). `price` is the route's own `{usd, model, status, minimum_usd}` when the vendor's catalogue is held for
  that route (Wave 11: the Runway routes), else null (the quote then prices from the engine's `price`).
  `preferred` is true on a video engine's route whose provider is the preferred one (`prefer.PREFERRED_VIDEO_PROVIDER`,
  or with `project_id` that project's `settings.preferred_video_provider`; `"none"` makes every route false), so
  the Make bar can show which route a quote will try first. `capabilities` is
  `{largest_size: "WxH" | null, image_tail, reference_images_max, elements, price_unit, runnable, unverified}`:
  `image_tail` is true when the manifest takes an `image_tail` or `end_image` input, `reference_images_max` is the
  `reference_images` input's `max_items` (0 when the manifest declares none), `runnable` is true when the manifest
  names a vendor model (`model_id`, or a route's `model_slug`; image_common refuses a submit without one) and
  `unverified` mirrors the `unverified_p0` flag. `presets` is `["default", ...]` plus the id of every runnable
  recipe that names the engine; `sizes` is `{legal: [{w, h, label}], auto, aspects}`, or null when the manifest
  declares none (a cloud engine without sizes is refused at the quote, `no_sizes`). With `project_id`, every entry
  gains `allowed`: true when some active route's effective grade is within what that project's custody allows
  (`Pipeline.allowed_grades`; the packet's own policy can only narrow it further, which the quote decides), and
  the project's own recipes join `presets`. The daemon's own fake engine is appended as `id: "fake"` while the
  app's engine is a stub (`/health` says `stub_engine`).
- `GET /engines/cards[?project_id=&task=&shot_class=]`: the Wilson-bounded engine cards folded from the project's
  ledger (`plate_engines.cards.from_ledger`), each `Card.to_dict()` plus `steering` (n has reached the router's
  threshold). No `project_id` answers `[]`. A ledger record whose `gate_report` is a path carries its verdict in
  `data.verdict`; that verdict is what the card counts.
- `GET /recipes[?project_id=&kind=]` lists the runnable recipes (`recipes.runnable_entries`: shipped, studio and,
  with `project_id`, that project's own) as `{id, title, kind, engine, shot_class, library, mask, params, origin,
  group, thumbnail, task, tokens, overrides}`. `tokens` are the `[TOKEN]` texts still open when the recipe is
  assembled against an empty Project Sheet with its own `overrides` (none for a recipe that ships a default for
  every token); `overrides` are those defaults, so an editor can offer every token, not only the open ones. A
  recipe file that fails to load is logged and left out.
- `POST /recipes {scope: studio|project, project_id?, overwrite?, recipe}` saves a schema-1 recipe
  (`recipes.save_runnable`): 201 `{id, path, origin}`, 409 `{"error": "recipe_exists"}`, 422 for a doc that does
  not fit the schema or an engine that is not a manifest.
- `GET /recipes/{id}/assemble[?project_id=&tokens=<urlencoded JSON object>]` is the text the recipe would send: the
  recipe's prompt filled from its own defaults, the caller's `tokens` and the project's Project Sheet, as
  `{prompt, negative_prompt, unfilled, unfilled_tokens, prompt_source: "recipe"}`.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from plate_engines.cards import from_ledger
from plate_engines.health import route_health
from plate_engines.prefer import PREFERRED_VIDEO_PROVIDER, VIDEO_KINDS

from .. import recipes as recipes_mod
from ..custody import Grade, effective_grade
from ..ledger import Ledger
from . import API, ApiError, Ctx, ctx, require_token
from .tierc import project_or_404

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])

log = logging.getLogger("plate_daemon")

DEFAULT_PRESET = "default"
FAKE_ENGINE_ID = "fake"
FAKE_ENGINE: dict[str, Any] = {
    "id": FAKE_ENGINE_ID,
    "name": "Fake (echo)",
    "kind": "image_edit",
    "allowed_tasks": ["relight", "people", "vegetation", "sky", "rephotograph_close"],
    "model_id": None,
    "version": "fake",
    "routes": [
        {
            "route_id": "local",
            "provider": "local",
            "custody_grade": "C0",
            "custody_status": "confirmed",
            "effective_grade": "C0",
            "status": "active",
            "upload_visibility": "local",
            "health": "ok",
            "price": None,
            "preferred": False,
        }
    ],
    "params": [],
    "presets": [DEFAULT_PRESET],
    "price": {"usd": 0, "model": "flat", "status": "confirmed"},
    "sizes": None,
    "flags": [],
    "capabilities": {
        "largest_size": None,
        "image_tail": False,
        "reference_images_max": 0,
        "elements": False,
        "price_unit": "flat",
        "runnable": True,
        "unverified": False,
    },
}


def _project_dir(c: Ctx, project_id: str | None) -> Path | None:
    """The project's folder after a 404/422 check (None without a `project_id`)."""
    if project_id is None:
        return None
    project_or_404(c, project_id)
    return c.pipeline.project_dir(project_id)


def _runnable(project_dir: Path | None = None) -> list[Any]:
    return recipes_mod.runnable_entries(project_dir)


def _route_price(r: Any) -> dict[str, Any] | None:
    """The route's own price (None when the route is priced by its engine's manifest price)."""
    if r.price is None:
        return None
    return {
        "usd": r.price.usd,
        "model": r.price.model,
        "status": r.price.status,
        "minimum_usd": r.price.minimum_usd,
    }


def _route(r: Any, health: str, m: Any, prefer_provider: str | None) -> dict[str, Any]:
    return {
        "route_id": r.route_id,
        "provider": r.provider,
        "region": r.region,
        "custody_grade": r.custody_grade,
        "custody_status": r.custody_status,
        "effective_grade": effective_grade(r).value,
        "status": r.status,
        "upload_visibility": r.upload_visibility,
        "health": health,
        "price": _route_price(r),
        "preferred": bool(
            m.kind in VIDEO_KINDS and prefer_provider is not None and r.provider == prefer_provider
        ),
    }


def _param(p: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"name": p.name, "type": p.type}
    if p.enum is not None:
        out["enum"] = list(p.enum)
    if p.range is not None:
        out["range"] = [int(x) for x in p.range] if p.type == "int" else list(p.range)
    if p.default is not None:
        out["default"] = p.default
    out["advanced"] = bool(p.advanced)
    return out


def _sizes(m: Any) -> dict[str, Any] | None:
    if m.sizes is None:
        return None
    return {
        "legal": [{"w": s.w, "h": s.h, "label": s.label} for s in m.sizes.legal],
        "auto": bool(m.sizes.auto),
        "aspects": list(m.sizes.aspects),
    }


def _largest_size(m: Any) -> str | None:
    """ "WxH" of the biggest size the manifest declares: the largest legal entry, else the biggest box of the
    arbitrary envelope (a square when the envelope allows one). None for an auto-only or size-less manifest."""
    sizes = m.sizes
    if sizes is None:
        return None
    if sizes.legal:
        best = max(sizes.legal, key=lambda s: s.w * s.h)
        return f"{best.w}x{best.h}"
    a = sizes.arbitrary
    if a is None:
        return None
    ratio = min(
        max(1.0, a.aspect_min), a.aspect_max
    )  # width over height, the nearest to square the envelope allows
    if ratio >= 1:
        return f"{a.max_side}x{round(a.max_side / ratio)}"
    return f"{round(a.max_side * ratio)}x{a.max_side}"


def _capabilities(m: Any) -> dict[str, Any]:
    inputs = {f.name: f for f in m.inputs}
    refs = inputs.get("reference_images")
    return {
        "largest_size": _largest_size(m),
        "image_tail": bool({"image_tail", "end_image"} & set(inputs)),
        "reference_images_max": int(refs.max_items or 0) if refs is not None else 0,
        "elements": "elements" in inputs,
        "price_unit": m.price.model,
        "runnable": bool(m.model_id) or any(r.model_slug for r in m.routes),
        "unverified": "unverified_p0" in m.flags,
    }


def engine_view(
    m: Any,
    recipe_ids: list[str],
    health: Mapping[str, Any] | None = None,
    prefer_provider: str | None = PREFERRED_VIDEO_PROVIDER,
) -> dict[str, Any]:
    """One manifest in the catalogue shape (see the module docstring). `health` is the pipeline's health board;
    `prefer_provider` is the provider whose routes read `preferred` on a video engine (None: none do)."""
    board = health if health is not None else {}
    return {
        "id": m.id,
        "name": m.name,
        "kind": m.kind,
        "allowed_tasks": list(m.allowed_tasks),
        "model_id": m.model_id,
        "version": m.version,
        "routes": [
            _route(r, route_health(board, m.id, r.route_id, r.provider)[0], m, prefer_provider)
            for r in m.routes
        ],
        "params": [_param(p) for p in m.params],
        "presets": [DEFAULT_PRESET, *recipe_ids],
        "price": {
            "usd": m.price.usd,
            "model": m.price.model,
            "status": m.price.status,
            "by": dict(m.price.by),
        },
        "sizes": _sizes(m),
        "flags": list(m.flags),
        "capabilities": _capabilities(m),
    }


def _allowed(view: dict[str, Any], grades: set[Grade]) -> bool:
    return any(r["status"] == "active" and Grade(r["effective_grade"]) in grades for r in view["routes"])


def _prefer_provider(c: Ctx, project_id: str | None) -> str | None:
    """The provider whose video routes read `preferred`: the constant, or the project's own
    `settings.preferred_video_provider` (`"none"` = manifest order = nobody preferred)."""
    if project_id is None:
        return PREFERRED_VIDEO_PROVIDER
    project = c.pipeline.load_project(project_id)
    stored = ((project.doc if project else {}).get("settings") or {}).get("preferred_video_provider")
    if not isinstance(stored, str) or not stored.strip():
        return PREFERRED_VIDEO_PROVIDER
    return None if stored.strip().lower() == "none" else stored.strip()


def build_engines(
    c: Ctx, project_id: str | None = None, project_dir: Path | None = None
) -> list[dict[str, Any]]:
    by_engine: dict[str, list[str]] = {}
    for rec in _runnable(project_dir):
        by_engine.setdefault(rec.engine, []).append(rec.id)
    health = c.pipeline.health_board()
    prefer_provider = _prefer_provider(c, project_id)
    views = [
        engine_view(m, by_engine.get(m.id, []), health, prefer_provider)
        for _id, m in sorted(c.pipeline.manifests.items(), key=lambda kv: kv[0])
    ]
    if getattr(c.engine, "stub", False):
        fake = copy.deepcopy(FAKE_ENGINE)
        fake["presets"] = [DEFAULT_PRESET, *by_engine.get(FAKE_ENGINE_ID, [])]
        views.append(fake)
    if project_id is not None:
        grades = c.pipeline.allowed_grades(project_id)
        for v in views:
            v["allowed"] = _allowed(v, grades)
    return views


@router.get("/engines")
def list_engines(request: Request, project_id: str | None = None) -> list[dict[str, Any]]:
    c = ctx(request)
    return build_engines(c, project_id, _project_dir(c, project_id))


def _verdicts(records: list[dict[str, Any]]) -> dict[str, Any]:
    """{gate_report path: {"verdict": v}} for the records that name their report by path (what the pipeline writes)
    and carry its verdict in `data.verdict`; `from_ledger` reads a path through this mapping."""
    out: dict[str, Any] = {}
    for rec in records:
        ref, data = rec.get("gate_report"), rec.get("data")
        if isinstance(ref, str) and isinstance(data, dict) and isinstance(data.get("verdict"), str):
            out[ref] = {"verdict": data["verdict"]}
    return out


@router.get("/engines/cards")
def engine_cards(
    request: Request,
    project_id: str | None = None,
    task: str | None = None,
    shot_class: str | None = None,
) -> list[dict[str, Any]]:
    c = ctx(request)
    project_dir = _project_dir(c, project_id)
    if project_dir is None:
        return []
    records = Ledger(project_dir).records()
    out = []
    for card in from_ledger(records, _verdicts(records)):
        if task is not None and card.task != task:
            continue
        if shot_class is not None and card.shot_class != shot_class:
            continue
        out.append({**card.to_dict(), "steering": card.steering()})
    return out


# ---------------------------------------------------------------------------- recipes


def recipe_view(r: Any) -> dict[str, Any]:
    open_tokens = recipes_mod.unfilled_tokens(recipes_mod.assemble_runnable(r, recipes_mod.ProjectSheet()))
    return {
        "id": r.id,
        "title": r.title,
        "kind": r.kind,
        "engine": r.engine,
        "shot_class": r.shot_class,
        "library": r.library,
        "mask": dict(r.mask),
        "params": dict(r.params),
        "origin": r.origin,
        "group": r.group,
        "thumbnail": r.thumbnail,
        "task": r.task,
        "tokens": open_tokens,
        "overrides": dict(r.overrides),
    }


@router.get("/recipes")
def list_recipes(
    request: Request, project_id: str | None = None, kind: str | None = None
) -> list[dict[str, Any]]:
    c = ctx(request)
    if kind is not None and kind not in recipes_mod.KINDS:
        raise ApiError(422, {"errors": [f"kind must be one of {list(recipes_mod.KINDS)}, got {kind!r}"]})
    rows = _runnable(_project_dir(c, project_id))
    return [recipe_view(r) for r in rows if kind is None or r.kind == kind]


class RecipeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["studio", "project"]
    project_id: str | None = None
    overwrite: bool = False
    recipe: dict[str, Any]


@router.post("/recipes", status_code=201)
def save_recipe(body: RecipeBody, request: Request) -> dict[str, Any]:
    c = ctx(request)
    if body.scope == "project" and not body.project_id:
        raise ApiError(422, {"errors": ["a project recipe needs project_id"]})
    project_dir = _project_dir(c, body.project_id)  # also lets a studio save see the project's own ids
    try:
        recipe = recipes_mod.parse_runnable(body.recipe, body.scope)
    except recipes_mod.RecipeError as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    if recipe.engine not in c.pipeline.manifests:
        raise ApiError(
            422,
            {
                "errors": [
                    f"engine {recipe.engine!r} is not an engine manifest; known: {sorted(c.pipeline.manifests)}"
                ]
            },
        )
    try:
        path = recipes_mod.save_runnable(body.recipe, body.scope, project_dir, overwrite=body.overwrite)
    except recipes_mod.RecipeExists as e:
        raise ApiError(409, {"error": "recipe_exists", "id": recipe.id, "errors": [str(e)]}) from e
    except recipes_mod.RecipeError as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    except OSError as e:
        raise ApiError(500, {"error": "recipe_not_saved", "errors": [f"{type(e).__name__}: {e}"]}) from e
    return {"id": recipe.id, "path": str(path), "origin": body.scope}


def _token_overrides(raw: str | None) -> dict[str, str]:
    if raw is None or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        raise ApiError(422, {"errors": [f"tokens must be a JSON object ({e})"]}) from e
    ok = isinstance(parsed, dict) and all(
        isinstance(k, str) and k.startswith("[") and k.endswith("]") and isinstance(v, str)
        for k, v in parsed.items()
    )
    if not ok:
        raise ApiError(422, {"errors": ['tokens must map literal "[TOKEN]" texts to text values']})
    return parsed


@router.get("/recipes/{recipe_id}/assemble")
def assemble_recipe(
    recipe_id: str, request: Request, project_id: str | None = None, tokens: str | None = None
) -> dict[str, Any]:
    c = ctx(request)
    overrides = _token_overrides(tokens)
    project_dir = _project_dir(c, project_id)
    if not recipes_mod.is_recipe_id(recipe_id):
        raise ApiError(404, {"error": "unknown_recipe", "recipe_id": recipe_id})
    try:
        recipe = recipes_mod.load_runnable(recipe_id, project_dir)
    except recipes_mod.RecipeError as e:
        raise ApiError(404, {"error": "unknown_recipe", "recipe_id": recipe_id, "errors": [str(e)]}) from e
    sheet = recipes_mod.ProjectSheet()
    if project_id is not None:
        project = c.pipeline.load_project(project_id)
        try:
            sheet = recipes_mod.ProjectSheet.from_dict(
                ((project.doc if project else {}).get("settings") or {}).get("project_sheet")
            )
        except recipes_mod.RecipeError as e:
            raise ApiError(422, {"error": "bad_project_sheet", "errors": [str(e)]}) from e
    prompt = recipes_mod.assemble_runnable(recipe, sheet, overrides)
    return {
        "prompt": prompt,
        "negative_prompt": recipe.negative_prompt or recipes_mod.DEFAULT_NEGATIVE_PROMPT,
        "unfilled": recipes_mod.unfilled_count(prompt),
        "unfilled_tokens": recipes_mod.unfilled_tokens(prompt),
        "prompt_source": "recipe",
    }
