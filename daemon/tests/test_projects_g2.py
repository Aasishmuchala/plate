"""G2 (docs/VERIFICATION.md section 2, router plan T111): a new take on an approved Edit must not lock the graph.

Repro: an Edit whose take was fully scrubbed and accepted, picked as its output (`outputHash`) and wired into a
Motion, gets a second take (a free re-mask of the stored raw take, or a second Make). The node's status used to
come from its newest run, so the Edit flipped to unapproved and every `PUT /projects/{id}/graph` answered
`422 VIDEO_LOCKED`: autosave and every quote in the project failed. Decision 31 as T111 reads it: the node's
status comes from the run behind its picked take; a newer take is a candidate and unapproves nothing. When the
artist picks the new take, the node is unapproved until that take is accepted and the Motion edge locks, but the
save goes through: the edge is stored with `locked: true` and the 200 lists it under `violations`.

Runs go through the real pipeline on `fixtures/packets/room_a` with `Tint`, an echoing FakeEngine that darkens
what it was sent by a factor the test can change, so two Makes (or a take and its re-mask under another feather)
are different pictures.
"""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.engine_fake import FakeEngine

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
SKY = "o:929c9e95"  # editable on room_a
UI = {"X-Plate-Client": "ui"}
MOTION_EDGE = "e->v"

pytestmark = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


class Tint(FakeEngine):
    """The echoing FakeEngine that darkens the image it was sent by `factor` (changeable between Makes)."""

    def __init__(self, *args, factor: float = 0.9, **kwargs):
        kwargs.setdefault("echo", True)
        super().__init__(*args, **kwargs)
        self.factor = factor

    def submit(self, payload, idempotency_key):
        payload = copy.deepcopy(payload)
        data = base64.b64decode(payload["inputs"]["image"]["b64"])
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
        out = np.rint(img.astype(np.float64) * self.factor).astype(img.dtype)
        payload["inputs"]["image"]["b64"] = base64.b64encode(bytes(cv2.imencode(".png", out)[1])).decode(
            "ascii"
        )
        return super().submit(payload, idempotency_key)


# ---------------------------------------------------------------------------- set-up


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.delenv("PLATE_ROUTER_MODE", raising=False)
    return Settings.load()


@pytest.fixture
def engine() -> Tint:
    return Tint()


@pytest.fixture
def c(sync_settings, engine, auth):
    with TestClient(create_app(sync_settings, engine), base_url=BASE) as client:
        client.app.state.pipeline.sleep = lambda _s: None
        r = client.post(
            "/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth
        )
        assert r.status_code == 201, r.text
        yield client


@pytest.fixture
def packet_id(c, auth) -> str:
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
    r = c.post("/api/v0/packets", json=body, headers=hdr)
    assert r.status_code == 202, r.text
    pid = r.json()["packet_id"]
    assert c.get(f"/api/v0/packets/{pid}", headers=auth).json()["status"] == "ingested"
    return pid


def _graph(packet_id: str, *, pick: str | None = None, motion: bool = True, edit: dict | None = None) -> dict:
    """Plate -> Mask (the sky) -> Edit [-> Motion], the Edit carrying `outputHash` when a take is picked."""
    e = {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}, **(edit or {})}
    if pick is not None:
        e["outputHash"] = pick
    nodes = [
        {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
        {"id": "m", "kind": "Mask", "label": "Mask", "params": {"ids": [SKY], "textLock": True}},
        e,
    ]
    edges = [{"id": "p->m", "source": "p", "target": "m"}, {"id": "m->e", "source": "m", "target": "e"}]
    if motion:
        nodes.append({"id": "v", "kind": "Motion", "label": "Motion", "riskLabel": "low"})
        edges.append({"id": MOTION_EDGE, "source": "e", "target": "v"})
    return {"nodes": nodes, "edges": edges}


def _put(c: TestClient, auth: dict, graph: dict):
    return c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers={**auth, **UI})


def _saved(c: TestClient, auth: dict, graph: dict) -> dict:
    r = _put(c, auth, graph)
    assert r.status_code == 200, r.text
    return r.json()


def _stored(settings: Settings) -> dict:
    """The stored graph as the routes answer it: `revision` (project.json `graph_revision`, T128) beside it."""
    doc = json.loads((settings.projects_dir / PROJECT / "project.json").read_text(encoding="utf-8"))
    return {**doc["graph"], "revision": doc.get("graph_revision", 0)}


