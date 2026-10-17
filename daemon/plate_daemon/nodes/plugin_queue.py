"""The plugin job queue: what the daemon asks the Max dock to do (Light match, Scout, Vantage preview).

A numeric node that needs the scene (Light, Scout) cannot run in the daemon: it enqueues a job here and
the dock, polling `GET /api/v0/plugin/jobs?since=<seq>`, picks it up, does the work in Max and answers
`POST /api/v0/plugin/jobs/{id}/result`. The queue is one SQLite file in the app-data folder
(`plugin_queue.sqlite`, WAL, opened per call): it holds work in flight, not project truth, so losing it
costs a re-queue and never a result (the run it belongs to keeps its own state in the project index).

    enqueue(action=..., project_id=..., payload=..., key=...)   -> Job   (the same key returns the same job)
    list_jobs(since=0, claim=False)                             -> [Job]  queued jobs after `since`
    finish(job_id, ok=True, result={...})                       -> Job    queued/claimed -> done/failed
    cancel(job_id)                                              -> Job    queued/claimed -> cancelled
    last_poll_utc() / counts()                                  -> when a dock last claimed, jobs waiting

States: `queued -> claimed -> done | failed`, or `-> cancelled`. `seq` is the cursor a dock keeps: it only
grows. A job that carries a `run_id` belongs to a node run, whose engine polls the job (nodes/runner.py); a
`receive` job (push a take into Max) also carries the run it shows, but no engine waits for it.

Every `claim=True` listing stamps `last_poll_utc`, even when it finds nothing: that is how the UI knows a dock is
alive (`GET /plugin/status`). The stamp lives in the same file, so it survives a daemon restart.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..db import connect, dumps, loads, tx
from ..idempotency import new_id
from ..timeutil import iso, utcnow

__all__ = [
    "ACTIONS",
    "ACTIVE_STATES",
    "FILE_NAME",
    "Job",
    "MAX_PAYLOAD_BYTES",
    "MAX_RESULT_BYTES",
    "RECEIVE_MODES",
    "JobStateError",
    "PluginQueue",
    "UnknownAction",
]

FILE_NAME = "plugin_queue.sqlite"
# What the dock knows how to do. A new action is a new entry here and a handler in plate_max.
ACTIONS = ("light.match", "light.vantage_preview", "scout", "receive")
RECEIVE_MODES = ("vfb_history", "viewport_background")  # where a `receive` job shows the take in Max
POLL_KEY = "last_poll_utc"
ACTIVE_STATES = ("queued", "claimed")
STATES = ("queued", "claimed", "done", "failed", "cancelled")
MAX_LIST = 200
MAX_PAYLOAD_BYTES = 64 * 1024  # a job's arguments: ids, hashes, a few numbers
MAX_RESULT_BYTES = 1024 * 1024  # a dock's answer: poses and packet ids, never pixels

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS plugin_queue (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        id TEXT NOT NULL UNIQUE,
        idempotency_key TEXT UNIQUE,
        action TEXT NOT NULL,
        project_id TEXT NOT NULL,
        packet_id TEXT,
        run_id TEXT,
        node_id TEXT,
        payload TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('queued', 'claimed', 'done', 'failed', 'cancelled')),
        result TEXT,
        created_utc TEXT NOT NULL,
        claimed_utc TEXT,
        finished_utc TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS plugin_queue_state ON plugin_queue(state, seq)",
    "CREATE INDEX IF NOT EXISTS plugin_queue_run ON plugin_queue(run_id)",
    "CREATE TABLE IF NOT EXISTS plugin_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


class UnknownAction(ValueError):
    """The dock has no handler for this action."""


class JobStateError(RuntimeError):
    """A result or cancel for a job that is not queued or claimed (already finished, or unknown)."""

    def __init__(self, job_id: str, state: str | None):
        self.job_id = job_id
        self.state = state
        super().__init__(
            f"plugin job {job_id} is {state or 'unknown'}; only queued or claimed jobs take this"
        )


@dataclass(frozen=True)
class Job:
    seq: int
    id: str
    action: str
    project_id: str
    state: str
    payload: dict[str, Any]
    created_utc: str
    packet_id: str | None = None
    run_id: str | None = None
    node_id: str | None = None
    idempotency_key: str | None = None
    result: dict[str, Any] | None = None
    claimed_utc: str | None = None
    finished_utc: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        d = dict(row)
        d["payload"] = loads(d["payload"]) or {}
        d["result"] = loads(d["result"])
        return cls(**d)

    def to_dict(self) -> dict[str, Any]:
        """The wire form the dock reads: the action's arguments sit at the top level next to the bookkeeping."""
        return {
            **self.payload,
            "id": self.id,
            "seq": self.seq,
            "action": self.action,
            "state": self.state,
            "project_id": self.project_id,
            "packet_id": self.packet_id,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "created_utc": self.created_utc,
            "claimed_utc": self.claimed_utc,
            "finished_utc": self.finished_utc,
            "result": self.result,
        }


