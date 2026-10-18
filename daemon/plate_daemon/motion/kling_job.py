"""Start/end motion job: a CropPair (or any approved start and end still) becomes a clip with a time map and a video
gate report (router plan T43 and T94, BUILD_PLAN decisions 13 and 14, Wave 9 decision 49).

The engines are `motion_engines(manifests)` (Wave 11 decision 70): every `video_startend` or `video_i2v` manifest
with a start-frame input. `START_END_ENGINES` are those that also take an end frame: Kling O3 4K, Kling v3 Pro and
Veo 3.1 (`KLING_ENGINES` is the T43 name, kept as an alias); Seedance 2.5 i2v and Runway Gen-4.5 take a start frame
only, so their takes are single-image (a held frame) and an end still wired to them is 422 `end_unsupported`. Every
route of these engines is custody D until written terms are filed, so an NDA project never reaches them.

The job is a small pipeline of plain functions over a frozen `MotionPlan`:

1. `plan_motion(pair, ...)` (or `plan_from_still` to cut the pair too, or `plan_two_stills` for two approved stills
   of one camera) builds the plan: `image` = the start crop, `image_tail` = the end crop, the prompt from a
   motion_croppair recipe with the prompt law applied (an end-frame pin means no pace words: slow, slowly, gently,
   leisurely, unhurried are stripped), a negative prompt under 30 words, `duration = ceil(shot_s)` clamped to the
   manifest's range and then up to the smallest duration EVERY active route accepts (`prefer.legal_durations`,
   decision 63: Veo 5 s -> 6 s, Kling 1-2 s -> 3 s), and the Move policy verdict (a `high`, `highest` or
   router-blocked move raises `MotionBlocked`, 409). The frame-rate map is a `TimeMap`: the motion packet's fps
   to the engine's (24, or the adapter's `output_fps`); a still-only CropPair has no packet, so its map is
   virtual (packet fps = engine fps, source "virtual") and says so.

   The pair may be a push-in (z0 < z1), a pull-back (z0 > z1, the reveal), a locked crop (z0 = z1; a
   `locked_tripod` move always is one, its end crop the start crop) or a slider drift (one zoom, an end centre).
   A single-image take (`single_image`, or a recipe with `motion.start_end: false`) sends no `image_tail` and pins
   nothing at the end, so no pace words are stripped; only a frame that holds may go without its end. `elements`
   (named reference pictures, Kling only) ride under `inputs.elements`; `extra_params` are the engine's own drawer
   parameters (e.g. `cfg_scale`), checked against the manifest's schema and merged into `MotionPlan.params()`.
2. `check_unlocked(graph, node_id)` is the server-side video lock: a Motion or CropPair node waits for every still
   feeding it (the end still included) to have passed its gate AND been approved by a human (canvas rule
   VIDEO_LOCKED). `quote_motion` calls it FIRST, so a locked node answers 409 `VIDEO_LOCKED`, naming the still,
   before any price is built or any run exists. The UI greys the node out; this is the enforcement.
3. `quote_motion(plan, graph, manifests, allowed_grades)` picks the allowed route (custody first: Atlas and Runway
   are grade D, refused for NDA projects with `CustodyViolation`), then the preference rule (`prefer.prefer_route`,
   decision 63: the `runway` route first for video when the plan's frame fits one of its legal ratios at the plan's
   resolution, else manifest order, with the reason on the route record), and prices the clip per second from the
   route's own price or the manifest's. A pinned route (`route_id`) is the only candidate; a Runway route that cannot
   take the frame is 422 `route_ratio` (Runway centre-crops an off-ratio frame, which would move the landing).
   The params must agree with the route (Wave 12, T138): each candidate's adapter is built as submit builds it and
   runs `validate_params(plan.params())` plus, on Runway, `check_expressible` (a seed on Kling, `camera_fixed` on
   Seedance...); a route that refuses is skipped for the next in preference order, the reason on the record
   (`prefer: runway skipped: <why>`), and when none takes them the quote is 422 `params` with no run.
   `to_run_create` shapes the same plan as a `RunCreate` for the run table (no STAGING_KEY: not an image run).
4. `run_motion(plan, engine, cas)` drives any engine that speaks the worker's protocol (submit / poll / fetch),
   stores the start and end stills and the clip in the project CAS, reads the clip's real fps (a clip that is not at
   the assumed rate gets its time map rebuilt from the clip, with a note) and runs `video_gates.run` with
   `hero_still` = start and `end_still` = end (None for a single-image take: the last frame is not scored).
   Landing, flicker, duration, output tags and, with a motion packet, reprojection drift come back in one versioned
   gate report. Camera lock is scored only for a locked tripod: a push-in moves by design. `AdapterEngine` puts a
   `plate_engines` video adapter (KlingStartEnd, VertexVeo, RunwayVideo) behind that protocol, renaming the inputs
   to the manifest's own (`start_image` / `end_image` for Veo).

Nothing here talks to a vendor on its own: engines and transports are passed in, so every test runs on fakes.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

import cv2
import httpx
import numpy as np
from blake3 import blake3

from plate_engines import prefer
from plate_engines.adapters import runway_schema
from plate_engines.adapters.atlas_video import frame_size
from plate_engines.adapters.base import InputError, ParamError
from plate_engines.adapters.base import _check_param as check_param
from plate_engines.atlas import (
    VIDEO_POLL_INTERVAL_S,
    VIDEO_POLL_TIMEOUT_S,
    atlas_ratio,
    strip_pace_words,
)
from plate_engines.loader import load_manifests
from plate_gates import io as pio
from plate_gates import reprojection, video_gates
from plate_gates.timemap import TimeMap, as_fps, fps_text
from plate_pixels.egress import encode_png8

from .. import canvas_rules, custody, idempotency
from ..adapter import PollResult
from ..models import Quote, RunCreate
from ..pipeline import PipelineError, adapter_class
from ..quotes import build_quote
from ..recipes import ProjectSheet, RunnableRecipe, assemble_runnable, load_runnable, unfilled_count
from . import croppair, move_policy

__all__ = [
    "DEFAULT_ENGINE",
    "DEFAULT_ENGINE_FPS",
    "DEFAULT_RECIPE",
    "KLING_ENGINES",
    "LOCKED_MOVE",
    "NEGATIVE_MAX_WORDS",
    "PLAN_PARAMS",
    "START_END_ENGINES",
    "TWO_STILLS_RECIPE",
    "AdapterEngine",
    "MotionBlocked",
    "MotionError",
    "MotionJobFailed",
    "MotionLocked",
    "MotionPlan",
    "MotionResult",
    "apply_prompt_law",
    "build_timemap",
    "check_unlocked",
    "clamp_duration",
    "element_png",
    "engine_frame",
    "motion_engines",
    "params_check",
    "pick_route",
    "plan_from_still",
    "plan_motion",
    "plan_two_stills",
    "quote_motion",
    "route_adapter",
    "run_motion",
    "runway_ratio",
    "takes_end",
    "to_run_create",
]

MOTION_KINDS = frozenset({"video_startend", "video_i2v"})
START_INPUTS = frozenset({"image", "start_image"})  # a Motion engine takes its start frame under one of these
END_INPUTS = frozenset({"image_tail", "end_image"})  # and its end frame, when it takes one
RUNWAY = prefer.PREFERRED_VIDEO_PROVIDER  # the provider whose routes need a legal ratio for the frame
DEFAULT_ENGINE = "kling-o3-4k"
DEFAULT_RECIPE = "croppair_pushin"
TWO_STILLS_RECIPE = "motion_time_of_day"  # the default when an end still is wired
DEFAULT_ENGINE_FPS = Fraction(24)  # Kling, Seedance, Veo and Gen-4.5 deliver 24 fps
DEFAULT_RESOLUTION = {
    "kling-o3-4k": "4K",
    "kling-v3-pro": "1080P",
    "veo-3.1": "1080p",
    "seedance-2.5-i2v": "1080P",
    "gen-4.5": None,
}
NEGATIVE_MAX_WORDS = 29  # the adapter rejects 30 and over: an anti-drift list, not an essay
TASK = "crop_pair_motion"  # the manifests' allowed_tasks entry; the router and the ledger key on it
LOCKED_MOVE = "locked_tripod"
PLAN_PARAMS = frozenset({"duration", "negative_prompt", "resolution", "seed"})  # the plan sets these itself


class MotionError(PipelineError):
    """A motion request refused before any spend. `status` and `code` become the HTTP answer."""


def motion_engines(manifests: Mapping[str, Any]) -> dict[str, bool]:
    """Engine id -> whether it takes an end frame (decision 70), in id order: every `video_startend` or `video_i2v`
    manifest with an `image` or `start_image` input is a Motion engine; one without an `image_tail` or `end_image`
    input takes a start frame only (its takes are single-image)."""
    out: dict[str, bool] = {}
    for engine_id, m in sorted(manifests.items()):
        names = {i.name for i in m.inputs}
        if m.kind in MOTION_KINDS and names & START_INPUTS:
            out[engine_id] = bool(names & END_INPUTS)
    return out


def takes_end(manifest: Any) -> bool:
    """True when the engine pins an end frame (`image_tail` / `end_image`)."""
    return bool({i.name for i in manifest.inputs} & END_INPUTS)


START_END_ENGINES = tuple(sorted(e for e, tail in motion_engines(load_manifests()).items() if tail))
KLING_ENGINES = START_END_ENGINES  # the T43 name, kept as an alias


class MotionLocked(MotionError):
    """The video node's upstream still has not passed its gates and been approved (canvas rule VIDEO_LOCKED)."""

    def __init__(self, message: str, **extra: Any):
        super().__init__(message, code="VIDEO_LOCKED", status=409, **extra)


