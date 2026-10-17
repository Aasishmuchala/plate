"""The only writer of run state: `transition` checks TRANSITIONS and appends a run event atomically.

`transition(conn, run_id, to, *, actor, reason=None, **fields)` reads the row, refuses an illegal move
with `IllegalTransition(from, to)`, writes the new state plus any whitelisted fields and appends one
`run_events` row (`seq` contiguous per run), all inside one `BEGIN IMMEDIATE` transaction or savepoint.
A move to SUBMITTED must carry a non-empty `provider_job_id` (the recovery rule depends on it).
`annotate` updates bookkeeping fields without a state change (the submit intent marker) and is guarded
by the expected state so a racing transition can never be overwritten.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from typing import Any

from .db import dumps, tx
from .models import Run, RunCreate, RunEvent
from .states import TRANSITIONS, RunState
from .timeutil import iso, utcnow


class IllegalTransition(Exception):
    def __init__(self, from_state: RunState, to_state: RunState):
        self.from_state = RunState(from_state)
        self.to_state = RunState(to_state)
        super().__init__(f"illegal transition {self.from_state.value} -> {self.to_state.value}")


class RunNotFound(KeyError):
    pass


JSON_FIELDS = {"gate_report", "normalized"}
TIME_FIELDS = {"submit_started_utc", "submitted_utc"}
WRITABLE_FIELDS = {
    "idempotency_key",
    "payload_key",
    "submit_started_utc",
    "provider_job_id",
    "submitted_utc",
    "result_url",
    "quote_id",
    "result_key",
    "normalized",
    "gate_report",
    "reason_code",
}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


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
    now_s = iso(now or utcnow())
    rid = new_id("run")
    with tx(conn):
        conn.execute(
            """INSERT INTO runs (id, project_id, packet_id, state, priority, inputs, params, route, attempt,
                                 created_utc, updated_utc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rid,
                spec.project_id,
                spec.packet_id,
                RunState.DRAFT.value,
                spec.priority,
                dumps(spec.inputs),
                dumps(spec.params),
                dumps(spec.route.model_dump()),
                spec.attempt,
                now_s,
                now_s,
            ),
        )
    return get_run(conn, rid)


def get_run(conn: sqlite3.Connection, run_id: str) -> Run:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise RunNotFound(run_id)
    return Run.from_row(row)


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
        (run_id, seq, kind, from_state.value, to_state.value, actor, reason, at),
    )


def append_note(conn: sqlite3.Connection, run_id: str, *, kind: str, actor: str, reason: str) -> None:
    """A non-transition event (e.g. an export override): from_state == to_state == current state."""
    with tx(conn):
        run = get_run(conn, run_id)
        _append_event(conn, run_id, run.state, run.state, actor, reason, iso(utcnow()), kind=kind)


def transition(
    conn: sqlite3.Connection,
    run_id: str,
    to: RunState | str,
    *,
    actor: str,
    reason: str | None = None,
    **fields: Any,
) -> Run:
    to = RunState(to)
    with tx(conn):
        row = conn.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        frm = RunState(row["state"])
        if to not in TRANSITIONS[frm]:
            raise IllegalTransition(frm, to)
        if to is RunState.SUBMITTED:
            pid = fields.get("provider_job_id")
            if not isinstance(pid, str) or not pid.strip():
                raise ValueError("provider_job_id required")
        enc = _encode(fields)
        now = iso(utcnow())
        cols = ", ".join(f"{k} = ?" for k in enc)
        sets = f"state = ?, updated_utc = ?{', ' + cols if cols else ''}"
        cur = conn.execute(
            f"UPDATE runs SET {sets} WHERE id = ? AND state = ?",
            (to.value, now, *enc.values(), run_id, frm.value),
        )
        if cur.rowcount != 1:  # pragma: no cover - impossible under BEGIN IMMEDIATE
            raise IllegalTransition(frm, to)
        _append_event(conn, run_id, frm, to, actor, reason, now)
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
