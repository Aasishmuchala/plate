"""Wave 12 stress fixes in the index, the SSE cursor and boot (router plan T144), each test an attacker's repro.

- ghost-index (api-fuzz): opening an index for a project that does not exist used to create
  `index/<id>.sqlite`, so a refused `POST /envelopes` left a file that `project_ids()` then counted and the
  project-less `GET /events/stream` answered 422 "2 projects are indexed". `Index.project` now raises
  `UnknownProject` (the API's 404 `not_found`) unless the caller makes the project real (`create=True`).
- index-loss-no-rebuild-sse-seq-reset (crash): a daemon started without its index served an empty project; a
  `plate reindex` restarted the event seq at 1, so a client reconnecting with `Last-Event-ID: 15` skipped every
  new event up to 15. Boot now rebuilds a lost index from a folder that has a ledger, and SSE ids are
  `<epoch>.<seq>`: a cursor from another epoch (or a bare one above the head) replays from 0.
- ledger-order-race-rebuild-wrong-state (money): an approve/cancel race appended `quoted, cancelled, approved`
  for a CANCELLED run, which the rebuild folded to APPROVED. The fold now follows `data.event_seq`, a terminal
  state is never undone by a non-terminal one, and a repeated `(run_id, kind, state)` folds once.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from plate_daemon import __main__ as daemon_main
from plate_daemon.api import create_app
from plate_daemon.config import Settings, index_db_path
from plate_daemon.db import tx
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.index import Index, UnknownProject, apply_packet_entry, fold_run_records
from plate_daemon.ledger import Ledger
from plate_daemon.machine import create_run
from plate_daemon.models import Route, RunCreate
from plate_daemon.projects import Project
from plate_daemon.quotes import quote_run
from plate_daemon.routes.events import cursor, parse_event_id
from plate_daemon.sse import Bus, EventFilter
from plate_daemon.states import RunState
from plate_daemon.tools import reindex

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
PID = "epochs"
ROUTE = Route(engine_id="fake", provider="fake", region="local")
PACKET_ENTRY = {
    "shot_id": "s01",
    "kind": "still",
    "tier": "A",
    "packet_version": "0.1.1",
    "path": "/packets/pkt_s",
    "manifest_blake3": "a" * 64,
}

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


class SseReader:
    """Reads `text/event-stream` frames; `event()` returns the next data frame's id parts and record."""

    def __init__(self, response: httpx.Response):
        self.lines = response.iter_lines()

    def frame(self) -> dict:
        frame: dict = {}
        for line in self.lines:
            if line == "":
                if frame:
                    return frame
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            frame[field] = value[1:] if value.startswith(" ") else value
        raise AssertionError("stream ended")

    def event(self) -> dict:
        while True:
            f = self.frame()
            if "data" in f:
                epoch, _, seq = f["id"].rpartition(".")
                return {"raw_id": f["id"], "epoch": epoch, "seq": int(seq), "rec": json.loads(f["data"])}


def _delete_index(db: Path) -> None:
    """Remove an index file and its WAL sidecars (Windows may hold a just-closed handle for a moment)."""
    for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        for _ in range(50):
            try:
                p.unlink(missing_ok=True)
                break
            except PermissionError:
                time.sleep(0.1)
        assert not p.exists(), p


def _no_index_files(home: Path) -> list[str]:
    d = home / "index"
    return sorted(p.name for p in d.glob("*.sqlite")) if d.is_dir() else []


def _folder_project(settings: Settings, pid: str = PID) -> tuple[Project, Index]:
    """A project folder under projects_dir, registered the way POST /projects registers it."""
    project = Project.create(settings.projects_dir / pid, pid, {"nda": False})
    idx = Index(settings.home)
    idx.set_project_root(pid, project.root, project.custody)
    return project, idx


def _envelope(project_id: str, owner: str = "agent:test") -> dict:
    return {"project_id": project_id, "owner": owner, "max_total_usd": 1, "max_per_run_usd": 1, "ttl_s": 3600}


# ---------------------------------------------------------------------------- no ghost indexes


