"""Human-in-the-loop routes: scrub and verdict, the human-minute timer, drop-to-resolve, the outbound log.

- `POST /runs/{id}/scrub {quadrants_visited, zoom_100, by?}` logs that a take was inspected: a ledger record of
  kind `scrub` whose `opaque_id` / `data.scrub_log_id` is the id returned. `data.full_scrub` is the daemon's
  own judgement (all four quadrants seen at 100% or more), never the client's claim; a partial scrub is
  logged too (it is evidence) but is not a full scrub, so `accept` and `verdict` refuse it.
- `POST /runs/{id}/verdict {verdict: ship|fix|render_it, reasons, scrub_log_id, rater?, seconds?}` records the
  human call: a line in `calib/labels.jsonl` (`plate_daemon.verdicts`) and a ledger record of kind `verdict`.
  Needs a DONE or GATE_FAIL run and a logged FULL scrub of that run (409 `scrub_required` otherwise); Fix and
  Render-it may carry reason codes from `calib/reason_codes.yaml`, a Ship carries none (422).
- `POST /timer {deliverable_id, action: start|stop, project_id?}` and `GET /timer/today[?project_id=]`: the
  one-key human-minute timer (`plate_daemon.timer`); `human_minutes` records go to the ledger.
- `POST /assets/resolve {path, project_id?}`: what is this dropped file? Lineage, then the exact BLAKE3, then the
  nearest perceptual hash (`phash.resolve_drop`), across every project's index unless `project_id` narrows it.
  A lineage claim from another project or another machine comes back with `run_id: null, known_run: false`.
- `GET /projects/{id}/outbound?tail=50`: the last requests in the project's `outbound.jsonl`, one merged record
  per request (`{"entries": [...]}`), for the custody panel.

Scrub, verdict and both timer actions reset the unattended clock (`Pipeline.touch_human`) only for a request
carrying `X-Plate-Client: ui` (decision 71, `routes.projects.from_ui`): `plate timer` in a terminal, curl, MCP
and the plugin never do, so a header-less timer start cannot lift the unattended cap.
"""

from __future__ import annotations

import secrets
import threading
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import verdicts
from ..ledger import Ledger, LedgerError
from ..lock import ProjectLocked
from ..machine import RunNotFound, get_run
from ..models import Run
from ..outbound_log import OutboundLog
from ..phash import DEFAULT_MAX_DISTANCE, PhashError, PhashIndex, resolve_drop
from ..states import RunState
from ..timer import HumanTimer, TimerError
from . import API, ApiError, Ctx, ctx, require_token, run_project
from .projects import from_ui
from .runs import find_scrub
from .tierc import project_or_404

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])

S = RunState
JUDGEABLE = (S.DONE, S.GATE_FAIL)
LEDGER_ERRORS = (LedgerError, ProjectLocked, OSError)
RESOLVE_ORDER = {"lineage": 0, "content_hash": 1, "phash": 2}
_TIMER_LOCK = threading.Lock()


class ScrubBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quadrants_visited: int = Field(ge=0, le=4)
    zoom_100: bool
    by: str = Field(default="human", min_length=1)


class VerdictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: str
    reasons: list[str] = Field(default_factory=list)
    scrub_log_id: str = Field(min_length=1)
    rater: str = Field(default="human", min_length=1)
    seconds: float | None = None


class TimerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deliverable_id: str
    action: Literal["start", "stop"]
    project_id: str | None = None


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    project_id: str | None = None
    max_distance: int = Field(default=DEFAULT_MAX_DISTANCE, ge=0, le=64)


# ---------------------------------------------------------------------------- scrub and verdict


def run_ledger_fields(run: Run) -> dict[str, Any]:
    """The identity fields `ledger.fold_runs` reads, taken from the run (as `Pipeline.ledger` does)."""
    route = run.route_dict
    return {
        "run_id": run.id,
        "node_id": run.node_id,
        "engine_id": route.get("engine_id"),
        "route_id": route.get("route_id") or route.get("provider"),
        "custody_grade": run.quote.custody_grade if run.quote else None,
    }


