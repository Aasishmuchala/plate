"""The run executor (router plan T131, decision 75): bounded workers, per-route in-flight caps, the orphan
watchdog, stop with workers mid-run, the inline mode and crash isolation.

`FakeExecute` records every call and sleeps on an Event per run until the test releases it, so nothing here
depends on timing beyond short polls; every executor a test builds is stopped (and its runs released) at
teardown so no worker thread outlives its test.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest

from plate_daemon.executor import DEFAULT_MAX_INFLIGHT, INLINE_MESSAGE, UNRESOLVED_ROUTE, RunExecutor


class Crash(BaseException):
    """Stands in for the fake engine's SimulatedCrash: a BaseException, not an Exception."""


class FakeExecute:
    """`execute(pid, rid)`: records start/end (with the thread name) and waits on that run's Event."""

    def __init__(self, raises: dict[str, BaseException] | None = None, *, open_: bool = False):
        self.raises = raises or {}
        self._lock = threading.Lock()
        self._gates: dict[str, threading.Event] = {}
        self._open = open_
        self.events: list[tuple[str, str, str, str]] = []  # (event, pid, rid, thread name)

    def gate(self, rid: str) -> threading.Event:
        with self._lock:
            ev = self._gates.get(rid)
            if ev is None:
                ev = self._gates[rid] = threading.Event()
                if self._open:
                    ev.set()
            return ev

    def release(self, rid: str) -> None:
        self.gate(rid).set()

    def release_all(self) -> None:
        with self._lock:
            self._open = True
            gates = list(self._gates.values())
        for ev in gates:
            ev.set()

    def _record(self, event: str, pid: str, rid: str) -> None:
        with self._lock:
            self.events.append((event, pid, rid, threading.current_thread().name))

    def __call__(self, pid: str, rid: str) -> str:
        self._record("start", pid, rid)
        try:
            exc = self.raises.get(rid)
            if exc is not None:
                raise exc
            self.gate(rid).wait(10.0)
            return "DONE"
        finally:
            self._record("end", pid, rid)

    def started(self, rid: str | None = None) -> list[str]:
        with self._lock:
            return [e[2] for e in self.events if e[0] == "start" and (rid is None or e[2] == rid)]

    def finished(self, rid: str | None = None) -> list[str]:
        with self._lock:
            return [e[2] for e in self.events if e[0] == "end" and (rid is None or e[2] == rid)]

    def index(self, event: str, rid: str) -> int:
        with self._lock:
            return next(i for i, e in enumerate(self.events) if e[0] == event and e[2] == rid)

    def threads(self) -> set[str]:
        with self._lock:
            return {e[3] for e in self.events if e[0] == "start"}