def test_opening_an_unknown_project_raises_and_creates_nothing(settings):
    idx = Index(settings.home)
    with pytest.raises(UnknownProject) as err:
        with idx.project("ghost"):
            pass
    assert err.value.project_id == "ghost" and isinstance(err.value, LookupError)
    with pytest.raises(UnknownProject):
        idx.connect("ghost")
    assert not index_db_path("ghost", settings.home).exists()
    assert idx.project_ids() == [] and idx.project_root("ghost") is None
    with pytest.raises(UnknownProject):
        idx.record_asset("ghost", "f" * 64, "png", 1)  # a CAS callback for a project that is not there
    assert _no_index_files(settings.home) == []

    with idx.project("real", create=True) as conn:  # what set_project_root / packet-first ingest pass
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert idx.project_ids() == ["real"]
    with idx.project("real") as conn:  # an existing index opens without asking
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_an_index_deleted_under_a_running_daemon_is_migrated_again_when_recreated(settings):
    idx = Index(settings.home)
    with idx.project("again", create=True):
        pass
    first = idx.epoch("again")
    _delete_index(index_db_path("again", settings.home))
    with pytest.raises(UnknownProject):
        idx.connect("again")
    with idx.project("again", create=True) as conn:  # the migrated-once cache must not skip the schema
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert idx.epoch("again") != first


@pytest.mark.skipif(sys.platform == "win32", reason="Windows forbids a newline in a file name")
def test_project_ids_take_whole_ids_only(settings):
    idx = Index(settings.home)
    with idx.project("ok", create=True):
        pass
    d = settings.home / "index"
    (d / "nl1\n.sqlite").write_bytes(b"")  # what `re.match` with `$` accepted before the fullmatch fix
    (d / "Not A Slug.sqlite").write_bytes(b"")
    assert idx.project_ids() == ["ok"]


def test_a_refused_envelope_for_an_unknown_project_leaves_no_sqlite(client, auth, settings, monkeypatch):
    r = client.post("/api/v0/envelopes", json=_envelope("never-created"), headers=auth)
    assert r.status_code == 404 and r.json() == {"error": "not_found", "project_id": "never-created"}
    r = client.post("/api/v0/envelopes", json=_envelope("never-created-2", owner="robot"), headers=auth)
    assert r.status_code == 422, r.text
    assert _no_index_files(settings.home) == []

    # Belt and braces with T126: even a route that reaches `index.project` for a project with no index (here
    # the route's own project check is fooled) answers the 404 and creates nothing.
    pipeline = client.app.state.pipeline
    monkeypatch.setattr(pipeline, "load_project", lambda pid: object())
    r = client.post("/api/v0/envelopes", json=_envelope("never-created"), headers=auth)
    assert r.status_code == 404, r.text
    assert r.json() == {"error": "not_found", "project_id": "never-created"}
    assert not index_db_path("never-created", settings.home).exists()
    assert client.app.state.index.project_ids() == []


def test_the_streams_answer_404_for_a_project_with_no_index(client, auth, settings):
    r = client.get("/api/v0/events/stream?project_id=ghost", headers=auth)
    assert r.status_code == 404 and r.json() == {"error": "not_found", "project_id": "ghost"}
    r = client.get(f"/api/v0/events?project_id=ghost&token={settings.token}")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert _no_index_files(settings.home) == []
    r = client.get("/api/v0/events/stream?project_id=x&after=not-a-cursor", headers=auth)
    assert r.status_code == 422 and "event id" in r.json()["errors"][0]


