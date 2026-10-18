"""The nightly scheduler (router plan T50): one run per local date, crash-resume, the six jobs on fixtures.

Nothing here sleeps or reaches a vendor. The clock and the timer are fakes, the price catalogues, billing CSVs
and canary adapters are fixtures, and the contract runner is a stub except in the tests that point the real
PytestContractRunner at a two-test suite written into tmp_path.
"""

from __future__ import annotations

import json
import shutil
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from types import SimpleNamespace

import pytest

from plate_daemon.config import Settings
from plate_daemon.fsutil import read_json, write_json_atomic
from plate_daemon.index import Index, apply_packet_entry, apply_run
from plate_daemon.ledger import Ledger
from plate_daemon.projects import Project
from plate_daemon.scheduler import (
    JOB_ORDER,
    CanarySet,
    ContractOutcome,
    ContractUnavailable,
    JobOutcome,
    LiveContractCase,
    Nightly,
    NightlyConfig,
    NightlyConfigError,
    PytestContractRunner,
    SchemaPair,
    parse_clock_time,
    parse_pytest_output,
    read_alerts,
)
from plate_engines.adapters.base import JobStatus
from plate_engines.canaries import CanaryCase
from plate_engines.health import AlertLog
from plate_engines.loader import load_manifests

BILLING = Path(__file__).resolve().parent / "fixtures" / "billing"
KLING = "kwaivgi/kling-video-o3-4k/image-to-video"


# ---------------------------------------------------------------------------- fakes


