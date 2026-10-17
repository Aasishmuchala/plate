"""STAFFAGE over HTTP: the client the People node (T45) talks to, and the custody it enforces on the way.

STAFFAGE (`~/Desktop/staffage`, Next.js) is the person pipeline: it takes a still, has a generator (Wan, Kling
through Atlas Cloud) put a cast member in it, mattes the result and returns `platePath` + `mattePath`. Its routes
and their JSON bodies are the ones in `src/app/api/**` (read 2026-10-17):

    POST /api/films {name}                         -> film
    GET|PUT /api/films/{id}                        -> film (PUT takes the whole film back)
    POST /api/plates {name, path, lensMm, cameraHeightM, lightDirection}   -> plate (201)
    POST /api/media/ingest {path}                  -> {key, meta, proxyPath, thumbPath}
    POST /api/person {filmId, shotId, castId, behavior, placement, light, mode, stillPath, endStillPath, prompt}
                                                   -> {job}
    GET  /api/person/{id}                          -> {job}
    POST /api/person/{id} {action: accept|reroll}  -> {ok} | {job}
    GET  /api/takes?filmId=                        -> {summary, recent}

STAFFAGE hands files over by path on the same machine (`assertUnderHome`), never by upload, so the client only
ever talks to a loopback address and the staged still sits in a folder under the user's home.

Custody (BUILD_PLAN decision 9, open question 6): STAFFAGE's own generator calls leave the box through Atlas
Cloud, whose upload URLs are public, so the People route is grade D. `StaffageSettings.route()` says so, and says
C0 only when STAFFAGE is configured `local_only` (an operator assertion that its generator runs on this box).
Egress's https-only guard cannot carry a loopback request, so this client applies the same three rules itself
before every request when a run is attached: the route's effective grade must be in the run's allowed grades
(`CustodyViolation`, nothing built), the text of the request must not carry the run's slugs, terms or paths
(`LeakBlocked`; absolute paths count by file name only, because the folder is the local hand-off), and the hop
is written to the project's outbound log (STAFFAGE as the vendor, the grade, the BLAKE3 of the body).

Transport: like every adapter (decision 8) the client takes an `httpx.BaseTransport`; with none it needs
`PLATE_LIVE=1` and then talks to the real STAFFAGE over loopback. Tests hand it the in-process fake.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from blake3 import blake3

from plate_engines.adapters.transport import LiveDisabled, make_client

from ..custody import Grade, effective_grade, require_route
from ..egress import EgressRun, LeakBlocked, _find_leak, _scanner, new_opaque_id
from ..fsutil import read_json
from ..outbound_log import OutboundLog
from ..timeutil import iso, utcnow

__all__ = [
    "DEFAULT_BASE",
    "PERSON_MODES",
    "ROUTE_ID",
    "LoopbackTransport",
    "StaffageClient",
    "StaffageError",
    "StaffageSettings",
    "build_person_prompt",
    "default_transport",
    "grade_of",
    "leak_hit",
]

DEFAULT_BASE = "http://127.0.0.1:3000"
ROUTE_ID = "staffage"
PERSON_MODES = ("i2v", "retake", "wan-edit")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_POLL_S = 5.0
DEFAULT_WAIT_S = 25 * 60.0  # STAFFAGE's own ship loop gives a take 25 minutes
PATH_KEYS = frozenset(
    {"path", "sourcePath", "proxyPath", "thumbPath", "mezzaninePath", "stillPath", "endStillPath"}
)
_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_TRUE = {"1", "true", "yes", "on"}


class StaffageError(RuntimeError):
    """STAFFAGE refused, failed, or could not be reached. `status` is the HTTP status when there was one."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


# ---------------------------------------------------------------------------- settings and custody


