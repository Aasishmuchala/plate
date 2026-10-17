"""PLATE pixels worker: a FastAPI app on 127.0.0.1:4611 fronting the CV backends.

Start it with ``python -m plate_pixels.server`` (or ``python -m plate_pixels``). It binds loopback only;
``PLATE_PIXELS_PORT`` changes the port, nothing changes the address. Stub mode is the default and imports no
``torch``; ``PLATE_PIXELS_REAL=1`` builds the real models instead, and a model that cannot be imported or
loaded is then an HTTP 500 on its own route (the MaxDirector pattern) rather than a quiet stub.

Auth is the daemon's: one bearer token, read from ``PLATE_TOKEN_FILE`` (a file holding only the token) or
else ``config.json`` (``PLATE_CONFIG`` / ``PLATE_HOME`` / ``platformdirs.user_data_dir("Plate",
appauthor=False)``). Every route but ``/health`` needs ``Authorization: Bearer <token>``; the ``Host`` header
must be ``127.0.0.1:<port>`` or ``localhost:<port>`` (DNS-rebinding guard) and a browser ``Origin`` header is
refused (no web page talks to this worker).

Routes (all JSON in, JSON out): ``POST /health`` (also GET) ``/depth`` ``/intrinsics`` ``/segment`` ``/flow``
``/ocr`` ``/tierc`` ``/admission`` plus the two AI-layer helpers ``/upscale`` and ``/lpips``. File PATHS go
in and file PATHS come out, never image bytes: an output defaults to a content-named file under
``<app home>/cache/pixels`` and ``out`` overrides it. Every response carries ``stub`` (true when the answer
came from a stub backend: never a measurement). Status codes: 400 bad input, 401/403 auth, 428 no client
consent record, 500 a real model is missing or a bug, 501 not wired yet, 503 the GPU cannot take the model.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import socket
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import blake3
import numpy as np
import platformdirs
import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__, exr, imageio
from .backends import (
    REAL_ENV,
    Backend,
    Backends,
    InputError,
    ModelUnavailable,
    image_shape,
    read_rgb,
    real_required,
)
from .flow import check_pair
from .licences import REGISTRY, LicenceError, licence_summary
from .nvml import Admission, AdmissionDenied
from .tierc import ConsentRequired, TierCError, build_tier_c_packet, write_label_png

log = logging.getLogger("plate_pixels")

HOST = "127.0.0.1"
PORT = 4611
PORT_ENV = "PLATE_PIXELS_PORT"
DAEMON_URL = "http://127.0.0.1:4610"
MIN_TOKEN_LEN = 16
OPEN_PATHS = frozenset({"/health"})
PLATE_UPSCALE_ERROR = "plate upscaling is forbidden (BUILD_PLAN section 1)"


# --------------------------------------------------------------------------- the shared token


class TokenUnavailable(RuntimeError):
    """No bearer token could be found or read (``PLATE_TOKEN_FILE`` / ``config.json``)."""


def app_home(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get("PLATE_HOME"):
        return Path(env["PLATE_HOME"])
    return Path(platformdirs.user_data_dir("Plate", appauthor=False))


def config_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get("PLATE_CONFIG"):
        return Path(env["PLATE_CONFIG"])
    return app_home(env) / "config.json"


def cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Where outputs land when a request names no ``out``: ``<app home>/cache/pixels`` (the daemon's cache)."""
    return app_home(env) / "cache" / "pixels"


def _read_config(path: Path) -> dict[str, Any]:
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise TokenUnavailable(f"{path}: cannot read it ({exc})") from exc
    except ValueError as exc:
        raise TokenUnavailable(f"{path}: not valid JSON ({exc}); fix or delete it") from exc
    if not isinstance(cfg, dict):
        raise TokenUnavailable(f"{path}: expected a JSON object")
    return cfg


