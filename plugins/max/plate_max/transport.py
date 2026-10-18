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

Deadlines. urllib's timeout bounds each socket read, not the call, so a daemon that trickles a byte a
second (or an event stream that only ever sends keep-alives) would hold a call forever. Here every
call (http://, which the daemon is) has a total deadline equal to its timeout_s: each read and send
on the connection gets only the time left (the status line and the headers too), the body is read in
64 KiB chunks with the clock checked between them, and DaemonTimeout is raised once timeout_s has
passed. follow_ingest also checks its window between chunks. An answer longer than MAX_RESPONSE_BYTES
(64 MiB) is ResponseTooLarge (an UnexpectedStatus); get_bytes keeps its own max_bytes (a reference
picture may be bigger than that).

POST /api/v0/hash is idempotent, so _post_hash (hash_files, hash_payloads) asks again after 1, 2 and
4 s when the daemon is unreachable, times out or answers 5xx (a restart); a 4xx is never retried.

Pumped calls. hash_files, hash_payloads, post_packet and follow_ingest take an optional pump (the
keyword, else config["_pump"], PUMP_KEY): with one, the call runs on a worker thread while the calling
thread calls pump() every 0.1 s until the answer is in (run_with_pump). The dock puts its
processEvents there for the actions it starts, so Max keeps painting while the daemon hashes a 400 MB
EXR. The key lives only in the in-memory config dict an action is handed: nothing sends, logs or
saves it.

Honest answers. A 200/202 from POST /packets must be a JSON object whose status is "known"/"ingesting"
(and whose packet_id, when there, is text); a 200 from /session must hold packet_versions and
packets_dirs; a 200 from /health must be a JSON object; get_json / post_json take only JSON (an object
or a list, holding the caller's keys when it names them). Anything else (an HTML page from whatever
else answers on the port) is UnexpectedStatus, so a Send never reads it as "Already sent".

A token that cannot go into a header (whitespace, a second line, non-ASCII) is Unauthorized
(MALFORMED_TOKEN) before anything is sent, and one config.load_config refused (None) is Unauthorized
(REFUSED_TOKEN): neither message ever carries the token.
"""

from __future__ import annotations

import base64
import codecs
import http.client
import json
import os
import socket
import threading
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
POST_OK_STATES = {200: "known", 202: "ingesting"}  # the status a 200/202 from POST /packets carries
HASH_PAYLOAD_LIMIT = 4 * 1024 * 1024  # the daemon's cap on in-memory payloads per request
HASH_TIMEOUT_S = 600.0  # a 400 MB EXR on a NAS takes a while to stream through BLAKE3
HASH_RETRY_DELAYS_S = (1.0, 2.0, 4.0)  # /hash is idempotent: asked again after these waits
SESSION_TIMEOUT_S = 3.0
SESSION_KEYS = ("packet_versions", "packets_dirs")
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
MAX_RESPONSE_BYTES = 64 * 1024 * 1024  # no JSON answer or event log of the daemon's is this big
READ_CHUNK = 64 * 1024
PUMP_KEY = "_pump"  # config[PUMP_KEY]: a callable the calling thread runs while a call is in flight
PUMP_INTERVAL_S = 0.1
MALFORMED_TOKEN = (
    "the configured token is malformed (whitespace or non-ASCII); fix config.json or PLATE_TOKEN_FILE"
)
REFUSED_TOKEN = (
    "the configured token was refused when it was read (whitespace, non-ASCII, or not 16-256 "
    "characters); fix config.json or PLATE_TOKEN_FILE"
)
_TOKEN_CHARS = frozenset(chr(c) for c in range(0x21, 0x7F))  # printable ASCII, no space

_sleep = time.sleep  # the /hash retry's waits (tests replace it)


class TransportError(Exception):
    """Base class for transport failures."""


class Unauthorized(TransportError):
    """401: the token in config.json is missing or wrong (or cannot be sent at all)."""


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


class ResponseTooLarge(UnexpectedStatus):
    """An answer longer than its cap (MAX_RESPONSE_BYTES, or get_bytes' max_bytes): the rest is never read."""

    def __init__(self, status_code: int, url: str, limit: int):
        TransportError.__init__(self, f"{url}: the answer is over {limit} bytes (HTTP {status_code})")
        self.status_code = status_code
        self.body = None
        self.limit = limit


class _NotJson(dict):
    """What _decode makes of an answer that is not JSON: {"raw": <its first 300 characters>}, kept apart
    from a JSON object so a 200 with an HTML page is never taken for the daemon's answer."""


def major_minor(version: str) -> str:
    return ".".join(version.split(".")[:2])


def _decode(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return _NotJson(raw=text[:300])


def _is_object(body: Any) -> bool:
    return isinstance(body, dict) and not isinstance(body, _NotJson)


def _has_keys(body: Any, keys: Sequence[str]) -> bool:
    return _is_object(body) and all(key in body for key in keys)


# --------------------------------------------------------------------------- total deadlines


class _DeadlineSocket(socket.socket):
    """A connected socket whose every read and send gets only the time left before plate_deadline (a
    time.monotonic() value), never more than the call's own timeout: a peer that trickles bytes cannot
    hold the call past its deadline, which a socket timeout alone (per read) cannot promise."""

    plate_deadline: float
    plate_timeout: float | None

    @classmethod
    def adopt(cls, sock: socket.socket, deadline: float) -> _DeadlineSocket:
        timeout = sock.gettimeout()
        new = cls(sock.family, sock.type, sock.proto, fileno=sock.detach())
        new.plate_deadline = deadline
        new.plate_timeout = timeout
        new.settimeout(timeout)
        return new

    def _arm(self) -> None:
        left = self.plate_deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("timed out (the call's deadline passed)")
        cap = self.plate_timeout
        super().settimeout(left if cap is None else min(cap, left))

    def recv(self, *args: Any) -> bytes:
        self._arm()
        return super().recv(*args)

    def recv_into(self, *args: Any) -> int:
        self._arm()
        return super().recv_into(*args)

    def send(self, *args: Any) -> int:
        self._arm()
        return super().send(*args)

    def sendall(self, *args: Any) -> None:
        self._arm()
        return super().sendall(*args)


class _DeadlineConnection(http.client.HTTPConnection):
    def __init__(self, *args: Any, deadline: float | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.plate_deadline = deadline

    def connect(self) -> None:
        super().connect()
        if self.plate_deadline is not None:
            self.sock = _DeadlineSocket.adopt(self.sock, self.plate_deadline)


class _DeadlineHandler(urllib.request.HTTPHandler):
    """http:// through _DeadlineConnection, with the deadline _arm_deadline put on the request."""

    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_DeadlineConnection, req, deadline=getattr(req, "plate_deadline", None))


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _DeadlineHandler)


def _arm_deadline(req: urllib.request.Request, timeout_s: float) -> float:
    """The call's total deadline (time.monotonic()), put on req for the connection to honour."""
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    req.plate_deadline = deadline  # type: ignore[attr-defined]
    return deadline


def _read_all(resp: Any, deadline: float, url: str, timeout_s: float, limit: int, status: int) -> bytes:
    """The body, in READ_CHUNK pieces with the clock checked between them; DaemonTimeout once the
    deadline passes, ResponseTooLarge past limit bytes."""
    read = getattr(resp, "read1", None) or resp.read
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise DaemonTimeout(f"{url}: no complete answer within {timeout_s:g} s")
        chunk = read(READ_CHUNK)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise ResponseTooLarge(status, url, limit)
        chunks.append(chunk)


def _open(req: urllib.request.Request, timeout_s: float) -> Any:
    """_opener.open(req): the response, or for a 4xx/5xx the HTTPError (it carries the body). A header
    http.client refuses to build (the bearer token: whitespace, a second line, non-ASCII) raises
    Unauthorized(MALFORMED_TOKEN), outside the handler, so the refused header's text (the token) is not
    attached to it as its context."""
    try:
        return _opener.open(req, timeout=timeout_s)
    except urllib.error.HTTPError as exc:
        return exc
    except ValueError:  # UnicodeEncodeError too
        if not req.has_header("Authorization"):
            raise
    raise Unauthorized(MALFORMED_TOKEN)


def _request(req: urllib.request.Request, timeout_s: float) -> tuple[int, Any]:
    deadline = _arm_deadline(req, timeout_s)
    try:
        resp = _open(req, timeout_s)  # 4xx/5xx still carry a JSON body
        try:
            status = resp.code if isinstance(resp, urllib.error.HTTPError) else resp.status
            raw = _read_all(resp, deadline, req.full_url, timeout_s, MAX_RESPONSE_BYTES, status)
        finally:
            resp.close()
        return status, _decode(raw)
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
    except http.client.HTTPException as exc:  # a status line or a body that is not HTTP
        raise DaemonUnreachable(f"{req.full_url}: not an HTTP answer ({type(exc).__name__})") from exc


def _url(daemon_url: str, path: str) -> str:
    return daemon_url.rstrip("/") + path


def _daemon_url(config: Mapping[str, Any]) -> str:
    return str(config.get("daemon_url") or "http://127.0.0.1:4610")


def _token(config: Mapping[str, Any]) -> str:
    """The bearer token, or Unauthorized: none, one load_config refused (None), or one no header can
    carry. The message never quotes the token."""
    token = config.get("token")
    if token is None and "token" in config:
        raise Unauthorized(REFUSED_TOKEN)
    if not token:
        raise Unauthorized("no token in config.json: run the installer or start the daemon once")
    text = str(token)
    if not set(text) <= _TOKEN_CHARS:
        raise Unauthorized(MALFORMED_TOKEN)
    return text


# --------------------------------------------------------------------------- pumped calls


def run_with_pump(
    fn: Callable[[], Any], pump: Callable[[], None] | None = None, interval_s: float = PUMP_INTERVAL_S
) -> Any:
    """fn() on a worker thread while this thread calls pump() every interval_s until fn returns; then
    fn's result, or fn's exception raised here. Without a pump fn simply runs on this thread. A pump
    that raises is not called again (the call in flight goes on)."""
    if pump is None:
        return fn()
    box: dict[str, Any] = {}
    done = threading.Event()

    def work() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 handed to the calling thread below
            box["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=work, name="plate-transport", daemon=True)
    worker.start()
    interval = max(0.001, float(interval_s))
    calling: Callable[[], None] | None = pump
    while not done.wait(interval):
        if calling is None:
            continue
        try:
            calling()
        except Exception:  # noqa: BLE001 a broken pump must never abandon a call in flight
            calling = None
    worker.join()
    if "error" in box:
        raise box.pop("error")
    return box.get("value")


def _pump_of(config: Mapping[str, Any], pump: Callable[[], None] | None) -> Callable[[], None] | None:
    chosen = pump if pump is not None else config.get(PUMP_KEY)
    return chosen if callable(chosen) else None


# --------------------------------------------------------------------------- health and session


def handshake(daemon_url: str, timeout_s: float = 3.0) -> dict[str, Any]:
    """GET /api/v0/health. problems is empty when this plugin's packet version is one the daemon reads.
    A 200 that is not a JSON object (something else answers on the port) raises UnexpectedStatus."""
    status, body = _request(urllib.request.Request(_url(daemon_url, "/api/v0/health")), timeout_s)
    if status == 200 and not _is_object(body):
        raise UnexpectedStatus(status, body)
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
    touches the network); any other non-200, or a 200 without packet_versions and packets_dirs, raises
    UnexpectedStatus."""
    req = urllib.request.Request(
        _url(_daemon_url(config), "/api/v0/session"),
        headers={"Authorization": f"Bearer {_token(config)}"},
    )
    status, body = _request(req, timeout_s)
    if status == 401:
        raise Unauthorized(f"daemon rejected the token ({body})")
    if status != 200 or not _has_keys(body, SESSION_KEYS):
        raise UnexpectedStatus(status, body)
    return body


# --------------------------------------------------------------------------- hashing


def _post_hash(body: dict[str, Any], config: Mapping[str, Any], timeout_s: float) -> dict[str, Any]:
    """POST /api/v0/hash, asked again after each of HASH_RETRY_DELAYS_S when the daemon is unreachable,
    times out or answers 5xx; the last failure is raised. A 4xx is never asked again."""
    token = _token(config)
    data = json.dumps(body).encode("utf-8")
    waits = list(HASH_RETRY_DELAYS_S)
    while True:
        req = urllib.request.Request(
            _url(_daemon_url(config), "/api/v0/hash"),
            data=data,
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            status, answer = _request(req, timeout_s)
        except DaemonUnreachable:  # DaemonTimeout too
            if not waits:
                raise
            _sleep(waits.pop(0))
            continue
        if 500 <= status <= 599 and waits:
            _sleep(waits.pop(0))
            continue
        break
    if status == 401:
        raise Unauthorized(f"daemon rejected the token ({answer})")
    if status in (403, 404):
        raise HashRefused(status, answer)
    if status != 200 or not _is_object(answer) or not isinstance(answer.get("hashes"), dict):
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
    paths: Sequence[Path | str],
    config: Mapping[str, Any],
    timeout_s: float = HASH_TIMEOUT_S,
    *,
    pump: Callable[[], None] | None = None,
) -> dict[str, dict[str, Any]]:
    """POST /api/v0/hash {"paths": [...]}: {str(path): {"algo": "blake3", "hex", "bytes"}} for every
    path, which must sit inside the daemon's packets folders. 401 raises Unauthorized, 403/404 raise
    HashRefused; a path the answer leaves out is HashRefused too. pump (else config["_pump"]): see
    run_with_pump."""
    sent = [os.path.abspath(str(p)) for p in paths]
    if not sent:
        return {}
    hashes = run_with_pump(lambda: _post_hash({"paths": sent}, config, timeout_s), _pump_of(config, pump))
    return {
        str(original): _pick(hashes, path, str(Path(path).resolve()))
        for original, path in zip(paths, sent, strict=True)
    }


def hash_payloads(
    payloads: Mapping[str, bytes],
    config: Mapping[str, Any],
    timeout_s: float = HASH_TIMEOUT_S,
    *,
    pump: Callable[[], None] | None = None,
) -> dict[str, dict[str, Any]]:
    """POST /api/v0/hash {"payloads": {name: base64}}: the same answer for in-memory bytes (the
    scene and view digests). At most 4 MiB in total, the daemon's cap. pump: as hash_files."""
    if not payloads:
        return {}
    if sum(len(v) for v in payloads.values()) > HASH_PAYLOAD_LIMIT:
        raise ValueError(f"hash payloads are over the {HASH_PAYLOAD_LIMIT // (1024 * 1024)} MiB limit")
    body = {"payloads": {name: base64.b64encode(data).decode("ascii") for name, data in payloads.items()}}
    hashes = run_with_pump(lambda: _post_hash(body, config, timeout_s), _pump_of(config, pump))
    return {name: _pick(hashes, name) for name in payloads}


# --------------------------------------------------------------------------- posting a packet


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


def _check_post_answer(status: int, body: Any) -> None:
    """A 200/202 is the daemon's only when it is a JSON object with the status that code carries
    ("known" / "ingesting") and, when it names one, a packet_id that is text."""
    wanted = POST_OK_STATES.get(status)
    if wanted is None:
        return
    packet_id = body.get("packet_id") if _is_object(body) else None
    if (
        not _is_object(body)
        or body.get("status") != wanted
        or ("packet_id" in body and (not isinstance(packet_id, str) or not packet_id))
    ):
        raise UnexpectedStatus(status, body)


def post_packet(
    packet_path: Path | str,
    config: Mapping[str, Any],
    timeout_s: float | None = None,
    *,
    pump: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """POST /api/v0/packets. Returns {"status_code", "body"} for 200/202/409/422/426 (plus
    "reposted": True when the first POST timed out and the re-post answered 200 or 202). A 200/202
    that is not the daemon's answer raises UnexpectedStatus.

    timeout_s None picks post_timeout_s(packet_path). On a timeout the same folder is posted once
    more with the same timeout; a re-post that answers anything but 200/202 (or nothing) raises
    PostTimedOut. pump (else config["_pump"]): see run_with_pump."""
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

    def send() -> dict[str, Any]:
        try:
            status, body = _request(request(), timeout)
        except DaemonTimeout as first:
            try:
                status, body = _request(request(), timeout)
            except DaemonUnreachable as exc:
                raise PostTimedOut(None, detail=str(exc)) from first
            if status not in REPOST_OK_STATUS:
                raise PostTimedOut(status, body) from first
            _check_post_answer(status, body)
            return {"status_code": status, "body": body, "reposted": True}
        if status == 401:
            raise Unauthorized(f"daemon rejected the token ({body})")
        if status not in PASS_THROUGH_STATUS:
            raise UnexpectedStatus(status, body)
        _check_post_answer(status, body)
        return {"status_code": status, "body": body}

    return run_with_pump(send, _pump_of(config, pump))


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


def _still_ingesting(problem: str | None) -> dict[str, Any]:
    return {"ingest": "ingesting", "problem": problem}


def _read_events(
    req: urllib.request.Request,
    timeout_s: float,
    after: int,
    window_end: float,
    clock: Callable[[], float],
) -> tuple[dict[str, Any] | None, int]:
    """One GET of the event log, read as it streams: (the follow's answer, or None when the stream
    ended without an ingest.done / ingest.failed, and the last seq seen). Frames are parsed chunk by
    chunk, so a stream that never ends still answers once its call deadline or the window passes."""
    deadline = _arm_deadline(req, timeout_s)
    try:
        resp = _open(req, timeout_s)
    except (OSError, http.client.HTTPException, Unauthorized) as exc:
        return _still_ingesting(f"events unreadable: {exc}"), after
    if isinstance(resp, urllib.error.HTTPError):
        resp.close()
        return _still_ingesting(f"events answered HTTP {resp.code}"), after
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    total = 0
    try:
        while True:
            if time.monotonic() > deadline:
                return _still_ingesting(
                    f"events unreadable: no complete answer within {timeout_s:g} s"
                ), after
            try:
                chunk = resp.read1(READ_CHUNK)
            except (OSError, http.client.HTTPException) as exc:
                return _still_ingesting(f"events unreadable: {exc}"), after
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                return _still_ingesting(f"the event log is over {MAX_RESPONSE_BYTES} bytes"), after
            text = (pending + decoder.decode(chunk, final=not chunk)).replace("\r\n", "\n")
            if chunk:
                cut = text.rfind("\n\n")
                complete, pending = (text[: cut + 2], text[cut + 2 :]) if cut >= 0 else ("", text)
            else:
                complete, pending = text, ""
            for event in parse_sse(complete):
                seq = event.get("seq")
                if isinstance(seq, int) and seq > after:
                    after = seq
                if event.get("type") == INGEST_DONE:
                    return {"ingest": "ingested", "problem": None}, after
                if event.get("type") == INGEST_FAILED:
                    return {"ingest": f"failed: {_ingest_errors(event)}", "problem": None}, after
            if not chunk:
                return None, after
            if clock() > window_end:
                return _still_ingesting("the event log was still open when the follow window ended"), after
    finally:
        resp.close()


def follow_ingest(
    events_url: str,
    config: Mapping[str, Any],
    window_s: float = FOLLOW_WINDOW_S,
    interval_s: float = FOLLOW_INTERVAL_S,
    timeout_s: float = FOLLOW_REQUEST_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    *,
    pump: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """GET events_url (bearer) every interval_s for up to window_s, until an ingest.done or
    ingest.failed event. Returns {"ingest": "ingested" | "failed: <errors>" | "ingesting", "problem":
    None or why the events could not be read (the ingest is then still "ingesting")}. Never raises
    for the daemon: the packet was already accepted, so a follow that fails only leaves it unknown.
    Each GET has a total deadline of timeout_s and the window is checked as the answer streams in.
    pump (else config["_pump"]): see run_with_pump."""
    token = _token(config)
    daemon_url = _daemon_url(config)

    def follow() -> dict[str, Any]:
        window_end = clock() + max(0.0, float(window_s))
        after = 0
        while True:
            req = urllib.request.Request(
                _events_request_url(daemon_url, events_url, after),
                headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
            )
            answer, after = _read_events(req, timeout_s, after, window_end, clock)
            if answer is not None:
                return answer
            if clock() + interval_s > window_end:
                return _still_ingesting(None)
            sleep(interval_s)

    return run_with_pump(follow, _pump_of(config, pump))


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


def _json_answer(status: int, body: Any, keys: Sequence[str] | None = None) -> Any:
    if status == 401:
        raise Unauthorized(f"daemon rejected the token ({body})")
    if status not in JSON_OK_STATUS:
        raise UnexpectedStatus(status, body)
    if isinstance(body, _NotJson) or not isinstance(body, (dict, list)):
        raise UnexpectedStatus(status, body)
    if keys and not _has_keys(body, keys):
        raise UnexpectedStatus(status, body)
    return body


def get_json(
    path: str,
    config: Mapping[str, Any],
    timeout_s: float = JSON_TIMEOUT_S,
    params: Mapping[str, Any] | None = None,
    *,
    keys: Sequence[str] | None = None,
) -> Any:
    """GET <daemon><path>[?params] with the bearer: the decoded JSON of a 200. No token or 401
    raises Unauthorized (no token never touches the network), any other status UnexpectedStatus,
    a dead daemon DaemonUnreachable (DaemonTimeout for a silent one). A 200 that is not a JSON object
    or list (or, with keys, an object holding them) is UnexpectedStatus. params: None values are left
    out, True/False are sent as 1/0."""
    req = urllib.request.Request(
        _url(_daemon_url(config), _with_query(api_path(path), params)),
        headers={"Authorization": f"Bearer {_token(config)}", "Accept": "application/json"},
    )
    status, body = _request(req, timeout_s)
    return _json_answer(status, body, keys)


def post_json(
    path: str,
    body: Any,
    config: Mapping[str, Any],
    timeout_s: float = JSON_TIMEOUT_S,
    *,
    keys: Sequence[str] | None = None,
) -> Any:
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
    return _json_answer(status, answer, keys)


def get_bytes(
    path: str,
    config: Mapping[str, Any],
    timeout_s: float = BYTES_TIMEOUT_S,
    params: Mapping[str, Any] | None = None,
    max_bytes: int = BYTES_MAX,
) -> tuple[bytes, str]:
    """GET a binary route (GET /api/v0/assets/{hash}) with the bearer: (bytes, Content-Type) of a
    200. 401 raises Unauthorized, any other status UnexpectedStatus (with the JSON error body), an
    answer longer than max_bytes ResponseTooLarge, a dead daemon DaemonUnreachable, and no complete
    answer within timeout_s (the whole download) DaemonTimeout."""
    req = urllib.request.Request(
        _url(_daemon_url(config), _with_query(api_path(path), params)),
        headers={"Authorization": f"Bearer {_token(config)}"},
    )
    deadline = _arm_deadline(req, timeout_s)
    try:
        resp = _open(req, timeout_s)
        if isinstance(resp, urllib.error.HTTPError):
            try:
                raw = _read_all(resp, deadline, req.full_url, timeout_s, MAX_RESPONSE_BYTES, resp.code)
            finally:
                resp.close()
            body = _decode(raw)
            if resp.code == 401:
                raise Unauthorized(f"daemon rejected the token ({body})")
            raise UnexpectedStatus(resp.code, body)
        try:
            status = resp.status
            content_type = str(resp.headers.get("Content-Type") or "")
            data = _read_all(resp, deadline, req.full_url, timeout_s, int(max_bytes), status)
        finally:
            resp.close()
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise DaemonTimeout(f"{req.full_url}: timed out after {timeout_s:g} s") from exc
        raise DaemonUnreachable(f"{req.full_url}: {exc}") from exc
    except TimeoutError as exc:
        raise DaemonTimeout(f"{req.full_url}: timed out after {timeout_s:g} s") from exc
    except OSError as exc:
        raise DaemonUnreachable(f"{req.full_url}: {exc}") from exc
    except http.client.HTTPException as exc:
        raise DaemonUnreachable(f"{req.full_url}: not an HTTP answer ({type(exc).__name__})") from exc
    if status != 200:
        raise UnexpectedStatus(status, {"raw": data[:300].decode("utf-8", errors="replace")})
    return data, content_type
