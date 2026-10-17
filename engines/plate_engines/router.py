"""The pure routing policy: custody filter, then doctrine, then health / price / latency / region.

New for the PLATE monorepo (BUILD_PLAN section 4 "Routing", decisions 9 and 13; CLAUDE.md doctrine rules 4, 8,
9 and 13). Nothing here does I/O. Custody comes first: a route whose written terms are not confirmed counts as
grade D, and a route that uploads to public URLs (Atlas today) counts as D whatever it declares. A pinned
engine that fails custody is refused with code "custody"; the router never substitutes another engine. The only
fallback is the same model on another provider (Route.same_model_fallback_of), and only when the primary route
of that model is not available. A provider with no health entry is treated as down (no signal, no spend).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .models import EngineManifest, Route
from .tasks import FORBIDDEN_TASKS

GRADE_ORDER: list[str] = ["C0", "C1", "C2", "D"]
NDA_MAX_GRADE = "C1"  # NDA projects run on C0 and C1 only (doctrine rule 9)

Pair = tuple[EngineManifest, Route]
RefusalCode = Literal["custody", "doctrine", "health", "pin_missing"]


class Refusal(Exception):
    """The router declines to route. code is one of custody, doctrine, health, pin_missing."""

    def __init__(self, code: RefusalCode, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class Health:
    up: bool
    latency_ms: float = 0.0


@dataclass(frozen=True)
class RouteDecision:
    engine_id: str
    version: str
    provider: str
    region: str
    custody_grade: str  # the effective grade, not the declared one
    price_usd: float  # unit price per PriceSpec.model; use estimate_price for a quote
    price_status: str = "unconfirmed"
    custody_status: str = "pending_written_terms"
    fallback_of: str | None = None


def effective_grade(route: Route) -> str:
    """The grade the router acts on: declared if confirmed, else D. Public uploads are D regardless."""
    if route.upload_visibility == "public":
        return "D"
    return route.custody_grade if route.custody_status == "confirmed" else "D"


def _grade_index(grade: str) -> int:
    if grade not in GRADE_ORDER:
        raise ValueError(f"unknown custody grade {grade!r}; expected one of {GRADE_ORDER}")
    return GRADE_ORDER.index(grade)


def custody_filter(manifests: Mapping[str, EngineManifest], max_grade: str) -> list[Pair]:
    """Keep every (manifest, route) whose effective grade is at or below max_grade."""
    limit = _grade_index(max_grade)
    return [(m, r) for m in manifests.values() for r in m.routes if _grade_index(effective_grade(r)) <= limit]


def _forbidden(task: str) -> Refusal:
    return Refusal("doctrine", f"task {task!r} is forbidden by doctrine (geometry first, plate untouched)")


def doctrine_filter(pairs: list[Pair], task: str) -> list[Pair]:
    """Refuse forbidden tasks; otherwise keep the pairs whose manifest allows the task."""
    if task in FORBIDDEN_TASKS:
        raise _forbidden(task)
    return [(m, r) for m, r in pairs if task in m.allowed_tasks]


def rank(
    pairs: list[Pair],
    health: Mapping[str, Health],
    prefer_region: str | None = None,
    price_weight: float = 1.0,
    latency_weight: float = 0.001,
) -> list[Pair]:
    """Drop down providers, then sort by price*price_weight + latency_ms*latency_weight.

    Engines whose price is still unconfirmed sort after those with a confirmed price. A region match breaks
    ties. A same-model fallback route is dropped while the primary route it falls back from is available.
    """
    alive = [(m, r) for m, r in pairs if (h := health.get(r.provider)) is not None and h.up]
    available = {(m.id, r.provider) for m, r in alive}
    kept = [
        (m, r)
        for m, r in alive
        if r.same_model_fallback_of is None or (m.id, r.same_model_fallback_of) not in available
    ]

    def key(pair: Pair) -> tuple:
        m, r = pair
        score = m.price.usd * price_weight + health[r.provider].latency_ms * latency_weight
        region_miss = 0 if prefer_region is not None and r.region == prefer_region else 1
        return (m.price.status != "confirmed", round(score, 9), region_miss, m.id, r.provider)

    return sorted(kept, key=key)


def estimate_price(
    manifest: EngineManifest,
    megapixels: float | None = None,
    seconds: float | None = None,
    images: int = 1,
) -> float:
    """Unit price times the billed quantity times the number of outputs. Placeholder prices stay placeholders."""
    p = manifest.price
    if p.model in ("flat", "per_image"):
        qty = 1.0
    elif p.model == "per_megapixel":
        if megapixels is None:
            raise ValueError(f"{manifest.id}: per_megapixel pricing needs megapixels")
        qty = megapixels
    else:
        if seconds is None:
            raise ValueError(f"{manifest.id}: per_second pricing needs seconds")
        qty = seconds
    return p.usd * qty * images


def _custody_message(
    engine_pin: str | None, task: str, max_grade: str, manifests: Mapping[str, EngineManifest]
) -> str:
    """Name what blocked the route, including Atlas-style public uploads."""
    kept = {(m.id, r.provider) for m, r in custody_filter(manifests, max_grade)}
    blocked = [(m, r) for m in manifests.values() for r in m.routes if (m.id, r.provider) not in kept]
    relevant = [(m, r) for m, r in blocked if task in m.allowed_tasks]
    bits = []
    for m, r in relevant or blocked:
        if r.upload_visibility == "public":
            why = f"{r.provider} uploads return public URLs (grade D)"
        elif r.custody_status != "confirmed":
            why = f"{r.provider} terms are pending written confirmation (treated as D)"
        else:
            why = f"{r.provider} is grade {r.custody_grade}"
        bits.append(f"{m.id} via {why}")
    subject = f"engine {engine_pin}" if engine_pin else f"task {task!r}"
    detail = "; ".join(bits[:6]) if bits else "no routes at all"
    return f"no route for {subject} within custody {max_grade}: {detail}"


def route(
    task: str,
    project_max_grade: str,
    manifests: Mapping[str, EngineManifest],
    health: Mapping[str, Health],
    prefer_region: str | None = None,
    engine_pin: str | None = None,
) -> RouteDecision:
    """Pick one route or raise Refusal. Order: custody, doctrine, then health / price / latency / region."""
    pool: Mapping[str, EngineManifest] = manifests
    if engine_pin is not None:
        if engine_pin not in manifests:
            raise Refusal("pin_missing", f"pinned engine {engine_pin!r} is not in the manifests")
        pool = {engine_pin: manifests[engine_pin]}

    if task in FORBIDDEN_TASKS:  # a forbidden task is refused as doctrine whatever the custody picture is
        raise _forbidden(task)

    pairs = custody_filter(pool, project_max_grade)
    if not pairs:
        raise Refusal("custody", _custody_message(engine_pin, task, project_max_grade, pool))

    allowed = doctrine_filter(pairs, task)
    if not allowed:
        if any(task in m.allowed_tasks for m in pool.values()):
            # Some engine allows the task but custody removed it: that is a custody refusal, not doctrine.
            raise Refusal("custody", _custody_message(engine_pin, task, project_max_grade, pool))
        reason = (
            f"pinned engine {engine_pin!r} does not allow task {task!r}"
            if engine_pin
            else f"no engine allows task {task!r}"
        )
        raise Refusal("doctrine", reason)

    ranked = rank(allowed, health, prefer_region)
    if not ranked:
        providers = sorted({r.provider for _, r in allowed})
        raise Refusal("health", f"no healthy provider among {providers} for task {task!r}")

    m, r = ranked[0]
    return RouteDecision(
        engine_id=m.id,
        version=m.version,
        provider=r.provider,
        region=r.region,
        custody_grade=effective_grade(r),
        price_usd=m.price.usd,
        price_status=m.price.status,
        custody_status=r.custody_status,
        fallback_of=r.same_model_fallback_of,
    )