class Clock:
    """A settable aware clock. The window is computed in the clock's own zone (UTC here)."""

    def __init__(self, year=2026, month=10, day=17, hour=0, minute=0, second=0):
        self.now = datetime(year, month, day, hour, minute, second, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def at(self, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> Clock:
        self.now = self.now.replace(day=day, hour=hour, minute=minute, second=second)
        return self


class FakeTimer:
    def __init__(self, interval, fn):
        self.interval, self.fn = interval, fn
        self.started = self.cancelled = False
        self.daemon = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


class Timers:
    def __init__(self):
        self.made: list[FakeTimer] = []

    def __call__(self, interval, fn):
        t = FakeTimer(interval, fn)
        self.made.append(t)
        return t

    @property
    def last(self) -> FakeTimer:
        return self.made[-1]


def counting_jobs(calls: list, *, fail: tuple[str, ...] = ()):
    def make(name):
        def job(ctx):
            calls.append((name, ctx.day))
            if name in fail:
                raise RuntimeError(f"{name} blew up")
            return JobOutcome(summary={"job": name})

        return job

    return {name: make(name) for name in JOB_ORDER}


def alert_kinds(nightly: Nightly) -> list[str]:
    return [a["kind"] for a in read_alerts(nightly.alerts_path)]


def make_cfg(settings: Settings, **over) -> NightlyConfig:
    return replace(NightlyConfig.load(settings, {}), **over)


def write_nightly_config(settings: Settings, block: dict) -> None:
    cfg = read_json(settings.config_path)
    cfg["nightly"] = block
    write_json_atomic(settings.config_path, cfg)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def timers() -> Timers:
    return Timers()


@pytest.fixture
def calls() -> list:
    return []


@pytest.fixture
def nightly(settings, clock, timers, calls) -> Nightly:
    return Nightly(settings, clock=clock, timer_factory=timers, env={}, jobs=counting_jobs(calls))


def make_project(settings: Settings, project_id: str = "alpha", **pins) -> Project:
    project = Project.create(settings.projects_dir / project_id, project_id, {"nda": False})
    project.doc["engine_pins"] = dict(pins)
    project.save()
    return project


# ---------------------------------------------------------------------------- configuration


def test_defaults(settings):
    cfg = NightlyConfig.load(settings, {})
    assert cfg.enabled and cfg.time == dtime(2, 0) and cfg.time_text == "02:00"
    assert cfg.catch_up and cfg.poll_s == 900.0 and cfg.canary_every_days == 7
    assert cfg.catalogue_dir == settings.home / "catalogues"
    assert cfg.billing_dir == settings.home / "billing"
    assert cfg.backup_dir is None and cfg.live_budget_usd == 0.0 and cfg.record_billed


def test_config_json_block_and_env_override(settings):
    write_nightly_config(
        settings,
        {
            "time": "03:15:30",
            "catch_up": False,
            "window_h": 2,
            "backup_dir": "bk",
            "billing_dir": None,
            "live_budget_usd": 1.5,
            "canary_every_days": 3,
            "disabled_jobs": ["backup"],
        },
    )
    cfg = NightlyConfig.load(settings, {})
    assert cfg.time == dtime(3, 15, 30) and cfg.time_text == "03:15:30"
    assert not cfg.catch_up and cfg.window_h == 2.0
    assert cfg.backup_dir == settings.home / "bk" and cfg.billing_dir is None
    assert cfg.live_budget_usd == 1.5 and cfg.canary_every_days == 3 and cfg.disabled_jobs == {"backup"}
    assert NightlyConfig.load(settings, {"PLATE_NIGHTLY_TIME": "04:00"}).time == dtime(4, 0)
    assert not NightlyConfig.load(settings, {"PLATE_NIGHTLY": "0"}).enabled


@pytest.mark.parametrize(
    "block",
    [
        {"time": "25:00"},
        {"time": "2am"},
        {"tiem": "02:00"},
        {"disabled_jobs": ["laundry"]},
        {"catch_up": "yes"},
        {"poll_s": -1},
        {"catalogue_dir": 7},
    ],
)
def test_bad_config_is_refused(settings, block):
    write_nightly_config(settings, block)
    with pytest.raises(NightlyConfigError):
        NightlyConfig.load(settings, {})


def test_parse_clock_time():
    assert parse_clock_time("2:05") == dtime(2, 5)
    assert parse_clock_time("23:59:59") == dtime(23, 59, 59)
    with pytest.raises(NightlyConfigError):
        parse_clock_time("24:00")


def test_unknown_job_override_is_refused(settings):
    with pytest.raises(ValueError, match="unknown nightly job"):
        Nightly(settings, env={}, jobs={"laundry": lambda ctx: None})


# ---------------------------------------------------------------------------- once per date


def test_nothing_runs_before_the_window(nightly, clock, calls):
    clock.at(17, 1, 59, 59)
    assert nightly.tick() is None
    assert calls == [] and not nightly.nightly_dir.exists()


def test_window_runs_every_job_once_in_order_and_writes_the_record(nightly, clock, calls):
    clock.at(17, 2, 0, 1)
    rec = nightly.tick()
    assert [name for name, _ in calls] == list(JOB_ORDER)
    assert rec["done"] is True and rec["date"] == "2026-10-17" and rec["window"] == "02:00"
    path = nightly.record_path("2026-10-17")
    assert path.name == "nightly_2026-10-17.json" and ":" not in path.name
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == rec and list(on_disk["jobs"]) == list(JOB_ORDER)
    assert all(j["status"] == "ok" and j["summary"]["job"] == n for n, j in on_disk["jobs"].items())
    assert path.read_bytes().endswith(b"\n") and b"\r\n" not in path.read_bytes()


def test_a_done_date_is_never_run_again_but_the_next_date_is(nightly, clock, calls):
    for day in (17, 18):
        for hour, minute in ((2, 0), (2, 30), (9, 0), (14, 45), (23, 59)):
            clock.at(day, hour, minute)
            nightly.tick()
    assert sorted(calls) == sorted((name, f"2026-10-{d}") for name in JOB_ORDER for d in (17, 18))
    assert nightly.read_record("2026-10-18")["done"] is True


def test_a_partial_record_resumes_with_the_missing_jobs(nightly, clock, calls):
    nightly.nightly_dir.mkdir(parents=True)
    partial = nightly._new_record("2026-10-17", clock())
    partial["jobs"] = {
        "prices_refresh": {"status": "ok", "summary": {"from": "before the crash"}},
        "retire_check": {"status": "ok", "summary": {}},
    }
    nightly._write_record("2026-10-17", partial)
    clock.at(17, 3, 0)
    rec = nightly.tick()
    assert [n for n, _ in calls] == list(JOB_ORDER[2:])
    assert rec["jobs"]["prices_refresh"]["summary"] == {"from": "before the crash"} and rec["done"]


def test_a_corrupt_record_is_treated_as_missing(nightly, clock, calls):
    nightly.nightly_dir.mkdir(parents=True)
    nightly.record_path("2026-10-17").write_text("{not json", encoding="utf-8")
    clock.at(17, 2, 0)
    assert nightly.tick()["done"] and len(calls) == len(JOB_ORDER)


def test_a_raising_job_is_recorded_alerted_and_does_not_stop_the_rest(settings, clock, timers, calls):
    n = Nightly(
        settings, clock=clock, timer_factory=timers, env={}, jobs=counting_jobs(calls, fail=("reconcile",))
    )
    clock.at(17, 2, 0)
    rec = n.tick()
    assert [name for name, _ in calls] == list(JOB_ORDER)  # canaries and backup still ran
    job = rec["jobs"]["reconcile"]
    assert job["status"] == "error" and "RuntimeError: reconcile blew up" in job["message"]
    assert [a["kind"] for a in job["alerts"]] == ["job_error"] and job["alerts"][0]["severity"] == "critical"
    assert rec["jobs"]["canaries"]["status"] == "ok" and rec["done"]
    assert alert_kinds(n) == ["job_error"]
    n.tick()  # an errored job is not retried the same date
    assert len(calls) == len(JOB_ORDER)


def test_run_now_runs_a_subset_and_force_reruns(nightly, clock, calls):
    clock.at(17, 12, 0)  # outside the window logic: run_now ignores it
    rec = nightly.run_now(["backup", "prices_refresh"])
    assert [n for n, _ in calls] == ["prices_refresh", "backup"] and rec["done"] is False
    nightly.run_now(["backup"])
    assert len(calls) == 2  # already has a result today
    nightly.run_now(["backup"], force=True)
    assert [n for n, _ in calls][-1] == "backup" and len(calls) == 3
    with pytest.raises(ValueError, match="unknown nightly jobs"):
        nightly.run_now(["laundry"])
    nightly.run_now()  # the rest
    assert nightly.read_record("2026-10-17")["done"] is True
    assert nightly.tick() is None  # and the timer would not run it again


def test_catch_up_off_skips_a_date_the_window_has_long_passed(settings, clock, timers, calls):
    cfg = make_cfg(settings, catch_up=False, window_h=4.0)
    n = Nightly(settings, config=cfg, clock=clock, timer_factory=timers, env={}, jobs=counting_jobs(calls))
    clock.at(17, 9, 0)
    assert n.tick() is None and calls == []
    clock.at(17, 5, 30)
    assert n.tick() is not None and len(calls) == len(JOB_ORDER)


def test_a_disabled_job_is_skipped_with_its_reason(settings, clock, timers, calls):
    cfg = make_cfg(settings, disabled_jobs=frozenset({"backup"}))
    n = Nightly(settings, config=cfg, clock=clock, timer_factory=timers, env={}, jobs=counting_jobs(calls))
    clock.at(17, 2, 0)
    rec = n.tick()
    assert "backup" not in [name for name, _ in calls]
    assert rec["jobs"]["backup"]["status"] == "skipped" and "disabled" in rec["jobs"]["backup"]["message"]
    assert rec["done"]


def test_status_reports_today_and_the_next_run(nightly, clock):
    clock.at(17, 1, 0)
    s = nightly.status()
    assert s["today"] is None and s["next_run"].startswith("2026-10-17T02:00:00") and not s["started"]
    clock.at(17, 2, 0)
    nightly.tick()
    s = nightly.status()
    assert s["today"]["done"] and set(s["today"]["jobs"]) == set(JOB_ORDER)
    assert s["next_run"].startswith("2026-10-18T02:00:00")


# ---------------------------------------------------------------------------- the timer


def test_start_arms_one_timer_capped_by_poll(nightly, clock, timers):
    clock.at(17, 0, 0)
    assert nightly.start() is True and nightly.start() is False
    assert len(timers.made) == 1 and timers.last.started and timers.last.daemon
    assert timers.last.interval == 900.0  # 2 h away, capped to poll_s


def test_the_delay_is_the_time_to_the_window_when_it_is_closer_than_poll(settings, clock, timers, calls):
    cfg = make_cfg(settings, poll_s=1e9)
    n = Nightly(settings, config=cfg, clock=clock, timer_factory=timers, env={}, jobs=counting_jobs(calls))
    clock.at(17, 0, 0)
    n.start()
    assert timers.last.interval == pytest.approx(2 * 3600)


def test_firing_before_the_window_only_rearms_and_firing_in_it_runs_the_jobs(nightly, clock, timers, calls):
    clock.at(17, 1, 0)
    nightly.start()
    timers.last.fn()
    assert calls == [] and len(timers.made) == 2
    clock.at(17, 2, 0, 5)
    timers.last.fn()
    assert len(calls) == len(JOB_ORDER) and len(timers.made) == 3
    assert timers.last.interval == 900.0  # tomorrow's window is far away: poll again
    timers.last.fn()
    assert len(calls) == len(JOB_ORDER)  # the date is done


def test_a_late_start_catches_up_after_the_startup_delay(settings, clock, timers, calls):
    n = Nightly(settings, clock=clock, timer_factory=timers, env={}, jobs=counting_jobs(calls))
    clock.at(17, 10, 0)
    n.start()
    assert timers.last.interval == 30.0
    timers.last.fn()
    assert len(calls) == len(JOB_ORDER)


def test_a_late_start_without_catch_up_waits_for_tomorrow(settings, clock, timers, calls):
    cfg = make_cfg(settings, catch_up=False, poll_s=1e9)
    n = Nightly(settings, config=cfg, clock=clock, timer_factory=timers, env={}, jobs=counting_jobs(calls))
    clock.at(17, 10, 0)
    n.start()
    assert timers.last.interval == pytest.approx(16 * 3600)
    timers.last.fn()
    assert calls == []


def test_stop_cancels_the_timer_and_a_late_fire_does_not_rearm(nightly, clock, timers):
    clock.at(17, 1, 0)
    nightly.start()
    armed = timers.last
    nightly.stop()
    assert armed.cancelled
    armed.fn()
    assert len(timers.made) == 1
    assert nightly.start() is True  # can be started again
    assert len(timers.made) == 2


def test_disabled_scheduler_does_not_start(settings, clock, timers, calls):
    cfg = make_cfg(settings, enabled=False)
    n = Nightly(settings, config=cfg, clock=clock, timer_factory=timers, env={}, jobs=counting_jobs(calls))
    assert n.start() is False and timers.made == []


def test_a_timer_thread_survives_a_failure_and_says_so(nightly, clock, timers, monkeypatch):
    clock.at(17, 2, 0)
    nightly.start()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(nightly, "_write_record", boom)
    timers.last.fn()
    assert len(timers.made) == 2  # re-armed
    assert nightly.last_error == "OSError: disk full"
    assert alert_kinds(nightly) == ["job_error"]


def test_a_real_timer_thread_runs_the_jobs(settings):
    now = datetime.now().astimezone()
    if now.hour == 0 and now.minute == 0:
        pytest.skip("too close to local midnight for a window set in the past")
    start = (now - timedelta(seconds=2)).time().replace(microsecond=0)
    done = threading.Event()
    seen: list[str] = []

    def job(name):
        def run(ctx):
            seen.append(name)
            if name == JOB_ORDER[-1]:
                done.set()

        return run

    cfg = make_cfg(settings, time=start, startup_delay_s=0.0, poll_s=0.2)
    n = Nightly(settings, config=cfg, env={}, jobs={name: job(name) for name in JOB_ORDER})
    try:
        assert n.start()
        assert done.wait(15), seen
    finally:
        n.stop(wait_s=5)
    assert seen == list(JOB_ORDER)


# ---------------------------------------------------------------------------- unconfigured defaults


def test_with_nothing_configured_every_job_skips_cleanly(settings, clock, timers):
    runner = lambda: ContractOutcome(True, passed=10)  # noqa: E731 - not the engines suites
    n = Nightly(settings, clock=clock, timer_factory=timers, env={}, contract_runner=runner)
    clock.at(17, 2, 0)
    rec = n.tick()
    statuses = {name: j["status"] for name, j in rec["jobs"].items()}
    assert statuses == {
        "prices_refresh": "skipped",
        "retire_check": "skipped",
        "contract_tests": "ok",
        "reconcile": "skipped",
        "canaries": "skipped",
        "backup": "skipped",
    }
    assert rec["done"] and read_alerts(n.alerts_path) == []
    assert "catalogue" in rec["jobs"]["prices_refresh"]["message"]


# ---------------------------------------------------------------------------- prices, requote, retirement


def write_catalogue(directory: Path, vendor: str, models: list[dict], as_of: str = "2026-10-18") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    doc = {"vendor": vendor, "as_of": as_of, "currency": "USD", "models": models}
    (directory / f"{vendor}.json").write_text(json.dumps(doc), encoding="utf-8", newline="\n")


def quoted_run_in_index(settings: Settings, project_id: str, run_id: str, engine_id: str, label=None) -> None:
    index = Index(settings.home)
    conn = index.connect(project_id, create=True)  # the helper makes the project real (T144)
    try:
        apply_packet_entry(
            conn,
            project_id,
            "pk1",
            {
                "shot_id": "s1",
                "kind": "still",
                "tier": "A",
                "packet_version": "0.1",
                "path": "packets/pk1",
                "manifest_blake3": "0" * 64,
            },
        )
        apply_run(
            conn,
            project_id,
            {
                "run_id": run_id,
                "state": "QUOTED",
                "packet_id": "pk1",
                "route": {"engine_id": engine_id, "provider": "atlas", "region": "us"},
                "quote_json": {"model": engine_id, "size": {"label": label} if label else None},
                "created_utc": "2026-10-17T00:00:00.000000Z",
                "updated_utc": "2026-10-17T00:00:00.000000Z",
            },
        )
    finally:
        conn.close()


@pytest.fixture
def manifests():
    return load_manifests()


def priced_nightly(settings, clock, timers, manifests, tmp_path, **kw) -> Nightly:
    cfg = make_cfg(settings, prices_overlay=tmp_path / "out" / "_prices.yaml")
    return Nightly(settings, config=cfg, clock=clock, timer_factory=timers, env={}, manifests=manifests, **kw)


def test_a_6_percent_move_flags_the_quoted_runs_on_that_engine(settings, clock, timers, manifests, tmp_path):
    old = manifests["kling-o3-4k"].price.usd
    write_catalogue(settings.home / "catalogues", "atlas", [
        {"id": KLING, "unit": "per_second", "usd": round(old * 1.06, 6), "tiers": {"atlas_discounted": 0.0536}}
    ])  # fmt: skip
    quoted_run_in_index(settings, "alpha", "rq1", "kling-o3-4k")
    quoted_run_in_index_second = Index(settings.home).connect("alpha")
    apply_run(
        quoted_run_in_index_second,
        "alpha",
        {
            "run_id": "rq2",
            "state": "QUOTED",
            "packet_id": "pk1",
            "route": {"engine_id": "kling-v3-pro", "provider": "atlas", "region": "us"},
            "quote_json": {"model": "kling-v3-pro"},
            "created_utc": "2026-10-17T00:00:00.000000Z",
            "updated_utc": "2026-10-17T00:00:00.000000Z",
        },
    )
    quoted_run_in_index_second.close()
    n = priced_nightly(settings, clock, timers, manifests, tmp_path)
    clock.at(17, 2, 0)
    job = n.run_now(["prices_refresh"])["jobs"]["prices_refresh"]
    assert job["status"] == "alerted"
    assert job["summary"]["requote_runs"] == ["rq1"] and job["summary"]["requotes"] == 1
    assert job["summary"]["changes"][0]["engine_id"] == "kling-o3-4k"
    assert job["summary"]["changes"][0]["pct"] == pytest.approx(6.0, abs=1e-3)
    assert [a["kind"] for a in job["alerts"]] == ["price_change", "requote"]
    assert alert_kinds(n) == ["price_change", "requote"]
    assert (tmp_path / "out" / "_prices.yaml").is_file()
    # the same catalogue the next night: the overlay now holds the price, so nothing moves again
    clock.at(18, 2, 0)
    again = n.run_now(["prices_refresh"])["jobs"]["prices_refresh"]
    assert (
        again["status"] == "ok"
        and again["summary"]["changes"] == []
        and again["summary"]["requote_runs"] == []
    )


def test_a_4_percent_move_is_recorded_but_flags_nothing(settings, clock, timers, manifests, tmp_path):
    old = manifests["kling-o3-4k"].price.usd
    write_catalogue(settings.home / "catalogues", "atlas", [
        {"id": KLING, "unit": "per_second", "usd": round(old * 1.04, 6)}
    ])  # fmt: skip
    calls = []
    n = priced_nightly(
        settings, clock, timers, manifests, tmp_path, quoted_runs=lambda: calls.append("read") or []
    )
    job = n.run_now(["prices_refresh"])["jobs"]["prices_refresh"]
    assert job["status"] == "ok" and job["summary"]["requotes"] == 0 and len(job["summary"]["changes"]) == 1
    assert calls == []  # no requote, so the indexes are not even read


def test_requote_uses_the_injected_quoted_run_source(settings, clock, timers, manifests, tmp_path):
    old = manifests["kling-o3-4k"].price.usd
    write_catalogue(settings.home / "catalogues", "atlas", [
        {"id": KLING, "unit": "per_second", "usd": round(old * 0.9, 6)}
    ])  # fmt: skip
    quoted = [
        {"run_id": "a", "engine_id": "kling-o3-4k"},
        {"run_id": "b", "engine_id": "kling-o3-4k", "tier": "atlas_discounted"},
    ]
    n = priced_nightly(settings, clock, timers, manifests, tmp_path, quoted_runs=lambda: quoted)
    job = n.run_now(["prices_refresh"])["jobs"]["prices_refresh"]
    assert job["summary"]["requote_runs"] == ["a"]  # a fall over 5% invalidates the quote too


def test_retire_check_alerts_on_a_pinned_model_the_catalogue_dropped(
    settings, clock, timers, manifests, tmp_path
):
    make_project(settings, "alpha", **{"kling-o3-4k": manifests["kling-o3-4k"].version})
    write_catalogue(settings.home / "catalogues", "atlas", [
        {"id": "some/other-model", "unit": "per_second", "usd": 0.1}
    ])  # fmt: skip
    n = priced_nightly(settings, clock, timers, manifests, tmp_path)
    job = n.run_now(["retire_check"])["jobs"]["retire_check"]
    assert job["status"] == "alerted" and job["summary"]["project_pins"] == 1
    assert [a["kind"] for a in job["alerts"]] == ["retired_pin"]
    assert (
        job["alerts"][0]["engine_id"] == "kling-o3-4k" and "alpha:kling-o3-4k" in job["alerts"][0]["message"]
    )
    # the model comes back in the catalogue: quiet
    write_catalogue(
        settings.home / "catalogues", "atlas", [{"id": KLING, "unit": "per_second", "usd": 0.357}]
    )
    assert n.run_now(["retire_check"], force=True)["jobs"]["retire_check"]["status"] == "ok"


# ---------------------------------------------------------------------------- contract tests


def contract_nightly(settings, clock, timers, runner=None, **kw) -> Nightly:
    return Nightly(settings, clock=clock, timer_factory=timers, contract_runner=runner, **{"env": {}, **kw})


def test_a_failing_contract_test_produces_an_alert_line(settings, clock, timers):
    outcome = ContractOutcome(
        False, passed=40, failed=1, failures=["engines/tests/test_adapters_image.py::test_x - boom"]
    )
    n = contract_nightly(settings, clock, timers, lambda: outcome)
    job = n.run_now(["contract_tests"])["jobs"]["contract_tests"]
    assert job["status"] == "alerted" and job["summary"]["recorded"]["status"] == "failed"
    lines = [a for a in read_alerts(n.alerts_path) if a["kind"] == "contract_test_failed"]
    assert len(lines) == 1 and lines[0]["severity"] == "critical"
    assert (
        "test_adapters_image.py::test_x" in lines[0]["message"] and lines[0]["data"]["source"] == "recorded"
    )
    # the engines AlertLog only knows its own kinds and skips the line instead of crashing
    assert AlertLog(n.alerts_path).read() == []


def test_a_passing_run_raises_nothing(settings, clock, timers):
    n = contract_nightly(settings, clock, timers, lambda: ContractOutcome(True, passed=90))
    job = n.run_now(["contract_tests"])["jobs"]["contract_tests"]
    assert job["status"] == "ok" and job["summary"]["recorded"]["passed"] == 90
    assert read_alerts(n.alerts_path) == []


def test_a_failure_without_detail_still_alerts(settings, clock, timers):
    n = contract_nightly(settings, clock, timers, lambda: ContractOutcome(False))
    assert n.run_now(["contract_tests"])["jobs"]["contract_tests"]["status"] == "alerted"


def test_failure_alerts_are_capped_with_a_count_of_the_rest(settings, clock, timers):
    failures = [f"t.py::test_{i} - no" for i in range(30)]
    n = contract_nightly(
        settings, clock, timers, lambda: ContractOutcome(False, failed=30, failures=failures)
    )
    n.run_now(["contract_tests"])
    lines = read_alerts(n.alerts_path)
    assert len(lines) == 26 and lines[-1]["data"]["more"] == 5


def test_unavailable_suites_skip_the_job(settings, clock, timers):
    def runner():
        raise ContractUnavailable("no checkout")

    job = contract_nightly(settings, clock, timers, runner).run_now(["contract_tests"])["jobs"][
        "contract_tests"
    ]
    assert job["status"] == "skipped" and job["message"] == "no checkout"


def test_the_default_runner_runs_a_pytest_suite_and_reports_failures(tmp_path):
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_fake_contract.py").write_text(
        "def test_good():\n    assert True\n\n\ndef test_bad():\n    assert 1 == 2\n",
        encoding="utf-8",
        newline="\n",
    )
    outcome = PytestContractRunner(suite, paths=("test_fake_contract.py",), timeout_s=120)()
    assert outcome.ok is False and outcome.passed == 1 and outcome.failed == 1
    assert len(outcome.failures) == 1 and "test_fake_contract.py::test_bad" in outcome.failures[0]
    (suite / "test_fake_contract.py").write_text(
        "def test_good():\n    assert True\n", encoding="utf-8", newline="\n"
    )
    good = PytestContractRunner(suite, paths=("test_fake_contract.py",), timeout_s=120)()
    assert good.ok and good.passed == 1 and good.failures == []


def test_the_default_runner_refuses_when_there_are_no_suites(tmp_path):
    with pytest.raises(ContractUnavailable):
        PytestContractRunner(tmp_path, paths=("engines/tests/test_nothing.py",))()


def test_parse_pytest_output_without_a_failed_line_still_fails():
    out = parse_pytest_output(
        2, "ImportError while importing test module\n!!! Interrupted: 1 error during collection !!!\n"
    )
    assert not out.ok and out.failures and "pytest exited 2" in out.failures[0]
    assert parse_pytest_output(0, "5 passed in 0.1s").passed == 5


def live_case(engine, cost, same_output=False):
    def run():
        a, b = (b"x", b"x") if same_output else (f"{engine}-a".encode(), f"{engine}-b".encode())
        return [("input-1", a), ("input-2", b)]

    return LiveContractCase(engine, cost, run)


def test_live_contracts_need_plate_live_and_a_budget(settings, clock, timers):
    cases = lambda: [live_case("kling-o3-4k", 0.5)]  # noqa: E731
    ok = ContractOutcome(True)
    off = contract_nightly(settings, clock, timers, lambda: ok, env={}, live_cases=cases)
    assert (
        off.run_now(["contract_tests"])["jobs"]["contract_tests"]["summary"]["live"]["reason"]
        == "PLATE_LIVE is not 1"
    )
    no_budget = contract_nightly(
        settings, clock, timers, lambda: ok, env={"PLATE_LIVE": "1"}, live_cases=cases
    )
    # same date, same record: force the second instance to run its own pass
    live = no_budget.run_now(["contract_tests"], force=True)["jobs"]["contract_tests"]["summary"]["live"]
    assert live["reason"] == "live_budget_usd is 0"


def test_live_contracts_run_cheapest_first_inside_the_budget_and_catch_an_ignored_input(
    settings, clock, timers
):
    cases = lambda: [  # noqa: E731
        live_case("kling-o3-4k", 0.5),
        live_case("nano-banana-2.1", 0.3, same_output=True),
        live_case("gpt-image-2.5", 0.4),
    ]
    cfg = make_cfg(settings, live_budget_usd=1.0)
    n = Nightly(
        settings,
        config=cfg,
        clock=clock,
        timer_factory=timers,
        env={"PLATE_LIVE": "1"},
        contract_runner=lambda: ContractOutcome(True),
        live_cases=cases,
    )
    job = n.run_now(["contract_tests"])["jobs"]["contract_tests"]
    live = job["summary"]["live"]
    assert [c["engine_id"] for c in live["cases"]] == ["nano-banana-2.1", "gpt-image-2.5"]
    assert live["spent_usd"] == pytest.approx(0.7) and live["over_budget"] == ["kling-o3-4k"]
    assert [c["status"] for c in live["cases"]] == ["failed", "ok"]
    assert job["status"] == "alerted"
    lines = [a for a in read_alerts(n.alerts_path) if a["kind"] == "contract_test_failed"]
    assert (
        len(lines) == 1
        and lines[0]["engine_id"] == "nano-banana-2.1"
        and lines[0]["data"]["source"] == "live"
    )
    assert "ignored the input" in lines[0]["message"]


# ---------------------------------------------------------------------------- reconcile


def _t(hms: str, day: str = "2026-10-16") -> str:
    return f"{day}T{hms}Z"


def submit(led: Ledger, run_id, engine, quoted, hms, *, grade="C1", job=None, model=None, end="failed"):
    led.append(
        kind="quoted",
        run_id=run_id,
        engine_id=engine,
        custody_grade=grade,
        cost_usd_quoted=quoted,
        at=_t(hms),
    )
    led.append(kind="approved", run_id=run_id, cost_usd_quoted=quoted, by="human", at=_t(hms))
    led.append(
        kind="submitted",
        run_id=run_id,
        provider_job_id=job,
        data={"model": model} if model else None,
        at=_t(hms),
    )
    led.append(kind=end, run_id=run_id, at=_t(hms))


def ledgers_for(settings: Settings) -> tuple[Ledger, Ledger]:
    """alpha holds four of the day's runs, beta two; together they are the whole reconcile fixture day."""
    alpha, beta = make_project(settings, "alpha"), make_project(settings, "beta")
    a, b = Ledger(alpha.root), Ledger(beta.root)
    submit(b, "o1", "gpt-image-2.5", 0.19, "09:12:25", job="resp_7f3a91", model="gpt-image-2.5", end="done")
    submit(b, "o2", "gpt-image-2.5", 0.19, "09:40:10", model="gpt-image-2.5", end="done")
    submit(a, "g1", "nano-banana-2.1", 0.067, "10:02:05", model="gemini-3.1-flash-image", end="done")
    submit(a, "g2", "nano-banana-pro", 0.134, "10:15:40", model="gemini-3-pro-image", end="done")
    submit(a, "a1", "seedance-2.5-i2v", 1.20, "11:00:00", grade="C2", job="pred_a1b2c3", end="done")
    submit(a, "a2", "kling-o3-4k", 2.50, "11:20:30", grade="C2", job="pred_d4e5f6", end="done")
    return a, b


def billed(led: Ledger) -> list[str]:
    return sorted(r["run_id"] for r in led.records() if r["kind"] == "billed")


def reconcile_nightly(settings, clock, timers, fixture: str) -> Nightly:
    shutil.copytree(BILLING / fixture, settings.home / "billing", dirs_exist_ok=True)
    clock.at(17, 2, 0)  # UTC: yesterday is 2026-10-16, the fixture day
    return Nightly(settings, clock=clock, timer_factory=timers, env={})


def test_reconcile_a_clean_day_across_two_projects_records_each_bill_in_its_own_ledger(
    settings, clock, timers
):
    a, b = ledgers_for(settings)
    n = reconcile_nightly(settings, clock, timers, "clean")
    job = n.run_now(["reconcile"])["jobs"]["reconcile"]
    assert job["status"] == "ok" and job["summary"]["ok"] and job["summary"]["day"] == "2026-10-16"
    assert job["summary"]["projects"] == ["alpha", "beta"] and job["summary"]["billed_records_written"] == 6
    assert billed(a) == ["a1", "a2", "g1", "g2"] and billed(b) == ["o1", "o2"]
    assert a.verify() == (True, None) and b.verify() == (True, None)
    # a second run (forced) writes nothing more: record_billed is idempotent per day
    assert n.run_now(["reconcile"], force=True)["jobs"]["reconcile"]["summary"]["billed_records_written"] == 0
    assert billed(a) == ["a1", "a2", "g1", "g2"]


def test_an_unmatched_billing_row_fails_the_day_with_an_alert(settings, clock, timers):
    a, b = ledgers_for(settings)
    n = reconcile_nightly(settings, clock, timers, "unmatched")
    job = n.run_now(["reconcile"])["jobs"]["reconcile"]
    assert job["status"] == "alerted" and job["summary"]["ok"] is False
    assert job["summary"]["report"]["unledgered_spend_usd"] == 0.84
    lines = [x for x in read_alerts(n.alerts_path) if x["kind"] == "reconcile_failed"]
    assert len(lines) == 1 and "unledgered spend USD 0.8400" in lines[0]["message"]
    assert lines[0]["data"]["unledgered_spend_usd"] == 0.84
    assert billed(a) == ["a1", "a2", "g1", "g2"]  # the matched bills are still recorded; the stray row is not


def test_record_billed_can_be_turned_off(settings, clock, timers):
    a, _ = ledgers_for(settings)
    shutil.copytree(BILLING / "clean", settings.home / "billing", dirs_exist_ok=True)
    clock.at(17, 2, 0)
    n = Nightly(
        settings, config=make_cfg(settings, record_billed=False), clock=clock, timer_factory=timers, env={}
    )
    job = n.run_now(["reconcile"])["jobs"]["reconcile"]
    assert job["status"] == "ok" and job["summary"]["billed_records_written"] == 0 and billed(a) == []


def test_a_ledger_that_cannot_be_written_is_reported_not_fatal(settings, clock, timers):
    ledgers_for(settings)
    shutil.copytree(BILLING / "clean", settings.home / "billing", dirs_exist_ok=True)
    clock.at(17, 2, 0)

    def locked(project):
        led = Ledger(project.root)
        if project.project_id == "beta":
            led.append = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("project.lock is held"))
        return led

    n = Nightly(settings, clock=clock, timer_factory=timers, env={}, ledger_factory=locked)
    job = n.run_now(["reconcile"])["jobs"]["reconcile"]
    assert job["status"] == "alerted" and len(job["summary"]["write_errors"]) == 1
    assert "beta" in job["summary"]["write_errors"][0] and job["summary"]["billed_records_written"] == 4
    assert [a["severity"] for a in job["alerts"]] == ["warn"]


