"""Motion, CropPair and People nodes on the run lifecycle (router plan T66, decision 38).

Node kinds are wired by install-wrappers, not by editing `pipeline.QUOTABLE_KINDS`: `install(pipeline)` wraps
`quote_node`, `approve`, `step`, `engine_for` and `cancel` on that Pipeline instance the way `nodes.install` does for
the numeric nodes, so `POST /projects/{id}/nodes/{node}/quote`, `POST /runs/{id}/approve`, `pipeline.execute` and
`resume`, and `POST /runs/{id}/cancel` drive these runs like an Edit: QUOTED, APPROVED, QUEUED, STAGING, SUBMITTED,
RUNNING, FETCHING, NORMALIZING, GATING, DONE or GATE_FAIL, every move published as `run.state` and ledgered (with
`data.model`, `data.task` and `data.shot_class`, and `data.previs` on a previs run). Three flavours:

    kling    A Motion or CropPair node on a Motion engine (`motion.kling_job.motion_engines`: Kling O3 4K, Kling
             v3 Pro and Veo 3.1 take a start and an end frame; Seedance 2.5 i2v and Gen-4.5 a start frame only,
             so their takes are single-image and an end still is 422 `end_unsupported`). Quote: the video lock
             first (`check_unlocked`: 409 VIDEO_LOCKED, naming the still, before a pair is cut or a price
             exists; the end still is locked like the start), the pair cut from the approved still on the
             `image` input (its `outputHash` in the CAS) by `plan_from_still`, or, with a
             still on the `end` input, `plan_two_stills` between the two (the end must come from the same packet,
             or a packet with the same `camera.name` and `scene_hash`, else 422 `end_frame_camera`). Reference
             nodes on `elements` become the plan's elements (422 `elements_unsupported` on an engine without
             them, 422 `reference_missing` for a hash not in the CAS); every node parameter the engine's manifest
             declares (`cfg_scale`, Veo's `aspect_ratio`) is a drawer parameter of the plan; `single_image`
             sends no end frame (a held frame only). Then the Move policy (a blocked move is 409
             HIGH_RISK_MOVE) and `quote_motion` (custody before price: every video route is grade D, so an NDA
             project gets `CustodyViolation`, 403; then the preference rule, Wave 11 decision 63: the body's
             `route_id`, else the project's `video_route_pins[engine]`, pins a route, and the project's
             `preferred_video_provider`, default `runway`, goes first where the frame fits; then each candidate's
             adapter checks the plan's params as submit would, Wave 12 T138: a route that refuses them, such as
             a Kling seed on Runway, is skipped for the next with `prefer: runway skipped: <why>`, and none
             taking them is 422 `params` with no run; the reasons are kept as `motion_run.route_reasons`).
             The worker submits the plan's payload (start under `image`,
             end under `image_tail`, opaque names), polls and fetches; the fetched clip goes into the CAS as
             `.mp4` at once and NORMALIZING hands it to `run_motion`, which stores the stills, reads the clip's
             real frame rate and runs the video gates. Outputs `[clip, start, end]` (`[clip, start]` for a
             single-image take); `normalized.timemap` is the map the gates used; the gate report is the video
             gate report.
    previs   A Motion node with `params.previs` (or `mode: previs`, or the Seedance r2v engine, which only previs
             drives here): `motion.previs` plans the reference-to-video take (refs, clay reference, timing hints)
             on the route the same overrides and preference pick (the hero still is the frame) and prices it;
             the worker submits, polls and fetches; the fetch burns the PREVIS watermark before
             anything is kept (the clean take never enters the CAS). `inputs.previs` and `normalized.previs` are
             true, the Motion node is marked previs, and delivery refuses the output (`delivery.guard`).
    people   A People node over STAFFAGE (`motion.people`). Quote: custody first (STAFFAGE is grade D unless it is
             configured `local_only`; an NDA project gets `CustodyViolation`), then the node's parameters,
             priced at about USD 0.10 per second of shot. `people.run` (STAFFAGE's job polled to its end) runs at
             SUBMIT, so a daemon that dies mid-take leaves a submit intent without a job id and recovery fails the
             run for a human instead of starting a second paid take. Outputs: the composite PNG16; the gate report
             is the still gates `people.run` scored.

A run made here carries `inputs.staging` (the marker `Pipeline.resume` looks for) with `node_kind`, and
`inputs.motion_run` (how to rebuild the job at execution). Execution rebuilds the job from that spec and the CAS and
refuses to send it when the approved still changed since the quote; what the vendor adapter did that the plan did
not say (the Runway adapter's `notes(job)`: a duration snap, reference roles) is appended to `normalized.notes` at
NORMALIZING. Approval goes through the budget rules (these
runs are priced); custody is re-checked at approval and again before the submit.

`router_preview` is an object with `prepare(q)` and `preview(c)` in the protocol `nodes.runner.preview` drives
(T73 registers it as `PREVIEWS["router"]`): the render-it router's card `{verdict, mode, D_px_median,
disocclusion_fraction, orbit_deg, explanation, numbers, ...}` from `router_math.route_move`, on the motion packet
whose hero is the node's plate when there is one, else on the still itself (a crop pair keeps the camera).

    install(pipeline) -> MotionRunner        idempotent; the runner is kept at `pipeline.motion_runner`
"""

from __future__ import annotations

import json
import logging
import math
import threading
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from blake3 import blake3

from plate_engines import prefer

from . import canvas_rules, custody
from .adapter import PollResult, lookup_fn, submit_is_idempotent
from .cas import CAS
from .custody import CustodyViolation
from .db import tx
from .idempotency import canonical_json
from .machine import create_run, get_run, transition
from .models import QUOTE_TTL, Quote, Run, RunCreate
from .motion import kling_job, people, previs
from .nodes.common import Prepared, cas_has, pick, plain
from .pipeline import LEDGER_KINDS, STAGING_KEY, PipelineError, adapter_class, decode_png16
from .quotes import insert_quote
from .recipes import ProjectSheet, RecipeError
from .states import TERMINAL, WORKER_STATES, Priority, RunState
from .worker import GateOutcome, cancel_run
from .worker import step as worker_step

__all__ = [
    "KINDS",
    "PEOPLE_ENGINE_ID",
    "PEOPLE_USD_PER_S",
    "SPEC_KEY",
    "MotionRunner",
    "RouterPreview",
    "flavour_of",
    "install",
    "is_motion_run",
    "router_card",
    "router_preview",
]

log = logging.getLogger(__name__)
S = RunState

KINDS = frozenset({"Motion", "CropPair", "People"})
FLAVOURS = ("kling", "previs", "people")
SPEC_KEY = "motion_run"  # runs.inputs[SPEC_KEY]: how to rebuild the job at execution
PEOPLE_ENGINE_ID = "staffage"
PEOPLE_USD_PER_S = 0.10  # motion/people.py: a STAFFAGE take costs about USD 0.10 per second of shot
PEOPLE_SHOT_S = 5.0
PEOPLE_ETA_S = 25 * 60.0  # STAFFAGE's own ship loop gives a take 25 minutes
PEOPLE_TASK = "people"
PREVIS_TASK = "previs"
CLIP_URL = "plate-motion://"
PEOPLE_URL = "plate-people://"
PLAN_CACHE = 4
MOVE_DIALS = {
    "reveal_m": ("reveal_m", "revealM"),
    "orbit_deg": ("orbit_deg", "orbitDeg"),
    "travel_cm": ("travel_cm", "travelCm", "sliderCm", "slider_cm"),
}
NOT_A_STILL = frozenset(
    {"end", "elements", "refs", "clip", "sky", "layers", "mask"}
)  # inputs that are not the start


def input_of(g: Any, edge: Mapping[str, Any]) -> str | None:
    """The input port an edge lands on, resolved as `canvas_rules` (and the UI's `checkHandles`) resolve it: the
    named `targetHandle`, else the first input of the target that takes the source port's type (a Reference with no
    handle lands on `elements` / `refs`, a Plate's beauty on `image`)."""
    handle = edge.get("targetHandle")
    if isinstance(handle, str) and handle:
        return handle
    kinds = {n["id"]: n.get("kind") for n in g.nodes}
    outputs = (canvas_rules.PORTS.get(kinds.get(edge["source"])) or {}).get("outputs", ())
    sh = edge.get("sourceHandle")
    out = next((o for o in outputs if o[0] == sh), None) if sh else (outputs[0] if outputs else None)
    inputs = (canvas_rules.PORTS.get(kinds.get(edge["target"])) or {}).get("inputs", ())
    if out is not None:
        hit = next((i for i in inputs if out[1] in i[2]), None)
        if hit is not None:
            return hit[0]
    return inputs[0][0] if inputs else None


