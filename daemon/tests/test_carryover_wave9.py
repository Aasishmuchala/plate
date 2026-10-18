"""Carry-over on supersede, the Plate editor's route, and the plugin status (router plan T96).

The world is the one `test_projects_wave8.py` builds: `fixtures/packets/room_a` ingested, an Edit run made by an
engine that echoes the PNG it was sent, and the take accepted after a logged full scrub. Then `room_a_v2` (the same
camera with one wall recoloured, `supersedes` room_a) arrives. `carryover.on_supersede` carries the accepted layer
onto it through `revisions.carry_over`; the route `POST /packets/{id}/carry-over` is the same call from the
button; `GET /plugin/status` and the `receive` job are the dock-facing half.
"""

from __future__ import annotations

import base64
import copy
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from plate_daemon import carryover
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.ledger import Ledger
from plate_daemon.machine import annotate, get_run
from plate_daemon.projects import Project
from plate_daemon.states import RunState

ROOT = Path(__file__).resolve().parents[2]
PACKETS = ROOT / "fixtures" / "packets"
ROOM_A, ROOM_A_V2, ROOM_B = (PACKETS / n for n in ("room_a", "room_a_v2", "room_b"))
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # the fixture manifests' project_id
SHRUB = "o:55cbe981"

pytestmark = pytest.mark.skipif(
    not all(
        (p / "manifest.json").is_file() and (p / "beauty.exr").is_file() for p in (ROOM_A, ROOM_A_V2, ROOM_B)
    ),
    reason="fixtures/packets/room_a, room_a_v2, room_b not built yet (T2)",
)