def test_reconcile_skips_without_exports_and_errors_on_a_malformed_one(settings, clock, timers):
    n = Nightly(settings, clock=clock, timer_factory=timers, env={})
    assert n.run_now(["reconcile"])["jobs"]["reconcile"]["status"] == "skipped"  # no billing folder
    (settings.home / "billing").mkdir()
    assert n.run_now(["reconcile"], force=True)["jobs"]["reconcile"]["message"].startswith("no openai.csv")
    (settings.home / "billing" / "atlas.csv").write_text("when,what\nx,y\n", encoding="utf-8", newline="\n")
    job = n.run_now(["reconcile"], force=True)["jobs"]["reconcile"]
    assert job["status"] == "error" and "BillingFormatError" in job["message"]
    assert alert_kinds(n)[-1] == "job_error"


# ---------------------------------------------------------------------------- canaries


class FakeAdapter:
    def __init__(self, engine_id="nano-banana-2.1", kind="image_edit", version="model-1"):
        self.engine_id, self.route_id = engine_id, engine_id.split("-")[0]
        self.manifest = SimpleNamespace(kind=kind)
        self.model_version = version
        self.jobs: dict[str, str] = {}

    def validate_params(self, params):
        return dict(params)

    def submit(self, staged):
        job = f"job-{len(self.jobs) + 1}"
        self.jobs[job] = staged.inputs["image"]
        return job

    def poll(self, job):
        return JobStatus(
            state="done",
            outputs=[f"https://fixture.invalid/{job}"],
            usage={"model_version": self.model_version},
        )

    def fetch(self, url):
        return f"{self.engine_id}|{self.jobs[url.rsplit('/', 1)[-1]]}".encode()


