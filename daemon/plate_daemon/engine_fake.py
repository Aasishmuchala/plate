"""FakeEngine: a scripted, deterministic provider for the state machine, crash and API tests.

It implements the `adapter.EngineAdapter` protocol with no network and no sleeping. Constructor flags
script the side exits: `refuse` (poll -> refused), `timeout` (poll never succeeds), `expire_result_url`
(fetch raises ResultExpired), `polls_until_done` (polls before a job succeeds; read at poll time, so a
restarted daemon can finish a job the previous one left running), `supports_lookup` (adopt a lost job
id by idempotency key), `crash_after_submit_before_persist` (the provider accepted the job, then the
daemon died before writing the id), and `crash_at` + `crash_phase` (die inside the worker step that
would leave `crash_at`: "before" it does anything, "after_effect" once its provider call or CAS write is
done but before the commit, "after_commit"). Jobs and the call counters (`submit_count`, `poll_count`,
`fetch_count`, `cancel_count`) persist to a JSON `journal` with atomic replaces, so a daemon killed with
`Popen.kill()` restarts against the same provider state: that is what makes `submit_count == 1` across a
real process restart an honest assertion. `PLATE_FAKE_ENGINE` (JSON of these flags) configures the
daemon's in-process instance.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

from .adapter import PollResult, ResultExpired, SimulatedCrash
from .fsutil import read_json, write_json_atomic
from .idempotency import canonical_json
from .states import RunState

CRASH_PHASES = ("before", "after_effect", "after_commit")
RESULT_SCHEME = "fake://results/"
FLAG_NAMES = (
    "crash_at",
    "crash_phase",
    "crash_after_submit_before_persist",
    "refuse",
    "timeout",
    "expire_result_url",
    "polls_until_done",
    "supports_lookup",
)


def _empty_state() -> dict[str, Any]:
    return {
        "submit_count": 0,
        "poll_count": 0,
        "fetch_count": 0,
        "cancel_count": 0,
        "lookup_count": 0,
        "next_job": 0,
        "jobs": {},
        "by_key": {},
    }


class FakeEngine:
    stub = True
    engine_id = "fake"

    def __init__(
        self,
        *,
        crash_at: RunState | str | None = None,
        crash_phase: str = "before",
        crash_after_submit_before_persist: bool = False,
        refuse: bool = False,
        timeout: bool = False,
        expire_result_url: bool = False,
        polls_until_done: int = 2,
        supports_lookup: bool = False,
        journal: Path | None = None,
    ):
        if crash_phase not in CRASH_PHASES:
            raise ValueError(f"crash_phase must be one of {CRASH_PHASES}")
        if polls_until_done < 1:
            raise ValueError("polls_until_done must be >= 1")
        self.crash_at = RunState(crash_at) if crash_at is not None else None
        self.crash_phase = crash_phase
        self.crash_after_submit_before_persist = crash_after_submit_before_persist
        self.refuse = refuse
        self.timeout = timeout
        self.expire_result_url = expire_result_url
        self.polls_until_done = polls_until_done
        self.supports_lookup = supports_lookup
        self.journal = Path(journal) if journal is not None else None
        self._lock = threading.RLock()
        self._crashed = False
        self._state = _empty_state()
        if self.journal is not None and self.journal.exists():
            loaded = read_json(self.journal)
            self._state.update(loaded)

    # ------------------------------------------------------------------ construction helpers

    @classmethod
    def from_env(cls, journal: Path | None, env_var: str = "PLATE_FAKE_ENGINE") -> FakeEngine:
        raw = os.environ.get(env_var, "").strip()
        flags = json.loads(raw) if raw else {}
        if not isinstance(flags, dict):
            raise ValueError(f"{env_var} must be a JSON object of FakeEngine flags")
        unknown = set(flags) - set(FLAG_NAMES)
        if unknown:
            raise ValueError(f"{env_var}: unknown FakeEngine flags {sorted(unknown)}")
        return cls(journal=journal, **flags)

    def restarted(self, **flags: Any) -> FakeEngine:
        """A fresh instance over the same provider state, as a restarted daemon would see it."""
        new = FakeEngine(journal=self.journal, **flags)
        if self.journal is None:
            with self._lock:
                new._state = copy.deepcopy(self._state)
        return new

    # ------------------------------------------------------------------ persistence

    def _save(self) -> None:
        if self.journal is not None:
            write_json_atomic(self.journal, self._state, indent=1)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    @property
    def submit_count(self) -> int:
        return self._state["submit_count"]

    @property
    def poll_count(self) -> int:
        return self._state["poll_count"]

    @property
    def fetch_count(self) -> int:
        return self._state["fetch_count"]

    @property
    def cancel_count(self) -> int:
        return self._state["cancel_count"]

    # ------------------------------------------------------------------ fault injection

    def fault_point(self, state: RunState, phase: str) -> None:
        """Called by the worker at each phase of a step; raises once when scripted to."""
        if (
            self.crash_at is not None
            and not self._crashed
            and state == self.crash_at
            and phase == self.crash_phase
        ):
            self._crashed = True
            raise SimulatedCrash(f"simulated crash leaving {state.value} ({phase})")

    # ------------------------------------------------------------------ EngineAdapter

    def submit(self, payload: dict[str, Any], idempotency_key: str) -> str:
        with self._lock:
            st = self._state
            st["submit_count"] += 1
            job_id = st["by_key"].get(idempotency_key)
            if job_id is None:
                st["next_job"] += 1
                job_id = f"fake-{st['next_job']:06d}"
                st["jobs"][job_id] = {
                    "idempotency_key": idempotency_key,
                    "payload_bytes": len(canonical_json(payload)),
                    "polls": 0,
                    "cancelled": False,
                }
                st["by_key"][idempotency_key] = job_id
            self._save()
        if self.crash_after_submit_before_persist and not self._crashed:
            self._crashed = True
            raise SimulatedCrash(
                "simulated crash after the provider accepted the job, before the id was persisted"
            )
        return job_id

    def poll(self, provider_job_id: str) -> PollResult:
        with self._lock:
            st = self._state
            st["poll_count"] += 1
            job = st["jobs"].get(provider_job_id)
            if job is None:
                self._save()
                return PollResult("failed", detail=f"unknown job {provider_job_id}")
            job["polls"] += 1
            self._save()
            if job["cancelled"]:
                return PollResult("failed", detail="cancelled")
            if self.refuse:
                return PollResult("refused", detail="fake moderation refusal")
            if self.timeout or job["polls"] < self.polls_until_done:
                return PollResult("running")
            return PollResult("succeeded", result_url=f"{RESULT_SCHEME}{provider_job_id}")

    def fetch(self, result_url: str) -> bytes:
        with self._lock:
            self._state["fetch_count"] += 1
            self._save()
            if self.expire_result_url:
                raise ResultExpired(f"{result_url} expired")
            if not result_url.startswith(RESULT_SCHEME):
                raise ResultExpired(f"{result_url} is not a fake result URL")
            job_id = result_url[len(RESULT_SCHEME) :]
            job = self._state["jobs"].get(job_id)
            if job is None:
                raise ResultExpired(f"{result_url}: no such job")
            return f"PLATE-FAKE-RESULT {job_id} {job['idempotency_key']}\n".encode()

    def cancel(self, provider_job_id: str) -> None:
        with self._lock:
            self._state["cancel_count"] += 1
            job = self._state["jobs"].get(provider_job_id)
            if job is not None:
                job["cancelled"] = True
            self._save()

    def lookup(self, idempotency_key: str) -> str | None:
        if not self.supports_lookup:
            raise NotImplementedError("this FakeEngine was built with supports_lookup=False")
        with self._lock:
            self._state["lookup_count"] += 1
            self._save()
            return self._state["by_key"].get(idempotency_key)
