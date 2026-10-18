"""Project routes: `GET /session`, `GET|POST /projects`, `GET|PUT /projects/{id}/graph`, custody, the node quote.

- `GET /session` answers what a client needs before it sends anything: the daemon and packet versions, the
  packet folders the daemon reads (`packets_dirs`; the Max plugin's preflight checks its own folder is one of
  them), `projects_dir`, `dev` and `cas_copy`.
- `GET /projects` lists the head of every project.json (`{project_id, custody, created_utc, engine_pins,
  root, nodes}`); `POST /projects {project_id, custody: {nda, allowed_grades?}, engine_pins?, root?}` makes
  the folder (default `<home>/projects/<id>`) and registers it with the index: 201, or 200 when it fills in
  the custody of a project a packet created first (`custody.unset`), else 409.
- `PUT /projects/{id}/graph` is where the canvas rules are ENFORCED (`canvas_rules`, the mirror of
  `ui/src/canvasRules.ts`; the UI's checks are UX). The server owns node status (decision 31): before any
  rule runs, every node's `gateStatus`, `approved`, `approvedBy` and `scrubLogId` are rewritten from the
  index (`derive_status`), whatever the client sent, so a made-up `scrubLogId` unlocks nothing. A node that
  names its take (`outputHash`, the take the artist picked) gets the status of the run behind that take
  (G2, T111): a newer take is one more candidate and unapproves nothing, and picking a take nobody has
  accepted unapproves the node until that take is accepted. Then an unknown node kind, a repeated id or any
  edge that breaks a rule answers `422 {"errors", "violations": [{edge|node, code, message}]}`, and a node
  the CLIENT newly claims approved without a `scrubLogId` answers SCRUB_REQUIRED. One refusal is held
  instead: VIDEO_LOCKED on an edge the project already stores, out of a still that names its take (the
  artist's own pick locked it). That edge is stored with `locked: true` (server-owned, like the node status:
  a client's `locked` is dropped) and listed under `violations` in the 200, so autosave never deadlocks;
  the Motion or CropPair behind it still refuses its quote. A NEW edge into a locked still, or a lock on a
  still with no picked take (it follows its newest run), is refused as before. A good graph is written
  atomically under the project lock (where a held edge must still be stored: one another writer removed
  meanwhile is a new edge again), announced on the bus as `graph.saved`, and answered as stored (the
  rewritten status and the flags included) so the UI reconciles.
- `PUT /projects/{id}/custody {nda, allowed_grades, reason?}`: tightening (no grade added, NDA not dropped)
  is always allowed; loosening needs a written reason (10 characters at least) and is ledgered first as
  `kind: "override"`, `by: "human"`, `data: {what: "custody", from, to}` (no ledger, no loosening: 503).
  Answers the project head.
- `POST /projects/{id}/nodes/{node}/quote {engine_id?, preset?, region_crop?, params?, route_id?, prompt?,
  packet_id?, priority?, attempt?, feather_px?, task?, shot_class?, recipe?}` turns a canvas node into a
  run in QUOTED (`Pipeline.quote_node`): `{run, quote, auto_cap_usd, router}` where `router` is the
  render-it verdict the pipeline stored at `run.inputs["router"]` (or null). `attempt` is left to the
  server when absent. A route the project's custody does not allow is 403 `{"error": "custody",
  "exception": "CustodyViolation"}`; rule, mask and engine refusals are 422.

A human is a request carrying `X-Plate-Client: ui` (decision 37; the UI sends it on every call). Only those
reset the unattended clock (`Pipeline.touch_human`): MCP, curl and the Max plugin never send it, so an agent
saving graphs or quoting all night cannot keep its own spend "attended".
"""

from __future__ import annotations

import copy
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .. import __version__, canvas_rules
from ..config import check_project_id
from ..custody import CustodyViolation
from ..ledger import Ledger, LedgerError
from ..lock import ProjectLocked
from ..pipeline import PipelineError, project_head
from ..projects import Project, ProjectError, check_custody, check_graph
from ..states import RunState
from . import API, ApiError, ctx, custody_error, pipeline_error, require_token
from .runs import find_scrub, run_view

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])

