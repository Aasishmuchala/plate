"""Pydantic models for engine manifests (BUILD_PLAN section 4 "Routing", decision 13).

New for the PLATE monorepo. A manifest is data: id, pinned version string, inputs, outputs, params, a price
function, a determinism class, licence facts, allowed tasks and one or more routes. Custody is data too: every
route carries custody_grade and custody_status, and the router treats anything not confirmed as grade D.
Unknown facts are marked unknown (price status, "unknown" terms hash / region / endpoint), never guessed.

T7 additions, all optional with defaults so older manifests still load: model_id, routes[].route_id / model_slug /
allowed_hosts / max_inflight / status, price.by / source / as_of, Param.advanced, IOField.max_items, Sizes,
plate_upscale_forbidden and flags. EngineSize is the runtime "this size was chosen" record adapters put on a Quote.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .tasks import FORBIDDEN_TASKS, TASKS

CustodyGrade = Literal["C0", "C1", "C2", "D"]

_TERMS_HASH = re.compile(r"^([0-9a-f]{64}|unknown)$")
_ENDPOINT = re.compile(r"^(https://\S+|local|unknown)$")
_TERMS_URL = re.compile(r"^(https?://\S+|unknown)$")
_ROUTE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
# An allowed host is an exact host name ("api.openai.com") or a suffix that starts with a dot (".aliyuncs.com").
_HOST = re.compile(r"^\.?[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
_FLAG = re.compile(r"^[a-z][a-z0-9_]*$")
_ASPECT = re.compile(r"^[1-9][0-9]*:[1-9][0-9]*$")


def host_allowed(host: str, allowed: list[str] | tuple[str, ...]) -> bool:
    """True when host equals an exact entry or ends with a ".suffix" entry (case-insensitive). Empty list = False."""
    h = host.lower().rstrip(".")
    for entry in allowed:
        if entry.startswith("."):
            if h.endswith(entry):
                return True
        elif h == entry:
            return True
    return False


class _Strict(BaseModel):
    """Frozen, extra-forbidding base: a typo in a YAML key fails loudly instead of being ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Route(_Strict):
    provider: str = Field(min_length=1)
    endpoint: str
    region: str = Field(min_length=1)
    custody_grade: CustodyGrade
    custody_status: Literal["confirmed", "pending_written_terms"]
    terms_hash: str
    # "unknown" is an addition to the pinned literal: some uploads have not been verified either way.
    upload_visibility: Literal["public", "private", "local", "unknown"]
    same_model_fallback_of: str | None = None
    note: str = ""
    # Defaults to provider (filled in by the before-validator below).
    route_id: str = ""
    # Route-specific vendor model string (an Atlas slug, a Vertex model name); None = use EngineManifest.model_id.
    model_slug: str | None = None
    # Hosts this route may talk to: exact names or ".suffix" entries. Empty = fail closed (adapters send nothing).
    allowed_hosts: list[str] = Field(default_factory=list)
    max_inflight: int = Field(default=2, ge=1)
    status: Literal["active", "retired"] = "active"

    @model_validator(mode="before")
    @classmethod
    def _default_route_id(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("route_id") and isinstance(data.get("provider"), str):
            return {**data, "route_id": data["provider"]}
        return data

    @model_validator(mode="after")
    def _check(self) -> Route:
        if not _ROUTE_ID.match(self.route_id):
            raise ValueError(f"route_id {self.route_id!r} must match {_ROUTE_ID.pattern}")
        for host in self.allowed_hosts:
            if not _HOST.match(host):
                raise ValueError(f"allowed_hosts entry {host!r}: use a lower-case host or a '.suffix'")
        if len(set(self.allowed_hosts)) != len(self.allowed_hosts):
            raise ValueError("allowed_hosts has duplicates")
        if self.endpoint == "local" and self.allowed_hosts:
            raise ValueError("a local route lists no allowed_hosts (nothing leaves the box)")
        if self.allowed_hosts and self.endpoint.startswith("https://"):
            host = (urlparse(self.endpoint).hostname or "").lower()
            if not host_allowed(host, self.allowed_hosts):
                raise ValueError(f"endpoint host {host!r} is not covered by allowed_hosts")
        if not _ENDPOINT.match(self.endpoint):
            raise ValueError(f"endpoint {self.endpoint!r} must be an https URL, 'local' or 'unknown'")
        if not _TERMS_HASH.match(self.terms_hash):
            raise ValueError("terms_hash must be a sha256 hex digest or 'unknown'")
        if self.upload_visibility == "public" and self.custody_grade != "D":
            raise ValueError("a route that uploads to public URLs must be custody grade D")
        if (self.custody_grade == "C0") != (self.upload_visibility == "local"):
            raise ValueError("custody C0 and upload_visibility 'local' go together")
        if self.custody_grade in ("C1", "C2") and self.upload_visibility != "private":
            raise ValueError("custody C1 and C2 require upload_visibility 'private'")
        if (
            self.custody_status == "confirmed"
            and self.custody_grade in ("C1", "C2")
            and (self.terms_hash == "unknown")
        ):
            raise ValueError("C1/C2 cannot be 'confirmed' without a terms_hash for the written terms")
        if self.same_model_fallback_of == self.provider:
            raise ValueError("same_model_fallback_of cannot name the route's own provider")
        return self


class PriceSpec(_Strict):
    model: Literal["flat", "per_megapixel", "per_second", "per_image"]
    usd: float = Field(ge=0)
    notes: str = ""
    # "unconfirmed": usd is a placeholder, not a vendor price. Only a catalogue or an invoice confirms it.
    status: Literal["unconfirmed", "confirmed"] = "unconfirmed"
    # Tier or resolution -> usd per billed unit (keys like "1K", "1080P", "atlas_discounted"). usd stays the
    # figure the router ranks on and quotes fall back to; by never replaces it.
    by: dict[str, float] = Field(default_factory=dict)
    source: str = ""
    as_of: date | None = None

    @model_validator(mode="after")
    def _check(self) -> PriceSpec:
        for key, value in self.by.items():
            if not key or value < 0:
                raise ValueError(f"price.by[{key!r}]: key must be non-empty and usd >= 0")
        return self


class IOField(_Strict):
    name: str = Field(min_length=1)
    type: Literal["image", "images", "video", "text", "mask", "json"]
    required: bool = True
    # Upper bound on how many items an images / video / json input may carry (vendor reference caps).
    max_items: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check(self) -> IOField:
        if self.max_items is not None and self.type not in ("images", "video", "json"):
            raise ValueError(f"input {self.name}: max_items only applies to images, video or json inputs")
        return self


class Param(_Strict):
    name: str = Field(min_length=1)
    type: Literal["int", "float", "str", "bool", "enum"]
    default: Any = None
    range: list[float] | None = None
    enum: list[str] | None = None
    # advanced only hides the field in the UI drawer; adapters still accept and validate the full vendor schema.
    advanced: bool = False

    @model_validator(mode="after")
    def _check(self) -> Param:
        if self.type == "enum":
            if not self.enum:
                raise ValueError(f"param {self.name}: enum type needs a non-empty enum list")
            if self.default is not None and self.default not in self.enum:
                raise ValueError(f"param {self.name}: default {self.default!r} not in enum")
        elif self.enum is not None:
            raise ValueError(f"param {self.name}: enum list only valid for type 'enum'")
        if self.range is not None:
            if self.type not in ("int", "float"):
                raise ValueError(f"param {self.name}: range only valid for int/float")
            if len(self.range) != 2 or self.range[0] > self.range[1]:
                raise ValueError(f"param {self.name}: range must be [min, max]")
            if self.default is not None:
                numeric = isinstance(self.default, (int, float)) and not isinstance(self.default, bool)
                if not numeric or not self.range[0] <= self.default <= self.range[1]:
                    raise ValueError(f"param {self.name}: default outside range")
        return self


class Licence(_Strict):
    vendor_terms_url: str
    # null = unknown (or not applicable); true only once written no-training terms are held (decision 9).
    training_opt_out: bool | None = None

    @model_validator(mode="after")
    def _check(self) -> Licence:
        if not _TERMS_URL.match(self.vendor_terms_url):
            raise ValueError("vendor_terms_url must be an http(s) URL or 'unknown'")
        return self


class LegalSize(_Strict):
    """One size the vendor accepts. For tiered engines (Gemini 1K/2K/4K) w x h is the tier's long-edge box."""

    w: int = Field(gt=0)
    h: int = Field(gt=0)
    label: str = ""


class ArbitrarySize(_Strict):
    min_side: int = Field(gt=0)
    max_side: int = Field(gt=0)
    aspect_min: float = Field(gt=0)
    aspect_max: float = Field(gt=0)

    @model_validator(mode="after")
    def _check(self) -> ArbitrarySize:
        if self.min_side > self.max_side or self.aspect_min > self.aspect_max:
            raise ValueError("arbitrary size: min_side <= max_side and aspect_min <= aspect_max required")
        return self


class Sizes(_Strict):
    """What sizes an engine accepts: a legal list, an arbitrary envelope, or "auto" (output follows the input)."""

    legal: list[LegalSize] = Field(default_factory=list)
    arbitrary: ArbitrarySize | None = None
    auto: bool = False
    aspects: list[str] = Field(default_factory=list)  # named aspect ratios, "16:9"

    @model_validator(mode="after")
    def _check(self) -> Sizes:
        if not (self.legal or self.arbitrary or self.auto):
            raise ValueError("sizes needs at least one of legal, arbitrary or auto")
        keys = [(x.w, x.h, x.label) for x in self.legal]
        if len(set(keys)) != len(keys):
            raise ValueError("sizes.legal has duplicates")
        for a in self.aspects:
            if not _ASPECT.match(a):
                raise ValueError(f"sizes.aspects entry {a!r} must look like '16:9'")
        return self


class EngineSize(_Strict):
    """The size an adapter will request or the vendor will return; auto is w = h = 0 with label 'auto'."""

    w: int = Field(ge=0)
    h: int = Field(ge=0)
    label: str = ""


class EngineManifest(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    pinned: bool
    kind: Literal[
        "image_edit",
        "image_gen",
        "video_i2v",
        "video_r2v",
        "video_startend",
        "upscale",
        "local_image",
        "local_video",
    ]
    inputs: list[IOField]
    outputs: list[IOField]
    params: list[Param]
    price: PriceSpec
    determinism: Literal["seeded", "best_effort", "nondeterministic"]
    licence: Licence
    allowed_tasks: list[str]
    routes: list[Route] = Field(min_length=1)
    # Vendor model string (None = not established yet, which a flag should say).
    model_id: str | None = None
    sizes: Sizes | None = None
    # Doctrine rule 5 in data: an upscaler never touches the plate. Required on every `upscale` engine.
    plate_upscale_forbidden: bool = False
    # Free-form markers such as "unverified_p0" (a fact here has not survived the P0 smoke test).
    flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> EngineManifest:
        if self.kind == "upscale" and not self.plate_upscale_forbidden:
            raise ValueError(
                "upscale engines must set plate_upscale_forbidden: true (the plate is never upscaled)"
            )
        for flag in self.flags:
            if not _FLAG.match(flag):
                raise ValueError(f"flag {flag!r} must be lower_snake_case")
        if len(set(self.flags)) != len(self.flags):
            raise ValueError("duplicate flags")
        for task in self.allowed_tasks:
            if task in FORBIDDEN_TASKS:
                raise ValueError(f"allowed_tasks lists forbidden task {task!r} (doctrine)")
            if task not in TASKS:
                raise ValueError(f"allowed_tasks lists {task!r}, not in the task vocabulary {sorted(TASKS)}")
        for label, names in (
            ("inputs", [f.name for f in self.inputs]),
            ("outputs", [f.name for f in self.outputs]),
            ("params", [p.name for p in self.params]),
            ("route providers", [r.provider for r in self.routes]),
            ("route ids", [r.route_id for r in self.routes]),
        ):
            if len(set(names)) != len(names):
                raise ValueError(f"duplicate names in {label}: {names}")
        providers = {r.provider for r in self.routes}
        for r in self.routes:
            if r.same_model_fallback_of is not None and r.same_model_fallback_of not in providers:
                raise ValueError(
                    f"route {r.provider}: same_model_fallback_of {r.same_model_fallback_of!r} "
                    f"names no route of this manifest"
                )
        return self
