"""Talks to the PLATE daemon over loopback HTTP, exactly as spec/PACKET.md section 9 says.

handshake() is GET /api/v0/health (the version check before Send); session() is GET /api/v0/session
with the bearer (the token check, and the packets folders the daemon reads); post_packet() is POST
/api/v0/packets with the bearer token and X-Plate-Packet-Version. compatible() judges a health
answer (packet version and daemon version). hash_files() / hash_payloads() are POST /api/v0/hash:
Max's Python has no BLAKE3, so the loopback daemon hashes for the plugin (BUILD_PLAN decision 5b).
follow_ingest() reads a packet's events_url after a 202 until the ingest ends or a few seconds pass.
get_json() / post_json() / get_bytes() are the bearer calls the dock and its job poller make to the
rest of the API (GET /api/v0/projects, GET and POST /api/v0/plugin/jobs..., GET /api/v0/assets/{hash}).
Standard library urllib only, with proxies switched off: the daemon is 127.0.0.1 and a system proxy
must never see the token. Status handling: 200/202/409/422/426 are returned for the dock to show; 401
raises Unauthorized; a dead daemon raises DaemonUnreachable and a silent one DaemonTimeout (a kind of
DaemonUnreachable).

post_packet's timeout grows with the folder (30 s + 15 s per GiB: the daemon validates the packet
before it answers). A POST that times out is sent once more, for the same folder: the daemon answers
200 "known" when the first one got through, 202 when it did not, and either is a success; anything
else is PostTimedOut.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import PACKET_VERSION, PLUGIN_VERSION, SENDER

PASS_THROUGH_STATUS = (200, 202, 409, 422, 426)
REPOST_OK_STATUS = (200, 202)  # what a re-post after a timeout may answer for the Send to succeed
HASH_PAYLOAD_LIMIT = 4 * 1024 * 1024  # the daemon's cap on in-memory payloads per request
HASH_TIMEOUT_S = 600.0  # a 400 MB EXR on a NAS takes a while to stream through BLAKE3
SESSION_TIMEOUT_S = 3.0
POST_TIMEOUT_BASE_S = 30.0
POST_TIMEOUT_PER_GIB_S = 15.0  # the daemon validates (and reads) the packet before it answers
GIB = 1024**3
FOLLOW_WINDOW_S = 5.0  # how long Send follows events_url after a 202
FOLLOW_INTERVAL_S = 1.0
FOLLOW_REQUEST_TIMEOUT_S = 3.0
INGEST_DONE = "ingest.done"
INGEST_FAILED = "ingest.failed"
API_PREFIX = "/api/v0"
JSON_TIMEOUT_S = 5.0
JSON_OK_STATUS = (200, 201)
BYTES_TIMEOUT_S = 120.0
BYTES_MAX = 512 * 1024 * 1024  # a reference picture; anything larger is not one

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class TransportError(Exception):
    """Base class for transport failures."""


class Unauthorized(TransportError):
    """401: the token in config.json is missing or wrong."""


class HashRefused(TransportError):
    """403 or 404 from /api/v0/hash: a path outside the packets folders, a missing file, or a daemon
    too old to have the route."""

    def __init__(self, status_code: int | None, detail: Any):
        super().__init__(f"daemon refused to hash (HTTP {status_code}): {detail}")
        self.status_code = status_code
        self.detail = detail


class DaemonUnreachable(TransportError):
    """Nothing answered on the daemon URL (not running, wrong port, firewall, timeout)."""


class DaemonTimeout(DaemonUnreachable):
    """The daemon took the request but did not answer within the timeout."""


class PostTimedOut(TransportError):
    """POST /api/v0/packets timed out and the one re-post answered something other than 200/202
    (status_code None: the re-post got no answer either)."""

    def __init__(self, status_code: int | None, body: Any = None, detail: str | None = None):
        shown = status_code if status_code is not None else f"nothing ({detail})"
        tail = f" ({body})" if body else ""
        super().__init__(f"timed out; re-post answered {shown}{tail}")
        self.status_code = status_code
        self.body = body


class UnexpectedStatus(TransportError):
    def __init__(self, status_code: int, body: Any):
        super().__init__(f"daemon answered HTTP {status_code}: {body}")
        self.status_code = status_code
        self.body = body


def major_minor(version: str) -> str:
    return ".".join(version.split(".")[:2])


def _decode(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return {"raw": text[:300]}


def _request(req: urllib.request.Request, timeout_s: float) -> tuple[int, Any]:
    try:
        with _opener.open(req, timeout=timeout_s) as resp:
            return resp.status, _decode(resp.read())
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a JSON body
        try:
            return exc.code, _decode(exc.read())
        finally:
            exc.close()
    except urllib.error.URLError as exc:  # connect (and send) failures arrive wrapped
        if isinstance(exc.reason, TimeoutError):
            # A timeout while connecting means nothing accepted the connection: a listening daemon is
            # accepted by the OS at once, however busy it is. On Windows a closed loopback port is not
            # refused straight away (the stack retries the SYN for about two seconds), so it shows up
            # here as a connect timeout and must read as unreachable, as ECONNREFUSED does on macOS.
            # A timeout waiting for the ANSWER is raised unwrapped below and stays DaemonTimeout.
            raise DaemonUnreachable(f"{req.full_url}: could not connect within {timeout_s:g} s") from exc
        raise DaemonUnreachable(f"{req.full_url}: {exc}") from exc
    except TimeoutError as exc:  # the answer did not come in time (socket.timeout is TimeoutError)
        raise DaemonTimeout(f"{req.full_url}: timed out after {timeout_s:g} s") from exc
    except OSError as exc:  # connection resets and the rest are OSError too
        raise DaemonUnreachable(f"{req.full_url}: {exc}") from exc


def _url(daemon_url: str, path: str) -> str:
    return daemon_url.rstrip("/") + path


def _daemon_url(config: Mapping[str, Any]) -> str:
    return str(config.get("daemon_url") or "http://127.0.0.1:4610")


def _token(config: Mapping[str, Any]) -> str:
    token = config.get("token")
    if not token:
        raise Unauthorized("no token in config.json: run the installer or start the daemon once")
    return str(token)


def handshake(daemon_url: str, timeout_s: float = 3.0) -> dict[str, Any]:
    """GET /api/v0/health. problems is empty when this plugin's packet version is one the daemon reads."""
    status, body = _request(urllib.request.Request(_url(daemon_url, "/api/v0/health")), timeout_s)
    problems: list[str] = []
    versions = body.get("packet_versions") if isinstance(body, dict) else None
    if status != 200 or not isinstance(body, dict):
        problems.append(f"health check answered HTTP {status}")
    elif not body.get("ok"):
        problems.append("daemon reports ok=false")
    if isinstance(versions, list):
        if major_minor(PACKET_VERSION) not in versions:
            problems.append(f"{major_minor(PACKET_VERSION)} not in packet_versions {versions}")
    elif status == 200:
        problems.append("health response has no packet_versions list")
    info = body if isinstance(body, dict) else {}
    return {
        "ok": bool(info.get("ok")) and not problems,
        "daemon_version": info.get("daemon_version"),
        "packet_versions": versions if isinstance(versions, list) else [],
        "problems": problems,
    }