def canary_nightly(settings, clock, timers, state):
    plates = [
        CanaryCase(f"plate-{i:02d}", "plate", {"image": f"p{i}.png", "prompt": "relight"}) for i in range(12)
    ]
    moves = [CanaryCase(f"move-{i:02d}", "move", {"image": f"m{i}.png"}) for i in range(4)]

    def gates(case, data, engine_id):
        return "fail" if case.case_id in state["bad"] else "pass"

    def canary_set():
        return CanarySet(plates, moves, {"nano-banana-2.1": FakeAdapter(version=state["version"])}, gates)

    return Nightly(settings, clock=clock, timer_factory=timers, env={}, canary_set=canary_set)


def test_canaries_run_weekly_and_a_drop_suspends_the_engine(settings, clock, timers):
    state = {"bad": set(), "version": "model-1"}
    n = canary_nightly(settings, clock, timers, state)
    first = n.run_now(["canaries"], now=clock.at(17, 2, 0)())["jobs"]["canaries"]
    assert first["status"] == "ok" and first["summary"]["engines"]["nano-banana-2.1"]["pass_rate"] == 1.0
    assert first["summary"]["compared_with"] is None  # the first run is a baseline, never drift
    assert (n.nightly_dir / "canaries_2026-10-17.json").is_file()
    # days 2 to 7: not due
    for day in range(18, 24):
        job = n.run_now(["canaries"], now=clock.at(day, 2, 0)())["jobs"]["canaries"]
        assert job["status"] == "skipped" and "next due 2026-10-24" in job["message"]
    assert not (n.nightly_dir / "canaries_2026-10-18.json").exists()
    # day 8: due; half the plates now fail the gates
    state["bad"] = {f"plate-{i:02d}" for i in range(6)}
    job = n.run_now(["canaries"], now=clock.at(24, 2, 0)())["jobs"]["canaries"]
    assert job["status"] == "alerted" and job["summary"]["suspended"] == ["nano-banana-2.1"]
    assert job["summary"]["compared_with"] == "2026-10-17"
    assert [a["kind"] for a in job["alerts"]] == ["canary_drift", "engine_suspended"]
    assert AlertLog(n.alerts_path).suspended_engines() == {"nano-banana-2.1"}


