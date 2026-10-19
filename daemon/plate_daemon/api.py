"""The daemon's only app factory, `create_app` (transport contract: spec/PACKET.md section 9).

`create_app(settings, engine=None)` is the composition root. It wires the per-project indexes, the event bus,
the per-project CAS and the pipeline into `app.state.ctx` (`routes.Ctx`) and includes every route module of
`plate_daemon.routes` (`routes.include_all`): packets, projects and graphs, runs, assets, events, engines,
pixels, the numeric and plugin node routes (`routes/numeric.py`), and whatever later tasks add there.
`auth.AuthMiddleware` wraps everything: Host and Origin allowlists on every request (403), and under `/api/`
`Authorization: Bearer <token>` compared in constant time (401 `{"error": "unauthorized"}`) except
`GET /api/v0/health` (the plugin's version handshake, defined here) and the query token `GET /api/v0/events`
accepts; the routers keep their own bearer dependency as a second check.

Node kinds beyond the Edit are wired by install-wrappers (decision 38), right after the Pipeline is built and
before anything can resume a run: `nodes.install` (Set, Recolour, LightMix, Light, Scout),
`motion_runs.install` (Motion, CropPair, People) and `llm.judge_node.install` (Judge); the Motion editor's
router card is `nodes.runner.PREVIEWS["router"]` (`MotionRouterPreview`: `motion_runs.router_preview` on a
Motion or CropPair node). A node run a stopped daemon left in flight carries the staging marker, so an
un-wired Pipeline would pick it up at the boot resume and fail it as an unknown engine.

Wave 9 adds three installs after those, in this order: `people_video.install` (a People node with
`params.video` is consulted before the Motion runner's still People, so it must wrap `motion_runs`),
`recomposite.install` (an Edit quote with `params.recomposite_of`) and `layer_upscale.install` (the
`upscale_ai_layer` ingress hook and the Magnific declaration), then `pipeline.route_tracker =
route_health.Tracker(home)`, which is what lets `Pipeline.health_board` mark a failing route degraded. Every
install is idempotent on its Pipeline.

The nightly scheduler's `on_alert` is wired to the bus: each line it appends to `alerts.jsonl` (engine kinds
and nightly kinds alike) becomes one `alert` record `{kind: "alert", source, level, message, at, alert_kind,
engine_id?}` with the level mapping of `routes/alerts.py` (`level_of`, `source_of`), the shape the UI's status
bar reads. The record goes to the project the line names (`data.project_id`), else to every indexed project:
the line is app-wide and `GET /alerts` keeps the durable copy, the bus is for whoever is watching.

The pixels worker reaches the pipeline through two lazy seams over the app's `PixelsClient` (`app.state.pixels`,
built on first use and shared with `routes/pixels.py`): `LazyOcr` (the text lock's OCR boxes) and `LazyDepth`
(the Tier C depth agreement). Nothing connects while the app is built. While no worker answers (none
listening, or one that refuses our token) OCR finds no boxes with one logged warning, and `pipeline.depth_fn`
reads None, the gate's "no depth backend" path. `/health` says which: `pixels: inproc | http | unavailable`.
`/health` also says whether a Max dock is alive and how much work waits for it (`plugin: {last_poll_utc,
queued}`, from the plugin queue) and how many alert lines were stamped in the last 24 hours (`alerts_recent`);
a figure that cannot be read is null, never an error.

Each project has its own CAS under its folder (`cas_for(project_id)`: the root the index registered, else
`<home>/projects/<id>`), and puts are recorded in that project's index. A packet that finished ingesting is
registered by reference in its project's `packets.json` (the folder is made, with custody C0 only, when the
packet arrives first) and announced on the bus as `packet.ingested` (project scope, no run id). A packet whose
manifest `supersedes` another goes through `revisions.on_new_packet` (the old packet's runs are marked, its
Plate nodes re-bound) and a `packet.superseded` record always follows, from revisions or from here. Then,
when `carryover.auto_enabled(project)` (the project's `settings.auto_carry_over`, `PLATE_AUTO_CARRY_OVER=0`
for the whole daemon), `carryover.on_supersede` carries the old packet's accepted layers onto the new one and
publishes `packet.carried`; an exception there is logged and never fails the ingest.

Lifespan: a background thread finishes the packets a killed daemon left half-hashed and drives on the
pipeline runs it left in a worker state (`Pipeline.resume`); then the nightly scheduler (`scheduler.Nightly`)
starts unless `PLATE_NIGHTLY=0`, and stops on shutdown. A scheduler that cannot be built or started is
logged and never keeps the API from serving.

Project leases (decision 83, `lease.py`): `create_app` installs this process's lease identity unless one is
installed already, so two apps in one process (the test suite) share one identity and one set of leases. The
first write to a project takes `<project>/lease.json`; the lifespan runs a `lease.Keeper` that refreshes the
heartbeat of every lease held, and on shutdown stops it and removes them (`lease.release_all`). A write to a
project another daemon's live lease covers is 409 `{"error": "project_leased", "host", "pid", "daemon_id",
"heartbeat_utc", "detail"}`; reads are served. `/health` counts the leases held (`leases`).

Wave 13 (T152): `finish` (the packet ingest's last step) calls `pipeline.warm_router` for an ingested packet, so
the render-it router's inputs are in the index before `packet.ingested` goes out (decision 84); the pipeline gets
`crash_exits_process`, so a scripted crash ends the process on every execution path (decision 85).

Unknown API paths answer 404 `{"error": "not_found", "detail": "no route /api/..."}` for every method (HEAD,
OPTIONS and TRACE included; `/api` and `/api/` too: a catch-all registered after the route modules), so a sender
never sees `405 Method Not Allowed` or the UI's HTML. `GET /api/v0/health` also answers HEAD.

Hardening (the stress test's api-fuzz findings): `BodyCap` refuses a JSON request body above
`Settings.json_max_bytes` (`PLATE_JSON_MAX_MB`, default 8 MiB) with 413 `too_large` before anything reads it, and a
POST/PUT/PATCH sent chunked, with no length, with 411 `length_required`; `/assets*` and `/hash*` keep their own
caps. Whatever a route lets escape is rendered as JSON, never the plain-text 500: `ProjectError` 422
`project_unreadable`, `index.UnknownProject` 404 `{"error": "not_found", "project_id": ...}` (a route opened the
index of a project that has none; opening never creates one, T144), `IllegalTransition` 409, `sqlite3.OperationalError` 507 `disk_full` or 503 `busy`, a NUL
byte in a path 422 `bad_path`, `OSError` 507 `disk_full` (ENOSPC) or 422 `filesystem`, and anything else 500
`internal` with the exception's class name (the traceback stays in the log, never in the body). The built UI (`ui/dist`) is served at `/` when it exists: `index.html` with `Cache-Control: no-cache`
and a same-origin Content-Security-Policy, the hashed `assets/*.js|css` immutable; otherwise `/` says how
to build it. Every response carries `X-Content-Type-Options: nosniff`. Errors raised as
`routes.ApiError(status, body)` are rendered as JSON; body validation errors are reshaped to
`422 {"errors": [...]}` so senders see one error format.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import (
    __version__,
    carryover,
    layer_upscale,
    lease,
    motion_runs,
    nodes,
    people_video,
    recomposite,
    revisions,
    route_health,
)
from .auth import AuthMiddleware
from .budgets import ALERT_KIND
from .cas import CAS
from .config import Settings, check_project_id
from .engine_fake import FakeEngine
from .index import Index, UnknownProject
from .ingest import finish_ingest, get_packet, pending_ingests
from .lease import ProjectLeased
from .ledger import Ledger
from .llm import judge_node
from .lock import ProjectLocked
from .machine import IllegalTransition
from .nodes import runner as node_runner
from .pipeline import Pipeline, PipelineError
from .pixels_client import DepthFn, OcrAdapter, PixelsClient, PixelsError, PixelsUnavailable
from .projects import Project, ProjectError
from .routes import API, ApiError, Ctx, illegal, include_all
from .routes import alerts as alert_routes
from .sse import Bus
from .timeutil import iso, utcnow

__all__ = [
    "API",
    "API_METHODS",
    "UI_CSP",
    "UI_NOT_BUILT",
    "ApiError",
    "BodyCap",
    "DaemonPipeline",
    "LazyDepth",
    "LazyOcr",
    "MotionRouterPreview",
    "PixelsSeam",
    "RecentAlerts",
    "alert_event",
    "create_app",
]

log = logging.getLogger("plate_daemon")
pixels_log = logging.getLogger("plate_daemon.pixels")

UI_NOT_BUILT = "UI not built: cd ui && npm run build\n"
# Same origin only. `blob:` for the pictures and clips the UI fetches with the token (canvas, viewer, video
# player), `data:` for inline thumbnails; React writes inline styles.
UI_CSP = (
    "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; "
    "style-src 'self' 'unsafe-inline'"
)
IMMUTABLE = "public, max-age=31536000, immutable"  # Vite names every built asset by its content hash
API_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"]
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
# The routes that cap their own bodies (assets.py reads a stream to its limit; hash takes payloads).
OWN_CAP_PREFIXES = (f"{API}/assets", f"{API}/hash")
NIGHTLY_OFF = frozenset({"0", "off", "false", "no"})  # as scheduler.NightlyConfig reads PLATE_NIGHTLY
PIXELS_PROBE_TTL_S = 30.0
REVISION_LOCK_WAIT_S = 5.0
ALERTS_RECENT_WINDOW = timedelta(hours=24)  # `/health` alerts_recent counts the lines stamped within it


def _fmt_validation(err: dict[str, Any]) -> str:
    loc = "/".join(str(p) for p in err.get("loc", ()) if p != "body") or "<body>"
    return f"{loc}: {err.get('msg', 'invalid')}"


def nightly_disabled(env: Mapping[str, str] | None = None) -> bool:
    return (os.environ if env is None else env).get("PLATE_NIGHTLY", "").strip().lower() in NIGHTLY_OFF


def default_nightly(settings: Settings, on_alert: Callable[[dict[str, Any]], None] | None = None) -> Any:
    """The scheduler the lifespan starts. Imported here so a broken scheduler module costs the nightly jobs
    only, never the API. `on_alert(line)` gets a copy of every alert line it appends (see `alert_event`)."""
    from .scheduler import Nightly

    return Nightly(settings, ledger_factory=lambda project: Ledger(project.root), on_alert=on_alert)


def alert_event(line: Mapping[str, Any]) -> dict[str, Any]:
    """The bus record for one alert line of `alerts.jsonl`: `kind` is the bus kind (`alert`), the line's own kind is
    `alert_kind`; `level` and `source` come from the mapping `GET /alerts` uses, so the status bar and the list
    agree."""
    kind = line.get("kind")
    at = line.get("at")
    event: dict[str, Any] = {
        "kind": ALERT_KIND,
        "source": alert_routes.source_of(kind),
        "level": alert_routes.level_of(dict(line)),
        "message": str(line.get("message", "")),
        "at": at if isinstance(at, str) and at else iso(utcnow()),
        "alert_kind": kind,
    }
    if line.get("engine_id"):
        event["engine_id"] = line["engine_id"]
    return event


def _read_manifest(packet_dir: Any) -> dict[str, Any]:
    try:
        doc = json.loads((Path(packet_dir) / "manifest.json").read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


@contextmanager
def _project_lock(project: Project, wait_s: float = REVISION_LOCK_WAIT_S) -> Iterator[None]:
    """The project's writer lock, retried while another handle holds it (`projects.Lock` fails at once)."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            lock = project.lock().acquire()
            break
        except ProjectLocked:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.release()