def _node(graph: dict, node_id: str = "e") -> dict:
    """The node as stored; a default the client did not send (`approved` false, `gateStatus` none) is absent."""
    return next(n for n in graph["nodes"] if n["id"] == node_id)


def _edge(graph: dict, edge_id: str = MOTION_EDGE) -> dict:
    return next(e for e in graph["edges"] if e["id"] == edge_id)


def _quote(c: TestClient, auth: dict, node: str = "e", **body):
    return c.post(f"/api/v0/projects/{PROJECT}/nodes/{node}/quote", json=body, headers={**auth, **UI})


def _make(c: TestClient, auth: dict) -> dict:
    """Quote the Edit as it stands and run it to its end: a take."""
    r = _quote(c, auth)
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]
    r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=auth)
    assert r.status_code == 200, r.text
    run = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
    assert run["state"] == "DONE", run.get("error") or run.get("reason_code")
    return run


def _re_mask(c: TestClient, auth: dict, run_id: str, feather_px: float = 4) -> dict:
    """The free re-mask of the verification session (W1), under another feather than the take's 8 px."""
    r = c.post(f"/api/v0/runs/{run_id}/recomposite", json={"feather_px": feather_px}, headers=auth)
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["state"] == "DONE", run.get("error") or run.get("reason_code")
    return run


def _accept(c: TestClient, auth: dict, run_id: str) -> str:
    r = c.post(f"/api/v0/runs/{run_id}/scrub", json={"quadrants_visited": 4, "zoom_100": True}, headers=auth)
    assert r.status_code == 200 and r.json()["full_scrub"] is True, r.text
    scrub_id = r.json()["scrub_log_id"]
    r = c.post(f"/api/v0/runs/{run_id}/accept", json={"scrub_log_id": scrub_id}, headers=auth)
    assert r.status_code == 200, r.text
    return scrub_id


def _approved_take(c: TestClient, auth: dict, packet_id: str) -> tuple[dict, str]:
    """Rows 1 to 13 and 17 of the verification: a take made, scrubbed, accepted, picked, wired into a Motion."""
    _saved(c, auth, _graph(packet_id, motion=False))
    take = _make(c, auth)
    scrub_id = _accept(c, auth, take["id"])
    body = _saved(c, auth, _graph(packet_id, pick=take["outputs"][0]))
    node = _node(body["graph"])
    assert node["approved"] is True and node["scrubLogId"] == scrub_id and node["gateStatus"] == "pass"
    assert body["violations"] == [] and "locked" not in _edge(body["graph"])
    return take, scrub_id


def _second_take(c: TestClient, auth: dict, engine: Tint, take: dict, how: str) -> dict:
    if how == "re_mask":
        second = _re_mask(c, auth, take["id"])
    else:
        engine.factor = 0.8  # the same Edit made again: another picture
        second = _make(c, auth)
    assert second["id"] != take["id"] and second["outputs"][0] != take["outputs"][0]
    return second


# ---------------------------------------------------------------------------- a newer take is a candidate


@pytest.mark.parametrize("how", ["re_mask", "make"])
def test_a_second_take_after_an_accept_leaves_the_picked_take_approved(c, auth, engine, packet_id, how):
    take, scrub_id = _approved_take(c, auth, packet_id)
    second = _second_take(c, auth, engine, take, how)

    graph = _graph(packet_id, pick=take["outputs"][0])  # the autosave after the new take: nothing re-picked
    body = _saved(c, auth, graph)
    node = _node(body["graph"])
    assert node["approved"] is True and node["gateStatus"] == "pass"
    assert node["scrubLogId"] == scrub_id and node["approvedBy"] == "human"
    assert body["violations"] == [] and "locked" not in _edge(body["graph"])
    assert _stored(c.app.state.settings) == body["graph"]

    # The new take is listed as a candidate of the node all the same.
    runs = c.get(f"/api/v0/projects/{PROJECT}/runs", params={"node_id": "e"}, headers=auth).json()
    assert {take["id"], second["id"]} <= {r["id"] for r in runs}

    # Quoting works again (every quote used to fail with "the graph is not saved"), and a re-roll in flight
    # does not unapprove the picked take either.
    r = _quote(c, auth)
    assert r.status_code == 200, r.text
    node = _node(_saved(c, auth, graph)["graph"])
    assert node["approved"] is True and node["scrubLogId"] == scrub_id