def test_the_stream_without_a_project_id_still_streams_after_refused_envelopes(client, auth, settings):
    r = client.post("/api/v0/projects", json={"project_id": "solo", "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text
    for body in (_envelope("never-created"), _envelope("never-created-2", owner="robot")):
        assert client.post("/api/v0/envelopes", json=body, headers=auth).status_code in (404, 422)
    idx = client.app.state.index
    assert idx.project_ids() == ["solo"]
    with idx.project("solo") as conn:
        with tx(conn):
            apply_packet_entry(conn, "solo", "pkt_s", PACKET_ENTRY)
        run = create_run(conn, RunCreate(project_id="solo", packet_id="pkt_s", route=ROUTE))
        quote_run(conn, run.id)
    epoch = idx.epoch("solo")

    r = client.get("/api/v0/events/stream", headers=auth)  # no project_id: the only project's transitions
    assert r.status_code == 200, r.text
    frames = [f for f in r.text.split("\n\n") if "data: " in f]
    assert frames, r.text
    ids = [line[4:] for f in frames for line in f.splitlines() if line.startswith("id: ")]
    data = [json.loads(line[6:]) for f in frames for line in f.splitlines() if line.startswith("data: ")]
    assert all(i.startswith(f"{epoch}.") for i in ids)
    assert {d["run_id"] for d in data} == {run.id} and {d["project_id"] for d in data} == {"solo"}
    assert data[-1]["to_state"] == "QUOTED"


# ---------------------------------------------------------------------------- the SSE cursor and the epoch


def test_cursor_reads_epoch_ids_bare_ids_and_nothing():
    assert parse_event_id("01ABC.7") == ("01ABC", 7)
    assert parse_event_id("7") == (None, 7)
    assert parse_event_id("") is None and parse_event_id(None) is None and parse_event_id("x.y") is None
    e, head = "01EPOCH", 10
    assert cursor(None, None, epoch=e, head=head) == 0
    assert cursor(None, f"{e}.4", epoch=e, head=head) == 4
    assert cursor(None, "01OLDEPOCH.4", epoch=e, head=head) == 0  # another epoch: replay from 0
    assert cursor(None, "4", epoch=e, head=head) == 4  # an old client's bare seq
    assert cursor(None, "15", epoch=e, head=head) == 0  # above the head: from another history
    assert cursor("3", None, epoch=e, head=head) == 3 and cursor(3, None, epoch=e, head=head) == 3
    assert cursor(f"{e}.2", None, epoch=e, head=head) == 2
    assert cursor("01OLDEPOCH.2", None, epoch=e, head=head) == 0
    # The browser's Last-Event-ID wins over the URL's after.
    assert cursor("2", f"{e}.6", epoch=e, head=head) == 6
    assert cursor("2", "garbage", epoch=e, head=head) == 2  # a malformed header falls back to after
    assert cursor("-5", None, epoch=e, head=head) == 0


def test_bus_records_carry_the_epoch_and_a_rebuild_gives_a_new_one(settings):
    project, idx = _folder_project(settings)
    bus = Bus(idx)
    first = bus.epoch(PID)
    assert first and bus.epoch(PID) == first  # stable while the index lives
    seqs = [bus.publish(None, {"kind": "k", "n": n}, project_id=PID) for n in range(3)]
    assert [r["epoch"] for r in bus.read(EventFilter(PID), 0)] == [first] * 3
    assert bus.head(PID) == seqs[-1]
    idx.rebuild_from_project(project.root)
    assert bus.epoch(PID) != first
    assert bus.read(EventFilter(PID), 0) == [] and bus.head(PID) == 0
    assert bus.publish(None, {"kind": "k"}, project_id=PID) == 1  # seq restarts: the epoch tells them apart


def test_an_index_from_before_epochs_gets_one_on_first_read(settings):
    _project, idx = _folder_project(settings)
    with idx.project(PID) as conn, tx(conn):
        conn.execute("DELETE FROM project_meta WHERE key = 'index_epoch'")
    bus = Bus(idx)
    bus.publish(None, {"kind": "k"}, project_id=PID)
    epoch = bus.read(EventFilter(PID), 0)[0]["epoch"]
    assert epoch and bus.epoch(PID) == epoch


def test_a_reconnect_from_before_a_reindex_receives_every_event_of_the_new_epoch(settings, live_server):
    project, idx = _folder_project(settings)
    bus = Bus(idx, poll_s=0.05)
    for n in range(15):
        bus.publish(None, {"kind": "run.state", "n": n}, project_id=PID)
    old = bus.epoch(PID)
    with live_server(bus=bus) as (s, url):
        q = f"{url}/api/v0/events?project_id={PID}&token={s.token}"
        with httpx.Client(timeout=10) as c:
            with c.stream("GET", q) as r:
                reader = SseReader(r)
                seen = [reader.event() for _ in range(15)]
            last = seen[-1]["raw_id"]
            assert last == f"{old}.15" and [e["seq"] for e in seen] == list(range(1, 16))

            report = reindex.reindex(project.root, settings.home)  # what `plate reindex` runs
            assert report.project_id == PID
            new = bus.epoch(PID)
            assert new != old
            # More new events than the old cursor: a bare seq would resume at 15 and skip 1..15.
            for n in range(18):
                bus.publish(None, {"kind": "run.state", "n": 100 + n}, project_id=PID)
            with c.stream("GET", q, headers={"Last-Event-ID": last}) as r:
                reader = SseReader(r)
                got = [reader.event() for _ in range(18)]
            assert [e["seq"] for e in got] == list(range(1, 19))
            assert {e["epoch"] for e in got} == {new}
            assert [e["rec"]["data"]["n"] for e in got] == [100 + n for n in range(18)]


def test_a_bare_last_event_id_above_the_head_replays_from_0(settings, live_server):
    project, idx = _folder_project(settings)
    bus = Bus(idx, poll_s=0.05)
    for n in range(15):
        bus.publish(None, {"kind": "k", "n": n}, project_id=PID)
    idx.rebuild_from_project(project.root)
    for n in range(4):
        bus.publish(None, {"kind": "k", "n": 50 + n}, project_id=PID)
    with live_server(bus=bus) as (s, url):
        q = f"{url}/api/v0/events?project_id={PID}&token={s.token}"
        with httpx.Client(timeout=10) as c, c.stream("GET", q, headers={"Last-Event-ID": "15"}) as r:
            assert r.status_code == 200
            reader = SseReader(r)
            got = [reader.event() for _ in range(4)]
    assert [e["seq"] for e in got] == [1, 2, 3, 4]
    assert [e["rec"]["data"]["n"] for e in got] == [50, 51, 52, 53]


def test_a_stream_open_across_a_rebuild_starts_again_at_the_new_epoch(settings, live_server):
    project, idx = _folder_project(settings)
    bus = Bus(idx, poll_s=0.05)
    for n in range(5):
        bus.publish(None, {"kind": "k", "n": n}, project_id=PID)
    with live_server(bus=bus) as (s, url):
        q = f"{url}/api/v0/events?project_id={PID}&token={s.token}"
        with httpx.Client(timeout=10) as c, c.stream("GET", q) as r:
            reader = SseReader(r)
            assert [reader.event()["seq"] for _ in range(5)] == [1, 2, 3, 4, 5]
            idx.rebuild_from_project(project.root)
            new = bus.epoch(PID)
            for n in range(3):
                bus.publish(None, {"kind": "k", "n": 10 + n}, project_id=PID)
            got = [reader.event() for _ in range(3)]
    assert [(e["epoch"], e["seq"]) for e in got] == [(new, 1), (new, 2), (new, 3)]


def test_the_run_event_stream_ids_carry_the_epoch_and_a_stale_cursor_replays(client, auth):
    r = client.post("/api/v0/projects", json={"project_id": PID, "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text
    idx = client.app.state.index
    with idx.project(PID) as conn:
        with tx(conn):
            apply_packet_entry(conn, PID, "pkt_s", PACKET_ENTRY)
        run = create_run(conn, RunCreate(project_id=PID, packet_id="pkt_s", route=ROUTE))
        quote_run(conn, run.id)
    epoch = idx.epoch(PID)

    def ids(headers: dict) -> list[str]:
        r = client.get(f"/api/v0/events/stream?project_id={PID}", headers=auth | headers)
        assert r.status_code == 200, r.text
        return [line[4:] for line in r.text.splitlines() if line.startswith("id: ")]

    every = ids({})
    assert every and all(i.startswith(f"{epoch}.") for i in every)
    assert ids({"Last-Event-ID": every[-1]}) == []  # nothing after the newest
    assert ids({"Last-Event-ID": f"01STALEEPOCH.{every[-1].rpartition('.')[2]}"}) == every
    assert ids({"Last-Event-ID": "999"}) == every  # a bare id above the head


# ---------------------------------------------------------------------------- fold by event seq


def _rec(kind: str, state: str, seq: int | None = None, **extra) -> dict:
    data = {"state": state, "packet_id": "pkt_s", **({"event_seq": seq} if seq is not None else {})}
    return {"run_id": "run_x", "kind": kind, "at": "2026-10-18T09:00:00Z", **extra, "data": data}


def test_fold_takes_the_state_of_the_highest_event_seq_and_terminal_states_win():
    raced = [_rec("quoted", "QUOTED"), _rec("cancelled", "CANCELLED", 9), _rec("approved", "APPROVED", 8)]
    assert fold_run_records(raced)["run_x"]["state"] is RunState.CANCELLED
    # Without seqs (records from before T145): file order, but a terminal state is never undone.
    old = [_rec("quoted", "QUOTED"), _rec("cancelled", "CANCELLED"), _rec("approved", "APPROVED")]
    assert fold_run_records(old)["run_x"]["state"] is RunState.CANCELLED
    # Non-terminal records out of order: the highest seq wins.
    swapped = [_rec("quoted", "QUOTED", 1), _rec("queued", "QUEUED", 3), _rec("approved", "APPROVED", 2)]
    assert fold_run_records(swapped)["run_x"]["state"] is RunState.QUEUED
    # A tie goes in file order.
    tie = [_rec("quoted", "QUOTED", 4), _rec("approved", "APPROVED", 4)]
    assert fold_run_records(tie)["run_x"]["state"] is RunState.APPROVED
    # The submitted record that raced after the cancel carried a stale state: still CANCELLED.
    stale = [
        _rec("quoted", "QUOTED", 1),
        _rec("approved", "APPROVED", 2),
        _rec("cancelled", "CANCELLED", 5),
        _rec("submitted", "SUBMITTED", 4, provider_job_id="job-1"),
    ]
    run = fold_run_records(stale)["run_x"]
    assert run["state"] is RunState.CANCELLED and run["provider_job_id"] == "job-1"


def test_a_repeated_kind_and_state_folds_once():
    first = _rec("cancelled", "CANCELLED", 9, reason="by the artist")
    again = {
        **_rec("cancelled", "CANCELLED", 9, reason="the race's second append"),
        "at": "2026-10-18T09:00:05Z",
    }
    run = fold_run_records([_rec("quoted", "QUOTED"), first, again])["run_x"]
    assert run["state"] is RunState.CANCELLED
    assert run["updated_utc"] == "2026-10-18T09:00:00Z"  # the duplicate was not folded


def test_a_rebuild_of_a_raced_ledger_agrees_with_the_live_state(tmp_path, settings):
    project = Project.create(tmp_path / "nas" / PID, PID, {"nda": False})
    project.register_packet("pkt_s", PACKET_ENTRY)
    ledger = Ledger(project.root)
    route = ROUTE.model_dump()
    ledger.append(
        {
            "run_id": "run_x",
            "kind": "quoted",
            "data": {"state": "QUOTED", "packet_id": "pkt_s", "route": route},
        }
    )
    ledger.append({"run_id": "run_x", "kind": "cancelled", "data": {"state": "CANCELLED", "event_seq": 9}})
    ledger.append({"run_id": "run_x", "kind": "approved", "data": {"state": "APPROVED", "event_seq": 8}})
    ledger.append({"run_id": "run_x", "kind": "cancelled", "data": {"state": "CANCELLED", "event_seq": 9}})
    out = Index(settings.home).rebuild_from_project(project.root)
    assert out["problems"] == [] and out["runs"] == 1
    with Index(settings.home).project(PID) as conn:
        assert conn.execute("SELECT state FROM runs WHERE id = 'run_x'").fetchone()[0] == "CANCELLED"


def test_a_failed_rebuild_of_a_new_index_leaves_no_file(tmp_path, settings, monkeypatch):
    project = Project.create(tmp_path / "nas" / PID, PID, {"nda": False})
    idx = Index(settings.home)

    def boom(*_a, **_k):
        raise RuntimeError("replay broke")

    monkeypatch.setattr("plate_daemon.index.fold_run_records", boom)
    with pytest.raises(RuntimeError):
        idx.rebuild_from_project(project.root)
    assert not index_db_path(PID, settings.home).exists() and idx.project_ids() == []


# ---------------------------------------------------------------------------- boot rebuilds a lost index


def test_boot_rebuilds_only_folders_with_a_ledger_and_no_index(settings, caplog):
    pdir = settings.projects_dir
    lost = Project.create(pdir / "lost", "lost", {"nda": False})
    lost.runs_jsonl.write_text("", encoding="utf-8", newline="\n")  # an empty ledger is still a ledger
    Project.create(pdir / "fresh", "fresh", {"nda": False})  # no ledger: nothing to rebuild from
    kept, idx = _folder_project(settings, "kept")
    kept.runs_jsonl.write_text("", encoding="utf-8", newline="\n")
    kept_epoch = idx.epoch("kept")
    broken = pdir / "broken"
    broken.mkdir()
    (broken / "project.json").write_text('{"project_id": "bro', encoding="utf-8", newline="\n")  # truncated
    (broken / "runs.jsonl").write_text("", encoding="utf-8", newline="\n")
    with caplog.at_level(logging.WARNING, logger="plate_daemon"):
        assert daemon_main.rebuild_missing_indexes(settings, idx) == ["lost"]
    assert idx.project_ids() == ["kept", "lost"]
    assert idx.epoch("kept") == kept_epoch  # an index that exists is never rebuilt at boot
    assert "index for lost was missing: rebuilt from the project folder" in caplog.text
    assert "broken" in caplog.text  # logged and skipped, never fatal
    assert daemon_main.rebuild_missing_indexes(settings, idx) == []  # idempotent


@needs_room_a
def test_a_daemon_started_with_its_index_deleted_rebuilds_it_and_serves(
    plate_env, monkeypatch, caplog, capsys
):
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.setenv("PLATE_NIGHTLY", "0")
    monkeypatch.setenv("PLATE_RUN_WORKERS", "0")
    settings = Settings.load()
    auth = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, FakeEngine()), base_url=BASE) as c:
        c.app.state.pipeline.sleep = lambda _s: None
        r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth)
        assert r.status_code == 201, r.text
        manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
        hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
        body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
        r = c.post("/api/v0/packets", json=body, headers=hdr)
        assert r.status_code == 202, r.text
        packet_id = r.json()["packet_id"]
        graph = {
            "nodes": [
                {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
                {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
            ],
            "edges": [{"id": "p->e", "source": "p", "target": "e"}],
        }
        assert c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=auth).status_code == 200
        r = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth)
        assert r.status_code == 200, r.text
        quoted = r.json()["run"]["id"]
        before = {x["id"]: x["state"] for x in c.get(f"/api/v0/projects/{PROJECT}/runs", headers=auth).json()}
    assert before == {quoted: "QUOTED"}

    db = index_db_path(PROJECT, settings.home)
    _delete_index(db)
    assert Index(settings.home).project_ids() == []

    seen: dict = {}

    class Serve:  # stands in for uvicorn.Server: "serving" is a client on the app main() built
        def __init__(self, config):
            self.app = config.app

        def run(self, sockets=None):
            with TestClient(self.app, base_url=BASE) as c2:
                seen["health"] = c2.get("/api/v0/health")
                seen["runs"] = c2.get(f"/api/v0/projects/{PROJECT}/runs", headers=auth)
                seen["run"] = c2.get(f"/api/v0/runs/{quoted}", headers=auth)
                seen["quote"] = c2.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth)

    import uvicorn

    monkeypatch.setattr(uvicorn, "Server", Serve)
    monkeypatch.setattr(daemon_main, "bind_sockets", lambda hosts, port: [])
    with caplog.at_level(logging.WARNING, logger="plate_daemon"):
        assert daemon_main.main() == 0
    capsys.readouterr()
    assert f"index for {PROJECT} was missing: rebuilt from the project folder" in caplog.text
    assert db.is_file()
    assert seen["health"].status_code == 200 and seen["health"].json()["ok"] is True
    assert seen["runs"].status_code == 200, seen["runs"].text
    assert {x["id"]: x["state"] for x in seen["runs"].json()} == before
    assert seen["run"].status_code == 200 and seen["run"].json()["state"] == "QUOTED"
    assert seen["quote"].status_code == 200, seen["quote"].text  # the packet is back: no unknown_packet
    assert seen["quote"].json()["run"]["id"] != quoted
