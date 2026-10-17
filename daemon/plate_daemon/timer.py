"""The human-minute timer (BUILD_PLAN decision 28): explicit start and stop per deliverable.

Ledger timestamps cannot see the time an artist spends in Max, Photoshop and Resolve, so the measurement of
"minutes per accepted deliverable" is a one-key timer: `POST /timer {deliverable_id, action: start|stop}`
(routes/misc.py) and `GET /timer/today`. One timer runs at a time. `start` writes `<home>/timer/running.json`
(so a daemon restart does not lose a running timer); `stop` removes it and appends a `human_minutes` record
with the elapsed minutes to a ledger:

- the project's `runs.jsonl` when the timer was started with a `project_id`;
- else the workspace ledger `<home>/timer/runs.jsonl` (the same hash-chained `Ledger`, in a folder of its
  own), because the panel's timer posts a deliverable and no project.

A record is `{kind: human_minutes, data: {deliverable_id, minutes, started_at, stopped_at, project_id}}`.
`today()` adds the records of the artist's local calendar day over the workspace ledger and every project
ledger it is given, per deliverable. The clock is injected, so tests never sleep.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .fsutil import read_json, write_json_atomic
from .ledger import Ledger, LedgerError
from .lock import ProjectLocked
from .timeutil import iso, parse_iso

__all__ = ["HumanTimer", "TimerError", "WORKSPACE_DIR", "check_deliverable", "day_bounds"]

WORKSPACE_DIR = "timer"
RUNNING_JSON = "running.json"
MAX_DELIVERABLE_LEN = 120
KIND = "human_minutes"


class TimerError(Exception):
    """A timer request the daemon refuses. `status` and `code` become the HTTP answer."""

    def __init__(self, status: int, code: str, detail: str, **extra: Any):
        super().__init__(detail)
        self.status, self.code, self.detail, self.extra = status, code, detail, extra


def check_deliverable(raw: Any) -> str:
    """The deliverable id, stripped: 1 to 120 characters with no control characters."""
    if not isinstance(raw, str) or not raw.strip():
        raise TimerError(422, "bad_deliverable", "name the deliverable first")
    name = raw.strip()
    if len(name) > MAX_DELIVERABLE_LEN or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise TimerError(
            422,
            "bad_deliverable",
            f"deliverable_id is 1-{MAX_DELIVERABLE_LEN} characters, no control characters",
        )
    return name


def day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """[start, end) of the local calendar day containing `now`, as aware datetimes."""
    local = now.astimezone()
    start_naive = local.replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    return start_naive.astimezone(), (start_naive + timedelta(days=1)).astimezone()


class HumanTimer:
    """`HumanTimer(home, ledger_dir_for, ledger_dirs)`.

    `ledger_dir_for(project_id)` is the folder of that project's ledger; `ledger_dirs()` lists the project
    ledger folders `today()` should read besides the workspace one."""

    def __init__(
        self,
        home: Path | str,
        ledger_dir_for: Callable[[str], Path],
        ledger_dirs: Callable[[], Iterable[Path]],
    ):
        self.workspace = Path(home) / WORKSPACE_DIR
        self._ledger_dir_for = ledger_dir_for
        self._ledger_dirs = ledger_dirs
        self._lock = threading.RLock()

    @property
    def state_path(self) -> Path:
        return self.workspace / RUNNING_JSON

    # ------------------------------------------------------------------ state

    def running(self) -> dict[str, Any] | None:
        with self._lock:
            try:
                doc = read_json(self.state_path)
            except FileNotFoundError:
                return None
            except ValueError:  # a torn or hand-edited file is no timer
                return None
        ok = isinstance(doc, dict) and all(
            isinstance(doc.get(k), str) for k in ("deliverable_id", "started_at")
        )
        return doc if ok else None

    # ------------------------------------------------------------------ start and stop

    def start(self, deliverable_id: str, now: datetime, project_id: str | None = None) -> dict[str, Any]:
        name = check_deliverable(deliverable_id)
        with self._lock:
            current = self.running()
            if current is not None:
                if current["deliverable_id"] == name:  # a second Space on the same timer changes nothing
                    return {**current, "already_running": True}
                raise TimerError(
                    409,
                    "timer_running",
                    f"another deliverable is running: {current['deliverable_id']}; stop it first",
                    running=current,
                )
            doc = {"deliverable_id": name, "started_at": iso(now), "project_id": project_id}
            write_json_atomic(self.state_path, doc)
            return {**doc, "already_running": False}

    def stop(self, deliverable_id: str, now: datetime) -> dict[str, Any]:
        name = check_deliverable(deliverable_id)
        with self._lock:
            current = self.running()
            if current is None:
                raise TimerError(409, "timer_not_running", "no timer is running")
            if current["deliverable_id"] != name:
                raise TimerError(
                    409,
                    "timer_running",
                    f"the running timer is {current['deliverable_id']}, not {name}",
                    running=current,
                )
            started = parse_iso(current["started_at"])
            assert started is not None
            minutes = round(max((now - started).total_seconds(), 0.0) / 60.0, 4)
            project_id = current.get("project_id") if isinstance(current.get("project_id"), str) else None
            record = {
                "kind": KIND,
                "at": now,
                "data": {
                    "deliverable_id": name,
                    "minutes": minutes,
                    "started_at": current["started_at"],
                    "stopped_at": iso(now),
                    "project_id": project_id,
                },
            }
            folder = self._ledger_dir_for(project_id) if project_id else self.workspace
            try:
                Ledger(folder).append(record)
            except (
                LedgerError,
                ProjectLocked,
                OSError,
            ) as e:  # the timer stays running: nothing was recorded
                raise TimerError(500, "ledger_unwritable", f"the minutes could not be ledgered: {e}") from e
            self.state_path.unlink(missing_ok=True)
            return {
                "deliverable_id": name,
                "minutes": minutes,
                "started_at": current["started_at"],
                "stopped_at": iso(now),
                "project_id": project_id,
            }

    # ------------------------------------------------------------------ the day's minutes

    def today(self, now: datetime, project_id: str | None = None) -> dict[str, Any]:
        """`{day, rows: [{deliverable_id, minutes}], totals: {minutes}, running}` for the local day of `now`.
        With `project_id` only that project's ledger is read."""
        start, end = day_bounds(now)
        if project_id:
            folders = [self._ledger_dir_for(project_id)]
        else:
            folders = [self.workspace, *self._ledger_dirs()]
        minutes: dict[str, float] = {}
        seen: set[Path] = set()
        for folder in folders:
            key = Path(folder).resolve()
            if key in seen:
                continue
            seen.add(key)
            for rec in Ledger(folder).records():
                if rec.get("kind") != KIND:
                    continue
                at = parse_iso(rec["at"]) if isinstance(rec.get("at"), str) else None
                data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
                name, mins = data.get("deliverable_id"), data.get("minutes")
                if at is None or not (start <= at < end) or not isinstance(name, str):
                    continue
                if isinstance(mins, bool) or not isinstance(mins, int | float):
                    continue
                minutes[name] = minutes.get(name, 0.0) + float(mins)
        rows = [{"deliverable_id": k, "minutes": round(v, 2)} for k, v in minutes.items()]
        current = self.running()
        return {
            "day": start.strftime("%Y-%m-%d"),
            "rows": rows,
            "totals": {"minutes": round(sum(minutes.values()), 2)},
            "running": current,
        }