def compatible(health: dict[str, Any]) -> tuple[bool, str]:
    """(ok, reason) for a /api/v0/health answer (or handshake()'s result, which has the same keys).
    The daemon must read this plugin's packet version ("0.1" in packet_versions) and be the same
    major.minor release as the plugin; reason is empty when ok."""
    if not isinstance(health, dict):
        return False, "no health answer from the daemon"
    if "ok" in health and not health["ok"]:
        return False, "daemon reports ok=false"
    versions = health.get("packet_versions")
    if not isinstance(versions, list):
        return False, "health response has no packet_versions list"
    wanted = major_minor(PACKET_VERSION)
    if wanted not in {major_minor(str(v)) for v in versions}:
        return False, f"daemon reads packet versions {versions}, this plugin writes {wanted}"
    daemon_version = health.get("daemon_version")
    if not daemon_version:
        return False, "health response has no daemon_version"
    if major_minor(str(daemon_version)) != major_minor(PLUGIN_VERSION):
        return False, (
            f"daemon {daemon_version} and plugin {PLUGIN_VERSION} are different releases "
            f"({major_minor(str(daemon_version))} vs {major_minor(PLUGIN_VERSION)}): update the older one"
        )
    return True, ""


def session(config: Mapping[str, Any], timeout_s: float = SESSION_TIMEOUT_S) -> dict[str, Any]:
    """GET /api/v0/session with the bearer: the daemon's answer ({ok, daemon_version, packet_versions,
    packets_dirs, projects_dir, dev, cas_copy}). No token or 401 raises Unauthorized (no token never
    touches the network); any other non-200 raises UnexpectedStatus."""
    req = urllib.request.Request(
        _url(_daemon_url(config), "/api/v0/session"),
        headers={"Authorization": f"Bearer {_token(config)}"},
    )
    status, body = _request(req, timeout_s)
    if status == 401:
        raise Unauthorized(f"daemon rejected the token ({body})")
    if status != 200 or not isinstance(body, dict):
        raise UnexpectedStatus(status, body)
    return body


