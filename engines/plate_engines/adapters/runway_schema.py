"""Runway Dev API schema tables: per-model field allowlists, ratios, durations, credits (router plan T117).

New for the PLATE monorepo (Wave 11, decisions 59 to 69). Pure data and pure functions, no IO, no httpx, no env reads:
the Runway adapter (T118) builds its request bodies against this module and the manifests (T119) take their route
prices and duration lists from the same numbers.

Where the numbers come from: Runway's published schemas (`POST /v1/image_to_video`, `POST /v1/text_to_video`, API
version 2024-11-06) and its credit catalogue (`guides/pricing.md`), read and skeptic-checked 2026-10-18. 1 credit is
USD 0.01. The ratio tiers (480P/720P/1080P/4K) are inferred from the pixel sizes; Runway's docs do not tabulate them.

Invariants kept in code:

* every request body is a subset of `ModelSpec.fields` (`check_body`): an unknown key is never sent to a closed schema;
* `ratio_for` returns a ratio only when the frame is within 2.5 % of a legal aspect, because Runway centre-crops an
  off-ratio frame and that would move a crop pair's landing; None means "do not send this frame to Runway";
* a prompt is never silently truncated or dropped: too long or empty-when-required raises, and a negative prompt on a
  model with no `negativePrompt` field is folded into the text as a final `Avoid:` line (decision 69).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "ASPECT_TOLERANCE",
    "DATA_URI_MAX_CHARS",
    "DOCUMENTED_OUTPUT_HOST",
    "FAILURE_PREFIXES",
    "KEY_ENV",
    "MODELS",
    "RUNWAY_BASE",
    "RUNWAY_HOST",
    "RUNWAY_VERSION",
    "USD_PER_CREDIT",
    "ModelSpec",
    "aspect",
    "check_body",
    "classify_failure",
    "data_uri_fits",
    "minimum_usd",
    "named_ratio_for",
    "prompt_text",
    "ratio_for",
    "snap_duration",
    "spec_for",
    "usd_per_second",
]

RUNWAY_BASE = "https://api.dev.runwayml.com/v1"
RUNWAY_HOST = "api.dev.runwayml.com"
RUNWAY_VERSION = "2024-11-06"
KEY_ENV = "RUNWAYML_API_SECRET"
DATA_URI_MAX_CHARS = 5_242_880
USD_PER_CREDIT = 0.01
DOCUMENTED_OUTPUT_HOST = "dnznrvs05pmza.cloudfront.net"
ASPECT_TOLERANCE = 0.025

Endpoint = Literal["image_to_video", "text_to_video"]
Keyframes = Literal["first_only", "first_last", "none"]
References = Literal["none", "promptImage_unpositioned", "references_field", "text_to_video_references"]


@dataclass(frozen=True, kw_only=True)
class ModelSpec:
    """One Runway model on one endpoint. `fields` is the ONLY set of keys a request body may carry."""

    model_id: str
    endpoint: Endpoint
    fields: frozenset[str]
    ratios: dict[str, tuple[str, ...]]  # keyed by tier label ("480P", "720P", "1080P", "4K") or "all"
    durations: tuple[int, ...]
    prompt_min: int
    prompt_max: int
    prompt_required: bool
    keyframes: Keyframes
    references: References
    references_max: int | None
    has_seed: bool
    has_audio: bool
    has_negative_prompt: bool
    has_cfg_scale: bool
    has_draft: bool
    credits: dict[str, float]  # credits per second of output, keyed by tier, "audio_off"/"audio_on" or "all"
    minimum_credits: int = 0
    closed: bool
    # text_to_video only: how many `referenceVideos` items (their combined length is not checked here)
    reference_videos_max: int = 0


_SEEDANCE_RATIOS: dict[str, tuple[str, ...]] = {
    "480P": ("992:432", "854:480", "752:560", "640:640", "560:752", "480:854"),
    "720P": ("1470:630", "1280:720", "1112:834", "960:960", "834:1112", "720:1280"),
    "1080P": ("2206:946", "1920:1080", "1664:1248", "1440:1440", "1248:1664", "1080:1920"),
}
_SEEDANCE_CREDITS: dict[str, float] = {"480P": 20, "720P": 30, "1080P": 68}
_KLING_DURATIONS = tuple(range(3, 16))

MODELS: dict[str, ModelSpec] = {
    "seedance2_5": ModelSpec(
        model_id="seedance2_5",
        endpoint="image_to_video",
        fields=frozenset(
            {
                "model",
                "promptImage",
                "promptText",
                "audio",
                "seed",
                "duration",
                "ratio",
                "referenceAudio",
                "draft",
            }
        ),
        ratios={tier: tuple(ratios) for tier, ratios in _SEEDANCE_RATIOS.items()},
        durations=tuple(range(4, 31)),
        prompt_min=1,
        prompt_max=15000,
        prompt_required=False,
        keyframes="first_last",
        references="promptImage_unpositioned",  # array items without `position`; cannot mix with keyframes
        references_max=None,
        has_seed=True,
        has_audio=True,
        has_negative_prompt=False,
        has_cfg_scale=False,
        has_draft=True,
        credits=dict(_SEEDANCE_CREDITS),
        minimum_credits=80,
        closed=True,
    ),
    "seedance2_5/text_to_video": ModelSpec(
        model_id="seedance2_5",
        endpoint="text_to_video",
        fields=frozenset(
            {
                "model",
                "promptText",
                "duration",
                "ratio",
                "audio",
                "seed",
                "references",
                "referenceVideos",
                "referenceAudio",
                "draft",
                "draftTaskId",
            }
        ),
        ratios={tier: tuple(ratios) for tier, ratios in _SEEDANCE_RATIOS.items()},
        durations=tuple(range(4, 31)),
        prompt_min=1,
        prompt_max=15000,
        prompt_required=False,
        keyframes="none",
        references="text_to_video_references",
        references_max=30,
        has_seed=True,
        has_audio=True,
        has_negative_prompt=False,
        has_cfg_scale=False,
        has_draft=True,
        credits=dict(_SEEDANCE_CREDITS),
        minimum_credits=80,
        closed=True,
        reference_videos_max=10,
    ),
    "kling3.0_pro": ModelSpec(
        model_id="kling3.0_pro",
        endpoint="image_to_video",
        fields=frozenset(
            {
                "model",
                "promptImage",
                "promptText",
                "ratio",
                "duration",
                "audio",
                "cfgScale",
                "providerSettings",
            }
        ),
        ratios={"1080P": ("1920:1080", "1080:1920", "1440:1440")},
        durations=_KLING_DURATIONS,
        prompt_min=2,
        prompt_max=2500,
        prompt_required=False,
        keyframes="first_last",  # array items need `position`, max 2, unique positions
        references="none",
        references_max=None,
        has_seed=False,
        has_audio=True,
        has_negative_prompt=False,
        has_cfg_scale=True,
        has_draft=False,
        credits={"audio_off": 12, "audio_on": 17},
        closed=True,
    ),
    "klingO3_4k": ModelSpec(
        model_id="klingO3_4k",
        endpoint="image_to_video",
        fields=frozenset(
            {
                "model",
                "promptImage",
                "promptText",
                "ratio",
                "duration",
                "audio",
                "references",
                "providerSettings",
            }
        ),
        ratios={"4K": ("3840:2160", "2160:3840", "2880:2880")},
        durations=_KLING_DURATIONS,
        prompt_min=2,
        prompt_max=2500,
        prompt_required=False,
        keyframes="first_last",
        references="references_field",
        references_max=7,  # including the keyframes
        has_seed=False,
        has_audio=True,
        has_negative_prompt=False,
        has_cfg_scale=False,
        has_draft=False,
        credits={"all": 43},
        closed=True,
    ),
    "veo3.1": ModelSpec(
        model_id="veo3.1",
        endpoint="image_to_video",
        fields=frozenset(
            {"model", "promptImage", "promptText", "ratio", "audio", "negativePrompt", "seed", "duration"}
        ),
        ratios={
            "720P": ("1280:720", "720:1280"),
            "1080P": ("1920:1080", "1080:1920"),
        },
        durations=(4, 6, 8),
        prompt_min=1,
        prompt_max=1000,
        prompt_required=False,
        keyframes="first_last",  # a last frame alone is refused by the vendor
        references="none",
        references_max=None,
        has_seed=True,
        has_audio=True,
        has_negative_prompt=True,
        has_cfg_scale=False,
        has_draft=False,
        credits={"audio_off": 20, "audio_on": 40},
        closed=True,
    ),
    "gen4.5": ModelSpec(
        model_id="gen4.5",
        endpoint="image_to_video",
        fields=frozenset(
            {
                "model",
                "promptText",
                "promptImage",
                "ratio",
                "duration",
                "seed",
                "contentModeration",
                "outputFormat",
                "proresProfile",
            }
        ),
        ratios={"all": ("1280:720", "720:1280", "1104:832", "960:960", "832:1104", "1584:672")},
        durations=tuple(range(2, 11)),
        prompt_min=1,
        prompt_max=1000,
        prompt_required=True,
        keyframes="first_only",  # array of exactly one `first`
        references="none",
        references_max=None,
        has_seed=True,
        has_audio=False,
        has_negative_prompt=False,
        has_cfg_scale=False,
        has_draft=False,
        credits={"all": 12},
        closed=False,  # Runway's schema is open here; PLATE still enforces the allowlist
    ),
}


def _known() -> str:
    return ", ".join(sorted(MODELS))


def spec_for(model_id: str, endpoint: Endpoint = "image_to_video") -> ModelSpec:
    """The spec for a Runway model id on an endpoint. `"seedance2_5/text_to_video"` is accepted as a full key."""
    if "/" in model_id:
        key = model_id
    elif endpoint == "image_to_video":
        key = model_id
    else:
        key = f"{model_id}/{endpoint}"
    try:
        return MODELS[key]
    except KeyError:
        raise KeyError(f"unknown Runway model {key!r}; known: {_known()}") from None


def aspect(ratio: str) -> float:
    """Width over height of a `W:H` ratio string."""
    try:
        w_s, h_s = ratio.split(":")
        w, h = float(w_s), float(h_s)
    except (ValueError, AttributeError):
        raise ValueError(f"not a W:H ratio: {ratio!r}") from None
    if w <= 0 or h <= 0:
        raise ValueError(f"not a W:H ratio: {ratio!r}")
    return w / h


def _candidates(spec: ModelSpec, tier: str | None) -> tuple[str, ...]:
    """The tier's ratios when the tier is given and present (case-insensitive), else every ratio in table order."""
    if tier is not None:
        wanted = tier.upper()
        for label, ratios in spec.ratios.items():
            if label.upper() == wanted:
                return ratios
    return tuple(r for ratios in spec.ratios.values() for r in ratios)


