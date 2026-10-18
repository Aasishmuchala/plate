"""Project settings routes (router plan T88 and T120): the Project Sheet, default engines, auto carry-over,
policy, and the video route preference.

- `GET /projects/{id}/settings` answers `{project_sheet, sheet_fields, default_engine, auto_carry_over, policy,
  preferred_video_provider, video_route_pins}`. `project_sheet` is the 14 Project Sheet fields as the recipes read
  them (`recipes.ProjectSheet`, snake_case, defaults filled in); `sheet_fields` is `[{key, lensbook_key, default}]`
  from `recipes/project_sheet.yaml`, in form order, so the UI shows a placeholder per field; `default_engine`
  is `{task: engine_id}`; `auto_carry_over` defaults to true; `policy` is the project's own rules
  (`settings.policy`: `{locked_classes, lint_threshold}`, read-only here, `{}` when unset).
  `preferred_video_provider` (default `runway`) and `video_route_pins` (`{engine_id: route_id}`, default `{}`)
  are the project's overrides of the video routing rule (Wave 11 decision 63; in precedence order: the quote
  body's `route_id`, then `video_route_pins`, then `preferred_video_provider`, where `none` keeps manifest order).
- `PUT /projects/{id}/settings {project_sheet?, default_engine?, auto_carry_over?, preferred_video_provider?,
  video_route_pins?}` replaces each key it carries and leaves the others alone. A sheet goes through
  `ProjectSheet.from_dict`, so an unknown key, or a value that is not text, is 422 and a field left out is back to
  its default (it is a PUT: send the whole form). Every `default_engine` value must be an engine manifest id
  (422), also `fake` while the app's engine is a stub (the Make bar offers it then). `preferred_video_provider`
  must be `none` or a provider an active route of a loaded manifest names (else 422 with `known_providers`);
  every `video_route_pins` key must be an engine manifest id and its value one of that manifest's active route
  ids (else 422 with `known_routes`); an empty map clears the pins. The write happens under the project lock on
  a fresh load of `project.json`, so a graph save in flight is not overwritten; the answer is the GET shape.
- A human is a request carrying `X-Plate-Client: ui` (decision 37): only those reset the unattended clock
  (`Pipeline.touch_human`).

The values live in `project.json` under `settings` (`project_sheet`, `default_engine`, `auto_carry_over`,
`preferred_video_provider`, `video_route_pins`), where the pipeline, the composer, previs, the motion runs and
the revision code read them.
"""

from __future__ import annotations

import re
from dataclasses import fields
from functools import lru_cache
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, ConfigDict, StrictBool

from plate_engines.prefer import PREFERRED_VIDEO_PROVIDER

from .. import recipes
from ..lock import ProjectLocked
from ..projects import Project, ProjectError
from ..recipes import ProjectSheet, RecipeError
from . import API, ApiError, Ctx, ctx, require_token
from .engines import FAKE_ENGINE_ID
from .projects import from_ui
from .tierc import project_or_404

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])

LOCK_TIMEOUT_S = 5.0
TASK_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")  # the router's task names: relight, people, sky, ...


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_sheet: dict[str, Any] | None = None
    default_engine: dict[str, str] | None = None
    auto_carry_over: StrictBool | None = None
    preferred_video_provider: str | None = None
    video_route_pins: dict[str, str] | None = None


@lru_cache(maxsize=1)
def sheet_fields() -> tuple[dict[str, str], ...]:
    """`[{key, lensbook_key, default}]` from `recipes/project_sheet.yaml`, in the dataclass's field order."""
    doc = yaml.safe_load((recipes.RECIPES_DIR / "project_sheet.yaml").read_text(encoding="utf-8"))
    by_key = {f["key"]: f for f in doc["fields"]}
    return tuple(
        {"key": f.name, "lensbook_key": by_key[f.name]["lensbook_key"], "default": by_key[f.name]["default"]}
        for f in fields(ProjectSheet)
    )


def _stored(project: Project) -> dict[str, Any]:
    stored = project.doc.get("settings")
    return stored if isinstance(stored, dict) else {}


