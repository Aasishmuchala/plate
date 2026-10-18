"""The daemon's event bus: durable events in the project index, live fan-out to SSE subscribers.

`Bus.publish(run_id, event)` appends `{seq, run_id, at, kind, json}` to the project's `events` table (seq
is the SQLite AUTOINCREMENT, so it never repeats within one index epoch) and wakes every subscriber; it may be
called from any thread (thread-pool routes, the worker). It never creates an index: an event for a project
that has none raises `UnknownProject`. `Bus.subscribe(filter)` is an async iterator that first replays the
stored events after `filter.after` (the browser's `Last-Event-ID`), then follows new ones. The table is the
source: a wake-up only says "look again", and subscribers also re-read every `poll_s` so nothing published
by another connection is missed. `GET /api/v0/events` streams it through `sse_starlette.EventSourceResponse`
with a comment heartbeat every `heartbeat_s` (15 s) so proxies and browsers keep the connection open;
EventSource cannot set headers, so that route also accepts `?token=`.

A rebuild (`plate reindex`, or the boot rebuild of a lost index) drops the `events` table, so seq starts again
at 1. Every record therefore carries the index's `epoch` (`Bus.epoch(project_id)`, `project_meta.index_epoch`)
and the SSE id is `<epoch>.<seq>`: `routes/events.cursor` sends a cursor from another epoch back to 0, and a
subscription that sees the epoch change under it (a rebuild while the stream is open) starts again from seq 0
of the new epoch, so no event of the new index is skipped.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .db import dumps, loads, tx
from .index import EPOCH_KEY, Index, UnknownProject, index_epoch
from .timeutil import iso, utcnow

__all__ = ["BATCH", "HEARTBEAT_S", "POLL_S", "Bus", "EventFilter", "UnknownProject", "event_id"]

HEARTBEAT_S = 15.0
POLL_S = 1.0
BATCH = 500


@dataclass(frozen=True)
class EventFilter:
    """What a subscription reads: one project, optionally one run, after `after` in index epoch `epoch` (None:
    the cursor was computed against the current epoch, or there is no cursor)."""

    project_id: str
    run_id: str | None = None
    after: int = 0
    epoch: str | None = None


def event_id(epoch: str, seq: int) -> str:
    """The SSE `id:` of an event: `<epoch>.<seq>`."""
    return f"{epoch}.{seq}"


class Bus:
    def __init__(
        self,
        index: Index,
        *,
        heartbeat_s: float = HEARTBEAT_S,
        poll_s: float = POLL_S,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.index = index
        self.heartbeat_s = heartbeat_s
        self.poll_s = poll_s
        self.clock = clock
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
        self._guard = threading.Lock()

    # ------------------------------------------------------------------ write side

    def resolve_project(self, run_id: str | None = None, project_id: str | None = None) -> str:
        """The project an event or a subscription belongs to: explicit id, else the run's project, else
        the only indexed project."""
        if project_id:
            return project_id
        if run_id:
            pid = self.index.locate_run(run_id)
            if pid is None:
                raise UnknownProject(detail=f"run {run_id} is not in any project index")
            return pid
        projects = self.index.project_ids()
        if len(projects) == 1:
            return projects[0]
        raise UnknownProject(detail=f"project_id required: {len(projects)} projects are indexed")

    def epoch(self, project_id: str) -> str:
        """The project index's epoch: the first half of every SSE id; a rebuild changes it."""
        return self.index.epoch(project_id)

    def head(self, project_id: str) -> int:
        """The highest event seq in the project's index (0 when it holds none)."""
        with self.index.project(project_id) as conn:
            return int(conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0])

    def publish(self, run_id: str | None, event: dict[str, Any], *, project_id: str | None = None) -> int:
        """Store and announce one event; returns its seq."""
        pid = self.resolve_project(run_id, project_id or event.get("project_id"))
        kind = str(event.get("kind") or event.get("type") or "event")
        with self.index.project(pid) as conn, tx(conn):
            cur = conn.execute(
                "INSERT INTO events (run_id, at, kind, json) VALUES (?, ?, ?, ?)",
                (run_id, iso(self.clock()), kind, dumps(event)),
            )
            seq = int(cur.lastrowid)
        self._wake_all()
        return seq

    def _wake_all(self) -> None:
        with self._guard:
            waiters = list(self._waiters)
        for loop, ev in waiters:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:  # that subscriber's loop has closed
                with self._guard:
                    self._waiters.discard((loop, ev))

    # ------------------------------------------------------------------ read side

    def read(self, flt: EventFilter, after: int, limit: int = BATCH) -> list[dict[str, Any]]:
        """Stored events after seq `after`, oldest first; each record carries the index `epoch` it was read in."""
        return self.read_batch(flt, after, limit)[1]

    def read_batch(
        self, flt: EventFilter, after: int, limit: int = BATCH
    ) -> tuple[str, list[dict[str, Any]]]:
        """(epoch, events after `after`), both read in one snapshot: a rebuild cannot slip between them."""
        sql = "SELECT seq, run_id, at, kind, json FROM events WHERE seq > ?"
        args: list[Any] = [after]
        if flt.run_id is not None:
            sql += " AND run_id = ?"
            args.append(flt.run_id)
        sql += " ORDER BY seq LIMIT ?"
        args.append(limit)
        with self.index.project(flt.project_id) as conn:
            conn.execute("BEGIN")  # a read transaction: one WAL snapshot for the epoch and the rows
            try:
                row = conn.execute("SELECT value FROM project_meta WHERE key = ?", (EPOCH_KEY,)).fetchone()
                rows = conn.execute(sql, args).fetchall()
            finally:
                conn.execute("COMMIT")
            epoch = str(loads(row["value"])) if row is not None else index_epoch(conn)
        return epoch, [
            {
                "seq": r["seq"],
                "epoch": epoch,
                "project_id": flt.project_id,
                "run_id": r["run_id"],
                "at": r["at"],
                "kind": r["kind"],
                "data": loads(r["json"]),
            }
            for r in rows
        ]

    async def subscribe(self, flt: EventFilter) -> AsyncIterator[dict[str, Any]]:
        """Replay events after `flt.after`, then follow new ones until the consumer stops iterating. When the
        index epoch is not `flt.epoch` (or changes while the stream is open: a rebuild), the cursor goes back
        to 0 and every event of the new epoch is delivered."""
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        waiter = (loop, wake)
        with self._guard:
            self._waiters.add(waiter)
        try:
            cursor = max(0, int(flt.after))
            epoch = flt.epoch
            while True:
                wake.clear()  # before the read: a publish racing the read still wakes the next wait
                now_epoch, batch = await asyncio.to_thread(self.read_batch, flt, cursor)
                if epoch is not None and now_epoch != epoch and cursor > 0:
                    epoch, cursor = now_epoch, 0  # the index was rebuilt: its seqs restarted at 1
                    continue
                epoch = now_epoch
                for rec in batch:
                    cursor = rec["seq"]
                    yield rec
                if batch:
                    continue
                try:
                    await asyncio.wait_for(wake.wait(), timeout=self.poll_s)
                except TimeoutError:
                    pass
        finally:
            with self._guard:
                self._waiters.discard(waiter)
