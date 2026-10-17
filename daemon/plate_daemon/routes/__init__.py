"""The daemon's HTTP routes under `/api/v0`, one module per resource (spec/PACKET.md section 9, router plan T25).

`include_all(app)` imports every module of this package in name order and includes its `router` (an
`fastapi.APIRouter`), so a later task adds `tierc.py` or `misc.py` here without editing this file or
`api.py`. A module whose name starts with `_` is skipped; a module without a `router` is a helper.

What the route modules share lives here:

- `API`, the prefix, and `ApiError(status, body)`, which `api.create_app` renders as `JSONResponse(body)`;
- `require_token`, the router-level bearer check (a second check behind `auth.AuthMiddleware`). It reads
  the token from `request.app.state.settings`, so it is a module-level dependency: under
  `from __future__ import annotations` FastAPI resolves `Depends(...)` from module globals;
- `Ctx` and `ctx(request)`: what `create_app` wires (settings, engine, index, bus, the per-project CAS,
  the pipeline, the packet-ingest finisher); `ctx(request).now()` reads the swappable `app.state.clock`;
- id lookups that answer 404 (`run_project`, `packet_project`) and the error mappings shared by the modules
  (`illegal` 409, `custody_error` 403 with `CustodyViolation`, `pipeline_error`);
- the SSE helpers of the seed's polling streams (`sse_frame`, `cursor`).
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, Header, Request

from ..cas import CAS
from ..config import Settings
from ..custody import CustodyViolation
from ..index import Index
from ..machine import IllegalTransition
from ..pipeline import Pipeline, PipelineError
from ..sse import Bus

__all__ = [
    "API",
    "MAX_WAIT_S",
    "SSE_POLL_S",
    "SSE_RETRY_MS",
    "ApiError",
    "Ctx",
    "ctx",
    "cursor",
    "custody_error",
    "illegal",
    "include_all",
    "packet_project",
    "pipeline_error",
    "require_token",
    "run_project",
    "sse_frame",
]

API = "/api/v0"
SSE_RETRY_MS = 2000
SSE_POLL_S = 0.25
MAX_WAIT_S = 120.0


class ApiError(Exception):
    def __init__(self, status: int, body: dict[str, Any]):
        self.status, self.body = status, body
        super().__init__(f"{status} {body}")


@dataclass
class Ctx:
    """Everything a route needs, built once by `api.create_app` and kept at `app.state.ctx`."""

    app: FastAPI
    settings: Settings
    engine: Any
    index: Index
    bus: Bus
    cas_for: Callable[[str], CAS]
    pipeline: Pipeline
    finish_packet: Callable[[str, str], None]
    crash_exits_process: bool = False

    def now(self) -> datetime:
        return self.app.state.clock()


def ctx(request: Request) -> Ctx:
    return request.app.state.ctx


def require_token(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
    token = request.app.state.settings.token
    scheme, _, presented = (authorization or "").partition(" ")
    ok = scheme.lower() == "bearer" and secrets.compare_digest(
        presented.strip().encode("utf-8"), token.encode("utf-8")
    )
    if not ok:
        raise ApiError(401, {"error": "unauthorized"})


# ---------------------------------------------------------------------------- lookups and error mappings


def run_project(c: Ctx, run_id: str) -> str:
    pid = c.index.locate_run(run_id)
    if pid is None:
        raise ApiError(404, {"error": "not_found", "run_id": run_id})
    return pid


def packet_project(c: Ctx, packet_id: str) -> str:
    pid = c.index.locate_packet(packet_id)
    if pid is None:
        raise ApiError(404, {"error": "not_found", "packet_id": packet_id})
    return pid


def illegal(e: IllegalTransition) -> ApiError:
    return ApiError(
        409,
        {"error": "illegal_transition", "from": e.from_state.value, "to": e.to_state.value, "detail": str(e)},
    )


def custody_error(e: CustodyViolation) -> ApiError:
    """403: the route's effective grade is outside what the project and packet allow (nothing was sent)."""
    return ApiError(
        403,
        {
            "error": "custody",
            "exception": type(e).__name__,
            "detail": str(e),
            "route_id": e.route_id,
            "grade": e.grade.value if e.grade is not None else None,
            "allowed": sorted(g.value for g in e.allowed),
        },
    )


def pipeline_error(e: PipelineError) -> ApiError:
    return ApiError(e.status, {"error": e.code, "detail": str(e), **e.extra})


# ---------------------------------------------------------------------------- SSE (the seed's polling streams)


def sse_frame(data: dict[str, Any], event_id: str | int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def cursor(after: int | None, last_event_id: str | None) -> int:
    if after is not None:
        return max(0, after)
    try:
        return max(0, int(last_event_id)) if last_event_id else 0
    except ValueError:
        return 0


# ---------------------------------------------------------------------------- discovery


def include_all(app: FastAPI) -> list[str]:
    """Include the `router` of every route module, in name order. Returns the module names included."""
    names: list[str] = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            app.include_router(router)
            names.append(info.name)
    return names
