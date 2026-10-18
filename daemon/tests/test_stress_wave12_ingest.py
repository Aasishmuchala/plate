"""Wave 12 ingest fixes (router plan T135), on the committed Blender packet `fixtures/packets/room_a`.

- The display TIFF is decoded at ingest (`check_display_tiff`, through `plate_pixels.imageio`, the reader
  `PacketView.plate16` uses): an LZW re-save of room_a's display master ingests and a fake-engine run reaches
  DONE; a JPEG-compressed TIFF, or any file the reader cannot decode, fails the packet with
  `display_tiff16: cannot decode (...)`. tifffile decodes none, Deflate and PackBits on its own; LZW needs the
  `imagecodecs` package, so that case runs when it is installed and a twin case proves the failure is named
  when it is not.
- `recompute_coverage_from_cryptomatte` gets per-id areas in one pass: the oracle is the old per-id
  `np.where(...).sum()`, computed inline, and 400 unlisted ids on a 640x360 Cryptomatte take well under 2 s.
- A `manifest.json` that is a symlink, or resolves outside the packet folder, is refused with the data files'
  wording. The portability lint bans creating symlinks in tests (Windows needs Developer Mode), so the link is
  simulated at the `pathlib` boundary: `Path.is_symlink` / `Path.resolve` answer for that one path.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import struct
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
import tifffile
from fastapi.testclient import TestClient

import plate_spec
from plate_daemon import ingest
from plate_daemon.api import create_app
from plate_daemon.cas import CAS, hash_file
from plate_daemon.config import Settings
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.index import Index
from plate_pixels import exr as exr_io

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
SKY_O, SHRUB_O = "o:929c9e95", "o:55cbe981"
HAVE_IMAGECODECS = importlib.util.find_spec("imagecodecs") is not None
needs_imagecodecs = pytest.mark.skipif(not HAVE_IMAGECODECS, reason="imagecodecs is not installed")

TIFF_NONE, TIFF_LZW, TIFF_JPEG, TIFF_DEFLATE, TIFF_PACKBITS = 1, 5, 7, 8, 32773

pytestmark = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


# ---------------------------------------------------------------------------- packets with another display TIFF


def _write_json(path: Path, doc: dict) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def _manifest(root: Path) -> dict:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def _rehash_display(root: Path) -> None:
    """Point the manifest at the display.tif now on disk (bytes + BLAKE3), as the attacker's repro does."""
    m = _manifest(root)
    digest, size = hash_file(root / "display.tif")
    ref = m["files"]["display_tiff16"]
    ref["hash"]["hex"], ref["bytes"] = digest, size
    _write_json(root / "manifest.json", m)
    assert plate_spec.validate_packet_dir(root, check_files=True) == []


def _compression_of(path: Path) -> int:
    with tifffile.TiffFile(path) as tf:
        return int(tf.pages[0].compression)


def _packet_with_display(tmp_path: Path, name: str, write) -> Path:
    dest = tmp_path / name
    shutil.copytree(ROOM_A, dest)
    write(dest / "display.tif")
    _rehash_display(dest)
    return dest


def _cv_resave(code: int):
    """Re-save the same pixels with cv2's IMWRITE_TIFF_COMPRESSION `code` (5 = LZW)."""

    def write(path: Path) -> None:
        img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        assert img is not None and img.dtype == np.uint16
        assert cv2.imwrite(str(path), img, [cv2.IMWRITE_TIFF_COMPRESSION, code])
        assert _compression_of(path) == code

    return write


def _cv_jpeg_8bit(path: Path) -> None:
    """A real JPEG-in-TIFF (libtiff cannot write one at 16 bits, so it is 8-bit)."""
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    params = [
        cv2.IMWRITE_TIFF_COMPRESSION,
        TIFF_JPEG,
        cv2.IMWRITE_TIFF_ROWSPERSTRIP,
        8,
    ]  # JPEG wants 8-row strips
    assert cv2.imwrite(str(path), (img >> 8).astype(np.uint8), params)
    assert _compression_of(path) == TIFF_JPEG


