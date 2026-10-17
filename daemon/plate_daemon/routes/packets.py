"""Packet routes: `POST /hash`, `POST /packets`, `GET /packets/{id}`, `GET /packets/{id}/events` (PACKET.md 9).

`POST /packets {packet_path, sender}` needs `X-Plate-Packet-Version` whose major.minor the daemon reads
(`426 {"supported": [...]}` otherwise). The folder is validated and EVERY file it names is re-hashed with
BLAKE3 before anything is accepted (`ingest.prepare_ingest` -> `plate_spec.validate_packet_dir`, check_files):
a mismatch answers `422 {"errors": [...]}` even though the plugin got its hashes from `POST /hash`. Then
202 (new: hashing into the index finishes in a background task, inline when `settings.sync_ingest`),
200 `known` (the same packet again), 409 (the same packet id with a different manifest). The packet stays
where it is (the NAS): the index and the project's `packets.json` hold it by reference.

`POST /hash {paths, payloads}` is BLAKE3 for the stdlib-only Max plugin (Decision 5b), confined to the packets
folders. `GET /packets/{id}/events` streams the ingest log as `text/event-stream` and ends on `ingest.done`
or `ingest.failed` (or after `wait_s`), so a client reconnects with `Last-Event-ID` instead of holding a socket.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ..hashing import BadHashRequest, HashRefused, PayloadTooLarge, hash_paths, hash_payloads
from ..ingest import PacketConflict, PacketInvalid, begin_ingest, get_packet, packet_events, prepare_ingest
from . import (
    API,
    MAX_WAIT_S,
    SSE_POLL_S,
    SSE_RETRY_MS,
    ApiError,
    ctx,
    cursor,
    packet_project,
    require_token,
    sse_frame,
)

router = APIRouter(prefix=API, dependencies=[Depends(require_token)])

VERSION_RE = re.compile(r"^\s*(\d+)\.(\d+)(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?\s*$")


def major_minor(header: str | None) -> str | None:
    m = VERSION_RE.match(header or "")
    return f"{int(m.group(1))}.{int(m.group(2))}" if m else None


def require_packet_version(
    request: Request, x_plate_packet_version: Annotated[str | None, Header()] = None
) -> str:
    # Module level, not a closure: under `from __future__ import annotations` FastAPI resolves a
    # parameter's `Depends(...)` from module globals, and the seed's closure silently became a query param.
    supported = list(request.app.state.settings.packet_versions)
    mm = major_minor(x_plate_packet_version)
    if mm is None or mm not in supported:
        raise ApiError(426, {"supported": supported})
    return mm


class Sender(BaseModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class PacketPost(BaseModel):
    packet_path: str = Field(min_length=1)
    sender: Sender


class HashBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(default_factory=list, max_length=100_000)
    payloads: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------- hashing for the plugin


@router.post("/hash")
def hash_route(request: Request, body: HashBody) -> dict[str, Any]:
    c = ctx(request)
    if not body.paths and not body.payloads:
        raise ApiError(422, {"errors": ["give paths and/or payloads to hash"]})
    try:
        hashes = hash_paths(body.paths, c.settings.packets_dirs)
        named = hash_payloads(body.payloads)
    except PayloadTooLarge as e:
        raise ApiError(413, {"error": "payload_too_large", "detail": str(e)}) from e
    except BadHashRequest as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    except HashRefused as e:
        raise ApiError(403, {"error": "forbidden_path", "detail": str(e)}) from e
    except FileNotFoundError as e:
        raise ApiError(404, {"error": "not_found", "path": str(e)}) from e
    clash = sorted(set(hashes) & set(named))
    if clash:
        raise ApiError(422, {"errors": [f"payload names collide with paths: {clash[:3]}"]})
    return {"hashes": {**hashes, **named}}


# ---------------------------------------------------------------------------- packets


@router.post("/packets", status_code=202)
def post_packet(
    request: Request,
    body: PacketPost,
    background: BackgroundTasks,
    version: Annotated[str, Depends(require_packet_version)],
) -> JSONResponse:
    c = ctx(request)
    try:
        prepared = prepare_ingest(body.packet_path)
    except PacketInvalid as e:
        raise ApiError(422, {"errors": e.errors}) from e
    if major_minor(prepared.manifest.get("packet_version")) != version:
        raise ApiError(
            422,
            {
                "errors": [
                    f"X-Plate-Packet-Version {version} != manifest packet_version "
                    f"{prepared.manifest.get('packet_version')}"
                ]
            },
        )
    packet_id = prepared.packet_id
    owner = c.index.locate_packet(packet_id) or prepared.project_id
    with c.index.project(owner) as conn:
        try:
            status, _rec = begin_ingest(
                conn, body.packet_path, sender=body.sender.model_dump(), prepared=prepared, now=c.now()
            )
        except PacketConflict as e:
            raise ApiError(409, {"error": "conflict", "packet_id": packet_id, "detail": str(e)}) from e
    c.index.remember(packet_id, owner)
    if status == "known":
        return JSONResponse({"packet_id": packet_id, "status": "known"}, status_code=200)
    if c.settings.sync_ingest:
        c.finish_packet(owner, packet_id)
    else:
        background.add_task(c.finish_packet, owner, packet_id)
    return JSONResponse(
        {"packet_id": packet_id, "status": "ingesting", "events_url": f"{API}/packets/{packet_id}/events"},
        status_code=202,
    )


@router.get("/packets/{packet_id}")
def get_packet_route(request: Request, packet_id: str) -> JSONResponse:
    c = ctx(request)
    pid = packet_project(c, packet_id)
    with c.index.project(pid) as conn:
        rec = get_packet(conn, packet_id)
    project = c.pipeline.load_project(pid)
    rec["registered"] = bool(project is not None and packet_id in project.packets())
    return JSONResponse(rec)


@router.get("/packets/{packet_id}/events")
async def packet_events_route(
    request: Request,
    packet_id: str,
    after: Annotated[int | None, Query()] = None,
    wait_s: Annotated[float, Query(ge=0, le=MAX_WAIT_S)] = 60.0,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    c = ctx(request)
    project_id = await run_in_threadpool(packet_project, c, packet_id)

    def read(seq: int) -> tuple[list[dict[str, Any]], str]:
        with c.index.project(project_id) as conn:
            return packet_events(conn, packet_id, seq), get_packet(conn, packet_id, with_blobs=False)[
                "status"
            ]

    async def gen() -> AsyncIterator[str]:
        seq = cursor(after, last_event_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        yield f"retry: {SSE_RETRY_MS}\n\n"
        while True:
            evs, status = await run_in_threadpool(read, seq)
            for ev in evs:
                seq = ev["seq"]
                yield sse_frame(ev, seq)
                if ev["type"] in ("ingest.done", "ingest.failed"):
                    return
            if status != "ingesting" and not evs:
                return
            if loop.time() >= deadline:
                return
            await asyncio.sleep(SSE_POLL_S)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
