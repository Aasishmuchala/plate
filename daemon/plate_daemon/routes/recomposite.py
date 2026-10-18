"""`POST /runs/{id}/recomposite {feather_px?, grow_px?, mask_hash?}`: a free re-mask of a stored raw take (router
plan T99, decision 54).

The run is a take of an Edit node that kept its raw layer (`normalized.raw_key`, see `recomposite`). The node's Mask
is read again from the graph (so the artist edits the Mask node, then presses Re-mask); `mask_hash` names a
plate-sized grey PNG already in the project's CAS (`POST /assets?kind=mask`) that replaces it; `grow_px` (-32..32)
dilates or erodes whichever mask applies; `feather_px` (4..12) is the feather inside the mask (the take's own when
left out). The route quotes the re-mask (0 USD, local C0), approves it as `human` (the artist pressed the button),
runs it to its end (a composite and the gates, no engine) and answers the new run as `GET /runs/{id}` does.

Refusals: 404 unknown run; 422 `not_recompositable` (not a pipeline take, no stored raw take, size map or egress
PNG, a take of another node, the node's Plate now names another packet), `bad_mask`, `mask_empty`, `mask_locked`,
`mask_missing`, `mask_size` and the canvas rules; 403 custody (never for a local C0 route).

The route drives `recomposite.install(pipeline)`'s runner itself, so it never falls through to a vendor quote even
when the app did not install the wrappers.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import recomposite
from ..custody import CustodyViolation
from ..machine import IllegalTransition, get_run
from ..pipeline import PipelineError
from ..quotes import QuoteExpired
from . import API, ApiError, ctx, custody_error, illegal, pipeline_error, require_token, run_project
from .runs import gate_overlays, run_view

router = APIRouter(prefix=API, dependencies=[Depends(require_token), Depends(gate_overlays)])

log = logging.getLogger("plate_daemon")

APPROVER = "human"


class RecompositeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feather_px: float | None = Field(
        default=None, ge=recomposite.FEATHER_RANGE[0], le=recomposite.FEATHER_RANGE[1]
    )
    grow_px: int | None = Field(default=None, ge=recomposite.GROW_RANGE[0], le=recomposite.GROW_RANGE[1])
    mask_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


@router.post("/runs/{run_id}/recomposite")
def recomposite_route(
    request: Request, run_id: str, body: Annotated[RecompositeBody | None, Body()] = None
) -> dict[str, Any]:
    c = ctx(request)
    body = body or RecompositeBody()
    pid = run_project(c, run_id)
    runner = recomposite.install(c.pipeline)
    params: dict[str, Any] = {recomposite.PARAM: run_id}
    if body.grow_px is not None:
        params[recomposite.GROW_PARAM] = body.grow_px
    if body.mask_hash is not None:
        params[recomposite.MASK_PARAM] = body.mask_hash
    quote_body: dict[str, Any] = {"params": params}
    if body.feather_px is not None:
        quote_body["feather_px"] = body.feather_px
    with c.index.project(pid) as conn:
        node_id = get_run(conn, run_id).node_id
        if not node_id:
            raise ApiError(
                422, {"error": "not_recompositable", "detail": f"run {run_id} was not made by a canvas node"}
            )
        try:
            run, _quote = runner.quote(conn, pid, node_id, quote_body, now=c.now())
            runner.approve(conn, pid, run.id, APPROVER, now=c.now())
            c.pipeline.queue(conn, pid, run.id)
        except CustodyViolation as e:
            raise custody_error(e) from e
        except PipelineError as e:
            raise pipeline_error(e) from e
        except QuoteExpired as e:
            raise ApiError(409, {"error": "quote_expired", "detail": str(e)}) from e
        except IllegalTransition as e:
            raise illegal(e) from e
    try:
        c.pipeline.execute(run.id, pid)
    except Exception as e:  # the run's state and reason say what stopped it; the answer carries them
        log.exception("re-mask %s of %s stopped: %s", run.id, run_id, e)
    with c.index.project(pid) as conn:
        return run_view(conn, run.id)
