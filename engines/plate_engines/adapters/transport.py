"""Transports for engine adapters: recorded fixtures by default, live only on purpose, allowlisted always.

New for the PLATE monorepo (router plan T7, decision 8). Every adapter takes an httpx transport. Tests and the
default build use FixtureTransport, which replays recordings from engines/plate_engines/contract/<engine>/<name>.json
and raises UnrecordedRequest for anything it has not seen, so no test and no default run can reach a vendor.
LiveTransport refuses to exist unless PLATE_LIVE=1. AllowlistTransport wraps either one and refuses any host the
route does not list (custody: nothing leaves for a host the manifest never named). HttpxAtlasTransport lets the
Atlas client in atlas.py run over any of them.

Recording file (UTF-8 JSON):
    {"request": {"method": "POST", "url": "https://host/path?x=1",   # query optional: omitted = any query
                 "body": {...}}                                       # JSON body, hashed in canonical form
                 # or "body_hash": "ab12cd34"                         # explicit key for multipart / binary bodies
                 # (neither = the request has no body)
     "response": {"status": 200, "json": {...}}                       # or "bytes_b64": "...", optional "headers"
     # or "responses": [{...}, {...}]                                 # served in order, the last one repeats
    }
A request matches on (METHOD, host, path, blake3(canonical body)[:8]). The canonical body is compact sorted JSON for
JSON bodies, the (field name, file name, blake3 of payload) list for multipart bodies (the random boundary never
matters), and the raw bytes otherwise.
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from blake3 import blake3

from ..atlas import TransportResponse
from ..models import host_allowed

CONTRACT_DIR = Path(__file__).resolve().parent.parent / "contract"


class UnrecordedRequest(RuntimeError):
    """FixtureTransport met a request no recording matches. It never falls through to the network."""


class HostNotAllowed(RuntimeError):
    """A request targets a host the route's allowed_hosts does not cover; nothing was sent."""


class LiveDisabled(RuntimeError):
    """LiveTransport needs PLATE_LIVE=1 (and, upstream, a budget envelope)."""


# ---- canonical request body -> 8 hex chars --------------------------------------------------------------


def _blake8(data: bytes) -> str:
    return blake3(data).hexdigest()[:8]


_BOUNDARY = re.compile(r"boundary=(?:\"([^\"]+)\"|([^;\s]+))", re.IGNORECASE)
_DISPOSITION_NAME = re.compile(r'\bname="([^"]*)"')
_DISPOSITION_FILE = re.compile(r'\bfilename="([^"]*)"')


def _multipart_canonical(content_type: str, body: bytes) -> bytes | None:
    m = _BOUNDARY.search(content_type)
    if m is None:
        return None
    delimiter = b"--" + (m.group(1) or m.group(2)).encode("latin-1")
    pieces: list[bytes] = []
    for part in body.split(delimiter)[1:]:
        if part.startswith(b"--"):  # closing delimiter
            break
        head, sep, payload = part.lstrip(b"\r\n").partition(b"\r\n\r\n")
        if not sep:
            continue
        header_text = head.decode("utf-8", errors="replace")
        name = _DISPOSITION_NAME.search(header_text)
        filename = _DISPOSITION_FILE.search(header_text)
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        label = f"{name.group(1) if name else ''}\x00{filename.group(1) if filename else ''}"
        pieces.append(label.encode("utf-8") + b"\x00" + blake3(payload).hexdigest().encode("ascii"))
    return b"\n".join(sorted(pieces))


def canonical_body(content_type: str, body: bytes) -> bytes:
    """The bytes that get hashed for matching: formatting, key order and multipart boundaries do not matter."""
    if not body:
        return b""
    if "multipart/" in content_type.lower():
        canon = _multipart_canonical(content_type, body)
        if canon is not None:
            return canon
    if "json" in content_type.lower() or body[:1] in (b"{", b"["):
        try:
            return json.dumps(
                json.loads(body.decode("utf-8")), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        except (ValueError, UnicodeDecodeError):
            pass
    return body


def body_key(content_type: str, body: bytes) -> str:
    """blake3(canonical body)[:8] as 8 hex characters."""
    return _blake8(canonical_body(content_type, body))


def json_body_key(obj: Any) -> str:
    """The key a recording gets for a JSON body (what write_recording stores)."""
    return body_key("application/json", json.dumps(obj, ensure_ascii=False).encode("utf-8"))


# ---- recordings -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Recording:
    name: str  # path relative to the fixture directory, for messages
    method: str
    host: str
    path: str
    body_hash: str
    query: dict[str, list[str]] | None
    responses: tuple[dict[str, Any], ...]

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.method, self.host, self.path, self.body_hash)