@dataclass(frozen=True)
class StaffageSettings:
    """Where STAFFAGE is and whether it stays on this box. Sources, strongest first: environment
    (`PLATE_STAFFAGE_URL`, `PLATE_STAFFAGE_LOCAL_ONLY`), then the `staffage` object of `config.json`
    (`{"base_url", "local_only"}`), then the defaults. `config.Settings` is not extended for this: the file and
    the environment are the setting."""

    base_url: str = DEFAULT_BASE
    local_only: bool = False
    timeout_s: float = DEFAULT_TIMEOUT_S

    @classmethod
    def load(cls, settings: Any = None, env: Mapping[str, str] | None = None) -> StaffageSettings:
        env = os.environ if env is None else env
        block: Mapping[str, Any] = {}
        attr = getattr(settings, "staffage", None)
        if isinstance(attr, Mapping):
            block = attr
        else:
            cfg_path = getattr(settings, "config_path", None)
            if cfg_path is not None and Path(cfg_path).is_file():
                try:
                    cfg = read_json(Path(cfg_path))
                except ValueError:
                    cfg = {}
                if isinstance(cfg, Mapping) and isinstance(cfg.get("staffage"), Mapping):
                    block = cfg["staffage"]
        base = env.get("PLATE_STAFFAGE_URL") or block.get("base_url") or DEFAULT_BASE
        if "PLATE_STAFFAGE_LOCAL_ONLY" in env:
            local_only = env["PLATE_STAFFAGE_LOCAL_ONLY"].strip().lower() in _TRUE
        else:
            local_only = block.get("local_only") is True
        return cls(base_url=str(base), local_only=local_only)

    def route(self) -> dict[str, Any]:
        """The custody route of a People run. Grade D unless `local_only` (then C0, confirmed, local)."""
        if self.local_only:
            return {
                "route_id": ROUTE_ID,
                "provider": ROUTE_ID,
                "custody_grade": "C0",
                "custody_status": "confirmed",
                "upload_visibility": "local",
                "allowed_hosts": sorted(LOOPBACK_HOSTS),
            }
        return {
            "route_id": ROUTE_ID,
            "provider": ROUTE_ID,
            "custody_grade": "D",
            "custody_status": "pending_written_terms",
            "upload_visibility": "public",  # STAFFAGE -> Atlas uploadMedia returns public URLs (custody.md E3)
            "allowed_hosts": sorted(LOOPBACK_HOSTS),
        }


# ---------------------------------------------------------------------------- transport


def _loopback_host(host: str) -> bool:
    return host.strip("[]").lower() in LOOPBACK_HOSTS


class LoopbackTransport(httpx.BaseTransport):
    """The real network, for loopback addresses only: STAFFAGE hands files over by path, so it is always on
    this machine. Any other host raises `StaffageError` before a connection is made."""

    def __init__(self, inner: httpx.BaseTransport | None = None) -> None:
        self._inner = inner if inner is not None else httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if not _loopback_host(request.url.host or ""):
            raise StaffageError(f"STAFFAGE is reached over loopback only, not {request.url.host!r}")
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


def default_transport() -> httpx.BaseTransport:
    """Loopback HTTP, but only under PLATE_LIVE=1 (decision 8): a People run spends money inside STAFFAGE."""
    if os.environ.get("PLATE_LIVE") != "1":
        raise LiveDisabled(
            "the STAFFAGE client needs a transport (tests pass the fake) or PLATE_LIVE=1 for the real loopback"
        )
    return LoopbackTransport()


# ---------------------------------------------------------------------------- prompt


def build_person_prompt(
    description: str, behavior: str, placement: str, light: str | None = None, wardrobe: str = ""
) -> str:
    """STAFFAGE's `buildPersonPrompt` (src/lib/personJobs.ts), for the case where PLATE has no STAFFAGE cast
    record to hand it: one outfit for the whole take, feet firmly on the ground, lit to match, everything else
    unchanged. Motion-only wording, no camera instructions (source-view-only)."""
    parts = [
        f"The person wears the exact same outfit for the ENTIRE video, never changing: {wardrobe} - every item "
        "keeps its exact color and form in every single frame."
        if wardrobe
        else "",
        f"Add {description}, {behavior}, {placement}.",
        "Their feet step naturally and firmly ON the ground surface, with realistic ground contact and a soft "
        "shadow beneath each step.",
        f"The person is lit to match the scene: {light}." if light else "",
        "Photorealistic documentary realism: natural skin texture, realistic proportions, natural fabric and "
        "hair movement, no beautification filter, no render-perfect look.",
        "Keep absolutely everything else unchanged: same camera, same environment, same architecture, same "
        "vegetation, same lighting, same materials.",
    ]
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------- the client


