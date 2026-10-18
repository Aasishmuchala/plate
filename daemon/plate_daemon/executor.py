"""The run executor: a bounded thread pool that drives approved runs off the request pool (router plan T131,
decision 75).

`RunExecutor(execute, workers=N, max_inflight_for=..., route_key_for=...)` owns one dispatcher thread
(`plate-executor`) and `N` worker threads (`plate-run-1` .. `plate-run-N`), all daemon threads. It knows
nothing about runs beyond a `(project_id, run_id)` pair: the caller supplies `execute(pid, rid)` (drive the run
to its end), `route_key_for(pid, rid) -> str` (which provider route the run occupies) and
`max_inflight_for(pid, rid) -> int` (that route's in-flight cap, the manifest's `max_inflight`, default 2).
Without `route_key_for` every run shares one route, `*`. Nothing here imports the pipeline or the routes, so
the pool is testable with a fake `execute`.

    submit(pid, run_ids)   enqueue the pairs FIFO; a run already queued or running is ignored. Never blocks on
                           a run: the lock it takes only guards the queue's bookkeeping.
    dispatcher             hands the queue's head to a free worker while the run's route has fewer runs
                           running than its cap; a run at its route cap goes to the back of the queue, so one
                           saturated route never holds up the others. The route key and the cap are resolved
                           once per submission, outside the lock (the callables may read an index); a resolver
                           that raises is logged and the run falls back to route `?` and the default cap.
    worker                 runs `execute(pid, rid)` to completion. Any exception, a BaseException such as the
                           fake engine's SimulatedCrash included, is logged and never propagated: the worker
                           takes the next run. A caller that wants a crash to end the process does that inside
                           `execute`.
    watch(scan)            registers a scan the dispatcher runs every `watch_interval_s` (first one interval
                           after `start()`); every `(pid, rid)` it yields that is neither queued nor running is
                           submitted. This re-drives runs left in a worker state with no thread on them. The
                           schedule follows the injected `clock` (a wall clock that jumps backwards scans at
                           once and reschedules); the real wait between checks is never longer than the
                           interval. `scan_now()` runs the scans in the caller's thread.

`workers == 0` is the inline mode the test suite uses: `inline` is True, nothing starts, `submit` raises
`RuntimeError("inline executor: run the BackgroundTask path")` and the caller keeps its old BackgroundTask path.

`wait_idle(timeout_s)` is True once nothing is queued or running. `stop(timeout_s)` drops the queue (the runs
stay QUEUED in the index for the next boot's resume or the watchdog), lets every worker finish the run it holds
and joins the threads until the deadline; it returns False when a worker was still mid-run, which is never
waited on past the deadline (the threads are daemon threads). `stop_event` is set on stop so a long `execute`
can wait on it instead of sleeping. A submit after stop is logged and dropped. An error inside the dispatcher
itself (a clock that raises, say) is logged and the dispatcher carries on, so queued runs are never stranded.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from .timeutil import utcnow

log = logging.getLogger(__name__)

DEFAULT_MAX_INFLIGHT = 2  # plate_engines.models.Route.max_inflight's default
UNRESOLVED_ROUTE = "?"
INLINE_MESSAGE = "inline executor: run the BackgroundTask path"

Pair = tuple[str, str]
Scan = Callable[[], Iterable[tuple[str, str]]]


def _single_route(project_id: str, run_id: str) -> str:
    return "*"


class RunExecutor:
    def __init__(
        self,
        execute: Callable[[str, str], Any],
        *,
        workers: int,
        max_inflight_for: Callable[[str, str], int],
        route_key_for: Callable[[str, str], str] | None = None,
        clock: Callable[[], datetime] = utcnow,
        watch_interval_s: float = 30.0,
    ):
        workers = int(workers)
        if workers < 0:
            raise ValueError(f"workers must be >= 0, got {workers}")
        if watch_interval_s <= 0:
            raise ValueError(f"watch_interval_s must be > 0, got {watch_interval_s}")
        self.execute = execute
        self.workers = workers
        self.inline = workers == 0
        self.max_inflight_for = max_inflight_for
        self.route_key_for = route_key_for or _single_route
        self.clock = clock
        self.watch_interval_s = float(watch_interval_s)
        self.stop_event = threading.Event()
        self._cv = threading.Condition(threading.Lock())
        self._queue: deque[Pair] = deque()
        self._queued: set[Pair] = set()
        self._resolved: dict[Pair, tuple[str, int]] = {}  # queued pair -> (route key, cap)
        self._running: dict[Pair, str] = {}  # running pair -> route key
        self._by_route: Counter[str] = Counter()
        self._idle = workers
        self._dirty = False  # something changed that the dispatcher has not looked at
        self._scans: list[Scan] = []
        self._last_scan: datetime | None = None
        self._next_scan: datetime | None = None
        self._handoff: queue.SimpleQueue[Pair | None] = queue.SimpleQueue()
        self._threads: list[threading.Thread] = []
        self._started = False
        self._stopped = False

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> RunExecutor:
        """Launch the dispatcher and the workers (no-op when inline or already started)."""
        if self.inline:
            return self
        with self._cv:
            if self._stopped:
                raise RuntimeError("executor stopped")
            if self._started:
                return self
            self._started = True
            now = self.clock()
            self._last_scan = now
            self._next_scan = now + timedelta(seconds=self.watch_interval_s)
            self._dirty = True
            # started under the lock so a concurrent stop() sees (and joins) every thread
            for n in range(1, self.workers + 1):
                t = threading.Thread(target=self._work_loop, name=f"plate-run-{n}", daemon=True)
                self._threads.append(t)
                t.start()
            t = threading.Thread(target=self._dispatch_loop, name="plate-executor", daemon=True)
            self._threads.append(t)
            t.start()
        return self

    def stop(self, timeout_s: float = 5.0) -> bool:
        """Drop the queue, stop the threads and wait for them until `timeout_s`. True when every thread ended."""
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._cv:
            if self._stopped:
                threads = list(self._threads)
            else:
                self._stopped = True
                self.stop_event.set()
                dropped = len(self._queue)
                self._queue.clear()
                self._queued.clear()
                self._resolved.clear()
                self._dirty = True
                self._cv.notify_all()
                threads = list(self._threads)
                if dropped:
                    log.info("executor stopped with %d queued run(s) left for the next resume", dropped)
                for _ in range(self.workers if self._started else 0):
                    self._handoff.put(None)
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        alive = [t.name for t in threads if t.is_alive()]
        if alive:
            log.warning("executor stop timed out with %d thread(s) mid-run: %s", len(alive), ", ".join(alive))
        return not alive

    # ------------------------------------------------------------------ queue

    def submit(self, project_id: str, run_ids: Sequence[str]) -> None:
        """Enqueue `(project_id, run_id)` pairs FIFO; runs already queued or running are ignored."""
        if self.inline:
            raise RuntimeError(INLINE_MESSAGE)
        if isinstance(run_ids, str):
            run_ids = [run_ids]
        self._enqueue((project_id, rid) for rid in run_ids)

    def _enqueue(self, pairs: Iterable[Pair]) -> int:
        added = 0
        with self._cv:
            if self._stopped:
                pairs = list(pairs)
                if pairs:
                    log.warning(
                        "executor stopped: %d run(s) not queued (the next resume picks them up)", len(pairs)
                    )
                return 0
            for pair in pairs:
                if pair in self._queued or pair in self._running:
                    continue
                self._queue.append(pair)
                self._queued.add(pair)
                added += 1
            if added:
                self._dirty = True
                self._cv.notify_all()
        return added

    def watch(self, scan: Scan) -> None:
        """Register a scan for orphaned runs; the dispatcher runs every scan each `watch_interval_s`."""
        with self._cv:
            self._scans.append(scan)
            self._cv.notify_all()

    def scan_now(self) -> int:
        """Run every registered scan now, in this thread. Returns how many runs it queued."""
        if self.inline:
            return 0
        with self._cv:
            scans = list(self._scans)
        added = 0
        for scan in scans:
            try:
                pairs = [p for p in scan() if self._is_pair(p)]
            except Exception as e:  # a broken scan never stops the dispatcher
                log.exception("executor watch scan failed: %s", e)
                continue
            n = self._enqueue((str(p[0]), str(p[1])) for p in pairs)
            if n:
                log.info("executor watch queued %d orphaned run(s)", n)
            added += n
        return added

    @staticmethod
    def _is_pair(p: Any) -> bool:
        if isinstance(p, tuple | list) and len(p) == 2:
            return True
        log.warning("executor watch scan yielded %r, not a (project_id, run_id) pair", p)
        return False

    # ------------------------------------------------------------------ reading

    def running_for(self, route_key: str) -> int:
        with self._cv:
            return self._by_route[route_key]

    def stats(self) -> dict[str, Any]:
        with self._cv:
            return {
                "queued": len(self._queue),
                "running": len(self._running),
                "workers": self.workers,
                "inline": self.inline,
            }

    def wait_idle(self, timeout_s: float) -> bool:
        """Wait until nothing is queued or running; False when `timeout_s` passes first."""
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._cv:
            while self._queue or self._running:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(remaining)
            return True

    # ------------------------------------------------------------------ threads

    def _resolve(self, pair: Pair) -> tuple[str, int]:
        pid, rid = pair
        try:
            key = str(self.route_key_for(pid, rid))
        except Exception as e:
            log.warning(
                "route key for run %s/%s failed (%s); it runs under route %r", pid, rid, e, UNRESOLVED_ROUTE
            )
            key = UNRESOLVED_ROUTE
        try:
            cap = self.max_inflight_for(pid, rid)
            cap = DEFAULT_MAX_INFLIGHT if cap is None else int(cap)
        except Exception as e:
            log.warning(
                "max_inflight for run %s/%s failed (%s); default %d", pid, rid, e, DEFAULT_MAX_INFLIGHT
            )
            cap = DEFAULT_MAX_INFLIGHT
        return key, max(1, cap)

    def _scan_due(self) -> bool:
        if not self._scans or self._next_scan is None:
            return False
        now = self.clock()
        return now >= self._next_scan or (self._last_scan is not None and now < self._last_scan)

    def _wait_s(self) -> float | None:
        if not self._scans or self._next_scan is None:
            return None
        left = (self._next_scan - self.clock()).total_seconds()
        return min(self.watch_interval_s, max(0.0, left))

    def _dispatch_locked(self) -> None:
        """One pass over the queue (caller holds the lock): free worker + room on the route -> hand it over;
        at its route cap -> back of the queue; no free worker -> the rest keeps its order."""
        for _ in range(len(self._queue)):
            if self._idle <= 0:
                return
            pair = self._queue.popleft()
            res = self._resolved.get(pair)
            if res is None:  # submitted while the dispatcher was resolving: next pass
                self._queue.append(pair)
                self._dirty = True
                continue
            key, cap = res
            if self._by_route[key] >= cap:
                self._queue.append(pair)
                continue
            self._queued.discard(pair)
            del self._resolved[pair]
            self._running[pair] = key
            self._by_route[key] += 1
            self._idle -= 1
            self._handoff.put(pair)

    def _dispatch_loop(self) -> None:
        while not self._stopped:
            try:
                self._dispatch_once()
            except Exception as e:  # a dispatcher that died would strand every queued run: log and go on
                log.exception("executor dispatcher error: %s", e)
                with self._cv:
                    self._dirty = True
                self.stop_event.wait(0.1)

    def _dispatch_once(self) -> None:
        """Wait for a change or a due scan, then scan, resolve what is new and hand runs to free workers."""
        with self._cv:
            while not self._stopped and not self._dirty and not self._scan_due():
                self._cv.wait(self._wait_s())
            if self._stopped:
                return
            self._dirty = False
            scan = self._scan_due()
            if scan:
                now = self.clock()
                self._last_scan = now
                self._next_scan = now + timedelta(seconds=self.watch_interval_s)
            todo = [p for p in self._queue if p not in self._resolved]
        if scan:
            self.scan_now()
        if todo:
            resolved = {p: self._resolve(p) for p in todo}
            with self._cv:
                for p, r in resolved.items():
                    if p in self._queued:
                        self._resolved[p] = r
        with self._cv:
            if self._stopped:
                return
            self._dispatch_locked()

    def _work_loop(self) -> None:
        while True:
            pair = self._handoff.get()
            if pair is None:
                return
            pid, rid = pair
            try:
                self.execute(pid, rid)
            except BaseException as e:  # logged, never propagated: the worker takes the next run
                log.exception("run %s/%s stopped: %s: %s", pid, rid, type(e).__name__, e)
            finally:
                with self._cv:
                    key = self._running.pop(pair, None)
                    if key is not None:
                        self._by_route[key] -= 1
                        if self._by_route[key] <= 0:
                            del self._by_route[key]
                    self._idle += 1
                    self._dirty = True
                    self._cv.notify_all()
