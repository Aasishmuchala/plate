"""The worker: one state transition per `step`, crash recovery that never resubmits blind.

`step(conn, engine, run_id, clock, sleep)` advances a run by at most one transition:
QUEUED -> STAGING (idempotency key + payload into the CAS), STAGING -> SUBMITTED (write a submit intent,
call `engine.submit`, persist the provider id before any poll), SUBMITTED -> RUNNING (first poll shows
running or succeeded), RUNNING -> FETCHING / REFUSED / FAILED(timeout), FETCHING -> NORMALIZING (fetch the
bytes into the CAS at once: result URLs expire), NORMALIZING -> GATING (stub normalisation),
GATING -> DONE or GATE_FAIL (stub gate; `params.force_gate_fail`). A poll that shows no progress leaves
the state alone. `run_to_completion` loops `step`, sleeping `poll_interval_s` between idle polls.

`recover_on_boot` handles what a dead process left behind: a run with a provider id keeps its state and
is re-polled (never resubmitted); a run in STAGING whose submit intent was written but whose provider id
was not is adopted through `engine.lookup` when the adapter has one, otherwise FAILED with
`submit_state_unknown` for a human to re-run; a STAGING run with no intent never reached the provider and
simply submits on its next step; an expired QUOTED run becomes EXPIRED. Steps on one run are serialised
by an in-process lock (the API's step and cancel routes run on a thread pool), and the daemon's
app-data lock keeps a second process away.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from .adapter import PollResult, ResultExpired, lookup_fn
from .cas import CAS
from .idempotency import canonical_json, idempotency_key
from .machine import IllegalTransition, annotate, get_run, transition
from .models import Run
from .paths import cas_root
from .quotes import expire_if_due
from .states import TERMINAL, TRANSITIONS, WORKER_STATES, RunState
from .timeutil import iso, utcnow

S = RunState
Clock = Callable[[], datetime]
Sleep = Callable[[float], None]

DEFAULT_POLL_TIMEOUT_S = 3600.0
DEFAULT_POLL_INTERVAL_S = 5.0
STUB_GATE_VERSION = "stub-0"

_locks_guard = threading.Lock()
_run_locks: dict[str, threading.RLock] = {}


class NotRunnable(Exception):
    """run_to_completion was asked to drive a run that waits on a human (DRAFT, QUOTED, APPROVED)."""


def run_lock(run_id: str) -> threading.RLock:
    with _locks_guard:
        lk = _run_locks.get(run_id)
        if lk is None:
            lk = _run_locks[run_id] = threading.RLock()
        return lk


def _fault(engine: Any, state: RunState, phase: str) -> None:
    fp = getattr(engine, "fault_point", None)
    if fp is not None:
        fp(state, phase)


def build_payload(run: Run) -> dict[str, Any]:
    """What goes to the provider: engine, inputs and params only. Project, shot and packet ids are C0 and
    never leave the box (PACKET.md section 12)."""
    return {"engine_id": run.route.engine_id, "inputs": run.inputs, "params": run.params}


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


def _timed_out(run: Run, now: datetime, poll_timeout_s: float) -> bool:
    started = run.submitted_utc or run.updated_utc
    return now - started >= timedelta(seconds=poll_timeout_s)


def _best_effort_cancel(engine: Any, provider_job_id: str) -> str:
    try:
        engine.cancel(provider_job_id)
        return "provider cancel requested"
    except Exception as e:  # reported in the event reason, never swallowed silently
        return f"provider cancel FAILED ({type(e).__name__}: {e}); check the provider console"


def _resolve_unknown_submit(
    conn: sqlite3.Connection, engine: Any, run: Run, actor: str, now: datetime
) -> RunState:
    """STAGING with a submit intent but no provider id: adopt via lookup, else fail for a human."""
    fn = lookup_fn(engine)
    job_id = None
    if fn is not None and run.idempotency_key:
        try:
            job_id = fn(run.idempotency_key)
        except Exception:  # an unanswerable lookup is the same as no lookup
            job_id = None
    if job_id:
        transition(
            conn,
            run.id,
            S.SUBMITTED,
            actor=actor,
            reason=f"adopted provider job {job_id} via idempotency-key lookup after a lost submit response",
            provider_job_id=job_id,
            submitted_utc=now,
        )
        return S.SUBMITTED
    transition(
        conn,
        run.id,
        S.FAILED,
        actor=actor,
        reason="a submit may have reached the provider but its job id was never persisted and the adapter "
        "cannot look it up; not resubmitting blind. Check the provider, then re-run as a new attempt.",
        reason_code="submit_state_unknown",
    )
    return S.FAILED


# ---------------------------------------------------------------------------- per-state handlers


def _queued(conn, engine, run: Run, clock: Clock, cas: CAS, poll_timeout_s: float, actor: str) -> RunState:
    key = idempotency_key(run.inputs, run.params, run.route_dict, run.attempt)
    payload_key = cas.put_bytes(canonical_json(build_payload(run)))
    _fault(engine, S.QUEUED, "after_effect")
    transition(conn, run.id, S.STAGING, actor=actor, idempotency_key=key, payload_key=payload_key)
    return S.STAGING


def _staging(conn, engine, run: Run, clock: Clock, cas: CAS, poll_timeout_s: float, actor: str) -> RunState:
    if run.submit_started_utc is not None:
        # A previous submit may have reached the provider. Never call submit again for this attempt.
        return _resolve_unknown_submit(conn, engine, run, actor, clock())
    if run.payload_key and cas.has(run.payload_key):
        payload = json.loads(cas.read_bytes(run.payload_key).decode("utf-8"))
    else:
        payload = build_payload(run)
    # Write-ahead intent: committed before the provider is called, so recovery can tell "never sent"
    # (no intent: safe to submit) from "maybe sent" (intent, no id: lookup or fail).
    annotate(conn, run.id, expect=S.STAGING, submit_started_utc=clock())
    try:
        job_id = engine.submit(payload, run.idempotency_key)
    except Exception as e:  # recorded as the failure reason
        transition(
            conn,
            run.id,
            S.FAILED,
            actor=actor,
            reason=f"submit raised {type(e).__name__}: {e}"[:500],
            reason_code="submit_error",
        )
        return S.FAILED
    _fault(engine, S.STAGING, "after_effect")
    transition(
        conn,
        run.id,
        S.SUBMITTED,
        actor=actor,
        reason=f"submitted as {job_id}",
        provider_job_id=job_id,
        submitted_utc=clock(),
    )
    return S.SUBMITTED


def _polling(conn, engine, run: Run, clock: Clock, cas: CAS, poll_timeout_s: float, actor: str) -> RunState:
    st = run.state
    res: PollResult = engine.poll(run.provider_job_id)
    _fault(engine, st, "after_effect")
    if res.status == "refused":
        transition(
            conn,
            run.id,
            S.REFUSED,
            actor=actor,
            reason=res.detail or "provider refused",
            reason_code="refused",
        )
        return S.REFUSED
    if res.status == "failed":
        transition(
            conn,
            run.id,
            S.FAILED,
            actor=actor,
            reason=res.detail or "provider failed",
            reason_code="provider_failed",
        )
        return S.FAILED
    if st is S.SUBMITTED and res.status in ("running", "succeeded"):
        transition(conn, run.id, S.RUNNING, actor=actor)
        return S.RUNNING
    if st is S.RUNNING and res.status == "succeeded":
        if not res.result_url:
            transition(
                conn,
                run.id,
                S.FAILED,
                actor=actor,
                reason="succeeded without a result URL",
                reason_code="no_result",
            )
            return S.FAILED
        transition(conn, run.id, S.FETCHING, actor=actor, result_url=res.result_url)
        return S.FETCHING
    if _timed_out(run, clock(), poll_timeout_s):
        note = _best_effort_cancel(engine, run.provider_job_id)
        transition(
            conn,
            run.id,
            S.FAILED,
            actor=actor,
            reason=f"no result within {poll_timeout_s:g} s of submit; {note}",
            reason_code="timeout",
        )
        return S.FAILED
    return st


def _fetching(conn, engine, run: Run, clock: Clock, cas: CAS, poll_timeout_s: float, actor: str) -> RunState:
    try:
        data = engine.fetch(run.result_url)
    except ResultExpired as e:
        transition(conn, run.id, S.FAILED, actor=actor, reason=str(e)[:500], reason_code="result_url_expired")
        return S.FAILED
    except Exception as e:  # recorded as the failure reason
        transition(
            conn,
            run.id,
            S.FAILED,
            actor=actor,
            reason=f"fetch raised {type(e).__name__}: {e}"[:500],
            reason_code="fetch_error",
        )
        return S.FAILED
    key = cas.put_bytes(data)
    _fault(engine, S.FETCHING, "after_effect")
    transition(
        conn, run.id, S.NORMALIZING, actor=actor, reason=f"{len(data)} bytes into the CAS", result_key=key
    )
    return S.NORMALIZING


def _normalizing(
    conn, engine, run: Run, clock: Clock, cas: CAS, poll_timeout_s: float, actor: str
) -> RunState:
    if not run.result_key or not cas.verify(run.result_key):
        transition(
            conn,
            run.id,
            S.FAILED,
            actor=actor,
            reason="result blob missing or corrupt",
            reason_code="result_missing",
        )
        return S.FAILED
    normalized = {"normalized": True, "stub": True, "result_key": run.result_key}
    _fault(engine, S.NORMALIZING, "after_effect")
    transition(conn, run.id, S.GATING, actor=actor, normalized=normalized)
    return S.GATING


def _gating(conn, engine, run: Run, clock: Clock, cas: CAS, poll_timeout_s: float, actor: str) -> RunState:
    failed = bool(run.params.get("force_gate_fail"))
    report = stub_gate_report(run, failed, clock())
    _fault(engine, S.GATING, "after_effect")
    if failed:
        transition(
            conn,
            run.id,
            S.GATE_FAIL,
            actor=actor,
            reason="stub gate forced to fail (params.force_gate_fail)",
            gate_report=report,
            reason_code="gate_fail_stub",
        )
        return S.GATE_FAIL
    transition(conn, run.id, S.DONE, actor=actor, gate_report=report)
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
) -> RunState:
    """Advance one run by at most one transition and return its state. Never sleeps (`sleep` is taken
    for signature parity with `run_to_completion`). Runs waiting on a human are returned unchanged."""
    cas = cas or CAS(cas_root())
    with run_lock(run_id):
        run = get_run(conn, run_id)
        st = run.state
        if st in TERMINAL or st not in WORKER_STATES:
            return st
        _fault(engine, st, "before")
        new = _HANDLERS[st](conn, engine, run, clock, cas, poll_timeout_s, actor)
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
) -> RunState:
    cas = cas or CAS(cas_root())
    for _ in range(max_steps):
        before = get_run(conn, run_id).state
        if before in TERMINAL:
            return before
        if before not in WORKER_STATES:
            raise NotRunnable(
                f"run {run_id} is {before.value}: it waits on a quote, approval or queue action"
            )
        after = step(conn, engine, run_id, clock, sleep, cas=cas, poll_timeout_s=poll_timeout_s)
        if after in TERMINAL:
            return after
        if after is before:
            sleep(poll_interval_s)
    raise RuntimeError(f"run {run_id} did not finish within {max_steps} steps")


def recover_on_boot(conn: sqlite3.Connection, engine: Any, clock: Clock = utcnow) -> list[dict[str, str]]:
    """Reconcile every non-terminal run after a restart. Returns one action record per run touched."""
    actions: list[dict[str, str]] = []
    terminal = tuple(s.value for s in TERMINAL)
    marks = ",".join("?" * len(terminal))
    ids = [
        r[0]
        for r in conn.execute(
            f"SELECT id FROM runs WHERE state NOT IN ({marks}) ORDER BY created_utc", terminal
        )
    ]
    for rid in ids:
        with run_lock(rid):
            run = get_run(conn, rid)
            if run.provider_job_id:
                actions.append({"run_id": rid, "state": run.state.value, "action": "repoll"})
            elif run.state is S.STAGING and run.submit_started_utc is not None:
                new = _resolve_unknown_submit(conn, engine, run, "recovery", clock())
                actions.append(
                    {
                        "run_id": rid,
                        "state": new.value,
                        "action": "adopted" if new is S.SUBMITTED else "failed",
                    }
                )
            elif run.state is S.QUOTED and expire_if_due(conn, rid, clock()):
                actions.append({"run_id": rid, "state": S.EXPIRED.value, "action": "expired"})
    return actions


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
