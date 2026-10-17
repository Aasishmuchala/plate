"""`POST /packets/{id}/carry-over {from_packet_id?}`: carry the approved layers of an older packet onto this one
(router plan T96; the manual twin of the ingest hook, which `settings.auto_carry_over` can switch off).

`from_packet_id` defaults to the `supersedes` of this packet's manifest. Both packets must be ingested into the same
project. The answer is `revisions.carry_over`'s list, one result per accepted DONE run of the old packet:
`{run_id, ids, status: carried | dropped, code, reason, iou, gates, new_run_id, output}`; a carried layer is already a
new run on this packet (`engine_id: carry-over`, no spend) that still needs the artist's accept. An old packet with
no accepted run answers `[]`. The same layer carried twice answers the first run (`code: already_carried`).

Refusals: 404 `not_found` (the packet, the predecessor, or the project), 409 `not_ingested` (either packet is still
hashing or failed), 409 `no_predecessor` (no `from_packet_id` and the manifest supersedes nothing),
409 `packet_unreadable` (the share went away), 422 for a predecessor that is the packet itself.
A request from the UI (`X-Plate-Client: ui`) counts as a human at the keyboard.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import carryover
from ..ingest import get_packet
from ..pipeline import PipelineError
from ..revisions import RevisionError
from . import API, ApiError, ctx, packet_project, pipeline_error, require_token

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])

CLIENT_HEADER = "x-plate-client"
UI_CLIENT = "ui"


class CarryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_packet_id: Annotated[str | None, Field(min_length=1)] = None


def _ingested(c: Any, project_id: str, packet_id: str) -> None:
    with c.index.project(project_id) as conn:
        status = get_packet(conn, packet_id, with_blobs=False)["status"]
    if status != "ingested":
        raise ApiError(409, {"error": "not_ingested", "packet_id": packet_id, "status": status})


@router.post("/packets/{packet_id}/carry-over")
def carry_over_route(request: Request, packet_id: str, body: CarryBody | None = None) -> list[dict[str, Any]]:
    c = ctx(request)
    project_id = packet_project(c, packet_id)
    _ingested(c, project_id, packet_id)
    try:
        new_pv = c.pipeline.packet(project_id, packet_id)
    except PipelineError as e:
        raise pipeline_error(e) from e
    except (OSError, ValueError) as e:  # the share went away (or the folder was edited) after the ingest
        raise ApiError(
            409, {"error": "packet_unreadable", "packet_id": packet_id, "detail": f"{type(e).__name__}: {e}"}
        ) from e

    old_id = (body.from_packet_id if body else None) or new_pv.manifest.get("supersedes")
    if not isinstance(old_id, str) or not old_id:
        raise ApiError(
            409,
            {
                "error": "no_predecessor",
                "packet_id": packet_id,
                "detail": "this packet's manifest supersedes nothing; pass from_packet_id",
            },
        )
    if old_id == packet_id:
        raise ApiError(422, {"errors": ["from_packet_id is the packet itself"]})
    if c.index.locate_packet(old_id) != project_id:
        raise ApiError(404, {"error": "not_found", "packet_id": old_id, "project_id": project_id})
    _ingested(c, project_id, old_id)

    project = c.pipeline.load_project(project_id)
    if project is None:
        raise ApiError(404, {"error": "not_found", "project_id": project_id})
    if request.headers.get(CLIENT_HEADER, "").strip().lower() == UI_CLIENT:
        c.pipeline.touch_human()
    try:
        return carryover.on_supersede(c.pipeline, project, new_pv, old_id, bus=c.bus, now=c.now())
    except PipelineError as e:
        raise pipeline_error(e) from e
    except RevisionError as e:
        raise ApiError(409, {"error": "carry_over_refused", "detail": str(e)}) from e
    except (OSError, ValueError) as e:  # a stored layer or plate the share no longer serves
        raise ApiError(
            409, {"error": "packet_unreadable", "packet_id": old_id, "detail": f"{type(e).__name__}: {e}"}
        ) from e