def _first_within(target: float, ratios: tuple[str, ...]) -> str | None:
    for r in ratios:
        if abs(target / aspect(r) - 1) <= ASPECT_TOLERANCE:
            return r
    return None


def ratio_for(model_id: str, w: int, h: int, tier: str | None = None) -> str | None:
    """The first legal ratio within 2.5 % of the frame's aspect, or None when the frame fits no ratio."""
    if w <= 0 or h <= 0:
        raise ValueError(f"frame size must be positive, got {w}x{h}")
    return _first_within(w / h, _candidates(spec_for(model_id), tier))


_NAMED_ASPECTS: dict[str, float] = {
    "21:9": 21 / 9,
    "16:9": 16 / 9,
    "4:3": 4 / 3,
    "1:1": 1.0,
    "3:4": 3 / 4,
    "9:16": 9 / 16,
}


def named_ratio_for(model_id: str, name: str, tier: str | None = None) -> str | None:
    """Map one of PLATE's ratio names to the model's ratio string. `adaptive` is None: use `ratio_for` on the frame."""
    if name == "adaptive":
        return None
    if name not in _NAMED_ASPECTS:
        raise ValueError(f"unknown ratio name {name!r}; known: adaptive, {', '.join(_NAMED_ASPECTS)}")
    return _first_within(_NAMED_ASPECTS[name], _candidates(spec_for(model_id), tier))


