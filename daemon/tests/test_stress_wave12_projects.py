"""Wave 12 stress fixes on the project routes (router plan T128, decisions 77, 79, 80).

Each test encodes an attacker's repro from the stress test: a client-sent `custody.unset` that re-opened a set
project's custody, two agents' read-modify-write saves losing one another's nodes, a storm of big PUTs that
answered `project_locked` naming the daemon's own pid, a node id that was an object (500), an unbounded graph,
and quote bodies the route let through to the pipeline (`priority: "high"`, `priority: 0` read as interactive,
an attempt of 2**63-1).
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.ledger import Ledger
from plate_daemon.projects import Lock
from plate_daemon.routes import projects as project_routes

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
API = "/api/v0"

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    return Settings.load()


@pytest.fixture
def c(sync_settings):
    with TestClient(create_app(sync_settings), base_url=BASE) as client:
        client.app.state.pipeline.sleep = lambda _s: None
        yield client


def _create(c: TestClient, auth: dict, project_id: str = PROJECT, **custody):
    return c.post(f"{API}/projects", json={"project_id": project_id, "custody": custody}, headers=auth)


def _doc(settings: Settings, project_id: str = PROJECT) -> dict:
    return json.loads((settings.projects_dir / project_id / "project.json").read_text(encoding="utf-8"))


def _packet(c: TestClient, auth: dict) -> str:
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
    r = c.post(f"{API}/packets", json=body, headers=hdr)
    assert r.status_code == 202, r.text
    return r.json()["packet_id"]


def _put(c: TestClient, auth: dict, graph: dict, project_id: str = PROJECT):
    return c.put(f"{API}/projects/{project_id}/graph", json=graph, headers=auth)


def _small_graph() -> dict:
    return {
        "nodes": [
            {"id": "p", "kind": "Plate", "packetId": "pkt_x", "tier": "A"},
            {"id": "e", "kind": "Edit"},
        ],
        "edges": [{"id": "p->e", "source": "p", "target": "e"}],
    }


def _big_graph(plates: int = 100, colours: int = 400, edges: int = 600) -> dict:
    """500 nodes and 600 edges: every Colour hangs off one or two Plates (a wide graph, cheap to validate)."""
    nodes = [{"id": f"p{i}", "kind": "Plate", "packetId": "pkt_x", "tier": "A"} for i in range(plates)]
    nodes += [
        {"id": f"c{i}", "kind": "Colour", "label": f"grade {i}", "position": {"x": 300, "y": 40 * i}}
        for i in range(colours)
    ]
    out = []
    for k in range(edges):
        col = k % colours
        plate = (k // colours + col) % plates
        out.append({"id": f"p{plate}->c{col}", "source": f"p{plate}", "target": f"c{col}"})
    return {"nodes": nodes, "edges": out}


# ---------------------------------------------------------------------------- custody `unset` is the server's


@pytest.mark.parametrize("mark", [True, 1])
def test_a_client_sent_unset_is_dropped_and_never_reopens_the_custody(c, auth, sync_settings, mark):
    r = _create(c, auth, nda=True, unset=mark)
    assert r.status_code == 201, r.text
    assert r.json()["custody"] == {"nda": True, "allowed_grades": ["C0", "C1"]}
    assert "unset" not in _doc(sync_settings)["custody"]

    # The repro: a plain re-POST used to fill in (and loosen) the custody of the "unset" project.
    again = _create(c, auth, nda=False)
    assert again.status_code == 409 and again.json()["error"] == "exists", again.text
    assert _doc(sync_settings)["custody"] == {"nda": True, "allowed_grades": ["C0", "C1"]}
    assert [
        x for x in Ledger(sync_settings.projects_dir / PROJECT).records() if x["kind"] == "override"
    ] == []


def test_an_unset_mark_in_a_custody_edit_is_dropped_too(c, auth, sync_settings):
    assert _create(c, auth, nda=False, allowed_grades=["C0"]).status_code == 201
    r = c.put(f"{API}/projects/{PROJECT}/custody", json={"nda": True, "allowed_grades": ["C0"]}, headers=auth)
    assert r.status_code == 200, r.text
    assert "unset" not in r.json()["custody"]
    assert _create(c, auth, nda=False).status_code == 409


@needs_room_a
def test_a_packet_first_project_takes_its_custody_once_and_the_fill_in_is_ledgered(c, auth, sync_settings):
    _packet(c, auth)  # the packet arrives before its project: the server marks the custody unset
    listed = {p["project_id"]: p for p in c.get(f"{API}/projects", headers=auth).json()}
    assert listed[PROJECT]["custody"]["unset"] is True

    r = _create(c, auth, nda=True)
    assert r.status_code == 200, r.text
    assert r.json()["custody"] == {"nda": True, "allowed_grades": ["C0", "C1"]}
    (rec,) = [x for x in Ledger(sync_settings.projects_dir / PROJECT).records() if x["kind"] == "override"]
    assert rec["by"] == "human" and rec["reason"] == project_routes.CUSTODY_FILL_REASON
    assert rec["data"]["what"] == "custody"
    assert rec["data"]["from"]["unset"] is True and rec["data"]["to"] == {
        "nda": True,
        "allowed_grades": ["C0", "C1"],
    }
    assert "unset" not in _doc(sync_settings)["custody"]

    # Filled once: the next POST is a project that exists.
    assert _create(c, auth, nda=False).status_code == 409


@needs_room_a
def test_a_fill_in_the_ledger_cannot_hold_does_not_happen(c, auth, sync_settings, monkeypatch):
    _packet(c, auth)

    def broken(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Ledger, "append", broken)
    r = _create(c, auth, nda=True)
    assert r.status_code == 503 and r.json()["error"] == "ledger_unavailable", r.text
    assert _doc(sync_settings)["custody"]["unset"] is True  # still the server's C0-only placeholder


# ---------------------------------------------------------------------------- the graph revision


def test_a_put_on_a_stale_revision_is_a_conflict_and_an_old_client_still_saves(c, auth, sync_settings):
    assert _create(c, auth, nda=False).status_code == 201
    got = c.get(f"{API}/projects/{PROJECT}/graph", headers=auth).json()
    assert got == {"nodes": [], "edges": [], "revision": 0}

    r = _put(c, auth, {**_small_graph(), "revision": 0})
    assert r.status_code == 200, r.text
    assert r.json()["graph"]["revision"] == 1
    assert "revision" not in _doc(sync_settings)["graph"] and _doc(sync_settings)["graph_revision"] == 1

    # The repro: a second tab saving what it read at revision 0 silently overwrote the first tab's save.
    r = _put(c, auth, {"nodes": [], "edges": [], "revision": 0})
    assert r.status_code == 409, r.text
    assert r.json() == {"error": "graph_conflict", "current": 1, "sent": 0}
    assert len(_doc(sync_settings)["graph"]["nodes"]) == 2  # nothing written

    r = _put(c, auth, _small_graph())  # no revision: an old client, accepted
    assert r.status_code == 200 and r.json()["graph"]["revision"] == 2
    assert c.get(f"{API}/projects/{PROJECT}/graph", headers=auth).json()["revision"] == 2
    (head,) = c.get(f"{API}/projects", headers=auth).json()
    assert head["graph_revision"] == 2

    for bad in ("1", 1.5, True, -1):
        r = _put(c, auth, {**_small_graph(), "revision": bad})
        assert r.status_code == 422 and r.json()["error"] == "bad_revision", (bad, r.text)


# ---------------------------------------------------------------------------- the append-node endpoint


def test_twelve_concurrent_appends_each_get_their_own_node(c, auth, sync_settings):
    assert _create(c, auth, nda=False).status_code == 201
    start = threading.Barrier(12)

    def add(_i: int):
        start.wait()
        return c.post(f"{API}/projects/{PROJECT}/nodes", json={"kind": "Colour"}, headers=auth)

    with ThreadPoolExecutor(max_workers=12) as pool:
        answers = list(pool.map(add, range(12)))
    assert [r.status_code for r in answers] == [201] * 12, [r.text for r in answers if r.status_code != 201]
    ids = [r.json()["node_id"] for r in answers]
    assert len(set(ids)) == 12 and set(ids) == {f"colour-{n}" for n in range(1, 13)}
    stored = _doc(sync_settings)
    assert sorted(n["id"] for n in stored["graph"]["nodes"] if n["kind"] == "Colour") == sorted(ids)
    assert stored["graph_revision"] == 12
    assert sorted(r.json()["revision"] for r in answers) == list(range(1, 13))


def test_an_append_connects_places_and_answers_like_a_put(c, auth, sync_settings):
    assert _create(c, auth, nda=False).status_code == 201
    assert _put(c, auth, _small_graph()).status_code == 200

    r = c.post(
        f"{API}/projects/{PROJECT}/nodes", json={"kind": "Colour", "connect_from": ["p"]}, headers=auth
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["node_id"] == "colour-1" and body["revision"] == 2 and body["graph"]["revision"] == 2
    node = next(n for n in body["graph"]["nodes"] if n["id"] == "colour-1")
    assert node["kind"] == "Colour" and node["label"] == "Colour" and node["params"] == {}
    assert node["position"] == {"x": 0, "y": 280}  # the Plate has no position: under the two nodes
    assert {"id": "p->colour-1", "source": "p", "target": "colour-1"} in body["graph"]["edges"]

    r = c.post(
        f"{API}/projects/{PROJECT}/nodes",
        json={
            "kind": "Plate",
            "label": "hero",
            "params": {"packet_id": "pkt_y", "tier": "A"},
            "position": {"x": 10, "y": 20},
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    plate = next(n for n in r.json()["graph"]["nodes"] if n["id"] == "plate-1")
    assert plate["packetId"] == "pkt_y" and plate["tier"] == "A" and plate["params"] == {}
    assert plate["label"] == "hero" and plate["position"] == {"x": 10, "y": 20}

    for body, error in (
        ({"kind": "TextToImage"}, "unknown_kind"),
        ({"kind": "Colour", "connect_from": ["ghost"]}, "unknown_node"),
    ):
        r = c.post(f"{API}/projects/{PROJECT}/nodes", json=body, headers=auth)
        assert r.status_code == 422 and r.json()["error"] == error, r.text
    for bad in ({"kind": "Colour", "position": {"x": "left", "y": 0}}, {"kind": "Colour", "extra": 1}):
        assert c.post(f"{API}/projects/{PROJECT}/nodes", json=bad, headers=auth).status_code == 422
    assert c.post(f"{API}/projects/ghost/nodes", json={"kind": "Colour"}, headers=auth).status_code == 404
    assert _doc(sync_settings)["graph_revision"] == 3  # nothing refused was stored

    # A rule refusal answers as PUT does (a Concept never reaches delivery), and stores nothing.
    assert (
        c.post(f"{API}/projects/{PROJECT}/nodes", json={"kind": "Concept"}, headers=auth).status_code == 201
    )
    r = c.post(
        f"{API}/projects/{PROJECT}/nodes",
        json={"kind": "Export", "connect_from": ["concept-1"]},
        headers=auth,
    )
    assert r.status_code == 422 and r.json()["violations"][0]["code"] == "CONCEPT_TO_DELIVERY", r.text
    assert _doc(sync_settings)["graph_revision"] == 4
    assert not any(n["kind"] == "Export" for n in _doc(sync_settings)["graph"]["nodes"])


# ---------------------------------------------------------------------------- save backpressure


def test_a_storm_of_big_puts_is_answered_never_500_or_project_locked(c, auth):
    assert _create(c, auth, nda=False).status_code == 201
    graph = _big_graph()
    assert len(graph["nodes"]) == 500 and len(graph["edges"]) == 600
    start = threading.Barrier(21)

    def put(_i: int):
        start.wait()
        return _put(c, auth, json.loads(json.dumps(graph)))

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(put, i) for i in range(20)]
        start.wait()
        time.sleep(0.2)  # mid-storm
        t0 = time.monotonic()
        health = c.get(f"{API}/health")
        health_s = time.monotonic() - t0
        answers = [f.result() for f in futures]
    assert health.status_code == 200 and health_s < 3.0, health_s
    seen = {(r.status_code, r.json().get("error")) for r in answers}
    assert seen <= {(200, None), (409, "graph_busy"), (429, "too_many_saves")}, [
        r.text[:200] for r in answers
    ]
    assert (200, None) in seen
    shed = [r.json() for r in answers if r.status_code == 429]
    assert all(b["retry_after_ms"] == project_routes.RETRY_AFTER_MS for b in shed)


def test_a_full_queue_is_shed_at_once_and_a_long_wait_is_graph_busy(c, auth, monkeypatch):
    assert _create(c, auth, nda=False).status_code == 201
    project = c.app.state.pipeline.load_project(PROJECT)
    key = str(project.root)
    slot = project_routes._SAVE_LOCKS.setdefault(key, threading.Lock())
    monkeypatch.setattr(project_routes, "GRAPH_SAVE_WAIT_S", 0.2)
    with slot:  # another save holds the project
        t0 = time.monotonic()
        r = _put(c, auth, _small_graph())
        assert r.status_code == 409 and r.json()["error"] == "graph_busy", r.text
        assert time.monotonic() - t0 < 3.0
        monkeypatch.setitem(project_routes._SAVE_WAITERS, key, project_routes.GRAPH_MAX_WAITERS)
        r = _put(c, auth, _small_graph())
        assert r.status_code == 429 and r.json() == {"error": "too_many_saves", "retry_after_ms": 500}
        monkeypatch.setitem(project_routes._SAVE_WAITERS, key, 0)
    assert _put(c, auth, _small_graph()).status_code == 200


def test_a_lock_held_in_this_process_is_waited_for_and_never_named_as_another_process(
    c, auth, sync_settings, monkeypatch
):
    assert _create(c, auth, nda=False).status_code == 201
    project_dir = sync_settings.projects_dir / PROJECT
    monkeypatch.setattr(project_routes, "LOCK_TIMEOUT_S", 0.3)
    with Lock(project_dir):  # another handle in this process (a ledger append, a packet registration)
        r = _put(c, auth, _small_graph())
    assert r.status_code == 409 and r.json()["error"] == "project_locked", r.text
    assert r.json()["detail"] == "another save of this project is in progress; retry"
    assert "pid" not in r.json()["detail"]

    # A short hold is waited out.
    lock = Lock(project_dir).acquire()
    threading.Timer(0.1, lock.release).start()
    monkeypatch.setattr(project_routes, "LOCK_TIMEOUT_S", 5.0)
    assert _put(c, auth, _small_graph()).status_code == 200


# ---------------------------------------------------------------------------- graph shape and size


@pytest.mark.parametrize("bad_id", [{}, [], 7, None, ""])
def test_a_node_id_that_is_not_a_string_is_unknown_node_never_500(c, auth, sync_settings, bad_id):
    assert _create(c, auth, nda=False).status_code == 201
    graph = {"nodes": [{"id": bad_id, "kind": "Plate"}], "edges": []}
    r = _put(c, auth, graph)
    assert r.status_code == 422, r.text
    assert [v["code"] for v in r.json()["violations"]] == ["UNKNOWN_NODE"]
    assert r.json()["errors"][0].startswith("UNKNOWN_NODE: ")
    data_form = {"nodes": [{"id": "n", "data": {"kind": ["Plate"]}}], "edges": []}
    assert _put(c, auth, data_form).json()["violations"][0]["code"] == "UNKNOWN_NODE"
    assert _doc(sync_settings)["graph"] == {"nodes": [], "edges": []}


def test_edges_need_string_ends(c, auth):
    assert _create(c, auth, nda=False).status_code == 201
    for edge in ({"source": {}, "target": "e"}, {"source": "p"}, {"from": "p", "to": "e"}):
        graph = {**_small_graph(), "edges": [edge]}
        r = _put(c, auth, graph)
        assert r.status_code == 422 and "string source" in r.json()["errors"][0], r.text


def test_a_graph_over_4_mib_is_graph_too_large(c, auth, sync_settings):
    assert _create(c, auth, nda=False).status_code == 201
    graph = {"nodes": [{"id": "c", "kind": "Colour", "label": "x" * (5 * 1024 * 1024)}], "edges": []}
    r = _put(c, auth, graph)
    assert r.status_code == 422, r.text[:300]
    body = r.json()
    assert body["error"] == "graph_too_large" and body["limit_bytes"] == 4 * 1024 * 1024
    assert body["bytes"] > body["limit_bytes"] and "the limit is 4 MiB" in body["errors"][0]
    assert _doc(sync_settings)["graph"] == {"nodes": [], "edges": []}

    # An append that would push the graph over is refused the same way.
    assert (
        _put(
            c,
            auth,
            {"nodes": [{"id": "c", "kind": "Colour", "label": "x" * (4 * 1024 * 1024 - 200)}], "edges": []},
        ).status_code
        == 200
    )
    r = c.post(f"{API}/projects/{PROJECT}/nodes", json={"kind": "Colour", "label": "y" * 400}, headers=auth)
    assert r.status_code == 422 and r.json()["error"] == "graph_too_large", r.text[:300]


# ---------------------------------------------------------------------------- quote body bounds


@needs_room_a
def test_the_quote_body_is_bounded_at_the_door(c, auth):
    assert _create(c, auth, nda=False).status_code == 201
    packet_id = _packet(c, auth)
    graph = {
        "nodes": [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A"},
            {"id": "e", "kind": "Edit", "params": {"engine": "fake"}},
        ],
        "edges": [{"id": "p->e", "source": "p", "target": "e"}],
    }
    assert _put(c, auth, graph).status_code == 200
    url = f"{API}/projects/{PROJECT}/nodes/e/quote"

    for body in (
        {"priority": "high"},
        {"priority": 7},
        {"priority": True},
        {"attempt": 2**63 - 1},
        {"attempt": 1001},
    ):
        r = c.post(url, json=body, headers=auth)
        assert r.status_code == 422, (body, r.text)

    r = c.post(url, json={"priority": 0}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["run"]["priority"] == "client_deadline"  # 0 is a class, not "absent"
    r = c.post(url, json={"priority": "agent_batch", "attempt": 1000}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["run"]["priority"] == "agent_batch" and r.json()["run"]["attempt"] == 1000
    r = c.post(url, json={}, headers=auth)
    assert r.status_code == 200 and r.json()["run"]["priority"] == "interactive", r.text
