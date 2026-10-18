"""Video import through the external escape hatch (router plan T87) on the committed Blender packet `room_a`.

A clip made from the plate frame by `plate_gates.video.write_test_clip` is imported like a Higgsfield or Runway
take: custody, quote, approval, the CAS, the video gate assembly, the ledger. `import_file` is called directly for
the keyword-only arguments the route does not carry yet (`approved_still_hash`, `end_still_hash`,
`motion_packet_id`). ffmpeg-dependent tests skip when the tool or the encoder is absent. No client data is read.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest
from blake3 import blake3
from fastapi.testclient import TestClient

from plate_daemon import imports, lineage
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.delivery import guard, tools
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.ledger import Ledger
from plate_daemon.pipeline import PipelineError, encode_png16
from plate_gates.video import write_test_clip

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
DOLLY = ROOT / "fixtures" / "packets" / "dolly_12f"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
FPS = 24

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    return Settings.load()


def _need_ffmpeg(*encoders: str) -> None:
    try:
        tools.ffmpeg_path()
        tools.ffprobe_path()
    except guard.ToolMissing:
        pytest.skip("ffmpeg / ffprobe not installed")
    listing = subprocess.run(
        [tools.ffmpeg_path(), "-hide_banner", "-encoders"], capture_output=True, check=False
    ).stdout.decode("utf-8", "replace")
    for enc in encoders:
        if not re.search(rf"\s{re.escape(enc)}\s", listing):
            pytest.skip(f"ffmpeg has no {enc} encoder")


def _client(settings: Settings) -> TestClient:
    c = TestClient(create_app(settings, FakeEngine()), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


def _ingest(c: TestClient, auth: dict, packet_dir: Path) -> str:
    manifest = json.loads((packet_dir / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    body = {"packet_path": str(packet_dir), "sender": {"name": "test", "version": "0"}}
    r = c.post("/api/v0/packets", json=body, headers=hdr)
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    assert c.get(f"/api/v0/packets/{packet_id}", headers=auth).json()["status"] == "ingested"
    return packet_id


def _setup(c: TestClient, auth: dict, *, nda: bool = False) -> str:
    """Project + room_a ingested + Plate -> Edit `e`. The packet id."""
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": nda}}, headers=auth)
    assert r.status_code == 201, r.text
    packet_id = _ingest(c, auth, ROOM_A)
    nodes = [
        {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
        {"id": "e", "kind": "Edit", "label": "e", "params": {"engine": "fake"}},
    ]
    edges = [{"id": "p->e", "source": "p", "target": "e"}]
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json={"nodes": nodes, "edges": edges}, headers=auth)
    assert r.status_code == 200, r.text
    return packet_id


def _plate16() -> np.ndarray:
    img = cv2.imread(str(ROOM_A / "display.tif"), cv2.IMREAD_UNCHANGED)
    assert img is not None and img.dtype == np.uint16
    return np.ascontiguousarray(img[..., :3][..., ::-1])


def _plate8() -> np.ndarray:
    return ((_plate16().astype(np.uint32) + 128) // 257).astype(np.uint8)


def _clip(
    path: Path, frame: np.ndarray | None = None, *, seconds: int = 2, size: tuple[int, int] | None = None
):
    """A `seconds`-long 24 fps clip of one frame (the plate's, unless `frame` is given), `size` = (w, h)."""
    img = _plate8() if frame is None else frame
    if size is not None and (img.shape[1], img.shape[0]) != size:
        img = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
    return write_test_clip(path, [img] * (seconds * FPS), FPS)


def _import(c: TestClient, auth: dict, **body):
    return c.post(f"/api/v0/projects/{PROJECT}/import", json=body, headers=auth)


def _records(settings: Settings, kind: str, run_id: str) -> list[dict]:
    led = Ledger(settings.projects_dir / PROJECT)
    assert led.verify() == (True, None)
    return [r for r in led.records() if r["kind"] == kind and r.get("run_id") == run_id]


def _cas_count(c: TestClient) -> int:
    return len(list(c.app.state.cas_for(PROJECT).iter_objects()))


def _runs(c: TestClient, auth: dict) -> list:
    return c.get(f"/api/v0/projects/{PROJECT}/runs", headers=auth).json()


def _remux(src: Path, dst: Path) -> Path:
    tools.run_ffmpeg(["-i", str(src), "-c", "copy", str(dst)])
    return dst


# ---------------------------------------------------------------------------- the happy path


@needs_room_a
@pytest.mark.parametrize("container", ["mp4", "mov"])
def test_a_clip_of_the_plate_is_done_with_a_video_gate_report(sync_settings, auth, tmp_path, container):
    _need_ffmpeg()
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        src = _clip(tmp_path / "take.mp4")
        if container == "mov":
            src = _remux(src, tmp_path / "take.mov")
        r = _import(
            c, auth, path=str(src), packet_id=packet_id, source="manual", cost_usd=1.5, note="vendor take"
        )
        assert r.status_code == 201, r.text
        run = r.json()
        run_id = run["id"]
        assert run["state"] == "DONE", run.get("error") or run.get("reason")
        assert run["exportable"] is True and run["node_id"] is None
        assert run["route"]["engine_id"] == "import:manual" and run["route"]["custody_grade"] == "C0"
        assert run["quote"]["price_usd"] == 1.5 and run["quote"]["price_status"] == "declared"

        raw_key = blake3(src.read_bytes()).hexdigest()
        imported = run["inputs"]["imported"]
        assert imported == {
            "path_name": src.name,
            "source": "manual",
            "note": "vendor take",
            "blake3": raw_key,
            "media": "video",
            "approved_still": None,
            "end_still": None,
            "motion_packet_id": None,
        }
        assert "mask" not in run["inputs"] and run["inputs"]["staging"]["media"] == "video"

        norm = run["normalized"]
        assert norm["media"] == "video" and norm["raw_key"] == raw_key == run["result_key"]
        probe = norm["probe"]
        assert (probe["width"], probe["height"], probe["fps"], probe["frames"]) == (640, 360, "24/1", 2 * FPS)
        tm = norm["timemap"]
        assert tm["packet_fps"] == tm["engine_fps"] == "24/1" and tm["engine_frames"] == 2 * FPS
        assert norm["start_still"] == run["inputs"]["plate"] and norm["end_still"] is None

        cas = c.app.state.cas_for(PROJECT)
        assert cas.find(raw_key).suffix == f".{container}"
        out_key = run["outputs"][0]
        out = cas.find(out_key)
        assert out_key != raw_key and out.suffix == f".{container}" and norm["lineage"] is True
        lin = lineage.read(out)
        assert lin is not None and lin.run_id == run_id and lin.packet_id == packet_id
        assert lin.engine_id == "import:manual" and lin.gate_report == f"gates/{run_id}.1.json"

        report = run["gate_report"]
        assert report["verdict"] == "pass" and report["landing"]["first"] <= 12
        assert report["landing"]["last"] is None  # no end still named: the last frame is not scored
        assert report["timemap"] == tm and report["report_path"] == f"gates/{run_id}.1.json"
        gate = c.get(f"/api/v0/runs/{run_id}/gate", headers=auth).json()
        assert gate["n"] == 1 and gate["verdict"] != "fail"
        assert gate["metrics"]["output"]["width"] == 640 and gate["metrics"]["output"]["nb_frames"] == 2 * FPS
        assert not any(ch["metric_id"] == "video.duration_s" for ch in gate["checks"])  # no declared shot

        (quoted,) = _records(sync_settings, "quoted", run_id)
        assert quoted["data"]["imported"] is True and quoted["data"]["media"] == "video"
        assert quoted["cost_usd_quoted"] == 1.5 and quoted["engine_id"] == "import:manual"
        (approved,) = _records(sync_settings, "approved", run_id)
        assert approved["data"]["media"] == "video" and approved["by"] == "human"
        (done,) = _records(sync_settings, "done", run_id)
        assert done["data"]["imported"] is True and done["data"]["media"] == "video"
        assert done["data"]["outputs"] == [out_key]
        assert _records(sync_settings, "submitted", run_id) == []

        # a clip is not scored again like an Edit still
        r = c.post(f"/api/v0/runs/{run_id}/gate/rescore", json={}, headers=auth)
        assert r.status_code == 409 and r.json()["error"] == "not_rescorable", r.text

        # the same clip again is the next attempt of the same request
        r = _import(
            c, auth, path=str(src), packet_id=packet_id, source="manual", cost_usd=1.5, note="vendor take"
        )
        assert r.status_code == 201 and r.json()["attempt"] == 2 and r.json()["state"] == "DONE", r.text


@needs_room_a
def test_a_node_import_carries_its_subgraph(sync_settings, auth, tmp_path):
    _need_ffmpeg()
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        src = _clip(tmp_path / "take.mp4", seconds=1)
        r = _import(c, auth, path=str(src), packet_id=packet_id, node_id="e", source="manual")
        assert r.status_code == 201, r.text
        run = r.json()
        assert run["node_id"] == "e" and {n["id"] for n in run["subgraph"]["nodes"]} == {"p", "e"}
        assert run["state"] == "DONE"


@needs_room_a
def test_the_imported_clip_delivers_as_hevc(sync_settings, auth, tmp_path):
    _need_ffmpeg("libx265")
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        src = _clip(tmp_path / "take.mp4", seconds=1)
        run = _import(c, auth, path=str(src), packet_id=packet_id, source="manual").json()
        assert run["state"] == "DONE", run
        out = tmp_path / "delivery"
        r = c.post(
            f"/api/v0/runs/{run['id']}/deliver",
            json={"format": "hevc", "out_dir": str(out), "name": "shot_01"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        stream = tools.probe_video(out / "shot_01.mp4")
        assert stream["codec_name"] == "hevc" and stream["color_primaries"] == "bt709"
        sidecar = json.loads(Path(r.json()["sidecar"]).read_text(encoding="utf-8"))
        assert sidecar["lineage"]["run_id"] == run["id"] and sidecar["gate"]["verdict"] != "fail"


@needs_room_a
def test_a_webm_has_no_lineage_carrier_and_its_frames_are_counted(sync_settings, auth, tmp_path):
    _need_ffmpeg("libvpx")
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        webm = tmp_path / "take.webm"
        tools.run_ffmpeg(["-i", str(_clip(tmp_path / "take.mp4", seconds=1)), "-c:v", "libvpx", str(webm)])
        r = _import(c, auth, path=str(webm), packet_id=packet_id, source="manual")
        assert r.status_code == 201, r.text
        run = r.json()
        assert run["state"] in ("DONE", "GATE_FAIL"), run
        norm = run["normalized"]
        raw_key = blake3(webm.read_bytes()).hexdigest()
        assert norm["media"] == "video" and norm["lineage"] is False
        assert norm["outputs"] == [raw_key] == [run["result_key"]]
        assert norm["probe"]["frames"] == FPS and (norm["probe"]["width"], norm["probe"]["height"]) == (
            640,
            360,
        )
        assert c.app.state.cas_for(PROJECT).find(raw_key).suffix == ".webm"


# ---------------------------------------------------------------------------- the still hashes


@needs_room_a
def test_the_approved_and_end_stills_are_what_the_first_and_last_frames_are_held_to(
    sync_settings, auth, tmp_path
):
    _need_ffmpeg()
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        pl = c.app.state.pipeline
        cas = c.app.state.cas_for(PROJECT)
        plate_key = cas.put_bytes(encode_png16(_plate16()), "png")
        inverted_key = cas.put_bytes(encode_png16(65535 - _plate16()), "png")
        src = _clip(tmp_path / "take.mp4", seconds=1)

        run = imports.import_file(
            pl,
            PROJECT,
            packet_id,
            src,
            source="manual",
            approved_still_hash=plate_key,
            end_still_hash=plate_key,
        )
        assert run.state.value == "DONE"
        landing = run.gate_report["landing"]
        assert landing["first"] <= 12 and landing["last"] is not None and landing["last"] <= 16
        assert run.normalized["start_still"] == plate_key and run.normalized["end_still"] == plate_key
        assert run.inputs["imported"]["approved_still"] == plate_key
        assert run.inputs["imported"]["end_still"] == plate_key

        # a start still the clip does not start on: the first frame lands far from it
        far = imports.import_file(
            pl, PROJECT, packet_id, src, source="manual", approved_still_hash=inverted_key
        )
        assert far.gate_report["landing"]["first"] > 100 and far.gate_report["landing"]["last"] is None
        assert far.attempt == 1 and far.id != run.id  # a different request from the one above

        for kwargs, code, status in (
            ({"approved_still_hash": "ab" * 32}, "not_found", 404),
            ({"end_still_hash": "not-a-hash"}, "bad_still_hash", 422),
        ):
            with pytest.raises(PipelineError) as exc:
                imports.import_file(pl, PROJECT, packet_id, src, source="manual", **kwargs)
            assert (exc.value.code, exc.value.status) == (code, status)

        # the arguments are a clip's: a still refuses them
        with pytest.raises(PipelineError) as exc:
            imports.import_file(
                pl, PROJECT, packet_id, ROOM_A / "display.tif", source="manual", approved_still_hash=plate_key
            )
        assert exc.value.code == "not_a_clip" and exc.value.status == 422
        assert len(_runs(c, auth)) == 2  # the three refusals left no run


# ---------------------------------------------------------------------------- the motion packet


@needs_room_a
@pytest.mark.skipif(
    not (DOLLY / "manifest.json").is_file(), reason="fixtures/packets/dolly_12f not built (T2)"
)
def test_a_motion_packet_sets_the_size_the_clip_must_have(sync_settings, auth, tmp_path):
    _need_ffmpeg()
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        motion_id = _ingest(c, auth, DOLLY)
        pl = c.app.state.pipeline
        before = _cas_count(c)

        # the plate's size is not the sequence's size when a motion packet is named
        full = _clip(tmp_path / "full.mp4", seconds=1)
        with pytest.raises(PipelineError) as exc:
            imports.import_file(pl, PROJECT, packet_id, full, source="manual", motion_packet_id=motion_id)
        err = exc.value
        assert err.code == "size_mismatch" and err.status == 422
        assert err.extra["expected"] == [320, 180] and err.extra["sequence"] == [320, 180]
        assert err.extra["file"] == [640, 360] and err.extra["motion_packet_id"] == motion_id

        # ... and the sequence's size is not the plate's size without one
        small = _clip(tmp_path / "small.mp4", seconds=1, size=(320, 180))
        with pytest.raises(PipelineError) as exc:
            imports.import_file(pl, PROJECT, packet_id, small, source="manual")
        assert exc.value.code == "size_mismatch" and exc.value.extra["expected"] == [640, 360]

        for named, code, status in ((packet_id, "no_motion_packet", 422), ("pkt_absent", "not_found", 404)):
            with pytest.raises(PipelineError) as exc:
                imports.import_file(pl, PROJECT, packet_id, small, source="manual", motion_packet_id=named)
            assert (exc.value.code, exc.value.status) == (code, status)
        assert _runs(c, auth) == [] and _cas_count(c) == before

        # at the sequence's size the run goes ahead and the camera path is scored against the packet
        run = imports.import_file(pl, PROJECT, packet_id, small, source="manual", motion_packet_id=motion_id)
        assert run.state.value in ("DONE", "GATE_FAIL"), run.reason
        assert run.normalized["motion_packet_id"] == motion_id
        assert run.inputs["imported"]["motion_packet_id"] == motion_id
        stored = sync_settings.projects_dir / PROJECT / run.gate_report["report_path"]
        report = json.loads(stored.read_text(encoding="utf-8"))
        assert "drift" in report["metrics"]
        assert any("reprojection drift" in n or "drift reference" in n for n in report["backend_notes"])


# ---------------------------------------------------------------------------- refusals leave nothing behind


@needs_room_a
def test_clip_refusals_leave_no_run_and_no_object(sync_settings, auth, tmp_path, monkeypatch):
    _need_ffmpeg()
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth, nda=True)
        good = _clip(tmp_path / "good.mp4", seconds=1)
        before = _cas_count(c)

        # a Higgsfield or Runway clip is grade D: an NDA project refuses it before the file is read
        for source in ("higgsfield", "runway"):
            r = _import(c, auth, path=str(good), packet_id=packet_id, source=source)
            assert r.status_code == 403, r.text
            assert r.json()["error"] == "custody" and r.json()["grade"] == "D"

        small = _clip(tmp_path / "small.mp4", seconds=1, size=(320, 180))
        r = _import(c, auth, path=str(small), packet_id=packet_id, source="manual")
        assert r.status_code == 422 and r.json()["error"] == "size_mismatch", r.text
        assert r.json()["plate"] == [640, 360] and r.json()["file"] == [320, 180]

        junk = tmp_path / "junk.mp4"
        junk.write_bytes(b"not a video at all")
        r = _import(c, auth, path=str(junk), packet_id=packet_id, source="manual")
        assert r.status_code == 422 and r.json()["error"] == "unreadable_video", r.text
        r = _import(c, auth, path=str(tmp_path / "absent.mp4"), packet_id=packet_id, source="manual")
        assert r.status_code == 422 and r.json()["error"] == "unreadable_video"

        def no_ffprobe(_path):
            raise guard.ToolMissing("ffprobe not found")

        monkeypatch.setattr(imports, "probe_video", no_ffprobe)
        r = _import(c, auth, path=str(good), packet_id=packet_id, source="manual")
        assert r.status_code == 503 and r.json()["error"] == "tool_missing", r.text
        monkeypatch.undo()

        assert _runs(c, auth) == [] and _cas_count(c) == before
        assert not (sync_settings.projects_dir / PROJECT / "runs.jsonl").exists() or not any(
            rec["kind"] == "quoted" for rec in Ledger(sync_settings.projects_dir / PROJECT).records()
        )

        # a manual clip is C0: an NDA project takes it
        r = _import(c, auth, path=str(good), packet_id=packet_id, source="manual")
        assert r.status_code == 201 and r.json()["state"] == "DONE", r.text


# ---------------------------------------------------------------------------- what is a clip (router plan T137)


def _one_frame_h264(path: Path) -> Path:
    """A real 1-frame h264 mp4 (`ffmpeg -frames:v 1`), the attacker's second repro."""
    png = path.with_suffix(".src.png")
    assert cv2.imwrite(str(png), np.ascontiguousarray(_plate8()[..., ::-1]))
    tools.run_ffmpeg(["-i", str(png), "-frames:v", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)])
    return path


@needs_room_a
def test_a_png_named_mp4_is_a_still_with_a_clip_suffix(sync_settings, auth, tmp_path):
    _need_ffmpeg()
    still = tmp_path / "still.mp4"
    ok, buf = cv2.imencode(".png", np.ascontiguousarray(_plate8()[..., ::-1]))
    assert ok
    still.write_bytes(bytes(buf))
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        before = _cas_count(c)
        r = _import(c, auth, path=str(still), packet_id=packet_id, source="manual")
        assert r.status_code == 422 and r.json()["error"] == "unreadable_video", r.text
        assert "still image" in r.json()["detail"] and "clip suffix" in r.json()["detail"]
        assert _runs(c, auth) == [] and _cas_count(c) == before


@needs_room_a
@pytest.mark.parametrize("make", ["mpeg4", "h264"])
def test_a_clip_of_one_frame_is_refused(sync_settings, auth, tmp_path, make):
    if make == "h264":
        _need_ffmpeg("libx264")
        one = _one_frame_h264(tmp_path / "one.mp4")
    else:
        _need_ffmpeg()
        one = write_test_clip(tmp_path / "one.mp4", [_plate8()], FPS)
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        before = _cas_count(c)
        r = _import(c, auth, path=str(one), packet_id=packet_id, source="manual")
        assert r.status_code == 422 and r.json()["error"] == "unreadable_video", r.text
        assert "at least 2 frames" in r.json()["detail"] and "has 1" in r.json()["detail"]
        assert _runs(c, auth) == [] and _cas_count(c) == before


@needs_room_a
def test_a_clip_of_two_frames_is_the_shortest_that_imports(sync_settings, auth, tmp_path):
    _need_ffmpeg()
    two = write_test_clip(tmp_path / "two.mp4", [_plate8()] * 2, FPS)
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        r = _import(c, auth, path=str(two), packet_id=packet_id, source="manual")
        assert r.status_code == 201, r.text
        assert r.json()["state"] in {"DONE", "GATE_FAIL"}


@pytest.mark.parametrize(
    ("format_name", "still"),
    [
        ("png_pipe", True),
        ("jpeg_pipe", True),
        ("tiff_pipe", True),
        ("webp_pipe", True),
        ("image2", True),
        ("mov,mp4,m4a,3gp,3g2,mj2", False),
        ("matroska,webm", False),
        ("gif", False),
    ],
)
def test_ffprobes_format_name_decides_whether_a_container_is_a_still(format_name, still):
    assert imports._is_still_container(format_name) is still
