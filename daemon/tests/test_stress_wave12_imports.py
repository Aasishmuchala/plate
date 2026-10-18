"""Wave 12 stress fixes in imports and Tier C (router plan T137), each test the attacker's repro.

- `POST /projects/{id}/import` stats the path first: a FIFO, device, socket or directory is 422 `not_a_file`
  and is never opened (45 FIFOs used to block 45 worker threads and wedge `/health`); over 1 GiB is 413
  `too_large`; `cost_usd` above 1000 (or not finite) is 422 `bad_cost`; a 250-character extension is no 500.
- A still's size is compared with the plate from its header BEFORE it is decoded: a 117 KiB 6000 x 6000 PNG into
  a 640 x 360 project is `size_mismatch` with no pixel allocated, and a header over 100 000 000 px is 422
  `too_many_pixels`.
- Tier C: `shot_id` is a slug by `fullmatch` (`"s1\\n"` is not one); a plate whose header declares more than
  `TIERC_MAX_PIXELS` is 422 `too_many_pixels` before a build starts (the kit route and `plate_to_tiff16` too); a
  JPEG's EXIF Orientation is applied, so a phone photo is a portrait packet; the kit `camera` is a plain name
  whose `<camera>_camera.json` stays inside the kit folder (422 `bad_camera`).

Clip refusals (a 1-frame clip, a PNG renamed `.mp4`) are in `test_imports_video.py`, where the ffmpeg helpers are.
The pixels worker is never started: Tier C runs in this process on the stub backends (`PLATE_PIXELS_INPROC=1`).
"""

from __future__ import annotations

import json
import os
import struct
import time
import tracemalloc
import zlib
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from plate_daemon import imports
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.imports import IMPORT_MAX_BYTES, MAX_COST_USD
from plate_daemon.routes import tierc as tierc_routes
from plate_pixels import imageio

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
TIERC_PROJECT = "client-job"
MB = 1024 * 1024

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)
needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo is POSIX-only")


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    return Settings.load()


