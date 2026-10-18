"""Which route of a video engine goes first, and which durations a plan may use (Wave 11, decisions 61 and 63).

New for the PLATE monorepo. Pure: no I/O, no clock, no schema tables. Runway is the preferred video provider, but
preference is a rule, not list order: the manifests keep the Atlas route first, and `prefer_route` moves the
preferred provider's routes to the front for video engines when the frame fits. A provider that would have to
centre-crop an off-ratio frame is skipped, because the crop would move a crop pair's landing; the reason says why.
The ratio check is injected as `ratio_for` (T117's `runway_schema.ratio_for`), so this module imports no tables.

`legal_durations` is the set of whole seconds EVERY active route of an engine accepts, so a motion plan is legal on
whichever route the quote picks and never under-prices. `clamp_to_legal` ceils a shot length onto that set.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from .models import EngineManifest, Route
from .router import KIND_FAMILIES

__all__ = [
    "PREFERRED_VIDEO_PROVIDER",
    "VIDEO_KINDS",
    "clamp_to_legal",
    "legal_durations",
    "prefer_route",
]

PREFERRED_VIDEO_PROVIDER = "runway"
VIDEO_KINDS = KIND_FAMILIES["video"]

# (vendor model string, width, height, tier label) -> the legal ratio name, or None when none is within tolerance.
RatioFor = Callable[[str, int, int, str | None], str | None]


def _range_durations(manifest: EngineManifest) -> list[int]:
    """The whole seconds in the manifest's int `duration` param range; [] when there is no such param."""
    for p in manifest.params:
        if p.name == "duration" and p.type == "int" and p.range is not None:
            lo, hi = p.range
            return list(range(math.ceil(lo), math.floor(hi) + 1))
    return []


def legal_durations(manifest: EngineManifest) -> list[int]:
    """The sorted whole seconds every constrained active route accepts.

    A route is constrained by its own `durations` list, else by the manifest's `duration` param range. A manifest
    with no constrained route returns [] (no constraint); callers treat [] as "any duration in the param range".
    """
    sets: list[set[int]] = []
    for route in manifest.routes:
        if route.status != "active":
            continue
        allowed = route.durations if route.durations is not None else _range_durations(manifest)
        if allowed:
            sets.append(set(allowed))
    if not sets:
        return []
    return sorted(set.intersection(*sets))


def clamp_to_legal(seconds: float, legal: list[int], lo: int, hi: int) -> int:
    """Ceil `seconds` into [lo, hi], then to the smallest legal value at or above it (else the largest legal one).

    An empty `legal` list means no constraint: the clamped ceil is returned. The take outlasts the shot, never the
    other way round.
    """
    want = min(max(math.ceil(seconds), lo), hi)
    if not legal:
        return want
    ordered = sorted(legal)
    return next((d for d in ordered if d >= want), ordered[-1])


def prefer_route(
    manifest: EngineManifest,
    candidates: list[Route],
    *,
    provider: str | None,
    frame: tuple[int, int] | None = None,
    tier: str | None = None,
    ratio_for: RatioFor | None = None,
) -> tuple[list[Route], list[str]]:
    """Order `candidates` (given in manifest order) with `provider`'s routes first, for video engines only.

    Returns (routes, reasons). With no provider (None or "none") or a non-video engine the order is unchanged. A
    route of `provider` goes to the front when there is no frame to check, no checker, or `ratio_for` finds a
    legal ratio for the frame on that route's model; otherwise it keeps its place and the reason names the model.
    Front routes keep manifest order, and so do the rest.
    """
    routes = list(candidates)
    if provider is None or provider == "none" or manifest.kind not in VIDEO_KINDS:
        return routes, ["prefer: manifest order"]
    front: list[Route] = []
    rest: list[Route] = []
    skipped: list[str] = []
    for route in routes:
        if route.provider != provider:
            rest.append(route)
            continue
        slug = route.model_slug or manifest.model_id or manifest.id
        if frame is None or ratio_for is None or ratio_for(slug, frame[0], frame[1], tier) is not None:
            front.append(route)
        else:
            rest.append(route)
            skipped.append(f"prefer: {provider} skipped: no legal ratio for {frame[0]}x{frame[1]} on {slug}")
    if not front and not skipped:
        return routes, [f"prefer: no {provider} route among the candidates, manifest order"]
    reasons = [f"prefer: {provider} first for video"] if front else []
    reasons.extend(skipped)
    return front + rest, reasons