def _post_hash(body: dict[str, Any], config: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    token = _token(config)
    req = urllib.request.Request(
        _url(_daemon_url(config), "/api/v0/hash"),
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    status, answer = _request(req, timeout_s)
    if status == 401:
        raise Unauthorized(f"daemon rejected the token ({answer})")
    if status in (403, 404):
        raise HashRefused(status, answer)
    if status != 200 or not isinstance(answer, dict) or not isinstance(answer.get("hashes"), dict):
        raise UnexpectedStatus(status, answer)
    return answer["hashes"]


def _pick(hashes: dict[str, Any], key: str, alt: str | None = None) -> dict[str, Any]:
    entry = hashes.get(key)
    if entry is None and alt is not None:
        entry = hashes.get(alt)
    if not isinstance(entry, dict) or not entry.get("hex"):
        raise HashRefused(None, f"daemon returned no hash for {key!r}")
    return entry


def hash_files(
    paths: Sequence[Path | str], config: dict[str, Any], timeout_s: float = HASH_TIMEOUT_S
) -> dict[str, dict[str, Any]]:
    """POST /api/v0/hash {"paths": [...]}: {str(path): {"algo": "blake3", "hex", "bytes"}} for every
    path, which must sit inside the daemon's packets folders. 401 raises Unauthorized, 403/404 raise
    HashRefused; a path the answer leaves out is HashRefused too."""
    sent = [os.path.abspath(str(p)) for p in paths]
    if not sent:
        return {}
    hashes = _post_hash({"paths": sent}, config, timeout_s)
    return {
        str(original): _pick(hashes, path, str(Path(path).resolve()))
        for original, path in zip(paths, sent, strict=True)
    }


def hash_payloads(
    payloads: Mapping[str, bytes], config: dict[str, Any], timeout_s: float = HASH_TIMEOUT_S
) -> dict[str, dict[str, Any]]:
    """POST /api/v0/hash {"payloads": {name: base64}}: the same answer for in-memory bytes (the
    scene and view digests). At most 4 MiB in total, the daemon's cap."""
    if not payloads:
        return {}
    if sum(len(v) for v in payloads.values()) > HASH_PAYLOAD_LIMIT:
        raise ValueError(f"hash payloads are over the {HASH_PAYLOAD_LIMIT // (1024 * 1024)} MiB limit")
    body = {"payloads": {name: base64.b64encode(data).decode("ascii") for name, data in payloads.items()}}
    hashes = _post_hash(body, config, timeout_s)
    return {name: _pick(hashes, name) for name in payloads}


def folder_bytes(folder: Path | str) -> int:
    """Total size of the files under folder (0 for a folder that is not there)."""
    total = 0
    for path in Path(folder).rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def post_timeout_s(folder: Path | str) -> float:
    """The POST /api/v0/packets timeout for a packet folder: 30 s + 15 s per GiB of it."""
    return POST_TIMEOUT_BASE_S + POST_TIMEOUT_PER_GIB_S * folder_bytes(folder) / GIB


def post_packet(
    packet_path: Path | str, config: dict[str, Any], timeout_s: float | None = None
) -> dict[str, Any]:
    """POST /api/v0/packets. Returns {"status_code", "body"} for 200/202/409/422/426 (plus
    "reposted": True when the first POST timed out and the re-post answered 200 or 202).

    timeout_s None picks post_timeout_s(packet_path). On a timeout the same folder is posted once
    more with the same timeout; a re-post that answers anything but 200/202 (or nothing) raises
    PostTimedOut."""
    token = _token(config)
    payload = json.dumps({"packet_path": str(Path(packet_path).resolve()), "sender": dict(SENDER)})
    timeout = post_timeout_s(packet_path) if timeout_s is None else float(timeout_s)

    def request() -> urllib.request.Request:
        return urllib.request.Request(
            _url(_daemon_url(config), "/api/v0/packets"),
            data=payload.encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Plate-Packet-Version": PACKET_VERSION,
                "Content-Type": "application/json",
            },
        )

    try:
        status, body = _request(request(), timeout)
    except DaemonTimeout as first:
        try:
            status, body = _request(request(), timeout)
        except DaemonUnreachable as exc:
            raise PostTimedOut(None, detail=str(exc)) from first
        if status not in REPOST_OK_STATUS:
            raise PostTimedOut(status, body) from first
        return {"status_code": status, "body": body, "reposted": True}
    if status == 401:
        raise Unauthorized(f"daemon rejected the token ({body})")
    if status not in PASS_THROUGH_STATUS:
        raise UnexpectedStatus(status, body)
    return {"status_code": status, "body": body}


# --------------------------------------------------------------------------- following an ingest


def events_url_for(packet_id: str) -> str:
    """The daemon's event log route for a packet (what a 202 names as events_url)."""
    return f"/api/v0/packets/{urllib.parse.quote(str(packet_id), safe='')}/events"


def _events_request_url(daemon_url: str, events_url: str, after: int) -> str:
    """events_url (absolute, or relative to the daemon) with wait_s=0, so the daemon answers with the
    events it has instead of holding the stream open, and after=<last seq seen>."""
    full = events_url if "://" in events_url else _url(daemon_url, "/" + events_url.lstrip("/"))
    parts = urllib.parse.urlsplit(full)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k not in ("wait_s", "after")]
    query += [("wait_s", "0"), ("after", str(after))]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def parse_sse(text: str) -> list[dict[str, Any]]:
    """The JSON data of every server-sent event in text (frames split by a blank line; "retry:",
    "id:" and comment lines skipped; a frame whose data is not a JSON object is dropped)."""
    events: list[dict[str, Any]] = []
    for frame in text.replace("\r\n", "\n").split("\n\n"):
        data = [line[5:].lstrip(" ") for line in frame.split("\n") if line.startswith("data:")]
        if not data:
            continue
        try:
            doc = json.loads("\n".join(data))
        except ValueError:
            continue
        if isinstance(doc, dict):
            events.append(doc)
    return events