def _append(c: Ctx, project_id: str, record: dict[str, Any]) -> dict[str, Any]:
    record = {k: v for k, v in record.items() if v is not None and v != ""}
    try:
        return Ledger(c.pipeline.project_dir(project_id), clock=c.now).append(record)
    except LEDGER_ERRORS as e:
        raise ApiError(500, {"error": "ledger_unwritable", "detail": f"{type(e).__name__}: {e}"}) from e


def _judgeable_run(c: Ctx, project_id: str, run_id: str, what: str) -> Run:
    with c.index.project(project_id) as conn:
        try:
            run = get_run(conn, run_id)
        except RunNotFound as e:
            raise ApiError(404, {"error": "not_found", "run_id": run_id}) from e
    if run.state not in JUDGEABLE:
        raise ApiError(
            409,
            {
                "error": "not_judgeable",
                "state": run.state.value,
                "detail": f"{what} a DONE or GATE_FAIL take; this run is {run.state.value}",
            },
        )
    return run


@router.post("/runs/{run_id}/scrub")
def scrub_route(request: Request, run_id: str, body: ScrubBody) -> dict[str, Any]:
    c = ctx(request)
    pid = run_project(c, run_id)
    run = _judgeable_run(c, pid, run_id, "scrub")
    full = body.quadrants_visited >= 4 and body.zoom_100
    scrub_id = f"scrub-{secrets.token_hex(6)}"
    _append(
        c,
        pid,
        {
            "kind": "scrub",
            **run_ledger_fields(run),
            "by": body.by,
            "opaque_id": scrub_id,
            "data": {
                "scrub_log_id": scrub_id,
                "quadrants_visited": body.quadrants_visited,
                "zoom_100": body.zoom_100,
                "full_scrub": full,
            },
        },
    )
    if from_ui(request):
        c.pipeline.touch_human()
    c.pipeline.publish(
        pid, run_id, {"kind": "run.scrubbed", "run_id": run_id, "scrub_log_id": scrub_id, "full_scrub": full}
    )
    return {
        "scrub_log_id": scrub_id,
        "run_id": run_id,
        "quadrants_visited": body.quadrants_visited,
        "zoom_100": body.zoom_100,
        "full_scrub": full,
    }


@router.post("/runs/{run_id}/verdict")
def verdict_route(request: Request, run_id: str, body: VerdictBody) -> dict[str, Any]:
    c = ctx(request)
    pid = run_project(c, run_id)
    try:
        reasons = verdicts.check_reasons(body.verdict, body.reasons)
    except verdicts.VerdictError as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    run = _judgeable_run(c, pid, run_id, "judge")
    scrub = find_scrub(Ledger(c.pipeline.project_dir(pid)).records(), run_id, body.scrub_log_id)
    if scrub is None:
        raise ApiError(
            409,
            {
                "error": "scrub_required",
                "code": "SCRUB_REQUIRED",
                "detail": f"no logged full scrub {body.scrub_log_id!r} of run {run_id}",
            },
        )
    at = c.now()
    plate = run.inputs.get("plate")
    try:
        label = verdicts.build_label(
            at=at,
            rater=body.rater,
            run_id=run_id,
            verdict=body.verdict,
            reasons=reasons,
            candidate=run.outputs[0] if run.outputs else (run.result_key or run_id),
            plate=plate if isinstance(plate, str) else None,
            gate_report=run.gate_report_path,
            seconds=body.seconds,
        )
    except verdicts.VerdictError as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    rec = _append(
        c,
        pid,
        {
            "kind": "verdict",
            **run_ledger_fields(run),
            "by": label["rater"],
            "reason": ", ".join(reasons) or None,
            "data": {
                "verdict": body.verdict,
                "reasons": reasons,
                "scrub_log_id": body.scrub_log_id,
                "rater": label["rater"],
                "decision": verdicts.DECISION[body.verdict],
            },
        },
    )
    try:
        verdicts.append_label(verdicts.labels_path(c.settings), label)
    except verdicts.VerdictError as e:
        raise ApiError(
            500, {"error": "labels_unwritable", "detail": str(e), "ledgered": True, "ledger_seq": rec["seq"]}
        ) from e
    if from_ui(request):
        c.pipeline.touch_human()
    c.pipeline.publish(
        pid,
        run_id,
        {"kind": "run.verdict", "run_id": run_id, "verdict": body.verdict, "rater": label["rater"]},
    )
    return {
        "ok": True,
        "run_id": run_id,
        "verdict": body.verdict,
        "reasons": reasons,
        "rater": label["rater"],
        "decision": verdicts.DECISION[body.verdict],
        "ledger_seq": rec["seq"],
    }


