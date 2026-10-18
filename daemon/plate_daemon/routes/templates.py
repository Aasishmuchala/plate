"""Template routes: list the graph templates and lay one into a project (router plan T95).

- `GET /templates[?project_id=]` lists `[{id, title, nodes, set, origin}]`: the shipped templates
  (`plate_daemon.templates`) and, with a project, the ones in `<project>/templates/` (a project template
  shadows a shipped id). `nodes` is how many nodes one packet's chain holds (a Plate included); `set` says the
  template ends with one Set node over every new Edit. 404 for an unknown project.
- `POST /projects/{id}/templates/apply {template_id, packet_ids, engine_id?, recipe?, prompt?, task?,
  quote: true, n: 1, auto_approve: false}` builds one chain per packet, column by column to the right of what
  the canvas already holds (`graph_build.apply_template`; a packet's Plate is made once), and stores the graph
  through `PUT /projects/{id}/graph` itself (`routes.projects.put_graph`: the canvas rules, the server-owned
  node status, the project lock, the `graph.saved` event), so nothing is stored that a hand-drawn graph could
  not be. Then, when `quote`, every NEW Edit is quoted as `POST .../nodes/{node}/quote` quotes it (`engine_id`,
  `recipe`, `prompt` and `task` ride along) and, when `n` > 1, fanned out to a best-of-N group. A node the
  pipeline refuses (no engine, a locked mask, a custody route) is listed under `refused` and does not fail the
  batch. `auto_approve` approves each quote `by="auto"` through `POST /runs/{id}/approve`; one the budget rules
  refuse stays QUOTED, is listed under `awaiting_approval` and, all together, raises one bus `alert`
  `"<k> quotes waiting for approval: <usd> USD"`.
  Answers `{nodes_added, edits, runs, total_usd, refused, awaiting_approval, added, set_node, graph}`: `runs`
  are the run views as `GET /runs/{id}` gives them (after the approvals), `total_usd` the sum of their quotes,
  `added` the new node ids and `graph` the stored graph.
  404 unknown project, packet or template; 409 a packet not ingested; 422 a packet of another project, an
  `auto_approve` without `quote`, or a graph the canvas rules refuse (the violations as `PUT` answers them).
"""

from __future__ import annotations

import copy
import logging
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import graph_build, templates
from ..budgets import ALERT_KIND
from ..worker import MAX_FAN_OUT
from . import API, ApiError, ctx, require_token
from .packets import packet_summary
from .projects import NodeQuoteBody, _project, put_graph
from .projects import quote_node as quote_node_route
from .runs import ApproveBody, FanoutBody, approve_route, fanout_route, run_view

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])

log = logging.getLogger("plate_daemon")

MAX_PACKETS = 64
# The apply body's fields that the quote body also has.
QUOTE_FIELDS = ("engine_id", "recipe", "prompt", "task")


class ApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_id: str = Field(min_length=1)
    packet_ids: list[str] = Field(min_length=1, max_length=MAX_PACKETS)
    engine_id: str | None = None
    recipe: str | None = None
    prompt: str | None = None
    task: str | None = None
    quote: bool = True
    n: int = Field(default=1, ge=1, le=MAX_FAN_OUT)
    auto_approve: bool = False


def _refusal(node: str, e: ApiError) -> dict[str, Any]:
    """`{node, error, detail?, status}`: what a refused quote tells the caller (`error` is the code)."""
    body = e.body if isinstance(e.body, dict) else {}
    out: dict[str, Any] = {"node": node, "status": e.status, "error": body.get("error") or "refused"}
    detail = body.get("detail") or (body.get("errors") or [None])[0]
    if detail:
        out["detail"] = detail
    return out


@router.get("/templates")
def list_templates_route(
    request: Request, project_id: Annotated[str | None, Query(min_length=1)] = None
) -> list[dict[str, Any]]:
    root = _project(request, project_id).root if project_id is not None else None
    return [
        {"id": t["id"], "title": t["title"], "nodes": len(t["nodes"]), "set": t["set"], "origin": t["origin"]}
        for t in templates.list(root)
    ]