def test_a_changed_model_string_suspends_even_when_the_pass_rate_holds(settings, clock, timers):
    state = {"bad": set(), "version": "model-1"}
    n = canary_nightly(settings, clock, timers, state)
    n.run_now(["canaries"], now=clock.at(17, 2, 0)())
    state["version"] = "model-2"
    job = n.run_now(["canaries"], now=clock.at(24, 2, 0)())["jobs"]["canaries"]
    assert (
        job["summary"]["suspended"] == ["nano-banana-2.1"]
        and "model string changed" in job["alerts"][0]["message"]
    )


def test_force_runs_the_canaries_before_they_are_due(settings, clock, timers):
    n = canary_nightly(settings, clock, timers, {"bad": set(), "version": "model-1"})
    n.run_now(["canaries"], now=clock.at(17, 2, 0)())
    job = n.run_now(["canaries"], force=True, now=clock.at(18, 2, 0)())["jobs"]["canaries"]
    assert job["status"] == "ok" and job["summary"]["compared_with"] == "2026-10-17"


# ---------------------------------------------------------------------------- backup


def test_backup_runs_per_project_into_a_dated_folder(settings, clock, timers, tmp_path):
    make_project(settings, "alpha")
    make_project(settings, "beta")
    calls = []

    def fake_backup(project_dir, dest):
        calls.append((Path(project_dir).name, Path(dest)))
        return {"files": 3}

    dest_root = tmp_path / "nas"
    n = Nightly(
        settings,
        config=make_cfg(settings, backup_dir=dest_root),
        clock=clock,
        timer_factory=timers,
        env={},
        backup_fn=fake_backup,
    )
    job = n.run_now(["backup"])["jobs"]["backup"]
    assert job["status"] == "ok" and job["summary"]["projects"] == 2
    assert calls == [
        ("alpha", dest_root / "alpha" / "2026-10-17"),
        ("beta", dest_root / "beta" / "2026-10-17"),
    ]
    assert job["summary"]["backed_up"][0]["result"] == {"files": 3}
    assert all(":" not in str(d.name) for _, d in calls)


