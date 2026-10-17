"""Project routes: `GET /session`, `GET|POST /projects`, `GET|PUT /projects/{id}/graph`, and the node quote.

- `GET /projects` lists the head of every project.json (`{project_id, custody, created_utc, engine_pins,
  root, nodes}`); `POST /projects {project_id, custody: {nda, allowed_grades?}, engine_pins?, root?}` makes
  the folder (default `<home>/projects/<id>`) and registers it with the index: 201, or 200 when it fills in
  the custody of a project a packet created first (`custody.unset`), else 409.
- `PUT /projects/{id}/graph` is where the canvas rules are ENFORCED (`canvas_rules`, the mirror of
  `ui/src/canvasRules.ts`; the UI's checks are UX): an unknown node kind, a repeated id or any edge that
  breaks a rule answers `422 {"errors", "violations": [{edge|node, code, message}]}`, and a node newly
  marked approved without a `scrubLogId` answers SCRUB_REQUIRED. A good graph is written atomically under
  the project lock and announced on the bus as `graph.saved`.
- `POST /projects/{id}/nodes/{node}/quote {engine_id?, preset?, region_crop?, params?, route_id?, prompt?,
  packet_id?, priority?, attempt?, feather_px?}` turns a canvas node into a run in QUOTED
  (`Pipeline.quote_node`): `{run, quote, auto_cap_usd}`. A route the project's custody does not allow is
  403 `{"error": "custody", "exception": "CustodyViolation"}`; rule, mask and engine refusals are 422.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import __version__, canvas_rules
from ..config import check_project_id
from ..custody import CustodyViolation
from ..pipeline import PipelineError, project_head
from ..projects import Project, ProjectError, check_custody, check_graph
from . import API, ApiError, ctx, custody_error, pipeline_error, require_token
from .runs import run_view

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])


class ProjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    custody: dict[str, Any]
    engine_pins: dict[str, str] = Field(default_factory=dict)
    root: str | None = None


class NodeQuoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine_id: str | None = None
    preset: str | None = None
    region_crop: bool | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    route_id: str | None = None
    packet_id: str | None = None
    prompt: str | None = None
    priority: str | int | None = None
    attempt: int = Field(default=1, ge=1)
    feather_px: float | None = Field(default=None, ge=4, le=12)  # inside the mask, 4-12 px (decision 4)


def _project(request: Request, project_id: str) -> Project:
    try:
        check_project_id(project_id)
    except ValueError as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    project = ctx(request).pipeline.load_project(project_id)
    if project is None:
        raise ApiError(404, {"error": "not_found", "project_id": project_id})
    return project


@router.get("/session")
def session(request: Request) -> dict[str, Any]:
    settings = ctx(request).settings
    return {"ok": True, "daemon_version": __version__, "packet_versions": list(settings.packet_versions)}


@router.get("/projects")
def list_projects(request: Request) -> list[dict[str, Any]]:
    return ctx(request).pipeline.project_list()


@router.post("/projects", status_code=201)
def create_project(request: Request, body: ProjectBody) -> JSONResponse:
    c = ctx(request)
    try:
        pid = check_project_id(body.project_id)
        custody = check_custody(body.custody)
    except (ValueError, ProjectError) as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    if body.root is not None and not Path(body.root).is_absolute():
        raise ApiError(422, {"errors": [f"root must be an absolute folder, got {body.root!r}"]})
    existing = c.pipeline.load_project(pid)
    if existing is not None:
        if not existing.custody.get("unset"):
            raise ApiError(409, {"error": "exists", "project_id": pid, "root": str(existing.root)})
        with existing.lock(timeout_s=5.0):
            existing.doc["custody"] = custody
            existing.doc["engine_pins"] = dict(body.engine_pins)
            existing.save()
        c.index.set_project_root(pid, existing.root, custody)
        c.pipeline.touch_human()
        return JSONResponse(project_head(existing), status_code=200)
    root = Path(body.root) if body.root else c.settings.projects_dir / pid
    try:
        project = Project.create(root, pid, custody, now=c.now())
    except FileExistsError as e:
        raise ApiError(409, {"error": "exists", "project_id": pid, "detail": str(e)}) from e
    except ProjectError as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    if body.engine_pins:
        with project.lock(timeout_s=5.0):
            project.doc["engine_pins"] = dict(body.engine_pins)
            project.save()
    c.index.set_project_root(pid, project.root, project.custody)
    c.pipeline.touch_human()
    return JSONResponse(project_head(project), status_code=201)


@router.get("/projects/{project_id}/graph")
def get_graph(request: Request, project_id: str) -> dict[str, Any]:
    return _project(request, project_id).graph


def _newly_approved_without_scrub(old: Any, new: Any) -> list[dict[str, Any]]:
    before = {n["id"]: n for n in canvas_rules.as_graph(old).nodes}
    out = []
    for n in canvas_rules.as_graph(new).nodes:
        was = before.get(n["id"]) or {}
        if n.get("approved") is True and was.get("approved") is not True and not n.get("scrubLogId"):
            res = canvas_rules.can_accept(n, None)
            out.append({"node": n["id"], "code": res.code, "message": res.message})
    return out


@router.put("/projects/{project_id}/graph")
def put_graph(request: Request, project_id: str, graph: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    c = ctx(request)
    project = _project(request, project_id)
    try:
        check_graph(graph)
        problems = canvas_rules.check_nodes(graph)
        if not problems:
            problems = canvas_rules.validate_graph(graph) + _newly_approved_without_scrub(
                project.graph, graph
            )
    except (ProjectError, ValueError) as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    if problems:
        raise ApiError(
            422, {"errors": [f"{p['code']}: {p['message']}" for p in problems], "violations": problems}
        )
    with project.lock(timeout_s=5.0):
        project.save_graph(graph)
    c.pipeline.touch_human()
    c.pipeline.publish(
        project_id,
        None,
        {
            "kind": "graph.saved",
            "nodes": len(graph.get("nodes") or []),
            "edges": len(graph.get("edges") or []),
        },
    )
    return {"ok": True, "project_id": project_id, "graph": project.graph}


@router.post("/projects/{project_id}/nodes/{node_id}/quote")
def quote_node(request: Request, project_id: str, node_id: str, body: NodeQuoteBody) -> dict[str, Any]:
    c = ctx(request)
    _project(request, project_id)
    with c.index.project(project_id) as conn:
        try:
            run, quote = c.pipeline.quote_node(conn, project_id, node_id, body.model_dump(), now=c.now())
        except CustodyViolation as e:
            raise custody_error(e) from e
        except PipelineError as e:
            raise pipeline_error(e) from e
        view = run_view(conn, run.id)
    return {
        "run": view,
        "quote": quote.model_dump(mode="json"),
        "auto_cap_usd": c.pipeline.policy.per_run_auto_cap_usd,
    }