class MotionBlocked(MotionError):
    """The Move policy blocks this move (high or highest risk without an override, or a blocking router verdict)."""

    def __init__(self, message: str, **extra: Any):
        super().__init__(message, code="MOVE_BLOCKED", status=409, **extra)


class MotionJobFailed(RuntimeError):
    """The engine refused or failed the job, or it did not finish in time. `state` is refused, failed or timeout."""

    def __init__(self, state: str, detail: str | None = None):
        super().__init__(f"motion job {state}" + (f": {detail}" if detail else ""))
        self.state = state
        self.detail = detail


# ---------------------------------------------------------------------------- the lock


def check_unlocked(graph: Any, node_id: str) -> None:
    """Raise `MotionLocked` (409 VIDEO_LOCKED, `still` naming the node) unless every still feeding this Motion or
    CropPair node, the end still included, has gate status `pass` and is approved; any other canvas-rule violation on
    its inputs is a 422 with the rule's code."""
    g = canvas_rules.as_graph(graph)
    node = next((n for n in g.nodes if n.get("id") == node_id), None)
    if node is None:
        raise MotionError(f"no node {node_id}", code="not_found", status=404)
    if node.get("kind") not in canvas_rules.VIDEO_KINDS:
        raise MotionError(
            f"{node.get('kind')} nodes are not motion nodes", code="node_kind_unsupported", status=422
        )
    incoming = [e for e in g.edges if e["target"] == node_id]
    if not incoming:
        raise MotionLocked(
            f"{node.get('kind')} {node_id} has no upstream still: video starts from an approved still",
            node=node_id,
        )
    failures = [(e, canvas_rules.validate_edge(g, e)) for e in incoming]
    failures = [(e, r) for e, r in failures if not r.ok]
    for e, r in failures:
        if r.code == "VIDEO_LOCKED":
            raise MotionLocked(r.message or "video is locked", node=node_id, edge=e, still=e["source"])
    if failures:
        e, r = failures[0]
        raise MotionError(
            r.message or "canvas rule violated", code=r.code or "pipeline", node=node_id, edge=e
        )


# ---------------------------------------------------------------------------- prompt law, duration, time map


def apply_prompt_law(prompt: str, *, end_pinned: bool) -> tuple[str, dict[str, Any]]:
    """The prompt as it will be sent. With an end frame pinned, pace words are removed (Kling reads them as a speed
    the two frames already fix). Raises MotionError when nothing is left."""
    text = prompt.strip()
    law: dict[str, Any] = {"end_pinned": bool(end_pinned), "pace_words_stripped": False}
    if end_pinned:
        stripped = strip_pace_words(text)
        law["pace_words_stripped"] = stripped != text
        text = stripped
    if not text:
        raise MotionError("the prompt is empty after the prompt law", code="prompt_empty")
    law["tokens_unfilled"] = unfilled_count(text)
    return text, law


def clamp_duration(shot_s: float, manifest: Any) -> int:
    """`ceil(shot_s)` clamped to the manifest's duration range, then up to the smallest duration every active route
    accepts (decision 63: Kling 1-2 s -> 3 s, the Runway floor; Veo 5 s -> 6 s, Runway takes 4, 6 or 8), so the
    plan is legal on whichever route the quote picks and the quote never under-prices. The take outlasts the shot."""
    if not shot_s > 0:
        raise MotionError(f"shot length must be positive, got {shot_s!r}", code="shot_length")
    p = next((p for p in manifest.params if p.name == "duration"), None)
    lo, hi = (int(p.range[0]), int(p.range[1])) if p is not None and p.range else (1, 15)
    return prefer.clamp_to_legal(float(shot_s), prefer.legal_durations(manifest), lo, hi)


