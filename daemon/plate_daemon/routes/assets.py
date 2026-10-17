"""Asset route: `GET /assets/{hash}?fmt=png|rgba16f|thumb[&project_id=]` (formats: `plate_daemon.assets`).

A hash names immutable bytes, so every answer carries `Cache-Control: private, max-age=31536000, immutable`
(private: the token-gated bytes never sit in a shared cache). 404 for an unknown or malformed key, 415 for
a pixel format of something that is not an image.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from ..assets import AssetFormatError, AssetNotFound, locate, render
from . import API, ApiError, ctx, require_token

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])

CACHE = "private, max-age=31536000, immutable"


@router.get("/assets/{key}")
async def get_asset(
    request: Request,
    key: str,
    fmt: Annotated[str | None, Query()] = None,
    project_id: Annotated[str | None, Query()] = None,
) -> Response:
    c = ctx(request)

    def work() -> tuple[bytes, str, dict[str, str]]:
        asset = locate(key, c.index.project_ids(), c.cas_for, project_id)
        return render(asset, fmt)

    try:
        body, media_type, headers = await run_in_threadpool(work)
    except AssetNotFound as e:
        raise ApiError(404, {"error": "not_found", "hash": key}) from e
    except AssetFormatError as e:
        raise ApiError(415, {"error": "unsupported_format", "detail": str(e)}) from e
    except ValueError as e:  # an invalid project id
        raise ApiError(422, {"errors": [str(e)]}) from e
    return Response(content=body, media_type=media_type, headers={**headers, "Cache-Control": CACHE})
