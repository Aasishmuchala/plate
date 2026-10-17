"""The engine adapter protocol the worker drives, and the errors adapters raise.

An adapter submits a payload under an idempotency key and returns the provider's job id; `poll` reports
`queued | running | succeeded | refused | failed` plus a result URL; `fetch` downloads the result bytes
(raising `ResultExpired` once the provider's URL has lapsed); `cancel` stops spend. `lookup` is
optional: an adapter that can find a job by idempotency key lets recovery adopt a job whose id was lost
in a crash instead of failing it. Real adapters (Atlas and friends) live in `plate_engines` and arrive
behind this protocol later; today only `engine_fake.FakeEngine` implements it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

PollStatus = Literal["queued", "running", "succeeded", "refused", "failed"]


@dataclass(frozen=True)
class PollResult:
    status: PollStatus
    result_url: str | None = None
    detail: str | None = None


class ResultExpired(Exception):
    """The provider's result URL no longer serves the bytes."""


class SimulatedCrash(BaseException):
    """Raised by the fake engine to model the daemon process dying mid-step.

    A BaseException so no `except Exception` in the worker can swallow it: like a real crash it unwinds
    the step, rolls back the open SQLite transaction and leaves only what was already committed."""


@runtime_checkable
class EngineAdapter(Protocol):
    def submit(self, payload: dict[str, Any], idempotency_key: str) -> str: ...

    def poll(self, provider_job_id: str) -> PollResult: ...

    def fetch(self, result_url: str) -> bytes: ...

    def cancel(self, provider_job_id: str) -> None: ...


def lookup_fn(engine: Any):
    """The adapter's `lookup(idempotency_key) -> str | None`, or None when it has none."""
    fn = getattr(engine, "lookup", None)
    if fn is None or getattr(engine, "supports_lookup", True) is False:
        return None
    return fn
