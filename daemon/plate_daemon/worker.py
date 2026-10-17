"""The worker: one state transition per `step`, a priority `Runner`, crash recovery that never resubmits blind.

`step(conn, engine, run_id, clock, sleep)` advances a run by at most one transition:
QUEUED -> STAGING (payload into the CAS; the machine computes the idempotency key), STAGING -> SUBMITTED
(write a submit intent, call `engine.submit`, persist the provider id at once, then commit SUBMITTED: two
phases, so a crash in between leaves an id recovery re-polls), SUBMITTED -> RUNNING (first poll shows
running or succeeded), RUNNING -> FETCHING / REFUSED / FAILED(timeout), FETCHING -> NORMALIZING (fetch the
bytes into the CAS at once: result URLs expire), NORMALIZING -> GATING (`normalize_fn`, a stub by default),
GATING -> DONE or GATE_FAIL (`gate_fn`; the stub fails only on `params.force_gate_fail`). A poll that shows
no progress leaves the state alone; a poll that raises (an unknown job, a transport fault) fails the run as
`provider_poll_error` and never propagates. `run_to_completion` loops `step`, sleeping `poll_interval_s` between
idle polls. A queued run whose request is identical to another run's is CANCELLED as `duplicate_request`.

`Runner(index, bus, adapters, gate_fn, normalize_fn)` drives every project of an `Index`: `tick()` advances
runnable runs by priority, then age; a QUEUED run starts only while its route has fewer than `max_inflight`
runs in STAGING / SUBMITTED / RUNNING (from the engine manifest's route, default 2); FETCHING, NORMALIZING
and GATING run straight through in the same tick. Every move is published on the bus as `run.state`.
`fan_out(run, n)` turns a QUOTED run into a best-of-N group under one quote (`sibling_group`; each sibling's
idempotency key carries its index); when every sibling is terminal the group is ranked by gate score and
published as `siblings.ranked`.

`resume_on_boot` (and `recover_on_boot` for one connection) handles what a dead process left behind: a run
with a provider id is re-polled, never resubmitted (one left in STAGING after phase one is promoted to
SUBMITTED); a run in STAGING whose intent was written but whose id was not is adopted through
`engine.lookup` when the adapter has one, re-staged with the same idempotency key when the adapter declares
`idempotent_submit`, otherwise FAILED as `submit_state_unknown` for a human; a STAGING run with no intent
never reached the provider and simply submits on its next step; an expired QUOTED run becomes EXPIRED.
`recover_on_boot` leaves pipeline runs (`skip=is_pipeline_run`) to `Pipeline.resume`, which knows their real engine.
Steps on one run are serialised by an in-process lock (the API's step and cancel routes run on a thread
pool), and the daemon's app-data lock keeps a second process away.

Route health (router plan T85): a failed submit and a poll that raises are provider failures, a fetch that
returns bytes is a success; each is noted on a `route_health.Tracker` (`tracker.note_failure(engine_id,
route_id, reason_code)` / `note_success(engine_id, route_id)`). The tracker is the `route_tracker=` argument of
`step` / `run_to_completion` / `Runner`, else `engine.route_tracker`, else `engine.pipeline.route_tracker` (the
pipeline's `StagedEngine` carries its pipeline), else none: with no tracker nothing is noted. A tracker fault is
logged and never touches the run.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .adapter import PollResult, ResultExpired, lookup_fn, submit_is_idempotent
from .cas import CAS
from .db import tx
from .idempotency import canonical_json, new_id
from .machine import DuplicateRequest, IllegalTransition, annotate, create_run, get_run, transition
from .models import Quote, Run, RunCreate
from .quotes import expire_if_due, insert_quote
from .states import (
    IMMEDIATE_STATES,
    INFLIGHT_STATES,
    TERMINAL,
    TRANSITIONS,
    WORKER_STATES,
    Priority,
    RunState,
)
from .timeutil import iso, utcnow

log = logging.getLogger(__name__)

S = RunState
Clock = Callable[[], datetime]
Sleep = Callable[[float], None]

DEFAULT_POLL_TIMEOUT_S = 3600.0
DEFAULT_POLL_INTERVAL_S = 5.0
DEFAULT_MAX_INFLIGHT = 2
MAX_FAN_OUT = 16
STUB_GATE_VERSION = "stub-0"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_locks_guard = threading.Lock()
_run_locks: dict[str, threading.RLock] = {}


class NotRunnable(Exception):
    """run_to_completion was asked to drive a run that waits on a human (DRAFT, QUOTED, APPROVED)."""


class FanOutError(ValueError):
    pass


@dataclass(frozen=True)
class GateOutcome:
    """What a gate function returns: the verdict, the report to keep, a score to rank siblings by."""

    passed: bool
    report: dict[str, Any] = field(default_factory=dict)
    score: float | None = None
    report_path: str | None = None


NormalizeFn = Callable[[Run, CAS], dict[str, Any]]
GateFn = Callable[[Run, CAS], GateOutcome]


def run_lock(run_id: str) -> threading.RLock:
    with _locks_guard:
        lk = _run_locks.get(run_id)
        if lk is None:
            lk = _run_locks[run_id] = threading.RLock()
        return lk


def _require_cas(cas: CAS | None) -> CAS:
    # There is no global CAS (router plan T5, Decision 6): callers pass the run's project CAS.
    if cas is None:
        raise TypeError("pass the project's CAS (CAS(project_dir / 'assets')); there is no global CAS")
    return cas


def _fault(engine: Any, state: RunState, phase: str) -> None:
    fp = getattr(engine, "fault_point", None)
    if fp is not None:
        fp(state, phase)


def build_payload(run: Run) -> dict[str, Any]:
    """What goes to the provider: engine, inputs and params (plus the variant of a best-of-N sibling).
    Project, shot and packet ids are C0 and never leave the box (PACKET.md section 12)."""
    payload: dict[str, Any] = {"engine_id": run.route.engine_id, "inputs": run.inputs, "params": run.params}
    if run.sibling_index is not None:
        payload["variant"] = run.sibling_index
    return payload


def stub_gate_report(run: Run, failed: bool, now: datetime) -> dict[str, Any]:
    return {
        "gate_version": STUB_GATE_VERSION,
        "metric_code_hash": None,
        "thresholds_id": None,
        "engine_version": run.route.engine_id,
        "created_utc": iso(now),
        "stub": True,
        "verdict": "fail" if failed else "pass",
        "metrics": {},
    }


def stub_normalize(run: Run, cas: CAS) -> dict[str, Any]:
    return {"normalized": True, "stub": True, "result_key": run.result_key, "outputs": [run.result_key]}


@dataclass
class _Ctx:
    conn: sqlite3.Connection
    engine: Any
    clock: Clock
    cas: CAS | None
    poll_timeout_s: float
    actor: str
    normalize_fn: NormalizeFn | None = None
    gate_fn: GateFn | None = None
    route_tracker: Any = None


def tracker_of(engine: Any, explicit: Any = None) -> Any:
    """The route-health tracker for a step: the one passed in, else the engine's own, else its pipeline's."""
    if explicit is not None:
        return explicit
    own = getattr(engine, "route_tracker", None)
    if own is not None:
        return own
    return getattr(getattr(engine, "pipeline", None), "route_tracker", None)


def _note_provider(c: _Ctx, run: Run, ok: bool, code: str = "") -> None:
    """Tell the tracker how the provider just behaved on this run's route. Never raises."""
    tracker = c.route_tracker
    if tracker is None:
        return
    try:
        engine_id, route_id = run.route.engine_id, run.route_dict.get("route_id")
        if ok:
            tracker.note_success(engine_id, route_id)
        else:
            tracker.note_failure(engine_id, route_id, code)
    except Exception as e:  # route health is advice for the Make bar; it must never fail a run
        log.warning("route health not updated for run %s: %s: %s", run.id, type(e).__name__, e)


