"""Kling start/end motion job: a CropPair (or any approved start and end still) becomes a clip with a time map and a
video gate report (router plan T43, BUILD_PLAN decisions 13 and 14).

The job is a small pipeline of plain functions over a frozen `MotionPlan`:

1. `plan_motion(pair, ...)` (or `plan_from_still` to cut the pair too) builds the plan: `image` = the start crop,
   `image_tail` = the end crop, the prompt from a motion_croppair recipe with the prompt law applied (an end-frame
   pin means no pace words: slow, slowly, gently, leisurely, unhurried are stripped), a negative prompt under 30
   words, `duration = ceil(shot_s)` clamped to the manifest's 1 to 15, and the Move policy verdict (a `high`,
   `highest` or router-blocked move raises `MotionBlocked`, 409). The frame-rate map is a `TimeMap`: the motion
   packet's fps to the engine's (24, or the adapter's `output_fps`); a still-only CropPair has no packet, so its map
   is virtual (packet fps = engine fps, source "virtual") and says so.
2. `check_unlocked(graph, node_id)` is the server-side video lock: a Motion or CropPair node waits for an upstream
   still whose gate passed AND that a human approved (canvas rule VIDEO_LOCKED). `quote_motion` calls it FIRST, so a
   locked node answers 409 `VIDEO_LOCKED` before any price is built or any run exists. The UI greys the node out;
   this is the enforcement.
3. `quote_motion(plan, graph, manifests, allowed_grades)` picks the allowed route (custody first: Atlas is grade D,
   refused for NDA projects with `CustodyViolation`) and prices the clip per second from the manifest. `to_run_create`
   shapes the same plan as a `RunCreate` for the run table (no STAGING_KEY: this is not an image run).
4. `run_motion(plan, engine, cas)` drives any engine that speaks the worker's protocol (submit / poll / fetch),
   stores the start and end stills and the clip in the project CAS, reads the clip's real fps (a clip that is not at
   the assumed rate gets its time map rebuilt from the clip, with a note) and runs `video_gates.run` with
   `hero_still` = start and `end_still` = end. Landing, flicker, duration, output tags and, with a motion packet,
   reprojection drift come back in one versioned gate report. Camera lock is scored only for a locked tripod: a
   push-in moves by design. `AdapterEngine` puts a `plate_engines` video adapter (KlingStartEnd) behind that protocol.

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

import numpy as np
from blake3 import blake3

from plate_engines.adapters.atlas_video import frame_size
from plate_engines.atlas import (
    VIDEO_POLL_INTERVAL_S,
    VIDEO_POLL_TIMEOUT_S,
    atlas_duration,
    atlas_ratio,
    strip_pace_words,
)
from plate_engines.loader import load_manifests
from plate_gates import io as pio
from plate_gates import reprojection, video_gates
from plate_gates.timemap import TimeMap, as_fps, fps_text

from .. import canvas_rules, custody, idempotency
from ..adapter import PollResult
from ..models import Quote, RunCreate
from ..pipeline import PipelineError
from ..quotes import build_quote
from ..recipes import ProjectSheet, RunnableRecipe, assemble_runnable, load_runnable, unfilled_count
from . import croppair, move_policy

__all__ = [
    "DEFAULT_ENGINE",
    "DEFAULT_ENGINE_FPS",
    "DEFAULT_RECIPE",
    "KLING_ENGINES",
    "NEGATIVE_MAX_WORDS",
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
    "pick_route",
    "plan_from_still",
    "plan_motion",
    "quote_motion",
    "run_motion",
    "to_run_create",
]

KLING_ENGINES = ("kling-o3-4k", "kling-v3-pro")
DEFAULT_ENGINE = "kling-o3-4k"
DEFAULT_RECIPE = "croppair_pushin"
DEFAULT_ENGINE_FPS = Fraction(24)  # Kling and Seedance deliver 24 fps
DEFAULT_RESOLUTION = {"kling-o3-4k": "4K", "kling-v3-pro": "1080P"}
NEGATIVE_MAX_WORDS = 29  # the adapter rejects 30 and over: an anti-drift list, not an essay
TASK = "crop_pair_motion"  # the manifests' allowed_tasks entry; the router and the ledger key on it


class MotionError(PipelineError):
    """A motion request refused before any spend. `status` and `code` become the HTTP answer."""


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
    """Raise `MotionLocked` (409 VIDEO_LOCKED) unless every still feeding this Motion or CropPair node has gate
    status `pass` and is approved; any other canvas-rule violation on its inputs is a 422 with the rule's code."""
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
            raise MotionLocked(r.message or "video is locked", node=node_id, edge=e)
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
    """`ceil(shot_s)` clamped to the manifest's duration range (Kling 1 to 15)."""
    if not shot_s > 0:
        raise MotionError(f"shot length must be positive, got {shot_s!r}", code="shot_length")
    p = next((p for p in manifest.params if p.name == "duration"), None)
    lo, hi = (int(p.range[0]), int(p.range[1])) if p is not None and p.range else (1, 15)
    return atlas_duration(float(shot_s), lo, hi)


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
    """Everything one Kling start/end job needs, frozen. `pair` is the croppair record without the PNG bytes."""

    node_id: str
    engine_id: str
    start_png: bytes
    end_png: bytes
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

    # -- identity ---------------------------------------------------------------------------------------

    @property
    def start_hash(self) -> str:
        return blake3(self.start_png).hexdigest()

    @property
    def end_hash(self) -> str:
        return blake3(self.end_png).hexdigest()

    @property
    def size(self) -> tuple[int, int]:
        return _png_size(self.start_png)

    def params(self) -> dict[str, Any]:
        """The engine's own parameters (the manifest's full schema names)."""
        p: dict[str, Any] = {"duration": self.duration_s, "negative_prompt": self.negative_prompt}
        if self.resolution:
            p["resolution"] = self.resolution
        if self.seed is not None:
            p["seed"] = self.seed
        return p

    def input_hashes(self) -> dict[str, str]:
        """Inputs by hash only (the idempotency key and the run row never carry prompt text)."""
        return idempotency.input_hashes(
            {"image": self.start_hash, "image_tail": self.end_hash, "prompt": self.prompt}
        )

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
        """The worker-protocol payload: start under `image`, end under `image_tail`, opaque names, the prompt."""
        return {
            "engine_id": self.engine_id,
            "inputs": {
                "image": self._image(self.start_png, "start"),
                "image_tail": self._image(self.end_png, "end"),
                "prompt": self.prompt,
            },
            "params": self.params(),
        }

    def adapter_inputs(self, directory: str | Path) -> dict[str, Any]:
        """The same job as `plate_engines` adapter inputs: both frames written to `directory` under opaque names, so
        `KlingStartEnd.quote(...)` and `.submit(...)` take them (their ratio follows the PNG headers)."""
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        out: dict[str, Any] = {"prompt": self.prompt}
        for name, png in (("image", self.start_png), ("image_tail", self.end_png)):
            path = d / _opaque(png)
            path.write_bytes(png)
            w, h = _png_size(png)
            out[name] = {"path": str(path), "role": "plate", "w": w, "h": h}
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
            "start": self.start_hash,
            "end": self.end_hash,
            "size": list(self.size),
            "boxes": [list(b) for b in self.pair["boxes"]],
            "push_ratio": self.pair["push_ratio"],
            "sizemap": self.pair["sizemap"],
            "reconform": self.pair["reconform"],
            "move": self.move,
            "timemap": self.timemap.to_json(),
            "timemap_source": self.timemap_source,
            "tier": self.tier,
        }