def snap_duration(model_id: str, seconds: float) -> int:
    """Ceil to whole seconds, then the smallest legal duration at or above it; the largest when none is."""
    if not math.isfinite(seconds):
        raise ValueError(f"duration must be finite, got {seconds!r}")
    need = math.ceil(seconds)
    legal = sorted(spec_for(model_id).durations)
    for d in legal:
        if d >= need:
            return d
    return legal[-1]


def usd_per_second(model_id: str, *, resolution: str | None = None, audio: bool = False) -> float:
    """USD per second of output. Seedance is priced by tier, Kling 3.0 Pro and Veo by audio, the rest flat.

    Seedance with no `resolution` answers the dearest tier, so a quote built without one never under-prices.
    """
    spec = spec_for(model_id)
    credits = spec.credits
    if "all" in credits:
        per_second = credits["all"]
    elif "audio_on" in credits:
        per_second = credits["audio_on" if audio else "audio_off"]
    elif resolution is None:
        per_second = max(credits.values())
    else:
        tier = resolution.upper()
        if tier not in credits:
            raise ValueError(
                f"{spec.model_id} has no price for resolution {resolution!r}; tiers: {', '.join(credits)}"
            )
        per_second = credits[tier]
    return round(per_second * USD_PER_CREDIT, 6)


