"""Export crops to the camera's safe frame (router plan T114).

When a packet declares `camera.safe_frame`, still export (`export.export`), batch export
(`POST /projects/{id}/export`) and frame/clip delivery (`POST /runs/{id}/deliver`) write the safe-frame region
by default: a pixel crop, nothing resampled. `full_frame: true` keeps the whole take. The sidecar and the
ledger `export` record carry `crop: {x, y, width, height}` or `full_frame: true`; a packet without a safe frame
is unchanged. The library tests build their own index and packet; the route tests run on the committed
Blender packet `room_a` (a 2.39:1 band, y 46 height 268, inside a 640 x 360 frame) with an engine that echoes
the plate it was sent. ffmpeg-dependent tests skip when the tool or the encoder is absent.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np
import OpenImageIO as oiio
import pytest
from fastapi.testclient import TestClient

from plate_daemon import db, export, lineage
from plate_daemon.api import create_app
from plate_daemon.cas import CAS
from plate_daemon.config import Settings
from plate_daemon.delivery import frames, guard, tools
from plate_daemon.delivery.guard import DeliveryError
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.gate_reports import Service
from plate_daemon.index import apply_packet_entry
from plate_daemon.ledger import Ledger
from plate_daemon.machine import annotate, create_run, transition
from plate_daemon.models import QUOTE_TTL, Quote, Route, Run, RunCreate
from plate_daemon.pipeline import decode_png16
from plate_daemon.quotes import insert_quote
from plate_daemon.states import RunState
from plate_daemon.timeutil import utcnow
from plate_pixels import imageio

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
FAKE_ROUTE = {"engine_id": "fake", "provider": "fake", "region": "local"}
S = RunState

BAND = (0, 46, 640, 268)  # room_a's safe frame
BAND_DICT = {"x": 0, "y": 46, "width": 640, "height": 268}

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


def _ffmpeg_ok(*encoders: str) -> bool:
    try:
        tools.ffmpeg_path()
        tools.ffprobe_path()
    except guard.ToolMissing:
        return False
    listing = subprocess.run(
        [tools.ffmpeg_path(), "-hide_banner", "-encoders"], capture_output=True, check=False
    ).stdout.decode("utf-8", "replace")
    return all(re.search(rf"\s{re.escape(enc)}\s", listing) for enc in encoders)


needs_ffmpeg = pytest.mark.skipif(
    not _ffmpeg_ok("libx264", "libx265", "prores_ks"), reason="ffmpeg with libx264, libx265, prores_ks absent"
)


def _manifest(safe: tuple[int, int, int, int] | None, width: int = 640, height: int = 360) -> dict:
    camera: dict = {"resolution": {"width": width, "height": height, "pixel_aspect": 1.0}}
    if safe is not None:
        x, y, w, h = safe
        camera["safe_frame"] = {"x": x, "y": y, "width": w, "height": h}
    return {"camera": camera, "colour": {"working_space": "lin_rec709"}}


def _exr_size(path: Path) -> tuple[int, int]:
    inp = oiio.ImageInput.open(str(path))
    assert inp is not None, oiio.geterror()
    try:
        spec = inp.spec()
        return spec.width, spec.height
    finally:
        inp.close()


def _exr_pixels(path: Path) -> np.ndarray:
    inp = oiio.ImageInput.open(str(path))
    assert inp is not None, oiio.geterror()
    try:
        spec = inp.spec()
        return np.asarray(inp.read_image("float"), dtype=np.float32).reshape(spec.height, spec.width, -1)
    finally:
        inp.close()


# ---------------------------------------------------------------------------- placing the rectangle


def test_place_safe_frame_vectors():
    res = (640, 360)
    assert export.place_safe_frame(BAND, res, 640, 360) == BAND  # the render itself
    assert export.place_safe_frame(BAND, res, 640, 268) == (0, 0, 640, 268)  # already the safe frame
    assert export.place_safe_frame(BAND, res, 1920, 1080) == (0, 138, 1920, 804)  # an engine's 3x take
    # 46 x 0.2 = 9.2 and 314 x 0.2 = 62.8: both edges round inward, so no overscan row survives
    assert export.place_safe_frame(BAND, res, 128, 72) == (0, 10, 128, 52)
    # a safe frame that is the whole render has no overscan to remove, whatever the take's size
    assert export.place_safe_frame((0, 0, 640, 360), res, 64, 64) == (0, 0, 64, 64)
    with pytest.raises(export.SafeFrameError) as e:
        export.place_safe_frame(BAND, res, 64, 64)  # another aspect: the band cannot be located
    assert e.value.code == "SAFE_FRAME_MISMATCH" and "full_frame" in str(e.value)


@pytest.mark.parametrize("size", [(1280, 720), (1919, 1080), (3840, 2160), (854, 480), (321, 181)])
def test_a_scaled_rectangle_never_reaches_into_the_overscan(size):
    width, height = size
    x, y, w, h = export.place_safe_frame(BAND, (640, 360), width, height)
    assert Fraction(x, width) >= Fraction(BAND[0], 640) and Fraction(y, height) >= Fraction(BAND[1], 360)
    assert Fraction(x + w, width) <= Fraction(BAND[0] + BAND[2], 640)
    assert Fraction(y + h, height) <= Fraction(BAND[1] + BAND[3], 360)
    assert h >= height * 268 // 360 - 2  # inward by at most a pixel on each edge


def test_framing_records_crop_full_frame_or_nothing():
    m = _manifest(BAND)
    assert export.framing(m, 640, 360).record() == {"crop": BAND_DICT}
    assert export.framing(m, 640, 360, full_frame=True).record() == {
        "full_frame": True,
        "safe_frame": BAND_DICT,
    }
    assert export.framing(_manifest(None), 640, 360).record() == {}
    assert export.framing(None, 640, 360).record() == {}
    assert export.framing(None, 640, 360, full_frame=True).record() == {"full_frame": True}
    whole = export.framing(_manifest((0, 0, 640, 360)), 640, 360)
    assert whole.crop == (0, 0, 640, 360) and not whole.cuts(640, 360)
    assert export.framing(m, 640, 360).cuts(640, 360)


def test_a_420_encode_snaps_the_crop_inward_to_even_coordinates():
    odd = _manifest((1, 45, 637, 269))
    assert export.framing(odd, 640, 360).crop == (1, 45, 637, 269)  # stills and 4:4:4: the exact rectangle
    assert export.framing(odd, 640, 360, even=True).crop == (2, 46, 636, 268)


def test_cutdown_validation_refuses_a_safe_frame_outside_the_take_unless_full_frame():
    bad = _manifest((0, 46, 700, 268))
    with pytest.raises(export.SafeFrameError, match="does not fit"):
        export.framing(bad, 640, 360)
    assert export.framing(bad, 640, 360, full_frame=True).record() == {"full_frame": True}
    with pytest.raises(export.SafeFrameError):
        export.framing(_manifest(BAND), 64, 64)
    assert export.framing(_manifest(BAND), 64, 64, full_frame=True).record() == {"full_frame": True}
    malformed = {"camera": {"safe_frame": {"x": 0}}}
    with pytest.raises(export.SafeFrameError):
        export.framing(malformed, 640, 360)
    assert export.framing(malformed, 640, 360, full_frame=True).record() == {"full_frame": True}


# ---------------------------------------------------------------------------- frames.crop_frames


def test_crop_frames_is_a_pixel_crop_with_alpha_and_refuses_a_box_outside():
    rng = np.random.default_rng(7)
    rgba = (rng.random((36, 64, 4)) * 65535).astype(np.uint16)
    cut = frames.crop_frames([rgba, rgba[::-1].copy()], (2, 5, 60, 26))
    assert (cut.width, cut.height, cut.count, cut.has_alpha) == (60, 26, 2, True)
    assert frames.open_frames(cut) is cut  # an opened Frames is what the encoders take
    first, second = list(cut)
    assert np.array_equal(first.rgb, rgba[5:31, 2:62, :3].astype(np.float32) / 65535.0)
    assert np.array_equal(first.alpha, rgba[5:31, 2:62, 3].astype(np.float32) / 65535.0)
    assert np.array_equal(second.rgb, rgba[::-1][5:31, 2:62, :3].astype(np.float32) / 65535.0)
    with pytest.raises(DeliveryError, match="not inside"):
        frames.crop_frames(rgba, (10, 10, 60, 30))
    with pytest.raises(DeliveryError):
        frames.crop_frame(
            frames.Frame(rgb=np.zeros((4, 4, 3), np.float32), alpha=None, encoding="srgb"), (0, 0, 5, 4)
        )


# ---------------------------------------------------------------------------- export.export (the library)

LIB_PROJECT, LIB_PACKET = "frame-proj", "frame-pk"
LIB_SAFE = (2, 5, 60, 26)  # inside a 64 x 36 render


def _source16(width: int = 64, height: int = 36) -> np.ndarray:
    y, x = np.mgrid[0:height, 0:width]
    rgb = np.stack([x / (width - 1), y / (height - 1), ((x * 7 + y * 3) % 11) / 10.0], axis=-1)
    return np.rint(rgb * 65535.0).astype(np.uint16)


class Project:
    """An index, a registered packet whose manifest declares `manifest`, and a CAS holding one result PNG."""

    def __init__(self, tmp: Path, manifest: dict | None, take: np.ndarray):
        self.tmp = tmp
        self.conn = db.open_index(tmp / "index.sqlite")
        self.folder = tmp / "packet"
        self.folder.mkdir()
        if manifest is not None:
            (self.folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        apply_packet_entry(
            self.conn,
            LIB_PROJECT,
            LIB_PACKET,
            {
                "shot_id": "S1",
                "kind": "still",
                "tier": "A",
                "packet_version": "0.1.1",
                "path": str(self.folder),
                "manifest_blake3": "0" * 64,
            },
        )
        self.cas = CAS(tmp / "assets")
        self.take = take
        imageio.write_png16(tmp / "take.png", take)
        key = self.cas.put_bytes((tmp / "take.png").read_bytes(), ext="png")
        now = datetime(2026, 10, 18, tzinfo=UTC)
        self.run = Run(
            id="run-frame-1",
            project_id=LIB_PROJECT,
            packet_id=LIB_PACKET,
            state=RunState.DONE,
            priority="interactive",
            route=Route(engine_id="fake", provider="local", region="local"),
            attempt=1,
            node_id="n1",
            result_key=key,
            created_utc=now,
            updated_utc=now,
        )
        self.out = tmp / "out"

    def export(self, fmt: str, name: str, **kw) -> export.Exported:
        return export.export(self.run, fmt, self.out, conn=self.conn, cas=self.cas, name=name, **kw)

    def block(self, name: str) -> dict:
        return json.loads((self.out / f"{name}.gate.json").read_text(encoding="utf-8"))["export"]


def test_export_writes_the_safe_frame_of_every_format_and_records_the_crop(tmp_path):
    p = Project(tmp_path, _manifest(LIB_SAFE, 64, 36), _source16())
    x, y, w, h = LIB_SAFE
    want = {"x": x, "y": y, "width": w, "height": h}
    done = {fmt: p.export(fmt, "take") for fmt in ("png16", "tiff16", "exr")}
    png = cv2.imread(str(p.out / "take.png"), cv2.IMREAD_UNCHANGED)[..., ::-1]
    assert png.shape == (h, w, 3) and np.array_equal(png, p.take[y : y + h, x : x + w])  # a pixel crop
    tif = imageio.read(p.out / "take.tif")
    assert tif.rgb.shape == (h, w, 3)
    assert _exr_size(p.out / "take.exr") == (w, h)
    block = p.block("take")
    assert block["crop"] == want and "full_frame" not in block
    assert block["files"] == ["take.png", "take.tif", "take.exr"]
    assert all(d.framing.record() == {"crop": want} for d in done.values())
    assert lineage.read(p.out / "take.png").run_id == p.run.id  # lineage as before


def test_full_frame_keeps_the_whole_take_and_names_the_safe_frame(tmp_path):
    p = Project(tmp_path, _manifest(LIB_SAFE, 64, 36), _source16())
    done = p.export("png16", "whole", full_frame=True)
    png = cv2.imread(str(p.out / "whole.png"), cv2.IMREAD_UNCHANGED)[..., ::-1]
    assert np.array_equal(png, p.take)
    x, y, w, h = LIB_SAFE
    rec = {"full_frame": True, "safe_frame": {"x": x, "y": y, "width": w, "height": h}}
    assert done.framing.record() == rec
    block = p.block("whole")
    assert block["full_frame"] is True and block["safe_frame"] == rec["safe_frame"] and "crop" not in block


def test_one_sidecar_is_one_framing(tmp_path):
    p = Project(tmp_path, _manifest(LIB_SAFE, 64, 36), _source16())
    p.export("png16", "take")
    with pytest.raises(export.NotExportable, match="One sidecar is one framing"):
        p.export("exr", "take", full_frame=True)
    assert not (p.out / "take.exr").exists() and p.block("take")["files"] == ["take.png"]
    p.export("exr", "take")  # the same framing shares the sidecar
    assert p.block("take")["files"] == ["take.png", "take.exr"]


def test_a_packet_without_a_safe_frame_is_unchanged(tmp_path):
    p = Project(tmp_path, {"colour": {"working_space": "lin_rec709"}}, _source16())
    done = p.export("png16", "take")
    png = cv2.imread(str(p.out / "take.png"), cv2.IMREAD_UNCHANGED)[..., ::-1]
    assert np.array_equal(png, p.take) and done.framing.record() == {}
    assert not {"crop", "full_frame", "safe_frame"} & set(p.block("take"))


def test_a_scaled_take_gets_the_rectangle_scaled_inward(tmp_path):
    p = Project(tmp_path, _manifest(LIB_SAFE, 64, 36), _source16(128, 72))
    done = p.export("png16", "take")
    assert done.framing.crop == (4, 10, 120, 52)
    png = cv2.imread(str(p.out / "take.png"), cv2.IMREAD_UNCHANGED)[..., ::-1]
    assert np.array_equal(png, p.take[10:62, 4:124])


def test_a_take_of_another_aspect_is_refused_unless_full_frame(tmp_path):
    p = Project(tmp_path, _manifest(LIB_SAFE, 64, 36), _source16(48, 48))
    with pytest.raises(export.SafeFrameError) as e:
        p.export("png16", "take")
    assert e.value.code == "SAFE_FRAME_MISMATCH" and not p.out.exists()
    done = p.export("png16", "take", full_frame=True)
    assert done.framing.record() == {"full_frame": True}
    assert cv2.imread(str(done.path), cv2.IMREAD_UNCHANGED).shape == (48, 48, 3)


def test_an_unreadable_manifest_is_refused_unless_full_frame(tmp_path):
    p = Project(tmp_path, None, _source16())  # registered, but the folder holds no manifest
    with pytest.raises(export.SafeFrameError) as e:
        p.export("png16", "take")
    assert e.value.code == "PACKET_UNREADABLE" and not p.out.exists()
    assert p.export("png16", "take", full_frame=True).framing.record() == {"full_frame": True}


def test_a_run_whose_packet_the_index_does_not_know_is_unchanged(tmp_path):
    p = Project(tmp_path, _manifest(LIB_SAFE, 64, 36), _source16())
    run = p.run.model_copy(update={"packet_id": "pk-elsewhere"})
    done = export.export(run, "png16", p.out, conn=p.conn, cas=p.cas, name="take")
    assert done.framing.record() == {}
    assert cv2.imread(str(done.path), cv2.IMREAD_UNCHANGED).shape == (36, 64, 3)


# ---------------------------------------------------------------------------- the routes on room_a


class EchoEngine(FakeEngine):
    """A FakeEngine that hands back the PNG it was sent (the gates pass; the composite is the plate)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent: dict[str, bytes] = {}

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        self.sent[job] = base64.b64decode(payload["inputs"]["image"]["b64"])
        return job

    def fetch(self, result_url):
        super().fetch(result_url)
        return self.sent[result_url[len(RESULT_SCHEME) :]]


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    return Settings.load()


