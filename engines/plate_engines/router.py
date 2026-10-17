"""The pure routing policy: custody filter, then doctrine, then health / price / latency / region.

New for the PLATE monorepo (BUILD_PLAN section 4 "Routing", decisions 9 and 13; CLAUDE.md doctrine rules 4, 8,
9 and 13). Nothing here does I/O. Custody comes first: a route whose written terms are not confirmed counts as
grade D, and a route that uploads to public URLs (Atlas today) counts as D whatever it declares. A pinned
engine that fails custody is refused with code "custody"; the router never substitutes another engine. The only
fallback is the same model on another provider (Route.same_model_fallback_of), and only when the primary route
of that model is not available. A provider with no health entry is treated as down (no signal, no spend).

T27 adds `choose()`, the full policy the daemon calls: custody filter on a set of allowed grades, then doctrine
(task kind, plate_upscale_forbidden, re-photograph only for close and medium shots), then project pins (a pin that
no manifest serves any more raises RetiredPin), then health (only status "ok"), then engine cards (a cell with
n >= 20 ranks by its Wilson lower bound), then price, latency and region, in that order. It returns a
`RouteChoice`; when that route fails, `next_same_model(choice)` lists only other routes of the same model, each
marked `requote=True`. A different model is never offered: the person chooses it. `route()` stays for the
callers that already use it (adapters import `effective_grade` and `estimate_price` from here).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from .health import route_health
from .models import EngineManifest, Route
from .tasks import FORBIDDEN_TASKS

GRADE_ORDER: list[str] = ["C0", "C1", "C2", "D"]
NDA_MAX_GRADE = "C1"  # NDA projects run on C0 and C1 only (doctrine rule 9)
CARD_MIN_N = 20  # an engine-card cell steers routing only from this many scored runs (BUILD_PLAN section 4)

Pair = tuple[EngineManifest, Route]
RefusalCode = Literal["custody", "doctrine", "health", "pin_missing", "retired_pin"]


class Refusal(Exception):
    """The router declines to route. code is one of custody, doctrine, health, pin_missing, retired_pin."""

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


# ---------------------------------------------------------------------------- T27: choose()

RETAKE_SHOT_CLASSES = frozenset({"close", "medium"})  # a re-photograph is allowed on these shot classes only
REPHOTOGRAPH_TASKS = frozenset({"rephotograph_close"})
# Task.kind may name a manifest kind exactly or one of these families.
KIND_FAMILIES: dict[str, frozenset[str]] = {
    "image": frozenset({"image_edit", "image_gen", "local_image"}),
    "video": frozenset({"video_i2v", "video_r2v", "video_startend", "local_video"}),
    "upscale": frozenset({"upscale"}),
}
ALL_KINDS: frozenset[str] = frozenset().union(*KIND_FAMILIES.values())


class RetiredPin(Refusal):
    """A project pins an engine (and version) that no manifest serves any more. Nothing is substituted."""

    def __init__(self, reason: str) -> None:
        super().__init__("retired_pin", reason)


@dataclass(frozen=True)
class Task:
    """What the caller wants done.

    name is a vocabulary task (tasks.TASKS). kind restricts the engines to one manifest kind, a list of kinds or
    a family ("image", "video", "upscale"); None leaves it to the manifests' allowed_tasks. shot_class matters
    for re-photograph (close or medium only) and for the engine-card cell. target "plate" means the input is the
    client plate itself, which no upscaler may touch (doctrine rule 5).
    """

    name: str
    kind: str | tuple[str, ...] | list[str] | None = None
    shot_class: str | None = None
    target: Literal["layer", "plate"] = "layer"

    def kinds(self) -> frozenset[str] | None:
        if self.kind is None:
            return None
        names = [self.kind] if isinstance(self.kind, str) else list(self.kind)
        out: set[str] = set()
        for n in names:
            if n in KIND_FAMILIES:
                out |= KIND_FAMILIES[n]
            elif n in ALL_KINDS:
                out.add(n)
            else:
                raise ValueError(
                    f"unknown task kind {n!r}; expected a manifest kind or one of {sorted(KIND_FAMILIES)}"
                )
        return frozenset(out)


@dataclass(frozen=True)
class Pin:
    """A project pin: this engine, and this exact version when given."""

    engine_id: str
    version: str | None = None


@dataclass(frozen=True)
class Prefs:
    """Soft preferences. The quantities turn a unit price into a quote-sized price when the caller knows them."""

    region: str | None = None
    min_card_n: int = CARD_MIN_N
    megapixels: float | None = None
    seconds: float | None = None
    images: int = 1


@dataclass(frozen=True)
class RouteChoice:
    engine_id: str
    route_id: str
    reasons: list[str] = field(default_factory=list)
    provider: str = ""
    model_id: str = ""
    version: str = ""
    region: str = ""
    custody_grade: str = ""  # the effective grade
    price_usd: float = 0.0  # unit price per PriceSpec.model
    price_status: str = "unconfirmed"
    fallback_of: str | None = None
    requote: bool = False  # True on a same-model fallback: price and custody must be quoted again
    alternatives: tuple[RouteChoice, ...] = field(default=(), repr=False, compare=False)


def as_task(task: Task | str | Mapping[str, Any]) -> Task:
    if isinstance(task, Task):
        return task
    if isinstance(task, str):
        return Task(name=task)
    return Task(**dict(task))


def as_pin(value: Pin | str | Mapping[str, Any] | tuple[str, str | None]) -> Pin:
    if isinstance(value, Pin):
        return value
    if isinstance(value, str):
        return Pin(engine_id=value)
    if isinstance(value, Mapping):
        return Pin(**dict(value))
    engine_id, version = value
    return Pin(engine_id=engine_id, version=version)


def as_prefs(value: Prefs | Mapping[str, Any] | None) -> Prefs:
    if value is None:
        return Prefs()
    if isinstance(value, Prefs):
        return value
    return Prefs(**dict(value))


def model_key(manifest: EngineManifest) -> str:
    """The vendor model id, else the engine id: what RouteChoice.model_id reports."""
    return manifest.model_id or manifest.id


def same_model_key(manifest: EngineManifest) -> tuple[str, str]:
    """Two routes are the same model when this matches: model id and kind. Seedance 2.5 reference-to-video and
    image-to-video share a vendor model id but take different inputs, so one is not a drop-in for the other."""
    return (model_key(manifest), manifest.kind)


def _card_cells(cards: Any) -> dict[tuple[str, str, str | None], Any]:
    if cards is None:
        return {}
    values = cards.values() if isinstance(cards, Mapping) else cards
    return {(c.engine_id, c.task, c.shot_class): c for c in values}


def _unit_price(manifest: EngineManifest, prefs: Prefs) -> float:
    try:
        return estimate_price(
            manifest, megapixels=prefs.megapixels, seconds=prefs.seconds, images=prefs.images
        )
    except ValueError:  # the unit needs a quantity the caller did not give: rank on the unit price
        return manifest.price.usd


def _doctrine_problem(manifest: EngineManifest, task: Task, kinds: frozenset[str] | None) -> str | None:
    """Why this engine cannot serve the task by doctrine, or None when it can."""
    if task.name not in manifest.allowed_tasks:
        return f"{manifest.id} does not allow task {task.name!r}"
    if kinds is not None and manifest.kind not in kinds:
        return f"{manifest.id} is kind {manifest.kind!r}, the task needs one of {sorted(kinds)}"
    if task.target == "plate" and (manifest.kind == "upscale" or manifest.plate_upscale_forbidden):
        return f"{manifest.id} is plate_upscale_forbidden (the plate is never upscaled)"
    return None


def _custody_reason(
    pairs: list[Pair], allowed: frozenset[str], task: Task, kinds: frozenset[str] | None
) -> str:
    """Name what custody removed, with the reason per route (public upload, pending terms, grade)."""
    bits = []
    relevant = [(m, r) for m, r in pairs if _doctrine_problem(m, task, kinds) is None]
    for m, r in relevant or pairs:
        if effective_grade(r) in allowed:
            continue
        if r.upload_visibility == "public":
            why = f"{r.provider} uploads return public URLs (grade D)"
        elif r.custody_status != "confirmed":
            why = f"{r.provider} terms are pending written confirmation (treated as D)"
        else:
            why = f"{r.provider} is grade {r.custody_grade}"
        bits.append(f"{m.id} via {why}")
    detail = "; ".join(bits[:6]) if bits else "no routes at all"
    return (
        f"no route for task {task.name!r} within custody {sorted(allowed, key=GRADE_ORDER.index)}: {detail}"
    )


def _as_manifest_list(
    manifests: Mapping[str, EngineManifest] | Iterable[EngineManifest],
) -> list[EngineManifest]:
    return list(manifests.values()) if isinstance(manifests, Mapping) else list(manifests)


def _as_choice(
    m: EngineManifest,
    r: Route,
    reasons: list[str],
    *,
    requote: bool = False,
    rest: tuple[RouteChoice, ...] = (),
) -> RouteChoice:
    return RouteChoice(
        engine_id=m.id,
        route_id=r.route_id,
        reasons=reasons,
        provider=r.provider,
        model_id=model_key(m),
        version=m.version,
        region=r.region,
        custody_grade=effective_grade(r),
        price_usd=m.price.usd,
        price_status=m.price.status,
        fallback_of=r.same_model_fallback_of,
        requote=requote,
        alternatives=rest,
    )


def choose(
    task: Task | str | Mapping[str, Any],
    manifests: Mapping[str, EngineManifest] | Iterable[EngineManifest],
    allowed_grades: Iterable[str],
    project_pins: Mapping[str, Any] | None,
    cards: Any,
    health: Mapping[str, Any] | None,
    prefs: Prefs | Mapping[str, Any] | None = None,
) -> RouteChoice:
    """Pick one route or raise Refusal (RetiredPin for a pin nothing serves). Pure: no I/O, no clock.

    allowed_grades is the set the run may use (custody.project_allowed). project_pins maps a task name to a Pin
    (or an engine id, a (engine, version) pair, or a {"engine_id", "version"} mapping). cards is an iterable or a
    mapping of engine cards (anything with engine_id, task, shot_class, n and wilson_lower_95). health maps keys
    to health entries (see health.route_health); None means no signal at all, so every route is down.
    """
    t = as_task(task)
    pf = as_prefs(prefs)
    allowed = frozenset(str(g) for g in allowed_grades)
    for g in allowed:
        if g not in GRADE_ORDER:
            raise ValueError(f"unknown custody grade {g!r}; expected one of {GRADE_ORDER}")
    kinds = t.kinds()
    reasons: list[str] = []

    if t.name in FORBIDDEN_TASKS:  # whatever custody says, a forbidden task is a doctrine refusal
        raise _forbidden(t.name)
    if t.name in REPHOTOGRAPH_TASKS and t.shot_class not in RETAKE_SHOT_CLASSES:
        raise Refusal(
            "doctrine",
            f"task {t.name!r} on shot_class {t.shot_class!r}: re-photograph is allowed on close and medium "
            "shots only (movie-grade is a render, a wide stays a render)",
        )

    pool = _as_manifest_list(manifests)
    pin = as_pin(project_pins[t.name]) if project_pins and t.name in project_pins else None
    if pin is not None:
        served = [m for m in pool if m.id == pin.engine_id and pin.version in (None, m.version)]
        if not served:
            have = sorted({f"{m.id}@{m.version}" for m in pool if m.id == pin.engine_id}) or "no such engine"
            raise RetiredPin(
                f"project pins {t.name!r} to {pin.engine_id}@{pin.version or '*'}, which no manifest serves "
                f"any more ({have}); pick another version or engine by hand"
            )
        pool = served
        reasons.append(f"pin: project pins {t.name!r} to {pin.engine_id}@{pin.version or '*'}")

    every = [(m, r) for m in pool for r in m.routes if r.status == "active"]
    if not every:
        if pin is not None:
            raise RetiredPin(f"pinned engine {pin.engine_id} has no active route left")
        raise Refusal("custody", f"no route for task {t.name!r}: no active routes at all")

    # 1. custody
    inside = [(m, r) for m, r in every if effective_grade(r) in allowed]
    if not inside:
        raise Refusal("custody", _custody_reason(every, allowed, t, kinds))
    reasons.append(
        f"custody: {len(inside)} of {len(every)} routes within {sorted(allowed, key=GRADE_ORDER.index)}"
    )

    # 2. doctrine
    served_pairs = [(m, r) for m, r in inside if _doctrine_problem(m, t, kinds) is None]
    if not served_pairs:
        if any(_doctrine_problem(m, t, kinds) is None for m in pool):
            raise Refusal("custody", _custody_reason(every, allowed, t, kinds))
        naming = [m for m in pool if t.name in m.allowed_tasks]  # these allow the task by name: say why not
        if not naming:
            raise Refusal(
                "doctrine",
                f"pinned engine {pin.engine_id!r} does not allow task {t.name!r}"
                if pin is not None
                else f"no engine allows task {t.name!r}",
            )
        why = sorted({p for m in naming if (p := _doctrine_problem(m, t, kinds))})
        raise Refusal("doctrine", "; ".join(why[:4]))
    reasons.append(f"doctrine: {len({m.id for m, _ in served_pairs})} engine(s) serve {t.name!r}")

    # 3. health (the pin, when there is one, already narrowed the pool above)
    board = health or {}
    healthy: list[tuple[EngineManifest, Route, float]] = []
    sick: list[str] = []
    for m, r in served_pairs:
        status, latency, why = route_health(board, m.id, r.route_id, r.provider)
        if status == "ok":
            healthy.append((m, r, latency))
        else:
            sick.append(f"{m.id} via {r.route_id}: {status} ({why})")
    if not healthy:
        raise Refusal("health", f"no healthy route for task {t.name!r}: " + "; ".join(sick[:6]))
    if sick:
        reasons.append(f"health: {len(sick)} route(s) skipped ({sick[0]})")

    # A same-model fallback route waits while the primary route it falls back from is healthy.
    up = {(m.id, r.provider) for m, r, _ in healthy}
    live = [
        (m, r, lat)
        for m, r, lat in healthy
        if r.same_model_fallback_of is None or (m.id, r.same_model_fallback_of) not in up
    ]

    # 4. cards, 5. price, 6. latency, 7. region
    cells = _card_cells(cards)

    def card_of(m: EngineManifest) -> Any:
        card = cells.get((m.id, t.name, t.shot_class))
        return card if card is not None and card.n >= pf.min_card_n else None

    def key(item: tuple[EngineManifest, Route, float]) -> tuple:
        m, r, latency = item
        card = card_of(m)
        lower = round(card.wilson_lower_95, 4) if card is not None else 0.0
        region_miss = 0 if pf.region is not None and r.region == pf.region else 1
        return (
            -lower,
            m.price.status != "confirmed",
            round(_unit_price(m, pf), 9),
            round(latency, 3),
            region_miss,
            m.id,
            r.route_id,
        )

    ranked = sorted(live, key=key)
    m, r, latency = ranked[0]
    card = card_of(m)
    if card is not None:
        reasons.append(
            f"cards: {m.id} on {t.name}/{t.shot_class} has n={card.n}, wilson_lower_95={card.wilson_lower_95:.4f}"
        )
    else:
        reasons.append(
            f"cards: no cell with n >= {pf.min_card_n} for {t.name}/{t.shot_class}; ranked on price"
        )
    unit = "unconfirmed " if m.price.status != "confirmed" else ""
    reasons.append(f"price: {unit}{_unit_price(m, pf):g} USD ({m.price.model}), latency {latency:g} ms")
    if pf.region is not None:
        reasons.append(
            f"region: {r.region!r} ({'preferred' if r.region == pf.region else 'not the preferred ' + pf.region})"
        )
    if r.same_model_fallback_of is not None:
        reasons.append(
            f"fallback: {r.provider} stands in for {r.same_model_fallback_of}, which is not available (same model)"
        )

    # Same model only, healthy, within custody: what next_same_model() will offer if this route fails.
    chosen_model = same_model_key(m)
    others = [
        (x, y, lat)
        for x, y, lat in sorted(healthy, key=key)
        if same_model_key(x) == chosen_model and not (x.id == m.id and y.route_id == r.route_id)
    ]
    rest: tuple[RouteChoice, ...] = ()
    for x, y, _ in reversed(others):
        rest = (
            _as_choice(
                x,
                y,
                [
                    f"same-model fallback to {y.provider} after {r.provider} failed: "
                    "the price and custody are quoted again before anything is sent"
                ],
                requote=True,
                rest=rest,
            ),
            *rest,
        )
    return _as_choice(m, r, reasons, rest=rest)


def next_same_model(choice: RouteChoice) -> list[RouteChoice]:
    """The routes to try when `choice` failed: the same model on another route only, best first, requote=True.

    Empty when the model has no other healthy route within custody. Never offers a different model: that is a
    decision for a person, with a new quote.
    """
    return list(choice.alternatives)
