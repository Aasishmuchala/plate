"""Route health: a persisted run of consecutive provider failures per `<engine>/<route>` (router plan T85).

`plate_engines.health` knows what the canaries and the alert log say about an engine. It cannot know that a
provider has started to forget job ids, time out or refuse submits right now. The worker can: every poll that
raises and every submit that fails is a data point. `Tracker` keeps those points so the Make bar can show a
route as degraded (decision 51: after 3 consecutive provider failures within an hour) without waiting for a
canary.

    tracker = Tracker(home)
    tracker.note_failure("kling-o3-4k", "atlas", "provider_poll_error")   # worker, on a failed poll or submit
    tracker.note_success("kling-o3-4k", "atlas")                          # worker, on a result that was fetched
    tracker.degraded()  -> {"kling-o3-4k/atlas": {"failures": 3, "since": "...Z", "reason": "..."}}
    tracker.reset("kling-o3-4k/atlas")                                    # a person says the route is fine

A key is `<engine_id>/<route_id>`, the key the health board uses (`Pipeline.health_board`); a failure that names
no route lands on the bare engine id. A route is degraded when at least `DEGRADE_AFTER` failures with no success
between them fall inside the last hour, so a route nobody calls any more heals by itself and gets retried, while
one that keeps failing stays degraded. `since` is the first of those failures, `reason` the latest one's code.

State is a small JSON file at `<home>/cache/route_health.json` (UTF-8, LF, written atomically) so a restart keeps
the count. Every call reads the file, changes it and writes it back under one lock per file, so two Tracker
objects on one home (the pipeline's and a test's) agree. The worker never lets a tracker fault touch a run.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .fsutil import read_json, write_json_atomic
from .timeutil import iso, parse_iso, utcnow

log = logging.getLogger(__name__)

FILE_NAME = "route_health.json"
VERSION = 1
DEGRADE_AFTER = 3
WINDOW_S = 3600.0
MAX_POINTS = (
    50  # failure times kept per key; far more than DEGRADE_AFTER, bounded for a route that never heals
)
MAX_REASON_CHARS = 200

_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def route_key(engine_id: str, route_id: str | None) -> str:
    """`<engine>/<route>`, or the bare engine id when the run names no route."""
    return f"{engine_id}/{route_id}" if route_id else str(engine_id)


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.absolute())
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.RLock()
        return lk


class Tracker:
    """Consecutive provider failures per route, persisted. See the module docstring."""

    def __init__(
        self,
        home: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
        degrade_after: int = DEGRADE_AFTER,
        window_s: float = WINDOW_S,
    ) -> None:
        if degrade_after < 1:
            raise ValueError("degrade_after must be at least 1")
        self.path = Path(home) / "cache" / FILE_NAME
        self.degrade_after = degrade_after
        self.window = timedelta(seconds=window_s)
        self._clock = clock or utcnow
        self._lock = _lock_for(self.path)

    # ------------------------------------------------------------------ file

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.is_file():
            return {}
        try:
            doc = read_json(self.path)
        except (OSError, ValueError) as e:  # a torn or hand-edited file starts the count again
            log.warning("route health %s unreadable, starting empty: %s", self.path, e)
            return {}
        routes = doc.get("routes") if isinstance(doc, dict) else None
        if not isinstance(routes, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for key, entry in routes.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("failures"), list):
                continue
            times = [t for t in entry["failures"] if isinstance(t, str) and self._parse(t) is not None]
            if times:
                out[str(key)] = {"failures": times, "reason": str(entry.get("reason", ""))}
        return out

    def _write(self, state: dict[str, dict[str, Any]]) -> None:
        write_json_atomic(self.path, {"v": VERSION, "routes": state}, indent=1)

    @staticmethod
    def _parse(text: str) -> datetime | None:
        try:
            dt = parse_iso(text)
        except ValueError:
            return None
        return dt.astimezone(UTC) if dt is not None else None

    # ------------------------------------------------------------------ the worker's calls

    def note_failure(
        self, engine_id: str, route_id: str | None, reason: str, *, now: datetime | None = None
    ) -> None:
        """One more provider failure on this route (a poll that raised, a submit that failed)."""
        key = route_key(engine_id, route_id)
        at = (now or self._clock()).astimezone(UTC)
        with self._lock:
            state = self._read()
            entry = state.setdefault(key, {"failures": [], "reason": ""})
            cutoff = at - self.window
            kept = [t for t in entry["failures"] if (p := self._parse(t)) is not None and p >= cutoff]
            kept.append(iso(at))
            entry["failures"] = kept[-MAX_POINTS:]
            entry["reason"] = str(reason)[:MAX_REASON_CHARS]
            self._write(state)

    def note_success(self, engine_id: str, route_id: str | None) -> None:
        """A result came back from this route: the run of failures is over."""
        self.reset(route_key(engine_id, route_id))

    def reset(self, key: str) -> bool:
        """Forget the failures of `<engine>/<route>`. True when there was something to forget."""
        with self._lock:
            state = self._read()
            if state.pop(key, None) is None:
                return False
            self._write(state)
            return True

    # ------------------------------------------------------------------ the readers

    def degraded(self, now: datetime | None = None) -> dict[str, dict[str, Any]]:
        """`{"<engine>/<route>": {failures, since, reason}}` for every route with at least `degrade_after`
        consecutive failures inside the last hour. Reads only; a healed route simply drops out."""
        at = (now or self._clock()).astimezone(UTC)
        cutoff = at - self.window
        with self._lock:
            state = self._read()
        out: dict[str, dict[str, Any]] = {}
        for key, entry in sorted(state.items()):
            recent = sorted(
                t for t in entry["failures"] if (p := self._parse(t)) is not None and cutoff <= p <= at
            )
            if len(recent) >= self.degrade_after:
                out[key] = {"failures": len(recent), "since": recent[0], "reason": entry["reason"]}
        return out

    def is_degraded(self, engine_id: str, route_id: str | None, now: datetime | None = None) -> bool:
        return route_key(engine_id, route_id) in self.degraded(now)
