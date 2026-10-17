"""FastAPI surface of the daemon, all under `/api/v0` (transport contract: spec/PACKET.md section 9).

`create_app(settings, engine=None)` wires the packet ingest, the run lifecycle and the event streams to
the per-project indexes. `auth.AuthMiddleware` wraps everything: Host and Origin allowlists on every
request (403), and under `/api/` `Authorization: Bearer <token>` compared in constant time (401
`{"error": "unauthorized"}`) except `GET /api/v0/health` (the plugin's version handshake) and the query
token `GET /api/v0/events` accepts; the router keeps its own bearer dependency as a second check.
`GET /api/v0/session` answers the UI's token check; `POST /api/v0/hash` hashes packet files and payloads
for the plugin (`hashing`); `GET /api/v0/events` streams `sse.Bus`. Each project has its own CAS under
its folder (`cas_for(project_id)`: the root the index registered, else `<home>/projects/<id>`), and puts
are recorded in that project's index. The built UI (`ui/dist`) is served at `/` when it exists; otherwise
`/` says how to build it. `POST /api/v0/packets` additionally requires `X-Plate-Packet-Version`
whose major.minor the daemon reads (`426 {"supported": [...]}` otherwise), answers 202 / 200 known / 409 /
422 `{"errors": [...]}`, and finishes hashing in a background task (inline when `settings.sync_ingest`).
Body validation errors are reshaped to `422 {"errors": [...]}` so senders see one error format.
Run routes drive quote -> approve -> queue -> step -> export; `/step` is the dev/test driver (there is no
background scheduler yet). SSE streams (`text/event-stream`, `data: {json}` frames with `id:` lines,
`Last-Event-ID` honoured) end on `ingest.done` for packets and after `wait_s` for run events, so clients
reconnect instead of holding sockets open forever.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Body, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.concurrency import run_in_threadpool

from . import __version__
from .adapter import SimulatedCrash
from .auth import AuthMiddleware
from .cas import CAS
from .config import Settings, check_project_id
from .engine_fake import FakeEngine
from .export import NotExportable, export_run
from .hashing import BadHashRequest, HashRefused, PayloadTooLarge, hash_paths, hash_payloads
from .index import Index
from .ingest import (
    PacketConflict,
    PacketInvalid,
    begin_ingest,
    finish_ingest,
    get_packet,
    packet_events,
    pending_ingests,
    prepare_ingest,
)
from .machine import IllegalTransition, RunNotFound, create_run, events_after, get_run, run_events, transition
from .models import RunCreate
from .quotes import (
    EnvelopeRequired,
    QuoteExpired,
    QuoteNotFound,
    approve,
    create_envelope,
    get_envelope,
    quote_run,
)
from .sse import Bus, EventFilter, UnknownProject
from .states import RunState
from .timeutil import utcnow
from .worker import cancel_run, step

log = logging.getLogger("plate_daemon")

API = "/api/v0"
VERSION_RE = re.compile(r"^\s*(\d+)\.(\d+)(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?\s*$")
SSE_RETRY_MS = 2000
SSE_POLL_S = 0.25
MAX_WAIT_S = 120.0
UI_NOT_BUILT = "UI not built: cd ui && npm run build\n"


class ApiError(Exception):
    def __init__(self, status: int, body: dict[str, Any]):
        self.status, self.body = status, body
        super().__init__(f"{status} {body}")


def major_minor(header: str | None) -> str | None:
    m = VERSION_RE.match(header or "")
    return f"{int(m.group(1))}.{int(m.group(2))}" if m else None


class Sender(BaseModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class PacketPost(BaseModel):
    packet_path: str = Field(min_length=1)
    sender: Sender


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: Literal["human", "auto", "agent"]
    envelope_id: str | None = None


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = "human"
    reason: str | None = None


class ExportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dest: str = Field(min_length=1)
    override: bool = False
    reason: str | None = None


class HashBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(default_factory=list, max_length=100_000)
    payloads: dict[str, str] = Field(default_factory=dict)


class EnvelopeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    owner: str = Field(min_length=1)
    max_total_usd: float = Field(ge=0)
    max_per_run_usd: float = Field(ge=0)
    ttl_s: float = Field(gt=0)


def require_packet_version(
    request: Request, x_plate_packet_version: Annotated[str | None, Header()] = None
) -> str:
    # Module level, not inside create_app: under `from __future__ import annotations` FastAPI resolves a
    # parameter's `Depends(...)` from module globals, and the seed's closure silently became a query param.
    supported = list(request.app.state.settings.packet_versions)
    mm = major_minor(x_plate_packet_version)
    if mm is None or mm not in supported:
        raise ApiError(426, {"supported": supported})
    return mm


def _fmt_validation(err: dict[str, Any]) -> str:
    loc = "/".join(str(p) for p in err.get("loc", ()) if p != "body") or "<body>"
    return f"{loc}: {err.get('msg', 'invalid')}"


def _sse(data: dict[str, Any], event_id: str | int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _cursor(after: int | None, last_event_id: str | None) -> int:
    if after is not None:
        return max(0, after)
    try:
        return max(0, int(last_event_id)) if last_event_id else 0
    except ValueError:
        return 0


def create_app(
    settings: Settings,
    engine: Any | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
    cas_for: Callable[[str], CAS] | None = None,
    bus: Bus | None = None,
    crash_exits_process: bool = False,
) -> FastAPI:
    """Build the app. `clock` (aware-UTC callable) is swappable at `app.state.clock` for tests;
    `cas_for(project_id)` overrides where a project's CAS lives; `crash_exits_process` turns a scripted
    SimulatedCrash into a hard process exit (python -m plate_daemon)."""
    engine = engine if engine is not None else FakeEngine.from_env(journal=settings.home / "fake_engine.json")
    index = Index(settings.home)
    bus = bus or Bus(index)

    def project_cas(project_id: str) -> CAS:
        root = index.project_root(project_id) or settings.projects_dir / check_project_id(project_id)

        def record(key: str, ext: str, nbytes: int) -> None:
            try:
                index.record_asset(project_id, key, ext, nbytes)
            except sqlite3.Error as e:  # the index is a cache: the object is safe in the folder
                log.warning("asset %s of %s not indexed: %s (plate reindex fixes it)", key, project_id, e)

        return CAS(root / "assets", on_put=record)

    cas_for = cas_for or project_cas
    active_ingests: set[str] = set()
    ingest_guard = threading.Lock()

    def now() -> datetime:
        return app.state.clock()

    def finish(project_id: str, packet_id: str) -> None:
        with ingest_guard:
            if packet_id in active_ingests:
                return
            active_ingests.add(packet_id)
        try:
            with index.project(project_id) as conn:
                finish_ingest(conn, cas_for(project_id), packet_id, cas_copy=settings.cas_copy)
        finally:
            with ingest_guard:
                active_ingests.discard(packet_id)

    def resume_ingests() -> None:
        for pid in index.project_ids():
            with index.project(pid) as conn:
                todo = pending_ingests(conn)
            for packet_id in todo:
                finish(pid, packet_id)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Packets a killed daemon left half-hashed finish in the background; the API is up meanwhile.
        threading.Thread(target=resume_ingests, name="plate-resume-ingest", daemon=True).start()
        yield

    app = FastAPI(
        title="PLATE daemon",
        version=__version__,
        lifespan=lifespan,
        # No schema or docs pages: they would be served without the token (only /api/ is token-gated).
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(AuthMiddleware, settings=settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.clock = clock or utcnow
    app.state.cas_for = cas_for
    app.state.index = index
    app.state.bus = bus

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.body, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"errors": [_fmt_validation(e) for e in exc.errors()]}, status_code=422)

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, token = (authorization or "").partition(" ")
        ok = scheme.lower() == "bearer" and secrets.compare_digest(
            token.strip().encode("utf-8"), settings.token.encode("utf-8")
        )
        if not ok:
            raise ApiError(401, {"error": "unauthorized"})

    # ------------------------------------------------------------------ helpers

    def run_project(run_id: str) -> str:
        pid = index.locate_run(run_id)
        if pid is None:
            raise ApiError(404, {"error": "not_found", "run_id": run_id})
        return pid

    def packet_project(packet_id: str) -> str:
        pid = index.locate_packet(packet_id)
        if pid is None:
            raise ApiError(404, {"error": "not_found", "packet_id": packet_id})
        return pid

    def run_json(conn, run_id: str) -> dict[str, Any]:
        return get_run(conn, run_id).model_dump(mode="json")

    def illegal(e: IllegalTransition) -> ApiError:
        return ApiError(
            409,
            {
                "error": "illegal_transition",
                "from": e.from_state.value,
                "to": e.to_state.value,
                "detail": str(e),
            },
        )

    # ------------------------------------------------------------------ health (no auth)

    @app.get(f"{API}/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "daemon_version": __version__,
            "packet_versions": list(settings.packet_versions),
            "stub_engine": bool(getattr(engine, "stub", False)),
        }

    r = APIRouter(prefix=API, dependencies=[Depends(require_token)])

    # ------------------------------------------------------------------ session and hashing

    @r.get("/session")
    def session() -> dict[str, Any]:
        return {"ok": True, "daemon_version": __version__, "packet_versions": list(settings.packet_versions)}

    @r.post("/hash")
    def hash_route(body: HashBody) -> dict[str, Any]:
        if not body.paths and not body.payloads:
            raise ApiError(422, {"errors": ["give paths and/or payloads to hash"]})
        try:
            hashes = hash_paths(body.paths, settings.packets_dirs)
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

    # ------------------------------------------------------------------ packets

    @r.post("/packets", status_code=202)
    def post_packet(
        body: PacketPost,
        background: BackgroundTasks,
        version: Annotated[str, Depends(require_packet_version)],
    ) -> JSONResponse:
        try:
            prepared = prepare_ingest(body.packet_path)
        except PacketInvalid as e:
            raise ApiError(422, {"errors": e.errors}) from e
        if major_minor(prepared.manifest.get("packet_version")) != version:
            raise ApiError(
                422,
                {
                    "errors": [
                        f"X-Plate-Packet-Version {version} != manifest packet_version {prepared.manifest.get('packet_version')}"
                    ]
                },
            )
        packet_id = prepared.packet_id
        owner = index.locate_packet(packet_id) or prepared.project_id
        with index.project(owner) as conn:
            try:
                status, _rec = begin_ingest(
                    conn, body.packet_path, sender=body.sender.model_dump(), prepared=prepared, now=now()
                )
            except PacketConflict as e:
                raise ApiError(409, {"error": "conflict", "packet_id": packet_id, "detail": str(e)}) from e
        index.remember(packet_id, owner)
        if status == "known":
            return JSONResponse({"packet_id": packet_id, "status": "known"}, status_code=200)
        if settings.sync_ingest:
            finish(owner, packet_id)
        else:
            background.add_task(finish, owner, packet_id)
        return JSONResponse(
            {
                "packet_id": packet_id,
                "status": "ingesting",
                "events_url": f"{API}/packets/{packet_id}/events",
            },
            status_code=202,
        )

    @r.get("/packets/{packet_id}")
    def get_packet_route(packet_id: str) -> JSONResponse:
        with index.project(packet_project(packet_id)) as conn:
            return JSONResponse(get_packet(conn, packet_id))

    @r.get("/packets/{packet_id}/events")
    async def packet_events_route(
        packet_id: str,
        after: Annotated[int | None, Query()] = None,
        wait_s: Annotated[float, Query(ge=0, le=MAX_WAIT_S)] = 60.0,
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        project_id = await run_in_threadpool(packet_project, packet_id)

        def read(seq: int) -> tuple[list[dict[str, Any]], str]:
            with index.project(project_id) as conn:
                return packet_events(conn, packet_id, seq), get_packet(conn, packet_id, with_blobs=False)[
                    "status"
                ]

        async def gen() -> AsyncIterator[str]:
            seq = _cursor(after, last_event_id)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + wait_s
            yield f"retry: {SSE_RETRY_MS}\n\n"
            while True:
                evs, status = await run_in_threadpool(read, seq)
                for ev in evs:
                    seq = ev["seq"]
                    yield _sse(ev, seq)
                    if ev["type"] in ("ingest.done", "ingest.failed"):
                        return
                if status != "ingesting" and not evs:
                    return
                if loop.time() >= deadline:
                    return
                await asyncio.sleep(SSE_POLL_S)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------ runs

    @r.post("/runs", status_code=201)
    def create_run_route(body: RunCreate) -> JSONResponse:
        with index.project(body.project_id) as conn:
            row = conn.execute("SELECT status FROM packets WHERE packet_id = ?", (body.packet_id,)).fetchone()
            if row is None:
                raise ApiError(
                    422,
                    {
                        "errors": [
                            f"packet {body.packet_id} is not in project {body.project_id}; send it first"
                        ]
                    },
                )
            if row["status"] != "ingested":
                raise ApiError(
                    409, {"error": "packet_not_ready", "packet_id": body.packet_id, "status": row["status"]}
                )
            run = create_run(conn, body, now=now())
        index.remember(run.id, body.project_id)
        return JSONResponse(run.model_dump(mode="json"), status_code=201)

    @r.get("/runs/{run_id}")
    def get_run_route(run_id: str) -> dict[str, Any]:
        with index.project(run_project(run_id)) as conn:
            return run_json(conn, run_id)

    @r.get("/runs/{run_id}/events")
    def run_events_route(run_id: str) -> list[dict[str, Any]]:
        with index.project(run_project(run_id)) as conn:
            return [e.model_dump(mode="json") for e in run_events(conn, run_id)]

    @r.post("/runs/{run_id}/quote")
    def quote_route(run_id: str) -> dict[str, Any]:
        with index.project(run_project(run_id)) as conn:
            try:
                run, q = quote_run(conn, run_id, now=now())
            except IllegalTransition as e:
                raise illegal(e) from e
            return {"run": run.model_dump(mode="json"), "quote": q.model_dump(mode="json")}

    @r.post("/runs/{run_id}/approve")
    def approve_route(run_id: str, body: ApproveBody) -> dict[str, Any]:
        with index.project(run_project(run_id)) as conn:
            try:
                run = approve(conn, run_id, body.actor, body.envelope_id, now())
            except QuoteExpired as e:
                raise ApiError(409, {"error": "quote_expired", "detail": str(e)}) from e
            except IllegalTransition as e:
                raise illegal(e) from e
            except EnvelopeRequired as e:
                raise ApiError(422, {"error": "envelope_required", "detail": str(e)}) from e
            except QuoteNotFound as e:
                raise ApiError(409, {"error": "quote_missing", "detail": str(e)}) from e
            return run.model_dump(mode="json")

    @r.post("/runs/{run_id}/queue")
    def queue_route(run_id: str) -> dict[str, Any]:
        with index.project(run_project(run_id)) as conn:
            try:
                return transition(conn, run_id, RunState.QUEUED, actor="api").model_dump(mode="json")
            except IllegalTransition as e:
                raise illegal(e) from e

    @r.post("/runs/{run_id}/cancel")
    def cancel_route(run_id: str, body: Annotated[CancelBody | None, Body()] = None) -> dict[str, Any]:
        body = body or CancelBody()
        with index.project(run_project(run_id)) as conn:
            try:
                return cancel_run(conn, engine, run_id, actor=body.actor, reason=body.reason).model_dump(
                    mode="json"
                )
            except IllegalTransition as e:
                raise illegal(e) from e

    @r.post("/runs/{run_id}/step")
    def step_route(run_id: str) -> dict[str, Any]:
        pid = run_project(run_id)
        with index.project(pid) as conn:
            try:
                step(conn, engine, run_id, now, cas=cas_for(pid))
            except SimulatedCrash as e:
                if crash_exits_process:
                    sys.stderr.write(f"plate_daemon: {e}; exiting as a crashed process would\n")
                    sys.stderr.flush()
                    os._exit(70)
                raise ApiError(500, {"error": "simulated_crash", "detail": str(e)}) from None
            except RunNotFound as e:
                raise ApiError(404, {"error": "not_found", "run_id": run_id}) from e
            return run_json(conn, run_id)

    @r.post("/runs/{run_id}/export")
    def export_route(run_id: str, body: ExportBody) -> dict[str, Any]:
        dest = Path(body.dest)
        if not dest.is_absolute():
            raise ApiError(422, {"errors": [f"dest must be an absolute file path, got {body.dest!r}"]})
        pid = run_project(run_id)
        with index.project(pid) as conn:
            try:
                out = export_run(
                    conn, run_id, dest, override=body.override, reason=body.reason, cas=cas_for(pid)
                )
            except NotExportable as e:
                raise ApiError(409, {"error": "not_exportable", "detail": str(e)}) from e
            return {"run_id": run_id, "path": str(out)}

    @r.get("/events/stream")
    async def events_stream(
        after: Annotated[int | None, Query()] = None,
        project_id: Annotated[str | None, Query()] = None,
        wait_s: Annotated[float, Query(ge=0, le=MAX_WAIT_S)] = 0.0,
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        projects = await run_in_threadpool(index.project_ids)
        if project_id is None:
            if len(projects) > 1:
                raise ApiError(
                    422, {"errors": [f"project_id required: {len(projects)} projects are indexed"]}
                )
            project_id = projects[0] if projects else None
        elif project_id not in projects:
            raise ApiError(404, {"error": "not_found", "project_id": project_id})

        def read(cursor: int) -> list[dict[str, Any]]:
            if project_id is None:
                return []
            with index.project(project_id) as conn:
                return [
                    dict(e.model_dump(mode="json"), project_id=project_id) for e in events_after(conn, cursor)
                ]

        async def gen() -> AsyncIterator[str]:
            cursor = _cursor(after, last_event_id)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + wait_s
            yield f"retry: {SSE_RETRY_MS}\n\n"
            while True:
                evs = await run_in_threadpool(read, cursor)
                for ev in evs:
                    cursor = ev["event_id"]
                    yield _sse(ev, cursor)
                if loop.time() >= deadline:
                    return
                if not evs:
                    await asyncio.sleep(SSE_POLL_S)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------ envelopes

    @r.post("/envelopes", status_code=201)
    def create_envelope_route(body: EnvelopeBody) -> JSONResponse:
        try:
            with index.project(body.project_id) as conn:
                env = create_envelope(
                    conn,
                    owner=body.owner,
                    max_total_usd=body.max_total_usd,
                    max_per_run_usd=body.max_per_run_usd,
                    ttl_s=body.ttl_s,
                    now=now(),
                )
        except ValueError as e:
            raise ApiError(422, {"errors": [str(e)]}) from e
        index.remember(env.id, body.project_id)
        return JSONResponse(dict(env.model_dump(mode="json"), project_id=body.project_id), status_code=201)

    @r.get("/envelopes/{envelope_id}")
    def get_envelope_route(envelope_id: str) -> dict[str, Any]:
        pid = index.locate_envelope(envelope_id)
        if pid is None:
            raise ApiError(404, {"error": "not_found", "envelope_id": envelope_id})
        with index.project(pid) as conn:
            env = get_envelope(conn, envelope_id)
            return dict(env.model_dump(mode="json"), project_id=pid)

    app.include_router(r)

    # ------------------------------------------------------------------ the event bus (query token allowed)

    ev = APIRouter(prefix=API)

    @ev.get("/events")
    async def bus_events(
        run_id: Annotated[str | None, Query()] = None,
        project_id: Annotated[str | None, Query()] = None,
        after: Annotated[int | None, Query()] = None,
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> EventSourceResponse:
        try:
            pid = await run_in_threadpool(bus.resolve_project, run_id, project_id)
            check_project_id(pid)
        except UnknownProject as e:
            raise ApiError(404 if run_id else 422, {"errors": [str(e)]}) from e
        except ValueError as e:
            raise ApiError(422, {"errors": [str(e)]}) from e
        if pid not in await run_in_threadpool(index.project_ids):
            raise ApiError(404, {"error": "not_found", "project_id": pid})
        flt = EventFilter(project_id=pid, run_id=run_id, after=_cursor(after, last_event_id))

        async def gen() -> AsyncIterator[ServerSentEvent]:
            async for rec in bus.subscribe(flt):
                yield ServerSentEvent(
                    data=json.dumps(rec, ensure_ascii=False, separators=(",", ":")), id=str(rec["seq"])
                )

        return EventSourceResponse(gen(), ping=bus.heartbeat_s, headers={"Cache-Control": "no-cache"})

    app.include_router(ev)

    # ------------------------------------------------------------------ the built UI at /

    ui_dist = Path(settings.ui_dist)
    if (ui_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=ui_dist, html=True), name="ui")
    else:

        @app.get("/", response_class=PlainTextResponse, include_in_schema=False)
        def ui_not_built() -> str:
            return UI_NOT_BUILT

    return app
