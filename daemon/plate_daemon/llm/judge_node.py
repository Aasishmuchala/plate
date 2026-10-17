"""The Judge node: the two-sample consensus judge as an in-process quotable node (router plan T66, decisions 38, 41).

`install(pipeline)` wraps `quote_node`, `approve`, `step`, `engine_for` and `cancel` for kind `Judge` on that Pipeline
instance (the install-wrapper pattern of `nodes.install`), so the daemon's quote, approve, run and cancel routes drive a
Judge run like an Edit and every move is published as `run.state` and ledgered.

Quote, in the order money and custody are at stake:

1. Custody first (decision 5a). The Anthropic API route (`custody.llm_route()`; pending written terms count as D) must
   be among the run's grades (project custody ∩ the plate packet's). An NDA project, or any project that keeps D out,
   answers 422 `{"error": "unavailable", "detail": "judge unavailable on NDA until Anthropic terms are confirmed"}`
   and no run row is made.
2. The candidate: the latest DONE or GATE_FAIL run of the nearest upstream node that has one; its first output must
   be a still in the CAS (a clip is 422 `not_a_still`; nothing to judge is 422 `no_candidate`). The candidate, its
   gate-flagged tiles (the judge's crops, at most four) and the NDA flag are pinned in the run, so what was quoted is
   what is judged; the judge reads the candidate's newest stored report when it runs.
3. The price: two opus samples (`llm/prices.yaml`), each a plate and candidate pair at 1536 px wide (the open
   derivative width), the rubric and the request text in, `judge.MAX_TOKENS` out (the worst case, as the live
   transport's budget check counts it). `custody_grade` is the route's effective grade, `provider` anthropic,
   `model` claude-opus-5-5 (the route's judge model).

Execution: the engine is in-process (submit, poll and fetch are bookkeeping). NORMALIZING calls `judge.judge` on the
candidate and the packet's display master with the recorded-fixture transport by default (`JudgeRunner.transport`
replaces it), through an `AnthropicClient` that ledgers every call (`done` records with `data.llm`, their own
`llm_<hex>` run ids, the judge run as `data.parent_run_id`); the run itself gets one `llm` record with the verdict
and the samples' cost. GATING -> DONE stores `gate_report` = the upstream report + `{"judge": {...}}`: the judge
never changes the numeric verdict (a gate failure stays a failure, and forces `fix`). `normalized.judge` is the
Verdict as a dict, and `outputs` is empty: the judge makes no pixels, so nothing can be exported through it. A judge
that answers no verdict (a sample still unreadable after its retry) fails the run. `nda` is the project's or the
packet's NDA flag: False whenever the route is allowed today, and the NDA derivatives (cropped, 1024 px) once the
Anthropic terms are filed and an NDA project may call the judge.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import canvas_rules, custody
from ..adapter import PollResult
from ..cas import CAS
from ..custody import CustodyViolation
from ..db import tx
from ..idempotency import canonical_json
from ..ledger import Ledger
from ..machine import create_run, get_run, transition
from ..models import QUOTE_TTL, Quote, Run, RunCreate
from ..pipeline import LEDGER_KINDS, STAGING_KEY, PipelineError
from ..quotes import insert_quote
from ..states import TERMINAL, WORKER_STATES, Priority, RunState
from ..worker import cancel_run, run_lock
from ..worker import step as worker_step
from . import judge as judge_mod
from .client import (
    CHARS_PER_TOKEN,
    IMAGE_PX_PER_TOKEN,
    AnthropicClient,
    FixtureTransport,
    Usage,
    estimate_cost,
    load_prices,
)
from .derivative import MAX_WIDTH_OPEN

__all__ = [
    "JUDGE_TASK",
    "KIND",
    "SAMPLES_QUOTED",
    "SPEC_KEY",
    "JudgeEngine",
    "JudgeRunner",
    "install",
    "is_judge_run",
    "judge_estimate_usd",
]

log = logging.getLogger(__name__)
S = RunState

KIND = "Judge"
SPEC_KEY = "judge"  # runs.inputs[SPEC_KEY]: the pinned candidate, crops and model
JUDGE_TASK = "judge"
SAMPLES_QUOTED = 2  # the consensus takes two samples; a third only when they disagree
MAX_CROPS = 4  # gate-flagged tiles sent to the judge, worst first as the gates list them
JOB_PREFIX = "jdg_"
JOB_URL = "plate-judge://"
JUDGE_ETA_S = 60.0
REQUEST_TEXT_CHARS = 400  # the user turn's own words around the images (labels, the pair sentence)
STILL_SUFFIXES = frozenset({".png", ".tif", ".tiff", ".jpg", ".jpeg"})
CANDIDATE_STATES = (S.DONE.value, S.GATE_FAIL.value)


def is_judge_run(run: Run) -> bool:
    staging = run.inputs.get(STAGING_KEY)
    return (
        isinstance(run.inputs.get(SPEC_KEY), Mapping)
        and isinstance(staging, Mapping)
        and (staging.get("node_kind") == KIND)
    )


def judge_estimate_usd(model: str, width: int, height: int, *, samples: int = SAMPLES_QUOTED) -> float:
    """USD for `samples` judge samples of one plate and candidate pair at 1536 px wide (the aspect of the plate),
    with the rubric in and `judge.MAX_TOKENS` out, at `llm/prices.yaml`."""
    w = MAX_WIDTH_OPEN
    h = max(1, round(height * w / max(width, 1)))
    image_tokens = 2 * math.ceil(w * h / IMAGE_PX_PER_TOKEN)
    text_tokens = math.ceil((len(judge_mod.rubric()) + REQUEST_TEXT_CHARS) / CHARS_PER_TOKEN)
    sample = estimate_cost(model, Usage(image_tokens + text_tokens, judge_mod.MAX_TOKENS), load_prices())
    return round(samples * sample, 6)


def _nda(project: Any, manifest: Mapping[str, Any]) -> bool:
    packet_custody = (manifest.get("policy") or {}).get("custody") or {}
    return bool((project.custody or {}).get("nda")) or bool(packet_custody.get("nda"))


def _plate_size(pv: Any) -> tuple[int, int]:
    entry = pv.files.get(pv.plate_role) or {}
    w, h = entry.get("width"), entry.get("height")
    if not (isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0):
        res = (pv.manifest.get("camera") or {}).get("resolution") or {}
        w, h = int(res.get("width") or 0), int(res.get("height") or 0)
    if w <= 0 or h <= 0:
        raise PipelineError(f"packet {pv.packet_id} does not say its plate's size", code="unknown_packet")
    return w, h


def crops_of(report: Mapping[str, Any] | None, width: int, height: int) -> list[list[float]]:
    """The gate-flagged tiles (`flagged_points`, plate pixels) as fractional (x0, y0, x1, y1) boxes, at most four."""
    out: list[list[float]] = []
    for p in (report or {}).get("flagged_points") or []:
        try:
            x, y, w, h = (float(p[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            continue
        x0, y0 = min(max(x / width, 0.0), 1.0), min(max(y / height, 0.0), 1.0)
        x1, y1 = min(max((x + w) / width, 0.0), 1.0), min(max((y + h) / height, 0.0), 1.0)
        box = [round(x0, 6), round(y0, 6), round(x1, 6), round(y1, 6)]
        if x1 > x0 and y1 > y0 and box not in out:
            out.append(box)
        if len(out) >= MAX_CROPS:
            break
    return out


class JudgeEngine:
    """The 'provider' of a Judge run: the work is NORMALIZING's, so submit, poll and fetch are bookkeeping. Submit
    re-checks custody first: a project tightened since the quote never reaches the API."""

    stub = False
    idempotent_submit = True
    supports_lookup = True

    def __init__(self, runner: JudgeRunner, run: Run):
        self.runner, self.run = runner, run

    @staticmethod
    def _job(key: str) -> str:
        return f"{JOB_PREFIX}{key[:26]}"

    def submit(self, payload: dict[str, Any], idempotency_key: str) -> str:
        self.runner.require_route(self.run)
        return self._job(idempotency_key)

    def lookup(self, idempotency_key: str) -> str | None:
        return self._job(idempotency_key)

    def poll(self, provider_job_id: str) -> PollResult:
        return PollResult("succeeded", result_url=f"{JOB_URL}{provider_job_id}")

    def fetch(self, result_url: str) -> bytes:
        return canonical_json({"engine": KIND, "job": result_url.removeprefix(JOB_URL)})

    def cancel(self, provider_job_id: str) -> None:
        return None


class JudgeRunner:
    """Quotes and drives Judge runs. `transport` is the LLM transport (None: the recorded fixtures under
    `daemon/tests/fixtures/llm`); `routes_path` is the custody routes file the LLM route is read from."""

    def __init__(self, pipeline: Any):
        self.pipeline = pipeline
        self.transport: Any = None
        self.routes_path: Path | str = custody.ROUTES_YAML

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

    def _candidate(self, conn: Any, g: Any, node_id: str, cas: CAS) -> Run | None:
        """The latest DONE or GATE_FAIL run of the nearest upstream node that has one. Its first output must be a
        still in the CAS: a clip upstream is refused (422 `not_a_still`) rather than judging some other node."""
        up: dict[str, list[str]] = {}
        for e in g.edges:
            up.setdefault(e["target"], []).append(e["source"])
        seen, frontier = {node_id}, list(up.get(node_id, ()))
        marks = ",".join("?" * len(CANDIDATE_STATES))
        while frontier:
            nxt: list[str] = []
            for nid in frontier:
                if nid in seen:
                    continue
                seen.add(nid)
                row = conn.execute(
                    f"SELECT id FROM runs WHERE node_id = ? AND state IN ({marks}) "
                    "ORDER BY created_utc DESC, id DESC LIMIT 1",
                    (nid, *CANDIDATE_STATES),
                ).fetchone()
                if row is None:
                    nxt.extend(up.get(nid, ()))
                    continue
                run = get_run(conn, row[0])
                key = run.outputs[0] if run.outputs else None
                path = cas.find(key) if isinstance(key, str) and len(key) == 64 else None
                if path is None or path.suffix.lower() not in STILL_SUFFIXES:
                    raise PipelineError(
                        f"the judge looks at stills; the latest take of {nid} ({run.id}) is not one in the CAS",
                        code="not_a_still",
                    )
                return run
            frontier = nxt
        return None

    # ------------------------------------------------------------------ custody

    def _route(self) -> Any:
        return custody.llm_route(self.routes_path)

    def allowed_for(self, run: Run) -> set[Any]:
        pl = self.pipeline
        return pl.allowed_grades(run.project_id, pl.packet(run.project_id, run.packet_id).manifest)

    def require_route(self, run: Run) -> Any:
        """CustodyViolation unless the LLM route (as the routes file says now) is allowed for the run."""
        return custody.require_route(self._route(), self.allowed_for(run), what="LLM judge")

    # ------------------------------------------------------------------ quote, approve

    def quote(
        self,
        conn: Any,
        project_id: str,
        node_id: str,
        body: Mapping[str, Any] | None = None,
        *,
        now: datetime | None = None,
    ) -> tuple[Run, Quote]:
        body = dict(body or {})
        pl = self.pipeline
        now = now or pl.clock()
        project, g, node = self._graph_node(project_id, node_id)
        if node.get("kind") != KIND:
            raise PipelineError(f"{node.get('kind')} nodes are not Judge nodes", code="node_kind_unsupported")
        upstream = canvas_rules.ancestors(g, node_id) | {node_id}
        broken = [v for v in canvas_rules.validate_graph(g) if v["edge"]["target"] in upstream]
        if broken:
            raise PipelineError(broken[0]["message"], code=broken[0]["code"], violations=broken)
        plates = canvas_rules.upstream_plates(g, node_id)
        packet_id = body.get("packet_id") or next(
            (p.get("packetId") for p in plates if p.get("packetId")), None
        )
        if not isinstance(packet_id, str) or not packet_id:
            raise PipelineError(
                "a Judge judges a take of a plate: connect a Plate upstream", code="NO_PLATE_ANCHOR"
            )
        pv = pl.packet(project_id, packet_id)
        route = self._route()
        if not custody.route_allowed(route, pl.allowed_grades(project_id, pv.manifest)):
            raise PipelineError(judge_mod.UNAVAILABLE_NOTICE, code="unavailable")
        cas = pl.cas_for(project_id)
        cand = self._candidate(conn, g, node_id, cas)
        if cand is None:
            raise PipelineError(
                f"nothing upstream of {node_id} has a finished still take to judge", code="no_candidate"
            )
        model = route.models.get(judge_mod.JUDGE_ROLE)
        if not model:
            raise PipelineError("custody_routes.yaml llm.models names no judge model", code="unknown_engine")
        width, height = _plate_size(pv)
        report = self._upstream_report(cand)
        spec = {
            "candidate_run": cand.id,
            "candidate_node": cand.node_id,
            "candidate": cand.outputs[0],
            "upstream_verdict": (report or {}).get("verdict"),
            "crops": crops_of(report, width, height),
            "model": model,
            "nda": _nda(project, pv.manifest),
        }
        route_dict = {
            "engine_id": f"anthropic:{model}",
            "provider": route.provider,
            "region": "unknown",
            "route_id": route.route_id,
            "custody_grade": route.custody_grade,
            "custody_status": route.custody_status,
            "upload_visibility": route.upload_visibility,
            "model_version": model,
        }
        last = conn.execute("SELECT MAX(attempt) FROM runs WHERE node_id = ?", (node_id,)).fetchone()[0] or 0
        run_spec = RunCreate(
            project_id=project_id,
            packet_id=packet_id,
            node_id=node_id,
            priority=Priority.parse(body.get("priority") or "interactive"),
            inputs={
                "candidate": cand.outputs[0],
                "plate": pv.plate_hash,
                STAGING_KEY: {"node_kind": KIND, "judge": True, "region_crop": False},
                SPEC_KEY: spec,
            },
            params={"samples": SAMPLES_QUOTED},
            route=route_dict,
            attempt=max(int(body.get("attempt") or 1), int(last) + 1),
        )
        quote = Quote(
            provider=route.provider,
            model=model,
            model_version=model,
            region="unknown",
            custody_grade=route.effective.value,
            price_usd=judge_estimate_usd(model, width, height),
            eta_s=JUDGE_ETA_S,
            created_utc=now,
            expires_utc=now + QUOTE_TTL,
            route_id=route.route_id,
            price_status="confirmed" if load_prices().confirmed else "unconfirmed",
        )
        run = create_run(conn, run_spec, now=now)
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
                reason=(
                    f"judge {model} on {cand.id} ({route.effective.value}), {SAMPLES_QUOTED} samples, "
                    f"about {quote.price_usd:.4f} USD"
                ),
                quote_id=quote.id,
                quote_json=quote.terms_json(),
            )
        self._after(conn, project_id, run.id, S.DRAFT)
        return run, quote

    def _upstream_report(self, cand: Run) -> dict[str, Any] | None:
        """The candidate's latest stored gate report (the versioned file, which keeps `advisory`), else its own."""
        svc = self.pipeline.report_service(cand.project_id)
        stored = svc.latest(cand.id) if svc is not None else None
        if stored is not None:
            return dict(stored.report)
        return dict(cand.gate_report) if cand.gate_report else None

    # ------------------------------------------------------------------ driving

    def engine_for(self, run: Run) -> JudgeEngine:
        return JudgeEngine(self, run)

    def _transport(self) -> Any:
        transport = (
            self.transport()
            if callable(self.transport) and not hasattr(self.transport, "send")
            else self.transport
        )
        return transport if transport is not None else FixtureTransport()

    def normalize(self, run: Run, cas: CAS) -> dict[str, Any]:
        """NORMALIZING: the judge on the pinned candidate against the packet's display master."""
        pl = self.pipeline
        spec = dict(run.inputs[SPEC_KEY])
        pv = pl.packet(run.project_id, run.packet_id)
        cand_path = cas.find(spec["candidate"])
        if cand_path is None:
            raise PipelineError(
                f"the candidate {spec['candidate'][:12]} is not in the CAS", code="no_candidate"
            )
        with pl.index.project(run.project_id) as conn:
            cand = get_run(conn, spec["candidate_run"])
        report = self._upstream_report(cand) or {"verdict": spec.get("upstream_verdict")}
        project_dir = pl.project_dir(run.project_id)
        client = AnthropicClient(
            transport=self._transport(),
            allowed_grades=self.allowed_for(run),
            routes_path=self.routes_path,
            ledger=Ledger(project_dir, clock=pl.clock),
            run_id=run.id,
            node_id=run.node_id,
        )
        verdict = judge_mod.judge(
            cand_path,
            pv.root / pv.files[pv.plate_role]["path"],
            report,
            bool(spec.get("nda")),
            [tuple(b) for b in spec.get("crops") or []],
            client=client,
            sleep=pl.sleep,
        )
        if verdict.unavailable == "custody":
            raise CustodyViolation(
                "the LLM route is not allowed for this run any more", allowed=self.allowed_for(run)
            )
        if verdict.unavailable:
            raise RuntimeError(f"the judge gave no verdict: {verdict.error or verdict.unavailable}")
        found = verdict.as_dict()
        pl.ledger(
            run.project_id,
            "llm",
            run,
            data={
                "purpose": JUDGE_TASK,
                "model": spec.get("model"),
                "samples": verdict.samples,
                "score": verdict.score,
                "verdict": verdict.verdict,
                "forced_by_gate": verdict.forced_by_gate,
                "cost_usd_estimated": verdict.cost_usd,
                "candidate_run": cand.id,
            },
        )
        combined = {**report, "judge": found}
        return {
            "outputs": [],
            "judge": found,
            "candidate": {"run_id": cand.id, "node_id": cand.node_id, "key": spec["candidate"]},
            "crops": spec.get("crops") or [],
            "gate_key": cas.put_bytes(canonical_json(combined), "json"),
        }

    def _finish(self, conn: Any, project_id: str, run_id: str, cas: CAS) -> RunState:
        """GATING -> DONE with the upstream report + the judge: the numeric verdict is the gates', never the judge's."""
        with run_lock(run_id):
            run = get_run(conn, run_id)
            if run.state is not S.GATING:
                return run.state
            try:
                combined = json.loads(cas.read_bytes((run.normalized or {})["gate_key"]).decode("utf-8"))
            except (KeyError, OSError, ValueError) as e:
                transition(
                    conn,
                    run_id,
                    S.FAILED,
                    actor="worker",
                    reason=f"judge report unreadable: {e}"[:500],
                    reason_code="gate_error",
                )
                return S.FAILED
            svc = self.pipeline.report_service(project_id)
            if svc is not None:
                path = svc.write(run_id, combined)
                project_dir = self.pipeline.project_dir(project_id)
                try:
                    combined["report_path"] = Path(path).relative_to(project_dir).as_posix()
                except ValueError:
                    combined["report_path"] = Path(path).as_posix()
            judged = combined.get("judge") or {}
            transition(
                conn,
                run_id,
                S.DONE,
                actor="worker",
                reason=f"judge: {judged.get('verdict')} {judged.get('score')} (gates: {combined.get('verdict')})",
                gate_report=combined,
            )
            return S.DONE

    def step(self, conn: Any, project_id: str, run_id: str, cas: CAS | None = None) -> RunState:
        pl = self.pipeline
        cas = cas or pl.cas_for(project_id)
        run = get_run(conn, run_id)
        before = run.state
        if before in TERMINAL or before not in WORKER_STATES:
            return before
        if before is S.GATING:
            after = self._finish(conn, project_id, run_id, cas)
        else:
            after = worker_step(
                conn,
                self.engine_for(run),
                run_id,
                pl.clock,
                cas=cas,
                poll_timeout_s=pl.poll_timeout_s,
                normalize_fn=self.normalize,
            )
        if after is not before:
            self._after(conn, project_id, run_id, before)
        return after

    def cancel(
        self, conn: Any, project_id: str, run_id: str, *, actor: str = "human", reason: str | None = None
    ) -> Run:
        run = get_run(conn, run_id)
        before = run.state
        out = cancel_run(conn, self.engine_for(run), run_id, actor=actor, reason=reason)
        self._after(conn, project_id, run_id, before)
        return out

    def _after(self, conn: Any, project_id: str, run_id: str, before: RunState) -> None:
        """`Pipeline.after` with the judge's ledger fields (`data.model`, `data.task`)."""
        pl = self.pipeline
        run = get_run(conn, run_id)
        event = {"kind": "run.state", "run_id": run_id, "from": before.value, "to": run.state.value}
        if run.reason_code:
            event["reason_code"] = run.reason_code
        pl.publish(project_id, run_id, event)
        kind = LEDGER_KINDS.get(run.state)
        if kind is not None:
            data = {
                "model": (run.inputs.get(SPEC_KEY) or {}).get("model"),
                "task": JUDGE_TASK,
                "node_kind": KIND,
            }
            pl.ledger(project_id, kind, run, data=data)