def _client(settings: Settings) -> TestClient:
    c = TestClient(create_app(settings, EchoEngine()), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


def _setup(c: TestClient, auth: dict, nodes: tuple[str, ...] = ("e", "e2")) -> str:
    """Project + room_a ingested + Plate -> an Edit per name in `nodes` on the fake engine. The packet id."""
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["camera"]["safe_frame"] == BAND_DICT  # the fixture this file is about
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
    r = c.post("/api/v0/packets", json=body, headers=hdr)
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    graph = [{"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"}]
    graph += [{"id": n, "kind": "Edit", "label": n, "params": {"engine": "fake"}} for n in nodes]
    edges = [{"id": f"p->{n}", "source": "p", "target": n} for n in nodes]
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json={"nodes": graph, "edges": edges}, headers=auth)
    assert r.status_code == 200, r.text
    return packet_id


def _run(c: TestClient, auth: dict, node: str) -> dict:
    r = c.post(f"/api/v0/projects/{PROJECT}/nodes/{node}/quote", json={}, headers=auth)
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]
    r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=auth)
    assert r.status_code == 200, r.text
    run = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
    assert run["state"] == "DONE", run.get("error")
    return run


def _forge(c: TestClient, packet_id: str, out_key: str, *, node: str) -> str:
    """A DONE run of `node` whose take is `out_key` in the project's CAS, with a stored passing gate report."""
    index = c.app.state.index
    project_dir = c.app.state.pipeline.project_dir(PROJECT)
    now = utcnow()
    with index.project(PROJECT) as conn:
        spec = RunCreate(
            project_id=PROJECT,
            packet_id=packet_id,
            node_id=node,
            inputs={"take": out_key},
            params={},
            route=FAKE_ROUTE,
            attempt=1,
        )
        run = create_run(conn, spec, now=now)
        index.remember(run.id, PROJECT)
        q = Quote(
            run_id=run.id,
            provider="fake",
            model="fake",
            region="local",
            custody_grade="C0",
            price_usd=0.0,
            eta_s=0.0,
            created_utc=now,
            expires_utc=now + QUOTE_TTL,
        )
        with db.tx(conn):
            insert_quote(conn, q)
            transition(conn, run.id, S.QUOTED, actor="test", quote_id=q.id, quote_json=q.terms_json())
        transition(conn, run.id, S.APPROVED, actor="human", approved_by="human")
        transition(conn, run.id, S.QUEUED, actor="test")
        transition(conn, run.id, S.STAGING, actor="test")
        annotate(conn, run.id, expect=S.STAGING, provider_job_id=f"job-{run.id}")
        transition(conn, run.id, S.SUBMITTED, actor="test")
        transition(conn, run.id, S.RUNNING, actor="test")
        transition(conn, run.id, S.FETCHING, actor="test", result_url="fake://take")
        transition(conn, run.id, S.NORMALIZING, actor="test", result_key=out_key)
        transition(conn, run.id, S.GATING, actor="test", normalized={"outputs": [out_key]})
        report = {
            "gate_version": "0.1.0",
            "metric_code_hash": "ab" * 32,
            "threshold_set_id": "test",
            "verdict": "pass",
            "checks": [{"metric_id": "texture.lapvar", "value": 412.0, "status": "pass"}],
        }
        path = Service(project_dir).write(run.id, report)
        report["report_path"] = Path(path).relative_to(project_dir).as_posix()
        transition(conn, run.id, S.DONE, actor="test", gate_report=report)
    return run.id