def _tag_as_jpeg_16bit(path: Path) -> None:
    """A 16-bit TIFF whose Compression tag (259) is rewritten to 7: the header says JPEG."""
    arr = tifffile.imread(path)
    tifffile.imwrite(path, arr, photometric="rgb")
    raw = bytearray(path.read_bytes())
    bo = "<" if raw[:2] == b"II" else ">"
    ifd = struct.unpack_from(f"{bo}I", raw, 4)[0]
    (count,) = struct.unpack_from(f"{bo}H", raw, ifd)
    for i in range(count):
        at = ifd + 2 + 12 * i
        if struct.unpack_from(f"{bo}H", raw, at)[0] == 259:
            struct.pack_into(f"{bo}H", raw, at + 8, TIFF_JPEG)
            break
    else:  # pragma: no cover - tifffile always writes the tag
        raise AssertionError("no Compression tag")
    path.write_bytes(bytes(raw))
    assert _compression_of(path) == TIFF_JPEG


def _not_a_tiff(path: Path) -> None:
    path.write_bytes(b"II*\x00" + b"\x00" * 64)  # a TIFF signature and nothing behind it


def _project_index(settings: Settings):
    """The project's index connection. An Index opens existing projects only (T144), so register the
    project folder first, as `POST /projects` does."""
    index = Index(settings.home)
    index.set_project_root(PROJECT, settings.projects_dir / PROJECT)
    return index.project(PROJECT)


def _ingest_lib(settings: Settings, packet: Path) -> tuple[dict, list[dict]]:
    """begin + finish on the project's real index; returns (record, events)."""
    pid = _manifest(packet)["packet_id"]
    with _project_index(settings) as conn:
        ingest.begin_ingest(conn, packet)
        rec = ingest.finish_ingest(conn, CAS(settings.projects_dir / PROJECT / "assets"), pid)
        return rec, ingest.packet_events(conn, pid)


# ---------------------------------------------------------------------------- through the app (fake engine)


@pytest.fixture
def app_client(plate_env, monkeypatch):
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.delenv("PLATE_ROUTER_MODE", raising=False)
    settings = Settings.load()
    with TestClient(create_app(settings, FakeEngine(echo=True)), base_url=BASE) as c:
        c.app.state.pipeline.sleep = lambda _s: None
        yield c, {"Authorization": f"Bearer {settings.token}"}


def _post_packet(c: TestClient, auth: dict, packet: Path):
    version = _manifest(packet)["packet_version"]
    return c.post(
        "/api/v0/packets",
        json={"packet_path": str(packet), "sender": {"name": "t", "version": "0"}},
        headers={**auth, "X-Plate-Packet-Version": version},
    )


def _create_project(c: TestClient, auth: dict) -> None:
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text


def _run_an_edit_to_the_end(c: TestClient, auth: dict, packet_id: str) -> dict:
    graph = {
        "nodes": [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
            {"id": "m", "kind": "Mask", "params": {"ids": [SKY_O], "textLock": True}},
            {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
        ],
        "edges": [
            {"id": "p->m", "source": "p", "target": "m"},
            {"id": "m->e", "source": "m", "target": "e"},
        ],
    }
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=auth)
    assert r.status_code == 200, r.text
    r = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth)
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]
    r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=auth)
    assert r.status_code == 200, r.text
    return c.get(f"/api/v0/runs/{run_id}", headers=auth).json()


# ---------------------------------------------------------------------------- the display TIFF is decoded


@needs_imagecodecs
def test_an_lzw_display_tiff_ingests_and_a_fake_engine_run_reaches_done(app_client, tmp_path):
    c, auth = app_client
    packet = _packet_with_display(tmp_path, "room_a_lzw", _cv_resave(TIFF_LZW))
    _create_project(c, auth)
    r = _post_packet(c, auth, packet)
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    rec = c.get(f"/api/v0/packets/{packet_id}", headers=auth).json()
    assert rec["status"] == "ingested", rec["error"]

    run = _run_an_edit_to_the_end(c, auth, packet_id)
    assert run["state"] == "DONE", run.get("error")


@pytest.mark.parametrize(
    "code",
    [
        TIFF_NONE,
        TIFF_DEFLATE,
        pytest.param(TIFF_LZW, marks=needs_imagecodecs),
        TIFF_PACKBITS,
    ],
    ids=["none", "deflate", "lzw", "packbits"],
)
def test_every_decodable_compression_ingests(settings, tmp_path, code):
    packet = _packet_with_display(tmp_path, f"room_a_c{code}", _cv_resave(code))
    rec, events = _ingest_lib(settings, packet)
    assert rec["status"] == "ingested", rec["error"]
    assert events[-1]["type"] == "ingest.done"