# ---------------------------------------------------------------------------- wiring into a Pipeline


def install(pipeline: Any) -> JudgeRunner:
    """Let `pipeline` quote and drive Judge nodes (idempotent; the runner is kept at `pipeline.judge_runner`).
    Every other node and run goes to what was installed before."""
    existing = getattr(pipeline, "judge_runner", None)
    if existing is not None:
        return existing
    runner = JudgeRunner(pipeline)
    pipeline.judge_runner = runner
    orig_quote, orig_step, orig_engine_for = pipeline.quote_node, pipeline.step, pipeline.engine_for
    orig_approve, orig_cancel = pipeline.approve, pipeline.cancel

    def quote_node(
        conn: Any, project_id: str, node_id: str, body: Mapping[str, Any], *, now: datetime | None = None
    ) -> Any:
        if runner.kind_of(project_id, node_id) == KIND:
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
        run = get_run(conn, run_id)
        if is_judge_run(run):
            runner.require_route(run)  # the routes file or the project's custody may have changed
        return orig_approve(conn, project_id, run_id, by, envelope_id, reason=reason, now=now)

    def step(conn: Any, project_id: str, run_id: str, cas: CAS | None = None) -> RunState:
        if is_judge_run(get_run(conn, run_id)):
            return runner.step(conn, project_id, run_id, cas)
        return orig_step(conn, project_id, run_id, cas)

    def engine_for(run: Run, cas: CAS) -> Any:
        return runner.engine_for(run) if is_judge_run(run) else orig_engine_for(run, cas)

    def cancel(
        conn: Any, project_id: str, run_id: str, *, actor: str = "human", reason: str | None = None
    ) -> Run:
        if is_judge_run(get_run(conn, run_id)):
            return runner.cancel(conn, project_id, run_id, actor=actor, reason=reason)
        return orig_cancel(conn, project_id, run_id, actor=actor, reason=reason)

    pipeline.quote_node, pipeline.approve, pipeline.step = quote_node, approve, step
    pipeline.engine_for, pipeline.cancel = engine_for, cancel
    return runner
