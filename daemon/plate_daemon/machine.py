"""The only writer of run state: `transition` checks TRANSITIONS and the guards, appends a run event atomically.

`transition(conn, run_id, to, *, actor, reason=None, now=None, **fields)` reads the run, refuses an illegal
move with `IllegalTransition(from, to)`, writes the new state plus any whitelisted fields and appends one
`run_events` row (`seq` contiguous per run), all inside one `BEGIN IMMEDIATE` transaction or savepoint.
`apply(run, to, reason)` is the same check as a pure function on a `Run` (no IO), which the property suite
drives through every pair of states. The guards:

- QUOTED -> APPROVED needs `approved_by` (`human`, `auto` or `agent:<envelope_id>`; an actor of that form
  counts) and an unexpired quote; past `quote_expires_utc` the run is committed to EXPIRED instead and
  `QuoteExpired` is raised. Approval reserves the quoted spend (`budgets.reserve`) in the same transaction.
- -> STAGING computes `idempotency_key` from the run (`staging_key`); a second run with the same key is a
  duplicate request (`DuplicateRequest`): re-run as a new attempt instead.
- -> SUBMITTED needs `provider_job_id` already persisted (two-phase: the worker `annotate`s the id the
  moment the provider answers, then transitions), so no crash can leave a submitted run recovery cannot
  re-poll.
- Any terminal state closes the run's spend: refunded when the provider never had the job, settled
  otherwise (`budgets.close_out`).

`annotate` updates bookkeeping fields without a state change (the submit intent and the provider id) and
is guarded by the expected state so a racing transition can never be overwritten.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from . import budgets
from .db import dumps, loads, tx
from .idempotency import idempotency_key, input_hashes, new_id, route_id
from .models import APPROVED_BY_RE, HistoryEntry, Quote, Run, RunCreate, RunEvent, quote_from_terms
from .states import TERMINAL, TRANSITIONS, RunState
from .timeutil import iso, utcnow

__all__ = [
    "DuplicateRequest",
    "IllegalTransition",
    "MissingApprover",
    "MissingProviderJobId",
    "QuoteExpired",
    "RunNotFound",
    "annotate",
    "append_note",
    "apply",
    "create_run",
    "events_after",
    "get_run",
    "new_id",
    "run_events",
    "staging_key",
    "transition",
]


class IllegalTransition(Exception):
    def __init__(self, from_state: RunState, to_state: RunState):
        self.from_state = RunState(from_state)
        self.to_state = RunState(to_state)
        super().__init__(f"illegal transition {self.from_state.value} -> {self.to_state.value}")


class RunNotFound(KeyError):
    pass


class QuoteExpired(Exception):
    """The run's quote lapsed before approval; the run is EXPIRED and must be re-quoted."""


class MissingApprover(ValueError):
    """APPROVED needs approved_by: human, auto or agent:<envelope_id>."""


class MissingProviderJobId(ValueError):
    """SUBMITTED needs the provider job id persisted first."""


class DuplicateRequest(Exception):
    """Another run already staged the identical request (same inputs, params, route and attempt)."""

    def __init__(self, run_id: str, other_run_id: str | None, key: str):
        self.run_id = run_id
        self.other_run_id = other_run_id
        self.key = key
        super().__init__(
            f"run {run_id} is the same request as run {other_run_id} (idempotency key {key[:16]}...); "
            "re-run it as a new attempt"
        )


JSON_FIELDS = {"gate_report", "normalized", "quote_json"}
TIME_FIELDS = {"submit_started_utc", "submitted_utc"}
WRITABLE_FIELDS = {
    "idempotency_key",
    "payload_key",
    "submit_started_utc",
    "provider_job_id",
    "submitted_utc",
    "result_url",
    "quote_id",
    "quote_json",
    "result_key",
    "normalized",
    "gate_report",
    "reason_code",
}


def staging_key(run: Run) -> str:
    """The run's idempotency key: input hashes, params, route id, attempt (and sibling index)."""
    return idempotency_key(
        input_hashes(run.inputs),
        run.params,
        route_id(run.route_dict),
        run.attempt,
        sibling_index=run.sibling_index,
    )


def approver_from_actor(actor: str | None) -> str | None:
    return actor if isinstance(actor, str) and APPROVED_BY_RE.match(actor) else None