def _timed_out(run: Run, now: datetime, poll_timeout_s: float) -> bool:
    started = run.submitted_utc or run.updated_utc
    return now - started >= timedelta(seconds=poll_timeout_s)


def _best_effort_cancel(engine: Any, provider_job_id: str) -> str:
    try:
        engine.cancel(provider_job_id)
        return "provider cancel requested"
    except Exception as e:  # reported in the event reason, never swallowed silently
        return f"provider cancel FAILED ({type(e).__name__}: {e}); check the provider console"


def _fail(c: _Ctx, run: Run, code: str, reason: str) -> RunState:
    transition(c.conn, run.id, S.FAILED, actor=c.actor, reason=reason[:500], reason_code=code)
    return S.FAILED


def _submit(c: _Ctx, run: Run, note: str = "") -> RunState:
    if c.cas is not None and run.payload_key and c.cas.has(run.payload_key):
        payload = json.loads(c.cas.read_bytes(run.payload_key).decode("utf-8"))
    else:
        payload = build_payload(run)
    # Write-ahead intent: committed before the provider is called, so recovery can tell "never sent"
    # (no intent: safe to submit) from "maybe sent" (intent, no id: lookup, idempotent re-stage or fail).
    annotate(c.conn, run.id, expect=S.STAGING, submit_started_utc=c.clock())
    try:
        job_id = c.engine.submit(payload, run.idempotency_key)
    except Exception as e:  # recorded as the failure reason
        _note_provider(c, run, False, "submit_error")
        return _fail(c, run, "submit_error", f"submit raised {type(e).__name__}: {e}")
    if not isinstance(job_id, str) or not job_id.strip():
        _note_provider(c, run, False, "submit_error")
        return _fail(c, run, "submit_error", f"submit returned no job id ({job_id!r})")
    _fault(c.engine, S.STAGING, "after_effect")
    # Phase one: the id is durable the moment the provider has answered...
    annotate(c.conn, run.id, expect=S.STAGING, provider_job_id=job_id, submitted_utc=c.clock())
    _fault(c.engine, S.STAGING, "after_persist")
    # ...phase two: only then does the run count as submitted.
    transition(c.conn, run.id, S.SUBMITTED, actor=c.actor, reason=f"{note}submitted as {job_id}")
    return S.SUBMITTED