def test_a_best_of_n_group_after_an_accept_leaves_the_picked_take_approved(c, auth, engine, packet_id):
    take, scrub_id = _approved_take(c, auth, packet_id)
    engine.factor = 0.8
    first = _quote(c, auth).json()["run"]["id"]
    sibs = c.post(f"/api/v0/runs/{first}/fanout", json={"n": 2}, headers=auth)
    assert sibs.status_code == 200, sibs.text
    assert c.post(f"/api/v0/runs/{first}/approve", json={"by": "human"}, headers=auth).status_code == 200
    runs = [c.get(f"/api/v0/runs/{s['id']}", headers=auth).json() for s in sibs.json()]
    assert [r["state"] for r in runs] == ["DONE", "DONE"]
    assert all(r["outputs"][0] != take["outputs"][0] for r in runs)
    node = _node(_saved(c, auth, _graph(packet_id, pick=take["outputs"][0]))["graph"])
    assert node["approved"] is True and node["scrubLogId"] == scrub_id


# ---------------------------------------------------------------------------- picking the new take


def test_picking_the_new_take_unapproves_the_node_and_holds_the_motion_edge_locked(c, auth, packet_id):
    take, scrub_id = _approved_take(c, auth, packet_id)
    second = _re_mask(c, auth, take["id"])
    picked = _graph(packet_id, pick=second["outputs"][0])

    body = _saved(c, auth, picked)  # the save goes through ...
    node = _node(body["graph"])
    assert "approved" not in node and node["gateStatus"] == "pass"
    assert "scrubLogId" not in node and "approvedBy" not in node
    # ... with the edge the pick locked flagged and listed
    assert [(v["code"], v["edge"]["id"]) for v in body["violations"]] == [("VIDEO_LOCKED", MOTION_EDGE)]
    assert "is locked until" in body["violations"][0]["message"]
    assert _edge(body["graph"])["locked"] is True
    stored = _stored(c.app.state.settings)
    assert stored == body["graph"] and _edge(stored)["locked"] is True
    assert c.get(f"/api/v0/projects/{PROJECT}/graph", headers=auth).json() == stored

    # Autosave never deadlocks: the same graph again (the UI never sends the flag) is stored again.
    again = _saved(c, auth, picked)
    assert again["graph"] == {**stored, "revision": stored["revision"] + 1} and len(again["violations"]) == 1

    # The flag is the server's: a client cannot clear it, nor set it on an edge that is not locked.
    cleared = copy.deepcopy(picked)
    _edge(cleared)["locked"] = False
    assert _edge(_saved(c, auth, cleared)["graph"])["locked"] is True

    # The lock is real: the Motion refuses its quote, while the rest of the project still quotes.
    r = _quote(c, auth, node="v")
    assert r.status_code == 409 and r.json()["error"] == "VIDEO_LOCKED", r.text
    r = _quote(c, auth)
    assert r.status_code == 200, r.text

    # Accepting the picked take approves the node and lifts the lock.
    second_scrub = _accept(c, auth, second["id"])
    body = _saved(c, auth, picked)
    node = _node(body["graph"])
    assert node["approved"] is True and node["scrubLogId"] == second_scrub
    assert body["violations"] == [] and "locked" not in _edge(body["graph"])
    flagged = copy.deepcopy(picked)
    _edge(flagged)["locked"] = True
    assert "locked" not in _edge(_saved(c, auth, flagged)["graph"])

    # Picking the first take back: it is still the take that was accepted against its own scrub.
    node = _node(_saved(c, auth, _graph(packet_id, pick=take["outputs"][0]))["graph"])
    assert node["approved"] is True and node["scrubLogId"] == scrub_id


def test_picking_an_older_unaccepted_take_unapproves_a_node_whose_newest_take_is_accepted(
    c, auth, engine, packet_id
):
    """The other half of the doctrine: the Motion animates the picked take, so the picked take must be the
    accepted one. A newest accepted run no longer lends its approval to an older pick."""
    _saved(c, auth, _graph(packet_id, motion=False))
    older = _make(c, auth)
    engine.factor = 0.8
    newer = _make(c, auth)
    newer_scrub = _accept(c, auth, newer["id"])
    node = _node(_saved(c, auth, _graph(packet_id, motion=False))["graph"])  # no pick: the newest run decides
    assert node["approved"] is True and node["scrubLogId"] == newer_scrub
    node = _node(_saved(c, auth, _graph(packet_id, pick=older["outputs"][0], motion=False))["graph"])
    assert "approved" not in node and node["gateStatus"] == "pass" and "scrubLogId" not in node


