"""The adapter interface every engine adapter implements (router plan T7; per-vendor adapters are T17 and T18).

New for the PLATE monorepo. An Adapter is built from one manifest and one of its routes plus an httpx transport:
a FixtureTransport in tests, a LiveTransport only under PLATE_LIVE=1. Construction without a transport raises
(nothing leaves by accident); the one exception is a local route, which makes no HTTP calls at all. Every request
goes through an AllowlistTransport built from the route's allowed_hosts. The adapter exposes the vendor's FULL
parameter schema as declared in the manifest (advanced: true only hides a field in the UI) and rejects unknown
fields with ParamError. Prices come from the manifest and stay as unconfirmed as the manifest says.

Result downloads (`fetch`, router plan T130, decision 76) stream: a `Content-Length` over the cap is refused at
once, the body is counted chunk by chunk into a SpooledTemporaryFile (8 MiB in memory, then disk) against
`FETCH_MAX_BYTES` (256 MiB for image kinds, 2 GiB for video kinds, by the manifest kind) and against a wall-clock
`FETCH_DEADLINE_S` (600 s) measured from the request start; over either is a `FetchError`. The body is read on a
helper thread so the deadline holds even while one read blocks (a vendor dripping a byte every 59 s never trips
httpx's per-read timeout); the caller stops waiting at the deadline and the helper closes the response at its
next chunk or read timeout.
"""

from __future__ import annotations

import queue
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import Field

from ..atlas import AtlasClient
from ..models import EngineManifest, EngineSize, Param, Route, _Strict
from ..router import effective_grade, estimate_price
from .transport import AllowlistTransport, HttpxAtlasTransport, make_client

__all__ = [
    "FETCH_DEADLINE_S",
    "FETCH_MAX_BYTES",
    "Adapter",
    "AtlasClient",
    "FetchError",
    "InputError",
    "JobState",
    "JobStatus",
    "MissingTransport",
    "ParamError",
    "PlateUpscaleForbidden",
    "Quote",
    "SilentlyIgnoredInput",
    "Staged",
    "stream_download",
]

JobState = Literal["queued", "running", "done", "failed", "refused"]

MiB = 1024 * 1024
# Result download caps (decision 76): by the manifest kind, video kinds get the larger one.
FETCH_MAX_BYTES = {"image": 256 * MiB, "video": 2048 * MiB}
VIDEO_MANIFEST_KINDS = frozenset({"video_i2v", "video_r2v", "video_startend", "local_video"})
FETCH_DEADLINE_S = 600.0
FETCH_SPOOL_BYTES = 8 * MiB  # held in memory below this; a larger body spills to a temp file
_FETCH_QUEUE_DEPTH = 4  # chunks in flight between the reading thread and the caller
_FETCH_PUT_POLL_S = 0.25


class ParamError(ValueError):
    """A parameter is unknown to the engine's schema, or its value breaks the declared type, enum or range."""


class InputError(ValueError):
    """An input is missing, unknown or over its declared item cap."""


class PlateUpscaleForbidden(InputError):
    """An input tagged role=plate reached an upscaler (doctrine rule 5: the plate is never upscaled)."""


class MissingTransport(TypeError):
    """An adapter for a cloud route was built without a transport."""


class FetchError(RuntimeError):
    """A result download came back with a non-2xx status, or exceeded its byte cap or wall-clock deadline."""


class SilentlyIgnoredInput(RuntimeError):
    """A vendor returned the identical output for two different inputs: it ignored the input (nightly contract test)."""


class Quote(_Strict):
    engine_id: str
    route_id: str
    price_usd: float = Field(ge=0)
    eta_s: float = Field(ge=0)
    custody_grade: Literal[
        "C0", "C1", "C2", "D"
    ]  # the effective grade the router acts on, not the declared one
    size: EngineSize
    model_version: str
    # Honest quotes say when the number is still a placeholder (manifest price.status).
    price_status: Literal["unconfirmed", "confirmed"] = "unconfirmed"


