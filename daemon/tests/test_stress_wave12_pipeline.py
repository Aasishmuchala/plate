"""Wave 12 stress fixes in the pipeline's ledger and execution (router plan T145), each test an attacker's repro.

- Decision 73, the ledger never drops a record. A project lock held for 7 s right after an approval
  (`t13_lock_lost.py`) and a read-only `runs.jsonl` while the run is in flight (`t9_readonly.py`) used to lose
  the `submitted` record, so `GET /spend` showed nothing for a run that billed; the records now wait in the
  index's ledger outbox and go in, in order, before the next append. An override the ledger cannot take is a
  503 `ledger_unavailable` with no note behind it. 36 approve/cancel races (`t2_cancel_race.py`) used to write
  one run's records out of order or twice; every run now ends CANCELLED live and in a rebuilt index, with one
  `cancelled` record each. A submit whose answer was lost (`submit_state_unknown`) is ledgered with
  `data.billing_unknown`, never `refunded`.
- Decision 75, runs drive on a bounded executor. 45 approvals of slow jobs (`s_inflight.py`) used to pin the
  40-thread request pool and freeze `/health`; on the executor every approval answers at once and no more than
  the route's `max_inflight` run at a time. An index held under `BEGIN IMMEDIATE` for 12 s (`t17_index_busy.py`)
  used to strand the run in RUNNING; the step is retried and the run ends DONE. A run the daemon already stopped
  on twice in NORMALIZING is failed `resume_limit` at the next boot instead of crash-looping it.
- A cancel that a stalled vendor call holds up answers 202 `cancel: requested` at once.
- A `project.json` that does not read is listed by `GET /projects` as `broken: true` instead of disappearing.

Runs go through the real pipeline on `fixtures/packets/room_a` with fakes whose result is the PNG they were
sent (`echo=True`), so the gates score real pixels.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import statistics
import threading
import time
from collections import Counter
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from plate_daemon.adapter import PollResult
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.index import Index
from plate_daemon.ledger import Ledger, outbox_count
from plate_daemon.machine import append_note, get_run
from plate_daemon.pipeline import FAKE_ROUTE
from plate_daemon.projects import Lock

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
ACTIVE = ("STAGING", "SUBMITTED", "RUNNING", "FETCHING", "NORMALIZING", "GATING")

pytestmark = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    return Settings.load()


def _client(settings: Settings, engine: FakeEngine) -> TestClient:
    c = TestClient(create_app(settings, engine), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


def _graph(packet_id: str) -> dict:
    return {
        "nodes": [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
            {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
        ],
        "edges": [{"id": "p->e", "source": "p", "target": "e"}],
    }


def _setup(c: TestClient | httpx.Client, auth: dict) -> str:
    """Project + room_a ingested + a Plate feeding one Edit node `e`. The packet id."""
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
    r = c.post("/api/v0/packets", json=body, headers=hdr)
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    deadline = time.monotonic() + 60
    while c.get(f"/api/v0/packets/{packet_id}", headers=auth).json()["status"] != "ingested":
        assert time.monotonic() < deadline, "the packet never finished ingesting"
        time.sleep(0.05)
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json=_graph(packet_id), headers=auth)
    assert r.status_code == 200, r.text
    return packet_id


def _quote(c: TestClient | httpx.Client, auth: dict) -> dict:
    r = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _approve(c: TestClient | httpx.Client, auth: dict, run_id: str, **body):
    return c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human", **body}, headers=auth)


def _ledger(settings: Settings) -> Ledger:
    return Ledger(settings.projects_dir / PROJECT)


def _kinds(settings: Settings, run_id: str) -> list[str]:
    led = _ledger(settings)
    assert led.verify() == (True, None)
    return [r["kind"] for r in led.records() if r.get("run_id") == run_id]


def _notes(c: TestClient, run_id: str, kind: str) -> int:
    with c.app.state.index.project(PROJECT) as conn:
        q = "SELECT COUNT(*) FROM run_events WHERE run_id = ? AND kind = ?"
        return conn.execute(q, (run_id, kind)).fetchone()[0]


def _wait(predicate, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, what
        time.sleep(0.02)


# ---------------------------------------------------------------------------- decision 73: the outbox


class HoldsTheLockAtSubmit(FakeEngine):
    """An echo fake that, the moment the provider has the job, holds the project's writer lock from another
    thread for `hold_s` (what `plate reindex` or `plate gc` does on a big project, `t13_lock_lost.py`)."""

    def __init__(self, project_dir: Path, hold_s: float = 7.0):
        super().__init__(echo=True)
        self.project_dir, self.hold_s = project_dir, hold_s
        self.released = threading.Event()

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        held = threading.Event()

        def hold() -> None:
            with Lock(self.project_dir):
                held.set()
                time.sleep(self.hold_s)
            self.released.set()

        threading.Thread(target=hold, name="hold-project-lock", daemon=True).start()
        assert held.wait(5)
        return job


def test_a_project_lock_held_through_the_submit_loses_no_record(sync_settings, auth, caplog):
    engine = HoldsTheLockAtSubmit(sync_settings.projects_dir / PROJECT)
    with _client(sync_settings, engine) as c, caplog.at_level(logging.WARNING, "plate_daemon.pipeline"):
        _setup(c, auth)
        quoted = _quote(c, auth)
        run_id = quoted["run"]["id"]
        r = _approve(c, auth, run_id)
        assert r.status_code == 200, r.text
        assert engine.released.wait(15)
        assert c.get(f"/api/v0/runs/{run_id}", headers=auth).json()["state"] == "DONE"
        assert "queued in the ledger outbox" in caplog.text  # the submit came while the lock was held
        assert _kinds(sync_settings, run_id) == ["quoted", "approved", "submitted", "fetched", "done"]
        with c.app.state.index.project(PROJECT) as conn:
            assert outbox_count(conn) == 0
        spend = c.get(f"/api/v0/projects/{PROJECT}/spend", headers=auth).json()
        assert spend["spent_usd_quoted"] == pytest.approx(quoted["quote"]["price_usd"])
        assert spend["spent_usd_quoted"] == pytest.approx(0.04) and spend["generations"] == 1


class ReadOnlyLedgerAtSubmit(FakeEngine):
    """An echo fake that makes `runs.jsonl` read-only the moment the provider has the job (`t9_readonly.py`)."""

    def __init__(self, ledger_path: Path):
        super().__init__(echo=True)
        self.ledger_path = ledger_path

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        os.chmod(self.ledger_path, 0o444)
        return job


def test_a_read_only_ledger_during_the_run_parks_the_records_until_the_next_append(sync_settings, auth):
    path = sync_settings.projects_dir / PROJECT / "runs.jsonl"
    engine = ReadOnlyLedgerAtSubmit(path)
    try:
        with _client(sync_settings, engine) as c:
            _setup(c, auth)
            run_id = _quote(c, auth)["run"]["id"]
            r = _approve(c, auth, run_id)
            assert r.status_code == 200, r.text
            if os.access(path, os.W_OK):
                pytest.skip("this account writes read-only files (root?)")
            assert c.get(f"/api/v0/runs/{run_id}", headers=auth).json()["state"] == "DONE"
            assert _kinds(sync_settings, run_id) == ["quoted", "approved"]  # the rest waits in the outbox
            with c.app.state.index.project(PROJECT) as conn:
                assert outbox_count(conn) == 3
            os.chmod(path, 0o644)

            nxt = _quote(c, auth)["run"]["id"]  # the next append drains the outbox first, in order
            with c.app.state.index.project(PROJECT) as conn:
                assert outbox_count(conn) == 0
            led = _ledger(sync_settings)
            assert led.verify() == (True, None)
            order = [(r["kind"], r.get("run_id")) for r in led.records()]
            assert order[-4:] == [
                ("submitted", run_id),
                ("fetched", run_id),
                ("done", run_id),
                ("quoted", nxt),
            ]
            spend = c.get(f"/api/v0/projects/{PROJECT}/spend", headers=auth).json()
            assert spend["generations"] == 1 and spend["spent_usd_quoted"] == pytest.approx(0.04)
    finally:
        if path.exists():
            os.chmod(path, 0o644)


def test_an_override_the_ledger_cannot_take_is_503_and_leaves_no_note(sync_settings, auth):
    path = sync_settings.projects_dir / PROJECT / "runs.jsonl"
    try:
        with _client(sync_settings, FakeEngine(echo=True, shift_px=12)) as c:
            _setup(c, auth)
            run_id = _quote(c, auth)["run"]["id"]
            assert _approve(c, auth, run_id).status_code == 200
            assert c.get(f"/api/v0/runs/{run_id}", headers=auth).json()["state"] == "GATE_FAIL"
            os.chmod(path, 0o444)
            if os.access(path, os.W_OK):
                pytest.skip("this account writes read-only files (root?)")
            body = {"reason": "the client signed the drifted take off", "by": "human"}
            r = c.post(f"/api/v0/runs/{run_id}/override", json=body, headers=auth)
            assert r.status_code == 503, r.text
            assert r.json()["error"] == "ledger_unavailable" and r.json()["run_id"] == run_id
            assert _notes(c, run_id, "override") == 0
            view = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
            assert view["override"] is None and view["exportable"] is False
            with c.app.state.index.project(PROJECT) as conn:
                assert outbox_count(conn) == 0  # a fail-closed kind is refused, never parked

            os.chmod(path, 0o644)
            r = c.post(f"/api/v0/runs/{run_id}/override", json=body, headers=auth)
            assert r.status_code == 200, r.text
            assert _notes(c, run_id, "override") == 1
            assert _kinds(sync_settings, run_id).count("override") == 1
            assert c.get(f"/api/v0/runs/{run_id}", headers=auth).json()["exportable"] is True
    finally:
        if path.exists():
            os.chmod(path, 0o644)


def test_approve_cancel_races_end_cancelled_live_and_rebuilt_with_one_record_each(
    sync_settings, auth, tmp_path
):
    engine = FakeEngine(timeout=True)  # the job never finishes: only the cancel ends a run
    with _client(sync_settings, engine) as c:
        c.app.state.pipeline.sleep = lambda _s: time.sleep(0.002)
        _setup(c, auth)
        run_ids: list[str] = []
        answers: Counter[tuple[int, int]] = Counter()
        for i in range(36):
            run_id = _quote(c, auth)["run"]["id"]
            run_ids.append(run_id)
            delay = 0.002 * (i % 6)  # 0 to 10 ms after the approval starts
            out: dict[str, int] = {}

            def approve(rid: str = run_id, out: dict[str, int] = out) -> None:
                out["approve"] = _approve(TestClient(c.app, base_url=BASE), auth, rid).status_code

            def cancel(rid: str = run_id, d: float = delay, out: dict[str, int] = out) -> None:
                time.sleep(d)
                r = TestClient(c.app, base_url=BASE).post(f"/api/v0/runs/{rid}/cancel", json={}, headers=auth)
                out["cancel"] = r.status_code

            threads = [threading.Thread(target=approve), threading.Thread(target=cancel)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
            answers[(out["approve"], out["cancel"])] += 1
            assert out["approve"] in (200, 409) and out["cancel"] in (200, 202, 409), out

        def all_cancelled() -> bool:
            with c.app.state.index.project(PROJECT) as conn:
                return all(get_run(conn, rid).state.value == "CANCELLED" for rid in run_ids)

        _wait(all_cancelled, 30, "every raced run ends CANCELLED in the live index")
        led = _ledger(sync_settings)
        assert led.verify() == (True, None)
        per_run = Counter((r.get("run_id"), r["kind"]) for r in led.records())
        for rid in run_ids:
            assert per_run[(rid, "cancelled")] == 1, (rid, per_run)
            assert per_run[(rid, "quoted")] == 1 and per_run[(rid, "approved")] <= 1

    copy = tmp_path / "rebuilt" / "projects" / PROJECT
    shutil.copytree(sync_settings.projects_dir / PROJECT, copy)
    index = Index(tmp_path / "rebuilt")
    index.rebuild_from_project(copy)
    with index.project(PROJECT) as conn:
        states = {rid: get_run(conn, rid).state.value for rid in run_ids}
    assert set(states.values()) == {"CANCELLED"}, states


def test_a_submit_whose_answer_was_lost_is_ledgered_billing_unknown_never_refunded(sync_settings, auth):
    class TimesOutAfterCreating(FakeEngine):
        idempotent_submit = False  # no lookup either: nothing can say whether the job exists

        def submit(self, payload, idempotency_key):
            super().submit(payload, idempotency_key)  # the provider has the job...
            raise httpx.ReadTimeout("the read operation timed out")  # ...and the answer never came

    engine = TimesOutAfterCreating(echo=True)
    with _client(sync_settings, engine) as c:
        _setup(c, auth)
        run_id = _quote(c, auth)["run"]["id"]
        assert _approve(c, auth, run_id).status_code == 200
        run = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
        assert (run["state"], run["reason_code"]) == ("FAILED", "submit_state_unknown")
        assert engine.submit_count == 1  # never resubmitted blind
        failed = [r for r in _ledger(sync_settings).records() if r["kind"] == "failed"]
        assert len(failed) == 1 and failed[0]["run_id"] == run_id
        assert failed[0]["data"]["billing_unknown"] is True
        assert "refunded" not in failed[0]["data"]
        assert failed[0]["data"]["reason_code"] == "submit_state_unknown"


# ---------------------------------------------------------------------------- decision 75: execution


class SlowJobs(FakeEngine):
    """An echo fake whose jobs take `job_s` seconds of its own clock, which only the test moves (frozen until
    `advance`). Its route allows `max_inflight` 4."""

    route = {**FAKE_ROUTE, "max_inflight": 4}

    def __init__(self, job_s: float = 20.0):
        super().__init__(echo=True, polls_until_done=1)
        self.job_s = job_s
        self.now = 0.0
        self.started: dict[str, float] = {}
        self.mu = threading.Lock()

    def advance(self, seconds: float) -> None:
        with self.mu:
            self.now += seconds

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        with self.mu:
            self.started.setdefault(job, self.now)
        return job

    def poll(self, provider_job_id):
        with self.mu:
            ready = self.now - self.started.get(provider_job_id, self.now) >= self.job_s
        return super().poll(provider_job_id) if ready else PollResult("running")


def test_45_slow_approvals_run_on_the_executor_and_never_freeze_the_api(
    plate_env, monkeypatch, live_server, settings
):
    monkeypatch.setenv("PLATE_RUN_WORKERS", "4")
    engine = SlowJobs(job_s=20.0)
    apps: list = []

    def fast_polls(app) -> None:
        app.state.pipeline.poll_interval_s = 0.05
        apps.append(app)

    with live_server(engine=engine, on_app=fast_polls) as (s, url):
        auth = {"Authorization": f"Bearer {s.token}"}
        pipeline = apps[0].state.pipeline
        assert not pipeline.executor.inline and pipeline.executor.workers == 4
        db = Path(s.home) / "index" / f"{PROJECT}.sqlite"
        with httpx.Client(base_url=url, timeout=30) as http:
            _setup(http, auth)
            run_ids = [_quote(http, auth)["run"]["id"] for _ in range(45)]

            stop = threading.Event()
            health_s: list[float] = []
            health_codes: list[int] = []
            active_seen: list[int] = []

            def probe() -> None:
                with httpx.Client(base_url=url, timeout=10) as h:
                    while not stop.is_set():
                        t0 = time.monotonic()
                        health_codes.append(h.get("/api/v0/health", headers=auth).status_code)
                        health_s.append(time.monotonic() - t0)
                        ro = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=5)
                        try:
                            marks = ",".join("?" * len(ACTIVE))
                            q = f"SELECT COUNT(*) FROM runs WHERE state IN ({marks})"
                            active_seen.append(ro.execute(q, ACTIVE).fetchone()[0])
                        finally:
                            ro.close()
                        time.sleep(0.05)

            prober = threading.Thread(target=probe, name="probe", daemon=True)
            prober.start()
            try:
                t0 = time.monotonic()
                for rid in run_ids:
                    r = _approve(http, auth, rid)
                    assert r.status_code == 200, r.text
                approvals_s = time.monotonic() - t0
                assert approvals_s < 15, approvals_s

                def four_running() -> bool:
                    states = Counter(
                        http.get(f"/api/v0/runs/{rid}", headers=auth).json()["state"] for rid in run_ids
                    )
                    return states["RUNNING"] == 4 and states["QUEUED"] == 41

                _wait(four_running, 15, "four runs RUNNING at the route cap, the rest QUEUED")
                assert pipeline.executor.stats()["running"] == 4

                def all_done() -> bool:
                    engine.advance(engine.job_s)  # the fake's time passes: every job submitted so far ends
                    states = {
                        http.get(f"/api/v0/runs/{rid}", headers=auth).json()["state"] for rid in run_ids
                    }
                    return states == {"DONE"}

                _wait(all_done, 240, "every approved run ends DONE once the fake's jobs finish")
            finally:
                stop.set()
                prober.join(15)
        assert health_codes and set(health_codes) == {200}
        # The attack this pins froze the API for the whole run (30 s+). One slow probe on a loaded runner
        # (windows-latest saw a 2.2 s outlier) is not a freeze: the median must stay fast and no probe may
        # take more than 5 s.
        assert statistics.median(health_s) < 0.5 and max(health_s) < 5.0, (
            statistics.median(health_s),
            max(health_s),
        )
        assert max(active_seen) <= 4, max(active_seen)
        assert engine.submit_count == 45


class IndexHeldWhileRunning(FakeEngine):
    """An echo fake that, when its job first reports success (the run is RUNNING), has another connection hold
    the project index under `BEGIN IMMEDIATE` for `hold_s` (`t17_index_busy.py`)."""

    def __init__(self, db_path: Path, hold_s: float = 12.0):
        super().__init__(echo=True, polls_until_done=2)
        self.db_path, self.hold_s = db_path, hold_s
        self.held, self.released = threading.Event(), threading.Event()

    def poll(self, provider_job_id):
        res = super().poll(provider_job_id)
        if res.status == "succeeded" and not self.held.is_set():

            def hold() -> None:
                db = sqlite3.connect(self.db_path, isolation_level=None, timeout=30)
                db.execute("BEGIN IMMEDIATE")
                self.held.set()
                time.sleep(self.hold_s)
                db.execute("COMMIT")
                db.close()
                self.released.set()

            threading.Thread(target=hold, name="hold-index", daemon=True).start()
            assert self.held.wait(5)
        return res


def test_a_run_survives_an_index_held_busy_for_12_s(sync_settings, auth, caplog):
    engine = IndexHeldWhileRunning(sync_settings.home / "index" / f"{PROJECT}.sqlite")
    with _client(sync_settings, engine) as c, caplog.at_level(logging.WARNING, "plate_daemon.pipeline"):
        _setup(c, auth)
        run_id = _quote(c, auth)["run"]["id"]
        assert _approve(c, auth, run_id).status_code == 200
        assert engine.released.is_set()
        run = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
        assert run["state"] == "DONE", run.get("error")
        assert "the project index is busy" in caplog.text
        assert _kinds(sync_settings, run_id)[-2:] == ["fetched", "done"]


def test_a_run_the_daemon_stopped_on_twice_in_normalizing_fails_resume_limit(sync_settings, auth):
    with _client(sync_settings, FakeEngine(echo=True)) as c:
        _setup(c, auth)
        pl = c.app.state.pipeline
        stuck, once = (_quote(c, auth)["run"]["id"] for _ in range(2))
        with c.app.state.index.project(PROJECT) as conn:
            for rid, notes in ((stuck, 2), (once, 1)):
                assert _approve(c, auth, rid, start=False).status_code == 200
                pl.queue(conn, PROJECT, rid)
                while get_run(conn, rid).state.value != "NORMALIZING":
                    pl.step(conn, PROJECT, rid)
                for n in range(notes):  # what two (one) earlier boots that died in NORMALIZING wrote
                    append_note(conn, rid, kind="resume", actor="system", reason=f"boot {n + 1}")
            result_key = get_run(conn, stuck).result_key
            assert result_key

    with _client(sync_settings, FakeEngine(echo=True)) as c:  # the next boot: the lifespan resumes

        def settled() -> bool:
            states = {c.get(f"/api/v0/runs/{rid}", headers=auth).json()["state"] for rid in (stuck, once)}
            return states <= {"FAILED", "DONE"}

        _wait(settled, 60, "the boot resume settles both runs")
        run = c.get(f"/api/v0/runs/{stuck}", headers=auth).json()
        assert (run["state"], run["reason_code"]) == ("FAILED", "resume_limit")
        assert "the daemon stopped twice while driving this run" in run["error"]
        assert result_key in run["error"]
        assert _notes(c, stuck, "resume") == 2  # no third attempt was made
        assert c.get(f"/api/v0/runs/{once}", headers=auth).json()["state"] == "DONE"
        assert _notes(c, once, "resume") == 2
        r = c.get("/api/v0/health", headers=auth)
        assert r.status_code == 200 and r.json()["ok"] is True
        failed = [r for r in _ledger(sync_settings).records() if r["kind"] == "failed"]
        assert [(r["run_id"], r["data"]["reason_code"]) for r in failed] == [(stuck, "resume_limit")]


class StallsTheRunningPoll(FakeEngine):
    """An echo fake whose second poll (the run is RUNNING) blocks until the test lets it go."""

    def __init__(self):
        super().__init__(echo=True, polls_until_done=3)
        self.stalled, self.go = threading.Event(), threading.Event()
        self.n = 0

    def poll(self, provider_job_id):
        self.n += 1
        if self.n == 2:
            self.stalled.set()
            assert self.go.wait(30)
        return super().poll(provider_job_id)


def test_a_cancel_held_up_by_a_stalled_vendor_call_answers_202_at_once(sync_settings, auth):
    engine = StallsTheRunningPoll()
    with _client(sync_settings, engine) as c:
        _setup(c, auth)
        run_id = _quote(c, auth)["run"]["id"]
        approved: dict[str, int] = {}
        t = threading.Thread(target=lambda: approved.update(code=_approve(c, auth, run_id).status_code))
        t.start()
        try:
            assert engine.stalled.wait(30)
            t0 = time.monotonic()
            r = TestClient(c.app, base_url=BASE).post(f"/api/v0/runs/{run_id}/cancel", json={}, headers=auth)
            assert time.monotonic() - t0 < 3.5
            assert r.status_code == 202, r.text
            assert r.json() == {"run_id": run_id, "state": "RUNNING", "cancel": "requested"}
        finally:
            engine.go.set()
            t.join(60)
        assert approved["code"] == 200
        assert c.get(f"/api/v0/runs/{run_id}", headers=auth).json()["state"] == "CANCELLED"
        assert _kinds(sync_settings, run_id).count("cancelled") == 1


# ---------------------------------------------------------------------------- the project list


def test_a_project_json_that_does_not_read_is_listed_as_broken(client, auth, settings):
    for pid in ("good", "torn"):
        r = client.post("/api/v0/projects", json={"project_id": pid, "custody": {"nda": False}}, headers=auth)
        assert r.status_code == 201, r.text
    doc = settings.projects_dir / "torn" / "project.json"
    raw = doc.read_bytes()
    doc.write_bytes(raw[: len(raw) // 2])  # a sync tool's half-written copy
    r = client.get("/api/v0/projects", headers=auth)
    assert r.status_code == 200, r.text
    heads = {h["project_id"]: h for h in r.json()}
    assert set(heads) == {"good", "torn"}
    assert "broken" not in heads["good"] and heads["good"]["custody"]["nda"] is False
    torn = heads["torn"]
    assert torn["broken"] is True and Path(torn["root"]) == settings.projects_dir / "torn"
    assert torn["detail"]
