"""The pixels client and `/pixels/idpick|segment|ocr` (router plan T62).

`idpick` is answered by the daemon from the packet's own Cryptomatte, so those tests read the same EXR
(`plate_pixels.exr`) to find their pixels. `segment` and `ocr` go to the pixels worker: in this process
(`PLATE_PIXELS_INPROC=1`, stub backends) for most tests, over a real socket to a real worker app for the
HTTP contract (Host guard, bearer token, file paths in and out), and to a port nothing listens on for the 503.
Packets are `fixtures/packets/room_a` by reference (never written to) and a Tier C packet built from a tmp
PNG.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
import uvicorn
from fastapi.testclient import TestClient

import plate_spec
from plate_daemon import pixels_client
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.pixels_client import INPROC_ENV, PORT_ENV, PixelsClient, PixelsError, PixelsUnavailable
from plate_daemon.routes import pixels as pixels_routes
from plate_pixels import exr as pexr

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"
SHAPE = (360, 640)

pytestmark = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


# ---------------------------------------------------------------------------- fixtures


@pytest.fixture
def settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.setenv(INPROC_ENV, "1")
    monkeypatch.delenv("PLATE_PIXELS_REAL", raising=False)
    monkeypatch.delenv(PORT_ENV, raising=False)
    return Settings.load()


def _ingest(c: TestClient, auth: dict, folder: Path) -> str:
    version = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["packet_version"]
    r = c.post(
        "/api/v0/packets",
        json={"packet_path": str(folder), "sender": {"name": "test", "version": "0"}},
        headers={**auth, "X-Plate-Packet-Version": version},
    )
    assert r.status_code == 202, r.text
    return r.json()["packet_id"]


@pytest.fixture
def client(settings, auth) -> TestClient:
    with TestClient(create_app(settings), base_url=BASE) as c:
        c.packet_id = _ingest(c, auth, ROOM_A)
        yield c


def _plate_png(path: Path, width: int = 96, height: int = 64) -> Path:
    y, x = np.mgrid[0:height, 0:width]
    img = np.stack([x * 255 // width, y * 255 // height, (x + y) * 255 // (width + height)], axis=-1)
    assert cv2.imwrite(str(path), np.ascontiguousarray(img.astype(np.uint8)[..., ::-1]))
    return path


def _tier_c(c: TestClient, auth: dict, tmp_path: Path) -> tuple[str, Path]:
    """A Tier C packet built from a tmp PNG (in-process stub worker): (packet id, packet folder)."""
    r = c.post("/api/v0/projects", json={"project_id": "client-job", "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text
    r = c.post(
        "/api/v0/projects/client-job/tierc",
        json={
            "plate_path": str(_plate_png(tmp_path / "plate.png")),
            "shot_id": "hero",
            "consent_record": "ok",
        },
        headers=auth,
    )
    assert r.status_code == 202 and r.json()["status"] == "ingested", r.text
    return r.json()["packet_id"], Path(r.json()["packet_dir"])


# ---------------------------------------------------------------------------- what the EXR says


@pytest.fixture(scope="module")
def exr_truth():
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    ids, cov = pexr.read_cryptomatte(ROOM_A / "beauty.exr", manifest, "object")
    z = pexr.read_elements(ROOM_A / "beauty.exr", manifest, ["zdepth"])["zdepth"][..., 0]
    return SimpleNamespace(manifest=manifest, ids=ids, cov=cov, z=z)


def _interior(truth, name: str) -> tuple[int, int]:
    """(x, y) of a pixel the object owns outright: rank 0 is its id with coverage ~1."""
    want = np.uint32(int(plate_spec.cryptomatte_id(name)[2:], 16))
    hits = np.argwhere((truth.ids[..., 0] == want) & (truth.cov[..., 0] >= 0.99))
    assert len(hits), f"{name} has no interior pixel in room_a"
    y, x = hits[len(hits) // 2]
    return int(x), int(y)


def _idpick(c, auth, packet_id, x, y, **extra):
    return c.post(
        "/api/v0/pixels/idpick", json={"packet_id": packet_id, "x": x, "y": y, **extra}, headers=auth
    )


# ---------------------------------------------------------------------------- idpick


def test_idpick_names_the_shrub_and_says_it_is_editable(client, auth, exr_truth):
    x, y = _interior(exr_truth, "VEG_shrub_A")
    r = _idpick(client, auth, client.packet_id, x, y)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == plate_spec.cryptomatte_id("VEG_shrub_A") == "o:55cbe981"
    assert body["name"] == "VEG_shrub_A" and body["class"] == "vegetation" and body["kind"] == "object"
    assert body["editable"] is True and body["locked"] is False
    assert body["coverage"] >= 0.99
    assert body["depth_m"] > 0
    assert body["depth_m"] == pytest.approx(float(exr_truth.z[y, x]) * 1.0)  # room_a: meters_per_unit 1


def test_idpick_on_architecture_says_locked(client, auth, exr_truth):
    x, y = _interior(exr_truth, "ARCH_wall_N")
    body = _idpick(client, auth, client.packet_id, x, y).json()
    assert body["id"] == "o:57b3d007" and body["name"] == "ARCH_wall_N" and body["class"] == "architecture"
    assert body["locked"] is True and body["editable"] is False


def test_idpick_material_kind_answers_with_an_m_id(client, auth, exr_truth):
    x, y = _interior(exr_truth, "VEG_shrub_A")
    body = _idpick(client, auth, client.packet_id, x, y, kind="material").json()
    assert body["id"].startswith("m:") and body["kind"] == "material"
    assert body["name"] == "VEG_leaf" and body["editable"] is True and body["locked"] is False
    wall = _idpick(
        client, auth, client.packet_id, *_interior(exr_truth, "ARCH_wall_N"), kind="material"
    ).json()
    assert wall["id"] == "m:57b3d007" and wall["locked"] is True


def test_idpick_has_no_depth_where_the_renderer_hit_nothing(client, auth, exr_truth):
    sky = np.argwhere(exr_truth.z >= 1.0e9)
    assert len(sky), "room_a should have background Z"
    y, x = (int(v) for v in sky[len(sky) // 2])
    body = _idpick(client, auth, client.packet_id, x, y).json()
    assert body["depth_m"] is None
    assert body["id"] is None or body["id"].startswith("o:")


def test_idpick_on_a_pixel_nothing_covers_has_no_id(client, auth, exr_truth):
    empty = np.argwhere(exr_truth.ids[..., 0] == 0)
    assert len(empty), "room_a should have uncovered pixels"
    y, x = (int(v) for v in empty[0])
    body = _idpick(client, auth, client.packet_id, x, y).json()
    assert body["id"] is None and body["name"] is None and body["editable"] is False
    assert body["locked"] is False and body["coverage"] == 0.0


def test_idpick_on_an_edge_pixel_names_the_larger_contributor(client, auth, exr_truth):
    edge = np.argwhere(
        (exr_truth.cov[..., 0] > 0) & (exr_truth.cov[..., 0] < 0.5) & (exr_truth.ids[..., 0] != 0)
    )
    assert len(edge), "room_a should have partially covered pixels"
    y, x = (int(v) for v in edge[0])
    body = _idpick(client, auth, client.packet_id, x, y).json()
    best = int(np.argmax(exr_truth.cov[y, x]))
    assert body["id"] == f"o:{int(exr_truth.ids[y, x, best]):08x}"
    assert body["coverage"] == pytest.approx(float(exr_truth.cov[y, x, best]))
    assert body["coverage"] < 0.5


def test_idpick_floors_fractional_pixels(client, auth, exr_truth):
    x, y = _interior(exr_truth, "VEG_shrub_A")
    a = _idpick(client, auth, client.packet_id, x, y).json()
    b = _idpick(client, auth, client.packet_id, x + 0.9, y + 0.4).json()
    assert a == b


def test_idpick_refusals(client, auth, tmp_path):
    assert _idpick(client, auth, "pkt_NOPE", 1, 1).status_code == 404
    for x, y in ((640, 0), (0, 360), (-1, 0), (0, -0.5)):
        r = _idpick(client, auth, client.packet_id, x, y)
        assert r.status_code == 422, (x, y, r.text)
        assert r.json()["error"] == "out_of_range" and r.json()["shape"] == [360, 640]
    assert _idpick(client, auth, client.packet_id, 1, 1, kind="surface").status_code == 422
    assert (
        client.post("/api/v0/pixels/idpick", json={"packet_id": client.packet_id}, headers=auth).status_code
        == 422
    )
    assert client.post("/api/v0/pixels/idpick", json={"packet_id": "p", "x": 1, "y": 1}).status_code == 401

    tier_c, _folder = _tier_c(client, auth, tmp_path)
    r = _idpick(client, auth, tier_c, 3, 3)
    assert r.status_code == 422 and r.json() == {"error": "no_cryptomatte"}


def test_idpick_needs_no_pixels_worker(client, auth, exr_truth, monkeypatch):
    """idpick is the daemon's own: it answers with the worker neither in process nor listening."""
    monkeypatch.delenv(INPROC_ENV, raising=False)
    monkeypatch.setenv(PORT_ENV, str(_free_port()))
    x, y = _interior(exr_truth, "VEG_shrub_A")
    assert _idpick(client, auth, client.packet_id, x, y).json()["name"] == "VEG_shrub_A"