def until(pred: Callable[[], bool], timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def new_pool_threads(before: set[threading.Thread]) -> list[threading.Thread]:
    """Executor threads started since `before` was taken (other threads in the process are ignored)."""
    return [
        t
        for t in threading.enumerate()
        if t not in before and (t.name.startswith("plate-run-") or t.name == "plate-executor")
    ]


@pytest.fixture
def make() -> Iterator[Callable[..., tuple[RunExecutor, FakeExecute]]]:
    built: list[tuple[RunExecutor, FakeExecute]] = []

    def factory(
        fake: FakeExecute | None = None,
        *,
        workers: int = 4,
        cap: int | Callable[[str, str], int] = 100,
        route: str | Callable[[str, str], str] = "atlas",
        start: bool = True,
        **kw,
    ) -> tuple[RunExecutor, FakeExecute]:
        fake = fake or FakeExecute()
        ex = RunExecutor(
            fake,
            workers=workers,
            max_inflight_for=cap if callable(cap) else (lambda p, r: cap),
            route_key_for=route if callable(route) else (lambda p, r: route),
            **kw,
        )
        built.append((ex, fake))
        if start:
            ex.start()
        return ex, fake

    yield factory
    for ex, fake in built:
        fake.release_all()
        assert ex.stop(timeout_s=5.0), "an executor thread outlived its test"


# ---------------------------------------------------------------------------- the pool


def test_forty_submits_on_eight_workers_fill_the_pool_and_queue_the_rest(make):
    ex, fake = make(workers=8)
    t0 = time.monotonic()
    for i in range(40):
        ex.submit("p1", [f"r{i:02d}"])
    assert time.monotonic() - t0 < 0.5, "submit waited on a run"
    assert until(lambda: ex.stats()["running"] == 8 and len(fake.started()) == 8)
    time.sleep(0.1)  # nothing else starts while the eight sleep
    assert ex.stats() == {"queued": 32, "running": 8, "workers": 8, "inline": False}
    assert sorted(fake.started()) == [f"r{i:02d}" for i in range(8)]  # FIFO
    assert ex.running_for("atlas") == 8
    assert ex.wait_idle(0.05) is False

    fake.release_all()
    assert ex.wait_idle(10.0)
    assert sorted(fake.started()) == [f"r{i:02d}" for i in range(40)]  # each exactly once
    assert ex.stats() == {"queued": 0, "running": 0, "workers": 8, "inline": False}
    assert ex.running_for("atlas") == 0


def test_runs_execute_fifo_on_one_worker(make):
    ex, fake = make(FakeExecute(open_=True), workers=1)
    ex.submit("p1", ["a", "b", "c"])
    ex.submit("p1", ["d", "e"])
    assert ex.wait_idle(5.0)
    assert fake.started() == ["a", "b", "c", "d", "e"]


def test_worker_threads_are_named_daemon_threads(make):
    before = set(threading.enumerate())
    ex, _ = make(workers=3)
    mine = new_pool_threads(before)
    names = sorted(t.name for t in mine)
    assert names == ["plate-executor", "plate-run-1", "plate-run-2", "plate-run-3"]
    assert all(t.daemon for t in mine)
    assert ex.start() is ex  # a second start is a no-op
    assert len(new_pool_threads(before)) == 4


def test_submit_accepts_one_id_as_a_string(make):
    ex, fake = make(FakeExecute(open_=True))
    ex.submit("p1", "run-1")
    assert ex.wait_idle(5.0)
    assert fake.started() == ["run-1"]


# ---------------------------------------------------------------------------- per-route caps


def test_route_cap_one_starts_the_second_run_only_after_the_first_finishes(make):
    routes = {"a": "kling/atlas", "b": "kling/atlas", "c": "flux/bfl"}
    caps = {"kling/atlas": 1, "flux/bfl": 2}
    ex, fake = make(workers=4, route=lambda p, r: routes[r], cap=lambda p, r: caps[routes[r]])
    ex.submit("p1", ["a", "b", "c"])
    # the run behind the capped one starts at once: a capped run goes to the back, it does not block the queue
    assert until(lambda: set(fake.started()) == {"a", "c"})
    time.sleep(0.15)
    assert fake.started("b") == []
    assert ex.running_for("kling/atlas") == 1
    assert ex.stats()["queued"] == 1 and ex.stats()["running"] == 2

    fake.release("a")
    assert until(lambda: fake.started("b") == ["b"])
    assert fake.index("end", "a") < fake.index("start", "b")
    assert ex.running_for("kling/atlas") == 1
    fake.release_all()
    assert ex.wait_idle(5.0)


def test_per_route_caps_are_independent(make):
    ex, fake = make(workers=8, route=lambda p, r: r.split("-")[0], cap=lambda p, r: 2)
    ex.submit("p1", [f"{route}-{i}" for route in ("runway", "atlas", "vertex") for i in range(4)])
    assert until(lambda: len(fake.started()) == 6)
    time.sleep(0.1)
    assert len(fake.started()) == 6
    assert {k: ex.running_for(k) for k in ("runway", "atlas", "vertex")} == {
        "runway": 2,
        "atlas": 2,
        "vertex": 2,
    }
    fake.release_all()
    assert ex.wait_idle(5.0)
    assert len(fake.started()) == 12


def test_resolver_failures_fall_back_and_a_cap_below_one_is_one(make, caplog):
    def route(p: str, r: str) -> str:
        if r == "bad-route":
            raise LookupError("run not in the index")
        return "z"

    def cap(p: str, r: str) -> int:
        if r == "bad-cap":
            raise LookupError("manifest missing")
        return 0

    caplog.set_level(logging.WARNING, logger="plate_daemon.executor")
    ex, fake = make(workers=4, route=route, cap=cap)
    ex.submit("p1", ["z1", "z2", "bad-route", "bad-cap"])
    # cap 0 -> 1, so z2 waits behind z1; bad-route runs on '?'; bad-cap gets the default cap 2, so it joins z1
    assert until(lambda: set(fake.started()) == {"z1", "bad-route", "bad-cap"})
    time.sleep(0.1)
    assert fake.started("z2") == []
    assert ex.running_for("z") == 2 and ex.running_for(UNRESOLVED_ROUTE) == 1
    assert "run not in the index" in caplog.text and "manifest missing" in caplog.text
    fake.release_all()
    assert ex.wait_idle(5.0)
    assert sorted(fake.started()) == ["bad-cap", "bad-route", "z1", "z2"]
    assert DEFAULT_MAX_INFLIGHT == 2


# ---------------------------------------------------------------------------- dedupe and the watchdog


def test_duplicates_of_a_queued_or_running_run_are_ignored(make):
    ex, fake = make(workers=1)
    ex.submit("p1", ["x"])
    assert until(lambda: fake.started() == ["x"])
    ex.submit("p1", ["x", "y"])
    ex.submit("p1", ["y", "y"])
    assert ex.stats() == {"queued": 1, "running": 1, "workers": 1, "inline": False}
    ex.submit("p2", ["y"])  # the pair is the identity: the same run id in another project is another run
    assert ex.stats()["queued"] == 2
    fake.release_all()
    assert ex.wait_idle(5.0)
    assert sorted(fake.started()) == ["x", "y", "y"]


def test_watchdog_runs_an_orphan_once(make):
    calls: list[int] = []
    ex, fake = make(workers=4, watch_interval_s=0.02)

    def scan():
        calls.append(1)
        # what a worker-state query returns: every unfinished run, the one already on a worker included
        return [("p1", rid) for rid in ("live", "orphan") if not fake.finished(rid)]

    ex.submit("p1", ["live"])
    assert until(lambda: fake.started("live") == ["live"])
    ex.watch(scan)
    assert until(lambda: fake.started("orphan") == ["orphan"])
    n = len(calls)
    assert until(lambda: len(calls) >= n + 5)  # several more scans while both runs are mid-run
    assert fake.started("live") == ["live"] and fake.started("orphan") == ["orphan"]

    fake.release_all()
    assert ex.wait_idle(5.0)
    n = len(calls)
    assert until(lambda: len(calls) >= n + 3)
    assert ex.wait_idle(5.0)
    assert fake.started("live") == ["live"]
    assert fake.started("orphan") == ["orphan"]


def test_scan_now_and_a_broken_scan(make, caplog):
    caplog.set_level(logging.WARNING, logger="plate_daemon.executor")
    ex, fake = make(FakeExecute(open_=True), workers=2)

    def broken():
        raise OSError("index unreadable")

    ex.watch(broken)
    ex.watch(lambda: [("p1", "o1"), "junk", ("p1", "o2")])
    assert ex.scan_now() == 2
    assert ex.wait_idle(5.0)
    assert sorted(fake.started()) == ["o1", "o2"]
    assert "index unreadable" in caplog.text and "'junk'" in caplog.text
    ex.submit("p1", ["after"])  # the executor still serves after a scan raised
    assert ex.wait_idle(5.0)
    assert "after" in fake.started()


def test_watch_schedule_follows_the_injected_clock(make):
    now = [datetime(2026, 10, 18, 9, 0, tzinfo=UTC)]
    calls: list[datetime] = []
    ex, _ = make(clock=lambda: now[0], watch_interval_s=0.03)
    ex.watch(lambda: calls.append(now[0]) or [])
    time.sleep(0.2)
    assert calls == []  # the clock has not moved: no scan is due
    now[0] += timedelta(seconds=31)
    assert until(lambda: len(calls) == 1, 2.0)
    time.sleep(0.2)
    assert len(calls) == 1
    now[0] -= timedelta(hours=1)  # a wall clock that jumps backwards scans at once and reschedules
    assert until(lambda: len(calls) == 2, 2.0)
    time.sleep(0.2)
    assert len(calls) == 2


def test_the_dispatcher_survives_its_own_error(make, caplog):
    caplog.set_level(logging.ERROR, logger="plate_daemon.executor")
    state = {"calls": 0}

    def clock() -> datetime:
        state["calls"] += 1
        if state["calls"] == 3:  # start() reads it once; the dispatcher's first checks hit this
            raise OSError("clock source gone")
        return datetime(2026, 10, 18, 9, 0, tzinfo=UTC)

    ex, fake = make(FakeExecute(open_=True), workers=2, clock=clock, watch_interval_s=0.05)
    ex.watch(lambda: [])
    assert until(lambda: "clock source gone" in caplog.text, 2.0)
    ex.submit("p1", ["a", "b"])
    assert ex.wait_idle(5.0)
    assert sorted(fake.started()) == ["a", "b"]


# ---------------------------------------------------------------------------- stop, inline, crashes


def test_stop_returns_within_the_timeout_with_workers_mid_sleep(make):
    ex, fake = make(workers=3)
    ex.submit("p1", ["a", "b", "c", "d", "e"])
    assert until(lambda: ex.stats()["running"] == 3 and len(fake.started()) == 3)
    t0 = time.monotonic()
    ok = ex.stop(timeout_s=0.3)
    elapsed = time.monotonic() - t0
    assert ok is False
    assert elapsed < 1.0
    assert ex.stop_event.is_set()
    assert ex.stats()["queued"] == 0  # the queue is dropped: those runs stay QUEUED for the next resume
    ex.submit("p1", ["late"])  # logged and dropped, never raised at a request handler
    assert ex.stats()["queued"] == 0
    with pytest.raises(RuntimeError, match="stopped"):
        ex.start()

    fake.release_all()
    assert ex.stop(timeout_s=5.0) is True  # the workers finish their runs and exit
    assert sorted(fake.started()) == ["a", "b", "c"]
    assert ex.stats()["running"] == 0


def test_stop_before_start_and_twice(make):
    ex, _ = make(start=False)
    ex.submit("p1", ["a"])  # queued before start: dispatched once started, dropped by stop
    assert ex.stats()["queued"] == 1
    assert ex.stop(timeout_s=1.0) is True
    assert ex.stop(timeout_s=1.0) is True
    assert ex.stats()["queued"] == 0


def test_workers_zero_is_inline_and_submit_raises(make):
    before = set(threading.enumerate())
    scanned: list[int] = []
    ex, fake = make(workers=0)
    ex.watch(lambda: scanned.append(1) or [("p1", "x")])
    assert ex.inline is True
    assert ex.start() is ex
    assert new_pool_threads(before) == []
    with pytest.raises(RuntimeError) as err:
        ex.submit("p1", ["a"])
    assert str(err.value) == INLINE_MESSAGE == "inline executor: run the BackgroundTask path"
    assert ex.stats() == {"queued": 0, "running": 0, "workers": 0, "inline": True}
    assert ex.scan_now() == 0 and scanned == []
    assert ex.wait_idle(0.0) is True
    assert ex.stop() is True
    assert fake.started() == []


def test_bad_arguments():
    with pytest.raises(ValueError, match="workers"):
        RunExecutor(lambda p, r: None, workers=-1, max_inflight_for=lambda p, r: 1)
    with pytest.raises(ValueError, match="watch_interval_s"):
        RunExecutor(lambda p, r: None, workers=1, max_inflight_for=lambda p, r: 1, watch_interval_s=0)


def test_a_crashing_execute_does_not_kill_the_worker(make, caplog):
    caplog.set_level(logging.ERROR, logger="plate_daemon.executor")
    fake = FakeExecute(
        {"boom": ValueError("adapter exploded"), "crash": Crash("process died mid-step")}, open_=True
    )
    ex, _ = make(fake, workers=1)
    ex.submit("p1", ["boom", "crash", "ok"])
    assert ex.wait_idle(5.0)
    assert fake.started() == ["boom", "crash", "ok"]
    assert fake.threads() == {"plate-run-1"}  # one worker ran all three: neither raise ended it
    assert "adapter exploded" in caplog.text and "process died mid-step" in caplog.text
    assert ex.stats() == {"queued": 0, "running": 0, "workers": 1, "inline": False}

    ex.submit("p1", ["next"])
    assert ex.wait_idle(5.0)
    assert fake.started()[-1] == "next"
    worker = next(t for t in threading.enumerate() if t.name == "plate-run-1")
    assert worker.is_alive()


def test_wait_idle_times_out_while_a_run_is_mid_run(make):
    ex, fake = make(workers=2)
    ex.submit("p1", ["slow"])
    assert until(lambda: fake.started() == ["slow"])
    t0 = time.monotonic()
    assert ex.wait_idle(0.1) is False
    assert 0.05 < time.monotonic() - t0 < 1.0
    fake.release("slow")
    assert ex.wait_idle(5.0) is True