@pytest.mark.skipif(HAVE_IMAGECODECS, reason="needs an environment without imagecodecs")
def test_an_lzw_display_tiff_without_imagecodecs_is_refused_at_ingest_not_at_submit(settings, tmp_path):
    packet = _packet_with_display(tmp_path, "room_a_lzw", _cv_resave(TIFF_LZW))
    rec, events = _ingest_lib(settings, packet)
    assert rec["status"] == "failed"
    assert rec["error"].startswith("display_tiff16: cannot decode (") and "imagecodecs" in rec["error"]
    assert events[-1]["type"] == "ingest.failed"


@pytest.mark.parametrize(
    "write", [_cv_jpeg_8bit, _tag_as_jpeg_16bit], ids=["jpeg-8bit-from-libtiff", "jpeg-tag-on-16bit"]
)
def test_a_jpeg_compressed_display_tiff_fails_the_packet(settings, tmp_path, write):
    packet = _packet_with_display(tmp_path, "room_a_jpeg", write)
    rec, events = _ingest_lib(settings, packet)
    assert rec["status"] == "failed"
    assert rec["error"].startswith("display_tiff16: cannot decode (")
    assert "JPEG" in rec["error"] and "refused" in rec["error"]
    assert events[-1]["type"] == "ingest.failed"
    assert events[-1]["data"]["errors"] == [rec["error"]]


def test_the_failed_packet_is_what_the_app_reports(app_client, tmp_path):
    c, auth = app_client
    packet = _packet_with_display(tmp_path, "room_a_jpeg", _cv_jpeg_8bit)
    r = _post_packet(c, auth, packet)
    assert r.status_code == 202, r.text
    rec = c.get(f"/api/v0/packets/{r.json()['packet_id']}", headers=auth).json()
    assert rec["status"] == "failed" and "display_tiff16: cannot decode (" in rec["error"]


def test_a_file_the_reader_cannot_decode_fails_the_packet(settings, tmp_path):
    packet = _packet_with_display(tmp_path, "room_a_junk", _not_a_tiff)
    rec, _events = _ingest_lib(settings, packet)
    assert rec["status"] == "failed" and rec["error"].startswith("display_tiff16: cannot decode (")
    assert "\n" not in rec["error"]


def test_the_committed_deflate_master_and_a_placeholder_payload_pass_the_check(tmp_path):
    assert ingest.check_display_tiff(ROOM_A, _manifest(ROOM_A)) == []
    placeholder = plate_spec.materialize_example("tierA_still_clubhouse", tmp_path / "ph")
    assert ingest.check_display_tiff(placeholder, _manifest(placeholder)) == []  # no pixels to decode
    tier_c = plate_spec.materialize_example("tierC_client_plate", tmp_path / "tc")
    assert ingest.check_display_tiff(tier_c, _manifest(tier_c)) == []  # no display_tiff16 role at all


# ---------------------------------------------------------------------------- one-pass coverage


def _old_area(ranks: np.ndarray, cov: np.ndarray, rid: int) -> float:
    """The per-id form the one-pass table replaces."""
    n_pixels = ranks.shape[0] * ranks.shape[1]
    return float(np.where(ranks == np.uint32(rid), cov, 0.0).sum(dtype=np.float64)) / n_pixels


def test_one_pass_areas_equal_the_old_per_id_sum_on_room_a():
    manifest = _manifest(ROOM_A)
    ranks, cov = exr_io.read_cryptomatte(ROOM_A / "beauty.exr", manifest, "object")
    new = ingest._id_areas(ranks, cov, ranks.shape[0] * ranks.shape[1])
    every_id = {int(v) for v in np.unique(ranks)}
    assert set(new) == every_id and len(every_id) > 5
    for rid in every_id:
        assert new[rid] == pytest.approx(_old_area(ranks, cov, rid), abs=1e-6), f"o:{rid:08x}"


def test_the_omitted_id_message_carries_the_same_area_as_before():
    manifest = _manifest(ROOM_A)
    legend = json.loads((ROOM_A / "id_legend.json").read_text(encoding="utf-8"))
    legend["entries"] = [e for e in legend["entries"] if e["name"] != "VEG_shrub_A"]
    errors = ingest.recompute_coverage_from_cryptomatte(ROOM_A, manifest, legend=legend)
    ranks, cov = exr_io.read_cryptomatte(ROOM_A / "beauty.exr", manifest, "object")
    want = _old_area(ranks, cov, int(SHRUB_O[2:], 16))
    assert errors == [f"legend omits visible id {SHRUB_O} ({want * 100:.2f}%)"]


