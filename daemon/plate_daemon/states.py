"""The run state machine as data (BUILD_PLAN section 4 "Job state machine").

`TRANSITIONS` is the only source of legal moves; `machine.transition` enforces it in one SQLite
transaction and the hypothesis suite proves the table and the enforcement agree. Terminal states have no
exits. `WORKER_STATES` are the states the worker advances on its own; DRAFT, QUOTED and APPROVED move
only on a human, auto or agent action (quote before spend, doctrine 8).
"""

from __future__ import annotations

from enum import StrEnum


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
