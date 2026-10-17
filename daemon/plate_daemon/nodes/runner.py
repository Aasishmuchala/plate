"""NodeRunner: the numeric and plugin nodes through the same run lifecycle as every other node.

A run made here walks the state machine like an Edit run does (DRAFT, QUOTED, APPROVED, QUEUED, STAGING,
SUBMITTED, RUNNING, FETCHING, NORMALIZING, GATING, DONE or GATE_FAIL), so the ledger, the SSE events, the gate
report files and `GET /runs/{id}` need no special case. What differs is only who does the work:

    Set, Recolour, LightMix   in this process (`NumericEngine`: submit, poll and fetch are bookkeeping; the
                              work is `normalize`, the check is `gate`). Route `plate-numeric/numeric-local`,
                              custody C0, price 0.0000 USD, approved `auto`.
    Light, Scout              in Max. `PluginEngine.submit` puts a job on the plugin queue; the run sits in
                              SUBMITTED/RUNNING until the dock has answered (and, for Light, the superseding
                              packet has arrived); a restart picks the poll up again from the queue.

    quote(conn, project_id, node_id, body)      -> (Run QUOTED, Quote)   graph and packets checked first
    approve(conn, project_id, run_id, by)       -> Run APPROVED          any approver; a 0 USD run needs no budget call
    make(conn, project_id, node_id, body)       -> Run                   quote + approve + queue + advance
    advance(conn, project_id, run_id)           -> Run                   steps until terminal or waiting
    step(conn, project_id, run_id)              -> RunState              exactly one transition
    preview(project_id, node_id, what, params)  -> dict                  numbers only, nothing stored

`install(pipeline)` makes the daemon's own routes drive these runs: it wraps `quote_node`, `approve`, `step` and
`engine_for` on that Pipeline instance, so `POST /projects/{id}/nodes/{node}/quote`, `POST /runs/{id}/approve`,
`pipeline.execute`, `pipeline.resume` (a run made here carries `inputs.staging`, the marker `resume` looks
for) and `pipeline.cancel` handle a numeric run the way they handle an Edit run. The wrapped methods fall
through to the originals for every other node and run.

Install before the daemon's boot-time `resume` starts (in `api.create_app`, right after the Pipeline is built):
a node run left in flight by a stopped daemon carries the `staging` marker, so an un-wired Pipeline would pick
it up and fail it as an unknown engine.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import canvas_rules
from ..adapter import PollResult
from ..cas import CAS
from ..db import tx
from ..idempotency import canonical_json
from ..machine import create_run, get_run, transition
from ..models import QUOTE_TTL, Quote, Run, RunCreate
from ..pipeline import STAGING_KEY, PipelineError
from ..quotes import insert_quote
from ..states import TERMINAL, WORKER_STATES, Priority, RunState
from ..worker import GateOutcome
from ..worker import step as worker_step
from . import light_node, lightmix_node, recolour_node, scout_node, set_node
from .common import (
    NUMERIC_ENGINE_ID,
    NUMERIC_ROUTE,
    PLUGIN_ENGINE_ID,
    PLUGIN_ROUTE,
    SPEC_KEY,
    QuoteContext,
    RunContext,
    plain,
)
from .plugin_queue import JobStateError, PluginQueue

__all__ = [
    "KINDS",
    "MODULES",
    "NUMERIC_KINDS",
    "PLUGIN_KINDS",
    "PREVIEWS",
    "NodeRunner",
    "NumericEngine",
    "PluginEngine",
    "install",
    "is_node_run",
]

log = logging.getLogger(__name__)
S = RunState

MODULES: dict[str, Any] = {
    m.KIND: m for m in (set_node, recolour_node, lightmix_node, light_node, scout_node)
}
NUMERIC_KINDS = frozenset({set_node.KIND, recolour_node.KIND, lightmix_node.KIND})
PLUGIN_KINDS = frozenset({light_node.KIND, scout_node.KIND})
KINDS = NUMERIC_KINDS | PLUGIN_KINDS
PREVIEWS: dict[str, Any] = {"set": set_node, "lightmix": lightmix_node, "recolour": recolour_node}

NUMERIC_URL = "plate-node://"
PLUGIN_URL = "plate-plugin://"
NUMERIC_ETA_S = 5.0
PLUGIN_ETA_S = 300.0
VIEW_CACHE = 4
COLOUR_CACHE = 8


def is_node_run(run: Run) -> bool:
    """A run this module made: a spec in `inputs.numeric` and one of its two local routes."""
    return isinstance(run.inputs.get(SPEC_KEY), Mapping) and run.route.engine_id in (
        NUMERIC_ENGINE_ID,
        PLUGIN_ENGINE_ID,
    )


def module_of(run: Run) -> Any:
    kind = (run.inputs.get(STAGING_KEY) or {}).get("node_kind")
    if kind not in MODULES:
        raise PipelineError(
            f"run {run.id} names no numeric node kind ({kind!r})", code="node_kind_unsupported"
        )
    return MODULES[kind]


# ---------------------------------------------------------------------------- engines (the worker's protocol)


class NumericEngine:
    """The 'provider' of an in-process node: its job id is a function of the idempotency key, a poll always
    succeeds and a fetch returns a small marker. A crash at any point re-runs the same deterministic work."""

    stub = False
    idempotent_submit = True
    supports_lookup = True
    route = NUMERIC_ROUTE

    @staticmethod
    def _job(key: str) -> str:
        return f"num_{key[:26]}"

    def submit(self, payload: dict[str, Any], idempotency_key: str) -> str:
        return self._job(idempotency_key)

    def lookup(self, idempotency_key: str) -> str | None:
        return self._job(idempotency_key)

    def poll(self, provider_job_id: str) -> PollResult:
        return PollResult("succeeded", result_url=f"{NUMERIC_URL}{provider_job_id}")

    def fetch(self, result_url: str) -> bytes:
        return canonical_json({"engine": NUMERIC_ENGINE_ID, "job": result_url.removeprefix(NUMERIC_URL)})

    def cancel(self, provider_job_id: str) -> None:
        return None


class PluginEngine:
    """The Max dock as a provider. Bound to one run (`PluginEngine(runner, run)`) it can submit, poll and fetch;
    unbound it can still look up and cancel, which need only the queue."""

    stub = False
    idempotent_submit = True
    supports_lookup = True
    route = PLUGIN_ROUTE

    def __init__(self, runner: NodeRunner, run: Run | None = None):
        self.runner = runner
        self.run = run

    def _bound(self) -> Run:
        if self.run is None:
            raise RuntimeError("this PluginEngine is not bound to a run")
        return self.run

    def submit(self, payload: dict[str, Any], idempotency_key: str) -> str:
        run = self._bound()
        module = module_of(run)
        job = self.runner.queue.enqueue(
            action=module.ACTION,
            project_id=run.project_id,
            payload=module.job_payload(dict(run.inputs[SPEC_KEY]), run.packet_id),
            packet_id=run.packet_id,
            run_id=run.id,
            node_id=run.node_id,
            key=idempotency_key,
            now=self.runner.pipeline.clock(),
        )
        return job.id

    def lookup(self, idempotency_key: str) -> str | None:
        job = self.runner.queue.by_key(idempotency_key)
        return job.id if job is not None else None

    def poll(self, provider_job_id: str) -> PollResult:
        run = self._bound()
        job = self.runner.queue.get(provider_job_id)
        if job is None:
            return PollResult("failed", detail=f"plugin job {provider_job_id} is not in the queue any more")
        if job.state == "queued":
            return PollResult("queued", detail="waiting for the Max dock")
        if job.state == "claimed":
            return PollResult("running", detail="the Max dock has the job")
        if job.state == "cancelled":
            return PollResult("failed", detail="the plugin job was cancelled")
        if job.state == "failed":
            detail = (job.result or {}).get("detail")
            return PollResult("failed", detail=str(detail or "the Max dock reported a failure")[:500])
        settled = module_of(run).settle(self.runner.pipeline, run, job)
        if settled.status == "ready":
            return PollResult("succeeded", result_url=f"{PLUGIN_URL}{job.id}")
        if settled.status == "failed":
            return PollResult("failed", detail=settled.detail)
        return PollResult("running", detail=settled.detail)

    def fetch(self, result_url: str) -> bytes:
        run = self._bound()
        job = self.runner.queue.get(result_url.removeprefix(PLUGIN_URL))
        if job is None or job.state != "done":
            raise RuntimeError(f"plugin job {result_url} is not finished")
        module = module_of(run)
        settled = module.settle(self.runner.pipeline, run, job)
        if settled.status != "ready":
            raise RuntimeError(f"plugin job {job.id} finished but its result is not here: {settled.detail}")
        return canonical_json(plain(module.result_blob(run, job, settled)))

    def cancel(self, provider_job_id: str) -> None:
        try:
            self.runner.queue.cancel(
                provider_job_id, reason="the run was cancelled", now=self.runner.pipeline.clock()
            )
        except JobStateError:
            return None  # already finished: nothing to stop


# ---------------------------------------------------------------------------- the runner


class NodeRunner:
    def __init__(self, pipeline: Any, queue: PluginQueue | None = None):
        self.pipeline = pipeline
        self.queue = queue or PluginQueue(pipeline.settings.home, clock=pipeline.clock)
        self.numeric_engine = NumericEngine()
        self._guard = threading.Lock()
        self._views: OrderedDict[tuple[str, str], Any] = OrderedDict()
        self._colour: OrderedDict[str, Any] = OrderedDict()

    # ------------------------------------------------------------------ caches

    def view(self, pv: Any) -> Any:
        """The gates package's reading of a packet (Z, normals, Cryptomatte, legend), cached per packet."""
        from plate_gates import crossview

        key = (str(pv.root), pv.packet_id)
        with self._guard:
            hit = self._views.get(key)
            if hit is not None:
                self._views.move_to_end(key)
                return hit
        loaded = crossview.PacketView.load(pv.root)
        with self._guard:
            self._views[key] = loaded
            while len(self._views) > VIEW_CACHE:
                self._views.popitem(last=False)
        return loaded

    def colour(self, manifest: Mapping[str, Any]) -> Any:
        """The packet's colour pipeline (OCIO or legacy gamma), cached by its colour block."""
        from plate_pixels.ocio import Pipeline

        key = json.dumps(manifest["colour"], sort_keys=True)
        with self._guard:
            hit = self._colour.get(key)
            if hit is not None:
                self._colour.move_to_end(key)
                return hit
        built = Pipeline.from_manifest(manifest)
        with self._guard:
            self._colour[key] = built
            while len(self._colour) > COLOUR_CACHE:
                self._colour.popitem(last=False)
        return built

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
        """The kind of a graph node, or None when the project, graph or node is not there."""
        try:
            return self._graph_node(project_id, node_id)[2].get("kind")
        except (PipelineError, ValueError):
            return None

    def _quote_context(
        self, conn: Any, project_id: str, node_id: str, body: Mapping[str, Any]
    ) -> tuple[QuoteContext, dict[str, Any]]:
        _project, g, node = self._graph_node(project_id, node_id)
        upstream = canvas_rules.ancestors(g, node_id) | {node_id}
        broken = [v for v in canvas_rules.validate_graph(g) if v["edge"]["target"] in upstream]
        if broken:
            v = broken[0]
            raise PipelineError(v["message"], code=v["code"], violations=broken)
        plates = canvas_rules.upstream_plates(g, node_id)
        if not plates and not body.get("packet_id"):
            raise PipelineError(
                "a node works on a plate camera: connect a Plate (or name packet_id)", code="NO_PLATE_ANCHOR"
            )
        node_params = node.get("params") if isinstance(node.get("params"), Mapping) else {}
        overlay = body.get("params") if isinstance(body.get("params"), Mapping) else {}
        q = QuoteContext(
            pipeline=self.pipeline,
            conn=conn,
            project_id=project_id,
            node=node,
            graph=g,
            node_params=dict(node_params),
            overlay=dict(overlay),
            body=dict(body),
            plates=plates,
            cas=self.pipeline.cas_for(project_id),
        )
        return q, node

    # ------------------------------------------------------------------ quote, approve, queue, make

    def quote(
        self,
        conn: Any,
        project_id: str,
        node_id: str,
        body: Mapping[str, Any] | None = None,
        *,
        now: datetime | None = None,
    ) -> tuple[Run, Quote]:
        """A numeric or plugin node -> a QUOTED run (price 0, local C0). Refusals are PipelineErrors: 404 project
        or node, 422 rules / packet / parameters."""
        body = dict(body or {})
        pl = self.pipeline
        now = now or pl.clock()
        q, node = self._quote_context(conn, project_id, node_id, body)
        kind = node.get("kind")
        module = MODULES.get(kind)
        if module is None:
            raise PipelineError(f"{kind} nodes are not numeric nodes", code="node_kind_unsupported")
        prepared = module.prepare(q)
        pv = pl.packet(project_id, prepared.packet_id)
        plugin = kind in PLUGIN_KINDS
        route = dict(PLUGIN_ROUTE if plugin else NUMERIC_ROUTE)
        inputs = {
            "plate": pv.plate_hash,
            SPEC_KEY: prepared.spec,
            STAGING_KEY: {"node_kind": kind, "numeric": True, "region_crop": False},
        }
        last = conn.execute("SELECT MAX(attempt) FROM runs WHERE node_id = ?", (node_id,)).fetchone()[0] or 0
        spec = RunCreate(
            project_id=project_id,
            packet_id=prepared.packet_id,
            node_id=node_id,
            priority=Priority.parse(body.get("priority") or "interactive"),
            inputs=inputs,
            params={},
            route=route,
            attempt=max(int(body.get("attempt") or 1), int(last) + 1),
        )
        run = create_run(conn, spec, now=now)
        pl.index.remember(run.id, project_id)
        quote = Quote(
            run_id=run.id,
            provider=route["provider"],
            model=str(kind).lower(),
            model_version=route["model_version"],
            region=route["region"],
            custody_grade="C0",
            price_usd=0.0,
            eta_s=PLUGIN_ETA_S if plugin else NUMERIC_ETA_S,
            created_utc=now,
            expires_utc=now + QUOTE_TTL,
            route_id=route["route_id"],
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
                reason=f"{kind} {'in the Max dock' if plugin else 'in-process'}, local C0, 0.0000 USD",
                quote_id=quote.id,
                quote_json=quote.terms_json(),
            )
        pl.after(conn, project_id, run.id, S.DRAFT, note="; ".join(q.notes) or None)
        return run, quote

    def approve(
        self,
        conn: Any,
        project_id: str,
        run_id: str,
        by: str = "auto",
        *,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> Run:
        """QUOTED -> APPROVED as `human`, `auto` or `agent:<envelope_id>`. The run costs 0 USD, so there is no
        budget decision to make: the daily and unattended caps, the auto-approve limit and the envelope rules
        exist to stop spend, and a free local run neither needs an envelope nor is refused because the day's
        vendor spend is high. An expired quote still expires the run (`QuoteExpired`)."""
        pl = self.pipeline
        run = get_run(conn, run_id)
        if run.quote is not None and run.quote.price_usd > 0:
            raise PipelineError(
                f"run {run_id} has a priced quote; approve it through the budget rules", code="priced"
            )
        run = transition(
            conn,
            run_id,
            S.APPROVED,
            actor=by,
            approved_by=by,
            now=now or pl.clock(),
            reason=reason or "approved 0.0000 USD (local C0 node)",
        )
        if by == "human":
            pl.touch_human(now)
        pl.after(conn, project_id, run_id, S.QUOTED)
        return run

    def make(
        self,
        conn: Any,
        project_id: str,
        node_id: str,
        body: Mapping[str, Any] | None = None,
        *,
        by: str = "auto",
        now: datetime | None = None,
    ) -> Run:
        """Quote, approve (`auto`), queue and advance as far as the run can go without outside help. A numeric
        node ends DONE or GATE_FAIL; a plugin node stops in SUBMITTED until the dock answers (call `advance`
        again, or let `Pipeline.execute` poll)."""
        run, _quote = self.quote(conn, project_id, node_id, body, now=now)
        self.approve(conn, project_id, run.id, by, now=now)
        self.pipeline.queue(conn, project_id, run.id)
        return self.advance(conn, project_id, run.id)

    # ------------------------------------------------------------------ driving

    def engine_for(self, run: Run) -> Any:
        return PluginEngine(self, run) if run.route.engine_id == PLUGIN_ENGINE_ID else self.numeric_engine

    def step(self, conn: Any, project_id: str, run_id: str, cas: CAS | None = None) -> RunState:
        """One worker transition with this module's hooks; published and ledgered when the state moved."""
        pl = self.pipeline
        cas = cas or pl.cas_for(project_id)
        run = get_run(conn, run_id)
        before = run.state
        if before in TERMINAL or before not in WORKER_STATES:
            return before
        after = worker_step(
            conn,
            self.engine_for(run),
            run_id,
            pl.clock,
            cas=cas,
            poll_timeout_s=pl.poll_timeout_s,
            normalize_fn=self.normalize,
            gate_fn=self.gate,
        )
        if after is not before:
            pl.after(conn, project_id, run_id, before)
        return after

    def advance(self, conn: Any, project_id: str, run_id: str, *, max_steps: int = 64) -> Run:
        """Step until the run is terminal or a step moves nothing (it waits on the Max dock). Never sleeps."""
        cas = self.pipeline.cas_for(project_id)
        for _ in range(max_steps):
            before = get_run(conn, run_id).state
            if before in TERMINAL or before not in WORKER_STATES:
                break
            if self.step(conn, project_id, run_id, cas) is before:
                break
        return get_run(conn, run_id)

    def normalize(self, run: Run, cas: CAS) -> dict[str, Any]:
        out = module_of(run).normalize(RunContext.for_run(self, run, cas))
        if not isinstance(out.get("outputs"), list):
            raise PipelineError(f"{module_of(run).KIND} stored no outputs list", code="normalize_error")
        return plain(out)

    def gate(self, run: Run, cas: CAS) -> GateOutcome:
        """GATING: score what is stored; the report is versioned in the project's gates/ folder."""
        pl = self.pipeline
        module = module_of(run)
        report = module.gate(RunContext.for_run(self, run, cas))
        project_dir = pl.project_dir(run.project_id)
        svc = pl.report_service(run.project_id)
        if svc is not None:
            path = svc.write(run.id, report)
        else:
            from plate_gates.report import write

            path = write(report, project_dir, run.id)
        try:
            ref = Path(path).relative_to(project_dir).as_posix()
        except ValueError:
            ref = Path(path).as_posix()
        return GateOutcome(
            passed=report.verdict != "fail",
            report=report.model_dump(mode="json"),
            score=module.score(report),
            report_path=ref,
        )

    # ------------------------------------------------------------------ preview

    def preview(
        self, project_id: str, node_id: str, what: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Numbers for the node editors (`POST /projects/{id}/nodes/{node}/preview`): `what` is set, lightmix or
        recolour. The node's saved params are the base, `params` the artist's edits over them; no run is made
        and nothing is stored."""
        module = PREVIEWS.get(what)
        if module is None:
            raise PipelineError(
                f"no numeric preview '{what}' (set, lightmix or recolour)", code="unknown_preview", status=404
            )
        pl = self.pipeline
        q, _node = self._quote_context(None, project_id, node_id, {"params": dict(params or {})})
        prepared = module.prepare(q)
        c = RunContext(self, pl.cas_for(project_id), project_id, prepared.spec, prepared.packet_id)
        return plain(module.preview(c))


# ---------------------------------------------------------------------------- wiring into a Pipeline


def install(pipeline: Any) -> NodeRunner:
    """Let `pipeline` quote and drive the numeric and plugin nodes (idempotent; returns the runner).

    Wraps four methods on this instance and leaves every other node and run to the originals:
    `quote_node` (a Set, Recolour, LightMix, Light or Scout node is quoted here), `approve` (a free local run
    takes no budget decision), `step` (a node run takes this module's engine, normalize and gate) and
    `engine_for` (so `cancel` reaches the plugin queue). The runner is
    kept at `pipeline.node_runner`.
    """
    existing = getattr(pipeline, "node_runner", None)
    if existing is not None:
        return existing
    runner = NodeRunner(pipeline)
    pipeline.node_runner = runner
    orig_quote, orig_step = pipeline.quote_node, pipeline.step
    orig_engine_for, orig_approve = pipeline.engine_for, pipeline.approve

    def quote_node(
        conn: Any, project_id: str, node_id: str, body: Mapping[str, Any], *, now: datetime | None = None
    ) -> Any:
        if runner.kind_of(project_id, node_id) in KINDS:
            return runner.quote(conn, project_id, node_id, body, now=now)
        return orig_quote(conn, project_id, node_id, body, now=now)

    def step(conn: Any, project_id: str, run_id: str, cas: CAS | None = None) -> RunState:
        if is_node_run(get_run(conn, run_id)):
            return runner.step(conn, project_id, run_id, cas)
        return orig_step(conn, project_id, run_id, cas)

    def engine_for(run: Run, cas: CAS) -> Any:
        return runner.engine_for(run) if is_node_run(run) else orig_engine_for(run, cas)

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
        if is_node_run(get_run(conn, run_id)):
            return [runner.approve(conn, project_id, run_id, by, reason=reason, now=now)]
        return orig_approve(conn, project_id, run_id, by, envelope_id, reason=reason, now=now)

    pipeline.quote_node, pipeline.step, pipeline.engine_for, pipeline.approve = (
        quote_node,
        step,
        engine_for,
        approve,
    )
    return runner
