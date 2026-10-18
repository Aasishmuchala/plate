"""Recomposite: a free re-mask of a stored raw take (router plan T99, decision 54).

An Edit take keeps what the engine sent back (`normalized.raw_key`), the size map and the egress PNG it was staged
with (`normalized.sizemap`, `normalized.egress_key`). Putting that raw take through another mask, another feather
or a grown or shrunk mask is a new run that costs nothing: no engine is called, the raw bytes are read from the
project's CAS, and the pipeline normalizes and gates the result exactly as it does for a vendor take. Because the
size map is the parent's (T92's pinned size map), a region crop lands where it did, so a bigger or smaller mask
re-composites the same pixels the engine produced.

    body.params.recomposite_of = <run id>      on an Edit node's quote selects this module
    body.params.grow_px        = -32..32       dilate (+) or erode (-) the editable mask, elliptical kernel
    body.params.mask_hash      = <CAS key>     a plate-sized grey PNG that replaces the node's mask
    body.feather_px            = 4..12         inside-the-mask feather; the parent's when left out

`install(pipeline)` (idempotent; `pipeline.recomposite_runner`) wraps `quote_node`, `approve` and `engine_for` on
that Pipeline instance, the way `nodes.runner.install` does, and leaves every other node and run to the originals.
Install it right after the Pipeline is built, before the boot-time `resume`. `POST /runs/{id}/recomposite`
(`routes/recomposite.py`) drives the same runner directly.

A recomposite run is a normal pipeline run with these differences:

- `inputs` are the parent's, with `mask` read again from the graph (or `mask_hash`, then grown or shrunk),
  `recomposite_of`, the parent's raw take as `recomposite_raw`, and `inputs.staging` carrying the parent's
  `sizemap` and `png_key` (`pinned_egress` stages from them) and the feather. `reference_images` are dropped
  (nothing leaves the box, and the `submitted` ledger record must not say otherwise) and so are the staging's
  `task` and `shot_class`: the engine cards count a run as a sample of its engine, and a free replay is not one.
- The route is the parent's with `provider "local-recomposite"`, `route_id "recomposite"`, custody C0 (confirmed)
  and `upload_visibility "local"`; the quote is 0 USD, `price_status "confirmed"`. Approval needs no budget
  decision; custody is still checked.
- `ReplayEngine` is the worker's engine: `submit` answers `recomp:<run id>`, `poll` is done, `fetch` returns the
  parent's raw bytes from the CAS (the same key, so `normalized.raw_key` stays the original take and a
  recomposite can be re-composited again).

The attempt is numbered by `Pipeline.next_attempt` (decision 40), so the same request twice is attempt 1 and 2.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np

from . import canvas_rules
from .adapter import PollResult
from .cas import CAS
from .db import tx
from .machine import RunNotFound, create_run, get_run, transition
from .models import QUOTE_TTL, Quote, Run, RunCreate
from .pipeline import (
    DEFAULT_FEATHER_PX,
    STAGING_KEY,
    SUBGRAPH_KEY,
    PipelineError,
    is_pipeline_run,
    subgraph_snapshot,
)
from .quotes import insert_quote
from .states import Priority, RunState

__all__ = [
    "ENGINE_URL",
    "FEATHER_RANGE",
    "GROW_PARAM",
    "GROW_RANGE",
    "MASK_PARAM",
    "PARAM",
    "PROVIDER",
    "RAW_KEY",
    "ROUTE_ID",
    "Recomposer",
    "ReplayEngine",
    "grow_mask",
    "install",
    "is_recomposite",
    "recomposite_route",
]

log = logging.getLogger(__name__)
S = RunState

PARAM = "recomposite_of"  # body.params and run.inputs: the parent run's id
GROW_PARAM = "grow_px"
MASK_PARAM = "mask_hash"
RAW_KEY = "recomposite_raw"  # run.inputs: the parent's raw take, so the engine needs no other lookup
PROVIDER = "local-recomposite"
ROUTE_ID = "recomposite"
MODEL = "recomposite"
ENGINE_URL = "plate-recomposite://"
JOB_PREFIX = "recomp:"
FEATHER_RANGE = (4.0, 12.0)  # decision 4: the feather lives inside the mask
GROW_RANGE = (-32, 32)
ETA_S = 5.0
# What the engine cards must not count: a replay is not a sample of the engine that made the take.
CARD_FIELDS = ("task", "shot_class")
_HEX64 = frozenset("0123456789abcdef")


def is_recomposite(run: Run) -> bool:
    """A run this module made: `inputs.recomposite_of` on the local recomposite route."""
    return isinstance(run.inputs.get(PARAM), str) and run.route_dict.get("route_id") == ROUTE_ID


def recomposite_route(parent_route: Mapping[str, Any]) -> dict[str, Any]:
    """The parent's route made local: nothing leaves the box, so custody is a confirmed C0 whatever the vendor's
    status was (a pending route would otherwise read as D)."""
    route = {k: v for k, v in parent_route.items() if k != "reasons"}
    route.update(
        provider=PROVIDER,
        region="local",
        route_id=ROUTE_ID,
        custody_grade="C0",
        custody_status="confirmed",
        upload_visibility="local",
    )
    route.setdefault("model_version", MODEL)
    return route


def grow_mask(mask: np.ndarray, px: int) -> np.ndarray:
    """`mask` dilated (px > 0) or eroded (px < 0) with an elliptical kernel of 2 * |px| + 1; px 0 is a copy. The
    frame's edge neither grows nor shrinks a mask that touches it."""
    m = np.asarray(mask, dtype=bool)
    if px == 0:
        return m.copy()
    size = 2 * abs(int(px)) + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    src = m.astype(np.uint8)
    out = cv2.dilate(src, kernel) if px > 0 else cv2.erode(src, kernel)
    return out > 0