@router.post("/projects/{project_id}/templates/apply")
def apply_route(
    request: Request, project_id: str, body: ApplyBody, background: BackgroundTasks
) -> dict[str, Any]:
    c = ctx(request)
    project = _project(request, project_id)
    if body.auto_approve and not body.quote:
        raise ApiError(422, {"errors": ["auto_approve approves quotes: send quote: true with it"]})
    try:
        template = templates.load(body.template_id, project.root)
    except templates.TemplateNotFound as e:
        raise ApiError(404, {"error": "not_found", "template_id": body.template_id}) from e
    except templates.TemplateError as e:
        raise ApiError(422, {"error": "bad_template", "detail": str(e)}) from e

    summaries = []
    for packet_id in dict.fromkeys(body.packet_ids):  # once each, in the order given
        summary = packet_summary(request, packet_id)  # 404 unknown, 409 not ingested
        if summary["project_id"] != project_id:
            raise ApiError(
                422,
                {
                    "errors": [
                        f"packet {packet_id} belongs to project {summary['project_id']}, not {project_id}"
                    ]
                },
            )
        summaries.append(summary)

    graph = copy.deepcopy(project.graph)
    try:
        applied = graph_build.apply_template(
            graph,
            summaries,
            template=template,
            engine_id=body.engine_id,
            recipe=body.recipe,
            prompt=body.prompt,
            task=body.task,
            column=graph_build.next_column(graph),
        )
    except graph_build.GraphBuildError as e:
        raise ApiError(422, {"error": "bad_template", "detail": str(e)}) from e
    stored = put_graph(request, project_id, applied.graph)  # the canvas rules; an ApiError 422 on a violation

    refused: list[dict[str, Any]] = []
    groups: list[tuple[str, list[str]]] = []  # (Edit id, its run ids); the first run is the one approved
    if body.quote:
        quote_body = {k: getattr(body, k) for k in QUOTE_FIELDS if getattr(body, k) is not None}
        for edit_id in applied.edits:
            try:
                made = quote_node_route(request, project_id, edit_id, NodeQuoteBody(**quote_body))
            except ApiError as e:
                refused.append(_refusal(edit_id, e))
                continue
            ids = [made["run"]["id"]]
            if body.n > 1:
                try:
                    ids = [r["id"] for r in fanout_route(request, ids[0], FanoutBody(n=body.n))]
                except ApiError as e:  # the quote stands, as one run
                    refused.append(_refusal(edit_id, e))
            groups.append((edit_id, ids))

    waiting: list[dict[str, Any]] = []
    if body.auto_approve:
        for edit_id, ids in groups:
            try:
                approve_route(request, ids[0], ApproveBody(by="auto"), background)
            except ApiError as e:
                waiting.append({"run_id": ids[0], **_refusal(edit_id, e)})
    with c.index.project(project_id) as conn:
        runs = [run_view(conn, rid) for _edit, ids in groups for rid in ids]
    price = {r["id"]: float(r.get("cost_usd_quoted") or 0.0) for r in runs}
    for w in waiting:
        group = next(ids for _edit, ids in groups if ids[0] == w["run_id"])
        w["usd"] = round(sum(price[rid] for rid in group), 6)
    if waiting:
        usd = sum(w["usd"] for w in waiting)
        c.pipeline.publish(
            project_id,
            None,
            {
                "kind": ALERT_KIND,
                "source": "templates",
                "level": "warning",
                "message": f"{len(waiting)} quotes waiting for approval: {usd:.2f} USD",
            },
        )
    return {
        "nodes_added": len(applied.added),
        "edits": applied.edits,
        "runs": runs,
        "total_usd": round(sum(price.values()), 6),
        "refused": refused,
        "awaiting_approval": waiting,
        "added": applied.added,
        "set_node": applied.set_id,
        "graph": stored["graph"],
    }