def _strings(obj: Any, out: list[str]) -> None:
    """Every string a JSON body carries, with absolute paths reduced to their file name."""
    if isinstance(obj, str):
        if os.path.isabs(obj) or re.match(r"^[A-Za-z]:[\\/]", obj) or obj.startswith("\\\\"):
            obj = re.split(r"[\\/]", obj)[-1]
        out.append(obj)
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            out.append(str(k))
            _strings(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _strings(v, out)


class StaffageClient:
    """The seven routes the People node uses, plus the two film routes that put a shot into a film.

    `run`, `route` and `log` are attached by the caller (`people.run` does it): with a `run` every request is
    custody-checked and leak-scanned first; with a `log` every request leaves a durable outbound-log pair.
    """

    def __init__(
        self,
        base: str = DEFAULT_BASE,
        transport: httpx.BaseTransport | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        run: EgressRun | None = None,
        route: Any = None,
        log: OutboundLog | None = None,
    ) -> None:
        parts = urlsplit(base)
        if parts.scheme not in ("http", "https") or not _loopback_host(parts.hostname or ""):
            raise StaffageError(f"STAFFAGE base {base!r}: it must be a loopback http address (127.0.0.1)")
        self.base = base.rstrip("/")
        self.timeout_s = timeout_s
        self._transport = transport
        self._http: httpx.Client | None = None
        self.run = run
        self.route = route
        self.log = log

    # -- plumbing ----------------------------------------------------------------------------------------

    def attach(
        self, *, run: EgressRun | None = None, route: Any = None, log: OutboundLog | None = None
    ) -> StaffageClient:
        if run is not None:
            self.run = run
        if route is not None:
            self.route = route
        if log is not None:
            self.log = log
        return self

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def _client(self) -> httpx.Client:
        if self._http is None:
            transport = self._transport if self._transport is not None else default_transport()
            self._http = make_client(transport, self.timeout_s)
        return self._http

    def _guard(self, request: httpx.Request) -> tuple[str | None, str]:
        """Custody, leak scan, outbound log (in that order). Returns (log entry id | None, grade)."""
        route = self.route if self.route is not None else StaffageSettings().route()
        if self.run is not None:
            grade = require_route(route, self.run.allowed_grades, what="STAFFAGE").value
        else:
            grade = effective_grade(route).value
        body = request.read()
        if self.run is not None:
            texts: list[str] = [request.url.path, request.url.query.decode("utf-8", "replace")]
            if body:
                try:
                    _strings(json.loads(body.decode("utf-8")), texts)
                except ValueError:
                    texts.append(body.decode("utf-8", "replace"))
            hit = leak_hit(self.run, texts)
            if hit is not None:
                raise LeakBlocked(
                    f"route {ROUTE_ID!r}: the request carries {hit!r}, which never leaves the box"
                )
        if self.log is None:
            return None, grade
        ids = list(dict.fromkeys(_ULID.findall(body.decode("utf-8", "replace"))))
        entry = self.log.request(
            entry_id=new_opaque_id(),
            run_id=self.run.run_id if self.run is not None else None,
            route_id=ROUTE_ID,
            host=request.url.host or "",
            method=request.method,
            path=request.url.path,
            bytes_out=len(body),
            custody_grade=grade,
            opaque_ids=ids,
            body_hash=blake3(body).hexdigest(),
        )
        return entry, grade

    def _request(
        self, method: str, path: str, *, json: Any = None, params: Mapping[str, str] | None = None
    ) -> Any:
        http = self._client()
        request = http.build_request(method, self.base + path, json=json, params=params)
        entry, _grade = self._guard(request)
        try:
            response = http.send(request)
        except Exception as e:
            if entry is not None and self.log is not None:
                self.log.error(entry, error=f"{type(e).__name__}: {e}")
            if isinstance(e, (StaffageError, LiveDisabled)):
                raise
            raise StaffageError(f"STAFFAGE did not answer {method} {path}: {type(e).__name__}: {e}") from e
        if entry is not None and self.log is not None:
            self.log.response(entry, status=response.status_code, bytes_in=len(response.content))
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {"raw": response.text[:500]}
        if response.status_code // 100 != 2:
            message = data.get("error") if isinstance(data, Mapping) else None
            raise StaffageError(
                f"STAFFAGE {method} {path} -> {response.status_code}: {message or response.reason_phrase}",
                status=response.status_code,
                body=data,
            )
        return data

    # -- the routes --------------------------------------------------------------------------------------

    def create_film(self, name: str) -> dict[str, Any]:
        return self._request("POST", "/api/films", json={"name": name})

    def get_film(self, film_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/films/{film_id}")

    def put_film(self, film: Mapping[str, Any]) -> dict[str, Any]:
        """STAFFAGE's PUT takes the whole film back; the body's id must match."""
        return self._request("PUT", f"/api/films/{film['id']}", json=dict(film))

    def register_plate(
        self,
        name: str,
        path: str | Path,
        lensMm: float | None = None,
        cameraHeightM: float | None = None,
        lightDirection: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "path": str(path), "lightDirection": lightDirection}
        if lensMm is not None:
            body["lensMm"] = lensMm
        if cameraHeightM is not None:
            body["cameraHeightM"] = cameraHeightM
        return self._request("POST", "/api/plates", json=body)

    def ingest_media(self, path: str | Path) -> dict[str, Any]:
        return self._request("POST", "/api/media/ingest", json={"path": str(path)})

    def add_shot(
        self, film_id: str, source_path: str | Path, ingest: Mapping[str, Any], name: str | None = None
    ) -> dict[str, Any]:
        """Append an ingested media file to the film as a shot (GET film, add, PUT film). Returns the shot."""
        film = self.get_film(film_id)
        shot = {
            "id": str(uuid.uuid4()),
            "name": name or Path(source_path).stem,
            "sourcePath": str(source_path),
            "proxyPath": ingest.get("proxyPath"),
            "thumbPath": ingest.get("thumbPath"),
            "mezzaninePath": None,
            "depth": None,
            "meta": ingest.get("meta"),
            "layers": [],
            "addedAt": iso(utcnow()),
        }
        film.setdefault("shots", []).append(shot)
        self.put_film(film)
        return shot

    def start_person(
        self,
        filmId: str,
        shotId: str,
        castId: str | None = None,
        behavior: str | None = None,
        placement: str | None = None,
        light: str | None = None,
        mode: str = "i2v",
        stillPath: str | Path | None = None,
        endStillPath: str | Path | None = None,
        *,
        prompt: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Start a person job. The same rules STAFFAGE enforces are checked here first, so a bad call costs
        nothing: a mode it knows, a still for i2v and retake, and either a full prompt or cast + behaviour +
        placement."""
        if mode not in PERSON_MODES:
            raise StaffageError(f"mode {mode!r}: expected one of {list(PERSON_MODES)}")
        if mode in ("i2v", "retake") and not stillPath:
            raise StaffageError(f"{mode} needs a chosen still (stillPath)")
        if not prompt and not (castId and behavior and placement):
            raise StaffageError("either a full prompt, or castId + behavior + placement")
        body: dict[str, Any] = {"filmId": filmId, "shotId": shotId, "mode": mode}
        for key, value in (
            ("castId", castId),
            ("behavior", behavior),
            ("placement", placement),
            ("light", light),
            ("prompt", prompt),
            ("model", model),
            ("stillPath", str(stillPath) if stillPath else None),
            ("endStillPath", str(endStillPath) if endStillPath else None),
        ):
            if value is not None:
                body[key] = value
        return self._request("POST", "/api/person", json=body)["job"]

    def get_person(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/person/{job_id}")["job"]

    def accept(self, job_id: str) -> dict[str, Any]:
        """The review gate's accept: the layer is marked accepted and the decision lands in STAFFAGE's takes."""
        return self._request("POST", f"/api/person/{job_id}", json={"action": "accept"})

    def takes(self, film_id: str | None = None) -> dict[str, Any]:
        return self._request("GET", "/api/takes", params={"filmId": film_id} if film_id else None)

    def wait_for_person(
        self,
        job_id: str,
        *,
        poll_s: float = DEFAULT_POLL_S,
        timeout_s: float = DEFAULT_WAIT_S,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        """Poll the job until it is `done` (returned) or `error` (raises). The job id is polled, never resubmitted:
        a slow take is waited for, and a timeout raises with the id so the caller can resume."""
        deadline = clock() + timeout_s
        while True:
            job = self.get_person(job_id)
            status = job.get("status")
            if status == "done":
                return job
            if status == "error":
                raise StaffageError(
                    f"STAFFAGE job {job_id} failed: {job.get('error') or 'no reason given'}", body=job
                )
            if clock() >= deadline:
                raise StaffageError(
                    f"STAFFAGE job {job_id} still {status!r} after {timeout_s:.0f} s", body=job
                )
            sleep(poll_s)


def leak_hit(run: EgressRun, texts: Iterable[str | None]) -> str | None:
    """The first of the run's slugs, terms or paths that `texts` carry, or None. Egress's scanner."""
    return _find_leak(_scanner(run.slugs, run.terms, run.paths), [t for t in texts if t])


def grade_of(settings: StaffageSettings) -> Grade:
    """The effective custody grade of a People run under these settings."""
    return effective_grade(settings.route())