def _grey_png(mask: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", np.where(mask, 255, 0).astype(np.uint8))
    if not ok:
        raise ValueError("cv2 could not encode the mask")
    return bytes(buf)


def _number(
    value: Any, what: str, lo: float, hi: float, *, integer: bool = False, code: str
) -> float | int | None:
    """`value` as a number in [lo, hi] (None and "" are 'not given'); anything else is a 422 `code`."""
    if value is None or value == "":
        return None
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    if ok and integer:
        ok = float(value) == int(value)
    if not ok or not lo <= value <= hi:
        kind = "an integer" if integer else "a number"
        raise PipelineError(f"{what} must be {kind} from {lo:g} to {hi:g}, got {value!r}", code=code)
    return int(value) if integer else float(value)


class ReplayEngine:
    """The worker's engine for one recomposite run: the 'provider' is the project's own CAS. Its job id is a
    function of the run, so a lost submit answer is adopted by `lookup` and a re-staged submit lands on the same
    job; a poll always succeeds; a fetch returns the parent's raw bytes."""

    stub = False
    idempotent_submit = True
    supports_lookup = True

    def __init__(self, pipeline: Any, run: Run, cas: CAS):
        self.pipeline = pipeline
        self.run = run
        self.cas = cas

    @property
    def job_id(self) -> str:
        return f"{JOB_PREFIX}{self.run.id}"

    def raw_key(self) -> str:
        key = self.run.inputs.get(RAW_KEY)
        if not isinstance(key, str) or not key:
            raise PipelineError(
                f"run {self.run.id} names no raw take to re-composite", code="not_recompositable"
            )
        return key

    def submit(self, payload: dict[str, Any], idempotency_key: str) -> str:
        self.pipeline.require_custody(self.run)
        key = self.raw_key()
        if not self.cas.exists(key):
            raise FileNotFoundError(f"the stored raw take {key[:12]} is not in this project's CAS")
        return self.job_id

    def lookup(self, idempotency_key: str) -> str | None:
        return self.job_id

    def poll(self, provider_job_id: str) -> PollResult:
        return PollResult("succeeded", result_url=f"{ENGINE_URL}{self.run.id}")

    def fetch(self, result_url: str) -> bytes:
        if result_url != f"{ENGINE_URL}{self.run.id}":
            raise ValueError(f"{result_url!r} is not the replay of run {self.run.id}")
        return self.cas.read_bytes(self.raw_key())

    def cancel(self, provider_job_id: str) -> None:
        return None


class Recomposer:
    """Quotes, approves and supplies the engine for recomposite runs on one Pipeline."""

    def __init__(self, pipeline: Any):
        self.pipeline = pipeline

    @staticmethod
    def requested(body: Mapping[str, Any] | None) -> bool:
        """Does a quote body ask for a recomposite (`params.recomposite_of` is set)?"""
        params = (body or {}).get("params")
        return isinstance(params, Mapping) and params.get(PARAM) not in (None, "")

    # ------------------------------------------------------------------ the parent

    def parent_of(self, conn: Any, project_id: str, node_id: str, run_id: Any) -> Run:
        """The run to re-composite, or a 404 / 422 `not_recompositable`: it must be a pipeline run of this node
        that kept its raw take, its size map and the egress PNG it was staged with, all still in the CAS."""
        if not isinstance(run_id, str) or not run_id:
            raise PipelineError(f"{PARAM} must be a run id", code="not_recompositable")
        try:
            parent = get_run(conn, run_id)
        except RunNotFound as e:
            raise PipelineError(f"no run {run_id}", code="not_found", status=404) from e
        if parent.project_id != project_id or parent.node_id != node_id:
            raise PipelineError(
                f"run {run_id} is not a take of node {node_id} in project {project_id}",
                code="not_recompositable",
            )
        norm = parent.normalized or {}
        cas = self.pipeline.cas_for(project_id)
        raw, egress, sizemap = norm.get("raw_key"), norm.get("egress_key"), norm.get("sizemap")
        missing = []
        if not is_pipeline_run(parent):
            missing.append("it is not a pipeline take")
        if not isinstance(raw, str) or not cas.exists(raw):
            missing.append("its raw take is not stored")
        if not isinstance(egress, str) or not cas.exists(egress):
            missing.append("the egress PNG it was staged with is not stored")
        if not isinstance(sizemap, Mapping):
            missing.append("it kept no size map")
        base = parent.inputs.get("base")
        if isinstance(base, str) and not cas.exists(base):
            missing.append("the take it was made on top of is not stored")
        if missing:
            raise PipelineError(
                f"run {run_id} cannot be re-composited: {'; '.join(missing)}", code="not_recompositable"
            )
        return parent

    # ------------------------------------------------------------------ the mask

    def mask_for(
        self, graph: Any, node_id: str, project_id: str, pv: Any, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        """The new run's mask spec: `params.mask_hash` when given, else the node's current Mask (read from the
        graph again); grown or shrunk by `params.grow_px` into a stored mask. Refused (422) when it is empty."""
        pl = self.pipeline
        cas = pl.cas_for(project_id)
        grow = _number(params.get(GROW_PARAM), GROW_PARAM, *GROW_RANGE, integer=True, code="bad_grow")
        key = params.get(MASK_PARAM)
        if key not in (None, ""):
            if not isinstance(key, str) or len(key) != 64 or not set(key) <= _HEX64:
                raise PipelineError(f"{MASK_PARAM} must be a CAS key (64 hex)", code="bad_mask")
            spec: dict[str, Any] = {
                "kind": "union",
                "ids": [],
                "keys": [key],
                "source": f"{PARAM}:{MASK_PARAM}",
            }
        else:
            spec = pl.mask_spec(graph, node_id, pv)
        pl.check_mask_keys(project_id, spec, pv)
        probe = SimpleNamespace(inputs={"mask": spec})  # editable_mask reads nothing else of a run
        editable = pl.editable_mask(probe, pv, cas)  # raises mask_empty
        if not grow:
            return spec
        grown = grow_mask(editable, int(grow))
        stored = {"kind": "union", "ids": [], "keys": [cas.put_bytes(_grey_png(grown), "png")]}
        stored.update(source=spec.get("source"), grow_px=int(grow))
        # a mask shrunk to nothing is refused here, before a run row exists
        pl.editable_mask(SimpleNamespace(inputs={"mask": stored}), pv, cas)
        return stored

    # ------------------------------------------------------------------ quote, approve

    def quote(
        self,
        conn: Any,
        project_id: str,
        node_id: str,
        body: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> tuple[Run, Quote]:
        """An Edit node + `params.recomposite_of` -> a QUOTED run at 0 USD. Refusals, all before a run row: 404
        project, node or parent run; 422 `not_recompositable`, `bad_grow`, `bad_feather`, `bad_mask`,
        `mask_empty` / `mask_locked` / `mask_missing` / `mask_size`, canvas rules, a pinned size map that no longer
        fits (`bad_sizemap`, `pinned_png_missing`)."""
        pl = self.pipeline
        now = now or pl.clock()
        body = dict(body or {})
        params_in = body.get("params") if isinstance(body.get("params"), Mapping) else {}
        project = pl.load_project(project_id)
        if project is None:
            raise PipelineError(f"no project {project_id}", code="not_found", status=404)
        g = canvas_rules.as_graph(project.graph)
        node = next((n for n in g.nodes if n.get("id") == node_id), None)
        if node is None:
            raise PipelineError(f"project {project_id} has no node {node_id}", code="not_found", status=404)
        if node.get("kind") != "Edit":
            raise PipelineError(
                f"only an Edit take can be re-composited, {node_id} is a {node.get('kind')}",
                code="not_recompositable",
            )
        parent = self.parent_of(conn, project_id, node_id, params_in.get(PARAM))
        feather = _number(body.get("feather_px"), "feather_px", *FEATHER_RANGE, code="bad_feather")
        upstream = canvas_rules.ancestors(g, node_id) | {node_id}
        broken = [v for v in canvas_rules.validate_graph(g) if v["edge"]["target"] in upstream]
        if broken:
            v = broken[0]
            raise PipelineError(v["message"], code=v["code"], violations=broken)
        plates = canvas_rules.upstream_plates(g, node_id)
        if not plates:
            raise PipelineError(
                "generation must be anchored to a plate camera (source-view-only)", code="NO_PLATE_ANCHOR"
            )
        if plates[0].get("packetId") not in (None, parent.packet_id):
            raise PipelineError(
                f"the node's Plate now names packet {plates[0].get('packetId')}; run {parent.id} was made on "
                f"{parent.packet_id}",
                code="not_recompositable",
            )
        pv = pl.packet(project_id, parent.packet_id)
        if parent.inputs.get("plate") not in (None, pv.plate_hash):
            raise PipelineError(
                f"packet {parent.packet_id}: the plate is not the one run {parent.id} was made on",
                code="not_recompositable",
            )
        norm = parent.normalized or {}
        staging = {k: v for k, v in (parent.inputs.get(STAGING_KEY) or {}).items() if k not in CARD_FIELDS}
        parent_feather = staging.get("feather_px") or (norm.get("composite") or {}).get("feather_px")
        staging.update(
            sizemap=norm["sizemap"],
            png_key=norm["egress_key"],
            feather_px=float(feather if feather is not None else parent_feather or DEFAULT_FEATHER_PX),
        )
        pl.pinned_egress(staging, pv, pl.cas_for(project_id))  # the parent's size map still fits this plate
        mask = self.mask_for(g, node_id, project_id, pv, params_in)

        inputs = copy.deepcopy(parent.inputs)
        inputs.pop("reference_images", None)
        inputs.update(
            {
                "mask": mask,
                STAGING_KEY: staging,
                PARAM: parent.id,
                RAW_KEY: norm["raw_key"],
                SUBGRAPH_KEY: subgraph_snapshot(project.graph, upstream),
            }
        )
        params: dict[str, Any] = {PARAM: parent.id}
        for name in (GROW_PARAM, MASK_PARAM):
            if params_in.get(name) not in (None, ""):
                params[name] = params_in[name]
        route = recomposite_route(parent.route_dict)
        attempt = body.get("attempt")
        if attempt is None:
            attempt = pl.next_attempt(conn, node_id, inputs, params, route)
        run = create_run(
            conn,
            RunCreate(
                project_id=project_id,
                packet_id=parent.packet_id,
                node_id=node_id,
                priority=Priority.parse(body.get("priority") or "interactive"),
                inputs=inputs,
                params=params,
                route=route,
                attempt=int(attempt),
            ),
            now=now,
        )
        pl.index.remember(run.id, project_id)
        quote = Quote(
            run_id=run.id,
            provider=PROVIDER,
            model=MODEL,
            model_version=str(route.get("model_version") or MODEL),
            region="local",
            custody_grade="C0",
            price_usd=0.0,
            eta_s=ETA_S,
            created_utc=now,
            expires_utc=now + QUOTE_TTL,
            route_id=ROUTE_ID,
            price_status="confirmed",
        )
        with tx(conn):
            insert_quote(conn, quote)
            run = transition(
                conn,
                run.id,
                S.QUOTED,
                actor="system",
                now=now,
                reason=f"re-mask of the raw take of {parent.id}, local C0, 0.0000 USD",
                quote_id=quote.id,
                quote_json=quote.terms_json(),
            )
        pl.after(conn, project_id, run.id, S.DRAFT)
        return run, quote

    def approve(
        self,
        conn: Any,
        project_id: str,
        run_id: str,
        by: str = "human",
        *,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> Run:
        """QUOTED -> APPROVED as `human`, `auto` or `agent:<envelope_id>`. The run costs 0 USD, so there is no
        budget decision (the caps and envelopes exist to stop spend); custody is still checked, and an expired
        quote still expires the run."""
        pl = self.pipeline
        run = get_run(conn, run_id)
        pl.require_custody(run)
        if run.quote is not None and run.quote.price_usd > 0:
            raise PipelineError(
                f"run {run_id} has a priced quote; approve it through the budget rules", code="priced"
            )
        now = now or pl.clock()
        run = transition(
            conn,
            run_id,
            S.APPROVED,
            actor=by,
            approved_by=by,
            now=now,
            reason=reason or "approved 0.0000 USD (local re-mask of a stored take)",
        )
        if by == "human":
            pl.touch_human(now)
        pl.after(conn, project_id, run_id, S.QUOTED)
        return run

    def engine_for(self, run: Run, cas: CAS) -> ReplayEngine:
        return ReplayEngine(self.pipeline, run, cas)


def install(pipeline: Any) -> Recomposer:
    """Let `pipeline` quote and drive recomposite runs (idempotent; returns the runner, kept at
    `pipeline.recomposite_runner`). Wraps `quote_node` (an Edit quote with `params.recomposite_of` is quoted
    here), `approve` (a free local run takes no budget decision) and `engine_for` (the replay engine); everything
    else falls through to the originals."""
    existing = getattr(pipeline, "recomposite_runner", None)
    if existing is not None:
        return existing
    runner = Recomposer(pipeline)
    pipeline.recomposite_runner = runner
    orig_quote, orig_approve, orig_engine_for = pipeline.quote_node, pipeline.approve, pipeline.engine_for

    def quote_node(
        conn: Any, project_id: str, node_id: str, body: Mapping[str, Any], *, now: datetime | None = None
    ) -> Any:
        if runner.requested(body):
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
        if is_recomposite(get_run(conn, run_id)):
            return [runner.approve(conn, project_id, run_id, by, reason=reason, now=now)]
        return orig_approve(conn, project_id, run_id, by, envelope_id, reason=reason, now=now)

    def engine_for(run: Run, cas: CAS) -> Any:
        return runner.engine_for(run, cas) if is_recomposite(run) else orig_engine_for(run, cas)

    pipeline.quote_node, pipeline.approve, pipeline.engine_for = quote_node, approve, engine_for
    return runner
