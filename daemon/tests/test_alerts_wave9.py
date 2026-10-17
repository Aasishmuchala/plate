"""Alerts to the artist and route degradation (router plan T85).

Three seams, all on fakes: `route_health.Tracker` (consecutive provider failures per route, persisted), the
worker hooks that feed it (a poll that raises and a failed submit note a failure, a fetch notes a success), the
`GET /alerts` route over the alert files, and `Nightly(on_alert=...)`, which hands every appended alert line
to the daemon. Nothing sleeps and nothing reaches a vendor.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from plate_daemon import route_health
from plate_daemon.cas import CAS
from plate_daemon.db import tx
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.index import Index, apply_packet_entry
from plate_daemon.machine import create_run, get_run, transition
from plate_daemon.models import Route, RunCreate
from plate_daemon.projects import Project
from plate_daemon.quotes import approve, quote_run
from plate_daemon.route_health import Tracker, route_key
from plate_daemon.routes import alerts as alerts_route
from plate_daemon.scheduler import (
    NIGHTLY_ALERT_KINDS,
    JobOutcome,
    Nightly,
    read_alerts,
)
from plate_daemon.states import RunState
from plate_daemon.tools.alerts import BudgetAlert
from plate_daemon.worker import Runner, step
from plate_engines.health import Alert, AlertLog

S = RunState
PID = "alerts"
PACKET = "pkt_alerts"
ROUTE = Route(engine_id="fake", provider="fake", region="local", route_id="local")
KEY = "fake/local"
T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------- helpers


class UnknownJob(Exception):
    """What an adapter raises when the provider has no such job."""


class PollRaises(FakeEngine):
    """A FakeEngine whose poll raises: the provider forgot the job, or the transport failed."""

    def poll(self, provider_job_id):
        raise UnknownJob("no such job at the provider")


class Clock:
    def __init__(self, now: datetime = T0):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


def make_index(home: Path) -> Index:
    idx = Index(home)
    with idx.project(PID) as conn, tx(conn):
        apply_packet_entry(
            conn,
            PID,
            PACKET,
            {
                "shot_id": "s01",
                "kind": "still",
                "tier": "A",
                "packet_version": "0.1.1",
                "path": str(home / "packets" / PACKET),
                "manifest_blake3": "a" * 64,
            },
        )
    return idx


def cas_of(home: Path) -> CAS:
    return CAS(home / "projects" / PID / "assets")


def queued(idx: Index, route: Route = ROUTE) -> str:
    with idx.project(PID) as conn:
        run = create_run(
            conn,
            RunCreate(
                project_id=PID,
                packet_id=PACKET,
                inputs={"plate": uuid.uuid4().hex, "prompt": "low sun"},
                route=route,
            ),
            now=T0,
        )
        quote_run(conn, run.id, now=T0)
        approve(conn, run.id, "human", now=T0)
        transition(conn, run.id, S.QUEUED, actor="human")
    idx.remember(run.id, PID)
    return run.id


def submitted(idx: Index, home: Path, good: FakeEngine | None = None, **kw) -> str:
    """A run the (good) provider has accepted: QUEUED -> STAGING -> SUBMITTED."""
    rid = queued(idx, **kw)
    good = good or FakeEngine()
    with idx.project(PID) as conn:
        assert step(conn, good, rid, cas=cas_of(home)) is S.STAGING
        assert step(conn, good, rid, cas=cas_of(home)) is S.SUBMITTED
    return rid


def drive(idx: Index, home: Path, rid: str, engine, **kw) -> RunState:
    with idx.project(PID) as conn:
        return step(conn, engine, rid, cas=cas_of(home), **kw)


# ---------------------------------------------------------------------------- the tracker


def test_route_key_is_engine_slash_route_or_the_bare_engine():
    assert route_key("kling-o3-4k", "atlas") == "kling-o3-4k/atlas"
    assert route_key("kling-o3-4k", None) == "kling-o3-4k" and route_key("kling-o3-4k", "") == "kling-o3-4k"


def test_three_consecutive_failures_inside_an_hour_degrade_a_route(tmp_path):
    clock = Clock()
    t = Tracker(tmp_path, clock=clock)
    for _ in range(2):
        t.note_failure("fake", "local", "provider_poll_error")
        clock.advance(minutes=1)
    assert t.degraded() == {}  # two is not enough
    t.note_failure("fake", "local", "submit_error")
    assert t.degraded() == {
        KEY: {"failures": 3, "since": "2026-10-18T09:00:00.000000Z", "reason": "submit_error"}
    }
    assert t.is_degraded("fake", "local") and not t.is_degraded("fake", "other")


def test_failures_spread_over_more_than_an_hour_are_not_degraded(tmp_path):
    clock = Clock()
    t = Tracker(tmp_path, clock=clock)
    for _ in range(3):
        t.note_failure("fake", "local", "provider_poll_error")
        clock.advance(minutes=40)
    assert t.degraded() == {}  # at most two of them fall inside any one hour


def test_a_degraded_route_the_artist_stopped_calling_heals_after_an_hour(tmp_path):
    clock = Clock()
    t = Tracker(tmp_path, clock=clock)
    for _ in range(3):
        t.note_failure("fake", "local", "provider_poll_error")
    assert KEY in t.degraded()
    assert KEY in t.degraded(now=T0 + timedelta(minutes=59))
    assert t.degraded(now=T0 + timedelta(minutes=61)) == {}  # explicit `now`, read-only


def test_a_route_that_keeps_failing_stays_degraded(tmp_path):
    clock = Clock()
    t = Tracker(tmp_path, clock=clock)
    for _ in range(8):  # one failure every 20 minutes for almost three hours
        t.note_failure("fake", "local", "provider_poll_error")
        clock.advance(minutes=20)
    got = t.degraded()
    assert KEY in got and got[KEY]["failures"] == 3  # three inside the last hour
    assert datetime.fromisoformat(got[KEY]["since"].replace("Z", "+00:00")) >= clock.now - timedelta(hours=1)


def test_a_success_ends_the_run_of_failures_and_reset_forgets_a_key(tmp_path):
    t = Tracker(tmp_path, clock=Clock())
    for _ in range(3):
        t.note_failure("fake", "local", "provider_poll_error")
        t.note_failure("other", None, "submit_error")
    assert set(t.degraded()) == {KEY, "other"}
    t.note_success("fake", "local")
    assert set(t.degraded()) == {"other"}
    assert t.reset("other") is True and t.reset("other") is False
    assert t.degraded() == {}
    t.note_success("never-seen", "x")  # nothing to forget, nothing written, no error


def test_failures_after_a_success_start_a_new_run(tmp_path):
    t = Tracker(tmp_path, clock=Clock())
    t.note_failure("fake", "local", "a")
    t.note_failure("fake", "local", "a")
    t.note_success("fake", "local")
    t.note_failure("fake", "local", "a")
    assert t.degraded() == {}  # 1 failure since the success, not 3


def test_the_count_survives_a_restart_and_two_trackers_agree(tmp_path):
    clock = Clock()
    for _ in range(3):
        Tracker(tmp_path, clock=clock).note_failure("fake", "local", "provider_poll_error")
    path = tmp_path / "cache" / "route_health.json"
    assert path.is_file()
    raw = path.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")  # LF on every OS
    doc = json.loads(raw.decode("utf-8"))
    assert doc["v"] == 1 and len(doc["routes"][KEY]["failures"]) == 3
    assert KEY in Tracker(tmp_path, clock=clock).degraded()  # a fresh object, the same file


@pytest.mark.parametrize("junk", ["{not json", "[]", '{"routes": 7}', '{"routes": {"fake/local": 3}}', ""])
def test_an_unreadable_state_file_starts_empty_instead_of_failing(tmp_path, junk):
    path = tmp_path / "cache" / "route_health.json"
    path.parent.mkdir(parents=True)
    path.write_text(junk, encoding="utf-8")
    t = Tracker(tmp_path, clock=Clock())
    assert t.degraded() == {}
    t.note_failure("fake", "local", "x")  # and it can be written over
    assert (
        t.degraded() == {}
        and len(json.loads(path.read_text(encoding="utf-8"))["routes"][KEY]["failures"]) == 1
    )


def test_the_failure_list_per_key_is_bounded(tmp_path):
    t = Tracker(tmp_path, clock=Clock())
    for _ in range(route_health.MAX_POINTS + 25):
        t.note_failure("fake", "local", "x")
    doc = json.loads((tmp_path / "cache" / "route_health.json").read_text(encoding="utf-8"))
    assert len(doc["routes"][KEY]["failures"]) == route_health.MAX_POINTS


def test_threads_noting_failures_lose_none(tmp_path):
    import threading

    t = Tracker(tmp_path, clock=Clock())
    threads = [
        threading.Thread(target=lambda: [t.note_failure("fake", "local", "x") for _ in range(10)])
        for _ in range(6)
    ]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert t.degraded()[KEY]["failures"] == 50  # capped at MAX_POINTS, none of the 60 corrupted the file


# ---------------------------------------------------------------------------- the worker hooks


def test_three_unknown_job_polls_degrade_the_route_and_a_fourth_run_that_fetches_resets_it(tmp_path):
    idx = make_index(tmp_path)
    tracker = Tracker(tmp_path, clock=Clock())
    for n in range(3):
        rid = submitted(idx, tmp_path)
        assert drive(idx, tmp_path, rid, PollRaises(), route_tracker=tracker) is S.FAILED
        with idx.project(PID) as conn:
            assert get_run(conn, rid).reason_code == "provider_poll_error"
        assert (KEY in tracker.degraded()) is (n == 2)
    assert tracker.degraded()[KEY]["reason"] == "provider_poll_error"

    good = FakeEngine()
    rid = submitted(idx, tmp_path, good)
    assert drive(idx, tmp_path, rid, good, route_tracker=tracker) is S.RUNNING
    assert KEY in tracker.degraded()  # a poll that answers is not a delivered result
    assert drive(idx, tmp_path, rid, good, route_tracker=tracker) is S.FETCHING
    assert KEY in tracker.degraded()
    assert drive(idx, tmp_path, rid, good, route_tracker=tracker) is S.NORMALIZING  # the fetch returned bytes
    assert tracker.degraded() == {}


def test_a_failed_submit_notes_a_failure_with_the_submit_error_code(tmp_path):
    idx = make_index(tmp_path)
    tracker = Tracker(tmp_path, clock=Clock())
    for _ in range(3):
        rid = queued(idx)
        engine = FakeEngine(behaviour="fail_submit")
        assert drive(idx, tmp_path, rid, engine, route_tracker=tracker) is S.STAGING
        assert drive(idx, tmp_path, rid, engine, route_tracker=tracker) is S.FAILED
        with idx.project(PID) as conn:
            assert get_run(conn, rid).reason_code == "submit_error"
    assert tracker.degraded()[KEY]["reason"] == "submit_error"


def test_the_worker_passes_engine_route_and_reason_code_to_the_tracker(tmp_path):
    idx = make_index(tmp_path)
    calls: list[tuple] = []
    tracker = SimpleNamespace(
        note_failure=lambda *a: calls.append(("fail", *a)), note_success=lambda *a: calls.append(("ok", *a))
    )
    rid = submitted(idx, tmp_path)
    drive(idx, tmp_path, rid, PollRaises(), route_tracker=tracker)
    assert calls == [("fail", "fake", "local", "provider_poll_error")]
    calls.clear()
    # A run whose route names no route id reports None, and the tracker files it under the engine.
    rid = submitted(idx, tmp_path, route=Route(engine_id="fake", provider="fake", region="local"))
    drive(idx, tmp_path, rid, PollRaises(), route_tracker=tracker)
    assert calls == [("fail", "fake", None, "provider_poll_error")]


def test_the_tracker_is_found_on_the_engines_pipeline_and_absent_means_no_op(tmp_path):
    idx = make_index(tmp_path)
    tracker = Tracker(tmp_path, clock=Clock())

    class StagedLike(PollRaises):
        """What `Pipeline.step` drives: an engine that carries its pipeline."""

        pipeline = SimpleNamespace(route_tracker=tracker)

    rid = submitted(idx, tmp_path)
    assert drive(idx, tmp_path, rid, StagedLike()) is S.FAILED
    assert (
        len(
            json.loads((tmp_path / "cache" / "route_health.json").read_text(encoding="utf-8"))["routes"][KEY][
                "failures"
            ]
        )
        == 1
    )

    class NoTracker(PollRaises):
        pipeline = SimpleNamespace()  # T107 has not attached one

    rid = submitted(idx, tmp_path)
    assert drive(idx, tmp_path, rid, NoTracker()) is S.FAILED  # nothing noted, nothing raised
    rid = submitted(idx, tmp_path)
    assert drive(idx, tmp_path, rid, PollRaises()) is S.FAILED  # no pipeline attribute at all
    assert (
        len(
            json.loads((tmp_path / "cache" / "route_health.json").read_text(encoding="utf-8"))["routes"][KEY][
                "failures"
            ]
        )
        == 1
    )


def test_a_tracker_that_raises_never_touches_the_run(tmp_path):
    idx = make_index(tmp_path)

    def boom(*_a):
        raise OSError("disk full")

    tracker = SimpleNamespace(note_failure=boom, note_success=boom)
    rid = submitted(idx, tmp_path)
    assert drive(idx, tmp_path, rid, PollRaises(), route_tracker=tracker) is S.FAILED
    with idx.project(PID) as conn:
        assert get_run(conn, rid).reason_code == "provider_poll_error"
    good = FakeEngine()
    rid = submitted(idx, tmp_path, good)
    drive(idx, tmp_path, rid, good, route_tracker=tracker)  # RUNNING
    drive(idx, tmp_path, rid, good, route_tracker=tracker)  # FETCHING
    assert drive(idx, tmp_path, rid, good, route_tracker=tracker) is S.NORMALIZING


def test_the_runner_hands_its_tracker_to_every_step(tmp_path):
    idx = make_index(tmp_path)
    tracker = Tracker(tmp_path, clock=Clock())
    runner = Runner(idx, None, {"*": PollRaises()}, route_tracker=tracker)
    for _ in range(3):
        submitted(idx, tmp_path)
    runner.tick()
    assert tracker.degraded()[KEY]["failures"] == 3


# ---------------------------------------------------------------------------- GET /alerts


def write_engine_alert(path: Path, kind: str, at: str, **kw) -> None:
    AlertLog(path).append(Alert(kind=kind, message=kw.pop("message", f"{kind} happened"), at=at, **kw))


def write_budget_alert(project_dir: Path, kind: str, key: str, at: str, project_id: str | None) -> None:
    line = BudgetAlert(kind, key, f"{kind} for {key}", {"day": "2026-10-18"}).to_line(at, project_id)
    with open(project_dir / "alerts.jsonl", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")


def get_alerts(client, auth, **params):
    r = client.get("/api/v0/alerts", params=params, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def test_alerts_needs_the_token(client):
    assert client.get("/api/v0/alerts").status_code == 401


def test_no_alert_files_is_an_empty_list(client, auth):
    assert get_alerts(client, auth) == []


def test_alerts_lists_engine_and_budget_lines_with_their_levels_newest_first(client, auth, settings):
    engine_log = settings.home / "alerts.jsonl"
    write_engine_alert(engine_log, "engine_suspended", "2026-10-18T08:00:00Z", engine_id="kling-o3-4k")
    write_engine_alert(engine_log, "canary_drift", "2026-10-18T08:05:00Z", engine_id="nano-banana-2.1")
    write_engine_alert(engine_log, "retired_pin", "2026-10-18T08:06:00Z", engine_id="flux-3")
    nightly_log = settings.home / "logs" / "alerts.jsonl"
    write_engine_alert(nightly_log, "price_change", "2026-10-18T07:00:00Z", engine_id="seedream-5-pro")
    write_engine_alert(nightly_log, "requote", "2026-10-18T07:01:00Z")
    write_engine_alert(nightly_log, "schema_change", "2026-10-18T07:02:00Z")
    write_engine_alert(nightly_log, "unit_mismatch", "2026-10-18T07:03:00Z")
    write_engine_alert(nightly_log, "engine_resumed", "2026-10-18T09:00:00Z", severity="info", engine_id="x")
    project = Project.create(settings.projects_dir / "alpha", "alpha", {"nda": False})
    write_budget_alert(project.root, "daily_cap_exceeded", "k1", "2026-10-18T10:00:00Z", "alpha")
    write_budget_alert(project.root, "unattended_spend", "k2", "2026-10-18T06:00:00Z", "alpha")

    got = get_alerts(client, auth)

    assert [a["at"] for a in got] == sorted((a["at"] for a in got), reverse=True)  # newest first
    by_kind = {a["kind"]: a for a in got}
    assert len(got) == 10
    for kind in ("engine_suspended", "canary_drift", "retired_pin"):
        assert by_kind[kind]["level"] == "error" and by_kind[kind]["source"] == "engine"
    for kind in ("price_change", "requote", "schema_change", "unit_mismatch"):
        assert by_kind[kind]["level"] == "warning"
    assert by_kind["engine_resumed"]["level"] == "info"
    assert by_kind["engine_suspended"]["engine_id"] == "kling-o3-4k"
    assert "engine_id" not in by_kind["requote"] and "project_id" not in by_kind["requote"]
    exceeded, unattended = by_kind["daily_cap_exceeded"], by_kind["unattended_spend"]
    assert (
        exceeded["level"] == "critical" and unattended["level"] == "warn"
    )  # a budget line keeps its severity
    assert exceeded["source"] == "budget" and exceeded["project_id"] == "alpha"
    assert exceeded["message"] == "daily_cap_exceeded for k1" and exceeded["at"] == "2026-10-18T10:00:00Z"
    assert set(exceeded) == {"kind", "level", "message", "at", "source", "project_id"}


def test_a_budget_line_in_a_project_folder_takes_that_projects_id(client, auth, settings):
    project = Project.create(settings.projects_dir / "beta", "beta", {"nda": False})
    write_budget_alert(
        project.root, "daily_cap_near", "k", "2026-10-18T10:00:00Z", None
    )  # no data.project_id
    [only] = get_alerts(client, auth)
    assert only["project_id"] == "beta" and only["level"] == "warn"


def test_nightly_kinds_keep_their_severity_and_unknown_kinds_are_info(client, auth, settings):
    log = settings.home / "logs" / "alerts.jsonl"
    log.parent.mkdir(parents=True)
    lines = [
        {"at": "2026-10-18T02:00:00Z", "kind": "job_error", "severity": "critical", "message": "boom"},
        {"at": "2026-10-18T02:01:00Z", "kind": "backup_failed", "severity": "warn", "message": "slow disk"},
        {"at": "2026-10-18T02:02:00Z", "kind": "something_new", "message": "no severity"},
    ]
    with open(log, "w", encoding="utf-8", newline="\n") as fh:
        fh.writelines(json.dumps(x) + "\n" for x in lines)
    got = {a["kind"]: a for a in get_alerts(client, auth)}
    assert got["job_error"]["level"] == "critical" and got["job_error"]["source"] == "nightly"
    assert got["backup_failed"]["level"] == "warn"
    assert got["something_new"]["level"] == "info" and got["something_new"]["source"] == "engine"


def test_since_and_limit(client, auth, settings):
    log = settings.home / "alerts.jsonl"
    for minute in range(6):
        write_engine_alert(log, "price_change", f"2026-10-18T09:0{minute}:00Z", message=f"m{minute}")
    assert [a["message"] for a in get_alerts(client, auth, limit=2)] == ["m5", "m4"]
    assert [a["message"] for a in get_alerts(client, auth, since="2026-10-18T09:04:00Z")] == ["m5", "m4"]
    assert [a["message"] for a in get_alerts(client, auth, since="2026-10-18T09:04:00+00:00", limit=1)] == [
        "m5"
    ]
    assert get_alerts(client, auth, since="2026-10-19T00:00:00Z") == []
    assert len(get_alerts(client, auth)) == 6


@pytest.mark.parametrize("params", [{"since": "yesterday"}, {"since": ""}])
def test_a_bad_since_is_a_422(client, auth, params):
    r = client.get("/api/v0/alerts", params=params, headers=auth)
    assert r.status_code == 422 and r.json()["error"] == "bad_since"


@pytest.mark.parametrize("limit", [0, -1, 1001])
def test_a_limit_outside_1_to_1000_is_a_422(client, auth, limit):
    assert client.get("/api/v0/alerts", params={"limit": limit}, headers=auth).status_code == 422


def test_a_torn_last_line_and_a_line_without_a_time_do_not_hide_the_rest(client, auth, settings):
    log = settings.home / "alerts.jsonl"
    write_engine_alert(log, "price_change", "2026-10-18T09:00:00Z", message="whole")
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "requote", "message": "no time"}) + "\n")
        fh.write('{"kind": "schema_ch')  # a crash mid-write
    got = get_alerts(client, auth)
    assert [a["message"] for a in got] == ["whole", "no time"]  # the one with no time sorts last
    assert got[1]["at"] == ""
    assert get_alerts(client, auth, since="2026-10-18T00:00:00Z") == [got[0]]  # and never matches a `since`


def test_the_same_file_is_not_listed_twice(client, auth, settings):
    write_engine_alert(settings.home / "alerts.jsonl", "price_change", "2026-10-18T09:00:00Z")
    assert len(get_alerts(client, auth)) == 1  # the pipeline lists it once and no project folder repeats it


def test_the_route_module_does_not_import_the_scheduler_and_spells_its_kinds_right():
    import ast

    assert alerts_route.NIGHTLY_KINDS == NIGHTLY_ALERT_KINDS
    tree = ast.parse(Path(alerts_route.__file__).read_text(encoding="utf-8"))
    imported = {(node.module or "") for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    assert not any("scheduler" in name for name in imported)  # a broken scheduler must not take the API down


# ---------------------------------------------------------------------------- Nightly(on_alert=...)


class SchedClock:
    def __init__(self):
        self.now = datetime(2026, 10, 17, 3, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def alerting_jobs(*, fail: tuple[str, ...] = ()):
    """Every job a no-op, except `prices_refresh` (writes two engine alerts) and the ones named in `fail`."""

    def noop(ctx):
        return JobOutcome()

    def prices(ctx):
        ctx.alerts.write("price_change", "nano moved 12%", engine_id="nano-banana-2.1")
        ctx.alerts.write("requote", "r1 needs a new quote", severity="info")
        return JobOutcome()

    def failing(ctx):
        raise RuntimeError("blew up")

    from plate_daemon.scheduler import JOB_ORDER

    jobs = {name: noop for name in JOB_ORDER}
    jobs["prices_refresh"] = prices
    for name in fail:
        jobs[name] = failing
    return jobs


def make_nightly(settings, **kw) -> Nightly:
    return Nightly(settings, clock=SchedClock(), timer_factory=lambda *_a: None, env={}, **kw)


def test_on_alert_fires_once_for_every_line_the_nightly_appends(settings):
    seen: list[dict] = []
    n = make_nightly(settings, jobs=alerting_jobs(fail=("backup",)), on_alert=seen.append)
    rec = n.run_now()
    lines = read_alerts(n.alerts_path)
    assert [a["kind"] for a in lines] == ["price_change", "requote", "job_error"]
    assert seen == lines  # one call per line, in order, with the line as it was written
    assert seen[0]["engine_id"] == "nano-banana-2.1" and seen[0]["severity"] == "warn"
    assert seen[2]["severity"] == "critical" and seen[2]["data"] == {"job": "backup"}
    assert rec["jobs"]["backup"]["status"] == "error"


def test_on_alert_gets_a_copy_it_can_change_freely(settings):
    def scribble(line):
        line["kind"] = "scribbled"
        line["data"]["x"] = 1

    n = make_nightly(settings, jobs=alerting_jobs(fail=("backup",)), on_alert=scribble)
    rec = n.run_now()
    assert [a["kind"] for a in read_alerts(n.alerts_path)] == ["price_change", "requote", "job_error"]
    assert [a["kind"] for a in rec["jobs"]["backup"]["alerts"]] == [
        "job_error"
    ]  # the job record is untouched


def test_a_raising_on_alert_is_logged_and_never_fatal(settings, caplog):
    calls: list[str] = []

    def broken(line):
        calls.append(line["kind"])
        raise RuntimeError("bus is down")

    n = make_nightly(settings, jobs=alerting_jobs(fail=("backup",)), on_alert=broken)
    with caplog.at_level("WARNING"):
        rec = n.run_now()
    assert calls == ["price_change", "requote", "job_error"]  # every line still offered
    assert rec["done"] and rec["jobs"]["prices_refresh"]["status"] == "alerted"  # the jobs ran to the end
    assert [a["kind"] for a in read_alerts(n.alerts_path)] == ["price_change", "requote", "job_error"]
    assert "on_alert failed for price_change alert: RuntimeError: bus is down" in caplog.text


def test_no_on_alert_changes_nothing(settings):
    n = make_nightly(settings, jobs=alerting_jobs(fail=("backup",)))
    assert n.on_alert is None
    n.run_now()
    assert [a["kind"] for a in read_alerts(n.alerts_path)] == ["price_change", "requote", "job_error"]


def test_on_alert_can_be_attached_after_the_nightly_is_built(settings):
    seen: list[str] = []
    n = make_nightly(settings, jobs=alerting_jobs())
    n.on_alert = lambda line: seen.append(line["kind"])
    n.run_now()
    assert seen == ["price_change", "requote"]


def test_the_timer_thread_error_alert_reaches_on_alert_too(settings):
    seen: list[dict] = []
    n = make_nightly(settings, jobs=alerting_jobs(), on_alert=seen.append)

    def explode(now=None):
        raise RuntimeError("tick blew up")

    n.tick = explode  # type: ignore[method-assign]
    n._fire()
    assert [a["kind"] for a in seen] == ["job_error"] and seen[0]["data"] == {"job": "scheduler"}
    assert seen == read_alerts(n.alerts_path)
