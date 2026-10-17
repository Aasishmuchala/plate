"""Masks scaled to the packet, SAM click to a plate-sized 0/255 mask, and reference uploads (router plan T82).

Two silent failures on real plates are pinned here: a mask painted at the editor's working size (2048 on the
long side) used to be stored at that size, so the quote refused it with 422 `mask_size`; and `/pixels/segment`
used to name the 16-bit label map as `mask_hash`, which the pipeline then read as a grey mask (every non-zero
label editable). The packet is the committed Blender `fixtures/packets/room_a` (640 x 360), by reference; the
segmenter is the in-process stub (`PLATE_PIXELS_INPROC=1`). References are synthetic images made here.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from plate_daemon.api import create_app
from plate_daemon.cas import CAS
from plate_daemon.config import Settings
from plate_daemon.pixels_client import INPROC_ENV, PORT_ENV
from plate_daemon.routes import assets as asset_routes
from plate_daemon.routes import pixels as pixel_routes

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
H, W = 360, 640

pytestmark = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


# ---------------------------------------------------------------------------- set-up


@pytest.fixture
def settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.setenv(INPROC_ENV, "1")
    monkeypatch.delenv("PLATE_PIXELS_REAL", raising=False)
    monkeypatch.delenv(PORT_ENV, raising=False)
    monkeypatch.delenv("PLATE_ROUTER_MODE", raising=False)
    return Settings.load()


def _ingest(c: TestClient, auth: dict, folder: Path) -> str:
    version = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["packet_version"]
    r = c.post(
        "/api/v0/packets",
        json={"packet_path": str(folder), "sender": {"name": "test", "version": "0"}},
        headers={**auth, "X-Plate-Packet-Version": version},
    )
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    assert c.get(f"/api/v0/packets/{packet_id}", headers=auth).json()["status"] == "ingested"
    return packet_id


@pytest.fixture
def client(settings, auth):
    """A daemon with project `plate-fixture` (custody set, not NDA) and room_a ingested into it."""
    with TestClient(create_app(settings), base_url=BASE) as c:
        c.app.state.pipeline.sleep = lambda _s: None
        r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth)
        assert r.status_code == 201, r.text
        c.packet_id = _ingest(c, auth, ROOM_A)
        yield c


def _png(arr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", np.ascontiguousarray(arr))
    assert ok
    return bytes(buf)


def _decode(data: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)


def _painted(h: int = 576, w: int = 1024) -> np.ndarray:
    """A mask painted at a working size: a hard box, and a soft-edged 128 band the scaling must binarise."""
    m = np.zeros((h, w), np.uint8)
    m[h // 6 : h // 2, w // 8 : w // 2] = 255
    m[h * 2 // 3 : h * 5 // 6, w * 3 // 5 : w * 4 // 5] = 128
    return m


def _put_graph(c: TestClient, auth: dict, packet_id: str, mask: dict) -> None:
    graph = {
        "nodes": [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
            {"id": "m", "kind": "Mask", "params": mask},
            {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
        ],
        "edges": [{"id": "p->m", "source": "p", "target": "m"}, {"id": "m->e", "source": "m", "target": "e"}],
    }
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=auth)
    assert r.status_code == 200, r.text


def _quote(c: TestClient, auth: dict):
    return c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth)


def _jpeg(w: int, h: int, *, description: str | None = None) -> bytes:
    y, x = np.mgrid[0:h, 0:w]
    rgb = np.stack([x * 255 // max(1, w - 1), y * 255 // max(1, h - 1), np.full_like(x, 90)], axis=-1)
    img = Image.fromarray(rgb.astype(np.uint8), "RGB")
    buf = io.BytesIO()
    if description is None:
        img.save(buf, "JPEG", quality=85)
    else:
        exif = Image.Exif()
        exif[0x010E] = description  # ImageDescription
        exif[0x013B] = "Client Photographer"  # Artist
        img.save(buf, "JPEG", quality=85, exif=exif.tobytes())
    return buf.getvalue()


def _png_chunks(data: bytes) -> list[str]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    out, i = [], 8
    while i < len(data):
        n = int.from_bytes(data[i : i + 4], "big")
        out.append(data[i + 4 : i + 8].decode("ascii"))
        i += 12 + n
    return out


def _upload_reference(c: TestClient, auth: dict, body: bytes, query: str = f"project_id={PROJECT}"):
    return c.post(f"/api/v0/assets?kind=reference&{query}", content=body, headers=auth)


# ---------------------------------------------------------------------------- masks scaled to the packet


def test_a_working_size_mask_is_stored_at_the_plate_size_and_binarised(client, auth, settings):
    painted = _painted()
    r = client.post(
        f"/api/v0/assets?kind=mask&project_id={PROJECT}&packet_id={client.packet_id}",
        content=_png(painted),
        headers={**auth, "Content-Type": "image/png"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["width"], body["height"]) == (W, H)
    assert body["scaled_from"] == [1024, 576]
    assert body["project_id"] == PROJECT and body["packet_id"] == client.packet_id

    stored = _decode(CAS(settings.projects_dir / PROJECT / "assets").read_bytes(body["hash"]))
    assert stored.dtype == np.uint8 and stored.shape == (H, W)
    assert set(np.unique(stored).tolist()) <= {0, 255}
    expected = cv2.resize(painted, (W, H), interpolation=cv2.INTER_NEAREST) > 127
    assert np.array_equal(stored == 255, expected)
    assert expected[H * 3 // 4, W * 7 // 10]  # the 128 band became editable, not dropped


def test_a_mask_already_at_the_plate_size_is_kept_byte_for_byte(client, auth, settings):
    soft = _painted(H, W)
    r = client.post(
        f"/api/v0/assets?kind=mask&project_id={PROJECT}&packet_id={client.packet_id}",
        content=_png(soft),
        headers=auth,
    )
    assert r.status_code == 201, r.text
    assert r.json()["scaled_from"] is None and (r.json()["width"], r.json()["height"]) == (W, H)
    stored = _decode(CAS(settings.projects_dir / PROJECT / "assets").read_bytes(r.json()["hash"]))
    assert np.array_equal(stored, soft)  # no threshold unless it was resized (the T56 behaviour)

    # Without a packet the T56 behaviour is unchanged: the working-size mask is stored as it came.
    r = client.post(f"/api/v0/assets?kind=mask&project_id={PROJECT}", content=_png(_painted()), headers=auth)
    assert r.status_code == 201 and (r.json()["width"], r.json()["height"]) == (1024, 576)
    assert r.json()["scaled_from"] is None


def test_the_scaled_mask_quotes_where_the_working_size_one_was_refused(client, auth):
    raw = client.post(
        f"/api/v0/assets?kind=mask&project_id={PROJECT}", content=_png(_painted()), headers=auth
    )
    _put_graph(client, auth, client.packet_id, {"maskHash": raw.json()["hash"], "textLock": False})
    refused = _quote(client, auth)
    assert refused.status_code == 422 and refused.json()["error"] == "mask_size", refused.text

    scaled = client.post(
        f"/api/v0/assets?kind=mask&project_id={PROJECT}&packet_id={client.packet_id}",
        content=_png(_painted()),
        headers=auth,
    )
    key = scaled.json()["hash"]
    _put_graph(client, auth, client.packet_id, {"maskHash": key, "textLock": False})
    r = _quote(client, auth)
    assert r.status_code == 200, r.text
    assert r.json()["run"]["inputs"]["mask"] == {
        "kind": "union",
        "ids": [],
        "keys": [key],
        "source": "mask:m",
    }


def test_mask_upload_packet_refusals(client, auth, tmp_path):
    png = _png(_painted())
    url = f"/api/v0/assets?kind=mask&project_id={PROJECT}"
    r = client.post(f"{url}&packet_id=pkt_NOPE", content=png, headers=auth)
    assert r.status_code == 404 and r.json()["packet_id"] == "pkt_NOPE"

    # A packet of another project is not one of this project's.
    r = client.post(
        "/api/v0/projects", json={"project_id": "other-job", "custody": {"nda": False}}, headers=auth
    )
    assert r.status_code == 201, r.text
    r = client.post(
        f"/api/v0/assets?kind=mask&project_id=other-job&packet_id={client.packet_id}", content=png
    )
    assert r.status_code == 401
    r = client.post(
        f"/api/v0/assets?kind=mask&project_id=other-job&packet_id={client.packet_id}",
        content=png,
        headers=auth,
    )
    assert r.status_code == 404
    # The rest of the T56 refusals still come first.
    assert client.post(
        f"/api/v0/assets?kind=mask&packet_id={client.packet_id}", content=png, headers=auth
    ).json() == {"error": "project_required"}
    r = client.post(f"{url}&packet_id={client.packet_id}", content=b"not an image", headers=auth)
    assert r.status_code == 422 and r.json()["error"] == "bad_mask"


def test_fit_mask_unit():
    grey = np.array([[0, 128], [127, 255]], np.uint8)
    png, w, h, scaled_from = asset_routes.fit_mask(_png(grey), (4, 4))
    assert (w, h, scaled_from) == (4, 4, [2, 2])
    out = _decode(png)
    assert out.tolist() == [[0, 0, 255, 255]] * 4  # 128 -> 255 and 127 -> 0, no new grey at the edges
    same, *_rest, none = asset_routes.fit_mask(_png(grey), (2, 2))
    assert same == _png(grey) and none is None


# ---------------------------------------------------------------------------- SAM click -> plate-sized mask


def test_segment_with_one_point_stores_a_plate_sized_mask_of_the_clicked_segment(client, auth, settings):
    x, y = 320, 180
    r = client.post(
        "/api/v0/pixels/segment",
        json={"packet_id": client.packet_id, "points": [[x + 0.6, y + 0.2]]},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stub"] is True and body["shape"] == [H, W]
    cas = CAS(settings.projects_dir / PROJECT / "assets")

    labels = _decode(cas.read_bytes(body["labels_hash"]))
    assert labels.dtype == np.uint16 and labels.shape == (H, W)
    assert body["selected"] == [int(labels[y, x])] and body["selected"][0] != 0

    mask = _decode(cas.read_bytes(body["mask_hash"]))
    assert mask.dtype == np.uint8 and mask.ndim == 2 and mask.shape == (H, W)
    assert set(np.unique(mask).tolist()) == {0, 255}
    assert mask[y, x] == 255
    assert np.array_equal(mask == 255, labels == body["selected"][0])

    # As a Mask node's samHash it quotes (the old 16-bit label map was read as "every segment").
    _put_graph(client, auth, client.packet_id, {"samHash": body["mask_hash"], "textLock": False})
    q = _quote(client, auth)
    assert q.status_code == 200, q.text
    assert q.json()["run"]["inputs"]["mask"]["keys"] == [body["mask_hash"]]


def test_segment_points_union_their_segments(client, auth, settings):
    cas = CAS(settings.projects_dir / PROJECT / "assets")
    first = client.post(
        "/api/v0/pixels/segment", json={"packet_id": client.packet_id, "points": [[5, 5]]}, headers=auth
    ).json()
    labels = _decode(cas.read_bytes(first["labels_hash"]))
    other = np.argwhere(labels != labels[5, 5])
    oy, ox = (int(v) for v in other[len(other) // 2])
    both = client.post(
        "/api/v0/pixels/segment",
        json={"packet_id": client.packet_id, "points": [[5, 5], [ox, oy], [ox, oy], [W + 50, -3]]},
        headers=auth,
    ).json()
    assert both["selected"] == sorted({int(labels[5, 5]), int(labels[oy, ox])})
    mask = _decode(cas.read_bytes(both["mask_hash"]))
    assert np.array_equal(mask == 255, np.isin(labels, both["selected"]))


def test_segment_without_points_has_labels_and_no_mask(client, auth):
    r = client.post("/api/v0/pixels/segment", json={"packet_id": client.packet_id}, headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mask_hash"] is None and body["selected"] == []
    assert isinstance(body["labels_hash"], str) and len(body["labels_hash"]) == 64
    off = client.post(
        "/api/v0/pixels/segment", json={"packet_id": client.packet_id, "points": [[-5, 2]]}, headers=auth
    ).json()
    assert off["mask_hash"] is None and off["selected"] == []  # a point off the plate selects nothing


def test_segment_selection_helpers_unit():
    labels = np.array([[0, 1, 1], [2, 2, 0]], np.uint16)
    assert pixel_routes.selected_labels(labels, [(0.9, 0.1), (1.2, 0.0), (0.0, 1.5), (2.0, 1.0), (9, 9)]) == [
        1,
        2,
    ]
    assert pixel_routes.selected_labels(labels, [(0, 0)]) == []
    grown = pixel_routes.at_plate_size(labels, (4, 6))
    assert grown.shape == (4, 6) and grown.dtype == np.uint16 and grown[3, 5] == 0 and grown[0, 2] == 1
    assert pixel_routes.at_plate_size(labels, (2, 3)) is labels
    mask = _decode(pixel_routes.selection_mask_png(labels, [2]))
    assert mask.tolist() == [[0, 0, 0], [255, 255, 0]]


# ---------------------------------------------------------------------------- references


def test_a_large_jpeg_reference_is_shrunk_to_4096_and_listed(client, auth, settings):
    r = _upload_reference(client, auth, _jpeg(5000, 3000))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "reference" and body["project_id"] == PROJECT
    assert (body["width"], body["height"]) == (4096, 2458)
    assert body["scaled_from"] == [5000, 3000]

    stored = CAS(settings.projects_dir / PROJECT / "assets").read_bytes(body["hash"])
    img = _decode(stored)
    assert img.dtype == np.uint8 and img.shape == (2458, 4096, 3)

    listed = client.get(f"/api/v0/projects/{PROJECT}/references", headers=auth)
    assert listed.status_code == 200, listed.text
    entries = listed.json()
    assert [e["hash"] for e in entries] == [body["hash"]]
    entry = entries[0]
    assert (entry["width"], entry["height"], entry["bytes"]) == (4096, 2458, len(stored))
    assert entry["uploaded_utc"].endswith("Z") and "note" not in entry

    doc = json.loads((settings.projects_dir / PROJECT / "references.json").read_text(encoding="utf-8"))
    assert doc == {"schema": 1, "references": entries}


def test_reference_upload_is_idempotent_and_keeps_a_note(client, auth):
    small = _jpeg(300, 200)
    first = _upload_reference(client, auth, small, f"project_id={PROJECT}&note=warm%20oak%20sample")
    assert first.status_code == 201, first.text
    assert first.json()["scaled_from"] is None and (first.json()["width"], first.json()["height"]) == (
        300,
        200,
    )
    again = _upload_reference(client, auth, small, f"project_id={PROJECT}&note=another")
    assert again.status_code == 201 and again.json()["hash"] == first.json()["hash"]
    entries = client.get(f"/api/v0/projects/{PROJECT}/references", headers=auth).json()
    assert len(entries) == 1 and entries[0]["note"] == "warm oak sample"  # the first entry stands

    second = _upload_reference(client, auth, _jpeg(320, 200))
    entries = client.get(f"/api/v0/projects/{PROJECT}/references", headers=auth).json()
    assert [e["hash"] for e in entries] == [first.json()["hash"], second.json()["hash"]]


def test_a_reference_carries_pixels_only(client, auth, settings):
    secret = "SECRET-CLIENT-TOWER-B"
    jpeg = _jpeg(640, 400, description=secret)
    assert secret.encode() in jpeg
    r = _upload_reference(client, auth, jpeg)
    assert r.status_code == 201, r.text
    stored = CAS(settings.projects_dir / PROJECT / "assets").read_bytes(r.json()["hash"])
    assert secret.encode() not in stored and b"Client Photographer" not in stored
    assert set(_png_chunks(stored)) <= {"IHDR", "IDAT", "IEND"}
    assert stored[24:26] == bytes([8, 2])  # IHDR: 8-bit, colour type 2 (RGB)

    # An RGBA PNG loses its alpha; a 16-bit PNG is stored at 8 bits.
    rgba = np.dstack([np.full((50, 80, 3), 120, np.uint8), np.full((50, 80), 7, np.uint8)])
    r = _upload_reference(client, auth, _png(rgba))
    assert r.status_code == 201, r.text
    back = _decode(CAS(settings.projects_dir / PROJECT / "assets").read_bytes(r.json()["hash"]))
    assert back.shape == (50, 80, 3) and back.dtype == np.uint8
    deep = (np.random.default_rng(1).integers(0, 65536, (40, 60, 3))).astype(np.uint16)
    r = _upload_reference(client, auth, _png(deep))
    assert r.status_code == 201, r.text
    back = _decode(CAS(settings.projects_dir / PROJECT / "assets").read_bytes(r.json()["hash"]))
    assert back.dtype == np.uint8 and back.shape == (40, 60, 3)


def test_reference_refusals(client, auth, monkeypatch):
    jpeg = _jpeg(64, 48)
    r = client.post("/api/v0/assets?kind=reference", content=jpeg, headers=auth)
    assert r.status_code == 422 and r.json() == {"error": "project_required"}
    r = client.post("/api/v0/assets?kind=reference&project_id=ghost", content=jpeg, headers=auth)
    assert r.status_code == 404
    r = client.post("/api/v0/assets?kind=reference&project_id=Bad Id", content=jpeg, headers=auth)
    assert r.status_code == 422
    r = client.post("/api/v0/assets?kind=sticker&project_id=" + PROJECT, content=jpeg, headers=auth)
    assert r.status_code == 422 and r.json()["error"] == "bad_kind"
    for bad in (b"", b"not an image", b"GIF89a" + bytes(64), jpeg[:3] + bytes(40)):
        r = _upload_reference(client, auth, bad)
        assert r.status_code == 422 and r.json()["error"] == "bad_image", (bad[:8], r.text)
    tiff = cv2.imencode(".tiff", np.zeros((8, 8, 3), np.uint8))[1].tobytes()
    r = _upload_reference(client, auth, tiff)
    assert r.status_code == 422 and r.json()["error"] == "bad_image"  # PNG or JPEG only
    assert client.post(f"/api/v0/assets?kind=reference&project_id={PROJECT}", content=jpeg).status_code == 401

    monkeypatch.setattr("plate_daemon.routes.assets.REFERENCE_MAX_BYTES", 1000)
    big = _jpeg(400, 400)
    assert len(big) > 1000
    r = _upload_reference(client, auth, big)
    assert r.status_code == 413 and r.json()["error"] == "too_large"
    assert client.get(f"/api/v0/projects/{PROJECT}/references", headers=auth).json() == []


def test_references_list_refusals(client, auth, settings):
    assert client.get("/api/v0/projects/ghost/references", headers=auth).status_code == 404
    assert client.get(f"/api/v0/projects/{PROJECT}/references").status_code == 401
    (settings.projects_dir / PROJECT / "references.json").write_text("[1, 2]", encoding="utf-8", newline="\n")
    r = client.get(f"/api/v0/projects/{PROJECT}/references", headers=auth)
    assert r.status_code == 409 and r.json()["error"] == "references_unreadable"
    r = _upload_reference(client, auth, _jpeg(32, 32))
    assert r.status_code == 409 and r.json()["error"] == "references_unreadable"


def test_reference_limit_is_32_mib():
    assert asset_routes.REFERENCE_MAX_BYTES == 32 * 1024 * 1024
    assert asset_routes.REFERENCE_MAX_SIDE == 4096
