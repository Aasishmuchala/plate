"""The client review route (router plan T74; BUILD_PLAN decisions 21 and 39).

- `POST /projects/{id}/review {run_ids: [...], watermark?: "REVIEW", max_width?: 2048, title?, subtitle?, author?,
  reply_to?}` ->
  `{zip_path, bytes, takes}`: `review_bundle.export_review` writes the bundle (a zip of one static HTML page
  with an A/B wipe per take, watermarked 8-bit proxies no wider than `max_width` (2048 at most) and gate
  badges) into the project's `review/` folder. A take's images are the run's gated output (`outputs[0]` in
  the project's CAS) against its packet's display master, so the client sees what the gates scored. `subtitle`
  and `author` (120 characters at most each) fill the cover header under the title.
- Refused before anything is written: 404 unknown project, or a run that is not in it; 409 `not_reviewable`
  for a take a client must not see, as `delivery.guard.authorize` decides it (`code`): previs
  (PREVIS_NOT_DELIVERABLE, a draft; `motion.previs.is_previs` on the run as `GET /runs/{id}` answers it, and
  its inputs, params and normalized record), GATE_FAIL without an override (GATE_FAIL; one recorded by `POST
  /runs/{id}/override` lets it in), and any state but DONE or GATE_FAIL (RUN_NOT_DELIVERABLE). A take with no
  stored gate report is allowed and shows "not scored". 422 `review_error` for a take the exporter cannot
  build (no output in the CAS, a video take, a packet with no display master) and `{"errors"}` for a bad
  watermark, width or reply-to address.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from ..config import check_project_id
from ..delivery.guard import DeliveryRefused, Provenance, authorize
from ..export import recorded_override
from ..machine import RunNotFound, get_run
from ..models import Run
from ..motion.previs import is_previs
from ..pipeline import PipelineError
from ..review_bundle import (
    COVER_MAX_CHARS,
    DEFAULT_WATERMARK,
    MAX_WIDTH_LIMIT,
    ReviewError,
    TakeSource,
    export_review,
)
from . import API, ApiError, Ctx, ctx, pipeline_error, require_token
from .runs import run_view

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])

MAX_TAKES = 200


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_ids: list[str] = Field(min_length=1, max_length=MAX_TAKES)
    watermark: str = DEFAULT_WATERMARK
    max_width: int = MAX_WIDTH_LIMIT
    title: str | None = None
    subtitle: str | None = Field(default=None, max_length=COVER_MAX_CHARS)
    author: str | None = Field(default=None, max_length=COVER_MAX_CHARS)
    reply_to: str | None = None


def _previs(run: Run, view: dict[str, Any]) -> bool:
    return any(is_previs(obj) for obj in (view, run.inputs, run.params, run.normalized))


def check_reviewable(conn: Any, run: Run, project_dir: Any) -> None:
    """Raise ApiError 409 `not_reviewable` for a previs take, a GATE_FAIL take without an override, or a run
    that has no take (decision 39: the same guard as delivery, a missing report allowed)."""
    view = run_view(conn, run.id)
    prov = Provenance.from_run(run, project_dir=project_dir, override_reason=recorded_override(conn, run.id))
    if _previs(run, view):
        prov = dataclasses.replace(prov, previs=True)
    try:
        authorize(prov, require_report=False)
    except DeliveryRefused as e:
        raise ApiError(
            409, {"error": "not_reviewable", "code": e.code, "run_id": run.id, "detail": str(e)}
        ) from e


def take_source(c: Ctx, run: Run) -> TakeSource:
    """The run's gated output against its packet's display master (the plate the gates compared it to)."""
    key = run.outputs[0] if run.outputs else None
    candidate = c.cas_for(run.project_id).find(key) if key else None
    if candidate is None:
        raise ReviewError(f"run {run.id} has no output in the project CAS")
    pv = c.pipeline.packet(run.project_id, run.packet_id)
    plate = pv.root / pv.files[pv.plate_role]["path"]
    return TakeSource(run_id=run.id, plate=plate, candidate=candidate, plate_manifest=pv.manifest)


@router.post("/projects/{project_id}/review")
def review_route(request: Request, project_id: str, body: ReviewBody) -> dict[str, Any]:
    c = ctx(request)
    try:
        check_project_id(project_id)
    except ValueError as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    project = c.pipeline.load_project(project_id)
    if project is None:
        raise ApiError(404, {"error": "not_found", "project_id": project_id})
    run_ids = list(dict.fromkeys(body.run_ids))
    sources: dict[str, TakeSource] = {}
    try:
        with c.index.project(project_id) as conn:
            for rid in run_ids:
                if c.index.locate_run(rid) != project_id:
                    raise ApiError(404, {"error": "not_found", "run_id": rid, "project_id": project_id})
                try:
                    run = get_run(conn, rid)
                except RunNotFound as e:
                    raise ApiError(404, {"error": "not_found", "run_id": rid}) from e
                check_reviewable(conn, run, project.root)
                sources[rid] = take_source(c, run)
        options: dict[str, Any] = {"reply_to": body.reply_to, "sources": sources, "clock": c.now}
        if body.title is not None:
            options["title"] = body.title
        options["subtitle"], options["author"] = body.subtitle, body.author
        zip_path = export_review(
            project, run_ids, project.review_dir, body.watermark, body.max_width, **options
        )
    except PipelineError as e:
        raise pipeline_error(e) from e
    except ReviewError as e:
        raise ApiError(422, {"error": "review_error", "detail": str(e)}) from e
    except ValueError as e:  # watermark, max_width, title, reply_to
        raise ApiError(422, {"errors": [str(e)]}) from e
    return {"zip_path": str(zip_path), "bytes": zip_path.stat().st_size, "takes": len(run_ids)}
