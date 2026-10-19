"""Project leases (router plan T151, decision 83): one daemon per project folder, 409 for the others.

The finding (Wave 12 dropped item `second-daemon-same-project-adopts-runs`): a second daemon pointed at the same
project folder (a second machine on the NAS, a second home on one box) rebuilt its index from the folder, found
the first daemon's in-flight run in a worker state and drove it on as an orphan: a second poll loop, and a
second set of ledger records, for a job only the first daemon's provider session knew. `project.lock` is held
per write, so it never stopped that. Re-run on 83fb756 (test (3) without the lease checks): daemon B logged
`resumed 1 pipeline run(s) a stopped daemon left in flight` and A's ledger for the run read `quoted, approved,
submitted, failed`, the `failed` written by B.

Now `<project>/lease.json` names the daemon that writes the project and its heartbeat. Another daemon's writes
(anything that takes `project.lock`) are 409 `project_leased`, its boot resume and orphan watch leave the runs
alone, and reads still work. A stale lease (no heartbeat for `LEASE_TTL_S`, or this host's pid is gone) is
taken over with a WARNING. CLI tools install no identity and are never refused.

(1) the lease module, (2) the routes in one process with a foreign lease written by hand, (3) two real daemons
on one folder, (4) `plate reindex` on a leased project.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from plate_daemon import lease
from plate_daemon import pipeline as pipeline_mod
from plate_daemon.adapter import PollResult
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.db import tx
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.fsutil import write_json_atomic
from plate_daemon.index import Index, apply_packet_entry
from plate_daemon.ledger import Ledger, outbox_count
from plate_daemon.lock import project_lock
from plate_daemon.machine import create_run, transition
from plate_daemon.models import Route, RunCreate
from plate_daemon.pipeline import FAKE_ROUTE, STAGING_KEY, Pipeline
from plate_daemon.projects import Project
from plate_daemon.quotes import approve, quote_run
from plate_daemon.states import RunState
from plate_daemon.timeutil import iso, utcnow
from plate_daemon.tools.reindex import reindex

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
FOREIGN_HOST = "render-box-2"
FOREIGN_PID = 4242
FOREIGN_ID = "01JFOREIGNDAEMON000000000A"
S = RunState

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built",
)


@pytest.fixture(autouse=True)
def leases_off():
    """Every test starts and ends with no lease identity (`create_app` installs one for the whole process)."""
    lease.uninstall()
    yield
    lease.uninstall()


def foreign(project_dir: Path, *, age_s: float = 0.0, **fields) -> dict:
    """Write another daemon's lease into the folder, its heartbeat `age_s` seconds old."""
    beat = utcnow() - timedelta(seconds=age_s)
    info = {
        "host": FOREIGN_HOST,
        "pid": FOREIGN_PID,
        "daemon_id": FOREIGN_ID,
        "started_utc": iso(beat - timedelta(hours=1)),
        "heartbeat_utc": iso(beat),
        **fields,
    }
    write_json_atomic(Path(project_dir) / lease.LEASE_NAME, info)
    return info


def on_disk(project_dir: Path) -> dict:
    return json.loads((Path(project_dir) / lease.LEASE_NAME).read_text(encoding="utf-8"))


