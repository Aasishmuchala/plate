"""Assets by reference, `POST /assets?kind=mask`, and streamed non-image assets (router plan T56).

The plate of the committed Blender packet `fixtures/packets/room_a` is served from the packet folder by its
BLAKE3 (never copied into the CAS); masks are uploaded as raw image bytes and land in the project's CAS.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from plate_daemon import assets
from plate_daemon.api import create_app
from plate_daemon.cas import CAS
from plate_daemon.config import Settings

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"

pytestmark = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file(), reason="fixtures/packets/room_a not built yet (T2)"
)


@pytest.fixture
def sync_client(plate_env, monkeypatch):
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    settings = Settings.load()
    with TestClient(create_app(settings), base_url=BASE) as c:
        yield c, settings, {"Authorization": f"Bearer {settings.token}"}


def _manifest(path: Path = ROOM_A) -> dict:
    return json.loads((path / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture
def ingested(sync_client):
    """(client, settings, auth, packet_id) with room_a ingested into `plate-fixture`."""
    c, settings, auth = sync_client
    r = c.post(
        "/api/v0/packets",
        json={"packet_path": str(ROOM_A), "sender": {"name": "t", "version": "0"}},
        headers={**auth, "X-Plate-Packet-Version": _manifest()["packet_version"]},
    )
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    assert c.get(f"/api/v0/packets/{packet_id}", headers=auth).json()["status"] == "ingested"
    return c, settings, auth, packet_id


def _plate_hash() -> str:
    return _manifest()["files"]["display_tiff16"]["hash"]["hex"]


def _mask(h: int = 360, w: int = 640) -> np.ndarray:
    m = np.zeros((h, w), np.uint8)
    m[60:200, 100:420] = 255
    m[220:300, 30:90] = 128
    return m


def _png(arr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", arr)
    assert ok
    return bytes(buf)


def _decode(data: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)


# ---------------------------------------------------------------------------- GET: by reference


def test_plate_is_served_by_reference_in_every_form(ingested):
    c, settings, auth, _pid = ingested
    key = _plate_hash()
    url = f"/api/v0/assets/{key}"

    thumb = c.get(f"{url}?fmt=thumb", headers=auth)
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/png"
    assert thumb.headers["x-plate-source"] == "packet" and "immutable" in thumb.headers["cache-control"]
    t = _decode(thumb.content)
    assert t.dtype == np.uint8 and t.shape == (180, 320, 3)

    half = c.get(f"{url}?fmt=rgba16f", headers=auth)
    assert half.status_code == 200 and half.headers["x-plate-source"] == "packet"
    assert len(half.content) == 1_843_200
    assert (half.headers["x-plate-width"], half.headers["x-plate-height"]) == ("640", "360")
    rgba = np.frombuffer(half.content, "<f2").reshape(360, 640, 4)
    plate = cv2.imread(str(ROOM_A / "display.tif"), cv2.IMREAD_UNCHANGED)
    assert plate.dtype == np.uint16
    assert np.allclose(rgba[..., :3], plate[..., ::-1].astype(np.float32) / 65535.0, atol=1e-3)
    assert np.all(rgba[..., 3] == 1.0)

    png = c.get(f"{url}?fmt=png", headers=auth)
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">IIBB", png.content[16:26])[:3] == (640, 360, 16)  # IHDR: 16-bit
    assert np.array_equal(_decode(png.content), plate)

    # The same bytes by the explicit project, and a project that does not exist is a clean 404.
    assert c.get(f"{url}?fmt=thumb&project_id={PROJECT}", headers=auth).status_code == 200
    assert c.get(f"{url}?project_id=ghost", headers=auth).status_code == 404
    assert not (settings.home / "index" / "ghost.sqlite").exists()

    # By reference: nothing about the plate entered the project's CAS.
    cas = CAS(settings.projects_dir / PROJECT / "assets")
    assert cas.find(key) is None


def test_no_fmt_streams_the_packet_file_with_range(ingested):
    c, _settings, auth, _pid = ingested
    key = _plate_hash()
    size = (ROOM_A / "display.tif").stat().st_size

    full = c.get(f"/api/v0/assets/{key}", headers=auth)
    assert full.status_code == 200 and full.headers["content-type"] == "image/tiff"
    assert full.headers["x-plate-source"] == "packet" and "immutable" in full.headers["cache-control"]
    assert full.content == (ROOM_A / "display.tif").read_bytes()
    assert full.headers["accept-ranges"] == "bytes" and int(full.headers["content-length"]) == size

    part = c.get(f"/api/v0/assets/{key}", headers={**auth, "Range": "bytes=0-99"})
    assert part.status_code == 206
    assert part.headers["content-length"] == "100" and part.headers["accept-ranges"] == "bytes"
    assert part.headers["content-range"] == f"bytes 0-99/{size}"
    assert part.content == full.content[:100]
    assert part.headers["x-plate-source"] == "packet"

    tail = c.get(f"/api/v0/assets/{key}", headers={**auth, "Range": "bytes=-50"})
    assert tail.status_code == 206 and tail.content == full.content[-50:]


def test_non_image_packet_files_stream_and_refuse_pixel_formats(ingested):
    c, _settings, auth, _pid = ingested
    legend = _manifest()["files"]["id_legend"]["hash"]["hex"]
    r = c.get(f"/api/v0/assets/{legend}", headers=auth)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.content == (ROOM_A / "id_legend.json").read_bytes()
    assert c.get(f"/api/v0/assets/{legend}?fmt=thumb", headers=auth).status_code == 415

    exr = _manifest()["files"]["beauty_exr"]["hash"]["hex"]  # served as bytes, never decoded
    r = c.get(f"/api/v0/assets/{exr}", headers={**auth, "Range": "bytes=0-3"})
    assert r.status_code == 206 and r.headers["content-type"] == "image/x-exr"
    assert r.content == (ROOM_A / "beauty.exr").read_bytes()[:4]
    assert c.get(f"/api/v0/assets/{exr}?fmt=png", headers=auth).status_code == 415


def test_cas_object_still_answers_with_cas_source(ingested):
    c, settings, auth, _pid = ingested
    cas = CAS(settings.projects_dir / PROJECT / "assets")
    key = cas.put_bytes(b'{"a": 1}\n', "json")
    r = c.get(f"/api/v0/assets/{key}", headers=auth)
    assert r.status_code == 200 and r.content == b'{"a": 1}\n'
    assert r.headers["x-plate-source"] == "cas" and "immutable" in r.headers["cache-control"]
    assert r.headers["content-type"].startswith("application/json")
    ranged = c.get(f"/api/v0/assets/{key}", headers={**auth, "Range": "bytes=0-3"})
    assert ranged.status_code == 206 and ranged.content == b'{"a"'
    # A CAS object wins over a packet file with the same hash, and a stored PNG is served untouched.
    png = _png(_mask())
    pkey = cas.put_bytes(png, "png")
    assert c.get(f"/api/v0/assets/{pkey}?fmt=png", headers=auth).content == png


def test_unknown_and_malformed_keys_and_missing_packet_files(ingested):
    c, _settings, auth, pid = ingested
    assert c.get(f"/api/v0/assets/{'0' * 64}", headers=auth).status_code == 404
    assert c.get(f"/api/v0/assets/{'0' * 64}?fmt=thumb", headers=auth).status_code == 404
    assert c.get("/api/v0/assets/not-a-hash", headers=auth).status_code == 404
    assert c.get(f"/api/v0/assets/{_plate_hash()}").status_code == 401
    assert c.post("/api/v0/assets?kind=mask&project_id=" + PROJECT, content=b"x").status_code == 401


def test_a_packet_file_that_left_the_disk_is_a_404(tmp_path, ingested):
    c, settings, auth, pid = ingested
    key = _plate_hash()
    with c.app.state.index.project(PROJECT) as conn:
        conn.execute("UPDATE packets SET path = ? WHERE packet_id = ?", (str(tmp_path / "moved"), pid))
    assert c.get(f"/api/v0/assets/{key}", headers=auth).status_code == 404


def test_locate_refuses_a_relpath_that_leaves_the_packet_folder(tmp_path, ingested):
    c, settings, auth, pid = ingested
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret")
    key = "ab" * 32
    with c.app.state.index.project(PROJECT) as conn:
        conn.execute(
            "INSERT INTO blobs (packet_id, relpath, role, size, blake3, in_cas) VALUES (?, ?, 'x', 6, ?, 0)",
            (pid, "../outside.bin", key),
        )
        conn.execute("UPDATE packets SET path = ? WHERE packet_id = ?", (str(tmp_path / "pk"), pid))
    (tmp_path / "pk").mkdir()
    assert c.get(f"/api/v0/assets/{key}", headers=auth).status_code == 404
    with pytest.raises(assets.AssetNotFound):
        assets.locate(key, [PROJECT], c.app.state.cas_for, PROJECT, index=c.app.state.index)


def test_locate_without_an_index_is_cas_only(ingested):
    c, _settings, _auth, _pid = ingested
    with pytest.raises(assets.AssetNotFound):
        assets.locate(_plate_hash(), [PROJECT], c.app.state.cas_for)
    a = assets.locate(_plate_hash(), [PROJECT], c.app.state.cas_for, index=c.app.state.index)
    assert a.by_reference and a.ext == "tif" and a.project_id == PROJECT


# ---------------------------------------------------------------------------- POST: masks


def test_mask_upload_round_trips_through_the_cas(ingested):
    c, settings, auth, _pid = ingested
    mask = _mask()
    r = c.post(
        f"/api/v0/assets?kind=mask&project_id={PROJECT}",
        content=_png(mask),
        headers={**auth, "Content-Type": "image/png"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["width"] == 640 and body["height"] == 360 and body["project_id"] == PROJECT
    key = body["hash"]
    assert CAS(settings.projects_dir / PROJECT / "assets").find(key) is not None

    got = c.get(f"/api/v0/assets/{key}", headers=auth)
    assert got.status_code == 200 and got.headers["x-plate-source"] == "cas"
    back = _decode(got.content)
    assert back.dtype == np.uint8 and back.ndim == 2 and np.array_equal(back, mask)

    again = c.post(f"/api/v0/assets?kind=mask&project_id={PROJECT}", content=_png(mask), headers=auth)
    assert again.status_code == 201 and again.json()["hash"] == key  # content-addressed


def test_mask_upload_flattens_rgb_and_rgba_to_the_first_channel(ingested):
    c, _settings, auth, _pid = ingested
    mask = _mask(90, 160)
    rgb = np.zeros((90, 160, 3), np.uint8)
    rgb[..., 2] = mask  # cv2 order is BGR: this is the PNG's red channel
    rgb[..., 0] = 7
    rgba = np.dstack([rgb, np.full((90, 160), 255, np.uint8)])
    for arr in (rgb, rgba):
        r = c.post(f"/api/v0/assets?kind=mask&project_id={PROJECT}", content=_png(arr), headers=auth)
        assert r.status_code == 201, r.text
        assert (r.json()["width"], r.json()["height"]) == (160, 90)
        back = _decode(c.get(f"/api/v0/assets/{r.json()['hash']}", headers=auth).content)
        assert back.ndim == 2 and np.array_equal(back, mask)


def test_mask_upload_refusals(ingested, monkeypatch):
    c, _settings, auth, _pid = ingested
    png = _png(_mask())
    url = "/api/v0/assets"

    r = c.post(f"{url}?kind=other&project_id={PROJECT}", content=png, headers=auth)
    assert r.status_code == 422 and r.json()["error"] == "bad_kind"
    r = c.post(f"{url}?project_id={PROJECT}", content=png, headers=auth)
    assert r.status_code == 422 and r.json()["error"] == "bad_kind"
    r = c.post(f"{url}?kind=mask", content=png, headers=auth)
    assert r.status_code == 422 and r.json() == {"error": "project_required"}
    r = c.post(f"{url}?kind=mask&project_id=ghost", content=png, headers=auth)
    assert r.status_code == 404
    r = c.post(f"{url}?kind=mask&project_id=Bad Id", content=png, headers=auth)
    assert r.status_code == 422
    # Bytes with no image header are judged on the header (T136): `bad_image`, before any decode.
    for body in (b"not an image", b""):
        r = c.post(f"{url}?kind=mask&project_id={PROJECT}", content=body, headers=auth)
        assert r.status_code == 422, r.text
        assert r.json() == {"error": "bad_image", "detail": "not a PNG/JPEG/WebP/BMP/TIFF header"}
    # A PNG header that is fine on a body cv2 cannot read is still a mask refusal, after the decode.
    r = c.post(f"{url}?kind=mask&project_id={PROJECT}", content=png[:40], headers=auth)
    assert r.status_code == 422 and r.json()["error"] == "bad_mask"
    r = c.post(
        f"{url}?kind=mask&project_id={PROJECT}", content=_png(_mask().astype(np.uint16) * 257), headers=auth
    )
    assert r.status_code == 422 and r.json()["error"] == "bad_mask"

    # The size limit: by Content-Length, and on a chunked body with no length to read.
    monkeypatch.setattr("plate_daemon.routes.assets.MASK_MAX_BYTES", 1000)
    big = _png(np.random.default_rng(0).integers(0, 256, (200, 200), dtype=np.uint8))
    assert len(big) > 1000
    r = c.post(f"{url}?kind=mask&project_id={PROJECT}", content=big, headers=auth)
    assert r.status_code == 413 and r.json()["error"] == "too_large"

    def chunks():
        for i in range(0, len(big), 700):
            yield big[i : i + 700]

    r = c.post(f"{url}?kind=mask&project_id={PROJECT}", content=chunks(), headers=auth)
    assert r.status_code == 413
    r = c.post(
        f"{url}?kind=mask&project_id={PROJECT}", content=png[:900] if len(png) > 900 else png, headers=auth
    )
    assert r.status_code in (201, 422)  # under the limit: it is judged on its bytes, not refused for size


def test_mask_limit_is_32_mib():
    assert assets.MASK_MAX_BYTES == 32 * 1024 * 1024