def _adopt(c: _Ctx, run: Run, job_id: str, reason: str) -> RunState:
    annotate(c.conn, run.id, expect=S.STAGING, provider_job_id=job_id, submitted_utc=c.clock())
    transition(c.conn, run.id, S.SUBMITTED, actor=c.actor, reason=reason)
    return S.SUBMITTED


def _resolve_unknown_submit(c: _Ctx, run: Run) -> tuple[RunState, str]:
    """STAGING with a submit intent but no provider id: adopt via lookup, re-stage if the provider
    dedupes on the key, else fail for a human. Returns the new state and `adopted | restaged | failed`."""
    fn = lookup_fn(c.engine)
    job_id = None
    if fn is not None and run.idempotency_key:
        try:
            job_id = fn(run.idempotency_key)
        except Exception:  # an unanswerable lookup is the same as no lookup
            job_id = None
    if job_id:
        reason = f"adopted provider job {job_id} via idempotency-key lookup after a lost submit response"
        return _adopt(c, run, job_id, reason), "adopted"
    if submit_is_idempotent(c.engine):
        note = "re-staged with the same idempotency key after a lost submit response; "
        new = _submit(c, run, note=note)
        return new, "restaged" if new is S.SUBMITTED else "failed"
    return (
        _fail(
            c,
            run,
            "submit_state_unknown",
            "a submit may have reached the provider but its job id was never persisted and the adapter "
            "cannot look it up; not resubmitting blind. Check the provider, then re-run as a new attempt.",
        ),
        "failed",
    )


# ---------------------------------------------------------------------------- per-state handlers


def _queued(c: _Ctx, run: Run) -> RunState:
    cas = _require_cas(c.cas)
    payload_key = cas.put_bytes(canonical_json(build_payload(run)), "json")
    _fault(c.engine, S.QUEUED, "after_effect")
    try:
        transition(c.conn, run.id, S.STAGING, actor=c.actor, payload_key=payload_key)
    except DuplicateRequest as e:
        transition(
            c.conn,
            run.id,
            S.CANCELLED,
            actor=c.actor,
            reason=str(e)[:500],
            reason_code="duplicate_request",
        )
        return S.CANCELLED
    return S.STAGING


def _staging(c: _Ctx, run: Run) -> RunState:
    if run.provider_job_id:
        # Phase one committed before a crash: the provider has the job. Re-poll it, never resubmit.
        transition(
            c.conn,
            run.id,
            S.SUBMITTED,
            actor=c.actor,
            reason=f"provider job {run.provider_job_id} was accepted before a restart; re-polling",
        )
        return S.SUBMITTED
    if run.submit_started_utc is not None:
        # A previous submit may have reached the provider. Never call submit blind for this attempt.
        return _resolve_unknown_submit(c, run)[0]
    return _submit(c, run)


def _polling(c: _Ctx, run: Run) -> RunState:
    st = run.state
    try:
        res: PollResult = c.engine.poll(run.provider_job_id)
    except Exception as e:  # an unknown job or a transport fault ends the run; it never unwinds the worker
        # FAILED with the provider id kept: a job may exist, so the spend settles instead of refunding.
        _note_provider(c, run, False, "provider_poll_error")
        return _fail(c, run, "provider_poll_error", f"{type(e).__name__}: {e}"[:500])
    _fault(c.engine, st, "after_effect")
    if res.status == "refused":
        transition(
            c.conn,
            run.id,
            S.REFUSED,
            actor=c.actor,
            reason=res.detail or "provider refused",
            reason_code="refused",
        )
        return S.REFUSED
    if res.status == "failed":
        return _fail(c, run, "provider_failed", res.detail or "provider failed")
    if st is S.SUBMITTED and res.status in ("running", "succeeded"):
        transition(c.conn, run.id, S.RUNNING, actor=c.actor)
        return S.RUNNING
    if st is S.RUNNING and res.status == "succeeded":
        if not res.result_url:
            return _fail(c, run, "no_result", "succeeded without a result URL")
        transition(c.conn, run.id, S.FETCHING, actor=c.actor, result_url=res.result_url)
        return S.FETCHING
    if _timed_out(run, c.clock(), c.poll_timeout_s):
        note = _best_effort_cancel(c.engine, run.provider_job_id)
        return _fail(c, run, "timeout", f"no result within {c.poll_timeout_s:g} s of submit; {note}")
    return st


