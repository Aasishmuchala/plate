"""Project routes, wave 8 (router plan T59): the server owns node status, a human is `X-Plate-Client: ui`,
custody edits ledger their loosening, the quote takes the router's fields, `/session` names the packet folders.

Decision 31: `PUT /projects/{id}/graph` rewrites every node's `gateStatus`, `approved`, `approvedBy` and
`scrubLogId` from the index before the canvas rules run, so a client that claims an approval (the audit's
made-up `scrubLogId`) unlocks nothing; only a DONE (or overridden) take with an `accept` note whose full
scrub is in the ledger does. Runs that need a take go through the real pipeline on `fixtures/packets/room_a`
with an engine that echoes the PNG it was sent (optionally shifted, to fail the gates).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.ledger import Ledger
from plate_daemon.machine import append_note

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
UI = {"X-Plate-Client": "ui"}
REASON = "client signed the cloud addendum on 2026-10-18"

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


class EchoEngine(FakeEngine):
    """A FakeEngine that hands back the PNG it was sent, moved `shift_px` to the right."""

    def __init__(self, *args, shift_px: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self.shift_px = shift_px
        self.sent: dict[str, bytes] = {}

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        self.sent[job] = base64.b64decode(payload["inputs"]["image"]["b64"])
        return job

    def fetch(self, result_url):
        super().fetch(result_url)
        data = self.sent[result_url[len(RESULT_SCHEME) :]]
        if self.shift_px:
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
            data = bytes(cv2.imencode(".png", np.roll(img, self.shift_px, axis=1))[1])
        return data


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    return Settings.load()


def _client(settings: Settings, engine=None) -> TestClient:
    c = TestClient(create_app(settings, engine), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


def _project(c: TestClient, auth: dict, project_id: str = PROJECT, **custody) -> dict:
    body = {"project_id": project_id, "custody": {"nda": False, **custody}}
    r = c.post("/api/v0/projects", json=body, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _packet(c: TestClient, auth: dict) -> str:
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
    r = c.post("/api/v0/packets", json=body, headers=hdr)
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    assert c.get(f"/api/v0/packets/{packet_id}", headers=auth).json()["status"] == "ingested"
    return packet_id


def _graph(packet_id: str = "pkt_x", *, edit: dict | None = None, motion: bool = False) -> dict:
    nodes = [
        {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
        {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}, **(edit or {})},
    ]
    edges = [{"id": "p->e", "source": "p", "target": "e"}]
    if motion:
        nodes.append({"id": "m", "kind": "Motion", "label": "Motion", "riskLabel": "low"})
        edges.append({"id": "e->m", "source": "e", "target": "m"})
    return {"nodes": nodes, "edges": edges}


def _put(c: TestClient, auth: dict, graph: dict, **headers):
    return c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers={**auth, **headers})


def _stored(settings: Settings, node_id: str = "e") -> dict:
    doc = json.loads((settings.projects_dir / PROJECT / "project.json").read_text(encoding="utf-8"))
    return next(n for n in doc["graph"]["nodes"] if n["id"] == node_id)


def _quote(c: TestClient, auth: dict, **body) -> dict:
    r = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json=body, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _run(c: TestClient, auth: dict, by: str = "auto") -> dict:
    run_id = _quote(c, auth)["run"]["id"]
    r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": by}, headers=auth)
    assert r.status_code == 200, r.text
    return c.get(f"/api/v0/runs/{run_id}", headers=auth).json()


def _scrub_and_accept(c: TestClient, auth: dict, run_id: str) -> str:
    r = c.post(f"/api/v0/runs/{run_id}/scrub", json={"quadrants_visited": 4, "zoom_100": True}, headers=auth)
    assert r.status_code == 200 and r.json()["full_scrub"] is True, r.text
    scrub_id = r.json()["scrub_log_id"]
    r = c.post(f"/api/v0/runs/{run_id}/accept", json={"scrub_log_id": scrub_id}, headers=auth)
    assert r.status_code == 200, r.text
    return scrub_id


# ---------------------------------------------------------------------------- the server owns node status


CLAIM = {"gateStatus": "pass", "approved": True, "approvedBy": "human", "scrubLogId": "made-up"}


def test_a_claimed_approval_on_an_unrun_node_is_rewritten_and_unlocks_nothing(sync_settings, auth):
    with _client(sync_settings) as c:
        _project(c, auth)
        r = _put(c, auth, _graph(edit=CLAIM))
        assert r.status_code == 200, r.text
        node = _stored(sync_settings)
        assert node["approved"] is False and node["gateStatus"] == "none"
        assert "scrubLogId" not in node and "approvedBy" not in node
        assert next(n for n in r.json()["graph"]["nodes"] if n["id"] == "e") == node  # answered as stored

        # The same claim with an Edit -> Motion edge: the rules read the server's status, so video stays locked.
        r = _put(c, auth, _graph(edit=CLAIM, motion=True))
        assert r.status_code == 422, r.text
        assert [v["code"] for v in r.json()["violations"]] == ["VIDEO_LOCKED"]

        # A claimed approval with no scrub id at all is still answered SCRUB_REQUIRED.
        r = _put(c, auth, _graph(edit={"gateStatus": "pass", "approved": True}))
        assert r.status_code == 422 and r.json()["violations"][0]["code"] == "SCRUB_REQUIRED"


def test_claims_are_rewritten_in_either_wire_form_and_spelling(sync_settings, auth):
    with _client(sync_settings) as c:
        _project(c, auth)
        graph = _graph()
        graph["nodes"][1] = {
            "id": "e",
            "position": {"x": 0, "y": 0},
            "data": {"kind": "Edit", "gate_status": "pass", "approved": True, "scrub_log_id": "x"},
            "approved": True,  # ignored by the rules next to `data`, dropped all the same
        }
        assert _put(c, auth, graph).status_code == 200
        node = _stored(sync_settings)
        assert node["data"] == {"kind": "Edit", "gateStatus": "none", "approved": False}
        assert "approved" not in node

        plain = _graph()  # a node that never claimed anything round-trips unchanged
        assert _put(c, auth, plain).status_code == 200
        doc = json.loads((sync_settings.projects_dir / PROJECT / "project.json").read_text(encoding="utf-8"))
        assert doc["graph"] == plain


@needs_room_a
def test_an_accepted_take_unlocks_motion_and_a_reroll_relocks_it_until_cancelled(sync_settings, auth):
    with _client(sync_settings, EchoEngine()) as c:
        _project(c, auth)
        packet_id = _packet(c, auth)
        assert _put(c, auth, _graph(packet_id)).status_code == 200
        run = _run(c, auth)
        assert run["state"] == "DONE", run.get("error")

        # DONE but not accepted: the gate passes, the node is not approved, video stays locked.
        assert _put(c, auth, _graph(packet_id)).status_code == 200
        node = _stored(sync_settings)
        assert node["gateStatus"] == "pass" and "approved" not in node
        assert _put(c, auth, _graph(packet_id, motion=True)).json()["violations"][0]["code"] == "VIDEO_LOCKED"

        scrub_id = _scrub_and_accept(c, auth, run["id"])
        r = _put(c, auth, _graph(packet_id, motion=True))  # the plain node, no claims
        assert r.status_code == 200, r.text
        node = _stored(sync_settings)
        assert node["gateStatus"] == "pass" and node["approved"] is True
        assert node["scrubLogId"] == scrub_id and node["approvedBy"] == "human"
        assert next(n for n in r.json()["graph"]["nodes"] if n["id"] == "e") == node

        # A client that sends stale fields gets the server's back.
        stale = _graph(packet_id, edit={"approved": False, "scrubLogId": "old"}, motion=True)
        assert _put(c, auth, stale).status_code == 200
        assert _stored(sync_settings)["scrubLogId"] == scrub_id

        # A re-roll in flight speaks for the node: nothing to show yet, so Motion locks again ...
        reroll = _quote(c, auth)["run"]["id"]
        r = _put(c, auth, _graph(packet_id, motion=True))
        assert r.status_code == 422 and r.json()["violations"][0]["code"] == "VIDEO_LOCKED"
        # ... and a cancelled one does not: the accepted take is the node's take again.
        assert c.post(f"/api/v0/runs/{reroll}/cancel", headers=auth).json()["state"] == "CANCELLED"
        assert _put(c, auth, _graph(packet_id, motion=True)).status_code == 200
        assert _stored(sync_settings)["approved"] is True


@needs_room_a
def test_an_accept_note_without_its_logged_scrub_approves_nothing(sync_settings, auth):
    with _client(sync_settings, EchoEngine()) as c:
        _project(c, auth)
        packet_id = _packet(c, auth)
        assert _put(c, auth, _graph(packet_id)).status_code == 200
        run = _run(c, auth)
        assert run["state"] == "DONE", run.get("error")
        with c.app.state.index.project(PROJECT) as conn:  # an index edited behind the daemon's back
            note = json.dumps({"scrub_log_id": "scrub-forged", "decision": "accepted"}, sort_keys=True)
            append_note(conn, run["id"], kind="accept", actor="human", reason=note)
        assert _put(c, auth, _graph(packet_id)).status_code == 200
        node = _stored(sync_settings)
        assert node["gateStatus"] == "pass" and "approved" not in node and "scrubLogId" not in node


@needs_room_a
def test_a_gate_failure_is_fail_until_overridden(sync_settings, auth):
    with _client(sync_settings, EchoEngine(shift_px=12)) as c:
        _project(c, auth)
        packet_id = _packet(c, auth)
        assert _put(c, auth, _graph(packet_id)).status_code == 200
        run = _run(c, auth, by="human")
        assert run["state"] == "GATE_FAIL", run.get("error")
        assert _put(c, auth, _graph(packet_id, edit={"gateStatus": "pass"})).status_code == 200
        assert _stored(sync_settings)["gateStatus"] == "fail"
        r = c.post(
            f"/api/v0/runs/{run['id']}/override",
            json={"reason": "client approved the shifted crop", "by": "human"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        assert _put(c, auth, _graph(packet_id)).status_code == 200
        assert _stored(sync_settings)["gateStatus"] == "pass"


@needs_room_a
def test_the_accepted_sibling_of_a_best_of_n_group_approves_the_node(sync_settings, auth):
    with _client(sync_settings, EchoEngine()) as c:
        _project(c, auth)
        packet_id = _packet(c, auth)
        assert _put(c, auth, _graph(packet_id)).status_code == 200
        first = _quote(c, auth)["run"]["id"]
        sibs = c.post(f"/api/v0/runs/{first}/fanout", json={"n": 2}, headers=auth)
        assert sibs.status_code == 200, sibs.text
        assert c.post(f"/api/v0/runs/{first}/approve", json={"by": "human"}, headers=auth).status_code == 200
        ids = [s["id"] for s in sibs.json()]
        states = [c.get(f"/api/v0/runs/{i}", headers=auth).json()["state"] for i in ids]
        assert states == ["DONE", "DONE"]
        scrub_id = _scrub_and_accept(c, auth, ids[0])  # sibling 0, the older run of the two
        assert _put(c, auth, _graph(packet_id, motion=True)).status_code == 200
        node = _stored(sync_settings)
        assert node["approved"] is True and node["scrubLogId"] == scrub_id and node["gateStatus"] == "pass"


# ---------------------------------------------------------------------------- a human is X-Plate-Client: ui


@needs_room_a
def test_only_a_ui_request_resets_the_unattended_clock(sync_settings, auth):
    with _client(sync_settings, EchoEngine()) as c:
        pipeline = c.app.state.pipeline
        assert pipeline.last_human_utc is None
        _project(c, auth)  # curl, MCP, the plugin: no header
        assert pipeline.last_human_utc is None
        r = c.post(
            "/api/v0/projects", json={"project_id": "other", "custody": {"nda": False}}, headers=auth | UI
        )
        assert r.status_code == 201
        assert pipeline.last_human_utc is not None

        packet_id = _packet(c, auth)
        pipeline.last_human_utc = None
        assert _put(c, auth, _graph(packet_id)).status_code == 200
        assert pipeline.last_human_utc is None
        assert _put(c, auth, _graph(packet_id), **UI).status_code == 200
        assert pipeline.last_human_utc is not None

        pipeline.last_human_utc = None
        _quote(c, auth)
        assert pipeline.last_human_utc is None
        r = c.post(
            f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth | {"x-plate-client": "UI"}
        )
        assert r.status_code == 200, r.text
        assert pipeline.last_human_utc is not None

        pipeline.last_human_utc = None
        r = c.put(
            f"/api/v0/projects/{PROJECT}/custody",
            json={"nda": True, "allowed_grades": ["C0"]},
            headers=auth | {"X-Plate-Client": "mcp"},
        )
        assert r.status_code == 200 and pipeline.last_human_utc is None


@needs_room_a
def test_the_quote_takes_the_router_fields_and_answers_the_router_verdict(sync_settings, auth):
    with _client(sync_settings, EchoEngine()) as c:
        _project(c, auth)
        packet_id = _packet(c, auth)
        assert _put(c, auth, _graph(packet_id)).status_code == 200
        body = {"task": "relight", "shot_class": "wide", "recipe": "night_relight_changelist"}
        q = _quote(c, auth, engine_id="fake", **body)
        assert "router" in q and q["run"]["state"] == "QUOTED"
        assert q["router"] == q["run"]["inputs"].get("router")
        assert (
            c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={"attempt": 0}, headers=auth).status_code
            == 422
        )
        bogus = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={"tasks": "relight"}, headers=auth)
        assert bogus.status_code == 422  # still no unknown fields


# ---------------------------------------------------------------------------- custody


def test_custody_tightens_freely_and_loosens_only_with_a_ledgered_reason(sync_settings, auth):
    with _client(sync_settings) as c:
        _project(c, auth)
        url = f"/api/v0/projects/{PROJECT}/custody"
        project_dir = sync_settings.projects_dir / PROJECT

        r = c.put(url, json={"nda": False, "allowed_grades": ["C0", "C1", "C2"]}, headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["custody"] == {"nda": False, "allowed_grades": ["C0", "C1", "C2"]}
        assert r.json()["project_id"] == PROJECT and r.json()["root"] == str(project_dir)
        r = c.put(url, json={"nda": True, "allowed_grades": ["C0", "C1"]}, headers=auth)  # NDA on: tighter
        assert r.status_code == 200 and r.json()["custody"] == {"nda": True, "allowed_grades": ["C0", "C1"]}
        assert [x for x in Ledger(project_dir).records() if x["kind"] == "override"] == []

        # Loosening: NDA off, or any grade added, needs a written reason.
        for body in (
            {"nda": False, "allowed_grades": ["C0", "C1"]},
            {"nda": False, "allowed_grades": ["C0", "C1", "D"], "reason": "too short"},
        ):
            r = c.put(url, json=body, headers=auth)
            assert r.status_code == 422 and r.json()["error"] == "reason_required", r.text
        assert c.get("/api/v0/projects", headers=auth).json()[0]["custody"]["nda"] is True  # unchanged

        r = c.put(
            url, json={"nda": False, "allowed_grades": ["C0", "C1", "D"], "reason": REASON}, headers=auth
        )
        assert r.status_code == 200, r.text
        assert r.json()["custody"] == {"nda": False, "allowed_grades": ["C0", "C1", "D"]}
        (rec,) = [x for x in Ledger(project_dir).records() if x["kind"] == "override"]
        assert rec["by"] == "human" and rec["reason"] == REASON and "run_id" not in rec
        assert rec["data"] == {
            "what": "custody",
            "from": {"nda": True, "allowed_grades": ["C0", "C1"]},
            "to": {"nda": False, "allowed_grades": ["C0", "C1", "D"]},
        }
        with c.app.state.index.project(PROJECT) as conn:  # the index's copy follows
            row = conn.execute("SELECT value FROM project_meta WHERE key = 'custody'").fetchone()
        assert json.loads(row["value"]) == {"nda": False, "allowed_grades": ["C0", "C1", "D"]}
        assert c.app.state.pipeline.load_project(PROJECT).custody["allowed_grades"] == ["C0", "C1", "D"]

        # Invalid custody is 422 and changes nothing; an unknown project is 404.
        for body in (
            {"nda": True, "allowed_grades": ["C0", "D"], "reason": REASON},  # NDA work never leaves for D
            {"nda": False, "allowed_grades": ["C9"], "reason": REASON},
            {"nda": False, "allowed_grades": [], "reason": REASON},
            {"nda": "yes", "allowed_grades": ["C0"]},
            {"nda": False},
        ):
            assert c.put(url, json=body, headers=auth).status_code == 422, body
        assert (
            c.put(url, json={"nda": False, "allowed_grades": ["C0"], "extra": 1}, headers=auth).status_code
            == 422
        )
        assert (
            c.put(
                "/api/v0/projects/absent/custody", json={"nda": True, "allowed_grades": ["C0"]}, headers=auth
            ).status_code
            == 404
        )
        assert len([x for x in Ledger(project_dir).records() if x["kind"] == "override"]) == 1


def test_a_loosening_the_ledger_cannot_hold_does_not_happen(sync_settings, auth, monkeypatch):
    with _client(sync_settings) as c:
        _project(c, auth, allowed_grades=["C0"])

        def broken(self, *a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(Ledger, "append", broken)
        r = c.put(
            f"/api/v0/projects/{PROJECT}/custody",
            json={"nda": False, "allowed_grades": ["C0", "D"], "reason": REASON},
            headers=auth,
        )
        assert r.status_code == 503 and r.json()["error"] == "ledger_unavailable"
        assert c.app.state.pipeline.load_project(PROJECT).custody["allowed_grades"] == ["C0"]


def test_a_packet_first_project_takes_an_explicit_custody(sync_settings, auth):
    with _client(sync_settings) as c:
        project = c.app.state.pipeline.ensure_project("early")  # what a packet arriving first makes
        assert project.custody.get("unset") is True
        r = c.put(
            "/api/v0/projects/early/custody", json={"nda": True, "allowed_grades": ["C0"]}, headers=auth
        )
        assert r.status_code == 200, r.text
        assert r.json()["custody"] == {"nda": True, "allowed_grades": ["C0"]}  # no longer unset


# ---------------------------------------------------------------------------- the session


def test_the_session_names_the_packet_folders(sync_settings, auth):
    with _client(sync_settings) as c:
        s = c.get("/api/v0/session", headers=auth).json()
    assert s["ok"] is True and s["packet_versions"]
    assert s["packets_dirs"] == [str(p) for p in sync_settings.packets_dirs] and s["packets_dirs"]
    assert s["projects_dir"] == str(sync_settings.projects_dir)
    assert s["dev"] is False and s["cas_copy"] is False