def build_timemap(
    shot_s: float,
    engine_fps: Any = DEFAULT_ENGINE_FPS,
    packet_motion: Any = None,
    packet_fps: Any = None,
) -> tuple[TimeMap, str]:
    """(TimeMap, source). With a motion packet (folder, manifest.json path or manifest mapping) the map is the
    packet's fps and frame range to the engine's; without one it is virtual: packet fps = `packet_fps` or the engine's
    and a frame range that spans `shot_s`, so the record still states the rate the clip was asked for."""
    efps = as_fps(engine_fps)
    if packet_motion is not None:
        _, manifest = reprojection.load_packet(packet_motion)
        return video_gates.build_timemap(manifest, efps), "packet"
    pfps = as_fps(packet_fps) if packet_fps is not None else efps
    n = max(int(pfps * Fraction(str(shot_s)) + Fraction(1, 2)), 1)
    return TimeMap(pfps, efps, (0, n - 1)), "virtual"


# ---------------------------------------------------------------------------- the plan


def _opaque(png: bytes) -> str:
    """An egress-style opaque file name: nothing but a hash prefix reaches the vendor."""
    return f"{blake3(png).hexdigest()[:26]}.png"


def _png_size(png: bytes) -> tuple[int, int]:
    if png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR":
        raise MotionError("start and end frames must be PNG", code="bad_frame")
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


@dataclass(frozen=True)
class MotionPlan:
    """Everything one start/end job needs, frozen. `pair` is the croppair record without the PNG bytes. `end_png`
    None is a single-image take (no `image_tail`); `elements` are `{name, png}` reference pictures; `extra_params`
    the engine's own drawer parameters, already checked against its manifest."""

    node_id: str
    engine_id: str
    start_png: bytes
    end_png: bytes | None
    prompt: str
    negative_prompt: str
    shot_s: float
    duration_s: int
    resolution: str | None
    seed: int | None
    move: dict[str, Any]
    timemap: TimeMap
    timemap_source: str
    engine_fps: Fraction
    pair: dict[str, Any]
    recipe_id: str | None = None
    shot_class: str = "wide"
    tier: str = "A"
    packet_motion: Any = None
    law: dict[str, Any] = field(default_factory=dict)
    model_version: str = ""
    elements: tuple[dict[str, Any], ...] = ()
    extra_params: dict[str, Any] = field(default_factory=dict)

    # -- identity ---------------------------------------------------------------------------------------

    @property
    def start_hash(self) -> str:
        return blake3(self.start_png).hexdigest()

    @property
    def end_hash(self) -> str | None:
        return blake3(self.end_png).hexdigest() if self.end_png is not None else None

    @property
    def single_image(self) -> bool:
        return self.end_png is None

    @property
    def element_hashes(self) -> list[str]:
        return [blake3(e["png"]).hexdigest() for e in self.elements]

    @property
    def size(self) -> tuple[int, int]:
        return _png_size(self.start_png)

    def params(self) -> dict[str, Any]:
        """The engine's own parameters (the manifest's full schema names): the drawer's extras, then the plan's. An
        empty negative prompt is left out: an engine that declares none (Seedance 2.5, Gen-4.5) would refuse it."""
        p: dict[str, Any] = dict(self.extra_params)
        p["duration"] = self.duration_s
        if self.negative_prompt:
            p["negative_prompt"] = self.negative_prompt
        if self.resolution:
            p["resolution"] = self.resolution
        if self.seed is not None:
            p["seed"] = self.seed
        return p

    def present_inputs(self) -> dict[str, Any]:
        """Which inputs the job sends, by hash (and the prompt): what a route's `check_expressible` reads at the
        quote, before any frame is staged."""
        inputs: dict[str, Any] = {"image": self.start_hash, "prompt": self.prompt}
        if self.end_png is not None:
            inputs["image_tail"] = self.end_hash
        if self.elements:
            inputs["elements"] = list(self.element_hashes)
        return inputs

    def input_hashes(self) -> dict[str, str]:
        """Inputs by hash only (the idempotency key and the run row never carry prompt text)."""
        inputs: dict[str, Any] = {"image": self.start_hash}
        if self.end_png is not None:
            inputs["image_tail"] = self.end_hash
        inputs["prompt"] = self.prompt
        if self.elements:
            inputs["elements"] = [
                [e["name"], h] for e, h in zip(self.elements, self.element_hashes, strict=True)
            ]
        return idempotency.input_hashes(inputs)

    def idempotency_key(self, route: Mapping[str, Any] | None = None, attempt: int = 1) -> str:
        rid = idempotency.route_id(dict(route) if route else {"engine_id": self.engine_id})
        return idempotency.idempotency_key(self.input_hashes(), self.params(), rid, attempt)

    # -- what the engine receives -----------------------------------------------------------------------

    def _image(self, png: bytes, which: str) -> dict[str, Any]:
        w, h = _png_size(png)
        return {
            "b64": base64.b64encode(png).decode("ascii"),
            "mime": "image/png",
            "name": _opaque(png),
            "role": "plate",
            "w": w,
            "h": h,
            "sizemap": self.pair["sizemap"][which],
        }

    def payload(self) -> dict[str, Any]:
        """The worker-protocol payload: start under `image`, end under `image_tail` (none for a single-image take),
        elements under `elements`, opaque names, the prompt."""
        inputs: dict[str, Any] = {"image": self._image(self.start_png, "start")}
        if self.end_png is not None:
            inputs["image_tail"] = self._image(self.end_png, "end")
        inputs["prompt"] = self.prompt
        if self.elements:
            inputs["elements"] = [
                {
                    "name": e["name"],
                    "image": {
                        "b64": base64.b64encode(e["png"]).decode("ascii"),
                        "mime": "image/png",
                        "name": _opaque(e["png"]),
                    },
                }
                for e in self.elements
            ]
        return {"engine_id": self.engine_id, "inputs": inputs, "params": self.params()}

    def adapter_inputs(self, directory: str | Path) -> dict[str, Any]:
        """The same job as `plate_engines` adapter inputs: the frames written to `directory` under opaque names, so
        `KlingStartEnd.quote(...)` and `.submit(...)` take them (their ratio follows the PNG headers)."""
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        out: dict[str, Any] = {"prompt": self.prompt}
        for name, png in (("image", self.start_png), ("image_tail", self.end_png)):
            if png is None:
                continue
            path = d / _opaque(png)
            path.write_bytes(png)
            w, h = _png_size(png)
            out[name] = {"path": str(path), "role": "plate", "w": w, "h": h}
        if self.elements:
            out["elements"] = []
            for e in self.elements:
                path = d / _opaque(e["png"])
                path.write_bytes(e["png"])
                out["elements"].append({"name": e["name"], "path": str(path)})
        return out

    def record(self) -> dict[str, Any]:
        """JSON-safe description for the node and the ledger: no pixels, no prompt hash tricks."""
        return {
            "node_id": self.node_id,
            "engine_id": self.engine_id,
            "task": TASK,
            "shot_class": self.shot_class,
            "recipe": self.recipe_id,
            "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "prompt_law": dict(self.law),
            "shot_s": self.shot_s,
            "duration_s": self.duration_s,
            "resolution": self.resolution,
            "seed": self.seed,
            "params": self.params(),
            "start": self.start_hash,
            "end": self.end_hash,
            "single_image": self.single_image,
            "elements": [
                {"name": e["name"], "hash": h}
                for e, h in zip(self.elements, self.element_hashes, strict=True)
            ],
            "size": list(self.size),
            "boxes": [list(b) for b in self.pair["boxes"]],
            "push_ratio": self.pair["push_ratio"],
            "pair": pair_summary(self.pair),
            "sizemap": self.pair["sizemap"],
            "reconform": self.pair["reconform"],
            "move": self.move,
            "timemap": self.timemap.to_json(),
            "timemap_source": self.timemap_source,
            "tier": self.tier,
        }