def dead_pid() -> int:
    """The pid of a process that has exited (and been reaped)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(30)
    return proc.pid


def _wait(predicate, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, what
        time.sleep(0.05)


# ---------------------------------------------------------------------------- (1) the lease module


def test_acquire_writes_the_five_keys_and_remembers_the_file(tmp_path):
    me = lease.install("01JDAEMONA0000000000000000")
    info = lease.acquire(tmp_path)
    doc = on_disk(tmp_path)
    assert set(doc) == {"host", "pid", "daemon_id", "started_utc", "heartbeat_utc"}
    assert doc == info.to_json()
    assert (doc["host"], doc["pid"], doc["daemon_id"]) == (socket.gethostname(), os.getpid(), me.daemon_id)
    assert lease.held() == [(tmp_path / lease.LEASE_NAME).absolute()]
    assert lease.held_by_other(tmp_path) is None  # ours
    later = lease.acquire(tmp_path, now=utcnow() + timedelta(seconds=5))
    assert later.started_utc == info.started_utc and later.heartbeat_utc > info.heartbeat_utc


def test_a_second_identity_is_refused_with_the_first_holders_host_and_pid(tmp_path):
    lease.install("01JDAEMONA0000000000000000")
    lease.acquire(tmp_path)
    lease.install("01JDAEMONB0000000000000000")  # a second daemon (same host, a live pid: this one)
    with pytest.raises(lease.ProjectLeased) as e:
        lease.acquire(tmp_path)
    assert (e.value.host, e.value.pid) == (socket.gethostname(), os.getpid())
    assert e.value.daemon_id == "01JDAEMONA0000000000000000"
    assert "is leased by another PLATE daemon" in str(e.value)
    assert f"wait {lease.LEASE_TTL_S:g} s" in str(e.value)
    assert on_disk(tmp_path)["daemon_id"] == "01JDAEMONA0000000000000000"  # nothing written
    assert lease.held_by_other(tmp_path).daemon_id == "01JDAEMONA0000000000000000"


def test_a_lease_91_s_old_is_taken_over_with_a_warning_naming_the_old_pid(tmp_path, caplog):
    foreign(tmp_path, age_s=91)
    me = lease.install()
    assert lease.held_by_other(tmp_path) is None  # stale: nobody holds it
    with caplog.at_level(logging.WARNING, "plate_daemon.lease"):
        info = lease.acquire(tmp_path)
    assert info.daemon_id == me.daemon_id and on_disk(tmp_path)["daemon_id"] == me.daemon_id
    assert "took over the stale lease on" in caplog.text
    assert f"{FOREIGN_HOST} pid {FOREIGN_PID}" in caplog.text


def test_a_live_foreign_lease_is_refused_and_one_from_a_dead_pid_on_this_host_is_not(tmp_path):
    lease.install()
    foreign(tmp_path, age_s=1)
    with pytest.raises(lease.ProjectLeased) as e:
        lease.acquire(tmp_path)
    assert (e.value.host, e.value.pid, e.value.heartbeat_utc) == (
        FOREIGN_HOST,
        FOREIGN_PID,
        on_disk(tmp_path)["heartbeat_utc"],
    )
    # A daemon on this box that died (kill -9) a second ago: its lease is stale at once, not after 90 s.
    foreign(tmp_path, age_s=1, host=socket.gethostname(), pid=dead_pid())
    assert lease.is_live(lease.read(tmp_path)) is False
    lease.acquire(tmp_path)
    assert on_disk(tmp_path)["daemon_id"] == lease.identity().daemon_id


def test_heartbeat_moves_the_heartbeat_and_drops_a_lease_someone_took_over(tmp_path, caplog):
    lease.install()
    t0 = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    first = lease.acquire(tmp_path, now=t0)
    assert lease.heartbeat(now=t0 + timedelta(seconds=30)) == 1
    doc = on_disk(tmp_path)
    assert doc["heartbeat_utc"] == iso(t0 + timedelta(seconds=30)) and doc["started_utc"] == first.started_utc
    foreign(tmp_path)  # a takeover (or clock trouble): the file is not ours any more
    with caplog.at_level(logging.WARNING, "plate_daemon.lease"):
        assert lease.heartbeat() == 0
    assert "this daemon no longer holds it" in caplog.text
    assert lease.held() == [] and on_disk(tmp_path)["daemon_id"] == FOREIGN_ID  # never overwritten


def test_release_all_removes_only_our_own_files(tmp_path):
    lease.install()
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    lease.acquire(mine)
    lease.acquire(theirs)
    foreign(theirs, age_s=1)  # taken over since
    assert lease.release_all() == 1
    assert not (mine / lease.LEASE_NAME).exists() and on_disk(theirs)["daemon_id"] == FOREIGN_ID
    assert lease.held() == []


@pytest.mark.parametrize("content", ["[]", "garbage{", '{"host": "x"}', '{"host": "x", "pid": true}', ""])
def test_a_lease_file_that_is_not_a_lease_is_treated_as_absent(tmp_path, content):
    (tmp_path / lease.LEASE_NAME).write_text(content, encoding="utf-8")
    assert lease.read(tmp_path) is None
    me = lease.install()
    assert lease.held_by_other(tmp_path) is None
    assert lease.acquire(tmp_path).daemon_id == me.daemon_id
    assert on_disk(tmp_path)["daemon_id"] == me.daemon_id


def test_from_json_wants_the_five_keys_with_their_types():
    good = {"host": "h", "pid": 1, "daemon_id": "d", "started_utc": "s", "heartbeat_utc": "b"}
    assert lease.LeaseInfo.from_json(good) == lease.LeaseInfo("h", 1, "d", "s", "b")
    assert lease.LeaseInfo.from_json({**good, "extra": 1}) == lease.LeaseInfo("h", 1, "d", "s", "b")
    for bad in (
        [],
        None,
        "x",
        {**good, "pid": "1"},
        {**good, "pid": False},
        {k: good[k] for k in list(good)[:4]},
    ):
        assert lease.LeaseInfo.from_json(bad) is None
    assert lease.is_live(lease.LeaseInfo("h", 1, "d", "s", "not a time")) is False


def test_the_project_lock_raises_before_anything_is_written_and_the_home_lock_takes_no_lease(tmp_path):
    lease.install()
    foreign(tmp_path, age_s=1)
    with pytest.raises(lease.ProjectLeased):
        with project_lock(tmp_path, name="project.lock"):
            pass
    assert not (tmp_path / "project.lock").exists() and not (tmp_path / "project.lock.json").exists()
    with project_lock(tmp_path):  # the app-data home lock (`.plate.lock`): no lease
        pass
    assert on_disk(tmp_path)["daemon_id"] == FOREIGN_ID
    lease.uninstall()  # the CLI: no identity, so no lease is consulted or written
    with project_lock(tmp_path, name="project.lock"):
        pass
    assert on_disk(tmp_path)["daemon_id"] == FOREIGN_ID


def test_a_lease_written_between_our_acquire_and_the_lock_wins(tmp_path, monkeypatch):
    """Two daemons that both found no lease: the one whose lease was overwritten before it got the lock is
    refused once it holds it (nothing written, the lock let go), so only one of them writes."""
    lease.install()
    real = lease.acquire

    def racing(project_dir, now=None):
        info = real(project_dir, now)
        foreign(project_dir)  # the other daemon's lease lands while this one waits for the lock
        return info

    monkeypatch.setattr(lease, "acquire", racing)
    ran = False
    with pytest.raises(lease.ProjectLeased) as e:
        with project_lock(tmp_path, name="project.lock"):
            ran = True
    assert not ran and e.value.daemon_id == FOREIGN_ID
    assert not (tmp_path / "project.lock.json").exists()
    lease.uninstall()
    with project_lock(tmp_path, name="project.lock"):  # released: the next holder gets it at once
        pass


def test_the_keeper_beats_and_stops(tmp_path):
    lease.install()
    lease.acquire(tmp_path, now=utcnow() - timedelta(seconds=60))
    before = on_disk(tmp_path)["heartbeat_utc"]
    keeper = lease.Keeper(interval_s=0.05)
    keeper.start()
    try:
        _wait(lambda: on_disk(tmp_path)["heartbeat_utc"] != before, 5, "the keeper never beat")
    finally:
        keeper.stop()
    assert keeper._thread is None


# ---------------------------------------------------------------------------- (2) the routes, one process


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    return Settings.load()


def _graph(packet_id: str) -> dict:
    return {
        "nodes": [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
            {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
        ],
        "edges": [{"id": "p->e", "source": "p", "target": "e"}],
    }


def _setup(c, auth: dict) -> dict:
    """Project + room_a ingested + a Plate feeding one Edit node `e`. The graph."""
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
    r = c.post("/api/v0/packets", json=body, headers=hdr)
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    _wait(
        lambda: c.get(f"/api/v0/packets/{packet_id}", headers=auth).json()["status"] == "ingested",
        60,
        "the packet never finished ingesting",
    )
    graph = _graph(packet_id)
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=auth)
    assert r.status_code == 200, r.text
    return graph


def _head(c, auth: dict) -> dict:
    heads = c.get("/api/v0/projects", headers=auth).json()
    return next(h for h in heads if h["project_id"] == PROJECT)


def _leased_409(r: httpx.Response) -> dict:
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["error"] == "project_leased", body
    assert (body["host"], body["pid"], body["daemon_id"]) == (FOREIGN_HOST, FOREIGN_PID, FOREIGN_ID)
    assert body["heartbeat_utc"] and "is leased by another PLATE daemon" in body["detail"]
    return body


@needs_room_a
def test_a_live_foreign_lease_refuses_writes_409_and_serves_reads(sync_settings, auth):
    with TestClient(create_app(sync_settings), base_url=BASE) as c:
        graph = _setup(c, auth)
        folder = sync_settings.projects_dir / PROJECT
        assert on_disk(folder)["daemon_id"] == lease.identity().daemon_id  # the first write took it
        assert c.get("/api/v0/health").json()["leases"] >= 1
        head = _head(c, auth)["lease"]
        assert head["mine"] is True and head["live"] is True
        r = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth)
        assert r.status_code == 200, r.text
        quoted = r.json()["run"]["id"]

        foreign(folder, age_s=1)
        ledger_before = (folder / "runs.jsonl").read_bytes() if (folder / "runs.jsonl").exists() else b""
        _leased_409(c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=auth))
        _leased_409(c.post(f"/api/v0/projects/{PROJECT}/nodes", json={"kind": "Edit"}, headers=auth))
        _leased_409(c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth))
        _leased_409(c.post(f"/api/v0/runs/{quoted}/approve", json={"by": "human"}, headers=auth))
        runs = c.get(f"/api/v0/projects/{PROJECT}/runs", headers=auth).json()
        assert [(x["id"], x["state"]) for x in runs] == [(quoted, "QUOTED")]  # no row behind the 409s
        r = c.get(f"/api/v0/projects/{PROJECT}/graph", headers=auth)
        assert r.status_code == 200 and [n["id"] for n in r.json()["nodes"]] == ["p", "e"]
        head = _head(c, auth)["lease"]
        assert head["mine"] is False and head["live"] is True
        assert (head["host"], head["pid"], head["daemon_id"]) == (FOREIGN_HOST, FOREIGN_PID, FOREIGN_ID)
        after = (folder / "runs.jsonl").read_bytes() if (folder / "runs.jsonl").exists() else b""
        assert after == ledger_before and on_disk(folder)["daemon_id"] == FOREIGN_ID

        foreign(folder, age_s=120)  # two minutes without a heartbeat: stale
        assert _head(c, auth)["lease"]["live"] is False
        r = c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=auth)
        assert r.status_code == 200, r.text
        assert on_disk(folder)["daemon_id"] == lease.identity().daemon_id
        assert _head(c, auth)["lease"]["mine"] is True
        assert c.get("/api/v0/health").json()["leases"] >= 1
    assert not (folder / lease.LEASE_NAME).exists()  # the lifespan released it


@needs_room_a
def test_a_lease_taken_between_the_check_and_the_ledger_cancels_the_quote_in_this_index_only(
    sync_settings, auth, monkeypatch
):
    """The backstop: the `quoted` record a lease refuses cancels the run (`project_leased`) with nothing
    ledgered or parked, and the answer is still 409 `project_leased`, never 503 `ledger_unavailable`."""
    with TestClient(create_app(sync_settings), base_url=BASE) as c:
        _setup(c, auth)
        folder = sync_settings.projects_dir / PROJECT
        before = Ledger(folder).records()
        monkeypatch.setattr(Pipeline, "refuse_if_leased", lambda self, project_id: None)
        foreign(folder, age_s=1)
        _leased_409(c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth))
        runs = c.get(f"/api/v0/projects/{PROJECT}/runs", headers=auth).json()
        assert [(r["state"], r["reason_code"]) for r in runs] == [("CANCELLED", "project_leased")]
        assert Ledger(folder).records() == before
        with c.app.state.index.project(PROJECT) as conn:
            assert outbox_count(conn) == 0


ROUTE = Route(engine_id="fake", provider="fake", region="local")
T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)


def test_resume_and_the_orphan_watch_leave_a_leased_projects_runs_alone(
    plate_env, tmp_path, monkeypatch, caplog
):
    pid, packet = "boot", "pkt_boot"
    idx = Index(tmp_path)
    with idx.project(pid, create=True) as conn, tx(conn):
        entry = {
            "shot_id": "s01",
            "kind": "still",
            "tier": "A",
            "packet_version": "0.1.1",
            "path": str(tmp_path / "packets" / packet),
            "manifest_blake3": "a" * 64,
        }
        apply_packet_entry(conn, pid, packet, entry)
    with idx.project(pid) as conn:
        inputs = {"plate": uuid.uuid4().hex, STAGING_KEY: {"node_kind": "Edit", "region_crop": False}}
        run = create_run(
            conn, RunCreate(project_id=pid, packet_id=packet, inputs=inputs, route=ROUTE), now=T0
        )
        quote_run(conn, run.id, now=T0)
        approve(conn, run.id, "human", now=T0)
        transition(conn, run.id, S.QUEUED, actor="human")
    idx.remember(run.id, pid)
    pipeline = Pipeline(
        settings=Settings.load(),
        index=idx,
        cas_for=lambda _pid: None,
        bus=None,
        engine=FakeEngine(),
        manifests={},
        clock=lambda: T0,
    )
    monkeypatch.setattr(pipeline_mod, "ORPHAN_GRACE_S", -1.0)  # the run's last move counts as old
    lease.install()
    folder = pipeline.project_dir(pid)
    foreign(folder, age_s=1)
    with caplog.at_level(logging.WARNING, "plate_daemon.pipeline"):
        assert pipeline.orphaned_runs() == []
        assert pipeline.resume() == []
        assert pipeline.orphaned_runs() == []
    msg = f"project {pid} is leased by {FOREIGN_HOST} pid {FOREIGN_PID}: its runs stay with that daemon"
    assert caplog.text.count(msg) == 1  # once per project per LEASE_TTL_S
    with idx.project(pid) as conn:
        state = conn.execute("SELECT state FROM runs WHERE id = ?", (run.id,)).fetchone()[0]
        notes = conn.execute(
            "SELECT COUNT(*) FROM run_events WHERE run_id = ? AND kind = 'resume'", (run.id,)
        )
        assert state == "QUEUED" and notes.fetchone()[0] == 0
    foreign(folder, age_s=lease.LEASE_TTL_S + 1)  # lapsed: the watch would pick the run up now
    assert pipeline.orphaned_runs() == [(pid, run.id)]
    pipeline.close(1.0)


# ---------------------------------------------------------------------------- (3) two daemons, one folder


class SlowJobs(FakeEngine):
    """An echo fake whose jobs run until the test calls `advance` (its own clock, frozen until then)."""

    route = {**FAKE_ROUTE, "max_inflight": 4}

    def __init__(self, job_s: float = 15.0):
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


class ChildDaemon:
    """`python -m plate_daemon` on its own home, its output in a file (never a pipe that can fill)."""

    def __init__(self, home: Path, port: int, child_env, **env: str):
        self.home, self.port = home, port
        self.base = f"http://127.0.0.1:{port}/api/v0"
        self.out = home / f"daemon-{uuid.uuid4().hex[:6]}.log"
        environ = child_env(
            PLATE_HOME=str(home),
            PLATE_PORT=str(port),
            PLATE_FAKE_ENGINE="1",
            PLATE_SYNC_INGEST="1",
            PLATE_NIGHTLY="0",
            PLATE_PIXELS_INPROC="1",
            PLATE_LOG_LEVEL="warning",
            **env,
        )
        self.fh = open(self.out, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "plate_daemon"], stdout=self.fh, stderr=subprocess.STDOUT, env=environ
        )
        deadline = time.monotonic() + 60
        while True:
            if self.proc.poll() is not None:
                raise AssertionError(f"daemon exited {self.proc.returncode}: {self.log()[-2000:]}")
            try:
                if httpx.get(self.base + "/health", timeout=5).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            assert time.monotonic() < deadline, "daemon did not answer within 60 s"
            time.sleep(0.2)
        self.token = json.loads((home / "config.json").read_text(encoding="utf-8-sig"))["token"]

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}", **kw.pop("headers", {})}
        return httpx.request(method, self.base + path, headers=headers, timeout=30, **kw)

    def log(self) -> str:
        self.fh.flush()
        return self.out.read_text(encoding="utf-8", errors="replace")

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(30)
        self.fh.close()


@needs_room_a
def test_a_second_daemon_on_the_folder_leaves_the_first_daemons_run_alone(
    tmp_path, monkeypatch, live_server, child_env, free_port
):
    monkeypatch.setenv("PLATE_RUN_WORKERS", "4")
    # Daemon B runs with a 5 s TTL (so a lease A failed to remove would also lapse); A beats fast enough for it.
    monkeypatch.setattr(lease, "HEARTBEAT_S", 0.5)
    engine = SlowJobs(job_s=15.0)

    def fast_polls(app) -> None:
        app.state.pipeline.poll_interval_s = 0.05

    home_b = tmp_path / "home_b"
    home_b.mkdir()
    b: ChildDaemon | None = None
    try:
        with live_server(engine=engine, on_app=fast_polls) as (s, url):
            auth = {"Authorization": f"Bearer {s.token}"}
            folder = s.projects_dir / PROJECT
            with httpx.Client(base_url=url, timeout=30) as http:
                _setup(http, auth)
                r = http.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth)
                assert r.status_code == 200, r.text
                run_id = r.json()["run"]["id"]
                r = http.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=auth)
                assert r.status_code == 200, r.text
                _wait(
                    lambda: http.get(f"/api/v0/runs/{run_id}", headers=auth).json()["state"] == "RUNNING",
                    30,
                    "the run never reached RUNNING on daemon A",
                )
                assert on_disk(folder)["daemon_id"] == lease.identity().daemon_id

                Index(home_b).rebuild_from_project(folder)  # B's index is a cache of the same folder
                b = ChildDaemon(home_b, free_port(), child_env, PLATE_LEASE_TTL_S="5", PLATE_RUN_WORKERS="4")
                _wait(lambda: "is leased by" in b.log(), 30, "daemon B never logged the lease")
                assert f"project {PROJECT} is leased by {socket.gethostname()} pid {os.getpid()}" in b.log()
                runs = b.call("GET", f"/projects/{PROJECT}/runs").json()
                assert run_id in [x["id"] for x in runs]
                r = b.call("POST", f"/projects/{PROJECT}/nodes/e/quote", json={})
                assert r.status_code == 409 and r.json()["error"] == "project_leased", r.text
                assert (r.json()["host"], r.json()["pid"]) == (socket.gethostname(), os.getpid())
                assert engine.submit_count == 1

                engine.advance(engine.job_s)
                _wait(
                    lambda: http.get(f"/api/v0/runs/{run_id}", headers=auth).json()["state"] == "DONE",
                    60,
                    "the run never finished on daemon A",
                )
                assert engine.submit_count == 1
                journal_b = home_b / "fake_engine.json"
                assert (
                    not journal_b.exists()
                    or json.loads(journal_b.read_text(encoding="utf-8"))["submit_count"] == 0
                )
                kinds = [x["kind"] for x in Ledger(folder).records() if x.get("run_id") == run_id]
                assert kinds == ["quoted", "approved", "submitted", "fetched", "done"]
        assert not (folder / lease.LEASE_NAME).exists()  # A stopped: its lease is gone

        def b_quotes() -> bool:
            r = b.call("POST", f"/projects/{PROJECT}/nodes/e/quote", json={})
            assert r.status_code in (200, 409), r.text
            return r.status_code == 200

        _wait(b_quotes, 10, "daemon B could not write the project once A stopped")
        doc = on_disk(folder)
        assert doc["pid"] == b.proc.pid and doc["daemon_id"] != lease.identity().daemon_id
    finally:
        if b is not None:
            b.kill()


# ---------------------------------------------------------------------------- (4) plate reindex


def test_plate_reindex_still_works_on_a_leased_project(tmp_path, child_env):
    folder = tmp_path / "projects" / "tula-a"
    Project.create(folder, "tula-a", {"nda": False})
    foreign(folder, age_s=1)
    rep = reindex(folder, tmp_path / "home")  # no identity in this process: the CLI's case
    assert rep.project_id == "tula-a"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "plate_daemon.tools",
            "reindex",
            str(folder),
            "--home",
            str(tmp_path / "home2"),
        ],
        env=child_env(PLATE_HOME=str(tmp_path / "home2")),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert on_disk(folder)["daemon_id"] == FOREIGN_ID  # untouched
    lease.install()  # a daemon's process, by contrast, is refused
    with pytest.raises(lease.ProjectLeased):
        reindex(folder, tmp_path / "home3")
