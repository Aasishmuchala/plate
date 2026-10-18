"""Asset routes (formats: `plate_daemon.assets`).

`GET /assets/{hash}[?fmt=png|rgba16f|thumb][&project_id=]`: a hash names immutable bytes, so every answer
carries `Cache-Control: private, max-age=31536000, immutable` (private: the token-gated bytes never sit in a
shared cache) and `X-Plate-Source: cas | packet` (a packet file is served by reference, decision 32). With no
`fmt` the file itself is streamed (`FileResponse`: Range and conditional requests, so a video seeks); with
one, the decoded image is rendered in memory. 404 for an unknown or malformed key, 415 for a pixel format of
something that is not an image.

Every upload is judged on its header before a pixel is decoded (router plan T136): `plate_pixels.imghdr`
names the declared size from the first bytes, so a 410 KiB PNG that declares 12000 x 12000 x 3 costs a header
read, not 432 MB. Bytes whose header it does not read (empty, a GIF, a clip, truncated) are 422 `bad_image`
(`"not a PNG/JPEG/WebP/BMP/TIFF header"`); a declared `width * height` over `UPLOAD_MAX_PIXELS` (100 000 000,
in `plate_daemon.assets`) is 422 `{"error": "too_many_pixels", "declared": [w, h], "limit": N}`. Both come
before `cv2.imdecode`, for masks and references alike.

`POST /assets?kind=mask&project_id=<id>[&packet_id=<id>]`: store an uploaded mask in the project's CAS as an
8-bit grey PNG. The body is the raw image (`Content-Type: image/png`, at most 32 MiB). With `packet_id` (a packet
of that project) a mask whose size is not the packet's plate size is resized to it nearest-neighbour and
thresholded `> 127` to 0/255, so a mask painted at the editor's working size lands where the quote checks it
(router plan T82). 201 `{hash, width, height, project_id, scaled_from: [w, h] | null}`; 422 `bad_kind` /
`project_required` / `bad_image` / `too_many_pixels` / `bad_mask` (an image that decodes but is not an 8-bit
grey, RGB or RGBA mask), 404 for an unknown project or packet, 413 over the limit.

`POST /assets?kind=reference&project_id=<id>[&note=<text>]`: store a reference image (decision 46: an engine
input, never pixels of the output). The body is a PNG or JPEG (at most 32 MiB); it is decoded as 8-bit colour
(alpha dropped), a side over `REFERENCE_MAX_SIDE` is shrunk with INTER_AREA (never enlarged), and it is
re-encoded as an 8-bit RGB PNG, so no metadata of the upload survives. The hash is appended to
`<project>/references.json` (`{schema, references: [{hash, width, height, bytes, uploaded_utc, note?}]}`,
idempotent on the hash). 201 `{hash, width, height, kind: "reference", project_id, scaled_from}`; refusals as
for masks, with 422 `bad_image` / `too_many_pixels`. `GET /projects/{id}/references` lists the entries in upload order.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Any

import cv2
import numpy as np
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from ..assets import (
    MASK_MAX_BYTES,
    AssetFormatError,
    AssetNotFound,
    BadImage,
    BadMask,
    TooManyPixels,
    check_header,
    locate,
    mask_png,
    render,
)
from ..fsutil import read_json, write_json_atomic
from ..lock import ProjectLocked
from ..pipeline import PipelineError
from ..projects import Project
from ..timeutil import iso
from . import API, ApiError, Ctx, ctx, pipeline_error, require_token
from .pixels import plate_size

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])

CACHE = "private, max-age=31536000, immutable"
KINDS = ("mask", "reference")
REFERENCE_MAX_BYTES = 32 * 1024 * 1024
REFERENCE_MAX_SIDE = 4096  # a longer side is shrunk to this; nothing is ever enlarged
REFERENCES_JSON = "references.json"
REFERENCES_SCHEMA = 1
NOTE_MAX_CHARS = 500
LOCK_TRIES = 40  # x LOCK_RETRY_S: the project lock is held for milliseconds at a time
LOCK_RETRY_S = 0.05
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


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
    except TooManyPixels as e:
        raise too_many_pixels(e) from e
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


def too_many_pixels(e: TooManyPixels) -> ApiError:
    """422 `too_many_pixels`: the header declared more than the limit, and nothing was decoded."""
    return ApiError(422, {"error": "too_many_pixels", "declared": list(e.declared), "limit": e.limit})


def bad_image(e: BadImage) -> ApiError:
    return ApiError(422, {"error": "bad_image", "detail": str(e)})


def fit_mask(png: bytes, size: tuple[int, int]) -> tuple[bytes, int, int, list[int] | None]:
    """(PNG, width, height, scaled_from) of an 8-bit grey mask PNG at the plate's `size` = (W, H).

    A mask already at that size is kept byte for byte; another is resized nearest-neighbour (no new grey
    levels at the edges) and thresholded `> 127` to 0/255, so the quote's `mask_size` check and the
    staging's `> 127` read agree with what the artist painted.

    The header is read first (`BadImage` / `TooManyPixels`, nothing decoded); a PNG that then does not decode is
    a `BadMask`."""
    check_header(png)
    grey = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_UNCHANGED)
    if grey is None:
        raise BadMask("the mask is not a decodable image")
    h, w = grey.shape[:2]
    if (w, h) == size:
        return png, w, h, None
    scaled = cv2.resize(grey, size, interpolation=cv2.INTER_NEAREST)
    out = np.where(scaled > 127, 255, 0).astype(np.uint8)
    ok, buf = cv2.imencode(".png", np.ascontiguousarray(out))
    if not ok:
        raise BadMask("cv2 could not encode the scaled mask")
    return bytes(buf), size[0], size[1], [w, h]


def reference_png(data: bytes) -> tuple[bytes, int, int, list[int] | None]:
    """(8-bit RGB PNG, width, height, scaled_from) of an uploaded PNG or JPEG reference.

    Decoded as 8-bit colour (alpha and 16-bit depth dropped, a JPEG's EXIF orientation applied by the decoder),
    shrunk with INTER_AREA when a side passes `REFERENCE_MAX_SIDE`, then re-encoded: the PNG carries pixels
    only, so nothing of the upload's metadata (EXIF, GPS, ICC, XMP) reaches the CAS.

    The header is read before the decode: `BadImage` when it is none PLATE reads, `TooManyPixels` over
    `UPLOAD_MAX_PIXELS` (a 16000 x 16000 reference is refused, not shrunk to 4096)."""
    if not (data.startswith(PNG_MAGIC) or data.startswith(JPEG_MAGIC)):
        raise BadImage("a reference is a PNG or JPEG image")
    check_header(data)
    try:
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    except cv2.error as e:
        raise BadImage(f"the body is not a decodable image ({e})") from e
    if img is None or img.ndim != 3 or img.shape[2] != 3 or img.size == 0:
        raise BadImage("the body is not a decodable image")
    h, w = img.shape[:2]
    scaled_from = None
    long_side = max(w, h)
    if long_side > REFERENCE_MAX_SIDE:
        k = REFERENCE_MAX_SIDE / long_side
        size = (
            (REFERENCE_MAX_SIDE, max(1, round(h * k)))
            if w >= h
            else (max(1, round(w * k)), REFERENCE_MAX_SIDE)
        )
        img = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
        scaled_from = [w, h]
        h, w = img.shape[:2]
    ok, buf = cv2.imencode(".png", np.ascontiguousarray(img))
    if not ok:
        raise BadImage("cv2 could not encode the reference")
    return bytes(buf), w, h, scaled_from


def _project_or_404(c: Ctx, project_id: str) -> Project:
    try:
        project = c.pipeline.load_project(project_id)
    except ValueError as e:  # an invalid project id
        raise ApiError(422, {"errors": [str(e)]}) from e
    if project is None:
        raise ApiError(404, {"error": "not_found", "project_id": project_id})
    return project


def _plate_size_of(c: Ctx, project_id: str, packet_id: str) -> tuple[int, int]:
    """(W, H) of the packet's display master; 404 when the packet is not one of the project's."""
    try:
        pv = c.pipeline.packet(project_id, packet_id)
    except PipelineError as e:
        if e.code == "unknown_packet":
            raise ApiError(
                404, {"error": "not_found", "packet_id": packet_id, "project_id": project_id}
            ) from e
        raise pipeline_error(e) from e
    except (OSError, ValueError) as e:  # the share went away, or the folder was edited, after the ingest
        raise ApiError(
            409, {"error": "packet_unreadable", "packet_id": packet_id, "detail": f"{type(e).__name__}: {e}"}
        ) from e
    try:
        h, w = plate_size(pv)
    except PipelineError as e:
        raise pipeline_error(e) from e
    except (OSError, ValueError) as e:
        raise ApiError(
            409, {"error": "packet_unreadable", "packet_id": packet_id, "detail": f"{type(e).__name__}: {e}"}
        ) from e
    return w, h


def references_path(project: Project) -> Path:
    return project.root / REFERENCES_JSON


def read_references(project: Project) -> list[dict[str, Any]]:
    """The entries of `<project>/references.json` (none when the file does not exist yet)."""
    path = references_path(project)
    if not path.is_file():
        return []
    doc = read_json(path)
    refs = doc.get("references") if isinstance(doc, dict) else None
    if not isinstance(refs, list) or not all(isinstance(r, dict) for r in refs):
        raise ValueError(f"{path}: expected {{schema, references: [entry, ...]}}")
    return refs


def add_reference(project: Project, entry: dict[str, Any]) -> dict[str, Any]:
    """Append `entry` unless its hash is listed already (then the first entry stands); under the project lock,
    retried briefly because another writer holds it for milliseconds at a time."""
    for attempt in range(LOCK_TRIES):
        try:
            with project.lock(timeout_s=0.0):
                refs = read_references(project)
                for old in refs:
                    if old.get("hash") == entry["hash"]:
                        return old
                refs.append(entry)
                write_json_atomic(
                    references_path(project), {"schema": REFERENCES_SCHEMA, "references": refs}, indent=1
                )
                return entry
        except ProjectLocked:
            if attempt == LOCK_TRIES - 1:
                raise
            time.sleep(LOCK_RETRY_S)
    raise AssertionError("unreachable")


@router.post("/assets")
async def post_asset(
    request: Request,
    kind: Annotated[str | None, Query()] = None,
    project_id: Annotated[str | None, Query()] = None,
    packet_id: Annotated[str | None, Query()] = None,
    note: Annotated[str | None, Query(max_length=NOTE_MAX_CHARS)] = None,
) -> Response:
    c = ctx(request)
    if kind not in KINDS:
        raise ApiError(422, {"error": "bad_kind", "detail": "kind must be 'mask' or 'reference'"})
    if not project_id:
        raise ApiError(422, {"error": "project_required"})
    project = await run_in_threadpool(_project_or_404, c, project_id)
    if kind == "reference":
        return await _post_reference(request, c, project, note)
    size = await run_in_threadpool(_plate_size_of, c, project_id, packet_id) if packet_id else None
    raw = await _read_capped(request, MASK_MAX_BYTES)
    if raw is None:
        raise ApiError(413, {"error": "too_large", "limit_bytes": MASK_MAX_BYTES})

    def work() -> dict[str, object]:
        png, w, h = mask_png(raw)
        scaled_from = None
        if size is not None:
            png, w, h, scaled_from = fit_mask(png, size)
        key = c.cas_for(project_id).put_bytes(png, "png")
        out: dict[str, object] = {"hash": key, "width": w, "height": h, "project_id": project_id}
        if packet_id:
            out["packet_id"] = packet_id
        out["scaled_from"] = scaled_from
        return out

    try:
        out = await run_in_threadpool(work)
    except BadMask as e:
        raise ApiError(422, {"error": "bad_mask", "detail": str(e)}) from e
    except BadImage as e:
        raise bad_image(e) from e
    except TooManyPixels as e:
        raise too_many_pixels(e) from e
    return JSONResponse(out, status_code=201)


async def _post_reference(request: Request, c: Ctx, project: Project, note: str | None) -> Response:
    raw = await _read_capped(request, REFERENCE_MAX_BYTES)
    if raw is None:
        raise ApiError(413, {"error": "too_large", "limit_bytes": REFERENCE_MAX_BYTES})
    project_id = project.project_id

    def work() -> dict[str, object]:
        png, w, h, scaled_from = reference_png(raw)
        key = c.cas_for(project_id).put_bytes(png, "png")
        entry: dict[str, Any] = {
            "hash": key,
            "width": w,
            "height": h,
            "bytes": len(png),
            "uploaded_utc": iso(c.now()),
        }
        if note:
            entry["note"] = note
        add_reference(project, entry)
        return {
            "hash": key,
            "width": w,
            "height": h,
            "kind": "reference",
            "project_id": project_id,
            "scaled_from": scaled_from,
        }

    try:
        out = await run_in_threadpool(work)
    except BadImage as e:
        raise bad_image(e) from e
    except TooManyPixels as e:
        raise too_many_pixels(e) from e
    except ProjectLocked as e:
        raise ApiError(409, {"error": "project_locked", "project_id": project_id, "detail": str(e)}) from e
    except ValueError as e:  # references.json was edited into something else
        raise ApiError(409, {"error": "references_unreadable", "detail": str(e)}) from e
    return JSONResponse(out, status_code=201)


@router.get("/projects/{project_id}/references")
def list_references(request: Request, project_id: str) -> list[dict[str, Any]]:
    project = _project_or_404(ctx(request), project_id)
    try:
        return read_references(project)
    except ValueError as e:
        raise ApiError(409, {"error": "references_unreadable", "detail": str(e)}) from e