class PluginQueue:
    """See the module docstring. Cheap to construct: the file and its schema are made on first use."""

    def __init__(self, home: Path | str, *, clock: Any = utcnow):
        self.path = Path(home) / FILE_NAME
        self.clock = clock
        self._ready = False
        self._guard = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        conn = connect(self.path)
        if not self._ready:
            with self._guard:
                if not self._ready:
                    with tx(conn):
                        for stmt in SCHEMA:
                            conn.execute(stmt)
                    self._ready = True
        return conn

    def _now(self, now: datetime | None) -> str:
        return iso(now or self.clock())

    # ------------------------------------------------------------------ writing

    def enqueue(
        self,
        *,
        action: str,
        project_id: str,
        payload: dict[str, Any] | None = None,
        packet_id: str | None = None,
        run_id: str | None = None,
        node_id: str | None = None,
        key: str | None = None,
        now: datetime | None = None,
    ) -> Job:
        """Queue one job. A `key` seen before returns the job it made (a re-submit after a crash must not
        make the dock do the work twice). `payload` must be JSON (no NaN)."""
        if action not in ACTIONS:
            raise UnknownAction(f"unknown plugin action {action!r}; the dock handles {', '.join(ACTIONS)}")
        body = dumps(json.loads(json.dumps(payload or {}, allow_nan=False)))
        if len(body.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"a plugin job's arguments are over {MAX_PAYLOAD_BYTES} bytes")
        job_id = new_id("pj", now or self.clock())
        conn = self._conn()
        try:
            with tx(conn):
                if key is not None:
                    hit = conn.execute(
                        "SELECT * FROM plugin_queue WHERE idempotency_key = ?", (key,)
                    ).fetchone()
                    if hit is not None:
                        return Job.from_row(hit)
                conn.execute(
                    """INSERT INTO plugin_queue (id, idempotency_key, action, project_id, packet_id, run_id,
                                                 node_id, payload, state, created_utc)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)""",
                    (job_id, key, action, project_id, packet_id, run_id, node_id, body, self._now(now)),
                )
            return Job.from_row(conn.execute("SELECT * FROM plugin_queue WHERE id = ?", (job_id,)).fetchone())
        finally:
            conn.close()

    def finish(
        self,
        job_id: str,
        *,
        ok: bool = True,
        result: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> Job:
        """The dock's answer: queued or claimed -> done (ok) or failed. Raises JobStateError otherwise."""
        text = dumps(json.loads(json.dumps(result or {}, allow_nan=False)))
        if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValueError(f"a plugin job's result is over {MAX_RESULT_BYTES} bytes; send ids, not pixels")
        return self._close(job_id, "done" if ok else "failed", text, now)

    def cancel(self, job_id: str, *, reason: str | None = None, now: datetime | None = None) -> Job:
        """queued or claimed -> cancelled (a cancelled run stops asking the dock for the work)."""
        text = dumps({"cancelled": True, "reason": reason}) if reason else None
        return self._close(job_id, "cancelled", text, now)

    def _close(self, job_id: str, state: str, result_text: str | None, now: datetime | None) -> Job:
        conn = self._conn()
        try:
            with tx(conn):
                row = conn.execute("SELECT state FROM plugin_queue WHERE id = ?", (job_id,)).fetchone()
                if row is None or row["state"] not in ACTIVE_STATES:
                    raise JobStateError(job_id, row["state"] if row else None)
                conn.execute(
                    "UPDATE plugin_queue SET state = ?, result = COALESCE(?, result), finished_utc = ? WHERE id = ?",
                    (state, result_text, self._now(now), job_id),
                )
            return Job.from_row(conn.execute("SELECT * FROM plugin_queue WHERE id = ?", (job_id,)).fetchone())
        finally:
            conn.close()

    # ------------------------------------------------------------------ reading

    def get(self, job_id: str) -> Job | None:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM plugin_queue WHERE id = ?", (job_id,)).fetchone()
        finally:
            conn.close()
        return Job.from_row(row) if row is not None else None

    def by_key(self, key: str) -> Job | None:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM plugin_queue WHERE idempotency_key = ?", (key,)).fetchone()
        finally:
            conn.close()
        return Job.from_row(row) if row is not None else None

    def for_run(self, run_id: str) -> list[Job]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM plugin_queue WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
        finally:
            conn.close()
        return [Job.from_row(r) for r in rows]

    def list_jobs(
        self,
        since: int = 0,
        *,
        states: tuple[str, ...] = ("queued",),
        claim: bool = False,
        limit: int = 50,
        now: datetime | None = None,
    ) -> list[Job]:
        """Jobs after cursor `since` (their `seq`), oldest first. With `claim`, the queued ones returned are
        marked claimed in the same transaction, so a second dock never picks the same job."""
        bad = [s for s in states if s not in STATES]
        if bad:
            raise ValueError(f"unknown job states {bad}")
        marks = ",".join("?" * len(states))
        conn = self._conn()
        try:
            with tx(conn):
                rows = conn.execute(
                    f"SELECT * FROM plugin_queue WHERE seq > ? AND state IN ({marks}) ORDER BY seq LIMIT ?",
                    (max(int(since), 0), *states, max(1, min(int(limit), MAX_LIST))),
                ).fetchall()
                if claim:
                    stamp = self._now(now)
                    conn.execute(
                        """INSERT INTO plugin_meta (key, value) VALUES (?, ?)
                           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                        (POLL_KEY, stamp),
                    )
                    for r in rows:
                        if r["state"] == "queued":
                            conn.execute(
                                "UPDATE plugin_queue SET state = 'claimed', claimed_utc = ? WHERE id = ?",
                                (stamp, r["id"]),
                            )
                    rows = [
                        conn.execute("SELECT * FROM plugin_queue WHERE id = ?", (r["id"],)).fetchone()
                        for r in rows
                    ]
        finally:
            conn.close()
        return [Job.from_row(r) for r in rows]

    def last_poll_utc(self) -> str | None:
        """When a dock last listed with `claim=True` (ISO-8601 UTC), or None when none ever has."""
        conn = self._conn()
        try:
            row = conn.execute("SELECT value FROM plugin_meta WHERE key = ?", (POLL_KEY,)).fetchone()
        finally:
            conn.close()
        return str(row["value"]) if row else None

    def counts(self) -> dict[str, int]:
        """`{queued, claimed}`: the jobs waiting for a dock and the ones a dock has taken and not answered."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM plugin_queue WHERE state IN ('queued', 'claimed') GROUP BY state"
            ).fetchall()
        finally:
            conn.close()
        found = {r["state"]: int(r["n"]) for r in rows}
        return {"queued": found.get("queued", 0), "claimed": found.get("claimed", 0)}