def test_pick_rank_prefers_a_dominant_rank_zero_else_the_best_coverage():
    ids = np.array([7, 8, 9, 0], dtype=np.uint32)
    assert pixels_routes.pick_rank(ids, np.array([0.5, 0.4, 0.1, 0.0], np.float32)) == 0  # dominant: >= 0.5
    assert pixels_routes.pick_rank(ids, np.array([0.3, 0.6, 0.1, 0.0], np.float32)) == 1  # unsorted ranks
    assert pixels_routes.pick_rank(ids, np.array([0.3, 0.2, 0.1, 0.0], np.float32)) == 0  # best of the weak
    assert pixels_routes.pick_rank(np.zeros(4, np.uint32), np.zeros(4, np.float32)) is None
    assert (
        pixels_routes.pick_rank(np.array([0, 5, 0, 0], np.uint32), np.array([0.9, 0.1, 0, 0], np.float32))
        == 1
    )


def test_depth_m_converts_scene_units_with_the_manifest():
    pv = SimpleNamespace(
        manifest={"units": {"meters_per_unit": 0.001}},
        files={"beauty_exr": {"path": "x.exr"}},
        root=Path("."),
        _cache={"pixels_z": np.array([[2500.0, 1.0e10], [0.0, np.nan]], np.float32)},
    )
    assert pixels_routes.depth_m_at(pv, 0, 0) == pytest.approx(2.5)
    assert pixels_routes.depth_m_at(pv, 1, 0) is None  # nothing hit
    assert pixels_routes.depth_m_at(pv, 0, 1) is None  # not a depth
    assert pixels_routes.depth_m_at(pv, 1, 1) is None
    pv._cache["pixels_z"] = None
    assert pixels_routes.depth_m_at(pv, 0, 0) is None  # tier C: no Z