def _parse_recording(name: str, doc: Any) -> Recording:
    if not isinstance(doc, dict) or not isinstance(doc.get("request"), dict):
        raise ValueError(f"{name}: a recording is an object with a 'request' object")
    req = doc["request"]
    url = req.get("url")
    if not isinstance(url, str):
        raise ValueError(f"{name}: request.url is required")
    parts = urlsplit(url)
    if not parts.hostname:
        raise ValueError(f"{name}: request.url {url!r} has no host")
    if "body" in req and "body_hash" in req:
        raise ValueError(f"{name}: give request.body or request.body_hash, not both")
    if "body_hash" in req:
        body_hash = str(req["body_hash"])
        if not re.fullmatch(r"[0-9a-f]{8}", body_hash):
            raise ValueError(f"{name}: request.body_hash must be 8 lower-case hex characters")
    elif "body" in req:
        body_hash = json_body_key(req["body"])
    else:
        body_hash = _blake8(b"")
    if ("response" in doc) == ("responses" in doc):
        raise ValueError(f"{name}: give exactly one of 'response' or 'responses'")
    responses = tuple(doc["responses"]) if "responses" in doc else (doc["response"],)
    if not responses:
        raise ValueError(f"{name}: 'responses' is empty")
    for r in responses:
        if not isinstance(r, dict) or not isinstance(r.get("status"), int):
            raise ValueError(f"{name}: every response needs an integer status")
        if ("json" in r) and ("bytes_b64" in r):
            raise ValueError(f"{name}: a response has json or bytes_b64, not both")
    return Recording(
        name=name,
        method=str(req.get("method", "GET")).upper(),
        host=parts.hostname.lower(),
        path=parts.path or "/",
        body_hash=body_hash,
        query=parse_qs(parts.query, keep_blank_values=True) if parts.query else None,
        responses=responses,
    )


def load_recordings(directory: Path) -> list[Recording]:
    """Every *.json under directory (recursively), in path order. A duplicate request key is an error."""
    out: list[Recording] = []
    seen: dict[tuple, str] = {}
    for path in sorted(Path(directory).rglob("*.json")):
        name = path.relative_to(directory).as_posix()
        try:
            rec = _parse_recording(name, json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, OSError) as exc:
            raise ValueError(f"unreadable recording {name}: {exc}") from exc
        ident = (*rec.key, json.dumps(rec.query, sort_keys=True))
        if ident in seen:
            raise ValueError(
                f"duplicate recording for {rec.method} {rec.host}{rec.path}: {name} and {seen[ident]}"
            )
        seen[ident] = name
        out.append(rec)
    return out


def write_recording(
    directory: Path,
    name: str,
    *,
    method: str,
    url: str,
    body: Any = None,
    body_hash: str | None = None,
    response: Mapping[str, Any] | None = None,
    responses: Sequence[Mapping[str, Any]] | None = None,
) -> Path:
    """Write directory/<name>.json in the recording format (UTF-8, LF). Used by tests and by recording sessions."""
    if ":" in name or "/" in name or "\\" in name:
        raise ValueError(f"recording name {name!r} must be a plain file stem (no ':' or separators)")
    request: dict[str, Any] = {"method": method.upper(), "url": url}
    if body is not None:
        request["body"] = body
    if body_hash is not None:
        request["body_hash"] = body_hash
    doc: dict[str, Any] = {"request": request}
    if responses is not None:
        doc["responses"] = [dict(r) for r in responses]
    else:
        doc["response"] = dict(response or {"status": 200})
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return path


@dataclass
class ServedCall:
    """One request FixtureTransport answered (tests assert on these)."""

    method: str
    host: str
    path: str
    recording: str
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