def minimum_usd(model_id: str) -> float:
    """The per-generation floor in USD (Seedance 2.5: 0.80), 0.0 where the vendor states none."""
    return round(spec_for(model_id).minimum_credits * USD_PER_CREDIT, 6)


def data_uri_fits(nbytes: int, mime: str) -> bool:
    """True when `nbytes` of `mime` base64-encodes into a data URI within Runway's 5,242,880 character cap."""
    if nbytes < 0:
        raise ValueError(f"nbytes must be >= 0, got {nbytes}")
    return len(f"data:{mime};base64,") + 4 * math.ceil(nbytes / 3) <= DATA_URI_MAX_CHARS


def check_body(model_id: str, endpoint: Endpoint, body: Mapping[str, object]) -> None:
    """Raise ValueError naming every key of `body` that is not in the model's field allowlist."""
    spec = spec_for(model_id, endpoint)
    extra = sorted(set(body) - spec.fields)
    if extra:
        raise ValueError(
            f"{spec.model_id} {spec.endpoint} body has keys outside the allowlist: {', '.join(extra)}; "
            f"allowed: {', '.join(sorted(spec.fields))}"
        )


def _utf16_units(text: str) -> int:
    """Length in UTF-16 code units, the unit Runway's schemas count (never less than the code point count)."""
    return len(text.encode("utf-16-le")) // 2


def prompt_text(model_id: str, prompt: str, negative_prompt: str | None) -> str:
    """The `promptText` for a model: fold a negative prompt the model cannot express, then enforce the length caps.

    An empty result is legal only on a model whose prompt is optional (the caller then omits `promptText`).
    """
    spec = spec_for(model_id)
    text = prompt or ""
    negative = (negative_prompt or "").strip()
    if negative and not spec.has_negative_prompt:
        text = f"{text}\nAvoid: {negative}" if text.strip() else f"Avoid: {negative}"
    if not text.strip():
        if spec.prompt_required:
            raise ValueError(
                f"{spec.model_id} requires a prompt ({spec.prompt_min} to {spec.prompt_max} characters)"
            )
        return ""
    n = _utf16_units(text)
    if n > spec.prompt_max:
        raise ValueError(f"{spec.model_id} prompt is {n} characters; the limit is {spec.prompt_max}")
    if n < spec.prompt_min:
        raise ValueError(f"{spec.model_id} prompt is {n} characters; the minimum is {spec.prompt_min}")
    return text


# decision 66's order: the first matching prefix wins
FAILURE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("SAFETY.", "refused"),
    ("INPUT_PREPROCESSING.SAFETY.", "refused"),
    ("INTERNAL.BAD_OUTPUT.", "bad_output"),
    ("ASSET.", "input"),
    ("INPUT_PREPROCESSING.VALIDATION", "input"),
    ("INPUT_VALIDATION.", "input"),
    ("DEPRECATED_MODEL", "input"),
    ("QUOTA", "input"),
    ("FORBIDDEN", "input"),
    ("THIRD_PARTY.", "transient"),
    ("INPUT_PREPROCESSING.INTERNAL", "transient"),
    ("INTERNAL", "transient"),
)


def classify_failure(code: str | None) -> str:
    """Map a task `failureCode` (or a 400 `reason`) to refused / bad_output / input / transient."""
    if not code:
        return "transient"
    upper = code.strip().upper()
    for prefix, kind in FAILURE_PREFIXES:
        if upper.startswith(prefix):
            return kind
    return "transient"