def pair_summary(pair: Mapping[str, Any]) -> dict[str, Any]:
    """`{mode, direction, push_ratio, zoom}` of a pair record: what the run row and the node show of the move."""
    return {
        "mode": pair.get("mode", "croppair"),
        "direction": pair.get("direction", "in"),
        "push_ratio": pair.get("push_ratio"),
        "zoom": dict(pair.get("zoom") or {}),
    }


def _resolve_recipe(recipe: RunnableRecipe | str | None) -> RunnableRecipe | None:
    if recipe is None:
        return None
    r = load_runnable(recipe) if isinstance(recipe, str) else recipe
    if r.kind != "motion_croppair":
        raise MotionError(f"recipe {r.id} is a {r.kind} recipe, not motion_croppair", code="recipe_kind")
    return r


def _manifest(engine_id: str, manifests: Mapping[str, Any] | None) -> Any:
    mf = manifests if manifests is not None else load_manifests()
    engines = motion_engines(mf)
    if engine_id not in engines:
        if engine_id in START_END_ENGINES and engine_id not in mf:
            raise MotionError(f"unknown engine {engine_id!r}", code="unknown_engine")
        raise MotionError(
            f"{engine_id} is not a motion engine (a start frame, or a start and an end frame); have {list(engines)}",
            code="engine_kind",
        )
    return mf[engine_id]


def _resolution(engine_id: str, manifest: Any, resolution: str | None) -> str | None:
    """The resolution in the manifest's own spelling (`4K`, `1080P` for Kling; `1080p` for Veo), matched without
    regard to case; None when the engine has no resolution parameter."""
    p = next((p for p in manifest.params if p.name == "resolution"), None)
    if p is None:
        return None
    value = str(resolution or DEFAULT_RESOLUTION.get(engine_id) or p.default or "").strip()
    if not value:
        return None
    if p.enum:
        hit = next((e for e in p.enum if str(e).upper() == value.upper()), None)
        if hit is None:
            raise MotionError(f"{engine_id}: resolution {value!r} is not one of {p.enum}", code="resolution")
        return str(hit)
    return value.upper()


def _extra_params(manifest: Any, extra: Mapping[str, Any] | None, size: tuple[int, int]) -> dict[str, Any]:
    """The drawer's parameters checked against the manifest's schema (the adapters' own check): an unknown name, a
    name the plan sets itself, or a value outside its type, enum or range is a 422 `params`. A manifest with an
    `aspect_ratio` enum gets the frame's orientation unless the drawer set one."""
    schema = {p.name: p for p in manifest.params}
    out: dict[str, Any] = {}
    for name, value in (extra or {}).items():
        if value is None:
            continue
        if name in PLAN_PARAMS:
            raise MotionError(
                f"{name} is set by the plan (duration from the shot length, negative_prompt, resolution and seed "
                "by name), not as a drawer parameter",
                code="params",
            )
        if name not in schema:
            raise MotionError(
                f"{manifest.id}: unknown parameter {name!r}; the schema has {sorted(schema)}", code="params"
            )
        try:
            out[name] = check_param(schema[name], value)
        except ParamError as e:
            raise MotionError(f"{manifest.id}: {e}", code="params") from e
    ar = schema.get("aspect_ratio")
    if ar is not None and ar.enum and "aspect_ratio" not in out:
        want = atlas_ratio(*size)
        if want in ar.enum:
            out["aspect_ratio"] = want
    return out