class FixtureTransport(httpx.MockTransport):
    """Replays recordings; an unknown request raises UnrecordedRequest instead of touching the network."""

    def __init__(self, directory: Path | str = CONTRACT_DIR) -> None:
        self.directory = Path(directory)
        self.recordings = load_recordings(self.directory)
        self.served: list[ServedCall] = []
        self._index: dict[tuple, list[Recording]] = {}
        for rec in self.recordings:
            self._index.setdefault(rec.key, []).append(rec)
        self._count: dict[str, int] = {}
        super().__init__(self._answer)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        key = (
            request.method.upper(),
            (request.url.host or "").lower(),
            request.url.path or "/",
            body_key(request.headers.get("content-type", ""), body),
        )
        query = parse_qs(request.url.query.decode("ascii"), keep_blank_values=True)
        matches = [r for r in self._index.get(key, []) if r.query is None or r.query == query]
        if not matches:
            raise UnrecordedRequest(self._explain(key, query))
        rec = max(matches, key=lambda r: r.query is not None)  # a query-specific recording beats a catch-all
        n = self._count.get(rec.name, 0)
        self._count[rec.name] = n + 1
        spec = rec.responses[min(n, len(rec.responses) - 1)]
        self.served.append(ServedCall(key[0], key[1], key[2], rec.name, body, dict(request.headers)))
        return self._build(spec)

    def _explain(self, key: tuple, query: dict[str, list[str]]) -> str:
        method, host, path, digest = key
        same_path = [r for r in self.recordings if (r.method, r.host, r.path) == (method, host, path)]
        hint = (
            "recorded bodies for this path: " + ", ".join(f"{r.body_hash} ({r.name})" for r in same_path)
            if same_path
            else "no recording shares this method, host and path"
        )
        qs = f" query={query}" if query else ""
        return (
            f"no recording for {method} {host}{path}{qs} body={digest} under {self.directory} "
            f"({len(self.recordings)} loaded; {hint}). Fixtures never fall through to the network."
        )

    @staticmethod
    def _build(spec: Mapping[str, Any]) -> httpx.Response:
        headers = dict(spec.get("headers") or {})
        status = int(spec["status"])
        if "json" in spec:
            return httpx.Response(status, json=spec["json"], headers=headers)
        if "bytes_b64" in spec:
            return httpx.Response(status, content=base64.b64decode(spec["bytes_b64"]), headers=headers)
        return httpx.Response(status, headers=headers)


class LiveTransport(httpx.HTTPTransport):
    """Real network. Exists only when PLATE_LIVE=1; the budget envelope is checked by the caller (decision 8)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if os.environ.get("PLATE_LIVE") != "1":
            raise LiveDisabled(
                "LiveTransport needs PLATE_LIVE=1 (and a budget envelope); fixtures are the default"
            )
        super().__init__(*args, **kwargs)


def make_client(transport: httpx.BaseTransport, timeout: float = 60.0) -> httpx.Client:
    """The engines-side httpx.Client constructor (the custody grep allows clients only here and in the daemon's
    egress module). Always over a given transport, never following redirects, and with trust_env=False so no
    proxy from the environment can mount a real network transport around the fixture or egress one."""
    return httpx.Client(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False)


class AllowlistTransport(httpx.BaseTransport):
    """Refuses any request whose host is not covered by allowed_hosts (exact names or '.suffix'), then delegates."""

    def __init__(self, inner: httpx.BaseTransport, allowed_hosts: Sequence[str]) -> None:
        self._inner = inner
        self.allowed_hosts = tuple(allowed_hosts)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        if not host_allowed(host, self.allowed_hosts):
            raise HostNotAllowed(
                f"host {host!r} is not in this route's allowed_hosts {list(self.allowed_hosts)}"
            )
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


class HttpxAtlasTransport:
    """atlas.Transport over an httpx transport, so AtlasClient runs on fixtures, the allowlist or the live network."""

    def __init__(self, transport: httpx.BaseTransport, timeout: float = 60.0) -> None:
        self._client = httpx.Client(transport=transport, timeout=timeout)

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        json: dict | None = None,
        files: dict | None = None,
    ) -> TransportResponse:
        resp = self._client.request(method, url, headers=dict(headers or {}), json=json, files=files)
        try:
            data = resp.json()
        except ValueError:
            data = None
        return TransportResponse(
            status=resp.status_code, json=data if isinstance(data, dict) else None, body=resp.content
        )

    def close(self) -> None:
        self._client.close()
