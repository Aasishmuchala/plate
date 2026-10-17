"""Who may talk to the daemon: Host and Origin allowlists for everything, a bearer token for the API.

`AuthMiddleware` is plain ASGI and wraps the whole app, the static UI included, so the checks hold for
every route and for streaming responses:

1. Host must be exactly one of `Settings.host_allowlist` (`127.0.0.1:4610`, `localhost:4610`, the
   Tailscale address), else 403. This is the DNS-rebinding guard: a page on `evil.example` that resolves
   to 127.0.0.1 still sends `Host: evil.example:4610`.
2. An `Origin` header, when present, must be in `Settings.origin_allowlist` (the same hosts over http,
   plus the Vite dev origins under `PLATE_DEV=1`), else 403. Browsers send it on cross-site requests;
   the Max plugin and curl send none.
3. Under `/api/`, every route except `GET /api/v0/health` needs `Authorization: Bearer <token>`,
   compared in constant time, else 401. `GET /api/v0/events` also accepts `?token=` because EventSource
   cannot set headers; the token is removed from the query string before the app (and uvicorn's access
   log, which reads the same scope) sees it.

The UI itself (`/`, assets) needs no token: it arrives in the URL fragment, which browsers never send.
"""

from __future__ import annotations

import json
import secrets
from urllib.parse import parse_qsl, urlencode

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings

API_PREFIX = "/api/"
HEALTH_PATH = "/api/v0/health"
EVENTS_PATH = "/api/v0/events"


class AuthMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.token = settings.token.encode("utf-8")
        self.hosts = frozenset(h.lower() for h in settings.host_allowlist)
        self.origins = frozenset(o.lower() for o in settings.origin_allowlist)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        hosts = headers.getlist("host")
        if len(hosts) != 1 or hosts[0].strip().lower() not in self.hosts:
            await self._deny(scope, receive, send, 403, {"error": "forbidden_host"})
            return
        origins = headers.getlist("origin")
        if len(origins) > 1 or (origins and origins[0].strip().lower() not in self.origins):
            await self._deny(scope, receive, send, 403, {"error": "forbidden_origin"})
            return
        path = scope.get("path", "")
        if path.startswith(API_PREFIX) and path != HEALTH_PATH:
            ok = self._bearer_ok(headers.get("authorization"))
            if not ok and path == EVENTS_PATH and scope.get("method") == "GET":
                ok = self._query_token_ok(scope)
            if not ok:
                await self._deny(scope, receive, send, 401, {"error": "unauthorized"})
                return
        await self.app(scope, receive, send)

    def _check(self, presented: str) -> bool:
        return secrets.compare_digest(presented.strip().encode("utf-8"), self.token)

    def _bearer_ok(self, authorization: str | None) -> bool:
        scheme, _, token = (authorization or "").partition(" ")
        return scheme.lower() == "bearer" and bool(token.strip()) and self._check(token)

    def _query_token_ok(self, scope: Scope) -> bool:
        pairs = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        tokens = [v for k, v in pairs if k == "token"]
        if len(tokens) != 1 or not tokens[0] or not self._check(tokens[0]):
            return False
        # Mutate in place: uvicorn's access log formats this same scope dict after the response starts.
        scope["query_string"] = urlencode([(k, v) for k, v in pairs if k != "token"]).encode("latin-1")
        return True

    @staticmethod
    async def _deny(scope: Scope, receive: Receive, send: Send, status: int, body: dict[str, str]) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        payload = json.dumps(body).encode("utf-8")
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
        if status == 401:
            headers.append((b"www-authenticate", b"Bearer"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": payload})
