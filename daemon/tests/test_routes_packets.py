"""The route modules (router plan T25): packet ingest per PACKET.md section 9, projects and graphs with the
canvas rules enforced server-side, node quoting refusals, assets, and the shared canvas-rule vectors.

Packet tests use the committed Blender packet `fixtures/packets/room_a` by reference, and tmp copies of it
for the conflict and the hash-mismatch cases (the fixture itself is never written to).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from plate_daemon import canvas_rules
from plate_daemon.api import create_app
from plate_daemon.cas import CAS
from plate_daemon.config import Settings
from plate_daemon.routes import include_all
from plate_daemon.sse import EventFilter

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
VECTORS = ROOT / "spec" / "canvas_rules_vectors.json"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file(), reason="fixtures/packets/room_a not built yet (T2)"
)


@pytest.fixture
def sync_client(plate_env, monkeypatch):
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    settings = Settings.load()
    with TestClient(create_app(settings), base_url=BASE) as c:
        yield c, settings, {"Authorization": f"Bearer {settings.token}"}


def _version(path: Path = ROOM_A) -> str:
    return json.loads((path / "manifest.json").read_text(encoding="utf-8"))["packet_version"]


def _post(c: TestClient, auth: dict, path: Path, *, version: str | None = None):
    hdr = {**auth, "X-Plate-Packet-Version": version or _version(path)}
    return c.post(
        "/api/v0/packets",
        json={"packet_path": str(path), "sender": {"name": "t", "version": "0"}},
        headers=hdr,
    )


def _copy(tmp_path: Path, name: str) -> Path:
    dest = tmp_path / name
    shutil.copytree(ROOM_A, dest)
    return dest


# ---------------------------------------------------------------------------- the route package


def test_every_route_module_is_included_by_name():
    app = FastAPI()
    names = include_all(app)
    assert {"assets", "events", "packets", "projects", "runs"} <= set(names)
    assert names == sorted(names)


# ---------------------------------------------------------------------------- POST /packets


@needs_room_a
def test_packet_ingest_answers_per_the_transport_contract(sync_client):
    c, settings, auth = sync_client
    body = {"packet_path": str(ROOM_A), "sender": {"name": "t", "version": "0"}}
    assert c.post("/api/v0/packets", json=body).status_code == 401
    r = c.post("/api/v0/packets", json=body, headers=auth)
    assert r.status_code == 426 and r.json() == {"supported": ["0.1"]}
    assert _post(c, auth, ROOM_A, version="9.9").status_code == 426
    assert _post(c, auth, Path("fixtures/packets/room_a"), version="0.1").status_code == 422  # relative
    assert _post(c, auth, ROOM_A.parent / "absent", version="0.1").status_code == 422
    bad = c.post(
        "/api/v0/packets",
        json={"packet_path": str(ROOM_A)},
        headers={**auth, "X-Plate-Packet-Version": "0.1"},
    )
    assert bad.status_code == 422 and bad.json()["errors"]

    r = _post(c, auth, ROOM_A)
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    assert r.json()["events_url"] == f"/api/v0/packets/{packet_id}/events"
    rec = c.get(f"/api/v0/packets/{packet_id}", headers=auth).json()
    assert rec["status"] == "ingested" and rec["project_id"] == PROJECT and rec["registered"] is True
    assert {b["relpath"] for b in rec["blobs"]} == {"beauty.exr", "display.tif", "id_legend.json"}
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    by_rel = {b["relpath"]: b["blake3"] for b in rec["blobs"]}
    assert by_rel["display.tif"] == manifest["files"]["display_tiff16"]["hash"]["hex"]

    again = _post(c, auth, ROOM_A)
    assert again.status_code == 200 and again.json() == {"packet_id": packet_id, "status": "known"}

    with c.stream("GET", f"/api/v0/packets/{packet_id}/events", headers=auth) as s:
        assert s.headers["content-type"].startswith("text/event-stream")
        frames = [json.loads(line[6:]) for line in s.iter_lines() if line.startswith("data: ")]
    assert frames[0]["type"] == "ingest.started" and frames[-1]["type"] == "ingest.done"
    assert sum(f["type"] == "ingest.file" for f in frames) == 3

    # By reference: packets.json names the folder; no packet file was copied into the project.
    project_dir = settings.projects_dir / PROJECT
    entry = json.loads((project_dir / "packets.json").read_text(encoding="utf-8"))["packets"][packet_id]
    assert entry["path"] == str(ROOM_A) and entry["status"] == "ingested" and entry["tier"] == "A"
    assert not (project_dir / "assets").exists() or not any((project_dir / "assets").rglob("*.tif"))


@needs_room_a
def test_a_file_whose_bytes_changed_is_refused_with_422(sync_client, tmp_path):
    c, _settings, auth = sync_client
    pkt = _copy(tmp_path, "tampered")
    tif = pkt / "display.tif"
    data = bytearray(tif.read_bytes())
    data[-100] ^= 0xFF  # same size, different bytes: only the BLAKE3 can tell
    tif.write_bytes(bytes(data))
    r = _post(c, auth, pkt)
    assert r.status_code == 422, r.text
    assert any(re.search(r"display_tiff16: blake3 mismatch", e) for e in r.json()["errors"])
    manifest = json.loads((pkt / "manifest.json").read_text(encoding="utf-8"))
    assert c.get(f"/api/v0/packets/{manifest['packet_id']}", headers=auth).status_code == 404


@needs_room_a
def test_the_same_packet_id_with_another_manifest_is_a_conflict(sync_client, tmp_path):
    c, _settings, auth = sync_client
    assert _post(c, auth, ROOM_A).status_code == 202
    pkt = _copy(tmp_path, "edited")
    mpath = pkt / "manifest.json"
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    manifest["created_utc"] = "2026-10-18T00:00:00Z"
    mpath.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    r = _post(c, auth, pkt)
    assert r.status_code == 409 and r.json()["error"] == "conflict"
    assert r.json()["packet_id"] == manifest["packet_id"]


@needs_room_a
def test_a_packet_before_its_project_makes_a_c0_project_a_human_then_sets(sync_client):
    c, settings, auth = sync_client
    assert _post(c, auth, ROOM_A).status_code == 202
    listed = {p["project_id"]: p for p in c.get("/api/v0/projects", headers=auth).json()}
    assert (
        listed[PROJECT]["custody"]["allowed_grades"] == ["C0"] and listed[PROJECT]["custody"]["unset"] is True
    )
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": True}}, headers=auth)
    assert r.status_code == 200 and r.json()["custody"]["allowed_grades"] == ["C0", "C1"]
    again = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth)
    assert again.status_code == 409


# ---------------------------------------------------------------------------- projects and graphs


def test_projects_are_created_listed_and_validated(sync_client, tmp_path):
    c, settings, auth = sync_client
    r = c.post(
        "/api/v0/projects",
        json={"project_id": "tula-wot", "custody": {"nda": False}, "engine_pins": {"fake": "1"}},
        headers=auth,
    )
    assert r.status_code == 201 and r.json()["engine_pins"] == {"fake": "1"}
    assert r.json()["custody"]["allowed_grades"] == ["C0", "C1", "C2", "D"]
    elsewhere = tmp_path / "nas" / "abhigna"
    r = c.post(
        "/api/v0/projects",
        json={"project_id": "abhigna", "custody": {"nda": True}, "root": str(elsewhere)},
        headers=auth,
    )
    assert r.status_code == 201 and (elsewhere / "project.json").is_file()
    ids = [p["project_id"] for p in c.get("/api/v0/projects", headers=auth).json()]
    assert ids == ["abhigna", "tula-wot"]
    bad = (
        {"project_id": "Bad Name", "custody": {"nda": False}},
        {"project_id": "nda-d", "custody": {"nda": True, "allowed_grades": ["D"]}},
        {"project_id": "rel", "custody": {"nda": False}, "root": "relative/folder"},
    )
    for body in bad:
        assert c.post("/api/v0/projects", json=body, headers=auth).status_code == 422, body
    assert (
        c.post(
            "/api/v0/projects", json={"project_id": "tula-wot", "custody": {"nda": False}}, headers=auth
        ).status_code
        == 409
    )
    assert c.get("/api/v0/projects/absent/graph", headers=auth).status_code == 404


def test_graph_writes_are_held_to_the_canvas_rules(sync_client):
    c, settings, auth = sync_client
    assert (
        c.post(
            "/api/v0/projects", json={"project_id": "p1", "custody": {"nda": False}}, headers=auth
        ).status_code
        == 201
    )
    assert c.get("/api/v0/projects/p1/graph", headers=auth).json() == {
        "nodes": [],
        "edges": [],
        "revision": 0,
    }
    plate = {"id": "p", "kind": "Plate", "packetId": "pkt_x", "tier": "A"}
    good = {
        "nodes": [plate, {"id": "e", "kind": "Edit"}, {"id": "x", "kind": "Export"}],
        "edges": [{"id": "p->e", "source": "p", "target": "e"}, {"id": "e->x", "source": "e", "target": "x"}],
    }
    r = c.put("/api/v0/projects/p1/graph", json=good, headers=auth)
    assert r.status_code == 200 and r.json()["graph"]["nodes"][1]["id"] == "e"
    saved = json.loads((settings.projects_dir / "p1" / "project.json").read_text(encoding="utf-8"))
    assert saved["graph"] == good
    assert any(x["kind"] == "graph.saved" for x in c.app.state.bus.read(EventFilter("p1"), 0))

    concept = {
        "nodes": [{"id": "k", "kind": "Concept"}, {"id": "x", "kind": "Export"}],
        "edges": [{"id": "k->x", "source": "k", "target": "x"}],
    }
    r = c.put("/api/v0/projects/p1/graph", json=concept, headers=auth)
    assert r.status_code == 422 and r.json()["violations"][0]["code"] == "CONCEPT_TO_DELIVERY"
    unknown = {"nodes": [{"id": "t", "kind": "TextToImage"}], "edges": []}
    r = c.put("/api/v0/projects/p1/graph", json=unknown, headers=auth)
    assert r.status_code == 422 and r.json()["violations"][0]["code"] == "UNKNOWN_NODE"
    assert (
        c.put("/api/v0/projects/p1/graph", json={"nodes": "x", "edges": []}, headers=auth).status_code == 422
    )

    approved = json.loads(json.dumps(good))
    approved["nodes"][1].update({"gateStatus": "pass", "approved": True})
    r = c.put("/api/v0/projects/p1/graph", json=approved, headers=auth)
    assert r.status_code == 422 and r.json()["violations"][0]["code"] == "SCRUB_REQUIRED"
    approved["nodes"][1]["scrubLogId"] = "scrub_1"
    assert c.put("/api/v0/projects/p1/graph", json=approved, headers=auth).status_code == 200
    # The rejected writes left the last good graph on disk, and (decision 31, T59) the server
    # rewrote the client's claimed approval from the index: no run, no accept, so nothing approved.
    saved = json.loads((settings.projects_dir / "p1" / "project.json").read_text(encoding="utf-8"))
    edit = saved["graph"]["nodes"][1]
    assert edit.get("approved", False) is False
    assert edit.get("gateStatus", "none") == "none"
    assert "scrubLogId" not in edit


@needs_room_a
def test_node_quotes_refuse_what_the_pipeline_cannot_run(sync_client):
    c, _settings, auth = sync_client
    assert (
        c.post(
            "/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth
        ).status_code
        == 201
    )
    packet_id = _post(c, auth, ROOM_A).json()["packet_id"]
    graph = {
        "nodes": [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A"},
            {"id": "e", "kind": "Edit"},
            {"id": "lone", "kind": "Edit"},
            {"id": "m", "kind": "Mask", "params": {"ids": ["o:929c9e95"]}},
        ],
        "edges": [{"id": "p->e", "source": "p", "target": "e"}, {"id": "p->m", "source": "p", "target": "m"}],
    }
    assert c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=auth).status_code == 200

    def quote(node: str, **body):
        return c.post(f"/api/v0/projects/{PROJECT}/nodes/{node}/quote", json=body, headers=auth)

    assert quote("ghost", engine_id="fake").status_code == 404
    assert c.post("/api/v0/projects/absent/nodes/e/quote", json={}, headers=auth).status_code == 404
    assert quote("m", engine_id="fake").json()["error"] == "node_kind_unsupported"
    lone = quote("lone", engine_id="fake")
    assert lone.status_code == 422 and lone.json()["error"] == "NO_PLATE_ANCHOR"
    assert quote("e").status_code == 422  # no engine named anywhere
    assert quote("e", engine_id="no-such-engine").json()["error"] == "unknown_engine"
    # Egress never guesses a size: a cloud engine whose manifest declares none is refused at the quote. Every
    # shipped image engine declares sizes now (flux-3-image since T61), so strip them from this app's copy.
    manifests = c.app.state.pipeline.manifests
    flux = manifests["flux-3-image"]
    manifests["flux-3-image"] = flux.model_copy(update={"sizes": None})
    try:
        assert quote("e", engine_id="flux-3-image").json()["error"] == "no_sizes"
    finally:
        manifests["flux-3-image"] = flux
    up = quote("e", engine_id="real-esrgan")  # an upscaler: the plate is never upscaled
    assert up.status_code == 422 and up.json()["error"] == "engine_kind"
    assert quote("e", engine_id="fake", feather_px=40).status_code == 422  # the feather lives inside 4-12 px
    assert quote("e", engine_id="fake", bogus=1).status_code == 422
    ok = quote("e", engine_id="fake", prompt="dusk, warm windows")
    assert ok.status_code == 200 and ok.json()["run"]["inputs"]["prompt"] == "dusk, warm windows"
    assert (
        ok.json()["run"]["inputs"]["plate"]
        == json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))["files"]["display_tiff16"][
            "hash"
        ]["hex"]
    )


# ---------------------------------------------------------------------------- assets


def test_assets_are_served_by_hash_and_only_images_take_a_pixel_format(sync_client):
    c, settings, auth = sync_client
    assert (
        c.post(
            "/api/v0/projects", json={"project_id": "p2", "custody": {"nda": False}}, headers=auth
        ).status_code
        == 201
    )
    cas = CAS(settings.projects_dir / "p2" / "assets")
    key = cas.put_bytes(b'{"a": 1}\n', "json")
    r = c.get(f"/api/v0/assets/{key}", headers=auth)
    assert (
        r.status_code == 200
        and r.content == b'{"a": 1}\n'
        and r.headers["content-type"] == "application/json"
    )
    assert "immutable" in r.headers["cache-control"]
    assert c.get(f"/api/v0/assets/{key}?fmt=thumb", headers=auth).status_code == 415
    assert c.get(f"/api/v0/assets/{key}?fmt=bogus", headers=auth).status_code == 415
    assert c.get("/api/v0/assets/not-a-hash", headers=auth).status_code == 404
    assert c.get(f"/api/v0/assets/{key}?project_id=Bad Id", headers=auth).status_code == 422
    assert c.get(f"/api/v0/assets/{key}").status_code == 401


# ---------------------------------------------------------------------------- the shared canvas-rule vectors


def _camel(k: str) -> str:
    return re.sub(r"_([a-z])", lambda m: m.group(1).upper(), k)


def check_vector(vector: dict) -> str | None:
    """None when canvas_rules agrees with a vector of spec/canvas_rules_vectors.json; else why not."""
    v = {_camel(k): val for k, val in vector.items()}
    graph = (
        v["graph"]
        if isinstance(v.get("graph"), dict)
        else {"nodes": v.get("nodes") or [], "edges": v.get("edges") or []}
    )
    expect = v.get("expect", v.get("expected"))
    if isinstance(expect, str):
        expect = {"ok": True} if expect == "ok" else {"ok": False, "code": expect}
    raw_edge = next((v[k] for k in ("edge", "candidate", "newEdge", "connect") if v.get(k) is not None), None)
    if raw_edge is not None:
        got = canvas_rules.validate_edge(graph, raw_edge)
    elif "scrubLog" in v or "accept" in v:
        got = canvas_rules.can_accept(v["node"], v.get("scrubLog", v.get("accept")))
    elif isinstance(v.get("node"), str) and isinstance(v.get("depth"), int):
        depth = canvas_rules.generation_depth(
            canvas_rules.as_graph(graph).nodes, canvas_rules.as_graph(graph).edges, v["node"]
        )
        return None if depth == v["depth"] else f"depth of {v['node']}: expected {v['depth']}, got {depth}"
    else:
        raise AssertionError(f"no known shape (keys {sorted(vector)})")
    if got.ok != expect["ok"]:
        return f"expected ok={expect['ok']}, got {got.to_dict()}"
    if not got.ok and expect.get("code") and got.code != expect["code"]:
        return f"expected {expect['code']}, got {got.code}"
    return None


_FILE = json.loads(VECTORS.read_text(encoding="utf-8"))
_VECTORS = _FILE["vectors"] if isinstance(_FILE, dict) else _FILE


def test_the_vector_file_covers_every_rule_code():
    codes = {(v.get("expect") or {}).get("code") for v in _VECTORS if isinstance(v.get("expect"), dict)}
    assert set(canvas_rules.RULE_CODES) <= codes
    assert any("depth" in v for v in _VECTORS) and any(v.get("expect", {}).get("ok") for v in _VECTORS)


@pytest.mark.parametrize("vector", _VECTORS, ids=[v["name"] for v in _VECTORS])
def test_canvas_rules_agree_with_the_shared_vectors(vector):
    assert check_vector(vector) is None


def test_the_vector_checker_reports_a_disagreement():
    v = next(x for x in _VECTORS if (x.get("expect") or {}).get("code") == "CONCEPT_TO_DELIVERY")
    assert "expected ok=True" in check_vector({**v, "expect": {"ok": True}})
    assert "expected CYCLE" in check_vector({**v, "expect": {"ok": False, "code": "CYCLE"}})
