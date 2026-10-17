"""Pydantic models for engine manifests (BUILD_PLAN section 4 "Routing", decision 13).

New for the PLATE monorepo. A manifest is data: id, pinned version string, inputs, outputs, params, a price
function, a determinism class, licence facts, allowed tasks and one or more routes. Custody is data too: every
route carries custody_grade and custody_status, and the router treats anything not confirmed as grade D.
Unknown facts are marked unknown (price status, "unknown" terms hash / region / endpoint), never guessed.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .tasks import FORBIDDEN_TASKS, TASKS

CustodyGrade = Literal["C0", "C1", "C2", "D"]

_TERMS_HASH = re.compile(r"^([0-9a-f]{64}|unknown)$")
_ENDPOINT = re.compile(r"^(https://\S+|local|unknown)$")
_TERMS_URL = re.compile(r"^(https?://\S+|unknown)$")


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

    @model_validator(mode="after")
    def _check(self) -> Route:
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


class IOField(_Strict):
    name: str = Field(min_length=1)
    type: Literal["image", "images", "video", "text", "mask", "json"]
    required: bool = True


class Param(_Strict):
    name: str = Field(min_length=1)
    type: Literal["int", "float", "str", "bool", "enum"]
    default: Any = None
    range: list[float] | None = None
    enum: list[str] | None = None

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


class EngineManifest(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    pinned: bool
    kind: Literal["image_edit", "image_gen", "video_i2v", "video_r2v", "upscale"]
    inputs: list[IOField]
    outputs: list[IOField]
    params: list[Param]
    price: PriceSpec
    determinism: Literal["seeded", "best_effort", "nondeterministic"]
    licence: Licence
    allowed_tasks: list[str]
    routes: list[Route] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> EngineManifest:
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