def test_a_failing_backup_alerts_and_the_other_projects_still_run(settings, clock, timers, tmp_path):
    make_project(settings, "alpha")
    make_project(settings, "beta")
    seen = []

    def flaky(project_dir, dest):
        seen.append(Path(project_dir).name)
        if Path(project_dir).name == "alpha":
            raise OSError("NAS offline")

    n = Nightly(
        settings,
        config=make_cfg(settings, backup_dir=tmp_path / "nas"),
        clock=clock,
        timer_factory=timers,
        env={},
        backup_fn=flaky,
    )
    job = n.run_now(["backup"])["jobs"]["backup"]
    assert seen == ["alpha", "beta"] and job["status"] == "alerted"
    assert job["summary"]["failed"] == ["alpha: OSError: NAS offline"]
    lines = [a for a in read_alerts(n.alerts_path) if a["kind"] == "backup_failed"]
    assert len(lines) == 1 and lines[0]["data"]["project_id"] == "alpha"


def test_backup_skips_without_a_folder_or_without_the_tool(settings, clock, timers, tmp_path, monkeypatch):
    assert (
        Nightly(settings, clock=clock, timer_factory=timers, env={}).run_now(["backup"])["jobs"]["backup"][
            "message"
        ]
        == "no backup_dir configured"
    )
    monkeypatch.setattr("plate_daemon.scheduler._load_backup_runner", lambda: None)
    n = Nightly(
        settings,
        config=make_cfg(settings, backup_dir=tmp_path / "nas"),
        clock=clock,
        timer_factory=timers,
        env={},
    )
    job = n.run_now(["backup"], force=True)["jobs"]["backup"]
    assert job["status"] == "skipped" and "tools.backup" in job["message"]