def _fetching(c: _Ctx, run: Run) -> RunState:
    cas = _require_cas(c.cas)
    try:
        data = c.engine.fetch(run.result_url)
    except ResultExpired as e:
        return _fail(c, run, "result_url_expired", str(e))
    except Exception as e:  # recorded as the failure reason
        return _fail(c, run, "fetch_error", f"fetch raised {type(e).__name__}: {e}")
    _note_provider(c, run, True)  # the route delivered a result: its run of failures is over
    key = cas.put_bytes(data, "png" if data[:8] == PNG_MAGIC else "bin")
    _fault(c.engine, S.FETCHING, "after_effect")
    transition(
        c.conn, run.id, S.NORMALIZING, actor=c.actor, reason=f"{len(data)} bytes into the CAS", result_key=key
    )
    return S.NORMALIZING


def _normalizing(c: _Ctx, run: Run) -> RunState:
    cas = _require_cas(c.cas)
    if not run.result_key or not cas.verify(run.result_key):
        return _fail(c, run, "result_missing", "result blob missing or corrupt")
    try:
        normalized = (c.normalize_fn or stub_normalize)(run, cas)
    except Exception as e:  # recorded as the failure reason
        return _fail(c, run, "normalize_error", f"normalize raised {type(e).__name__}: {e}")
    _fault(c.engine, S.NORMALIZING, "after_effect")
    transition(c.conn, run.id, S.GATING, actor=c.actor, normalized=normalized)
    return S.GATING


def _gating(c: _Ctx, run: Run) -> RunState:
    cas = _require_cas(c.cas)
    if c.gate_fn is None:
        failed = bool(run.params.get("force_gate_fail"))
        outcome = GateOutcome(passed=not failed, report=stub_gate_report(run, failed, c.clock()))
    else:
        try:
            outcome = c.gate_fn(run, cas)
        except Exception as e:  # recorded as the failure reason
            return _fail(c, run, "gate_error", f"gate raised {type(e).__name__}: {e}")
    report = dict(outcome.report)
    report["verdict"] = "pass" if outcome.passed else "fail"
    if outcome.score is not None:
        report["score"] = float(outcome.score)
    if outcome.report_path is not None:
        report["report_path"] = str(outcome.report_path)
    _fault(c.engine, S.GATING, "after_effect")
    if not outcome.passed:
        reason = "stub gate forced to fail (params.force_gate_fail)" if c.gate_fn is None else "gates failed"
        transition(
            c.conn,
            run.id,
            S.GATE_FAIL,
            actor=c.actor,
            reason=reason,
            gate_report=report,
            reason_code="gate_fail_stub" if c.gate_fn is None else "gate_fail",
        )
        return S.GATE_FAIL
    transition(c.conn, run.id, S.DONE, actor=c.actor, gate_report=report)
    return S.DONE


_HANDLERS = {
    S.QUEUED: _queued,
    S.STAGING: _staging,
    S.SUBMITTED: _polling,
    S.RUNNING: _polling,
    S.FETCHING: _fetching,
    S.NORMALIZING: _normalizing,
    S.GATING: _gating,
}


# ---------------------------------------------------------------------------- public API


def step(
    conn: sqlite3.Connection,
    engine: Any,
    run_id: str,
    clock: Clock = utcnow,
    sleep: Sleep = time.sleep,
    *,
    cas: CAS | None = None,
    poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
    actor: str = "worker",
    normalize_fn: NormalizeFn | None = None,
    gate_fn: GateFn | None = None,
    route_tracker: Any = None,
) -> RunState:
    """Advance one run by at most one transition and return its state. Never sleeps (`sleep` is taken
    for signature parity with `run_to_completion`). Runs waiting on a human are returned unchanged."""
    cas = _require_cas(cas)
    with run_lock(run_id):
        run = get_run(conn, run_id)
        st = run.state
        if st in TERMINAL or st not in WORKER_STATES:
            return st
        c = _Ctx(
            conn,
            engine,
            clock,
            cas,
            poll_timeout_s,
            actor,
            normalize_fn,
            gate_fn,
            tracker_of(engine, route_tracker),
        )
        _fault(engine, st, "before")
        new = _HANDLERS[st](c, run)
        if new is not st:
            _fault(engine, st, "after_commit")
        return new


