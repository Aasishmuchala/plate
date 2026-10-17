"""PLATE pixels service: a localhost HTTP server (stdlib only) fronting the CV backends.

Seeded from ``cv_sidecar/server.py``. Start it with ``python -m plate_pixels.server`` (127.0.0.1:4611,
``PLATE_PIXELS_PORT`` overrides the port; the bind address is never configurable). All routes are POST
with JSON in and JSON out; unknown routes answer 404, bad input 400. Runs in stub mode by default and
imports no ``torch``. Backends are built when a server is created, never at import time.

Routes: /health /depth /calibrate /segment /flow /textlock /upscale /lpips. See docs/pixels.md.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .backends import Backends, InputError, ModelUnavailable
from .licences import REGISTRY

HOST = "127.0.0.1"
PORT = 4611
PORT_ENV = "PLATE_PIXELS_PORT"
MAX_BODY = 1024 * 1024
PLATE_UPSCALE_ERROR = "plate upscaling is forbidden (BUILD_PLAN section 1)"

Result = tuple[int, dict[str, Any]]


def _path(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise InputError(f"missing or invalid field {key!r}: expected a file path string")
    return value


def _points(body: dict[str, Any]) -> list[list[float]]:
    points = body.get("points", [])
    ok = isinstance(points, list) and all(
        isinstance(p, list | tuple)
        and len(p) == 2
        and all(isinstance(c, int | float) and not isinstance(c, bool) for c in p)
        for p in points
    )
    if not ok:
        raise InputError("invalid field 'points': expected [[x, y], ...]")
    return [list(p) for p in points]


def _scale(body: dict[str, Any]) -> float:
    scale = body.get("scale", 2)
    if isinstance(scale, bool) or not isinstance(scale, int | float) or scale <= 0:
        raise InputError("invalid field 'scale': expected a positive number")
    return float(scale)


def health(backends: Backends) -> dict[str, Any]:
    by_id = backends.by_model_id()
    models = {}
    for model_id, lic in REGISTRY.items():
        backend = by_id.get(model_id)
        models[model_id] = {
            "available": bool(backend and backend.available),
            "licence": lic.licence,
            "commercial_ok": lic.commercial_ok,
        }
    return {
        "ok": True,
        "stub_mode": not all(b.available for b in backends.all()),
        "torch": "torch" in sys.modules,
        "models": models,
    }


def route(path: str, body: dict[str, Any], backends: Backends) -> Result:
    """Dispatch one request. Returns (HTTP status, JSON-able dict); raises InputError for bad input."""
    if path == "/health":
        return 200, health(backends)
    if path == "/depth":
        return 200, backends.depth.infer(_path(body, "png"))
    if path == "/calibrate":
        return 200, backends.calibration.calibrate(_path(body, "png"))
    if path == "/segment":
        return 200, backends.segment.segment(_path(body, "png"), _points(body))
    if path == "/flow":
        return 200, backends.flow.flow(_path(body, "a"), _path(body, "b"))
    if path == "/textlock":
        return 200, backends.textlock.boxes(_path(body, "png"))
    if path == "/upscale":
        if body.get("is_plate"):
            return 400, {"error": PLATE_UPSCALE_ERROR}
        return 200, backends.upscale.upscale(_path(body, "png"), _scale(body))
    if path == "/lpips":
        return 200, backends.lpips.distance(_path(body, "a"), _path(body, "b"))
    return 404, {"error": f"unknown route {path}"}


class Handler(BaseHTTPRequestHandler):
    backends: Backends  # bound by make_server

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400, {"error": "invalid Content-Length"})
        if length > MAX_BODY:
            return self._send(413, {"error": f"request body over {MAX_BODY} bytes"})
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise InputError("request body must be a JSON object")
            code, result = route(self.path, body, self.backends)
        except (InputError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            code, result = 400, {"error": str(exc)}
        except NotImplementedError as exc:
            code, result = 501, {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            code, result = 500, {"error": str(exc)}
        self._send(code, result)

    def _send(self, code: int, result: dict[str, Any]) -> None:
        payload = json.dumps(result).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:  # keep the console quiet
        pass


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind a port that is already listening.
    allow_reuse_address = os.name != "nt"


def make_server(host: str = HOST, port: int = PORT, backends: Backends | None = None) -> ThreadingHTTPServer:
    """Build (not start) the server. Port 0 picks a free port; read it from ``server_address``."""
    bound = type("BoundHandler", (Handler,), {"backends": Backends() if backends is None else backends})
    return _Server((host, port), bound)


def configured_port() -> int:
    raw = os.environ.get(PORT_ENV)
    if not raw:
        return PORT
    try:
        port = int(raw)
    except ValueError:
        raise SystemExit(f"{PORT_ENV} must be an integer, got {raw!r}") from None
    if not 0 < port < 65536:
        raise SystemExit(f"{PORT_ENV} must be 1-65535, got {port}")
    return port


def main() -> None:
    try:
        server = make_server(HOST, configured_port())
    except ModelUnavailable as exc:
        raise SystemExit(f"pixels service cannot start: {exc}") from None
    stub = health(server.RequestHandlerClass.backends)["stub_mode"]
    print(f"PLATE pixels service on http://{HOST}:{server.server_address[1]} (stub_mode={stub})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