class EchoEngine(FakeEngine):
    """A FakeEngine that hands back the PNG it was sent."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent: dict[str, bytes] = {}

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        self.sent[job] = base64.b64decode(payload["inputs"]["image"]["b64"])
        return job

    def fetch(self, result_url):
        super().fetch(result_url)
        return self.sent[result_url[len(RESULT_SCHEME) :]]


class Bus:
    """Records what carry-over publishes."""

    def __init__(self) -> None:
        self.events: list[tuple[str | None, dict[str, Any]]] = []

    def publish(self, run_id: str | None, event: dict[str, Any], *, project_id: str | None = None) -> int:
        assert project_id == PROJECT
        self.events.append((run_id, event))
        return len(self.events)

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [e for _, e in self.events if e["kind"] == kind]


class View:
    """A PacketView whose legend is replaced (everything else is the real packet)."""

    def __init__(self, base: Any, legend: dict):
        self._base, self._legend = base, legend

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    @property
    def legend(self) -> dict:
        return self._legend


@dataclass
class World:
    client: TestClient
    auth: dict[str, str]
    settings: Settings
    old: str  # room_a
    new: str  # room_a_v2
    run_id: str  # the accepted Edit run on room_a

    @property
    def pipeline(self) -> Any:
        return self.client.app.state.pipeline

    def project(self) -> Project:
        project = self.pipeline.load_project(PROJECT)
        assert project is not None
        return project

    def carry(self, bus: Any = None, new_pv: Any = None, **kw) -> list[dict]:
        pv = new_pv or self.pipeline.packet(PROJECT, self.new)
        return carryover.on_supersede(self.pipeline, self.project(), pv, self.old, bus=bus, **kw)

    def runs(self, packet_id: str) -> list[Any]:
        with self.pipeline.index.project(PROJECT) as conn:
            ids = [
                r[0]
                for r in conn.execute("SELECT id FROM runs WHERE packet_id = ? ORDER BY id", (packet_id,))
            ]
            return [get_run(conn, i) for i in ids]


def _manifest(path: Path) -> dict:
    return json.loads((path / "manifest.json").read_text(encoding="utf-8"))


def _ingest(c: TestClient, auth: dict, path: Path) -> str:
    hdr = {**auth, "X-Plate-Packet-Version": _manifest(path)["packet_version"]}
    r = c.post(
        "/api/v0/packets",
        json={"packet_path": str(path), "sender": {"name": "t", "version": "0"}},
        headers=hdr,
    )
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    assert c.get(f"/api/v0/packets/{packet_id}", headers=auth).json()["status"] == "ingested"
    return packet_id


def _make_and_accept(c: TestClient, auth: dict, *, accept: bool = True) -> str:
    q = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth)
    assert q.status_code == 200, q.text
    run_id = q.json()["run"]["id"]
    assert c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=auth).status_code == 200
    assert c.get(f"/api/v0/runs/{run_id}", headers=auth).json()["state"] == "DONE"
    if accept:
        r = c.post(
            f"/api/v0/runs/{run_id}/scrub", json={"quadrants_visited": 4, "zoom_100": True}, headers=auth
        )
        assert r.status_code == 200 and r.json()["full_scrub"] is True, r.text
        r = c.post(
            f"/api/v0/runs/{run_id}/accept", json={"scrub_log_id": r.json()["scrub_log_id"]}, headers=auth
        )
        assert r.status_code == 200, r.text
    return run_id


def _world(accept: bool):
    settings = Settings.load()
    auth = {"Authorization": f"Bearer {settings.token}"}
    with TestClient(create_app(settings, EchoEngine()), base_url=BASE) as c:
        c.app.state.pipeline.sleep = lambda _s: None
        r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth)
        assert r.status_code == 201, r.text
        old = _ingest(c, auth, ROOM_A)
        graph = {
            "nodes": [
                {"id": "p", "kind": "Plate", "packetId": old, "tier": "A", "label": "Plate"},
                {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
            ],
            "edges": [{"id": "p->e", "source": "p", "target": "e"}],
        }
        assert c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=auth).status_code == 200
        run_id = _make_and_accept(c, auth, accept=accept)
        new = _ingest(
            c, auth, ROOM_A_V2
        )  # the ingest hook marks room_a's runs superseded and re-binds the Plate
        yield World(c, auth, settings, old, new, run_id)


@pytest.fixture
def world(plate_env, monkeypatch):
    """room_a with one accepted Edit run, room_a_v2 ingested on top."""
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.setenv("PLATE_AUTO_CARRY_OVER", "0")  # T107 carries at ingest; these call it by hand
    yield from _world(accept=True)


@pytest.fixture
def world_unaccepted(plate_env, monkeypatch):
    """The same, but the artist never accepted the take."""
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.setenv("PLATE_AUTO_CARRY_OVER", "0")
    yield from _world(accept=False)


# ---------------------------------------------------------------------------- the accepted layers of a packet


def test_approved_layers_are_the_accepted_done_runs_with_their_mask_ids_and_composite(world):
    unaccepted = _make_and_accept(world.client, world.auth, accept=False)
    with world.pipeline.index.project(PROJECT) as conn:
        run = get_run(conn, world.run_id)
        layers = carryover.approved_layers(conn, world.project(), world.old)
        assert carryover.approved_layers(conn, None, world.old) == layers
        assert carryover.approved_layers(conn, world.project(), world.new) == []
    assert unaccepted != world.run_id and run.state is RunState.DONE
    assert layers == [
        {
            "run_id": world.run_id,
            "mask_hash": run.normalized["mask_key"],
            "ids": run.inputs["mask"]["ids"],
            "rgb16_hash": run.outputs[0],
        }
    ]
    assert layers[0]["ids"] and SHRUB in layers[0]["ids"]  # the packet's editable ids, by Cryptomatte id


def test_the_union_of_a_chains_masks_beats_the_runs_own_mask(world):
    union = "ab" * 32
    with world.pipeline.index.project(PROJECT) as conn:
        norm = get_run(conn, world.run_id).normalized
        annotate(conn, world.run_id, expect=RunState.DONE, normalized={**norm, "mask_union_key": union})
        [layer] = carryover.approved_layers(conn, world.project(), world.old)
    assert layer["mask_hash"] == union


# ---------------------------------------------------------------------------- on_supersede


def test_on_supersede_carries_the_accepted_layer_onto_the_new_packet(world):
    bus = Bus()
    results = world.carry(bus)

    [res] = results
    assert res["run_id"] == world.run_id and res["status"] == "carried" and res["iou"] == pytest.approx(1.0)
    assert res["gates"]["verdict"] == "pass"
    json.dumps(results)  # plain JSON: the route and the event carry it as it is

    [new_run] = world.runs(world.new)  # a new run on the new packet, the artist still has to accept it
    assert res["new_run_id"] == new_run.id and new_run.state is RunState.DONE
    assert new_run.inputs["carried_from"] == world.run_id
    assert new_run.route.engine_id == "carry-over" and new_run.outputs == [res["output"]]
    with world.pipeline.index.project(PROJECT) as conn:
        assert carryover.approved_layers(conn, world.project(), world.new) == []  # not accepted for them

    done = [
        r for r in Ledger(world.project().root).records() if r["kind"] == "done" and r["run_id"] == new_run.id
    ]
    assert [r["data"]["carried_from"] for r in done] == [world.run_id]  # the folder holds it first

    [event] = bus.of("packet.carried")
    assert event["packet_id"] == world.new and event["supersedes"] == world.old
    assert (event["carried"], event["dropped"]) == (1, 0) and event["results"] == results
    assert [e["kind"] for _, e in bus.events] == ["run.carried", "packet.carried"]


def test_carrying_twice_writes_nothing_new(world):
    first = world.carry()
    again = world.carry()
    assert again[0]["code"] == "already_carried" and again[0]["new_run_id"] == first[0]["new_run_id"]
    assert len(world.runs(world.new)) == 1


def test_a_layer_whose_ids_changed_is_dropped_with_the_reason_and_counted(world):
    new_pv = world.pipeline.packet(PROJECT, world.new)
    legend = copy.deepcopy(new_pv.legend)
    entry = next(e for e in legend["entries"] if e["id"] == SHRUB)
    entry["name"] = "VEG_shrub_renamed"
    bus = Bus()
    [res] = world.carry(bus, new_pv=View(new_pv, legend))

    assert (
        res["status"] == "dropped"
        and res["code"] == "legend_changed"
        and res["reason"] == "legend entry changed"
    )
    assert res["new_run_id"] is None and world.runs(world.new) == []
    [event] = bus.of("packet.carried")
    assert (event["carried"], event["dropped"]) == (0, 1)
    assert [e["code"] for e in bus.of("layer.dropped")] == ["legend_changed"]


def test_with_nothing_accepted_nothing_is_carried_but_the_record_is_still_published(world_unaccepted):
    world = world_unaccepted
    bus = Bus()
    assert world.carry(bus) == []
    [event] = bus.of("packet.carried")
    assert (event["carried"], event["dropped"], event["results"]) == (0, 0, [])
    assert world.runs(world.new) == []
    assert _post(world, world.new).json() == []


def test_an_unknown_old_packet_is_a_pipeline_error(world):
    from plate_daemon.pipeline import PipelineError

    with pytest.raises(PipelineError):
        carryover.on_supersede(
            world.pipeline, world.project(), world.pipeline.packet(PROJECT, world.new), "pkt_nope"
        )


# ---------------------------------------------------------------------------- the switch


def _project_with(settings_doc: dict[str, Any]) -> Project:
    doc = {
        "project_id": PROJECT,
        "custody": {"nda": False, "allowed_grades": ["C0"]},
        "settings": settings_doc,
    }
    return Project(root=Path("."), doc=doc)


@pytest.mark.parametrize(
    ("settings_doc", "env", "expected"),
    [
        ({}, {}, True),  # default on
        ({"auto_carry_over": True}, {}, True),
        ({"auto_carry_over": False}, {}, False),
        ({"auto_carry_over": "off"}, {}, False),
        ({}, {"PLATE_AUTO_CARRY_OVER": "0"}, False),
        ({"auto_carry_over": True}, {"PLATE_AUTO_CARRY_OVER": "false"}, False),
        ({}, {"PLATE_AUTO_CARRY_OVER": "1"}, True),
        ({"auto_carry_over": False}, {"PLATE_AUTO_CARRY_OVER": "1"}, False),  # the env only ever turns it off
    ],
)
def test_auto_enabled_reads_the_project_setting_and_the_env_switch(settings_doc, env, expected):
    assert carryover.auto_enabled(_project_with(settings_doc), env) is expected


def test_auto_enabled_reads_the_process_environment_by_default(monkeypatch):
    project = _project_with({})
    monkeypatch.setenv("PLATE_AUTO_CARRY_OVER", "0")
    assert carryover.auto_enabled(project) is False
    monkeypatch.delenv("PLATE_AUTO_CARRY_OVER")
    assert carryover.auto_enabled(project) is True


# ---------------------------------------------------------------------------- POST /packets/{id}/carry-over


def _post(w: World, packet_id: str, body: dict | None = None, **headers):
    return w.client.post(f"/api/v0/packets/{packet_id}/carry-over", json=body, headers={**w.auth, **headers})


def test_the_route_answers_the_results_defaulting_to_the_manifests_supersedes(world):
    before = world.pipeline.last_human_utc
    r = _post(world, world.new)
    assert r.status_code == 200, r.text
    [res] = r.json()
    assert res["run_id"] == world.run_id and res["status"] == "carried" and res["new_run_id"]
    assert world.pipeline.last_human_utc == before  # curl is not a human: the unattended clock did not move

    world.client.app.state.clock = lambda: datetime(2031, 1, 1, tzinfo=UTC)
    again = _post(world, world.new, {"from_packet_id": world.old}, **{"X-Plate-Client": "ui"})
    assert again.status_code == 200 and again.json()[0]["code"] == "already_carried"
    assert world.pipeline.last_human_utc == datetime(2031, 1, 1, tzinfo=UTC)  # the UI's button is
    assert len(world.runs(world.new)) == 1


def test_the_route_refuses_what_the_summary_route_refuses(world):
    c, auth = world.client, world.auth
    assert c.post(f"/api/v0/packets/{world.new}/carry-over").status_code == 401
    assert _post(world, "pkt_nope").status_code == 404
    # room_a supersedes nothing: with no from_packet_id there is nothing to carry from
    r = _post(world, world.old)
    assert r.status_code == 409 and r.json()["error"] == "no_predecessor"
    # a predecessor the project does not hold
    r = _post(world, world.new, {"from_packet_id": "pkt_nope"})
    assert r.status_code == 404 and r.json()["packet_id"] == "pkt_nope"
    assert _post(world, world.new, {"from_packet_id": world.new}).status_code == 422
    assert _post(world, world.new, {"surprise": 1}).status_code == 422
    # a packet that is not ingested yet (either side)
    with world.pipeline.index.project(PROJECT) as conn:
        conn.execute("UPDATE packets SET status = 'ingesting' WHERE packet_id = ?", (world.new,))
        conn.commit()
    r = _post(world, world.new)
    assert r.status_code == 409 and r.json() == {
        "error": "not_ingested",
        "packet_id": world.new,
        "status": "ingesting",
    }
    with world.pipeline.index.project(PROJECT) as conn:
        conn.execute("UPDATE packets SET status = 'ingested' WHERE packet_id = ?", (world.new,))
        conn.execute("UPDATE packets SET status = 'failed' WHERE packet_id = ?", (world.old,))
        conn.commit()
    r = _post(world, world.new)
    assert r.status_code == 409 and r.json()["packet_id"] == world.old
    assert c.get(f"/api/v0/packets/{world.new}", headers=auth).status_code == 200


# ---------------------------------------------------------------------------- GET /plugin/status and the receive job


def _status(w: World) -> dict:
    r = w.client.get("/api/v0/plugin/status", headers=w.auth)
    assert r.status_code == 200, r.text
    return r.json()


def test_plugin_status_stamps_every_claiming_poll_and_counts_the_waiting_jobs(world):
    c, auth = world.client, world.auth
    assert c.get("/api/v0/plugin/status").status_code == 401
    assert _status(world) == {"last_poll_utc": None, "queued": 0, "claimed": 0, "expired_claims": 0}

    r = c.post("/api/v0/plugin/jobs", json={"action": "receive", "run_id": world.run_id}, headers=auth)
    assert r.status_code == 201, r.text
    assert _status(world) == {"last_poll_utc": None, "queued": 1, "claimed": 0, "expired_claims": 0}

    c.get("/api/v0/plugin/jobs?since=0", headers=auth)  # a look without claiming is not a dock
    assert _status(world)["last_poll_utc"] is None

    jobs = c.get("/api/v0/plugin/jobs?since=0&claim=1", headers=auth).json()["jobs"]
    assert [j["id"] for j in jobs] == [r.json()["id"]]
    status = _status(world)
    assert status["queued"] == 0 and status["claimed"] == 1 and isinstance(status["last_poll_utc"], str)

    first = status["last_poll_utc"]
    c.app.state.clock = lambda: datetime(2030, 1, 1, tzinfo=UTC)
    c.get(f"/api/v0/plugin/jobs?since={jobs[0]['seq']}&claim=1", headers=auth)  # an empty claim still counts
    assert _status(world)["last_poll_utc"] != first and _status(world)["last_poll_utc"].startswith(
        "2030-01-01"
    )


def test_a_receive_job_carries_the_run_the_mode_and_the_project_to_the_dock(world):
    c, auth = world.client, world.auth
    r = c.post(
        "/api/v0/plugin/jobs",
        json={"action": "receive", "run_id": world.run_id, "mode": "viewport_background"},
        headers=auth,
    )
    assert r.status_code == 201 and r.json()["state"] == "queued"
    [job] = c.get("/api/v0/plugin/jobs?since=0&claim=1", headers=auth).json()["jobs"]
    assert job["action"] == "receive" and job["run_id"] == world.run_id
    assert (
        job["mode"] == "viewport_background"
        and job["project_id"] == PROJECT
        and job["packet_id"] == world.old
    )
    # the dock answers {ok, detail}
    r = c.post(f"/api/v0/plugin/jobs/{job['id']}/result", json={"ok": True, "detail": "shown"}, headers=auth)
    assert r.status_code == 200 and r.json()["state"] == "done"

    default = c.post("/api/v0/plugin/jobs", json={"action": "receive", "run_id": world.run_id}, headers=auth)
    assert c.get(f"/api/v0/plugin/jobs/{default.json()['id']}", headers=auth).json()["mode"] == "vfb_history"


def test_a_receive_job_is_refused_without_a_run_with_a_take(world):
    c, auth = world.client, world.auth

    def post(**body):
        return c.post("/api/v0/plugin/jobs", json={"action": "receive", **body}, headers=auth)

    assert post().status_code == 422
    assert post(run_id="").status_code == 422
    assert post(run_id=world.run_id, mode="screen").status_code == 422
    assert post(run_id=world.run_id, packet_id=world.new).status_code == 422  # the run is on another packet
    r = post(run_id="run_nope")
    assert r.status_code == 404 and r.json()["run_id"] == "run_nope"

    q = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth)  # QUOTED: no take yet
    r = post(run_id=q.json()["run"]["id"])
    assert r.status_code == 409 and r.json()["error"] == "no_output"
    # every other action still names its packet
    assert c.post("/api/v0/plugin/jobs", json={"action": "scout"}, headers=auth).status_code == 422
    assert _status(world)["queued"] == 0
