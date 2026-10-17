"""Engine-legal frame sizes and the size pick for vendor egress (BUILD_PLAN decision 3).

An engine manifest's ``sizes`` block (``plate_engines.models.Sizes``, router plan T7) holds a ``legal`` list of
``{w, h, label}`` presets, an ``arbitrary`` envelope ``{min_side, max_side, aspect_min, aspect_max}``, an
``auto`` flag (the output follows the input) and, for tiered engines, named ``aspects``: then each legal entry
is a tier's long-edge box ("2K" = 2048 x 2048) and the legal sizes are every aspect fitted inside every box.

``pick_size`` decides what to ask the engine for. ``exact``: a legal aspect matches the frame within 0.5%.
``arbitrary``: the engine takes any size in its envelope (clamped to its min/max side). ``pad``: the nearest
legal aspect (least padded area; the wider on a tie), reached by padding, never by cropping. Resolution never forces the plate up: a fixed-size engine
gets the largest preset that fits inside the frame (egress downsamples with INTER_AREA); an ``auto`` engine gets
the smallest preset that covers the frame, because it accepts the frame at native size. When nothing fits,
egress sends native pixels and the engine's own output is mapped back by ingress.

Pure functions, no IO. The manifest is duck-typed (a pydantic ``EngineManifest``, its ``sizes`` block or a
plain dict) so ``plate_pixels`` never imports ``plate_engines``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ASPECT_TOLERANCE",
    "ArbitrarySizes",
    "EngineSize",
    "LegalSizes",
    "NoLegalSize",
    "UNVERIFIED_FLAG",
    "legal_sizes",
    "pad_dims",
    "pick_size",
    "same_aspect",
]

# "Same aspect" for the exact mode: relative difference of width/height ratios.
ASPECT_TOLERANCE = 0.005
# A manifest flagged with this has facts that have not survived the P0 smoke test. An unverified arbitrary
# envelope never overrides verified presets (gpt-image-2.5 lists its documented sizes plus their hull).
UNVERIFIED_FLAG = "unverified_p0"

_SIZE_TEXT = re.compile(r"^\s*(\d+)\s*[x*×]\s*(\d+)\s*$")
_ASPECT_TEXT = re.compile(r"^\s*([1-9]\d*)\s*:\s*([1-9]\d*)\s*$")


class NoLegalSize(ValueError):
    """The engine manifest declares no size this frame can be sent at."""


@dataclass(frozen=True)
class EngineSize:
    """A size to request from an engine. ``label`` is the vendor tier ("2K") and ``ratio`` the named aspect
    ("16:9") when the manifest has them; adapters pass them through as the vendor's size parameters."""

    width: int
    height: int
    label: str = ""
    ratio: str = ""
    aspect: float = field(init=False)

    def __post_init__(self) -> None:
        if (
            not (isinstance(self.width, int) and isinstance(self.height, int))
            or self.width < 1
            or self.height < 1
        ):
            raise ValueError(f"EngineSize needs positive integer sides, got {self.width!r} x {self.height!r}")
        object.__setattr__(self, "aspect", self.width / self.height)

    @property
    def area(self) -> int:
        return self.width * self.height

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "aspect": self.aspect,
            "label": self.label,
            "ratio": self.ratio,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> EngineSize:
        """Accepts this class's dict and the manifest's ``{w, h, label}`` form."""
        w = d.get("width", d.get("w"))
        h = d.get("height", d.get("h"))
        return cls(int(w), int(h), str(d.get("label") or ""), str(d.get("ratio") or ""))


@dataclass(frozen=True)
class ArbitrarySizes:
    """An engine that takes any size whose sides and aspect sit inside this envelope."""

    min_side: int = 1
    max_side: int = 32768
    aspect_min: float = 0.0
    aspect_max: float = math.inf

    def accepts_aspect(self, aspect: float) -> bool:
        return self.aspect_min * (1 - 1e-9) <= aspect <= self.aspect_max * (1 + 1e-9)

    def clamp_aspect(self, aspect: float) -> float:
        return min(max(aspect, self.aspect_min), self.aspect_max)

    def clamp(self, w: int, h: int) -> EngineSize:
        """Scale (w, h) so its long side is at most max_side and its short side at least min_side."""
        s = 1.0
        if max(w, h) > self.max_side:
            s = self.max_side / max(w, h)
        if min(w, h) * s < self.min_side:
            s = self.min_side / min(w, h)
        return EngineSize(max(1, int(round(w * s))), max(1, int(round(h * s))))