S = RunState
CLIENT_HEADER = "X-Plate-Client"
UI_CLIENT = "ui"
MIN_CUSTODY_REASON = 10  # the same bar as an override reason (export.MIN_OVERRIDE_REASON)
LOCK_TIMEOUT_S = 5.0
LEDGER_ERRORS = (LedgerError, ProjectLocked, OSError)
# The node fields the server derives; the snake_case spellings are dropped because canvas_rules reads them too.
STATUS_FIELDS = ("gateStatus", "approved", "approvedBy", "scrubLogId")
SNAKE_STATUS_FIELDS = {"gate_status": "gateStatus", "approved_by": "approvedBy", "scrub_log_id": "scrubLogId"}
# A run that ended without a take does not speak for its node: the node keeps the status of its last take
# (a cancelled or failed re-roll must not relock Motion behind an accepted still, or cost a paid re-run).
NO_TAKE_STATES = frozenset({S.CANCELLED.value, S.EXPIRED.value, S.FAILED.value, S.REFUSED.value})
STATUS_RANK = {"pass": 2, "fail": 1, "none": 0}
STATUS_DEFAULTS: dict[str, Any] = {"gateStatus": "none", "approved": False}  # canvas_rules.node_data's
# The server's flag on a stored edge whose still locked after the artist picked another take (T111).
EDGE_LOCKED = "locked"
HELD_CODES = frozenset({"VIDEO_LOCKED"})


class ProjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    custody: dict[str, Any]
    engine_pins: dict[str, str] = Field(default_factory=dict)
    root: str | None = None


class CustodyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nda: StrictBool
    allowed_grades: list[str]
    reason: str | None = Field(default=None, max_length=500)


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
    attempt: int | None = Field(default=None, ge=1)  # absent: the pipeline numbers repeats (decision 40)
    feather_px: float | None = Field(default=None, ge=4, le=12)  # inside the mask, 4-12 px (decision 4)
    task: str | None = None  # relight, people, vegetation, sky, rephotograph_close (the router's task names)
    shot_class: str | None = None  # wide, medium, close
    recipe: str | None = None  # a runnable recipe id or path (decision 33)


def from_ui(request: Request) -> bool:
    """Is this request a human at the UI? Decision 37: the UI sends `X-Plate-Client: ui`; nothing else does."""
    return request.headers.get(CLIENT_HEADER, "").strip().lower() == UI_CLIENT


def _touch_if_human(request: Request) -> None:
    if from_ui(request):
        ctx(request).pipeline.touch_human()


def _project(request: Request, project_id: str) -> Project:
    try:
        check_project_id(project_id)
    except ValueError as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    project = ctx(request).pipeline.load_project(project_id)
    if project is None:
        raise ApiError(404, {"error": "not_found", "project_id": project_id})
    return project


def _locked(project_id: str, e: ProjectLocked) -> ApiError:
    return ApiError(409, {"error": "project_locked", "project_id": project_id, "detail": str(e)})


@router.get("/session")
def session(request: Request) -> dict[str, Any]:
    settings = ctx(request).settings
    return {
        "ok": True,
        "daemon_version": __version__,
        "packet_versions": list(settings.packet_versions),
        "packets_dirs": [str(p) for p in settings.packets_dirs],
        "projects_dir": str(settings.projects_dir) if settings.projects_dir is not None else None,
        "dev": bool(settings.dev),
        "cas_copy": bool(settings.cas_copy),
    }


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
        with existing.lock(timeout_s=LOCK_TIMEOUT_S):
            existing.doc["custody"] = custody
            existing.doc["engine_pins"] = dict(body.engine_pins)
            existing.save()
        c.index.set_project_root(pid, existing.root, custody)
        _touch_if_human(request)
        return JSONResponse(project_head(existing), status_code=200)
    root = Path(body.root) if body.root else c.settings.projects_dir / pid
    try:
        project = Project.create(root, pid, custody, now=c.now())
    except FileExistsError as e:
        raise ApiError(409, {"error": "exists", "project_id": pid, "detail": str(e)}) from e
    except ProjectError as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    if body.engine_pins:
        with project.lock(timeout_s=LOCK_TIMEOUT_S):
            project.doc["engine_pins"] = dict(body.engine_pins)
            project.save()
    c.index.set_project_root(pid, project.root, project.custody)
    _touch_if_human(request)
    return JSONResponse(project_head(project), status_code=201)