def edges_by_input(g: Any, node_id: str) -> dict[str, list[dict[str, Any]]]:
    """The node's incoming edges grouped by the input they land on, in edge order."""
    out: dict[str, list[dict[str, Any]]] = {}
    for e in g.edges:
        if e["target"] == node_id:
            out.setdefault(input_of(g, e) or "image", []).append(e)
    return out


def is_motion_run(run: Run) -> bool:
    """A run this module made: a `motion_run` spec and the staging marker naming a Motion, CropPair or People node."""
    spec = run.inputs.get(SPEC_KEY)
    staging = run.inputs.get(STAGING_KEY)
    return (
        isinstance(spec, Mapping)
        and spec.get("flavour") in FLAVOURS
        and isinstance(staging, Mapping)
        and staging.get("node_kind") in KINDS
    )


def flavour_of(run: Run) -> str:
    return str((run.inputs.get(SPEC_KEY) or {}).get("flavour"))


def _number(value: Any, what: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PipelineError(f"{what} must be a finite number, got {value!r}", code="params")
    return float(value)


def _blocked(e: kling_job.MotionBlocked) -> PipelineError:
    """The Move policy's refusal as the canvas names it: 409 HIGH_RISK_MOVE, the policy's verdict attached."""
    return PipelineError(str(e), code="HIGH_RISK_MOVE", status=409, **plain(e.extra))


def _camera_of(manifest: Mapping[str, Any]) -> tuple[Any, Any]:
    """(camera name, scene hash) of a packet manifest: two packets that share both shoot one camera."""
    cam = manifest.get("camera") if isinstance(manifest.get("camera"), Mapping) else {}
    scene = manifest.get("scene_hash") if isinstance(manifest.get("scene_hash"), Mapping) else {}
    return cam.get("name"), scene.get("hex")


def _sheet(project: Any) -> ProjectSheet:
    settings = project.doc.get("settings") or {}
    data = settings.get("project_sheet") if isinstance(settings, Mapping) else None
    return ProjectSheet.from_dict(data if isinstance(data, Mapping) else None)


def _route_choice(project: Any, body: Mapping[str, Any], engine_id: str) -> tuple[str | None, str | None]:
    """(route_id, prefer_provider) for a video quote (decision 63): the body's `route_id`, else the project's
    `video_route_pins[engine_id]`; the project's `preferred_video_provider` (default `runway`, "none" = manifest
    order)."""
    settings = project.doc.get("settings") or {}
    settings = settings if isinstance(settings, Mapping) else {}
    pins = settings.get("video_route_pins") if isinstance(settings.get("video_route_pins"), Mapping) else {}
    route_id = body.get("route_id") or pins.get(engine_id)
    prefer_provider = settings.get("preferred_video_provider", prefer.PREFERRED_VIDEO_PROVIDER)
    return (str(route_id) if route_id else None), prefer_provider


# ---------------------------------------------------------------------------- the render-it router


def _still_geometry(pv: Any) -> tuple[Any, Any, Any, str] | None:
    """(Z, rank-0 object ids, background value, depth kind) of a Tier A packet, or None."""
    exr = pv.files.get("beauty_exr")
    if pv.tier != "A" or exr is None:
        return None
    from plate_pixels.exr import read_elements

    z = read_elements(pv.root / exr["path"], pv.manifest, ["zdepth"])["zdepth"]
    z = z[..., 0] if z.ndim == 3 else z
    pair = pv.crypto("object")
    ids = pair[0][..., 0] if pair is not None else None
    zd = next((e for e in exr.get("elements") or [] if e.get("element") == "zdepth"), {})
    return z, ids, zd.get("background_value"), str(zd.get("depth_kind") or "planar")


def _detail(pv: Any, geometry: Any) -> dict[str, Any] | None:
    """`router_math.detail_resolvability` of the still (the verdict card's D), or None when it cannot be read."""
    from plate_gates import router_math

    if geometry is None or geometry[1] is None:
        return None
    z, ids, bg, _kind = geometry
    cam = pv.manifest.get("camera") or {}
    try:
        return router_math.detail_resolvability(
            z, ids, pv.legend or {}, cam["intrinsics_px"], pv.manifest.get("units") or {}, bg_value=bg
        )
    except (KeyError, TypeError, ValueError) as e:
        log.info("detail resolvability of %s not measured: %s", pv.packet_id, e)
        return None


def motion_packet_for(pipeline: Any, project: Any, hero_packet_id: str, named: str | None = None) -> Any:
    """The ingested motion packet whose hero still is `hero_packet_id` (or the one `named`), else None. Two
    candidates and no name is None too: the router does not guess which path the artist meant."""
    pid = project.project_id
    if named:
        pv = pipeline.packet(pid, named)
        if pv.manifest.get("kind") != "motion":
            raise PipelineError(f"packet {named} is not a motion packet", code="no_motion_packet")
        return pv
    found = []
    for packet_id, entry in sorted(project.packets().items()):
        if entry.get("kind") not in (None, "motion") or entry.get("status") not in (None, "ingested"):
            continue
        try:
            pv = pipeline.packet(pid, packet_id)
        except PipelineError:
            continue
        motion = pv.manifest.get("motion") or {}
        if pv.manifest.get("kind") == "motion" and motion.get("hero_still_packet_id") == hero_packet_id:
            found.append(pv)
    return found[0] if len(found) == 1 else None


def router_card(
    pipeline: Any, project_id: str, pv: Any, *, motion_packet_id: str | None = None, mode: str | None = None
) -> tuple[Any, str, str | None]:
    """(RouterVerdict, source, motion packet id). `source` is `motion_packet` (the rendered camera path from the
    packet's first to its last frame), `still` (no motion packet: a crop pair keeps the camera, so the start and
    end camera are the still's own) or `none` (Tier C or no Z: a human decides). The mode is the daemon's
    (`PLATE_ROUTER_MODE`, advisory by default), never the request's."""
    from plate_gates import router_math

    mode = router_math.router_mode(mode)
    project = pipeline.load_project(project_id)
    if project is None:
        raise PipelineError(f"no project {project_id}", code="not_found", status=404)
    motion = motion_packet_for(pipeline, project, pv.packet_id, motion_packet_id)
    geometry = _still_geometry(pv)
    detail = _detail(pv, geometry)
    if motion is not None:
        packet_motion, z_first, z_last = router_math.load_motion(motion.root)
        verdict = router_math.route_move(packet_motion, z_first, z_last, mode=mode, detail=detail)
        return verdict, "motion_packet", motion.packet_id
    if geometry is not None:
        z, _ids, bg, depth_kind = geometry
        cam = pv.manifest["camera"]
        still = {
            "cam_start": cam["world_from_camera"],
            "cam_end": cam["world_from_camera"],
            "K": cam["intrinsics_px"]["K"],
            "bg_value": bg,
            "depth_kind": depth_kind,
            "packet_id": pv.packet_id,
        }
        verdict = router_math.route_move(still, z, z, mode=mode, detail=detail)
        text = (
            "no motion packet: a crop pair of the still keeps the camera at both ends; " + verdict.explanation
        )
        return verdict.model_copy(update={"explanation": text}), "still", None
    numbers: dict[str, Any] = {"disocclusion_fraction": None, "orbit_deg": None, "crossings": []}
    verdict = router_math.RouterVerdict(
        mode=mode,
        verdict="unknown",
        numbers=numbers,
        explanation=f"packet {pv.packet_id} has no Z buffer (Tier C): the router cannot measure the move; "
        f"a human decides ({mode}).",
    )
    return verdict, "none", None


def card_of(verdict: Any, source: str, motion_packet_id: str | None) -> dict[str, Any]:
    """The verdict card the Motion editor parses (`parseRouterCard`): the RouterVerdict plus its three numbers
    at the top level."""
    d = verdict.model_dump(mode="json")
    n = d.get("numbers") or {}
    return plain(
        {
            "verdict": d["verdict"],
            "mode": d["mode"],
            "D_px_median": n.get("D_px_median"),
            "disocclusion_fraction": n.get("disocclusion_fraction"),
            "orbit_deg": n.get("orbit_deg"),
            "explanation": d["explanation"],
            "blocks": d.get("blocks", False),
            "numbers": n,
            "source": source,
            "motion_packet_id": motion_packet_id,
        }
    )


class RouterPreview:
    """`nodes.runner.PREVIEWS["router"]`: the router card for a Motion or CropPair node. Numbers only: no run, no
    spend, nothing stored."""

    KIND = "router"

    def prepare(self, q: Any) -> Prepared:
        packet_id = q.plate_packet_id()
        spec = {
            "packet_id": packet_id,
            "node_kind": q.node.get("kind"),
            "motion_packet_id": q.param("motion_packet_id", "motionPacketId"),
        }
        return Prepared(spec=plain(spec), packet_id=packet_id)

    def preview(self, c: Any) -> dict[str, Any]:
        pv = c.packet()
        verdict, source, mp = router_card(
            c.pipeline, c.project_id, pv, motion_packet_id=c.spec.get("motion_packet_id")
        )
        return card_of(verdict, source, mp)


router_preview = RouterPreview()


# ---------------------------------------------------------------------------- engines (the worker's protocol)


class _Replay:
    """`run_motion` after the worker fetched: submit answers the job the provider already ran, poll succeeds, fetch
    hands back the clip from the CAS. Nothing is sent twice."""

    def __init__(self, job: str, data: bytes, output_fps: float | None):
        self.job, self.data, self.output_fps = job, data, output_fps

    def submit(self, payload: Mapping[str, Any], idempotency_key: str) -> str:
        return self.job

    def poll(self, provider_job_id: str) -> PollResult:
        return PollResult("succeeded", result_url=f"{CLIP_URL}{self.job}")

    def fetch(self, result_url: str) -> bytes:
        return self.data


class _VideoEngine:
    """A vendor video engine (Kling or Seedance, or a test's fake) behind the worker, for one run. Submit rebuilds the
    job from the run's spec and re-checks custody; fetch keeps the clip in the CAS as `.mp4` at once and hands the
    worker a small JSON marker naming it (the worker stores that as the run's result)."""

    stub = False

    def __init__(self, runner: MotionRunner, run: Run, inner: Any, cas: CAS):
        self.runner, self.run, self.inner, self.cas = runner, run, inner, cas
        self.idempotent_submit = submit_is_idempotent(inner)
        self.supports_lookup = lookup_fn(inner) is not None

    @property
    def output_fps(self) -> float | None:
        fps = getattr(self.inner, "output_fps", None)
        return float(fps) if isinstance(fps, (int, float)) and fps > 0 else None

    def fault_point(self, state: RunState, phase: str) -> None:
        fp = getattr(self.inner, "fault_point", None)
        if fp is not None:
            fp(state, phase)

    def poll(self, provider_job_id: str) -> PollResult:
        return self.inner.poll(provider_job_id)

    def fetch(self, result_url: str) -> bytes:
        return canonical_json(plain(self.keep(self.inner.fetch(result_url))))

    def cancel(self, provider_job_id: str) -> None:
        self.inner.cancel(provider_job_id)

    def lookup(self, idempotency_key: str) -> str | None:
        fn = lookup_fn(self.inner)
        if fn is None:
            raise NotImplementedError("the inner engine has no lookup")
        return fn(idempotency_key)

    def keep(self, data: bytes) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError


class KlingEngine(_VideoEngine):
    def submit(self, payload: dict[str, Any], idempotency_key: str) -> str:
        self.runner.check_lock(self.run)  # the still may have lost its approval since the quote
        plan = self.runner.kling_plan(self.run)
        self.runner.require_custody(self.run)
        return self.inner.submit(plan.payload(), idempotency_key)

    def keep(self, data: bytes) -> dict[str, Any]:
        return {"clip": self.cas.put_bytes(data, "mp4"), "bytes": len(data), "output_fps": self.output_fps}


class PrevisEngine(_VideoEngine):
    def submit(self, payload: dict[str, Any], idempotency_key: str) -> str:
        p = self.runner.previs_plan(self.run)
        self.runner.require_custody(self.run)
        return self.inner.submit(dict(p.payload), idempotency_key)

    def keep(self, data: bytes) -> dict[str, Any]:
        """The PREVIS watermark is burned in before anything is kept: only the watermarked mp4 enters the CAS."""
        work = self.runner._previs_dir(self.run.project_id, str(self.run.node_id))
        work.mkdir(parents=True, exist_ok=True)
        raw = work / f"{self.run.id}.raw.mp4"
        out = work / f"{self.run.id}.mp4"
        raw.write_bytes(data)
        try:
            wm = previs.burn_watermark(
                raw, out, backend=self.runner.watermark_backend, ffmpeg=self.runner.ffmpeg
            )
        finally:
            raw.unlink(missing_ok=True)  # the clean take is never kept
        try:
            key = self.cas.put_file(out, "mp4")
        finally:
            out.unlink(missing_ok=True)
        return {
            "clip": key,
            "watermark": wm,
            "result_blake3": blake3(data).hexdigest(),
            "result_kept": False,
        }


class PeopleEngine:
    """STAFFAGE as the provider of a People run. `people.run` (stage the still, start the person job, poll it to
    its end, composite under the packet's Z, gate) happens in `submit`, and its result is kept in the CAS as JSON
    that the job id names; poll and fetch only read it back. Never idempotent: a lost submit is a STAFFAGE job
    that may exist, so recovery fails the run for a human rather than paying for a second take."""

    stub = False
    idempotent_submit = False
    supports_lookup = False
    PREFIX = "ppl_"

    def __init__(self, runner: MotionRunner, run: Run, cas: CAS):
        self.runner, self.run, self.cas = runner, run, cas

    def submit(self, payload: dict[str, Any], idempotency_key: str) -> str:
        blob = self.runner.run_people(self.run, self.cas)
        return f"{self.PREFIX}{self.cas.put_bytes(canonical_json(plain(blob)), 'json')}"

    def poll(self, provider_job_id: str) -> PollResult:
        key = provider_job_id.removeprefix(self.PREFIX)
        if not cas_has(self.cas, key):
            return PollResult("failed", detail=f"the People result {provider_job_id} is not in the CAS")
        return PollResult("succeeded", result_url=f"{PEOPLE_URL}{key}")

    def fetch(self, result_url: str) -> bytes:
        return self.cas.read_bytes(result_url.removeprefix(PEOPLE_URL))

    def cancel(self, provider_job_id: str) -> None:
        return None  # the STAFFAGE job had finished before its id was recorded; STAFFAGE has no cancel


class _NoEngine:
    """What `cancel` holds when the run's engine cannot be built: nothing to stop at a provider."""

    def cancel(self, provider_job_id: str) -> None:
        raise NotImplementedError("no engine could be built for this run")


# ---------------------------------------------------------------------------- the runner


class MotionRunner:
    """Quotes and drives Motion, CropPair and People runs. `engines` maps an engine id to a daemon-protocol engine
    (tests, local stubs) that replaces the vendor adapter for execution; the quote is still the manifest's.
    `people_client` is the StaffageClient (or a factory of one) People runs talk to; None builds the default."""

    def __init__(self, pipeline: Any):
        self.pipeline = pipeline
        self.engines: dict[str, Any] = {}
        self.people_client: Any = None
        self.staffage_settings: Any = None
        self.watermark_backend = "auto"
        self.ffmpeg: Any = None
        self._guard = threading.Lock()
        self._engines: dict[str, Any] = {}
        self._plans: OrderedDict[tuple[str, str], Any] = OrderedDict()

    # ------------------------------------------------------------------ graph

    def _graph_node(self, project_id: str, node_id: str) -> tuple[Any, Any, dict[str, Any]]:
        project = self.pipeline.load_project(project_id)
        if project is None:
            raise PipelineError(f"no project {project_id}", code="not_found", status=404)
        g = canvas_rules.as_graph(project.graph)
        node = next((n for n in g.nodes if n.get("id") == node_id), None)
        if node is None:
            raise PipelineError(f"project {project_id} has no node {node_id}", code="not_found", status=404)
        return project, g, node

    def kind_of(self, project_id: str, node_id: str) -> str | None:
        try:
            return self._graph_node(project_id, node_id)[2].get("kind")
        except (PipelineError, ValueError):
            return None

    @staticmethod
    def _upstream_ok(g: Any, node_id: str) -> None:
        """Any canvas rule broken upstream of the node refuses the quote with that rule's code (422)."""
        upstream = canvas_rules.ancestors(g, node_id) | {node_id}
        broken = [v for v in canvas_rules.validate_graph(g) if v["edge"]["target"] in upstream]
        if broken:
            v = broken[0]
            raise PipelineError(v["message"], code=v["code"], violations=broken)

    def _plate(self, project_id: str, g: Any, node_id: str, body: Mapping[str, Any]) -> Any:
        """The PacketView of the plate the node hangs off (the request's `packet_id`, else the nearest Plate
        upstream of `node_id`, which may be the start still rather than the video node)."""
        plates = canvas_rules.upstream_plates(g, node_id)
        packet_id = body.get("packet_id") or next(
            (p.get("packetId") for p in plates if p.get("packetId")), None
        )
        if not isinstance(packet_id, str) or not packet_id:
            raise PipelineError(
                "generation must be anchored to a plate camera (source-view-only)", code="NO_PLATE_ANCHOR"
            )
        return self.pipeline.packet(project_id, packet_id)

    def _still(self, g: Any, node_id: str, pv: Any, cas: CAS, *, locked: bool) -> tuple[str | None, str, Any]:
        """(CAS key or None for the plate itself, source node id, uint16 RGB) of the still feeding the node: the
        upstream node's `outputHash` in this project's CAS, or the plate's display master when a Plate feeds it."""
        source = self._start_source(g, node_id)
        if source is None:
            raise PipelineError(f"node {node_id} has nothing upstream to start from", code="no_still")
        key = source.get("outputHash")
        if cas_has(cas, key):
            return key, source["id"], decode_png16(cas.read_bytes(key))
        if source.get("kind") == "Plate":
            return None, source["id"], pv.plate16()
        msg = f"the still {source['id']} feeding {node_id} has no output in this project's CAS to start from"
        if locked:
            raise kling_job.MotionLocked(msg, node=node_id)
        raise PipelineError(msg, code="no_still")

    @staticmethod
    def _start_source(g: Any, node_id: str) -> dict[str, Any] | None:
        """The node feeding the start still: the first edge on the `image` input, else the first incoming edge that
        is not an end still, a reference or a clip (a Plate wired by its packet handle, as before)."""
        by_id = {n["id"]: n for n in g.nodes}
        ports = edges_by_input(g, node_id)
        edges = ports.get("image") or [e for name, es in ports.items() if name not in NOT_A_STILL for e in es]
        edges = [e for e in edges if (by_id.get(e["source"]) or {}).get("kind") != "Reference"]
        return by_id.get(edges[0]["source"]) if edges else None

    def _end_still(
        self, project_id: str, g: Any, node_id: str, edges: list[dict[str, Any]], pv: Any, cas: CAS
    ) -> dict[str, Any] | None:
        """The still on the Motion node's `end` input: `{key, node, packet_id}`, or None when nothing is wired
        there. Its output must be in the CAS (409 VIDEO_LOCKED naming it otherwise, as for the start) and it must
        come from the start's camera: the same packet, or one with the same `camera.name` and `scene_hash` (422
        `end_frame_camera`)."""
        if not edges:
            return None
        if len(edges) > 1:
            raise PipelineError(
                f"{node_id} has {len(edges)} end stills; a move has one", code="params", node=node_id
            )
        by_id = {n["id"]: n for n in g.nodes}
        source = by_id[edges[0]["source"]]
        sid = str(source["id"])
        key = source.get("outputHash")
        if not cas_has(cas, key):
            raise kling_job.MotionLocked(
                f"the end still {sid} feeding {node_id} has no output in this project's CAS: make and approve it first",
                node=node_id,
                still=sid,
            )
        plates = canvas_rules.upstream_plates(g, sid)
        end_packet = next((p.get("packetId") for p in plates if p.get("packetId")), None)
        if end_packet != pv.packet_id:
            epv = self.pipeline.packet(project_id, end_packet) if isinstance(end_packet, str) else None
            same = epv is not None and _camera_of(epv.manifest) == _camera_of(pv.manifest)
            if not same or _camera_of(pv.manifest) == (None, None):
                raise PipelineError(
                    f"the end still {sid} is from packet {end_packet} and the start from {pv.packet_id}: a move "
                    "between two stills keeps one camera (same packet, or the same camera name and scene hash)",
                    code="end_frame_camera",
                    node=node_id,
                    still=sid,
                    start_packet=pv.packet_id,
                    end_packet=end_packet,
                )
        return {"key": key, "node": sid, "packet_id": end_packet}

    def _elements(
        self, g: Any, node_id: str, edges: list[dict[str, Any]], engine_id: str, cas: CAS
    ) -> list[dict[str, Any]]:
        """The Reference nodes on the `elements` input as `[{name, hash, node}]`, in edge order. Refused before any
        byte is read when the engine takes no elements (422 `elements_unsupported`), then for a reference whose
        picture is not in the project's CAS (422 `reference_missing`)."""
        if not edges:
            return []
        manifest = self.pipeline.manifests.get(engine_id)
        if manifest is not None and not any(i.name == "elements" for i in manifest.inputs):
            raise PipelineError(
                f"{engine_id} takes no elements; Kling O3 4K does", code="elements_unsupported", node=node_id
            )
        by_id = {n["id"]: n for n in g.nodes}
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for i, e in enumerate(edges):
            ref = by_id[e["source"]]
            rp = ref.get("params") if isinstance(ref.get("params"), Mapping) else {}
            key = pick(rp, "referenceHash", "reference_hash") or ref.get("outputHash")
            if not cas_has(cas, key):
                raise PipelineError(
                    f"reference {ref['id']} names no picture in this project's CAS ({key!r}); upload it first",
                    code="reference_missing",
                    node=ref["id"],
                )
            label = ref.get("label") if ref.get("label") not in (None, "", "Reference") else None
            name = str(pick(rp, "name", "elementName", "element_name") or label or f"element{i + 1}").strip()
            base, n = name, 2
            while name in seen:  # the engine addresses elements by name: keep them distinct
                name, n = f"{base}{n}", n + 1
            seen.add(name)
            out.append({"name": name, "hash": key, "node": ref["id"]})
        return out

    def _element_pngs(self, cas: CAS, spec: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The spec's elements as `{name, png}` for the plan, re-encoded for the engine (deterministic)."""
        return [
            {
                "name": el["name"],
                "png": kling_job.element_png(
                    cas.read_bytes(el["hash"]),
                    spec["engine_id"],
                    manifests=self.pipeline.manifests,
                    resolution=spec.get("resolution"),
                ),
            }
            for el in spec.get("elements") or ()
        ]

    @staticmethod
    def _attempt(conn: Any, node_id: str, body: Mapping[str, Any]) -> int:
        last = conn.execute("SELECT MAX(attempt) FROM runs WHERE node_id = ?", (node_id,)).fetchone()[0] or 0
        return max(int(body.get("attempt") or 1), int(last) + 1)

    # ------------------------------------------------------------------ quote

    def quote(
        self,
        conn: Any,
        project_id: str,
        node_id: str,
        body: Mapping[str, Any] | None = None,
        *,
        now: datetime | None = None,
    ) -> tuple[Run, Quote]:
        """A Motion, CropPair or People node -> a QUOTED run. Refusals are raised before any run row exists:
        PipelineError (404 project or node, 409 VIDEO_LOCKED / HIGH_RISK_MOVE / packet not ingested, 422 rules,
        parameters, engine) or CustodyViolation (403)."""
        body = dict(body or {})
        now = now or self.pipeline.clock()
        project, g, node = self._graph_node(project_id, node_id)
        kind = node.get("kind")
        if kind not in KINDS:
            raise PipelineError(f"{kind} nodes are not motion nodes", code="node_kind_unsupported")
        overlay = body.get("params") if isinstance(body.get("params"), Mapping) else {}
        node_params = node.get("params") if isinstance(node.get("params"), Mapping) else {}
        params = {**node_params, **overlay}
        if kind == "People":
            spec, quote, reason = self._quote_people(conn, project, g, node, params, body, now)
        else:
            kling_job.check_unlocked(g, node_id)  # the video lock before anything is cut, routed or priced
            self._upstream_ok(g, node_id)
            if self._is_previs(kind, params, body):
                spec, quote, reason = self._quote_previs(conn, project, g, node, params, body, now)
            else:
                spec, quote, reason = self._quote_kling(conn, project, g, node, params, body, now)
        return self._create_quoted(conn, project_id, spec, quote, reason, now)

    def _is_previs(self, kind: str, params: Mapping[str, Any], body: Mapping[str, Any]) -> bool:
        if params.get("previs") is True or params.get("mode") == "previs":
            return True
        engine_id = body.get("engine_id") or pick(params, "engine_id", "engineId", "engine")
        if engine_id == previs.ENGINE_ID:
            return True
        manifest = self.pipeline.manifests.get(engine_id) if isinstance(engine_id, str) else None
        return manifest is not None and list(manifest.allowed_tasks or []) == [PREVIS_TASK]

    def _quote_kling(
        self,
        conn: Any,
        project: Any,
        g: Any,
        node: dict[str, Any],
        params: dict[str, Any],
        body: Any,
        now: Any,
    ) -> tuple[RunCreate, Quote, str]:
        pl = self.pipeline
        project_id, node_id, kind = project.project_id, str(node["id"]), str(node.get("kind"))
        start = self._start_source(g, node_id)
        pv = self._plate(project_id, g, str(start["id"]) if start else node_id, body)
        cas = pl.cas_for(project_id)
        still_key, still_node, still16 = self._still(g, node_id, pv, cas, locked=True)
        ports = edges_by_input(g, node_id)
        end = self._end_still(project_id, g, node_id, ports.get("end", []), pv, cas)
        engine_id = body.get("engine_id") or pick(params, "engine_id", "engineId", "engine")
        engine_id = str(engine_id or kling_job.DEFAULT_ENGINE)
        elements = self._elements(g, node_id, ports.get("elements", []), engine_id, cas)
        manifest = pl.manifests.get(engine_id)
        declared = {p.name for p in manifest.params} if manifest is not None else set()
        extra = {k: params[k] for k in sorted(declared - kling_job.PLAN_PARAMS) if params.get(k) is not None}
        single = pick(params, "single_image", "singleImage")
        if single is not None and not isinstance(single, bool):
            raise PipelineError(f"single_image must be true or false, got {single!r}", code="params")
        if single and end is not None:
            raise PipelineError(
                "single_image sends the start frame alone; unwire the end still or drop single_image",
                code="params",
            )
        move_params: dict[str, Any] = {}
        for name, spellings in MOVE_DIALS.items():
            value = _number(pick(params, *spellings), name)
            if value is not None:
                move_params[name] = value
        override = params.get("override") or node.get("overrides")
        if override:
            move_params["override"] = plain(override)
        # The render-it router on the camera path the artist rendered, when a motion packet starts at this plate:
        # in blocking mode a render_move verdict blocks the move whatever the override (move_policy).
        router = None
        motion = motion_packet_for(
            pl, project, pv.packet_id, pick(params, "motion_packet_id", "motionPacketId")
        )
        if motion is not None:
            try:
                verdict, _source, _mp = router_card(pl, project_id, pv, motion_packet_id=motion.packet_id)
                router = verdict.model_dump(mode="json")
            except Exception as e:  # the router informs the policy; it is never why a quote cannot be made
                log.warning("router verdict for %s/%s not measured: %s", project_id, node_id, e)
        cx, cy = _number(pick(params, "cx"), "cx"), _number(pick(params, "cy"), "cy")
        cx1, cy1 = _number(pick(params, "cx1", "cxEnd"), "cx1"), _number(pick(params, "cy1", "cyEnd"), "cy1")
        z0 = _number(pick(params, "z0", "zoomStart", "zoom_start"), "z0")
        z1 = _number(pick(params, "z1", "zoomEnd", "zoom_end"), "z1")
        seed = pick(params, "seed")
        default_recipe = kling_job.TWO_STILLS_RECIPE if end is not None else kling_job.DEFAULT_RECIPE
        motion_spec = {
            "flavour": "kling",
            "kind": kind,
            "still": still_key,
            "still_node": still_node,
            "end_still": end["key"] if end else None,
            "end_node": end["node"] if end else None,
            "end_packet": end["packet_id"] if end else None,
            "elements": elements,
            "extra_params": plain(extra),
            "single_image": single,
            "recipe": str(pick(params, "recipe") or body.get("recipe") or default_recipe),
            "centre": [cx, cy] if cx is not None and cy is not None else None,
            "centre_end": [cx1, cy1] if cx1 is not None and cy1 is not None else None,
            "zoom": [z0, z1] if z0 is not None and z1 is not None else None,
            "engine_id": engine_id,
            "move_kind": pick(params, "move", "move_kind", "moveKind"),
            "move_params": move_params,
            "shot_s": _number(pick(params, "duration_s", "durationS", "duration", "shot_s"), "duration"),
            "prompt": body.get("prompt") or pick(params, "prompt"),
            "negative_prompt": pick(params, "negative_prompt", "negativePrompt"),
            "seed": int(seed) if isinstance(seed, (int, float)) and not isinstance(seed, bool) else None,
            "resolution": pick(params, "resolution"),
            "router": router,
        }
        plan = self._plan_kling(project, pv, node_id, motion_spec, still16)
        allowed = pl.allowed_grades(project_id, pv.manifest)
        route_id, prefer_provider = _route_choice(project, body, engine_id)
        route, quote = kling_job.quote_motion(
            plan, g, pl.manifests, allowed, route_id=route_id, prefer_provider=prefer_provider, now=now
        )
        motion_spec["route_reasons"] = list(route.get("reasons") or [])
        cas.put_bytes(plan.start_png, "png")
        if plan.end_png is not None:
            cas.put_bytes(plan.end_png, "png")
        spec = kling_job.to_run_create(
            plan, project_id, pv.packet_id, route, attempt=self._attempt(conn, node_id, body)
        )
        inputs = {
            **spec.inputs,
            "plate": pv.plate_hash,
            "still": still_key or pv.plate_hash,
            "start_hash": plan.start_hash,
            "end_hash": plan.end_hash,
            STAGING_KEY: {"node_kind": kind, "motion": True, "region_crop": False},
            SPEC_KEY: plain(motion_spec),
        }
        if end is not None:
            inputs["end_still"] = end["key"]
        spec = spec.model_copy(
            update={"inputs": inputs, "priority": Priority.parse(body.get("priority") or "interactive")}
        )
        pair = kling_job.pair_summary(plan.pair)
        how = "two stills" if pair["mode"] == "two_stills" else f"{pair['direction']} crop pair"
        if plan.single_image:
            how += ", single image"
        reason = (
            f"{kind} on {quote.provider}/{quote.model} {quote.region} {quote.custody_grade}, "
            f"{plan.duration_s} s, {quote.price_usd:.4f} USD ({plan.move['risk']} risk move, {how})"
        )
        return spec, quote, reason

    def _plan_kling(
        self, project: Any, pv: Any, node_id: str, spec: Mapping[str, Any], still16: Any
    ) -> kling_job.MotionPlan:
        cas = self.pipeline.cas_for(project.project_id)
        kwargs: dict[str, Any] = {
            "recipe": spec.get("recipe") or kling_job.DEFAULT_RECIPE,
            "node_id": node_id,
            "engine_id": spec["engine_id"],
            "manifests": self.pipeline.manifests,
            "sheet": _sheet(project),
            "move_params": dict(spec.get("move_params") or {}),
            "router_verdict": spec.get("router"),
            "tier": pv.tier,
            "extra_params": dict(spec.get("extra_params") or {}),
        }
        for name in ("prompt", "negative_prompt", "seed", "resolution", "move_kind", "shot_s"):
            if spec.get(name) is not None:
                kwargs[name] = spec[name]
        try:
            kwargs["elements"] = self._element_pngs(cas, spec)
            if spec.get("end_still"):  # two approved stills of one camera
                end16 = decode_png16(cas.read_bytes(spec["end_still"]))
                return kling_job.plan_two_stills(still16, end16, **kwargs)
            if spec.get("centre"):
                kwargs["centre"] = list(spec["centre"])
            if spec.get("centre_end"):
                kwargs["centre_end"] = list(spec["centre_end"])
            if spec.get("zoom"):
                kwargs["zoom"] = (float(spec["zoom"][0]), float(spec["zoom"][1]))
            if spec.get("single_image") is not None:
                kwargs["single_image"] = bool(spec["single_image"])
            return kling_job.plan_from_still(still16, **kwargs)
        except kling_job.MotionBlocked as e:
            raise _blocked(e) from e
        except PipelineError:
            raise
        except RecipeError as e:
            raise PipelineError(str(e), code="unknown_move") from e
        except ValueError as e:  # a malformed dial, zoom pair or centre
            raise PipelineError(str(e), code="params") from e

    def _quote_previs(
        self,
        conn: Any,
        project: Any,
        g: Any,
        node: dict[str, Any],
        params: dict[str, Any],
        body: Any,
        now: Any,
    ) -> tuple[RunCreate, Quote, str]:
        pl = self.pipeline
        project_id, node_id, kind = project.project_id, str(node["id"]), str(node.get("kind"))
        if kind != "Motion":
            raise PipelineError(f"previs is a Motion node's take, not a {kind}'s", code="node_kind")
        quality = str(pick(params, "quality") or "draft")
        engine_id = str(
            body.get("engine_id") or pick(params, "engine_id", "engineId", "engine") or previs.ENGINE_ID
        )
        manifest = pl.manifests.get(engine_id)
        if manifest is None:
            raise PipelineError(f"unknown engine {engine_id!r}", code="unknown_engine")
        ctx = previs._context(project, node_id)  # the lock again, the motion packet, the allowed grades
        route_id, prefer_provider = _route_choice(project, body, engine_id)
        route = kling_job.pick_route(  # custody before a byte, then the preference on the hero still's frame
            manifest,
            ctx["allowed"],
            route_id,
            prefer_provider=prefer_provider,
            frame=previs._hero_frame(pl.cas_for(project_id), ctx),
            tier=previs.QUALITY_RESOLUTION.get(quality),
        )
        p = previs.plan(
            project,
            node_id,
            quality,
            route=route,
            engine_id=engine_id,
            work_dir=self._previs_dir(project_id, node_id),
            cas=pl.cas_for(project_id),
            ffmpeg=self.ffmpeg,
            _ctx=ctx,
        )
        quote = previs.quote(p, pl.manifests, now=now)
        motion_spec = {
            "flavour": "previs",
            "kind": kind,
            "quality": quality,
            "engine_id": engine_id,
            "motion_packet_id": p.motion_packet_id,
            "hero_packet_id": p.hero_packet_id,
            "hero_node_id": p.hero_node_id,
            "tier": pl.packet(project_id, p.hero_packet_id).tier,
            "record": p.record(),  # what the take records, kept so NORMALIZING needs no second plan
            "route_reasons": list(route.get("reasons") or []),
        }
        inputs = {
            "previs": True,
            "reference_images": p.input_keys["reference_images"],
            "reference_videos": p.input_keys["reference_videos"],
            "prompt": p.prompt,
            "plate": pl.packet(project_id, p.hero_packet_id).plate_hash,
            "motion": {"task": PREVIS_TASK, "shot_class": previs.SHOT_CLASS, "timemap": p.timemap.to_json()},
            STAGING_KEY: {"node_kind": kind, "motion": True, "previs": True, "region_crop": False},
            SPEC_KEY: motion_spec,
        }
        spec = RunCreate(
            project_id=project_id,
            packet_id=p.hero_packet_id,
            node_id=node_id,
            priority=Priority.parse(body.get("priority") or "interactive"),
            inputs=plain(inputs),
            params=p.params,
            route=route,
            attempt=self._attempt(conn, node_id, body),
        )
        reason = (
            f"previs ({quality}, {p.resolution}) on {quote.provider}/{quote.model} {quote.custody_grade}, "
            f"{p.duration_s} s, {quote.price_usd:.4f} USD; watermarked PREVIS, never delivered"
        )
        return spec, quote, reason

    def _quote_people(
        self,
        conn: Any,
        project: Any,
        g: Any,
        node: dict[str, Any],
        params: dict[str, Any],
        body: Any,
        now: Any,
    ) -> tuple[RunCreate, Quote, str]:
        pl = self.pipeline
        project_id, node_id = project.project_id, str(node["id"])
        self._upstream_ok(g, node_id)
        pv = self._plate(project_id, g, node_id, body)
        settings = self._staffage_settings()
        route = self._people_route(settings)
        allowed = pl.allowed_grades(project_id, pv.manifest)
        custody.require_route(route, allowed, what="People node (STAFFAGE)")  # NDA -> CustodyViolation, first
        snapshot = {"id": node_id, "kind": "People", "params": plain(params)}
        try:
            people.parse_params(snapshot)
        except people.PeopleError as e:
            raise PipelineError(str(e), code=e.code, status=e.status, **plain(e.extra)) from e
        cas = pl.cas_for(project_id)
        still_key, still_node, _still16 = self._still(g, node_id, pv, cas, locked=False)
        shot_s = (
            _number(pick(params, "duration_s", "durationS", "duration", "shot_s"), "duration")
            or PEOPLE_SHOT_S
        )
        price = round(PEOPLE_USD_PER_S * shot_s, 6)
        quote = Quote(
            provider=route["provider"],
            model=PEOPLE_ENGINE_ID,
            model_version=route["model_version"],
            region=route["region"],
            custody_grade=custody.effective_grade(route).value,
            price_usd=price,
            eta_s=PEOPLE_ETA_S,
            created_utc=now,
            expires_utc=now + QUOTE_TTL,
            route_id=route["route_id"],
            price_status="unconfirmed",
        )
        inputs = {
            "plate": pv.plate_hash,
            "still": still_key or pv.plate_hash,
            "motion": {"task": PEOPLE_TASK, "shot_class": "people"},
            STAGING_KEY: {"node_kind": "People", "motion": True, "region_crop": False},
            SPEC_KEY: {
                "flavour": "people",
                "kind": "People",
                "still": still_key,
                "still_node": still_node,
                "node": snapshot,
                "shot_s": shot_s,
            },
        }
        spec = RunCreate(
            project_id=project_id,
            packet_id=pv.packet_id,
            node_id=node_id,
            priority=Priority.parse(body.get("priority") or "interactive"),
            inputs=plain(inputs),
            params={"duration": shot_s},
            route=route,
            attempt=self._attempt(conn, node_id, body),
        )
        reason = (
            f"People over STAFFAGE ({quote.custody_grade}), about {price:.4f} USD for a {shot_s:g} s take"
        )
        return spec, quote, reason

    def _staffage_settings(self) -> Any:
        return self.staffage_settings or people.StaffageSettings.load(self.pipeline.settings)

    @staticmethod
    def _people_route(settings: Any) -> dict[str, Any]:
        r = settings.route()
        return {
            "engine_id": PEOPLE_ENGINE_ID,
            "provider": r["provider"],
            "region": "local",
            "route_id": r["route_id"],
            "custody_grade": r["custody_grade"],
            "custody_status": r["custody_status"],
            "upload_visibility": r["upload_visibility"],
            "model_version": "staffage",
        }

    def _create_quoted(
        self, conn: Any, project_id: str, spec: RunCreate, quote: Quote, reason: str, now: datetime
    ) -> tuple[Run, Quote]:
        pl = self.pipeline
        run = create_run(conn, spec, now=now)
        pl.index.remember(run.id, project_id)
        quote = quote.model_copy(update={"run_id": run.id})
        with tx(conn):
            insert_quote(conn, quote)
            run = transition(
                conn,
                run.id,
                S.QUOTED,
                actor="system",
                now=now,
                reason=reason[:500],
                quote_id=quote.id,
                quote_json=quote.terms_json(),
            )
        self._after(conn, project_id, run.id, S.DRAFT)
        return run, quote

    # ------------------------------------------------------------------ custody

    def allowed_for(self, run: Run) -> set[Any]:
        """Project custody ∩ the run's packet (∩ the motion packet of a previs run)."""
        pl = self.pipeline
        allowed = pl.allowed_grades(run.project_id, pl.packet(run.project_id, run.packet_id).manifest)
        motion_id = (run.inputs.get(SPEC_KEY) or {}).get("motion_packet_id")
        if motion_id and motion_id != run.packet_id:
            allowed &= pl.allowed_grades(run.project_id, pl.packet(run.project_id, motion_id).manifest)
        return allowed

    def require_custody(self, run: Run) -> Any:
        return custody.require_route(run.route_dict, self.allowed_for(run), what=run.route.engine_id)

    def check_lock(self, run: Run) -> None:
        """The video lock on the graph as it is now (409 VIDEO_LOCKED), checked again before a submit."""
        project = self.pipeline.load_project(run.project_id)
        if project is None:
            raise PipelineError(f"no project {run.project_id}", code="not_found", status=404)
        kling_job.check_unlocked(project.graph, str(run.node_id))

    # ------------------------------------------------------------------ rebuilding the job at execution

    def _cached(self, key: tuple[str, str]) -> Any:
        with self._guard:
            hit = self._plans.get(key)
            if hit is not None:
                self._plans.move_to_end(key)
            return hit

    def _cache(self, key: tuple[str, str], value: Any) -> Any:
        with self._guard:
            self._plans[key] = value
            while len(self._plans) > PLAN_CACHE:
                self._plans.popitem(last=False)
        return value

    def kling_plan(self, run: Run) -> kling_job.MotionPlan:
        """The run's MotionPlan, re-cut from the approved still it was quoted on (deterministic). A still that
        changed since the quote is refused: what was priced and approved is what is sent."""
        hit = self._cached(("kling", run.id))
        if hit is not None:
            return hit
        pl = self.pipeline
        spec = dict(run.inputs[SPEC_KEY])
        project = pl.load_project(run.project_id)
        if project is None:
            raise PipelineError(f"no project {run.project_id}", code="not_found", status=404)
        pv = pl.packet(run.project_id, run.packet_id)
        cas = pl.cas_for(run.project_id)
        still16 = decode_png16(cas.read_bytes(spec["still"])) if spec.get("still") else pv.plate16()
        plan = self._plan_kling(project, pv, str(run.node_id), spec, still16)
        if (plan.start_hash, plan.end_hash) != (run.inputs.get("image"), run.inputs.get("image_tail")):
            raise PipelineError(
                f"run {run.id}: the start and end frames no longer match the quote; re-quote the node",
                code="inputs_changed",
            )
        return self._cache(("kling", run.id), plan)

    def _previs_dir(self, project_id: str, node_id: str) -> Path:
        tag = blake3(node_id.encode("utf-8")).hexdigest()[:16]
        return self.pipeline.settings.home / "cache" / "previs" / project_id / tag

    def previs_plan(self, run: Run) -> Any:
        """The run's PrevisPlan for its submit, re-planned on the current graph (the video lock is checked again and
        the refs and the clay reference are staged again; the payload names them by path). Refs that are not the
        ones quoted are refused."""
        hit = self._cached(("previs", run.id))
        if hit is not None:
            return hit
        pl = self.pipeline
        spec = dict(run.inputs[SPEC_KEY])
        project = pl.load_project(run.project_id)
        if project is None:
            raise PipelineError(f"no project {run.project_id}", code="not_found", status=404)
        node_id = str(run.node_id)
        ctx = previs._context(project, node_id)
        p = previs.plan(
            project,
            node_id,
            str(spec.get("quality") or "draft"),
            route=run.route_dict,
            engine_id=run.route.engine_id,
            work_dir=self._previs_dir(run.project_id, node_id),
            cas=pl.cas_for(run.project_id),
            ffmpeg=self.ffmpeg,
            _ctx=ctx,
        )
        if p.motion_packet_id != spec.get("motion_packet_id") or plain(
            p.input_keys["reference_images"]
        ) != plain(run.inputs.get("reference_images")):
            raise PipelineError(
                f"run {run.id}: the previs references changed since the quote; re-quote the node",
                code="inputs_changed",
            )
        return self._cache(("previs", run.id), p)

    def run_people(self, run: Run, cas: CAS) -> dict[str, Any]:
        """`people.run` for a People run (custody re-checked first); the facts kept as the run's result."""
        pl = self.pipeline
        self.require_custody(run)
        spec = dict(run.inputs[SPEC_KEY])
        project = pl.load_project(run.project_id)
        if project is None:
            raise PipelineError(f"no project {run.project_id}", code="not_found", status=404)
        pv = pl.packet(run.project_id, run.packet_id)
        still16 = decode_png16(cas.read_bytes(spec["still"])) if spec.get("still") else pv.plate16()
        client = self.people_client() if callable(self.people_client) else self.people_client
        from .outbound_log import OutboundLog

        result = people.run(
            project,
            dict(spec["node"]),
            pv,
            still16,
            client=client,
            settings=self._staffage_settings(),
            run_id=run.id,
            log=OutboundLog.for_project(project.root),
            sleep=pl.sleep,
        )
        report = result.report.model_dump(mode="json") if result.report is not None else None
        return {"summary": result.summary(), "assets": dict(result.assets), "report": report}

    # ------------------------------------------------------------------ driving

    def _inner(self, run: Run) -> Any:
        """The vendor engine of a Kling or previs run: an injected one by engine id, else the manifest's adapter on
        the project's egress (allowlist, metadata strip, leak scan, outbound log)."""
        pl = self.pipeline
        eid = run.route.engine_id
        if eid in self.engines:
            return self.engines[eid]
        if eid in pl.engines:
            return pl.engines[eid]
        manifest = pl.manifests.get(eid)
        if manifest is None:
            raise PipelineError(f"unknown engine {eid!r}", code="unknown_engine")
        rid = run.route_dict.get("route_id")
        route = next((r for r in manifest.routes if r.route_id == rid), None)
        if route is None:
            raise PipelineError(f"{eid} has no route {rid!r}", code="unknown_route")
        staging = pl.settings.home / "cache" / "adapters" / run.id
        project = pl.load_project(run.project_id)
        if flavour_of(run) == "previs":
            motion = pl.packet(run.project_id, run.inputs[SPEC_KEY]["motion_packet_id"])
            return previs.seedance_engine(
                project, run.id, route, manifest, motion, settings=pl.settings, staging_dir=staging
            )
        cls = adapter_class(manifest, route)
        if cls is None:
            raise PipelineError(f"no adapter speaks {eid} via {rid}", code="no_adapter")
        transport = None
        if route.upload_visibility != "local":
            from .egress import EgressRun

            pv = pl.packet(run.project_id, run.packet_id)
            erun = EgressRun.for_project(
                project.doc if project else {"project_id": run.project_id},
                run.id,
                packet=pv.manifest,
                legend=pv.legend,
                packet_dir=pv.root,
            )
            transport = pl.egress_for(run.project_id).transport(erun, route)
        return kling_job.AdapterEngine(cls(manifest, route, transport=transport), staging)

    def engine_for(self, run: Run, cas: CAS | None = None) -> Any:
        with self._guard:
            hit = self._engines.get(run.id)
        if hit is not None:
            return hit
        cas = cas or self.pipeline.cas_for(run.project_id)
        flavour = flavour_of(run)
        if flavour == "people":
            engine: Any = PeopleEngine(self, run, cas)
        elif flavour == "previs":
            engine = PrevisEngine(self, run, self._inner(run), cas)
        else:
            engine = KlingEngine(self, run, self._inner(run), cas)
        with self._guard:
            self._engines[run.id] = engine
        return engine

    def _forget(self, run_id: str) -> None:
        with self._guard:
            self._engines.pop(run_id, None)
            for key in [k for k in self._plans if k[1] == run_id]:
                self._plans.pop(key, None)

    def step(self, conn: Any, project_id: str, run_id: str, cas: CAS | None = None) -> RunState:
        """One worker transition with this module's engine, normalize and gate; published and ledgered."""
        pl = self.pipeline
        cas = cas or pl.cas_for(project_id)
        run = get_run(conn, run_id)
        before = run.state
        if before in TERMINAL or before not in WORKER_STATES:
            return before
        try:
            engine = self.engine_for(run, cas)
        except (PipelineError, CustodyViolation) as e:  # no engine could be built: nothing was sent
            to = S.CANCELLED if before is S.QUEUED else S.FAILED
            code = getattr(e, "code", None) or "no_engine"
            transition(conn, run_id, to, actor="worker", reason=str(e)[:500], reason_code=code)
            self._after(conn, project_id, run_id, before)
            self._forget(run_id)
            return to
        after = worker_step(
            conn,
            engine,
            run_id,
            pl.clock,
            cas=cas,
            poll_timeout_s=pl.poll_timeout_s,
            normalize_fn=self.normalize,
            gate_fn=self.gate,
        )
        if after is not before:
            self._after(conn, project_id, run_id, before)
        if after in TERMINAL:
            self._forget(run_id)
        return after

    def cancel(
        self, conn: Any, project_id: str, run_id: str, *, actor: str = "human", reason: str | None = None
    ) -> Run:
        """Cancel a run; one already at the provider gets exactly one cancel call on its engine first."""
        run = get_run(conn, run_id)
        before = run.state
        try:
            engine: Any = self.engine_for(run, self.pipeline.cas_for(project_id))
        except (PipelineError, CustodyViolation):
            engine = _NoEngine()
        out = cancel_run(conn, engine, run_id, actor=actor, reason=reason)
        self._after(conn, project_id, run_id, before)
        self._forget(run_id)
        return out

    # ------------------------------------------------------------------ NORMALIZING and GATING

    def adapter_notes(self, run: Run) -> list[str]:
        """What the run's vendor adapter did that the plan did not say, by its task id (the Runway adapter's
        `notes(job)`: a duration snap, reference roles); [] for an adapter that keeps none, an injected engine, or
        a daemon restarted since the submit (the notes live in the adapter)."""
        with self._guard:
            engine = self._engines.get(run.id)
        adapter = getattr(getattr(engine, "inner", None), "adapter", None)
        fn = getattr(adapter, "notes", None)
        if not callable(fn) or not run.provider_job_id:
            return []
        try:
            return [str(n) for n in fn(str(run.provider_job_id)) or ()]
        except Exception as e:  # notes explain a take; they never fail one
            log.warning("adapter notes for %s not read: %s", run.id, e)
            return []

    def normalize(self, run: Run, cas: CAS) -> dict[str, Any]:
        flavour = flavour_of(run)
        if flavour == "people":
            return self._normalize_people(run, cas)
        marker = json.loads(cas.read_bytes(run.result_key).decode("utf-8"))
        if flavour == "previs":
            return self._normalize_previs(run, cas, marker)
        return self._normalize_kling(run, cas, marker)

    def _normalize_kling(self, run: Run, cas: CAS, marker: Mapping[str, Any]) -> dict[str, Any]:
        plan = self.kling_plan(run)
        clip = cas.read_bytes(marker["clip"])
        res = kling_job.run_motion(
            plan,
            _Replay(str(run.provider_job_id), clip, marker.get("output_fps")),
            cas,
            route=run.route_dict,
            attempt=run.attempt,
            sleep=lambda _s: None,
            poll_interval_s=0.0,
        )
        record = res.record(plan)
        record.pop("gate", None)
        return plain(
            {
                "outputs": [k for k in (res.video_key, res.start_key, res.end_key) if k is not None],
                "clip": res.video_key,
                "start": res.start_key,
                "end": res.end_key,
                "timemap": res.timemap.to_json(),
                "timemap_source": res.timemap_source,
                "motion": record,
                "notes": [*res.notes, *self.adapter_notes(run)],
                "gate_key": cas.put_bytes(canonical_json(plain(res.gate_report)), "json"),
            }
        )

    def _normalize_previs(self, run: Run, cas: CAS, marker: Mapping[str, Any]) -> dict[str, Any]:
        spec = run.inputs[SPEC_KEY]
        record = {
            **dict(spec.get("record") or {}),
            "run_id": run.id,
            "output_key": marker["clip"],
            "watermark": marker.get("watermark"),
            "result_blake3": marker.get("result_blake3"),
            "provider_job_id": run.provider_job_id,
            "engine_fps": float(previs.ENGINE_FPS),
        }
        marked = True
        project = self.pipeline.load_project(run.project_id)
        try:
            previs._mark_node(project, str(run.node_id), run.id, marker["clip"], str(spec.get("quality")))
        except Exception as e:  # the run carries previs: true either way; delivery reads the run
            marked = False
            log.warning("motion node %s not marked previs: %s", run.node_id, e)
        out: dict[str, Any] = {
            "outputs": [marker["clip"]],
            "clip": marker["clip"],
            "previs": True,
            "timemap": record.get("timemap"),
            "watermark": marker.get("watermark"),
            "previs_record": record,
            "node_marked": marked,
        }
        notes = self.adapter_notes(run)
        if notes:
            out["notes"] = notes
        return plain(out)

    def _normalize_people(self, run: Run, cas: CAS) -> dict[str, Any]:
        blob = json.loads(cas.read_bytes(run.result_key).decode("utf-8"))
        assets = dict(blob.get("assets") or {})
        if not cas_has(cas, assets.get("output")):
            raise PipelineError("STAFFAGE's composite is not in the CAS", code="normalize_error")
        out: dict[str, Any] = {
            "outputs": [assets["output"]],
            "people": blob.get("summary") or {},
            "assets": assets,
        }
        if blob.get("report") is not None:
            out["gate_key"] = cas.put_bytes(canonical_json(blob["report"]), "json")
        return plain(out)

    def _previs_report(self, run: Run) -> dict[str, Any]:
        from plate_gates import thresholds
        from plate_gates.report import EngineRef, GateReport

        norm = run.normalized or {}
        report = GateReport(
            threshold_set_id=thresholds.load().id,
            engine=EngineRef(
                id=run.route.engine_id, version=str(run.route_dict.get("model_version") or "") or None
            ),
            tier=(run.inputs.get(SPEC_KEY) or {}).get("tier") or "A",
            metrics={"previs": True, "watermark": norm.get("watermark"), "timemap": norm.get("timemap")},
            checks=[],
            backend_notes=[
                "previs: watermarked PREVIS and never delivered; no fidelity gate scores a previs take"
            ],
        )
        return report.model_dump(mode="json")

    def gate(self, run: Run, cas: CAS) -> GateOutcome:
        """GATING: the stored report (video gates, People's still gates, or the previs record), versioned in the
        project's gates/ folder. An unmeasured take never passes."""
        pl = self.pipeline
        norm = run.normalized or {}
        flavour = flavour_of(run)
        if flavour == "previs":
            report = self._previs_report(run)
        elif norm.get("gate_key"):
            report = json.loads(cas.read_bytes(norm["gate_key"]).decode("utf-8"))
        else:
            report = {"verdict": "fail", "metrics": {}, "checks": [], "backend_notes": ["no gate report"]}
        project_dir = pl.project_dir(run.project_id)
        svc = pl.report_service(run.project_id)
        ref = None
        if svc is not None:
            path = svc.write(run.id, report)
            try:
                ref = Path(path).relative_to(project_dir).as_posix()
            except ValueError:
                ref = Path(path).as_posix()
        out = dict(report)
        if flavour == "kling":
            out["landing"] = (report.get("metrics") or {}).get("landing")
            out["timemap"] = norm.get("timemap")
        if flavour == "previs":
            out["previs"] = True
        return GateOutcome(passed=report.get("verdict") != "fail", report=out, report_path=ref)

    # ------------------------------------------------------------------ bus and ledger

    def _ledger_data(self, run: Run) -> dict[str, Any]:
        motion = run.inputs.get("motion") if isinstance(run.inputs.get("motion"), Mapping) else {}
        data: dict[str, Any] = {
            "model": run.route_dict.get("model_version") or run.route.engine_id,
            "task": motion.get("task"),
            "shot_class": motion.get("shot_class"),
            "node_kind": (run.inputs.get(STAGING_KEY) or {}).get("node_kind"),
        }
        if flavour_of(run) == "previs":
            data["previs"] = True
        elements = run.inputs.get("elements")
        if isinstance(elements, list) and elements:
            data["elements"] = [e.get("hash") for e in elements if isinstance(e, Mapping)]
        return {k: v for k, v in data.items() if v is not None}

    def _after(
        self, conn: Any, project_id: str, run_id: str, before: RunState, *, note: str | None = None
    ) -> None:
        """`Pipeline.after` for these runs: the `run.state` event, and the ledger record with the motion fields."""
        pl = self.pipeline
        run = get_run(conn, run_id)
        event = {"kind": "run.state", "run_id": run_id, "from": before.value, "to": run.state.value}
        if run.reason_code:
            event["reason_code"] = run.reason_code
        pl.publish(project_id, run_id, event)
        kind = LEDGER_KINDS.get(run.state)
        if kind is not None:
            pl.ledger(project_id, kind, run, note=note, data=self._ledger_data(run))


# ---------------------------------------------------------------------------- wiring into a Pipeline


def install(pipeline: Any) -> MotionRunner:
    """Let `pipeline` quote and drive Motion, CropPair and People nodes (idempotent; returns the runner, kept at
    `pipeline.motion_runner`). Wraps `quote_node`, `approve`, `step`, `engine_for` and `cancel` on this instance and
    leaves every other node and run to what was there before (the originals, or another install's wrappers).
    Install before the daemon's boot-time `resume` starts, as `nodes.install` says."""
    existing = getattr(pipeline, "motion_runner", None)
    if existing is not None:
        return existing
    runner = MotionRunner(pipeline)
    pipeline.motion_runner = runner
    orig_quote, orig_step, orig_engine_for = pipeline.quote_node, pipeline.step, pipeline.engine_for
    orig_approve, orig_cancel = pipeline.approve, pipeline.cancel

    def quote_node(
        conn: Any, project_id: str, node_id: str, body: Mapping[str, Any], *, now: datetime | None = None
    ) -> Any:
        if runner.kind_of(project_id, node_id) in KINDS:
            return runner.quote(conn, project_id, node_id, body, now=now)
        return orig_quote(conn, project_id, node_id, body, now=now)

    def approve(
        conn: Any,
        project_id: str,
        run_id: str,
        by: str,
        envelope_id: str | None = None,
        *,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> list[Run]:
        if is_motion_run(get_run(conn, run_id)):
            runner.require_custody(get_run(conn, run_id))  # the project's custody may have tightened
        return orig_approve(conn, project_id, run_id, by, envelope_id, reason=reason, now=now)

    def step(conn: Any, project_id: str, run_id: str, cas: CAS | None = None) -> RunState:
        if is_motion_run(get_run(conn, run_id)):
            return runner.step(conn, project_id, run_id, cas)
        return orig_step(conn, project_id, run_id, cas)

    def engine_for(run: Run, cas: CAS) -> Any:
        return runner.engine_for(run, cas) if is_motion_run(run) else orig_engine_for(run, cas)

    def cancel(
        conn: Any, project_id: str, run_id: str, *, actor: str = "human", reason: str | None = None
    ) -> Run:
        if is_motion_run(get_run(conn, run_id)):
            return runner.cancel(conn, project_id, run_id, actor=actor, reason=reason)
        return orig_cancel(conn, project_id, run_id, actor=actor, reason=reason)

    pipeline.quote_node, pipeline.approve, pipeline.step = quote_node, approve, step
    pipeline.engine_for, pipeline.cancel = engine_for, cancel
    return runner