def _elements(manifest: Any, elements: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """`[{name, png}]` -> the plan's elements; refused (422) for an engine whose manifest has no `elements` input,
    over its item cap, or without a name and a PNG each."""
    items = list(elements or ())
    if not items:
        return ()
    spec = next((i for i in manifest.inputs if i.name == "elements"), None)
    if spec is None:
        raise MotionError(
            f"{manifest.id} takes no elements; Kling O3 4K does",
            code="elements_unsupported",
            engine=manifest.id,
        )
    if spec.max_items is not None and len(items) > spec.max_items:
        raise MotionError(
            f"{len(items)} elements, {manifest.id} takes at most {spec.max_items}", code="too_many_elements"
        )
    out: list[dict[str, Any]] = []
    for i, el in enumerate(items):
        name = str(el.get("name") or "").strip()
        png = el.get("png")
        if not name:
            raise MotionError(f"elements[{i}] needs a name", code="params")
        if not isinstance(png, bytes | bytearray):
            raise MotionError(f"elements[{i}] needs the picture as PNG bytes", code="params")
        _png_size(bytes(png))
        out.append({"name": name, "png": bytes(png)})
    return tuple(out)


def plan_motion(
    pair: Mapping[str, Any],
    *,
    shot_s: float | None = None,
    node_id: str = "motion",
    engine_id: str = DEFAULT_ENGINE,
    recipe: RunnableRecipe | str | None = DEFAULT_RECIPE,
    sheet: ProjectSheet | None = None,
    prompt: str | None = None,
    negative_prompt: str | None = None,
    move_kind: str | None = None,
    move_params: Mapping[str, Any] | None = None,
    router_verdict: Any = None,
    resolution: str | None = None,
    seed: int | None = None,
    packet_motion: Any = None,
    packet_fps: Any = None,
    output_fps: Any = None,
    tier: str = "A",
    manifests: Mapping[str, Any] | None = None,
    elements: Iterable[Mapping[str, Any]] = (),
    extra_params: Mapping[str, Any] | None = None,
    single_image: bool = False,
) -> MotionPlan:
    """Build the plan for a start/end pair (`croppair.make_pair`'s or `make_two_stills`' result). `single_image`
    drops the end frame (no `image_tail`, nothing pinned at the end); an engine that takes a start frame only
    (Seedance 2.5 i2v, Gen-4.5) always does, so its pair must hold the frame (422 `single_image_move` otherwise) and
    two stills are 422 `end_unsupported`. Raises MotionBlocked (409) when the Move policy blocks the move,
    MotionError (422) for a bad engine, resolution, prompt, negative prompt, element or drawer parameter."""
    manifest = _manifest(engine_id, manifests)
    if not takes_end(manifest):  # a start frame only (Seedance 2.5 i2v, Gen-4.5): the take is single-image
        if pair.get("mode") == "two_stills":
            raise MotionError(
                f"{engine_id} takes a start frame only; unwire the end still",
                code="end_unsupported",
                engine=engine_id,
            )
        end = pair.get("end_png")
        if not single_image and end is not None and bytes(end) != bytes(pair["start_png"]):
            raise MotionError(
                f"{engine_id} takes a start frame only, so the take holds the frame: a moving crop needs its end "
                "frame pinned (one zoom and one centre, or a start/end engine)",
                code="single_image_move",
                engine=engine_id,
            )
        single_image = True
    r = _resolve_recipe(recipe)
    motion = dict(r.motion or {}) if r is not None else {}
    shot = float(shot_s if shot_s is not None else motion.get("duration_s", 5))
    duration = clamp_duration(shot, manifest)
    els = _elements(manifest, elements)
    start_png = bytes(pair["start_png"])
    end_png = None if single_image or pair.get("end_png") is None else bytes(pair["end_png"])
    extra = _extra_params(manifest, extra_params, _png_size(start_png))

    move = move_policy.evaluate(
        move_kind or str(motion.get("move") or "push_in"), move_params or {}, router_verdict
    )
    if move["blocked"]:
        raise MotionBlocked("; ".join(move["reasons"]), move=move)

    if prompt is None:
        if r is None:
            raise MotionError("give a prompt or a motion_croppair recipe", code="prompt_empty")
        prompt = assemble_runnable(r, sheet or ProjectSheet())
    text, law = apply_prompt_law(prompt, end_pinned=end_png is not None)  # an end pin strips pace words

    neg = negative_prompt if negative_prompt is not None else (r.negative_prompt if r is not None else None)
    neg = (neg or "").strip()
    words = len(neg.split())
    if words > NEGATIVE_MAX_WORDS:
        raise MotionError(
            f"negative prompt is {words} words; keep it under 30 (an anti-drift list, not an essay)",
            code="negative_prompt_long",
        )

    efps = as_fps(output_fps) if output_fps else DEFAULT_ENGINE_FPS
    tm, tm_source = build_timemap(shot, efps, packet_motion, packet_fps)
    pair_record = {k: v for k, v in pair.items() if k not in ("start_png", "end_png")}
    return MotionPlan(
        node_id=node_id,
        engine_id=engine_id,
        start_png=start_png,
        end_png=end_png,
        prompt=text,
        negative_prompt=neg,
        shot_s=shot,
        duration_s=duration,
        resolution=_resolution(engine_id, manifest, resolution),
        seed=seed,
        move=move,
        timemap=tm,
        timemap_source=tm_source,
        engine_fps=efps,
        pair=pair_record,
        recipe_id=r.id if r is not None else None,
        shot_class=r.shot_class if r is not None else "wide",
        tier=tier,
        packet_motion=packet_motion,
        law=law,
        model_version=str(manifest.model_id or manifest.version),
        elements=els,
        extra_params=extra,
    )


def engine_frame(
    engine_id: str,
    width: int,
    height: int,
    *,
    manifests: Mapping[str, Any] | None = None,
    resolution: str | None = None,
) -> tuple[int, int]:
    """The engine's frame for a picture of this size: its resolution tier at the picture's orientation (3840 x 2160
    for 4K, 1920 x 1080 for 1080p); (0, 0) when the tier names no size."""
    manifest = _manifest(engine_id, manifests)
    res = _resolution(engine_id, manifest, resolution)
    env = frame_size(res, atlas_ratio(width, height), (width, height))
    return env.w, env.h


def _envelope(engine_id: str, width: int, height: int, plan_kwargs: Mapping[str, Any]) -> tuple[int, int]:
    return engine_frame(
        engine_id,
        width,
        height,
        manifests=plan_kwargs.get("manifests"),
        resolution=plan_kwargs.get("resolution"),
    )


def element_png(
    data: bytes,
    engine_id: str = DEFAULT_ENGINE,
    *,
    manifests: Mapping[str, Any] | None = None,
    resolution: str | None = None,
    quantise: str = "round",
) -> bytes:
    """A reference picture (PNG or JPEG bytes from the project CAS) as an element the engine may take: decoded,
    re-encoded as a house 8-bit sRGB PNG (metadata stripped, so nothing but pixels reaches the vendor) and shrunk
    with `INTER_AREA` until its longest side fits the engine's frame. Never enlarged."""
    img = cv2.imdecode(np.frombuffer(bytes(data), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise MotionError("an element is not a picture OpenCV can read (PNG or JPEG)", code="bad_frame")
    rgb = np.ascontiguousarray(img[..., ::-1])
    h, w = rgb.shape[:2]
    limit = max(engine_frame(engine_id, w, h, manifests=manifests, resolution=resolution))
    if limit and max(w, h) > limit:
        scale = limit / max(w, h)
        size = (max(1, int(w * scale)), max(1, int(h * scale)))
        rgb = cv2.resize(rgb, size, interpolation=cv2.INTER_AREA)
    return encode_png8(rgb, quantise)


def _still16(still: Any, what: str) -> np.ndarray:
    arr = np.asarray(still)
    if arr.ndim != 3:
        raise MotionError(f"{what} must be H x W x 3, got shape {arr.shape}", code="bad_frame")
    return arr


def _zoom(value: Any, what: str) -> float:
    try:
        z = float(value)
    except (TypeError, ValueError):
        raise MotionError(f"{what} must be a number, got {value!r}", code="params") from None
    return z


def plan_from_still(
    approved_still16: Any,
    *,
    recipe: RunnableRecipe | str = DEFAULT_RECIPE,
    centre: Iterable[float] | None = None,
    zoom: tuple[float, float] | None = None,
    centre_end: Iterable[float] | None = None,
    engine_size: Any = None,
    quantise: str = "de00",
    single_image: bool | None = None,
    **plan_kwargs: Any,
) -> MotionPlan:
    """Cut the pair from the approved still with the recipe's croppair block (`z0`, `z1`, `center`, `center_end`;
    `centre`, `zoom` and `centre_end` override) and plan the job. The frame envelope is the engine's resolution tier
    at the still's orientation (3840 x 2160 for 4K) unless `engine_size` says otherwise; the plate is never upscaled
    to fill it.

    A `locked_tripod` move (the node's `move_kind`, else the recipe's `motion.move`) is one zoom with the end crop
    equal to the start: an explicit equal pair's zoom, else the recipe's single zoom, else 1.0 (an unequal pair is
    not a hold, so it is not used). A pull-back is a zoom pair with z0 > z1. `single_image` (default: the recipe's
    `motion.start_end: false`, and always on an engine that takes a start frame only) sends the start frame alone,
    which only a frame that holds may do (one zoom, one centre)."""
    r = _resolve_recipe(recipe)
    assert r is not None
    cp = dict(r.croppair or {})
    motion = dict(r.motion or {})
    if motion.get("two_stills"):
        raise MotionError(
            f"recipe {r.id} moves between two approved stills: wire the end still to the node's end input",
            code="end_required",
        )
    move = move_policy.normalise_move(str(plan_kwargs.get("move_kind") or motion.get("move") or "push_in"))
    recipe_zoom = (_zoom(cp.get("z0", 1.0), "z0"), _zoom(cp.get("z1", 1.18), "z1"))
    z0, z1 = (_zoom(zoom[0], "z0"), _zoom(zoom[1], "z1")) if zoom is not None else recipe_zoom
    end_centre = centre_end if centre_end is not None else cp.get("center_end")
    if move == LOCKED_MOVE:
        # one zoom: the asked one when it is one, else the recipe's single one, else 1.0
        if zoom is None or z0 != z1:
            z0 = z1 = recipe_zoom[0] if recipe_zoom[0] == recipe_zoom[1] else 1.0
        end_centre = None  # the end crop is the start crop: the camera-lock gate measures the hold
    elif move == "push_in" and z0 == z1:
        raise MotionError(
            f"a push-in needs two zooms (z0 < z1 in, z0 > z1 a pull-back), got {z0:g} twice", code="params"
        )
    arr = _still16(approved_still16, "the approved still")
    h, w = arr.shape[:2]
    cx, cy = croppair.centre_px(w, h, list(centre) if centre is not None else cp.get("center", [0.5, 0.5]))
    cx1 = cy1 = None
    if end_centre is not None:
        cx1, cy1 = croppair.centre_px(w, h, list(end_centre))
    single = bool(single_image) if single_image is not None else motion.get("start_end") is False
    engine_id = plan_kwargs.get("engine_id", DEFAULT_ENGINE)
    if plan_kwargs.get("manifests") is None:
        plan_kwargs["manifests"] = load_manifests()  # read once for the envelope, the hold check and the plan
    start_only = not takes_end(_manifest(engine_id, plan_kwargs["manifests"]))
    if single or start_only:
        if z0 != z1 or (cx1 is not None and (cx1, cy1) != (cx, cy)):
            raise MotionError(
                "a single-image take only holds the frame (one zoom, one centre): a moving crop needs its end frame "
                "pinned" + (f" ({engine_id} takes a start frame only)" if start_only else ""),
                code="single_image_move",
            )
        single = True
    if engine_size is None:
        engine_size = _envelope(engine_id, w, h, plan_kwargs)
    pair = croppair.make_pair(
        arr,
        cx,
        cy,
        z0,
        z1,
        engine_size,
        quantise=quantise,
        cx1=cx1,
        cy1=cy1,
        allow_equal=True,
        allow_out=True,
    )
    return plan_motion(pair, recipe=r, single_image=single, **plan_kwargs)


def plan_two_stills(
    start16: Any,
    end16: Any,
    *,
    recipe: RunnableRecipe | str = TWO_STILLS_RECIPE,
    engine_size: Any = None,
    quantise: str = "de00",
    **plan_kwargs: Any,
) -> MotionPlan:
    """Plan a start/end job between two approved stills of one camera (decision 49): each still's whole frame at
    z = 1, brought to one frame by the same envelope rule as `plan_from_still`; the pair record is
    `{mode: "two_stills"}`. Stills of different sizes are a 422 `end_size`."""
    r = _resolve_recipe(recipe)
    assert r is not None
    a = _still16(start16, "the start still")
    b = _still16(end16, "the end still")
    if a.shape[:2] != b.shape[:2]:
        raise MotionError(
            f"the end still is {b.shape[1]} x {b.shape[0]} and the start {a.shape[1]} x {a.shape[0]}: two stills "
            "of one camera are the same size",
            code="end_size",
        )
    h, w = a.shape[:2]
    engine_id = plan_kwargs.get("engine_id", DEFAULT_ENGINE)
    if engine_size is None:
        engine_size = _envelope(engine_id, w, h, plan_kwargs)
    try:
        pair = croppair.make_two_stills(a, b, engine_size, quantise=quantise)
    except croppair.CropPairError as e:
        raise MotionError(str(e), code="bad_frame") from e
    plan_kwargs.pop("single_image", None)  # both ends are the artist's stills: both are pinned
    return plan_motion(pair, recipe=r, **plan_kwargs)


# ---------------------------------------------------------------------------- quote


def runway_ratio(slug: str, w: int, h: int, tier: str | None) -> str | None:
    """`runway_schema.ratio_for`, except None when the model has resolution tiers and `tier` is not one of them:
    the adapter refuses a Kling resolution other than its slug's own (decision 69), so that route does not fit."""
    tiers = [t.upper() for t in runway_schema.spec_for(slug).ratios if t != "all"]
    if tier is not None and tiers and str(tier).upper() not in tiers:
        return None
    return runway_schema.ratio_for(slug, w, h, tier)


def _check_fits(manifest: Any, r: Any, frame: tuple[int, int] | None, tier: str | None) -> None:
    """A Runway route the frame does not fit (a pinned one, or the engine's only one) is refused at the quote:
    Runway centre-crops an off-ratio frame, which would move the landing, and its adapter refuses it anyway."""
    if r.provider != RUNWAY or frame is None:
        return
    slug = r.model_slug or manifest.model_id
    if runway_ratio(slug, frame[0], frame[1], tier) is not None:
        return
    spec = runway_schema.spec_for(slug)
    legal = next((v for k, v in spec.ratios.items() if tier and k.upper() == str(tier).upper()), None)
    legal = legal or tuple(x for v in spec.ratios.values() for x in v)
    at = f" at {tier}" if tier else ""
    raise MotionError(
        f"{manifest.id} via {r.route_id}: no legal ratio for {frame[0]}x{frame[1]}{at} on {slug} (legal: "
        f"{', '.join(legal)}); Runway centre-crops an off-ratio frame, which would move the landing: pick another "
        "route or crop the still to a legal ratio",
        code="route_ratio",
        engine=manifest.id,
        route_id=r.route_id,
    )


class _QuoteTransport(httpx.BaseTransport):
    """The network of an adapter built at the quote: it only checks params, so any request is a bug."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"a quote sends nothing; refused {request.method} {request.url.host}")


def route_adapter(manifest: Any, route: Any) -> Any:
    """The `plate_engines` adapter for `manifest` on `route`, built as submit builds it (`pipeline.adapter_class`)
    but on a transport that sends nothing; None when no adapter speaks the route (the run would fail `no_adapter`
    at execution, as before). An adapter that cannot be built refuses the route (InputError)."""
    cls = adapter_class(manifest, route)
    if cls is None:
        return None
    try:
        return cls(manifest, route, transport=_QuoteTransport())
    except ValueError as e:
        raise InputError(f"{manifest.id} via {route.route_id}: the adapter cannot be built ({e})") from e


def params_check(plan: MotionPlan, manifest: Any) -> Callable[[Any], None]:
    """`accept` for `pick_route`: the route's adapter validates `plan.params()` exactly as submit does and, where
    it has one (Runway), runs `check_expressible` on them and on which inputs the plan sends. Raises ParamError or
    InputError for a route that would refuse the job at submit."""

    def accept(route: Any) -> None:
        adapter = route_adapter(manifest, route)
        if adapter is None:
            return
        params = adapter.validate_params(plan.params())
        check = getattr(adapter, "check_expressible", None)
        if callable(check):
            check(params, plan.present_inputs())

    return accept


def _accepted(
    manifest: Any,
    routes: list[Any],
    accept: Callable[[Any], None],
    frame: tuple[int, int] | None,
    tier: str | None,
) -> tuple[Any, list[str]]:
    """The first of `routes` (preference order; the first already fits the frame) that the frame fits and `accept`
    takes, with a `prefer: <provider> skipped: <why>` reason per route `accept` refused. None taking the job is a
    MotionError 422 `params` naming the first refusal (the preferred route's), every route's under `routes`."""
    skipped: list[str] = []
    refusals: list[dict[str, str]] = []
    for i, r in enumerate(routes):
        if i:
            try:  # a later Runway route the frame does not fit: prefer_route said so already
                _check_fits(manifest, r, frame, tier)
            except MotionError as e:
                refusals.append({"route_id": r.route_id, "detail": str(e)})
                continue
        try:
            accept(r)
        except (ParamError, InputError) as e:
            refusals.append({"route_id": r.route_id, "detail": str(e)})
            skipped.append(f"prefer: {r.provider} skipped: {e}")
            continue
        return r, skipped
    raise MotionError(refusals[0]["detail"], code="params", engine=manifest.id, routes=refusals)


def pick_route(
    manifest: Any,
    allowed_grades: Iterable[Any],
    route_id: str | None = None,
    *,
    prefer_provider: str | None = prefer.PREFERRED_VIDEO_PROVIDER,
    frame: tuple[int, int] | None = None,
    tier: str | None = None,
    accept: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """The run's route record: the active routes (only `route_id` when one is pinned) whose effective custody grade
    the run allows, ordered by the preference rule (`prefer.prefer_route`: `prefer_provider`'s routes first for a
    video engine when `frame` fits one of their legal ratios at `tier`; None or "none" keeps manifest order), the
    first one taken. `reasons` on the record says why. Raises `CustodyViolation` (403) when no route is allowed:
    every route of these engines is grade D, so an NDA project never gets here; MotionError 422 `route_ratio` when
    the route taken is a Runway route the frame does not fit. With `accept` (`params_check`), a route it refuses
    (ParamError / InputError) is skipped for the next in order, and none taking the job is 422 `params`."""
    candidates = [r for r in manifest.routes if r.status == "active"]
    if route_id is not None:
        candidates = [r for r in candidates if r.route_id == route_id]
    if not candidates:
        raise MotionError(f"{manifest.id} has no active route {route_id or ''}".strip(), code="unknown_route")
    allowed = list(allowed_grades)
    ok = [r for r in candidates if custody.route_allowed(r, allowed)]
    if not ok:
        custody.require_route(candidates[0], allowed, what=manifest.id)  # raises CustodyViolation
    ok, reasons = prefer.prefer_route(
        manifest, ok, provider=prefer_provider, frame=frame, tier=tier, ratio_for=runway_ratio
    )
    r = ok[0]
    _check_fits(manifest, r, frame, tier)
    if accept is not None:
        r, skipped = _accepted(manifest, ok, accept, frame, tier)
        reasons = [*reasons, *skipped]
    return {
        "engine_id": manifest.id,
        "provider": r.provider,
        "region": r.region or "unknown",
        "route_id": r.route_id,
        "custody_grade": r.custody_grade,
        "custody_status": r.custody_status,
        "upload_visibility": r.upload_visibility,
        "model_version": r.model_slug or manifest.model_id or manifest.version,
        "reasons": list(reasons),
    }


def quote_motion(
    plan: MotionPlan,
    graph: Any,
    manifests: Mapping[str, Any],
    allowed_grades: Iterable[Any],
    *,
    route_id: str | None = None,
    prefer_provider: str | None = prefer.PREFERRED_VIDEO_PROVIDER,
    now: Any = None,
) -> tuple[dict[str, Any], Quote]:
    """(route, Quote) for the job. The lock comes first: a locked video node raises MotionLocked (409) before a
    route is picked or a price is built. Then custody (CustodyViolation), then the preference rule on the plan's
    frame and resolution, skipping a route whose adapter refuses the plan's params (`params_check`; none left is
    MotionError 422 `params`), then the route's price per second (its own price, else the manifest's)."""
    check_unlocked(graph, plan.node_id)
    manifest = _manifest(plan.engine_id, manifests)
    route = pick_route(
        manifest,
        allowed_grades,
        route_id,
        prefer_provider=prefer_provider,
        frame=plan.size,
        tier=plan.resolution,
        accept=params_check(plan, manifest),
    )
    quote = build_quote(manifest, route, plan.input_hashes(), plan.params(), now=now)
    return route, quote


def to_run_create(
    plan: MotionPlan, project_id: str, packet_id: str, route: Mapping[str, Any], *, attempt: int = 1
) -> RunCreate:
    """The plan as a run row spec: inputs by CAS hash plus the prompt, the engine's params, the route. There is no
    egress staging block, so the image pipeline does not mistake it for an Edit run. A single-image take has no
    `image_tail`."""
    inputs: dict[str, Any] = {"image": plan.start_hash}
    if plan.end_png is not None:
        inputs["image_tail"] = plan.end_hash
    inputs["prompt"] = plan.prompt
    if plan.elements:
        inputs["elements"] = [
            {"name": e["name"], "hash": h} for e, h in zip(plan.elements, plan.element_hashes, strict=True)
        ]
    inputs["motion"] = {
        "task": TASK,
        "shot_class": plan.shot_class,
        "move_kind": plan.move["move_kind"],
        "risk": plan.move["risk"],
        "label": plan.move["label"],
        "boxes": [list(b) for b in plan.pair["boxes"]],
        "push_ratio": plan.pair["push_ratio"],
        "pair": pair_summary(plan.pair),
        "single_image": plan.single_image,
        "timemap": plan.timemap.to_json(),
    }
    return RunCreate(
        project_id=project_id,
        packet_id=packet_id,
        node_id=plan.node_id,
        inputs=inputs,
        params=plan.params(),
        route=dict(route),
        attempt=attempt,
    )


# ---------------------------------------------------------------------------- run


@dataclass
class MotionResult:
    """What a finished job leaves: CAS keys (no end key for a single-image take), the time map actually used, the
    gate report and the job id."""

    video_key: str
    video_path: Path
    start_key: str
    end_key: str | None
    gate_report: dict[str, Any]
    timemap: TimeMap
    timemap_source: str
    provider_job_id: str
    idempotency_key: str
    notes: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        return str(self.gate_report["verdict"])

    def record(self, plan: MotionPlan) -> dict[str, Any]:
        """The Motion node's output: the plan record plus the clip, the (possibly rebuilt) time map and the gate."""
        out = plan.record()
        out.update(
            video=self.video_key,
            provider_job_id=self.provider_job_id,
            idempotency_key=self.idempotency_key,
            timemap=self.timemap.to_json(),
            timemap_source=self.timemap_source,
            gate=self.gate_report,
            verdict=self.verdict,
            notes=list(self.notes),
        )
        return out


def run_motion(
    plan: MotionPlan,
    engine: Any,
    cas: Any,
    *,
    route: Mapping[str, Any] | None = None,
    attempt: int = 1,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    poll_interval_s: float = VIDEO_POLL_INTERVAL_S,
    timeout_s: float = VIDEO_POLL_TIMEOUT_S,
    flow_backend: Any = None,
    gate_kwargs: Mapping[str, Any] | None = None,
) -> MotionResult:
    """Submit, poll, fetch, store and gate. `engine` speaks the worker protocol (`submit(payload, key)`,
    `poll(job) -> PollResult`, `fetch(url) -> bytes`); raises MotionJobFailed for a refused, failed or timed-out job."""
    start_key = cas.put_bytes(plan.start_png, "png")
    end_key = cas.put_bytes(plan.end_png, "png") if plan.end_png is not None else None
    key = plan.idempotency_key(route, attempt)
    job = engine.submit(plan.payload(), key)
    deadline = now() + timeout_s
    while True:
        polled: PollResult = engine.poll(job)
        if polled.status == "succeeded":
            break
        if polled.status in ("refused", "failed"):
            raise MotionJobFailed(polled.status, polled.detail)
        if now() >= deadline:
            raise MotionJobFailed("timeout", f"job {job} did not finish in {timeout_s:g} s")
        sleep(poll_interval_s)
    if not polled.result_url:
        raise MotionJobFailed("failed", "the engine finished without a result")
    video_key = cas.put_bytes(engine.fetch(polled.result_url), "mp4")
    video_path = cas.find(video_key)
    assert video_path is not None

    notes: list[str] = []
    tm = plan.timemap
    clip_fps = pio.probe(video_path)["fps"]
    reported = getattr(engine, "output_fps", None)
    if reported and as_fps(reported) != tm.engine_fps and as_fps(reported) == clip_fps:
        notes.append(f"the adapter reports {fps_text(as_fps(reported))} fps")
    if clip_fps != tm.engine_fps:
        notes.append(
            f"the clip is {fps_text(clip_fps)} fps, not the assumed {fps_text(tm.engine_fps)}: "
            "time map rebuilt from the clip"
        )
        tm = TimeMap(tm.packet_fps, clip_fps, tm.packet_frames)

    model_version = (route or {}).get("model_version") or plan.model_version
    report = video_gates.run(
        video_path,
        cas.find(start_key),
        cas.find(end_key) if end_key is not None else None,
        plan.packet_motion,
        tm,
        flow_backend,
        shot_duration_s=plan.shot_s,
        expect_locked=plan.move["move_kind"] == LOCKED_MOVE,
        engine={"id": plan.engine_id, "version": str(model_version)},
        tier=plan.tier,
        **dict(gate_kwargs or {}),
    )
    return MotionResult(
        video_key=video_key,
        video_path=video_path,
        start_key=start_key,
        end_key=end_key,
        gate_report=report,
        timemap=tm,
        timemap_source=plan.timemap_source,
        provider_job_id=str(job),
        idempotency_key=key,
        notes=notes,
    )


# ---------------------------------------------------------------------------- a plate_engines adapter as a worker engine


class AdapterEngine:
    """A `plate_engines` video adapter (KlingStartEnd, VertexVeo) behind the worker protocol `run_motion` drives.

    submit() writes the payload's frames (and elements) under their opaque names into `staging_dir`, names them the
    way the adapter's manifest does (`image` / `image_tail` for Kling, `start_image` / `end_image` for Veo),
    validates the params with the adapter's own schema and hands the adapter a `Staged`; poll() maps a JobStatus
    onto a PollResult and keeps the adapter's `output_fps` (Kling reports none, so the time map stays at 24 until the
    clip is probed)."""

    ALIASES = {"image": ("image", "start_image"), "image_tail": ("image_tail", "end_image")}

    stub = False
    idempotent_submit = False
    supports_lookup = False

    def __init__(self, adapter: Any, staging_dir: str | Path):
        self.adapter = adapter
        self.staging_dir = Path(staging_dir)
        self.output_fps: float | None = None

    def submit(self, payload: Mapping[str, Any], idempotency_key: str) -> str:
        from plate_engines.adapters.base import Staged
        from plate_engines.models import EngineSize

        self.staging_dir.mkdir(parents=True, exist_ok=True)
        inputs: dict[str, Any] = {"prompt": payload["inputs"]["prompt"]}
        dims: tuple[int, int] | None = None
        for name in ("image", "image_tail"):
            img = payload["inputs"].get(name)
            if img is None:
                continue
            path = self.staging_dir / str(img["name"])
            path.write_bytes(base64.b64decode(img["b64"]))
            ref = {"path": str(path), "role": img.get("role", "plate"), "w": img["w"], "h": img["h"]}
            inputs[self._input_name(name)] = ref
            if name == "image":
                dims = (int(img["w"]), int(img["h"]))
        elements = []
        for el in payload["inputs"].get("elements") or ():
            path = self.staging_dir / str(el["image"]["name"])
            path.write_bytes(base64.b64decode(el["image"]["b64"]))
            elements.append({"name": el["name"], "path": str(path)})
        if elements:
            inputs["elements"] = elements
        params = self.adapter.validate_params(dict(payload.get("params") or {}))
        size = EngineSize(w=dims[0], h=dims[1]) if dims else None
        return self.adapter.submit(Staged(inputs=inputs, params=params, size=size))

    def _input_name(self, name: str) -> str:
        """The adapter manifest's name for a worker-protocol input (the first alias it declares)."""
        manifest = getattr(self.adapter, "manifest", None)
        declared = {i.name for i in getattr(manifest, "inputs", None) or ()}
        return next((a for a in self.ALIASES[name] if a in declared), name)

    def poll(self, provider_job_id: str) -> PollResult:
        st = self.adapter.poll(provider_job_id)
        if st.output_fps:
            self.output_fps = float(st.output_fps)
        if st.state == "done":
            if not st.outputs:
                return PollResult("failed", detail="the engine finished without an output")
            return PollResult("succeeded", result_url=st.outputs[0])
        if st.state in ("failed", "refused"):
            return PollResult(st.state, detail=st.message or None)
        return PollResult("running" if st.state == "running" else "queued")

    def fetch(self, result_url: str) -> bytes:
        return self.adapter.fetch(result_url)

    def cancel(self, provider_job_id: str) -> None:
        fn = getattr(self.adapter, "cancel", None)
        if fn is None:
            raise NotImplementedError(f"{self.adapter.engine_id} has no provider cancel")
        fn(provider_job_id)