# ---------------------------------------------------------------------------- the human-minute timer


def timer_for(c: Ctx) -> HumanTimer:
    with _TIMER_LOCK:
        timer = getattr(c.app.state, "timer", None)
        if timer is None:
            timer = c.app.state.timer = HumanTimer(
                c.settings.home,
                c.pipeline.project_dir,
                lambda: [c.pipeline.project_dir(p) for p in c.index.project_ids()],
            )
        return timer


def _timer_error(e: TimerError) -> ApiError:
    return ApiError(e.status, {"error": e.code, "detail": e.detail, **e.extra})


@router.post("/timer")
def timer_route(request: Request, body: TimerBody) -> dict[str, Any]:
    c = ctx(request)
    timer = timer_for(c)
    try:
        if body.action == "start":
            if body.project_id:
                project_or_404(c, body.project_id)
            state = timer.start(body.deliverable_id, c.now(), body.project_id)
            if from_ui(request):
                c.pipeline.touch_human()
            return {"ok": True, "action": "start", **state}
        done = timer.stop(body.deliverable_id, c.now())
    except TimerError as e:
        raise _timer_error(e) from e
    if from_ui(request):
        c.pipeline.touch_human()
    return {"ok": True, "action": "stop", **done}


@router.get("/timer/today")
def timer_today_route(request: Request, project_id: Annotated[str | None, Query()] = None) -> dict[str, Any]:
    c = ctx(request)
    if project_id:
        project_or_404(c, project_id)
    return timer_for(c).today(c.now(), project_id)


# ---------------------------------------------------------------------------- drop to resolve


@router.post("/assets/resolve")
def resolve_route(request: Request, body: ResolveBody) -> dict[str, Any]:
    c = ctx(request)
    path = Path(body.path)
    if not path.is_absolute():
        raise ApiError(422, {"errors": [f"path must be absolute, got {body.path!r}"]})
    if not path.is_file():
        raise ApiError(404, {"error": "not_found", "detail": f"no such file: {body.path}"})
    known = c.index.project_ids()
    if body.project_id is not None and body.project_id not in known:
        raise ApiError(404, {"error": "not_found", "project_id": body.project_id})
    scope: list[str | None] = [body.project_id] if body.project_id else (list(known) or [None])
    best: dict[str, Any] | None = None
    try:
        for pid in scope:
            index = PhashIndex(c.index, pid) if pid else PhashIndex()
            hit = resolve_drop(path, index, body.max_distance)
            if hit is None:
                continue
            if pid and hit["by"] != "lineage":
                hit = {**hit, "project_id": pid}
            rank = (RESOLVE_ORDER.get(hit["by"], 9), hit.get("distance") or 0)
            if best is None or rank < (RESOLVE_ORDER.get(best["by"], 9), best.get("distance") or 0):
                best = hit
            if hit["by"] == "lineage":
                break  # the file's own claim does not depend on which project's index we asked
    except PermissionError as e:
        raise ApiError(403, {"error": "forbidden", "detail": str(e)}) from e
    except (PhashError, OSError) as e:
        raise ApiError(422, {"errors": [f"{type(e).__name__}: {e}"]}) from e
    if best is None:
        raise ApiError(
            404, {"error": "no_match", "detail": "no lineage, stored content or similar image found"}
        )
    if best["by"] == "lineage":
        run_id = best.get("run_id")
        owner = c.index.locate_run(run_id) if run_id else None
        if owner is None or owner != best.get("project_id"):
            best = {**best, "run_id": None, "known_run": False}
        else:
            best = {**best, "known_run": True}
    return best


# ---------------------------------------------------------------------------- the outbound log


@router.get("/projects/{project_id}/outbound")
def outbound_route(
    request: Request, project_id: str, tail: Annotated[int, Query(ge=1, le=1000)] = 50
) -> dict[str, Any]:
    c = ctx(request)
    project = project_or_404(c, project_id)
    return {"entries": OutboundLog.for_project(project.root).tail(tail)}