def _ingest_errors(event: Mapping[str, Any]) -> str:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    errors = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errors, list) and errors:
        return "; ".join(str(e) for e in errors)
    if isinstance(errors, str) and errors:
        return errors
    return "the daemon gave no reason"


def follow_ingest(
    events_url: str,
    config: Mapping[str, Any],
    window_s: float = FOLLOW_WINDOW_S,
    interval_s: float = FOLLOW_INTERVAL_S,
    timeout_s: float = FOLLOW_REQUEST_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """GET events_url (bearer) every interval_s for up to window_s, until an ingest.done or
    ingest.failed event. Returns {"ingest": "ingested" | "failed: <errors>" | "ingesting", "problem":
    None or why the events could not be read (the ingest is then still "ingesting")}. Never raises
    for the daemon: the packet was already accepted, so a follow that fails only leaves it unknown."""
    token = _token(config)
    daemon_url = _daemon_url(config)
    deadline = clock() + max(0.0, float(window_s))
    after = 0
    while True:
        req = urllib.request.Request(
            _events_request_url(daemon_url, events_url, after),
            headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
        )
        try:
            with _opener.open(req, timeout=timeout_s) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            exc.close()
            return {"ingest": "ingesting", "problem": f"events answered HTTP {exc.code}"}
        except OSError as exc:
            return {"ingest": "ingesting", "problem": f"events unreadable: {exc}"}
        for event in parse_sse(text):
            seq = event.get("seq")
            if isinstance(seq, int) and seq > after:
                after = seq
            if event.get("type") == INGEST_DONE:
                return {"ingest": "ingested", "problem": None}
            if event.get("type") == INGEST_FAILED:
                return {"ingest": f"failed: {_ingest_errors(event)}", "problem": None}
        if clock() + interval_s > deadline:
            return {"ingest": "ingesting", "problem": None}
        sleep(interval_s)


# --------------------------------------------------------------------------- the rest of the API


def api_path(path: str) -> str:
    """A daemon route as an absolute path: "/api/v0/projects" stays as it is, "projects" and
    "/projects" get the /api/v0 prefix."""
    text = "/" + str(path).lstrip("/")
    return text if text.startswith("/api/") else API_PREFIX + text


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _with_query(path: str, params: Mapping[str, Any] | None) -> str:
    if not params:
        return path
    pairs = [(str(k), _query_value(v)) for k, v in params.items() if v is not None]
    if not pairs:
        return path
    return path + ("&" if "?" in path else "?") + urllib.parse.urlencode(pairs)


def _json_answer(status: int, body: Any) -> Any:
    if status == 401:
        raise Unauthorized(f"daemon rejected the token ({body})")
    if status not in JSON_OK_STATUS:
        raise UnexpectedStatus(status, body)
    return body


def get_json(
    path: str,
    config: Mapping[str, Any],
    timeout_s: float = JSON_TIMEOUT_S,
    params: Mapping[str, Any] | None = None,
) -> Any:
    """GET <daemon><path>[?params] with the bearer: the decoded JSON of a 200. No token or 401
    raises Unauthorized (no token never touches the network), any other status UnexpectedStatus,
    a dead daemon DaemonUnreachable (DaemonTimeout for a silent one). params: None values are left
    out, True/False are sent as 1/0."""
    req = urllib.request.Request(
        _url(_daemon_url(config), _with_query(api_path(path), params)),
        headers={"Authorization": f"Bearer {_token(config)}", "Accept": "application/json"},
    )
    status, body = _request(req, timeout_s)
    return _json_answer(status, body)


def post_json(path: str, body: Any, config: Mapping[str, Any], timeout_s: float = JSON_TIMEOUT_S) -> Any:
    """POST body as JSON to <daemon><path> with the bearer: the decoded answer of a 200 or 201,
    with the errors get_json raises. A body holding NaN or infinity raises ValueError before
    anything is sent (the daemon would refuse it)."""
    data = json.dumps(body, allow_nan=False).encode("utf-8")
    req = urllib.request.Request(
        _url(_daemon_url(config), api_path(path)),
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {_token(config)}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    status, answer = _request(req, timeout_s)
    return _json_answer(status, answer)


def get_bytes(
    path: str,
    config: Mapping[str, Any],
    timeout_s: float = BYTES_TIMEOUT_S,
    params: Mapping[str, Any] | None = None,
    max_bytes: int = BYTES_MAX,
) -> tuple[bytes, str]:
    """GET a binary route (GET /api/v0/assets/{hash}) with the bearer: (bytes, Content-Type) of a
    200. 401 raises Unauthorized, any other status UnexpectedStatus (with the JSON error body), an
    answer longer than max_bytes TransportError, a dead daemon DaemonUnreachable."""
    req = urllib.request.Request(
        _url(_daemon_url(config), _with_query(api_path(path), params)),
        headers={"Authorization": f"Bearer {_token(config)}"},
    )
    try:
        with _opener.open(req, timeout=timeout_s) as resp:
            status = resp.status
            content_type = str(resp.headers.get("Content-Type") or "")
            data = resp.read(int(max_bytes) + 1)
    except urllib.error.HTTPError as exc:
        try:
            status, body = exc.code, _decode(exc.read())
        finally:
            exc.close()
        if status == 401:
            raise Unauthorized(f"daemon rejected the token ({body})") from None
        raise UnexpectedStatus(status, body) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise DaemonTimeout(f"{req.full_url}: timed out after {timeout_s:g} s") from exc
        raise DaemonUnreachable(f"{req.full_url}: {exc}") from exc
    except TimeoutError as exc:
        raise DaemonTimeout(f"{req.full_url}: timed out after {timeout_s:g} s") from exc
    except OSError as exc:
        raise DaemonUnreachable(f"{req.full_url}: {exc}") from exc
    if status != 200:
        raise UnexpectedStatus(status, {"raw": data[:300].decode("utf-8", errors="replace")})
    if len(data) > max_bytes:
        raise TransportError(f"{req.full_url}: the answer is over {max_bytes} bytes")
    return data, content_type