class LegalSizes(list):
    """``list[EngineSize]`` of presets, carrying the engine's ``arbitrary`` envelope and ``auto`` flag."""

    def __init__(
        self,
        sizes: Iterable[EngineSize] = (),
        arbitrary: ArbitrarySizes | None = None,
        auto: bool = False,
    ) -> None:
        super().__init__(sizes)
        self.arbitrary = arbitrary
        self.auto = auto

    def __repr__(self) -> str:
        return f"LegalSizes({list.__repr__(self)}, arbitrary={self.arbitrary!r}, auto={self.auto})"


# --------------------------------------------------------------------------- manifest reading


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _parse_aspect(text: str) -> tuple[int, int]:
    m = _ASPECT_TEXT.match(str(text))
    if not m:
        raise ValueError(f"aspect {text!r} must look like '16:9'")
    return int(m.group(1)), int(m.group(2))


def _parse_entry(entry: Any) -> tuple[int, int, str] | None:
    """A legal entry -> (w, h, label); None for the literal 'auto' (a vendor mode, not a size)."""
    if isinstance(entry, str):
        if entry.strip().lower() == "auto":
            return None
        m = _SIZE_TEXT.match(entry)
        if not m:
            raise ValueError(f"legal size {entry!r} is not 'WxH'")
        return int(m.group(1)), int(m.group(2)), ""
    w = _get(entry, "w", _get(entry, "width"))
    h = _get(entry, "h", _get(entry, "height"))
    if w is None or h is None:
        raise ValueError(f"legal size entry {entry!r} needs w and h")
    return int(w), int(h), str(_get(entry, "label", "") or "")


def _fit_in_box(box_w: int, box_h: int, p: int, q: int) -> tuple[int, int]:
    s = min(box_w / p, box_h / q)
    return max(1, int(round(p * s))), max(1, int(round(q * s)))


def legal_sizes(engine_manifest: Any, *, allow_unverified: bool = False) -> LegalSizes:
    """Every size the engine accepts, sorted by aspect then area; ``.arbitrary`` / ``.auto`` ride along.

    ``engine_manifest`` may be an ``EngineManifest``, a manifest dict, or the ``sizes`` block itself. A manifest
    without sizes yields an empty result (``pick_size`` then raises ``NoLegalSize``: never a silent guess).
    """
    sizes = _get(engine_manifest, "sizes")
    if sizes is None and any(_get(engine_manifest, k) is not None for k in ("legal", "arbitrary")):
        sizes = engine_manifest  # the sizes block was passed directly
    if sizes is None:
        return LegalSizes()
    entries = [e for e in (_parse_entry(x) for x in (_get(sizes, "legal") or [])) if e is not None]
    aspects = [_parse_aspect(a) for a in (_get(sizes, "aspects") or [])]
    out: dict[tuple[int, int], EngineSize] = {}
    if aspects:
        # Tiered engine: each legal entry is a long-edge box; every named aspect fits inside every box.
        for bw, bh, label in entries:
            for p, q in aspects:
                w, h = _fit_in_box(bw, bh, p, q)
                out.setdefault((w, h), EngineSize(w, h, label, f"{p}:{q}"))
    else:
        for w, h, label in entries:
            out.setdefault((w, h), EngineSize(w, h, label))
    presets = sorted(out.values(), key=lambda s: (s.aspect, s.area))

    arbitrary = None
    raw = _get(sizes, "arbitrary")
    if raw is not None:
        arbitrary = ArbitrarySizes(
            min_side=int(_get(raw, "min_side", 1) or 1),
            max_side=int(_get(raw, "max_side", 32768) or 32768),
            aspect_min=float(_get(raw, "aspect_min", 0.0) or 0.0),
            aspect_max=float(_get(raw, "aspect_max", math.inf) or math.inf),
        )
        flags = _get(engine_manifest, "flags") or []
        if presets and UNVERIFIED_FLAG in flags and not allow_unverified:
            arbitrary = None
    auto = bool(_get(sizes, "auto", False))
    if not presets and arbitrary is None and auto:
        # Output follows the input and no envelope is known: any size, no clamp.
        arbitrary = ArbitrarySizes()
    return LegalSizes(presets, arbitrary=arbitrary, auto=auto)


