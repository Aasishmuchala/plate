"""Nightly jobs as daemon tasks: price refresh, retirement check, contract tests, reconciliation, weekly
canaries and backup (router plan T50; BUILD_PLAN section 4 "Routing" and section 9).

`Nightly(settings)` owns six jobs and runs them once per local date, inside a window that starts at
`02:00` (config below). It is glue over the pieces other tasks built: `plate_engines.prices` (T27),
`plate_engines.retire` (T27), `plate_engines.canaries` (T27), `plate_daemon.reconcile` and `ledger` (T13)
and, when it exists, `plate_daemon.tools.backup` (T42). Nothing here talks to a vendor on its own: the
default run is offline (catalogue files, billing CSVs and recorded contract fixtures); a live call happens
only under `PLATE_LIVE=1` with a budget, and only through cases the caller registers.

Jobs, in this order (the order matters: prices first so a requote flag is raised before anything else
reads the overlay, contract tests before canaries):

    prices_refresh   prices.refresh over the vendor catalogue JSON files; the overlay is rewritten only
                     when a price moved. A move over 5% flags every QUOTED run on that engine (and tier)
                     for requote: a `requote` alert per run and `requote_runs` in the record.
    retire_check     retire.check: a pinned model id that no catalogue lists any more -> `retired_pin`.
    contract_tests   every adapter's recorded fixtures (a pytest run of the engines contract suites by
                     default); with PLATE_LIVE=1 and `live_budget_usd` > 0 also the cheapest live cases
                     and the output-depends-on-input check. Each failure is a `contract_test_failed` alert.
                     Registered schema pairs (old schema, schema served today) are compared with
                     schema_diff.diff: a removed required field or a changed enum is a `schema_change`.
    reconcile        yesterday's UTC day of billing CSVs against the ledgers of every project; the matched
                     bills are written into the owning project's ledger once (record_billed). Over 1% or
                     any unledgered spend -> `reconcile_failed`.
    canaries         weekly (every `canary_every_days`, default 7): canaries.run over the registered
                     canary set, compared with the previous report; drift suspends the engine.
    backup           plate_daemon.tools.backup.run per project into `<backup_dir>/<project_id>/<date>`.

A job whose input is not configured (no catalogue folder, no billing exports, no canary set, no backup
folder) is recorded as `skipped` with the reason; that is not an error and raises no alert.

Files, all under `<home>/logs/` (Windows safe names, UTF-8, LF):

    nightly/nightly_<YYYY-MM-DD>.json   the record of that date: {v, date, window, done, jobs: {name:
                                        {status, started, finished, message, summary, alerts}}}. `done` is
                                        the marker: true once every job has a result, and a done date is
                                        never run again by the timer. The record is rewritten (atomically)
                                        after each job, so a crash resumes with the jobs that are missing.
    nightly/canaries_<date>.json        the canary report that the next weekly run is compared with.
    alerts.jsonl                        the shared alert stream. The engine kinds (price_change, requote,
                                        retired_pin, canary_drift, engine_suspended ...) are written by
                                        plate_engines.health.AlertLog; the nightly kinds below are written
                                        here in the same line shape. AlertLog.read() skips lines whose kind
                                        it does not know, so use `read_alerts()` to see every line.

Job status is `ok`, `alerted` (it ran and raised a warn or critical alert), `skipped` or `error` (the job
raised; a `job_error` alert is written and the remaining jobs still run). Nightly alert kinds:
`job_error`, `contract_test_failed`, `reconcile_failed`, `backup_failed`.

Configuration is the `"nightly"` object of `config.json` (the file `Settings.config_path` names); every key
is optional and an unknown key is refused so a typo cannot silently disable a job:

    {"nightly": {"enabled": true, "time": "02:00", "catch_up": true, "window_h": 4,
                 "poll_s": 900, "startup_delay_s": 30,
                 "catalogue_dir": "catalogues", "billing_dir": "billing", "backup_dir": null,
                 "prices_overlay": null, "live_budget_usd": 0, "canary_every_days": 7,
                 "record_billed": true, "disabled_jobs": []}}

Relative folders are relative to the app home. `PLATE_NIGHTLY_TIME` overrides `time`; `PLATE_NIGHTLY=0`
turns the scheduler off. `time` is local wall-clock time, `HH:MM` or `HH:MM:SS`.

Scheduling uses `threading.Timer` on a daemon thread. The timer never sleeps longer than `poll_s` (15
minutes) so a laptop that slept through 02:00, or a DST change, is noticed at the next wake-up. With
`catch_up` (default) a daemon that starts after the window runs the missing date `startup_delay_s` later;
without it the date is skipped once more than `window_h` hours past the start. The clock, the timer factory,
the environment and every external seam (catalogue fetcher, contract runner, live cases, canary set, backup
function, quoted-run source, ledger factory) are injectable, so tests never sleep and never reach a vendor.

Wiring (daemon lifespan, a three-line change in api.py that this task does not own):

    nightly = Nightly(settings, ledger_factory=..., on_alert=...)   # a factory that reuses the open projects.Lock
    nightly.start()                                                 # on startup
    nightly.stop()                                                  # on shutdown

`on_alert(line)` (also settable as `nightly.on_alert`) is called with a copy of every alert line the nightly
appends to `alerts.jsonl`, engine kinds and nightly kinds alike, so the daemon can put it on the bus for the
artist (router plan T85). A callback that raises is logged and never stops a job.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal
from pathlib import Path
from typing import Any

from plate_engines import canaries, prices, retire, schema_diff
from plate_engines.canaries import CanaryReport
from plate_engines.health import Alert, AlertLog
from plate_engines.loader import load_manifests

from .db import connect, loads
from .fsutil import read_json, write_json_atomic
from .index import Index
from .ledger import Ledger
from .projects import PROJECT_JSON, Project
from .reconcile import load_billing, reconcile, record_billed
from .timeutil import iso, stamp

log = logging.getLogger(__name__)

JOB_ORDER = ("prices_refresh", "retire_check", "contract_tests", "reconcile", "canaries", "backup")
NIGHTLY_ALERT_KINDS = frozenset({"job_error", "contract_test_failed", "reconcile_failed", "backup_failed"})
DEFAULT_TIME = "02:00"
MAX_FAILURE_ALERTS = 25
RECORD_VERSION = 1
DEFAULT_CONTRACT_TESTS = (
    "engines/tests/test_atlas_contract.py",
    "engines/tests/test_transport.py",
    "engines/tests/test_adapters_image.py",
    "engines/tests/test_adapters_video.py",
)
CONFIG_KEYS = frozenset(
    {
        "enabled",
        "time",
        "catch_up",
        "window_h",
        "poll_s",
        "startup_delay_s",
        "catalogue_dir",
        "billing_dir",
        "backup_dir",
        "prices_overlay",
        "live_budget_usd",
        "canary_every_days",
        "record_billed",
        "disabled_jobs",
    }
)
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_DAY_RE = re.compile(r"^canaries_(\d{4}-\d{2}-\d{2})\.json$")
_FAILED_RE = re.compile(r"^FAILED\s+(\S+)(?:\s+-\s+(.*))?$", re.M)
_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors)\b")


class NightlyConfigError(ValueError):
    """The `nightly` block of config.json (or PLATE_NIGHTLY_TIME) is not valid."""


class ContractUnavailable(RuntimeError):
    """The recorded-fixture contract suites cannot run here (no checkout, no pytest)."""


# ---------------------------------------------------------------------------- configuration


def parse_clock_time(text: Any) -> dtime:
    """`HH:MM` or `HH:MM:SS` (local wall-clock time) -> datetime.time."""
    m = _TIME_RE.match(str(text).strip())
    if not m:
        raise NightlyConfigError(f"nightly time {text!r}: expected HH:MM or HH:MM:SS")
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if h > 23 or mi > 59 or s > 59:
        raise NightlyConfigError(f"nightly time {text!r}: out of range")
    return dtime(h, mi, s)


def _number(raw: Mapping[str, Any], key: str, default: float, *, lo: float, hi: float | None = None) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise NightlyConfigError(f"nightly.{key}: expected a number, got {value!r}")
    if value < lo or (hi is not None and value > hi):
        raise NightlyConfigError(
            f"nightly.{key}: {value} is outside {lo} to {hi if hi is not None else 'inf'}"
        )
    return float(value)


def _flag(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise NightlyConfigError(f"nightly.{key}: expected true or false, got {value!r}")
    return value


def _folder(raw: Mapping[str, Any], key: str, home: Path, default: Path | None) -> Path | None:
    if key not in raw:
        return default
    value = raw[key]
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise NightlyConfigError(f"nightly.{key}: expected a folder path or null, got {value!r}")
    p = Path(value)
    return p if p.is_absolute() else home / p


@dataclass(frozen=True)
class NightlyConfig:
    enabled: bool = True
    time: dtime = dtime(2, 0)
    catch_up: bool = True
    window_h: float = 4.0
    poll_s: float = 900.0
    startup_delay_s: float = 30.0
    catalogue_dir: Path | None = None
    billing_dir: Path | None = None
    backup_dir: Path | None = None
    prices_overlay: Path | None = None
    live_budget_usd: float = 0.0
    canary_every_days: int = 7
    record_billed: bool = True
    disabled_jobs: frozenset[str] = frozenset()

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any] | None, home: Path, env: Mapping[str, str] | None = None
    ) -> NightlyConfig:
        env = os.environ if env is None else env
        raw = dict(raw or {})
        unknown = sorted(set(raw) - CONFIG_KEYS)
        if unknown:
            raise NightlyConfigError(f"nightly: unknown keys {unknown}; expected {sorted(CONFIG_KEYS)}")
        disabled = raw.get("disabled_jobs", [])
        if not isinstance(disabled, list) or not all(isinstance(j, str) for j in disabled):
            raise NightlyConfigError("nightly.disabled_jobs: expected a list of job names")
        bad = sorted(set(disabled) - set(JOB_ORDER))
        if bad:
            raise NightlyConfigError(f"nightly.disabled_jobs: unknown jobs {bad}; expected {list(JOB_ORDER)}")
        enabled = _flag(raw, "enabled", True)
        if env.get("PLATE_NIGHTLY", "").strip().lower() in {"0", "off", "false", "no"}:
            enabled = False
        return cls(
            enabled=enabled,
            time=parse_clock_time(env.get("PLATE_NIGHTLY_TIME") or raw.get("time", DEFAULT_TIME)),
            catch_up=_flag(raw, "catch_up", True),
            window_h=_number(raw, "window_h", 4.0, lo=0.0, hi=24.0),
            poll_s=_number(raw, "poll_s", 900.0, lo=0.001),
            startup_delay_s=_number(raw, "startup_delay_s", 30.0, lo=0.0),
            catalogue_dir=_folder(raw, "catalogue_dir", home, home / "catalogues"),
            billing_dir=_folder(raw, "billing_dir", home, home / "billing"),
            backup_dir=_folder(raw, "backup_dir", home, None),
            prices_overlay=_folder(raw, "prices_overlay", home, None),
            live_budget_usd=_number(raw, "live_budget_usd", 0.0, lo=0.0),
            canary_every_days=int(_number(raw, "canary_every_days", 7, lo=1, hi=366)),
            record_billed=_flag(raw, "record_billed", True),
            disabled_jobs=frozenset(disabled),
        )

    @classmethod
    def load(cls, settings: Any, env: Mapping[str, str] | None = None) -> NightlyConfig:
        home = Path(settings.home)
        raw: Any = {}
        config_path = getattr(settings, "config_path", None)
        if config_path and Path(config_path).is_file():
            try:
                doc = read_json(Path(config_path))
            except ValueError as exc:
                raise NightlyConfigError(f"{config_path}: not valid JSON ({exc})") from exc
            raw = doc.get("nightly") if isinstance(doc, dict) else None
            if raw is not None and not isinstance(raw, dict):
                raise NightlyConfigError(f"{config_path}: 'nightly' must be an object")
        return cls.from_mapping(raw, home, env)

    @property
    def time_text(self) -> str:
        return self.time.strftime("%H:%M:%S") if self.time.second else self.time.strftime("%H:%M")


# ---------------------------------------------------------------------------- alerts


def _stamp_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_alert_line(path: Path, line: Mapping[str, Any]) -> None:
    text = json.dumps(dict(line), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n")


def read_alerts(path: Path | str) -> list[dict[str, Any]]:
    """Every alert line in `alerts.jsonl` as a dict, nightly kinds included. A torn line is skipped."""
    p = Path(path)
    if not p.is_file():
        return []
    out: list[dict[str, Any]] = []
    with open(p, encoding="utf-8", newline="") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


class _Alerts(AlertLog):
    """The engines AlertLog, plus nightly-kind lines in the same shape, plus a copy of every line a job wrote
    (`captured`) so the job's record can say what it raised."""

    def __init__(
        self,
        path: Path,
        clock: Callable[[], datetime],
        on_alert: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(path, clock)
        self._now = clock
        self._on_alert = on_alert
        self.captured: list[dict[str, Any]] = []

    def _emit(self, line: Mapping[str, Any]) -> None:
        """Hand a copy of a line that is now in the file to `on_alert`; its failure is logged, never raised."""
        if self._on_alert is None:
            return
        try:
            self._on_alert(_jsonable(dict(line)))
        except Exception as exc:  # the file is the record; a listener that breaks must not break a job
            log.warning("on_alert failed for %s alert: %s: %s", line.get("kind"), type(exc).__name__, exc)

    def append(self, alert: Alert) -> Alert:
        stamped = super().append(alert)
        line = stamped.to_dict()
        self.captured.append(line)
        self._emit(line)
        return stamped

    def nightly(
        self,
        kind: str,
        message: str,
        *,
        severity: str = "critical",
        engine_id: str | None = None,
        route_id: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if kind not in NIGHTLY_ALERT_KINDS:
            raise ValueError(
                f"unknown nightly alert kind {kind!r}; expected one of {sorted(NIGHTLY_ALERT_KINDS)}"
            )
        line = {
            "v": 1,
            "at": _stamp_utc(self._now()),
            "kind": kind,
            "severity": severity,
            "engine_id": engine_id,
            "route_id": route_id,
            "message": message,
            "data": _jsonable(dict(data or {})),
        }
        _append_alert_line(self.path, line)
        self.captured.append(line)
        self._emit(line)
        return line


# ---------------------------------------------------------------------------- seams the caller can supply


@dataclass
class ContractOutcome:
    """What a contract runner returns: ok, counts, and one line per failure (`test id - reason`)."""

    ok: bool
    passed: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass(frozen=True)
class LiveContractCase:
    """One live contract call. `run()` makes the real (cheapest) call(s) and returns (input digest, output
    bytes) pairs; the nightly job then runs the output-depends-on-input check over them."""

    engine_id: str
    cost_usd: float
    run: Callable[[], Sequence[tuple[str, bytes]]]


@dataclass
class CanarySet:
    """Everything canaries.run needs: 12 owned plates, 4 moves, the adapters, the gate callback."""

    plates: Sequence[Any]
    moves: Sequence[Any]
    adapters: Mapping[str, Any]
    gates: Callable[[Any, bytes, str], Any]
    run_kwargs: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SchemaPair:
    """The request schema an adapter was built from and the one the vendor serves today, for schema_diff."""

    engine_id: str
    old: Mapping[str, Any]
    new: Mapping[str, Any]
    route_id: str | None = None


class PytestContractRunner:
    """Runs the engines' recorded-fixture contract suites in a child pytest (offline: those suites block
    sockets) and turns the result into a ContractOutcome. Needs a source checkout and pytest."""

    def __init__(
        self,
        repo_root: Path | str | None = None,
        paths: Sequence[str] = DEFAULT_CONTRACT_TESTS,
        timeout_s: float = 900.0,
    ) -> None:
        self.repo_root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
        self.paths = tuple(paths)
        self.timeout_s = timeout_s

    def __call__(self) -> ContractOutcome:
        if importlib.util.find_spec("pytest") is None:
            raise ContractUnavailable("pytest is not installed in this environment")
        targets = [p for p in self.paths if (self.repo_root / p).exists()]
        if not targets:
            raise ContractUnavailable(f"no contract suites under {self.repo_root} ({', '.join(self.paths)})")
        env = {k: v for k, v in os.environ.items() if k != "PLATE_LIVE"}
        env["PYTHONIOENCODING"] = "utf-8"
        cmd = [sys.executable, "-m", "pytest", *targets, "-rf", "--tb=no", "-p", "no:cacheprovider"]
        try:
            done = subprocess.run(
                cmd,
                cwd=self.repo_root,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ContractOutcome(
                False, failed=1, failures=[f"contract suites - timed out after {self.timeout_s:g} s"]
            )
        return parse_pytest_output(done.returncode, done.stdout)


def parse_pytest_output(returncode: int, output: str) -> ContractOutcome:
    """A child pytest's exit code and text -> ContractOutcome (`-rf` lists one FAILED line per failure)."""
    failures = [f"{m.group(1)} - {m.group(2) or 'failed'}" for m in _FAILED_RE.finditer(output)]
    passed = failed = 0
    for line in reversed(output.strip().splitlines()):
        found = _COUNT_RE.findall(line)
        if found and " in " in line:
            for n, word in found:
                if word == "passed":
                    passed = int(n)
                else:
                    failed += int(n)
            break
    if returncode == 0:
        return ContractOutcome(True, passed, 0, [])
    if not failures:
        tail = " | ".join(output.strip().splitlines()[-3:])
        failures = [f"pytest exited {returncode} - {tail}".strip()]
    return ContractOutcome(False, passed, max(failed, len(failures)), failures)


# ---------------------------------------------------------------------------- job plumbing


@dataclass
class JobOutcome:
    status: str = "ok"  # ok | skipped | error; the runner upgrades ok to alerted
    message: str = ""
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class JobContext:
    now: datetime
    day: str
    alerts: _Alerts
    live: bool
    force: bool = False


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, set | frozenset):
        return [_jsonable(v) for v in sorted(value, key=str)]
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _jsonable(to_dict())
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def _load_backup_runner() -> Callable[..., Any] | None:
    name = f"{__package__}.tools.backup"
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name == name:
            return None
        raise
    run = getattr(module, "run", None)
    return run if callable(run) else None


# ---------------------------------------------------------------------------- the scheduler


class Nightly:
    """Runs the nightly jobs once per local date. See the module docstring for the contract."""

    def __init__(
        self,
        settings: Any,
        *,
        config: NightlyConfig | None = None,
        clock: Callable[[], datetime] | None = None,
        timer_factory: Callable[[float, Callable[[], None]], Any] | None = None,
        env: Mapping[str, str] | None = None,
        jobs: Mapping[str, Callable[[JobContext], JobOutcome | None]] | None = None,
        manifests: Mapping[str, Any] | None = None,
        quoted_runs: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
        ledger_factory: Callable[[Project], Ledger] | None = None,
        catalogue_fetcher: Callable[[Path], Any] | None = None,
        contract_runner: Callable[[], ContractOutcome] | None = None,
        live_cases: Callable[[], Sequence[LiveContractCase]] | None = None,
        canary_set: Callable[[], CanarySet] | None = None,
        schema_pairs: Callable[[], Iterable[SchemaPair]] | None = None,
        backup_fn: Callable[..., Any] | None = None,
        on_alert: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.on_alert = on_alert
        self.home = Path(settings.home)
        self.env = os.environ if env is None else env
        self.config = config or NightlyConfig.load(settings, self.env)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._timer_factory = timer_factory or threading.Timer
        self._manifests_arg = manifests
        self._quoted_runs_fn = quoted_runs
        self._ledger_factory = ledger_factory or (lambda project: Ledger(project.root))
        self._catalogue_fetcher = catalogue_fetcher
        self._contract_runner = contract_runner
        self._live_cases = live_cases
        self._canary_set = canary_set
        self._schema_pairs = schema_pairs
        self._backup_fn = backup_fn
        self.logs_dir = self.home / "logs"
        self.nightly_dir = self.logs_dir / "nightly"
        self.alerts_path = self.logs_dir / "alerts.jsonl"
        self._jobs: dict[str, Callable[[JobContext], JobOutcome | None]] = {
            name: getattr(self, f"_job_{name}") for name in JOB_ORDER
        }
        for name, fn in (jobs or {}).items():
            if name not in self._jobs:
                raise ValueError(f"unknown nightly job {name!r}; expected one of {list(JOB_ORDER)}")
            self._jobs[name] = fn
        self._run_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._timer: Any = None
        self._started = False
        self._stopping = False
        self.last_error: str | None = None

    def _alerts(self) -> _Alerts:
        return _Alerts(self.alerts_path, self._clock, self._forward_alert)

    def _forward_alert(self, line: dict[str, Any]) -> None:
        """Read `on_alert` at call time, so the daemon can attach the bus after the Nightly was built."""
        callback = self.on_alert
        if callback is not None:
            callback(line)

    # ------------------------------------------------------------------ records

    def record_path(self, day: str) -> Path:
        return self.nightly_dir / f"nightly_{day}.json"

    def read_record(self, day: str) -> dict[str, Any] | None:
        path = self.record_path(day)
        if not path.is_file():
            return None
        try:
            rec = read_json(path)
        except (OSError, ValueError):
            return None
        return rec if isinstance(rec, dict) and isinstance(rec.get("jobs"), dict) else None

    def _write_record(self, day: str, rec: Mapping[str, Any]) -> None:
        write_json_atomic(self.record_path(day), rec, indent=1)

    def _new_record(self, day: str, now: datetime) -> dict[str, Any]:
        return {
            "v": RECORD_VERSION,
            "date": day,
            "window": self.config.time_text,
            "started_at": iso(now),
            "finished_at": None,
            "done": False,
            "jobs": {},
        }

    # ------------------------------------------------------------------ window and ticking

    def _window_start(self, now: datetime) -> datetime:
        return datetime.combine(now.date(), self.config.time, tzinfo=now.tzinfo)

    def _pending_today(self, now: datetime) -> bool:
        start = self._window_start(now)
        if now < start:
            return False
        day = now.date().isoformat()
        rec = self.read_record(day)
        if rec and rec.get("done"):
            return False
        return self.config.catch_up or now - start <= timedelta(hours=self.config.window_h)

    def next_run(self, now: datetime | None = None) -> datetime:
        """`now` when today's date is pending, else the start of the next window."""
        now = now or self._clock()
        if self._pending_today(now):
            return now
        start = self._window_start(now)
        return start if now < start else start + timedelta(days=1)

    def tick(self, now: datetime | None = None) -> dict[str, Any] | None:
        """Run today's missing jobs if the window has opened and the date is not done. Returns the record
        when jobs ran, None when there was nothing to do (or another run holds the lock)."""
        now = now or self._clock()
        if not self._pending_today(now):
            return None
        return self._run_day(now, now.date().isoformat(), None, force=False, blocking=False)

    def run_now(
        self, jobs: Iterable[str] | None = None, *, force: bool = False, now: datetime | None = None
    ) -> dict[str, Any]:
        """Run the named jobs (default all) for today's date regardless of the window. Jobs that already have
        a result today are left alone unless `force`. Blocks while another run is in progress."""
        names = None if jobs is None else list(jobs)
        unknown = sorted(set(names or ()) - set(JOB_ORDER))
        if unknown:
            raise ValueError(f"unknown nightly jobs {unknown}; expected {list(JOB_ORDER)}")
        now = now or self._clock()
        rec = self._run_day(now, now.date().isoformat(), names, force=force, blocking=True)
        assert rec is not None
        return rec

    def _run_day(
        self, now: datetime, day: str, names: list[str] | None, *, force: bool, blocking: bool
    ) -> dict[str, Any] | None:
        if not self._run_lock.acquire(blocking=blocking):
            return None
        try:
            rec = self.read_record(day) or self._new_record(day, now)
            alerts = self._alerts()
            ctx = JobContext(
                now=now, day=day, alerts=alerts, live=self.env.get("PLATE_LIVE") == "1", force=force
            )
            for name in JOB_ORDER:
                if names is not None and name not in names:
                    continue
                if name in rec["jobs"] and not force:
                    continue
                rec["jobs"][name] = self._run_job(name, ctx)
                self._write_record(day, rec)
            rec["done"] = all(name in rec["jobs"] for name in JOB_ORDER)
            if rec["done"]:
                rec["finished_at"] = iso(self._clock())
            self._write_record(day, rec)
            return rec
        finally:
            self._run_lock.release()

    def _run_job(self, name: str, ctx: JobContext) -> dict[str, Any]:
        ctx.alerts.captured = []
        started = iso(self._clock())
        if name in self.config.disabled_jobs and not ctx.force:
            out = JobOutcome("skipped", "disabled in config (nightly.disabled_jobs)")
        else:
            try:
                out = self._jobs[name](ctx) or JobOutcome()
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                ctx.alerts.nightly("job_error", f"{name}: {message}", data={"job": name})
                out = JobOutcome("error", message)
        status = out.status
        if status == "ok" and any(a.get("severity") in ("warn", "critical") for a in ctx.alerts.captured):
            status = "alerted"
        return {
            "status": status,
            "started": started,
            "finished": iso(self._clock()),
            "message": out.message,
            "summary": _jsonable(out.summary),
            "alerts": _jsonable(ctx.alerts.captured),
        }

    # ------------------------------------------------------------------ the timer

    def _next_delay(self, now: datetime, first: bool) -> float:
        if self._pending_today(now):
            return self.config.startup_delay_s if first else 5.0
        return max((self.next_run(now) - now).total_seconds(), 0.0)

    def _arm(self, first: bool = False) -> None:
        delay = min(self._next_delay(self._clock(), first), self.config.poll_s)
        timer = self._timer_factory(delay, self._fire)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _fire(self) -> None:
        try:
            self.tick()
        except Exception as exc:  # a timer thread must survive: say so and try again next time
            self.last_error = f"{type(exc).__name__}: {exc}"
            try:
                self._alerts().nightly(
                    "job_error", f"scheduler: {self.last_error}", data={"job": "scheduler"}
                )
            except OSError:
                pass
        finally:
            with self._state_lock:
                if self._started and not self._stopping:
                    self._arm()

    def start(self) -> bool:
        """Arm the timer. False when the scheduler is disabled or already started."""
        if not self.config.enabled:
            return False
        with self._state_lock:
            if self._started:
                return False
            self._started, self._stopping = True, False
            self._arm(first=True)
        return True

    def stop(self, wait_s: float = 0.0) -> None:
        """Cancel the timer. A job that is already running finishes; with `wait_s` this waits for it."""
        with self._state_lock:
            self._stopping, self._started = True, False
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        if wait_s > 0 and self._run_lock.acquire(timeout=wait_s):
            self._run_lock.release()

    @property
    def running(self) -> bool:
        return self._run_lock.locked()

    def status(self) -> dict[str, Any]:
        """A small summary for the UI status bar: enabled, next run, today's job statuses."""
        now = self._clock()
        rec = self.read_record(now.date().isoformat())
        return {
            "enabled": self.config.enabled,
            "started": self._started,
            "running": self.running,
            "window": self.config.time_text,
            "next_run": iso(self.next_run(now)),
            "today": None
            if rec is None
            else {
                "done": bool(rec.get("done")),
                "jobs": {k: v.get("status") for k, v in rec["jobs"].items()},
            },
            "alerts_path": str(self.alerts_path),
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------ shared lookups

    def _manifests(self) -> Mapping[str, Any]:
        return self._manifests_arg if self._manifests_arg is not None else load_manifests()

    def _projects(self) -> tuple[list[Project], list[str]]:
        """Projects this daemon knows: the index's registered roots plus every folder under projects_dir."""
        candidates: list[Path] = []
        problems: list[str] = []
        index = Index(self.home)
        try:
            for pid in index.project_ids():
                root = index.project_root(pid)
                if root is not None:
                    candidates.append(root)
        except (OSError, sqlite3.Error, ValueError) as exc:
            problems.append(f"index: {type(exc).__name__}: {exc}")
        projects_dir = getattr(self.settings, "projects_dir", None)
        if projects_dir and Path(projects_dir).is_dir():
            candidates += sorted(p for p in Path(projects_dir).iterdir() if (p / PROJECT_JSON).is_file())
        found: dict[str, Project] = {}
        for root in candidates:
            try:
                project = Project.load(root)
            except (OSError, ValueError) as exc:
                problems.append(f"{root}: {exc}")
                continue
            found.setdefault(project.project_id, project)
        return list(found.values()), problems

    def _quoted_runs(self, manifests: Mapping[str, Any]) -> list[dict[str, Any]]:
        """QUOTED runs across the per-project indexes: {run_id, engine_id, tier, project_id}."""
        if self._quoted_runs_fn is not None:
            return [dict(r) for r in self._quoted_runs_fn()]
        out: list[dict[str, Any]] = []
        index = Index(self.home)
        for pid in index.project_ids():
            try:
                conn = connect(index.db_path(pid))
                try:
                    rows = conn.execute(
                        "SELECT id, route, quote_json FROM runs WHERE state = 'QUOTED'"
                    ).fetchall()
                finally:
                    conn.close()
            except sqlite3.Error:
                continue
            for row in rows:
                route, quote = loads(row["route"]) or {}, loads(row["quote_json"]) or {}
                engine_id = route.get("engine_id") or quote.get("model")
                label = (quote.get("size") or {}).get("label")
                manifest = manifests.get(engine_id)
                tier = label if manifest is not None and label in manifest.price.by else None
                out.append({"run_id": row["id"], "engine_id": engine_id, "tier": tier, "project_id": pid})
        return out

    def _catalogue_dir(self, ctx: JobContext) -> Path | None:
        cat = self.config.catalogue_dir
        if ctx.live and self._catalogue_fetcher is not None and cat is not None:
            self._catalogue_fetcher(cat)
        if cat is None or not cat.is_dir() or not any(cat.glob("*.json")):
            return None
        return cat

    # ------------------------------------------------------------------ jobs

    def _job_prices_refresh(self, ctx: JobContext) -> JobOutcome:
        cat = self._catalogue_dir(ctx)
        if cat is None:
            return JobOutcome("skipped", f"no vendor catalogue files in {self.config.catalogue_dir}")
        manifests = self._manifests()
        changes = prices.refresh(
            cat, manifests=manifests, overlay_path=self.config.prices_overlay, alerts=ctx.alerts
        )
        flagged: list[str] = []
        checked = 0
        if changes.requotes:
            quoted = self._quoted_runs(manifests)
            checked = len(quoted)
            flagged = prices.requote_runs(changes, quoted, alerts=ctx.alerts)
        summary = {
            "changes": [c.to_dict() for c in changes],
            "requotes": len(changes.requotes),
            "requote_runs": flagged,
            "quoted_runs_checked": checked,
            "skipped": list(changes.skipped),
        }
        return JobOutcome(summary=summary)

    def _job_retire_check(self, ctx: JobContext) -> JobOutcome:
        cat = self._catalogue_dir(ctx)
        if cat is None:
            return JobOutcome("skipped", f"no vendor catalogue files in {self.config.catalogue_dir}")
        projects, problems = self._projects()
        pins = {
            f"{p.project_id}:{engine_id}": (engine_id, version or None)
            for p in projects
            for engine_id, version in p.engine_pins.items()
        }
        found = retire.check(self._manifests(), prices.load_catalogues(cat), pins=pins, alerts=ctx.alerts)
        return JobOutcome(
            summary={
                "retired_pins": len(found),
                "projects": len(projects),
                "project_pins": len(pins),
                "problems": problems,
            }
        )

    def _job_contract_tests(self, ctx: JobContext) -> JobOutcome:
        summary: dict[str, Any] = {}
        ran = False
        runner = self._contract_runner or PytestContractRunner()
        try:
            outcome = runner()
        except ContractUnavailable as exc:
            summary["recorded"] = {"status": "skipped", "reason": str(exc)}
        else:
            ran = True
            summary["recorded"] = {
                "status": "ok" if outcome.ok else "failed",
                "passed": outcome.passed,
                "failed": outcome.failed,
                "failures": list(outcome.failures),
            }
            failures = outcome.failures or ([] if outcome.ok else ["the contract runner reported a failure"])
            self._contract_alerts(ctx, failures, "recorded")
        live_ran = self._live_contracts(ctx, summary)
        schema_ran = self._schema_checks(ctx, summary)
        if not (ran or live_ran or schema_ran):
            return JobOutcome("skipped", summary["recorded"]["reason"], summary)
        return JobOutcome(summary=summary)

    @staticmethod
    def _contract_alerts(
        ctx: JobContext, failures: Sequence[str], source: str, engine_id: str | None = None
    ) -> None:
        for line in list(failures)[:MAX_FAILURE_ALERTS]:
            ctx.alerts.nightly(
                "contract_test_failed",
                f"{source} contract test failed: {line}",
                engine_id=engine_id,
                data={"source": source, "test": line},
            )
        extra = len(failures) - MAX_FAILURE_ALERTS
        if extra > 0:
            ctx.alerts.nightly(
                "contract_test_failed",
                f"{extra} more {source} contract failures not listed",
                data={"source": source, "more": extra},
            )

    def _schema_checks(self, ctx: JobContext, summary: dict[str, Any]) -> bool:
        if self._schema_pairs is None:
            summary["schema"] = {"status": "skipped", "reason": "no schema sources registered"}
            return False
        checked = []
        for pair in self._schema_pairs():
            d = schema_diff.diff(
                pair.old, pair.new, alerts=ctx.alerts, engine_id=pair.engine_id, route_id=pair.route_id
            )
            checked.append(
                {
                    "engine_id": pair.engine_id,
                    "alert": d["alert"],
                    "added": d["added"],
                    "removed": d["removed"],
                    "changed": len(d["changed"]),
                    "reasons": d["reasons"],
                }
            )
        summary["schema"] = {"status": "ran" if checked else "skipped", "checked": checked}
        return bool(checked)

    def _live_contracts(self, ctx: JobContext, summary: dict[str, Any]) -> bool:
        budget = self.config.live_budget_usd
        if not ctx.live:
            summary["live"] = {"status": "skipped", "reason": "PLATE_LIVE is not 1"}
            return False
        if budget <= 0:
            summary["live"] = {"status": "skipped", "reason": "live_budget_usd is 0"}
            return False
        if self._live_cases is None:
            summary["live"] = {"status": "skipped", "reason": "no live contract cases registered"}
            return False
        from plate_engines.adapters.atlas_video import check_output_depends_on_input
        from plate_engines.adapters.base import SilentlyIgnoredInput

        spent, results, over_budget = 0.0, [], []
        for case in sorted(self._live_cases(), key=lambda c: (c.cost_usd, c.engine_id)):
            if spent + case.cost_usd > budget + 1e-9:
                over_budget.append(case.engine_id)
                continue
            spent += case.cost_usd
            try:
                check_output_depends_on_input(case.run())
            except SilentlyIgnoredInput as exc:
                results.append({"engine_id": case.engine_id, "status": "failed", "reason": str(exc)})
            except Exception as exc:
                results.append(
                    {
                        "engine_id": case.engine_id,
                        "status": "failed",
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
            else:
                results.append({"engine_id": case.engine_id, "status": "ok", "reason": ""})
            if results[-1]["status"] == "failed":
                self._contract_alerts(
                    ctx, [f"{case.engine_id} - {results[-1]['reason']}"], "live", engine_id=case.engine_id
                )
        summary["live"] = {
            "status": "ran" if results else "skipped",
            "budget_usd": budget,
            "spent_usd": round(spent, 6),
            "cases": results,
            "over_budget": over_budget,
        }
        return bool(results)

    def _job_reconcile(self, ctx: JobContext) -> JobOutcome:
        billing = self.config.billing_dir
        if billing is None or not billing.is_dir():
            return JobOutcome("skipped", f"no billing exports folder at {billing}")
        try:
            rows = load_billing(billing)
        except FileNotFoundError:
            return JobOutcome("skipped", f"no openai.csv, google.csv or atlas.csv in {billing}")
        day = (ctx.now.astimezone(UTC) - timedelta(days=1)).date()
        projects, problems = self._projects()
        ledgers = {p.project_id: self._ledger_factory(p) for p in projects if p.runs_jsonl.is_file()}
        records: list[dict[str, Any]] = []
        owner: dict[str, str] = {}
        for pid, ledger in ledgers.items():
            for rec in ledger.records():
                records.append(rec)
                if isinstance(rec.get("run_id"), str):
                    owner.setdefault(rec["run_id"], pid)
        report = reconcile(records, rows, day)
        recorded, write_errors = 0, []
        if self.config.record_billed:
            for pid, ledger in ledgers.items():
                mine = [m for m in report.matched if owner.get(m["run_id"]) == pid]
                if not mine:
                    continue
                try:
                    recorded += record_billed(ledger, replace(report, matched=mine, unmatched_billing=[]))
                except Exception as exc:
                    write_errors.append(f"{pid}: {type(exc).__name__}: {exc}")
        if not report.ok:
            ctx.alerts.nightly(
                "reconcile_failed",
                f"reconciliation of {report.day} failed: " + "; ".join(report.failures[:3]),
                data={
                    "day": report.day,
                    "failures": report.failures,
                    "unledgered_spend_usd": report.unledgered_spend_usd,
                },
            )
        for line in write_errors:
            ctx.alerts.nightly(
                "reconcile_failed",
                f"could not write billed records: {line}",
                severity="warn",
                data={"day": report.day},
            )
        summary = {
            "day": report.day,
            "ok": report.ok,
            "projects": sorted(ledgers),
            "billing_rows": len(rows),
            "billed_records_written": recorded,
            "write_errors": write_errors,
            "problems": problems,
            "report": report.to_dict(),
        }
        return JobOutcome(summary=summary)

    def _canary_dates(self) -> list[tuple[date, Path]]:
        found = []
        if self.nightly_dir.is_dir():
            for p in self.nightly_dir.iterdir():
                m = _DAY_RE.match(p.name)
                if m:
                    found.append((date.fromisoformat(m.group(1)), p))
        return sorted(found)

    def _job_canaries(self, ctx: JobContext) -> JobOutcome:
        if self._canary_set is None:
            return JobOutcome("skipped", "no canary set registered (12 owned plates and 4 moves)")
        today = date.fromisoformat(ctx.day)
        earlier = [(d, p) for d, p in self._canary_dates() if d < today]
        if not ctx.force and earlier:
            last = earlier[-1][0]
            due = last + timedelta(days=self.config.canary_every_days)
            if today < due:
                return JobOutcome(
                    "skipped", f"weekly: last run {last.isoformat()}, next due {due.isoformat()}"
                )
        cs = self._canary_set()
        report = canaries.run(
            cs.plates, cs.moves, cs.adapters, cs.gates, **{"clock": self._clock, **dict(cs.run_kwargs)}
        )
        previous = None
        if earlier:
            try:
                previous = CanaryReport.from_dict(read_json(earlier[-1][1]))
            except (OSError, ValueError, KeyError, TypeError):
                previous = None
        suspended = canaries.compare(previous, report, alerts=ctx.alerts)
        name = f"canaries_{ctx.day}.json"
        write_json_atomic(self.nightly_dir / name, report.to_dict(), indent=1)
        summary = {
            "engines": {
                eid: {
                    "n": e.n,
                    "passes": e.passes,
                    "pass_rate": e.pass_rate,
                    "model_version": e.model_version,
                }
                for eid, e in sorted(report.engines.items())
            },
            "skipped": dict(report.skipped),
            "suspended": suspended,
            "compared_with": earlier[-1][0].isoformat() if previous is not None and earlier else None,
            "report_file": name,
        }
        return JobOutcome(summary=summary)

    def _job_backup(self, ctx: JobContext) -> JobOutcome:
        dest_root = self.config.backup_dir
        if dest_root is None:
            return JobOutcome("skipped", "no backup_dir configured")
        fn = self._backup_fn or _load_backup_runner()
        if fn is None:
            return JobOutcome("skipped", "plate_daemon.tools.backup is not installed yet")
        projects, problems = self._projects()
        done: list[dict[str, Any]] = []
        failed: list[str] = []
        for project in projects:
            dest = dest_root / project.project_id / ctx.day
            if dest.exists():
                dest = dest_root / project.project_id / stamp(ctx.now)
            try:
                result = fn(project.root, dest)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                failed.append(f"{project.project_id}: {message}")
                ctx.alerts.nightly(
                    "backup_failed",
                    f"backup of {project.project_id} failed: {message}",
                    data={"project_id": project.project_id},
                )
                continue
            done.append({"project_id": project.project_id, "dest": str(dest), "result": result})
        return JobOutcome(
            summary={"backed_up": done, "failed": failed, "projects": len(projects), "problems": problems}
        )