def test_a_picked_picture_that_is_none_of_the_nodes_takes_approves_nothing(c, auth, packet_id):
    take, _scrub = _approved_take(c, auth, packet_id)
    stray = "f" * 64  # no run of this node made it: an upload, a stale or a made-up hash
    body = _saved(c, auth, _graph(packet_id, pick=stray))
    node = _node(body["graph"])
    assert "approved" not in node and node.get("gateStatus", "none") == "none"
    assert _edge(body["graph"])["locked"] is True and len(body["violations"]) == 1
    # With no pick at all the node follows its newest run, which is the accepted take.
    body = _saved(c, auth, _graph(packet_id))
    assert _node(body["graph"])["approved"] is True and body["violations"] == []


# ---------------------------------------------------------------------------- what is still refused


def test_a_new_edge_into_an_unapproved_picked_take_is_refused(c, auth, packet_id):
    take, _scrub = _approved_take(c, auth, packet_id)
    second = _re_mask(c, auth, take["id"])
    pick = second["outputs"][0]

    # The Motion edge removed, then drawn again while the unaccepted take is picked: a new edge, refused.
    _saved(c, auth, _graph(packet_id, pick=pick, motion=False))
    r = _put(c, auth, _graph(packet_id, pick=pick))
    assert r.status_code == 422, r.text
    assert [(v["code"], v["edge"]["id"]) for v in r.json()["violations"]] == [("VIDEO_LOCKED", MOTION_EDGE)]
    assert all(e["id"] != MOTION_EDGE for e in _stored(c.app.state.settings)["edges"])

    # Held on the stored edge, refused on a second Motion drawn from the same still.
    _saved(c, auth, _graph(packet_id, pick=take["outputs"][0]))
    _saved(c, auth, _graph(packet_id, pick=pick))
    graph = _graph(packet_id, pick=pick)
    graph["nodes"].append({"id": "v2", "kind": "Motion", "label": "Motion", "riskLabel": "low"})
    graph["edges"].append({"id": "e->v2", "source": "e", "target": "v2"})
    r = _put(c, auth, graph)
    assert r.status_code == 422, r.text
    assert [v["edge"]["id"] for v in r.json()["violations"]] == ["e->v2", MOTION_EDGE]
    assert r.json()["errors"][0].startswith("VIDEO_LOCKED: ")
    assert all(n["id"] != "v2" for n in _stored(c.app.state.settings)["nodes"])


def test_claims_on_a_picked_take_are_still_the_servers(c, auth, packet_id):
    take, scrub_id = _approved_take(c, auth, packet_id)
    second = _re_mask(c, auth, take["id"])
    pick = second["outputs"][0]
    _saved(c, auth, _graph(packet_id, pick=pick))  # stored unapproved, the edge held

    # A real scrub id of ANOTHER take approves nothing on the picked one.
    claim = {"gateStatus": "pass", "approved": True, "approvedBy": "human", "scrubLogId": scrub_id}
    node = _node(_saved(c, auth, _graph(packet_id, pick=pick, edit=claim))["graph"])
    assert node["approved"] is False and "scrubLogId" not in node and "approvedBy" not in node
    # A claimed approval with no scrub id at all is still SCRUB_REQUIRED (decision 31 unchanged).
    r = _put(c, auth, _graph(packet_id, pick=pick, edit={"gateStatus": "pass", "approved": True}))
    assert r.status_code == 422, r.text
    assert [v["code"] for v in r.json()["violations"]] == ["SCRUB_REQUIRED", "VIDEO_LOCKED"]


def test_a_held_edge_another_writer_removed_meanwhile_is_a_new_edge_again(c, auth, packet_id, monkeypatch):
    take, _scrub = _approved_take(c, auth, packet_id)
    second = _re_mask(c, auth, take["id"])
    pipeline = c.app.state.pipeline
    stale = pipeline.load_project(PROJECT)  # read by this PUT before the other writer saved
    with stale.lock(timeout_s=5.0):
        fresh = pipeline.load_project(PROJECT)
        fresh.save_graph(_graph(packet_id, pick=take["outputs"][0], motion=False))
    monkeypatch.setattr(pipeline, "load_project", lambda _pid: stale)

    r = _put(c, auth, _graph(packet_id, pick=second["outputs"][0]))
    assert r.status_code == 422, r.text
    assert [(v["code"], v["edge"]["id"]) for v in r.json()["violations"]] == [("VIDEO_LOCKED", MOTION_EDGE)]
    assert all(e["id"] != MOTION_EDGE for e in _stored(c.app.state.settings)["edges"])
