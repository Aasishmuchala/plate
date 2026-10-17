"""HTTP routes of the numeric and plugin nodes (T48), under `/api/v0` behind the bearer token.

    GET  /plugin/jobs?since=<seq>[&claim=1][&state=queued,claimed][&limit=]
                              what the Max dock polls: queued jobs after its cursor, oldest first; `next` is the
                              cursor to send next time. With `claim=1` the jobs returned are marked claimed in the
                              same transaction, so two docks never take the same job.
    POST /plugin/jobs         {action, packet_id, ...arguments}: queue a job without a run (the Light editor's
                              "Match in Max" and "Vantage preview" buttons). The project is the packet's.
    GET  /plugin/jobs/{id}    one job, its state and result.
    GET  /plugin/status       {last_poll_utc, queued, claimed}: when a dock last polled with claim=1 (null: never),
                              how many jobs wait for one and how many a dock has taken and not answered. The status
                              bar's "Max dock" chip reads it.
    POST /plugin/jobs/{id}/result
                              the dock's answer {ok, detail?, packet_id?, metrics?, poses?, previews?, packets?}:
                              queued or claimed -> done (ok) or failed; 409 when the job is already finished.
                              The run that owns the job (Light, Scout) moves on at its next poll.
    POST /projects/{id}/nodes/{node}/preview {what, params}
                              numbers for the node editors: `what` is set, lightmix or recolour. No run, no spend.

The dock's contract (for whoever writes its poller; nothing in `plate_max` polls yet). A job from `GET` is
`{id, seq, action, state, project_id, packet_id, run_id, node_id, created_utc, claimed_utc, finished_utc,
result, ...arguments}` with these arguments per action:

    light.match            camera, reference_hash (fetch the picture from `GET /assets/{hash}`), locks (MaxGaffer
                           parameter keys), deep, supersedes (= packet_id: the re-render must say so)
    light.vantage_preview  camera
    scout                  count, keep, scene_hash
    receive                run_id, mode (`vfb_history` | `viewport_background`): the dock reads `GET /runs/{run_id}`,
                           saves `outputs[0]` from `GET /assets/{hash}` and shows it in Max (display only). Queued
                           by `POST /plugin/jobs {action: "receive", run_id, mode?}`, which needs no `packet_id`
                           (the run's is used) and refuses a run with no output (409 `no_output`).

and its answer is `{ok, detail?, packet_id?, metrics?, poses?, previews?, packets?}`: `packet_id` the re-rendered
packet (Light; the run also finds it by `supersedes`), `metrics` e.g. {de00}, `poses` the scout cameras, `previews`
CAS hashes of low-res pictures it uploaded, `packets` packet ids it sent. `ok: false` with a `detail` fails the run
with that text. Send the new packet through `POST /packets` as usual; the answer and the packet may arrive in
either order.

Mounting: `plate_daemon.routes` includes every module there that exposes a `router`, so wiring this file in is
`routes/numeric.py` holding `from ..nodes.routes import router`; the routes call `install(pipeline)` on first use,
which is idempotent (`api.create_app` should call it too, before the boot-time resume, see runner.install).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from ..machine import get_run
from ..pipeline import PipelineError
from ..routes import API, ApiError, ctx, pipeline_error, require_token
from .plugin_queue import ACTIONS, RECEIVE_MODES, JobStateError, UnknownAction
from .runner import NodeRunner, install

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])


class JobBody(BaseModel):
    """A job the UI queues directly; the action's arguments ride along as extra fields."""

    model_config = ConfigDict(extra="allow")
    action: str = Field(min_length=1)
    packet_id: str | None = Field(default=None, min_length=1)  # every action but `receive` names its packet


class ResultBody(BaseModel):
    """The dock's answer; whatever else it sends (poses, previews, metrics) is kept with the job."""

    model_config = ConfigDict(extra="allow")
    ok: bool = True
    detail: str | None = Field(default=None, max_length=2000)
    packet_id: str | None = None


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    what: str
    params: dict[str, Any] = Field(default_factory=dict)


def runner_of(request: Request) -> NodeRunner:
    return install(ctx(request).pipeline)


@router.get("/plugin/jobs")
def list_jobs(
    request: Request, since: int = 0, claim: bool = False, state: str = "queued", limit: int = 50
) -> dict[str, Any]:
    runner = runner_of(request)
    states = tuple(s.strip() for s in state.split(",") if s.strip())
    try:
        jobs = runner.queue.list_jobs(since, states=states, claim=claim, limit=limit)
    except ValueError as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    return {"jobs": [j.to_dict() for j in jobs], "next": max([j.seq for j in jobs], default=max(since, 0))}


