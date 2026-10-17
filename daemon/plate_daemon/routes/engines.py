"""Engine and recipe catalogue routes: what the Make bar can offer (router plan T61).

- `GET /engines[?project_id=]` lists every engine manifest (`c.pipeline.manifests`, sorted by id) in one fixed
  shape: `{id, name, kind, allowed_tasks, model_id, version, routes, params, presets, price, sizes, flags}`.
  A route is `{route_id, provider, region, custody_grade, custody_status, effective_grade, status,
  upload_visibility}`, where `effective_grade` is `custody.effective_grade` (a pending route counts as D, a
  public upload is D) and is what the quote acts on, so the UI never has to repeat that rule. `presets` is
  `["default", ...]` plus the id of every runnable recipe that names the engine; `sizes` is
  `{legal: [{w, h, label}], auto, aspects}`, or null when the manifest declares none (a cloud engine without
  sizes is refused at the quote, `no_sizes`). With `project_id`, every entry gains `allowed`: true when some
  active route's effective grade is within what that project's custody allows (`Pipeline.allowed_grades`; the
  packet's own policy can only narrow it further, which the quote decides). The daemon's own fake engine is
  appended as `id: "fake"` while the app's engine is a stub (`/health` says `stub_engine`).
- `GET /recipes` lists the runnable recipes (`recipes.list_runnable`) as `{id, title, kind, engine,
  shot_class, library, mask, params}`; a recipe file that fails to load is logged and left out.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

from .. import recipes as recipes_mod
from ..custody import Grade, effective_grade
from . import API, Ctx, ctx, require_token
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
        }
    ],
    "params": [],
    "presets": [DEFAULT_PRESET],
    "price": {"usd": 0, "model": "flat", "status": "confirmed"},
    "sizes": None,
    "flags": [],
}


def _runnable() -> list[Any]:
    out = []
    for rid in recipes_mod.list_runnable():
        try:
            out.append(recipes_mod.load_runnable(rid))
        except recipes_mod.RecipeError as e:
            log.warning("recipe %s left out of /recipes: %s", rid, e)
    return out


def _route(r: Any) -> dict[str, Any]:
    return {
        "route_id": r.route_id,
        "provider": r.provider,
        "region": r.region,
        "custody_grade": r.custody_grade,
        "custody_status": r.custody_status,
        "effective_grade": effective_grade(r).value,
        "status": r.status,
        "upload_visibility": r.upload_visibility,
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


def engine_view(m: Any, recipe_ids: list[str]) -> dict[str, Any]:
    """One manifest in the catalogue shape (see the module docstring)."""
    return {
        "id": m.id,
        "name": m.name,
        "kind": m.kind,
        "allowed_tasks": list(m.allowed_tasks),
        "model_id": m.model_id,
        "version": m.version,
        "routes": [_route(r) for r in m.routes],
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
    }


def _allowed(view: dict[str, Any], grades: set[Grade]) -> bool:
    return any(r["status"] == "active" and Grade(r["effective_grade"]) in grades for r in view["routes"])


def build_engines(c: Ctx, project_id: str | None = None) -> list[dict[str, Any]]:
    by_engine: dict[str, list[str]] = {}
    for rec in _runnable():
        by_engine.setdefault(rec.engine, []).append(rec.id)
    views = [
        engine_view(m, by_engine.get(m.id, []))
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
    if project_id is not None:
        project_or_404(c, project_id)
    return build_engines(c, project_id)


@router.get("/recipes")
def list_recipes() -> list[dict[str, Any]]:
    return [
        {
            "id": r.id,
            "title": r.title,
            "kind": r.kind,
            "engine": r.engine,
            "shot_class": r.shot_class,
            "library": r.library,
            "mask": dict(r.mask),
            "params": dict(r.params),
        }
        for r in _runnable()
    ]