def _resolve_recipe(recipe: RunnableRecipe | str | None) -> RunnableRecipe | None:
    if recipe is None:
        return None
    r = load_runnable(recipe) if isinstance(recipe, str) else recipe
    if r.kind != "motion_croppair":
        raise MotionError(f"recipe {r.id} is a {r.kind} recipe, not motion_croppair", code="recipe_kind")
    return r


def _manifest(engine_id: str, manifests: Mapping[str, Any] | None) -> Any:
    if engine_id not in KLING_ENGINES:
        raise MotionError(
            f"{engine_id} is not a Kling start/end engine; have {list(KLING_ENGINES)}", code="engine_kind"
        )
    m = (manifests if manifests is not None else load_manifests()).get(engine_id)
    if m is None:
        raise MotionError(f"unknown engine {engine_id!r}", code="unknown_engine")
    return m


def _resolution(engine_id: str, manifest: Any, resolution: str | None) -> str | None:
    p = next((p for p in manifest.params if p.name == "resolution"), None)
    if p is None:
        return None
    value = str(resolution or DEFAULT_RESOLUTION.get(engine_id) or p.default or "").upper()
    if not value:
        return None
    if p.enum and value not in p.enum:
        raise MotionError(f"{engine_id}: resolution {value!r} is not one of {p.enum}", code="resolution")
    return value


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
) -> MotionPlan:
    """Build the plan for a start/end pair (`croppair.make_pair`'s result). Raises MotionBlocked (409) when the Move
    policy blocks the move, MotionError (422) for a bad engine, resolution, prompt or negative prompt."""
    manifest = _manifest(engine_id, manifests)
    r = _resolve_recipe(recipe)
    motion = dict(r.motion or {}) if r is not None else {}
    shot = float(shot_s if shot_s is not None else motion.get("duration_s", 5))
    duration = clamp_duration(shot, manifest)

    move = move_policy.evaluate(
        move_kind or str(motion.get("move") or "push_in"), move_params or {}, router_verdict
    )
    if move["blocked"]:
        raise MotionBlocked("; ".join(move["reasons"]), move=move)

    if prompt is None:
        if r is None:
            raise MotionError("give a prompt or a motion_croppair recipe", code="prompt_empty")
        prompt = assemble_runnable(r, sheet or ProjectSheet())
    text, law = apply_prompt_law(prompt, end_pinned=True)  # a Kling job always pins the end frame

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
        start_png=bytes(pair["start_png"]),
        end_png=bytes(pair["end_png"]),
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
    )