# --------------------------------------------------------------------------- the pick


def same_aspect(a: float, b: float, tol: float = ASPECT_TOLERANCE) -> bool:
    return abs(a / b - 1.0) <= tol


def pad_dims(w: int, h: int, aspect: float) -> tuple[int, int]:
    """The smallest (W, H) >= (w, h) with W / H == aspect to the pixel: pad one axis, never crop."""
    if w / h >= aspect:
        return w, max(h, int(round(w / aspect)))
    return max(w, int(round(h * aspect))), h


def _ratio_name(aspect: float, presets: list[EngineSize]) -> str:
    for s in presets:
        if s.ratio and same_aspect(s.aspect, aspect):
            return s.ratio
    return ""


def _resolution(cands: list[EngineSize], w: int, h: int, auto: bool) -> EngineSize:
    fits = [s for s in cands if s.width <= w and s.height <= h]
    covers = [s for s in cands if s.width >= w and s.height >= h]
    if auto:
        # The engine takes native input: ask for the smallest tier that covers it, else the biggest there is.
        return min(covers, key=lambda s: s.area) if covers else max(cands, key=lambda s: s.area)
    # Fixed-size engine: the largest preset the frame can be downsampled to; if none, the smallest preset
    # (egress then sends native pixels, never an upsampled plate).
    return max(fits, key=lambda s: s.area) if fits else min(cands, key=lambda s: s.area)


def pick_size(
    src_w: int,
    src_h: int,
    legal: Iterable[EngineSize],
    arbitrary: ArbitrarySizes | None = None,
) -> tuple[EngineSize, str]:
    """Return ``(EngineSize, mode)`` with mode ``exact`` | ``arbitrary`` | ``pad`` (see the module docstring).

    ``legal`` is normally ``legal_sizes(manifest)``; its ``.arbitrary`` and ``.auto`` are used unless
    ``arbitrary`` is passed explicitly. A plain list of ``EngineSize`` means fixed presets only.
    """
    if src_w < 1 or src_h < 1:
        raise ValueError(f"source size must be positive, got {src_w} x {src_h}")
    arb = arbitrary if arbitrary is not None else getattr(legal, "arbitrary", None)
    auto = bool(getattr(legal, "auto", False))
    presets = list(legal)
    a = src_w / src_h

    exact = [s for s in presets if same_aspect(s.aspect, a)]
    if exact:
        fits = [s for s in exact if s.width <= src_w and s.height <= src_h]
        if not fits and not auto and arb is not None and arb.accepts_aspect(a):
            size = arb.clamp(src_w, src_h)
            return EngineSize(size.width, size.height, ratio=_ratio_name(a, presets)), "arbitrary"
        return _resolution(exact, src_w, src_h, auto), "exact"
    if arb is not None and arb.accepts_aspect(a):
        size = arb.clamp(src_w, src_h)
        return EngineSize(size.width, size.height, ratio=_ratio_name(a, presets)), "arbitrary"
    if presets:
        # |log(a / x)| is exactly the log of the padded area over the frame area, so the nearest aspect is the
        # least padding; on a tie (4:3 between 1:1 and 16:9) the wider aspect wins.
        target = min({s.aspect for s in presets}, key=lambda x: (round(abs(math.log(a / x)), 9), -x))
        pw, ph = pad_dims(src_w, src_h, target)
        cands = [s for s in presets if same_aspect(s.aspect, target)]
        return _resolution(cands, pw, ph, auto), "pad"
    if arb is not None:
        pw, ph = pad_dims(src_w, src_h, arb.clamp_aspect(a))
        return arb.clamp(pw, ph), "pad"
    raise NoLegalSize("engine manifest declares no sizes (sizes.legal / sizes.arbitrary / sizes.auto)")