def run_to_completion(
    conn: sqlite3.Connection,
    engine: Any,
    run_id: str,
    clock: Clock = utcnow,
    sleep: Sleep = time.sleep,
    *,
    cas: CAS | None = None,
    poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    max_steps: int = 100_000,
    normalize_fn: NormalizeFn | None = None,
    gate_fn: GateFn | None = None,
    route_tracker: Any = None,
) -> RunState:
    cas = _require_cas(cas)
    for _ in range(max_steps):
        before = get_run(conn, run_id).state
        if before in TERMINAL:
            return before
        if before not in WORKER_STATES:
            raise NotRunnable(
                f"run {run_id} is {before.value}: it waits on a quote, approval or queue action"
            )
        after = step(
            conn,
            engine,
            run_id,
            clock,
            sleep,
            cas=cas,
            poll_timeout_s=poll_timeout_s,
            normalize_fn=normalize_fn,
            gate_fn=gate_fn,
            route_tracker=route_tracker,
        )
        if after in TERMINAL:
            return after
        if after is before:
            sleep(poll_interval_s)
    raise RuntimeError(f"run {run_id} did not finish within {max_steps} steps")


def _non_terminal_ids(conn: sqlite3.Connection) -> list[str]:
    terminal = tuple(s.value for s in TERMINAL)
    marks = ",".join("?" * len(terminal))
    return [
        r[0]
        for r in conn.execute(
            f"SELECT id FROM runs WHERE state NOT IN ({marks}) ORDER BY created_utc, id", terminal
        )
    ]


def is_pipeline_run(run: Run) -> bool:
    """`pipeline.is_pipeline_run`, bound late: the pipeline module imports this one."""
    from .pipeline import is_pipeline_run as _is_pipeline_run

    return _is_pipeline_run(run)


def _recover(
    conn: sqlite3.Connection,
    adapter_for: Callable[[Run], Any],
    clock: Clock,
    cas: CAS | None,
    skip: Callable[[Run], bool] | None = None,
    route_tracker: Any = None,
) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    for rid in _non_terminal_ids(conn):
        with run_lock(rid):
            run = get_run(conn, rid)
            if skip is not None and skip(run):
                continue
            if run.provider_job_id:
                if run.state is S.STAGING:
                    engine = adapter_for(run)
                    c = _Ctx(
                        conn,
                        engine,
                        clock,
                        cas,
                        DEFAULT_POLL_TIMEOUT_S,
                        "recovery",
                        route_tracker=tracker_of(engine, route_tracker),
                    )
                    new = _staging(c, run)
                    actions.append({"run_id": rid, "state": new.value, "action": "promoted"})
                else:
                    actions.append({"run_id": rid, "state": run.state.value, "action": "repoll"})
            elif run.state is S.STAGING and run.submit_started_utc is not None:
                engine = adapter_for(run)
                if engine is None:
                    actions.append({"run_id": rid, "state": run.state.value, "action": "no_adapter"})
                    continue
                c = _Ctx(
                    conn,
                    engine,
                    clock,
                    cas,
                    DEFAULT_POLL_TIMEOUT_S,
                    "recovery",
                    route_tracker=tracker_of(engine, route_tracker),
                )
                new, how = _resolve_unknown_submit(c, run)
                actions.append({"run_id": rid, "state": new.value, "action": how})
            elif run.state is S.STAGING:
                actions.append({"run_id": rid, "state": run.state.value, "action": "restage"})
            elif run.state is S.QUOTED and expire_if_due(conn, rid, clock()):
                actions.append({"run_id": rid, "state": S.EXPIRED.value, "action": "expired"})
    return actions


def recover_on_boot(
    conn: sqlite3.Connection,
    engine: Any,
    clock: Clock = utcnow,
    *,
    cas: CAS | None = None,
    skip: Callable[[Run], bool] | None = is_pipeline_run,
    route_tracker: Any = None,
) -> list[dict[str, str]]:
    """Reconcile every non-terminal run after a restart. Returns one action record per run touched.

    `engine` is the one engine every run is reconciled against, which is only right for seed runs (the
    in-process fake). A run for which `skip(run)` is true is left alone: by default pipeline runs, which the
    pipeline resumes with their real engine (`Pipeline.resume` through `StagedEngine` / `AdapterBridge`), so
    boot never resubmits one to the wrong provider. Pass `skip=None` to reconcile every run."""
    return _recover(conn, lambda run: engine, clock, cas, skip, route_tracker)