@router.get("/projects/{project_id}/graph")
def get_graph(request: Request, project_id: str) -> dict[str, Any]:
    return _project(request, project_id).graph


# ---------------------------------------------------------------------------- server-derived node status


def _status_of(view: Mapping[str, Any]) -> str:
    state = view.get("state")
    if state == S.DONE.value:
        return "pass"
    if state == S.GATE_FAIL.value:
        return "pass" if view.get("override") is not None else "fail"
    return "none"


def _node_status(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    records: Callable[[], list[dict[str, Any]]],
    picked: str | None = None,
) -> dict[str, Any]:
    """`{gateStatus, approved[, approvedBy, scrubLogId]}` of one node from its runs, newest first.

    A node that names its take (`picked`, its `outputHash`) is decided by the run(s) whose first output is that
    take and by nothing else (T111, G2): a newer take is one more candidate, so a re-mask or a second Make after
    an accept leaves the node approved, and a picked take nobody accepted leaves it unapproved until it is. A
    picked picture that is none of the node's takes (a stale or made-up hash, an upload) was never gated or
    accepted as this node's take: the defaults. A node with no pick follows its latest run that produced (or
    may still produce) a take; when that run is one of a best-of-N group, the whole group does. Among the
    deciding runs, the one the artist accepted approves, else the best gate outcome stands unapproved. Approval
    is the run's latest `accept` note, and only when `find_scrub` confirms its logged full scrub in the ledger."""
    live = [r for r in rows if r["state"] not in NO_TAKE_STATES]
    if picked is not None:
        candidates = [r for r in live if r["output"] == picked]
    elif live:
        group = live[0]["sibling_group"]
        candidates = [r for r in live if r["sibling_group"] == group] if group else live[:1]
    else:
        candidates = []
    if not candidates:
        return dict(STATUS_DEFAULTS)
    views = [run_view(conn, r["id"]) for r in candidates]
    accepted: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for v in views:
        note = v.get("accepted")
        if not isinstance(note, dict) or _status_of(v) != "pass":
            continue
        scrub_id = note.get("scrub_log_id")
        if not isinstance(scrub_id, str) or not scrub_id:
            continue
        if find_scrub(records(), v["id"], scrub_id) is not None:
            accepted.append((str(note.get("at") or ""), v, note))
    if accepted:
        _at, v, note = max(accepted, key=lambda t: t[0])
        out: dict[str, Any] = {
            "gateStatus": _status_of(v),
            "approved": True,
            "scrubLogId": note["scrub_log_id"],
        }
        if isinstance(note.get("by"), str) and note["by"]:
            out["approvedBy"] = note["by"]
        return out
    best = max(views, key=lambda v: STATUS_RANK[_status_of(v)])  # max keeps the first (newest) on a tie
    return {"gateStatus": _status_of(best), "approved": False}


def picked_takes(graph: Mapping[str, Any]) -> dict[str, str]:
    """`{node id: outputHash}` for every node of `graph` that names its take, read where the canvas rules and
    the Motion runner read it (`canvas_rules.node_data`: the node's `data` object when it has one, else the
    node; either spelling)."""
    out: dict[str, str] = {}
    for raw in graph.get("nodes") or []:
        if not isinstance(raw, Mapping):
            continue
        n = canvas_rules.node_data(raw)
        nid, key = n.get("id"), n.get("outputHash")
        if isinstance(nid, str) and nid and isinstance(key, str) and key:
            out[nid] = key
    return out