def _clip(path: Path, size: str, seconds: int = 1) -> Path:
    """An untagged H.264 test clip at 24 fps."""
    tools.run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={size}:rate=24:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(path),
        ]
    )
    return path


def _records(settings: Settings, kind: str, run_id: str) -> list[dict]:
    led = Ledger(settings.projects_dir / PROJECT)
    assert led.verify() == (True, None)
    return [r for r in led.records() if r["kind"] == kind and r.get("run_id") == run_id]


def _composite(settings: Settings, run: dict) -> np.ndarray:
    return decode_png16(CAS(settings.projects_dir / PROJECT / "assets").read_bytes(run["outputs"][0]))


def _png16(path: Path) -> np.ndarray:
    return np.ascontiguousarray(cv2.imread(str(path), cv2.IMREAD_UNCHANGED)[..., ::-1])


@needs_room_a
def test_batch_export_of_room_a_writes_the_band_and_the_full_frame_on_request(sync_settings, auth, tmp_path):
    with _client(sync_settings) as c:
        _setup(c, auth)
        a, b = _run(c, auth, "e"), _run(c, auth, "e2")
        url = f"/api/v0/projects/{PROJECT}/export"
        out = tmp_path / "batch"
        r = c.post(url, json={"run_ids": [a["id"]], "out_dir": str(out), "format": "png16"}, headers=auth)
        assert r.status_code == 200, r.text
        (entry,) = r.json()["exported"]
        assert entry["crop"] == BAND_DICT and "full_frame" not in entry
        img = _png16(Path(entry["path"]))
        assert img.shape == (268, 640, 3) and np.array_equal(img, _composite(sync_settings, a)[46:314])
        assert json.loads(Path(entry["sidecar"]).read_text(encoding="utf-8"))["export"]["crop"] == BAND_DICT
        (rec,) = _records(sync_settings, "export", a["id"])
        assert rec["data"]["batch"] is True and rec["data"]["crop"] == BAND_DICT

        body = {"run_ids": [b["id"]], "out_dir": str(out), "format": "png16", "full_frame": True}
        r = c.post(url, json=body, headers=auth)
        assert r.status_code == 200, r.text
        (entry,) = r.json()["exported"]
        assert entry["full_frame"] is True and entry["safe_frame"] == BAND_DICT and "crop" not in entry
        assert np.array_equal(_png16(Path(entry["path"])), _composite(sync_settings, b))
        block = json.loads(Path(entry["sidecar"]).read_text(encoding="utf-8"))["export"]
        assert block["full_frame"] is True and block["safe_frame"] == BAND_DICT
        (rec,) = _records(sync_settings, "export", b["id"])
        assert rec["data"]["full_frame"] is True and "crop" not in rec["data"]

        r = c.post(url, json={**body, "full_frame": "maybe"}, headers=auth)
        assert r.status_code == 422


