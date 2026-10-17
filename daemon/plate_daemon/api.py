"""The daemon's only app factory, `create_app` (transport contract: spec/PACKET.md section 9).

`create_app(settings, engine=None)` wires the per-project indexes, the event bus, the per-project CAS and the
pipeline into `app.state.ctx` (`routes.Ctx`) and includes every route module of `plate_daemon.routes`
(`routes.include_all`): packets, projects and graphs, runs, assets, events, and whatever later tasks add
there. `auth.AuthMiddleware` wraps everything: Host and Origin allowlists on every request (403), and under
`/api/` `Authorization: Bearer <token>` compared in constant time (401 `{"error": "unauthorized"}`) except
`GET /api/v0/health` (the plugin's version handshake, defined here) and the query token `GET /api/v0/events`
accepts; the routers keep their own bearer dependency as a second check.

Each project has its own CAS under its folder (`cas_for(project_id)`: the root the index registered, else
`<home>/projects/<id>`), and puts are recorded in that project's index. A packet that finished ingesting is
registered by reference in its project's `packets.json` (the folder is made, with custody C0 only, when the
packet arrives first). At start-up a background thread finishes the packets a killed daemon left half-hashed
and drives on the pipeline runs it left in a worker state (`Pipeline.resume`). The built UI (`ui/dist`) is
served at `/` when it exists; otherwise `/` says how to build it. Errors raised as `routes.ApiError(status,
body)` are rendered as JSON; body validation errors are reshaped to `422 {"errors": [...]}` so senders see
one error format.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .auth import AuthMiddleware
from .cas import CAS
from .config import Settings, check_project_id
from .engine_fake import FakeEngine
from .index import Index
from .ingest import finish_ingest, get_packet, pending_ingests
from .pipeline import Pipeline
from .routes import API, ApiError, Ctx, include_all
from .sse import Bus
from .timeutil import utcnow

__all__ = ["API", "UI_NOT_BUILT", "ApiError", "create_app"]

log = logging.getLogger("plate_daemon")

UI_NOT_BUILT = "UI not built: cd ui && npm run build\n"


def _fmt_validation(err: dict[str, Any]) -> str:
    loc = "/".join(str(p) for p in err.get("loc", ()) if p != "body") or "<body>"
    return f"{loc}: {err.get('msg', 'invalid')}"


def create_app(
    settings: Settings,
    engine: Any | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
    cas_for: Callable[[str], CAS] | None = None,
    bus: Bus | None = None,
    crash_exits_process: bool = False,
    engines: Mapping[str, Any] | None = None,
    manifests: Mapping[str, Any] | None = None,
) -> FastAPI:
    """Build the app. `clock` (aware-UTC callable) is swappable at `app.state.clock` for tests;
    `cas_for(project_id)` overrides where a project's CAS lives; `crash_exits_process` turns a scripted
    SimulatedCrash into a hard process exit (python -m plate_daemon). `engine` serves the `fake` engine id
    and the seed's `POST /runs` runs; `engines` adds daemon-protocol adapters by engine id; `manifests`
    replaces the engine manifests on disk (`plate_engines.loader`)."""
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

    def resume() -> None:
        resume_ingests()
        picked = pipeline.resume()
        if picked:
            log.info("resumed %d pipeline run(s) a stopped daemon left in flight", len(picked))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Packets a killed daemon left half-hashed finish, and pipeline runs it left in flight drive on, in the
        # background; the API is up meanwhile.
        threading.Thread(target=resume, name="plate-resume", daemon=True).start()
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
    pipeline = Pipeline(
        settings=settings,
        index=index,
        cas_for=cas_for,
        bus=bus,
        engine=engine,
        engines=engines,
        manifests=manifests,
        clock=lambda: app.state.clock(),
    )

    def finish(project_id: str, packet_id: str) -> None:
        with ingest_guard:
            if packet_id in active_ingests:
                return
            active_ingests.add(packet_id)
        try:
            with index.project(project_id) as conn:
                finish_ingest(conn, cas_for(project_id), packet_id, cas_copy=settings.cas_copy)
                status = get_packet(conn, packet_id, with_blobs=False)["status"]
            if status == "ingested":
                try:
                    pipeline.register_packet(project_id, packet_id)
                except Exception as e:  # the index has it; packets.json catches up on the next send
                    log.warning("packet %s not registered in %s/packets.json: %s", packet_id, project_id, e)
        finally:
            with ingest_guard:
                active_ingests.discard(packet_id)

    def resume_ingests() -> None:
        for pid in index.project_ids():
            with index.project(pid) as conn:
                todo = pending_ingests(conn)
            for packet_id in todo:
                finish(pid, packet_id)

    app.add_middleware(AuthMiddleware, settings=settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.clock = clock or utcnow
    app.state.cas_for = cas_for
    app.state.index = index
    app.state.bus = bus
    app.state.pipeline = pipeline
    app.state.ctx = Ctx(
        app=app,
        settings=settings,
        engine=engine,
        index=index,
        bus=bus,
        cas_for=cas_for,
        pipeline=pipeline,
        finish_packet=finish,
        crash_exits_process=crash_exits_process,
    )

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.body, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"errors": [_fmt_validation(e) for e in exc.errors()]}, status_code=422)

    # ------------------------------------------------------------------ health (no auth)

    @app.get(f"{API}/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "daemon_version": __version__,
            "packet_versions": list(settings.packet_versions),
            "stub_engine": bool(getattr(engine, "stub", False)),
        }

    include_all(app)

    # ------------------------------------------------------------------ the built UI at /

    ui_dist = Path(settings.ui_dist)
    if (ui_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=ui_dist, html=True), name="ui")
    else:

        @app.get("/", response_class=PlainTextResponse, include_in_schema=False)
        def ui_not_built() -> str:
            return UI_NOT_BUILT

    return app