def derive_status(
    conn: sqlite3.Connection, project_dir: Path, graph: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """The server's status for every node id of `graph` (decision 31), from the node's picked take when it
    names one (`picked_takes`). A node that never ran (Plate, Mask, Export, a fresh Edit...) is
    `{gateStatus: "none", approved: false}`."""
    ids = {n.get("id") for n in graph.get("nodes") or [] if isinstance(n, Mapping)}
    ids = {i for i in ids if isinstance(i, str) and i}
    picks = picked_takes(graph)
    by_node: dict[str, list[sqlite3.Row]] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        rows = conn.execute(
            "SELECT id, node_id, state, json_extract(quote_json, '$.sibling_group') AS sibling_group, "
            "CASE WHEN json_valid(normalized) THEN json_extract(normalized, '$.outputs[0]') END AS output "
            f"FROM runs WHERE node_id IN ({marks}) ORDER BY created_utc DESC, rowid DESC",
            tuple(sorted(ids)),
        ).fetchall()
        for r in rows:
            by_node.setdefault(r["node_id"], []).append(r)
    cache: list[list[dict[str, Any]]] = []

    def records() -> list[dict[str, Any]]:  # the ledger is read once, and only if some run was accepted
        if not cache:
            cache.append(Ledger(project_dir).records())
        return cache[0]

    return {nid: _node_status(conn, by_node.get(nid, []), records, picks.get(nid)) for nid in ids}


def _apply_status(graph: dict[str, Any], status: Mapping[str, Mapping[str, Any]]) -> None:
    """Write the derived status onto each node, in place, where `canvas_rules.node_data` reads it (the
    node's `data` object when it has one, else the node itself), dropping whatever the client sent under
    either spelling. A field the client did not send and whose value is the default (`gateStatus "none"`,
    `approved false`) stays absent, so a plain graph round-trips unchanged."""
    for node in graph.get("nodes") or []:
        nid = node.get("id")
        st = status.get(nid) if isinstance(nid, str) else None
        st = st if st is not None else STATUS_DEFAULTS
        target = node["data"] if isinstance(node.get("data"), dict) else node
        sent: set[str] = set()
        for holder in (target, node) if target is not node else (node,):
            for f in (*STATUS_FIELDS, *SNAKE_STATUS_FIELDS):
                if f in holder:
                    sent.add(SNAKE_STATUS_FIELDS.get(f, f))
                    del holder[f]
        for f in STATUS_FIELDS:
            if f in st and (f in sent or st[f] != STATUS_DEFAULTS.get(f)):
                target[f] = st[f]


def _newly_approved_without_scrub(old: Any, new: Any) -> list[dict[str, Any]]:
    before = {n["id"]: n for n in canvas_rules.as_graph(old).nodes}
    out = []
    for n in canvas_rules.as_graph(new).nodes:
        was = before.get(n["id"]) or {}
        if n.get("approved") is True and was.get("approved") is not True and not n.get("scrubLogId"):
            res = canvas_rules.can_accept(n, None)
            out.append({"node": n["id"], "code": res.code, "message": res.message})
    return out


def _edge_key(e: Mapping[str, Any]) -> tuple[Any, ...]:
    """An edge as the rules see it (`canvas_rules.as_edge`): source, target and the two handles; not its id."""
    return (e["source"], e["target"], e.get("sourceHandle"), e.get("targetHandle"))


def _edge_keys(graph: Any) -> set[tuple[Any, ...]]:
    """The edges of a stored graph; one that does not read holds none (nothing is held against it)."""
    try:
        return {_edge_key(e) for e in canvas_rules.as_graph(graph).edges}
    except (ValueError, TypeError, KeyError):
        return set()


def _held(v: Mapping[str, Any], stored: set[tuple[Any, ...]], picked: Mapping[str, str]) -> bool:
    """Does the save keep this violation (flagged) instead of refusing the graph? Only VIDEO_LOCKED on an edge
    the project already stores, out of a still that names its take: the artist's pick locked it. An edge the
    client is adding, and a still with no picked take (it follows its newest run), stay refusals."""
    e = v.get("edge")
    return (
        v.get("code") in HELD_CODES
        and isinstance(e, Mapping)
        and e.get("source") in picked
        and _edge_key(e) in stored
    )


def _flag_edges(graph: dict[str, Any], held: list[dict[str, Any]]) -> None:
    """`locked: true` on every edge of `graph` a held violation names (the client's own flags are gone)."""
    keys = {_edge_key(v["edge"]) for v in held}
    for raw in graph.get("edges") or []:
        try:
            key = _edge_key(canvas_rules.as_edge(raw))
        except ValueError:
            continue
        if key in keys:
            raw[EDGE_LOCKED] = True


def _refused(problems: list[dict[str, Any]]) -> ApiError:
    return ApiError(
        422, {"errors": [f"{p['code']}: {p['message']}" for p in problems], "violations": problems}
    )


@router.put("/projects/{project_id}/graph")
def put_graph(request: Request, project_id: str, graph: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    c = ctx(request)
    project = _project(request, project_id)
    held: list[dict[str, Any]] = []
    try:
        check_graph(graph)
        claimed = copy.deepcopy(graph)
        for edge in graph["edges"]:
            edge.pop(EDGE_LOCKED, None)  # server-owned, like the node status
        with c.index.project(project_id) as conn:
            _apply_status(graph, derive_status(conn, project.root, graph))
        problems = canvas_rules.check_nodes(graph)
        if not problems:
            # The rules read the server's status; a lock the artist's pick put on a stored edge is held.
            stored, picked = _edge_keys(project.graph), picked_takes(graph)
            for v in canvas_rules.validate_graph(graph):
                (held if _held(v, stored, picked) else problems).append(v)
            # SCRUB_REQUIRED answers a client that claims an approval.
            problems += _newly_approved_without_scrub(project.graph, claimed)
    except (ProjectError, ValueError) as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    if problems:
        raise _refused(problems + held)
    _flag_edges(graph, held)
    try:
        with project.lock(timeout_s=LOCK_TIMEOUT_S):
            project = Project.load(project.root)  # under the lock: never write back a stale custody
            stored = _edge_keys(project.graph)
            gone = [v for v in held if _edge_key(v["edge"]) not in stored]
            if gone:  # removed by another writer since the check: a new edge again, so refused
                raise _refused(gone + [v for v in held if v not in gone])
            project.save_graph(graph)
    except ProjectLocked as e:
        raise _locked(project_id, e) from e
    _touch_if_human(request)
    c.pipeline.publish(
        project_id,
        None,
        {
            "kind": "graph.saved",
            "nodes": len(graph.get("nodes") or []),
            "edges": len(graph.get("edges") or []),
        },
    )
    return {"ok": True, "project_id": project_id, "graph": project.graph, "violations": held}


# ---------------------------------------------------------------------------- custody


@router.put("/projects/{project_id}/custody")
def put_custody(request: Request, project_id: str, body: CustodyBody) -> dict[str, Any]:
    c = ctx(request)
    project = _project(request, project_id)
    try:
        with project.lock(timeout_s=LOCK_TIMEOUT_S) as lock:
            project = Project.load(project.root)
            old = dict(project.custody)
            keep = {k: v for k, v in old.items() if k not in ("nda", "allowed_grades", "unset")}
            try:
                new = check_custody({**keep, "nda": body.nda, "allowed_grades": list(body.allowed_grades)})
            except ProjectError as e:
                raise ApiError(422, {"errors": [str(e)]}) from e
            added = set(new["allowed_grades"]) - set(old["allowed_grades"])
            loosened = bool(added) or (bool(old["nda"]) and not new["nda"])
            reason = (body.reason or "").strip()
            if loosened:
                if len(reason) < MIN_CUSTODY_REASON:
                    raise ApiError(
                        422,
                        {
                            "error": "reason_required",
                            "errors": [
                                f"loosening custody needs a reason of at least {MIN_CUSTODY_REASON} characters"
                            ],
                            "added_grades": sorted(added),
                        },
                    )
                try:  # ledgered first: a loosening the ledger cannot hold does not happen
                    Ledger(project.root, lock=lock, clock=c.now).append(
                        {
                            "kind": "override",
                            "by": "human",
                            "reason": reason,
                            "data": {"what": "custody", "from": old, "to": new},
                        }
                    )
                except LEDGER_ERRORS as e:
                    raise ApiError(
                        503, {"error": "ledger_unavailable", "detail": f"custody unchanged: {e}"}
                    ) from e
            if new != old:
                project.doc["custody"] = new
                project.save()
    except ProjectLocked as e:
        raise _locked(project_id, e) from e
    c.index.set_project_root(project_id, project.root, project.custody)
    _touch_if_human(request)
    return project_head(project)


# ---------------------------------------------------------------------------- quoting a node


@router.post("/projects/{project_id}/nodes/{node_id}/quote")
def quote_node(request: Request, project_id: str, node_id: str, body: NodeQuoteBody) -> dict[str, Any]:
    c = ctx(request)
    _project(request, project_id)
    _touch_if_human(request)
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
        "router": (run.inputs or {}).get("router"),
    }
