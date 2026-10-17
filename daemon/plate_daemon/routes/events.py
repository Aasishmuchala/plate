"""Event routes: the bus (`GET /events?run_id=&project_id=&token=`) and the seed's run-event poll stream.

`GET /events` streams `sse.Bus` (every `run.state`, `siblings.ranked`, `graph.saved`, budget alert...) with
`sse_starlette` and a heartbeat comment every 15 s; `Last-Event-ID` (or `after`) replays what was missed.
It is the ONE route that accepts the token in the query (`?token=`, because EventSource cannot set a header):
`auth.AuthMiddleware` checks it and strips it before the app sees it, so this route carries no bearer
dependency of its own. `GET /events/stream` is the seed's polling stream of run transitions (bearer only),
which ends after `wait_s` so clients reconnect instead of holding sockets open forever.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.concurrency import run_in_threadpool

from ..config import check_project_id
from ..machine import events_after
from ..sse import EventFilter, UnknownProject
from . import API, MAX_WAIT_S, SSE_POLL_S, SSE_RETRY_MS, ApiError, ctx, cursor, require_token, sse_frame

router = APIRouter(prefix=API)


@router.get("/events")
async def bus_events(
    request: Request,
    run_id: Annotated[str | None, Query()] = None,
    project_id: Annotated[str | None, Query()] = None,
    after: Annotated[int | None, Query()] = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> EventSourceResponse:
    c = ctx(request)
    try:
        pid = await run_in_threadpool(c.bus.resolve_project, run_id, project_id)
        check_project_id(pid)
    except UnknownProject as e:
        raise ApiError(404 if run_id else 422, {"errors": [str(e)]}) from e
    except ValueError as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    if pid not in await run_in_threadpool(c.index.project_ids):
        raise ApiError(404, {"error": "not_found", "project_id": pid})
    flt = EventFilter(project_id=pid, run_id=run_id, after=cursor(after, last_event_id))

    async def gen() -> AsyncIterator[ServerSentEvent]:
        async for rec in c.bus.subscribe(flt):
            yield ServerSentEvent(
                data=json.dumps(rec, ensure_ascii=False, separators=(",", ":")), id=str(rec["seq"])
            )

    return EventSourceResponse(gen(), ping=c.bus.heartbeat_s, headers={"Cache-Control": "no-cache"})


@router.get("/events/stream", dependencies=[Depends(require_token)])
async def events_stream(
    request: Request,
    after: Annotated[int | None, Query()] = None,
    project_id: Annotated[str | None, Query()] = None,
    wait_s: Annotated[float, Query(ge=0, le=MAX_WAIT_S)] = 0.0,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    c = ctx(request)
    projects = await run_in_threadpool(c.index.project_ids)
    if project_id is None:
        if len(projects) > 1:
            raise ApiError(422, {"errors": [f"project_id required: {len(projects)} projects are indexed"]})
        project_id = projects[0] if projects else None
    elif project_id not in projects:
        raise ApiError(404, {"error": "not_found", "project_id": project_id})

    def read(at: int) -> list[dict[str, Any]]:
        if project_id is None:
            return []
        with c.index.project(project_id) as conn:
            return [dict(e.model_dump(mode="json"), project_id=project_id) for e in events_after(conn, at)]

    async def gen() -> AsyncIterator[str]:
        at = cursor(after, last_event_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        yield f"retry: {SSE_RETRY_MS}\n\n"
        while True:
            evs = await run_in_threadpool(read, at)
            for ev in evs:
                at = ev["event_id"]
                yield sse_frame(ev, at)
            if loop.time() >= deadline:
                return
            if not evs:
                await asyncio.sleep(SSE_POLL_S)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
