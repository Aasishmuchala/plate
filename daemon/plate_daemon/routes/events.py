"""Event routes: the bus (`GET /events?run_id=&project_id=&token=`) and the seed's run-event poll stream.

`GET /events` streams `sse.Bus` (every `run.state`, `siblings.ranked`, `graph.saved`, budget alert...) with
`sse_starlette` and a heartbeat comment every 15 s; `Last-Event-ID` (or `after`) replays what was missed.
It is the ONE route that accepts the token in the query (`?token=`, because EventSource cannot set a header):
`auth.AuthMiddleware` checks it and strips it before the app sees it, so this route carries no bearer
dependency of its own. `GET /events/stream` is the seed's polling stream of run transitions (bearer only),
which ends after `wait_s` so clients reconnect instead of holding sockets open forever.

Both streams name their events `<epoch>.<seq>` (the SSE `id:`), where the epoch is the project index's
`index_epoch` (Wave 12, T144): a rebuild (`plate reindex`, the boot rebuild of a lost index) restarts seq at 1,
and a cursor from before it would otherwise skip every new event up to its old number. `cursor(after,
last_event_id, epoch=, head=)` reads either form: `<epoch>.<seq>` from another epoch is 0 (replay the new
index from its first event), a bare integer (an old client, or the UI's own `after=<seq>`) above the
current head is 0, and nothing is 0. `Last-Event-ID` wins over `after` when it is well formed: the browser
sends it on reconnect and it names the last event actually delivered, which `after` (fixed in the URL) does
not. A project with no index is 404 `not_found` on both streams; neither ever creates one.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.concurrency import run_in_threadpool

from ..config import check_project_id
from ..db import loads
from ..index import EPOCH_KEY, UnknownProject, index_epoch
from ..machine import events_after
from ..sse import EventFilter, event_id
from . import API, MAX_WAIT_S, SSE_POLL_S, SSE_RETRY_MS, ApiError, ctx, require_token, sse_frame

router = APIRouter(prefix=API)

EVENT_ID_RE = re.compile(r"(?:([0-9A-Za-z]{1,64})\.)?(-?[0-9]{1,19})")
AFTER_HELP = "after must be an event id: <epoch>.<seq> or <seq>"


def parse_event_id(raw: str | None) -> tuple[str | None, int] | None:
    """`<epoch>.<seq>` -> (epoch, seq); a bare integer -> (None, seq); empty or malformed -> None."""
    if raw is None:
        return None
    m = EVENT_ID_RE.fullmatch(raw.strip())
    if m is None:
        return None
    return m.group(1), int(m.group(2))


def cursor(
    after: str | int | None,
    last_event_id: str | None,
    *,
    epoch: str | None = None,
    head: int | None = None,
) -> int:
    """The seq to replay after. `Last-Event-ID` (when well formed) wins over `after`. An id from an epoch that
    is not `epoch` is 0; a seq above `head` (the newest event of the current epoch) is 0: the client's cursor
    belongs to another history of this index. With `epoch`/`head` None that check is skipped."""
    parsed = parse_event_id(last_event_id)
    if parsed is None and after is not None:
        parsed = (None, after) if isinstance(after, int) else parse_event_id(after)
    if parsed is None:
        return 0
    their_epoch, seq = parsed
    if their_epoch is not None and epoch is not None and their_epoch != epoch:
        return 0
    if head is not None and seq > head:
        return 0
    return max(0, seq)


def _check_after(after: str | None) -> None:
    if after is not None and parse_event_id(after) is None:
        raise ApiError(422, {"errors": [AFTER_HELP]})


def _snapshot_epoch(conn: Any) -> str | None:
    row = conn.execute("SELECT value FROM project_meta WHERE key = ?", (EPOCH_KEY,)).fetchone()
    return None if row is None else str(loads(row["value"]))


@router.get("/events")
async def bus_events(
    request: Request,
    run_id: Annotated[str | None, Query()] = None,
    project_id: Annotated[str | None, Query()] = None,
    after: Annotated[str | None, Query()] = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> EventSourceResponse:
    c = ctx(request)
    _check_after(after)
    try:
        pid = await run_in_threadpool(c.bus.resolve_project, run_id, project_id)
        check_project_id(pid)
    except UnknownProject as e:
        raise ApiError(404 if run_id else 422, {"errors": [str(e)]}) from e
    except ValueError as e:
        raise ApiError(422, {"errors": [str(e)]}) from e
    if pid not in await run_in_threadpool(c.index.project_ids):
        raise ApiError(404, {"error": "not_found", "project_id": pid})
    epoch = await run_in_threadpool(c.bus.epoch, pid)
    head = await run_in_threadpool(c.bus.head, pid)
    start = cursor(after, last_event_id, epoch=epoch, head=head)
    flt = EventFilter(project_id=pid, run_id=run_id, after=start, epoch=epoch)

    async def gen() -> AsyncIterator[ServerSentEvent]:
        async for rec in c.bus.subscribe(flt):
            yield ServerSentEvent(
                data=json.dumps(rec, ensure_ascii=False, separators=(",", ":")),
                id=event_id(rec["epoch"], rec["seq"]),
            )

    return EventSourceResponse(gen(), ping=c.bus.heartbeat_s, headers={"Cache-Control": "no-cache"})


@router.get("/events/stream", dependencies=[Depends(require_token)])
async def events_stream(
    request: Request,
    after: Annotated[str | None, Query()] = None,
    project_id: Annotated[str | None, Query()] = None,
    wait_s: Annotated[float, Query(ge=0, le=MAX_WAIT_S)] = 0.0,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    c = ctx(request)
    _check_after(after)
    projects = await run_in_threadpool(c.index.project_ids)
    if project_id is None:
        if len(projects) > 1:
            raise ApiError(422, {"errors": [f"project_id required: {len(projects)} projects are indexed"]})
        project_id = projects[0] if projects else None
    elif project_id not in projects:
        raise ApiError(404, {"error": "not_found", "project_id": project_id})

    def head_of(pid: str) -> tuple[str, int]:
        with c.index.project(pid) as conn:
            head = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM run_events").fetchone()[0])
            return index_epoch(conn), head

    def read(at: int) -> tuple[str | None, list[dict[str, Any]]]:
        if project_id is None:
            return None, []
        with c.index.project(project_id) as conn:
            conn.execute("BEGIN")  # a read transaction: one snapshot for the epoch and the events
            try:
                epoch = _snapshot_epoch(conn)
                evs = [dict(e.model_dump(mode="json"), project_id=project_id) for e in events_after(conn, at)]
            finally:
                conn.execute("COMMIT")
            return (epoch if epoch is not None else index_epoch(conn)), evs

    epoch: str | None = None
    at = 0
    if project_id is not None:
        epoch, head = await run_in_threadpool(head_of, project_id)
        at = cursor(after, last_event_id, epoch=epoch, head=head)

    async def gen() -> AsyncIterator[str]:
        nonlocal epoch, at
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        yield f"retry: {SSE_RETRY_MS}\n\n"
        while True:
            now_epoch, evs = await run_in_threadpool(read, at)
            if epoch is not None and now_epoch is not None and now_epoch != epoch and at > 0:
                epoch, at = now_epoch, 0  # rebuilt under the stream: replay the new index from its start
                continue
            epoch = now_epoch
            for ev in evs:
                at = ev["event_id"]
                yield sse_frame(ev, event_id(epoch, at) if epoch is not None else at)
            if loop.time() >= deadline:
                return
            if not evs:
                await asyncio.sleep(SSE_POLL_S)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