class JobStatus(_Strict):
    state: JobState
    outputs: list[str] = Field(default_factory=list)
    message: str = ""
    # Recorded by the video adapters (Seedance is 24 fps; 1080p is 10-bit) and the billing usage by image adapters.
    output_fps: float | None = None
    output_bits: int | None = None
    usage: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class Staged:
    """What submit() receives: inputs and params after egress.prepare and validate_params, plus the chosen size."""

    inputs: Mapping[str, Any]
    params: Mapping[str, Any]
    size: EngineSize | None = None


def _check_param(p: Param, value: Any) -> Any:
    """Return value coerced to p's type, or raise ParamError."""
    name = p.name
    if p.type == "bool":
        if not isinstance(value, bool):
            raise ParamError(f"{name}: expected true or false, got {value!r}")
        return value
    if p.type == "str":
        if not isinstance(value, str):
            raise ParamError(f"{name}: expected a string, got {value!r}")
        return value
    if p.type == "enum":
        if value not in (p.enum or []):
            raise ParamError(f"{name}: {value!r} is not one of {p.enum}")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ParamError(f"{name}: expected a number, got {value!r}")
    if p.type == "int":
        if isinstance(value, float):
            if value != int(value):
                raise ParamError(f"{name}: expected an integer, got {value!r}")
            value = int(value)
    else:
        value = float(value)
    if p.range is not None and not p.range[0] <= value <= p.range[1]:
        raise ParamError(f"{name}: {value!r} is outside {p.range[0]:g} to {p.range[1]:g}")
    return value


