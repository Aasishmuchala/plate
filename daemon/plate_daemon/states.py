"""The run state machine as data (BUILD_PLAN section 4 "Job state machine"), and run priorities.

`TRANSITIONS` is the only source of legal moves; `machine.transition` enforces it in one SQLite
transaction and `tests/test_machine_properties.py` proves the table and the enforcement agree. Terminal
states have no exits. `WORKER_STATES` are the states the worker advances on its own; DRAFT, QUOTED and
APPROVED move only on a human, auto or agent action (quote before spend, doctrine 8).

`Priority` orders the worker's queue (lower first, then older first). The index stores and the API speaks
the lower-case label (`"interactive"`); `Priority.parse` accepts a member, its number, name or label.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any


class RunState(StrEnum):
    DRAFT = "DRAFT"
    QUOTED = "QUOTED"
    APPROVED = "APPROVED"
    QUEUED = "QUEUED"
    STAGING = "STAGING"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    FETCHING = "FETCHING"
    NORMALIZING = "NORMALIZING"
    GATING = "GATING"
    DONE = "DONE"
    GATE_FAIL = "GATE_FAIL"
    FAILED = "FAILED"
    REFUSED = "REFUSED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"

    def __str__(self) -> str:
        return self.value


class Priority(IntEnum):
    CLIENT_DEADLINE = 0
    INTERACTIVE = 1
    AGENT_BATCH = 2
    CALIBRATION = 3

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def parse(cls, value: Any) -> Priority:
        if isinstance(value, cls):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return cls(value)
        if isinstance(value, str):
            try:
                return cls[value.strip().upper()]
            except KeyError:
                pass
        raise ValueError(f"priority must be one of {[p.label for p in cls]} (or 0-3), got {value!r}")


S = RunState

TERMINAL: frozenset[RunState] = frozenset({S.DONE, S.GATE_FAIL, S.FAILED, S.REFUSED, S.CANCELLED, S.EXPIRED})

TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    S.DRAFT: frozenset({S.QUOTED, S.CANCELLED}),
    S.QUOTED: frozenset({S.APPROVED, S.EXPIRED, S.CANCELLED}),
    S.APPROVED: frozenset({S.QUEUED, S.CANCELLED}),
    S.QUEUED: frozenset({S.STAGING, S.CANCELLED}),
    S.STAGING: frozenset({S.SUBMITTED, S.FAILED, S.CANCELLED}),
    S.SUBMITTED: frozenset({S.RUNNING, S.REFUSED, S.FAILED, S.CANCELLED}),
    S.RUNNING: frozenset({S.FETCHING, S.REFUSED, S.FAILED, S.CANCELLED}),
    S.FETCHING: frozenset({S.NORMALIZING, S.FAILED}),
    S.NORMALIZING: frozenset({S.GATING, S.FAILED}),
    S.GATING: frozenset({S.DONE, S.GATE_FAIL, S.FAILED}),
    **{t: frozenset() for t in TERMINAL},
}

# States the worker advances by itself, in pipeline order.
WORKER_STATES: tuple[RunState, ...] = (
    S.QUEUED,
    S.STAGING,
    S.SUBMITTED,
    S.RUNNING,
    S.FETCHING,
    S.NORMALIZING,
    S.GATING,
)

# States that only exist after a provider accepted the job: the row must carry provider_job_id.
PROVIDER_STATES: frozenset[RunState] = frozenset(
    {S.SUBMITTED, S.RUNNING, S.FETCHING, S.NORMALIZING, S.GATING, S.DONE, S.GATE_FAIL}
)

# States that hold one of a route's provider slots (`max_inflight`).
INFLIGHT_STATES: frozenset[RunState] = frozenset({S.STAGING, S.SUBMITTED, S.RUNNING})

# Local states the worker runs straight through: a result URL may expire, so fetching never waits a turn.
IMMEDIATE_STATES: frozenset[RunState] = frozenset({S.FETCHING, S.NORMALIZING, S.GATING})

# Terminal states that count as a failure for `Run.error`.
ERROR_STATES: frozenset[RunState] = frozenset({S.GATE_FAIL, S.FAILED, S.REFUSED, S.EXPIRED})


def is_terminal(state: RunState | str) -> bool:
    return RunState(state) in TERMINAL