# ---------------------------------------------------------------------------- segment


def test_segment_stores_the_label_map_in_the_project_cas(client, auth):
    r = client.post(
        "/api/v0/pixels/segment", json={"packet_id": client.packet_id, "points": [[320, 180]]}, headers=auth
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["shape"] == list(SHAPE) and body["stub"] is True
    assert body["segments"] and {"id", "bbox", "area"} <= set(body["segments"][0])
    got = client.get(f"/api/v0/assets/{body['labels_hash']}", headers=auth)
    assert got.status_code == 200 and got.headers["content-type"] == "image/png"
    labels = cv2.imdecode(np.frombuffer(got.content, np.uint8), cv2.IMREAD_UNCHANGED)
    assert labels.shape == SHAPE and labels.dtype == np.uint16
    assert sorted(int(v) for v in np.unique(labels) if v) == sorted(s["id"] for s in body["segments"])
    assert body["selected"] == [int(labels[180, 320])]  # T82: the clicked segment, as a plate-sized mask
    # the same call again lands on the same content-named objects
    again = client.post(
        "/api/v0/pixels/segment", json={"packet_id": client.packet_id, "points": [[320, 180]]}, headers=auth
    )
    assert again.json()["labels_hash"] == body["labels_hash"]
    assert again.json()["mask_hash"] == body["mask_hash"]


def test_segment_without_points_and_with_bad_input(client, auth):
    r = client.post("/api/v0/pixels/segment", json={"packet_id": client.packet_id}, headers=auth)
    assert r.status_code == 200 and r.json()["segments"]
    assert (
        client.post("/api/v0/pixels/segment", json={"packet_id": "pkt_NOPE"}, headers=auth).status_code == 404
    )
    for bad in ([[1, 2, 3]], [["a", "b"]], [[1, 2]] * 65, "nope"):
        r = client.post(
            "/api/v0/pixels/segment", json={"packet_id": client.packet_id, "points": bad}, headers=auth
        )
        assert r.status_code == 422, (bad, r.text)
    assert client.post("/api/v0/pixels/segment", json={"packet_id": client.packet_id}).status_code == 401


def test_segment_and_ocr_work_on_a_tier_c_packet(client, auth, tmp_path):
    packet_id, folder = _tier_c(client, auth, tmp_path)
    r = client.post(
        "/api/v0/pixels/segment", json={"packet_id": packet_id, "points": [[10, 10]]}, headers=auth
    )
    assert r.status_code == 200 and r.json()["shape"] == [64, 96], r.text
    (folder / "client_plate.png.ocr.json").write_text(
        json.dumps([{"bbox": [10.2, 5, 40, 20.5], "text": "EXIT", "conf": 0.9}]),
        encoding="utf-8",
        newline="\n",
    )
    r = client.post("/api/v0/pixels/ocr", json={"packet_id": packet_id}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "boxes": [{"bbox": [10, 5, 40, 21], "text": "EXIT", "conf": 0.9}],
        "shape": [64, 96],
        "stub": True,
    }


def test_a_missing_display_master_is_a_409_not_a_500(client, auth, tmp_path):
    packet_id, folder = _tier_c(client, auth, tmp_path)
    (folder / "client_plate.png").unlink()
    for route in ("segment", "ocr"):
        r = client.post(f"/api/v0/pixels/{route}", json={"packet_id": packet_id}, headers=auth)
        assert r.status_code == 409 and r.json()["error"] == "packet_unreadable", (route, r.text)


# ---------------------------------------------------------------------------- ocr


def test_ocr_reports_the_plate_shape(client, auth):
    r = client.post("/api/v0/pixels/ocr", json={"packet_id": client.packet_id}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json() == {"boxes": [], "shape": [360, 640], "stub": True}
    assert client.post("/api/v0/pixels/ocr", json={"packet_id": "pkt_NOPE"}, headers=auth).status_code == 404
    assert client.post("/api/v0/pixels/ocr", json={"packet_id": client.packet_id}).status_code == 401


# ---------------------------------------------------------------------------- the worker is not running


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_http_mode_with_nothing_listening_is_503(client, auth, monkeypatch):
    monkeypatch.delenv(INPROC_ENV, raising=False)
    monkeypatch.setenv(PORT_ENV, str(_free_port()))
    for route, body in (("segment", {"points": [[1, 1]]}), ("ocr", {})):
        r = client.post(f"/api/v0/pixels/{route}", json={"packet_id": client.packet_id, **body}, headers=auth)
        assert r.status_code == 503, (route, r.text)
        assert r.json()["error"] == "pixels_unavailable"
        assert "python -m plate_pixels" in r.json()["detail"] and INPROC_ENV in r.json()["detail"]


# ---------------------------------------------------------------------------- a real worker on a socket


class Worker:
    def __init__(self, token: str):
        from plate_pixels.backends import Backends
        from plate_pixels.server import create_app as create_worker

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.server = uvicorn.Server(
            uvicorn.Config(
                create_worker(token, Backends(), port=self.port), log_level="warning", lifespan="on"
            )
        )
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)

    def __enter__(self) -> Worker:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline and self.thread.is_alive(), "the worker did not start"
            time.sleep(0.02)
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(10)
        self.sock.close()


def test_routes_against_a_real_worker_over_http(client, auth, settings, monkeypatch):
    """The contract between the two processes: the Host guard, the bearer token, paths in and out."""
    with Worker(settings.token) as worker:
        monkeypatch.delenv(INPROC_ENV, raising=False)
        monkeypatch.setenv(PORT_ENV, str(worker.port))
        r = client.post(
            "/api/v0/pixels/segment", json={"packet_id": client.packet_id, "points": [[5, 5]]}, headers=auth
        )
        assert r.status_code == 200, r.text
        assert r.json()["shape"] == list(SHAPE) and r.json()["stub"] is True and r.json()["segments"]
        got = client.get(f"/api/v0/assets/{r.json()['mask_hash']}", headers=auth)
        assert cv2.imdecode(np.frombuffer(got.content, np.uint8), cv2.IMREAD_UNCHANGED).shape == SHAPE
        r = client.post("/api/v0/pixels/ocr", json={"packet_id": client.packet_id}, headers=auth)
        assert r.status_code == 200 and r.json() == {"boxes": [], "shape": [360, 640], "stub": True}


def test_a_worker_with_another_token_is_a_502_naming_the_cause(client, auth, monkeypatch):
    with Worker("a-different-token-of-sufficient-length") as worker:
        monkeypatch.delenv(INPROC_ENV, raising=False)
        monkeypatch.setenv(PORT_ENV, str(worker.port))
        r = client.post("/api/v0/pixels/ocr", json={"packet_id": client.packet_id}, headers=auth)
        assert r.status_code == 502 and r.json()["error"] == "pixels_auth", r.text


def test_an_injected_client_is_used_and_model_errors_are_503(client, auth):
    from plate_pixels.backends import Backends, ModelUnavailable, StubSegment

    class Missing(StubSegment):
        def segment(self, rgb, prompts=None):
            raise ModelUnavailable("sam2.1: not installed (sam2) and PLATE_PIXELS_REAL=1")

    client.app.state.pixels = PixelsClient(
        client.app.state.settings, inproc=True, backends=Backends(segment=Missing())
    )
    r = client.post("/api/v0/pixels/segment", json={"packet_id": client.packet_id}, headers=auth)
    assert r.status_code == 503 and r.json()["error"] == "model_unavailable"
    assert "sam2.1" in r.json()["detail"]


def test_the_workers_status_codes_map_to_the_daemons():
    f = pixels_routes.pixels_failure
    assert (f(PixelsError(400, "bad image")).status, f(PixelsError(400, "bad image")).body) == (
        422,
        {"errors": ["bad image"]},
    )
    assert f(PixelsError(401, "unauthorized")).body["error"] == "pixels_auth"
    assert f(PixelsError(403, "forbidden_host")).status == 502
    assert f(PixelsError(500, "no model", {"real_required": True})).body["error"] == "model_unavailable"
    assert f(PixelsError(500, "bug")).status == 502
    assert f(PixelsError(501, "not wired")).status == 502
    assert f(PixelsError(503, "gpu busy")).status == 503
    assert f(PixelsError(504, "slow")).body["error"] == "pixels_timeout"


# ---------------------------------------------------------------------------- the client on its own


def _write_png(path: Path, w: int = 96, h: int = 64) -> Path:
    return _plate_png(path, w, h)


def test_client_in_process_segment_ocr_depth(settings, tmp_path):
    c = PixelsClient(settings)
    assert c.inproc is True and c.status() == "inproc" and c.available() is True
    plate = _write_png(tmp_path / "p.png")

    seg = c.segment(plate, [[3, 4]])
    assert seg["shape"] == [64, 96] and seg["stub"] is True and seg["segments"]
    labels_path = Path(seg["labels_png_path"])
    assert labels_path.parent == settings.home / "cache" / "pixels" and labels_path.suffix == ".png"
    assert ":" not in labels_path.name
    assert cv2.imdecode(np.fromfile(labels_path, np.uint8), cv2.IMREAD_UNCHANGED).dtype == np.uint16

    (tmp_path / "p.png.ocr.json").write_text(
        json.dumps([{"bbox": [1, 2, 3, 4], "text": "A", "conf": 0.5}]), encoding="utf-8", newline="\n"
    )
    ocr = c.ocr(plate)
    assert ocr["shape"] == [64, 96] and ocr["boxes"][0]["text"] == "A"

    depth = c.depth(plate)
    assert depth["depth"].dtype == np.float32 and depth["depth"].shape == (64, 96)
    assert depth["depth_path"] is None and depth["stub"] is True
    assert c.depth(plate, load=False)["depth"] is None


def test_client_failures_use_the_workers_status_codes(settings, tmp_path):
    c = PixelsClient(settings, inproc=True)
    with pytest.raises(PixelsError) as e:
        c.segment(tmp_path / "absent.png")
    assert e.value.status == 400
    with pytest.raises(PixelsError) as e:
        c.segment(_write_png(tmp_path / "p.png"), [[0, 0]] * 65)
    assert e.value.status == 400


def test_client_http_without_a_worker(settings, tmp_path, monkeypatch):
    monkeypatch.delenv(INPROC_ENV, raising=False)
    monkeypatch.setenv(PORT_ENV, str(_free_port()))
    c = PixelsClient(settings)
    assert c.inproc is False and c.status() == "unavailable" and c.available() is False
    with pytest.raises(PixelsUnavailable) as e:
        c.ocr(_write_png(tmp_path / "p.png"))
    assert "python -m plate_pixels" in str(e.value) and INPROC_ENV in str(e.value)


def test_client_defaults_and_overrides(settings, monkeypatch):
    monkeypatch.delenv(INPROC_ENV, raising=False)
    monkeypatch.delenv(PORT_ENV, raising=False)
    c = PixelsClient(settings)
    assert c.inproc is False and c.port == 4611 and c.token == settings.token
    monkeypatch.setenv(PORT_ENV, "4999")
    monkeypatch.setenv(INPROC_ENV, "1")
    assert c.port == 4999 and c.inproc is True
    monkeypatch.setenv(INPROC_ENV, "true")  # only the literal "1" turns it on
    assert c.inproc is False
    assert PixelsClient(settings, inproc=True, port=1234).port == 1234


def test_client_http_against_a_real_worker(settings, tmp_path, monkeypatch):
    monkeypatch.delenv(INPROC_ENV, raising=False)
    plate = _write_png(tmp_path / "p.png")
    with Worker(settings.token) as worker:
        monkeypatch.setenv(PORT_ENV, str(worker.port))
        c = PixelsClient(settings)
        assert c.status() == "http" and c.available() is True
        seg = c.segment(plate, [[3, 4]])
        assert seg["shape"] == [64, 96] and Path(seg["labels_png_path"]).is_file() and seg["stub"] is True
        assert c.ocr(plate)["shape"] == [64, 96]
        depth = c.depth(plate)
        assert depth["depth"].shape == (64, 96) and Path(depth["depth_path"]).is_file()
        with pytest.raises(PixelsError) as e:
            c.ocr(tmp_path / "absent.png")
        assert e.value.status == 400
        assert PixelsClient(settings.model_copy(update={"token": "x" * 32})).available() is False  # 401


# ---------------------------------------------------------------------------- what Pipeline(ocr=, depth_fn=) takes


def test_ocr_adapter_in_process(settings):
    adapter = PixelsClient(settings).ocr_adapter()
    assert adapter.name == "pixels-inproc"
    assert adapter.detect(np.zeros((8, 8, 3), np.uint8)) == []  # stub: nothing without a sidecar
    with pytest.raises(ValueError):
        adapter.detect(np.zeros((8, 8), np.uint8))
    with pytest.raises(ValueError):
        adapter.detect(np.zeros((8, 8, 3), np.float32))


def test_ocr_adapter_with_no_worker_warns_once_and_finds_nothing(settings, monkeypatch, caplog):
    monkeypatch.delenv(INPROC_ENV, raising=False)
    monkeypatch.setenv(PORT_ENV, str(_free_port()))
    adapter = PixelsClient(settings).ocr_adapter()
    rgb = np.zeros((8, 8, 3), np.uint8)
    with caplog.at_level("WARNING", logger="plate_daemon.pixels"):
        assert adapter.detect(rgb) == [] and adapter.detect(rgb) == []
    assert adapter.unavailable is True
    assert len([r for r in caplog.records if r.name == "plate_daemon.pixels"]) == 1
    with pytest.raises(PixelsUnavailable):
        PixelsClient(settings).ocr_adapter(strict=True).detect(rgb)


def test_adapters_over_http_leave_no_files_behind(settings, monkeypatch):
    monkeypatch.delenv(INPROC_ENV, raising=False)
    rgb = np.random.default_rng(1).integers(0, 255, (48, 64, 3), dtype=np.uint8)
    with Worker(settings.token) as worker:
        monkeypatch.setenv(PORT_ENV, str(worker.port))
        client = PixelsClient(settings)
        assert client.ocr_adapter().detect(rgb) == []
        z = client.depth_fn()(rgb)
        assert z.dtype == np.float32 and z.shape == (48, 64)
        # the same stub the in-process path runs, so the two modes agree
        again = PixelsClient(settings, inproc=True).depth_fn()(rgb)
        np.testing.assert_array_equal(z, again)
    cache = settings.home / "cache" / "pixels"
    assert not list(cache.glob("*-in-*")) and not list(cache.glob("depth-*.exr"))


def test_depth_fn_in_process(settings):
    fn = PixelsClient(settings).depth_fn()
    assert fn.name == "pixels-inproc-depth"
    z = fn(np.zeros((16, 24, 3), np.uint8))
    assert z.dtype == np.float32 and z.shape == (16, 24) and np.isfinite(z).all()


def test_depth_fn_with_no_worker_raises_unavailable(settings, monkeypatch):
    monkeypatch.delenv(INPROC_ENV, raising=False)
    monkeypatch.setenv(PORT_ENV, str(_free_port()))
    with pytest.raises(PixelsUnavailable):
        PixelsClient(settings).depth_fn()(np.zeros((8, 8, 3), np.uint8))
    assert not list((settings.home / "cache" / "pixels").glob("*-in-*"))


# ---------------------------------------------------------------------------- the egress grep stays clean


@pytest.mark.parametrize("module", [pixels_client, pixels_routes], ids=["pixels_client", "routes.pixels"])
def test_no_httpx_in_the_pixels_modules(module):
    """test_egress bans httpx outside Egress; the worker is on loopback and these modules use urllib."""
    assert "httpx" not in Path(module.__file__).read_text(encoding="utf-8")
