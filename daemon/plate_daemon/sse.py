"""The daemon's event bus: durable events in the project index, live fan-out to SSE subscribers.

`Bus.publish(run_id, event)` appends `{seq, run_id, at, kind, json}` to the project's `events` table (seq
is the SQLite AUTOINCREMENT, so it never repeats within a project) and wakes every subscriber; it may be
called from any thread (thread-pool routes, the worker). `Bus.subscribe(filter)` is an async iterator
that first replays the stored events after `filter.after` (the browser's `Last-Event-ID`), then follows
new ones. The table is the source: a wake-up only says "look again", and subscribers also re-read every
`poll_s` so nothing published by another connection is missed. `GET /api/v0/events` streams it through
`sse_starlette.EventSourceResponse` with a comment heartbeat every `heartbeat_s` (15 s) so proxies and
browsers keep the connection open; EventSource cannot set headers, so that route also accepts `?token=`.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .db import dumps, loads, tx
from .index import Index
from .timeutil import iso, utcnow

HEARTBEAT_S = 15.0
POLL_S = 1.0
BATCH = 500


class UnknownProject(LookupError):
    pass


@dataclass(frozen=True)
class EventFilter:
    project_id: str
    run_id: str | None = None
    after: int = 0


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
                raise UnknownProject(f"run {run_id} is not in any project index")
            return pid
        projects = self.index.project_ids()
        if len(projects) == 1:
            return projects[0]
        raise UnknownProject(f"project_id required: {len(projects)} projects are indexed")

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
        sql = "SELECT seq, run_id, at, kind, json FROM events WHERE seq > ?"
        args: list[Any] = [after]
        if flt.run_id is not None:
            sql += " AND run_id = ?"
            args.append(flt.run_id)
        sql += " ORDER BY seq LIMIT ?"
        args.append(limit)
        with self.index.project(flt.project_id) as conn:
            rows = conn.execute(sql, args).fetchall()
        return [
            {
                "seq": r["seq"],
                "project_id": flt.project_id,
                "run_id": r["run_id"],
                "at": r["at"],
                "kind": r["kind"],
                "data": loads(r["json"]),
            }
            for r in rows
        ]

    async def subscribe(self, flt: EventFilter) -> AsyncIterator[dict[str, Any]]:
        """Replay events after `flt.after`, then follow new ones until the consumer stops iterating."""
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        waiter = (loop, wake)
        with self._guard:
            self._waiters.add(waiter)
        try:
            cursor = max(0, int(flt.after))
            while True:
                wake.clear()  # before the read: a publish racing the read still wakes the next wait
                batch = await asyncio.to_thread(self.read, flt, cursor)
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