def resolve_token(env: Mapping[str, str] | None = None, create: bool = False) -> str:
    """The daemon's bearer token: ``PLATE_TOKEN_FILE`` wins, else ``config.json``'s ``token``.

    With ``create`` a missing token is added to ``config.json`` the way the daemon does it (other keys,
    which belong to the Max plugin, are kept), so the worker and the daemon agree whichever starts first.
    """
    env = os.environ if env is None else env
    if env.get("PLATE_TOKEN_FILE"):
        path = Path(env["PLATE_TOKEN_FILE"])
        try:
            token = path.read_text(encoding="utf-8-sig").strip()  # utf-8-sig: PowerShell 5.1 may add a BOM
        except OSError as exc:
            raise TokenUnavailable(f"PLATE_TOKEN_FILE={path}: cannot read it ({exc})") from exc
        if len(token) < MIN_TOKEN_LEN:
            raise TokenUnavailable(
                f"PLATE_TOKEN_FILE={path}: the token must be at least {MIN_TOKEN_LEN} characters"
            )
        return token
    path = config_path(env)
    cfg = _read_config(path) if path.is_file() else {}
    token = cfg.get("token")
    if isinstance(token, str) and token:
        return token
    if not create:
        raise TokenUnavailable(
            f"no token: start the daemon once (it creates {path}) or set PLATE_TOKEN_FILE to a file holding the token"
        )
    cfg["token"] = secrets.token_urlsafe(32)
    cfg.setdefault("daemon_url", DAEMON_URL)
    with imageio.atomic_target(path) as part:
        part.write_text(json.dumps(cfg, indent=1) + "\n", encoding="utf-8", newline="\n")
    with contextlib.suppress(OSError):  # owner-only on POSIX; %LOCALAPPDATA% is already per-user on Windows
        os.chmod(path, 0o600)
    return str(cfg["token"])


# --------------------------------------------------------------------------- request guard


class Guard:
    """Plain ASGI: Host allowlist, no browser Origin, bearer token (constant-time) on every route but /health."""

    def __init__(self, app: ASGIApp, token: str, port: int) -> None:
        self.app = app
        self.token = token.encode("utf-8")
        self.hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = Headers(scope=scope)
        hosts = headers.getlist("host")
        if len(hosts) != 1 or hosts[0].strip().lower() not in self.hosts:
            await self._deny(scope, receive, send, 403, "forbidden_host")
            return
        if headers.getlist("origin"):
            await self._deny(scope, receive, send, 403, "forbidden_origin")
            return
        if scope.get("path", "") not in OPEN_PATHS and not self._bearer_ok(headers.get("authorization")):
            await self._deny(scope, receive, send, 401, "unauthorized")
            return
        await self.app(scope, receive, send)

    def _bearer_ok(self, authorization: str | None) -> bool:
        scheme, _, presented = (authorization or "").partition(" ")
        presented = presented.strip()
        if scheme.lower() != "bearer" or not presented:
            return False
        return secrets.compare_digest(presented.encode("utf-8"), self.token)

    @staticmethod
    async def _deny(scope: Scope, receive: Receive, send: Send, status: int, error: str) -> None:
        await JSONResponse({"error": error}, status_code=status)(scope, receive, send)


# --------------------------------------------------------------------------- request models


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathReq(_Req):
    image: str = Field(min_length=1)


class OutReq(PathReq):
    out: str | None = None


class SegmentReq(OutReq):
    points: list[tuple[float, float]] | None = None


class FlowReq(_Req):
    a: str = Field(min_length=1)
    b: str = Field(min_length=1)
    out: str | None = None


class TierCReq(_Req):
    plate_path: str = Field(min_length=1)
    out_dir: str = Field(min_length=1)
    project_id: str
    shot_id: str
    consent_record: str = ""
    labels: dict[int, dict[str, str]] | None = None
    nda: bool = False


class AdmissionReq(_Req):
    required_mb: int = Field(0, ge=0)
    reserve_mb: int = Field(2048, ge=0)
    model: str | None = None