def _client(settings: Settings) -> TestClient:
    c = TestClient(create_app(settings, FakeEngine()), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


def _setup(c: TestClient, auth: dict, *, nda: bool = False) -> str:
    """Project + room_a ingested + Plate -> Edit `e`. The packet id."""
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": nda}}, headers=auth)
    assert r.status_code == 201, r.text
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
    r = c.post("/api/v0/packets", json=body, headers=hdr)
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    nodes = [
        {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
        {"id": "e", "kind": "Edit", "label": "e", "params": {"engine": "fake"}},
    ]
    edges = [{"id": "p->e", "source": "p", "target": "e"}]
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json={"nodes": nodes, "edges": edges}, headers=auth)
    assert r.status_code == 200, r.text
    return packet_id


def _import(c: TestClient, auth: dict, **body):
    return c.post(f"/api/v0/projects/{PROJECT}/import", json=body, headers=auth)


def _runs(c: TestClient, auth: dict) -> list:
    return c.get(f"/api/v0/projects/{PROJECT}/runs", headers=auth).json()


def _plate16() -> np.ndarray:
    img = cv2.imread(str(ROOM_A / "display.tif"), cv2.IMREAD_UNCHANGED)
    assert img is not None and img.dtype == np.uint16
    return np.ascontiguousarray(img[..., :3][..., ::-1])


def _png_bytes(rgb8: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", np.ascontiguousarray(rgb8[..., ::-1]))
    assert ok
    return bytes(buf)


def _flat_png(path: Path, width: int, height: int) -> Path:
    path.write_bytes(_png_bytes(np.full((height, width, 3), 90, np.uint8)))
    return path


def _png_header(path: Path, width: int, height: int) -> Path:
    """A PNG that is a signature, an IHDR declaring `width` x `height` and an IEND: all a header reader needs."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b""))
    return path


def _gradient(width: int, height: int) -> np.ndarray:
    y, x = np.mgrid[0:height, 0:width]
    return np.stack([x * 255 // (width - 1), y * 255 // (height - 1), (x * 3 + y * 5) % 256], axis=-1).astype(
        np.uint8
    )


def _jpeg(path: Path, arr: np.ndarray, orientation: int | None = None) -> Path:
    kwargs = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kwargs["exif"] = exif.tobytes()
    Image.fromarray(arr).save(path, "JPEG", quality=95, subsampling=0, **kwargs)
    return path


# ---------------------------------------------------------------------------- the import route: what is a file


@needs_room_a
@needs_fifo
def test_forty_five_fifos_are_422_not_a_file_and_health_stays_up(sync_settings, auth, tmp_path):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        pipes = []
        for i in range(45):
            pipe = tmp_path / f"pipe{i}.png"
            os.mkfifo(pipe)
            pipes.append(pipe)
        started = time.monotonic()
        for pipe in pipes:
            r = _import(c, auth, path=str(pipe), packet_id=packet_id, source="manual")
            assert r.status_code == 422 and r.json()["error"] == "not_a_file", r.text
        assert time.monotonic() - started < 5.0
        assert c.get("/api/v0/health", headers=auth).status_code == 200
        assert _runs(c, auth) == []


@needs_room_a
def test_a_directory_and_a_device_are_not_a_file_and_a_missing_one_stays_unreadable(
    sync_settings, auth, tmp_path
):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        r = _import(c, auth, path=str(tmp_path), packet_id=packet_id, source="manual")
        assert r.status_code == 422 and r.json()["error"] == "not_a_file", r.text
        (tmp_path / "folder.png").mkdir()
        r = _import(c, auth, path=str(tmp_path / "folder.png"), packet_id=packet_id, source="manual")
        assert r.status_code == 422 and r.json()["error"] == "not_a_file"
        if Path("/dev/null").exists():
            r = _import(c, auth, path="/dev/null", packet_id=packet_id, source="manual")
            assert r.status_code == 422 and r.json()["error"] == "not_a_file"
        r = _import(c, auth, path=str(tmp_path / "absent.png"), packet_id=packet_id, source="manual")
        assert r.status_code == 422 and r.json()["error"] == "unreadable_image"
        r = _import(c, auth, path=str(tmp_path / "absent.mp4"), packet_id=packet_id, source="manual")
        assert r.status_code == 422 and r.json()["error"] == "unreadable_video"
        assert _runs(c, auth) == []


@needs_room_a
def test_a_file_over_the_size_limit_is_413_too_large_without_being_read(
    sync_settings, auth, tmp_path, monkeypatch
):
    assert IMPORT_MAX_BYTES == 1 << 30  # 1 GiB; the test shrinks it so no gigabyte is written
    monkeypatch.setattr(imports, "IMPORT_MAX_BYTES", 2000)
    big = tmp_path / "huge.png"
    big.write_bytes(b"\x89PNG" + b"\0" * 2000)  # 2004 bytes
    exact = tmp_path / "exact.png"
    exact.write_bytes(b"\0" * 2000)
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        r = _import(c, auth, path=str(big), packet_id=packet_id, source="manual")
        assert r.status_code == 413, r.text
        assert r.json()["error"] == "too_large" and r.json()["limit"] == 2000 and r.json()["size"] == 2004
        assert _runs(c, auth) == []
        # exactly the limit passes the size check and is then refused as what it is: not an image
        r = _import(c, auth, path=str(exact), packet_id=packet_id, source="manual")
        assert r.status_code == 422 and r.json()["error"] == "unreadable_image", r.text


# ---------------------------------------------------------------------------- the import route: what is declared


@needs_room_a
@pytest.mark.parametrize("cost", [1e308, MAX_COST_USD + 0.01, 1e6])
def test_a_declared_cost_over_the_bound_is_422_bad_cost(sync_settings, auth, tmp_path, cost):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        r = _import(
            c, auth, path=str(ROOM_A / "display.tif"), packet_id=packet_id, source="higgsfield", cost_usd=cost
        )
        assert r.status_code == 422 and r.json()["error"] == "bad_cost", r.text
        assert _runs(c, auth) == []


@needs_room_a
def test_a_non_finite_declared_cost_is_422_and_leaves_no_run(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        body = json.dumps({"path": str(ROOM_A / "display.tif"), "packet_id": packet_id, "source": "manual"})
        raw = body[:-1] + ', "cost_usd": Infinity}'
        r = c.post(
            f"/api/v0/projects/{PROJECT}/import",
            content=raw,
            headers={**auth, "Content-Type": "application/json"},
        )
        assert r.status_code == 422, r.text
        assert _runs(c, auth) == []


@needs_room_a
def test_a_declared_cost_at_the_bound_is_taken(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        r = _import(
            c,
            auth,
            path=str(ROOM_A / "display.tif"),
            packet_id=packet_id,
            source="manual",
            cost_usd=MAX_COST_USD,
        )
        assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------- header before decode


@needs_room_a
def test_a_6000_square_png_is_size_mismatch_from_its_header_with_no_decode(
    sync_settings, auth, tmp_path, monkeypatch
):
    bomb = _flat_png(tmp_path / "bomb.png", 6000, 6000)
    assert bomb.stat().st_size < 200 * 1024  # ~117 KiB: a small file that declares 36 million pixels

    def decoded(*_a, **_k):
        raise AssertionError("the file was decoded before its header was compared with the plate")

    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        monkeypatch.setattr(imports, "_decode", decoded)
        tracemalloc.start()
        try:
            r = _import(c, auth, path=str(bomb), packet_id=packet_id, source="manual")
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert r.status_code == 422, r.text
        body = r.json()
        assert body["error"] == "size_mismatch"
        assert body["plate"] == [640, 360] and body["file"] == [6000, 6000]
        # a decode of 36 Mpx is about 1.5 GB of float32 and uint16 copies; the header check allocates nothing
        assert peak < 50 * MB, f"peak traced allocation {peak / MB:.1f} MB"
        assert _runs(c, auth) == []


@needs_room_a
def test_a_header_over_the_pixel_cap_is_422_too_many_pixels(sync_settings, auth, tmp_path):
    bomb = _png_header(tmp_path / "bomb.png", 11000, 11000)  # 121 Mpx declared, 70 bytes on disk
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        r = _import(c, auth, path=str(bomb), packet_id=packet_id, source="manual")
        assert r.status_code == 422, r.text
        body = r.json()
        assert body["error"] == "too_many_pixels"
        assert body["declared"] == [11000, 11000] and body["limit"] == imports.IMPORT_MAX_PIXELS
        assert _runs(c, auth) == []


@needs_room_a
def test_a_jpeg_turned_by_its_exif_orientation_is_judged_upright(sync_settings, auth, tmp_path):
    """The plate is 640 x 360. A 360 x 640 JPEG with Orientation 6 is a 640 x 360 picture once turned: the
    header (swapped) passes, the decoder's upright shape is the one compared. Without the tag it is a mismatch."""
    stored = _gradient(360, 640)  # width 360, height 640
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        r = _import(
            c, auth, path=str(_jpeg(tmp_path / "t.jpg", stored)), packet_id=packet_id, source="manual"
        )
        assert r.status_code == 422 and r.json()["error"] == "size_mismatch", r.text
        assert r.json()["file"] == [360, 640]
        # Orientation 6 turns it 90 degrees: 640 wide, 360 high, the plate's size
        r = _import(
            c, auth, path=str(_jpeg(tmp_path / "t6.jpg", stored, 6)), packet_id=packet_id, source="manual"
        )
        assert r.status_code == 201, r.text
        # a tag that does not make it fit is still a mismatch, named with the upright size
        wide = _gradient(640, 360)
        r = _import(
            c, auth, path=str(_jpeg(tmp_path / "w6.jpg", wide, 6)), packet_id=packet_id, source="manual"
        )
        assert r.status_code == 422 and r.json()["error"] == "size_mismatch", r.text
        assert r.json()["file"] == [360, 640]


@needs_room_a
def test_a_250_character_extension_is_a_clean_422_not_a_500(sync_settings, auth, tmp_path):
    ext = "a" * 250
    junk = tmp_path / f"p.{ext}"
    junk.write_bytes(b"this is not an image")
    plate = tmp_path / f"q.{ext}"
    plate.write_bytes(_png_bytes((_plate16() // 257).astype(np.uint8)))
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        r = _import(c, auth, path=str(junk), packet_id=packet_id, source="manual")
        assert r.status_code == 422 and r.json()["error"] == "unreadable_image", r.text
        # the suffix only picks the decoder: a real image with an absurd extension is still an image
        r = _import(c, auth, path=str(plate), packet_id=packet_id, source="manual")
        assert r.status_code == 201, r.text


def test_the_temp_name_keeps_sixteen_characters_of_the_suffix(tmp_path, monkeypatch):
    seen: list[str] = []
    real = imageio.read

    def spy(path, *a, **k):
        seen.append(Path(path).name)
        return real(path, *a, **k)

    monkeypatch.setattr(imageio, "read", spy)
    raw = _png_bytes(_gradient(8, 8))
    imports._decode(raw, "." + "b" * 250, tmp_path / "cache")
    imports._decode(raw, ".png:stream", tmp_path / "cache")  # a ':' would name a Windows alternate stream
    assert seen[0] == "import." + "b" * 15 and len(seen[0]) == len("import") + 16
    assert seen[1] == "import.pngstream" and ":" not in seen[1]


# ---------------------------------------------------------------------------- Tier C


def _tierc(c: TestClient, auth: dict, plate: Path, shot_id: str = "hero", project: str = TIERC_PROJECT):
    return c.post(
        f"/api/v0/projects/{project}/tierc",
        json={"plate_path": str(plate), "shot_id": shot_id, "consent_record": "signed 2026-10-01"},
        headers=auth,
    )


def _tierc_project(c: TestClient, auth: dict, project: str = TIERC_PROJECT) -> None:
    r = c.post("/api/v0/projects", json={"project_id": project, "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text


def _packet_resolution(body: dict) -> tuple[int, int]:
    manifest = json.loads((Path(body["packet_dir"]) / "manifest.json").read_text(encoding="utf-8"))
    res = manifest["camera"]["resolution"]
    return res["width"], res["height"]


def _packet_folders(settings: Settings) -> list[Path]:
    return sorted((settings.packets_dirs[0] / TIERC_PROJECT).glob("tierc-*"))


@pytest.mark.parametrize(
    ("orientation", "size", "transpose"),
    [
        (6, (100, 200), Image.Transpose.ROTATE_270),
        (8, (100, 200), Image.Transpose.ROTATE_90),
        (3, (200, 100), Image.Transpose.ROTATE_180),
    ],
)
def test_a_jpeg_with_an_orientation_tag_becomes_an_upright_tier_c_packet(
    sync_settings, auth, tmp_path, monkeypatch, orientation, size, transpose
):
    monkeypatch.setenv("PLATE_PIXELS_INPROC", "1")
    stored = _gradient(200, 100)
    plain = cv2.cvtColor(cv2.imread(str(_jpeg(tmp_path / "plain.jpg", stored))), cv2.COLOR_BGR2RGB)
    tagged = _jpeg(tmp_path / f"o{orientation}.jpg", stored, orientation)
    with _client(sync_settings) as c:
        _tierc_project(c, auth)
        r = _tierc(c, auth, tagged)
        assert r.status_code == 202, r.text
        body = r.json()
        assert _packet_resolution(body) == size
        written = cv2.cvtColor(
            cv2.imread(str(Path(body["packet_dir"]) / "client_plate.png")), cv2.COLOR_BGR2RGB
        )
        want = np.asarray(Image.fromarray(plain).transpose(transpose))
        assert written.shape == want.shape
        assert np.array_equal(written, want)  # upright, not merely the right shape


def test_a_4000_square_tier_c_plate_under_the_cap_is_still_202(sync_settings, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("PLATE_PIXELS_INPROC", "1")
    plate = _flat_png(tmp_path / "wide.png", 4000, 4000)  # 16 Mpx, ~52 KiB
    assert plate.stat().st_size < 100 * 1024
    with _client(sync_settings) as c:
        _tierc_project(c, auth)
        r = _tierc(c, auth, plate)
        assert r.status_code == 202, r.text
        assert r.json()["tier"] == "C" and _packet_resolution(r.json()) == (4000, 4000)


@pytest.mark.parametrize("shot_id", ["s1\n", "hero\n", "s1 ", "S1", "../x", ""])
def test_a_tier_c_shot_id_is_a_slug_by_fullmatch(sync_settings, auth, tmp_path, monkeypatch, shot_id):
    monkeypatch.setenv("PLATE_PIXELS_INPROC", "1")
    plate = _flat_png(tmp_path / "plate.png", 64, 48)
    with _client(sync_settings) as c:
        _tierc_project(c, auth)
        r = _tierc(c, auth, plate, shot_id)
        assert r.status_code == 422, r.text
        assert (
            _packet_folders(sync_settings) == []
        )  # nothing was built, not even a folder named with a newline


def test_check_slug_uses_fullmatch():
    from plate_daemon.routes.tierc import ApiError, check_slug

    assert check_slug("shot_id", "s1") == "s1"
    for bad in ("s1\n", "\ns1", "s1\r\n"):
        with pytest.raises(ApiError) as e:
            check_slug("shot_id", bad)
        assert e.value.status == 422


def test_a_plate_whose_header_is_over_the_cap_is_422_before_a_build(
    sync_settings, auth, tmp_path, monkeypatch
):
    monkeypatch.setenv("PLATE_PIXELS_INPROC", "1")
    bomb = _png_header(tmp_path / "bomb.png", 11000, 11000)
    with _client(sync_settings) as c:
        _tierc_project(c, auth)
        r = _tierc(c, auth, bomb)
        assert r.status_code == 422, r.text
        body = r.json()
        assert body["error"] == "too_many_pixels" and body["declared"] == [11000, 11000]
        assert body["limit"] == 100_000_000
        assert _packet_folders(sync_settings) == []


def test_the_cap_holds_in_http_mode_and_a_workers_400_keeps_its_code(
    sync_settings, auth, tmp_path, monkeypatch
):
    monkeypatch.delenv("PLATE_PIXELS_INPROC", raising=False)
    plate = _flat_png(tmp_path / "plate.png", 64, 48)
    bomb = _png_header(tmp_path / "bomb.png", 11000, 11000)
    posted: list[dict] = []

    def worker(url, **kw):  # the pixels worker, refusing a plate its own build_tier_c_packet found too big
        posted.append(kw["json"])
        return httpx.Response(400, json={"error": "too_many_pixels: the file declares 11000 x 11000"})

    monkeypatch.setattr(tierc_routes.httpx, "post", worker)
    with _client(sync_settings) as c:
        _tierc_project(c, auth)
        r = _tierc(c, auth, bomb)  # refused by the daemon from the header: the worker is never called
        assert r.status_code == 422 and r.json()["error"] == "too_many_pixels"
        assert posted == []
        r = _tierc(c, auth, plate)  # a worker that still says no keeps the code across the process line
        assert r.status_code == 422 and r.json()["error"] == "too_many_pixels", r.text
        assert len(posted) == 1
        assert _packet_folders(sync_settings) == []


def test_build_tier_c_packet_refuses_an_over_cap_plate_from_its_header(tmp_path, monkeypatch):
    from plate_pixels.backends import Backends
    from plate_pixels.tierc import build_tier_c_packet

    bomb = _png_header(tmp_path / "bomb.png", 11000, 11000)

    def never(*_a, **_k):
        raise AssertionError("the plate was decoded")

    monkeypatch.setattr(imageio, "_decode_cv", never)
    with pytest.raises(imageio.TooManyPixels):
        build_tier_c_packet(bomb, tmp_path / "out", Backends(), "p1", "hero", "signed")
    assert not (tmp_path / "out").exists()


def test_plate_to_tiff16_refuses_an_over_cap_plate_and_converts_a_normal_one(tmp_path):
    bomb = _png_header(tmp_path / "bomb.png", 11000, 11000)
    with pytest.raises(imageio.TooManyPixels):
        tierc_routes.plate_to_tiff16(bomb, tmp_path / "bomb.tif")
    assert not (tmp_path / "bomb.tif").exists()
    ok = _flat_png(tmp_path / "ok.png", 32, 24)
    tierc_routes.plate_to_tiff16(ok, tmp_path / "ok.tif")
    assert (tmp_path / "ok.tif").is_file()


# ---------------------------------------------------------------------------- the client packet kit


def _kit(tmp_path: Path, name: str = "CAM_a") -> Path:
    """A kit folder with just a camera JSON: the camera refusals come before anything else is read."""
    folder = tmp_path / "kit"
    folder.mkdir(exist_ok=True)
    (folder / f"{name}_camera.json").write_text("{}", encoding="utf-8")
    return folder


def _kit_post(c: TestClient, auth: dict, folder: Path, plate: Path, camera: str, **extra):
    body = {"folder": str(folder), "camera": camera, "plate_path": str(plate), **extra}
    return c.post(f"/api/v0/projects/{TIERC_PROJECT}/kit", json=body, headers=auth)


@pytest.mark.parametrize(
    "camera",
    ["../escape", "..\\escape", "a/b", "/etc/passwd", "CAM_a\n", "CAM:a", "x" * 65, "Cam (hero)", "café"],
)
def test_a_kit_camera_that_is_not_a_plain_name_is_422_bad_camera(sync_settings, auth, tmp_path, camera):
    plate = _flat_png(tmp_path / "plate.png", 64, 48)
    with _client(sync_settings) as c:
        _tierc_project(c, auth)
        r = _kit_post(c, auth, _kit(tmp_path), plate, camera)
        assert r.status_code == 422 and r.json()["error"] == "bad_camera", r.text
        assert not (sync_settings.packets_dirs[0] / TIERC_PROJECT).exists() or not any(
            (sync_settings.packets_dirs[0] / TIERC_PROJECT).iterdir()
        )


def test_a_kit_camera_json_that_resolves_outside_the_folder_is_422_bad_camera(
    sync_settings, auth, tmp_path, monkeypatch
):
    """The portability lint bans making a symlink in a test, so the link is simulated at the `Path.resolve`
    boundary: the camera JSON of `CAM_link` resolves to a file outside the kit folder."""
    plate = _flat_png(tmp_path / "plate.png", 64, 48)
    folder = _kit(tmp_path, "CAM_link")
    outside = tmp_path / "elsewhere" / "secret_camera.json"
    real_resolve = Path.resolve

    def resolve(self, *a, **k):
        if self.name == "CAM_link_camera.json":
            return real_resolve(outside, *a, **k)
        return real_resolve(self, *a, **k)

    monkeypatch.setattr(Path, "resolve", resolve)
    with _client(sync_settings) as c:
        _tierc_project(c, auth)
        r = _kit_post(c, auth, folder, plate, "CAM_link")
        assert r.status_code == 422 and r.json()["error"] == "bad_camera", r.text
        assert "outside the kit folder" in r.json()["detail"]


def test_a_kit_camera_with_a_space_or_dots_is_a_plain_name(sync_settings, auth, tmp_path):
    from plate_daemon.routes.tierc import check_camera

    folder = _kit(tmp_path)
    for name in ("CAM_a", "Camera 01", "cam.v2-final_A", "x" * 64):
        assert check_camera(folder, name) == name


def test_a_kit_plate_over_the_pixel_cap_is_422_before_the_kit_is_read(sync_settings, auth, tmp_path):
    bomb = _png_header(tmp_path / "bomb.png", 11000, 11000)
    with _client(sync_settings) as c:
        _tierc_project(c, auth)
        r = _kit_post(c, auth, _kit(tmp_path), bomb, "CAM_a")
        assert r.status_code == 422, r.text
        assert r.json()["error"] == "too_many_pixels" and r.json()["declared"] == [11000, 11000]


def test_a_kit_shot_id_with_a_trailing_newline_is_422(sync_settings, auth, tmp_path):
    plate = _flat_png(tmp_path / "plate.png", 64, 48)
    with _client(sync_settings) as c:
        _tierc_project(c, auth)
        r = _kit_post(c, auth, _kit(tmp_path), plate, "CAM_a", shot_id="cam-a\n")
        assert r.status_code == 422, r.text
