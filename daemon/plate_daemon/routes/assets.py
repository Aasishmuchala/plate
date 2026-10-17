"""Asset routes (formats: `plate_daemon.assets`).

`GET /assets/{hash}[?fmt=png|rgba16f|thumb][&project_id=]`: a hash names immutable bytes, so every answer
carries `Cache-Control: private, max-age=31536000, immutable` (private: the token-gated bytes never sit in a
shared cache) and `X-Plate-Source: cas | packet` (a packet file is served by reference, decision 32). With no
`fmt` the file itself is streamed (`FileResponse`: Range and conditional requests, so a video seeks); with
one, the decoded image is rendered in memory. 404 for an unknown or malformed key, 415 for a pixel format of
something that is not an image.

`POST /assets?kind=mask&project_id=<id>`: store an uploaded mask in the project's CAS as an 8-bit grey PNG.
The body is the raw image (`Content-Type: image/png`, at most 32 MiB). 201 `{hash, width, height,
project_id}`; 422 `bad_kind` / `project_required` / `bad_mask`, 404 for an unknown project, 413 over the limit.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from ..assets import MASK_MAX_BYTES, AssetFormatError, AssetNotFound, BadMask, locate, mask_png, render
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

    def find():
        return locate(key, c.index.project_ids(), c.cas_for, project_id, index=c.index)

    try:
        asset = await run_in_threadpool(find)
        source = {"Cache-Control": CACHE, "X-Plate-Source": "packet" if asset.by_reference else "cas"}
        if not fmt:
            return FileResponse(asset.path, media_type=asset.media_type, headers=source)
        body, media_type, headers = await run_in_threadpool(render, asset, fmt)
    except AssetNotFound as e:
        raise ApiError(404, {"error": "not_found", "hash": key}) from e
    except AssetFormatError as e:
        raise ApiError(415, {"error": "unsupported_format", "detail": str(e)}) from e
    except ValueError as e:  # an invalid project id
        raise ApiError(422, {"errors": [str(e)]}) from e
    return Response(content=body, media_type=media_type, headers={**headers, **source})


async def _read_capped(request: Request, limit: int) -> bytes | None:
    """The request body, or None once it passes `limit` bytes (the stream is not read further)."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/assets")
async def post_asset(
    request: Request,
    kind: Annotated[str | None, Query()] = None,
    project_id: Annotated[str | None, Query()] = None,
) -> Response:
    c = ctx(request)
    if kind != "mask":
        raise ApiError(422, {"error": "bad_kind", "detail": "kind must be 'mask'"})
    if not project_id:
        raise ApiError(422, {"error": "project_required"})
    try:
        project = await run_in_threadpool(c.pipeline.load_project, project_id)
    except ValueError as e:  # an invalid project id
        raise ApiError(422, {"errors": [str(e)]}) from e
    if project is None:
        raise ApiError(404, {"error": "not_found", "project_id": project_id})
    raw = await _read_capped(request, MASK_MAX_BYTES)
    if raw is None:
        raise ApiError(413, {"error": "too_large", "limit_bytes": MASK_MAX_BYTES})

    def work() -> dict[str, object]:
        png, w, h = mask_png(raw)
        key = c.cas_for(project_id).put_bytes(png, "png")
        return {"hash": key, "width": w, "height": h, "project_id": project_id}

    try:
        out = await run_in_threadpool(work)
    except BadMask as e:
        raise ApiError(422, {"error": "bad_mask", "detail": str(e)}) from e
    return JSONResponse(out, status_code=201)