class UpscaleReq(_Req):
    image: str = Field(min_length=1)
    scale: float = Field(2.0, gt=0)
    ai_layer: bool = False  # must be true: only AI-generated layers may be upscaled
    is_plate: bool = False


class LpipsReq(_Req):
    a: str = Field(min_length=1)
    b: str = Field(min_length=1)


# --------------------------------------------------------------------------- the app


def _error(status: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


def _respond(work: Callable[[], dict[str, Any]]) -> JSONResponse:
    """Run one route body and map its failures to the documented status codes."""
    try:
        return JSONResponse(work())
    except InputError as exc:
        return _error(400, str(exc))
    except ConsentRequired:
        return _error(428, "consent record required")
    except FileExistsError as exc:
        return _error(409, str(exc))
    except ValueError as exc:
        return _error(400, str(exc))
    except AdmissionDenied as exc:
        return _error(503, str(exc))
    except ModelUnavailable as exc:
        return _error(500, str(exc), real_required=real_required())
    except NotImplementedError as exc:
        return _error(501, str(exc))
    except TierCError as exc:
        return _error(500, str(exc), errors=exc.errors)
    except LicenceError as exc:
        return _error(500, str(exc))
    except Exception as exc:  # noqa: BLE001 - a route must always answer JSON
        log.exception("pixels route failed")
        return _error(500, f"{type(exc).__name__}: {exc}")


def _identity(backend: Backend) -> dict[str, Any]:
    return {"stub": bool(backend.is_stub), "model": backend.model_id, "impl": backend.impl}


def create_app(
    token: str,
    backends: Backends | None = None,
    admission: Admission | None = None,
    port: int = PORT,
    env: Mapping[str, str] | None = None,
) -> FastAPI:
    """Build the worker app. Nothing heavy happens here: backends are built on first use."""
    if not token:
        raise ValueError("the pixels worker needs a bearer token")
    env = os.environ if env is None else env
    backends = Backends() if backends is None else backends
    admission = Admission(env=env) if admission is None else admission
    gpu_lock = threading.RLock()
    admitted: set[str] = set()

    app = FastAPI(
        title="PLATE pixels worker", version=__version__, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.backends = backends
    app.state.admission = admission

    def use(*kinds: str) -> contextlib.AbstractContextManager[Any]:
        """Admit each real GPU backend once (VRAM headroom, scheduled windows) and serialise GPU work."""
        real = [b for b in (backends.get(k) for k in kinds) if not b.is_stub and b.vram_mb > 0]
        for backend in real:
            if backend.model_id not in admitted:
                ok, reason = admission.admit(backend.vram_mb, model=backend.model_id)
                if not ok:
                    raise AdmissionDenied(reason)
                admitted.add(backend.model_id)
        return gpu_lock if real else contextlib.nullcontext()

    def out_path(explicit: str | None, kind: str, sources: list[str], ext: str) -> Path:
        if explicit:
            target = Path(explicit)
            if target.is_dir():
                raise InputError(f"out {explicit!r} is a folder: name the output file")
            return target
        h = blake3.blake3(kind.encode("utf-8"))
        for source in sources:
            p = Path(source)
            st = p.stat()
            h.update(str(p.resolve()).encode("utf-8"))
            h.update(f"|{st.st_size}|{st.st_mtime_ns}|".encode("ascii"))
        return cache_dir(env) / f"{kind}-{h.hexdigest()[:16]}{ext}"

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return _error(404, f"unknown route {request.url.path}")
        return _error(exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _bad_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [
            {"field": ".".join(str(p) for p in e["loc"] if p != "body"), "problem": e["msg"]}
            for e in exc.errors()
        ]
        first = problems[0] if problems else {"field": "", "problem": "invalid request"}
        return _error(
            400, f"invalid request: {first['field'] or 'body'}: {first['problem']}", detail=problems
        )

    @app.api_route("/health", methods=["GET", "POST"])
    def health() -> JSONResponse:
        def work() -> dict[str, Any]:
            described = backends.describe()
            available = backends.availability()
            return {
                "ok": True,
                "version": __version__,
                "stub_mode": any(v["stub"] for v in described.values()),
                "real_required": real_required(),
                "torch": "torch" in sys.modules,
                "backends": described,
                "models": {
                    e.name: {
                        "available": bool(available.get(e.name, False)),
                        "licence": licence_summary(e),
                        "commercial_ok": e.commercial_ok,
                    }
                    for e in REGISTRY
                },
            }

        return _respond(work)

    @app.post("/depth")
    def depth(req: OutReq) -> JSONResponse:
        def work() -> dict[str, Any]:
            backend = backends.depth
            rgb = read_rgb(req.image)
            with use("depth"):
                z = backend.infer(rgb)
            target = out_path(req.out, "depth", [req.image], ".exr")
            exr.write_multilayer(target, {"": {"Z": z}}, {"": "float"})
            return {
                **_identity(backend),
                "shape": list(z.shape),
                "depth_exr": str(target),
                "channel": "Z",
                "depth_semantics": backend.depth_semantics,
                "depth_is_metric": False,
                "min": float(z.min()),
                "max": float(z.max()),
            }

        return _respond(work)

    @app.post("/intrinsics")
    def intrinsics(req: PathReq) -> JSONResponse:
        def work() -> dict[str, Any]:
            backend = backends.intrinsics
            rgb = read_rgb(req.image)
            with use("intrinsics"):
                result = backend.calibrate(rgb)
            return {
                **_identity(backend),
                "shape": list(rgb.shape[:2]),
                **{k: float(v) for k, v in result.items()},
            }

        return _respond(work)

    @app.post("/segment")
    def segment(req: SegmentReq) -> JSONResponse:
        def work() -> dict[str, Any]:
            backend = backends.segment
            rgb = read_rgb(req.image)
            prompts = [[x, y] for x, y in req.points] if req.points is not None else None
            with use("segment"):
                labels, found = backend.segment(rgb, prompts)
            target = out_path(req.out, "segments", [req.image], ".png")
            write_label_png(target, labels)
            return {
                **_identity(backend),
                "shape": list(labels.shape),
                "segments_png": str(target),
                "segments": found,
            }

        return _respond(work)

    @app.post("/flow")
    def flow(req: FlowReq) -> JSONResponse:
        def work() -> dict[str, Any]:
            backend = backends.flow
            a, b = read_rgb(req.a), read_rgb(req.b)
            check_pair(a, b)
            with use("flow"):
                u, v = backend.flow(a, b)
            target = out_path(req.out, "flow", [req.a, req.b], ".exr")
            exr.write_multilayer(target, {"": {"U": u, "V": v}}, {"": "float"})
            return {
                **_identity(backend),
                "shape": list(u.shape),
                "flow_exr": str(target),
                "channels": ["U", "V"],
                "eval_width": backend.eval_width,
                "median_u": float(np.median(u)),
                "median_v": float(np.median(v)),
                "median_px": float(np.median(np.hypot(u, v))),
            }

        return _respond(work)

    @app.post("/ocr")
    def ocr(req: PathReq) -> JSONResponse:
        def work() -> dict[str, Any]:
            backend = backends.ocr
            height, width = image_shape(req.image)
            with use("ocr"):
                boxes = backend.detect_file(req.image)
            return {**_identity(backend), "shape": [height, width], "boxes": boxes}

        return _respond(work)

    @app.post("/tierc")
    def tierc(req: TierCReq) -> JSONResponse:
        def work() -> dict[str, Any]:
            if not req.consent_record.strip():  # before any backend is built or admitted
                raise ConsentRequired("a client consent record is required to build a tier C packet")
            kinds = ("depth", "intrinsics", "segment")
            with use(*kinds):
                packet = build_tier_c_packet(
                    req.plate_path,
                    req.out_dir,
                    backends,
                    req.project_id,
                    req.shot_id,
                    req.consent_record,
                    labels=req.labels,
                    nda=req.nda,
                )
            manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
            return {
                "stub": any(backends.get(k).is_stub for k in kinds),
                "packet_dir": str(packet),
                "packet_id": manifest["packet_id"],
                "tier": manifest["tier"],
                "estimated": {k: manifest["estimated"][k] for k in ("depth", "intrinsics", "segments")},
            }

        return _respond(work)

    @app.post("/admission")
    def admission_route(req: AdmissionReq) -> JSONResponse:
        def work() -> dict[str, Any]:
            ok, reason = admission.admit(req.required_mb, req.reserve_mb, req.model)
            return {"ok": ok, "reason": reason, "stub": False, **admission.snapshot()}

        return _respond(work)

    @app.post("/upscale")
    def upscale(req: UpscaleReq) -> JSONResponse:
        def work() -> dict[str, Any]:
            if req.is_plate or not req.ai_layer:  # doctrine: the plate is never upscaled
                raise InputError(PLATE_UPSCALE_ERROR)
            backend = backends.upscale
            read_rgb(req.image)
            with use("upscale"):
                return {**_identity(backend), **backend.upscale(req.image, req.scale)}

        return _respond(work)

    @app.post("/lpips")
    def lpips(req: LpipsReq) -> JSONResponse:
        def work() -> dict[str, Any]:
            backend = backends.lpips
            read_rgb(req.a)
            read_rgb(req.b)
            with use("lpips"):
                return {**_identity(backend), **backend.distance(req.a, req.b)}

        return _respond(work)

    app.add_middleware(Guard, token=token, port=port)  # in front of every route and of validation
    return app


# --------------------------------------------------------------------------- process entry


def configured_port(env: Mapping[str, str] | None = None) -> int:
    raw = (os.environ if env is None else env).get(PORT_ENV)
    if not raw:
        return PORT
    try:
        port = int(raw)
    except ValueError:
        raise SystemExit(f"{PORT_ENV} must be an integer, got {raw!r}") from None
    if not 0 < port < 65536:
        raise SystemExit(f"{PORT_ENV} must be 1-65535, got {port}")
    return port


def bind_socket(host: str, port: int) -> socket.socket:
    """One listening socket on ``host``. On Windows it is exclusive: SO_REUSEADDR there would let another
    process bind the same port and steal requests; on POSIX it only allows an immediate rebind."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":  # pragma: no cover - Windows only
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except BaseException:
        sock.close()
        raise
    return sock


def missing_real() -> dict[str, str]:
    """Real backends that cannot run here, model -> reason; probes package specs, imports nothing heavy."""
    from .backends import _real_classes

    out: dict[str, str] = {}
    for cls in _real_classes().values():
        ok, reason = cls.probe()
        if not ok:
            out[cls.model_id] = reason
    return out


def main() -> int:
    port = configured_port()
    try:
        token = resolve_token(create=True)
    except TokenUnavailable as exc:
        print(f"plate_pixels: {exc}", file=sys.stderr)
        return 2
    backends = Backends()
    app = create_app(token, backends=backends, port=port)
    try:
        sock = bind_socket(HOST, port)
    except OSError as exc:
        print(f"plate_pixels: cannot listen on {HOST}:{port}: {exc}", file=sys.stderr)
        return 2
    stub = any(v["stub"] for v in backends.describe().values())
    print(f"PLATE pixels http://{HOST}:{port} (stub_mode={stub})", flush=True)
    if real_required():
        missing = [f"{name} ({reason})" for name, reason in missing_real().items()]
        if missing:
            print(
                f"plate_pixels: {REAL_ENV}=1 but not runnable here: {'; '.join(missing)}",
                file=sys.stderr,
                flush=True,
            )
    config = uvicorn.Config(
        app,
        log_level=os.environ.get("PLATE_LOG_LEVEL", "warning").lower(),
        proxy_headers=False,
        server_header=False,
        lifespan="off",
    )
    try:
        uvicorn.Server(config).run(sockets=[sock])
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