def plan_from_still(
    approved_still16: Any,
    *,
    recipe: RunnableRecipe | str = DEFAULT_RECIPE,
    centre: Iterable[float] | None = None,
    zoom: tuple[float, float] | None = None,
    engine_size: Any = None,
    quantise: str = "de00",
    **plan_kwargs: Any,
) -> MotionPlan:
    """Cut the pair from the approved still with the recipe's croppair block (`z0`, `z1`, `center`; `centre` and
    `zoom` override) and plan the job. The frame envelope is the engine's resolution tier at the still's
    orientation (3840 x 2160 for 4K) unless `engine_size` says otherwise; the plate is never upscaled to fill it."""
    r = _resolve_recipe(recipe)
    assert r is not None
    cp = dict(r.croppair or {})
    z0, z1 = zoom if zoom is not None else (float(cp.get("z0", 1.0)), float(cp.get("z1", 1.18)))
    arr = np.asarray(approved_still16)
    if arr.ndim != 3:
        raise MotionError(f"the approved still must be H x W x 3, got shape {arr.shape}", code="bad_frame")
    h, w = arr.shape[:2]
    cx, cy = croppair.centre_px(w, h, list(centre) if centre is not None else cp.get("center", [0.5, 0.5]))
    engine_id = plan_kwargs.get("engine_id", DEFAULT_ENGINE)
    if engine_size is None:
        manifest = _manifest(engine_id, plan_kwargs.get("manifests"))
        res = _resolution(engine_id, manifest, plan_kwargs.get("resolution"))
        env = frame_size(res, atlas_ratio(w, h), (w, h))
        engine_size = (env.w, env.h)
    pair = croppair.make_pair(arr, cx, cy, z0, z1, engine_size, quantise=quantise)
    return plan_motion(pair, recipe=r, **plan_kwargs)


# ---------------------------------------------------------------------------- quote


