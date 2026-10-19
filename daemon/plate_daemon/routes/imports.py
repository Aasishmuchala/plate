"""The external-import route (router plan T74, decision 42): a file made outside PLATE comes back as a run.

- `POST /projects/{id}/import {path, packet_id, node_id?, source: "higgsfield"|"runway"|"manual", cost_usd?,
  note?}` -> 201 with the run as `GET /runs/{id}` answers it (`run_view`), at its end: DONE or GATE_FAIL on
  the packet's gates (or FAILED with the reason). `imports.import_file` does the work: the file is copied into
  the project's CAS as it is, a lineage-tagged 16-bit PNG of it is the run's `outputs[0]`, the run is quoted at
  the declared `cost_usd`, approved by the human who imported it and gated like an Edit take, and its ledger
  records carry `data.imported: true`. Nothing leaves the box.
- Refusals, all before a run row exists: 404 unknown project, packet or node; 422 `unknown_source`, a `path`
  that is not absolute, `not_a_file` (a FIFO, device, socket or directory: `os.stat` says so and nothing is
  opened, so a hung mount or a FIFO with no writer cannot hold a worker thread), `unreadable_image` (a missing
  file, or scene-linear), `unreadable_video` (a clip of fewer than 2 frames, or a still with a clip suffix),
  `bad_cost` (`cost_usd` must be finite and at most 1000), `too_many_pixels` (a header over 100 000 000 px),
  `size_mismatch` (`{plate: [w, h], file: [w, h]}`: an import is never resized onto the plate, and the header
  is compared before the file is decoded); 413 `too_large` (over 1 GiB); 403 `custody` when the source's grade
  is not one the project and the packet allow: a Higgsfield or Runway file is grade D (its plate already went
  to that vendor), so an NDA project refuses it. 409 `approval_refused` (the run is CANCELLED) when an approver
  other than a human is turned down by the budget rules.
- Declared money is spent elsewhere (decision 89): the run's reservation is marked `external`, so it counts
  against neither the daily nor the unattended cap (`budgets.committed_since`); the ledger and `plate spend`
  still carry the price. A `cost_usd` above `per_user_daily_cap_usd` answers 201 with a `warnings` line saying
  so; under the cap there is no `warnings` key.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..config import check_project_id
from ..custody import CustodyViolation
from ..imports import MAX_NOTE_CHARS, check_import_file, import_file
from ..machine import IllegalTransition
from ..pipeline import PipelineError
from . import API, ApiError, ctx, custody_error, illegal, pipeline_error, require_token
from .runs import run_view

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    packet_id: str = Field(min_length=1)
    node_id: str | None = Field(default=None, min_length=1)
    source: str = Field(min_length=1)  # checked by imports.import_route: 422 unknown_source
    cost_usd: float = Field(default=0.0, ge=0)
    note: str | None = Field(default=None, max_length=MAX_NOTE_CHARS)


@router.post("/projects/{project_id}/import", status_code=201)
def import_route(request: Request, project_id: str, body: ImportBody) -> JSONResponse:
    c = ctx(request)
    try:
        check_project_id(project_id)
    except ValueError as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    path = Path(body.path)
    if not path.is_absolute():
        raise ApiError(422, {"errors": [f"path must be an absolute file path, got {body.path!r}"]})
    try:
        check_import_file(path)  # stat only: 422 not_a_file / unreadable_*, 413 too_large; never opens a FIFO
        run = import_file(
            c.pipeline,
            project_id,
            body.packet_id,
            path,
            source=body.source,
            node_id=body.node_id,
            cost_usd=body.cost_usd,
            note=body.note,
            by="human",
        )
    except CustodyViolation as e:
        raise custody_error(e) from e
    except PipelineError as e:
        raise pipeline_error(e) from e
    except IllegalTransition as e:  # the run was cancelled while it was being imported
        raise illegal(e) from e
    with c.index.project(project_id) as conn:
        view = run_view(conn, run.id)
    cap = c.pipeline.policy.per_user_daily_cap_usd
    if body.cost_usd > cap:
        view = {
            **view,
            "warnings": [
                f"declared cost {body.cost_usd:.2f} USD is above the daily cap {cap:.2f} USD; it is recorded "
                "as external spend and does not count against the automation caps"
            ],
        }
    return JSONResponse(view, status_code=201)