@needs_room_a
def test_the_single_export_route_writes_the_band_by_default(sync_settings, auth, tmp_path):
    with _client(sync_settings) as c:
        _setup(c, auth, nodes=("e",))
        run = _run(c, auth, "e")
        out = tmp_path / "single"
        r = c.post(
            f"/api/v0/runs/{run['id']}/export",
            json={"out_dir": str(out), "format": "tiff16", "name": "take"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        tif = imageio.read(out / "take.tif")
        assert tif.rgb.shape == (268, 640, 3)
        assert json.loads((out / "take.gate.json").read_text(encoding="utf-8"))["export"]["crop"] == BAND_DICT


@needs_room_a
def test_deliver_a_still_as_exr_writes_the_band_and_the_full_frame_on_request(sync_settings, auth, tmp_path):
    with _client(sync_settings) as c:
        _setup(c, auth, nodes=("e",))
        run = _run(c, auth, "e")
        url = f"/api/v0/runs/{run['id']}/deliver"
        out = tmp_path / "delivery"
        r = c.post(url, json={"format": "exr_sequence", "out_dir": str(out), "name": "band"}, headers=auth)
        assert r.status_code == 200, r.text
        assert r.json() == {
            "path": str(out / "band.0001.exr"),
            "sidecar": str(out / "band.gate.json"),
            "format": "exr_sequence",
        }
        assert _exr_size(out / "band.0001.exr") == (640, 268)
        side = json.loads((out / "band.gate.json").read_text(encoding="utf-8"))
        assert side["crop"] == BAND_DICT and side["lineage"]["run_id"] == run["id"]
        assert side["files"][0]["name"] == "band.0001.exr"
        (rec,) = _records(sync_settings, "export", run["id"])
        assert rec["data"]["delivery"] is True and rec["data"]["crop"] == BAND_DICT

        body = {"format": "exr_sequence", "out_dir": str(out), "name": "whole", "full_frame": True}
        r = c.post(url, json=body, headers=auth)
        assert r.status_code == 200, r.text
        assert _exr_size(out / "whole.0001.exr") == (640, 360)
        side = json.loads((out / "whole.gate.json").read_text(encoding="utf-8"))
        assert side["full_frame"] is True and side["safe_frame"] == BAND_DICT and "crop" not in side
        # the same pixels: the band is rows 46..313 of the whole frame, nothing resampled
        assert np.array_equal(_exr_pixels(out / "band.0001.exr"), _exr_pixels(out / "whole.0001.exr")[46:314])


@needs_room_a
@needs_ffmpeg
def test_deliver_a_clip_crops_prores_hevc_and_exr_frames_and_the_timeline_keeps_the_take(
    sync_settings, auth, tmp_path
):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth, nodes=("e",))
        key = c.app.state.cas_for(PROJECT).put_file(_clip(tmp_path / "take.mp4", "640x360"), "mp4")
        run_id = _forge(c, packet_id, key, node="motion")
        out = tmp_path / "delivery"

        def deliver(fmt: str, name: str, **extra):
            body = {"format": fmt, "out_dir": str(out), "name": name, **extra}
            return c.post(f"/api/v0/runs/{run_id}/deliver", json=body, headers=auth)

        for fmt, name in (("prores4444", "p"), ("hevc", "h")):
            r = deliver(fmt, name)
            assert r.status_code == 200, (fmt, r.text)
            got = tools.probe_video(Path(r.json()["path"]))
            assert (got["width"], got["height"]) == (640, 268), fmt
            assert got["color_primaries"] == "bt709" and got["fps"] == 24
            side = json.loads(Path(r.json()["sidecar"]).read_text(encoding="utf-8"))
            assert side["crop"] == BAND_DICT and side["color"]["verified"] is True
        r = deliver("hevc", "h_full", full_frame=True)
        assert r.status_code == 200, r.text
        assert tools.probe_video(Path(r.json()["path"]))["width"] == 640
        assert tools.probe_video(Path(r.json()["path"]))["height"] == 360

        r = deliver("exr_sequence", "x")
        assert r.status_code == 200, r.text
        band = sorted(out.glob("x.*.exr"))
        assert len(band) == 24 and _exr_size(band[0]) == (640, 268)
        r = deliver("exr_sequence", "xf", full_frame=True)
        assert r.status_code == 200, r.text
        whole = sorted(out.glob("xf.*.exr"))
        assert np.array_equal(_exr_pixels(band[5]), _exr_pixels(whole[5])[46:314])
        assert not list((sync_settings.home / "cache" / "deliver").glob("*"))  # the decoded frames are gone

        r = deliver("fcpxml", "t")  # a timeline writes no pixel: it carries the whole take and says so
        assert r.status_code == 200, r.text
        side = json.loads(Path(r.json()["sidecar"]).read_text(encoding="utf-8"))
        assert side["full_frame"] is True and side["safe_frame"] == BAND_DICT
        r = deliver("av1_retag", "a")  # an H.264 take has nothing to re-tag, framed or not
        assert r.status_code == 422 and r.json()["error"] == "delivery_error", r.text

        records = {r["data"]["dest_name"]: r["data"] for r in _records(sync_settings, "export", run_id)}
        assert records["h.mp4"]["crop"] == BAND_DICT and records["h_full.mp4"]["full_frame"] is True
        assert records["t.fcpxml"]["full_frame"] is True


@needs_room_a
@needs_ffmpeg
def test_deliver_a_scaled_clip_scales_the_band_and_refuses_another_aspect(sync_settings, auth, tmp_path):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth, nodes=("e",))
        cas = c.app.state.cas_for(PROJECT)
        scaled = _forge(c, packet_id, cas.put_file(_clip(tmp_path / "s.mp4", "1280x720"), "mp4"), node="m1")
        square = _forge(c, packet_id, cas.put_file(_clip(tmp_path / "q.mp4", "360x360"), "mp4"), node="m2")
        out = tmp_path / "delivery"

        r = c.post(
            f"/api/v0/runs/{scaled}/deliver", json={"format": "hevc", "out_dir": str(out)}, headers=auth
        )
        assert r.status_code == 200, r.text
        got = tools.probe_video(Path(r.json()["path"]))
        assert (got["width"], got["height"]) == (1280, 536)  # the band at 2x: y 92, height 536
        side = json.loads(Path(r.json()["sidecar"]).read_text(encoding="utf-8"))
        assert side["crop"] == {"x": 0, "y": 92, "width": 1280, "height": 536}

        body = {"format": "hevc", "out_dir": str(out)}
        r = c.post(f"/api/v0/runs/{square}/deliver", json=body, headers=auth)
        assert r.status_code == 422 and r.json()["error"] == "safe_frame_mismatch", r.text
        assert "full_frame" in r.json()["detail"] and not (out / f"{square}.mp4").exists()
        assert _records(sync_settings, "export", square) == []
        r = c.post(f"/api/v0/runs/{square}/deliver", json={**body, "full_frame": True}, headers=auth)
        assert r.status_code == 200, r.text
        assert tools.probe_video(Path(r.json()["path"]))["width"] == 360