def cancel_run(
    conn: sqlite3.Connection, engine: Any, run_id: str, *, actor: str = "human", reason: str | None = None
) -> Run:
    """Cancel a run; a run already at the provider gets exactly one `engine.cancel` call first."""
    with run_lock(run_id):
        run = get_run(conn, run_id)
        if S.CANCELLED not in TRANSITIONS[run.state]:
            raise IllegalTransition(run.state, S.CANCELLED)
        notes = [reason] if reason else []
        if run.provider_job_id:
            notes.append(_best_effort_cancel(engine, run.provider_job_id))
        elif run.state is S.STAGING and run.submit_started_utc is not None:
            fn = lookup_fn(engine)
            job_id = None
            if fn is not None and run.idempotency_key:
                try:
                    job_id = fn(run.idempotency_key)
                except Exception:
                    job_id = None
            if job_id:
                notes.append(_best_effort_cancel(engine, job_id))
            else:
                notes.append("a submit may have reached the provider; its job could not be found to cancel")
        return transition(conn, run_id, S.CANCELLED, actor=actor, reason="; ".join(notes) or None)


# ---------------------------------------------------------------------------- best-of-N


def fan_out(conn: sqlite3.Connection, run_id: str, n: int, *, now: datetime | None = None) -> list[Run]:
    """Make a QUOTED run sibling 0 of a best-of-n group and create n - 1 QUOTED siblings on the same
    quote terms (same price each, same expiry). Approve them together with `quotes.approve_group`."""
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_FAN_OUT:
        raise FanOutError(f"n must be an integer from 1 to {MAX_FAN_OUT}, got {n!r}")
    now = now or utcnow()
    with tx(conn):
        run = get_run(conn, run_id)
        if run.state is not S.QUOTED or run.quote is None:
            raise FanOutError(
                f"run {run_id} is {run.state.value}: fan out a QUOTED run (one quote covers the group)"
            )
        if run.sibling_group:
            raise FanOutError(f"run {run_id} is already in best-of-N group {run.sibling_group}")
        if run.quote.expired(now):
            raise FanOutError(f"run {run_id}'s quote expired at {iso(run.quote.expires_utc)}; re-quote first")
        group = new_id("grp", now)
        terms = run.quote.terms_json()
        terms.update({"sibling_group": group, "sibling_index": 0, "siblings": n})
        annotate(conn, run_id, expect=S.QUOTED, quote_json=terms)
        ids = [run_id]
        for i in range(1, n):
            sib = create_run(
                conn,
                RunCreate(
                    project_id=run.project_id,
                    packet_id=run.packet_id,
                    node_id=run.node_id,
                    priority=run.priority,
                    inputs=run.inputs,
                    params=run.params,
                    route=run.route,
                    attempt=run.attempt,
                ),
                now=now,
            )
            q = Quote(
                **{
                    **terms,
                    "id": new_id("qt", now),
                    "run_id": sib.id,
                    "sibling_index": i,
                    "created_utc": run.quote.created_utc,
                    "expires_utc": run.quote.expires_utc,
                }
            )
            insert_quote(conn, q)
            transition(
                conn,
                sib.id,
                S.QUOTED,
                actor="system",
                now=now,
                reason=f"best-of-{n} sibling {i} of run {run_id} (group {group})",
                quote_id=q.id,
                quote_json=q.terms_json(),
            )
            ids.append(sib.id)
        return [get_run(conn, rid) for rid in ids]


def siblings(conn: sqlite3.Connection, sibling_group: str) -> list[Run]:
    rows = conn.execute(
        "SELECT id FROM runs WHERE json_extract(quote_json, '$.sibling_group') = ? "
        "ORDER BY json_extract(quote_json, '$.sibling_index')",
        (sibling_group,),
    ).fetchall()
    return [get_run(conn, r[0]) for r in rows]


def _rank_key(run: Run) -> tuple[int, float, int]:
    klass = 0 if run.state is S.DONE else 1 if run.state is S.GATE_FAIL else 2
    score = run.gate_score
    return klass, -score if score is not None else float("inf"), run.sibling_index or 0


def rank_siblings(conn: sqlite3.Connection, sibling_group: str) -> list[Run]:
    """The group best first: DONE before GATE_FAIL before the rest, then gate score, then sibling index."""
    return sorted(siblings(conn, sibling_group), key=_rank_key)


# ---------------------------------------------------------------------------- the runner


def route_key(route: Mapping[str, Any]) -> str:
    return f"{route.get('engine_id')}/{route.get('route_id') or route.get('provider')}"