def pick_route(manifest: Any, allowed_grades: Iterable[Any], route_id: str | None = None) -> dict[str, Any]:
    """The first active route whose effective custody grade the run allows, as the run's route record.
    Raises `CustodyViolation` (403) when none is allowed: Atlas is grade D, so an NDA project never gets here."""
    candidates = [r for r in manifest.routes if r.status == "active"]
    if route_id is not None:
        candidates = [r for r in candidates if r.route_id == route_id]
    if not candidates:
        raise MotionError(f"{manifest.id} has no active route {route_id or ''}".strip(), code="unknown_route")
    allowed = list(allowed_grades)
    ok = [r for r in candidates if custody.route_allowed(r, allowed)]
    if not ok:
        custody.require_route(candidates[0], allowed, what=manifest.id)  # raises CustodyViolation
    r = ok[0]
    return {
        "engine_id": manifest.id,
        "provider": r.provider,
        "region": r.region or "unknown",
        "route_id": r.route_id,
        "custody_grade": r.custody_grade,
        "custody_status": r.custody_status,
        "upload_visibility": r.upload_visibility,
        "model_version": r.model_slug or manifest.model_id or manifest.version,
    }


def quote_motion(
    plan: MotionPlan,
    graph: Any,
    manifests: Mapping[str, Any],
    allowed_grades: Iterable[Any],
    *,
    route_id: str | None = None,
    now: Any = None,
) -> tuple[dict[str, Any], Quote]:
    """(route, Quote) for the job. The lock comes first: a locked video node raises MotionLocked (409) before a
    route is picked or a price is built. Then custody (CustodyViolation), then the manifest price per second."""
    check_unlocked(graph, plan.node_id)
    manifest = _manifest(plan.engine_id, manifests)
    route = pick_route(manifest, allowed_grades, route_id)
    quote = build_quote(manifest, route, plan.input_hashes(), plan.params(), now=now)
    return route, quote


def to_run_create(
    plan: MotionPlan, project_id: str, packet_id: str, route: Mapping[str, Any], *, attempt: int = 1
) -> RunCreate:
    """The plan as a run row spec: inputs by CAS hash plus the prompt, the engine's params, the route. There is no
    egress staging block, so the image pipeline does not mistake it for an Edit run."""
    return RunCreate(
        project_id=project_id,
        packet_id=packet_id,
        node_id=plan.node_id,
        inputs={
            "image": plan.start_hash,
            "image_tail": plan.end_hash,
            "prompt": plan.prompt,
            "motion": {
                "task": TASK,
                "shot_class": plan.shot_class,
                "move_kind": plan.move["move_kind"],
                "risk": plan.move["risk"],
                "label": plan.move["label"],
                "boxes": [list(b) for b in plan.pair["boxes"]],
                "push_ratio": plan.pair["push_ratio"],
                "timemap": plan.timemap.to_json(),
            },
        },
        params=plan.params(),
        route=dict(route),
        attempt=attempt,
    )


# ---------------------------------------------------------------------------- run


@dataclass
class MotionResult:
    """What a finished job leaves: CAS keys, the time map actually used, the gate report and the job id."""

    video_key: str
    video_path: Path
    start_key: str
    end_key: str
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
    end_key = cas.put_bytes(plan.end_png, "png")
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
        cas.find(end_key),
        plan.packet_motion,
        tm,
        flow_backend,
        shot_duration_s=plan.shot_s,
        expect_locked=plan.move["move_kind"] == "locked_tripod",
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
    """A `plate_engines` video adapter (KlingStartEnd) behind the worker protocol `run_motion` drives.

    submit() writes the payload's two frames under their opaque names into `staging_dir`, validates the params with
    the adapter's own schema and hands the adapter a `Staged`; poll() maps a JobStatus onto a PollResult and keeps
    the adapter's `output_fps` (Kling reports none, so the time map stays at 24 until the clip is probed)."""

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
            inputs[name] = {"path": str(path), "role": img.get("role", "plate"), "w": img["w"], "h": img["h"]}
            if name == "image":
                dims = (int(img["w"]), int(img["h"]))
        params = self.adapter.validate_params(dict(payload.get("params") or {}))
        size = EngineSize(w=dims[0], h=dims[1]) if dims else None
        return self.adapter.submit(Staged(inputs=inputs, params=params, size=size))

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