def _create_receive(request: Request, body: JobBody) -> dict[str, Any]:
    """Queue a `receive` job: show a run's take in Max. The job belongs to the run (its `run_id`), not to a node run
    the dock has to answer, so nothing waits on it."""
    c = ctx(request)
    runner = runner_of(request)
    extra = body.model_extra or {}
    run_id, mode = extra.get("run_id"), extra.get("mode", RECEIVE_MODES[0])
    if not isinstance(run_id, str) or not run_id:
        raise ApiError(422, {"errors": ["receive needs a run_id"]})
    if mode not in RECEIVE_MODES:
        raise ApiError(422, {"errors": [f"mode must be one of {', '.join(RECEIVE_MODES)}, got {mode!r}"]})
    project_id = c.index.locate_run(run_id)
    if project_id is None:
        raise ApiError(404, {"error": "not_found", "run_id": run_id})
    with c.index.project(project_id) as conn:
        run = get_run(conn, run_id)
    if not run.outputs:
        raise ApiError(
            409,
            {
                "error": "no_output",
                "run_id": run_id,
                "state": run.state.value,
                "detail": "the run has no take",
            },
        )
    if body.packet_id and body.packet_id != run.packet_id:
        raise ApiError(422, {"errors": [f"run {run_id} is on packet {run.packet_id}, not {body.packet_id}"]})
    job = runner.queue.enqueue(
        action="receive",
        project_id=project_id,
        payload={"run_id": run_id, "mode": mode},
        packet_id=run.packet_id,
        run_id=run_id,
        node_id=run.node_id,
        now=c.now(),
    )
    return {"id": job.id, "state": job.state, "seq": job.seq}


@router.post("/plugin/jobs", status_code=201)
def create_job(request: Request, body: JobBody) -> dict[str, Any]:
    c = ctx(request)
    runner = runner_of(request)
    if body.action not in ACTIONS:
        raise ApiError(422, {"error": "unknown_action", "detail": f"the dock handles {', '.join(ACTIONS)}"})
    if body.action == "receive":
        return _create_receive(request, body)
    if not body.packet_id:
        raise ApiError(422, {"errors": [f"{body.action} needs a packet_id"]})
    project_id = c.index.locate_packet(body.packet_id)
    if project_id is None:
        raise ApiError(404, {"error": "not_found", "packet_id": body.packet_id})
    args = {k: v for k, v in (body.model_extra or {}).items() if k not in ("project_id", "run_id", "node_id")}
    args["packet_id"] = body.packet_id
    try:
        job = runner.queue.enqueue(
            action=body.action, project_id=project_id, payload=args, packet_id=body.packet_id, now=c.now()
        )
    except (UnknownAction, ValueError) as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    return {"id": job.id, "state": job.state, "seq": job.seq}


@router.get("/plugin/status")
def plugin_status(request: Request) -> dict[str, Any]:
    queue = runner_of(request).queue
    return {"last_poll_utc": queue.last_poll_utc(), **queue.counts()}


@router.get("/plugin/jobs/{job_id}")
def get_job(request: Request, job_id: str) -> dict[str, Any]:
    job = runner_of(request).queue.get(job_id)
    if job is None:
        raise ApiError(404, {"error": "not_found", "job_id": job_id})
    return job.to_dict()


@router.post("/plugin/jobs/{job_id}/result")
def job_result(request: Request, job_id: str, body: ResultBody) -> dict[str, Any]:
    c = ctx(request)
    runner = runner_of(request)
    if runner.queue.get(job_id) is None:
        raise ApiError(404, {"error": "not_found", "job_id": job_id})
    try:
        job = runner.queue.finish(job_id, ok=body.ok, result=body.model_dump(exclude_none=True), now=c.now())
    except JobStateError as e:
        raise ApiError(409, {"error": "job_not_active", "state": e.state, "detail": str(e)}) from e
    except ValueError as e:  # NaN or something that is not JSON
        raise ApiError(422, {"errors": [str(e)]}) from e
    c.pipeline.publish(
        job.project_id,
        job.run_id,
        {
            "kind": "plugin.job",
            "job_id": job.id,
            "action": job.action,
            "state": job.state,
            "run_id": job.run_id,
        },
    )
    return job.to_dict()


@router.post("/projects/{project_id}/nodes/{node_id}/preview")
def preview(request: Request, project_id: str, node_id: str, body: PreviewBody) -> dict[str, Any]:
    runner = runner_of(request)
    try:
        return runner.preview(project_id, node_id, body.what, body.params)
    except PipelineError as e:
        raise pipeline_error(e) from e