class RecentAlerts:
    """`/health` `alerts_recent`: how many alert lines (all files `GET /alerts` reads) are stamped within the last
    24 hours. The count is kept until an alert file changes or the minute turns, so the status bar's polling does not
    re-read every file each time. None when the files cannot be read."""

    def __init__(self, ctx: Callable[[], Ctx], clock: Callable[[], datetime]):
        self._ctx = ctx
        self._clock = clock
        self._guard = threading.Lock()
        self._memo: tuple[tuple[Any, ...], int] | None = None

    def count(self) -> int | None:
        try:
            c = self._ctx()
            now = self._clock()
            files: list[tuple[str, int, int]] = []
            for path, _project in alert_routes.alert_files(c):
                try:
                    st = path.stat()
                except OSError:
                    continue
                files.append((str(path), st.st_mtime_ns, st.st_size))
            sig = (tuple(files), int(now.timestamp() // 60))
            with self._guard:
                if self._memo is not None and self._memo[0] == sig:
                    return self._memo[1]
            n = len(alert_routes.collect(c, now - ALERTS_RECENT_WINDOW))
            with self._guard:
                self._memo = (sig, n)
            return n
        except Exception as e:  # health is the plugin's handshake: it never fails over a bad alert file
            log.warning("alerts_recent not counted: %s: %s", type(e).__name__, e)
            return None


# ---------------------------------------------------------------------------- the pixels worker, lazily


class PixelsSeam:
    """How the pipeline reaches the pixels worker. `client()` is the app's `PixelsClient` as it is now (a test
    or an operator may swap `app.state.pixels`); `ready()` says whether it answers. In process it always
    does; over HTTP the answer is a probe (`PixelsClient.available`: a worker that listens AND takes our
    token) kept for `ttl_s` seconds, and a call that finds no worker marks it down at once."""

    def __init__(
        self,
        client: Callable[[], PixelsClient],
        *,
        ttl_s: float = PIXELS_PROBE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.ttl_s = ttl_s
        self._clock = clock
        self._guard = threading.Lock()
        self._probe: tuple[PixelsClient, float, bool] | None = None  # (client, when, up)

    def ready(self) -> bool:
        client = self.client()
        if client.inproc:
            return True
        with self._guard:
            now = self._clock()
            p = self._probe
            if p is not None and p[0] is client and now - p[1] < self.ttl_s:
                return p[2]
            up = client.available()
            self._probe = (client, now, up)
            return up

    def mark_down(self) -> None:
        with self._guard:
            self._probe = (self.client(), self._clock(), False)

    def status(self) -> str:
        """`inproc`, `http` (a worker answers) or `unavailable`."""
        if self.client().inproc:
            return "inproc"
        return "http" if self.ready() else "unavailable"


class LazyOcr:
    """`Pipeline(ocr=...)`: `detect(rgb8)` through the client's `OcrAdapter`, built on the first call. While no
    worker answers, or the worker cannot read text (a model missing, not wired), it finds no boxes and logs
    one warning per outage. The text lock keeps its Cryptomatte text classes either way (doctrine 3); only
    the OCR boxes are missing."""

    def __init__(self, seam: PixelsSeam):
        self.seam = seam
        self._adapter: tuple[PixelsClient, OcrAdapter] | None = None
        self._warned = False

    @property
    def name(self) -> str:
        return f"pixels-{'inproc' if self.seam.client().inproc else 'http'}"

    def _skip(self, why: str) -> list[dict[str, Any]]:
        if not self._warned:
            self._warned = True
            pixels_log.warning("OCR text boxes skipped, so the text lock has no OCR boxes: %s", why)
        return []

    def detect(self, rgb8: np.ndarray) -> list[dict[str, Any]]:
        client = self.seam.client()
        if not self.seam.ready():
            return self._skip(
                f"no pixels worker answers on 127.0.0.1:{client.port}; start it with `python -m plate_pixels` "
                "or set PLATE_PIXELS_INPROC=1"
            )
        if self._adapter is None or self._adapter[0] is not client:
            self._adapter = (client, client.ocr_adapter())
        adapter = self._adapter[1]
        try:
            boxes = adapter.detect(rgb8)
        except PixelsError as e:
            return self._skip(f"the pixels worker could not read text ({e.status}: {e.detail})")
        if adapter.unavailable:  # it went away since the probe; the adapter has warned
            self.seam.mark_down()
            self._warned = True
            return boxes
        self._warned = False
        return boxes


class LazyDepth:
    """`Pipeline(depth_fn=...)` for `DaemonPipeline`: `resolve()` is the client's `DepthFn` while a worker
    answers, else None. Called directly it raises `PixelsUnavailable` when there is none."""

    def __init__(self, seam: PixelsSeam):
        self.seam = seam

    @property
    def name(self) -> str:
        return f"pixels-{'inproc' if self.seam.client().inproc else 'http'}-depth"

    def resolve(self) -> DepthFn | None:
        return self.seam.client().depth_fn() if self.seam.ready() else None

    def __call__(self, rgb8: np.ndarray) -> np.ndarray:
        fn = self.resolve()
        if fn is None:
            raise PixelsUnavailable(
                "no pixels worker answers: depth needs `python -m plate_pixels` or PLATE_PIXELS_INPROC=1"
            )
        try:
            return fn(rgb8)
        except PixelsUnavailable:
            self.seam.mark_down()
            raise


class DaemonPipeline(Pipeline):
    """The Pipeline `create_app` builds. A `LazyDepth` given as `depth_fn` is resolved on every read, so the
    gates (and the rescore route) see None while no pixels worker answers and take their "no depth backend"
    path instead of failing a Tier C run. Assigning `depth_fn` (a test's fake, or None) works as on a plain
    Pipeline."""

    @property  # type: ignore[override]
    def depth_fn(self) -> Any:
        fn = self.__dict__.get("_depth_fn")
        return fn.resolve() if isinstance(fn, LazyDepth) else fn

    @depth_fn.setter
    def depth_fn(self, fn: Any) -> None:
        self.__dict__["_depth_fn"] = fn


class MotionRouterPreview:
    """`nodes.runner.PREVIEWS["router"]`: `motion_runs.router_preview` (the Motion editor's router card) for a
    Motion or CropPair node, the camera moves the render-it router judges. Any other node asking for a router
    card is 404 `unknown_preview`, as an unknown `what` is."""

    KIND = motion_runs.router_preview.KIND
    NODE_KINDS = frozenset({"Motion", "CropPair"})

    def prepare(self, q: Any) -> Any:
        kind = q.node.get("kind")
        if kind not in self.NODE_KINDS:
            raise PipelineError(
                f"no router preview for a {kind} node (Motion or CropPair)",
                code="unknown_preview",
                status=404,
            )
        return motion_runs.router_preview.prepare(q)

    def preview(self, c: Any) -> dict[str, Any]:
        return motion_runs.router_preview.preview(c)


# ---------------------------------------------------------------------------- response headers


class SecurityHeaders:
    """Plain ASGI (streaming responses pass untouched): `X-Content-Type-Options: nosniff` on every response,
    the API's, the UI's and the refusals of `AuthMiddleware` (this wraps it)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).setdefault("X-Content-Type-Options", "nosniff")
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _content_length(raw: str) -> int | None:
    """A Content-Length value as a non-negative integer, None when it is not one."""
    raw = raw.strip()
    return int(raw) if raw.isascii() and raw.isdigit() else None


class BodyCap:
    """Plain ASGI, wrapped around `AuthMiddleware` (so it runs first) for `/api/` paths other than the assets and
    hash routes, which cap their own bodies: a `Content-Length` above `limit_bytes` is 413
    `{"error": "too_large", "limit_bytes": N}`, and a POST/PUT/PATCH that is chunked (Transfer-Encoding) or has a
    Content-Length that is not a number is 411 `{"error": "length_required"}`. Neither reads the body, and both
    close the connection, so a 50 MiB upload costs the daemon a header parse. A request with no Content-Length
    and no Transfer-Encoding has no body (HTTP/1.1) and passes."""

    def __init__(self, app: ASGIApp, limit_bytes: int):
        self.app = app
        self.limit_bytes = limit_bytes

    @staticmethod
    def covers(path: str) -> bool:
        return path.startswith("/api/") and not path.startswith(OWN_CAP_PREFIXES)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.covers(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        lengths = [_content_length(v) for v in headers.getlist("content-length")]
        if scope.get("method") in BODY_METHODS and (
            headers.get("transfer-encoding") is not None or any(n is None for n in lengths)
        ):
            await self._refuse(send, 411, {"error": "length_required"})
            return
        if any(n is not None and n > self.limit_bytes for n in lengths):
            await self._refuse(send, 413, {"error": "too_large", "limit_bytes": self.limit_bytes})
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _refuse(send: Send, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
            (b"connection", b"close"),  # the unread body is not left on a kept-alive connection
        ]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": payload})


class UiFiles(StaticFiles):
    """`ui/dist` with cache and content-security headers: HTML is revalidated on every load (`no-cache`) and
    carries `UI_CSP`; the content-hashed `assets/*.js|css` never change, so they are immutable."""

    def __init__(self, *, directory: Path, html: bool = True):
        super().__init__(directory=directory, html=html)
        self._assets = (Path(directory) / "assets").resolve()

    def file_response(
        self, full_path: Any, stat_result: os.stat_result, scope: Scope, status_code: int = 200
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        path = Path(full_path)
        suffix = path.suffix.lower()
        if suffix in (".html", ".htm"):
            response.headers["Cache-Control"] = "no-cache"
            response.headers["Content-Security-Policy"] = UI_CSP
        elif suffix in (".js", ".css") and path.resolve().is_relative_to(self._assets):
            response.headers["Cache-Control"] = IMMUTABLE
        return response


# ---------------------------------------------------------------------------- the app


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
    nightly_factory: Callable[[Settings], Any] | None = None,
) -> FastAPI:
    """Build the app. `clock` (aware-UTC callable) is swappable at `app.state.clock` for tests;
    `cas_for(project_id)` overrides where a project's CAS lives; `crash_exits_process` turns a scripted
    SimulatedCrash into a hard process exit (python -m plate_daemon). `engine` serves the `fake` engine id
    and the seed's `POST /runs` runs; `engines` adds daemon-protocol adapters by engine id; `manifests`
    replaces the engine manifests on disk (`plate_engines.loader`); `nightly_factory(settings)` builds the
    scheduler the lifespan starts (default `scheduler.Nightly`; kept at `app.state.nightly`)."""
    engine = engine if engine is not None else FakeEngine.from_env(journal=settings.home / "fake_engine.json")
    if not lease.enabled():  # decision 83; a second app in this process shares the first one's identity
        lease.install()
    index = Index(settings.home)
    bus = bus or Bus(index)

    def project_cas(project_id: str) -> CAS:
        root = index.project_root(project_id) or settings.projects_dir / check_project_id(project_id)

        def record(key: str, ext: str, nbytes: int) -> None:
            # The index is a cache: the object is safe in the folder. UnknownProject: the project has no index
            # yet (opening one never makes it); a rebuild finds the object.
            try:
                index.record_asset(project_id, key, ext, nbytes)
            except (sqlite3.Error, UnknownProject) as e:
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

    def start_nightly() -> None:
        if nightly_disabled():
            log.info("nightly scheduler off (PLATE_NIGHTLY=0)")
            return
        try:
            nightly = app.state.nightly
            if nightly is None:
                nightly = app.state.nightly = (nightly_factory or default_nightly)(settings)
            if getattr(nightly, "on_alert", None) is None:  # a factory that did not wire the bus gets ours
                nightly.on_alert = publish_alert
            nightly.start()  # False when config.json disables it or it is already started
        except Exception as e:  # the jobs wait for the next start; the API serves regardless
            log.warning("nightly scheduler not started: %s: %s", type(e).__name__, e)

    def stop_nightly() -> None:
        nightly = app.state.nightly
        if nightly is None:
            return
        try:
            nightly.stop()
        except Exception as e:
            log.warning("nightly scheduler did not stop cleanly: %s: %s", type(e).__name__, e)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Packets a killed daemon left half-hashed finish, and pipeline runs it left in flight drive on, in the
        # background; the API is up meanwhile. The node installs below have already wrapped the pipeline.
        threading.Thread(target=resume, name="plate-resume", daemon=True).start()
        start_nightly()
        keeper = lease.Keeper()
        keeper.start()
        try:
            yield
        finally:
            stop_nightly()
            keeper.stop()
            released = lease.release_all()
            if released:
                log.info("released %d project lease(s)", released)

    app = FastAPI(
        title="PLATE daemon",
        version=__version__,
        lifespan=lifespan,
        # No schema or docs pages: they would be served without the token (only /api/ is token-gated).
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.state.pixels = None
    app.state.nightly = None

    def pixels_client() -> PixelsClient:
        client = app.state.pixels
        if client is None:
            client = app.state.pixels = PixelsClient(settings)
        return client

    pixels = PixelsSeam(pixels_client)
    pipeline = DaemonPipeline(
        settings=settings,
        index=index,
        cas_for=cas_for,
        bus=bus,
        engine=engine,
        engines=engines,
        manifests=manifests,
        clock=lambda: app.state.clock(),
        ocr=LazyOcr(pixels),
        depth_fn=LazyDepth(pixels),
        crash_exits_process=crash_exits_process,  # decision 85: every execution path, not only the dev step
    )
    # Decision 38: every node kind beyond the Edit, wrapped onto this instance before any resume can run.
    nodes.install(pipeline)
    motion_runs.install(pipeline)
    judge_node.install(pipeline)
    # Wave 9, in this order: People in motion wraps the Motion runner so a People node with `params.video` is
    # consulted here first; then the free re-mask of a stored take and the AI-layer upscale hook.
    people_video.install(pipeline)
    recomposite.install(pipeline)
    layer_upscale.install(pipeline)
    pipeline.route_tracker = route_health.Tracker(settings.home)  # degraded routes reach the health board
    node_runner.PREVIEWS["router"] = MotionRouterPreview()

    def publish_alert(line: Mapping[str, Any]) -> None:
        """`Nightly.on_alert`: one `alert` bus record per line the nightly appended (see `alert_event`)."""
        event = alert_event(line)
        data = line.get("data")
        named = data.get("project_id") if isinstance(data, Mapping) else None
        known = index.project_ids()
        for pid in [named] if isinstance(named, str) and named in known else known:
            try:
                bus.publish(None, dict(event), project_id=pid)
            except Exception as e:  # the file is the record; a project the bus cannot write to misses one
                log.warning("alert %s not published for %s: %s", event["alert_kind"], pid, e)

    def announce(project_id: str, packet_id: str, row: Mapping[str, Any]) -> None:
        supersedes = _read_manifest(row.get("path")).get("supersedes") or None
        pipeline.publish(
            project_id,
            None,
            {
                "kind": "packet.ingested",
                "packet_id": packet_id,
                "project_id": project_id,
                "shot_id": row.get("shot_id"),
                "tier": row.get("tier"),
                "packet_kind": row.get("kind"),  # still | motion ("kind" is the event's own)
                "supersedes": supersedes,
            },
        )
        if supersedes:
            supersede(project_id, packet_id, supersedes)

    def supersede(project_id: str, packet_id: str, old_id: str) -> None:
        revise(project_id, packet_id, old_id)
        carry(project_id, packet_id, old_id)

    def carry(project_id: str, packet_id: str, old_id: str) -> None:
        """The artist's accepted layers of the old packet ride onto the new one (decision 25), unless the project
        or `PLATE_AUTO_CARRY_OVER=0` turned that off. The project lock is not held: the ledger takes it per append."""
        try:
            project = pipeline.load_project(project_id)
            if project is None or not carryover.auto_enabled(project):
                return
            view = pipeline.packet(project_id, packet_id)
            carryover.on_supersede(pipeline, project, view, old_id, bus=bus)
        except Exception as e:  # the packet stays ingested; the carry-over button is the manual route
            problem = f"{type(e).__name__}: {e}"
            log.warning("carry-over %s -> %s in %s failed: %s", old_id, packet_id, project_id, problem)

    def revise(project_id: str, packet_id: str, old_id: str) -> None:
        rev: revisions.Revision | None = None
        problem = ""
        try:
            project = pipeline.load_project(project_id)
            if project is None:
                raise LookupError(f"project {project_id} has no project.json")
            view = pipeline.packet(project_id, packet_id)
            with _project_lock(project):
                project = pipeline.load_project(project_id) or project  # the graph as it is under the lock
                with index.project(project_id) as conn:
                    rev = revisions.on_new_packet(project, view, conn=conn, bus=bus)
        except Exception as e:  # the packet stays ingested; its predecessor's runs are just not marked
            problem = f"{type(e).__name__}: {e}"
            log.warning("revision %s -> %s in %s failed: %s", old_id, packet_id, project_id, problem)
        if rev is not None and rev.invalidated:
            return  # revisions published packet.superseded (and run.superseded per marked run)
        body = (
            rev.to_dict()
            if rev is not None
            else {
                "packet_id": packet_id,
                "supersedes": old_id,
                "invalidated": False,
                "reason": f"revision failed: {problem}",
                "superseded_runs": [],
                "already_superseded": [],
                "rebound_nodes": [],
            }
        )
        pipeline.publish(project_id, None, {"kind": "packet.superseded", **body})

    def finish(project_id: str, packet_id: str) -> None:
        with ingest_guard:
            if packet_id in active_ingests:
                return
            active_ingests.add(packet_id)
        try:
            with index.project(project_id) as conn:
                finish_ingest(conn, cas_for(project_id), packet_id, cas_copy=settings.cas_copy)
                row = get_packet(conn, packet_id, with_blobs=False)
            if row["status"] == "ingested":
                try:
                    pipeline.register_packet(project_id, packet_id)
                except Exception as e:  # the index has it; packets.json catches up on the next send
                    log.warning("packet %s not registered in %s/packets.json: %s", packet_id, project_id, e)
                # Decision 84: the router's inputs once, here, before anyone is told the packet is in (so the
                # first quote, and a carry-over's, decodes nothing for the router). Never raises.
                pipeline.warm_router(project_id, packet_id)
                try:
                    announce(project_id, packet_id, row)
                except Exception as e:  # watchers miss one record; the packet is ingested regardless
                    log.warning("packet %s ingested but not announced: %s", packet_id, e)
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
    # Outside the auth check, so an oversized body is refused without being read.
    app.add_middleware(BodyCap, limit_bytes=settings.json_max_bytes)
    app.add_middleware(SecurityHeaders)  # added last, so outermost: the auth refusals carry it too
    app.state.settings = settings
    app.state.engine = engine
    app.state.clock = clock or utcnow
    app.state.cas_for = cas_for
    app.state.index = index
    app.state.bus = bus
    app.state.pipeline = pipeline
    app.state.pixels_seam = pixels
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

    # What a route lets escape is still JSON. Starlette picks the handler by the exception's most specific class,
    # so ProjectError (a ValueError) is not taken by the ValueError handler.

    @app.exception_handler(ProjectError)
    async def _project_error(_request: Request, exc: ProjectError) -> JSONResponse:
        return JSONResponse({"error": "project_unreadable", "detail": str(exc)}, status_code=422)

    @app.exception_handler(ProjectLeased)
    async def _project_leased(_request: Request, exc: ProjectLeased) -> JSONResponse:
        # Decision 83: another daemon's live lease covers the project; nothing was written.
        body = {
            "error": "project_leased",
            "host": exc.host,
            "pid": exc.pid,
            "daemon_id": exc.daemon_id,
            "heartbeat_utc": exc.heartbeat_utc,
            "detail": str(exc),
        }
        return JSONResponse(body, status_code=409)

    @app.exception_handler(UnknownProject)
    async def _unknown_project(_request: Request, exc: UnknownProject) -> JSONResponse:
        # A route opened the index of a project that has none: the project does not exist (nothing was created).
        body: dict[str, Any] = {"error": "not_found", "project_id": exc.project_id}
        if exc.project_id is None:
            body["detail"] = str(exc)
        return JSONResponse(body, status_code=404)

    @app.exception_handler(IllegalTransition)
    async def _illegal_transition(_request: Request, exc: IllegalTransition) -> JSONResponse:
        err = illegal(exc)
        return JSONResponse(err.body, status_code=err.status)

    @app.exception_handler(sqlite3.OperationalError)
    async def _sqlite_error(_request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        detail = str(exc)
        if "disk is full" in detail:
            return JSONResponse({"error": "disk_full", "detail": detail}, status_code=507)
        return JSONResponse({"error": "busy", "retry": True, "detail": detail}, status_code=503)

    @app.exception_handler(ValueError)
    async def _value_error(_request: Request, exc: ValueError) -> JSONResponse:
        if "embedded null" in str(exc):  # os.stat / open / mkdir on a path with a NUL byte
            return JSONResponse({"error": "bad_path", "detail": "a path cannot contain NUL"}, status_code=422)
        raise exc  # not ours: the Exception handler answers it (and the server logs the traceback)

    @app.exception_handler(OSError)
    async def _os_error(_request: Request, exc: OSError) -> JSONResponse:
        what = str(exc.strerror or exc)
        detail = what if exc.filename is None else f"{what}: {exc.filename}"
        if exc.errno == errno.ENOSPC:
            return JSONResponse({"error": "disk_full", "detail": detail}, status_code=507)
        code = errno.errorcode.get(exc.errno, "unknown") if exc.errno is not None else "unknown"
        return JSONResponse({"error": "filesystem", "errno": code, "detail": detail}, status_code=422)

    @app.exception_handler(Exception)
    async def _internal_error(_request: Request, exc: Exception) -> JSONResponse:
        # Outermost handler (ServerErrorMiddleware): it sends this, then re-raises so the server logs the
        # traceback. It sits outside SecurityHeaders, so the header is set here.
        return JSONResponse(
            {"error": "internal", "exception": type(exc).__name__},
            status_code=500,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    # ------------------------------------------------------------------ health (no auth)

    recent_alerts = RecentAlerts(lambda: app.state.ctx, lambda: app.state.clock())

    def plugin_health() -> dict[str, Any]:
        """When a Max dock last polled and how many jobs wait for one (`GET /plugin/status` has the detail)."""
        try:
            queue = pipeline.node_runner.queue
            return {"last_poll_utc": queue.last_poll_utc(), "queued": int(queue.counts().get("queued", 0))}
        except Exception as e:
            log.warning("plugin status not read for /health: %s: %s", type(e).__name__, e)
            return {"last_poll_utc": None, "queued": None}

    # FastAPI's GET routes do not answer HEAD on their own; the plugin's handshake probe may use it.
    @app.api_route(f"{API}/health", methods=["GET", "HEAD"])
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "daemon_version": __version__,
            "packet_versions": list(settings.packet_versions),
            "stub_engine": bool(getattr(engine, "stub", False)),
            "pixels": pixels.status(),
            "plugin": plugin_health(),
            "alerts_recent": recent_alerts.count(),
            "leases": len(lease.held()),
        }

    include_all(app)

    # ------------------------------------------------------------------ unknown API paths: a JSON 404

    # After every route module and before the UI mount: a path no router knows, or a method its route does
    # not take, answers a JSON 404 rather than a 405 or the UI's HTML.
    def not_found(rest: str) -> JSONResponse:
        return JSONResponse({"error": "not_found", "detail": f"no route /api/{rest}"}, status_code=404)

    @app.api_route("/api", methods=API_METHODS, include_in_schema=False)
    def api_root_not_found() -> JSONResponse:  # `/api` without the slash is the same answer as `/api/`
        return not_found("")

    @app.api_route("/api/{rest:path}", methods=API_METHODS, include_in_schema=False)
    def api_not_found(rest: str) -> JSONResponse:
        return not_found(rest)

    # ------------------------------------------------------------------ the built UI at /

    ui_dist = Path(settings.ui_dist)
    if (ui_dist / "index.html").is_file():
        app.mount("/", UiFiles(directory=ui_dist, html=True), name="ui")
    else:

        @app.get("/", response_class=PlainTextResponse, include_in_schema=False)
        def ui_not_built() -> str:
            return UI_NOT_BUILT

    return app