# ---------------------------------------------------------------------------- alerts file


def test_read_alerts_skips_a_torn_line_and_keeps_every_kind(settings, clock, timers):
    n = Nightly(
        settings,
        clock=clock,
        timer_factory=timers,
        env={},
        contract_runner=lambda: ContractOutcome(False, failures=["t - x"]),
    )
    n.run_now(["contract_tests"])
    AlertLog(n.alerts_path).write("requote", "run r1 needs a new quote", engine_id="kling-o3-4k")
    with open(n.alerts_path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write('{"kind": "torn')
    kinds = [a["kind"] for a in read_alerts(n.alerts_path)]
    assert kinds == ["contract_test_failed", "requote"]
    assert [a.kind for a in AlertLog(n.alerts_path).read()] == ["requote"]
    assert read_alerts(n.alerts_path.with_name("absent.jsonl")) == []


# ---------------------------------------------------------------------------- schema pairs


def test_a_removed_required_field_in_a_schema_pair_raises_schema_change(settings, clock, timers):
    old = {
        "type": "object",
        "required": ["image"],
        "properties": {"image": {"type": "string"}, "prompt": {"type": "string"}},
    }
    new = {"type": "object", "properties": {"prompt": {"type": "string"}, "seed": {"type": "integer"}}}
    harmless = {"type": "object", "properties": {"prompt": {"type": "string"}}}
    pairs = lambda: [  # noqa: E731
        SchemaPair("kling-o3-4k", old, new, route_id="atlas"),
        SchemaPair("nano-banana-2.1", harmless, harmless),
    ]
    n = contract_nightly(settings, clock, timers, lambda: ContractOutcome(True), schema_pairs=pairs)
    job = n.run_now(["contract_tests"])["jobs"]["contract_tests"]
    assert job["status"] == "alerted"
    checked = {c["engine_id"]: c for c in job["summary"]["schema"]["checked"]}
    assert checked["kling-o3-4k"]["alert"] == "schema_change" and checked["nano-banana-2.1"]["alert"] is None
    lines = [a for a in read_alerts(n.alerts_path) if a["kind"] == "schema_change"]
    assert len(lines) == 1 and lines[0]["engine_id"] == "kling-o3-4k" and lines[0]["route_id"] == "atlas"
    assert "removed required field 'image'" in lines[0]["message"]
    assert [a.kind for a in AlertLog(n.alerts_path).read()] == [
        "schema_change"
    ]  # the engines log reads its own kinds


def test_without_schema_sources_the_contract_job_says_so(settings, clock, timers):
    n = contract_nightly(settings, clock, timers, lambda: ContractOutcome(True))
    job = n.run_now(["contract_tests"])["jobs"]["contract_tests"]
    assert job["summary"]["schema"] == {"status": "skipped", "reason": "no schema sources registered"}