class Adapter(ABC):
    """One engine on one route. Subclasses implement quote, submit and poll; fetch has an allowlisted default."""

    def __init__(
        self,
        manifest: EngineManifest,
        route: Route | str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.manifest = manifest
        self.route = self._pick_route(manifest, route)
        if transport is None and self.route.upload_visibility != "local":
            raise MissingTransport(
                f"{manifest.id} via {self.route.route_id}: pass a transport (FixtureTransport in tests; "
                "LiveTransport only under PLATE_LIVE=1); an adapter never opens its own connection"
            )
        self._transport = transport
        self._client: httpx.Client | None = None

    @staticmethod
    def _pick_route(manifest: EngineManifest, route: Route | str | None) -> Route:
        if isinstance(route, Route):
            if route not in manifest.routes:
                raise ValueError(f"route {route.route_id!r} is not a route of {manifest.id}")
            chosen = route
        elif isinstance(route, str):
            found = [r for r in manifest.routes if r.route_id == route]
            if not found:
                raise ValueError(
                    f"{manifest.id} has no route {route!r}; it has {[r.route_id for r in manifest.routes]}"
                )
            chosen = found[0]
        else:
            active = [r for r in manifest.routes if r.status == "active"]
            if not active:
                raise ValueError(f"{manifest.id} has no active route")
            chosen = active[0]
        if chosen.status != "active":
            raise ValueError(f"route {chosen.route_id!r} of {manifest.id} is retired")
        return chosen

    # ---- identity ---------------------------------------------------------------------------------------

    @property
    def engine_id(self) -> str:
        return self.manifest.id

    @property
    def route_id(self) -> str:
        return self.route.route_id

    @property
    def model_version(self) -> str:
        """The vendor model string for this route, else the manifest's, else its (placeholder) version."""
        return self.route.model_slug or self.manifest.model_id or self.manifest.version

    @property
    def custody_grade(self) -> str:
        return effective_grade(self.route)

    # ---- inputs and params ------------------------------------------------------------------------------

    def validate_params(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Check params against the manifest's full schema; returns a copy with defaults filled in.

        Unknown names raise ParamError (a typo must never be silently dropped); so do wrong types, enum values
        outside the list and numbers outside the range. A None value means "not set" and is dropped.
        """
        schema = {p.name: p for p in self.manifest.params}
        unknown = sorted(set(params) - set(schema))
        if unknown:
            raise ParamError(
                f"{self.engine_id}: unknown parameter(s) {unknown}; the schema has {sorted(schema)}"
            )
        out: dict[str, Any] = {p.name: p.default for p in self.manifest.params if p.default is not None}
        for name, value in params.items():
            if value is None:
                out.pop(name, None)
            else:
                out[name] = _check_param(schema[name], value)
        return out

    def check_inputs(self, inputs: Mapping[str, Any]) -> None:
        """Required inputs present, no unknown inputs, item caps respected, and the plate never reaches an upscaler."""
        declared = {f.name: f for f in self.manifest.inputs}
        unknown = sorted(set(inputs) - set(declared))
        if unknown:
            raise InputError(
                f"{self.engine_id}: unknown input(s) {unknown}; the manifest has {sorted(declared)}"
            )
        missing = [n for n, f in declared.items() if f.required and inputs.get(n) is None]
        if missing:
            raise InputError(f"{self.engine_id}: missing required input(s) {missing}")
        for name, value in inputs.items():
            cap = declared[name].max_items
            if cap is not None and isinstance(value, (list, tuple)) and len(value) > cap:
                raise InputError(f"{self.engine_id}: input {name} has {len(value)} items, the cap is {cap}")
            if (
                self.manifest.plate_upscale_forbidden
                and isinstance(value, Mapping)
                and value.get("role") == "plate"
            ):
                raise PlateUpscaleForbidden(
                    f"{self.engine_id}: input {name} is tagged role=plate; the plate is never upscaled"
                )

    # ---- prices -----------------------------------------------------------------------------------------

    def estimate(
        self,
        *,
        tier: str | None = None,
        seconds: float | None = None,
        megapixels: float | None = None,
        images: int = 1,
    ) -> float:
        """Manifest price: price.by[tier] when the tier is priced, else price.usd, times the billed quantity."""
        p = self.manifest.price
        unit = p.by[tier] if tier is not None and tier in p.by else p.usd
        priced = self.manifest.model_copy(update={"price": p.model_copy(update={"usd": unit})})
        return estimate_price(priced, megapixels=megapixels, seconds=seconds, images=images)

    def make_quote(self, price_usd: float, eta_s: float, size: EngineSize) -> Quote:
        return Quote(
            engine_id=self.engine_id,
            route_id=self.route_id,
            price_usd=price_usd,
            eta_s=eta_s,
            custody_grade=self.custody_grade,
            size=size,
            model_version=self.model_version,
            price_status=self.manifest.price.status,
        )

    # ---- the interface ----------------------------------------------------------------------------------

    @abstractmethod
    def quote(self, inputs: Mapping[str, Any], params: Mapping[str, Any]) -> Quote:
        """Price and size the job from manifest data alone; no request is made."""

    @abstractmethod
    def submit(self, staged: Staged) -> str:
        """Send the job; returns the provider's job id."""

    @abstractmethod
    def poll(self, provider_job_id: str) -> JobStatus:
        """One look at the job (no sleeping, no looping): queued, running, done, failed or refused."""

    def fetch(self, url: str) -> bytes:
        """Download a result. The host must be in the route's allowed_hosts; no credentials are sent. Streamed
        under `fetch_cap()` bytes and `fetch_deadline_s` seconds (`stream_download`)."""
        return self.download(url)

    # Instance or subclass overrides of the download limits (None: by the manifest kind).
    fetch_max_bytes: int | None = None
    fetch_deadline_s: float = FETCH_DEADLINE_S

    def fetch_cap(self) -> int:
        """The byte cap for one result download: `fetch_max_bytes`, else 2 GiB for video kinds, 256 MiB else."""
        if self.fetch_max_bytes is not None:
            return int(self.fetch_max_bytes)
        kind = "video" if self.manifest.kind in VIDEO_MANIFEST_KINDS else "image"
        return FETCH_MAX_BYTES[kind]

    def download(
        self, url: str, *, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None
    ) -> bytes:
        """GET `url` on this adapter's allowlisted client, streamed under its byte cap and deadline."""
        return stream_download(
            self.http(),
            url,
            max_bytes=self.fetch_cap(),
            deadline_s=self.fetch_deadline_s,
            params=params,
            headers=headers,
        )

    # ---- HTTP plumbing ----------------------------------------------------------------------------------

    def guarded_transport(self) -> httpx.BaseTransport:
        if self._transport is None:
            raise MissingTransport(f"{self.engine_id} via {self.route_id}: a local route makes no HTTP calls")
        return AllowlistTransport(self._transport, self.route.allowed_hosts)

    def http(self) -> httpx.Client:
        """The adapter's only HTTP client: allowlisted, never following redirects off-list (httpx does not follow)."""
        if self._client is None:
            self._client = make_client(self.guarded_transport(), timeout=60.0)
        return self._client

    def atlas_client(self, **kwargs: Any) -> AtlasClient:
        """An AtlasClient riding the same guarded transport (kwargs: key_env, sleep, now)."""
        return AtlasClient(HttpxAtlasTransport(self.guarded_transport()), **kwargs)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


# ---- streamed downloads ---------------------------------------------------------------------------------


def stream_download(
    client: httpx.Client,
    url: str,
    *,
    max_bytes: int,
    deadline_s: float = FETCH_DEADLINE_S,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> bytes:
    """GET `url` and return its body, or raise `FetchError` past `max_bytes` (a `Content-Length` over it is
    refused before the body) or `deadline_s` seconds from the request start; a non-2xx status is
    `FetchError("download failed: <status>")` and a transport error propagates as the client raised it.

    The request and the body are read on a daemon thread that hands chunks over a short queue, so the caller
    waits at most the time left on the deadline whatever one read does. The body is spooled (8 MiB in memory,
    then a temp file) so memory never holds more than that plus a few chunks until the download is complete."""
    started = time.monotonic()
    chunks: queue.Queue[tuple[Any, ...]] = queue.Queue(maxsize=_FETCH_QUEUE_DEPTH)
    stop = threading.Event()

    def hand_over(item: tuple[Any, ...]) -> bool:
        while not stop.is_set():
            try:
                chunks.put(item, timeout=_FETCH_PUT_POLL_S)
                return True
            except queue.Full:
                continue
        return False

    def read() -> None:
        try:
            request = client.build_request("GET", url, params=params, headers=headers)
            response = client.send(request, stream=True)
        except BaseException as e:  # handed to the caller, raised there
            hand_over(("error", e))
            return
        try:
            length = _declared_length(response.headers.get("content-length"))
            if not hand_over(("head", response.status_code, length)):
                return
            if response.status_code // 100 != 2 or (length is not None and length > max_bytes):
                return  # refused on the head: not one byte of the body is read
            for chunk in response.iter_bytes():
                if stop.is_set() or not hand_over(("data", chunk)):
                    return
            hand_over(("end",))
        except BaseException as e:
            hand_over(("error", e))
        finally:
            response.close()

    worker = threading.Thread(target=read, name="plate-fetch", daemon=True)
    worker.start()
    spool = tempfile.SpooledTemporaryFile(max_size=FETCH_SPOOL_BYTES)
    total = 0
    try:
        while True:
            left = deadline_s - (time.monotonic() - started)
            try:
                if left <= 0:
                    raise queue.Empty
                item = chunks.get(timeout=left)
            except queue.Empty:
                raise FetchError(f"download exceeded {deadline_s:g} s ({total} bytes in)") from None
            tag = item[0]
            if tag == "error":
                raise item[1]
            if tag == "head":
                status, length = item[1], item[2]
                if status // 100 != 2:
                    raise FetchError(f"download failed: {status}")
                if length is not None and length > max_bytes:
                    raise FetchError(f"download exceeded {max_bytes} bytes (Content-Length {length})")
                continue
            if tag == "end":
                break
            total += len(item[1])
            if total > max_bytes:
                raise FetchError(f"download exceeded {max_bytes} bytes")
            spool.write(item[1])
        spool.seek(0)
        return spool.read()
    finally:
        stop.set()
        spool.close()


def _declared_length(value: str | None) -> int | None:
    """A `Content-Length` header as an int, or None when absent or not a plain number."""
    if value is None or not value.strip().isdigit():
        return None
    return int(value.strip())
