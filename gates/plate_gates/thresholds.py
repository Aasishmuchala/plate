"""Gate thresholds as data (thresholds.yaml), keyed by a threshold-set id that every gate report carries.

Source: BUILD_PLAN.md section 3 decision 14 (~/Desktop/test/vizmaker-dossier/BUILD_PLAN.md), corrected by the
fidelity-harness scout (texture fine band 0.40-0.60, landing 12/16) and the router plan decision 3. Changing a
number means adding a NEW set id; a shipped report keeps pointing at the set it was scored against.

    ts = thresholds.load()                      # the committed set, cached
    status, thr = thresholds.check("texture.lapvar", 412.0)   # ('pass'|'fail'|'advisory', Threshold)

check() returns 'fail' only for a hard threshold that is missed; a missed advisory / calibration_needed
threshold returns 'advisory' (reported, never blocks); anything inside its bound returns 'pass'.
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "DEFAULT_THRESHOLDS_ID",
    "Threshold",
    "ThresholdSet",
    "load",
    "check",
    "long_edge_min_px",
]

DEFAULT_THRESHOLDS_ID = "ts-2026.10.17-draft"

_YAML = Path(__file__).with_name("thresholds.yaml")

Status = Literal["hard", "advisory", "calibration_needed"]


class Threshold(BaseModel):
    """One metric's bound. Exactly one of `value` / `range` / `param` defines it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    op: Literal["le", "ge", "eq", "param"] | None = None
    value: float | None = None
    range: tuple[float, float] | None = None
    param: str | None = None
    tol: float = 0.0
    unit: str | None = None
    resolution_px: int | None = None
    status: Status
    provenance: str
    tier: str | None = None
    variants: dict[str, float] = Field(default_factory=dict)
    status_by_backend: dict[str, Status] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _shape(self) -> Threshold:
        if self.range is not None:
            if self.value is not None or self.param is not None:
                raise ValueError(f"{self.id}: range excludes value/param")
            if self.range[0] > self.range[1]:
                raise ValueError(f"{self.id}: range low > high")
        elif self.op is None:
            raise ValueError(f"{self.id}: needs op (le|ge|eq|param) when there is no range")
        elif self.op != "param" and self.value is None and self.param is None:
            raise ValueError(f"{self.id}: needs value or param")
        return self

    def effective_status(self, backend: str | None = None) -> Status:
        """hard / advisory / calibration_needed after the backend override (unknown backend -> base status)."""
        if self.status_by_backend and backend in self.status_by_backend:
            return self.status_by_backend[backend]
        return self.status

    def bound(self, variant: str | None = None, target: float | None = None) -> float | tuple[float, float]:
        """The number(s) the value is held to: target (param entries) > variant > value > range."""
        if target is not None:
            return float(target)
        if variant is not None:
            if variant not in self.variants:
                raise KeyError(f"{self.id}: no variant {variant!r} (have {sorted(self.variants)})")
            return self.variants[variant]
        if self.range is not None:
            return self.range
        if self.value is None:
            raise ValueError(
                f"{self.id}: bound is supplied at check time (param {self.param!r}): pass target="
            )
        return float(self.value)

    def passes(self, value: float, variant: str | None = None, target: float | None = None) -> bool:
        v = float(value)
        if math.isnan(v):
            return False
        b = self.bound(variant, target)
        if isinstance(b, tuple):
            return b[0] <= v <= b[1]
        if self.op == "le":
            return v <= b
        if self.op == "ge":
            return v >= b
        if self.op == "eq":
            return abs(v - b) <= self.tol
        raise ValueError(f"{self.id} is a parameter, not a checkable metric")


class ThresholdSet(BaseModel):
    """The whole committed set: id plus metrics keyed by metric id."""

    model_config = ConfigDict(extra="forbid")

    id: str
    metrics: dict[str, Threshold]

    def get(self, metric_id: str) -> Threshold:
        try:
            return self.metrics[metric_id]
        except KeyError:
            raise KeyError(f"unknown metric id {metric_id!r}") from None

    def check(
        self,
        metric_id: str,
        value: float,
        *,
        variant: str | None = None,
        backend: str | None = None,
        target: float | None = None,
    ) -> tuple[Literal["pass", "fail", "advisory"], Threshold]:
        """Score one value. `variant` picks a named bound (gbuffer preview), `backend` the flow backend
        (raft hard / dis advisory), `target` supplies a bound the set leaves open (video.duration_s)."""
        thr = self.get(metric_id)
        if thr.op == "param":
            raise ValueError(f"{metric_id} is a parameter, not a checkable metric")
        if thr.passes(value, variant=variant, target=target):
            return "pass", thr
        return ("fail" if thr.effective_status(backend) == "hard" else "advisory"), thr

    def long_edge_min_px(self, width: int) -> int:
        """Minimum connected-edge length at an evaluation width: round(30 * width / 1500)."""
        thr = self.get("long_edge_min_px")
        return max(int(round(float(thr.value) * width / float(thr.resolution_px))), 1)


@lru_cache(maxsize=1)
def load() -> ThresholdSet:
    """Parse and validate thresholds.yaml (UTF-8). Cached for the process; the set is immutable data."""
    raw = yaml.safe_load(_YAML.read_text(encoding="utf-8"))
    entries = raw["metrics"]
    ids = [e["id"] for e in entries]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate metric id in thresholds.yaml")
    return ThresholdSet(id=raw["set_id"], metrics={e["id"]: Threshold(**e) for e in entries})


def check(
    metric_id: str,
    value: float,
    *,
    variant: str | None = None,
    backend: str | None = None,
    target: float | None = None,
) -> tuple[Literal["pass", "fail", "advisory"], Threshold]:
    """Module-level shorthand for load().check(...)."""
    return load().check(metric_id, value, variant=variant, backend=backend, target=target)


def long_edge_min_px(width: int) -> int:
    """Shorthand for load().long_edge_min_px(width)."""
    return load().long_edge_min_px(width)