def test_400_unlisted_ids_on_a_640x360_cryptomatte_recompute_in_under_two_seconds(monkeypatch):
    h, w, levels = 360, 640, 6
    ranks = np.zeros((h, w, levels), np.uint32)
    cov = np.zeros((h, w, levels), np.float32)
    flat_ids, flat_cov = ranks[..., 0].reshape(-1), cov[..., 0].reshape(-1)
    block = 500  # pixels per sliver id: 0.22 % of the frame, under the 0.5 % visibility floor
    for i in range(400):
        flat_ids[i * block : (i + 1) * block] = 0x1000 + i * 7919
        flat_cov[i * block : (i + 1) * block] = 1.0
    big = 0xBEEF0001  # one id over the floor: 3 % of the frame
    flat_ids[-6912:], flat_cov[-6912:] = big, 1.0
    ranks[..., 0], cov[..., 0] = flat_ids.reshape(h, w), flat_cov.reshape(h, w)

    def fake_read(path, manifest, which="object"):
        if which == "material":
            raise exr_io.MissingChannel("cryptomatte_material", "CryptoMaterial00.R")
        return ranks, cov

    monkeypatch.setattr(exr_io, "read_cryptomatte", fake_read)
    notes: list[str] = []
    t0 = time.perf_counter()
    errors = ingest.recompute_coverage_from_cryptomatte(
        ROOM_A, _manifest(ROOM_A), legend={"entries": []}, notes=notes
    )
    took = time.perf_counter() - t0
    assert took < 2.0, f"{took:.2f} s"
    assert errors == [f"legend omits visible id o:{big:08x} (3.00%)"]
    assert (
        len(notes) == ingest.MAX_NOTES + 1
        and notes[-1] == "380 more ids below the visibility floor are not in the legend"
    )


# ---------------------------------------------------------------------------- manifest.json that escapes


@pytest.fixture
def packet_copy(tmp_path) -> Path:
    dest = tmp_path / "room_a_copy"
    shutil.copytree(ROOM_A, dest)
    return dest


def _pretend_symlink(monkeypatch, manifest: Path) -> None:
    real = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: True if self == manifest else real(self))


def _pretend_resolves_to(monkeypatch, manifest: Path, target: Path) -> None:
    real = Path.resolve
    monkeypatch.setattr(
        Path, "resolve", lambda self, *a, **k: target if self == manifest else real(self, *a, **k)
    )


def test_a_real_manifest_is_not_an_escape(packet_copy):
    assert ingest.manifest_escapes(packet_copy) is False
    assert ingest.manifest_escapes(packet_copy / "nothing-here") is False  # missing: the validator says so
    assert ingest.prepare_ingest(packet_copy).packet_id == _manifest(ROOM_A)["packet_id"]


def test_a_symlinked_manifest_is_refused(packet_copy, monkeypatch):
    _pretend_symlink(monkeypatch, packet_copy / "manifest.json")
    assert ingest.manifest_escapes(packet_copy) is True
    with pytest.raises(ingest.PacketInvalid) as e:
        ingest.prepare_ingest(packet_copy)
    assert e.value.errors == ["manifest.json: path escapes the packet folder"]


def test_a_manifest_that_resolves_outside_the_folder_is_refused(packet_copy, tmp_path, monkeypatch):
    outside = tmp_path / "elsewhere" / "manifest.json"
    outside.parent.mkdir()
    shutil.copyfile(packet_copy / "manifest.json", outside)
    _pretend_resolves_to(monkeypatch, packet_copy / "manifest.json", outside)
    assert ingest.manifest_escapes(packet_copy) is True
    with pytest.raises(ingest.PacketInvalid) as e:
        ingest.prepare_ingest(packet_copy)
    assert "path escapes the packet folder" in e.value.errors[0]


def test_the_route_answers_422_for_a_symlinked_manifest(app_client, packet_copy, monkeypatch):
    c, auth = app_client
    _pretend_symlink(monkeypatch, packet_copy / "manifest.json")
    r = _post_packet(c, auth, packet_copy)
    assert r.status_code == 422, r.text
    assert r.json()["errors"] == ["manifest.json: path escapes the packet folder"]


def test_a_manifest_swapped_for_a_link_after_accept_fails_the_ingest(settings, packet_copy, monkeypatch):
    pid = _manifest(packet_copy)["packet_id"]
    with _project_index(settings) as conn:
        ingest.begin_ingest(conn, packet_copy)
        _pretend_symlink(monkeypatch, packet_copy / "manifest.json")
        rec = ingest.finish_ingest(conn, CAS(settings.projects_dir / PROJECT / "assets"), pid)
    assert rec["status"] == "failed" and rec["error"] == "manifest.json: path escapes the packet folder"