class Runner:
    """Drives every project of an index: priority-then-age order, per-route concurrency, crash resume."""

    def __init__(
        self,
        index: Any,
        bus: Any = None,
        adapters: Mapping[str, Any] | Any = None,
        gate_fn: GateFn | None = None,
        normalize_fn: NormalizeFn | None = None,
        *,
        cas_for: Callable[[str], CAS] | None = None,
        manifests: Mapping[str, Any] | None = None,
        clock: Clock = utcnow,
        poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
        default_max_inflight: int = DEFAULT_MAX_INFLIGHT,
        route_tracker: Any = None,
    ):
        self.index = index
        self.bus = bus
        self.adapters = adapters
        self.gate_fn = gate_fn
        self.normalize_fn = normalize_fn
        self.manifests = manifests or {}
        self.clock = clock
        self.poll_timeout_s = poll_timeout_s
        self.default_max_inflight = default_max_inflight
        self.route_tracker = route_tracker
        self._cas_for = cas_for
        self._cas: dict[str, CAS] = {}
        self._ranked: set[str] = set()

    # ------------------------------------------------------------------ wiring

    def cas(self, project_id: str) -> CAS:
        if self._cas_for is not None:
            return self._cas_for(project_id)
        if project_id not in self._cas:
            root = self.index.project_root(project_id) or self.index.home / "projects" / project_id

            def record(key: str, ext: str, nbytes: int) -> None:
                try:
                    self.index.record_asset(project_id, key, ext, nbytes)
                except sqlite3.Error as e:  # the index is a cache: the object is safe in the folder
                    log.warning("asset %s not recorded in the %s index: %s", key, project_id, e)

            self._cas[project_id] = CAS(root / "assets", on_put=record)
        return self._cas[project_id]

    def adapter_for(self, route: Mapping[str, Any] | Run) -> Any:
        """The adapter for a route: `<engine>/<route_id or provider>`, then the engine id, then the
        provider, then `"*"`. A bare adapter (not a mapping) serves every route."""
        if isinstance(route, Run):
            route = route.route_dict
        a = self.adapters
        if a is None:
            return None
        if not isinstance(a, Mapping):
            return a
        for k in (route_key(route), route.get("engine_id"), route.get("provider"), "*"):
            if k in a:
                return a[k]
        return None

    def max_inflight(self, route: Mapping[str, Any]) -> int:
        m = self.manifests.get(route.get("engine_id"))
        if m is not None:
            for r in m.routes:
                if r.route_id == route.get("route_id") or r.provider == route.get("provider"):
                    return int(r.max_inflight)
        return self.default_max_inflight

    def _publish(self, project_id: str, run_id: str | None, event: dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(run_id, event, project_id=project_id)
        except Exception as e:  # the bus is for watchers; a failed publish never stops a run
            log.warning("event %s for %s not published: %s", event.get("kind"), run_id, e)

    # ------------------------------------------------------------------ advancing

    def _advance(
        self,
        conn: sqlite3.Connection,
        adapter: Any,
        project_id: str,
        run_id: str,
        actions: list[dict[str, Any]],
    ) -> RunState:
        st = get_run(conn, run_id).state
        while True:
            new = step(
                conn,
                adapter,
                run_id,
                self.clock,
                cas=self.cas(project_id),
                poll_timeout_s=self.poll_timeout_s,
                normalize_fn=self.normalize_fn,
                gate_fn=self.gate_fn,
                route_tracker=self.route_tracker,
            )
            if new is st:
                actions.append(
                    {"project_id": project_id, "run_id": run_id, "state": st.value, "action": "waiting"}
                )
                return st
            actions.append(
                {
                    "project_id": project_id,
                    "run_id": run_id,
                    "from": st.value,
                    "state": new.value,
                    "action": "advanced",
                }
            )
            self._publish(
                project_id, run_id, {"kind": "run.state", "run_id": run_id, "from": st.value, "to": new.value}
            )
            st = new
            if st not in IMMEDIATE_STATES:  # a result URL may expire: fetch, normalise and gate at once
                return st

    def _scan(self) -> tuple[list[tuple[Priority, str, str, str, RunState, dict[str, Any]]], Counter]:
        rows: list[tuple[Priority, str, str, str, RunState, dict[str, Any]]] = []
        inflight: Counter = Counter()
        states = tuple(s.value for s in WORKER_STATES)
        marks = ",".join("?" * len(states))
        for pid in self.index.project_ids():
            with self.index.project(pid) as conn:
                found = conn.execute(
                    f"SELECT id, state, priority, created_utc, route FROM runs WHERE state IN ({marks})",
                    states,
                ).fetchall()
            for r in found:
                route = json.loads(r["route"])
                state = RunState(r["state"])
                if state in INFLIGHT_STATES:
                    inflight[route_key(route)] += 1
                rows.append((Priority.parse(r["priority"]), r["created_utc"], r["id"], pid, state, route))
        rows.sort(key=lambda x: (int(x[0]), x[1], x[2]))
        return rows, inflight

    def tick(self) -> list[dict[str, Any]]:
        """One pass over every runnable run, most urgent first. Returns what happened to each."""
        actions: list[dict[str, Any]] = []
        rows, inflight = self._scan()
        touched_projects: set[str] = set()
        for _prio, _age, run_id, pid, state, route in rows:
            adapter = self.adapter_for(route)
            if adapter is None:
                actions.append(
                    {"project_id": pid, "run_id": run_id, "state": state.value, "action": "no_adapter"}
                )
                continue
            if state is S.QUEUED:
                rk = route_key(route)
                if inflight[rk] >= self.max_inflight(route):
                    actions.append(
                        {"project_id": pid, "run_id": run_id, "state": state.value, "action": "throttled"}
                    )
                    continue
                inflight[rk] += 1
            with self.index.project(pid) as conn:
                self._advance(conn, adapter, pid, run_id, actions)
            touched_projects.add(pid)
        for pid in touched_projects:
            actions.extend(self._rank_finished_groups(pid))
        return actions

    def run_until_idle(self, max_ticks: int = 1000) -> list[dict[str, Any]]:
        """Tick until no run is left in a worker state (for tests and the CLI's synchronous path)."""
        actions: list[dict[str, Any]] = []
        for _ in range(max_ticks):
            rows, _ = self._scan()
            if not rows:
                return actions
            actions.extend(self.tick())
        raise RuntimeError(f"runs still in flight after {max_ticks} ticks")

    # ------------------------------------------------------------------ boot

    def resume_on_boot(self) -> list[dict[str, Any]]:
        """Reconcile every project after a restart, then re-poll (never resubmit) every run the provider
        already has, so a finished job is fetched before its URL lapses."""
        actions: list[dict[str, Any]] = []
        for pid in self.index.project_ids():
            cas = self.cas(pid)
            with self.index.project(pid) as conn:
                for a in _recover(conn, self.adapter_for, self.clock, cas, None, self.route_tracker):
                    actions.append({"project_id": pid, **a})
                for rid in _non_terminal_ids(conn):
                    run = get_run(conn, rid)
                    if not run.provider_job_id or run.state not in (S.SUBMITTED, S.RUNNING, S.FETCHING):
                        continue
                    adapter = self.adapter_for(run)
                    if adapter is None:
                        actions.append(
                            {
                                "project_id": pid,
                                "run_id": rid,
                                "state": run.state.value,
                                "action": "no_adapter",
                            }
                        )
                        continue
                    self._advance(conn, adapter, pid, rid, actions)
        return actions

    # ------------------------------------------------------------------ best-of-N

    def _project_of(self, run_id: str, project_id: str | None) -> str:
        pid = project_id or self.index.locate_run(run_id)
        if pid is None:
            raise KeyError(f"run {run_id} is not in any project index")
        return pid

    def fan_out(self, run_id: str, n: int, project_id: str | None = None) -> list[Run]:
        pid = self._project_of(run_id, project_id)
        with self.index.project(pid) as conn:
            runs = fan_out(conn, run_id, n, now=self.clock())
        for r in runs:
            self.index.remember(r.id, pid)
        group = runs[0].sibling_group
        self._publish(pid, run_id, {"kind": "siblings.created", "group": group, "runs": [r.id for r in runs]})
        return runs

    def rank(self, sibling_group: str, project_id: str) -> list[Run]:
        with self.index.project(project_id) as conn:
            return rank_siblings(conn, sibling_group)

    def _rank_finished_groups(self, project_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self.index.project(project_id) as conn:
            groups = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT json_extract(quote_json, '$.sibling_group') FROM runs "
                    "WHERE json_extract(quote_json, '$.sibling_group') IS NOT NULL"
                )
            ]
            for g in groups:
                if g in self._ranked:
                    continue
                runs = siblings(conn, g)
                if not runs or any(r.state not in TERMINAL for r in runs):
                    continue
                ranked = sorted(runs, key=_rank_key)
                self._ranked.add(g)
                order = [r.id for r in ranked]
                out.append({"project_id": project_id, "group": g, "action": "ranked", "order": order})
                self._publish(
                    project_id,
                    None,
                    {
                        "kind": "siblings.ranked",
                        "group": g,
                        "order": order,
                        "scores": [r.gate_score for r in ranked],
                        "states": [r.state.value for r in ranked],
                    },
                )
        return out