def _check(
    run: Run, to: RunState, now: datetime, actor: str, fields: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """Table and guards. Returns the fields to write and the approver; raises on any refusal."""
    frm = run.state
    if to not in TRANSITIONS[frm]:
        raise IllegalTransition(frm, to)
    fields = dict(fields)
    approved_by = fields.pop("approved_by", None)
    unknown = set(fields) - WRITABLE_FIELDS
    if unknown:
        raise TypeError(f"not writable run fields: {sorted(unknown)}")
    if to is RunState.APPROVED:
        approved_by = approved_by or approver_from_actor(actor)
        if not isinstance(approved_by, str) or not APPROVED_BY_RE.match(approved_by):
            raise MissingApprover(
                f"APPROVED needs approved_by 'human', 'auto' or 'agent:<envelope_id>', got {approved_by!r}"
            )
        if run.quote is not None and run.quote.expired(now):
            raise QuoteExpired(f"quote {run.quote.id} expired at {iso(run.quote.expires_utc)}")
    elif approved_by is not None:
        raise TypeError("approved_by is only written by the move to APPROVED")
    if to is RunState.STAGING:
        key = staging_key(run)
        if fields.get("idempotency_key") not in (None, key):
            raise ValueError("idempotency_key is computed at STAGING; a different one was passed")
        fields["idempotency_key"] = key
    if to is RunState.SUBMITTED:
        pid = run.provider_job_id
        if not isinstance(pid, str) or not pid.strip():
            raise MissingProviderJobId(
                "SUBMITTED needs provider_job_id persisted first (annotate it, then transition)"
            )
        if fields.pop("provider_job_id", pid) != pid:
            raise ValueError(f"provider_job_id differs from the persisted {pid!r}")
    return fields, approved_by


def apply(
    run: Run,
    to: RunState | str,
    reason: str | None = None,
    *,
    now: datetime | None = None,
    actor: str = "system",
    **fields: Any,
) -> Run:
    """The transition as a pure function: the new Run, or IllegalTransition / a guard's exception."""
    to = RunState(to)
    now = now or utcnow()
    write, approved_by = _check(run, to, now, actor, fields)
    entry = HistoryEntry(from_=run.state, to=to, at=now, reason=reason, actor=actor)
    update: dict[str, Any] = {"state": to, "updated_utc": now, "history": [*run.history, entry], **write}
    if approved_by is not None:
        update["approved_by"] = approved_by
    return run.model_copy(update=update)


def _encode(fields: dict[str, Any]) -> dict[str, Any]:
    unknown = set(fields) - WRITABLE_FIELDS
    if unknown:
        raise TypeError(f"not writable run fields: {sorted(unknown)}")
    out = {}
    for k, v in fields.items():
        if k in JSON_FIELDS and v is not None:
            v = dumps(v)
        elif k in TIME_FIELDS and isinstance(v, datetime):
            v = iso(v)
        out[k] = v
    return out


def create_run(conn: sqlite3.Connection, spec: RunCreate, *, now: datetime | None = None) -> Run:
    """Insert a DRAFT run. The packet must already be in this project's index (a run is anchored to a
    packet's camera: doctrine 2)."""
    now = now or utcnow()
    now_s = iso(now)
    rid = new_id("run", now)
    with tx(conn):
        conn.execute(
            """INSERT INTO runs (id, project_id, packet_id, node_id, state, priority, inputs, params, route,
                                 attempt, created_utc, updated_utc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rid,
                spec.project_id,
                spec.packet_id,
                spec.node_id,
                RunState.DRAFT.value,
                spec.priority.label,
                dumps(spec.inputs),
                dumps(spec.params),
                dumps(spec.route.model_dump()),
                spec.attempt,
                now_s,
                now_s,
            ),
        )
    return get_run(conn, rid)


def load_quote(conn: sqlite3.Connection, row: sqlite3.Row) -> Quote | None:
    """The run's quote: terms from `runs.quote_json`, approval from the `quotes` row."""
    terms = loads(row["quote_json"]) if "quote_json" in row.keys() else None
    if row["quote_id"]:
        qrow = conn.execute("SELECT * FROM quotes WHERE id = ?", (row["quote_id"],)).fetchone()
        if qrow is not None:
            same = isinstance(terms, dict) and terms.get("id") == row["quote_id"]
            return Quote.from_row(qrow, terms if same else None)
    if isinstance(terms, dict) and terms.get("id"):
        return quote_from_terms(terms)  # an index rebuilt from the ledger
    return None


def get_run(conn: sqlite3.Connection, run_id: str) -> Run:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise RunNotFound(run_id)
    events = conn.execute(
        "SELECT kind, from_state, to_state, actor, reason, at_utc FROM run_events WHERE run_id = ? ORDER BY seq",
        (run_id,),
    ).fetchall()
    return Run.from_row(row, quote=load_quote(conn, row), events=events)


def _append_event(
    conn: sqlite3.Connection,
    run_id: str,
    from_state: RunState,
    to_state: RunState,
    actor: str,
    reason: str | None,
    at: str,
    kind: str = "transition",
) -> None:
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    conn.execute(
        """INSERT INTO run_events (run_id, seq, kind, from_state, to_state, actor, reason, at_utc)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, seq, kind, RunState(from_state).value, RunState(to_state).value, actor, reason, at),
    )


def append_note(conn: sqlite3.Connection, run_id: str, *, kind: str, actor: str, reason: str) -> None:
    """A non-transition event (e.g. an export override): from_state == to_state == current state."""
    with tx(conn):
        run = get_run(conn, run_id)
        _append_event(conn, run_id, run.state, run.state, actor, reason, iso(utcnow()), kind=kind)


def _persist(
    conn: sqlite3.Connection,
    run: Run,
    to: RunState,
    actor: str,
    reason: str | None,
    now: datetime,
    fields: dict[str, Any],
    approved_by: str | None,
) -> None:
    enc = _encode(fields)
    now_s = iso(now)
    cols = ", ".join(f"{k} = ?" for k in enc)
    sets = f"state = ?, updated_utc = ?{', ' + cols if cols else ''}"
    try:
        cur = conn.execute(
            f"UPDATE runs SET {sets} WHERE id = ? AND state = ?",
            (to.value, now_s, *enc.values(), run.id, run.state.value),
        )
    except sqlite3.IntegrityError:
        key = enc.get("idempotency_key")
        if key is None:
            raise
        other = conn.execute(
            "SELECT id FROM runs WHERE idempotency_key = ? AND id != ?", (key, run.id)
        ).fetchone()
        if other is None:
            raise
        raise DuplicateRequest(run.id, other[0], key) from None
    if cur.rowcount != 1:  # pragma: no cover - impossible under BEGIN IMMEDIATE
        raise IllegalTransition(run.state, to)
    if to is RunState.APPROVED and run.quote_id:
        conn.execute(
            "UPDATE quotes SET approved_by = ?, approved_utc = ? WHERE id = ?",
            (approved_by, now_s, run.quote_id),
        )
    _append_event(conn, run.id, run.state, to, actor, reason, now_s)
    if to is RunState.APPROVED:
        budgets.reserve(conn, get_run(conn, run.id), approved_by or actor, now)
    elif to in TERMINAL:
        budgets.close_out(conn, get_run(conn, run.id), now)


def transition(
    conn: sqlite3.Connection,
    run_id: str,
    to: RunState | str,
    *,
    actor: str,
    reason: str | None = None,
    now: datetime | None = None,
    **fields: Any,
) -> Run:
    to = RunState(to)
    now = now or utcnow()
    expired: QuoteExpired | None = None
    with tx(conn):
        run = get_run(conn, run_id)
        try:
            write, approved_by = _check(run, to, now, actor, fields)
        except QuoteExpired as e:
            _persist(conn, run, RunState.EXPIRED, "system", str(e), now, {}, None)
            expired = e
        else:
            _persist(conn, run, to, actor, reason, now, write, approved_by)
    if expired is not None:
        raise expired
    return get_run(conn, run_id)


def annotate(conn: sqlite3.Connection, run_id: str, *, expect: RunState, **fields: Any) -> Run:
    """Write bookkeeping fields without changing state; refuses if the run left `expect` meanwhile."""
    enc = _encode(fields)
    with tx(conn):
        cols = ", ".join(f"{k} = ?" for k in enc)
        cur = conn.execute(
            f"UPDATE runs SET {cols}, updated_utc = ? WHERE id = ? AND state = ?",
            (*enc.values(), iso(utcnow()), run_id, RunState(expect).value),
        )
        if cur.rowcount != 1:
            run = get_run(conn, run_id)
            raise IllegalTransition(run.state, expect)
        return get_run(conn, run_id)


def run_events(conn: sqlite3.Connection, run_id: str) -> list[RunEvent]:
    rows = conn.execute("SELECT * FROM run_events WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()
    return [RunEvent.from_row(r) for r in rows]


def events_after(conn: sqlite3.Connection, after: int, limit: int = 500) -> list[RunEvent]:
    rows = conn.execute(
        "SELECT * FROM run_events WHERE id > ? ORDER BY id LIMIT ?", (after, limit)
    ).fetchall()
    return [RunEvent.from_row(r) for r in rows]
