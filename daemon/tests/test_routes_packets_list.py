"""The Plate-linking routes (router plan T55): `GET /projects/{id}/packets` and `GET /packets/{id}/summary`.

Packets come from the committed Blender fixtures `fixtures/packets/room_a` and `room_a_v2` by reference; the
failed and half-ingested packets are tmp copies of `room_b` (the fixtures themselves are never written to).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plate_daemon.api import create_app
from plate_daemon.config import Settings

ROOT = Path(__file__).resolve().parents[2]
PACKETS = ROOT / "fixtures" / "packets"
ROOM_A, ROOM_A_V2, ROOM_B = PACKETS / "room_a", PACKETS / "room_a_v2", PACKETS / "room_b"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"
ROOM_A_PLATE = "d31462e31c9c6bc7067c85a13deb1acb1001bf08640d4d4a5232d04a3580e7fe"

pytestmark = pytest.mark.skipif(
    not all((p / "manifest.json").is_file() for p in (ROOM_A, ROOM_A_V2, ROOM_B)),
    reason="fixtures/packets/room_a, room_a_v2, room_b not built yet (T2)",
)


@pytest.fixture
def sync_client(plate_env, monkeypatch):
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    settings = Settings.load()
    with TestClient(create_app(settings), base_url=BASE) as c:
        yield c, {"Authorization": f"Bearer {settings.token}"}


def _manifest(path: Path) -> dict:
    return json.loads((path / "manifest.json").read_text(encoding="utf-8"))


def _post(c: TestClient, auth: dict, path: Path):
    return c.post(
        "/api/v0/packets",
        json={"packet_path": str(path), "sender": {"name": "t", "version": "0"}},
        headers={**auth, "X-Plate-Packet-Version": _manifest(path)["packet_version"]},
    )


def _ingest(c: TestClient, auth: dict, path: Path) -> str:
    r = _post(c, auth, path)
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    for _ in range(100):  # sync ingest is done on return; polling keeps the test honest if that changes
        if c.get(f"/api/v0/packets/{packet_id}", headers=auth).json()["status"] != "ingesting":
            break
    assert c.get(f"/api/v0/packets/{packet_id}", headers=auth).json()["status"] == "ingested"
    return packet_id


@pytest.fixture
def two_packets(sync_client):
    c, auth = sync_client
    a = _ingest(c, auth, ROOM_A)
    v2 = _ingest(c, auth, ROOM_A_V2)
    return c, auth, a, v2


# ---------------------------------------------------------------------------- GET /projects/{id}/packets


def test_the_list_carries_the_plate_and_the_supersession_chain(two_packets):
    c, auth, a, v2 = two_packets
    r = c.get(f"/api/v0/projects/{PROJECT}/packets", headers=auth)
    assert r.status_code == 200
    items = r.json()
    assert isinstance(items, list) and [i["packet_id"] for i in items] == [a, v2]  # received order
    first, second = items
    assert first["plate"] == {
        "role": "display_tiff16",
        "relpath": "display.tif",
        "blake3": ROOM_A_PLATE,
        "width": 640,
        "height": 360,
    }
    assert first["supersedes"] is None and first["superseded_by"] == v2
    assert second["supersedes"] == a and second["superseded_by"] is None
    assert second["plate"]["blake3"] == _manifest(ROOM_A_V2)["files"]["display_tiff16"]["hash"]["hex"]
    assert first["camera_name"] == "CAM_a" and second["camera_name"] == "CAM_a"
    assert (first["shot_id"], second["shot_id"]) == ("room-a", "room-a-v2")
    for item in items:  # every packet_entry field rides along
        assert item["project_id"] == PROJECT
        assert item["tier"] == "A" and item["kind"] == "still" and item["status"] == "ingested"
        assert item["path"] and item["manifest_blake3"] and item["received_utc"] and item["ingested_utc"]
        assert "error" not in item
    assert first["path"] == str(ROOM_A)


def test_status_all_lists_a_failed_packet_and_the_default_hides_it(two_packets, tmp_path):
    c, auth, a, v2 = two_packets
    copy = tmp_path / "room_b_copy"
    shutil.copytree(ROOM_B, copy)
    finish = c.app.state.ctx.finish_packet
    c.app.state.ctx.finish_packet = lambda *_a: None  # leave it 'ingesting' so a file can change under it
    try:
        r = _post(c, auth, copy)
        assert r.status_code == 202, r.text
        failed = r.json()["packet_id"]
    finally:
        c.app.state.ctx.finish_packet = finish
    with (copy / "display.tif").open("ab") as fh:
        fh.write(b"\0")
    finish(PROJECT, failed)
    assert c.get(f"/api/v0/packets/{failed}", headers=auth).json()["status"] == "failed"

    default = c.get(f"/api/v0/projects/{PROJECT}/packets", headers=auth).json()
    assert [i["packet_id"] for i in default] == [a, v2]
    listed = c.get(f"/api/v0/projects/{PROJECT}/packets?status=all", headers=auth).json()
    assert {i["packet_id"] for i in listed} == {a, v2, failed}
    bad = next(i for i in listed if i["packet_id"] == failed)
    assert bad["status"] == "failed" and bad["error"] and bad["plate"]["relpath"] == "display.tif"
    assert c.get(f"/api/v0/projects/{PROJECT}/packets?status=ingested", headers=auth).json() == default
    assert c.get(f"/api/v0/projects/{PROJECT}/packets?status=bogus", headers=auth).status_code == 422


def test_a_vanished_share_still_lists_the_row_without_manifest_fields(two_packets, tmp_path):
    c, auth, a, v2 = two_packets
    copy = tmp_path / "room_b_gone"
    shutil.copytree(ROOM_B, copy)
    packet_id = _ingest(c, auth, copy)
    shutil.rmtree(copy)
    item = next(
        i
        for i in c.get(f"/api/v0/projects/{PROJECT}/packets", headers=auth).json()
        if i["packet_id"] == packet_id
    )
    assert item["plate"] is None and item["supersedes"] is None and item["camera_name"] is None
    assert item["status"] == "ingested" and item["path"] == str(copy)
    summary = c.get(f"/api/v0/packets/{packet_id}/summary", headers=auth)
    assert summary.status_code == 409 and summary.json()["error"] == "packet_unreadable"


def test_the_list_refuses_an_unknown_project_a_bad_slug_and_a_missing_token(two_packets):
    c, auth, _a, _v2 = two_packets
    r = c.get("/api/v0/projects/nope/packets", headers=auth)
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert c.get("/api/v0/projects/Bad Id/packets", headers=auth).status_code == 422
    assert c.get(f"/api/v0/projects/{PROJECT}/packets").status_code == 401


# ---------------------------------------------------------------------------- GET /packets/{id}/summary


def test_the_summary_resolves_editable_locked_and_planar(two_packets):
    c, auth, a, v2_id = two_packets
    r = c.get(f"/api/v0/packets/{a}/summary", headers=auth)
    assert r.status_code == 200, r.text
    s = r.json()
    manifest = _manifest(ROOM_A)
    assert s["packet_id"] == a and s["project_id"] == PROJECT and s["shot_id"] == "room-a"
    assert (s["kind"], s["tier"], s["status"]) == ("still", "A", "ingested")
    assert s["size"] == {"width": 640, "height": 360}
    assert s["plate"]["blake3"] == ROOM_A_PLATE and s["plate"]["role"] == "display_tiff16"
    assert s["supersedes"] is None
    assert s["camera"] == manifest["camera"] and s["policy"] == manifest["policy"]

    entries = s["legend"]["entries"]
    legend = json.loads((ROOM_A / "id_legend.json").read_text(encoding="utf-8"))["entries"]
    assert [e["id"] for e in entries] == [e["id"] for e in legend]
    assert all(
        set(e) == {"id", "kind", "name", "class", "pixel_share", "editable", "locked", "planar"}
        for e in entries
    )
    shrub = next(e for e in entries if e["name"] == "VEG_shrub_A")
    assert shrub["editable"] is True and shrub["locked"] is False and shrub["class"] == "vegetation"
    assert shrub["id"] in manifest["policy"]["editable_ids"] and shrub["kind"] == "object"
    assert sum(e["planar"] for e in entries) == 6
    wall = next(e for e in entries if e["name"] == "ARCH_wall_N")
    assert wall["locked"] is True and wall["editable"] is False and wall["planar"] is True
    assert {e["id"] for e in entries if e["editable"]} == set(manifest["policy"]["editable_ids"]) & {
        e["id"] for e in entries
    }

    v2 = c.get(f"/api/v0/packets/{v2_id}/summary", headers=auth).json()
    assert v2["supersedes"] == a and v2["shot_id"] == "room-a-v2"


def test_the_summary_refuses_an_unknown_packet_and_one_still_ingesting(sync_client):
    c, auth = sync_client
    r = c.get("/api/v0/packets/pkt_NOPE/summary", headers=auth)
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    finish = c.app.state.ctx.finish_packet
    c.app.state.ctx.finish_packet = lambda *_a: None  # the ingest has been accepted but has not finished
    try:
        packet_id = _post(c, auth, ROOM_A).json()["packet_id"]
    finally:
        c.app.state.ctx.finish_packet = finish
    r = c.get(f"/api/v0/packets/{packet_id}/summary", headers=auth)
    assert r.status_code == 409 and r.json() == {"error": "not_ingested", "status": "ingesting"}
    finish(PROJECT, packet_id)
    assert c.get(f"/api/v0/packets/{packet_id}/summary", headers=auth).status_code == 200
    assert c.get(f"/api/v0/packets/{packet_id}/summary").status_code == 401