def settings_view(project: Project) -> dict[str, Any]:
    """The GET shape for one project (a pure read of `project.json`)."""
    stored = _stored(project)
    try:
        sheet = ProjectSheet.from_dict(stored.get("project_sheet")).as_dict()
    except RecipeError as e:  # a hand-edited project.json; a PUT replaces it
        raise ApiError(
            422, {"error": "bad_project_sheet", "errors": [str(e)], "project_id": project.project_id}
        ) from e
    engines = stored.get("default_engine")
    carry = stored.get("auto_carry_over")
    policy = stored.get("policy")
    provider = stored.get("preferred_video_provider")
    provider = provider if isinstance(provider, str) and provider else PREFERRED_VIDEO_PROVIDER
    pins = stored.get("video_route_pins")
    return {
        "project_sheet": sheet,
        "sheet_fields": [dict(f) for f in sheet_fields()],
        "default_engine": (
            {k: v for k, v in engines.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(engines, dict)
            else {}
        ),
        "auto_carry_over": carry if isinstance(carry, bool) else True,
        "policy": dict(policy) if isinstance(policy, dict) else {},
        "preferred_video_provider": provider,
        "video_route_pins": (
            {k: v for k, v in pins.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(pins, dict)
            else {}
        ),
    }


def _known_engines(c: Ctx) -> set[str]:
    known = set(c.pipeline.manifests)
    if getattr(c.engine, "stub", False):
        known.add(FAKE_ENGINE_ID)
    return known


def _check_default_engine(c: Ctx, mapping: dict[str, str]) -> dict[str, str]:
    known = _known_engines(c)
    errors = []
    for task, engine_id in mapping.items():
        if not TASK_KEY.match(task):
            errors.append(f"default_engine: task {task!r} must be a lower-case name like 'relight'")
        if engine_id not in known:
            errors.append(f"default_engine[{task!r}]: {engine_id!r} is not an engine manifest id")
    if errors:
        raise ApiError(422, {"errors": errors, "known_engines": sorted(known)})
    return dict(mapping)


def _known_providers(c: Ctx) -> list[str]:
    return sorted(
        {r.provider for m in c.pipeline.manifests.values() for r in m.routes if r.status == "active"}
    )


def _check_provider(c: Ctx, value: str) -> str:
    known = _known_providers(c)
    if value != "none" and value not in known:
        raise ApiError(
            422,
            {
                "errors": [
                    f"preferred_video_provider: {value!r} is neither 'none' nor a provider of an active route"
                ],
                "known_providers": known,
            },
        )
    return value


def _check_route_pins(c: Ctx, mapping: dict[str, str]) -> dict[str, str]:
    manifests = c.pipeline.manifests
    errors: list[str] = []
    routes: dict[str, list[str]] = {}
    for engine_id, route_id in mapping.items():
        m = manifests.get(engine_id)
        if m is None:
            errors.append(f"video_route_pins: {engine_id!r} is not an engine manifest id")
            continue
        routes[engine_id] = [r.route_id for r in m.routes if r.status == "active"]
        if route_id not in routes[engine_id]:
            errors.append(
                f"video_route_pins[{engine_id!r}]: {route_id!r} is not one of its routes {routes[engine_id]}"
            )
    if errors:
        raise ApiError(422, {"errors": errors, "known_routes": routes})
    return dict(mapping)


@router.get("/projects/{project_id}/settings")
def get_settings(request: Request, project_id: str) -> dict[str, Any]:
    return settings_view(project_or_404(ctx(request), project_id))


@router.put("/projects/{project_id}/settings")
def put_settings(request: Request, project_id: str, body: Annotated[SettingsBody, Body()]) -> dict[str, Any]:
    c = ctx(request)
    project = project_or_404(c, project_id)
    changes: dict[str, Any] = {}
    if body.project_sheet is not None:
        try:
            changes["project_sheet"] = ProjectSheet.from_dict(body.project_sheet).as_dict()
        except RecipeError as e:
            raise ApiError(422, {"errors": [str(e)]}) from e
    if body.default_engine is not None:
        changes["default_engine"] = _check_default_engine(c, body.default_engine)
    if body.auto_carry_over is not None:
        changes["auto_carry_over"] = body.auto_carry_over
    if body.preferred_video_provider is not None:
        changes["preferred_video_provider"] = _check_provider(c, body.preferred_video_provider)
    if body.video_route_pins is not None:
        changes["video_route_pins"] = _check_route_pins(c, body.video_route_pins)
    try:
        with project.lock(timeout_s=LOCK_TIMEOUT_S):
            project = Project.load(project.root)  # under the lock: never write back a stale graph
            if changes:
                settings = project.doc.get("settings")
                project.doc["settings"] = settings = settings if isinstance(settings, dict) else {}
                settings.update(changes)
                project.save()
    except ProjectLocked as e:
        raise ApiError(409, {"error": "project_locked", "project_id": project_id, "detail": str(e)}) from e
    except ProjectError as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    if from_ui(request):
        c.pipeline.touch_human()
    return settings_view(project)
